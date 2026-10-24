# SPDX-License-Identifier: BSD-3-Clause
"""Time-varying latches require explicit manual thermal intervals."""

import argparse
import json
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
parser.add_argument("mode", choices=("automatic", "manual", "constant"))
parser.add_argument("--checkpoint")
parser.add_argument("--restore", action="store_true")
parser.add_argument("--before-startup", action="store_true")
args = parser.parse_args()
if args.restore and not args.checkpoint:
    parser.error("--restore requires --checkpoint")
if args.before_startup and args.mode != "automatic":
    parser.error("--before-startup is an automatic-mode rejection check")

system = System(mem_mode="timing", mem_ranges=[AddrRange("4KiB")])
system.voltage_domain = VoltageDomain(voltage="1V")
system.clk_domain = SrcClockDomain(
    clock="1GHz", voltage_domain=system.voltage_domain
)
system.memory = SimpleMemory(range=system.mem_ranges[0])
system.system_port = system.memory.port
system.zone = SubSystem(
    thermal_domain=ThermalDomain(initial_temperature="25C")
)
system.actor = PowerActor()
system.actor.power_state.default_state = "ON"
system.actor.power_model = [
    PowerModel(
        subsystem=system.zone,
        pm=[LatchedPowerModel() for _ in range(4)],
    )
]
system.thermal_model = ThermalModel(
    step=0.001, automatic_step=args.mode != "manual"
)
hot, ambient = ThermalNode(), ThermalNode()
system.thermal_model.addDomain(system.zone.thermal_domain, hot)
system.thermal_model.addReference(ThermalReference(temperature="25C"), ambient)
system.thermal_model.addResistor(ThermalResistor(resistance=2), hot, ambient)
system.thermal_model.addCapacitor(
    ThermalCapacitor(capacitance=0.5), hot, ambient
)

root = Root(full_system=False, system=system)
m5.instantiate(args.checkpoint if args.restore else None)
source = system.actor.power_model[0].pm[0]
half_step = m5.ticks.fromSeconds(0.0005)
if args.restore:
    assert m5.curTick() == half_step
    if not args.before_startup:
        m5.simulate(0)
elif not args.before_startup:
    m5.simulate(half_step)
if args.checkpoint and not args.restore:
    m5.checkpoint(args.checkpoint)
    result = dict(status="PASS", mode="save", tick=m5.curTick())
else:
    if args.mode == "manual":
        system.thermal_model.advance(0.0005)
    # Automatic with a changed latch must terminate here with a diagnostic,
    # even before the first simulate() call following checkpoint restore.
    # Reasserting the same constant value is allowed.
    source.setPower(0 if args.mode == "constant" else 10, 0)
    if args.mode == "automatic":
        raise AssertionError("automatic thermal accepted a changed latch")
    m5.simulate(half_step + 1)
    if args.mode == "manual":
        system.thermal_model.advance(0.0005)
    temperature = system.zone.thermal_domain.getTemperatureC()
    energy = source.getEnergyJ()
    expected = 25 + 0.0005 * 10 / (0.5 + 0.0005 / 2)
    if args.mode == "manual":
        assert abs(temperature - expected) < 1e-10, temperature
        assert abs(energy - 0.00500000001) < 1e-12, energy
    else:
        assert abs(temperature - 25) < 1e-10, temperature
        assert energy == 0, energy
    result = dict(
        status="PASS",
        mode=args.mode,
        tick=m5.curTick(),
        temperature_c=temperature,
        energy_j=energy,
        qualification="SYNTHETIC_NATIVE_INTERVAL_ONLY",
    )
Path(m5.options.outdir, "result.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result))
