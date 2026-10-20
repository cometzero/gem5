/* SPDX-License-Identifier: BSD-3-Clause */
#define _GNU_SOURCE
#include "common.h"

#if defined(__aarch64__)
#include <arm_neon.h>

#endif

#define KIB 1024UL
#define WINDOW (128 * KIB)
static volatile float observed;

/* Eight independent accumulators avoid a single reduction dependency chain.
 * Algorithmic FLOPs count FMA only; checksum and loop control are excluded. */
static __attribute__((noinline)) float
kernel(const float *data, size_t bytes, unsigned repeat, unsigned fmas)
{
#if defined(__aarch64__)
    float32x4_t a = vdupq_n_f32(0), b = a, c = a, d = a;
    float32x4_t e = a, f = a, g = a, h = a;
    float32x4_t one = vdupq_n_f32(1);
#define OP_FMA(dst, src)                                                      \
    __asm__ volatile("fmla %0.4s, %1.4s, %2.4s"                               \
                     : "+w"(dst)                                              \
                     : "w"(src), "w"(one))
#define OP_ADD(dst, src)                                                      \
    __asm__ volatile("fadd %0.4s, %0.4s, %1.4s" : "+w"(dst) : "w"(src))
#define EIGHT(op)                                                             \
    op(a, va);                                                                \
    op(b, vb);                                                                \
    op(c, vc);                                                                \
    op(d, vd);                                                                \
    op(e, ve);                                                                \
    op(f, vf);                                                                \
    op(g, vg);                                                                \
    op(h, vh)
    for (unsigned pass = 0; pass < repeat; ++pass) {
        for (size_t j = 0; j < bytes / sizeof(float); j += 32) {
            float32x4_t va = vld1q_f32(data + j);
            float32x4_t vb = vld1q_f32(data + j + 4);
            float32x4_t vc = vld1q_f32(data + j + 8);
            float32x4_t vd = vld1q_f32(data + j + 12);
            float32x4_t ve = vld1q_f32(data + j + 16);
            float32x4_t vf = vld1q_f32(data + j + 20);
            float32x4_t vg = vld1q_f32(data + j + 24);
            float32x4_t vh = vld1q_f32(data + j + 28);
            if (fmas) {
                for (unsigned r = 0; r < fmas; ++r) {
                    EIGHT(OP_FMA);
                }
            } else {
                EIGHT(OP_ADD);
            }
        }
    }
#undef EIGHT
#undef OP_ADD
#undef OP_FMA
    return vaddvq_f32(a) + vaddvq_f32(b) + vaddvq_f32(c) + vaddvq_f32(d) +
           vaddvq_f32(e) + vaddvq_f32(f) + vaddvq_f32(g) + vaddvq_f32(h);
#else
    float sum = 0;
    for (unsigned pass = 0; pass < repeat; ++pass) {
        for (size_t j = 0; j < bytes / sizeof(float); ++j) {
            sum += data[j] * (fmas ? fmas : 1);
        }
    }
    return sum;
#endif
}

static __attribute__((noinline)) float
compute(unsigned iterations)
{
#if defined(__aarch64__)
    float32x4_t a = vdupq_n_f32(0), b = a, c = a, d = a;
    float32x4_t e = a, f = a, g = a, h = a;
    float32x4_t one = vdupq_n_f32(1);
    for (unsigned i = 0; i < iterations; ++i) {
        __asm__ volatile("fmla %0.4s, %8.4s, %8.4s\n"
                         "fmla %1.4s, %8.4s, %8.4s\n"
                         "fmla %2.4s, %8.4s, %8.4s\n"
                         "fmla %3.4s, %8.4s, %8.4s\n"
                         "fmla %4.4s, %8.4s, %8.4s\n"
                         "fmla %5.4s, %8.4s, %8.4s\n"
                         "fmla %6.4s, %8.4s, %8.4s\n"
                         "fmla %7.4s, %8.4s, %8.4s\n"
                         : "+w"(a), "+w"(b), "+w"(c), "+w"(d), "+w"(e),
                           "+w"(f), "+w"(g), "+w"(h)
                         : "w"(one));
    }
    return vaddvq_f32(a) + vaddvq_f32(b) + vaddvq_f32(c) + vaddvq_f32(d) +
           vaddvq_f32(e) + vaddvq_f32(f) + vaddvq_f32(g) + vaddvq_f32(h);
#else
    return 32.0f * iterations;
#endif
}

static int
report(unsigned id, const char *level, const char *name, size_t working_set,
       size_t bytes, uint64_t flops, float result, float expected)
{
    int ok = result == expected;
    printf("ROOF_CASE {\"id\":%u,\"level\":\"%s\",\"kernel\":\"%s\","
           "\"working_set_bytes\":%zu,\"useful_bytes\":%zu,"
           "\"flops\":%llu,\"checksum\":%.0f,\"expected\":%.0f,"
           "\"measured_unique_lines\":%zu,\"repeats\":%zu,"
           "\"result\":\"%s\"}\n",
           id, level, name, working_set, bytes, (unsigned long long)flops,
           result, expected, (working_set < bytes ? working_set : bytes) / 64,
           working_set && working_set < bytes ? bytes / working_set : 1,
           ok ? "PASS" : "FAIL");
    fflush(stdout);
    return ok;
}

