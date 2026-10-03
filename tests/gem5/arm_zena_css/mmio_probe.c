/*
 * Copyright (c) 2026 Chanho Park
 * SPDX-License-Identifier: BSD-3-Clause
 *
 * Guest-only Zena AP register and optional RTC alarm probe.
 *
 * Build: aarch64-linux-gnu-gcc -std=c11 -O2 -Wall -Wextra -Werror -static \
 *            mmio_probe.c -o mmio_probe
 * Usage: mmio_probe {4|16} {minimal|legacy-io|qemu-compatible}
 *                  [--block] [--rtc-alarm]
 *
 * All /dev/mem mappings are read-only. RTC alarms use the bound Linux
 * driver's ioctl interface; an existing active alarm is never replaced.
 * Success prints ZENA_MMIO_PASS and exits 0. Errors print ZENA_MMIO_FAIL
 * and exit 1; invalid arguments exit 2. Alarm success additionally prints
 * two ZENA_RTC_ALARM lines with positive IRQ deltas and quiet=1.
 */

#define _GNU_SOURCE
#define _FILE_OFFSET_BITS 64

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/rtc.h>
#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define GICD_BASE UINT64_C(0x20800000)
#define GICR_BASE UINT64_C(0x20880000)
#define GICR_STRIDE 0x40000
#define GIC_PIDR2 0xffe8
#define GICR_TYPER 0x0008
#define RTC_BASE UINT64_C(0x300d0000)
#define RTC_DR 0x00
#define RTC_MR 0x04
#define RTC_CR 0x0c
#define RTC_IMSC 0x10
#define RTC_RIS 0x14
#define RTC_MIS 0x18

struct mapping
{
    void *base;
    size_t size;
};

static int
failure(const char *stage)
{
    fprintf(stderr, "ZENA_MMIO_FAIL stage=%s errno=%d detail=%s\n", stage,
            errno, strerror(errno));
    return 1;
}

static int
map_region(int fd, uint64_t address, size_t bytes, struct mapping *region)
{
    const long page_size = sysconf(_SC_PAGESIZE);
    if (page_size <= 0 || address % (uint64_t)page_size) {
        errno = EINVAL;
        return failure("mapping-alignment");
    }
    region->size = (bytes + page_size - 1) / page_size * page_size;
    region->base =
        mmap(NULL, region->size, PROT_READ, MAP_SHARED, fd, (off_t)address);
    if (region->base == MAP_FAILED) {
        return failure("mmap");
    }
    return 0;
}

static uint32_t
read32(const struct mapping *region, size_t offset)
{
    return *(volatile const uint32_t *)((const char *)region->base + offset);
}

static uint64_t
read64(const struct mapping *region, size_t offset)
{
    /* GICR_TYPER is read with one naturally aligned 64-bit MMIO access. */
    return *(volatile const uint64_t *)((const char *)region->base + offset);
}

static int
check_gic(int fd, unsigned cpus)
{
    struct mapping distributor, redistributors;
    if (map_region(fd, GICD_BASE, 0x10000, &distributor)) {
        return 1;
    }
    const uint32_t pidr2 = read32(&distributor, GIC_PIDR2);
    munmap(distributor.base, distributor.size);
    printf("ZENA_MMIO_GICD pidr2=0x%08" PRIx32 " arch=%u\n", pidr2,
           (pidr2 >> 4) & 15);
    if (((pidr2 >> 4) & 15) != 4) {
        errno = EPROTO;
        return failure("GICD-PIDR2");
    }
    if (map_region(fd, GICR_BASE, cpus * GICR_STRIDE, &redistributors)) {
        return 1;
    }
    int result = 0;
    for (unsigned cpu = 0; cpu < cpus; ++cpu) {
        const size_t offset = cpu * GICR_STRIDE;
        const uint64_t typer = read64(&redistributors, offset + GICR_TYPER);
        const uint32_t affinity = typer >> 32;
        const uint32_t expected = ((cpu / 4) << 16) | ((cpu % 4) << 8);
        const unsigned processor = (typer >> 8) & 0xffff;
        const unsigned last = (typer >> 4) & 1;
        const uint32_t rpidr2 = read32(&redistributors, offset + GIC_PIDR2);
        printf("ZENA_MMIO_GICR cpu=%u typer=0x%016" PRIx64
               " affinity=0x%08" PRIx32 " expected=0x%08" PRIx32
               " processor=%u last=%u pidr2=0x%08" PRIx32 "\n",
               cpu, typer, affinity, expected, processor, last, rpidr2);
        if (affinity != expected || processor != cpu ||
            last != (cpu == cpus - 1) || ((rpidr2 >> 4) & 15) != 4) {
            errno = EPROTO;
            result = failure("GICR-TYPER-PIDR2");
            break;
        }
    }
    munmap(redistributors.base, redistributors.size);
    return result;
}

