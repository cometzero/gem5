# SPDX-License-Identifier: BSD-3-Clause
"""Fixed DRAM reference and NPU-facing fabric supply experiments.

The direct/fanout cases use identical per-channel streams. The single-port
case merges those address sequences into one sequential source, as a DMA
would, but does not claim identical packet timestamps or mixed-R/W order.
"""

import argparse
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
from zena_cmn.memory_backend import build_memory_backend
from zena_cmn.parameters import load_parameters


def positive(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--path", choices=("direct", "fanout", "single"), required=True
)
parser.add_argument(
    "--read-percent", type=int, choices=(0, 50, 100), required=True
)
parser.add_argument("--fabric-width", type=positive, default=32)
parser.add_argument("--fabric-clock", default="2GHz")
parser.add_argument("--outstanding", type=positive, default=1024)
parser.add_argument("--warmup-us", type=positive, default=10)
parser.add_argument("--roi-us", type=positive, default=40)
args = parser.parse_args()
if args.outstanding % 16 or args.fabric_width not in (32, 64, 128, 256, 512):
    parser.error(
        "outstanding must divide by 16; width must be 32..512 power of two"
    )

profile = (
    Path(__file__).resolve().parents[3]
    / "configs/zena_cmn/profiles/a720ae_npu_v01_async.yaml"
)
params = load_parameters(profile)
if args.path == "direct":
    params["npu"]["enabled"] = False
    params["scope"]["npu_present"] = False
    params["memory"]["create_future_npu_arbiter_now"] = False
else:
    params["npu"]["fabric_width"] = args.fabric_width

system = System(
    mem_mode="timing", mem_ranges=[AddrRange(0x80000000, size="2GiB")]
)
system.voltage_domain = VoltageDomain()
system.clk_domain = SrcClockDomain(
    clock="2GHz", voltage_domain=system.voltage_domain
)
backend = build_memory_backend(system, params)
count = 1 if args.path == "single" else 16
system.generators = [
    PyTrafficGen(
        max_outstanding_reqs=args.outstanding // count, elastic_req=True
    )
    for _ in range(count)
]
if args.path == "direct":
    system.buses = [IOXBar(width=64) for _ in range(count)]
    for gen, bus, ctrl in zip(
        system.generators, system.buses, system.mem_ctrls
    ):
        gen.port = bus.cpu_side_ports
        ctrl.port = bus.mem_side_ports
    system.system_port = system.buses[0].cpu_side_ports
else:
    system.fabric_clock = SrcClockDomain(
        clock=args.fabric_clock, voltage_domain=system.voltage_domain
    )
    system.memory_bus.clk_domain = system.fabric_clock
    for gen in system.generators:
        gen.port = system.memory_bus.cpu_side_ports
    system.system_port = system.memory_bus.cpu_side_ports

root = Root(full_system=False, system=system)
m5.instantiate()
out = Path(m5.options.outdir)
warmup, roi = args.warmup_us * 1_000_000, args.roi_us * 1_000_000
start_addr, end_addr = 0x81000000, 0x85000000
for index, gen in enumerate(system.generators):
    gen.start(
        iter(
            [
                gen.createStrided(
                    warmup + roi + 1,
                    start_addr,
                    end_addr,
                    index * 64,
                    64,
                    64,
                    count * 64,
                    125 * count,
                    125 * count,
                    args.read_percent,
                    0,
                ),
                gen.createIdle(1_000_000_000),
            ]
        )
    )


def simulate(ticks):
    event = m5.simulate(ticks)
    if event.getCause() != "simulate() limit reached":
        raise RuntimeError(event.getCause())


def counters():
    m5.stats.dump()
    body = (
        (out / "stats.txt")
        .read_text()
        .split("Begin Simulation Statistics")[-1]
    )
    result = {}
    for line in body.splitlines():
        fields = line.split()
        if len(fields) > 1:
            try:
                result[fields[0]] = float(fields[1])
            except ValueError:
                pass
    return result


simulate(warmup)
before = counters()
begin = int(m5.curTick())
simulate(roi)
after = counters()
finish = int(m5.curTick())


def delta(name):
    return after[name] - before[name]


def generator_prefix(index):
    return "system.generators" + (str(index) if count > 1 else "") + "."


channels = []
for channel in range(16):
    prefix = f"system.mem_ctrls{channel}.dram."
    rd, wr = (
        delta(prefix + name) for name in ("dramBytesRead", "dramBytesWritten")
    )
    assert rd + wr > 0
    assert args.read_percent != 100 or wr == 0
    assert args.read_percent != 0 or rd == 0
    channels.append({"channel": channel, "read_bytes": rd, "write_bytes": wr})
dram_read = sum(c["read_bytes"] for c in channels)
dram_write = sum(c["write_bytes"] for c in channels)
source_read = sum(
    delta(generator_prefix(i) + "bytesRead") for i in range(count)
)
source_write = sum(
    delta(generator_prefix(i) + "bytesWritten") for i in range(count)
)
m5.drain()
completed = counters()
for i in range(count):
    prefix = generator_prefix(i)
    assert completed[prefix + "numSuppressed"] == 0
    assert completed[prefix + "numPackets"] == (
        completed[prefix + "totalReads"] + completed[prefix + "totalWrites"]
    ), "generated requests did not complete after drain"
result = {
    "status": "PASS",
    "qualification": "NATIVE_TRAFFIC_ENVELOPE_NOT_LINUX_OR_SILICON",
    "path": args.path,
    "read_percent": args.read_percent,
    "fabric_width": args.fabric_width if args.path != "direct" else 64,
    "fabric_clock": args.fabric_clock if args.path != "direct" else "2GHz",
    "generators": count,
    "outstanding_total": args.outstanding,
    "offered_GBps": 512,
    "address_start": start_addr,
    "address_end": end_addr,
    "address_pattern": "64B sequential per channel; merged sequential for single",
    "warmup_us": args.warmup_us,
    "roi_us": (finish - begin) / 1_000_000,
    "dram_read_GBps": dram_read / (finish - begin) * 1000,
    "dram_write_GBps": dram_write / (finish - begin) * 1000,
    "dram_total_GBps": (dram_read + dram_write) / (finish - begin) * 1000,
    "source_response_GBps": (source_read + source_write)
    / (finish - begin)
    * 1000,
    "dram_fingerprint": backend["fingerprint"],
    "dram_parameters": backend["parameters"],
    "channels": channels,
    "source_retries": sum(
        delta(generator_prefix(i) + "numRetries") for i in range(count)
    ),
    "drain_ticks": int(m5.curTick()) - finish,
    "all_requests_completed": True,
    "limits": [
        "Synthetic payload is not byte-checked",
        "Write response precedes DRAM service; reported rate uses DRAM counters",
        "No CPU/CHI or SMMU in this reference",
    ],
}
(out / "saturation-result.json").write_text(
    json.dumps(result, indent=2) + "\n"
)
print(
    json.dumps(
        {
            key: result[key]
            for key in ("status", "path", "read_percent", "dram_total_GBps")
        }
    )
)
