# SPDX-License-Identifier: BSD-3-Clause
"""Exercise Zena DRAM channel wiring without Linux/cache bottlenecks.

Run under gem5.opt. --check-data uses MemTest's checked reads/writes near
both ends of the 2 GiB map. Otherwise generate linear and random traffic.
This qualifies the generic model only, not LPDDR5X silicon or a Thor DMC.
"""

import argparse
import json
import sys
from pathlib import Path

import m5
from m5.objects import (
    AddrRange,
    IOXBar,
    MemTest,
    PyTrafficGen,
    Root,
    SrcClockDomain,
    System,
    VoltageDomain,
)

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "configs/example/arm")
)
import zena_css_models as models

parser = argparse.ArgumentParser(description=__doc__)
models.add_options(parser)
models.add_block_options(parser)
parser.add_argument("--check-data", action="store_true")
parser.set_defaults(
    cpus=4,
    cpu_clock="2GHz",
    cpu_mode="timing",
    roi_cpu_mode=None,
    io_profile="minimal",
    memory_model="lpddr5x-8533",
    memory_channels=4,
)
args = parser.parse_args()
model = models.settings(args)
if args.memory_model == "simple":
    parser.error("this fixture requires DRAM")
base, capacity = 0x80000000, 2 * 1024**3
system = System(mem_mode="timing", mem_ranges=[AddrRange(base, size=capacity)])
system.voltage_domain = VoltageDomain()
system.clk_domain = SrcClockDomain(
    clock=model["fabric_clock"], voltage_domain=system.voltage_domain
)
system.membus = IOXBar()
system.system_port = system.membus.cpu_side_ports
models.configure_memory(system, model)
assert sum(c.dram.range.size() for c in system.mem_ctrls) == capacity
for index, controller in enumerate(system.mem_ctrls):
    dram = controller.dram
    assert dram.range.size() == (
        int(dram.device_size.value)
        * int(dram.devices_per_rank.value)
        * int(dram.ranks_per_channel.value)
    )
    assert dram.range.intlvMatch == index
if args.check_data:
    system.tester = MemTest(
        interval=20,
        size=1024**2,
        base_addr_1=base,
        base_addr_2=base + capacity - 1024**2,
        percent_functional=0,
        percent_uncacheable=0,
        max_loads=10000,
    )
    system.tester.port = system.membus.cpu_side_ports
else:
    system.generators = [PyTrafficGen() for _ in range(args.memory_channels)]
    for generator in system.generators:
        generator.port = system.membus.cpu_side_ports
root = Root(full_system=False, system=system)
m5.instantiate()
if not args.check_data:
    for index, generator in enumerate(system.generators):
        # Stagger the starting channel. All generators span many bank rows.
        start = base + index * 64
        generator.start(
            [
                generator.createLinear(
                    100_000_000, start, base + capacity, 64, 1000, 1000, 50, 0
                ),
                generator.createRandom(
                    100_000_000, start, base + capacity, 64, 1000, 1000, 50, 0
                ),
                generator.createIdle(10_000_000),
            ]
        )
event = m5.simulate(2_000_000_000 if args.check_data else 205_000_000)
expected = (
    "maximum number of loads reached"
    if args.check_data
    else "simulate() limit reached"
)
if event.getCause() != expected:
    raise RuntimeError(event.getCause())
m5.stats.dump()
# Check that all channels actually serviced reads and writes. Native MemTest
# additionally compares returned data against its reference contents.
stats = {}
for line in Path(m5.options.outdir, "stats.txt").read_text().splitlines():
    fields = line.split()
    if len(fields) >= 2:
        try:
            stats[fields[0]] = float(fields[1])
        except ValueError:
            pass
channel_results = []
for index in range(args.memory_channels):
    prefix = f"system.mem_ctrls{index}.dram"
    read = stats[prefix + ".dramBytesRead"]
    written = stats[prefix + ".dramBytesWritten"]
    assert read > 0 and written > 0, (index, read, written)
    channel_results.append(
        {"channel": index, "read_bytes": read, "write_bytes": written}
    )
Path(m5.options.outdir, "fixture-result.json").write_text(
    json.dumps(
        {
            "result": "PASS",
            "check_data": args.check_data,
            "cause": event.getCause(),
            "tick": m5.curTick(),
            "model": model,
            "channels": channel_results,
        },
        indent=2,
    )
    + "\n"
)
