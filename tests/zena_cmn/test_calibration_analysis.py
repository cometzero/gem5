# SPDX-License-Identifier: BSD-3-Clause
import copy
import json
import tempfile
import unittest
from pathlib import Path

from analyze_calibration import analyze
from test_roofline_analysis import (
    CASE,
    CONFIG,
    STATS,
)


class CalibrationAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        (self.run / "m5out").mkdir()
        self.put("run_manifest.json", {"variant": "calibration"})
        self.put("result.json", {"status": "PASS", "host_seconds": 1})
        config = copy.deepcopy(CONFIG)
        config["system"]["cpu_clk_domain"] = {"clock": [10]}
        self.put("m5out/config.json", config)
        self.put(
            "m5out/exit.json",
            {
                "status": "COMPLETED",
                "code": 0,
                "cause": "m5_exit instruction encountered",
            },
        )
        console = "CMN_ONLINE=0-15\n"
        events, stats = [], ""
        for i in range(101, 109):
            case = dict(CASE, id=i, instructions=10, dependent_loads=10)
            console += "ROOF_CASE " + json.dumps(case) + "\n"
            events.extend(
                [
                    {"phase": "roi_begin", "tick": i * 1000, "case_id": i},
                    {"phase": "roi_end", "tick": (i + 1) * 1000, "case_id": i},
                ]
            )
            stats += STATS.replace("finalTick 2000", f"finalTick {(i+1)*1000}")
        console += "ROOF_DONE=PASS\n"
        (self.run / "guest_console.log").write_text(console)
        self.put("m5out/roi-events.json", events)
        (self.run / "m5out/stats.txt").write_text(stats)

    def put(self, name, value):
        (self.run / name).write_text(json.dumps(value))

    def test_load_stream_variant_accepts_exact_case_set(self):
        self.put("run_manifest.json", {"variant": "load_streams"})
        config = json.loads((self.run / "m5out/config.json").read_text())
        for cpu in config["system"]["cpu"]:
            cpu["l2"]["use_prefetcher"] = False
        self.put("m5out/config.json", config)
        console, events, stats = "CMN_ONLINE=0-15\n", [], ""
        for i in range(301, 316):
            case = dict(
                CASE,
                id=i,
                level="L1",
                instructions=10,
                dependent_loads=0,
                working_set_bytes=32768,
            )
            console += "ROOF_CASE " + json.dumps(case) + "\n"
            events.extend(
                [
                    {"phase": "roi_begin", "tick": i * 1000, "case_id": i},
                    {"phase": "roi_end", "tick": (i + 1) * 1000, "case_id": i},
                ]
            )
            stats += STATS.replace("finalTick 2000", f"finalTick {(i+1)*1000}")
        (self.run / "guest_console.log").write_text(
            console + "ROOF_DONE=PASS\n"
        )
        self.put("m5out/roi-events.json", events)
        (self.run / "m5out/stats.txt").write_text(stats)
        rows = analyze(self.run)["cases"]
        self.assertEqual(len(rows), 15)
        self.assertTrue(all("residency" in row for row in rows))
        config["system"]["cpu"][0]["l2"]["use_prefetcher"] = True
        self.put("m5out/config.json", config)
        with self.assertRaisesRegex(ValueError, "prefetch off"):
            analyze(self.run)

    def test_execution_variant_remains_unqualified(self):
        self.put("run_manifest.json", {"variant": "execution_checks"})
        events = json.loads((self.run / "m5out/roi-events.json").read_text())
        for event in events:
            event["case_id"] += 300
        self.put("m5out/roi-events.json", events)
        lines = []
        for line in (self.run / "guest_console.log").read_text().splitlines():
            if line.startswith("ROOF_CASE "):
                row = json.loads(line[10:])
                row["id"] += 300
                row["dependent_loads"] = 0
                line = "ROOF_CASE " + json.dumps(row)
            lines.append(line)
        (self.run / "guest_console.log").write_text("\n".join(lines) + "\n")
        rows = analyze(self.run)["cases"]
        self.assertEqual(len(rows), 8)
        self.assertTrue(
            all(
                row["execution_qualification"]
                == "UNQUALIFIED_HARDWARE_MODEL_DIAGNOSTIC"
                for row in rows
            )
        )
        self.assertEqual(rows[5]["sog_reference_cycles_per_instruction"], 10)
        self.assertEqual(
            rows[7]["cycles_per_divide"], rows[6]["cycles_per_divide"] * 2
        )

    def test_execution_lsu_case_set(self):
        self.put(
            "run_manifest.json",
            {
                "variant": "execution_checks",
                "expected_cases": list(range(401, 413)),
            },
        )
        console, events, stats = "CMN_ONLINE=0-15\n", [], ""
        for identifier in range(401, 413):
            case = dict(
                CASE,
                id=identifier,
                level="L1" if identifier >= 409 else "compute",
                instructions=2048,
                dependent_loads=0,
                useful_bytes=65536 if identifier >= 409 else 0,
            )
            console += "ROOF_CASE " + json.dumps(case) + "\n"
            events.extend(
                [
                    {
                        "phase": "roi_begin",
                        "tick": identifier * 1000,
                        "case_id": identifier,
                    },
                    {
                        "phase": "roi_end",
                        "tick": (identifier + 1) * 1000,
                        "case_id": identifier,
                    },
                ]
            )
            stats += STATS.replace(
                "finalTick 2000", f"finalTick {(identifier + 1) * 1000}"
            )
        (self.run / "guest_console.log").write_text(
            console + "ROOF_DONE=PASS\n"
        )
        (self.run / "m5out/stats.txt").write_text(stats)
        self.put("m5out/roi-events.json", events)
        rows = analyze(self.run)["cases"]
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows[-1]["useful_read_bytes"], 32768)
        self.assertEqual(rows[-1]["useful_write_bytes"], 32768)
        self.assertEqual(rows[-2]["useful_write_bytes"], 65536)
        self.assertIsNone(rows[-1]["cycles_per_divide"])
        self.put(
            "run_manifest.json",
            {"variant": "execution_checks", "expected_cases": [401]},
        )
        with self.assertRaisesRegex(ValueError, "case set"):
            analyze(self.run)

    def test_exact_roi_cycles_and_missing_counter(self):
        rows = analyze(self.run)["cases"]
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[0]["cycles_per_target_instruction"], 10)
        self.assertIsNone(rows[0]["load_to_use_cycles"])
        self.assertTrue(all(v is None for v in rows[0]["prefetch"].values()))

    def test_missing_dram_counter_not_measured_zero(self):
        p = self.run / "m5out/stats.txt"
        p.write_text(
            "\n".join(
                line
                for line in p.read_text().splitlines()
                if "dramBytesRead" not in line
            )
        )
        self.assertIsNone(analyze(self.run)["cases"][0]["dram_bytes"]["read"])

    def test_overlapping_streams_are_throughput(self):
        p = self.run / "guest_console.log"
        p.write_text(
            p.read_text().replace(
                '"dependent_loads": 10',
                '"dependent_loads": 10, "independent_streams": 4',
            )
        )
        self.assertIn(
            "throughput, not latency",
            analyze(self.run)["cases"][0]["interpretation"],
        )

    def test_independent_load_loop_is_not_pointer_chase(self):
        p = self.run / "guest_console.log"
        p.write_text(
            p.read_text().replace(
                '"dependent_loads": 10',
                '"dependent_loads": 0, "independent_streams": 8',
            )
        )
        self.assertEqual(
            analyze(self.run)["cases"][0]["interpretation"],
            "kernel throughput, not guaranteed hardware peak",
        )

    def test_memory_pattern_case_contract(self):
        self.put("run_manifest.json", {"variant": "memory_patterns"})
        # The original eight-case calibration output must not qualify as seven
        # memory-pattern cases merely because its checksums pass.
        with self.assertRaisesRegex(RuntimeError, "missing or duplicate"):
            analyze(self.run)

    def test_padded_native_paths(self):
        p = self.run / "m5out/config.json"
        config = json.loads(p.read_text())
        config["system"]["cpu"][0]["l2"].update(
            use_prefetcher=True,
            prefetcher={"path": "system.cpu00.l2.prefetcher"},
        )
        p.write_text(json.dumps(config))
        p = self.run / "m5out/stats.txt"
        p.write_text(
            p.read_text().replace(
                "simFreq 1000000",
                "simFreq 1000000\nsystem.cpu0.l2.prefetcher.pfIssued 2\n"
                "system.cpu0.l2.prefetcher.pfUseful 1",
            )
        )
        row = analyze(self.run)["cases"][0]
        self.assertEqual(row["prefetch_status"], "measured")
        self.assertEqual(
            row["prefetch"], {"pfIssued": 2, "pfUseful": 1, "pfUnused": 0}
        )

    def test_missing_stats_rejected(self):
        p = self.run / "m5out/stats.txt"
        p.write_text(
            p.read_text().replace("finalTick 102000", "finalTick 102001")
        )
        with self.assertRaisesRegex(ValueError, "exact ROI"):
            analyze(self.run)

    def test_failed_run_rejected(self):
        self.put("result.json", {"status": "TIMEOUT"})
        with self.assertRaisesRegex(ValueError, "did not pass"):
            analyze(self.run)
