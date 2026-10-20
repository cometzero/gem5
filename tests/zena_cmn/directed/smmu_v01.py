# SPDX-License-Identifier: BSD-3-Clause
"""Native SMMUv3 stage-1 regression; no NPU or Linux required."""
import json
import re
import struct
from pathlib import Path

import m5
from m5.objects import (
    AddrRange,
    ArmFsWorkload,
    ArmSPI,
    ArmSystem,
    ArmTimingSimpleCPU,
    GicV2,
    NoncoherentXBar,
    PyTrafficGen,
    RealView,
    Root,
    SimpleMemory,
    SMMUv3,
    SMMUv3DeviceInterface,
    SrcClockDomain,
    VoltageDomain,
)

RAM = 0x80000000
REG = 0x10000000
GIC = 0x20000000
STE = RAM + 0x10000
CD = RAM + 0x20000
PT = RAM + 0x30000
CQ = RAM + 0x40000
EQ = RAM + 0x50000
PA0, PA1, PA2 = RAM + 0x100000, RAM + 0x120000, RAM + 0x140000
VA = 0x400000
SID = 0x100
IRQ = 114
system = ArmSystem(
    workload=ArmFsWorkload(),
    reset_addr=RAM,
    auto_reset_addr=False,
    highest_el_is_64=True,
    mem_mode="timing",
    mem_ranges=[AddrRange(RAM, size="16MiB")],
    external_memory_ranges=[
        AddrRange(VA, size="1MiB"),
        AddrRange(VA + (1 << 39), size="1MiB"),
    ],
)
system.voltage_domain = VoltageDomain()
system.clk_domain = SrcClockDomain(
    clock="1GHz", voltage_domain=system.voltage_domain
)
system.bus = NoncoherentXBar(
    width=16, frontend_latency=1, forward_latency=1, response_latency=1
)
system.memory = SimpleMemory(range=system.mem_ranges[0], latency="50ns")
system.memory.port = system.bus.mem_side_ports
system.system_port = system.bus.cpu_side_ports
system.cpu = ArmTimingSimpleCPU()
system.cpu.createThreads()
system.cpu.createInterruptController()
system.cpu.icache_port = system.bus.cpu_side_ports
system.cpu.dcache_port = system.bus.cpu_side_ports
system.platform = RealView()
system.platform.gic = GicV2(dist_addr=GIC, cpu_addr=GIC + 0x10000)
system.platform.gic.pio = system.bus.mem_side_ports
system.ifc = SMMUv3DeviceInterface(wrbuf_slots=4, xlate_slots=4)
system.smmu = SMMUv3(
    reg_map=AddrRange(REG, size=0x20000),
    device_interfaces=[system.ifc],
    smmu_idr1=0x00E70009,
    eventq_irq=ArmSPI(platform=system.platform, num=IRQ),
)
system.smmu.control = system.bus.mem_side_ports
system.smmu.request = system.bus.cpu_side_ports
system.smmu.walker = system.bus.cpu_side_ports
system.generator = PyTrafficGen(
    stream_gen="fixed", sids=[SID], ssids=[0], max_outstanding_reqs=8
)
system.generator.port = system.ifc.device_port
root = Root(full_system=True, system=system)
out = Path(m5.options.outdir)
(out / "smmu-result.json").unlink(missing_ok=True)
m5.instantiate()
proxy = system.getCCObject().physProxy
proxy.write(RAM, struct.pack("<II", 0xD503207F, 0x17FFFFFF))
results = []


def write64(addr, value):
    proxy.write(addr, struct.pack("<Q", value))


def write32(addr, value):
    proxy.write(addr, struct.pack("<I", value))


def read32(addr):
    return struct.unpack("<I", proxy.read(addr, 4))[0]


def irq_pending():
    return system.smmu.eventqInterruptActive()


def consume_events():
    prod = read32(REG + 0x100A8)
    write32(REG + 0x100AC, prod)
    assert not irq_pending(), "EVTQ wired IRQ remained asserted after consume"


def ste(config=5, valid=True):
    proxy.write(
        STE + SID * 64,
        struct.pack("<8Q", CD | config << 1 | valid, *([0] * 7)),
    )


def pte(index, pa, ro=False):
    write64(PT + 0x2000 + index * 8, pa | 0x403 | (0x80 if ro else 0))


cmd_prod = 0


def commands(*ops, wait=True):
    global cmd_prod
    for op in ops:
        proxy.write(CQ + (cmd_prod & 127) * 16, struct.pack("<QQ", op, 0))
        cmd_prod = (cmd_prod + 1) & 255
    write32(REG + 0x98, cmd_prod)
    if wait:
        for _ in range(1000):
            if read32(REG + 0x9C) == cmd_prod:
                return
            m5.simulate(10000)
        raise AssertionError("CMD_SYNC timed out")


def invalidate():
    commands(4 | SID << 32, 0x30, 0x46)


def start(addr, count=1, read=False, period=1000):
    m5.stats.reset()
    system.generator.start(
        iter(
            [
                system.generator.createLinear(
                    100000000,
                    addr,
                    addr + count * 64,
                    64,
                    period,
                    period,
                    100 if read else 0,
                    count * 64,
                ),
                system.generator.createIdle(1000),
                system.generator.createExit(0),
            ]
        )
    )


