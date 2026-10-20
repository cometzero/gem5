# SPDX-License-Identifier: BSD-3-Clause
"""Bounded native CHI/Garnet framing regression, no transport overrides."""
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

params = load_parameters(
    Path(__file__).resolve().parents[3]
    / "configs/zena_cmn/profiles/a720ae_public_candidate.yaml"
)
params["cache"]["l2"]["prefetch"] = "off"
system = System(mem_mode="timing", mem_ranges=[AddrRange("2GiB")])
system.voltage_domain = VoltageDomain()
system.clk_domain = SrcClockDomain(
    clock="2GHz", voltage_domain=system.voltage_domain
)
system.endpoints = [SubSystem() for _ in range(16)]
ruby = build_ruby(system, params, system.endpoints, full_system=False)
system.sys_port_proxy = RubyPortProxy(ruby_system=ruby)
system.system_port = system.sys_port_proxy.in_ports
system.generator = PyTrafficGen(max_outstanding_reqs=1)
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
)

out = Path(m5.options.outdir)
expected = contract(json.loads((out / "config.json").read_text()))
assert expected["data_flits_per_packet"] == 2
assert expected["data_packets_per_full_line"] == 2
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
result = {
    "contract": expected,
    "generator_counts": counts,
    "framing": framing(stats, expected),
    "exit": event.getCause(),
}
(out / "fabric-result.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
