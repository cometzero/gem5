# SPDX-License-Identifier: BSD-3-Clause
"""Native ledger checks: power changes, freeze/resume and manual RC steps."""

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
        pm=[LatchedPowerModel(dynamic_power=1)]
        + [LatchedPowerModel() for _ in range(3)],
    )
]
system.thermal_model = ThermalModel(automatic_step=False)
hot, ambient = ThermalNode(), ThermalNode()
system.thermal_model.addDomain(system.zone.thermal_domain, hot)
system.thermal_model.addReference(ThermalReference(temperature="25C"), ambient)
system.thermal_model.addResistor(ThermalResistor(resistance=2), hot, ambient)
system.thermal_model.addCapacitor(
    ThermalCapacitor(capacitance=0.5), hot, ambient
)
root = Root(full_system=False, system=system)
m5.instantiate()
source = system.actor.power_model[0].pm[0]
m5.simulate(m5.ticks.fromSeconds(0.01))
system.thermal_model.advance(0.01)
assert abs(source.getEnergyJ() - 0.01) < 1e-12
expected = 25 + 2 * 0.01 / 1.01
assert abs(system.zone.thermal_domain.getTemperatureC() - expected) < 1e-10
source.setPower(1.5, 0.5)
m5.simulate(m5.ticks.fromSeconds(0.01))
system.thermal_model.advance(0.01)
assert abs(source.getEnergyJ() - 0.03) < 1e-12
expected = (expected + 0.01 * (25 + 4)) / 1.01
assert abs(system.zone.thermal_domain.getTemperatureC() - expected) < 1e-10
source.setAccountingEnabled(False)
m5.simulate(m5.ticks.fromSeconds(0.1))
assert abs(source.getEnergyJ() - 0.03) < 1e-12
# No advance while frozen: thermal state must stay exactly at the boundary.
assert abs(system.zone.thermal_domain.getTemperatureC() - expected) < 1e-10
source.setAccountingEnabled(True)
source.setPower(2.0, 1.0)
m5.simulate(m5.ticks.fromSeconds(0.01))
assert abs(source.getEnergyJ() - 0.06) < 1e-12
result = dict(
    status="PASS",
    energy_j=source.getEnergyJ(),
    temperature_c=system.zone.thermal_domain.getTemperatureC(),
    qualification="SYNTHETIC_NATIVE_LEDGER_ONLY",
)
Path(m5.options.outdir, "result.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result))
