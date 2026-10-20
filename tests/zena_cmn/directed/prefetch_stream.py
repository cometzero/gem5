# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Exercise CHI's real prefetch path with a fixed-PC demand stream."""

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
parser.add_argument("--prefetch", choices=("off", "stride"), required=True)
parser.add_argument(
    "--l2-policy", choices=("inclusive", "exclusive_data"), default="inclusive"
)
args = parser.parse_args()
params = load_parameters()
params["cpu"].update(cores=4, clusters=1)
params["cache"]["dsu_l3"]["instances"] = 1
params["cache"]["l2"]["prefetch"] = args.prefetch
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
                400000000, 0, 2 * 1024**2, 64, 10000, 10000, 100, 0
            ),
            system.generator.createIdle(10000000),
            system.generator.createExit(0),
        ]
    )
)
event = m5.simulate(500000000)
m5.stats.dump()
stats = Path(m5.options.outdir, "stats.txt").read_text()
issued = re.search(
    r"^system.endpoints0.l2.prefetcher.pfIssued\s+(\d+)", stats, re.M
)
count = int(issued[1]) if issued else None
if args.prefetch == "stride":
    assert count is not None and count > 0, "prefetcher never issued a request"
assert "exit state" in event.getCause().lower(), event.getCause()
result = {
    "status": "PASS",
    "prefetch": args.prefetch,
    "l2_policy": args.l2_policy,
    "pfIssued": count,
    "exit_cause": event.getCause(),
    "ticks": m5.curTick(),
}
Path(m5.options.outdir, "prefetch-result.json").write_text(
    json.dumps(result, indent=2) + "\n"
)
print(json.dumps(result))
