/* SPDX-License-Identifier: BSD-3-Clause */
#define _GNU_SOURCE
#include "common.h"

#if defined(__aarch64__)
#include <arm_neon.h>

#endif

struct line
{
    struct line *next;
    unsigned char padding[56];
};
_Static_assert(sizeof(struct line) == 64, "requires 64-bit pointers");
static volatile uintptr_t observed;

static __attribute__((noinline)) float
arithmetic(unsigned mode, unsigned n)
{
#if defined(__aarch64__)
    float32x4_t a = vdupq_n_f32(0), b = a, c = a, d = a;
    float32x4_t e = a, f = a, g = a, h = a, one = vdupq_n_f32(1);
#define FMA(reg) "fmla %" #reg ".4s, %9.4s, %9.4s\n"
    if (!mode) {
        unsigned remaining = n;
        /* Keep the entire throughput loop in one fetch line. */
        if (remaining) {
            __asm__ volatile(".p2align 6\n1:\n" FMA(0) FMA(1) FMA(2) FMA(3)
                                 FMA(4) FMA(5) FMA(6)
                                     FMA(7) "subs %w8, %w8, #1\nb.ne 1b\n"
                             : "+w"(a), "+w"(b), "+w"(c), "+w"(d), "+w"(e),
                               "+w"(f), "+w"(g), "+w"(h), "+r"(remaining)
                             : "w"(one)
                             : "cc");
        }
        return vaddvq_f32(a) + vaddvq_f32(b) + vaddvq_f32(c) + vaddvq_f32(d) +
               vaddvq_f32(e) + vaddvq_f32(f) + vaddvq_f32(g) + vaddvq_f32(h);
    }
#undef FMA
#define FMA "fmla %0.4s, %1.4s, %1.4s\n"
#define ADD "fadd %0.4s, %0.4s, %1.4s\n"
    for (unsigned i = 0; i < n; ++i) {
        if (mode == 1) {
            __asm__ volatile(FMA FMA FMA FMA FMA FMA FMA FMA
                             : "+w"(a)
                             : "w"(one));
        } else {
            __asm__ volatile(ADD ADD ADD ADD ADD ADD ADD ADD
                             : "+w"(a)
                             : "w"(one));
        }
    }
#undef FMA
#undef ADD
    return vaddvq_f32(a);
#else
    (void)mode;
    return 32.0f * n;
#endif
}

static __attribute__((noinline)) float
load_only(const float *data)
{
#if defined(__aarch64__)
    float32x4_t a, b, c, d, e, f, g, h;
    for (unsigned pass = 0; pass < 4; ++pass) {
        const float *pointer = data;
        unsigned blocks = 256;
        /* A single fetch line isolates LSU throughput from fetch crossings. */
        __asm__ volatile(".p2align 6\n1:\n"
                         "ldr %q0, [%8]\n"
                         "ldr %q1, [%8, #16]\n"
                         "ldr %q2, [%8, #32]\n"
                         "ldr %q3, [%8, #48]\n"
                         "ldr %q4, [%8, #64]\n"
                         "ldr %q5, [%8, #80]\n"
                         "ldr %q6, [%8, #96]\n"
                         "ldr %q7, [%8, #112]\n"
                         "add %8, %8, #128\nsubs %w9, %w9, #1\nb.ne 1b\n"
                         : "=w"(a), "=w"(b), "=w"(c), "=w"(d), "=w"(e),
                           "=w"(f), "=w"(g), "=w"(h), "+r"(pointer),
                           "+r"(blocks)
                         :
                         : "memory", "cc");
    }
    return vaddvq_f32(a) + vaddvq_f32(b) + vaddvq_f32(c) + vaddvq_f32(d) +
           vaddvq_f32(e) + vaddvq_f32(f) + vaddvq_f32(g) + vaddvq_f32(h);
#else
    volatile float sink = 0;
    for (unsigned pass = 0; pass < 4; ++pass) {
        for (unsigned j = 0; j < 8192; ++j) {
            sink = data[j];
        }
    }
    return sink * 32;
#endif
}

static __attribute__((noinline)) struct line *
chase(struct line *p, unsigned n)
{
    for (unsigned i = 0; i < n; ++i) {
#if defined(__aarch64__)
        __asm__ volatile("ldr %0, [%0]" : "+r"(p) : : "memory");
#else
        p = *(struct line *volatile *)p;
#endif
    }
    return p;
}

