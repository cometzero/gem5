# SPDX-License-Identifier: BSD-3-Clause
"""Run bounded native DRAM/fabric/NPU diagnostics; retain exact commands.

This intentionally does not boot Linux, change CPU parameters, or modify the
DRAM timing model. Existing output directories are never overwritten.
"""

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIRECTED = ROOT / "tests/zena_cmn/directed"


def cases(suite, mib):
    if suite in ("backend", "all"):
        for read in (100, 0, 50):
            for path, width, roi in (
                ("direct", 32, 20),
                ("direct", 32, 40),
                ("fanout", 32, 40),
                ("single", 32, 40),
                ("single", 256, 40),
                ("single", 512, 40),
            ):
                yield (
                    f"{path}-r{read}-w{width}-us{roi}",
                    "memory_saturation.py",
                    [
                        "--path",
                        path,
                        "--read-percent",
                        str(read),
                        "--fabric-width",
                        str(width),
                        "--roi-us",
                        str(roi),
                    ],
                    "saturation-result.json",
                )
            for outstanding in (1024, 2048):
                yield (
                    f"direct-r{read}-us160-q{outstanding}",
                    "memory_saturation.py",
                    [
                        "--path",
                        "direct",
                        "--read-percent",
                        str(read),
                        "--roi-us",
                        "160",
                        "--outstanding",
                        str(outstanding),
                    ],
                    "saturation-result.json",
                )
    if suite in ("npu", "all"):
        for mode in ("read", "write", "copy"):
            for stage in ("baseline", "dma64", "dma128", "dma256", "dma512"):
                resources = "baseline" if stage == "baseline" else "scaled"
                yield (
                    f"npu-{stage}-{mode}-{mib}m",
                    "npu_saturation.py",
                    [
                        "--stage",
                        stage,
                        "--mode",
                        mode,
                        "--mib",
                        str(mib),
                        "--smmu-resources",
                        resources,
                    ],
                    "npu-saturation.json",
                )
            yield (
                f"npu-dma256-{mode}-{mib}m-default-smmu",
                "npu_saturation.py",
                [
                    "--stage",
                    "dma256",
                    "--mode",
                    mode,
                    "--mib",
                    str(mib),
                    "--smmu-resources",
                    "baseline",
                ],
                "npu-saturation.json",
            )
            for slots in (256, 512):
                yield (
                    f"npu-dma256-{mode}-{mib}m-slots{slots}",
                    "npu_saturation.py",
                    [
                        "--stage",
                        "dma256",
                        "--mode",
                        mode,
                        "--mib",
                        str(mib),
                        "--smmu-resources",
                        "scaled",
                        "--smmu-device-slots",
                        str(slots),
                    ],
                    "npu-saturation.json",
                )
            yield (
                f"npu-dma512-{mode}-{mib}m-slots1024",
                "npu_saturation.py",
                [
                    "--stage",
                    "dma512",
                    "--mode",
                    mode,
                    "--mib",
                    str(mib),
                    "--smmu-resources",
                    "scaled",
                    "--smmu-device-slots",
                    "1024",
                ],
                "npu-saturation.json",
            )


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gem5", type=Path, default=ROOT / "build/ARM/gem5.opt"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--suite", choices=("backend", "npu", "all"), default="all"
    )
    parser.add_argument("--mib", type=int, choices=(1, 2, 4, 8, 16), default=4)
    parser.add_argument(
        "--case", action="append", help="run only these exact case names"
    )
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()
    selected = [
        case
        for case in cases(args.suite, args.mib)
        if not args.case or case[0] in args.case
    ]
    if not selected or args.timeout <= 0:
        parser.error("no matching cases or invalid timeout")
    if args.case and set(args.case) != {case[0] for case in selected}:
        parser.error("unknown case name")
    args.output.mkdir(parents=True, exist_ok=False)
    binary = args.gem5.resolve(strict=True)
    manifest = {
        "qualification": "NATIVE_MODEL_DIAGNOSTIC_NOT_HARDWARE_OR_LINUX",
        "gem5": str(binary),
        "binary_sha256": digest(binary),
        "source_sha256": {
            str(DIRECTED / script): digest(DIRECTED / script)
            for script in {case[1] for case in selected}
        },
        "runs": [],
    }
    for name, script, options, result_name in selected:
        directory = args.output / name
        directory.mkdir()
        command = [
            str(binary),
            "--outdir=" + str(directory.resolve()),
            str(DIRECTED / script),
            *options,
        ]
        started = time.monotonic()
        record = {"case": name, "command": command, "status": "FAIL"}
        with (directory / "run.log").open("w") as log:
            try:
                completed = subprocess.run(
                    command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=args.timeout,
                    check=False,
                )
                record["returncode"] = completed.returncode
                result_path = directory / result_name
                if completed.returncode == 0 and result_path.exists():
                    record["result"] = json.loads(result_path.read_text())
                    record["status"] = record["result"]["status"]
            except subprocess.TimeoutExpired:
                record["status"] = "TIMEOUT"
        record["host_seconds"] = time.monotonic() - started
        manifest["runs"].append(record)
        (args.output / "campaign.json").write_text(
            json.dumps(manifest, indent=2) + "\n"
        )
        print(
            f'{name}: {record["status"]} ({record["host_seconds"]:.1f}s)',
            flush=True,
        )
        if record["status"] != "PASS":
            raise SystemExit(
                f"Stopped at {name}; see run.log. Previous results retained."
            )
    fingerprints = {
        run["result"]["dram_fingerprint"] for run in manifest["runs"]
    }
    if len(fingerprints) != 1:
        raise SystemExit("DRAM fingerprints differ across cases")
    if digest(binary) != manifest["binary_sha256"]:
        raise SystemExit("gem5 binary changed during campaign")
    manifest["status"] = "PASS"
    manifest["dram_fingerprint"] = fingerprints.pop()
    (args.output / "campaign.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
