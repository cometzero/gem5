# SPDX-License-Identifier: BSD-3-Clause
"""Short, cache-free LPDDR bandwidth experiment; no silicon calibration.

Unlike lpddr_traffic.py this measures pure read/write ROI windows. Native
MemTest and Linux correctness checks remain separate from this experiment.
"""

import argparse
import json
import math
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

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "configs/example/arm")
)
import zena_css_models as models

parser = argparse.ArgumentParser(description=__doc__)
models.add_options(parser)
models.add_block_options(parser)
parser.add_argument("--direction", choices=("read", "write"), default="read")
parser.add_argument("--warmup-us", type=models.positive_int, default=20)
parser.add_argument("--roi-us", type=models.positive_int, default=100)
parser.add_argument("--generators", type=int, choices=(16,), default=16)
parser.add_argument("--outstanding", type=models.positive_int, default=64)
parser.add_argument("--period-ps", type=models.positive_int, default=2000)
parser.set_defaults(
    cpus=4,
    cpu_clock="2GHz",
    cpu_mode="timing",
    roi_cpu_mode=None,
    io_profile="minimal",
    memory_model="lpddr5x-8533",
    memory_channels=4,
)
args = parser.parse_args()
if args.memory_model not in ("lpddr5-6400", "lpddr5x-8533"):
    parser.error("this experiment requires an LPDDR model")
model = models.settings(args)
base, capacity = 0x80000000, 2 * 1024**3
system = System(mem_mode="timing", mem_ranges=[AddrRange(base, size=capacity)])
system.voltage_domain = VoltageDomain()
system.clk_domain = SrcClockDomain(
    clock=model["fabric_clock"], voltage_domain=system.voltage_domain
)
system.membus = IOXBar()
system.system_port = system.membus.cpu_side_ports
models.configure_memory(system, model)
assert sum(c.dram.range.size() for c in system.mem_ctrls) == capacity
for controller in system.mem_ctrls:
    d = controller.dram
    assert d.range.size() == (
        int(d.device_size.value)
        * int(d.devices_per_rank.value)
        * int(d.ranks_per_channel.value)
    )
system.generators = [
    PyTrafficGen(max_outstanding_reqs=args.outstanding, elastic_req=True)
    for _ in range(args.generators)
]
for generator in system.generators:
    generator.port = system.membus.cpu_side_ports
root = Root(full_system=False, system=system)
m5.instantiate()
warmup = args.warmup_us * 1_000_000
roi = args.roi_us * 1_000_000
page = int(system.mem_ctrls[0].dram.device_rowbuffer_size.value)
region = capacity // args.generators
streams = []
for index, generator in enumerate(system.generators):
    # Disjoint 128 MiB regions avoid duplicate requests/write merging.
    # RoRaBaCoCh: stagger the initial bank across all 16 banks. Each
    # sequential stream still traverses all channels with 64-byte striping.
    start = base + index * region + index * page * args.memory_channels
    end = base + (index + 1) * region
    streams.append({"start": start, "end": end})
    generator.start(
        [
            generator.createLinear(
                warmup + roi + 1,
                start,
                end,
                64,
                args.period_ps,
                args.period_ps,
                100 if args.direction == "read" else 0,
                0,
            ),
            generator.createIdle(1_000_000_000),
        ]
    )


def simulate(ticks):
    event = m5.simulate(ticks)
    if event.getCause() != "simulate() limit reached":
        raise RuntimeError(event.getCause())


def counters():
    m5.stats.dump()
    body = (
        Path(m5.options.outdir, "stats.txt")
        .read_text()
        .split("Begin Simulation Statistics")[-1]
    )
    values = {}
    for line in body.splitlines():
        fields = line.split()
        if len(fields) > 1:
            try:
                values[fields[0]] = float(fields[1])
            except ValueError:
                pass
    channels = []
    for index in range(args.memory_channels):
        prefix = f"system.mem_ctrls{index}.dram"
        read = values[prefix + ".dramBytesRead"]
        written = values[prefix + ".dramBytesWritten"]
        assert math.isfinite(read + written) and read + written > 0
        assert (written if args.direction == "read" else read) == 0
        bank_stat = (
            "perBankRdBursts"
            if args.direction == "read"
            else "perBankWrBursts"
        )
        banks = [values[f"{prefix}.{bank_stat}::{b}"] for b in range(16)]
        assert all(b > 0 for b in banks)
        channels.append(
            {
                "channel": index,
                "read_bytes": read,
                "write_bytes": written,
                "bank_bursts": banks,
            }
        )
    generators = []
    for index in range(args.generators):
        prefix = f"system.generators{index}"
        generators.append(
            {
                name: values[f"{prefix}.{name}"]
                for name in (
                    "numPackets",
                    "numRetries",
                    "retryTicks",
                    "bytesRead",
                    "bytesWritten",
                )
            }
        )
    return {
        "tick": m5.curTick(),
        "channels": channels,
        "generators": generators,
    }


simulate(warmup)
m5.stats.reset()
simulate(roi // 2)
first = counters()
simulate(roi - roi // 2)
full = counters()
# Preserve the ROI before draining outstanding work; drain bytes are excluded.
m5.drain()
Path(m5.options.outdir, "bandwidth-result.json").write_text(
    json.dumps(
        {
            "result": "PASS",
            "qualification": "MODEL_ONLY",
            "model": model,
            "fixture": "cache-free IOXBar and fixed traffic generators; no CPUs",
            "parameters": vars(args),
            "streams": streams,
            "roi_start_tick": warmup,
            "roi_ticks": roi,
            "tick_hz": m5.ticks.fromSeconds(1.0),
            "first_half": first,
            "full_roi": full,
            "drained_tick": m5.curTick(),
            "offered_bytes_per_second": args.generators
            * 64
            * 1e12
            / args.period_ps,
        },
        indent=2,
    )
    + "\n"
)