static int
record(unsigned id, const char *level, const char *name, size_t size,
       uint64_t bytes, uint64_t flops, unsigned instructions, unsigned loads,
       unsigned warmup, uint64_t checksum, uint64_t expected, unsigned streams)
{
    int ok = checksum == expected;
    printf("ROOF_CASE {\"id\":%u,\"level\":\"%s\",\"kernel\":\"%s\","
           "\"working_set_bytes\":%zu,\"useful_bytes\":%llu,"
           "\"flops\":%llu,\"instructions\":%u,\"dependent_loads\":%u,"
           "\"warmup_loads\":%u,\"independent_streams\":%u,"
           "\"checksum\":%llu,\"expected\":%llu,"
           "\"result\":\"%s\"}\n",
           id, level, name, size, (unsigned long long)bytes,
           (unsigned long long)flops, instructions, loads, warmup, streams,
           (unsigned long long)checksum, (unsigned long long)expected,
           ok ? "PASS" : "FAIL");
    fflush(stdout);
    return ok;
}

static struct line *
pattern_ring(unsigned count, unsigned streams, unsigned mode)
{
    struct line *ring = aligned_alloc(64, count * sizeof(*ring));
    unsigned *order = malloc(count * sizeof(*order));
    if (!ring || !order) {
        die("pattern allocation");
    }
    memset(ring, 0, count * sizeof(*ring));
    unsigned length = count / streams;
    for (unsigned stream = 0; stream < streams; ++stream) {
        unsigned base = stream * length;
        for (unsigned i = 0; i < length; ++i) {
            order[i] = base + (mode == 1 ? (i * 17) % length : i);
        }
        if (mode == 2) {
            uint32_t rng = UINT32_C(0x12345678) + stream;
            for (unsigned i = length - 1; i; --i) {
                rng ^= rng << 13;
                rng ^= rng >> 17;
                rng ^= rng << 5;
                unsigned j = rng % (i + 1), tmp = order[j];
                order[j] = order[i];
                order[i] = tmp;
            }
        }
        for (unsigned i = 0; i < length; ++i) {
            ring[order[i]].next = &ring[order[(i + 1) % length]];
        }
        if (chase(ring + base, length) != ring + base) {
            exit(1);
        }
    }
    free(order);
    return ring;
}

static __attribute__((noinline)) unsigned
multi_chase(struct line *ring, unsigned streams)
{
    unsigned length = 4096 / streams;
    struct line *a = ring, *b = ring + (length % 4096);
    struct line *c = ring + ((2 * length) % 4096);
    struct line *d = ring + ((3 * length) % 4096);
    struct line *e = ring + ((4 * length) % 4096);
    struct line *f = ring + ((5 * length) % 4096);
    struct line *g = ring + ((6 * length) % 4096);
    struct line *h = ring + ((7 * length) % 4096);
#if defined(__aarch64__)
#define LOAD(reg) "ldr %" #reg ", [%" #reg "]\n"
    for (unsigned i = 0; i < length; ++i) {
        if (streams == 8) {
            __asm__ volatile(LOAD(0) LOAD(1) LOAD(2) LOAD(3) LOAD(4) LOAD(5)
                                 LOAD(6) LOAD(7)
                             : "+r"(a), "+r"(b), "+r"(c), "+r"(d), "+r"(e),
                               "+r"(f), "+r"(g), "+r"(h)
                             :
                             : "memory");
        } else if (streams == 4) {
            __asm__ volatile(LOAD(0) LOAD(1) LOAD(2) LOAD(3)
                             : "+r"(a), "+r"(b), "+r"(c), "+r"(d)
                             :
                             : "memory");
        } else if (streams == 2) {
            __asm__ volatile(LOAD(0) LOAD(1) : "+r"(a), "+r"(b) : : "memory");
        } else {
            __asm__ volatile(LOAD(0) : "+r"(a) : : "memory");
        }
    }
#undef LOAD
#else
    struct line *p[8] = {a, b, c, d, e, f, g, h};
    for (unsigned i = 0; i < length; ++i) {
        for (unsigned j = 0; j < streams; ++j) {
            p[j] = *(struct line *volatile *)p[j];
        }
    }
    a = p[0];
    b = p[1];
    c = p[2];
    d = p[3];
    e = p[4];
    f = p[5];
    g = p[6];
    h = p[7];
#endif
    struct line *ends[] = {a, b, c, d, e, f, g, h};
    unsigned correct = 0;
    for (unsigned i = 0; i < streams; ++i) {
        correct += ends[i] == ring + i * length;
    }
    return correct;
}

