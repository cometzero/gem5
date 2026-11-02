# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Native Garnet network statistics epoch and repeated-dump regression.

Run separately with --ruby-clock 2GHz and --mesh-clock 1GHz, 2GHz, 4GHz.
Each invocation uses a fresh output directory and the existing CHI builder.
This checks network link/VC counters, not Router buffer-counter aggregation,
CPU/Linux timing, physical CDC, or runtime DVFS transitions.
"""

import argparse
import hashlib
import json
import math
import re
import sys
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

SOURCE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SOURCE / "configs"))
from zena_cmn.parameters import load_parameters
from zena_cmn.ruby_system_builder import build_ruby


def require(condition, message):
    if not condition:
        raise ValueError(message)


def clock_period(clock):
    return {"1GHz": 1000, "2GHz": 500, "4GHz": 250}[clock]


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--profile", type=Path)
parser.add_argument(
    "--ruby-clock", choices=("1GHz", "2GHz", "4GHz"), default="2GHz"
)
parser.add_argument(
    "--mesh-clock", choices=("1GHz", "2GHz", "4GHz"), default="1GHz"
)
args = parser.parse_args()
profile = args.profile or (
    SOURCE / "configs/zena_cmn/profiles/a720ae_public_candidate.yaml"
)
overrides = {
    "ruby.clock": args.ruby_clock,
    "cmn.mesh.clock": args.mesh_clock,
    "cache.l2.prefetch": "off",
}
params = load_parameters(profile, overrides=overrides)
if params["cmn"]["network"] != "garnet":
    parser.error("this regression requires Garnet")
if params["ruby"]["timing_dvm"] or params["npu"]["enabled"]:
    parser.error("non-CPU endpoints require timing DVM and NPU disabled")
if params["cache"]["line_bytes"] != 64:
    parser.error("this bounded traffic fixture requires 64-byte cache lines")

out = Path(m5.options.outdir)
for name in (
    "config.json",
    "config.ini",
    "resolved-profile.json",
    "garnet-stats-result.json",
):
    if (out / name).exists():
        parser.error("output contains existing artifacts: " + name)
with (out / "resolved-profile.json").open("x") as stream:
    json.dump(params, stream, indent=2)
    stream.write("\n")

system = System(mem_mode="timing", mem_ranges=[AddrRange("2GiB")])
system.voltage_domain = VoltageDomain()
system.clk_domain = SrcClockDomain(
    clock=args.ruby_clock, voltage_domain=system.voltage_domain
)
system.cpu_clk_domain = SrcClockDomain(
    clock=params["cpu"]["clock"], voltage_domain=system.voltage_domain
)
system.cache_line_size = 64
system.endpoints = [SubSystem() for _ in range(params["cpu"]["cores"])]
ruby = build_ruby(system, params, system.endpoints, full_system=False)
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

config = json.loads((out / "config.json").read_text())
network_config = config["system"]["ruby"]["network"]
require(
    network_config["clk_domain"]["clock"] == [clock_period(args.mesh_clock)],
    "mesh clock was not instantiated",
)
require(
    config["system"]["ruby"]["clk_domain"]["clock"]
    == [clock_period(args.ruby_clock)],
    "Ruby clock was not instantiated",
)


def run_reads(start):
    system.generator.start(
        iter(
            [
                system.generator.createLinear(
                    50000000, start, start + 4096, 64, 500, 500, 100, 4096
                ),
                system.generator.createIdle(1000000),
                system.generator.createExit(0),
            ]
        )
    )
    event = m5.simulate(60000000)
    require("exit state" in event.getCause().lower(), event.getCause())


def idle(ticks):
    event = m5.simulate(ticks)
    require("limit" in event.getCause().lower(), event.getCause())


dump_count = 0
prefix = "system.ruby.network."
raw_names = (
    "ext_in_link_utilization",
    "ext_out_link_utilization",
    "int_link_utilization",
)


def dump_window(label):
    global dump_count
    m5.stats.dump()
    blocks = re.findall(
        r"Begin Simulation Statistics -+\n(.*?)"
        r"-+ End Simulation Statistics",
        (out / "stats.txt").read_text(),
        re.S,
    )
    require(len(blocks) == dump_count + 1, "dump did not append one block")
    dump_count += 1
    text = blocks[-1]

    def scalar(name, default=None):
        rows = re.findall(r"^" + re.escape(name) + r"\s+(\S+)", text, re.M)
        if not rows and default is not None:
            return default
        require(len(rows) == 1, "missing/duplicate statistic: " + name)
        value = float(rows[0])
        require(
            math.isfinite(value) and value >= 0, "invalid statistic: " + name
        )
        return value

    raw = {name: scalar(prefix + name) for name in raw_names}
    vector = re.findall(
        r"^" + re.escape(prefix + "avg_vc_load") + r"\s+\|(.*)$", text, re.M
    )
    require(len(vector) <= 1, "duplicate VC-load vector")
    # nozero suppresses a zero-valued vector and its total.
    loads = (
        [float(item.split()[0]) for item in vector[0].split("|")]
        if vector
        else []
    )
    require(
        all(math.isfinite(value) and value >= 0 for value in loads),
        "nonfinite/negative VC load",
    )
    require(scalar("simFreq") == 10**12, "unexpected tick frequency")
    return dict(
        label=label,
        tick=int(m5.curTick()),
        raw=raw,
        average=scalar(prefix + "avg_link_utilization"),
        vc_load=loads,
        vc_total=scalar(prefix + "avg_vc_load::total", default=0),
        reads=scalar("system.generator.totalReads"),
        writes=scalar("system.generator.totalWrites"),
        bytes_read=scalar("system.generator.bytesRead"),
    )


# Nonzero warmup traffic makes resetting the link counters observable.
run_reads(0)
warmup = dump_window("warmup")
require(
    warmup["reads"] == 64 and warmup["bytes_read"] == 4096,
    "warmup requests did not complete",
)
require(sum(warmup["raw"].values()) > 0, "warmup had no network traffic")
idle(10**9 - int(m5.curTick()))
reset_tick = int(m5.curTick())
m5.stats.reset()
zero = dump_window("zero_elapsed_after_reset")
require(
    all(value == 0 for value in zero["raw"].values())
    and zero["average"] == zero["vc_total"] == zero["reads"] == 0
    and all(value == 0 for value in zero["vc_load"]),
    "reset/zero-window statistics are not finite zero",
)

# Fresh addresses avoid warming the measured lines in the first phase.
run_reads(4096)
windows = [dump_window("measured")]
for cycles in (1, 1000):
    # m5.stats suppresses same-tick global dumps. Advance idle network cycles
    # so collateStats runs again, with no reset and no new network traffic.
    idle(cycles * clock_period(args.mesh_clock))
    windows.append(dump_window("repeated_after_" + str(cycles) + "_cycles"))

period = clock_period(args.mesh_clock)
for window in windows:
    require(
        window["reads"] == 64
        and window["writes"] == 0
        and window["bytes_read"] == 4096,
        "measured reads did not complete",
    )
    require(
        window["raw"] == windows[0]["raw"],
        "repeated dump accumulated link counts or traffic did not drain",
    )
    elapsed = (window["tick"] + period - 1) // period - (
        reset_tick + period - 1
    ) // period
    require(elapsed > 0, "measured window has no network cycles")
    expected = sum(window["raw"].values()) / elapsed
    require(expected > 0, "measured window has no link activity")
    # Text statistics are printed to six decimal places.
    for observed in (window["average"], window["vc_total"]):
        require(
            math.isclose(observed, expected, rel_tol=0, abs_tol=0.500001e-6),
            "link/VC average uses wrong epoch or accumulates on dump",
        )
    require(
        len(window["vc_load"]) == 4 * params["cmn"]["mesh"]["vcs_per_vnet"],
        "missing measured VC-load entries",
    )
    window["elapsed_network_cycles"] = elapsed
    window["expected_average"] = expected
    baseline = windows[0]
    scale = baseline["elapsed_network_cycles"] / elapsed
    for first, current in zip(baseline["vc_load"], window["vc_load"]):
        require(
            math.isclose(
                current, first * scale, rel_tol=0, abs_tol=1.000001e-6
            ),
            "repeated dump accumulated a VC load",
        )

result = dict(
    status="PASS",
    scope="native_network_stats_only",
    ruby_clock=args.ruby_clock,
    mesh_clock=args.mesh_clock,
    profile=str(profile.resolve()),
    profile_overrides=overrides,
    profile_sha256=hashlib.sha256(profile.read_bytes()).hexdigest(),
    reset_tick=reset_tick,
    warmup=warmup,
    zero_elapsed=zero,
    windows=windows,
    limitations=[
        "Router buffer_reads/buffer_writes repeated-dump behavior excluded",
        "fixed clock ratios tested; runtime DVFS transition not exercised",
        "one endpoint, no CPU/Linux/saturation or physical timing claim",
    ],
)
with (out / "garnet-stats-result.json").open("x") as stream:
    json.dump(result, stream, indent=2, allow_nan=False)
    stream.write("\n")
print(json.dumps(result, allow_nan=False))
