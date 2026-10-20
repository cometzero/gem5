/* SPDX-License-Identifier: BSD-3-Clause */
.arch armv8.5-a+sve2
.text
.global _start
_start:
    /* Same EL3 policy as the AP boot monitor. */
    ldr x0, =0x30531
    msr scr_el3, x0
    mov x0, #0x1100
    msr cptr_el3, x0
    msr zcr_el3, xzr
    adr x0, el2
    msr elr_el3, x0
    mov x0, #0x3c9
    msr spsr_el3, x0
    eret
el2:
    /* Linux __init_el2_sve clears TZ and selects the supported VL. */
    mov x0, #0x32ff
    msr cptr_el2, x0
    msr zcr_el2, xzr
    mov x0, #(1 << 31)
    msr hcr_el2, x0
    adr x0, el1
    msr elr_el2, x0
    mov x0, #0x3c5
    msr spsr_el2, x0
    eret
el1:
    mov x0, #(3 << 20)
    orr x0, x0, #(3 << 16)
    msr cpacr_el1, x0
    msr zcr_el1, xzr
    isb
    adr x0, el0
    msr elr_el1, x0
    msr spsr_el1, xzr
    eret
el0:
    mov x1, #0x80000000
    add x1, x1, #0x1000
    cntb x0
    str x0, [x1], #8
    ptrue p0.b
    dup z0.b, #3
    dup z1.b, #5
    add z0.b, z0.b, z1.b
    st1b {z0.b}, p0, [x1]
    add x1, x1, #16
    dup z2.b, #1
    eor3 z0.d, z0.d, z1.d, z2.d
    st1b {z0.b}, p0, [x1]
    add x1, x1, #16
    mov x0, #0xcafe
    str x0, [x1]
1:
    b 1b