static int
check_virtio(int fd, const char *name, uint64_t address, uint32_t version,
             uint32_t device_id)
{
    struct mapping region;
    if (map_region(fd, address, 0x200, &region)) {
        return 1;
    }
    const uint32_t magic = read32(&region, 0x00);
    const uint32_t actual_version = read32(&region, 0x04);
    const uint32_t actual_id = read32(&region, 0x08);
    const uint32_t vendor = read32(&region, 0x0c);
    const uint32_t status = read32(&region, 0x70);
    munmap(region.base, region.size);
    printf("ZENA_MMIO_VIRTIO name=%s base=0x%08" PRIx64 " magic=0x%08" PRIx32
           " version=%" PRIu32 " device=%" PRIu32 " vendor=0x%08" PRIx32
           " status=0x%02" PRIx32 "\n",
           name, address, magic, actual_version, actual_id, vendor, status);
    if (magic != 0x74726976 || actual_version != version ||
        actual_id != device_id || vendor != 0x1af4) {
        errno = EPROTO;
        return failure("virtio-identity");
    }
    return 0;
}

static int
rtc_irq_count(unsigned cpus, uint64_t *count)
{
    FILE *stream = fopen("/proc/interrupts", "r");
    if (!stream) {
        return failure("interrupts-open");
    }
    char *line = NULL;
    size_t capacity = 0;
    bool found = false;
    while (getline(&line, &capacity, stream) >= 0) {
        if (!strstr(line, "rtc-pl031")) {
            continue;
        }
        char *cursor = strchr(line, ':');
        if (!cursor) {
            break;
        }
        ++cursor;
        *count = 0;
        unsigned cpu;
        for (cpu = 0; cpu < cpus; ++cpu) {
            char *end;
            const uint64_t value = strtoull(cursor, &end, 10);
            if (end == cursor) {
                break;
            }
            *count += value;
            cursor = end;
        }
        char controller[32];
        unsigned intid;
        found = cpu == cpus &&
                sscanf(cursor, "%31s %u", controller, &intid) == 2 &&
                intid == 300;
        break;
    }
    free(line);
    fclose(stream);
    if (!found) {
        errno = EPROTO;
        return failure("RTC-INTID300-counter");
    }
    return 0;
}

static time_t
rtc_to_epoch(const struct rtc_time *rtc)
{
    struct tm date;
    date = (struct tm){.tm_sec = rtc->tm_sec,
                       .tm_min = rtc->tm_min,
                       .tm_hour = rtc->tm_hour,
                       .tm_mday = rtc->tm_mday,
                       .tm_mon = rtc->tm_mon,
                       .tm_year = rtc->tm_year};
    return timegm(&date);
}

