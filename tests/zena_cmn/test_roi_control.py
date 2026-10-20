# SPDX-License-Identifier: BSD-3-Clause
"""Lifecycle rejects duplicate/out-of-order ROI markers."""

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

path = Path(__file__).resolve().parents[2] / "configs/zena_cmn/roi_control.py"
spec = importlib.util.spec_from_file_location("roi_control", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class LifecycleTest(unittest.TestCase):
    def test_atomic_switch_and_single_roi(self):
        fake = types.SimpleNamespace(
            drain=Mock(),
            switchCpus=Mock(),
            stats=types.SimpleNamespace(reset=Mock(), dump=Mock()),
            curTick=lambda: 50,
        )
        system = types.SimpleNamespace(cpu=[1, 2], roi_cpu=[3, 4])
        with tempfile.TemporaryDirectory() as output, patch.dict(
            sys.modules, m5=fake
        ):
            control = module.RoiControl(system, output, True)
            with self.assertRaises(RuntimeError):
                control.handle("workend", 0)
            control.handle("workbegin", 0x5A535743)
            control.handle("workbegin", 0)
            control.handle("workend", 0)
            with self.assertRaises(RuntimeError):
                control.handle("workend", 0)
            fake.switchCpus.assert_called_once_with(system, [(1, 3), (2, 4)])
            fake.stats.reset.assert_called_once()
            fake.stats.dump.assert_called_once()
            self.assertEqual(
                len(json.loads(Path(output, "roi-events.json").read_text())), 3
            )


if __name__ == "__main__":
    unittest.main()
