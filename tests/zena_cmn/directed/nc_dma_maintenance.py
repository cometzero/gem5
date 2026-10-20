# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Timing-only CPU/CHI maintenance versus external noncoherent DMA.

Both negative controls assert the expected stale value without maintenance.
Clean uses conservative clean+invalidate; NC aliases must be cleaned first.
"""
import argparse
import json
import sys
from pathlib import Path

import m5
from m5.objects import (
    AddrRange,
    ChiDmaTest,
    Root,
    RubyPortProxy,
    SrcClockDomain,
    SubSystem,
    System,
    VoltageDomain,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "configs"))
from zena_cmn.parameters import (
    load_parameters,
    validate,
)
from zena_cmn.ruby_system_builder import build_ruby

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--profile", required=True)
parser.add_argument("--request-tbes", type=int, default=2)
parser.add_argument("--parallel", action="store_true")
parser.add_argument("--cross-cluster", action="store_true")
parser.add_argument("--evict-depth", type=int, choices=range(4), default=0)
args = parser.parse_args()
params = load_parameters(args.profile)
assert params["npu"]["enabled"], "shared noncoherent backend required"
params["cache"]["l2"]["prefetch"] = "off"
for level in ("l1i", "l1d", "l2", "dsu_l3"):
    params["cache"][level]["request_tbes"] = args.request_tbes
params["cmn"]["home_request_tbes"] = args.request_tbes
params["memory"]["snf_request_tbes"] = args.request_tbes
params = validate(params)
system = System(
    mem_mode="timing",
    mem_ranges=[AddrRange(params["memory"]["capacity_mib_total"] * 1024**2)],
)
system.voltage_domain = VoltageDomain()
system.clk_domain = SrcClockDomain(
    clock="2GHz", voltage_domain=system.voltage_domain
)
system.endpoints = [SubSystem() for _ in range(params["cpu"]["cores"])]
ruby = build_ruby(system, params, system.endpoints, full_system=False)
if args.evict_depth:
    # Diagnostic geometry, not a hardware performance profile.
    for endpoint in system.endpoints:
        endpoint.l1d.cache.size = "1KiB"
        endpoint.l1d.cache.assoc = 2
        endpoint.l2.cache.size = "4KiB"
        endpoint.l2.cache.assoc = 2
    for cluster in ruby.rnf:
        cluster.dsu_l3.cache.size = "16KiB"
        cluster.dsu_l3.cache.assoc = 2
    for home in ruby.hnf:
        home.cntrl.cache.size = "64KiB"
        home.cntrl.cache.assoc = 2
for snf in ruby.snf:
    snf.cntrl.wait_for_write_ack = True
system.sys_port_proxy = RubyPortProxy(ruby_system=ruby)
system.system_port = system.sys_port_proxy.in_ports
steps = []


def op(master, command, address, value=0, batch=None):
    steps.append(
        (
            master,
            command,
            address,
            value,
            len(steps) if batch is None else batch,
        )
    )


# Port 2 bypasses CHI entirely and shares the actual memory controllers.
# Initialize by timing writes, not a shadow memory or functional backdoor.
for case in range(8):
    addr = 0x100000 + case * 0x10000
    old, cpu, dma = 0x11110000 + case, 0x22220000 + case, 0x33330000 + case
    op(2, 1, addr, old)
    op(case % 2, 1, addr, cpu)
    if args.evict_depth:
        stride = (0, 512, 2048, 8192)[args.evict_depth]
        for victim in range(1, 2 * args.evict_depth + 2):
            op(case % 2, 0, addr + stride * victim, 0)
    op(2, 0, addr, old)  # Negative control: dirty CPU data is not in DRAM.
    op(case % 2, 2 if case % 2 else 4, addr)
    op(2, 0, addr, cpu)  # Clean completion must have reached memory.
    op(0, 0, addr, cpu)
    op(1, 0, addr, cpu)  # Shared copies, including a remote L1.
    op(2, 1, addr, dma)
    op(0, 0, addr, cpu)  # Negative control: external write does not snoop.
    op(1, 3, addr)
    op(0, 0, addr, dma)
    op(1, 0, addr, dma)
    op(0, 1, addr, cpu)
    op(2, 1, addr, dma + 1)
    op(0, 3, addr)  # IVAC must discard stale dirty data, never write it back.
    op(2, 0, addr, dma + 1)
    op(0, 0, addr, dma + 1)
    op(0, 4, addr)
    # No cacheable aliases remain: all CPU NC accesses must be nonallocating.
    op(0, 6, addr, cpu + 1)
    op(2, 0, addr, cpu + 1)
    op(0, 6, addr, cpu + 2)  # Same-line ordered writes/old ACK misassociation.
    op(0, 2, addr)
    op(2, 0, addr, cpu + 2)
    op(2, 1, addr, dma + 2)
    op(0, 5, addr, dma + 2)
    op(2, 1, addr + 8, old + 1)
    op(0, 6, addr + 8, cpu + 3)
    op(2, 0, addr, dma + 2)  # Partial NC write preserves untouched bytes.
    op(2, 0, addr + 8, cpu + 3)
    op(2, 1, addr + 8, dma + 3)
    op(0, 5, addr + 8, dma + 3)

if args.parallel:
    addr = 0x60000
    op(2, 1, addr, 11)
    op(0, 0, addr, 11)
    op(1, 0, addr, 11)
    op(2, 1, addr, 12)
    batch = len(steps)
    op(0, 3, addr, batch=batch)
    op(1, 3, addr, batch=batch)
    op(0, 0, addr, 12)
    op(1, 0, addr, 12)
    op(2, 1, addr, 13)
    batch = len(steps)
    op(0, 3, addr, batch=batch)
    op(0, 0, addr, 13, batch=batch)  # Queued follower must reissue after IVAC.
    for line in range(32):
        op(2, 1, 0x80000 + line * 1024, 0)
        op(line % 2, 1, 0x80000 + line * 1024, line + 100)
    batch = len(steps)
    for line in range(32):
        op(line % 2, 2, 0x80000 + line * 1024, batch=batch)
    for line in range(32):
        op(2, 0, 0x80000 + line * 1024, line + 100)
    batch = len(steps)
    for line in range(32):
        op(line % 2, 6, 0x80000 + line * 1024, line + 200, batch=batch)
    for line in range(32):
        op(2, 0, 0x80000 + line * 1024, line + 200)

system.tester = ChiDmaTest(
    masters=[x[0] for x in steps],
    commands=[x[1] for x in steps],
    addresses=[x[2] for x in steps],
    values=[x[3] for x in steps],
    batches=[x[4] for x in steps],
)
system.tester.ports = system.endpoints[0].data_sequencer.in_ports
peer = params["cpu"]["cores_per_cluster"] if args.cross_cluster else 1
system.tester.ports = system.endpoints[peer].data_sequencer.in_ports
system.tester.ports = system.memory_bus.cpu_side_ports
root = Root(full_system=False, system=system)
result_path = Path(m5.options.outdir) / "nc-dma-result.json"
result_path.unlink(missing_ok=True)
m5.instantiate()
event = m5.simulate(10**8)
assert event.getCause() == "CHI_DMA_TEST PASS", event.getCause()
m5.drain()
m5.stats.dump()
result = {
    "status": "PASS",
    "operations": len(steps),
    "cases": 8,
    "negative_controls": [
        "dirty_CPU_not_visible_to_DMA",
        "DMA_not_visible_to_cached_CPU",
    ],
    "completion_boundary": "native_memory_write_response_not_physical_DRAM_drain",
    "concurrency": (
        "same_line_CMO_and_read_follower_and_32_line_pressure"
        if args.parallel
        else "sequential_completion_dependency"
    ),
    "eviction_depth_requested": args.evict_depth,
    "peer_cpu": peer,
    "cross_cluster": args.cross_cluster,
    "drain_completed": True,
    "residency_evidence": "ProtocolTrace required; requested depth alone is not proof",
    "ticks": m5.curTick(),
}
result_path.write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
