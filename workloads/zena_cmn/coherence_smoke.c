/* SPDX-License-Identifier: BSD-3-Clause */
#define _GNU_SOURCE
#include <stdatomic.h>
#include <sys/mman.h>

#include "common.h"

static unsigned workers = 8, iterations = 32;
static pthread_barrier_t barrier;
static _Alignas(64) uint64_t payload;
static _Alignas(64) atomic_uint flag;
static _Alignas(64) atomic_uint_fast64_t counter;
/* Independent atomic objects share lines deliberately. */
static atomic_uint_fast64_t false_sharing[16];
static uint64_t dirty_sum[16], reader_sum[16], eviction_sum[16];
static uint64_t translation_sum[16];
static uint64_t *eviction, *page, *backing;
static int backing_fd, prefault_before_switch;
static size_t page_size;
static size_t eviction_bytes = 64 * 1024 * 1024;
enum
{
    LINE_BYTES = 64,
    WORDS_PER_LINE = LINE_BYTES / sizeof(uint64_t)
};

static void *
run(void *arg)
{
    unsigned id = (uintptr_t)arg;
    pin(id);
    size_t count = eviction_bytes / sizeof(*eviction) / workers;
    uint64_t *local = eviction + id * count;
    /* One dirty word covers each cache line without eight redundant stores. */
    for (size_t j = 0; j < count; j += WORDS_PER_LINE) {
        local[j] = j + id;
        if (!id && j && !(j % (1024 * 1024 / sizeof(*local)))) {
            printf("CMN_PROGRESS warmup_worker0_bytes=%zu/%zu\n",
                   j * sizeof(*local), count * sizeof(*local));
            fflush(stdout);
        }
    }
    sync_workers(&barrier);
    if (!id) {
        puts("CMN_PROGRESS warmup_complete");
        fflush(stdout);
    }
    sync_workers(&barrier);
    if (!id) {
        marker_begin(0);
    }
    sync_workers(&barrier);

    /* Two publication pairs: same cluster (0,1), then farthest core. */
    for (unsigned pair = 0; pair < 2; ++pair) {
        unsigned reader = pair ? workers - 1 : 1;
        for (unsigned j = 1; j <= iterations; ++j) {
            if (!id) {
                while (atomic_load_explicit(&flag, memory_order_acquire))
                    ;
                payload = j;
                atomic_store_explicit(&flag, 1, memory_order_release);
            } else if (id == reader) {
                while (!atomic_load_explicit(&flag, memory_order_acquire))
                    ;
                dirty_sum[id] += payload;
                atomic_store_explicit(&flag, 0, memory_order_release);
            }
        }
        sync_workers(&barrier);
    }
    for (unsigned j = 1; j <= iterations; ++j) {
        if (!id) {
            payload = j;
        }
        sync_workers(&barrier);
        reader_sum[id] += payload;
        sync_workers(&barrier);
        atomic_fetch_add_explicit(&counter, 1, memory_order_relaxed);
        atomic_fetch_add_explicit(&false_sharing[id], 1, memory_order_relaxed);
    }
    /* Default working set exceeds combined DSU L3 and Home SLC capacity. */
    for (size_t j = 0; j < count; j += WORDS_PER_LINE) {
        local[j] ^= UINT64_C(0x12345678);
    }
    sync_workers(&barrier);
    for (size_t j = 0; j < count; j += WORDS_PER_LINE) {
        eviction_sum[id] += local[j];
    }
    sync_workers(&barrier);
    /* No worker dereferences the mapping while another changes it. */
    for (unsigned j = 1; j <= iterations; ++j) {
        if (!id) {
            if (mprotect(page, page_size, PROT_NONE) ||
                munmap(page, page_size)) {
                die("mprotect/munmap");
            }
            size_t offset = (j % 2) * page_size;
            backing[offset / sizeof(*backing)] = j;
            void *mapped = mmap(page, page_size, PROT_READ | PROT_WRITE,
                                MAP_SHARED | MAP_FIXED, backing_fd, offset);
            if (mapped == MAP_FAILED) {
                die("mmap");
            }
            if (mprotect(page, page_size, PROT_READ)) {
                die("mprotect read-only");
            }
        }
        sync_workers(&barrier);
        translation_sum[id] += page[0];
        sync_workers(&barrier);
    }
    if (!id) {
        marker_end();
    }
    return NULL;
}
int
main(int argc, char **argv)
{
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--host")) {
            host = 1;
        } else if (!strcmp(argv[i], "--prefault-before-switch")) {
            prefault_before_switch = 1;
        } else if (!strcmp(argv[i], "--workers") && i + 1 < argc) {
            workers = number(argv[++i]);
        } else if (!strcmp(argv[i], "--iterations") && i + 1 < argc) {
            iterations = number(argv[++i]);
        } else if (!strcmp(argv[i], "--eviction-mib") && i + 1 < argc) {
            unsigned mib = number(argv[++i]);
            if (mib != 1 && mib != 64) {
                return 2;
            }
            eviction_bytes = (size_t)mib * 1024 * 1024;
        } else {
            fprintf(stderr,
                    "usage: %s [--host] [--workers 2|4|8|16] "
                    "[--iterations N] [--eviction-mib 1|64] "
                    "[--prefault-before-switch]\n",
                    argv[0]);
            return 2;
        }
    }
    if (workers != 2 && workers != 4 && workers != 8 && workers != 16) {
        return 2;
    }
    long system_page_size = sysconf(_SC_PAGESIZE);
    if (system_page_size <= 0) {
        die("page size");
    }
    page_size = system_page_size;
    if (prefault_before_switch) {
        eviction = aligned_alloc(LINE_BYTES, eviction_bytes);
        if (!eviction) {
            die("prefault allocation");
        }
        puts("CMN_PROGRESS prefault_before_switch_begin");
        fflush(stdout);
        for (size_t j = 0; j < eviction_bytes / sizeof(*eviction);
             j += page_size / sizeof(*eviction)) {
            eviction[j] = 0;
        }
        eviction[eviction_bytes / sizeof(*eviction) - 1] = 0;
        puts("CMN_PROGRESS prefault_before_switch_complete");
        fflush(stdout);
    }
    marker_begin(UINT64_C(0x5a535743));
    backing_fd = memfd_create("cmn-tlbi", 0);
    if (backing_fd < 0 || ftruncate(backing_fd, 2 * page_size)) {
        die("TLBI backing allocation");
    }
    backing = mmap(NULL, 2 * page_size, PROT_READ | PROT_WRITE, MAP_SHARED,
                   backing_fd, 0);
    page = mmap(NULL, page_size, PROT_READ | PROT_WRITE, MAP_SHARED,
                backing_fd, 0);
    if (!eviction) {
        eviction = aligned_alloc(LINE_BYTES, eviction_bytes);
    }
    if (page == MAP_FAILED || backing == MAP_FAILED || !eviction) {
        die("allocation");
    }
    if (pthread_barrier_init(&barrier, NULL, workers)) {
        die("barrier_init");
    }
    printf("CMN_PROGRESS first_touch_begin footprint_bytes=%zu "
           "line_bytes=%u checked_bytes_per_line=%zu\n",
           eviction_bytes, LINE_BYTES, sizeof(*eviction));
    fflush(stdout);
    pthread_t threads[16];
    for (unsigned i = 0; i < workers; ++i) {
        spawn(&threads[i], run, i);
    }
    for (unsigned i = 0; i < workers; ++i) {
        pthread_join(threads[i], NULL);
    }
    puts("CMN_PROGRESS roi_complete_verification_begin");
    fflush(stdout);
    uint64_t expected = (uint64_t)iterations * (iterations + 1) / 2;
    int ok = atomic_load(&counter) == (uint64_t)workers * iterations;
    size_t count = eviction_bytes / sizeof(*eviction) / workers;
    for (unsigned i = 0; i < workers; ++i) {
        uint64_t sum = 0;
        for (size_t j = 0; j < count; j += WORDS_PER_LINE) {
            sum += (j + i) ^ UINT64_C(0x12345678);
        }
        unsigned pair_count = (i == 1) + (i == workers - 1);
        ok &= dirty_sum[i] == pair_count * expected;
        ok &= reader_sum[i] == expected;
        ok &= translation_sum[i] == expected;
        ok &= eviction_sum[i] == sum;
        ok &= atomic_load(&false_sharing[i]) == iterations;
    }
    printf("CMN_RESULT {\"benchmark\":\"coherence\",\"result\":\"%s\","
           "\"workers\":%u,\"iterations\":%u,\"useful_bytes\":0,"
           "\"eviction_bytes\":%zu,\"large_eviction\":%s,"
           "\"eviction_line_bytes\":%u,\"eviction_lines\":%zu,"
           "\"checked_bytes_per_line\":%zu,"
           "\"prefault_before_switch_marker\":%s,"
           "\"same_cluster\":true,\"cross_cluster\":%s,"
           "\"atomic_isa\":\"armv8-a LL/SC (host uses native atomics)\"}\n",
           ok ? "PASS" : "FAIL", workers, iterations, eviction_bytes,
           eviction_bytes >= 64 * 1024 * 1024 ? "true" : "false", LINE_BYTES,
           eviction_bytes / LINE_BYTES, sizeof(*eviction),
           prefault_before_switch ? "true" : "false",
           workers > 4 ? "true" : "false");
    munmap(page, page_size);
    munmap(backing, 2 * page_size);
    close(backing_fd);
    free(eviction);
    return !ok;
}
