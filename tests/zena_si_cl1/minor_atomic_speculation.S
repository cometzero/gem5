/* SPDX-License-Identifier: BSD-3-Clause */
/* Train a conditional branch, then expose a wrong-path misaligned SWPAL. */
.arch armv8.1-a
.text
.global _start
_start:
    adr x20, atomic_word
    adr x21, branch_word
    mov x22, #64
    mov x23, #0
    mov x0, #1
    str x0, [x21]
    dsb sy
loop:
    ldr x1, [x21]
    cbz x1, passed
atomic_path:
    swpal xzr, x0, [x20]
    cbnz x23, failed
    subs x22, x22, #1
    b.ne loop
    mov x23, #1
    mov x20, #0x7fffffff
    str xzr, [x21]
    dsb sy
    b loop
passed:
    cmp x23, #1
    b.ne failed
    adr x20, atomic_word
    mov x1, #7
    swpal x1, x0, [x20]
    cbnz x0, failed
    ldr x0, [x20]
    cmp x0, #7
    b.ne failed
    mov x0, #0
    .inst 0xff210110            // m5_exit.
failed:
    mov x0, #0
    mov x1, #1
    .inst 0xff220110            // m5_fail.
    b failed
.data
.balign 64
atomic_word:
    .quad 0
.balign 64
branch_word:
    .quad 0
