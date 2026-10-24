/* SPDX-License-Identifier: BSD-3-Clause */
#define _GNU_SOURCE
#include "common.h"

#if defined(__aarch64__)
#include <arm_neon.h>

#endif

static unsigned workers = 16, passes = 2;
static const char *mode = "read", *kernel_name = "scalar";
static unsigned array_mib;
static int initialize_before_switch, channel_phase_spread;
static pthread_barrier_t barrier;
static double *a, *b, *c;
static size_t elements;
static size_t worker_begin[16], worker_end[16];
static double sums[16], warmup_sums[16];

static void
complete_memory(void)
{
#if defined(__aarch64__)
    __asm__ volatile("dsb sy" : : : "memory");
#else
    __sync_synchronize();
#endif
}

static __attribute__((noinline)) double
neon_read(size_t begin, size_t end)
{
#if defined(__aarch64__)
    float64x2_t v0 = vdupq_n_f64(0), v1 = v0, v2 = v0, v3 = v0;
    float64x2_t v4 = v0, v5 = v0, v6 = v0, v7 = v0;
    const double *data = a;
    for (size_t j = begin; j < end; j += 16) {
#define LOAD_ADD(n)                                                           \
    do {                                                                      \
        float64x2_t value = vld1q_f64(data + j + 2 * (n));                    \
        __asm__ volatile("fadd %0.2d, %0.2d, %1.2d"                           \
                         : "+w"(v##n)                                         \
                         : "w"(value));                                       \
    } while (0)
        LOAD_ADD(0);
        LOAD_ADD(1);
        LOAD_ADD(2);
        LOAD_ADD(3);
        LOAD_ADD(4);
        LOAD_ADD(5);
        LOAD_ADD(6);
        LOAD_ADD(7);
#undef LOAD_ADD
    }
    return vaddvq_f64(v0) + vaddvq_f64(v1) + vaddvq_f64(v2) + vaddvq_f64(v3) +
           vaddvq_f64(v4) + vaddvq_f64(v5) + vaddvq_f64(v6) + vaddvq_f64(v7);
#else
    (void)begin;
    (void)end;
    abort(); /* CLI rejects NEON on non-AArch64 hosts. */
#endif
}

static double
kernel(unsigned id)
{
    size_t begin = worker_begin[id];
    size_t end = worker_end[id];
    double sum = 0;
    if (!strcmp(kernel_name, "neon")) {
        return neon_read(begin, end);
    }
    if (!strcmp(mode, "read")) {
        for (size_t j = begin; j < end; ++j) {
            sum += a[j];
        }
    } else if (!strcmp(mode, "copy")) {
        for (size_t j = begin; j < end; ++j) {
            b[j] = a[j];
        }
    } else {
        /* STREAM Triad operation; not an official STREAM submission. */
        for (size_t j = begin; j < end; ++j) {
            a[j] = b[j] + 3.0 * c[j];
        }
    }
    return sum;
}
static void *
run(void *arg)
{
    unsigned id = (uintptr_t)arg;
    pin(id);
    if (initialize_before_switch) {
        /* All workers must exist and be pinned before the global switch. */
        sync_workers(&barrier);
        if (!id) {
            complete_memory();
            marker_begin(UINT64_C(0x5a535743));
        }
        sync_workers(&barrier);
    }
    size_t begin = worker_begin[id];
    size_t end = worker_end[id];
    for (size_t j = begin; !initialize_before_switch && j < end; ++j) {
        a[j] = 1.0;
        if (b) {
            b[j] = 2.0;
        }
        if (c) {
            c[j] = 3.0;
        }
    }
    /* Check the read warmup sum so the compiler must retain its loads. */
    warmup_sums[id] = kernel(id);
    complete_memory();
    sync_workers(&barrier);
    if (!id) {
        complete_memory();
        marker_begin(0);
        complete_memory();
    }
    sync_workers(&barrier);
    for (unsigned p = 0; p < passes; ++p) {
        sums[id] += kernel(id);
        if (p + 1 == passes) {
            complete_memory();
        }
        /* Prevent repeated stores from collapsing across passes. */
        sync_workers(&barrier);
    }
    if (!id) {
        complete_memory();
        marker_end();
        complete_memory();
    }
    /* Keep other workers' thread teardown outside the measured interval. */
    sync_workers(&barrier);
    return NULL;
}
int
main(int argc, char **argv)
{
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--host")) {
            host = 1;
        } else if (!strcmp(argv[i], "--initialize-before-switch")) {
            initialize_before_switch = 1;
        } else if (!strcmp(argv[i], "--channel-phase-spread")) {
            channel_phase_spread = 1;
        } else if (!strcmp(argv[i], "--array-mib") && i + 1 < argc) {
            array_mib = number(argv[++i]);
        } else if (!strcmp(argv[i], "--kernel") && i + 1 < argc) {
            kernel_name = argv[++i];
        } else if (!strcmp(argv[i], "--workers") && i + 1 < argc) {
            workers = number(argv[++i]);
        } else if (!strcmp(argv[i], "--passes") && i + 1 < argc) {
            passes = number(argv[++i]);
        } else if (!strcmp(argv[i], "--mode") && i + 1 < argc) {
            mode = argv[++i];
        } else {
            fprintf(stderr,
                    "usage: %s [--host] [--workers 1|4|8|16 (12 with --host)] "
                    "[--passes 1|2|4|8] [--mode read|copy|stream] "
                    "[--kernel scalar|neon] [--array-mib N] "
                    "[--initialize-before-switch] [--channel-phase-spread]\n",
                    argv[0]);
            return 2;
        }
    }
    if ((workers != 1 && workers != 4 && workers != 8 && workers != 16 &&
         !(host && workers == 12)) ||
        (passes != 1 && passes != 2 && passes != 4 && passes != 8) ||
        (strcmp(mode, "read") && strcmp(mode, "copy") &&
         strcmp(mode, "stream"))) {
        return 2;
    }
    if ((strcmp(kernel_name, "scalar") && strcmp(kernel_name, "neon")) ||
        (!strcmp(kernel_name, "neon") && strcmp(mode, "read")) ||
        array_mib > 512 || (channel_phase_spread && strcmp(mode, "read"))) {
        return 2;
    }
