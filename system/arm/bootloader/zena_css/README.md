# Zena CSS AP simulation firmware

This small EL3 monitor boots the AP-only gem5 Zena configuration. It is not
production secure firmware and does not model RSE, SCP, TF-A, Zena power
controllers, authentication, retention or isolation of EL3 RAM from Linux.
The platform must reserve `[0xffe00000, 0x100000000)` from Linux allocation.

Build from the workspace root:

```sh
make -C gem5/system/arm/bootloader/zena_css \
    BUILDDIR="$PWD/build/gem5-zena/firmware"
```

Select `zena_css4.elf` or `zena_css16.elf` to match the simulated CPU count.
All CPUs reset at `_start` (`0xffe00000`) in AArch64 EL3. The standard gem5
`ArmFsLinux` loader supplies x0 = DTB and x3 = Linux physical entry on each
CPU. The monitor sets CNTFRQ = 125 MHz, enables the non-secure GIC Group 1
interface, and enters the primary at non-secure EL2 with x0 = DTB.
MPIDR affinity must be `(cpu_index / 4) << 16 | (cpu_index % 4) << 8`.
Secondary CPUs wait inside EL3 until a PSCI CPU_ON request arrives.

The DT uses `arm,psci-1.0`, `method = "smc"` and per-CPU
`enable-method = "psci"`. The monitor implements a **PSCI 1.0 subset**:
VERSION, FEATURES, CPU_ON (32/64), CPU_OFF, AFFINITY_INFO (32/64, level 0)
and SYSTEM_OFF. CPU_ON validates the affinity, alignment and RAM entry;
returns ALREADY_ON/ON_PENDING when appropriate; and passes the supplied
context in x0 at the requested NS EL2 entry. Release/acquire state accesses,
a serialized CPU_ON publisher, DSB and SEV protect the parking handshake.
CPU_OFF discards its old monitor stack frame before waiting for another
CPU_ON. Each CPU has a separate 4 KiB EL3 stack.

SYSTEM_OFF prints `ZENA_GEM5_PSCI_SYSTEM_OFF` on PL011 and invokes `m5_exit`.
Unexpected EL3 exceptions invoke `m5_fail(0, 99)`. The simulator distinguishes
these exit causes from a simulation time limit; the Linux marker remains
necessary to attribute an exit to a guest shutdown.

In the default firmware, SYSTEM_RESET, SYSTEM_RESET2, CPU_SUSPEND, MIGRATE
and higher-level
AFFINITY_INFO are not implemented. Unsupported calls return NOT_SUPPORTED,
except invalid affinity/level arguments, which return INVALID_PARAMETERS.
FEATURES does not advertise RESET or SUSPEND. Because SYSTEM_RESET is absent,
this is not a complete PSCI-conformant firmware implementation. A launcher
restart is not a guest reboot and must not be reported as reset validation.

The firmware's ELF segments are RX and RW, but this does not enforce guest
access permissions while the EL3 MMU is disabled. The implementation is a
functional simulation monitor, not a security boundary or power model.


## Optional software warm reset

`make reset` also builds `zena_css{4,16}_reset.elf`. These images export
`zena_warm_reset = 1` (normal images export zero), advertise SYSTEM_RESET,
and issue `m5_work_begin(0x5a525354, 0)` for that request. The board must enable
work-item exit events and handle exactly that code in the same simulator
process. An ordinary firmware ELF must never advertise this feature.

The configuration drains the simulation, writes back and invalidates caches,
and invokes `ZenaFsLinux.resetGuest()`. That method quiesces all architectural
timers and event streams, PL011 IRQ/event state and PL031 IRQ/match state,
resets synchronous virtio devices, resets GIC distributor/redistributor/CPU
interface state, flushes all CPU TLBs, restores boot images and resets all
CPU architectural state. The next `simulate()` resumes the CPUs at EL3.
The system counter, RTC time, disk contents and RAM outside restored image
segments survive this software warm reset. This is not a hardware cold reset.

The initial reset API rejects CPU types other than AtomicSimpleCPU. ITS,
Ruby and asynchronous device backends are outside its validated contract.
Reset functionality requires actual second-boot and repeated-boot tests;
firmware build success alone does not establish it.


## Dispatcher negative self-test

Before publishing the secondary-CPU release flag, the primary calls the same
PSCI dispatcher through a volatile function pointer. Fifteen cases check
VERSION/FEATURES, unknown calls, nonexistent affinity, unaligned/out-of-RAM
CPU_ON entry, ALREADY_ON, invalid AFFINITY_INFO level, OFF state, the optional
RESET feature and SMC32 argument truncation. The test then verifies that all
CPU states, entry/context slots, the request lock and the release flag are
unchanged. Success prints
`ZENA_GEM5_PSCI_DISPATCH_SELFTEST=PASS cases=15`; failure prints its zero-based
case index (15 means final state validation) and executes `m5_fail(0, 100)`.

This is a **dispatcher self-test**, not proof of exception entry/return or
power control. Linux SMP bring-up and CPU hotplug test the actual SMC ABI and
CPU_ON/OFF execution separately. The self-test runs again on a software warm
reset. Inspect the complete gem5 terminal dump because an early UART marker
can leave the socket's finite history buffer before the host connects.
