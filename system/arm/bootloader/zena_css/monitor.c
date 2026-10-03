/* Copyright (c) 2026 Chanho Park */
/* SPDX-License-Identifier: BSD-3-Clause */
/* AP-only functional firmware; no Zena power-controller emulation. */
#include <stdint.h>

#if CPU_COUNT != 4 && CPU_COUNT != 16
#error "Zena supports exactly 4 or 16 AP CPUs"
#endif

#define PSCI_VERSION 0x84000000u
#define PSCI_CPU_OFF 0x84000002u
#define PSCI_CPU_ON32 0x84000003u
#define PSCI_CPU_ON64 0xc4000003u
#define PSCI_AFFINITY32 0x84000004u
#define PSCI_AFFINITY64 0xc4000004u
#define PSCI_SYSTEM_OFF 0x84000008u
#define PSCI_SYSTEM_RESET 0x84000009u
#define PSCI_FEATURES 0x8400000au
#define OFF 1u
#define ON 0u
#define ON_PENDING 2u
#define FIRMWARE_BASE 0xffe00000ul

struct CpuState
{
    uint64_t entry;
    uint64_t context;
    uint32_t state;
};
static struct CpuState cpus[CPU_COUNT];
static uint32_t initialized;
static uint32_t request_lock;

static void monitor_selftest(void);

extern void cpu_enter(uint64_t entry, uint64_t context)
    __attribute__((noreturn));
extern void cpu_park(void) __attribute__((noreturn));

static inline void
barrier(void)
{
    __asm__ volatile("dsb sy" ::: "memory");
}

static unsigned
cpu_index(void)
{
    uint64_t mpidr;
    __asm__ volatile("mrs %0, mpidr_el1" : "=r"(mpidr));
    return ((mpidr >> 16) & 255) * 4 + ((mpidr >> 8) & 255);
}

static int
affinity_index(uint64_t affinity)
{
    unsigned cluster = (affinity >> 16) & 255;
    unsigned core = (affinity >> 8) & 255;
    if ((affinity & ~0x30300ul) || core >= 4 || cluster >= CPU_COUNT / 4) {
        return -1;
    }
    return cluster * 4 + core;
}

static void
lock(void)
{
    while (__atomic_exchange_n(&request_lock, 1, __ATOMIC_ACQUIRE)) {
        __asm__ volatile("yield");
    }
}

static void
unlock(void)
{
    __atomic_store_n(&request_lock, 0, __ATOMIC_RELEASE);
}

static void
write32(uint64_t address, uint32_t value)
{
    *(volatile uint32_t *)address = value;
}

static uint32_t
read32(uint64_t address)
{
    return *(volatile uint32_t *)address;
}

static void
console_puts(const char *text)
{
    while (*text) {
        while (read32(0x1a400018) & (1u << 5)) {
            __asm__ volatile("yield");
        }
        write32(0x1a400000, *text++);
    }
}

static void
gic_initialize(void)
{
    /* No interrupts are enabled until Linux programs its own GIC state. */
    /* Keep ARE_S/ARE_NS set: this GIC model only supports affinity routing. */
    write32(0x20800000, 0x30);
    for (unsigned word = 1; word < 16; ++word) {
        write32(0x20800080 + 4 * word, UINT32_MAX);
    }
    /* Enable affinity routing and non-secure Group 1. */
    write32(0x20800000, 0x32);
    for (unsigned cpu = 0; cpu < CPU_COUNT; ++cpu) {
        uint64_t redist = 0x20880000ul + cpu * 0x40000ul;
        write32(redist + 0x14, read32(redist + 0x14) & ~2u);
        while (read32(redist + 0x14) & 4) {
            __asm__ volatile("yield");
        }
        write32(redist + 0x10080, UINT32_MAX);
    }
    barrier();
}

void __attribute__((noreturn))
monitor_park(void)
{
    unsigned id = cpu_index();
    for (;;) {
        if (__atomic_load_n(&cpus[id].state, __ATOMIC_ACQUIRE) == ON_PENDING) {
            uint64_t entry = cpus[id].entry;
            uint64_t context = cpus[id].context;
            __atomic_store_n(&cpus[id].state, ON, __ATOMIC_RELEASE);
            cpu_enter(entry, context);
        }
        __asm__ volatile("wfe");
    }
}

void __attribute__((noreturn))
monitor_boot(uint64_t dtb, uint64_t entry)
{
    unsigned id = cpu_index();
    if (!id) {
        for (unsigned cpu = 0; cpu < CPU_COUNT; ++cpu) {
            cpus[cpu].state = cpu ? OFF : ON;
        }
        gic_initialize();
        monitor_selftest();
        __atomic_store_n(&initialized, 1, __ATOMIC_RELEASE);
        barrier();
        __asm__ volatile("sev");
        cpu_enter(entry, dtb);
    }
    while (!__atomic_load_n(&initialized, __ATOMIC_ACQUIRE)) {
        __asm__ volatile("wfe");
    }
    monitor_park();
}

static int64_t
feature(uint32_t fid)
{
    switch (fid) {
        case PSCI_VERSION:
        case PSCI_CPU_OFF:
        case PSCI_CPU_ON32:
        case PSCI_CPU_ON64:
        case PSCI_AFFINITY32:
        case PSCI_AFFINITY64:
        case PSCI_SYSTEM_OFF:
        case PSCI_FEATURES:
#if WARM_RESET
        case PSCI_SYSTEM_RESET:
#endif
            return 0;
        default:
            return -1;
    }
}

