/* SPDX-License-Identifier: BSD-3-Clause */
/* Bare AArch64 SCMI polling probe; no firmware or Linux dependency.
 * Build with aarch64-linux-gnu-as then ld -N -Ttext=0x80000000 -e _start.
 * CPU0 discovers all domains, sets each to 2500, then reads it back.
 */
.text
.global _start
_start:
    mrs x0, mpidr_el1
    and x0, x0, #0xffffff
    cbnz x0, parked
    mov x10, #0x2a020000         // Shared SCMI buffer.
    mov x11, #0x2a000000        // MHU high-priority AP-to-SCP doorbell.
    mov w12, #1
    str wzr, [x10, #16]         // Polling; no completion interrupt.

    mov w0, #0x4c01            // Performance protocol attributes.
    mov w1, #4
    bl command
    ldr w8, [x10, #32]
    and w8, w8, #0xffff
    cbz w8, failed
    mov w9, #0
domain:
    str w9, [x10, #28]
    mov w0, #2500
    str w0, [x10, #32]
    mov w0, #0x4c07            // PERFORMANCE_LEVEL_SET.
    mov w1, #12
    bl command
    str w9, [x10, #28]
    mov w0, #0x4c08            // PERFORMANCE_LEVEL_GET.
    mov w1, #8
    bl command
    ldr w0, [x10, #32]
    cmp w0, #2500
    b.ne failed
    add w9, w9, #1
    cmp w9, w8
    b.lo domain
    mov x0, #0
    .inst 0xff210110           // m5_exit(0).
parked:
    wfi
    b parked
failed:
    mov x0, #0
    mov x1, #1
    .inst 0xff220110           // m5_fail(0, 1).
    b parked
command:
    str w1, [x10, #20]
    str w0, [x10, #24]
    str wzr, [x10, #4]
    dsb sy
    str w12, [x11, #0x128]
poll:
    ldr w0, [x10, #4]
    tbz w0, #0, poll
    dsb sy
    ldr w0, [x10, #28]
    cbnz w0, failed
    ret
