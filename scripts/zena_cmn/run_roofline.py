#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Run the 26-case single-active-core Roofline probe within 20 host minutes."""
import argparse
import hashlib
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

GEM5 = Path(__file__).resolve().parents[2]
WORKSPACE = GEM5.parent
sys.path.insert(0, str(WORKSPACE / "scripts"))
from run_smoke import prepare
from summarize_roi import stats_blocks
from test_zena_css import sha256

EXPECTED_CASES = set(range(1, 27))


def validate_completion(
    console, events, exit_info, expected_cases=EXPECTED_CASES
):
    lines = console.splitlines()
    if "ROOF_DONE=PASS" not in lines:
        raise RuntimeError("guest did not validate all Roofline cases")
    if "CMN_ONLINE=0-15\n" not in console:
        raise RuntimeError("guest must have 16 online CPUs")
    cases = [
        json.loads(line[len("ROOF_CASE ") :])
        for line in lines
        if line.startswith("ROOF_CASE ")
    ]
    if (
        len(cases) != len(expected_cases)
        or {case.get("id") for case in cases} != expected_cases
    ):
        raise RuntimeError("missing or duplicate guest Roofline cases")
    for case in cases:
        if (
            case.get("result") != "PASS"
            or "checksum" not in case
            or "expected" not in case
            or case["checksum"] != case["expected"]
        ):
            raise RuntimeError(
                f"guest checksum failed for case {case.get('id')}"
            )
    begins = [e["case_id"] for e in events if e["phase"] == "roi_begin"]
    ends = [e["case_id"] for e in events if e["phase"] == "roi_end"]
    if (
        begins != ends
        or set(ends) != expected_cases
        or len(ends) != len(expected_cases)
    ):
        raise RuntimeError("missing or duplicate Roofline ROI cases")
    if (
        exit_info.get("status") != "COMPLETED"
        or exit_info.get("code") != 0
        or exit_info.get("cause") != "m5_exit instruction encountered"
    ):
        raise RuntimeError("simulation did not exit normally")


