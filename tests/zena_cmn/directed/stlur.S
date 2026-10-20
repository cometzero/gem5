/* SPDX-License-Identifier: BSD-3-Clause */
.arch armv8.4-a
.text
.global _start
_start:
    mov x1, #0x80000000
    add x1, x1, #0x1000
    add x2, x1, #0x100
    mov x3, #0xaaaaaaaaaaaaaaaa
    stp x3, x3, [x2, #-16]
    stp x3, x3, [x2]
    stp x3, x3, [x2, #16]
    mov x3, #0x7788
    movk x3, #0x5566, lsl #16
    movk x3, #0x3344, lsl #32
    movk x3, #0x1122, lsl #48
    stlurb w3, [x2, #-1]
    stlurh w3, [x2, #2]
    stlur w3, [x2, #8]
    stlur x3, [x2, #16]
    ldr x4, [x2, #-8]
    str x4, [x1], #8
    ldr x4, [x2]
    str x4, [x1], #8
    ldr x4, [x2, #8]
    str x4, [x1], #8
    ldr x4, [x2, #16]
    str x4, [x1], #8
    ldapur w4, [x2, #8]
    str x4, [x1], #8
    ldapur x4, [x2, #16]
    str x4, [x1], #8
    mov x5, sp
    mov sp, x2
    stlur x3, [sp, #-16]
    ldapur x4, [sp, #-16]
    mov sp, x5
    str x4, [x1], #8
    stlur xzr, [x2, #16]
    ldapur x4, [x2, #16]
    str x4, [x1], #8
    mov x0, #0xcafe
    str x0, [x1]
1:
    wfi
    b 1b
