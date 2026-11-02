# SPDX-License-Identifier: BSD-3-Clause
"""Bounded native CHI/Garnet framing regression, no transport overrides."""

import argparse
import hashlib
import json
import re
import sys
from decimal import (
    ROUND_HALF_UP,
    Decimal,
)
from pathlib import Path

import m5
from m5.objects import (
    AddrRange,
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


def period_ticks(clock):
    value, unit = re.fullmatch(r"([0-9.]+)(GHz|MHz|kHz|Hz)", clock).groups()
    scale = {"GHz": 10**9, "MHz": 10**6, "kHz": 10**3, "Hz": 1}[unit]
    return int(
        (Decimal(10**12) / (Decimal(value) * scale)).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--profile", type=Path)
args = parser.parse_args()
profile = args.profile or (
    Path(__file__).resolve().parents[3]
    / "configs/zena_cmn/profiles/a720ae_public_candidate.yaml"
)
# Preserve the original no-argument regression's disabled prefetcher.
overrides = {} if args.profile else {"cache.l2.prefetch": "off"}
params = load_parameters(profile, overrides=overrides)
if params["cmn"]["network"] != "garnet":
    parser.error("framing test requires Garnet")
if params["ruby"]["timing_dvm"] or params["npu"]["enabled"]:
    parser.error("non-CPU endpoints do not support timing DVM or NPU")
payload = params["cmn"]["data_channel_bytes"]
width = params["cmn"]["mesh"]["flit_bytes"]
line = params["cache"]["line_bytes"]
if line != 64 or line % payload:
    parser.error("64-byte requests require whole CHI data payload chunks")
out = Path(m5.options.outdir)
if any(
    (out / name).exists()
    for name in ("config.json", "config.ini", "fabric-result.json")
):
    parser.error("output contains existing simulation artifacts")
(out / "resolved-profile.json").write_text(json.dumps(params, indent=2) + "\n")
system = System(mem_mode="timing", mem_ranges=[AddrRange("2GiB")])
system.voltage_domain = VoltageDomain()
system.clk_domain = SrcClockDomain(
    clock=params["ruby"]["clock"], voltage_domain=system.voltage_domain
)
system.cpu_clk_domain = SrcClockDomain(
    clock=params["cpu"]["clock"], voltage_domain=system.voltage_domain
)
system.cache_line_size = line
system.endpoints = [SubSystem() for _ in range(params["cpu"]["cores"])]
ruby = build_ruby(system, params, system.endpoints, full_system=False)
# SubSystem has no clock parameter. Bind the CHI_RNF children that inherit
# an actual CPU's clock in run_fs; these endpoints do not execute instructions.
for endpoint in system.endpoints:
    for name in ("inst_sequencer", "data_sequencer", "l1i", "l1d", "l2"):
        getattr(endpoint, name).clk_domain = system.cpu_clk_domain
system.sys_port_proxy = RubyPortProxy(ruby_system=ruby)
system.system_port = system.sys_port_proxy.in_ports
system.generator = PyTrafficGen(
    max_outstanding_reqs=1, clk_domain=system.cpu_clk_domain
)
system.generator.port = system.endpoints[0].data_sequencer.in_ports
root = Root(full_system=False, system=system)
m5.instantiate()
system.generator.start(
    iter(
        [
            system.generator.createLinear(
                50000000, 0, 4096, 64, 500, 500, 100, 4096
            ),
            system.generator.createIdle(1000000),
            system.generator.createExit(0),
        ]
    )
)
event = m5.simulate(60000000)
assert "exit state" in event.getCause().lower(), event.getCause()
m5.stats.dump()
sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "scripts/zena_cmn")
)
from analyze_fabric import (
    contract,
    framing,
    objects,
)

