# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Qualify a bounded read ROI-length and channel-phase comparison."""
import argparse
import csv
import hashlib
import json
import re
from pathlib import Path

from summarize_roi import (
    event_bounds,
    summarize,
)
from verify_config import validate_saved_report

CASES = {
    "read-p1": (1, False),
    "read-p2": (2, False),
    "read-p4": (4, False),
    "read-phase-p2": (2, True),
}
WSS = 16 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def validate_traffic(summary, passes, phase):
    guest = summary["guest"]
    expected = dict(
        workers=4,
        passes=passes,
        working_set_bytes=WSS,
        useful_bytes=WSS * passes,
        warmup_passes=1,
        checksum=WSS // 8 * passes,
        kernel="neon",
        benchmark="read",
        initialization_before_switch=True,
        channel_phase_spread=phase,
        allocated_bytes=WSS + (256 if phase else 0),
    )
    for key, value in expected.items():
        require(guest.get(key) == value, f"guest {key} mismatch")
    channels = summary["dram"]["channels"]
    require(
        set(channels) == {f"system.mem_ctrls{i}.dram" for i in range(16)},
        "missing DRAM channel",
    )
    for values in channels.values():
        require(
            set(values) == {"Read", "Written"}
            and all(
                isinstance(v, (float, int)) and v >= 0 for v in values.values()
            ),
            "invalid DRAM counters",
        )
    require(
        summary["application"]["useful_bytes"] == WSS * passes,
        "useful byte mismatch",
    )


def validate_exit(exit_record):
    require(
        exit_record.get("status") == "COMPLETED"
        and exit_record.get("code") == 0
        and exit_record.get("cause")
        in (
            "m5_exit instruction encountered",
            "exiting with last active thread context",
        ),
        "simulation exit was not normal completion",
    )


def compare(rows):
    require(len(rows) == len(CASES), "duplicate or missing cases")
    require({row["name"] for row in rows} == set(CASES), "incomplete cases")
    indexed = {row["name"]: row for row in rows}
    reference = indexed["read-p1"]["identity"]
    for name, (passes, phase) in CASES.items():
        row = indexed[name]
        require(
            row["passes"] == passes and row["phase"] == phase,
            "passes/phase mismatch",
        )
        require(row["identity"] == reference, "input hash/config mismatch")

    def change(first, second):
        result = {}
        for field in ("useful_GB_s", "dram_to_useful_ratio"):
            old, new = indexed[first][field], indexed[second][field]
            require(old > 0 and new > 0, "nonpositive measurement")
            result[field + "_relative_change"] = (new - old) / old
        return result

    successive = [change("read-p1", "read-p2"), change("read-p2", "read-p4")]
    return {
        "measurement_status": "PASS",
        "convergence_status": (
            "PASS"
            if all(abs(v) <= 0.05 for v in successive[-1].values())
            else "NOT_CONVERGED"
        ),
        "criterion": "last p2->p4: bandwidth and DRAM/useful each <=5%",
        "successive_changes": successive,
        "phase_vs_matched_p2": change("read-p2", "read-phase-p2"),
    }


