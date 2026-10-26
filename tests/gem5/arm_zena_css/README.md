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


## BTI compatibility and Linux PAuth probes

`run_bti_hint.py --gem5 /path/to/gem5.opt --output /tmp/bti-probe`
builds `bti_hint_probe.S` and reuses the no-extension Armv8 SE fixture for
Atomic, Timing and O3. Four BTI encodings preserve registers, SP and NZCV
through 1,024 iterations. O3 must have no non-speculative commit stalls beyond
the two exit/report syscalls. The old decoder produces 4,098 such stalls even
though the functional check passes.

FEAT_BTI is not implemented: `ID_AA64PFR1_EL1.BT` stays zero and HWCAP2 does
not advertise it. BTI therefore decodes as an architectural NOP, as specified
by [Arm A64 ISA, BTI](https://documentation-service.arm.com/static/685eb35c3cb5b729562ba389).
This is **not** guarded-page enforcement or full Armv8.5/A720AE feature parity.
Do not advertise FEAT_BTI without adding BTYPE, guarded-page and exception
semantics. Other HINT encodings and PAC instruction behavior are unchanged.

Build `pauth_linux_probe.c` and `pauth_branch_recovery.S` together with the
same guest compiler/sysroot and `-march=armv8.3-a`. Run the executable directly
under Linux for Atomic or under `gem5-control run` for O3 in a separate
snapshot restore. It checks HWCAP_PACA, observes an actual signed-pointer
change, verifies successful authentication, rejects a wrong modifier, checks
register recovery after a trained branch changes direction, and requires a
SIGSEGV when an invalid authenticated pointer is branched to in a child.
Exit 77 is SKIP, not PASS, when authentication is unavailable or not observed.
The branch check does not alone prove that the wrong-path instruction issued;
that requires the corresponding trace/counters. This probe does not cover
all PAuth key types or FPAC exception extensions.


`ID_AA64PFR1_EL1.BT` is zero because `src/arch/arm/regs/misc.cc` initializes
AA64PFR1 from zero and populates only SME and MPAMFrac. `ArmExtension` has no
FEAT_BTI implementation and `src/arch/arm/process.cc` does not set HWCAP2.BTI.
The default Apollo `configs/zena_cmn/cpu_profile.py:ap_release()` also does
not enable FEAT_PAuth. Its Linux probe must report SKIP for enabled-PAC checks;
CPU `release_se` parameters are not the FS system's active feature set.

For a separate enabled-PAC test, build and run:

```sh
aarch64-linux-gnu-gcc -nostdlib -static -Wl,--build-id=none \
  -Wl,-Ttext=0x80010000 pauth_enabled_probe.S -o /tmp/pauth-enabled.elf
/path/to/gem5.opt --outdir=/tmp/pac-atomic pauth_enabled_fs.py \
  --binary=/tmp/pauth-enabled.elf --cpu=atomic
/path/to/gem5.opt --outdir=/tmp/pac-o3 pauth_enabled_fs.py \
  --binary=/tmp/pauth-enabled.elf --cpu=o3
```

This bounded, independent Armv8.3 fixture sets APIA keys and SCTLR_EL3.EnIA,
requires signing to change the pointer, checks correct authentication and
branch, rejects a wrong modifier, checks branch recovery, and verifies a bad
authenticated target raises an instruction abort (ESR_EL3.EC=0x21). No Linux
or production AP feature configuration is changed. `--debug-flags=O3PipeView`
can additionally record issued-but-not-retired authentication instructions;
this does not establish physical speculative side-channel behavior.