config = json.loads((out / "config.json").read_text())
index = {obj["path"]: obj for obj in objects(config)}
expected = contract(config)
# CHI's generic control header is eight bytes, independent of data payload.
requested = {
    "payload_bytes": payload,
    "generic_header_bytes": 8,
    "flit_bytes": width,
    "data_flits_per_packet": (payload + 8 + width - 1) // width,
    "control_flits_per_packet": (8 + width - 1) // width,
    "data_packets_per_full_line": line // payload,
    "link_period_ticks": period_ticks(params["cmn"]["mesh"]["clock"]),
}
assert all(expected[key] == value for key, value in requested.items())
assert config["system"]["ruby"]["block_size_bytes"] == line
assert len(config["system"]["endpoints"]) == params["cpu"]["cores"]
periods = {}
for path, clock in (
    ("system.clk_domain", params["ruby"]["clock"]),
    ("system.cpu_clk_domain", params["cpu"]["clock"]),
    ("system.ruby.clk_domain", params["ruby"]["clock"]),
    ("system.ruby.network.clk_domain", params["cmn"]["mesh"]["clock"]),
):
    periods[path] = index[path]["clock"]
    assert periods[path] == [period_ticks(clock)], (path, periods[path])
bindings = {}
for obj in index.values():
    domain = None
    if obj.get("type") in ("CHI_Cache_Controller", "RubySequencer"):
        private = re.fullmatch(
            r"system\.endpoints\d+\.(l1i|l1d|l2|\w+_sequencer)",
            obj["path"],
        )
        domain = (
            "system.cpu_clk_domain" if private else "system.ruby.clk_domain"
        )
    elif obj.get("type") in ("MemCtrl", "DRAMInterface"):
        domain = "system.clk_domain"
    elif obj.get("type") == "CHI_Memory_Controller":
        domain = "system.ruby.clk_domain"
    elif obj.get("type") in ("GarnetRouter", "GarnetNetworkInterface"):
        domain = "system.ruby.network.clk_domain"
    if domain is not None:
        assert obj["clk_domain"] == domain, (obj["path"], obj["clk_domain"])
        bindings[obj["path"]] = domain
    if obj.get("type") in ("CHI_Cache_Controller", "CHI_Memory_Controller"):
        assert obj["data_channel_size"] == payload, obj["path"]
    if obj.get("type") in ("GarnetIntLink", "GarnetExtLink", "NetworkLink"):
        assert obj["width"] == width, (obj["path"], obj["width"])
stats = (out / "stats.txt").read_text()
counts = {}
for name in ("numPackets", "totalReads", "totalWrites", "bytesRead"):
    match = re.search(
        r"^system\.generator\." + name + r"\s+(\d+)", stats, re.M
    )
    assert match, f"missing generator statistic: {name}"
    counts[name] = int(match[1])
# totalReads increments only in recvTimingResp(), unlike numPackets.
assert counts == {
    "numPackets": 64,
    "totalReads": 64,
    "totalWrites": 0,
    "bytesRead": 4096,
}, counts
read_latency = re.search(
    r"^system\.generator\.totalReadLatency\s+(\d+)", stats, re.M
)
assert read_latency and int(read_latency[1]) > 0
frequencies = set(re.findall(r"^simFreq\s+(\d+)", stats, re.M))
assert frequencies == {"1000000000000"}, frequencies
result = {
    "status": "PASS",
    "scope": "native_CHI_Garnet_framing_not_CPU_or_silicon_timing",
    "profile": str(profile.resolve()),
    "profile_sha256": hashlib.sha256(profile.read_bytes()).hexdigest(),
    "profile_overrides": overrides,
    "resolved_profile_sha256": hashlib.sha256(
        (out / "resolved-profile.json").read_bytes()
    ).hexdigest(),
    "requested_dimensions": requested,
    "contract": expected,
    "instantiated_clock_period_ticks": periods,
    "instantiated_clock_bindings": bindings,
    "endpoint_mapping": "SubSystem children explicitly bound to CPU domain",
    "cpu_execution": "UNSUPPORTED_no_instruction_executing_CPU",
    "transport_overrides": False,
    "generator_counts": counts,
    "request_completion": "64 recvTimingResp reads, 4096 bytes, zero writes",
    "total_read_latency_ticks": int(read_latency[1]),
    "mean_read_latency_ticks": int(read_latency[1]) / counts["totalReads"],
    "finalTick": int(m5.curTick()),
    "simFreq": int(next(iter(frequencies))),
    "final_tick_scope": "includes fixed 50us generator state and 1us idle",
    "framing": framing(stats, expected),
    "exit": event.getCause(),
    "limitations": [
        "one active endpoint with one outstanding read; no saturation",
        "no Linux, CPU instructions, physical CDC or silicon validation",
        "split-clock transport progress is not calibrated bridge timing",
    ],
}
(out / "fabric-result.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
