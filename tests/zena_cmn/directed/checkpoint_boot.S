/* SPDX-License-Identifier: BSD-3-Clause */
/* Native save/restore probe, not a Linux boot qualification. */
.global _start, zena_cpu_count, zena_warm_reset
.set zena_cpu_count, 4
.set zena_warm_reset, 0
.text
_start:
    mrs x0, mpidr_el1
    and x0, x0, #0xffffff
    cbnz x0, parked
    mov x19, #0x1234
    mov x20, #0x80000000
    mov x0, #0xbeef
    str x0, [x20, #0x1000]
    mov x0, #0
    mov x1, #0
    .inst 0xff430110 /* m5_checkpoint */
    mov x1, #0x1234
    cmp x19, x1
    b.ne failed
    ldr x0, [x20, #0x1000]
    mov x1, #0xbeef
    cmp x0, x1
    b.ne failed
    add x0, x20, #0x2000
    mov x1, #1
    mov x2, #0
    .inst 0xff500110 /* m5_read_file: host file must change on restore */
    cmp x0, #1
    b.ne failed
    add x3, x20, #0x2000
    ldrb w0, [x3]
    cmp w0, #82 /* R */
    b.ne failed
    mov x0, #0x5743
    movk x0, #0x5a53, lsl #16
    mov x1, #0
    .inst 0xff5a0110 /* switch to O3 */
    mov x0, #0
    .inst 0xff5a0110 /* ROI begin */
    ldr x2, [x20, #0x1000]
    mov x0, #0
    .inst 0xff5b0110 /* ROI end */
    mov x0, #0
    .inst 0xff210110 /* success */
failed:
    mov x0, #0
    mov x1, #1
    .inst 0xff220110
parked:
    wfi
    b parked
