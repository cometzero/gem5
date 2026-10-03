# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause

"""Run the PAuth HINT no-op probe without FEAT_PAuth or CPU takeover."""

import argparse
import sys

import m5
from m5.objects import (
    AddrRange,
    ArmAtomicSimpleCPU,
    ArmISA,
    ArmO3CPU,
    ArmTimingSimpleCPU,
    Armv8,
    Cache,
    Process,
    Root,
    SEWorkload,
    SimpleMemory,
    SrcClockDomain,
    System,
    SystemXBar,
    VoltageDomain,
)

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--binary", required=True)
parser.add_argument("--cpu", choices=("atomic", "timing", "o3"), required=True)
args = parser.parse_args()

system = System()
system.clk_domain = SrcClockDomain(
    clock="1GHz", voltage_domain=VoltageDomain()
)
system.mem_mode = "atomic" if args.cpu == "atomic" else "timing"
system.mem_ranges = [AddrRange("512MiB")]
cpu_class = {
    "atomic": ArmAtomicSimpleCPU,
    "timing": ArmTimingSimpleCPU,
    "o3": ArmO3CPU,
}[args.cpu]
system.cpu = cpu_class(isa=[ArmISA(release_se=Armv8())])
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
system.cpu.createInterruptController()
system.workload = SEWorkload.init_compatible(args.binary)
system.cpu.workload = Process(cmd=[args.binary])
system.cpu.createThreads()
root = Root(full_system=False, system=system)
m5.instantiate()
event = m5.simulate(1_000_000_000)
print(
    f"PAUTH_HINT_EXIT cpu={args.cpu} cause={event.getCause()} "
    f"code={event.getCode()}"
)
sys.exit(
    event.getCode()
    or int(event.getCause() != "exiting with last active thread context")
)
