# SPDX-License-Identifier: BSD-3-Clause
"""Native residency boundaries, including reset without a preceding dump."""

import argparse
import json
import math
from pathlib import Path

import m5
from m5.objects import (
    AddrRange,
    LatchedPowerModel,
    PowerActor,
    PowerModel,
    Root,
    SimpleMemory,
    SrcClockDomain,
    SubSystem,
    System,
    VoltageDomain,
)

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "mode",
    choices=("reset-only", "dump-reset", "same-tick", "save", "restore"),
)
parser.add_argument("--checkpoint")
args = parser.parse_args()
if args.mode in ("save", "restore") and not args.checkpoint:
    parser.error("save/restore requires --checkpoint")

states = ("ON", "CLK_GATED", "SRAM_RETENTION", "OFF")
warmup_ticks = 1_000_000_000
interval_ticks = 100_000_000
system = System(mem_mode="timing", mem_ranges=[AddrRange("4KiB")])
system.voltage_domain = VoltageDomain(voltage="1V")
system.clk_domain = SrcClockDomain(
    clock="1GHz", voltage_domain=system.voltage_domain
)
system.memory = SimpleMemory(range=system.mem_ranges[0])
system.system_port = system.memory.port
system.zone = SubSystem()
system.actors = [PowerActor() for _ in states]
for actor, state in zip(system.actors, states):
    actor.power_state.default_state = state
    actor.power_state.possible_states = states
    actor.power_model = [
        PowerModel(
            subsystem=system.zone,
            pm=[
                LatchedPowerModel(dynamic_power=i + 1, static_power=0.5)
                for i in range(len(states))
            ],
        )
    ]

root = Root(full_system=False, system=system)
m5.instantiate(args.checkpoint if args.mode == "restore" else None)
if args.mode == "restore":
    assert m5.curTick() == warmup_ticks
    m5.simulate(0)
else:
    m5.simulate(warmup_ticks)

if args.mode == "save":
    # Deliberately do not dump: serialize an unaccounted residency tail.
    m5.checkpoint(args.checkpoint)
    observed = {}
else:
    if args.mode in ("dump-reset", "same-tick"):
        m5.stats.dump()
    m5.stats.reset()
    if args.mode == "same-tick":
        m5.stats.reset()
        for index, actor in enumerate(system.actors):
            power = actor.power_model[0].getDynamicPower()
            assert math.isfinite(power) and power == index + 1, power
            assert actor.power_model[0].getStaticPower() == 0.5
        m5.stats.dump()
        m5.stats.reset()
    m5.simulate(interval_ticks)
    m5.stats.dump()
    # The final dump is the measurement interval (prior dumps are warm-up).
    block = (
        Path(m5.options.outdir, "stats.txt")
        .read_text()
        .split("---------- Begin Simulation Statistics ----------")[-1]
    )
    counters = {}
    for line in block.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            try:
                counters[fields[0]] = float(fields[1])
            except ValueError:
                pass
    assert counters["simTicks"] == interval_ticks, counters["simTicks"]
    observed = {}
    for actor, state in zip(system.actors, states):
        prefix = actor.power_state.path() + ".pwrStateResidencyTicks::"
        residence = {s: counters.get(prefix + s, 0) for s in states}
        observed[state] = residence
        assert residence[state] == interval_ticks, (state, residence)
        assert sum(residence.values()) == interval_ticks, residence

result = dict(
    status="PASS",
    mode=args.mode,
    tick=m5.curTick(),
    interval_ticks=interval_ticks,
    residency_ticks=observed,
    qualification="SYNTHETIC_NATIVE_BOUNDARIES_ONLY",
)
Path(m5.options.outdir, "result.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result))
