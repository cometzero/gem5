# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Run with gem5: reject the LQ64 burst-overflow profile before simulation."""

import json
import sys
from pathlib import Path

import m5

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "configs"))
from zena_cmn.cpu_profile import create_cpu
from zena_cmn.parameters import load_parameters

params = load_parameters()
assert params["cpu"]["lq_entries"] == 32
cpu = create_cpu(params, 0)
assert int(cpu.LQEntries) == 32
capacity = (int(cpu.forwardComSize) + 1) * int(cpu.wbWidth)
assert capacity == 48
params["cpu"]["lq_entries"] = 64
try:
    create_cpu(params, 0)
except ValueError as error:
    assert "LQEntries=64 exceeds native IEW writeback window=48" in str(error)
else:
    raise AssertionError("unsafe LQ64 profile was accepted")
result = {
    "status": "PASS",
    "default_lq": 32,
    "native_writeback_slots": capacity,
    "rejected_lq": 64,
    "simulation_executed": False,
}
Path(m5.options.outdir, "guard-result.json").write_text(
    json.dumps(result, indent=2) + "\n"
)
print(json.dumps(result))