static int
alarm_cycle(int fd, const struct mapping *region, unsigned cpus,
            unsigned cycle)
{
    uint64_t before, after, quiet;
    struct rtc_time now;
    if (rtc_irq_count(cpus, &before)) {
        return 1;
    }
    if (ioctl(fd, RTC_RD_TIME, &now) < 0) {
        return failure("RTC-RD-TIME");
    }
    const time_t target = rtc_to_epoch(&now) + 2;
    struct tm date;
    if (target < 2 || target > UINT32_MAX || !gmtime_r(&target, &date)) {
        errno = ERANGE;
        return failure("RTC-alarm-date");
    }
    struct rtc_wkalrm alarm;
    alarm = (struct rtc_wkalrm){.enabled = 1,
                                .time = {.tm_sec = date.tm_sec,
                                         .tm_min = date.tm_min,
                                         .tm_hour = date.tm_hour,
                                         .tm_mday = date.tm_mday,
                                         .tm_mon = date.tm_mon,
                                         .tm_year = date.tm_year,
                                         .tm_wday = date.tm_wday,
                                         .tm_yday = date.tm_yday}};
    if (ioctl(fd, RTC_WKALM_SET, &alarm) < 0) {
        return failure("RTC-WKALM-SET");
    }
    const uint32_t match = read32(region, RTC_MR);
    const uint32_t mask = read32(region, RTC_IMSC);
    printf("ZENA_RTC_ARMED cycle=%u target=%" PRId64 " match=%" PRIu32
           " imsc=%" PRIu32 "\n",
           cycle, (int64_t)target, match, mask);
    if (match != (uint32_t)target || !(mask & 1)) {
        errno = EPROTO;
        return failure("RTC-alarm-registers");
    }
    struct pollfd event = {.fd = fd, .events = POLLIN};
    int ready;
    do {
        ready = poll(&event, 1, 6000);
    } while (ready < 0 && errno == EINTR);
    if (ready <= 0 || !(event.revents & POLLIN)) {
        if (ready >= 0) {
            errno = ETIMEDOUT;
        }
        return failure("RTC-alarm-poll");
    }
    unsigned long events;
    if (read(fd, &events, sizeof(events)) != sizeof(events)) {
        return failure("RTC-alarm-read");
    }
    const uint32_t fired = read32(region, RTC_DR);
    if ((events & (RTC_AF | RTC_IRQF)) != (RTC_AF | RTC_IRQF) ||
        fired < target || fired - target > 2 ||
        (read32(region, RTC_RIS) & 1) || (read32(region, RTC_MIS) & 1)) {
        errno = EPROTO;
        return failure("RTC-alarm-event-clear");
    }
    if (rtc_irq_count(cpus, &after)) {
        return 1;
    }
    struct timespec wait = {.tv_sec = 1};
    while (nanosleep(&wait, &wait) < 0) {
        if (errno != EINTR) {
            return failure("RTC-quiescence-sleep");
        }
    }
    if (rtc_irq_count(cpus, &quiet)) {
        return 1;
    }
    if (after <= before || quiet != after || (read32(region, RTC_RIS) & 1) ||
        (read32(region, RTC_MIS) & 1)) {
        errno = EPROTO;
        return failure("RTC-IRQ-quiescence");
    }
    if (ioctl(fd, RTC_AIE_OFF, 0) < 0) {
        return failure("RTC-AIE-OFF");
    }
    printf("ZENA_RTC_ALARM cycle=%u target=%" PRId64 " fired=%" PRIu32
           " events=0x%lx irq_delta=%" PRIu64 " quiet=1\n",
           cycle, (int64_t)target, fired, events, after - before);
    return 0;
}

