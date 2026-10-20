#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Verify and summarize the bounded 32 MiB Linux read scaling matrix."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from summarize_roi import (
    event_bounds,
    summarize,
)
from verify_config import validate_saved_report

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--base",
    type=Path,
    default=Path(__file__).resolve().parents[3] / "build/gem5-zena",
)
parser.add_argument(
    "--verification-dir", type=Path, help="fresh reports named <run-name>.json"
)
parser.add_argument(
    "--output-dir", type=Path, help="preserve historical summaries"
)
args = parser.parse_args()
base = args.base
rows = []
for workers in (1, 4, 8, 16):
    run = base / f"calibration-memory-neon{workers}-read32-atomic-threads"
    if not (run / "result.json").exists():
        rows.append(
            {
                "workers": workers,
                "status": "RUNNING" if run.exists() else "NOT_RUN",
                "run": str(run),
            }
        )
        continue
    result = json.loads((run / "result.json").read_text())
    assert result["status"] == "PASS", (workers, result)
    verification_path = (
        args.verification_dir / (run.name + ".json")
        if args.verification_dir
        else run / "m5out/config-verification.json"
    )
    verification = json.loads(verification_path.read_text())
    validate_saved_report(run / "m5out", verification)
    assert (
        verification["status"] == "PASS"
        and verification["device_tree"]["status"] == "PASS"
    )

    assert verification["counts"]["cpu"] == 16
    assert verification["counts"]["native_mc"] == 16
    begin, end = event_bounds(
        json.loads((run / "m5out/roi-events.json").read_text())
    )
    s = summarize(
        (run / "m5out/stats.txt").read_text(),
        (run / "guest_console.log").read_text().replace("\r", ""),
        begin,
        end,
    )
    channels = s["dram"]["channels"]
    assert set(channels) == {f"system.mem_ctrls{i}.dram" for i in range(16)}
    assert all(
        set(channels[k]) == {"Read", "Written"}
        and all(v is not None for v in channels[k].values())
        for k in channels
    )
    guest = s["guest"]
    assert (
        guest["checksum"] == 4194304
        and guest["workers"] == workers
        and guest["passes"] == 1
        and guest["working_set_bytes"] == 33554432
    )
    assert guest["kernel"] == "neon" and guest["initialization_before_switch"]
    m = json.loads((run / "run_manifest.json").read_text())
    for name in ("guest", "gem5", "profile", "kernel"):
        info = m["inputs"][name]
        assert (
            hashlib.sha256(Path(info["path"]).read_bytes()).hexdigest()
            == info["sha256"]
        ), (workers, name, "changedinput")
    rows.append(
        {
            "workers": workers,
            "status": "PASS",
            "config_verification": "PASS",
            "device_tree_verification": "PASS",
            "host_seconds": result["host_seconds"],
            "roi_seconds": s["roi"]["seconds"],
            "useful_bytes": s["application"]["useful_bytes"],
            "dram_read_bytes": s["dram"]["read_bytes"],
            "dram_write_bytes": s["dram"]["write_bytes"],
            "useful_GB_s": s["application"]["GB_s"],
            "dram_read_GB_s": s["dram"]["read_bytes"]
            / s["roi"]["seconds"]
            / 1e9,
            "dram_to_useful_ratio": s["dram"]["read_bytes"]
            / s["application"]["useful_bytes"],
            "finalchecksum": guest["checksum"],
            "run": str(run),
            "guest_SHA": m["inputs"]["guest"]["sha256"],
            "profile_SHA": m["inputs"]["profile"]["sha256"],
            "gem5_SHA": m["inputs"]["gem5"]["sha256"],
            "kernel_SHA": m["inputs"]["kernel"]["sha256"],
            "stats_block_index": s["roi"]["stats_block_index"],
        }
    )
passed = [r for r in rows if r["status"] == "PASS"]
for key in ("guest_SHA", "profile_SHA", "gem5_SHA", "kernel_SHA"):
    assert len({r[key] for r in passed}) <= 1
summary = {
    "status": "PASS" if len(passed) == 4 else "IN_PROGRESS",
    "rows": rows,
    "scope": "16 CPUs,16 channels,32MiB aggregate;1 timingwarm+1ROI;scalar default unchanged",
    "caveats": [
        "Concurrent runs influence host times; not isolated throughput comparisons.",
        "One pass is not a convergence or sustained maximum bandwidth measurement.",
        "DRAM counters include OS/background traffic; no physical-address filtering.",
        "Mixed read/write validation remains native backend; Linux write completion not calibrated.",
    ],
    "early_stop_64mib": "calibration-memory-neon16-read64/early-stop.json",
}
(
    (args.output_dir or base) / "calibration-memory-scaling-summary.json"
).write_text(json.dumps(summary, indent=2) + "\n")
fields = list(dict.fromkeys(key for row in rows for key in row))
with (
    (args.output_dir or base) / "calibration-memory-scaling-summary.csv"
).open("w") as f:
    writer = csv.DictWriter(f, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
print(json.dumps(rows, indent=2))
