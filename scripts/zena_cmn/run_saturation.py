# SPDX-License-Identifier: BSD-3-Clause
"""Run bounded native DRAM/fabric/NPU diagnostics; retain exact commands.

This intentionally does not boot Linux, change CPU parameters, or modify the
DRAM timing model. Existing output directories are never overwritten.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIRECTED = ROOT / "tests/zena_cmn/directed"
LEGACY_PROFILE = ROOT / "configs/zena_cmn/profiles/a720ae_npu_v01_async.yaml"


def backend_parameters(memory_profile=None, path="direct", fabric_width=32):
    """Select memory only; diagnostic routing remains owned by --path."""
    sys.path.insert(0, str(ROOT / "configs"))
    from zena_cmn.parameters import (
        DEFAULT_PROFILE,
        load_parameters,
    )

    if path not in ("direct", "fanout", "single"):
        raise ValueError("unsupported backend path")
    params = load_parameters(LEGACY_PROFILE)
    selected = (
        Path(memory_profile).resolve(strict=True)
        if memory_profile
        else LEGACY_PROFILE
    )
    if memory_profile:
        params["memory"] = load_parameters(selected)["memory"]
    memory = params["memory"]
    expected = dict(channels=16, interleave_bytes=64, capacity_mib_total=2048)
    if any(memory.get(key) != value for key, value in expected.items()):
        raise ValueError(
            "backend generator requires 16 channels, 64B interleave "
            "and a 2048 MiB aperture"
        )
    identity = {
        "path": str(selected),
        "sha256": digest(selected),
        "selection": (
            "memory_only" if memory_profile else "legacy_full_profile"
        ),
        "default_profile": str(DEFAULT_PROFILE),
        "default_sha256": digest(DEFAULT_PROFILE),
        "routing_profile": str(LEGACY_PROFILE),
        "routing_sha256": digest(LEGACY_PROFILE),
    }
    routed = path != "direct"
    params["npu"]["enabled"] = routed
    params["scope"]["npu_present"] = routed
    memory["create_future_npu_arbiter_now"] = routed
    if routed:
        params["npu"]["fabric_width"] = fabric_width
    return params, identity


def backend_source_hashes():
    """Record Python model definitions as well as the diagnostic harness."""
    paths = [Path(__file__), DIRECTED / "memory_saturation.py"]
    paths += list((ROOT / "configs/zena_cmn").rglob("*.py"))
    paths += [ROOT / "configs/example/arm/zena_css_models.py"]
    paths += [
        ROOT / "src/mem" / (name + ".py")
        for name in (
            "DRAMInterface",
            "MemInterface",
            "MemCtrl",
            "AbstractMemory",
        )
    ]
    return {str(path): digest(path) for path in sorted(paths)}


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


def main(argv=None):
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
        "--memory-profile",
        type=Path,
        help="use only this full profile's memory settings (backend suite)",
    )
    parser.add_argument(
        "--case", action="append", help="run only these exact case names"
    )
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args(argv)
    memory_identity = None
    if args.memory_profile:
        if args.suite != "backend":
            parser.error("--memory-profile requires --suite backend")
        try:
            _, memory_identity = backend_parameters(args.memory_profile)
            args.memory_profile = Path(memory_identity["path"])
        except (OSError, ValueError) as error:
            parser.error(str(error))
    selected = [
        case
        for case in cases(args.suite, args.mib)
        if not args.case or case[0] in args.case
    ]
    if not selected or args.timeout <= 0:
        parser.error("no matching cases or invalid timeout")
    if args.case and set(args.case) != {case[0] for case in selected}:
        parser.error("unknown case name")
    binary = args.gem5.resolve(strict=True)
    args.output.mkdir(parents=True, exist_ok=False)
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
    if args.suite == "backend":
        manifest["source_sha256"].update(backend_source_hashes())
        manifest["memory_profile"] = memory_identity or backend_parameters()[1]
    for name, script, options, result_name in selected:
        directory = args.output / name
        directory.mkdir()
        command = [
            str(binary),
            "--outdir=" + str(directory.resolve()),
            str(DIRECTED / script),
            *options,
        ]
        if args.memory_profile:
            command += ["--memory-profile", str(args.memory_profile)]
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
                    if args.memory_profile and (
                        record["result"].get("memory_profile")
                        != manifest["memory_profile"]
                    ):
                        record["status"] = "FAIL"
                        record["reason"] = "native memory profile mismatch"
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
    for path, expected in manifest["source_sha256"].items():
        if digest(Path(path)) != expected:
            raise SystemExit(f"source changed during campaign: {path}")
    if args.suite == "backend" and (
        backend_parameters(args.memory_profile)[1]
        != manifest["memory_profile"]
    ):
        raise SystemExit("memory profile changed during campaign")
    manifest["status"] = "PASS"
    manifest["dram_fingerprint"] = fingerprints.pop()
    (args.output / "campaign.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
