# SPDX-License-Identifier: BSD-3-Clause
import gzip
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = (
    Path(__file__).resolve().parents[2] / "scripts/zena_cmn/summarize_roi.py"
)
SPEC = importlib.util.spec_from_file_location("cmn_roi", SOURCE)
ROI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROI)


def block(extra="", ticks=2000, end=3000):
    return f"""---------- Begin Simulation Statistics ----------
simFreq 1000000
simTicks {ticks}
finalTick {end}
{extra}
---------- End Simulation Statistics ----------
"""


GUEST = 'CMN_RESULT {"result":"PASS","useful_bytes":4000}\n'
COUNTERS = """system.mc0.dram.dramBytesRead 12000
system.mc0.dram.dramBytesWritten 4000
system.mc1.dram.dramBytesRead 8000
system.mc1.dram.dramBytesWritten 0
"""


class RoiTests(unittest.TestCase):
    def test_prefault_rejects_timing_and_memory_bench(self):
        script = SOURCE.with_name("run_smoke.py")
        required = [
            "--gem5",
            "unused",
            "--kernel",
            "unused",
            "--bootloader",
            "unused",
            "--initrd",
            "unused",
            "--output",
            "unused",
            "--prefault-before-switch",
        ]
        for conflict, message in (
            (["--direct-timing"], "Atomic boot"),
            (["--mode", "read"], "only for coherence"),
        ):
            result = subprocess.run(
                [sys.executable, str(script)] + required + conflict,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn(message, result.stderr)

    def test_npu_delay_validation(self):
        required = [
            item
            for name in ("gem5", "kernel", "bootloader", "initrd", "output")
            for item in ("--" + name, "unused")
        ]
        for extra in (
            ["--npu-cpu-delay-us", "-1"],
            ["--npu-cpu-delay-us", "100001"],
            [
                "--npu-cpu-delay-us",
                "1",
                "--mode",
                "npu-bench",
                "--npu-workload",
                "npu",
            ],
        ):
            result = subprocess.run(
                [
                    sys.executable,
                    str(SOURCE.with_name("run_smoke.py")),
                    *required,
                    *extra,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("npu-cpu-delay-us", result.stderr)

    def test_npu_job_limits_before_launch(self):
        required = [
            item
            for name in ("gem5", "kernel", "bootloader", "initrd", "output")
            for item in ("--" + name, "unused")
        ]
        for extra, message in (
            (["--npu-jobs", "0"], "npu-jobs"),
            (["--npu-jobs", "33"], "npu-jobs"),
            (["--npu-jobs", "2"], "npu-jobs"),
            (
                ["--mode", "npu-bench", "--npu-mib", "16", "--passes", "4"],
                "64 MiB per job",
            ),
        ):
            result = subprocess.run(
                [
                    sys.executable,
                    str(SOURCE.with_name("run_smoke.py")),
                    *required,
                    *extra,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn(message, result.stderr)

    def test_disposable_initramfs(self):
        sys.path.insert(0, str(SOURCE.parent))
        import run_smoke

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            base = directory / "base.gz"
            base.write_bytes(gzip.compress(b"base archive"))
            binary = directory / "probe"
            binary.write_bytes(b"ELF test payload")
            image = run_smoke.prepare(
                base, binary, ["/probe", "a b"], directory
            )
            self.assertTrue(
                image.read_bytes().startswith(b"base archive070701")
            )
            self.assertIn(b"cmn-init", image.read_bytes())
            self.assertIn(b"ELF test payload", image.read_bytes())
            self.assertIn(
                "/probe 'a b'", (directory / "guest-init.sh").read_text()
            )
            self.assertEqual(
                gzip.decompress(base.read_bytes()), b"base archive"
            )
            run_smoke.prepare(base, binary, ["/probe"], directory, disk=True)
            script = (directory / "guest-init.sh").read_text()
            self.assertIn("CMN_DISK=PASS", script)
            self.assertLess(
                script.index("/probe\n"), script.index("mount /dev/vda")
            )

    def test_ambiguous_native_tbe_statistics(self):
        result = ROI.summarize(
            block("controller.avg_size 1\ncontroller.avg_size 2"),
            GUEST,
            1000,
            3000,
        )
        self.assertEqual(result["ambiguous_counters"], ["controller.avg_size"])
        self.assertIsNone(
            ROI.stats_blocks(
                block("controller.avg_size 1\ncontroller.avg_size 2")
            )[0]["controller.avg_size"]
        )

    def test_native_oneline_histogram(self):
        line = "cpu.l1d.outTransLatHist.SendReadShared | 10 100.00% 100.00%"
        value = ROI.stats_blocks(block(line))[0][line.split()[0]]
        self.assertEqual(value["format"], "native_oneline_histogram")
        self.assertIn("10 100.00%", value["raw"])

    def test_native_oneline_vector_fills_field_width(self):
        name = "system.ruby.network.flit_network_latency"
        for padding in ("", " "):
            raw = f"|{padding}102367065000 | 5910473500 (Unspecified)"
            with self.subTest(padding=padding):
                line = f"{name} {raw}"
                value = ROI.stats_blocks(block(line))[0][name]
                self.assertEqual(value["format"], "native_oneline_histogram")
                self.assertEqual(value["raw"], raw)
                with self.assertRaisesRegex(ValueError, "duplicate"):
                    ROI.stats_blocks(block(line + "\n" + line))

    def test_event_bounds(self):
        self.assertEqual(
            ROI.event_bounds(
                [
                    {"phase": "warmup", "tick": 10},
                    {"phase": "roi_begin", "tick": 1000},
                    {"phase": "roi_end", "tick": 3000},
                ]
            ),
            (1000, 3000),
        )
        with self.assertRaises(ValueError):
            ROI.event_bounds([{"phase": "roi_begin", "tick": 1}] * 2)

    def test_units_and_duplicate(self):
        stats = block(ticks=1000, end=1000) + 2 * block(COUNTERS)
        result = ROI.summarize(stats, GUEST, 1000, 3000)
        self.assertEqual(result["roi"]["seconds"], 0.002)
        self.assertEqual(result["application"]["GB_s"], 0.002)
        self.assertEqual(result["dram"]["GB_s"], 0.012)
        self.assertEqual(result["roi"]["duplicate_dumps_ignored"], 1)
        self.assertEqual(result["roi"]["stats_block_index"], 1)

    def test_absent_is_not_zero(self):
        result = ROI.summarize(block(), GUEST, 1000, 3000)
        self.assertIsNone(result["dram"]["GB_s"])
        self.assertIsNone(result["counters"]["tbe"]["value"])
        self.assertEqual(
            result["counters"]["tbe"]["availability"], "unavailable"
        )
        json.dumps(result, allow_nan=False)

    def test_missing_channel_counter(self):
        result = ROI.summarize(block("mc.dramBytesRead 12"), GUEST, 1000, 3000)
        self.assertIsNone(result["dram"]["write_bytes"])
        self.assertIsNone(result["dram"]["GB_s"])

    def test_conflicting_duplicate(self):
        with self.assertRaisesRegex(ValueError, "conflicting"):
            ROI.summarize(block(COUNTERS) + block(), GUEST, 1000, 3000)

    def test_wrong_time_or_guest(self):
        for begin, end, guest in [
            (1000, 1000, GUEST),
            (0, 3000, GUEST),
            (1000, 3000, GUEST * 2),
            (1000, 3000, GUEST.replace("PASS", "FAIL")),
        ]:
            with self.assertRaises(ValueError):
                ROI.summarize(block(), guest, begin, end)

    def test_truncated_or_duplicate_stat(self):
        with self.assertRaisesRegex(ValueError, "truncated"):
            ROI.stats_blocks(block().split("---------- End")[0])
        with self.assertRaisesRegex(ValueError, "duplicate statistic"):
            ROI.stats_blocks(block("simTicks 2000"))


if __name__ == "__main__":
    unittest.main()