def load_case(base, name, hash_cache):
    passes, phase = CASES[name]
    run = base / name
    m5out = run / "m5out"
    result = json.loads((run / "result.json").read_text())
    validate_exit(json.loads((m5out / "exit.json").read_text()))
    require(
        result["status"] == "PASS" and result["exit_code"] == 0,
        f"{name}: simulation did not complete",
    )
    verification = json.loads((m5out / "config-verification.json").read_text())
    validate_saved_report(m5out, verification)
    require(
        verification["counts"]["cpu"] == 4
        and verification["counts"]["native_mc"] == 16,
        "topology mismatch",
    )
    console = (run / "guest_console.log").read_text().replace("\r", "")
    require(
        "CMN_EXIT=0\n" in console and "CMN_ONLINE=0-3\n" in console,
        "guest completion/online CPUs missing",
    )
    begin, end = event_bounds(
        json.loads((m5out / "roi-events.json").read_text())
    )
    summary = summarize((m5out / "stats.txt").read_text(), console, begin, end)
    validate_traffic(summary, passes, phase)
    manifest = json.loads((run / "run_manifest.json").read_text())
    hashes = {}
    for key in (
        "guest",
        "gem5",
        "kernel",
        "bootloader",
        "base_initrd",
        "profile",
    ):
        item = manifest["inputs"][key]
        path = item["path"]
        if path not in hash_cache:
            hash_cache[path] = digest(path)
        require(hash_cache[path] == item["sha256"], f"changed {key} input")
        hashes[key] = item["sha256"]
    # The per-run archive contains different command-line arguments by design.
    item = manifest["inputs"]["guest_initrd"]
    require(digest(item["path"]) == item["sha256"], "changed guest initrd")
    config_text = (
        (m5out / "config.json")
        .read_text()
        .replace(str(run.resolve()), "<RUN>")
    )
    config = json.loads(config_text)
    # Ephemeral host console listener is not a simulated timing parameter.
    config["system"]["platform"]["terminal"]["port"] = "<HOST_CONSOLE_PORT>"
    canonical = json.dumps(config, sort_keys=True)
    identity = {
        "inputs": hashes,
        "configuration_sources": manifest["configuration_sources"],
        "resolved_profile": json.loads(
            (m5out / "resolved-profile.json").read_text()
        ),
        "effective_config_sha256": hashlib.sha256(
            canonical.encode()
        ).hexdigest(),
    }
    layout = []
    for worker, address, size in re.findall(
        r"^CMN_LAYOUT worker=(\d+) begin=(0x[0-9a-f]+) bytes=(\d+)$",
        console,
        re.M,
    ):
        layout.append(
            {
                "worker": int(worker),
                "virtual_start": address,
                "bytes": int(size),
                "page_offset_channel_phase": (int(address, 16) >> 6) & 15,
            }
        )
    if phase:
        require(
            len(layout) == 4
            and {r["worker"] for r in layout} == set(range(4)),
            "missing/duplicate phase layout",
        )
        layout.sort(key=lambda row: row["worker"])
        require(
            all(r["bytes"] == WSS // 4 for r in layout), "layout size mismatch"
        )
        require(
            all(
                int(layout[i]["virtual_start"], 16)
                - int(layout[i - 1]["virtual_start"], 16)
                == WSS // 4 + 64
                for i in range(1, 4)
            ),
            "layout stride mismatch",
        )
        require(
            len({r["page_offset_channel_phase"] for r in layout}) == 4,
            "channel phase not spread",
        )
    else:
        require(not layout, "unexpected phase layout")
    useful = summary["application"]["useful_bytes"]
    read = summary["dram"]["read_bytes"]
    return {
        "name": name,
        "passes": passes,
        "phase": phase,
        "identity": identity,
        "layout": layout,
        "roi_begin_tick": begin,
        "roi_end_tick": end,
        "roi_seconds": summary["roi"]["seconds"],
        "stats_block_index": summary["roi"]["stats_block_index"],
        "host_seconds": result["host_seconds"],
        "useful_bytes": useful,
        "dram_read_bytes": read,
        "dram_write_bytes": summary["dram"]["write_bytes"],
        "useful_GB_s": summary["application"]["GB_s"],
        "dram_read_GB_s": read / summary["roi"]["seconds"] / 1e9,
        "dram_to_useful_ratio": read / useful,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cache = {}
    rows = [load_case(args.base, name, cache) for name in CASES]
    result = compare(rows)
    result.update(
        rows=rows,
        qualification=[
            "MODEL_ONLY: 4 CPUs/workers,16 channels,16MiB,one warmup; not 16-core maximum",
            "Aggregate DRAM counters include OS/background; no physical-address filter",
            "Phase bits6..9 are page-offset bits only; no bank inference",
            "5% bounded last-pair convergence is project criterion, not sustained silicon bandwidth",
        ],
    )
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    table = [
        {k: v for k, v in row.items() if k not in ("identity", "layout")}
        for row in rows
    ]
    with args.output.with_suffix(".csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(table[0]))
        writer.writeheader()
        writer.writerows(table)
    print(
        json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=2)
    )


if __name__ == "__main__":
    main()
