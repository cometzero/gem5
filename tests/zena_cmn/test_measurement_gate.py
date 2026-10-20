# SPDX-License-Identifier: BSD-3-Clause
"""Execution success alone does not establish a complete measurement."""
import sys
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "scripts/zena_cmn")
)
from run_roofline import validate_measurements

EVENTS = [
    {"phase": "roi_begin", "case_id": 1, "tick": 100},
    {"phase": "roi_end", "case_id": 1, "tick": 200},
]
STATS = """---------- Begin Simulation Statistics ----------
finalTick 200
simTicks 100
simFreq 1000000000000
---------- End Simulation Statistics ----------
"""


class MeasurementGateTest(unittest.TestCase):
    def test_exact_roi(self):
        validate_measurements(STATS, EVENTS)

    def test_missing_mismatched_or_ambiguous_stats(self):
        for stats in (
            "",
            STATS.replace("finalTick 200", "finalTick 201"),
            STATS.replace("simTicks 100", "simTicks 99"),
            STATS * 2,
            STATS.replace("simFreq 1000000000000", "simFreq 0"),
        ):
            with self.subTest(stats=stats):
                with self.assertRaises(ValueError):
                    validate_measurements(stats, EVENTS)

    def test_incomplete_interval(self):
        with self.assertRaises(ValueError):
            validate_measurements(STATS, EVENTS[:1])

    def test_reversed_interval(self):
        with self.assertRaises(ValueError):
            validate_measurements(STATS, EVENTS[::-1])

    def test_overlapping_different_cases(self):
        events = [
            EVENTS[0],
            {"phase": "roi_begin", "case_id": 2, "tick": 150},
            EVENTS[1],
            {"phase": "roi_end", "case_id": 2, "tick": 250},
        ]
        stats = STATS + STATS.replace("finalTick 200", "finalTick 250")
        with self.assertRaisesRegex(ValueError, "overlapping"):
            validate_measurements(stats, events)