static int
memory_patterns(void)
{
    struct line *rings[7];
    for (unsigned i = 0; i < 7; ++i) {
        rings[i] = pattern_ring(i < 3 ? 16384 : 4096,
                                i < 3 ? 1 : 1U << (i - 3), i < 3 ? i : 2);
    }
    puts("ROOF_READY calibration=1 memory_patterns=1 atomic_prefault=1 "
         "active_cpu=0");
    fflush(stdout);
    marker_begin(UINT64_C(0x5a535743));
    int ok = 1;
    const char *names[] = {"sequential_lines", "stride17_lines",
                           "random_lines",     "random_streams1",
                           "random_streams2",  "random_streams4",
                           "random_streams8"};
    for (unsigned i = 0; i < 7; ++i) {
        unsigned count = i < 3 ? 16384 : 4096;
        unsigned streams = i < 3 ? 1 : 1U << (i - 3);
        if (i < 3) {
            observed = (uintptr_t)chase(rings[i], count);
        } else {
            observed = multi_chase(rings[i], streams);
        }
        marker_begin(201 + i);
        unsigned correct;
        if (i < 3) {
            correct = chase(rings[i], count) == rings[i];
        } else {
            correct = multi_chase(rings[i], streams);
        }
        marker_end();
        ok &= record(201 + i, i < 3 ? "L3_pattern" : "L2_MLP", names[i],
                     count * 64UL, count * 8UL, 0, count, count, count,
                     correct, streams, streams);
    }
    return ok;
}

static __attribute__((noinline)) uint64_t
independent_loads(const uint64_t *data, unsigned lines, unsigned streams,
                  unsigned window, unsigned loads)
{
    uint64_t last = 0;
    unsigned footprint = lines < loads ? lines : loads;
    unsigned partition = footprint / streams;
    unsigned per_stream = loads / streams;
    for (unsigned j = 0; j < per_stream; ++j) {
        for (unsigned stream = 0; stream < streams; ++stream) {
            unsigned line =
                (window + stream * partition + (j & (partition - 1))) &
                (lines - 1);
#if defined(__aarch64__)
            __asm__ volatile("ldr %0, [%1]"
                             : "=r"(last)
                             : "r"(data + line * 8)
                             : "memory");
#else
            last = *(const volatile uint64_t *)(data + line * 8);
#endif
        }
    }
    return last;
}

static int
load_streams(void)
{
    const unsigned lines[] = {512, 4096, 32768};
    const char *levels[] = {"L1", "L2", "L3"};
    uint64_t *buffers[3];
    for (unsigned k = 0; k < 3; ++k) {
        buffers[k] = aligned_alloc(64, lines[k] * 64UL);
        if (!buffers[k]) {
            die("independent load allocation");
        }
        for (unsigned j = 0; j < lines[k] * 8; ++j) {
            buffers[k][j] = 1;
        }
    }
    puts("ROOF_READY calibration=1 load_streams=1 atomic_prefault=1 "
         "active_cpu=0");
    fflush(stdout);
    marker_begin(UINT64_C(0x5a535743));
    int ok = 1;
    for (unsigned k = 0; k < 3; ++k) {
        observed = independent_loads(buffers[k], lines[k], 1, 0, lines[k]);
        for (unsigned n = 0; n < 5; ++n) {
            unsigned streams = 1U << n, id = 301 + 5 * k + n;
            unsigned window = (n * 2048) & (lines[k] - 1);
            marker_begin(id);
            uint64_t value =
                independent_loads(buffers[k], lines[k], streams, window, 2048);
            marker_end();
            ok &= record(id, levels[k], "independent_scalar_loads",
                         lines[k] * 64UL, 16384, 0, 2048, 0, n ? 0 : lines[k],
                         value, 1, streams);
        }
    }
    return ok;
}

/* Exact instruction diagnostics: stable operands, no memory inside the loop.
 */
