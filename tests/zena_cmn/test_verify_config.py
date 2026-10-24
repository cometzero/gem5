# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Set ZENA_CMN_RUN to an instantiated FS run for config mutation checks."""

import copy
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/zena_cmn"))
from verify_config import (
    partition,
    verify,
    verify_dtb,
)


class PartitionTest(unittest.TestCase):
    def test_binary_digest_across_chunks(self):
        import hashlib
        import tempfile

        from verify_config import digest

        payload = bytes(range(256)) * 8193
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "binary"
            path.write_bytes(payload)
            self.assertEqual(digest(path), hashlib.sha256(payload).hexdigest())

    def test_native_oracle_not_cached(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from verify_config import expected_memory

        calls = []

        def run(command, **kwargs):
            calls.append(command)
            Path(command[-1]).write_text(
                json.dumps({"generation": len(calls)})
            )
            return SimpleNamespace(returncode=0)

        with patch("verify_config.subprocess.run", side_effect=run):
            self.assertEqual(expected_memory("{}", "gem5")["generation"], 1)
            self.assertEqual(expected_memory("{}", "gem5")["generation"], 2)

    def test_exact_partition(self):
        for count in (4, 8, 16):
            for stripe in (64, 512, 1024):
                masks = [
                    1 << (stripe.bit_length() - 1 + b)
                    for b in range(count.bit_length() - 1)
                ]
                ranges = [
                    ":".join(map(str, [0x80000000, 0x100000000, i, *masks]))
                    for i in range(count)
                ]
                self.assertTrue(
                    partition(ranges, "2147483648:4294967296", count, stripe)
                )
                if stripe != 64:
                    self.assertFalse(
                        partition(ranges, "2147483648:4294967296", count, 64)
                    )
                ranges[-1] = ranges[0]
                self.assertFalse(
                    partition(ranges, "2147483648:4294967296", count, stripe)
                )


@unittest.skipUnless(
    os.environ.get("ZENA_CMN_RUN"), "requires actual config.json and DTB"
)
class RuntimeConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = Path(os.environ["ZENA_CMN_RUN"])
        cls.original = json.loads((cls.directory / "config.json").read_text())
        cls.profile = json.loads(
            (cls.directory / "resolved-profile.json").read_text()
        )

    def setUp(self):
        self.config = copy.deepcopy(self.original)

    def test_complete_configuration(self):
        self.assertEqual(verify(self.config, self.profile)["status"], "PASS")
        self.assertEqual(
            verify_dtb(
                self.directory / "system.dtb", self.config, self.profile
            )["status"],
            "PASS",
        )

    def test_npu_stream_and_payload_route_rejected(self):
        if not self.profile.get("npu", {}).get("enabled", False):
            self.skipTest("requires NPU profile")
        platform = self.config["system"]["platform"]
        platform["npu"]["stream_id"] = 0
        platform["npu_smmu"]["request"][
            "peer"
        ] = "system.ruby.dma_rni0.dma_sequencer.in_ports"
        failures = verify(self.config, self.profile)["failures"]
        self.assertIn("NPU stream ID", failures)
        self.assertIn("NPU payload outside CPU Home", failures)

    def test_cpu_clock_binding_rejected(self):
        for group in ("cpu", "roi_cpu"):
            if group not in self.original["system"]:
                continue
            with self.subTest(group=group):
                config = copy.deepcopy(self.original)
                cpu = config["system"][group][0]
                cpu["clk_domain"] = "system.ruby.clk_domain"
                self.assertIn(
                    f"CPU clock binding: {cpu['path']}",
                    verify(config, self.profile)["failures"],
                )

    def test_private_cache_clock_binding_rejected(self):
        system = self.config["system"]
        cache = system["cpu"][0]["l2"]
        domains = system.get("cluster_clocks", [])
        cache["clk_domain"] = (
            domains[-1]["path"]
            if len(domains) > 1
            else "system.ruby.clk_domain"
        )
        self.assertIn(
            f"cache clock binding: {cache['path']}",
            verify(self.config, self.profile)["failures"],
        )

    def test_scmi_domain_contract_rejected(self):
        if "scmi" not in self.original["system"].get("platform", {}):
            self.skipTest("requires SCMI profile")
        mutations = (
            (
                "cluster_clocks",
                "clock",
                [400, 500, 555],
                "SCMI cluster0 OPP contract",
            ),
            (
                "cluster_clocks",
                "init_perf_level",
                0,
                "SCMI cluster0 OPP contract",
            ),
            ("cluster_clocks", "domain_id", 9, "SCMI cluster0 OPP contract"),
            (
                "cluster_clocks",
                "voltage_domain",
                "system.voltage_domain",
                "SCMI cluster0 voltage binding",
            ),
            (
                "cluster_voltages",
                "voltage",
                [0.95, 0.8, 0.75],
                "SCMI cluster0 voltage binding",
            ),
        )
        for name, key, value, failure in mutations:
            with self.subTest(name=name, key=key):
                config = copy.deepcopy(self.original)
                config["system"][name][0][key] = value
                self.assertIn(
                    failure, verify(config, self.profile)["failures"]
                )
        self.config["system"]["dvfs_handler"]["domains"] = []
        self.assertIn(
            "SCMI DVFS handler binding",
            verify(self.config, self.profile)["failures"],
        )
        self.config = copy.deepcopy(self.original)
        self.config["system"]["cluster_clocks"] = []
        self.assertIn(
            "SCMI cluster domain count",
            verify(self.config, self.profile)["failures"],
        )
        self.config = copy.deepcopy(self.original)
        self.config["system"]["platform"]["scmi"]["perf_levels"] = [
            2500,
            2000,
            1700,
        ]
        self.assertIn(
            "SCMI performance levels",
            verify(self.config, self.profile)["failures"],
        )

    def test_wrong_iq_rejected(self):
        cpu = self.config["system"].get(
            "roi_cpu", self.config["system"]["cpu"]
        )[0]
        if "instQueues" in cpu:
            cpu["instQueues"][0]["numEntries"] = 64
        else:
            cpu["numIQEntries"] = 64
        self.assertIn(
            "effective instruction queue entries",
            verify(self.config, self.profile)["failures"],
        )

    def test_l2_bypass_rejected(self):
        self.config["system"]["cpu"][0]["l2"]["downstream_destinations"] = [
            self.config["system"]["ruby"]["hnf"][0]["cntrl"]["path"]
        ]
        self.assertIn(
            "CPU0 L2 -> own DSU", verify(self.config, self.profile)["failures"]
        )

    def test_l2_read_hit_pipeline_rejected(self):
        self.config["system"]["cpu"][0]["l2"]["read_hit_latency"] += 1
        self.assertIn(
            "CPU0 L2 read hit pipeline",
            verify(self.config, self.profile)["failures"],
        )

    def test_wrong_network_rejected(self):
        network = self.config["system"]["ruby"]["network"]
        network["type"] = (
            "SimpleNetwork"
            if network["type"] == "GarnetNetwork"
            else "GarnetNetwork"
        )
        self.assertIn(
            "selected network implementation",
            verify(self.config, self.profile)["failures"],
        )

    def test_wrong_prefetch_selection_rejected(self):
        l2 = self.config["system"]["cpu"][0]["l2"]
        l2["use_prefetcher"] = not l2["use_prefetcher"]
        self.assertIn(
            "CPU0 l2 prefetch selection",
            verify(self.config, self.profile)["failures"],
        )

    def test_private_handoff_mismatch_rejected(self):
        cpu = self.config["system"]["cpu"][0]
        cpu["l2"]["reqOut"]["allow_zero_latency"] = not cpu["l2"]["reqOut"][
            "allow_zero_latency"
        ]
        self.assertIn(
            "CPU0 l2 private pipeline handoff",
            verify(self.config, self.profile)["failures"],
        )

    def test_exclusive_data_binding_rejected(self):
        profile = copy.deepcopy(self.profile)
        profile["cache"]["l2"]["policy"] = "exclusive_data"
        cpu = self.config["system"]["cpu"][0]
        cpu["l2"]["exclusive_data_upstream_version"] = (
            cpu["l1d"]["version"] + 1
        )
        cpu["l2"]["dealloc_backinv_shared"] = True
        failures = verify(self.config, profile)["failures"]
        self.assertIn("CPU0 exclusive data upstream", failures)
        self.assertIn("CPU0 exclusive data back-invalidation", failures)

    def test_simple_transport_mutations_rejected(self):
        if self.profile["cmn"].get("network") != "simple":
            self.skipTest("requires instantiated Simple network")
        network = self.config["system"]["ruby"]["network"]
        local = network["routers"][-1]
        link = network["int_links"][0]
        mutations = (
            (local, "ext_routing_latency", 1, "Simple routing latency: "),
            (link, "bandwidth_factor", 0, "Simple link bandwidth/latency: "),
            (
                network,
                "endpoint_bandwidth",
                2000,
                "Simple shared-vnet bandwidth model",
            ),
            (
                link["buffers"][0],
                "buffer_size",
                0,
                "Simple finite link buffers: ",
            ),
            (
                local["port_buffers"][0],
                "buffer_size",
                0,
                "Simple finite switch buffers: ",
            ),
        )
        for obj, field, bad, prefix in mutations:
            with self.subTest(field=field, prefix=prefix):
                saved = obj[field]
                obj[field] = bad
                self.assertTrue(
                    any(
                        f.startswith(prefix)
                        for f in verify(self.config, self.profile)["failures"]
                    )
                )
                obj[field] = saved

    def test_network_omission_rejected(self):
        del self.config["system"]["ruby"]["network"]["ext_links"][0]
        self.assertEqual(verify(self.config, self.profile)["status"], "FAIL")

    def test_backing_store_update_rejected(self):
        self.config["system"]["ruby"]["access_backing_store"] = True
        self.assertIn(
            "no automatic backing-store update",
            verify(self.config, self.profile)["failures"],
        )

    def test_cross_cluster_dt_rejected(self):
        affinities = self.config["system"]["cpu_affinities"]
        if len(affinities) <= 4:
            self.skipTest("requires two instantiated clusters")
        affinities[0], affinities[4] = affinities[4], affinities[0]
        with self.assertRaises(ValueError):
            verify_dtb(
                self.directory / "system.dtb", self.config, self.profile
            )

    def test_clock_value_and_binding_rejected(self):
        self.config["system"]["cpu_clk_domain"]["clock"] = [10000]
        self.assertEqual(verify(self.config, self.profile)["status"], "FAIL")
        self.config = copy.deepcopy(self.original)
        self.config["system"].get("roi_cpu", self.config["system"]["cpu"])[0][
            "clk_domain"
        ] = "system.clk_domain"
        self.assertEqual(verify(self.config, self.profile)["status"], "FAIL")

    def test_native_memory_parameters_rejected(self):
        for key in (
            "tRCD",
            "tBURST",
            "device_size",
            "ranks_per_channel",
            "read_buffer_size",
        ):
            with self.subTest(key=key):
                config = copy.deepcopy(self.original)
                config["system"]["mem_ctrls"][0]["dram"][key] = 999999
                self.assertEqual(
                    verify(config, self.profile)["status"], "FAIL"
                )

    def test_unknown_native_class_rejected(self):
        profile = copy.deepcopy(self.profile)
        profile["memory"]["custom_class"] = "MissingDRAMClass"
        report = verify(self.config, profile)
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(
            any(
                "native class resolution failed" in reason
                for reason in report["failures"]
            )
        )

    def test_cache_clock_binding_rejected(self):
        self.config["system"]["cpu"][0]["l2"][
            "clk_domain"
        ] = "system.clk_domain"
        self.assertTrue(
            any(
                "cache clock binding" in reason
                for reason in verify(self.config, self.profile)["failures"]
            )
        )

    def test_stale_verification_rejected(self):
        import tempfile
        from unittest.mock import patch

        from verify_config import (
            artifact_hashes,
            validate_saved_report,
        )

        # Actual input files are copied; source files and original run untouched.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for name in ("config.json", "system.dtb", "resolved-profile.json"):
                (directory / name).write_bytes(
                    (self.directory / name).read_bytes()
                )
            hashes = artifact_hashes(directory)
            report = {
                "status": "PASS",
                "device_tree": {"status": "PASS"},
                "artifact_hashes": hashes,
            }
            validate_saved_report(directory, report)
            for name in ("config.json", "system.dtb", "resolved-profile.json"):
                with self.subTest(name=name):
                    original = (directory / name).read_bytes()
                    (directory / name).write_bytes(original + b" ")
                    with self.assertRaisesRegex(ValueError, "stale"):
                        validate_saved_report(directory, report)
                    (directory / name).write_bytes(original)
            with patch(
                "verify_config.artifact_hashes",
                return_value={**hashes, "verifier": "changed"},
            ):
                with self.assertRaisesRegex(ValueError, "stale"):
                    validate_saved_report(directory, report)


if __name__ == "__main__":
    unittest.main()
