# SPDX-License-Identifier: BSD-3-Clause
"""Compare native interval-start leakage with offline implicit RC replay.

This synthetic two-node fixture exercises numerical convergence, not a CPU,
Zena package or silicon power calibration. Run at both 100 and 50 microseconds.
"""

import argparse
import csv
import json
import sys
from pathlib import Path

import m5
from m5.objects import (
    AddrRange,
    Root,
    SimpleMemory,
    SrcClockDomain,
    System,
    VoltageDomain,
)

repo = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo / "configs"))
sys.path.insert(0, str(repo / "scripts/zena_cmn"))
from analyze_power import leakage_power
from thermal_replay import ThermalNetwork
from zena_cmn.power_runtime import configure_thermal

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--step-us", type=int, choices=(50, 100), required=True)
args = parser.parse_args()

profile = dict(
    schema_version=1,
    qualification="SYNTHETIC_RC",
    source="Numerical regression only; arbitrary RC and power, no target W",
    ambient_c=25.0,
    nodes=[
        dict(name="core", capacitance_j_per_k=0.01, initial_c=30.0),
        dict(
            name="package",
            capacitance_j_per_k=0.05,
            initial_c=25.0,
            ambient_resistance_k_per_w=2.0,
        ),
    ],
    edges=[dict(a="core", b="package", resistance_k_per_w=0.5)],
)
leakage = {
    "core": [dict(reference_w=0.5, alpha_per_k=0.04, reference_c=25)],
    "package": [dict(reference_w=0.05, alpha_per_k=0.02, reference_c=25)],
}
phases = [
    dict(duration_us=20000, dynamic_w=dict(core=1.0, package=0.2)),
    dict(duration_us=20000, dynamic_w=dict(core=4.0, package=0.5)),
    dict(duration_us=20000, dynamic_w=dict(core=0.2, package=0.1)),
]
system = System(mem_mode="timing", mem_ranges=[AddrRange("4KiB")])
system.voltage_domain = VoltageDomain(voltage="1V")
system.clk_domain = SrcClockDomain(
    clock="1GHz", voltage_domain=system.voltage_domain
)
system.memory = SimpleMemory(range=system.mem_ranges[0])
system.system_port = system.memory.port
names = configure_thermal(system, profile)
offline = ThermalNetwork(profile)
root = Root(full_system=False, system=system)
m5.instantiate()
m5.simulate(0)
sources = [actor.power_model[0].pm[0] for actor in system.power_actors]
for source in sources:
    # Retrospective interval input has one energy owner, as in PowerRuntime.
    source.setAccountingEnabled(False)


def temperatures():
    return [
        zone.thermal_domain.getTemperatureC() for zone in system.power_zones
    ]


step_s = args.step_us / 1_000_000
step_ticks = m5.ticks.fromSeconds(step_s)
assert step_ticks / 1_000_000_000_000 == step_s
initial = temperatures()
native_input_j = native_ambient_j = 0.0
max_temperature_difference_c = 0.0
max_native_balance_error_j = 0.0
trace = []
for phase_id, phase in enumerate(phases):
    assert phase["duration_us"] % args.step_us == 0
    for _ in range(phase["duration_us"] // args.step_us):
        start = temperatures()
        static_w = [
            sum(leakage_power(spec, start[i]) for spec in leakage[name])
            for i, name in enumerate(names)
        ]
        m5.simulate(step_ticks)
        for i, source in enumerate(sources):
            source.setPower(phase["dynamic_w"][names[i]], static_w[i])
        system.power_thermal.advance(step_s)
        end = temperatures()
        native_input_j += (
            sum(
                phase["dynamic_w"][name] + static_w[i]
                for i, name in enumerate(names)
            )
            * step_s
        )
        native_ambient_j += sum(
            g * (t - offline.ambient) * step_s
            for g, t in zip(offline.ambient_g, end)
        )
        native_stored_j = sum(
            c * (t - t0) for c, t, t0 in zip(offline.capacitance, end, initial)
        )
        balance = native_input_j - native_stored_j - native_ambient_j
        max_native_balance_error_j = max(
            max_native_balance_error_j, abs(balance)
        )
        other = offline.step(step_s, phase["dynamic_w"], leakage)
        difference = max(abs(a - b) for a, b in zip(end, offline.temp))
        max_temperature_difference_c = max(
            max_temperature_difference_c, difference
        )
        trace.append(
            dict(
                tick=m5.curTick(),
                phase=phase_id,
                native_core_c=end[0],
                native_package_c=end[1],
                implicit_core_c=offline.temp[0],
                implicit_package_c=offline.temp[1],
                native_input_j=native_input_j,
                implicit_input_j=offline.input_j,
                native_balance_error_j=balance,
                implicit_balance_error_j=other["balance_error_j"],
            )
        )

held_energy_j = sum(source.getEnergyJ() for source in sources)
energy_difference_fraction = abs(native_input_j - offline.input_j) / abs(
    offline.input_j
)
checks = dict(
    model_energy_agreement=energy_difference_fraction < 0.01,
    model_temperature_agreement=max_temperature_difference_c < 0.1,
    native_energy_balance=max_native_balance_error_j < 1e-8,
    offline_energy_balance=abs(other["balance_error_j"]) < 1e-8,
    single_energy_owner=held_energy_j == 0,
    exact_elapsed_ticks=m5.curTick() == 60_000_000_000,
)
result = dict(
    status="PASS" if all(checks.values()) else "FAIL",
    qualification="SYNTHETIC_NUMERICAL_CONVERGENCE_NOT_HARDWARE",
    step_us=args.step_us,
    elapsed_ms=60,
    native_scheme="MANUAL_RC_WITH_INTERVAL_START_LEAKAGE",
    offline_scheme="BACKWARD_EULER_WITH_IMPLICIT_LEAKAGE",
    input_profile=profile,
    leakage=leakage,
    phases=phases,
    native=dict(
        final_temperature_c=dict(zip(names, end)),
        input_j=native_input_j,
        stored_j=native_stored_j,
        ambient_j=native_ambient_j,
        max_balance_error_j=max_native_balance_error_j,
        disabled_held_energy_j=held_energy_j,
    ),
    offline=dict(
        final_temperature_c=dict(zip(names, offline.temp)),
        input_j=offline.input_j,
        stored_j=offline.stored_j,
        ambient_j=offline.ambient_j,
        balance_error_j=other["balance_error_j"],
    ),
    comparison=dict(
        energy_difference_fraction=energy_difference_fraction,
        max_temperature_difference_c=max_temperature_difference_c,
    ),
    project_thresholds=dict(energy_fraction=0.01, temperature_c=0.1),
    checks=checks,
)
with Path(m5.options.outdir, "trace.csv").open("w") as target:
    writer = csv.DictWriter(target, fieldnames=list(trace[0]))
    writer.writeheader()
    writer.writerows(trace)
Path(m5.options.outdir, "result.json").write_text(
    json.dumps(result, indent=2, allow_nan=False) + "\n"
)
print(json.dumps(result))
assert all(checks.values()), checks
