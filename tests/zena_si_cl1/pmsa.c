/* SPDX-License-Identifier: BSD-3-Clause */
#include <stdbool.h>
#include <stdint.h>

#define READ(reg)                                                             \
    ({                                                                        \
        uint64_t v;                                                           \
        __asm__ volatile("mrs %0, " #reg : "=r"(v));                          \
        v;                                                                    \
    })
#define WRITE(reg, v)                                                         \
    do {                                                                      \
        uint64_t tmp = (v);                                                   \
        __asm__ volatile("msr " #reg ", %0; isb" ::"r"(tmp) : "memory");      \
    } while (0)
#define TEST 0x40000000UL
#define DTCM 0x100100000UL
#define ITCM 0x100000000UL

volatile uint64_t recovery_pc, el2_return_pc;
static volatile uint64_t faults, fault_ec, fault_addr, fault_el, fault_iss;
static unsigned failures;
extern void enter_el1(void (*fn)(void));
extern uint64_t try_read(uint64_t address);
extern void try_write(uint64_t address, uint64_t value);
extern void try_exec(uint64_t address);
extern void try_tcm_read(void);
extern void try_tcm_write(uint64_t value);

static void
putc(char c)
{
    *(volatile uint32_t *)0x2a410000 = c;
}

static void
puts(const char *s)
{
    while (*s) {
        putc(*s++);
    }
}

static void
check(bool ok, const char *name)
{
    failures += !ok;
    puts(ok ? "PASS " : "FAIL ");
    puts(name);
    putc('\n');
}

uint64_t
exception_handler(void)
{
    const uint64_t el = READ(CurrentEL);
    const uint64_t esr = el == 8 ? READ(ESR_EL2) : READ(ESR_EL1);
    if (el == 8 && (esr >> 26) == 0x16) {
        WRITE(SPSR_EL2, 0x3c9);
        return el2_return_pc;
    }
    faults++;
    fault_el = el;
    fault_ec = esr >> 26;
    fault_iss = esr & 0x1ffffff;
    fault_addr = el == 8 ? READ(FAR_EL2) : READ(FAR_EL1);
    if (!recovery_pc) {
        puts("FAIL unexpected exception\n");
        putc(4);
        for (;;) {
            __asm__ volatile("wfi");
        }
    }
    return recovery_pc;
}

static void
region(unsigned index, uint64_t base, uint64_t end, unsigned ap, bool xn)
{
    WRITE(S3_0_C6_C2_1, index);
    WRITE(S3_0_C6_C8_1, 0);
    WRITE(S3_0_C6_C8_0, base | (ap << 2) | (xn ? 2 : 0));
    WRITE(S3_0_C6_C8_1, ((end - 1) & ~63UL) | 3);
}

static void
regions_trap(void)
{
    faults = 0;
    try_tcm_write(1);
    check(faults == 1 && fault_el == 8 && fault_ec == 0x18 &&
              fault_iss == 0x343c00,
          "ACTLR REGIONS traps EL1 TCM write");
}

static void
tidcp_trap(void)
{
    faults = 0;
    try_tcm_read();
    check(faults == 1 && fault_el == 8 && fault_ec == 0x18 &&
              fault_iss == 0x343c01,
          "HCR TIDCP traps EL1 TCM read");
}

static void
guest(void)
{
    check(READ(CurrentEL) == 4, "reset EL2 to EL1");
    check(READ(S3_0_C0_C0_4) == 32, "MPUIR region count");
    WRITE(CNTV_CVAL_EL0, UINT64_MAX);
    WRITE(CNTV_CTL_EL0, 1);
    uint64_t deadline = READ(CNTVCT_EL0) + 100;
    while (READ(CNTVCT_EL0) < deadline) {
        __asm__ volatile("nop");
    }
    check(!(READ(CNTV_CTL_EL0) & 4), "MAX CVAL does not expire early");
    deadline = READ(CNTVCT_EL0) + 100;
    WRITE(CNTV_CVAL_EL0, deadline);
    while (READ(CNTVCT_EL0) <= deadline) {
        __asm__ volatile("nop");
    }
    check(READ(CNTV_CTL_EL0) & 4, "timer reprogrammed after MAX CVAL");
    WRITE(CNTV_CTL_EL0, 0);
    WRITE(MAIR_EL1, 0xff00);
    region(0, 0x140000000, 0x140040000, 1, false);
    region(1, 0x2a410000, 0x2a411000, 1, true);
    // UART must be Device, attribute index 0.
    WRITE(S3_0_C6_C8_1, 0x2a410fc1);
    region(2, TEST, TEST + 4096, 1, true);
    region(3, DTCM, DTCM + 131072, 1, false);
    WRITE(SCTLR_EL1, READ(SCTLR_EL1) | 0x1005);
    *(volatile uint64_t *)TEST = 0x12345678;
    check(*(volatile uint64_t *)TEST == 0x12345678, "PMSA RW normal memory");

    region(2, TEST, TEST + 4096, 3, true);
    faults = 0;
    try_write(TEST, 99);
    check(faults == 1 && fault_ec == 0x25 && fault_addr == TEST &&
              *(volatile uint64_t *)TEST == 0x12345678,
          "PMSA RO write denied");
    faults = 0;
    try_exec(TEST);
    check(faults == 1 && fault_ec == 0x21 && fault_addr == TEST,
          "PMSA XN instruction denied");
    faults = 0;
    try_read(1UL << 40);
    check(faults == 1 && fault_ec == 0x25 && fault_addr == (1UL << 40),
          "PA40 out of range denied");
    faults = 0;
    try_read(0x48000000);
    check(faults == 1 && fault_addr == 0x48000000, "PMSA unmapped denied");
    region(4, TEST, TEST + 4096, 1, true);
    faults = 0;
    try_read(TEST);
    check(faults == 1 && fault_addr == TEST, "PMSA overlap denied");
    WRITE(S3_0_C6_C8_1, 0);

    faults = 0;
    try_read(DTCM);
    check(faults == 1 && fault_addr == DTCM, "disabled DTCM denied");
    WRITE(S3_0_C15_C0_2, READ(S3_0_C15_C0_2) | 1);
    *(volatile uint64_t *)DTCM = 0x13579bdf;
    check(*(volatile uint64_t *)DTCM == 0x13579bdf, "enabled DTCM RW");
    faults = 0;
    try_exec(DTCM);
    check(faults == 1 && fault_ec == 0x21, "DTCM instruction denied");
    region(5, ITCM, ITCM + 131072, 1, false);
    faults = 0;
    try_read(ITCM);
    check(faults == 1 && fault_addr == ITCM, "disabled ITCM denied");
    WRITE(S3_0_C15_C0_1, READ(S3_0_C15_C0_1) | 1);
    *(volatile uint64_t *)ITCM = 0xd65f03c0; /* ret */
    __asm__ volatile("dsb sy; isb" ::: "memory");
    faults = 0;
    try_exec(ITCM);
    check(faults == 0, "enabled ITCM data and instruction access");
    puts(failures ? "PMSA_TEST_FAIL\n" : "PMSA_TEST_PASS\n");
    putc(4);
}

void
main(void)
{
#ifdef PMSA_TEST_UNSUPPORTED_STAGE2
    WRITE(HCR_EL2, (1UL << 31) | 1);
    try_read(TEST);
    puts("FAIL unsupported stage 2 was not rejected\n");
    putc(4);
#else
    WRITE(HCR_EL2, 1UL << 31);
    WRITE(CNTHCTL_EL2, 3);
    WRITE(CPTR_EL2, 0x33ff);
    WRITE(ACTLR_EL2, 0);
    enter_el1(regions_trap);
    WRITE(ACTLR_EL2, 1);
    WRITE(HCR_EL2, (1UL << 31) | (1UL << 20));
    enter_el1(tidcp_trap);
    WRITE(HCR_EL2, 1UL << 31);
    enter_el1(guest);
#endif
}
