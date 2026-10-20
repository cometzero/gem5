# SPDX-License-Identifier: BSD-3-Clause
"""The native campaign keeps the required contrasts and finite resources."""

import copy
import importlib.util
import unittest
from pathlib import Path

PATH = (
    Path(__file__).resolve().parents[2] / "scripts/zena_cmn/run_saturation.py"
)
SPEC = importlib.util.spec_from_file_location("run_saturation", PATH)
CAMPAIGN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CAMPAIGN)
SUMMARY_SPEC = importlib.util.spec_from_file_location(
    "summarize_saturation", PATH.with_name("summarize_saturation.py")
)
SUMMARY = importlib.util.module_from_spec(SUMMARY_SPEC)
SUMMARY_SPEC.loader.exec_module(SUMMARY)


class SaturationCampaignTest(unittest.TestCase):
    def test_distinct_outputs_and_read_write_mix_coverage(self):
        cases = list(CAMPAIGN.cases("all", 4))
        self.assertEqual(len(cases), len({case[0] for case in cases}))
        for read in (0, 50, 100):
            for path in ("direct", "fanout", "single"):
                self.assertTrue(
                    any(
                        case[0].startswith(f"{path}-r{read}-")
                        for case in cases
                    )
                )

    def test_device_stages_and_smmu_control(self):
        cases = list(CAMPAIGN.cases("npu", 16))
        for mode in ("read", "write", "copy"):
            for stage in ("baseline", "dma64", "dma128", "dma256", "dma512"):
                name = f"npu-{stage}-{mode}-16m"
                case = next(case for case in cases if case[0] == name)
                args = dict(zip(case[2][::2], case[2][1::2]))
                self.assertEqual(args["--mib"], "16")
                self.assertEqual(
                    args["--smmu-resources"],
                    "baseline" if stage == "baseline" else "scaled",
                )
            self.assertIn(
                f"npu-dma256-{mode}-16m-default-smmu",
                {case[0] for case in cases},
            )
            maximum = next(
                case
                for case in cases
                if case[0] == f"npu-dma512-{mode}-16m-slots1024"
            )
            options = dict(zip(maximum[2][::2], maximum[2][1::2]))
            self.assertEqual(options["--smmu-device-slots"], "1024")
            self.assertEqual(options["--smmu-resources"], "scaled")

    def test_device_and_dram_completion_windows_are_distinct(self):
        record = {
            "case": "fixture",
            "status": "PASS",
            "host_seconds": 1,
            "result": {
                "status": "PASS",
                "polled_roi_ticks": 200000,
                "drain_extra_ticks": 20000,
                "job_ticks": 100000,
                "payload_read_bytes": 1000,
                "payload_write_bytes": 0,
                "device_payload_gbps": 10,
                "dram_drained_read_bytes": 1100,
                "dram_drained_write_bytes": 0,
                "args": {
                    "mode": "read",
                    "stage": "dma256",
                    "mib": 4,
                    "smmu_mode": "translated",
                    "smmu_resources": "scaled",
                },
                "settings": {
                    "ifc": {"xlate_slots": "128"},
                    "fabric_width": 256,
                    "dma_width": 256,
                    "max_outstanding": 512,
                },
                "npu": {"dmaRetries": 0, "outstandingHighWater": 128},
                "smmu_stats": {},
                "verification": "fixture",
            },
        }
        result = SUMMARY.row(record)
        self.assertEqual(result["device_GBps"], 10)
        self.assertEqual(result["dram_GBps"], 5)
        for changes in (
            {"job_ticks": 0},
            {"device_payload_gbps": 11},
            {"status": "FAIL"},
        ):
            invalid = copy.deepcopy(record)
            invalid["result"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                SUMMARY.row(invalid)


if __name__ == "__main__":
    unittest.main()
