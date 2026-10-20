# SPDX-License-Identifier: BSD-3-Clause
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "scripts/zena_cmn")
)
from analyze_slc_trace import analyze


class SlcTraceTest(unittest.TestCase):
    def test_complete_mapping_counter_reconciliation_and_target_noise(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "m5out").mkdir()
            (run / "roofline-analysis").mkdir()
            config = {
                "system": {
                    "ruby": {
                        "hnf": [{"cntrl": {"version": 52}}],
                        "rnf": [
                            {"dsu_l3": {"version": 12}},
                            {"dsu_l3": {"version": 25}},
                        ],
                    }
                }
            }
            (run / "m5out/config.json").write_text(json.dumps(config))
            roi = {
                "id": 22,
                "begin_tick": 1,
                "end_tick": 10,
                "slc_source": {"status": "UNVERIFIED_SLC_SOURCE"},
                "cache": {"SLC_proxy": {"hits": 1, "misses": 0}},
            }
            (run / "roofline-analysis/roofline.json").write_text(
                json.dumps({"cases": [roi]})
            )
            (run / "guest_console.log").write_text(
                "ROOF_TARGET "
                + json.dumps({"id": 22, "virtual_start": 4096, "bytes": 64})
                + "\n"
            )
            trans = run / "transitions.cc"
            trans.write_text(
                "case HASH_FUN(Cache_State_SC, Cache_Event_ReadShared):\nProfile_Hit();\nreturn TransitionResult_Valid;\ncase HASH_FUN(Cache_State_I, Cache_Event_ReadShared):\nProfile_Miss();\nreturn TransitionResult_Valid;\n"
            )
            tlb = "2: system.roi_cpu0.mmu.dtb: Lookup 0x1000, asn 0x1 -> hit vmn 0x0 ss NS ppn 0x8 size: 0xfff pa: 0x8000\n"
            events = "3 52 Cache ReadShared SC>BUSY [0x8000, line 0x8000]\n4 12 Cache ReadShared I>BUSY [0x8000, line 0x8000]\n"
            trace = run / "m5out/roi-trace.log"
            trace.write_text(tlb + events)

            def check():
                return analyze(run, trans)["cases"][0]

            self.assertEqual(check()["status"], "QUALIFIED_TARGET_ADDRESS_SLC")
            self.assertEqual(
                check()["aggregate_status"], "UNVERIFIED_SLC_SOURCE"
            )
            trace.write_text(events)
            self.assertTrue(check()["ambiguous_mapping"])
            trace.write_text(
                tlb
                + events
                + "5 52 Cache SendSnpShared BUSY>BUSY [0x8000, line 0x8000]\n"
            )
            self.assertEqual(
                check()["status"], "UNVERIFIED_TARGET_ADDRESS_SLC"
            )
            trace.write_text(
                tlb
                + events
                + "5 52 Cache SendSnpShared BUSY>BUSY [0x9000, line 0x9000]\n"
            )
            self.assertEqual(check()["status"], "QUALIFIED_TARGET_ADDRESS_SLC")
            self.assertEqual(len(check()["outside_forbidden_events"]), 1)
            trace.write_text(
                tlb + tlb.replace("pa: 0x8000", "pa: 0xa000") + events
            )
            self.assertTrue(check()["ambiguous_mapping"])
            for bad in (
                "5 52 Cache ReadShared I>BUSY [0x8000, line 0x8000]\n",
                "5 25 Cache SendCompData BUSY>BUSY [0x8000, line 0x8000]\n",
            ):
                trace.write_text(tlb + events + bad)
                self.assertEqual(
                    check()["status"], "UNVERIFIED_TARGET_ADDRESS_SLC"
                )
                self.assertTrue(check()["target_forbidden_events"])
            trace.write_text(tlb + events + events)
            self.assertFalse(check()["trace_counter_reconciled"])
            trans.write_text("// empty map")
            with self.assertRaises(ValueError):
                check()
