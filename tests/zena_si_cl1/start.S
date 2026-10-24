/* SPDX-License-Identifier: BSD-3-Clause */
.section .text.start, "ax"
.global _start
_start:
    mrs x0, mpidr_el1
    and x0, x0, #0xffff
    cbz x0, 9f
8:  wfi
    b 8b
9:
#ifdef SI_CL1_PRIMARY
    mrs x0, mpidr_el1
    ubfx x0, x0, #8, #8
    cbz x0, 4f
5:  wfi
    b 5b
4:
#endif
    msr daifset, #15
    msr spsel, #1
    ldr x0, =__stack_top
    mov sp, x0
    ldr x0, =vectors
    msr vbar_el2, x0
    msr vbar_el1, x0
    ldr x0, =__bss_start
    ldr x1, =__bss_end
1:  cmp x0, x1
    b.hs 2f
    str xzr, [x0], #8
    b 1b
2:  isb
    bl main
3:  wfi
    b 3b

.balign 2048
.global vectors
vectors:
.rept 16
    b exception_entry
    .balign 128
.endr

exception_entry:
    sub sp, sp, #256
    stp x0, x1, [sp, #0]
    stp x2, x3, [sp, #16]
    stp x4, x5, [sp, #32]
    stp x6, x7, [sp, #48]
    stp x8, x9, [sp, #64]
    stp x10, x11, [sp, #80]
    stp x12, x13, [sp, #96]
    stp x14, x15, [sp, #112]
    stp x16, x17, [sp, #128]
    stp x18, x19, [sp, #144]
    stp x20, x21, [sp, #160]
    stp x22, x23, [sp, #176]
    stp x24, x25, [sp, #192]
    stp x26, x27, [sp, #208]
    stp x28, x29, [sp, #224]
    str x30, [sp, #240]
    bl exception_handler
    mrs x1, CurrentEL
    cmp x1, #8
    b.ne 1f
    msr elr_el2, x0
    b 2f
1:  msr elr_el1, x0
2:  ldp x0, x1, [sp, #0]
    ldp x2, x3, [sp, #16]
    ldp x4, x5, [sp, #32]
    ldp x6, x7, [sp, #48]
    ldp x8, x9, [sp, #64]
    ldp x10, x11, [sp, #80]
    ldp x12, x13, [sp, #96]
    ldp x14, x15, [sp, #112]
    ldp x16, x17, [sp, #128]
    ldp x18, x19, [sp, #144]
    ldp x20, x21, [sp, #160]
    ldp x22, x23, [sp, #176]
    ldp x24, x25, [sp, #192]
    ldp x26, x27, [sp, #208]
    ldp x28, x29, [sp, #224]
    ldr x30, [sp, #240]
    add sp, sp, #256
    eret

.macro set_recovery label
    adr x2, \label
    adrp x3, recovery_pc
    str x2, [x3, :lo12:recovery_pc]
.endm

.macro clear_recovery
    adrp x3, recovery_pc
    str xzr, [x3, :lo12:recovery_pc]
.endm

.global try_read
try_read:
    set_recovery 1f
    ldr x0, [x0]
1:  clear_recovery
    ret

.global try_write
try_write:
    set_recovery 1f
    str x1, [x0]
1:  clear_recovery
    ret

.global try_exec
try_exec:
    stp x29, x30, [sp, #-16]!
    set_recovery 1f
    blr x0
1:  ldp x29, x30, [sp], #16
    clear_recovery
    ret

.global try_write32
try_write32:
    set_recovery 1f
    str w1, [x0]
1:  clear_recovery
    ret

.global try_el1_illegal
try_el1_illegal:
    set_recovery 1f
    mrs x0, sctlr_el2
1:  clear_recovery
    ret

.global try_el1_trvm
try_el1_trvm:
    set_recovery 1f
    mrs x0, S3_0_C6_C8_0 /* PRBAR_EL1 */
1:  clear_recovery
    ret

.global try_tcm_read
try_tcm_read:
    set_recovery 1f
    mrs x0, S3_0_C15_C0_2
1:  clear_recovery
    ret

.global try_tcm_write
try_tcm_write:
    set_recovery 1f
    msr S3_0_C15_C0_2, x0
1:  clear_recovery
    ret

.global enter_el1
enter_el1:
    stp x19, x20, [sp, #-96]!
    stp x21, x22, [sp, #16]
    stp x23, x24, [sp, #32]
    stp x25, x26, [sp, #48]
    stp x27, x28, [sp, #64]
    stp x29, x30, [sp, #80]
    mov x19, x0
    ldr x0, =__el1_stack_top
    msr sp_el1, x0
    mov x0, #0x3c5 /* AArch64 EL1h, all DAIF masked */
    msr spsr_el2, x0
    adr x0, el1_entry
    msr elr_el2, x0
    adr x0, el2_return
    adrp x1, el2_return_pc
    str x0, [x1, :lo12:el2_return_pc]
    isb
    eret
el1_entry:
    blr x19
    hvc #0x7ffe
el2_return:
    ldp x21, x22, [sp, #16]
    ldp x23, x24, [sp, #32]
    ldp x25, x26, [sp, #48]
    ldp x27, x28, [sp, #64]
    ldp x29, x30, [sp, #80]
    ldp x19, x20, [sp], #96
    ret