def finish(name, count):
    m5.simulate(110000000)
    m5.stats.dump()
    text = (
        (out / "stats.txt")
        .read_text()
        .split("Begin Simulation Statistics")[-1]
    )
    counts = {}
    for key in ("numPackets", "totalReads", "totalWrites"):
        match = re.search(
            r"^system\.generator\." + key + r"\s+(\d+)", text, re.M
        )
        assert match, key
        counts[key] = int(match[1])
    assert counts["numPackets"] == count, counts
    assert counts["totalReads"] + counts["totalWrites"] == count, counts
    results.append({"case": name, "status": "PASS", **counts})


def traffic(name, addr=VA, count=1, read=False):
    start(addr, count, read)
    finish(name, count)


def event_record(index, expected, address, write=True):
    words = struct.unpack("<4Q", proxy.read(EQ + index * 32, 32))
    assert words[0] == SID << 32 | (1 << 11) | expected, words
    assert words[2] == address, words
    assert bool(words[1] & (1 << 35)) == (not write), words
    assert words[3] == 0, words
    assert irq_pending(), "Missing wired fault IRQ"


# The IRQ stays disabled in the GIC; only its pending state is inspected.
write32(REG + 0x50, 4)
write64(REG + 0xA0, EQ | 7)
write64(REG + 0x90, CQ | 7)
write64(REG + 0x80, STE)
write32(REG + 0x88, 9)
write32(REG + 0x20, 0xC)
traffic("disabled-bypass", PA0)
assert proxy.read(PA0, 64) != bytes(64)
pattern = proxy.read(PA0, 64)
ste(4)
write32(REG + 0x20, 0xD)
traffic("STE-bypass", PA1)
assert proxy.read(PA1, 64) == pattern

# 39-bit VA, 4 KiB tables: L1 -> L2 -> L3, mapping two noncontiguous pages.
ste()
proxy.write(
    CD,
    struct.pack(
        "<8Q", 25 | (1 << 31) | (1 << 41) | (1 << 48), PT, 0, 0, 0, 0, 0, 0
    ),
)
write64(PT, (PT + 0x1000) | 3)
write64(PT + 0x1000 + 2 * 8, (PT + 0x2000) | 3)
pte(0, PA0)
pte(1, PA1)
invalidate()
proxy.write(PA0, bytes(4096))
proxy.write(PA1, bytes(4096))
traffic("stage1-noncontiguous-pages", VA, 65)
assert proxy.read(PA0, 4096) == pattern * 64
assert proxy.read(PA1, 64) == pattern
assert proxy.read(PA0 + 4096, 64) == bytes(64)

# Repeated invalid writes exceed four interface write-buffer beats.
traffic("repeated-unmapped-write", VA + 0x2000, 12)
for i in range(12):
    event_record(i, 0x10, VA + 0x2000 + i * 64)
write32(REG + 0x50, 0)
assert not irq_pending()
write32(REG + 0x50, 4)
assert irq_pending()
results.append({"case": "IRQ-mask-reenable", "status": "PASS"})
consume_events()
traffic("write-after-fault-recovery", VA)

# Populate every TLB using a READ, then reject the WRITE hit to the RO page.
pte(0, PA0, ro=True)
invalidate()
traffic("readonly-read-fill", VA, read=True)
proxy.write(PA0, b"\xa5" * 64)
traffic("readonly-write-hit", VA)
assert proxy.read(PA0, 64) == b"\xa5" * 64
event_record(12, 0x13, VA)
consume_events()
pte(0, PA2)
invalidate()
traffic("remap-after-TLBI", VA)
assert proxy.read(PA2, 64) == pattern
assert proxy.read(PA0, 64) == b"\xa5" * 64

# Command execution cannot publish CONS before an old walk finishes.
pte(0, PA0)
invalidate()
start(VA, read=True)
m5.simulate(3000)
pte(0, PA1)
commands(0x30, 0x46, wait=False)
assert read32(REG + 0x9C) != cmd_prod
finish("inflight-TLBI-sync", 1)
assert read32(REG + 0x9C) == cmd_prod
proxy.write(PA1, bytes(64))
traffic("inflight-remap-followup", VA)
assert proxy.read(PA1, 64) == pattern

# Input width and descriptor errors must not terminate the simulator.
traffic("input-address-size", VA + (1 << 39))
event_record(13, 0x11, VA + (1 << 39))
consume_events()
ste(valid=False)
invalidate()
traffic("invalid-STE", VA)
event_record(14, 4, VA)
consume_events()
ste()
write64(CD, 0)
invalidate()
traffic("invalid-CD", VA)
event_record(15, 0xA, VA)
consume_events()

write64(CD, (1 << 31) | (1 << 41) | 25 | (3 << 6))
invalidate()
traffic("reserved-CD-granule", VA)
event_record(16, 0xA, VA)
consume_events()
write64(CD, (1 << 31) | (1 << 41) | 63)
invalidate()
traffic("invalid-CD-T0SZ", VA)
event_record(17, 0xA, VA)
consume_events()

# Full queue must drop events with OVFLG rather than panic or leak credits.
traffic("eventq-overflow", VA, 140)
prod = read32(REG + 0x100A8)
assert prod & (1 << 31), hex(prod)
consume_events()
ste(4)
invalidate()
traffic("overflow-recovery", PA2)
proxy.write(PA2, bytes(64))
start(PA2)
m5.simulate(10000)
drain_begin = m5.curTick()
m5.drain()
assert proxy.read(PA2, 64) == pattern
assert m5.curTick() > drain_begin
finish("inflight-global-drain", 1)
m5.drain()
result = {
    "status": "PASS",
    "cases": results,
    "scope": "native SMMUv3 timing, 4 KiB stage1, fixed SID 0x100",
}
(out / "smmu-result.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
