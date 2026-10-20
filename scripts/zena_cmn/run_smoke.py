#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Run one bounded Linux CMN workload, preserving boot and ROI evidence."""
import argparse
import gzip
import hashlib
import json
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

GEM5 = Path(__file__).resolve().parents[2]
WORKSPACE = GEM5.parent
sys.path.insert(0, str(WORKSPACE / "scripts"))
from summarize_roi import (
    event_bounds,
    summarize,
)
from test_zena_css import (
    add_cpio_entry,
    sha256,
)


def prepare(base, binary, command, output, disk=False):
    disk_check = ""
    if disk:
        disk_check = """
mkdir -p /mnt /tmp
if mount /dev/vda /mnt &&
   dd if=/bin/busybox of=/tmp/cmn-reference bs=1024 count=64 &&
   cp /tmp/cmn-reference /mnt/cmn-probe && sync && umount /mnt &&
   mount -o ro /dev/vda /mnt && cmp /tmp/cmn-reference /mnt/cmn-probe &&
   umount /mnt; then
    echo CMN_DISK=PASS
else
    echo CMN_DISK=FAIL
    status=1
fi
"""
    script = f"""#!/bin/sh
export PATH=/bin:/sbin:/usr/bin:/usr/sbin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
exec </dev/console >/dev/console 2>&1
echo CMN_ONLINE=$(cat /sys/devices/system/cpu/online)
if [ ! -d /sys/devices/system/cpu/cpu0/cache/index0 ]; then
    echo CMN_CACHE=UNAVAILABLE
fi
for cache in /sys/devices/system/cpu/cpu*/cache/index*; do
    [ -d "$cache" ] || continue
    read -r level < "$cache/level"
    read -r type < "$cache/type"
    read -r shared < "$cache/shared_cpu_list"
    echo CMN_CACHE "$cache" "$level" "$type" "$shared"
done
{shlex.join(command)}
status=$?
{disk_check}
echo CMN_EXIT=$status
sync
poweroff -f
while :; do sleep 1; done
"""
    archive = bytearray()
    add_cpio_entry(archive, binary.name, binary.read_bytes())
    add_cpio_entry(archive, "cmn-init", script.encode())
    add_cpio_entry(archive, "TRAILER!!!", mode=0)
    archive.extend(b"\0" * (-len(archive) % 512))
    data = base.read_bytes()
    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    image = output / "guest.cpio"
    image.write_bytes(data + archive)
    (output / "guest-init.sh").write_text(script)
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("gem5", "kernel", "bootloader", "initrd", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--cpus", type=int, choices=(4, 8, 16), default=16)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--disk-image", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--channels", type=int, choices=(4, 8, 16), default=16)
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument("--max-ticks", type=int, default=10**12)
    parser.add_argument(
        "--mode",
        choices=("coherence", "read", "copy", "stream", "npu", "npu-bench"),
        default="coherence",
    )
    parser.add_argument(
        "--npu-app",
        type=Path,
        default=WORKSPACE / "build/gem5-zena/npu-apps/npu_v01_gemm",
    )
    parser.add_argument(
        "--npu-workload", choices=("cpu", "npu", "concurrent"), default="npu"
    )
    parser.add_argument("--npu-mib", type=int, choices=(1, 4, 16), default=1)
    parser.add_argument("--npu-async", action="store_true")
    parser.add_argument("--npu-jobs", type=int, default=1)
    parser.add_argument("--npu-cpu-delay-us", type=int, default=0)
    parser.add_argument("--iterations", type=int, default=4)
    parser.add_argument(
        "--eviction-mib", type=int, choices=(1, 64), default=64
    )
    parser.add_argument("--passes", type=int, choices=(1, 2, 4, 8), default=2)
    parser.add_argument(
        "--kernel-mode", choices=("scalar", "neon"), default="scalar"
    )
    parser.add_argument("--array-mib", type=int)
    parser.add_argument("--initialize-before-switch", action="store_true")
    parser.add_argument("--channel-phase-spread", action="store_true")
    parser.add_argument("--direct-timing", action="store_true")
    parser.add_argument("--prefault-before-switch", action="store_true")
    args = parser.parse_args()
    if args.mode.startswith("npu") and args.profile is None:
        name = (
            "a720ae_npu_v01_async.yaml"
            if args.npu_async
            else "a720ae_npu_v01.yaml"
        )
        args.profile = GEM5 / "configs/zena_cmn/profiles" / name
    if not 0 <= args.npu_cpu_delay_us <= 100000 or (
        args.npu_cpu_delay_us
        and (args.mode != "npu-bench" or args.npu_workload == "npu")
    ):
        parser.error("npu-cpu-delay-us requires CPU benchmark and 0..100000")
    if args.npu_async and args.mode != "npu-bench":
        parser.error("npu-async requires npu-bench")
    if not 1 <= args.npu_jobs <= 32 or (
        args.npu_jobs != 1 and args.mode != "npu-bench"
    ):
        parser.error("npu-jobs requires npu-bench and 1..32")
    if (
        args.mode == "npu-bench"
        and args.npu_workload != "cpu"
        and args.npu_mib * args.passes * 2 > 64
    ):
        parser.error("NPU tensor DMA exceeds 64 MiB per job")
    if args.channel_phase_spread and args.mode != "read":
        parser.error("channel-phase-spread supports read only")
    if args.prefault_before_switch and args.mode != "coherence":
        parser.error("prefault-before-switch is only for coherence")
    if args.prefault_before_switch and args.direct_timing:
        parser.error("prefault-before-switch requires Atomic boot")
    if args.initialize_before_switch and (
        args.mode == "coherence" or args.direct_timing
    ):
        parser.error(
            "initialization-before-switch requires memory workload and Atomic boot"
        )
    if args.array_mib is not None and (
        args.mode == "coherence" or not 1 <= args.array_mib <= 512
    ):
        parser.error("array-mib requires memory workload and 1..512 MiB")
    if args.kernel_mode == "neon" and args.mode != "read":
        parser.error("NEON currently supports read only")
    args.workers = args.workers or args.cpus
    if args.workers > args.cpus or args.workers not in (1, 2, 4, 8, 16):
        parser.error("worker count must fit online CPUs")
    if args.timeout <= 0 or args.max_ticks <= 0 or args.iterations <= 0:
        parser.error("timeouts and iterations must be positive")
    if (args.mode == "coherence" and args.workers == 1) or (
        args.mode != "coherence" and args.workers == 2
    ):
        parser.error(
            "coherence supports 2/4/8/16; memory supports 1/4/8/16 workers"
        )
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = {"status": "FAIL", "qualification": "MODEL_ONLY"}
    process = connection = None
    try:
        if args.mode == "npu-bench":
            binary = args.npu_app.resolve().with_name("npu_v01_bench")
            guest_command = [
                "/" + binary.name,
                "--mode",
                args.npu_workload,
                "--mib",
                str(args.npu_mib),
                "--workers",
                str(args.workers),
                "--passes",
                str(args.passes),
                "--gem5-markers",
            ]
            if args.npu_jobs != 1:
                guest_command += ["--jobs", str(args.npu_jobs)]
            if args.npu_cpu_delay_us:
                guest_command += ["--cpu-delay-us", str(args.npu_cpu_delay_us)]
            if args.npu_async:
                guest_command.append("--async")
        elif args.mode == "npu":
            binary = args.npu_app.resolve()
            app = "/" + binary.name
            guest_command = [
                "/bin/sh",
                "-c",
                shlex.join([app, "--invalid", "--noncoherent"])
                + " && "
                + shlex.join(
                    [app, "--repeat", "--noncoherent", "--gem5-markers"]
                ),
            ]
        else:
            with (output / "guest-build.log").open("w") as log:
                subprocess.run(
                    [
                        str(GEM5 / "workloads/zena_cmn/build.sh"),
                        str(output / "guest"),
                    ],
                    check=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            binary_name = (
                "coherence_smoke"
                if args.mode == "coherence"
                else "memory_bench"
            )
            guest_command = ["/" + binary_name, "--workers", str(args.workers)]
            if args.mode == "coherence":
                guest_command += [
                    "--iterations",
                    str(args.iterations),
                    "--eviction-mib",
                    str(args.eviction_mib),
                ]
            else:
                guest_command += [
                    "--mode",
                    args.mode,
                    "--passes",
                    str(args.passes),
                ]
                guest_command += ["--kernel", args.kernel_mode]
                if args.array_mib is not None:
                    guest_command += ["--array-mib", str(args.array_mib)]
                if args.initialize_before_switch:
                    guest_command.append("--initialize-before-switch")
                if args.channel_phase_spread:
                    guest_command.append("--channel-phase-spread")
            if args.prefault_before_switch:
                guest_command.append("--prefault-before-switch")
            binary = output / "guest" / binary_name
        image = prepare(
            args.initrd, binary, guest_command, output, bool(args.disk_image)
        )
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
            "--cpus=" + str(args.cpus),
            "--channels=" + str(args.channels),
            "--command-line=console=ttyAMA0,115200 earlycon=pl011,0x1a400000 rdinit=/cmn-init lpj=1000000",
        ]
        if args.profile:
            command.append("--profile=" + str(args.profile.resolve()))
        if args.disk_image:
            command.append("--disk-image=" + str(args.disk_image.resolve()))
        if args.direct_timing:
            command.append("--boot-mode=timing")
        manifest = {
            "revision": subprocess.check_output(
                ["git", "-C", str(GEM5), "rev-parse", "HEAD"], text=True
            ).strip(),
            "tracked_diff_sha256": hashlib.sha256(
                subprocess.check_output(
                    ["git", "-C", str(GEM5), "diff", "HEAD"]
                )
            ).hexdigest(),
            "configuration_sources": {
                str(path.relative_to(GEM5)): sha256(path)
                for path in sorted((GEM5 / "configs/zena_cmn").rglob("*"))
                if path.suffix in (".py", ".yaml")
            },
            "command": command,
            "guest_command": guest_command,
            "cpu_count": args.cpus,
            "channels": args.channels,
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
        if args.mode.startswith("npu"):
            sources = list((GEM5 / "src/dev/npu").glob("*"))
            sources += list((GEM5 / "src/dev/arm").glob("smmu_v3*"))
            sources += list(
                (WORKSPACE / "linux/drivers/accel/npu-v01").glob("*")
            )
            sources += [WORKSPACE / "linux/include/uapi/drm/npu_v01_accel.h"]
            sources += list((WORKSPACE / "npu_apps").glob("*.c"))
            sources += list((WORKSPACE / "npu_apps").glob("*.h"))
            sources += [
                GEM5 / "src/arch/arm/ArmISA.py",
                GEM5 / "src/arch/arm/isa.cc",
            ]
            manifest["device_sources"] = {
                str(path.relative_to(WORKSPACE)): sha256(path)
                for path in sorted(sources)
                if path.is_file()
            }
            kernel_manifest = args.kernel.parent / "zena-build-manifest.json"
            if kernel_manifest.exists():
                manifest["inputs"]["kernel_build_manifest"] = {
                    "path": str(kernel_manifest.resolve()),
                    "sha256": sha256(kernel_manifest),
                }
        if args.profile:
            manifest["inputs"]["profile"] = {
                "path": str(args.profile.resolve()),
                "sha256": sha256(args.profile),
            }
        if args.disk_image:
            manifest["inputs"]["disk_base"] = {
                "path": str(args.disk_image.resolve()),
                "sha256": sha256(args.disk_image),
            }
        (output / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n"
        )
        deadline = time.monotonic() + args.timeout
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
        if "CMN_EXIT=0" not in console:
            raise RuntimeError("guest did not finish successfully")
        if args.disk_image and "CMN_DISK=PASS" not in console:
            raise RuntimeError("timing-mode disk write/read check failed")
        expected_online = "0" if args.cpus == 1 else f"0-{args.cpus-1}"
        if "CMN_ONLINE=" + expected_online + "\n" not in console:
            raise RuntimeError(
                "guest online CPU count differs from requested model"
            )
        events = json.loads((output / "m5out/roi-events.json").read_text())
        begin, end = event_bounds(events)
        if args.mode == "npu-bench":
            from summarize_npu import summarize_npu_bench

            summary = summarize_npu_bench(
                (output / "m5out/stats.txt").read_text(),
                console,
                begin,
                end,
                args.channels,
            )
        elif args.mode == "npu":
            from summarize_npu import summarize_npu

            summary = summarize_npu(
                (output / "m5out/stats.txt").read_text(),
                console,
                begin,
                end,
                args.channels,
            )
        else:
            summary = summarize(
                (output / "m5out/stats.txt").read_text(), console, begin, end
            )
        (output / "roi_summary.json").write_text(
            json.dumps(summary, indent=2, allow_nan=False) + "\n"
        )
        result["status"] = "PASS"
    except Exception as exc:
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
