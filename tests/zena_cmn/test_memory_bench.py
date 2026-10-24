#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Check bounded memory workload kernels; not gem5 timing qualification."""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SOURCE = (
    Path(__file__).resolve().parents[2] / "workloads/zena_cmn/memory_bench.c"
)


@unittest.skipUnless(
    shutil.which("aarch64-linux-gnu-gcc")
    and shutil.which("qemu-aarch64-static"),
    "requires AArch64 cross compiler and qemu-aarch64-static",
)
class MemoryBench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.directory.name) / "memory_bench"
        subprocess.run(
            [
                "aarch64-linux-gnu-gcc",
                "-std=c11",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-pthread",
                "-static",
                "-march=armv8-a",
                "-mno-outline-atomics",
                str(SOURCE),
                "-o",
                str(cls.binary),
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def run_guest(self, *args):
        return subprocess.run(
            ["qemu-aarch64-static", str(self.binary), "--host", *args],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_scalar_neon_checksums(self):
        for workers in (1, 4, 8, 16):
            for kernel in ("scalar", "neon"):
                with self.subTest(workers=workers, kernel=kernel):
                    run = self.run_guest(
                        "--workers",
                        str(workers),
                        "--kernel",
                        kernel,
                        "--array-mib",
                        "1",
                        "--passes",
                        "1",
                        "--initialize-before-switch",
                    )
                    self.assertEqual(run.returncode, 0, run.stderr)
                    result = json.loads(
                        next(
                            line[len("CMN_RESULT ") :]
                            for line in run.stdout.splitlines()
                            if line.startswith("CMN_RESULT ")
                        )
                    )
                    self.assertEqual(result["checksum"], 131072)
                    self.assertEqual(result["warmup_checksum"], 131072)
                    self.assertEqual(result["result"], "PASS")
                    self.assertEqual(result["working_set_bytes"], 1048576)
                    self.assertEqual(result["warmup_passes"], 1)

    def test_default_scalar_and_passes(self):
        run = self.run_guest("--workers", "1", "--array-mib", "1")
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(
            next(
                line[len("CMN_RESULT ") :]
                for line in run.stdout.splitlines()
                if line.startswith("CMN_RESULT ")
            )
        )
        self.assertEqual(result["kernel"], "scalar")
        self.assertEqual(result["passes"], 2)
        self.assertEqual(result["checksum"], 262144)
        self.assertEqual(result["warmup_checksum"], 131072)
        self.assertFalse(result["initialization_before_switch"])
        self.assertEqual(result["first_touch"], "pinned-worker")
        self.assertFalse(result["channel_phase_spread"])
        self.assertEqual(result["allocated_bytes"], 1048576)

    def test_phase_spread_preserves_payload(self):
        for workers in (4, 16):
            for kernel in ("scalar", "neon"):
                for atomic in (False, True):
                    with self.subTest(
                        workers=workers, kernel=kernel, atomic=atomic
                    ):
                        args = [
                            "--workers",
                            str(workers),
                            "--array-mib",
                            "1",
                            "--passes",
                            "2",
                            "--kernel",
                            kernel,
                            "--channel-phase-spread",
                        ]
                        if atomic:
                            args.append("--initialize-before-switch")
                        run = self.run_guest(*args)
                        self.assertEqual(run.returncode, 0, run.stderr)
                        result = json.loads(
                            next(
                                line[11:]
                                for line in run.stdout.splitlines()
                                if line.startswith("CMN_RESULT ")
                            )
                        )
                        self.assertEqual(result["checksum"], 262144)
                        self.assertEqual(result["warmup_checksum"], 131072)
                        self.assertEqual(result["useful_bytes"], 2097152)
                        self.assertEqual(
                            result["allocated_bytes"], 1048576 + 64 * workers
                        )
                        starts = [
                            int(v, 16)
                            for v in re.findall(
                                r"begin=(0x[0-9a-f]+)", run.stdout
                            )
                        ]
                        self.assertEqual(len(starts), workers)
                        for a, b in zip(starts, starts[1:]):
                            self.assertEqual(b - a, 1048576 // workers + 64)
                            self.assertEqual(
                                (b >> 6) % 16, ((a >> 6) + 1) % 16
                            )

    def test_reject_unsupported(self):
        for args in (
            ("--mode", "copy", "--kernel", "neon"),
            ("--array-mib", "513"),
            ("--kernel", "unknown"),
            ("--mode", "copy", "--channel-phase-spread"),
        ):
            with self.subTest(args=args):
                self.assertEqual(self.run_guest(*args).returncode, 2)

    def test_sixteen_worker_writes(self):
        for mode, arrays in (("copy", 2), ("stream", 3)):
            for atomic in (False, True):
                with self.subTest(mode=mode, atomic=atomic):
                    args = [
                        "--workers",
                        "16",
                        "--array-mib",
                        "1",
                        "--passes",
                        "2",
                        "--mode",
                        mode,
                    ]
                    if atomic:
                        args.append("--initialize-before-switch")
                    run = self.run_guest(*args)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    result = json.loads(
                        next(
                            line[11:]
                            for line in run.stdout.splitlines()
                            if line.startswith("CMN_RESULT ")
                        )
                    )
                    self.assertEqual(result["result"], "PASS")
                    self.assertEqual(result["workers"], 16)
                    self.assertEqual(result["useful_bytes"], 2097152 * arrays)

    def test_twelve_host_workers_cover_uneven_partitions(self):
        for mode, kernel, arrays in (
            ("read", "scalar", 1),
            ("read", "neon", 1),
            ("copy", "scalar", 2),
            ("stream", "scalar", 3),
        ):
            for atomic in (False, True):
                with self.subTest(mode=mode, kernel=kernel, atomic=atomic):
                    args = [
                        "--workers",
                        "12",
                        "--array-mib",
                        "1",
                        "--passes",
                        "2",
                        "--mode",
                        mode,
                        "--kernel",
                        kernel,
                    ]
                    if atomic:
                        args.append("--initialize-before-switch")
                    run = self.run_guest(*args)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    result = json.loads(
                        next(
                            line[11:]
                            for line in run.stdout.splitlines()
                            if line.startswith("CMN_RESULT ")
                        )
                    )
                    self.assertEqual(result["result"], "PASS")
                    self.assertEqual(result["workers"], 12)
                    self.assertEqual(
                        result["working_set_bytes"], 1048576 * arrays
                    )
                    self.assertEqual(result["useful_bytes"], 2097152 * arrays)
                    self.assertEqual(
                        result["affinity"], "first-available-workers"
                    )
                    if mode == "read":
                        self.assertEqual(result["checksum"], 262144)
                        self.assertEqual(result["warmup_checksum"], 131072)
                    times = [
                        json.loads(line[13:])
                        for line in run.stdout.splitlines()
                        if line.startswith("CMN_HOST_ROI ")
                    ]
                    self.assertEqual(len(times), 1)
                    self.assertTrue(times[0]["host"])
                    self.assertEqual(times[0]["clock"], "CLOCK_MONOTONIC_RAW")
                    self.assertGreater(times[0]["elapsed_ns"], 0)
        run = subprocess.run(
            ["qemu-aarch64-static", str(self.binary), "--workers", "12"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(run.returncode, 2)

    def test_host_timer_ignores_nonzero_markers(self):
        source = Path(self.directory.name) / "markers.c"
        source.write_text(
            """#define _GNU_SOURCE
#include "common.h"
int main(void) {
    (void)number; (void)pin; (void)sync_workers; (void)spawn;
    host = 1;
    marker_begin(UINT64_C(0x5a535743));
    marker_end();
    marker_begin(101);
    marker_end();
    marker_begin(0);
    usleep(1000);
    marker_end();
    marker_end();
    return 0;
}
"""
        )
        binary = source.with_suffix("")
        subprocess.run(
            [
                "aarch64-linux-gnu-gcc",
                "-std=c11",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-static",
                "-pthread",
                "-I",
                str(SOURCE.parent),
                str(source),
                "-o",
                str(binary),
            ],
            check=True,
        )
        run = subprocess.run(
            ["qemu-aarch64-static", str(binary)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        lines = run.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("CMN_HOST_ROI "))
        result = json.loads(lines[0][13:])
        self.assertGreaterEqual(result["elapsed_ns"], 1000000)


if __name__ == "__main__":
    unittest.main()
