/* SPDX-License-Identifier: BSD-3-Clause */
ENTRY(_start)
SECTIONS
{
    . = 0x140000000;
    .text : { KEEP(*(.text.start)) *(.text*) *(.rodata*) }
    ASSERT(. < 0x140020000, "probe text exceeds MPU code region")
    . = 0x140020000;
    .data : { *(.data*) }
    .bss (NOLOAD) : {
        . = ALIGN(8); __bss_start = .; *(.bss*) *(COMMON)
        . = ALIGN(8); __bss_end = .;
    }
    ASSERT(. < 0x140030000, "probe globals exceed data region")
    __el1_stack_top = 0x140038000;
    __stack_top = 0x140040000;
    /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) }
}
