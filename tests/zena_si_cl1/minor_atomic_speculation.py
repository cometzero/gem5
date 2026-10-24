# SPDX-License-Identifier: BSD-3-Clause
"""Minor wrong-path AMO regression, using a short AArch64 SE executable.

Build with: aarch64-linux-gnu-gcc -nostdlib -static -march=armv8.1-a \
  -Wl,--build-id=none minor_atomic_speculation.S -o /tmp/minor-atomic.elf
Run with: build/ARM/gem5.opt --outdir=/tmp/minor-atomic \
  tests/zena_si_cl1/minor_atomic_speculation.py --firmware=/tmp/minor-atomic.elf

Only data-memory latency is extended, so the unresolved branch permits
wrong-path address generation. Aligned SWPAL read-modify-write is checked too.
"""

import argparse
import json
from pathlib import Path

import m5
from m5.objects import (
    AddrRange,
    ArmMinorCPU,
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
parser.add_argument("--firmware", required=True)
args = parser.parse_args()
system = System(mem_mode="timing", mem_ranges=[AddrRange("128MiB")])
system.clk_domain = SrcClockDomain(
    clock="1GHz", voltage_domain=VoltageDomain()
)
system.cache_line_size = 64
system.workload = SEWorkload.init_compatible(args.firmware)
system.cpu = ArmMinorCPU()
system.cpu.workload = Process(executable=args.firmware, cmd=[args.firmware])
system.cpu.createThreads()
system.cpu.createInterruptController()
system.bus = SystemXBar()
system.memory = SimpleMemory(range=system.mem_ranges[0], latency="100ns")
system.memory.port = system.bus.mem_side_ports
system.system_port = system.bus.cpu_side_ports
system.cpu.icache = Cache(
    size="4KiB",
    assoc=2,
    tag_latency=1,
    data_latency=1,
    response_latency=1,
    mshrs=4,
    tgts_per_mshr=8,
    is_read_only=True,
)
system.cpu.icache_port = system.cpu.icache.cpu_side
system.cpu.icache.mem_side = system.bus.cpu_side_ports
system.cpu.dcache_port = system.bus.cpu_side_ports
system.cpu.mmu.connectWalkerPorts(
    system.bus.cpu_side_ports, system.bus.cpu_side_ports
)
root = Root(full_system=False, system=system)
result_path = Path(m5.options.outdir) / "result.json"
result_path.unlink(missing_ok=True)
m5.instantiate()
event = m5.simulate(100_000_000)
assert event.getCause() == "m5_exit instruction encountered", event.getCause()
assert event.getCode() == 0, event.getCode()
result = {
    "status": "PASS",
    "tick": int(m5.curTick()),
    "coverage": "wrong_path_misaligned_amo_and_committed_aligned_swpal",
    "qualification": "NATIVE_SE_NOT_ZEPHYR_OR_SILICON",
}
result_path.write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
