# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
import sys
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "scripts/zena_cmn")
)
from analyze_fabric import (
    framing,
    latency,
)


def trace():
    events = [
        (0, 1, "Seq", "Begin"),
        (2, 1, "Cache", "SendReadShared"),
        (8, 8, "Cache", "AllocRequest"),
        (10, 8, "Cache", "DataArrayRead"),
        (16, 8, "Cache", "SendCompData"),
        (24, 1, "Cache", "CompData_SC"),
        (25, 1, "Cache", "CompData_SC"),
        (25, 1, "Seq", "Done"),
    ]
    return "\n".join(
        f"{t} {c} {k} {e} I>I [0x100, line 0x100]" for t, c, k, e in events
    )


class FabricTests(unittest.TestCase):
    def test_latency(self):
        result = latency(trace(), 0x100, 1, 8, 1)
        self.assertEqual(result["cycles"]["request_delivery"], 6)
        self.assertEqual(result["cycles"]["total"], 25)

    def test_overlap_rejected(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            latency(
                trace() + "\n0 1 Seq Begin I>I [0x100, line 0x100]",
                0x100,
                1,
                8,
                1,
            )

    def test_reused_address_requires_explicit_window(self):
        reused = trace() + "\n100 1 Seq Begin I>I [0x100, line 0x100]"
        with self.assertRaises(ValueError):
            latency(reused, 0x100, 1, 8, 1)
        self.assertEqual(
            latency(reused, 0x100, 1, 8, 1, 0, 30)["cycles"]["total"], 25
        )

    def test_missing_response_rejected(self):
        with self.assertRaises(ValueError):
            latency(trace().replace("CompData_SC", "Other", 1), 0x100, 1, 8, 1)

    def test_l2_miss_rejected(self):
        with self.assertRaises(ValueError):
            latency(
                trace() + "\n11 8 Cache SendReadShared I>I "
                "[0x100, line 0x100]",
                0x100,
                1,
                8,
                1,
            )

    def test_framing_and_negative(self):
        expected = dict(control_flits_per_packet=1, data_flits_per_packet=2)
        stats = (
            "system.ruby.network.packets_injected | 1 | 2 | 3 | 4\n"
            "system.ruby.network.flits_injected | 1 | 2 | 3 | 8\n"
        )
        self.assertEqual(framing(stats, expected)["status"], "PASS")
        with self.assertRaises(ValueError):
            framing(stats.replace("| 8", "| 7"), expected)
        with self.assertRaises(ValueError):
            framing("", expected)


if __name__ == "__main__":
    unittest.main()
