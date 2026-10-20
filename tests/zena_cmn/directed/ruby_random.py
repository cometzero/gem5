# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Native Ruby random coherency stress through every DSU and global Home.

This is not a Linux/TLBI/atomic-increment test. Tiny cache mode additionally
forces evictions; production cache sizes remain the default.
"""

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
    RubyTester,
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

parser = argparse.ArgumentParser()
parser.add_argument("--profile", required=True)
parser.add_argument("--clusters", type=int, choices=(1, 2, 4), default=None)
parser.add_argument("--channels", type=int, choices=(4, 8, 16), default=None)
parser.add_argument("--checks", type=int, default=1000)
parser.add_argument("--tiny-caches", action="store_true")
parser.add_argument("--l2-prefetch", choices=("off", "stride"), default=None)
parser.add_argument(
    "--l2-policy", choices=("inclusive", "exclusive_data"), default=None
)
parser.add_argument("--mixed-id", action="store_true")
parser.add_argument("--require-mixed-coverage", action="store_true")
parser.add_argument("--dmt-dct", action="store_true")
parser.add_argument(
    "--dmt", action=argparse.BooleanOptionalAction, default=None
)
parser.add_argument(
    "--dct", action=argparse.BooleanOptionalAction, default=None
)
parser.add_argument(
    "--slc-mode", choices=("allocate", "directory_only"), default=None
)
parser.add_argument("--request-tbes", type=int)
parser.add_argument("--max-ticks", type=int, default=10000000000)
args = parser.parse_args()
params = load_parameters(args.profile)
if args.clusters is not None:
    params["cpu"]["clusters"] = args.clusters
    params["cpu"]["cores"] = params["cpu"]["cores_per_cluster"] * args.clusters
    params["cache"]["dsu_l3"]["instances"] = args.clusters
    params["cmn"]["mesh"]["cluster_local_routers"] = args.clusters
    params["full_system"]["required_linux_cpu_clusters"] = args.clusters
    params["cmn"]["mesh"]["cluster_attach"] = params["cmn"]["mesh"][
        "cluster_attach"
    ][: args.clusters]
for value, parent, key in (
    (args.channels, params["memory"], "channels"),
    (args.l2_prefetch, params["cache"]["l2"], "prefetch"),
    (args.l2_policy, params["cache"]["l2"], "policy"),
    (args.slc_mode, params["cmn"], "slc_mode"),
):
    if value is not None:
        parent[key] = value
if args.dmt_dct:
    params["cmn"].update(dmt=True, dct=True)
for name in ("dmt", "dct"):
    if getattr(args, name) is not None:
        params["cmn"][name] = getattr(args, name)
if args.request_tbes is not None:
    for level in ("l1i", "l1d", "l2", "dsu_l3"):
        params["cache"][level]["request_tbes"] = args.request_tbes
if args.tiny_caches:
    for level in ("l1i", "l1d", "l2"):
        params["cache"][level]["size_kib_per_core"] = 1
        params["cache"][level]["assoc"] = 2
    params["cache"]["dsu_l3"]["assoc"] = 2
    params["cmn"]["slc_assoc"] = 2
params = validate(params)
mesh_routers = params["cmn"]["mesh"]["rows"] * params["cmn"]["mesh"]["columns"]
system = System(mem_mode="timing", mem_ranges=[AddrRange("2GiB")])
system.voltage_domain = VoltageDomain()
system.clk_domain = SrcClockDomain(
    clock="2GHz", voltage_domain=system.voltage_domain
)
system.cache_line_size = 64
# Separate holders avoid CHI_RNF overwriting a repeated RubyTester child.
system.endpoints = [SubSystem() for _ in range(params["cpu"]["cores"])]
ruby = build_ruby(system, params, system.endpoints, full_system=False)
if args.tiny_caches:
    for node in ruby.rnf:
        node.dsu_l3.cache.size = "4KiB"
    for node in ruby.hnf:
        node.cntrl.cache.size = "4KiB"
system.sys_port_proxy = RubyPortProxy(ruby_system=ruby)
system.system_port = system.sys_port_proxy.in_ports
system.tester = RubyTester(
    checks_to_complete=args.checks,
    num_cpus=params["cpu"]["cores"],
    wakeup_frequency=10,
)
# Native RubyTester progress IDs require matching read/write port indices.
# L1I remains registered; instruction-side traffic is covered by FS tests.
for endpoint in system.endpoints:
    system.tester.cpuDataPort = endpoint.data_sequencer.in_ports
    for seq in (endpoint.inst_sequencer, endpoint.data_sequencer):
        seq.no_retry_on_stall = True
        seq.using_ruby_tester = True
if args.mixed_id:
    # Read the same physical lines as the data checker through L1I. These
    # synthetic requests exercise I/D coherence, not ISA instruction execution.
    system.instruction_readers = [PyTrafficGen() for _ in system.endpoints]
    for endpoint, reader in zip(system.endpoints, system.instruction_readers):
        endpoint.inst_sequencer.using_ruby_tester = False
        endpoint.inst_sequencer.no_retry_on_stall = False
        reader.port = endpoint.inst_sequencer.in_ports
ruby.randomization = True

homes = [node.cntrl for node in ruby.hnf]
registered = [link.ext_node for link in ruby.network.ext_links]
for index, rnf in enumerate(ruby.rnf):
    assert not bool(rnf.dsu_l3.is_HN)
    assert list(rnf.dsu_l3.downstream_destinations) == homes
    for endpoint in rnf.getCpus():
        assert list(endpoint.l1i.downstream_destinations) == [endpoint.l2]
        assert list(endpoint.l1d.downstream_destinations) == [endpoint.l2]
        assert list(endpoint.l2.downstream_destinations) == [rnf.dsu_l3]
        assert bool(endpoint.l2.use_prefetcher) == (
            params["cache"]["l2"]["prefetch"] == "stride"
        )
        if params["cache"]["l2"]["prefetch"] == "stride":
            assert int(endpoint.l2.prefetcher.degree) == 1
            assert not bool(endpoint.l2.prefetcher.on_write)
    local = mesh_routers + index
    for controller in rnf.getAllControllers():
        assert registered.count(controller) == 1
        link = next(
            link
            for link in ruby.network.ext_links
            if link.ext_node is controller
        )
        assert int(link.int_node.router_id) == local
    outgoing = [
        link
        for link in ruby.network.int_links
        if int(link.src_node.router_id) == local
    ]
    incoming = [
        link
        for link in ruby.network.int_links
        if int(link.dst_node.router_id) == local
    ]
    assert len(outgoing) == len(incoming) == 1
    assert (
        int(outgoing[0].dst_node.router_id)
        == params["cmn"]["mesh"]["cluster_attach"][index]
    )
assert len(system.mem_ctrls) == params["memory"]["channels"]
assert len(ruby.snf) == params["memory"]["channels"]
assert len(ruby.hnf) == params["cmn"]["home_count"]
assert len(ruby.network.routers) == mesh_routers + params["cpu"]["clusters"]
root = Root(full_system=False, system=system)
m5.instantiate()
if args.mixed_id:
    for reader in system.instruction_readers:
        reader.start(
            iter(
                [
                    reader.createRandom(
                        args.max_ticks, 960, 26624, 64, 1000, 5000, 100, 0
                    )
                ]
            )
        )
event = m5.simulate(args.max_ticks)
passed = event.getCause() == "Ruby Tester completed"
m5.stats.dump()
coverage = {}
for name, count in re.findall(
    r"^system\.endpoints\d+\.l2\.inTransLatHist\.([^ .:]+)::samples\s+(\d+)",
    Path(m5.options.outdir, "stats.txt").read_text(),
    re.M,
):
    coverage[name] = coverage.get(name, 0) + int(count)
if args.require_mixed_coverage:
    assert args.mixed_id, "mixed coverage requires --mixed-id"
    for required in (
        "ReadShared",
        "ReadUnique",
        "WriteBackFull",
        "WriteBackFull_Stale",
    ):
        assert (
            coverage.get(required, 0) > 0
        ), f"missing L2 coverage: {required}"
result = {
    "status": "PASS" if passed else "FAIL",
    "l2_transaction_coverage": coverage,
    "cause": event.getCause(),
    "ticks": m5.curTick(),
    "clusters": params["cpu"]["clusters"],
    "cores": params["cpu"]["cores"],
    "channels": params["memory"]["channels"],
    "checks": args.checks,
    "tiny_caches": args.tiny_caches,
    "l2_prefetch": params["cache"]["l2"]["prefetch"],
    "l2_policy": params["cache"]["l2"]["policy"],
    "mixed_id": args.mixed_id,
    "dmt_dct": args.dmt_dct,
    "dmt": params["cmn"]["dmt"],
    "dct": params["cmn"]["dct"],
    "slc_mode": params["cmn"]["slc_mode"],
    "mesh_routers": mesh_routers,
    "request_tbes": args.request_tbes,
    "mixed_id_payload_checked": False,
}
Path(m5.options.outdir, "directed-result.json").write_text(
    json.dumps(result, indent=2) + "\n"
)
print(json.dumps(result))
if not passed:
    raise SystemExit(1)
