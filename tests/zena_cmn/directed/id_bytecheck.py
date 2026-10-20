# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Known-data I/D reads with native MemChecker and fixed eviction overlap."""
import argparse
import hashlib
import json
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
from zena_cmn.parameters import (
    load_parameters,
    validate,
)
from zena_cmn.ruby_system_builder import build_ruby

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "scripts/zena_cmn")
)
from summarize_roi import stats_blocks


def varint(value):
    data = bytearray()
    while value >= 128:
        data.append((value & 127) | 128)
        value >>= 7
    data.append(value)
    return bytes(data)


def message(fields):
    data = b"".join(varint(k << 3) + varint(v) for k, v in fields)
    return varint(len(data)) + data


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--profile", required=True)
parser.add_argument("--negative-control", action="store_true")
parser.add_argument("--request-tbes", type=int, default=2)
parser.add_argument(
    "--dmt", action=argparse.BooleanOptionalAction, default=None
)
parser.add_argument(
    "--dct", action=argparse.BooleanOptionalAction, default=None
)
args = parser.parse_args()
params = load_parameters(args.profile)
if params["cpu"]["cores"] < 4:
    parser.error("requires at least four endpoints")
for level in ("l1i", "l1d", "l2"):
    params["cache"][level]["size_kib_per_core"] = 1
    params["cache"][level]["assoc"] = 2
    params["cache"][level]["request_tbes"] = args.request_tbes
for name in ("dmt", "dct"):
    if getattr(args, name) is not None:
        params["cmn"][name] = getattr(args, name)
params = validate(params)
system = System(mem_mode="timing", mem_ranges=[AddrRange("2GiB")])
system.voltage_domain = VoltageDomain()
system.clk_domain = SrcClockDomain(
    clock="2GHz", voltage_domain=system.voltage_domain
)
system.endpoints = [SubSystem() for _ in range(params["cpu"]["cores"])]

ruby = build_ruby(system, params, system.endpoints, full_system=False)
system.sys_port_proxy = RubyPortProxy(ruby_system=ruby)
system.system_port = system.sys_port_proxy.in_ports
system.checker = MemChecker()
system.generators = [PyTrafficGen() for _ in range(4)]
system.monitors = [
    MemCheckerMonitor(memchecker=system.checker, warn_only=False)
    for _ in range(4)
]
# D writer, local D evictor, local I reader, remote D writer. Different
# requestor IDs produce different byte patterns; every positive write is seen.
ports = [
    system.endpoints[1].data_sequencer.in_ports,
    system.endpoints[0].data_sequencer.in_ports,
    system.endpoints[0].inst_sequencer.in_ports,
    system.endpoints[-1].data_sequencer.in_ports,
]
for index, (gen, monitor, port) in enumerate(
    zip(system.generators, system.monitors, ports)
):
    gen.port = monitor.cpu_side_port
    monitor.mem_side_port = port
# Negative control bypasses observation of the remote write, not coherence.
# The subsequent I-side read must disagree with the checker's old byte value.
if args.negative_control:
    system.unobserved = PyTrafficGen()
    system.unobserved.port = system.endpoints[-2].data_sequencer.in_ports
root = Root(full_system=False, system=system)
outdir = Path(m5.options.outdir)
m5.instantiate()
schedules = [[] for _ in range(4)]
negative = []
phase = 1000000
for case, offset in enumerate(
    (0, 1000, 2000, 4000, 8000, 16000, 32000, 64000)
):
    base = case * 6 * phase
    addr = 0x10000 + case * 0x10000
    schedules[0].append((base + 1000, 4, addr, 0))
    schedules[1].append((base + phase, 1, addr, 0))
    schedules[2].append((base + 2 * phase, 1, addr, 0x100))
    # Congruent lines overflow both ways of the local 1KiB L1D.
    schedules[1].extend(
        (base + 3 * phase + n * 1000, 1, addr + 512 * (n + 1), 0)
        for n in range(16)
    )
    write = (base + 3 * phase + offset, 4, addr, 0)
    (negative if args.negative_control else schedules[3]).append(write)
    schedules[2].append((base + 5 * phase, 1, addr, 0x100))
duration = 8 * 6 * phase + phase
hashes = []
for index, (gen, schedule) in enumerate(
    zip(
        list(system.generators)
        + ([system.unobserved] if args.negative_control else []),
        schedules + ([negative] if args.negative_control else []),
    )
):
    if not schedule:
        gen.start(iter([gen.createIdle(duration * 2)]))
        continue
    data = b"gem5" + message([(3, 10**12)])
    for tick, cmd, addr, flags in sorted(schedule):
        data += message([(1, tick), (2, cmd), (3, addr), (4, 64), (5, flags)])
    path = outdir / f"requests-{index}.trace"
    path.write_bytes(data)
    hashes.append(hashlib.sha256(data).hexdigest())
    gen.start(
        iter([gen.createTrace(duration, str(path)), gen.createIdle(duration)])
    )
event = m5.simulate(duration)
assert event.getCause() == "simulate() limit reached", event.getCause()
m5.stats.dump()
values = stats_blocks((outdir / "stats.txt").read_text())[-1]
for index, schedule in enumerate(schedules):
    for cmd, name in ((1, "totalReads"), (4, "totalWrites")):
        assert values[f"system.generators{index}.{name}"] == sum(
            x[1] == cmd for x in schedule
        )
assert (
    not args.negative_control
), "negative control incorrectly passed I-side byte check"
result = {
    "status": "PASS",
    "instruction_reads_checked": 16,
    "instruction_request_flag": "INST_FETCH",
    "read_bytes_checked": 16 * 64,
    "request_tbes": args.request_tbes,
    "dmt": params["cmn"]["dmt"],
    "dct": params["cmn"]["dct"],
    "trace_sha256": hashes,
    "race_branch_coverage": "requires separate ProtocolTrace analysis; offsets alone are not proof",
    "ticks": m5.curTick(),
}
(outdir / "bytecheck-result.json").write_text(
    json.dumps(result, indent=2) + "\n"
)
print(json.dumps(result))
