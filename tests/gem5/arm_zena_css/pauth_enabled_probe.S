/* SPDX-License-Identifier: BSD-3-Clause */
/* EL3-only native fixture: enable real PAC, never change the AP profile. */
.arch armv8.3-a
.text
.global _start
_start:
    mov x19, #0
    mrs x0, CurrentEL
    cmp x0, #12
    b.ne failed
    adr x0, vectors
    msr vbar_el3, x0
    mov x0, #16
    msr tcr_el3, x0
    mov x0, #0x1234
    msr APIAKeyLo_EL1, x0
    mov x0, #0x5678
    msr APIAKeyHi_EL1, x0
    mrs x0, sctlr_el3
    orr x0, x0, #(1 << 31)
    msr sctlr_el3, x0
    isb
    adr x20, valid_target
    mov x21, #0x3456
    mov x22, x20
    pacia x22, x21
    cmp x22, x20
    b.eq failed                 // Signing must actually be enabled.
    mov x23, x22
    autia x23, x21
    cmp x23, x20
    b.ne failed
    blr x23                    // Authenticated valid branch must return.
    mov x24, #0x789a
    mov x23, x22
    autia x23, x24
    cmp x23, x20
    b.eq failed                // Wrong modifier must not recover pointer.

    // Train a branch, then check a wrong-path auth cannot escape recovery.
    adr x25, flag
    mov x26, #128
    mov x27, #1
    str x27, [x25]
    mov x28, x22
1:
    ldr x27, [x25]
    cbz x27, 2f
    autia x28, x21
    mov x28, x22
    subs x26, x26, #1
    b.ne 1b
    mov x21, x24
    str xzr, [x25]
    b 1b
2:
    cmp x28, x22
    b.ne failed
    // A bad target must raise a same-EL instruction abort, not execute.
    mov x19, #1
    br x23
valid_target:
    ret
failed:
    mov x0, #0
    mov x1, #1
    .inst 0xff220110            // m5_fail.
    b failed
.balign 2048
vectors:
.rept 16
    b exception
    .space 124
.endr
exception:
    cmp x19, #1
    b.ne failed
    mrs x0, esr_el3
    lsr x0, x0, #26
    cmp x0, #0x21              // Instruction abort from same EL.
    b.ne failed
    mov x0, #0
    .inst 0xff210110            // m5_exit.
    b failed
.data
.balign 64
flag:
    .quad 0
.section .note.GNU-stack,"",%progbits
