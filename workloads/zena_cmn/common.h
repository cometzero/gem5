/* SPDX-License-Identifier: BSD-3-Clause */
#ifndef ZENA_CMN_COMMON_H
#define ZENA_CMN_COMMON_H
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static int host;
static int host_roi_active;
static struct timespec host_roi_start;
static void
die(const char *message)
{
    perror(message);
    exit(1);
}
static unsigned
number(const char *text)
{
    char *end;
    errno = 0;
    unsigned long n = strtoul(text, &end, 10);
    if (errno || *end || !*text || n > 1000000 || !n) {
        fprintf(stderr, "invalid positive integer: %s\n", text);
        exit(2);
    }
    return n;
}
static void
pin(unsigned id)
{
    cpu_set_t set, available;
    unsigned cpu = id;
    if (host) {
        if (sched_getaffinity(0, sizeof(available), &available)) {
            die("getaffinity");
        }
        unsigned n = 0;
        for (cpu = 0; cpu < CPU_SETSIZE; ++cpu) {
            if (CPU_ISSET(cpu, &available) && n++ == id) {
                break;
            }
        }
    }
    if (cpu >= CPU_SETSIZE) {
        fprintf(stderr, "requested worker CPU unavailable\n");
        exit(1);
    }
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    if (sched_setaffinity(0, sizeof(set), &set)) {
        die("setaffinity");
    }
}
static void
sync_workers(pthread_barrier_t *barrier)
{
    int rc = pthread_barrier_wait(barrier);
    if (rc && rc != PTHREAD_BARRIER_SERIAL_THREAD) {
        errno = rc;
        die("barrier");
    }
}
static void
marker_begin(uint64_t id)
{
    if (host && id == 0) {
        if (clock_gettime(CLOCK_MONOTONIC_RAW, &host_roi_start)) {
            die("clock_gettime");
        }
        host_roi_active = 1;
    }
#if defined(__aarch64__)
    if (!host) {
        register uint64_t x0 __asm__("x0") = id;
        register uint64_t x1 __asm__("x1") = 0;
        __asm__ volatile(".inst 0xff5a0110" : "+r"(x0), "+r"(x1) : : "memory");
    }
#else
    (void)id;
#endif
}
static void
marker_end(void)
{
    if (host && host_roi_active) {
        struct timespec end;
        if (clock_gettime(CLOCK_MONOTONIC_RAW, &end)) {
            die("clock_gettime");
        }
        int64_t elapsed = (int64_t)(end.tv_sec - host_roi_start.tv_sec) *
                              INT64_C(1000000000) +
                          end.tv_nsec - host_roi_start.tv_nsec;
        host_roi_active = 0;
        printf("CMN_HOST_ROI {\"host\":true,"
               "\"clock\":\"CLOCK_MONOTONIC_RAW\","
               "\"elapsed_ns\":%" PRId64 "}\n",
               elapsed);
    }
#if defined(__aarch64__)
    if (!host) {
        register uint64_t x0 __asm__("x0") = 0;
        register uint64_t x1 __asm__("x1") = 0;
        __asm__ volatile(".inst 0xff5b0110" : "+r"(x0), "+r"(x1) : : "memory");
    }
#endif
}
static void
spawn(pthread_t *thread, void *(*run)(void *), unsigned id)
{
    int rc = pthread_create(thread, NULL, run, (void *)(uintptr_t)id);
    if (rc) {
        errno = rc;
        die("pthread_create");
    }
}
#endif
