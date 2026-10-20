/* SPDX-License-Identifier: BSD-3-Clause */
.arch armv8-a+crypto+crc
.text
.global _start
_start:
    msr cptr_el3, xzr
    msr cptr_el2, xzr
    mov x0, #(3 << 20)
    msr cpacr_el1, x0
    isb
    mov x1, #0x80000000
    add x1, x1, #0x1000
    mrs x0, id_aa64isar0_el1
    str x0, [x1], #8
    mrs x0, S3_0_C0_C4_5
    str x0, [x1], #8
    mov w0, wzr
    mov w2, #0x31
    crc32b w0, w0, w2
    str x0, [x1], #8
    movi v0.16b, #0
    movi v1.16b, #0
    aese v0.16b, v1.16b
    str q0, [x1], #16
    mov w0, #0x5678
    movk w0, #0x1234, lsl #16
    fmov s0, w0
    sha1h s0, s0
    fmov w0, s0
    str x0, [x1], #8
    movi v0.16b, #0
    sha256su0 v0.4s, v1.4s
    str q0, [x1], #16
    mov x0, #0xcafe
    str x0, [x1]
1:
    wfi
    b 1b
