# SPDX-License-Identifier: BSD-3-Clause
CROSS_COMPILE ?= aarch64-linux-gnu-
CC = $(CROSS_COMPILE)gcc
LD = $(CROSS_COMPILE)ld
BUILDDIR ?= build
CFLAGS = -march=armv8-a -mgeneral-regs-only -mno-outline-atomics \
         -ffreestanding -fno-stack-protector -fno-pic -fno-builtin \
         -fno-unwind-tables -fno-asynchronous-unwind-tables -O2 -g \
         -Wall -Wextra -Werror

.PHONY: all clean reset
all: $(BUILDDIR)/zena_css4.elf $(BUILDDIR)/zena_css16.elf

reset: $(BUILDDIR)/zena_css4_reset.elf $(BUILDDIR)/zena_css16_reset.elf

$(BUILDDIR)/boot%_reset.o: boot.S | $(BUILDDIR)
	$(CC) $(CFLAGS) -DCPU_COUNT=$* -DWARM_RESET=1 -c $< -o $@

$(BUILDDIR)/monitor%_reset.o: monitor.c | $(BUILDDIR)
	$(CC) $(CFLAGS) -DCPU_COUNT=$* -DWARM_RESET=1 -c $< -o $@

$(BUILDDIR)/zena_css%_reset.elf: $(BUILDDIR)/boot%_reset.o \
                              $(BUILDDIR)/monitor%_reset.o link.ld
	$(LD) -T link.ld --build-id=none -o $@ $(filter %.o,$^)

$(BUILDDIR):
	mkdir -p $@

$(BUILDDIR)/boot%.o: boot.S | $(BUILDDIR)
	$(CC) $(CFLAGS) -DCPU_COUNT=$* -c $< -o $@

$(BUILDDIR)/monitor%.o: monitor.c | $(BUILDDIR)
	$(CC) $(CFLAGS) -DCPU_COUNT=$* -c $< -o $@

$(BUILDDIR)/zena_css%.elf: $(BUILDDIR)/boot%.o \
                        $(BUILDDIR)/monitor%.o link.ld
	$(LD) -T link.ld --build-id=none -o $@ $(filter %.o,$^)

clean:
	rm -f $(BUILDDIR)/boot*.o $(BUILDDIR)/monitor*.o \
	      $(BUILDDIR)/zena_css*.elf
