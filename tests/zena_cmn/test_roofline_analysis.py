# SPDX-License-Identifier: BSD-3-Clause
import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path

SOURCE = (
    Path(__file__).resolve().parents[2]
    / "scripts/zena_cmn/analyze_roofline.py"
)
sys.path.insert(0, str(SOURCE.parent))
SPEC = importlib.util.spec_from_file_location("roofline", SOURCE)
ROOF = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROOF)

CONFIG = {
    "system": {
        "mem_ctrls": [{"dram": {"path": "system.mc0.dram"}}],
        "cpu": [
            {
                "cpu_id": 0,
                "l1d": {"cache": {"path": "system.cpu00.l1d.cache"}},
                "l2": {"cache": {"path": "system.cpu00.l2.cache"}},
            }
        ],
        "ruby": {
            "rnf": [
                {
                    "dsu_l3": {
                        "cache": {"path": "system.ruby.rnf0.dsu_l3.cache"}
                    }
                }
            ],
            "hnf": [
                {"cntrl": {"cache": {"path": "system.ruby.hnf0.cntrl.cache"}}}
            ],
        },
    }
}
CASE = {
    "id": 1,
    "level": "L2",
    "kernel": "fma_r1",
    "working_set_bytes": 262144,
    "useful_bytes": 6400,
    "flops": 12800,
    "result": "PASS",
    "checksum": 123.0,
    "expected": 123.0,
}
EVENTS = [
    {"phase": "roi_begin", "tick": 1000, "case_id": 1},
    {"phase": "roi_end", "tick": 2000, "case_id": 1},
]
STATS = """---------- Begin Simulation Statistics ----------
simFreq 1000000
simTicks 1000
finalTick 2000
system.cpu0.l1d.cache.m_demand_hits 300
system.cpu0.l1d.cache.m_demand_misses 100
system.cpu0.l2.cache.m_demand_hits 100
system.cpu0.l2.cache.m_demand_misses 0
system.ruby.rnf0.dsu_l3.cache.m_demand_hits 0
system.ruby.rnf0.dsu_l3.cache.m_demand_misses 0
system.ruby.hnf0.cntrl.cache.m_demand_hits 0
system.ruby.hnf0.cntrl.cache.m_demand_misses 0
system.mc0.dram.dramBytesRead 64
system.mc0.dram.dramBytesWritten 0
---------- End Simulation Statistics ----------
"""


def analyze(stats=STATS, case=CASE, events=EVENTS, **kwargs):
    return ROOF.analyze(
        stats, "ROOF_CASE " + json.dumps(case), events, CONFIG, **kwargs
    )