static __attribute__((noinline)) float
execution(unsigned mode, unsigned n)
{
    if (mode < 2) {
        return arithmetic(mode, n);
    }
#if defined(__aarch64__)
    float32x4_t a = vdupq_n_f32(1), b = a, c = a, d = a;
    float32x4_t e = a, f = a, g = a, h = a;
    float32x4_t one = a, zero = vdupq_n_f32(0), two = vdupq_n_f32(2);
    if (mode == 2 || mode == 3) {
        for (unsigned i = 0; i < n; ++i) {
#define GENERAL "fmla %0.4s, %1.4s, %2.4s\nfmla %1.4s, %0.4s, %2.4s\n"
#define ALIAS "fmla %0.4s, %0.4s, %1.4s\n"
            if (mode == 2) {
                __asm__ volatile(GENERAL GENERAL GENERAL GENERAL
                                 : "+&w"(a), "+&w"(b)
                                 : "w"(zero));
            } else {
                __asm__ volatile(
                    ALIAS ALIAS ALIAS ALIAS ALIAS ALIAS ALIAS ALIAS
                    : "+&w"(a)
                    : "w"(zero));
            }
#undef GENERAL
#undef ALIAS
        }
        return vaddvq_f32(a);
    }
    if (mode == 4 || mode == 5) {
#define DIV(reg) "fdiv %" #reg ".4s, %8.4s, %9.4s\n"
#define CHAIN "fdiv %0.4s, %0.4s, %1.4s\n"
        for (unsigned i = 0; i < n; ++i) {
            if (mode == 4) {
                __asm__ volatile(DIV(0) DIV(1) DIV(2) DIV(3) DIV(4) DIV(5)
                                     DIV(6) DIV(7)
                                 : "=&w"(a), "=&w"(b), "=&w"(c), "=&w"(d),
                                   "=&w"(e), "=&w"(f), "=&w"(g), "=&w"(h)
                                 : "w"(one), "w"(two));
            } else {
                __asm__ volatile(
                    CHAIN CHAIN CHAIN CHAIN CHAIN CHAIN CHAIN CHAIN
                    : "+&w"(a)
                    : "w"(one));
            }
        }
#undef DIV
#undef CHAIN
        return mode == 5 ? vaddvq_f32(a)
                         : vaddvq_f32(a) + vaddvq_f32(b) + vaddvq_f32(c) +
                               vaddvq_f32(d) + vaddvq_f32(e) + vaddvq_f32(f) +
                               vaddvq_f32(g) + vaddvq_f32(h);
    }
    float sa, sb, sc, sd, se, sf, sg, sh, scalar_one = 1;
    a = zero;
#define SDIV(reg) "fdiv %s" #reg ", %s9, %s9\n"
#define VADD "fadd %8.4s, %10.4s, %10.4s\n"
    for (unsigned i = 0; i < n; ++i) {
        if (mode == 6) {
            __asm__ volatile(
                SDIV(0) SDIV(1) SDIV(2) SDIV(3) SDIV(4) SDIV(5) SDIV(6) SDIV(7)
                : "=&w"(sa), "=&w"(sb), "=&w"(sc), "=&w"(sd), "=&w"(se),
                  "=&w"(sf), "=&w"(sg), "=&w"(sh), "+&w"(a)
                : "w"(scalar_one), "w"(one));
        } else {
            __asm__ volatile(
                SDIV(0) VADD SDIV(1) VADD SDIV(2) VADD SDIV(3) VADD SDIV(4)
                    VADD SDIV(5) VADD SDIV(6) VADD SDIV(7) VADD
                : "=&w"(sa), "=&w"(sb), "=&w"(sc), "=&w"(sd), "=&w"(se),
                  "=&w"(sf), "=&w"(sg), "=&w"(sh), "+&w"(a)
                : "w"(scalar_one), "w"(one));
        }
    }
#undef SDIV
#undef VADD
    return sa + sb + sc + sd + se + sf + sg + sh + vaddvq_f32(a);
#else
    return mode < 4 || mode == 5 ? 4 : mode == 4 ? 16 : mode == 6 ? 8 : 16;
#endif
}

