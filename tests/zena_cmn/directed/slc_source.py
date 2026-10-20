# SPDX-License-Identifier: BSD-3-Clause
"""Qualify HNF data service after two distinct clusters establish clean sharing."""
import json
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
# Inactive generators remain instantiated across three 1 ms phases.
system.generators = [
    PyTrafficGen(max_outstanding_reqs=1, progress_check="10ms")
    for _ in range(3)
]
for gen, core in zip(system.generators, (0, 4, 8)):
    gen.port = system.endpoints[core].data_sequencer.in_ports
root = Root(full_system=False, system=system)
m5.instantiate()
results = []
for phase, gen in enumerate(system.generators):
    m5.stats.reset()
    start = m5.curTick()
    gen.start(
        iter(
            [
                gen.createLinear(
                    1000000000, 0, 256 * 1024, 64, 500, 500, 100, 256 * 1024
                ),
                gen.createIdle(1000000),
                gen.createExit(0),
            ]
        )
    )
    event = m5.simulate(1100000000)
    assert "exit state" in event.getCause().lower(), event.getCause()
    m5.stats.dump()
    results.append(
        {
            "phase": phase,
            "core": (0, 4, 8)[phase],
            "begin_tick": start,
            "end_tick": m5.curTick(),
            "cause": event.getCause(),
        }
    )
Path(m5.options.outdir, "slc-phases.json").write_text(
    json.dumps(results, indent=2) + "\n"
)

# Require source evidence, not a working-set-size inference.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "scripts/zena_cmn")
)
from summarize_roi import stats_blocks

blocks = stats_blocks(Path(m5.options.outdir, "stats.txt").read_text())
for row in results:
    block = next(
        b
        for b in blocks
        if b["finalTick"] == row["end_tick"]
        and b["simTicks"] == row["end_tick"] - row["begin_tick"]
    )
    phase = row["phase"]
    gen = f"system.generators{phase}"
    assert block[gen + ".totalReads"] == 4096
    assert block[gen + ".numPackets"] == 4096
    assert block[gen + ".numSuppressed"] == 0
    dsu = f"system.ruby.rnf{phase}.dsu_l3.cache"
    assert block[dsu + ".m_demand_hits"] == 0
    assert block[dsu + ".m_demand_misses"] == 4096
    row["hnf_hits"] = sum(
        block[f"system.ruby.hnf{i}.cntrl.cache.m_demand_hits"]
        for i in range(8)
    )
    row["hnf_misses"] = sum(
        block[f"system.ruby.hnf{i}.cntrl.cache.m_demand_misses"]
        for i in range(8)
    )
    row["dram_read_bytes"] = sum(
        block[f"system.mem_ctrls{i}.dram.dramBytesRead"] for i in range(16)
    )
    row["request_response_ns"] = (
        block[gen + ".avgReadLatency"] / block["simFreq"] * 1e9
    )
    # MessageBuffer zero counters are suppressed by native nozero flags.
    # A warm HNF must respond without requesting data from another RNF.
    row["hnf_snoop_messages"] = sum(
        block.get(f"system.ruby.hnf{i}.cntrl.snpOut.m_msg_count", 0)
        for i in range(8)
    )
    row["peer_data_messages"] = sum(
        block.get(f"system.ruby.rnf{i}.dsu_l3.datOut.m_msg_count", 0)
        for i in range(phase)
    )
    if phase:
        assert row["hnf_hits"] == 4096 and row["hnf_misses"] == 0
        assert row["dram_read_bytes"] == 0
        assert row["hnf_snoop_messages"] == 0
        assert row["peer_data_messages"] == 0
    else:
        assert row["hnf_hits"] == 0 and row["hnf_misses"] == 4096
        assert row["dram_read_bytes"] == 256 * 1024
Path(m5.options.outdir, "slc-source-result.json").write_text(
    json.dumps(
        {
            "status": "PASS",
            "qualification": "MODEL_ONLY_NATIVE_REQUEST_RESPONSE",
            "working_set_bytes": 256 * 1024,
            "phases": results,
            "note": "Latency includes Ruby/fabric; no O3/translation. Fixed phase idle time is not a throughput ROI.",
        },
        indent=2,
    )
    + "\n"
)
