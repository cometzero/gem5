# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "configs"))
from zena_cmn.parameters import (
    load_parameters,
    validate,
)


class ParametersTest(unittest.TestCase):
    def test_l2_read_hit_pipeline_cycles(self):
        self.assertEqual(
            load_parameters()["cache"]["l2"]["read_hit_pipeline_cycles"], 0
        )
        for value in (0, 1, 4):
            p = load_parameters(
                overrides={"cache.l2.read_hit_pipeline_cycles": value}
            )
            self.assertEqual(
                p["cache"]["l2"]["read_hit_pipeline_cycles"], value
            )
        for value in (-1, True, 1.5, "1", 2**31):
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_parameters(
                    overrides={"cache.l2.read_hit_pipeline_cycles": value}
                )
        with self.assertRaises(ValueError):
            load_parameters(overrides={"cache.l2.data_latency_cycles": 0})

    def test_npu_profile(self):
        profile = (
            Path(__file__).resolve().parents[2]
            / "configs/zena_cmn/profiles/a720ae_npu_v01.yaml"
        )
        p = load_parameters(profile)
        self.assertTrue(p["npu"]["enabled"])
        self.assertTrue(p["memory"]["create_future_npu_arbiter_now"])
        for change in (
            {"npu.enabled": False},
            {"npu.sram_size": 1024},
            {"npu.dma_width": 3},
            {"npu.max_outstanding": 257},
            {"npu.clock": "bad"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                load_parameters(profile, overrides=change)
        self.assertFalse(load_parameters()["npu"]["enabled"])
        async_profile = profile.with_name("a720ae_npu_v01_async.yaml")
        self.assertTrue(
            load_parameters(async_profile)["npu"]["enable_async_dma"]
        )
        with self.assertRaises(ValueError):
            load_parameters(overrides={"npu.enable_async_dma": True})

    def test_npu_scaling_is_explicit_and_bounded(self):
        profile = (
            Path(__file__).resolve().parents[2]
            / "configs/zena_cmn/profiles/a720ae_npu_saturation.yaml"
        )
        p = load_parameters(profile)
        self.assertEqual(p["npu"]["dma_issue_width"], 8)
        self.assertEqual(p["npu"]["max_outstanding"], 1024)
        baseline = profile.with_name("a720ae_npu_v01.yaml")
        for change in (
            {"npu.max_outstanding": 17},
            {"npu.dma_issue_width": 2},
            {"npu.dma_width": 32},
            {"npu.fabric_width": 64},
            {"npu.sram_bank_width": 8},
            {"npu.bank_queue_depth": 8},
            {"npu.smmu.device_xlate_slots": 32},
        ):
            with self.subTest(change=change):
                with self.assertRaisesRegex(
                    ValueError, "experimental_scaling"
                ):
                    load_parameters(baseline, overrides=change)
                load_parameters(
                    baseline,
                    overrides={"npu.experimental_scaling": True, **change},
                )
        for change in (
            {"npu.experimental_scaling": False},
            {"npu.max_outstanding": 1025},
            {"npu.dma_issue_width": 9},
            {"npu.dma_width": 1024},
            {"npu.fabric_width": 1024},
            {"npu.sram_bank_width": 64},
            {"npu.sram_bank_width": 3},
            {"npu.bank_queue_depth": 65},
            {"npu.smmu.ptw_slots": 1025},
            {"npu.smmu.device_utlb_slots": 0},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                load_parameters(profile, overrides=change)
        with self.assertRaisesRegex(ValueError, "requires NPU enabled"):
            load_parameters(overrides={"npu.experimental_scaling": True})
        load_parameters(
            profile,
            overrides={
                "npu.max_outstanding": 1024,
                "npu.dma_issue_width": 8,
                "npu.dma_width": 512,
                "npu.fabric_width": 512,
                "npu.sram_bank_width": 32,
            },
        )

    def test_defaults(self):
        p = load_parameters()
        self.assertEqual(p["cpu"]["cores"], 16)
        self.assertEqual(p["cpu"]["lq_entries"], 32)
        self.assertEqual(p["cache"]["l2"]["size_kib_per_core"], 512)
        self.assertEqual(p["memory"]["channels"], 16)

    def test_unknown_type_range(self):
        for change in (
            {"cpu.unknown": 1},
            {"cpu.cores": True},
            {"cpu.rob_entries": 0},
            {"cpu.clock": "2Gbps"},
            {"memory.channels": 3},
            {"cmn.network": "unknown"},
            {"scope.npu_present": True},
            {"cache.l2.tag_banks": 3},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                load_parameters(overrides=change)

    def test_geometry(self):
        for change in (
            {"cpu.clusters": 2},
            {"cmn.slc_size_mib_total": 7},
            {"cmn.mesh.home_attach": [0] * 8},
            {"memory.custom_module": "missing"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                load_parameters(overrides=change)

    def test_channels_and_transfer_options(self):
        for channels in (4, 8, 16):
            p = load_parameters(
                overrides={
                    "memory.channels": channels,
                    "cmn.dmt": True,
                    "cmn.dct": True,
                }
            )
            self.assertEqual(p["memory"]["channels"], channels)

    def test_exclusive_dct_accepted(self):
        p = load_parameters(
            overrides={
                "cache.l2.policy": "exclusive_data",
                "cmn.dct": True,
            }
        )
        self.assertEqual(p["cache"]["l2"]["policy"], "exclusive_data")
        self.assertTrue(p["cmn"]["dct"])

    def test_integrated_pipeline_requires_simple_same_clock(self):
        self.assertFalse(
            load_parameters()["cache"]["integrated_private_pipeline"]
        )
        overrides = {"cache.integrated_private_pipeline": True}
        with self.assertRaisesRegex(ValueError, "requires Simple"):
            load_parameters(overrides=overrides)
        overrides["cmn.network"] = "simple"
        p = load_parameters(overrides=overrides)
        self.assertTrue(p["cache"]["integrated_private_pipeline"])
        for name in ("cpu.clock", "ruby.clock", "cmn.mesh.clock"):
            with self.subTest(name=name), self.assertRaisesRegex(
                ValueError, "identical clocks"
            ):
                load_parameters(overrides={**overrides, name: "1GHz"})

    def test_simple_network_is_explicit(self):
        self.assertEqual(load_parameters()["cmn"]["network"], "garnet")
        p = load_parameters(overrides={"cmn.network": "simple"})
        self.assertEqual(p["cmn"]["network"], "simple")

    def test_ignored_guest_controls_rejected(self):
        for change in (
            {"benchmark.read_working_set_mib_total": 128},
            {"benchmark.default_passes": 4},
            {"benchmark.worker_counts": [4, 8]},
            {"full_system.dtb": "/tmp/external.dtb"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                load_parameters(overrides=change)

    def test_small_profile(self):
        p = load_parameters(
            overrides={
                "cpu.cores": 2,
                "cpu.clusters": 1,
                "cpu.cores_per_cluster": 2,
                "cache.dsu_l3.instances": 1,
                "cmn.mesh.cluster_local_routers": 1,
                "cmn.mesh.cluster_attach": [5],
                "full_system.required_linux_cpu_clusters": 1,
            }
        )
        self.assertEqual(p["cpu"]["cores"], 2)

    def test_documented_geometry(self):
        profile = (
            Path(__file__).resolve().parents[2]
            / "configs/zena_cmn/profiles/a720ae_documented.yaml"
        )
        p = load_parameters(profile)
        self.assertEqual(p["cpu"]["execution_profile"], "a720ae_documented")
        self.assertEqual(p["cmn"]["slc_size_kib_per_home"], 512)
        self.assertEqual(p["cmn"]["mesh"]["rows"], 6)
        for change in (
            {"cmn.mesh.home_attach": [0, 3, 5, 10, 13, 18, 20, 24]},
            {"cpu.execution_profile": "pretend_hardware"},
            {"cache.l2.prefetch": "aggressive"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                load_parameters(profile, overrides=change)

    def test_legacy_slc_units(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as source:
            source.write("cmn:\n  slc_size_mib_per_home: 1\n")
            source.flush()
            p = load_parameters(source.name)
            self.assertEqual(p["cmn"]["slc_size_kib_per_home"], 1024)

    def test_no_input_mutation(self):
        p = load_parameters()
        before = copy.deepcopy(p)
        validate(p)
        self.assertEqual(p, before)

    def test_duplicate_yaml(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as source:
            source.write("cpu:\n  cores: 16\n  cores: 4\n")
            source.flush()
            with self.assertRaises(ValueError):
                load_parameters(source.name)


if __name__ == "__main__":
    unittest.main()