static __attribute__((noinline)) void
lsu_kernel(unsigned mode, uint64_t *data, uint64_t last[8], uint64_t value)
{
#if defined(__aarch64__)
    uint64x2_t a = vdupq_n_u64(value), b = a, c = a, d = a;
    for (unsigned pass = 0; pass < 8; ++pass) {
        for (unsigned block = 0; block < 8192 / 64; ++block) {
            uint64_t *ptr = data + block * 8;
            if (mode == 0) {
                __asm__ volatile("ldr %q0, [%4]\nldr %q1, [%4, #16]\n"
                                 "ldr %q2, [%4, #32]\nldr %q3, [%4, #48]\n"
                                 : "=&w"(a), "=&w"(b), "=&w"(c), "=&w"(d)
                                 : "r"(ptr)
                                 : "memory");
            } else if (mode == 1) {
                __asm__ volatile(
                    "ldp %q0, %q1, [%4]\nldp %q2, %q3, [%4, #32]\n"
                    : "=&w"(a), "=&w"(b), "=&w"(c), "=&w"(d)
                    : "r"(ptr)
                    : "memory");
            } else if (mode == 2) {
                __asm__ volatile(
                    "stp %q0, %q1, [%4]\nstp %q2, %q3, [%4, #32]\n"
                    :
                    : "w"(a), "w"(b), "w"(c), "w"(d), "r"(ptr)
                    : "memory");
            } else {
                __asm__ volatile(
                    "ldp %q0, %q1, [%2]\nstp %q0, %q1, [%2, #32]\n"
                    : "=&w"(a), "=&w"(b)
                    : "r"(ptr)
                    : "memory");
            }
        }
    }
    vst1q_u64(last, a);
    vst1q_u64(last + 2, b);
    vst1q_u64(last + 4, c);
    vst1q_u64(last + 6, d);
#else
    for (unsigned pass = 0; pass < 8; ++pass) {
        for (unsigned block = 0; block < 8192 / 64; ++block) {
            volatile uint64_t *ptr = data + block * 8;
            for (unsigned k = 0; k < 8; ++k) {
                if (mode == 2) {
                    ptr[k] = value;
                } else if (mode == 3 && k >= 4) {
                    ptr[k] = ptr[k - 4];
                } else {
                    last[k] = ptr[k];
                }
            }
        }
    }
    for (unsigned k = 0; k < 8; ++k) {
        last[k] = data[1024 - 8 + k];
    }
#endif
}

static int
lsu_checks(void)
{
    uint64_t *data = aligned_alloc(64, 8192), last[8];
    const char *names[] = {"ldr_q_l1", "ldp_q_l1", "stp_q_l1", "ldp_stp_q_l1"};
    if (!data) {
        die("LSU allocation");
    }
    int ok = 1;
    for (unsigned mode = 0; mode < 4; ++mode) {
        for (unsigned j = 0; j < 1024; ++j) {
            data[j] = 1;
        }
        lsu_kernel(mode, data, last, 1);
        /* Warm full footprint, then prepare values to detect ROI stores. */
        for (unsigned j = 0; j < 1024; ++j) {
            data[j] = mode == 2 || (mode == 3 && j % 8 >= 4) ? 0 : 1;
        }
        marker_begin(409 + mode);
        lsu_kernel(mode, data, last, mode == 2 ? 2 : 1);
        marker_end();
        uint64_t checksum = 0, expected = mode == 2 ? 2 : 1;
        for (unsigned j = 0; j < 1024; ++j) {
            ok &= data[j] == expected;
            checksum += data[j];
        }
        for (unsigned j = 0; j < 8; ++j) {
            ok &= last[j] == expected;
            checksum += last[j];
        }
        ok &= record(409 + mode, "L1", names[mode], 8192, 65536, 0,
                     mode == 0 ? 4096 : 2048, 0, 0, checksum, expected * 1032,
                     4);
    }
    free(data);
    return ok;
}

static int
execution_checks(void)
{
    const char *names[] = {
        "fmla_q_independent8",        "fmla_q_accumulator_chain",
        "fmla_q_general_alternating", "fmla_q_alias_chain",
        "fdiv_q_fp32_independent8",   "fdiv_q_fp32_chain",
        "fdiv_s_independent8",        "fdiv_s_mixed_fadd_q"};
    const unsigned n = 1024;
    puts("ROOF_READY execution_checks=1 active_cpu=0");
    fflush(stdout);
    marker_begin(UINT64_C(0x5a535743));
    int ok = 1;
    for (unsigned mode = 0; mode < 8; ++mode) {
        observed = execution(mode, 32);
        marker_begin(401 + mode);
        float value = execution(mode, n);
        marker_end();
        unsigned expected = mode < 2                ? 32 * n
                            : mode < 4 || mode == 5 ? 4
                            : mode == 4             ? 16
                            : mode == 6             ? 8
                                                    : 16;
        unsigned flops = mode < 4    ? 64 * n
                         : mode < 6  ? 32 * n
                         : mode == 6 ? 8 * n
                                     : 40 * n;
        ok &= record(401 + mode, "compute", names[mode], 0, 0, flops,
                     (mode == 7 ? 16 : 8) * n, 0, 0, value, expected,
                     mode == 0 || mode == 4 || mode >= 6 ? 8 : 1);
    }
    return lsu_checks() && ok;
}

