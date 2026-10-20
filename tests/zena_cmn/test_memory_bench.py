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


if __name__ == "__main__":
    unittest.main()
