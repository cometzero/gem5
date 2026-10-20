# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Run using gem5.opt; instantiate the shared FUPool natively after tests."""
import sys
import unittest
from pathlib import Path

import m5
from m5.objects import (
    DefaultFUPool,
    Root,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "configs"))
from zena_cmn.a720ae_fu import create_execution_pool
from zena_cmn.cpu_profile import create_cpu
from zena_cmn.parameters import load_parameters


def operation_map(pool):
    return {
        str(op.opClass): (
            index,
            int(unit.count),
            int(op.opLat),
            bool(op.pipelined),
        )
        for index, unit in enumerate(pool.FUList)
        if int(unit.count)
        for op in unit.opList
    }


class CalibratedCpuTest(unittest.TestCase):
    def test_shared_documented_vector_pipes(self):
        pool = create_execution_pool()
        mapping = operation_map(pool)
        add, fma = (
            mapping[name] for name in ("SimdFloatAdd", "SimdFloatMultAcc")
        )
        self.assertEqual(add, (fma[0], 2, 2, True))
        self.assertEqual(fma[1:], (2, 4, True))
        self.assertEqual(mapping["FloatAdd"][0], add[0])
        self.assertEqual(mapping["FloatMultAcc"][0], add[0])
        self.assertEqual(mapping["SimdAdd"][0], add[0])
        operations = [
            str(op.opClass)
            for unit in pool.FUList
            if int(unit.count)
            for op in unit.opList
        ]
        self.assertEqual(len(operations), len(set(operations)))

    def test_preserves_coverage_and_unqualified_latencies(self):
        original = operation_map(DefaultFUPool())
        updated = operation_map(create_execution_pool())
        self.assertEqual(set(original), set(updated))
        for name in original:
            if name not in ("SimdFloatAdd", "SimdFloatMultAcc"):
                self.assertEqual(original[name][2:], updated[name][2:], name)
        for name in (
            "MemRead",
            "MemWrite",
            "IntAlu",
            "IntDiv",
            "Matrix",
            "SimdPredAlu",
        ):
            self.assertEqual(original[name][1:], updated[name][1:], name)

    def test_opt_in_does_not_change_generic_or_atomic(self):
        params = load_parameters()
        baseline = create_cpu(params, 0)
        params["cpu"]["execution_profile"] = "a720ae_documented"
        calibrated = create_cpu(params, 1)
        atomic = create_cpu(params, 0, atomic=True)
        self.assertEqual(atomic.type, "BaseAtomicSimpleCPU")
        self.assertEqual(
            operation_map(baseline.instQueues[0].fuPool),
            operation_map(DefaultFUPool()),
        )
        self.assertEqual(
            operation_map(calibrated.instQueues[0].fuPool)["SimdFloatMultAcc"][
                1:
            ],
            (2, 4, True),
        )
        self.assertEqual(int(calibrated.LQEntries), int(baseline.LQEntries))
        self.assertEqual(
            int(calibrated.numROBEntries), int(baseline.numROBEntries)
        )

    def test_unknown_profile_rejected(self):
        params = load_parameters()
        params["cpu"]["execution_profile"] = "unrecognized"
        with self.assertRaises(ValueError):
            create_cpu(params, 0)


if __name__ in ("__main__", "__m5_main__"):
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(CalibratedCpuTest)
    )
    if not result.wasSuccessful():
        raise SystemExit(1)
    root = Root(full_system=False)
    root.execution_pool = create_execution_pool()
    m5.instantiate()
    print("NATIVE_SHARED_FU_INSTANTIATE=PASS")
