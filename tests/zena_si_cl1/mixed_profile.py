# SPDX-License-Identifier: BSD-3-Clause
"""Negative native test: shared ArmISA metadata must reject mixed profiles."""

import argparse
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import m5
from m5.objects import Root

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--elf", type=Path, required=True)
parser.add_argument("--vmsa-first", action="store_true")
args = parser.parse_args()
config = Path(__file__).resolve().parents[2] / "configs/zena_si_cl1/run.py"
spec = importlib.util.spec_from_file_location("si_cl1", config)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
m5.ticks.setGlobalFrequency("1THz")
system = module.build(
    SimpleNamespace(
        elf=args.elf,
        cpu="atomic",
        clock="1GHz",
        sram_latency="20ns",
        tcm_latency="1ns",
    )
)
system.cpu[0 if args.vmsa_first else 3].isa[0].pmsa64 = False
root = Root(full_system=True, system=system)
m5.instantiate()
raise RuntimeError("mixed PMSA/VMSA profiles were not rejected")