static int
check_rtc(int mem_fd, unsigned cpus, bool test_alarm)
{
    struct mapping region;
    if (map_region(mem_fd, RTC_BASE, 0x1000, &region)) {
        return 1;
    }
    printf("ZENA_MMIO_RTC dr=%" PRIu32 " mr=%" PRIu32 " cr=%" PRIu32
           " imsc=%" PRIu32 " ris=%" PRIu32 " mis=%" PRIu32 "\n",
           read32(&region, RTC_DR), read32(&region, RTC_MR),
           read32(&region, RTC_CR), read32(&region, RTC_IMSC),
           read32(&region, RTC_RIS), read32(&region, RTC_MIS));
    int result = 0;
    if (!(read32(&region, RTC_CR) & 1)) {
        errno = EPROTO;
        result = failure("RTC-disabled");
    }
    if (!result && test_alarm) {
        int fd = open("/dev/rtc0", O_RDONLY | O_NONBLOCK | O_CLOEXEC);
        struct rtc_wkalrm saved;
        bool changed = false;
        if (fd < 0) {
            result = failure("RTC-open");
        } else if (ioctl(fd, RTC_WKALM_RD, &saved) < 0) {
            result = failure("RTC-WKALM-RD");
        } else if (saved.enabled || saved.pending ||
                   (read32(&region, RTC_IMSC) & 1) ||
                   (read32(&region, RTC_RIS) & 1)) {
            errno = EBUSY;
            result = failure("RTC-already-active");
        } else {
            changed = true;
            for (unsigned cycle = 1; cycle <= 2 && !result; ++cycle) {
                result = alarm_cycle(fd, &region, cpus, cycle);
            }
        }
        if (changed) {
            // Restore the disabled Linux alarm state, never the wall clock.
            // The disabled hardware MR need not retain its previous value.
            if (ioctl(fd, RTC_AIE_OFF, 0) < 0 ||
                ioctl(fd, RTC_WKALM_SET, &saved) < 0) {
                result = failure("RTC-alarm-restore");
            }
        }
        if (fd >= 0) {
            close(fd);
        }
    }
    munmap(region.base, region.size);
    return result;
}

static void
usage(const char *program)
{
    fprintf(stderr,
            "Usage: %s {4|16} {minimal|legacy-io|qemu-compatible} "
            "[--block] [--rtc-alarm]\n",
            program);
}

int
main(int argc, char **argv)
{
    if (argc == 2 && !strcmp(argv[1], "--help")) {
        usage(argv[0]);
        return 0;
    }
    if (argc < 3 || (strcmp(argv[1], "4") && strcmp(argv[1], "16"))) {
        usage(argv[0]);
        return 2;
    }
    const unsigned cpus = strtoul(argv[1], NULL, 10);
    const int profile = !strcmp(argv[2], "minimal")           ? 0
                        : !strcmp(argv[2], "legacy-io")       ? 1
                        : !strcmp(argv[2], "qemu-compatible") ? 2
                                                              : -1;
    bool block = false, alarm = false;
    for (int arg = 3; arg < argc; ++arg) {
        if (!strcmp(argv[arg], "--block")) {
            block = true;
        } else if (!strcmp(argv[arg], "--rtc-alarm")) {
            alarm = true;
        } else {
            usage(argv[0]);
            return 2;
        }
    }
    if (profile < 0 || (!profile && block)) {
        usage(argv[0]);
        return 2;
    }
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (sysconf(_SC_NPROCESSORS_ONLN) != cpus) {
        errno = EPROTO;
        return failure("online-CPU-count");
    }
    const int fd = open("/dev/mem", O_RDONLY | O_SYNC | O_CLOEXEC);
    if (fd < 0) {
        return failure("devmem-open");
    }
    int result = check_gic(fd, cpus);
    if (!result && profile) {
        result = check_virtio(fd, "rng", 0x30080000, profile, 4);
    }
    if (!result && block) {
        result = check_virtio(fd, "block", 0x30020000, profile, 2);
    }
    if (!result && profile == 2) {
        result = check_virtio(fd, "net", 0x30060000, 2, 1);
    }
    if (!result) {
        result = check_rtc(fd, cpus, alarm);
    }
    close(fd);
    if (!result) {
        printf("ZENA_MMIO_PASS cpus=%u profile=%s block=%u rtc_alarm=%u\n",
               cpus, argv[2], block, alarm);
    }
    return result;
}
