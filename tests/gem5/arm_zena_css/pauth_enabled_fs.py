# SPDX-License-Identifier: BSD-3-Clause
"""Bounded PAuth-enabled EL3 fixture, independent of the Apollo release."""

import argparse
import json
from pathlib import Path

import m5
from m5.objects import (
    AddrRange,
    ArmAtomicSimpleCPU,
    ArmFsWorkload,
    ArmO3CPU,
    ArmSystem,
    Armv83,
    Cache,
    Root,
    SimpleMemory,
    SrcClockDomain,
    SystemXBar,
    VoltageDomain,
)

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--binary", required=True)
parser.add_argument("--cpu", choices=("atomic", "o3"), required=True)
args = parser.parse_args()
system = ArmSystem(
    mem_mode="atomic" if args.cpu == "atomic" else "timing",
    highest_el_is_64=True,
    phys_addr_range_64=48,
    release=Armv83(),
    workload=ArmFsWorkload(object_file=args.binary),
    mem_ranges=[AddrRange(0x80000000, size="1MiB")],
)
system.clk_domain = SrcClockDomain(
    clock="1GHz", voltage_domain=VoltageDomain()
)
system.cpu = (ArmAtomicSimpleCPU if args.cpu == "atomic" else ArmO3CPU)()
system.bus = SystemXBar()
system.memory = SimpleMemory(range=system.mem_ranges[0], latency="10ns")
system.memory.port = system.bus.mem_side_ports
system.system_port = system.bus.cpu_side_ports
for name in ("icache", "dcache"):
    cache = Cache(
        size="16KiB",
        assoc=2,
        tag_latency=1,
        data_latency=1,
        response_latency=1,
        mshrs=4,
        tgts_per_mshr=8,
    )
    setattr(system, name, cache)
    setattr(system.cpu, name + "_port", cache.cpu_side)
    cache.mem_side = system.bus.cpu_side_ports
system.cpu.createThreads()
system.cpu.createInterruptController()
system.cpu.mmu.connectWalkerPorts(
    system.bus.cpu_side_ports, system.bus.cpu_side_ports
)
root = Root(full_system=True, system=system)
m5.instantiate()
event = m5.simulate(1_000_000_000)
passed = (
    event.getCause() == "m5_exit instruction encountered"
    and event.getCode() == 0
)
report = {
    "status": "PASS" if passed else "FAIL",
    "cpu": args.cpu,
    "cause": event.getCause(),
    "code": event.getCode(),
    "scope": "EL3 enabled APIA sign/auth, bad-modifier target abort, branch recovery",
    "qualification": "NATIVE_BAREMETAL_NOT_AP_LINUX_OR_SILICON",
}
(Path(m5.options.outdir) / "result.json").write_text(
    json.dumps(report, indent=2) + "\n"
)
print(json.dumps(report))
raise SystemExit(0 if passed else 1)
