#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Resolve a selected native DRAM class independently; never instantiate/simulate."""
import argparse
import hashlib
import importlib
import json
import sys
from pathlib import Path

import m5
from m5.objects import (
    DRAMInterface,
    MemCtrl,
)
from m5.proxy import isproxy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "configs"))
sys.path.insert(0, str(ROOT / "configs/example/arm"))

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("profile", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
profile = json.loads(args.profile.read_text())
memory = profile["memory"]
module = memory["custom_module"] or "zena_css_models"
name = memory["custom_class"] or "LPDDR5X_8533_1x16_BG_BL32"
cls = getattr(importlib.import_module(module), name)
if not issubclass(cls, DRAMInterface):
    raise ValueError("selected class is not a native DRAMInterface")
m5.ticks.setGlobalFrequency("1THz")
m5.ticks.fixGlobalFrequency()
# Explicit board overrides are applied without importing the backend builder.
geometry = {}
if not memory["preserve_device_geometry"]:
    geometry = dict(
        ranks_per_channel=1,
        device_size=f"{memory['capacity_mib_total'] * 1024**2 // memory['channels'] // int(cls.devices_per_rank.value)}B",
    )
dram = cls(addr_mapping="RoRaBaCoCh", page_policy="close_adaptive", **geometry)
ctrl = MemCtrl(dram=dram, mem_sched_policy="frfcfs")


def parameters(obj):
    result = {}
    excluded = {
        "range",
        "dram",
        "nvm",
        "clk_domain",
        "eventq_index",
        "system",
        "power_state",
        "power_model",
    }
    for key in obj._params.keys():
        if key in excluded:
            continue
        value = getattr(obj, key)
        if isproxy(value):
            value = value.unproxy(obj)
        result[key] = value.config_value()
    return result


source_hashes = {}
for module in tuple(sys.modules.values()):
    filename = getattr(module, "__file__", None)
    if filename and Path(filename).is_file():
        path = Path(filename).resolve()
        source_hashes[str(path)] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()

args.output.write_text(
    json.dumps(
        {
            "selected_class": f"{memory['custom_module'] or 'zena_css_models'}.{name}",
            "source_hashes": source_hashes,
            "dram": parameters(dram),
            "controller": parameters(ctrl),
        },
        indent=2,
    )
    + "\n"
)
