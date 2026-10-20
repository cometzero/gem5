# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "scripts/zena_cmn")
)
from check_memory_convergence import (
    CASES,
    WSS,
    compare,
    validate_exit,
    validate_traffic,
)


class ConvergenceTests(unittest.TestCase):
    def rows(self):
        return [
            dict(
                name=name,
                passes=p,
                phase=phase,
                identity={"hash": "abc"},
                useful_GB_s=20,
                dram_to_useful_ratio=1.1,
            )
            for name, (p, phase) in CASES.items()
        ]

    def test_pass_and_nonconvergence_are_separate(self):
        rows = self.rows()
        self.assertEqual(compare(rows)["convergence_status"], "PASS")
        rows[2]["useful_GB_s"] = 22
        result = compare(rows)
        self.assertEqual(result["measurement_status"], "PASS")
        self.assertEqual(result["convergence_status"], "NOT_CONVERGED")

    def test_ratio_must_converge_too(self):
        rows = self.rows()
        rows[2]["dram_to_useful_ratio"] = 1.3
        self.assertEqual(compare(rows)["convergence_status"], "NOT_CONVERGED")

    def test_passes_mismatch(self):
        rows = self.rows()
        rows[1]["passes"] = 4
        with self.assertRaisesRegex(ValueError, "passes"):
            compare(rows)

    def test_duplicate_case(self):
        rows = self.rows()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            compare(rows + [rows[0]])

    def test_exit_status(self):
        good = dict(
            status="COMPLETED", code=0, cause="m5_exit instruction encountered"
        )
        validate_exit(good)
        validate_exit(
            dict(good, cause="exiting with last active thread context")
        )
        for field, value in (
            ("status", "INCOMPLETE"),
            ("code", 1),
            ("cause", "simulate() limit reached"),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_exit(dict(good, **{field: value}))

    def test_hash_mismatch(self):
        rows = self.rows()
        rows[2]["identity"] = {"hash": "different"}
        with self.assertRaisesRegex(ValueError, "hash"):
            compare(rows)

    def test_traffic_validation(self):
        summary = {
            "guest": dict(
                workers=4,
                passes=1,
                working_set_bytes=WSS,
                useful_bytes=WSS,
                warmup_passes=1,
                checksum=WSS // 8,
                kernel="neon",
                benchmark="read",
                initialization_before_switch=True,
                channel_phase_spread=False,
                allocated_bytes=WSS,
            ),
            "dram": {
                "channels": {
                    f"system.mem_ctrls{i}.dram": {"Read": 1024, "Written": 0}
                    for i in range(16)
                }
            },
            "application": {"useful_bytes": WSS},
        }
        validate_traffic(summary, 1, False)
        bad = copy.deepcopy(summary)
        del bad["dram"]["channels"]["system.mem_ctrls15.dram"]
        with self.assertRaisesRegex(ValueError, "channel"):
            validate_traffic(bad, 1, False)
        summary["guest"]["checksum"] = 0
        with self.assertRaisesRegex(ValueError, "checksum"):
            validate_traffic(summary, 1, False)


if __name__ == "__main__":
    unittest.main()
