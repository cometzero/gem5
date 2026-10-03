#!/usr/bin/env python3
# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause

"""Compile and execute the 13 PAuth HINT no-op checks on three CPU models."""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gem5", type=Path, required=True)
    parser.add_argument("--cc", default="aarch64-linux-gnu-gcc")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--cpu", action="append", choices=("atomic", "timing", "o3")
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    simulator = args.gem5.resolve()
    probe = output / "pauth_hint_probe"
    build = [
        args.cc,
        "-nostdlib",
        "-static",
        "-Wl,--build-id=none",
        "-Wl,-e,_start",
        str(here / "pauth_hint_probe.S"),
        "-o",
        str(probe),
    ]
    (output / "build-command.json").write_text(json.dumps(build, indent=2))
    with (output / "build.log").open("w") as log:
        subprocess.run(build, stdout=log, stderr=subprocess.STDOUT, check=True)
    report = {
        "gem5": str(simulator),
        "gem5_sha256": sha256(simulator),
        "probe_sha256": sha256(probe),
        "scope": "13 HINT no-ops with FEAT_PAuth absent, direct SE execution",
        "tests": [],
    }
    success = "PAUTH_HINT_NO_PAUTH cases=13 failures=0x0000"
    for cpu in args.cpu or ("atomic", "timing", "o3"):
        directory = output / cpu
        directory.mkdir()
        command = [
            str(simulator),
            "--listener-mode=off",
            "--outdir=" + str(directory / "m5out"),
            str(here / "pauth_hint_se.py"),
            "--cpu=" + cpu,
            "--binary=" + str(probe),
        ]
        (directory / "command.json").write_text(json.dumps(command, indent=2))
        with (directory / "simulator.log").open("w") as log:
            result = subprocess.run(
                command, stdout=log, stderr=subprocess.STDOUT, timeout=60
            )
        text = (directory / "simulator.log").read_text(errors="replace")
        passed = result.returncode == 0 and success in text
        report["tests"].append(
            {
                "cpu": cpu,
                "returncode": result.returncode,
                "result": "PASS" if passed else "FAIL",
            }
        )
        print(f"{cpu}: {'PASS' if passed else 'FAIL'}", flush=True)
    passed = all(item["result"] == "PASS" for item in report["tests"])
    report["result"] = "PASS" if passed else "FAIL"
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
