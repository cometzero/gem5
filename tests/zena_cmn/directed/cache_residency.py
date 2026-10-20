# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Prove clean-victim residency for an L1D < working set < L2 stream."""

import argparse
import json
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

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "configs"))
from zena_cmn.parameters import load_parameters
from zena_cmn.ruby_system_builder import build_ruby

parser = argparse.ArgumentParser()
parser.add_argument(
    "--l2-policy", choices=("inclusive", "exclusive_data"), required=True
)
args = parser.parse_args()
params = load_parameters()
params["cpu"].update(cores=4, clusters=1)
params["cache"]["dsu_l3"]["instances"] = 1
params["cache"]["l2"]["prefetch"] = "off"
params["cache"]["l2"]["policy"] = args.l2_policy
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
system.generator.port = system.endpoints[0].data_sequencer.in_ports
root = Root(full_system=False, system=system)
m5.instantiate()
system.generator.start(
    iter(
        [
            system.generator.createLinear(
                400000000, 0, 256 * 1024, 64, 10000, 10000, 100, 0
            ),
            system.generator.createIdle(10000000),
            system.generator.createExit(0),
        ]
    )
)
m5.simulate(200000000)
m5.stats.reset()
event = m5.simulate(300000000)
m5.stats.dump()
stats = Path(m5.options.outdir, "stats.txt").read_text()


def counter(name):
    match = re.search(r"^" + re.escape(name) + r"\s+(\d+)", stats, re.M)
    assert match, f"missing statistic: {name}"
    return int(match[1])


hits = counter("system.endpoints0.l2.cache.m_demand_hits")
misses = counter("system.endpoints0.l2.cache.m_demand_misses")
rate = hits / (hits + misses)
dram_read = sum(
    int(value)
    for value in re.findall(
        r"^system\.mem_ctrls\d+\.dram\.dramBytesRead\s+(\d+)", stats, re.M
    )
)
assert "exit state" in event.getCause().lower(), event.getCause()
result = {
    "status": "PASS" if rate >= 0.95 else "FAIL",
    "l2_policy": args.l2_policy,
    "working_set_bytes": 256 * 1024,
    "l2_hits": hits,
    "l2_misses": misses,
    "l2_hit_rate": rate,
    "dram_bytes_read": dram_read,
    "exit_cause": event.getCause(),
    "ticks": m5.curTick(),
}
Path(m5.options.outdir, "residency-result.json").write_text(
    json.dumps(result, indent=2) + "\n"
)
print(json.dumps(result))
assert rate >= 0.95, "clean victims are not retained in private L2"
