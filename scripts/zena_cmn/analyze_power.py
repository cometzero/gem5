#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Extract exact ROI activity and an explicitly qualified event-energy ledger."""

import argparse
import hashlib
import json
import math
import re
from pathlib import Path

from summarize_roi import stats_blocks

CACHE_EVENTS = (
    "numDataArrayReads",
    "numDataArrayWrites",
    "numTagArrayReads",
    "numTagArrayWrites",
    "numAtomicALUOperations",
)
ROUTER_EVENTS = (
    "buffer_reads",
    "buffer_writes",
    "crossbar_activity",
    "sw_input_arbiter_activity",
    "sw_output_arbiter_activity",
)
QUALIFICATIONS = ("SYNTHETIC", "UNCALIBRATED", "CALIBRATED")
POWER_STATES = ("ON", "CLK_GATED", "SRAM_RETENTION", "OFF")


def stat_key(path, suffix):
    return re.sub(
        r"\.(cpu|o3_cpu|roi_cpu|mem_ctrls)0+(\d+)(?=\.)",
        r".\1\2",
        path + suffix,
    )


def counter(stats, path, suffix):
    key = stat_key(path, suffix)
    value = stats.get(path + suffix, stats.get(key))
    return (
        value
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
        else None
    )


def finite(value, name, positive=False):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or (value <= 0 if positive else value < 0)
    ):
        raise ValueError(f"invalid {name}: {value}")
    return value


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def actor_definitions(config):
    """One owner per physical block; caches are not CPU pipeline energy."""
    system = config["system"]
    actors = []
    objects = {}

    def index_objects(value):
        if isinstance(value, dict):
            if "path" in value:
                objects[value["path"]] = value
            for child in value.values():
                index_objects(child)
        elif isinstance(value, list):
            for child in value:
                index_objects(child)

    index_objects(system)

    def resolve(value):
        return (
            objects.get(value, {}) if isinstance(value, str) else value or {}
        )

    def add(name, kind, path, events, node, nozero=False, owner=None):
        clock = resolve((owner or {}).get("clk_domain"))
        voltage = resolve(clock.get("voltage_domain"))
        actors.append(
            dict(
                actor=name,
                kind=kind,
                path=path,
                event_names=events,
                thermal_node=node,
                nozero=nozero,
                clock_domain=clock.get("path"),
                voltage_domain=voltage.get("path"),
            )
        )

    cpus = system["cpu"]
    timing = {
        c["cpu_id"]: c
        for c in system.get("o3_cpu", system.get("roi_cpu", cpus))
    }
    for cpu in cpus:
        cid = cpu["cpu_id"]
        active = timing[cid]
        add(
            f"cpu{cid}",
            "cpu_pipeline",
            active["path"],
            ["commitStats0.numOps"],
            f"core{cid}",
            owner=active,
        )
        for level in ("l1i", "l1d", "l2"):
            cache = cpu[level]["cache"]
            if cache["type"] != "RubyCache":
                raise ValueError("only RubyCache array counters supported")
            add(
                f"cpu{cid}.{level}",
                level,
                cache["path"],
                CACHE_EVENTS,
                f"core{cid}",
                True,
                owner=cpu[level],
            )
    ruby = system["ruby"]
    for i, cluster in enumerate(ruby["rnf"]):
        add(
            f"dsu{i}",
            "dsu_l3",
            cluster["dsu_l3"]["cache"]["path"],
            CACHE_EVENTS,
            f"dsu{i}",
            True,
            owner=cluster["dsu_l3"],
        )
    for i, home in enumerate(ruby["hnf"]):
        add(
            f"slc{i}",
            "slc",
            home["cntrl"]["cache"]["path"],
            CACHE_EVENTS,
            "fabric",
            True,
            owner=home["cntrl"],
        )
    network = ruby["network"]
    if network["type"] != "GarnetNetwork":
        raise ValueError("power activity mapping requires GarnetNetwork")
    for router in network["routers"]:
        add(
            router["name"],
            "router",
            router["path"],
            ROUTER_EVENTS,
            "fabric",
            True,
            owner=router,
        )
    # These Garnet counters sum traversed flits, not utilization fractions.
    for direction in ("ext_in", "ext_out", "int"):
        add(
            f"links.{direction}",
            "link",
            network["path"],
            [direction + "_link_utilization"],
            "fabric",
            owner=network,
        )
    for i, ctrl in enumerate(system["mem_ctrls"]):
        add(
            f"dmc_phy{i}",
            "dmc_phy",
            ctrl["path"],
            ["readReqs", "writeReqs"],
            "dmc_phy",
            owner=ctrl,
        )
        add(
            f"dram{i}",
            "dram",
            ctrl["dram"]["path"],
            ["readBursts", "writeBursts", "dramBytesRead", "dramBytesWritten"],
            "dram",
            owner=ctrl,
        )
    add(
        "io",
        "io",
        system["iobus"]["path"],
        ["pktCount::total", "pktSize::total"],
        "io",
        True,
        owner=system["iobus"],
    )
    return actors