class RooflineTests(unittest.TestCase):
    def test_model_label_uses_instantiated_geometry(self):
        config = copy.deepcopy(CONFIG)
        config["system"]["ruby"]["hnf"][0]["cntrl"]["cache"]["size"] = (
            4 * 1024**2
        )
        config["system"]["ruby"]["network"] = {"type": "GarnetNetwork"}
        label = ROOF.model_description(config)
        self.assertIn("1-core", label)
        self.assertIn("4 MiB SLC / GarnetNetwork", label)
        self.assertIn("unknown", ROOF.model_description(CONFIG))

    def test_metrics_and_subline_miss_coverage(self):
        row = analyze()["cases"][0]
        self.assertEqual(row["residency"], "QUALIFIED_MODEL_PROXY")
        self.assertEqual(row["seconds"], 0.001)
        self.assertEqual(row["useful_AI"], 2)
        self.assertAlmostEqual(row["GFLOP_s"], 0.0128)
        self.assertEqual(row["dram_bytes"]["Read"], 64)
        self.assertEqual(row["cache"]["L1"]["hit_fraction"], 0.75)

    def test_read_roof_requires_stable_qualified_pair(self):
        base = {"level": "L2", "residency": "QUALIFIED_MODEL_PROXY"}
        pair = [
            dict(base, kernel="read_a", useful_GB_s=10),
            dict(base, kernel="read_b", useful_GB_s=10.4),
        ]
        self.assertEqual(
            ROOF.read_roofs(pair)["L2"]["roof_status"],
            "QUALIFIED_EMPIRICAL_ROOF",
        )
        pair[1]["useful_GB_s"] = 11
        result = ROOF.read_roofs(pair)["L2"]
        self.assertEqual(result["roof_status"], "UNVERIFIED_STABILITY")
        self.assertIsNone(result["roof_GB_s"])
        pair[1]["residency"] = "UNVERIFIED_TARGET_HIT"
        self.assertEqual(
            ROOF.read_roofs(pair)["L2"]["roof_status"], "UNVERIFIED_RESIDENCY"
        )
        self.assertEqual(
            ROOF.read_roofs(pair[:1])["L2"]["roof_status"],
            "UNVERIFIED_MISSING_READ_PAIR",
        )

    def test_checksum_verification(self):
        for checksum in (122, float("nan"), float("inf"), None):
            with self.assertRaises(ValueError):
                analyze(case=dict(CASE, checksum=checksum))

    def test_missing_dram_channel_is_unavailable(self):
        config = json.loads(json.dumps(CONFIG))
        config["system"]["mem_ctrls"].append(
            {"dram": {"path": "system.mc1.dram"}}
        )
        result = ROOF.analyze(
            STATS, "ROOF_CASE " + json.dumps(CASE), EVENTS, config
        )
        self.assertIsNone(result["cases"][0]["dram_bytes"]["Read"])
        self.assertIsNone(result["cases"][0]["dram_bytes"]["Written"])

    def test_missing_and_duplicate_events(self):
        for events in (
            EVENTS[:1],
            EVENTS + [EVENTS[0]],
            [{"phase": "roi_begin", "tick": 1000}],
        ):
            with self.assertRaises(ValueError):
                analyze(events=events)

    def test_duplicate_stats_rejected(self):
        with self.assertRaises(ValueError):
            analyze(stats=STATS + STATS)

    def test_duplicate_guest_rejected(self):
        guest = ("ROOF_CASE " + json.dumps(CASE) + "\n") * 2
        with self.assertRaises(ValueError):
            ROOF.analyze(STATS, guest, EVENTS, CONFIG)

    def test_mismatched_dump_rejected(self):
        with self.assertRaises(ValueError):
            analyze(stats=STATS.replace("finalTick 2000", "finalTick 2001"))

    def test_cold_dram_never_roof(self):
        case = dict(CASE, level="DRAM_cold")
        self.assertEqual(
            analyze(case=case)["cases"][0]["residency"], "COLD_DIAGNOSTIC"
        )

    def test_missing_counter_not_zero_or_qualified(self):
        result = analyze(
            stats=STATS.replace("system.cpu0.l2.cache.m_demand_hits 100\n", "")
        )
        self.assertIsNone(result["cases"][0]["cache"]["L2"]["hits"])
        self.assertEqual(
            result["cases"][0]["residency"], "UNVERIFIED_TARGET_HIT"
        )

    def test_partial_keeps_only_complete_passing_cases(self):
        events = EVENTS + [{"phase": "roi_begin", "tick": 3000, "case_id": 2}]
        with self.assertRaises(ValueError):
            analyze(events=events)
        self.assertEqual(
            len(analyze(events=events, allow_partial=True)["cases"]), 1
        )

    def test_slc_directory_misses_are_not_hits(self):
        stats = STATS.replace(
            "system.ruby.hnf0.cntrl.cache.m_demand_misses 0",
            "system.ruby.hnf0.cntrl.cache.m_demand_misses 100",
        )
        self.assertEqual(
            analyze(stats=stats, case=dict(CASE, level="SLC_proxy"))["cases"][
                0
            ]["residency"],
            "UNVERIFIED_TARGET_HIT",
        )

    def test_l3_rejects_l2_hit_contamination(self):
        caches = ROOF.cache_metrics(
            ROOF.stats_blocks(STATS)[0], ROOF.cache_paths(CONFIG)
        )
        caches["L3"] = {"hits": 100, "misses": 0, "hit_fraction": 1.0}
        self.assertEqual(
            ROOF.residency("L3", caches, 6400), "UNVERIFIED_UPPER_CACHE"
        )

    def test_slc_source_rejects_peer_dram_and_missing_buffers(self):
        config = copy.deepcopy(CONFIG)
        ruby = config["system"]["ruby"]
        ruby["hnf"][0]["cntrl"]["snpOut"] = {"path": "home.snp"}
        ruby["rnf"].append({"dsu_l3": {"datOut": {"path": "peer.dat"}}})
        caches = {"SLC_proxy": {"hits": 2048}, "L3": {"misses": 2048}}

        def check(stats, dram=0):
            return ROOF.slc_source(
                stats, config, caches, 2048, {"Read": dram}
            )["status"]

        self.assertEqual(check({}), "QUALIFIED_AGGREGATE_SLC")
        for key in ("home.snp.m_msg_count", "peer.dat.m_msg_count"):
            self.assertEqual(check({key: 1}), "UNVERIFIED_SLC_SOURCE")
        self.assertEqual(check({}, 64), "UNVERIFIED_SLC_SOURCE")
        del ruby["rnf"][1]["dsu_l3"]["datOut"]
        self.assertEqual(check({}), "UNVERIFIED_SLC_SOURCE")


if __name__ == "__main__":
    unittest.main()
