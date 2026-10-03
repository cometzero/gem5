/* Copyright (c) 2026 Chanho Park */
/* SPDX-License-Identifier: BSD-3-Clause */

/* All PAuth HINT encodings must preserve their destination without FEAT_PAuth.
 * This freestanding Linux/SE program does not depend on libc or a loader.
 */
.arch armv8.3-a
.file "pauth_hint_probe.S"
.text
.global _start
.type _start, %function

.macro check_hint instruction, destination, bit
    mov     x19, #0x1234
    movk    x19, #0x5678, lsl #16
    movk    x19, #0x9abc, lsl #32
    mov     \destination, x19
    \instruction
    cmp     \destination, x19
    b.eq    .Lok\@
    orr     x20, x20, #(1 << \bit)
.Lok\@:
.endm

_start:
    mov     x20, #0
    mov     x16, #0x5678
    check_hint pacia1716, x17, 0
    check_hint paciasp, x30, 1
    check_hint paciaz, x30, 2
    check_hint pacib1716, x17, 3
    check_hint pacibsp, x30, 4
    check_hint pacibz, x30, 5
    check_hint autia1716, x17, 6
    check_hint autiasp, x30, 7
    check_hint autiaz, x30, 8
    check_hint autib1716, x17, 9
    check_hint autibsp, x30, 10
    check_hint autibz, x30, 11
    check_hint xpaclri, x30, 12

    /* Print the complete 13-bit failure mask, including bits above exit(2). */
    adr     x1, result
    adr     x2, digits
    add     x3, x1, #(hex_value - result)
    mov     x4, #12
.Lhex:
    lsr     x5, x20, x4
    and     x5, x5, #15
    ldrb    w5, [x2, x5]
    strb    w5, [x3], #1
    subs    x4, x4, #4
    b.ge    .Lhex
    mov     x0, #1
    mov     x2, #(result_end - result)
    mov     x8, #64
    svc     #0
    cmp     x20, #0
    cset    x0, ne
    mov     x8, #93
    svc     #0
.size _start, . - _start

.section .rodata
digits:
    .ascii "0123456789abcdef"
.section .data
result:
    .ascii "PAUTH_HINT_NO_PAUTH cases=13 failures=0x"
hex_value:
    .ascii "0000\n"
result_end:
.section .note.GNU-stack,"",%progbits