int
main(int argc, char **argv)
{
    (void)number;
    (void)spawn;
    (void)sync_workers;
    int patterns = 0, independent = 0, execution_only = 0;
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--host")) {
            host = 1;
        } else if (!strcmp(argv[i], "--execution-checks")) {
            execution_only = 1;
        } else if (!strcmp(argv[i], "--memory-patterns")) {
            patterns = 1;
        } else if (!strcmp(argv[i], "--load-streams")) {
            independent = 1;
        } else {
            return 2;
        }
    }
    if (patterns + independent + execution_only > 1) {
        return 2;
    }
    pin(0);
    int ok;
    if (execution_only) {
        ok = execution_checks();
        goto done;
    }
    if (independent) {
        ok = load_streams();
        goto done;
    }
    if (patterns) {
        ok = memory_patterns();
        goto done;
    }
    const unsigned lines[] = {512, 4096, 32768, 65536};
    const unsigned steps[] = {4096, 4096, 1024, 1024};
    const char *levels[] = {"L1", "L2", "L3", "DRAM_cold"};
    struct line *rings[4], *expected[4];
    for (unsigned k = 0; k < 4; ++k) {
        rings[k] = aligned_alloc(64, lines[k] * sizeof(struct line));
        unsigned *order = malloc(lines[k] * sizeof(unsigned));
        if (!rings[k] || !order) {
            die("calibration allocation");
        }
        memset(rings[k], 0, lines[k] * sizeof(struct line));
        for (unsigned i = 0; i < lines[k]; ++i) {
            order[i] = i;
        }
        uint32_t rng = UINT32_C(0x12345678);
        for (unsigned i = lines[k] - 1; i; --i) {
            rng ^= rng << 13;
            rng ^= rng >> 17;
            rng ^= rng << 5;
            unsigned j = rng % (i + 1), t = order[j];
            order[j] = order[i];
            order[i] = t;
        }
        for (unsigned i = 0; i < lines[k]; ++i) {
            rings[k][order[i]].next = &rings[k][order[(i + 1) % lines[k]]];
        }
        expected[k] = chase(rings[k], steps[k]);
        if (chase(rings[k], lines[k]) != rings[k]) {
            return 1;
        }
        free(order);
    }
    float *data = aligned_alloc(64, 32768);
    if (!data) {
        die("load buffer allocation");
    }
    for (unsigned i = 0; i < 8192; ++i) {
        data[i] = 1;
    }
    puts("ROOF_READY calibration=1 atomic_prefault=1 active_cpu=0");
    fflush(stdout);
    marker_begin(UINT64_C(0x5a535743));
    ok = 1;
    const char *names[] = {"fma_independent8", "fma_chain", "fadd_chain"};
    for (unsigned mode = 0; mode < 3; ++mode) {
        observed = arithmetic(mode, 32);
        marker_begin(101 + mode);
        float value = arithmetic(mode, 2048);
        marker_end();
        ok &= record(101 + mode, "compute", names[mode], 0, 0,
                     2048 * 8 * (mode == 2 ? 4 : 8), 2048 * 8, 0, 0, value,
                     2048 * 32, 0);
    }
    observed = load_only(data);
    marker_begin(104);
    float value = load_only(data);
    marker_end();
    ok &= record(104, "L1", "load_only_ldr_q", 32768, 131072, 0, 8192, 0, 8192,
                 value, 32, 8);
    /* Cold DRAM precedes ring warmups; never infer residency from size alone.
     */
    const unsigned sequence[] = {3, 0, 1, 2};
    for (unsigned i = 0; i < 4; ++i) {
        unsigned k = sequence[i], warmup = k == 3 ? 0 : lines[k];
        if (warmup) {
            observed = (uintptr_t)chase(rings[k], warmup);
        }
        marker_begin(105 + k);
        struct line *last = chase(rings[k], steps[k]);
        marker_end();
        ok &= record(105 + k, levels[k], "random_pointer_chase",
                     lines[k] * 64UL, steps[k] * 8UL, 0, steps[k], steps[k],
                     warmup, last - rings[k], expected[k] - rings[k], 1);
    }
done:
    printf("ROOF_DONE=%s\n", ok ? "PASS" : "FAIL");
    fflush(stdout);
#if defined(__aarch64__)
    if (!host && ok) {
        register uint64_t x0 __asm__("x0") = 0;
        __asm__ volatile(".inst 0xff210110" : "+r"(x0) : : "memory");
    }
#endif
    return !ok;
}
