#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Replay qualified actor power through an explicit lumped thermal RC net."""

import argparse
import csv
import json
import math
from pathlib import Path

from analyze_power import (
    finite,
    leakage_power,
    sha256,
)


def temperature(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= -273.15
    ):
        raise ValueError("temperature must be finite and above absolute zero")
    return float(value)


def solve(matrix, rhs):
    """Cholesky solve for the positive-definite passive RC step matrix."""
    n = len(rhs)
    lower = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            value = matrix[i][j] - sum(
                lower[i][k] * lower[j][k] for k in range(j)
            )
            if i == j:
                if value <= 0:
                    raise ValueError("thermal matrix is not positive definite")
                lower[i][j] = math.sqrt(value)
            else:
                lower[i][j] = value / lower[j][j]
    result = [0.0] * n
    for i in range(n):
        result[i] = (
            rhs[i] - sum(lower[i][j] * result[j] for j in range(i))
        ) / lower[i][i]
    for i in reversed(range(n)):
        result[i] = (
            result[i] - sum(lower[j][i] * result[j] for j in range(i + 1, n))
        ) / lower[i][i]
    return result


class ThermalNetwork:
    def __init__(self, profile, initial=None):
        if profile.get("schema_version") != 1:
            raise ValueError("unsupported thermal schema")
        if profile.get("qualification") not in (
            "SYNTHETIC_RC",
            "ASSUMED_RC",
            "CALIBRATED_RC",
        ):
            raise ValueError("thermal qualification required")
        if not profile.get("source"):
            raise ValueError("thermal source required")
        self.profile = profile
        self.ambient = temperature(profile["ambient_c"])
        nodes = profile["nodes"]
        self.names = [n["name"] for n in nodes]
        if not nodes or len(self.names) != len(set(self.names)):
            raise ValueError("thermal nodes must be nonempty and unique")
        self.capacitance = [
            finite(n["capacitance_j_per_k"], "capacitance", True)
            for n in nodes
        ]
        self.ambient_g = [
            (
                1
                / finite(
                    n["ambient_resistance_k_per_w"], "ambient resistance", True
                )
                if "ambient_resistance_k_per_w" in n
                else 0
            )
            for n in nodes
        ]
        self.temp = [temperature(n["initial_c"]) for n in nodes]
        if initial is not None:
            if set(initial) != set(self.names):
                raise ValueError("thermal initial state node mismatch")
            self.temp = [temperature(initial[n]) for n in self.names]
        count = len(nodes)
        self.conductance = [[0.0] * count for _ in nodes]
        for i, value in enumerate(self.ambient_g):
            self.conductance[i][i] = value
        seen = set()
        for edge in profile.get("edges", []):
            a, b = edge["a"], edge["b"]
            if a == b or a not in self.names or b not in self.names:
                raise ValueError("invalid thermal edge")
            pair = tuple(sorted((a, b)))
            if pair in seen:
                raise ValueError("duplicate thermal edge")
            seen.add(pair)
            g = 1 / finite(edge["resistance_k_per_w"], "edge resistance", True)
            i, j = self.names.index(a), self.names.index(b)
            self.conductance[i][i] += g
            self.conductance[j][j] += g
            self.conductance[i][j] -= g
            self.conductance[j][i] -= g
        self.input_j = self.stored_j = self.ambient_j = 0.0
        self.elapsed_s = 0.0

    def step(self, seconds, dynamic_w, leakage=None):
        seconds = finite(seconds, "thermal step", True)
        unknown = set(dynamic_w) - set(self.names)
        if unknown or set(leakage or {}) - set(self.names):
            raise ValueError("unknown thermal power node")
        leakage = leakage or {}
        dynamic = [
            finite(dynamic_w.get(n, 0), "dynamic W") for n in self.names
        ]
        matrix = [[v for v in row] for row in self.conductance]
        for i, capacity in enumerate(self.capacitance):
            matrix[i][i] += capacity / seconds
        initial = self.temp[:]
        guess = initial[:]
        # Backward Euler with temperature-dependent leakage at the new state.
        for _ in range(100):
            power = [
                dynamic[i]
                + sum(
                    leakage_power(spec, guess[i])
                    for spec in leakage.get(name, [])
                )
                for i, name in enumerate(self.names)
            ]
            rhs = [
                self.capacitance[i] / seconds * initial[i]
                + self.ambient_g[i] * self.ambient
                + power[i]
                for i in range(len(self.names))
            ]
            updated = [temperature(t) for t in solve(matrix, rhs)]
            if max(abs(a - b) for a, b in zip(updated, guess)) < 1e-10:
                break
            guess = updated
        else:
            raise ValueError("leakage iteration did not converge; reduce step")
        self.temp = updated
        input_j = sum(power) * seconds
        stored_j = sum(
            c * (t - t0)
            for c, t, t0 in zip(self.capacitance, updated, initial)
        )
        ambient_j = sum(
            g * (t - self.ambient) * seconds
            for g, t in zip(self.ambient_g, updated)
        )
        residual = input_j - stored_j - ambient_j
        if abs(residual) > 1e-8 * max(1, abs(input_j), abs(stored_j)):
            raise ValueError("thermal energy conservation failure")
        self.input_j += input_j
        self.stored_j += stored_j
        self.ambient_j += ambient_j
        self.elapsed_s += seconds
        return {
            "time_s": self.elapsed_s,
            "temperature_c": dict(zip(self.names, updated)),
            "power_w": dict(zip(self.names, power)),
            "input_j": self.input_j,
            "stored_j": self.stored_j,
            "ambient_j": self.ambient_j,
            "balance_error_j": self.input_j - self.stored_j - self.ambient_j,
        }


