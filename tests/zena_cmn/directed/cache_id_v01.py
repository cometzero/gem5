# SPDX-License-Identifier: BSD-3-Clause
"""Execute AArch64 MRS/MSR to verify selectable native cache ID registers."""
import argparse
import json
import struct
import subprocess
from pathlib import Path

import m5
from m5.objects import (
    AddrRange,
    ArmFsWorkload,
    ArmSystem,
    ArmTimingSimpleCPU,
    NoncoherentXBar,
    Root,
    SimpleMemory,
    SrcClockDomain,
    VoltageDomain,
)

parser = argparse.ArgumentParser()
parser.add_argument("--default", action="store_true")
args = parser.parse_args()
ram = 0x80000000
clidr = 0x0B200123


# 64 B lines; L1 D/I 64 KiB 4-way, L2 512 KiB 8-way, L3 4 MiB 16-way.
def ccsidr(size, ways):
    return (
        0x70000000
        | (((size // (64 * ways)) - 1) << 13)
        | ((ways - 1) << 3)
        | 2
    )


values = [
    ccsidr(65536, 4),
    ccsidr(65536, 4),
    ccsidr(524288, 8),
    0,
    ccsidr(4194304, 16),
    0,
] + [0] * 8
system = ArmSystem(
    workload=ArmFsWorkload(),
    mem_mode="timing",
    reset_addr=ram,
    auto_reset_addr=False,
    highest_el_is_64=True,
    mem_ranges=[AddrRange(ram, size="64KiB")],
)
system.voltage_domain = VoltageDomain()
system.clk_domain = SrcClockDomain(
    clock="1GHz", voltage_domain=system.voltage_domain
)
system.bus = NoncoherentXBar(
    width=16, frontend_latency=1, forward_latency=1, response_latency=1
)
system.memory = SimpleMemory(range=system.mem_ranges[0], latency="10ns")
system.memory.port = system.bus.mem_side_ports
system.system_port = system.bus.cpu_side_ports
system.cpu = ArmTimingSimpleCPU()
system.cpu.createThreads()
system.cpu.createInterruptController()
system.cpu.icache_port = system.bus.cpu_side_ports
system.cpu.dcache_port = system.bus.cpu_side_ports
if not args.default:
    system.cpu.isa[0].cache_id_registers_enabled = True
    system.cpu.isa[0].clidr = clidr
    system.cpu.isa[0].ccsidr = values
root = Root(full_system=True, system=system)
out = Path(m5.options.outdir)
out.mkdir(parents=True, exist_ok=True)
(out / "cache-id-result.json").unlink(missing_ok=True)
source = Path(__file__).with_suffix(".S")
subprocess.run(
    ["aarch64-linux-gnu-as", str(source), "-o", str(out / "cache-id.o")],
    check=True,
)
subprocess.run(
    [
        "aarch64-linux-gnu-objcopy",
        "-O",
        "binary",
        str(out / "cache-id.o"),
        str(out / "cache-id.bin"),
    ],
    check=True,
)
m5.instantiate()
proxy = system.getCCObject().physProxy
proxy.write(ram, (out / "cache-id.bin").read_bytes())
m5.simulate(100000000)
words = struct.unpack("<18Q", proxy.read(ram + 0x1000, 18 * 8))
assert words[-1] == 0xCAFE, words
expected = [0] * 17 if args.default else [clidr] + values + [0, 0]
assert list(words[:-1]) == expected, (words, expected)
result = {
    "status": "PASS",
    "default": args.default,
    "clidr": words[0],
    "ccsidr": list(words[1:-1]),
}
(out / "cache-id-result.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
