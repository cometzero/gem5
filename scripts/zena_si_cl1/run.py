#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Run standalone CL1, preserving command, output and model provenance."""

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gem5", type=Path, default=root / "build/ARM/gem5.opt"
    )
    parser.add_argument(
        "--elf",
        type=Path,
        default=root.parent / "build/zena/si-cl1/zephyr/bin/zephyr.elf",
    )
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument(
        "--cpu", choices=["atomic", "timing", "minor"], default="minor"
    )
    parser.add_argument("--clock", default="1GHz")
    parser.add_argument("--sram-latency", default="20ns")
    parser.add_argument("--tcm-latency", default="1ns")
    parser.add_argument("--max-ticks", type=int, default=200000000000)
    parser.add_argument("--roi-schedule", type=Path)
    parser.add_argument(
        "--expect-marker",
        default="SI_CL1_RESULT status=PASS",
        help="required guest success marker",
    )
    args = parser.parse_args()
    if args.timeout <= 0 or args.max_ticks <= 0:
        parser.error("timeout and max-ticks must be positive")
    extra = [
        "--cpu",
        args.cpu,
        "--clock",
        args.clock,
        "--sram-latency",
        args.sram_latency,
        "--tcm-latency",
        args.tcm_latency,
        "--max-ticks",
        str(args.max_ticks),
    ]
    if args.roi_schedule:
        if not args.roi_schedule.is_file():
            parser.error(f"ROI schedule missing: {args.roi_schedule}")
        extra += ["--roi-schedule", str(args.roi_schedule.resolve())]
    if not args.gem5.is_file():
        parser.error(
            f"gem5 binary missing: {args.gem5}; build ARM/gem5.opt first"
        )
    if not args.elf.is_file():
        parser.error(
            f"ELF missing: {args.elf}; build and verify CL1 in QEMU first"
        )
    if args.outdir.exists():
        parser.error(f"output directory already exists: {args.outdir}")
    if not args.expect_marker:
        parser.error("expected guest marker must not be empty")
    args.outdir.mkdir(parents=True, exist_ok=False)
    command = [
        str(args.gem5.resolve()),
        "--outdir",
        str(args.outdir.resolve()),
        str(root / "configs/zena_si_cl1/run.py"),
        "--elf",
        str(args.elf.resolve()),
        *extra,
    ]
    manifest = {
        "command": command,
        "elf_sha256": sha(args.elf),
        "gem5_sha256": sha(args.gem5),
        "configuration": "standalone SI CL1 experimental PMSA64",
        "configuration_sha256": sha(root / "configs/zena_si_cl1/run.py"),
        "configuration_args": {
            "cpu": args.cpu,
            "clock": args.clock,
            "sram_latency": args.sram_latency,
            "tcm_latency": args.tcm_latency,
            "max_ticks": args.max_ticks,
        },
        "limitations": "generic timing proxy; no hardware calibration",
        "roi_schedule_sha256": (
            sha(args.roi_schedule) if args.roi_schedule else None
        ),
    }
    (args.outdir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    start = time.monotonic()
    with (args.outdir / "gem5.log").open("w") as log:
        try:
            result = subprocess.run(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=args.timeout,
                check=False,
            )
            rc = result.returncode
        except subprocess.TimeoutExpired:
            rc = 124
    serial_path = args.outdir / "system.platform.terminal"
    serial = (
        serial_path.read_text(errors="replace") if serial_path.exists() else ""
    )
    guest_pass = args.expect_marker in serial and "status=FAIL" not in serial
    manifest.update(
        gem5_returncode=rc,
        guest_pass=guest_pass,
        expected_marker=args.expect_marker,
    )
    if rc == 0 and not guest_pass:
        rc = 2
    elif rc < 0:
        rc = 128 - rc
    manifest.update(returncode=rc, host_seconds=time.monotonic() - start)
    (args.outdir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    print(json.dumps({"outdir": str(args.outdir), **manifest}))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
