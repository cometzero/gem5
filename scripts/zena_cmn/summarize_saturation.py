# SPDX-License-Identifier: BSD-3-Clause
"""Export native saturation evidence without mixing measurement windows."""

import argparse
import csv
import json
import math
from pathlib import Path


def row(record):
    result = record["result"]
    if record["status"] != "PASS" or result["status"] != "PASS":
        raise ValueError(
            "failed run cannot be summarized as performance evidence"
        )
    base = {"case": record["case"], "host_seconds": record["host_seconds"]}
    if "path" in result:
        return dict(
            base,
            kind="backend",
            mode={100: "read", 0: "write", 50: "mixed"}[
                result["read_percent"]
            ],
            path=result["path"],
            fabric_width=result["fabric_width"],
            roi_us=result["roi_us"],
            credits=result["outstanding_total"],
            dram_GBps=result["dram_total_GBps"],
            device_GBps="",
        )
    ticks = result["polled_roi_ticks"] + result["drain_extra_ticks"]
    if ticks <= 0 or result["job_ticks"] <= 0:
        raise ValueError("measurement duration must be positive")
    expected = (
        (result["payload_read_bytes"] + result["payload_write_bytes"])
        * 1000
        / result["job_ticks"]
    )
    if not math.isclose(expected, result["device_payload_gbps"], rel_tol=1e-9):
        raise ValueError("device bandwidth does not match payload/jobTicks")
    return dict(
        base,
        kind="npu",
        mode=result["args"]["mode"],
        stage=result["args"]["stage"],
        mib=result["args"]["mib"],
        smmu_mode=result["args"]["smmu_mode"],
        smmu_resources=result["args"]["smmu_resources"],
        smmu_slots=int(result["settings"]["ifc"]["xlate_slots"]),
        fabric_width=result["settings"]["fabric_width"],
        dma_width=result["settings"]["dma_width"],
        credits=result["settings"]["max_outstanding"],
        device_GBps=expected,
        dram_GBps=(
            result["dram_drained_read_bytes"]
            + result["dram_drained_write_bytes"]
        )
        * 1000
        / ticks,
        roi_us=ticks / 1e6,
        job_us=result["job_ticks"] / 1e6,
        payload_read_bytes=result["payload_read_bytes"],
        payload_write_bytes=result["payload_write_bytes"],
        dram_read_bytes=result["dram_drained_read_bytes"],
        dram_write_bytes=result["dram_drained_write_bytes"],
        dma_retries=result["npu"]["dmaRetries"],
        outstanding_high_water=result["npu"]["outstandingHighWater"],
        translation_mean_ns=result["smmu_stats"].get(
            "system.smmu.translationTimeDist::mean", 0
        )
        / 1000,
        verification=result["verification"],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path, nargs="+")
    parser.add_argument(
        "--output", type=Path, required=True, help="output filename prefix"
    )
    args = parser.parse_args()
    campaigns = [json.loads(path.read_text()) for path in args.campaign]
    if any(c.get("status") != "PASS" for c in campaigns):
        raise ValueError("campaign incomplete or failed")
    runs = [run for campaign in campaigns for run in campaign["runs"]]
    fingerprints = {run["result"]["dram_fingerprint"] for run in runs}
    binaries = {c["binary_sha256"] for c in campaigns}
    if len(fingerprints) != 1 or len(binaries) != 1:
        raise ValueError(
            "campaigns must share DRAM parameters and gem5 binary"
        )
    rows = [row(run) for run in runs]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "status": "PASS",
        "qualification": "NATIVE_MODEL_ONLY",
        "campaigns": [str(p) for p in args.campaign],
        "dram_fingerprint": fingerprints.pop(),
        "binary_sha256": binaries.pop(),
        "rows": rows,
        "measurement": {
            "device_GBps": "tensor read+write bytes / exact jobTicks; includes metadata/control time",
            "npu_dram_GBps": "all DRAM burst bytes / polled job window plus native drain",
            "backend_dram_GBps": "DRAM burst counter delta / fixed steady-state ROI",
        },
    }
    args.output.with_suffix(".json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    with args.output.with_suffix(".csv").open("w", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=sorted({key for r in rows for key in r})
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"PASS: {len(rows)} native runs, one DRAM fingerprint, one binary")


if __name__ == "__main__":
    main()
