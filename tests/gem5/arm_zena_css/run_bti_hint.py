#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Check absent-FEAT_BTI state preservation and absence of O3 serialization."""

import argparse
import json
import re
import subprocess
from pathlib import Path

from run_pauth_hint import sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gem5", type=Path, required=True)
    parser.add_argument("--cc", default="aarch64-linux-gnu-gcc")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    binary = output / "bti_hint_probe"
    command = [
        args.cc,
        "-nostdlib",
        "-static",
        "-Wl,--build-id=none",
        str(here / "bti_hint_probe.S"),
        "-o",
        str(binary),
    ]
    (output / "build-command.json").write_text(json.dumps(command, indent=2))
    subprocess.run(command, check=True)
    report = {
        "gem5_sha256": sha256(args.gem5),
        "binary_sha256": sha256(binary),
        "tests": [],
        "scope": "BTI NOP behavior without FEAT_BTI, not guarded pages",
    }
    for cpu in ("atomic", "timing", "o3"):
        directory = output / cpu
        directory.mkdir()
        # Reuse the existing Armv8 (no FEAT_BTI/PAuth) SE fixture.
        command = [
            str(args.gem5.resolve()),
            "--listener-mode=off",
            "--outdir=" + str(directory / "m5out"),
            str(here / "pauth_hint_se.py"),
            "--cpu=" + cpu,
            "--binary=" + str(binary),
        ]
        (directory / "command.json").write_text(json.dumps(command, indent=2))
        with (directory / "simulator.log").open("w") as log:
            result = subprocess.run(
                command, stdout=log, stderr=subprocess.STDOUT, timeout=60
            )
        log = (directory / "simulator.log").read_text()
        functional = (
            result.returncode == 0
            and "BTI_HINT_NO_BTI variants=4 iterations=1024 state=PASS" in log
        )
        item = {
            "cpu": cpu,
            "functional": functional,
            "returncode": result.returncode,
        }
        passed = functional
        if cpu == "o3" and functional:
            stats = (directory / "m5out/stats.txt").read_text()
            values = re.findall(
                r"^system.cpu.commit.commitNonSpecStalls\s+(\d+)", stats, re.M
            )
            if not values:
                raise ValueError("missing O3 non-speculative stall counter")
            item["non_spec_stalls"] = int(values[-1])
            # Two Linux syscalls are serializing; the 4096 BTIs must not be.
            passed &= item["non_spec_stalls"] <= 2
        item["status"] = "PASS" if passed else "FAIL"
        report["tests"].append(item)
        print(json.dumps(item), flush=True)
    passed = all(item["status"] == "PASS" for item in report["tests"])
    report["status"] = "PASS" if passed else "FAIL"
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
