# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Negative gates for explicit finite-supply native drain accounting."""
import unittest

from check_backend_traffic import check_drain


class DrainAccountingTest(unittest.TestCase):
    def setUp(self):
        self.manifest = {
            "channels": 4,
            "roi_ticks": 20,
            "tick_frequency": 10**12,
            "drain_completion": {
                "start_tick": 30,
                "end_tick": 32,
                "warmup_dump_index": 0,
                "roi_dump_index": 1,
                "completed_dump_index": 2,
                "native_drain_returned": True,
                "scope": "native drain",
            },
        }
        self.blocks = []
        # A response from warm-up crosses reset: lifetime accounting matters.
        for ticks, packets, reads, writes in (
            (10, 3, 1, 1),
            (20, 4, 2, 2),
            (22, 4, 2, 3),
        ):
            b = {
                "simTicks": ticks,
                "simFreq": 10**12,
                "finalTick": ticks if not self.blocks else ticks + 10,
            }
            for i in range(4):
                prefix = f"system.generators{i}."
                for key, value in {
                    "numPackets": packets,
                    "totalReads": reads,
                    "totalWrites": writes,
                    "numSuppressed": 0,
                    "bytesRead": reads * 64,
                    "bytesWritten": writes * 64,
                }.items():
                    b[prefix + key] = value
                for key, value in (
                    ("readReqs", reads),
                    ("writeReqs", writes),
                    ("bytesReadSys", reads * 64),
                    ("bytesWrittenSys", writes * 64),
                ):
                    b[f"system.mem_ctrls{i}.{key}"] = value
                for mode, value in (("Read", reads), ("Written", writes)):
                    b[f"system.mem_ctrls{i}.dram.dramBytes{mode}"] = value * 64
            self.blocks.append(b)

    def test_cross_reset_completed(self):
        result = check_drain(self.manifest, self.blocks)
        self.assertEqual(result["lifetime_generated_packets"], 28)
        self.assertEqual(result["additional_dram_burst_bytes"]["Written"], 256)

    def test_wrong_absolute_tick_rejected(self):
        for index in range(3):
            with self.subTest(dump=index):
                self.blocks[index]["finalTick"] += 1
                with self.assertRaises(AssertionError):
                    check_drain(self.manifest, self.blocks)
                self.blocks[index]["finalTick"] -= 1

    def test_wrong_frequency_rejected(self):
        for index in range(3):
            with self.subTest(dump=index):
                self.blocks[index]["simFreq"] = 10**9
                with self.assertRaises(AssertionError):
                    check_drain(self.manifest, self.blocks)
                self.blocks[index]["simFreq"] = 10**12

    def test_pending_response_rejected(self):
        self.blocks[2]["system.generators0.totalWrites"] -= 1
        with self.assertRaises(AssertionError):
            check_drain(self.manifest, self.blocks)

    def test_generation_during_drain_rejected(self):
        self.blocks[1]["system.generators0.numPackets"] -= 1
        with self.assertRaises(AssertionError):
            check_drain(self.manifest, self.blocks)

    def test_controller_acceptance_mismatch(self):
        self.blocks[2]["system.mem_ctrls0.writeReqs"] += 1
        with self.assertRaises(AssertionError):
            check_drain(self.manifest, self.blocks)

    def test_missing_controller_rejected(self):
        del self.blocks[2]["system.mem_ctrls3.dram.dramBytesWritten"]
        with self.assertRaises(KeyError):
            check_drain(self.manifest, self.blocks)

    def test_false_drain_rejected(self):
        self.manifest["drain_completion"]["native_drain_returned"] = False
        with self.assertRaises(AssertionError):
            check_drain(self.manifest, self.blocks)

    def test_wrong_dump_rejected(self):
        self.manifest["drain_completion"]["completed_dump_index"] = 1
        with self.assertRaises(AssertionError):
            check_drain(self.manifest, self.blocks)


if __name__ == "__main__":
    unittest.main()
