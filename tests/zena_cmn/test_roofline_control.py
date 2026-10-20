# SPDX-License-Identifier: BSD-3-Clause
"""Multi-ROI ordering and completion checks without simulation."""
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import (
    Mock,
    patch,
)

GEM5 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(GEM5 / "scripts/zena_cmn"))
from run_roofline import validate_completion

spec = importlib.util.spec_from_file_location(
    "roof_control", GEM5 / "configs/zena_cmn/roi_control.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RooflineControlTest(unittest.TestCase):
    def test_trace_enabled_only_inside_roi(self):
        fake = types.SimpleNamespace(
            switchCpus=Mock(),
            stats=types.SimpleNamespace(reset=Mock(), dump=Mock()),
            curTick=lambda: 10,
            trace=Mock(),
            debug=types.SimpleNamespace(
                flags={"ProtocolTrace": Mock(), "TLBVerbose": Mock()}
            ),
        )
        with tempfile.TemporaryDirectory() as output, patch.dict(
            sys.modules, m5=fake
        ):
            control = module.RoiControl(
                types.SimpleNamespace(cpu=[1], roi_cpu=[2]),
                output,
                True,
                multi_roi=True,
                trace_roi=True,
            )
            self.assertEqual(fake.trace.disable.call_count, 1)
            control.handle("workbegin", 0x5A535743)
            fake.trace.enable.assert_not_called()
            control.handle("workbegin", 22)
            fake.trace.enable.assert_called_once()
            control.handle("workend", 0)
            self.assertEqual(fake.trace.disable.call_count, 2)

    def test_order_and_reuse(self):
        fake = types.SimpleNamespace(
            switchCpus=Mock(),
            stats=types.SimpleNamespace(reset=Mock(), dump=Mock()),
            curTick=lambda: 10,
        )
        system = types.SimpleNamespace(cpu=[1], roi_cpu=[2])
        with tempfile.TemporaryDirectory() as output, patch.dict(
            sys.modules, m5=fake
        ):
            control = module.RoiControl(system, output, True, multi_roi=True)
            self.assertFalse(control.can_exit())
            with self.assertRaises(RuntimeError):
                control.handle("workbegin", 1)
            control.handle("workbegin", 0x5A535743)
            self.assertFalse(control.can_exit())
            control.handle("workbegin", 1)
            self.assertFalse(control.can_exit())
            with self.assertRaises(RuntimeError):
                control.handle("workbegin", 2)
            control.handle("workend", 0)
            self.assertTrue(control.can_exit())
            with self.assertRaises(RuntimeError):
                control.handle("workbegin", 1)
            with self.assertRaises(RuntimeError):
                control.handle("workend", 0)
            control.handle("workbegin", 2)
            control.handle("workend", 0)
            self.assertEqual(control.completed_cases, {1, 2})
            self.assertEqual(
                [e.get("case_id") for e in control.events], [None, 1, 1, 2, 2]
            )

    def test_l2_followup_preserves_strict_default(self):
        console = "CMN_ONLINE=0-15\nROOF_DONE=PASS\n" + "\n".join(
            "ROOF_CASE "
            + json.dumps(dict(id=i, result="PASS", checksum=1, expected=1))
            for i in (12, 13)
        )
        events = [
            dict(phase=phase, case_id=i)
            for i in (12, 13)
            for phase in ("roi_begin", "roi_end")
        ]
        exit_info = dict(
            status="COMPLETED", code=0, cause="m5_exit instruction encountered"
        )
        validate_completion(console, events, exit_info, {12, 13})
        with self.assertRaises(RuntimeError):
            validate_completion(console, events, exit_info)
        with self.assertRaises(RuntimeError):
            validate_completion(console, events[:-1], exit_info, {12, 13})

    def test_requires_every_case_and_normal_exit(self):
        cases = [
            dict(id=i, result="PASS", checksum=123, expected=123)
            for i in range(1, 27)
        ]

        def console_for(records):
            return "CMN_ONLINE=0-15\nROOF_DONE=PASS\n" + "\n".join(
                "ROOF_CASE " + json.dumps(c) for c in records
            )

        console = console_for(cases)
        events = [
            dict(phase=phase, case_id=i)
            for i in range(1, 27)
            for phase in ("roi_begin", "roi_end")
        ]
        exit_info = dict(
            status="COMPLETED", code=0, cause="m5_exit instruction encountered"
        )
        validate_completion(console, events, exit_info)
        for invalid_console in (
            console_for(cases[:-1]),
            console_for(cases + cases[-1:]),
            console_for([dict(cases[0], checksum=999)] + cases[1:]),
            console.replace("ROOF_DONE=PASS", "ROOF_DONE=PASS_NOT_REALLY"),
        ):
            with self.assertRaises(RuntimeError):
                validate_completion(invalid_console, events, exit_info)
        with self.assertRaises(RuntimeError):
            validate_completion(
                console,
                events,
                dict(exit_info, cause="simulate() limit reached"),
            )
        for records in (events[:-1], events + events[-2:]):
            with self.assertRaises(RuntimeError):
                validate_completion(console, records, exit_info)
        with self.assertRaises(RuntimeError):
            validate_completion(
                console, events, dict(status="INCOMPLETE", code=1)
            )


if __name__ == "__main__":
    unittest.main()
