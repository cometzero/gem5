/* SPDX-License-Identifier: BSD-3-Clause */
/* Train an authentication path, then verify squashed writes cannot escape. */
.arch armv8.3-a
.text
.global pauth_branch_recovery
.type pauth_branch_recovery, %function
pauth_branch_recovery:
    mov x3, x2
    mov x2, x0
    mov x4, #128
    mov w5, #1
    str w5, [x3]
1:
    ldr w5, [x3]
    cbz w5, 2f
    autia x2, x1
    mov x2, x0
    subs x4, x4, #1
    b.ne 1b
    eor x1, x1, #1
    str wzr, [x3]
    b 1b
2:
    cmp x2, x0
    cset w0, ne
    ret
.size pauth_branch_recovery, .-pauth_branch_recovery
.section .note.GNU-stack,"",%progbits
