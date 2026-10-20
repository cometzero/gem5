# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Deterministic, byte-checked hot-set displacement by a short stream."""

import argparse
import hashlib
import json
import random
import re
import sys
from pathlib import Path

import m5
from m5.objects import (
    AddrRange,
    MemChecker,
    MemCheckerMonitor,
    PyTrafficGen,
    Root,
    RubyPortProxy,
    SrcClockDomain,
    SubSystem,
    System,
    VoltageDomain,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "configs"))
from zena_cmn.parameters import load_parameters
from zena_cmn.ruby_system_builder import build_ruby

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "scripts/zena_cmn")
)
from summarize_roi import stats_blocks


def varint(value):
    encoded = bytearray()
    while value >= 128:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def message(fields):
    # packet.proto's integer-only fields use protobuf wire type zero.
    payload = b"".join(
        varint(key << 3) + varint(value) for key, value in fields
    )
    return varint(len(payload)) + payload


def write_trace(path, addresses, command, period, burst_gap=0, burst_lines=8):
    # Native protoio magic, PacketHeader.tick_freq, and Packet fields
    # tick/cmd/addr/size. MemCmd ReadReq=1 and WriteReq=4 in packet.hh.
    data = b"gem5" + message([(3, 10**12)])
    data += b"".join(
        message(
            [
                (1, (i + 1) * period + (i // burst_lines) * burst_gap),
                (2, command),
                (3, addr),
                (4, 64),
            ]
        )
        for i, addr in enumerate(addresses)
    )
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def snapshot(path, prefetch):
    values = stats_blocks(path.read_text())[-1]
    prefix = "system.endpoints0.l2."
    wanted = {
        "l1d_hits": "system.endpoints0.l1d.cache.m_demand_hits",
        "l1d_misses": "system.endpoints0.l1d.cache.m_demand_misses",
        "l2_hits": prefix + "cache.m_demand_hits",
        "l2_misses": prefix + "cache.m_demand_misses",
        "demand_reads": "system.generator.totalReads",
        "demand_writes": "system.generator.totalWrites",
        "read_bytes": "system.generator.bytesRead",
        "write_bytes": "system.generator.bytesWritten",
        "read_latency_ticks": "system.generator.avgReadLatency",
        "retries": "system.generator.numRetries",
        "retry_ticks": "system.generator.retryTicks",
    }
    # Missing primary counters must fail instead of looking like zero traffic.
    result = {name: values[key] for name, key in wanted.items()}
    assert all(
        value is not None
        or (name == "read_latency_ticks" and not result["demand_reads"])
        for name, value in result.items()
    ), "undefined primary statistic"
    pf_prefix = prefix + "prefetcher."
    result["prefetch_stats_present"] = prefetch == "stride"
    if prefetch == "stride":
        result["pf_issued"] = values[pf_prefix + "pfIssued"]
        result["pf_useful"] = values[pf_prefix + "pfUseful"]
        # Native pfUnused uses nozero; absence is valid only after verifying
        # that the active prefetcher's mandatory counters exist above.
        result["pf_unused"] = values.get(pf_prefix + "pfUnused", 0)
    else:
        assert not any(key.startswith(pf_prefix) for key in values)
        result.update(pf_issued=0, pf_useful=0, pf_unused=0)
    for direction in ("Read", "Written"):
        counters = {
            int(match[1]): value
            for key, value in values.items()
            if (
                match := re.fullmatch(
                    r"system\.mem_ctrls(\d+)\.dram\.dramBytes" + direction, key
                )
            )
        }
        assert set(counters) == set(
            range(16)
        ), "missing DRAM controller counters"
        result["dram_bytes_" + direction.lower()] = sum(counters.values())
    result["l2_downstream_read_transactions"] = sum(
        value
        for key, value in values.items()
        if key.startswith(prefix + "outTransLatHist.SendRead")
        and key.endswith("::samples")
    )
    return result


parser = argparse.ArgumentParser()
parser.add_argument("--prefetch", choices=("off", "stride"), required=True)
parser.add_argument("--burst-gap-ticks", type=int, default=500000)
parser.add_argument("--burst-lines", type=int, default=7)
parser.add_argument(
    "--profile",
    default=str(
        Path(__file__).resolve().parents[3]
        / "configs/zena_cmn/profiles/a720ae_public_candidate.yaml"
    ),
)
args = parser.parse_args()
assert 1 <= args.burst_lines <= 64
assert args.burst_gap_ticks >= 0
params = load_parameters(args.profile)
params["cpu"].update(cores=4, clusters=1)
params["cache"]["dsu_l3"]["instances"] = 1
params["cache"]["l2"].update(prefetch=args.prefetch, policy="exclusive_data")
params["cmn"]["mesh"]["cluster_attach"] = [5]
system = System(mem_mode="timing", mem_ranges=[AddrRange("2GiB")])
system.voltage_domain = VoltageDomain()
system.clk_domain = SrcClockDomain(
    clock="2GHz", voltage_domain=system.voltage_domain
)
system.endpoints = [SubSystem() for _ in range(4)]
ruby = build_ruby(system, params, system.endpoints, full_system=False)
system.sys_port_proxy = RubyPortProxy(ruby_system=ruby)
system.system_port = system.sys_port_proxy.in_ports
system.generator = PyTrafficGen()
system.checker = MemChecker()
system.monitor = MemCheckerMonitor(memchecker=system.checker, warn_only=False)
system.generator.port = system.monitor.cpu_side_port
system.monitor.mem_side_port = system.endpoints[0].data_sequencer.in_ports
root = Root(full_system=False, system=system)

hot = list(range(0, 480 * 1024, 64))
random.Random(2026).shuffle(hot)
stream = [
    512 * 1024 + page * 4096 + line * 64
    for page in range(192)
    for line in range(args.burst_lines)
]
phases = [
    ("initialize", hot + stream, 4),
    ("warm", hot * 2, 1),
    ("interference", stream, 1),
    ("probe", hot, 1),
]
period = 20000
outdir = Path(m5.options.outdir)
m5.instantiate()
states = []
metadata = []
for name, addresses, command in phases:
    trace = outdir / (name + ".trace")
    gap = args.burst_gap_ticks if name == "interference" else 0
    digest = write_trace(
        trace, addresses, command, period, gap, args.burst_lines
    )
    duration = (
        len(addresses) * period
        + ((len(addresses) - 1) // args.burst_lines) * gap
        + 10000000
    )
    states.append(system.generator.createTrace(duration, str(trace)))
    metadata.append(
        {
            "name": name,
            "requests": len(addresses),
            "command": command,
            "duration_ticks": duration,
            "burst_gap_ticks": gap,
            "burst_lines": args.burst_lines,
            "trace_sha256": digest,
        }
    )
states.extend(
    [system.generator.createIdle(10000000), system.generator.createExit(0)]
)
system.generator.start(iter(states))
results = []
for info in metadata:
    m5.stats.reset()
    event = m5.simulate(info["duration_ticks"])
    assert event.getCause() == "simulate() limit reached", event.getCause()
    m5.stats.dump()
    counters = snapshot(outdir / "stats.txt", args.prefetch)
    completed = counters[
        "demand_reads" if info["command"] == 1 else "demand_writes"
    ]
    assert completed == info["requests"], (
        info["name"],
        completed,
        info["requests"],
    )
    results.append({**info, **counters})
event = m5.simulate(20000000)
assert "exit state" in event.getCause().lower(), event.getCause()
if args.prefetch == "stride":
    assert (
        results[2]["pf_issued"] > 0
    ), "interference did not activate prefetch"
result = {
    "status": "PASS",
    "prefetch": args.prefetch,
    "profile": str(Path(args.profile).resolve()),
    "policy": "exclusive_data",
    "scope": "single cluster; four endpoints; one active data requestor",
    "hot_bytes": len(hot) * 64,
    "stream_demand_bytes": len(stream) * 64,
    "data_check": "MemCheckerMonitor; initialized requestor-ID byte pattern; warn_only=false",
    "phases": results,
    "ticks": m5.curTick(),
}
(outdir / "pollution-result.json").write_text(
    json.dumps(result, indent=2, allow_nan=False) + "\n"
)
print(json.dumps(result, allow_nan=False))