def validate_measurements(stats_text, events):
    """Check measurement completeness; cache-source qualification is separate."""
    blocks = stats_blocks(stats_text)
    begins = {}
    previous_end = -1
    measured = 0
    for event in events:
        phase = event.get("phase")
        if phase not in ("roi_begin", "roi_end"):
            continue
        case = event["case_id"]
        tick = event.get("tick")
        if type(tick) is not int or tick < 0:
            raise ValueError("invalid measurement tick")
        if phase == "roi_begin":
            if begins or tick < previous_end:
                raise ValueError("overlapping measurement interval")
            begins[case] = tick
            continue
        begin = begins.pop(case, None)
        if begin is None or tick <= begin:
            raise ValueError("invalid measurement interval")
        matches = [
            b
            for b in blocks
            if b.get("finalTick") == tick and b.get("simTicks") == tick - begin
        ]
        if (
            len(matches) != 1
            or not isinstance(matches[0].get("simFreq"), (int, float))
            or matches[0]["simFreq"] <= 0
        ):
            raise ValueError("missing or ambiguous exact ROI statistics")
        previous_end = tick
        measured += 1
    if begins or not measured:
        raise ValueError("incomplete measurement intervals")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("gem5", "kernel", "bootloader", "initrd", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--slc-trace", action="store_true")
    variants = parser.add_mutually_exclusive_group()
    variants.add_argument("--l2-check", action="store_true")
    variants.add_argument("--slc-check", action="store_true")
    variants.add_argument("--calibration", action="store_true")
    variants.add_argument("--memory-patterns", action="store_true")
    variants.add_argument("--load-streams", action="store_true")
    variants.add_argument("--execution-checks", action="store_true")
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument("--max-ticks", type=int, default=10**12)
    args = parser.parse_args()
    if not 0 < args.timeout <= 1200 or args.max_ticks <= 0:
        parser.error("timeout must be in (0, 1200] and max-ticks positive")
    if args.l2_check and args.timeout > 600:
        parser.error("l2-check timeout must not exceed 600 seconds")
    if args.slc_trace and not args.slc_check:
        parser.error("--slc-trace requires --slc-check")
    expected_cases = (
        set(range(301, 316))
        if args.load_streams
        else (
            set(range(201, 208))
            if args.memory_patterns
            else (
                set(range(101, 109))
                if args.calibration
                else {12, 13} if args.l2_check else EXPECTED_CASES
            )
        )
    )
    if args.execution_checks:
        expected_cases = set(range(401, 413))
    if args.slc_check:
        expected_cases = set(range(22, 27))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = {
        "status": "FAIL",
        "qualification": "MODEL_ONLY",
        "execution_status": "FAIL",
        "measurement_status": "NOT_RUN",
        "source_qualification": "NOT_EVALUATED",
        "status_contract": "PASS requires execution and exact ROI statistics; not a qualified bandwidth roof",
    }
    process = connection = None
    try:
        with (output / "guest-build.log").open("w") as log:
            subprocess.run(
                [
                    str(GEM5 / "workloads/zena_cmn/build.sh"),
                    str(output / "guest"),
                ],
                check=True,
                timeout=args.timeout,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        workload = (
            "calibration_probe"
            if args.calibration
            or args.memory_patterns
            or args.load_streams
            or args.execution_checks
            else "roofline_probe"
        )
        binary = output / "guest" / workload
        guest_command = ["/" + workload]
        if args.execution_checks:
            guest_command.append("--execution-checks")
        elif args.slc_check:
            guest_command.append("--slc-check")
        elif args.l2_check:
            guest_command.append("--l2-check")
        elif args.load_streams:
            guest_command.append("--load-streams")
        elif args.memory_patterns:
            guest_command.append("--memory-patterns")
        image = prepare(args.initrd, binary, guest_command, output)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        command = [
            str(args.gem5.resolve()),
            "--listener-mode=on",
            "--outdir=" + str(output / "m5out"),
            str(GEM5 / "configs/zena_cmn/run_fs.py"),
            "--kernel=" + str(args.kernel.resolve()),
            "--bootloader=" + str(args.bootloader.resolve()),
            "--initrd=" + str(image),
            "--terminal-port=" + str(port),
            "--max-ticks=" + str(args.max_ticks),
            "--cpus=16",
            "--roofline",
            "--channels=16",
            "--command-line=console=ttyAMA0,115200 earlycon=pl011,0x1a400000 rdinit=/cmn-init lpj=1000000",
        ]
        if args.profile:
            command.append("--profile=" + str(args.profile.resolve()))
        if args.slc_trace:
            command.append("--roofline-trace")
        manifest = {
            "revision": subprocess.check_output(
                ["git", "-C", str(GEM5), "rev-parse", "HEAD"], text=True
            ).strip(),
            "tracked_diff_sha256": hashlib.sha256(
                subprocess.check_output(
                    ["git", "-C", str(GEM5), "diff", "HEAD"]
                )
            ).hexdigest(),
            "workload_sources": {
                str(path.relative_to(GEM5)): sha256(path)
                for path in (GEM5 / "workloads/zena_cmn").glob("*")
                if path.is_file()
            },
            "runner_sha256": sha256(Path(__file__)),
            "configuration_sources": {
                str(path.relative_to(GEM5)): sha256(path)
                for path in sorted((GEM5 / "configs/zena_cmn").rglob("*"))
                if path.suffix in (".py", ".yaml")
            },
            "command": command,
            "guest_command": guest_command,
            "cpu_count": 16,
            "channels": 16,
            "expected_cases": sorted(expected_cases),
            "variant": (
                "execution_checks"
                if args.execution_checks
                else (
                    "slc_check"
                    if args.slc_check
                    else (
                        "load_streams"
                        if args.load_streams
                        else (
                            "memory_patterns"
                            if args.memory_patterns
                            else (
                                "calibration"
                                if args.calibration
                                else (
                                    "l2_full_traversal"
                                    if args.l2_check
                                    else "all_levels"
                                )
                            )
                        )
                    )
                )
            ),
            "timeout_includes_preparation": True,
            "host_timeout_seconds": args.timeout,
            "inputs": {
                name: {"path": str(path.resolve()), "sha256": sha256(path)}
                for name, path in {
                    "gem5": args.gem5,
                    "kernel": args.kernel,
                    "bootloader": args.bootloader,
                    "base_initrd": args.initrd,
                    "guest_initrd": image,
                    "guest": binary,
                }.items()
            },
        }
        if args.execution_checks:
            manifest["execution_qualification"] = {
                "documented_targets": {
                    "FADD_Q_FP32": "latency 2 cycles",
                    "FMLA_Q_FP32": "general latency 4 cycles; independent throughput 2/cycle",
                },
                "unqualified": [
                    "accumulator bypass",
                    "operand alias bypass",
                    "format-specific divider",
                    "LSU micro-op decomposition",
                    "shared FU contention",
                    "silicon performance",
                ],
                "scope": "exact instruction/operand diagnostics only; checksum PASS is not calibration PASS",
            }
        if args.profile:
            manifest["inputs"]["profile"] = {
                "path": str(args.profile.resolve()),
                "sha256": sha256(args.profile),
            }
        (output / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n"
        )
        deadline = started + args.timeout
        if time.monotonic() >= deadline:
            raise TimeoutError("preparation exhausted host runtime budget")
        with (output / "simout").open("w") as stdout, (output / "simerr").open(
            "w"
        ) as stderr, (output / "guest_console.log").open("wb") as serial:
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr)
            while time.monotonic() < deadline and process.poll() is None:
                if connection is None:
                    try:
                        connection = socket.create_connection(
                            ("127.0.0.1", port), timeout=0.5
                        )
                        connection.settimeout(0.5)
                    except OSError:
                        time.sleep(0.1)
                        continue
                try:
                    data = connection.recv(65536)
                    if data:
                        serial.write(data)
                        serial.flush()
                except socket.timeout:
                    pass
            if process.poll() is None:
                raise TimeoutError("host runtime budget exhausted")
            if connection is not None:
                while True:
                    try:
                        data = connection.recv(65536)
                    except (OSError, socket.timeout):
                        break
                    if not data:
                        break
                    serial.write(data)
            if process.returncode:
                raise RuntimeError(f"gem5 exited {process.returncode}")
        console = (
            (output / "guest_console.log")
            .read_text(errors="replace")
            .replace("\r", "")
        )
        events = json.loads((output / "m5out/roi-events.json").read_text())
        exit_info = json.loads((output / "m5out/exit.json").read_text())
        validate_completion(console, events, exit_info, expected_cases)
        result["execution_status"] = "PASS"
        result["measurement_status"] = "FAIL"
        validate_measurements((output / "m5out/stats.txt").read_text(), events)
        result["measurement_status"] = "PASS"
        result["status"] = "PASS"
    except Exception as exc:
        if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
            result["status"] = "TIMEOUT"
        result["error"] = str(exc)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if connection is not None:
            connection.close()
        result["host_seconds"] = time.monotonic() - started
        result["exit_code"] = process.returncode if process else None
        (output / "result.json").write_text(
            json.dumps(result, indent=2) + "\n"
        )
    print(json.dumps(result))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