int64_t
monitor_smc(uint64_t function, uint64_t arg1, uint64_t arg2, uint64_t arg3)
{
    uint32_t fid = function;
    int target;
    int64_t result;

    if (!(fid & (1u << 30))) {
        arg1 = (uint32_t)arg1;
        arg2 = (uint32_t)arg2;
        arg3 = (uint32_t)arg3;
    }
    switch (fid) {
        case PSCI_VERSION:
            return 0x10000; /* PSCI 1.0 */
        case PSCI_FEATURES:
            return feature(arg1);
        case PSCI_CPU_ON32:
        case PSCI_CPU_ON64:
            target = affinity_index(arg1);
            if (target < 0) {
                return -2; /* INVALID_PARAMETERS */
            }
            if ((arg2 & 3) || arg2 < 0x80000000ul || arg2 >= FIRMWARE_BASE) {
                return -9; /* INVALID_ADDRESS */
            }
            lock();
            result = __atomic_load_n(&cpus[target].state, __ATOMIC_ACQUIRE);
            if (result == OFF) {
                cpus[target].entry = arg2;
                cpus[target].context = arg3;
                __atomic_store_n(&cpus[target].state, ON_PENDING,
                                 __ATOMIC_RELEASE);
                result = 0;
            } else {
                result = result == ON ? -4 : -5;
            }
            unlock();
            barrier();
            __asm__ volatile("sev");
            return result;
        case PSCI_CPU_OFF:
            __atomic_store_n(&cpus[cpu_index()].state, OFF, __ATOMIC_RELEASE);
            barrier();
            cpu_park();
        case PSCI_AFFINITY32:
        case PSCI_AFFINITY64:
            target = affinity_index(arg1);
            if (arg2 != 0 || target < 0) {
                return -2;
            }
            return __atomic_load_n(&cpus[target].state, __ATOMIC_ACQUIRE);
#if WARM_RESET
        case PSCI_SYSTEM_RESET:
            barrier();
            /* Explicit board event; never treat a host restart as this reset.
             */
            __asm__ volatile("movz x0, #0x5354; movk x0, #0x5a52, lsl #16; "
                             "mov x1, #0; .inst 0xff5a0110" ::
                                 : "x0", "x1", "memory");
            for (;;) {
                __asm__ volatile("wfe");
            }
#endif
        case PSCI_SYSTEM_OFF:
            console_puts("ZENA_GEM5_PSCI_SYSTEM_OFF\n");
            barrier();
            __asm__ volatile("mov x0, #0; .inst 0xff210110" ::
                                 : "x0", "memory");
            for (;;) {
                __asm__ volatile("wfe");
            }
        default:
            /* RESET, SUSPEND and retention require real model support. */
            return -1;
    }
}

/*
 * Exercise the dispatcher before secondaries are released. This does not
 * execute SMC/ERET; Linux SMP/hotplug independently validates that ABI.
 * None of these calls is permitted to change a CPU's boot state.
 */
static void
monitor_selftest(void)
{
    static const struct
    {
        uint64_t function, affinity, entry, context;
        int64_t expected;
    } cases[] = {
        {PSCI_VERSION, 0, 0, 0, 0x10000},
        {PSCI_FEATURES, PSCI_CPU_ON64, 0, 0, 0},
        {PSCI_FEATURES, 0xdeadbeef, 0, 0, -1},
        {PSCI_CPU_ON64, 1, 0x80000000, 0, -2},
        {PSCI_CPU_ON64, (CPU_COUNT / 4) << 16, 0x80000000, 0, -2},
        {PSCI_CPU_ON64, 0x100, 0x80000001, 0, -9},
        {PSCI_CPU_ON64, 0x100, 0x7ffffffc, 0, -9},
        {PSCI_CPU_ON64, 0x100, FIRMWARE_BASE, 0, -9},
        {PSCI_CPU_ON64, 0, 0x80000000, 0, -4},
        {PSCI_AFFINITY64, 1, 0, 0, -2},
        {PSCI_AFFINITY64, 0, 1, 0, -2},
        {PSCI_AFFINITY64, 0x100, 0, 0, OFF},
#if WARM_RESET
        {PSCI_FEATURES, PSCI_SYSTEM_RESET, 0, 0, 0},
#else
        {PSCI_FEATURES, PSCI_SYSTEM_RESET, 0, 0, -1},
#endif
        /* SMC32 arguments must ignore bits above bit 31. */
        {PSCI_CPU_ON32, 1ull << 32, 0x80000000, 0, -4},
        {0xdeadbeef, 0, 0, 0, -1},
    };
    /* Volatile indirect call prevents constant-folding a separate test path.
     */
    int64_t (*volatile dispatch)(uint64_t, uint64_t, uint64_t, uint64_t) =
        monitor_smc;
    unsigned index;
    for (index = 0; index < sizeof(cases) / sizeof(cases[0]); ++index) {
        if (dispatch(cases[index].function, cases[index].affinity,
                     cases[index].entry,
                     cases[index].context) != cases[index].expected) {
            goto fail;
        }
    }
    for (unsigned cpu = 0; cpu < CPU_COUNT; ++cpu) {
        if (cpus[cpu].state != (cpu ? OFF : ON) || cpus[cpu].entry ||
            cpus[cpu].context) {
            goto fail;
        }
    }
    if (request_lock || initialized) {
        goto fail;
    }
    console_puts("ZENA_GEM5_PSCI_DISPATCH_SELFTEST=PASS cases=15\n");
    return;

fail: {
    char detail[] = "case=00\n";
    detail[5] = '0' + index / 10;
    detail[6] = '0' + index % 10;
    console_puts("ZENA_GEM5_PSCI_DISPATCH_SELFTEST=FAIL ");
    console_puts(detail);
}
    barrier();
    __asm__ volatile("mov x0, #0; mov x1, #100; .inst 0xff220110" ::
                         : "x0", "x1", "memory");
    for (;;) {
        __asm__ volatile("wfe");
    }
}