def roi_intervals(events):
    active, previous_end = None, -1
    intervals = []
    for event in events:
        if event.get("phase") not in ("roi_begin", "roi_end"):
            continue
        tick = event.get("tick")
        if isinstance(tick, bool) or not isinstance(tick, int) or tick < 0:
            raise ValueError("ROI tick must be a nonnegative integer")
        if event["phase"] == "roi_begin":
            if active is not None or tick < previous_end:
                raise ValueError("overlapping or unordered ROI")
            active = event
        else:
            if (
                active is None
                or tick <= active["tick"]
                or event.get("case_id") != active.get("case_id")
            ):
                raise ValueError("unmatched ROI end")
            intervals.append((active["tick"], tick, event.get("case_id")))
            previous_end, active = tick, None
    if active is not None or not intervals:
        raise ValueError("missing complete ROI")
    return intervals


def extract_window(
    config, stats, begin, end, epoch=0, case=None, *, definitions=None
):
    """Map one completed reset-bounded interval, offline or online."""
    if type(begin) is not int or type(end) is not int or end <= begin:
        raise ValueError("invalid activity interval")
    if stats.get("finalTick") != end or stats.get("simTicks") != end - begin:
        raise ValueError("activity requires exact reset-bounded statistics")
    if definitions is None:
        definitions = actor_definitions(config)
    frequency = finite(stats.get("simFreq"), "simFreq", True)
    actors = []
    for definition in definitions:
        path = definition["path"]
        values, sources, suppressed = {}, {}, []
        for name in definition["event_names"]:
            value = counter(stats, path, "." + name)
            # Only source-verified nozero stats may be absent as zero.
            if value is None and definition["nozero"]:
                key = re.sub(
                    r"\.(cpu|mem_ctrls)0+(\d+)(?=\.)",
                    r".\1\2",
                    path + "." + name,
                )
                if key in stats or path + "." + name in stats:
                    raise ValueError("invalid array/router event count")
                value = 0
                suppressed.append(name)
            values[name] = value
            sources[name] = (
                path + "." + name
                if path + "." + name in stats
                else stat_key(path, "." + name)
            )
        state_path = (
            path.rsplit(".cache", 1)[0] if path.endswith(".cache") else path
        )
        residency = {
            state: counter(
                stats,
                state_path,
                ".power_state.pwrStateResidencyTicks::" + state,
            )
            for state in (
                "ON",
                "CLK_GATED",
                "SRAM_RETENTION",
                "OFF",
                "UNDEFINED",
            )
        }
        residency = {k: v for k, v in residency.items() if v is not None}
        state_status = "UNAVAILABLE"
        if residency:
            state_status = (
                "VALID"
                if sum(residency.values()) == end - begin
                and not residency.get("UNDEFINED", 0)
                else "INVALID_INTERVAL_OR_UNDEFINED"
            )
        actors.append(
            {
                **{
                    k: definition[k]
                    for k in ("actor", "kind", "path", "thermal_node")
                },
                "events": values,
                "sources": sources,
                "zero_suppressed": suppressed,
                "state_ticks": residency,
                "state_status": state_status,
                "clock_domain": definition.get("clock_domain"),
                "voltage_domain": definition.get("voltage_domain"),
                "clock_period_ticks": (
                    counter(stats, definition["clock_domain"], ".clock")
                    if definition.get("clock_domain")
                    else None
                ),
                "voltage_v": (
                    counter(stats, definition["voltage_domain"], ".voltage")
                    if definition.get("voltage_domain")
                    else None
                ),
            }
        )
    return dict(
        schema_version=1,
        epoch=epoch,
        case_id=case,
        begin_tick=begin,
        end_tick=end,
        frequency_hz=frequency,
        seconds=(end - begin) / frequency,
        qualification="MODEL_ACTIVITY",
        actors=actors,
    )


def extract_activity(config, stats_text, events):
    blocks = stats_blocks(stats_text)
    records = []
    definitions = actor_definitions(config)
    for epoch, (begin, end, case) in enumerate(roi_intervals(events)):
        matches = [
            (i, b)
            for i, b in enumerate(blocks)
            if b.get("finalTick") == end and b.get("simTicks") == end - begin
        ]
        if not matches:
            raise ValueError(f"no exact reset-bounded dump for epoch {epoch}")
        index, stats = matches[0]
        semantic = lambda b: {
            k: v for k, v in b.items() if not k.startswith("host")
        }
        if any(semantic(b) != semantic(stats) for _, b in matches[1:]):
            raise ValueError("conflicting duplicate dumps")
        record = extract_window(
            config, stats, begin, end, epoch, case, definitions=definitions
        )
        record.update(
            phase="roi_begin",
            stats_block_index=index,
            duplicate_dumps_ignored=len(matches) - 1,
        )
        records.append(record)
    return records


def leakage_power(spec, temperature_c):
    """Explicit exponential leakage law; coefficients require calibration."""
    base = finite(spec["reference_w"], "leakage reference_w")
    alpha = finite(spec.get("alpha_per_k", 0), "leakage alpha_per_k")
    reference = spec.get("reference_c", 25.0)
    if not all(
        isinstance(v, (int, float)) and math.isfinite(v)
        for v in (reference, temperature_c)
    ):
        raise ValueError("invalid leakage temperature")
    try:
        value = base * math.exp(alpha * (temperature_c - reference))
    except OverflowError as exc:
        raise ValueError("leakage thermal runaway") from exc
    return finite(value, "leakage power")


def estimate_energy(activity, profile, external_energy=None):
    if profile.get("schema_version") != 1:
        raise ValueError("unsupported coefficient schema")
    qualification = profile.get("qualification")
    if qualification not in QUALIFICATIONS:
        raise ValueError("explicit coefficient qualification required")
    if not profile.get("source"):
        raise ValueError("coefficient source required")
    external = {}
    for item in external_energy or []:
        key = (item["epoch"], item["actor"])
        if key in external:
            raise ValueError("duplicate external energy owner")
        external[key] = item
    consumed = set()
    output = []
    for record in activity:
        seconds = finite(record["seconds"], "epoch seconds", True)
        actors, owners = [], set()
        for actor in record["actors"]:
            name = actor["actor"]
            if name in owners:
                raise ValueError("duplicate actor owner")
            owners.add(name)
            entry = {k: actor[k] for k in ("actor", "kind", "thermal_node")}
            entry.update(
                status="UNCONFIGURED",
                dynamic_j=None,
                leakage_j=None,
                total_j=None,
                average_w=None,
            )
            spec = profile.get("actors", {}).get(
                name, profile.get("kinds", {}).get(actor["kind"])
            )
            key = (record["epoch"], name)
            if key in external:
                ext = external[key]
                if spec is not None:
                    raise ValueError("external and event model double owner")
                if (
                    ext.get("begin_tick") != record["begin_tick"]
                    or ext.get("end_tick") != record["end_tick"]
                ):
                    raise ValueError("external energy interval mismatch")
                if ext.get("qualification") not in QUALIFICATIONS:
                    raise ValueError("external energy qualification required")
                if not ext.get("source"):
                    raise ValueError("external energy source required")
                total = finite(ext.get("total_j"), "external total_j")
                entry.update(
                    status=ext["qualification"],
                    total_j=total,
                    average_w=total / seconds,
                    source=ext["source"],
                    backend="external",
                )
                scope = ext.get("backend_energy_scope", "COMPLETE_ACTOR")
                if scope not in (
                    "COMPLETE_ACTOR",
                    "DRAM_CORE_ONLY_IO_UNCONFIGURED",
                ):
                    raise ValueError("unknown external energy scope")
                entry["backend_energy_scope"] = scope
                entry["qualification"] = ext["qualification"]
                if scope == "DRAM_CORE_ONLY_IO_UNCONFIGURED":
                    if actor["kind"] != "dram":
                        raise ValueError(
                            "partial DRAM energy requires DRAM owner"
                        )
                    entry.update(
                        status="PARTIAL",
                        total_j=None,
                        average_w=None,
                        configured_subtotal_j=total,
                        average_configured_w=total / seconds,
                        missing=["dram_io"],
                        interface_energy=ext.get("interface_energy"),
                    )
                consumed.add(key)
            elif spec is not None:
                coeff = spec.get("event_j", {})
                unknown = set(coeff) - set(actor["events"])
                if unknown:
                    raise ValueError(f"unknown event coefficients: {unknown}")
                # Never silently omit a required component or counter.
                missing = [
                    k
                    for k, v in coeff.items()
                    if v is None or actor["events"][k] is None
                ]
                entry["missing"] = missing
                if spec.get("leakage") is None:
                    missing.append("leakage")
                leakage_spec = dict(spec.get("leakage") or {})
                factors = leakage_spec.pop("state_factors", None)
                state_scale = 1.0
                if factors is not None:
                    if (
                        not isinstance(factors, dict)
                        or not factors
                        or set(factors) - set(POWER_STATES)
                    ):
                        raise ValueError("invalid leakage state_factors")
                    for value in factors.values():
                        finite(value, "leakage state factor")
                    states = actor.get("state_ticks", {})
                    ticks = record["end_tick"] - record["begin_tick"]
                    if (
                        actor.get("state_status") != "VALID"
                        or sum(states.values()) != ticks
                        or any(
                            value and state not in factors
                            for state, value in states.items()
                        )
                    ):
                        missing.append("state_residency")
                    else:
                        finite(ticks, "state interval ticks", True)
                        state_scale = sum(
                            finite(value, "state ticks")
                            * factors.get(state, 0)
                            / ticks
                            for state, value in states.items()
                        )
                        leakage_spec["reference_w"] = (
                            finite(
                                leakage_spec["reference_w"],
                                "leakage reference_w",
                            )
                            * state_scale
                        )
                voltage_scale = 1.0
                if "reference_voltage_v" in spec:
                    reference_v = finite(
                        spec["reference_voltage_v"], "reference voltage", True
                    )
                    voltage = actor.get("voltage_v")
                    if voltage is None:
                        missing.append("voltage_v")
                    else:
                        voltage_scale = (
                            finite(voltage, "actor voltage", True)
                            / reference_v
                        ) ** 2
                if not missing and coeff:
                    dynamic = (
                        sum(
                            finite(v, "event_j")
                            * finite(actor["events"][k], "event count")
                            for k, v in coeff.items()
                        )
                        * voltage_scale
                    )
                    leakage = leakage_power(
                        leakage_spec, profile.get("temperature_c", 25)
                    )
                    entry.update(
                        status=qualification,
                        dynamic_voltage_scale=voltage_scale,
                        voltage_v=actor.get("voltage_v"),
                        dynamic_j=dynamic,
                        leakage_j=leakage * seconds,
                        total_j=dynamic + leakage * seconds,
                        average_w=dynamic / seconds + leakage,
                        leakage=leakage_spec,
                        leakage_state_scale=state_scale,
                        source=profile["source"],
                        leakage_state_assumption=(
                            "RESIDENCY_WEIGHTED"
                            if factors is not None
                            else spec.get(
                                "state_assumption", "CONSTANT_ALL_STATES"
                            )
                        ),
                    )
            actors.append(entry)
        complete = all(a["total_j"] is not None for a in actors)
        subtotal = sum(
            a.get("configured_subtotal_j", a["total_j"] or 0) for a in actors
        )
        qualifications = {qualification} | {
            a.get("qualification", a["status"]) for a in actors
        }
        output.append(
            {
                **{
                    k: record[k]
                    for k in (
                        "epoch",
                        "case_id",
                        "begin_tick",
                        "end_tick",
                        "seconds",
                    )
                },
                "phase": record.get("phase"),
                "qualification": next(
                    q for q in QUALIFICATIONS if q in qualifications
                ),
                "coverage": "COMPLETE_LEDGER" if complete else "PARTIAL",
                "total_j": subtotal if complete else None,
                "average_w": subtotal / seconds if complete else None,
                "configured_subtotal_j": subtotal,
                "unconfigured_actors": [
                    a["actor"] for a in actors if a["total_j"] is None
                ],
                "actors": actors,
            }
        )
    if set(external) - consumed:
        raise ValueError("external energy contains unknown epoch/actor")
    return output


def analyze_run(run, profile, external_energy=None):
    manifests = [
        run / name
        for name in ("launch.json", "run_manifest.json")
        if (run / name).is_file()
    ]
    if len(manifests) != 1:
        raise ValueError("require one source launch or run manifest")
    files = {
        "manifest": manifests[0],
        "result": run / "result.json",
        "config": run / "m5out/config.json",
        "stats": run / "m5out/stats.txt",
        "events": run / "m5out/roi-events.json",
        "exit": run / "m5out/exit.json",
    }
    if json.loads(files["result"].read_text()).get("status") != "PASS":
        raise ValueError("source run did not pass")
    exit_record = json.loads(files["exit"].read_text())
    if (
        exit_record.get("status") != "COMPLETED"
        or exit_record.get("code") != 0
    ):
        raise ValueError("source run did not complete")
    activity = extract_activity(
        json.loads(files["config"].read_text()),
        files["stats"].read_text(),
        json.loads(files["events"].read_text()),
    )
    energy = estimate_energy(activity, profile, external_energy)
    summary = dict(
        schema_version=1,
        qualification=min(
            (e["qualification"] for e in energy), key=QUALIFICATIONS.index
        ),
        measurement="REANALYZED_EXISTING_MODEL_ACTIVITY",
        sources={
            k: {"path": str(p.resolve()), "sha256": sha256(p)}
            for k, p in files.items()
        },
        original_manifest=json.loads(files["manifest"].read_text()),
        epochs=len(energy),
        coverage=(
            "COMPLETE_LEDGER"
            if all(e["coverage"] == "COMPLETE_LEDGER" for e in energy)
            else "PARTIAL"
        ),
        unconfigured_actors=sorted(
            {a for e in energy for a in e["unconfigured_actors"]}
        ),
        coefficient_profile=profile,
        limitations=[
            "Not a newly measured simulation or calibrated silicon power.",
            "CPU committed-uop proxy excludes separate cache energy.",
            "State residency may not cover the ROI; no ON time inferred.",
            "I/O actor is an aggregate proxy, not GIC/SMMU/PHY characterization.",
            "DRAM bytes are burst accounting, not a command energy model.",
        ],
    )
    return activity, energy, summary


def write_jsonl(path, records):
    path.write_text(
        "".join(json.dumps(r, allow_nan=False) + "\n" for r in records)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--external-energy", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    profile = json.loads(args.profile.read_text())
    external = (
        [
            json.loads(line)
            for line in args.external_energy.read_text().splitlines()
            if line.strip()
        ]
        if args.external_energy
        else None
    )
    activity, energy, summary = analyze_run(args.run, profile, external)
    args.output.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.output / "activity.jsonl", activity)
    write_jsonl(args.output / "energy.jsonl", energy)
    summary["coefficient_profile_sha256"] = sha256(args.profile)
    (args.output / "power-summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False) + "\n"
    )
    print(
        json.dumps(
            {
                "epochs": len(activity),
                "coverage": summary["coverage"],
                "output": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
