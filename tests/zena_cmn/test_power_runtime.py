# SPDX-License-Identifier: BSD-3-Clause
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "configs"))
from zena_cmn.power_runtime import (
    EpochLedger,
    PowerRuntime,
)


class EpochTest(unittest.TestCase):
    def test_timing_start_publishes_native_temperature_once(self):
        runtime = object.__new__(PowerRuntime)
        runtime.active = False
        runtime.interval = 100
        runtime.reset = lambda: None
        values = {f"core{i}": 60 + i for i in range(8)}
        values.update(dsu0=64, dsu1=60)
        runtime.temperatures = lambda: values
        updates = []
        service = types.SimpleNamespace(
            setTemperatureMilliC=lambda c, t: updates.append((c, t))
        )
        runtime.system = types.SimpleNamespace(
            cpu=[None] * 8,
            platform=types.SimpleNamespace(scmi=service),
        )
        with patch.dict(
            sys.modules, {"m5": types.SimpleNamespace(curTick=lambda: 200)}
        ):
            runtime.start()
            runtime.start()
        self.assertEqual(updates, [(0, 64000), (1, 67000)])
        self.assertEqual(runtime.next_tick, 300)

    def test_zero_tick_sample_is_rejected(self):
        with patch.dict(sys.modules, {"m5": types.SimpleNamespace()}):
            for interval in (0, -1, 0.5):
                with self.assertRaisesRegex(ValueError, "one simulator tick"):
                    PowerRuntime(None, ".", {}, {}, interval, [], None)

    def test_duplicate_and_zero_length_dump(self):
        ledger = EpochLedger(100)
        self.assertIsNone(ledger.accept(100))
        self.assertEqual(ledger.accept(200), (100, 200, 0))
        self.assertIsNone(ledger.accept(200))
        ledger.reset(200)
        self.assertIsNone(ledger.accept(200))
        self.assertEqual(ledger.accept(250), (200, 250, 1))

    def test_no_overlap_or_time_travel(self):
        ledger = EpochLedger(100)
        with self.assertRaises(ValueError):
            ledger.accept(99)
        ledger.accept(200)
        with self.assertRaises(ValueError):
            ledger.accept(300)
        with self.assertRaises(ValueError):
            ledger.reset(300)

    def test_reset_captures_activity_before_dvfs(self):
        runtime = object.__new__(PowerRuntime)
        runtime.active = True
        runtime.ledger = EpochLedger(100)
        operations = []
        runtime.dump = lambda: operations.append("dump")
        runtime.original_reset = lambda: operations.append("reset")
        with patch.dict(
            sys.modules, {"m5": types.SimpleNamespace(curTick=lambda: 200)}
        ):
            runtime.reset()
        self.assertEqual(operations, ["dump", "reset"])
        self.assertEqual(runtime.ledger.begin, 200)

    def test_native_dvfs_dump_then_reset_has_one_epoch(self):
        runtime = object.__new__(PowerRuntime)
        runtime.active = True
        runtime.ledger = EpochLedger(100)
        self.assertEqual(runtime.ledger.accept(200), (100, 200, 0))
        operations = []
        runtime.dump = lambda: operations.append("dump")
        runtime.original_reset = lambda: operations.append("reset")
        with patch.dict(
            sys.modules, {"m5": types.SimpleNamespace(curTick=lambda: 200)}
        ):
            runtime.reset()
            runtime.reset()
        self.assertEqual(operations, ["reset", "reset"])
        self.assertEqual(runtime.ledger.epoch, 1)

    def test_atomic_boot_reset_excludes_energy(self):
        runtime = object.__new__(PowerRuntime)
        runtime.active = False
        runtime.ledger = EpochLedger(0)
        runtime.dump = lambda: self.fail("Atomic boot must not produce energy")
        runtime.original_reset = lambda: None
        with patch.dict(
            sys.modules, {"m5": types.SimpleNamespace(curTick=lambda: 200)}
        ):
            runtime.reset()
        self.assertEqual(runtime.ledger.epoch, 0)

    def test_restore_rebases_counters_without_recharging_energy(self):
        runtime = object.__new__(PowerRuntime)
        runtime.interval = 100
        runtime.original_reset = lambda: None
        runtime.write_summary = lambda: None
        state = dict(
            schema=1,
            active=True,
            epoch=4,
            next_tick=600,
            energy_j=2.5,
            last_state=None,
        )
        with patch.dict(
            sys.modules, {"m5": types.SimpleNamespace(curTick=lambda: 550)}
        ):
            runtime.restore(state)
        self.assertEqual(runtime.ledger.begin, 550)
        self.assertEqual(runtime.ledger.epoch, 4)
        self.assertEqual(runtime.energy_j, 2.5)
        self.assertEqual(runtime.next_tick, 600)
        state["next_tick"] = 550
        with patch.dict(
            sys.modules, {"m5": types.SimpleNamespace(curTick=lambda: 550)}
        ):
            with self.assertRaisesRegex(ValueError, "deadline"):
                runtime.restore(state)


if __name__ == "__main__":
    unittest.main()
