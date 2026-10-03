/* SPDX-License-Identifier: BSD-3-Clause */
OUTPUT_ARCH(aarch64)
ENTRY(_start)
PHDRS
{
    text PT_LOAD FLAGS(5);
    data PT_LOAD FLAGS(6);
}
SECTIONS
{
    . = 0xffe00000;
    .text : { *(.text*) } :text
    .rodata : { *(.rodata*) } :text
    . = ALIGN(4096);
    .data : { *(.data*) } :data
    .bss : { *(.bss*) *(COMMON) } :data
    ASSERT(. < 0xfff00000, "firmware exceeds reserved region")
    /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) }
}