def assumed_profile(nodes):
    """Sensitivity-only topology: each owned block exchanges heat with package."""
    names = sorted(set(nodes))
    if "package" in names:
        raise ValueError("package is reserved for a passive thermal node")
    return dict(
        schema_version=1,
        qualification="ASSUMED_RC",
        source="Sensitivity-only lumped model; no Zena package calibration",
        ambient_c=25.0,
        nodes=[
            dict(name=n, capacitance_j_per_k=0.1, initial_c=25.0)
            for n in names
        ]
        + [
            dict(
                name="package",
                capacitance_j_per_k=5,
                ambient_resistance_k_per_w=2.0,
                initial_c=25.0,
            )
        ],
        edges=[dict(a=n, b="package", resistance_k_per_w=1.0) for n in names],
    )


def replay(energy, schedule, profile, step_s=0.001, allow_partial=False):
    finite(step_s, "step_s", True)
    network = ThermalNetwork(profile)
    records = {e["epoch"]: e for e in energy}
    if len(records) != len(energy) or not schedule:
        raise ValueError("unique epochs and nonempty schedule required")
    trace = [
        dict(
            time_s=0.0,
            temperature_c=dict(zip(network.names, network.temp)),
            power_w={n: 0.0 for n in network.names},
            input_j=0.0,
            stored_j=0.0,
            ambient_j=0.0,
            balance_error_j=0.0,
        )
    ]
    omitted = set()
    qualifications = set()
    for phase_index, phase in enumerate(schedule):
        record = records[phase["epoch"]]
        seconds = finite(phase["seconds"], "phase seconds", True)
        scale = finite(phase.get("dynamic_scale", 1), "dynamic_scale")
        dynamic, leakage = {}, {}
        for actor in record["actors"]:
            name = actor["thermal_node"]
            if name not in network.names:
                raise ValueError(f"missing thermal node: {name}")
            if actor["total_j"] is None:
                omitted.add(actor["actor"])
                if not allow_partial:
                    raise ValueError(
                        "unconfigured energy; partial replay forbidden"
                    )
                if not actor.get("configured_subtotal_j"):
                    continue
            qualifications.add(actor.get("qualification", actor["status"]))
            if actor.get("backend") == "external":
                # External average power is fixed; no inferred leakage split.
                if scale != 1:
                    raise ValueError(
                        "external total energy cannot use dynamic_scale; "
                        "supply a separate idle epoch with scale 1"
                    )
                dynamic[name] = dynamic.get(name, 0) + actor.get(
                    "average_configured_w", actor["average_w"]
                )
            else:
                dynamic[name] = (
                    dynamic.get(name, 0)
                    + actor["dynamic_j"] / record["seconds"] * scale
                )
                leakage.setdefault(name, []).append(actor["leakage"])
        remaining = seconds
        while remaining > max(1e-14, seconds * 1e-12):
            duration = min(step_s, remaining)
            result = network.step(duration, dynamic, leakage)
            result.update(
                phase=phase_index, epoch=phase["epoch"], dynamic_scale=scale
            )
            trace.append(result)
            remaining -= duration
    summary = dict(
        schema_version=1,
        qualification=profile["qualification"],
        power_qualifications=sorted(qualifications),
        coverage="PARTIAL" if omitted else "COMPLETE_LEDGER",
        unconfigured_actors=sorted(omitted),
        step_s=step_s,
        replay_schedule=schedule,
        elapsed_s=network.elapsed_s,
        peak_c={
            n: max(r["temperature_c"][n] for r in trace) for n in network.names
        },
        final_c=trace[-1]["temperature_c"],
        input_j=network.input_j,
        stored_j=network.stored_j,
        ambient_j=network.ambient_j,
        balance_error_j=network.input_j - network.stored_j - network.ambient_j,
        limitations=[
            "Explicit replay schedule is not measured full-run wall time.",
            "Offline temperatures do not alter gem5 activity or DRAM refresh.",
            "Unconfigured actors omitted only with explicit partial permission.",
            "External energy uses fixed average W without leakage feedback.",
        ],
    )
    return trace, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--energy", type=Path, required=True)
    parser.add_argument("--schedule", type=Path, required=True)
    parser.add_argument(
        "--profile",
        type=Path,
        help="omit to create and save an ASSUMED_RC profile",
    )
    parser.add_argument("--step-s", type=float, default=0.001)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    energy = [
        json.loads(line)
        for line in args.energy.read_text().splitlines()
        if line.strip()
    ]
    schedule = json.loads(args.schedule.read_text())
    profile = (
        json.loads(args.profile.read_text())
        if args.profile
        else assumed_profile(
            a["thermal_node"] for r in energy for a in r["actors"]
        )
    )
    trace, summary = replay(
        energy, schedule, profile, args.step_s, args.allow_partial
    )
    summary["sources"] = {
        "energy_sha256": sha256(args.energy),
        "schedule_sha256": sha256(args.schedule),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    for filename, value in (
        ("thermal-profile.json", profile),
        ("thermal-summary.json", summary),
    ):
        (args.output / filename).write_text(
            json.dumps(value, indent=2, allow_nan=False) + "\n"
        )
    names = list(trace[0]["temperature_c"])
    with (args.output / "temperature.csv").open("w") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "time_s",
                *[n + "_c" for n in names],
                "input_j",
                "stored_j",
                "ambient_j",
                "balance_error_j",
            ]
        )
        for row in trace:
            writer.writerow(
                [
                    row["time_s"],
                    *[row["temperature_c"][n] for n in names],
                    row["input_j"],
                    row["stored_j"],
                    row["ambient_j"],
                    row["balance_error_j"],
                ]
            )
    print(
        json.dumps(
            {
                "coverage": summary["coverage"],
                "elapsed_s": summary["elapsed_s"],
                "peak_c": max(summary["peak_c"].values()),
            }
        )
    )


if __name__ == "__main__":
    main()
