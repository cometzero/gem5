# SPDX-License-Identifier: BSD-3-Clause
"""Compare native stats traversal with the original Python recursion."""

import hashlib
import json
from pathlib import Path

import m5
from m5.objects import (
    AddrRange,
    IOXBar,
    LatchedPowerModel,
    PowerActor,
    PowerModel,
    Root,
    SimpleMemory,
    SrcClockDomain,
    SubSystem,
    System,
    VoltageDomain,
)

system = System(mem_mode="timing", mem_ranges=[AddrRange("4KiB")])
system.voltage_domain = VoltageDomain(voltage="1V")
system.clk_domain = SrcClockDomain(
    clock="1GHz", voltage_domain=system.voltage_domain
)
system.bus = IOXBar()
system.memory = SimpleMemory(range=system.mem_ranges[0])
system.memory.port = system.bus.mem_side_ports
system.system_port = system.bus.cpu_side_ports
system.zone = SubSystem()
system.actors = [PowerActor() for _ in range(3)]
for actor, state in zip(system.actors, ("ON", "CLK_GATED", "OFF")):
    actor.power_state.default_state = state
    actor.power_model = [
        PowerModel(
            subsystem=system.zone,
            pm=[LatchedPowerModel(dynamic_power=1.25) for _ in range(4)],
        )
    ]
root = Root(full_system=False, system=system)
m5.instantiate()
m5.simulate(1_000_000_000)
assert hasattr(m5.stats._m5_stats, "prepareGroupChildren")
assert hasattr(m5.stats._m5_stats, "dumpGroup")

# Collate and update residency once. Repeating dump callbacks can alter Ruby
# aggregate counters; only prepare/export implementations are compared below.
m5.stats._m5_stats.processDumpQueue()
root.preDumpStats()


def reference_prepare():
    for stat in m5.stats.stats_list:
        stat.prepare()

    def children(group):
        for child in group.getStatGroups().values():
            for stat in child.getStats():
                stat.prepare()
            children(child)

    children(root)


def reference_dump(visitor, roots):
    def group_stats(group):
        for stat in group.getStats():
            stat.visit(visitor)
        for name, child in group.getStatGroups().items():
            visitor.beginGroup(name)
            group_stats(child)
            visitor.endGroup()

    if roots:
        for subroot in roots:
            for part in subroot.path_list():
                visitor.beginGroup(part)
            group_stats(subroot)
            for part in reversed(subroot.path_list()):
                visitor.endGroup()
    else:
        group_stats(root)
        for stat in m5.stats.stats_list:
            stat.visit(visitor)


def render(label, roots, native):
    path = Path(m5.options.outdir, label + ".txt")
    # initText owns one visitor; capture successive complete frames from it.
    stats_path = Path(m5.options.outdir, "stats.txt")
    offset = stats_path.stat().st_size if stats_path.exists() else 0
    visitor = m5.stats.outputList[0]
    if native:
        m5.stats.prepare()
    else:
        reference_prepare()
    visitor.begin("TRAVERSAL_REGRESSION")
    if native:
        m5.stats._dump_to_visitor(visitor, roots=roots)
    else:
        reference_dump(visitor, roots)
    visitor.end()
    payload = stats_path.read_bytes()[offset:].decode()
    path.write_text(payload)
    # Host cost counters change while the equivalent exports run sequentially.
    return "".join(
        line
        for line in payload.splitlines(keepends=True)
        if not line.startswith("host")
    )


results = []
for name, roots in (
    ("full", None),
    ("subroots", [system.memory, system.actors[1]]),
):
    reference = render(name + "-python", roots, False)
    native = render(name + "-native", roots, True)
    assert native == reference, name
    results.append(
        dict(
            name=name,
            status="PASS",
            normalized_lines=len(native.splitlines()),
            sha256=hashlib.sha256(native.encode()).hexdigest(),
        )
    )
result = dict(
    status="PASS",
    qualification="NATIVE_STATS_ORDER_AND_PAYLOAD_NOT_PERFORMANCE",
    normalized_fields="top-level host* counters only",
    cases=results,
)
Path(m5.options.outdir, "result.json").write_text(
    json.dumps(result, indent=2) + "\n"
)
print(json.dumps(result))
