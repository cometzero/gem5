# ARM baseline control for the Zena changes

`control_fs.py` calls upstream `starter_fs.create()` using a stock
`VExpress_GEM5_V2` platform, AtomicSimpleCPU and the default ARM affinity path.
It uses the original GICv3/PL011/PCI models and original spin-table bootloader.
The shared minimal kernel has no CLK_VEXPRESS driver. The fixture therefore
changes the generated UART clock-provider DT nodes to fixed-clock at their
existing model frequencies: MCC peripheral 24 MHz and DCC SMB 50 MHz. The
SimObjects remain unchanged. `control-dt-adaptations.json` records this
change; configurable VExpress clocks and unrelated peripheral clocks are
not qualified by this control.
This is a separate regression control, not a successful Zena test.

From the parent OSS workspace, prepare the unmodified bootloader:

```sh
mkdir -p build/gem5-zena/control/binaries
make -C gem5/system/arm/bootloader/arm64 \
  BUILDDIR="$PWD/build/gem5-zena/control/binaries" \
  "$PWD/build/gem5-zena/control/binaries/boot_v2.arm64"
```

Run with the same Linux/rootfs artifacts as the Zena test:

```sh
gem5/build/ARM/gem5.opt --listener-mode=on \
  --outdir=build/gem5-zena/control/run \
  gem5/tests/gem5/arm_zena_css/control_fs.py \
  --kernel=build/gem5-zena/linux/vmlinux \
  --initrd=build/buildroot/arm64/images/rootfs.cpio.gz \
  --binaries=build/gem5-zena/control/binaries \
  --cpus=4 --terminal-port=3956
```

The terminal is a raw TCP socket on localhost:3956. UART output is also saved
as `system.terminal` below the run directory. Mount proc/sysfs and check
`uname -a`, `/sys/devices/system/cpu/online` (`0-3`) and
`/proc/device-tree/cpus/cpu@*/reg` (default affinities 0, 1, 2, 3).
Use `--dtb-only` for source configuration checks without simulated execution.
A shell/CPU check followed by host termination establishes boot/SMP bring-up
only; it is not a PSCI shutdown, hotplug or reset result. The stock bootloader
uses spin-table and has no PSCI monitor.


For automated shell, default-affinity and timer-IRQ checks, use
`run_control.py --gem5 <binary> --kernel <vmlinux> --initrd <cpio.gz>
--binaries <bootloader-directory> --output <new-directory>`. It appends a
small guest m5_exit helper, keeps command/console/interrupt artifacts and
writes `result.json`. Successful guest m5_exit is explicitly not a PSCI
poweroff test.

The automated runner decompresses gzip input on the host and supplies raw
CPIO to avoid spending simulation time on decompression. The minimal kernel
has CONFIG_RANDOMIZE_BASE disabled; omit `nokaslr`, which that kernel would
otherwise pass to `/bin/sh` as a filename argument.
