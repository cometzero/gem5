# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Documented vector timings on two shared, otherwise approximate FP/SIMD FUs.

Arm A720AE SOG r0p0 Issue 2.0, sections 3.14/3.17: FADD latency 2,
FMLA general-result latency 4, throughput 2/cycle. Accumulator-specific
2-cycle forwarding is not represented by native OpDesc. Other instructions
in these broad gem5 opClasses inherit the same timing, without calibration.
Scalar FP and other SIMD share these pipes with their native latencies;
that sharing is an assumption. Integer, predicate, matrix and LSU FUs retain
native mappings, so this is not a complete A720AE execution-pipeline model.
"""

from m5.objects import (
    FP_ALU,
    DefaultFUPool,
    FP_MultDiv,
    FUDesc,
    FUPool,
    OpDesc,
    SIMD_Unit,
)

DOCUMENTED_VECTOR_LATENCIES = {
    "SimdFloatAdd": 2,
    "SimdFloatMultAcc": 4,
}


def create_execution_pool():
    pool = DefaultFUPool()
    shared_operations = []
    other_units = []
    for unit in pool.FUList:
        operations = [
            OpDesc(opClass=op.opClass, opLat=op.opLat, pipelined=op.pipelined)
            for op in unit.opList
        ]
        if isinstance(unit, (FP_ALU, FP_MultDiv, SIMD_Unit)):
            for operation in operations:
                latency = DOCUMENTED_VECTOR_LATENCIES.get(
                    str(operation.opClass)
                )
                if latency is not None:
                    operation.opLat = latency
                    operation.pipelined = True
            shared_operations.extend(operations)
        else:
            other_units.append(FUDesc(count=unit.count, opList=operations))
    names = [str(op.opClass) for op in shared_operations]
    if len(names) != len(set(names)) or not set(
        DOCUMENTED_VECTOR_LATENCIES
    ) <= set(names):
        raise ValueError(
            "native FP/SIMD mapping changed; review calibrated pool"
        )
    return FUPool(
        FUList=other_units + [FUDesc(count=2, opList=shared_operations)]
    )
