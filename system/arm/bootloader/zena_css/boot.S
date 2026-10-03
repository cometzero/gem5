/* Copyright (c) 2026 Chanho Park */
/* SPDX-License-Identifier: BSD-3-Clause */
/* Simulation-only Zena AP EL3 entry and PSCI exception glue. */

.text
#ifndef WARM_RESET
#define WARM_RESET 0
#endif
.global zena_warm_reset
.set zena_warm_reset, WARM_RESET
.global zena_cpu_count
.set zena_cpu_count, CPU_COUNT
.global _start
.type _start, %function
_start:
    mov x20, x0                     // gem5 DTB address
    mov x21, x3                     // gem5 Linux physical entry
    msr daifset, #15
    msr spsel, #1
    bl set_stack
    adr x0, vectors
    msr vbar_el3, x0
    // NS, HCE, RW, APK, API, and RES1; SMC remains enabled.
    ldr x0, =0x30531
    msr scr_el3, x0
    mov x0, #0x1100                 // permit SVE/SME below EL3
    msr cptr_el3, x0
    ldr x0, =125000000
    msr cntfrq_el0, x0
    mrs x0, S3_6_C12_C12_5          // ICC_SRE_EL3
    orr x0, x0, #15
    msr S3_6_C12_C12_5, x0
    isb
    mov x0, x20
    mov x1, x21
    bl monitor_boot
    b fatal_exception

/* Never carry an old Linux SMC frame into a new CPU_ON incarnation. */
.global cpu_park
.type cpu_park, %function
cpu_park:
    bl set_stack
    bl monitor_park
    b fatal_exception

.type set_stack, %function
set_stack:
    mrs x9, mpidr_el1
    ubfx x10, x9, #16, #8
    ubfx x9, x9, #8, #8
    add x9, x9, x10, lsl #2
    add x9, x9, #1
    ldr x10, =stacks
    add x10, x10, x9, lsl #12
    mov sp, x10
    ret

/* x0 is the entry, x1 its context. All boot paths enter masked NS EL2h. */
.global cpu_enter
.type cpu_enter, %function
cpu_enter:
    msr elr_el3, x0
    mov x0, x1
    mov x1, #0x3c9
    msr spsr_el3, x1
    msr sctlr_el2, xzr
    msr sctlr_el1, xzr
    msr hcr_el2, xzr
    // CPU_ON may follow CPU_OFF; do not inherit enabled guest timers.
    msr cntp_ctl_el0, xzr
    msr cntv_ctl_el0, xzr
    msr cnthp_ctl_el2, xzr
    msr cntkctl_el1, xzr
    msr cntvoff_el2, xzr
    mrs x1, mpidr_el1
    msr vmpidr_el2, x1
    mov x1, #3
    msr cnthctl_el2, x1
    mov x1, #0x33ff
    msr cptr_el2, x1
    mov x1, #15
    msr S3_4_C12_C9_5, x1           // ICC_SRE_EL2
    isb
    mov x1, xzr
    mov x2, xzr
    mov x3, xzr
    dsb sy
    eret

.balign 2048
vectors:
.rept 8
    b fatal_exception
    .balign 128
.endr
    b smc_entry                     // lower EL, AArch64, synchronous
    .balign 128
.rept 7
    b fatal_exception
    .balign 128
.endr

smc_entry:
    sub sp, sp, #160
    stp x0, x1, [sp, #0]
    stp x2, x3, [sp, #16]
    stp x4, x5, [sp, #32]
    stp x6, x7, [sp, #48]
    stp x8, x9, [sp, #64]
    stp x10, x11, [sp, #80]
    stp x12, x13, [sp, #96]
    stp x14, x15, [sp, #112]
    stp x16, x17, [sp, #128]
    stp x18, x30, [sp, #144]
    mrs x9, esr_el3
    lsr x9, x9, #26
    cmp x9, #0x17                   // SMC64 only
    b.ne fatal_exception
    bl monitor_smc
    ldr x1, [sp, #8]
    ldp x2, x3, [sp, #16]
    ldp x4, x5, [sp, #32]
    ldp x6, x7, [sp, #48]
    ldp x8, x9, [sp, #64]
    ldp x10, x11, [sp, #80]
    ldp x12, x13, [sp, #96]
    ldp x14, x15, [sp, #112]
    ldp x16, x17, [sp, #128]
    ldp x18, x30, [sp, #144]
    add sp, sp, #160
    eret

fatal_exception:
    mov x0, #0
    mov x1, #99
    .inst 0xff220110                // m5_fail(0, 99)
1:  wfe
    b 1b

.bss
.balign 4096
stacks:
    .skip CPU_COUNT * 4096
