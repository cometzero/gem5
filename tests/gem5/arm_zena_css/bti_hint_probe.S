/* SPDX-License-Identifier: BSD-3-Clause */
/* BTI's four encodings must preserve state when FEAT_BTI is absent. */
.text
.global _start
_start:
    mov x0, #0x1234
    mov x1, #0x5678
    mov x17, #0x1357
    mov x30, #0x2468
    mov x19, sp
    mov x20, #1024
loop:
    cmp x0, x1
    mrs x21, nzcv
    .inst 0xd503241f           // BTI.
    .inst 0xd503245f           // BTI c.
    .inst 0xd503249f           // BTI j.
    .inst 0xd50324df           // BTI jc.
    mrs x22, nzcv
    cmp x21, x22
    b.ne failed
    mov x23, #0x1234
    cmp x0, x23
    b.ne failed
    mov x23, #0x5678
    cmp x1, x23
    b.ne failed
    mov x23, #0x1357
    cmp x17, x23
    b.ne failed
    mov x23, #0x2468
    cmp x30, x23
    b.ne failed
    mov x23, sp
    cmp x19, x23
    b.ne failed
    subs x20, x20, #1
    b.ne loop
    mov x0, #1
    adr x1, message
    mov x2, #message_end-message
    mov x8, #64
    svc #0
    mov x0, #0
    b exit
failed:
    mov x0, #1
exit:
    mov x8, #93
    svc #0
message:
    .ascii "BTI_HINT_NO_BTI variants=4 iterations=1024 state=PASS\n"
message_end:
