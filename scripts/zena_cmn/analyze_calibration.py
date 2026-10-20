#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Extract exact calibration ROIs without treating pointer bytes as bandwidth roofs."""
import argparse
import json
from pathlib import Path

from analyze_roofline import (
    cache_metrics,
    cache_paths,
    counter,
    residency,
)
from run_roofline import validate_completion
from summarize_roi import stats_blocks


def analyze(run):
    manifest = json.loads((run / "run_manifest.json").read_text())
    variant = manifest.get("variant")
    if variant not in (
        "calibration",
        "memory_patterns",
        "load_streams",
        "execution_checks",
    ):
        raise ValueError("expected calibration run manifest")
    if variant == "execution_checks" and set(
        manifest.get("expected_cases", range(401, 409))
    ) not in (set(range(401, 409)), set(range(401, 413))):
        raise ValueError("invalid execution case set")
    result = json.loads((run / "result.json").read_text())
    if result["status"] != "PASS":
        raise ValueError("simulation did not pass")
    console = (run / "guest_console.log").read_text()
    events = json.loads((run / "m5out/roi-events.json").read_text())
    validate_completion(
        console,
        events,
        json.loads((run / "m5out/exit.json").read_text()),
        (
            set(manifest.get("expected_cases", range(401, 409)))
            if variant == "execution_checks"
            else (
                set(range(301, 316))
                if variant == "load_streams"
                else (
                    set(range(201, 208))
                    if variant == "memory_patterns"
                    else set(range(101, 109))
                )
            )
        ),
    )
    config = json.loads((run / "m5out/config.json").read_text())
    if variant == "load_streams" and any(
        cpu["l2"]["use_prefetcher"] for cpu in config["system"]["cpu"]
    ):
        raise ValueError(
            "independent load stream diagnostic requires prefetch off"
        )
    period = config["system"]["cpu_clk_domain"]["clock"][0]
    blocks = stats_blocks((run / "m5out/stats.txt").read_text())
    paths = cache_paths(config)
    dram_paths = [mc["dram"]["path"] for mc in config["system"]["mem_ctrls"]]
    prefetch_paths = [
        cpu["l2"]["prefetcher"]["path"]
        for cpu in config["system"]["cpu"]
        if cpu["l2"].get("use_prefetcher")
    ]
    rows = []
    for line in console.splitlines():
        if not line.startswith("ROOF_CASE "):
            continue
        row = json.loads(line[len("ROOF_CASE ") :])
        begins = [
            e["tick"]
            for e in events
            if e["phase"] == "roi_begin" and e["case_id"] == row["id"]
        ]
        ends = [
            e["tick"]
            for e in events
            if e["phase"] == "roi_end" and e["case_id"] == row["id"]
        ]
        if len(begins) != 1 or len(ends) != 1 or ends[0] <= begins[0]:
            raise ValueError("invalid ROI boundaries")
        ticks = ends[0] - begins[0]
        matching = [
            b
            for b in blocks
            if b.get("finalTick") == ends[0] and b.get("simTicks") == ticks
        ]
        if not matching or any(b != matching[0] for b in matching):
            raise ValueError("missing or conflicting exact ROI dump")
        stats = matching[0]
        seconds = ticks / stats["simFreq"]
        cycles = ticks / period
        row.update(
            begin_tick=begins[0],
            end_tick=ends[0],
            cycles=cycles,
            seconds=seconds,
            GFLOP_s=row["flops"] / seconds / 1e9,
            useful_GB_s=row["useful_bytes"] / seconds / 1e9,
            cycles_per_target_instruction=cycles / row["instructions"],
            load_to_use_cycles=stats.get(
                "system.roi_cpu0.lsq0.loadToUse::mean"
            ),
            caches=cache_metrics(stats, paths),
            prefetch_status=(
                "disabled"
                if not prefetch_paths
                else (
                    "measured"
                    if all(
                        counter(stats, path, ".pfIssued") is not None
                        and counter(stats, path, ".pfUseful") is not None
                        for path in prefetch_paths
                    )
                    else "missing_stats"
                )
            ),
            prefetch={
                name: (
                    sum(
                        (counter(stats, path, "." + name) or 0)
                        for path in prefetch_paths
                    )
                    if prefetch_paths
                    and all(
                        counter(stats, path, ".pfIssued") is not None
                        and counter(stats, path, ".pfUseful") is not None
                        for path in prefetch_paths
                    )
                    else None
                )
                for name in ("pfIssued", "pfUseful", "pfUnused")
            },
            dram_bytes={
                name: (
                    sum(
                        counter(stats, path, "." + suffix)
                        for path in dram_paths
                    )
                    if dram_paths
                    and all(
                        counter(stats, path, "." + suffix) is not None
                        for path in dram_paths
                    )
                    else None
                )
                for name, suffix in (
                    ("read", "dramBytesRead"),
                    ("write", "dramBytesWritten"),
                )
            },
        )
        if row["dependent_loads"] and row.get("independent_streams", 1) > 1:
            row["interpretation"] = (
                "overlapped pointer streams: cycles/load is throughput, not latency"
            )
        elif row["dependent_loads"]:
            row["interpretation"] = (
                "dependent pointer latency including loop/translation; verify source residency"
            )
        else:
            row["interpretation"] = (
                "kernel throughput, not guaranteed hardware peak"
            )
        if variant == "load_streams":
            row["residency"] = residency(
                row["level"], row["caches"], row["instructions"] * 64
            )
            row["measured_line_footprint"] = min(
                row["working_set_bytes"], row["instructions"] * 64
            )
        if variant == "execution_checks":
            guide_cycles = {
                401: 0.5,
                402: 2,
                403: 4,
                404: None,
                405: 4,
                406: 10,
                407: 1,
                408: None,
            }
            row["sog_reference_cycles_per_instruction"] = guide_cycles.get(
                row["id"]
            )
            row["cycles_per_divide"] = (
                cycles / (row["instructions"] / (2 if row["id"] == 408 else 1))
                if 405 <= row["id"] <= 408
                else None
            )
            if row["id"] >= 409:
                row["warmup_passes"] = 8
                row["warmup_footprint_bytes"] = 8192
                row["warmup_loads_interpretation"] = (
                    "Unused for LSU cases; legacy v2 value1024 counted loop blocks, not load instructions"
                )
                row["L1_residency"] = residency(
                    "L1", row["caches"], row["useful_bytes"]
                )
                row["useful_bytes_per_cycle"] = row["useful_bytes"] / cycles
                row["useful_read_bytes"] = (
                    65536
                    if row["id"] in (409, 410)
                    else 32768 if row["id"] == 412 else 0
                )
                row["useful_write_bytes"] = (
                    row["useful_bytes"] - row["useful_read_bytes"]
                )
                prefix = "system.roi_cpu0.commitStats0."
                row["committed_memory_instructions"] = {
                    key: stats.get(prefix + key)
                    for key in ("numLoadInsts", "numStoreInsts")
                }
                row["memory_micro_op_ratio_note"] = (
                    "Native committed load/store counters include ROI setup/control and must not be treated as isolated target uop counts"
                )
            row["execution_qualification"] = (
                "UNQUALIFIED_HARDWARE_MODEL_DIAGNOSTIC"
            )
            row["interpretation"] = (
                "Exact instruction/dependency probe; loop and marker overhead included; no FU timing calibration inferred from checksum PASS"
            )
        rows.append(row)
    return {
        "qualification": "MODEL_ONLY",
        "host_seconds": result["host_seconds"],
        "run": str(run),
        "cases": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    result = analyze(args.run)
    output = args.run / "calibration-summary.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(output)


if __name__ == "__main__":
    main()
