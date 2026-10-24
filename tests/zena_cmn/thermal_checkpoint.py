# SPDX-License-Identifier: BSD-3-Clause
"""Native 1 W RC continuation test. Run save, restore and control separately."""

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
    ThermalCapacitor,
    ThermalDomain,
    ThermalModel,
    ThermalNode,
    ThermalReference,
    ThermalResistor,
    VoltageDomain,
)

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("mode", choices=("save", "restore", "control"))
parser.add_argument("checkpoint")
parser.add_argument("--manual", action="store_true")
args = parser.parse_args()
system = System(mem_mode="timing", mem_ranges=[AddrRange("4KiB")])
system.voltage_domain = VoltageDomain(voltage="1V")
system.clk_domain = SrcClockDomain(
    clock="1GHz", voltage_domain=system.voltage_domain
)
system.zone = SubSystem(
    thermal_domain=ThermalDomain(initial_temperature="25C")
)
system.memory = SimpleMemory(range=system.mem_ranges[0])
system.system_port = system.memory.port
system.actor = PowerActor()
system.actor.power_state.default_state = "ON"
system.actor.power_model = [
    PowerModel(
        subsystem=system.zone,
        pm=[
            LatchedPowerModel(
                dynamic_power=0.123456789012345,
                static_power=1 - 0.123456789012345,
            )
        ]
        + [LatchedPowerModel() for _ in range(3)],
    )
]
system.thermal_model = ThermalModel(step=0.001, automatic_step=not args.manual)
hot, ambient = ThermalNode(), ThermalNode()
system.thermal_model.addDomain(system.zone.thermal_domain, hot)
system.thermal_model.addReference(ThermalReference(temperature="25C"), ambient)
system.thermal_model.addResistor(ThermalResistor(resistance=2), hot, ambient)
system.thermal_model.addCapacitor(
    ThermalCapacitor(capacitance=0.5), hot, ambient
)
root = Root(full_system=False, system=system)
m5.instantiate(args.checkpoint if args.mode == "restore" else None)
stop = 0.0255 if args.mode == "save" else 0.1
source = system.actor.power_model[0].pm[0]
if args.mode == "restore":
    boundary = json.loads(
        Path(args.checkpoint, "expected-native-state.json").read_text()
    )
    assert (
        system.zone.thermal_domain.getTemperatureC()
        == boundary["temperature_c"]
    )
    assert source.getEnergyJ() == boundary["energy_j"]
    assert source.getDynamicPower() == boundary["dynamic_w"]
    assert source.getStaticPower() == boundary["static_w"]
    assert abs(source.getEnergyJ() - 0.0255) < 1e-10
    source.setAccountingEnabled(True)
end = m5.ticks.fromSeconds(stop) + 1
if args.manual:
    step_ticks = m5.ticks.fromSeconds(0.001)
    while (m5.curTick() // step_ticks + 1) * step_ticks <= end:
        next_tick = (m5.curTick() // step_ticks + 1) * step_ticks
        m5.simulate(next_tick - m5.curTick())
        system.thermal_model.advance(0.001)
m5.simulate(end - m5.curTick())
if args.mode == "save":
    source.setAccountingEnabled(False)
    m5.checkpoint(args.checkpoint)
    boundary = dict(
        temperature_c=system.zone.thermal_domain.getTemperatureC(),
        energy_j=source.getEnergyJ(),
        dynamic_w=source.getDynamicPower(),
        static_w=source.getStaticPower(),
    )
    Path(args.checkpoint, "expected-native-state.json").write_text(
        json.dumps(boundary, indent=2)
    )
m5.stats.dump()
values = {}
for line in Path(m5.options.outdir, "stats.txt").read_text().splitlines():
    parts = line.split()
    if len(parts) > 1:
        try:
            values[parts[0]] = float(parts[1])
        except ValueError:
            pass
observed = values["system.zone.thermal_domain.currentTemp"]
steps = math.floor(stop / 0.001)
expected = 25 + 2 * (1 - 1.001**-steps)
assert abs(observed - expected) < 2e-6, (observed, expected)
energy = source.getEnergyJ()
assert abs(energy - stop) < 1e-10, (energy, stop)
result = dict(
    status="PASS",
    mode=args.mode,
    manual=args.manual,
    tick=m5.curTick(),
    temperature_c=system.zone.thermal_domain.getTemperatureC(),
    energy_j=energy,
    expected_c=expected,
    qualification="SYNTHETIC_NATIVE_STATE_ONLY",
)
Path(m5.options.outdir, "result.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result))
