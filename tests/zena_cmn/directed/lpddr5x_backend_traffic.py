# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Short native backend traffic: fixed finite supply, no CPU or silicon claim."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import m5
from m5.objects import (
    AddrRange,
    IOXBar,
    PyTrafficGen,
    Root,
    SrcClockDomain,
    System,
    VoltageDomain,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "configs"))
from zena_cmn.memory_backend import (
    build_memory_backend,
    resolved_memory_parameters,
)
from zena_cmn.parameters import load_parameters

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--channels", type=int, choices=(4, 8, 16), required=True)
parser.add_argument(
    "--interleave-bytes", type=int, choices=(64, 512, 1024), default=64
)
parser.add_argument(
    "--read-percent", type=int, choices=(0, 50, 100), default=100
)
parser.add_argument("--synthetic", action="store_true")
parser.add_argument("--hynix", action="store_true")
parser.add_argument(
    "--diagnostic",
    choices=("baseline", "mr4-half-interval", "queue16"),
    default="baseline",
)
parser.add_argument(
    "--drain-completion",
    action="store_true",
    help="stop finite supply and verify native drain after ROI",
)
args = parser.parse_args()
if args.synthetic and args.hynix:
    parser.error("--synthetic and --hynix are mutually exclusive")
if args.diagnostic != "baseline" and not args.hynix:
    parser.error("diagnostics require the explicit --hynix candidate")
overrides = {
    "memory.channels": args.channels,
    "memory.interleave_bytes": args.interleave_bytes,
}
if not args.synthetic:
    overrides.update(
        {
            "memory.custom_module": "zena_cmn.lpddr5x_documented",
            "memory.custom_class": "MicronY52Q_8533_1x16_BG_BL32_Partial",
            "memory.preserve_device_geometry": True,
        }
    )
if args.hynix:
    overrides.update(
        {
            "memory.custom_module": "zena_cmn.lpddr5x_hynix",
            "memory.custom_class": "HynixH58G56CK8BX146_8533_1x16_BG_BL32_Documented",
        }
    )
params = load_parameters(overrides=overrides)
system = System(
    mem_mode="timing", mem_ranges=[AddrRange(0x80000000, size="2GiB")]
)
system.voltage_domain = VoltageDomain()
system.clk_domain = SrcClockDomain(
    clock="2GHz", voltage_domain=system.voltage_domain
)
backend = build_memory_backend(system, params)
assert backend["address_validation"]["status"] == "PASS"
for controller in backend["controllers"]:
    if args.diagnostic == "mr4-half-interval":
        # General spec MR4=01011B, not this part's temperature qualification.
        controller.dram.tREFI = "1.953us"
    elif args.diagnostic == "queue16":
        controller.dram.read_buffer_size = 16
        controller.dram.write_buffer_size = 16
# Record effective overrides, not the factory's pre-diagnostic fingerprint.
effective_parameters = {
    "dram_class": backend["parameters"]["dram_class"],
    "dram": resolved_memory_parameters(backend["controllers"][0].dram),
    "controller": resolved_memory_parameters(backend["controllers"][0]),
}
effective_fingerprint = hashlib.sha256(
    json.dumps(effective_parameters, sort_keys=True).encode()
).hexdigest()
system.generators = [
    PyTrafficGen(max_outstanding_reqs=64) for _ in range(args.channels)
]
system.buses = [IOXBar(width=64) for _ in range(args.channels)]
for gen, bus, ctrl in zip(
    system.generators, system.buses, backend["controllers"]
):
    gen.port = bus.cpu_side_ports
    ctrl.port = bus.mem_side_ports
    if not args.synthetic:
        assert int(ctrl.dram.device_size) == (2 if args.hynix else 1) * 1024**3
        assert int(ctrl.dram.ranks_per_channel) == 1
system.system_port = system.buses[0].cpu_side_ports
root = Root(full_system=False, system=system)
m5.instantiate()


def traffic(gen, channel):
    yield gen.createStrided(
        30_000_000,
        0x80000000,
        0x84000000,
        channel * args.interleave_bytes,
        64,
        args.interleave_bytes,
        args.channels * args.interleave_bytes,
        2000,
        2000,
        args.read_percent,
        0,
    )
    yield gen.createIdle(3_000_000)
    yield gen.createExit(0)


for channel, generator in enumerate(system.generators):
    generator.start(traffic(generator, channel))
m5.simulate(10_000_000)
if args.drain_completion:
    m5.stats.dump()  # Preserve warm-up counters before reset.
m5.stats.reset()
start = int(m5.curTick())
m5.simulate(20_000_000)
end = int(m5.curTick())
assert end - start == 20_000_000
m5.stats.dump()
drain = None
if args.drain_completion:
    # BaseTrafficGen::drain stops new supply and resolves a pending retry.
    # MemCtrl::drain additionally empties queues and drains DRAM ranks.
    m5.drain()
    m5.stats.dump()
    drain = {
        "start_tick": end,
        "end_tick": int(m5.curTick()),
        "warmup_dump_index": 0,
        "roi_dump_index": 1,
        "completed_dump_index": 2,
        "native_drain_returned": True,
        "scope": "native controller queues and rank drain; not cache flush or silicon DQ completion",
    }
else:
    m5.simulate(2_000_000)
manifest = {
    "drain_completion": drain,
    "channels": args.channels,
    "interleave_bytes": args.interleave_bytes,
    "address_validation": backend["address_validation"],
    "read_percent": args.read_percent,
    "synthetic": args.synthetic,
    "hynix": args.hynix,
    "diagnostic": args.diagnostic,
    "roi_ticks": end - start,
    "tick_frequency": 10**12,
    "roi_seconds": (end - start) / 10**12,
    "request_bytes": 64,
    "period_ticks_per_generator": 2000,
    "outstanding_per_generator": 64,
    "factory_dram_fingerprint": backend["fingerprint"],
    "dram_fingerprint": effective_fingerprint,
    "parameters": effective_parameters,
    "part_provenance": backend["part_provenance"],
    "theoretical_raw_GBps": args.channels
    * 8
    * 2
    / float(backend["controllers"][0].dram.tCK)
    / 1e9,
    "exposed_rows_per_bank": (2 * 1024**3 // args.channels) // (2048 * 16),
    "qualification": "NATIVE_BACKEND_TRAFFIC_ONLY_NOT_PART_OR_CPU_CALIBRATION",
}
Path(m5.options.outdir, "traffic-manifest.json").write_text(
    json.dumps(manifest, indent=2) + "\n"
)
print("NATIVE_BACKEND_TRAFFIC_COMPLETED")
