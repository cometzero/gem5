# SPDX-License-Identifier: BSD-3-Clause
"""The native campaign keeps the required contrasts and finite resources."""

import contextlib
import copy
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
    def test_memory_selection_preserves_default_and_routing(self):
        legacy, legacy_id = CAMPAIGN.backend_parameters()
        self.assertIn("Hynix", legacy["memory"]["custom_class"])
        self.assertTrue(legacy["memory"]["preserve_device_geometry"])
        self.assertEqual(legacy_id["selection"], "legacy_full_profile")
        profile = (
            CAMPAIGN.ROOT / "configs/zena_cmn/profiles/a720ae_documented.yaml"
        )
        for route in ("direct", "fanout", "single"):
            with self.subTest(route=route):
                params, identity = CAMPAIGN.backend_parameters(
                    profile, route, 128
                )
                self.assertIsNone(params["memory"]["custom_module"])
                self.assertFalse(params["memory"]["preserve_device_geometry"])
                self.assertEqual(params["npu"]["enabled"], route != "direct")
                self.assertEqual(
                    params["scope"]["npu_present"], route != "direct"
                )
                self.assertEqual(
                    params["memory"]["create_future_npu_arbiter_now"],
                    route != "direct",
                )
                if route != "direct":
                    self.assertEqual(params["npu"]["fabric_width"], 128)
                self.assertEqual(identity["selection"], "memory_only")
                self.assertEqual(identity["sha256"], CAMPAIGN.digest(profile))

    def test_selected_nonmemory_fields_are_not_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile.yaml"
            profile.write_text(
                "cpu:\n  clock: 1GHz\nmemory:\n  preserve_device_geometry: false\n"
            )
            params, _ = CAMPAIGN.backend_parameters(profile)
            legacy, _ = CAMPAIGN.backend_parameters()
            self.assertEqual(params["cpu"], legacy["cpu"])
            self.assertEqual(params["cmn"], legacy["cmn"])
            self.assertFalse(params["memory"]["preserve_device_geometry"])

    def test_generator_geometry_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile.yaml"
            for key, value in (
                ("channels", 8),
                ("interleave_bytes", 512),
                ("capacity_mib_total", 1024),
            ):
                profile.write_text(f"memory:\n  {key}: {value}\n")
                with self.subTest(key=key), self.assertRaises(ValueError):
                    CAMPAIGN.backend_parameters(profile)

    def test_memory_profile_is_backend_only_before_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "campaign"
            for suite in ("all", "npu"):
                with (
                    self.subTest(suite=suite),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    with self.assertRaises(SystemExit) as error:
                        CAMPAIGN.main(
                            [
                                "--suite",
                                suite,
                                "--memory-profile",
                                "unused.yaml",
                                "--output",
                                str(output),
                            ]
                        )
                    self.assertEqual(error.exception.code, 2)
                    self.assertFalse(output.exists())

    def test_campaign_propagates_profile_and_binds_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "gem5.fixture"
            binary.write_text("test fixture; never executed")
            profile = (
                CAMPAIGN.ROOT
                / "configs/zena_cmn/profiles/a720ae_documented.yaml"
            )
            output = root / "campaign"
            _, identity = CAMPAIGN.backend_parameters(profile)
            commands = []

            def native_fixture(command, **kwargs):
                commands.append(command)
                out = Path(command[1].split("=", 1)[1])
                (out / "saturation-result.json").write_text(
                    json.dumps(
                        {
                            "status": "PASS",
                            "dram_fingerprint": "fixture-only",
                            "memory_profile": identity,
                        }
                    )
                )
                return subprocess.CompletedProcess(command, 0)

            arguments = [
                "--gem5",
                str(binary),
                "--output",
                str(output),
                "--suite",
                "backend",
                "--case",
                "direct-r100-w32-us20",
                "--memory-profile",
                str(profile),
            ]
            with (
                patch.object(
                    CAMPAIGN.subprocess, "run", side_effect=native_fixture
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                CAMPAIGN.main(arguments)
            self.assertEqual(
                commands[0][-2:], ["--memory-profile", str(profile)]
            )
            result = json.loads((output / "campaign.json").read_text())
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["memory_profile"], identity)
            self.assertEqual(result["dram_fingerprint"], "fixture-only")
            self.assertIn(
                str(CAMPAIGN.ROOT / "configs/zena_cmn/memory_backend.py"),
                result["source_sha256"],
            )
            with self.assertRaises(FileExistsError):
                CAMPAIGN.main(arguments)

    def test_campaign_rejects_result_using_another_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "gem5.fixture"
            binary.write_text("test fixture; never executed")

            def wrong_profile(command, **kwargs):
                out = Path(command[1].split("=", 1)[1])
                (out / "saturation-result.json").write_text(
                    json.dumps(
                        {
                            "status": "PASS",
                            "dram_fingerprint": "fixture-only",
                            "memory_profile": {},
                        }
                    )
                )
                return subprocess.CompletedProcess(command, 0)

            output = root / "campaign"
            with (
                patch.object(
                    CAMPAIGN.subprocess, "run", side_effect=wrong_profile
                ),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                CAMPAIGN.main(
                    [
                        "--gem5",
                        str(binary),
                        "--output",
                        str(output),
                        "--suite",
                        "backend",
                        "--case",
                        "direct-r100-w32-us20",
                        "--memory-profile",
                        str(CAMPAIGN.LEGACY_PROFILE),
                    ]
                )
            result = json.loads((output / "campaign.json").read_text())
            self.assertEqual(result["runs"][0]["status"], "FAIL")
            self.assertEqual(
                result["runs"][0]["reason"], "native memory profile mismatch"
            )

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
