# SPDX-License-Identifier: BSD-3-Clause
"""Execute AP feature ID and crypto regression without Linux boot."""
import argparse
import binascii
import json
import struct
import subprocess
import sys
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

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "configs"))
from zena_cmn.cpu_profile import (
    ap_release,
    create_cpu,
)
from zena_cmn.parameters import load_parameters

parser = argparse.ArgumentParser()
parser.add_argument("--stlur", action="store_true")
parser.add_argument("--sve-el0", action="store_true")
parser.add_argument("--o3", action="store_true")
args = parser.parse_args()
if args.stlur and args.sve_el0:
    parser.error("select one instruction regression")
ram = 0x80000000
system = ArmSystem(
    workload=ArmFsWorkload(),
    release=ap_release(),
    sve_vl=1,
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
system.cpu = (
    create_cpu(
        load_parameters(
            str(
                Path(__file__).resolve().parents[3]
                / "configs/zena_cmn/profiles/a720ae_documented.yaml"
            )
        ),
        0,
    )
    if args.o3
    else ArmTimingSimpleCPU()
)
system.cpu.createThreads()
system.cpu.createInterruptController()
system.cpu.icache_port = system.bus.cpu_side_ports
system.cpu.dcache_port = system.bus.cpu_side_ports
root = Root(full_system=True, system=system)
out = Path(m5.options.outdir)
source = (
    Path(__file__).with_name("sve_el0.S")
    if args.sve_el0
    else (
        Path(__file__).with_name("stlur.S")
        if args.stlur
        else Path(__file__).with_suffix(".S")
    )
)
subprocess.run(
    ["aarch64-linux-gnu-as", str(source), "-o", str(out / "features.o")],
    check=True,
)
subprocess.run(
    [
        "aarch64-linux-gnu-objcopy",
        "-O",
        "binary",
        str(out / "features.o"),
        str(out / "features.bin"),
    ],
    check=True,
)
m5.instantiate()
proxy = system.getCCObject().physProxy
proxy.write(ram, (out / "features.bin").read_bytes())
m5.simulate(100000000)
if args.sve_el0:
    words = struct.unpack("<6Q", proxy.read(ram + 0x1000, 48))
    assert words == (
        16,
        0x0808080808080808,
        0x0808080808080808,
        0x0C0C0C0C0C0C0C0C,
        0x0C0C0C0C0C0C0C0C,
        0xCAFE,
    ), [hex(word) for word in words]
    result = {
        "status": "PASS",
        "exception_level": 0,
        "sve_vl_bits": 128,
        "sve_add": "PASS",
        "sve2_eor3": "PASS",
        "cpu": "ArmO3CPU" if args.o3 else "ArmTimingSimpleCPU",
    }
    (out / "sve-el0-result.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps(result))
    raise SystemExit(0)
words = struct.unpack("<9Q", proxy.read(ram + 0x1000, 72))
if args.stlur:
    expected = (
        0x88AAAAAAAAAAAAAA,
        0xAAAAAAAA7788AAAA,
        0xAAAAAAAA55667788,
        0x1122334455667788,
        0x55667788,
        0x1122334455667788,
        0x1122334455667788,
        0,
        0xCAFE,
    )
    assert words == expected, ([hex(w) for w in words], expected)
    result = {
        "status": "PASS",
        "stlur_widths": [1, 2, 4, 8],
        "ldapur_widths": [4, 8],
        "negative_offset_sp_zr": "PASS",
    }
    (out / "stlur-result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    raise SystemExit(0)
isar, smfr, crc, aes_low, aes_high, sha1, sha2_low, sha2_high, done = words
assert done == 0xCAFE, words
assert [(isar >> shift) & 15 for shift in (4, 8, 12, 16)] == [1] * 4
assert smfr == 0, hex(smfr)
assert crc == (binascii.crc32(b"1", 0xFFFFFFFF) ^ 0xFFFFFFFF)
assert aes_low == aes_high == 0x6363636363636363
assert sha1 == 0x048D159E, hex(sha1)
assert sha2_low == sha2_high == 0
result = {
    "status": "PASS",
    "isar0": hex(isar),
    "smfr0": hex(smfr),
    "aes": "PASS",
    "sha1": "PASS",
    "sha256": "PASS",
    "crc32": "PASS",
    "pmull64": "UNSUPPORTED_NOT_ADVERTISED",
}
(out / "ap-features-result.json").write_text(
    json.dumps(result, indent=2) + "\n"
)
print(json.dumps(result))
