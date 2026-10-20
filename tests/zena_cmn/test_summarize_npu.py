# SPDX-License-Identifier: BSD-3-Clause
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "scripts/zena_cmn")
)
from summarize_npu import (
    payload_phase_overlap,
    summarize_npu,
    summarize_npu_bench,
)


class NpuSummaryTest(unittest.TestCase):
    console = (
        "NPU v0.1 GEMM PASS:\n" * 3
        + "NPU v0.1 reused BO visibility PASS: 3 submissions"
    )
    stats = """---------- Begin Simulation Statistics ----------
finalTick 100
simTicks 90
simFreq 1000000000000
system.platform.npu.jobs 3
system.platform.npu.macs 1536
system.platform.npu.dmaReadBytes 384
system.platform.npu.dmaWriteBytes 768
system.mem_ctrls0.dram.dramBytesRead 4096
system.mem_ctrls0.dram.dramBytesWritten 1024
---------- End Simulation Statistics ----------
"""

    def test_exact_roi(self):
        result = summarize_npu(self.stats, self.console, 10, 100)
        self.assertEqual(result["dram_bytes"], {"read": 4096, "write": 1024})
        self.assertEqual(result["status"], "PASS")

    def test_payload_phase_is_not_submission_overlap(self):
        result = {
            "roi_begin_tick": 10000000,
            "roi_end_tick": 20000000,
            "roi_seconds": 0.01,
            "npu": {
                "firstPayloadAcceptedTick": 15000000,
                "lastPayloadResponseTick": 16000000,
            },
        }
        record = {
            "mode": "concurrent",
            "roi_clock_before_ns": 9999900,
            "roi_begin_ns": 10000100,
            "cpu_begin_ns": 11000000,
            "cpu_end_ns": 12000000,
            "cpu_workers": [{"begin_ns": 11000000, "end_ns": 12000000}],
        }
        self.assertEqual(
            payload_phase_overlap(result, record)["status"],
            "ESTIMATED_NO_PHASE_OVERLAP",
        )
        record["cpu_workers"][0]["end_ns"] = 15500000
        self.assertEqual(
            payload_phase_overlap(result, record)["status"],
            "ESTIMATED_PHASE_OVERLAP",
        )
        record["cpu_workers"] = [
            {"begin_ns": 11000000, "end_ns": 14000000},
            {"begin_ns": 17000000, "end_ns": 18000000},
        ]
        self.assertEqual(
            payload_phase_overlap(result, record)["status"],
            "ESTIMATED_NO_PHASE_OVERLAP",
        )
        record["roi_clock_before_ns"] = 20000000
        with self.assertRaisesRegex(ValueError, "clock bracket"):
            payload_phase_overlap(result, record)

    def test_reject_missing_controller(self):
        with self.assertRaisesRegex(ValueError, "DRAM burst counters"):
            summarize_npu(self.stats, self.console, 10, 100, channels=2)

    def test_repeated_jobs_and_payload_envelope(self):
        record = {
            "result": "PASS",
            "checksums": "PASS",
            "mode": "concurrent",
            "mib": 1,
            "passes": 1,
            "jobs": 2,
            "jobs_completed": 2,
            "cpu_bytes": 2097152,
            "npu_read_bytes": 2097152,
            "npu_write_bytes": 2097152,
            "cpu_begin_ns": 10,
            "cpu_end_ns": 100,
            "npu_begin_ns": 20,
            "npu_end_ns": 110,
            "overlap_ns": 80,
            "cpu_workers": [{"begin_ns": 10, "end_ns": 100}],
            "npu_job_windows": [
                {"begin_ns": 20, "end_ns": 50, "dma_bytes": 2097152},
                {"begin_ns": 60, "end_ns": 110, "dma_bytes": 2097152},
            ],
        }
        stats = (
            self.stats.replace("jobs 3", "jobs 2")
            .replace("macs 1536", "macs 0")
            .replace("dmaReadBytes 384", "dmaReadBytes 2097152")
            .replace("dmaWriteBytes 768", "dmaWriteBytes 2097152")
        )

        def run(data=record):
            return summarize_npu_bench(
                stats, "NPU_BENCH_JSON=" + json.dumps(data), 10, 100
            )

        self.assertEqual(run()["status"], "PASS")
        self.assertEqual(
            run()["submission_windows_overlap"]["nanoseconds"], 70
        )
        record["cpu_workers"] = [{"begin_ns": 51, "end_ns": 59}]
        with self.assertRaisesRegex(ValueError, "did not overlap CPU"):
            run()
        record["cpu_workers"] = [{"begin_ns": 10, "end_ns": 100}]
        self.assertEqual(
            run()["payload_phase_overlap"]["status"], "UNAVAILABLE"
        )
        for field, value in (("jobs", 0), ("jobs", 33), ("jobs_completed", 1)):
            with self.subTest(field=field, value=value), self.assertRaises(
                ValueError
            ):
                run(dict(record, **{field: value}))
        record["npu_job_windows"][1]["begin_ns"] = 40
        with self.assertRaisesRegex(ValueError, "per-job"):
            run()
        record["npu_job_windows"][1]["begin_ns"] = 60
        record["npu_job_windows"][1]["dma_bytes"] -= 1
        with self.assertRaisesRegex(ValueError, "per-job"):
            run()

    def test_benchmark_accounting_and_overlap(self):
        record = {
            "result": "PASS",
            "checksums": "PASS",
            "mode": "concurrent",
            "mib": 1,
            "passes": 1,
            "cpu_bytes": 1048576,
            "npu_read_bytes": 1048576,
            "npu_write_bytes": 1048576,
            "cpu_begin_ns": 10,
            "cpu_end_ns": 100,
            "npu_begin_ns": 20,
            "npu_end_ns": 110,
            "overlap_ns": 80,
        }
        stats = (
            self.stats.replace("jobs 3", "jobs 1")
            .replace("macs 1536", "macs 0")
            .replace("dmaReadBytes 384", "dmaReadBytes 1048576")
            .replace("dmaWriteBytes 768", "dmaWriteBytes 1048576")
        )

        def run():
            return summarize_npu_bench(
                stats, "NPU_BENCH_JSON=" + json.dumps(record), 10, 100
            )

        self.assertEqual(run()["status"], "PASS")
        record["overlap_ns"] = 0
        with self.assertRaisesRegex(ValueError, "did not overlap"):
            run()
        record["overlap_ns"] = 80
        record["npu_read_bytes"] -= 1
        with self.assertRaisesRegex(ValueError, "accounting"):
            run()

    def test_reject_missing_or_bad_evidence(self):
        for stats, console, begin in (
            (self.stats, self.console, 11),
            (
                self.stats + self.stats.replace("macs 1536", "macs 512"),
                self.console,
                10,
            ),
            (
                self.stats.replace("simFreq 1000000000000", "simFreq 0"),
                self.console,
                10,
            ),
            (self.stats.replace("macs 1536", "macs 512"), self.console, 10),
            (self.stats, "", 10),
            (self.stats.replace("dramBytesRead", "unknown"), self.console, 10),
        ):
            with self.subTest(begin=begin), self.assertRaises(ValueError):
                summarize_npu(stats, console, begin, 100)


if __name__ == "__main__":
    unittest.main()
