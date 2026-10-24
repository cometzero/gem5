# SPDX-License-Identifier: BSD-3-Clause
"""A reset must not swallow a same-tick CPU power-domain transition."""

import argparse
import json
from pathlib import Path

import m5
from m5.objects import (
    AddrRange,
    ArmAtomicSimpleCPU,
    ArmFsWorkload,
    ArmSystem,
    Armv8,
    IOXBar,
    PowerActor,
    PowerDomain,
    Root,
    SimpleMemory,
    SrcClockDomain,
    VoltageDomain,
    ZenaCSS,
)

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--firmware", required=True)
args = parser.parse_args()
system = ArmSystem(
    mem_mode="atomic",
    mem_ranges=[AddrRange(ZenaCSS._RAM_BASE, size="4KiB")],
    workload=ArmFsWorkload(object_file=args.firmware),
    release=Armv8(),
    highest_el_is_64=True,
    auto_reset_addr=False,
    reset_addr=ZenaCSS._RAM_BASE,
    cpu_affinities=[0],
)
system.voltage_domain = VoltageDomain(voltage="1V")
system.clk_domain = SrcClockDomain(
    clock="2GHz", voltage_domain=system.voltage_domain
)
system.iobus = IOXBar()
system.platform = ZenaCSS()
system.platform.gic.cpu_max = 1
system.platform.attach(system.iobus)
system.memory = SimpleMemory(range=system.mem_ranges[0])
system.memory.port = system.iobus.mem_side_ports
system.system_port = system.iobus.cpu_side_ports
system.cpu = ArmAtomicSimpleCPU(cpu_id=0)
system.cpu.createThreads()
system.cpu.createInterruptController()
system.cpu.icache_port = system.iobus.cpu_side_ports
system.cpu.dcache_port = system.iobus.cpu_side_ports
system.domain = PowerDomain(leaders=[system.cpu.power_state])
system.follower = PowerActor()
system.follower.power_state.default_state = "ON"
system.follower.power_state.possible_states = ["ON", "OFF"]
system.follower.power_state.leaders = [system.domain]
root = Root(full_system=True, system=system)
m5.instantiate()
m5.simulate(1_000_000)
m5.drain()
boundary = m5.curTick()
m5.stats.reset()
system.cpu.switchOut()
assert m5.curTick() == boundary
m5.simulate(10)
m5.stats.dump()
counters = {}
for line in Path(m5.options.outdir, "stats.txt").read_text().splitlines():
    fields = line.split()
    if len(fields) >= 2 and fields[0].startswith(
        ("system.cpu.power_state.", "system.domain.", "system.follower.")
    ):
        try:
            counters[fields[0]] = float(fields[1])
        except ValueError:
            pass
assert counters["system.cpu.power_state.numTransitions"] == 1, counters
assert counters["system.domain.numLeaderCalls"] == 1, counters
assert counters["system.domain.numLeaderCallsChangingState"] == 1, counters
assert (
    counters["system.cpu.power_state.pwrStateResidencyTicks::OFF"] == 10
), counters
assert (
    counters["system.follower.power_state.pwrStateResidencyTicks::OFF"] == 9
), counters
assert (
    counters["system.follower.power_state.pwrStateResidencyTicks::ON"] == 1
), counters
result = dict(
    status="PASS",
    reset_and_transition_tick=boundary,
    follower_off_tick=boundary + 1,
    tick=m5.curTick(),
    counters=counters,
    qualification="BARE_NATIVE_POWER_DOMAIN_NOT_LINUX",
)
Path(m5.options.outdir, "result.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result))