#if !defined(__aarch64__)
    if (!strcmp(kernel_name, "neon")) {
        return 2;
    }
#endif
    if (!initialize_before_switch) {
        complete_memory();
        marker_begin(UINT64_C(0x5a535743));
    }
    size_t bytes =
        (array_mib ? array_mib : (!strcmp(mode, "read") ? 256 : 128)) *
        1024UL * 1024;
    unsigned arrays =
        !strcmp(mode, "read") ? 1 : (!strcmp(mode, "copy") ? 2 : 3);
    elements = bytes / sizeof(double);
    for (unsigned i = 0; i < workers; ++i) {
        /* Divide whole NEON iterations without dropping a 12-worker tail. */
        size_t phase = channel_phase_spread ? i * 8 : 0;
        worker_begin[i] = (elements / 16 * i / workers) * 16 + phase;
        worker_end[i] = (elements / 16 * (i + 1) / workers) * 16 + phase;
    }
    size_t allocated_bytes = bytes + (channel_phase_spread ? workers * 64 : 0);
    a = aligned_alloc(64, allocated_bytes);
    b = arrays > 1 ? aligned_alloc(64, bytes) : NULL;
    c = arrays > 2 ? aligned_alloc(64, bytes) : NULL;
    if (!a || (arrays > 1 && !b) || (arrays > 2 && !c)) {
        die("allocation (working set is never silently reduced)");
    }
    if (initialize_before_switch) {
        for (size_t j = 0; j < allocated_bytes / sizeof(double); ++j) {
            a[j] = 1.0;
            if (b) {
                b[j] = 2.0;
            }
            if (c) {
                c[j] = 3.0;
            }
        }
        complete_memory();
        puts("CMN_PROGRESS atomic_initialization_complete");
        fflush(stdout);
    }
    if (channel_phase_spread) {
        /* This phase step selects successive channels only at 64B stripes. */
        for (unsigned i = 0; i < workers; ++i) {
            printf("CMN_LAYOUT worker=%u begin=%p bytes=%zu\n", i,
                   (void *)(a + worker_begin[i]),
                   (worker_end[i] - worker_begin[i]) * sizeof(double));
        }
        fflush(stdout);
    }
    if (pthread_barrier_init(&barrier, NULL, workers)) {
        die("barrier_init");
    }
    pthread_t threads[16];
    for (unsigned i = 0; i < workers; ++i) {
        spawn(&threads[i], run, i);
    }
    for (unsigned i = 0; i < workers; ++i) {
        int rc = pthread_join(threads[i], NULL);
        if (rc) {
            errno = rc;
            die("pthread_join");
        }
    }
    int ok = 1;
    double checksum = 0, warmup_checksum = 0;
    for (unsigned i = 0; i < workers; ++i) {
        checksum += sums[i];
        warmup_checksum += warmup_sums[i];
    }
    if (!strcmp(mode, "read")) {
        for (unsigned i = 0; i < workers; ++i) {
            size_t count = worker_end[i] - worker_begin[i];
            ok &= sums[i] == (double)count * passes;
            ok &= warmup_sums[i] == (double)count;
        }
    } else {
        for (size_t j = 0; j < elements; ++j) {
            ok &= !strcmp(mode, "copy") ? b[j] == 1.0 : a[j] == 11.0;
        }
    }
    printf("CMN_RESULT {\"benchmark\":\"%s\",\"result\":\"%s\","
           "\"workers\":%u,\"passes\":%u,\"working_set_bytes\":%zu,"
           "\"useful_bytes\":%zu,\"warmup_passes\":1,"
           "\"checksum\":%.0f,\"warmup_checksum\":%.0f,\"kernel\":\"%s\","
           "\"initialization_before_"
           "switch\":%s,"
           "\"channel_phase_spread\":%s,\"allocated_bytes\":%zu,"
           "\"first_touch\":\"%s\",\"affinity\":\"%s\"}\n",
           mode, ok ? "PASS" : "FAIL", workers, passes, bytes * arrays,
           bytes * arrays * passes, checksum, warmup_checksum, kernel_name,
           initialize_before_switch ? "true" : "false",
           channel_phase_spread ? "true" : "false", allocated_bytes * arrays,
           initialize_before_switch ? "main-before-switch" : "pinned-worker",
           host ? "first-available-workers" : "0..workers-1");
    free(a);
    free(b);
    free(c);
    return !ok;
}