int
main(int argc, char **argv)
{
    (void)number;
    (void)spawn;
    (void)sync_workers;
    int l2_check = 0, slc_check = 0;
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--host")) {
            host = 1;
        } else if (!strcmp(argv[i], "--slc-check")) {
            slc_check = 1;
        } else if (!strcmp(argv[i], "--l2-check")) {
            l2_check = 1;
        } else {
            return 2;
        }
    }
    if (l2_check && slc_check) {
        return 2;
    }
    pin(0);
    const char *levels[] = {"DRAM_cold", "L1", "L2", "L3", "SLC_proxy"};
    size_t sizes[] = {4 * 1024 * KIB, 32 * KIB, 256 * KIB, 2 * 1024 * KIB,
                      6 * 1024 * KIB};
    float *dummy = NULL;
    if (slc_check) {
        dummy = aligned_alloc(64, 4 * KIB);
        if (!dummy) {
            die("SLC dummy allocation");
        }
        for (size_t j = 0; j < KIB; ++j) {
            dummy[j] = 1;
        }
    }
    float *buffers[5];
    for (unsigned i = 0; i < 5; ++i) {
        if ((slc_check && i != 4) || (l2_check && i != 2)) {
            continue;
        }
        if (slc_check) {
            sizes[i] = 1024 * KIB;
        }
        buffers[i] = aligned_alloc(64, sizes[i]);
        if (!buffers[i]) {
            die("roofline allocation");
        }
        for (size_t j = 0; j < sizes[i] / sizeof(float); ++j) {
            buffers[i][j] = 1;
        }
    }
    puts(
        "ROOF_READY atomic_prefault=1 active_cpu=0 checksum_flops_excluded=1");
    fflush(stdout);
    marker_begin(UINT64_C(0x5a535743));
    unsigned id = 1;
    int ok = 1;
    if (slc_check) {
        puts("ROOF_SLC_CHECK warmup_cpus=4,8 benchmark_cpu=0");
        fflush(stdout);
        const char *names[] = {"read_a", "read_b", "fma_r1", "fma_r8",
                               "fma_r64"};
        const unsigned rounds[] = {0, 0, 1, 8, 64};
        for (unsigned test = 0; test < 5; ++test) {
            const float *window = buffers[4] + test * WINDOW / sizeof(float);
            printf("ROOF_TARGET {\"id\":%u,\"virtual_start\":%llu,"
                   "\"bytes\":%lu,\"allocation_bytes\":%zu}\n",
                   22 + test, (unsigned long long)(uintptr_t)window, WINDOW,
                   sizes[4]);
            fflush(stdout);
            /* Two distinct DSUs establish clean sharing at the home. The
             * benchmark DSU never reads this window before its own ROI. */
            if (!host) {
                pin(4);
            }
            observed = kernel(window, WINDOW, 1, 0);
            if (!host) {
                pin(8);
            }
            observed = kernel(window, WINDOW, 1, 0);
            if (!host) {
                pin(0);
            }
            /* Warm CPU0 instruction/stack/control state without touching
             * the measured target. In particular observed shares a cache
             * line with marker host state and was last written on CPU8. */
            observed = kernel(dummy, 4 * KIB, 1, rounds[test]);
            if (observed != KIB * (rounds[test] ? rounds[test] : 1)) {
                return 1;
            }
#if defined(__aarch64__)
            __asm__ volatile("dsb ish" : : : "memory");
#endif
            marker_begin(22 + test);
            float result = kernel(window, WINDOW, 1, rounds[test]);
            marker_end();
            ok &= report(22 + test, "SLC_proxy", names[test], sizes[4], WINDOW,
                         WINDOW / sizeof(float) * 2 * rounds[test], result,
                         WINDOW / sizeof(float) *
                             (rounds[test] ? rounds[test] : 1));
        }
        goto done;
    }
    if (l2_check) {
        puts("ROOF_WARMUP level=L2 bytes=262144 mode=l2-check");
        fflush(stdout);
        observed = kernel(buffers[2], sizes[2], 1, 0);
        for (unsigned test = 0; test < 2; ++test) {
            marker_begin(12 + test);
            float result = kernel(buffers[2], sizes[2], 2, 0);
            marker_end();
            ok &= report(12 + test, "L2", test ? "read_b" : "read_a", sizes[2],
                         sizes[2] * 2, 0, result, 131072);
        }
        goto done;
    }
    observed = compute(128);
    marker_begin(id);
    float sum = compute(8192);
    marker_end();
    ok &= report(id++, "compute", "fma8", 0, 0, 8192 * 64, sum, 8192 * 32);
    const char *names[] = {"read_a", "read_b", "fma_r1", "fma_r8", "fma_r64"};
    unsigned rounds[] = {0, 0, 1, 8, 64};
    for (unsigned level = 0; level < 5; ++level) {
        if (level) {
            printf("ROOF_WARMUP level=%s bytes=%zu\n", levels[level],
                   sizes[level]);
            fflush(stdout);
            observed = kernel(buffers[level], sizes[level], 1, 0);
        }
        for (unsigned test = 0; test < 5; ++test) {
            size_t bytes = sizes[level] < WINDOW ? sizes[level] : WINDOW;
            size_t offset = (test * bytes) % sizes[level];
            unsigned repeat = WINDOW / bytes;
            marker_begin(id);
            sum = kernel(buffers[level] + offset / sizeof(float), bytes,
                         repeat, rounds[test]);
            marker_end();
            uint64_t flops = WINDOW / sizeof(float) * 2 * rounds[test];
            float expected =
                WINDOW / sizeof(float) * (rounds[test] ? rounds[test] : 1);
            ok &= report(id++, levels[level], names[test], sizes[level],
                         WINDOW, flops, sum, expected);
        }
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
