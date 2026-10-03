#!/usr/bin/env python3
# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Run the stock ARM Linux control independently of Zena feature validation."""

import argparse
import gzip
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(WORKSPACE / "scripts"))
from test_zena_css import (
    Console,
    add_cpio_entry,
    irq_delta,
    require,
    sha256,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gem5", type=Path, required=True)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--initrd", type=Path, required=True)
    parser.add_argument("--binaries", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--terminal-port", type=int, default=3956)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    result = {
        "result": "FAIL",
        "scope": "VExpress ARM baseline control",
        "zena_validation": False,
        "checks": {},
    }
    process = connection = None
    start = time.monotonic()
    try:
        source = output / "control-exit.S"
        source.write_text(
            ".text\n.global _start\n_start:\n"
            "mov x0, #0\n.inst 0xff210110\nb .\n"
        )
        binary = output / "control-exit"
        subprocess.run(
            [
                "aarch64-linux-gnu-gcc",
                "-nostdlib",
                "-static",
                "-Wl,--build-id=none",
                str(source),
                "-o",
                str(binary),
            ],
            check=True,
        )
        archive = bytearray()
        add_cpio_entry(archive, "control-exit", binary.read_bytes())
        add_cpio_entry(archive, "TRAILER!!!", mode=0)
        archive.extend(b"\0" * (-len(archive) % 512))
        initrd = output / "control-initrd.cpio"
        contents = args.initrd.read_bytes()
        if contents.startswith(b"\x1f\x8b"):
            contents = gzip.decompress(contents)
        initrd.write_bytes(contents + archive)
        command = [
            str(args.gem5.resolve()),
            "--listener-mode=on",
            f"--outdir={output / 'm5out'}",
            str(Path(__file__).with_name("control_fs.py")),
            f"--kernel={args.kernel.resolve()}",
            f"--initrd={initrd}",
            f"--binaries={args.binaries.resolve()}",
            "--cpus=4",
            f"--terminal-port={args.terminal_port}",
        ]
        (output / "command.json").write_text(json.dumps(command, indent=2))
        result["inputs"] = {
            name: {"path": str(path.resolve()), "sha256": sha256(path)}
            for name, path in (
                ("kernel", args.kernel),
                ("initrd", initrd),
                ("firmware", args.binaries / "boot_v2.arm64"),
            )
        }
        with (output / "simulator.log").open("w") as log, (
            output / "console.log"
        ).open("wb") as console_log:
            process = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT
            )
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    connection = socket.create_connection(
                        ("127.0.0.1", args.terminal_port), 1
                    )
                    break
                except OSError:
                    require(
                        process.poll() is None,
                        "simulator exited before listening",
                    )
                    time.sleep(0.1)
            require(connection is not None, "control UART did not listen")
            console = Console(connection, console_log, args.timeout)
            console.expect(r"(?:^|\n)(?:/|~)? ?# $")
            console.send("stty -echo; export PS1='CONTROL# '")
            console.command(
                "mount -t proc proc /proc; mount -t sysfs sysfs /sys; echo READY"
            )
            identity = console.command(
                "uname -a; cat /proc/device-tree/model; echo"
            )
            (output / "identity.txt").write_text(identity)
            online = console.command("cat /sys/devices/system/cpu/online")
            require(online == "0-3", f"control CPUs not online: {online}")
            dt = console.command(
                "for p in /proc/device-tree/cpus/cpu@*/reg; do "
                "od -An -v -tx1 \"$p\" | tr -d ' \\n'; echo; done"
            )
            affinities = sorted(int(value, 16) for value in dt.splitlines())
            require(
                affinities == [0, 1, 2, 3],
                f"wrong default affinities: {affinities}",
            )
            result["checks"]["linux_boot_smp"] = {
                "result": "PASS",
                "online": online,
                "affinities": affinities,
            }
            before = console.command("cat /proc/interrupts")
            console.command("sleep 0.05")
            after = console.command("cat /proc/interrupts")
            (output / "interrupts-before.txt").write_text(before)
            (output / "interrupts-after.txt").write_text(after)
            delta = irq_delta(
                before, after, 4, lambda name: "arch_timer" in name
            )
            require(
                sum(delta) > 0, f"control timer did not interrupt: {delta}"
            )
            result["checks"]["timer"] = {"result": "PASS", "irq_delta": delta}
            config = json.loads((output / "m5out/config.json").read_text())
            require(
                config["system"]["cpu_affinities"] == [],
                "explicit affinities enabled",
            )
            dmesg = console.command("dmesg")
            (output / "dmesg.txt").write_text(dmesg)
            require(
                "Kernel panic" not in dmesg and "Internal error:" not in dmesg,
                "control kernel fault",
            )
            console.send("echo GEM5_ARM_CONTROL_PASS; /control-exit")
            process.wait(timeout=60)
            require(
                process.returncode == 0,
                f"gem5 exit status {process.returncode}",
            )
            event = json.loads(
                (output / "m5out/control-exit.json").read_text()
            )
            require(
                event["cause"] == "m5_exit instruction encountered"
                and event["code"] == 0,
                f"unexpected control exit {event}",
            )
            result["dt_adaptations"] = json.loads(
                (output / "m5out/control-dt-adaptations.json").read_text()
            )
            result["limitations"] = [
                "UART DT fixed-clock fixture for kernel without CLK_VEXPRESS",
                "VExpress configurable clock, RTC and watchdog not qualified",
                "Guest m5_exit only; no PSCI shutdown/reset qualification",
            ]
            result["exit"] = event
            result["result"] = "PASS"
    except Exception as exc:
        result["error"] = repr(exc)
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if connection:
            connection.close()
        result["host_elapsed_seconds"] = time.monotonic() - start
        (output / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2), flush=True)
    return int(result["result"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
