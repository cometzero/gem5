/* SPDX-License-Identifier: BSD-3-Clause */
.text
.global _start
_start:
    mov x1, #0x80000000
    add x1, x1, #0x1000
    mrs x0, clidr_el1
    str x0, [x1], #8
    mov x2, #0
1:
    msr csselr_el1, x2
    isb
    mrs x0, ccsidr_el1
    str x0, [x1], #8
    add x2, x2, #1
    cmp x2, #16
    b.ne 1b
    mov x0, #0xcafe
    str x0, [x1]
2:
    wfi
    b 2b
