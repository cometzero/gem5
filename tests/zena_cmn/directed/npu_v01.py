# SPDX-License-Identifier: BSD-3-Clause
"""Native NPU PIO/DMA/SRAM/IME regression with byte-checked results."""
import argparse
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
    NpuV01,
    RealView,
    Root,
    SimpleMemory,
    SMMUv3,
    SMMUv3DeviceInterface,
    SrcClockDomain,
    System,
    VoltageDomain,
)

parser = argparse.ArgumentParser()
parser.add_argument("--smmu", action="store_true")
parser.add_argument("--async-dma", action="store_true")
parser.add_argument("--experimental-scaling", action="store_true")
parser.add_argument("--scaling-width", type=int, choices=(64, 128, 256, 512))
args = parser.parse_args()
if args.scaling_width is not None and not args.experimental_scaling:
    parser.error("--scaling-width requires --experimental-scaling")
dma_width = (args.scaling_width or 256) if args.experimental_scaling else 16
dma_issue_width = dma_width // 64 if args.experimental_scaling else 1
max_outstanding = dma_width * 2 if args.experimental_scaling else 16
RAM, REG, GIC, SMMU = 0x80000000, 0x10000000, 0x20000000, 0x30000000
PKG, CTX, BIND = RAM + 0x1000, RAM + 0x2000, RAM + 0x3000
INPUT, WEIGHT, OUTPUT = RAM + 0x10FFD, RAM + 0x40000, RAM + 0x80FFF
DONE_IRQ, FAULT_IRQ = 100, 101
system = ArmSystem(
    mem_mode="timing",
    mem_ranges=[AddrRange(RAM, size="16MiB")],
    workload=ArmFsWorkload(),
    reset_addr=RAM,
    auto_reset_addr=False,
    highest_el_is_64=True,
)
system.voltage_domain = VoltageDomain()
system.clk_domain = SrcClockDomain(
    clock="1GHz", voltage_domain=system.voltage_domain
)
system.bus = NoncoherentXBar(
    width=32, frontend_latency=1, forward_latency=1, response_latency=1
)
system.memory = SimpleMemory(
    range=system.mem_ranges[0], latency="50ns", bandwidth="4GiB/s"
)
system.memory.port = system.bus.mem_side_ports
system.system_port = system.bus.cpu_side_ports
system.platform = RealView()
system.platform.gic = GicV2(dist_addr=GIC, cpu_addr=GIC + 0x10000)
system.platform.gic.pio = system.bus.mem_side_ports
system.npu = NpuV01(
    enable_async_dma=args.async_dma,
    experimental_scaling=args.experimental_scaling,
    max_outstanding=max_outstanding,
    dma_width=dma_width,
    dma_issue_width=dma_issue_width,
    sram_bank_width=dma_width // 16 if args.experimental_scaling else 4,
    pio_addr=REG,
    pio_latency="1ns",
    interrupt=ArmSPI(platform=system.platform, num=DONE_IRQ),
    fault_interrupt=ArmSPI(platform=system.platform, num=FAULT_IRQ),
)
system.npu.pio = system.bus.mem_side_ports
if args.smmu:
    system.ifc = SMMUv3DeviceInterface(
        wrbuf_slots=4, xlate_slots=max(4, dma_issue_width)
    )
    system.smmu = SMMUv3(
        reg_map=AddrRange(SMMU, size=0x20000),
        device_interfaces=[system.ifc],
        smmu_idr1=0x00E70009,
    )
    system.smmu.control = system.bus.mem_side_ports
    system.smmu.request = system.bus.cpu_side_ports
    system.smmu.walker = system.bus.cpu_side_ports
    system.npu.dma = system.ifc.device_port
else:
    system.npu.dma = system.bus.cpu_side_ports
system.cpu = ArmTimingSimpleCPU()
system.cpu.createThreads()
system.cpu.createInterruptController()
system.cpu.connectAllPorts(
    system.bus.cpu_side_ports,
    system.bus.cpu_side_ports,
    system.bus.mem_side_ports,
)
root = Root(full_system=True, system=system)
m5.instantiate()
proxy = system.getCCObject().physProxy
proxy.write(RAM, struct.pack("<II", 0xD503207F, 0x17FFFFFF))
results = []


def w32(addr, value):
    proxy.write(addr, struct.pack("<I", value))


def r32(addr):
    return struct.unpack("<I", proxy.read(addr, 4))[0]


def r64(addr):
    return r32(addr) | r32(addr + 4) << 32


def pending(irq):
    # Device IRQ latch; GIC routing is exercised separately by Linux.
    return bool(r32(REG + 0x50) & (1 if irq == DONE_IRQ else 2))


def wait():
    for _ in range(10000):
        if not r32(REG + 0x14) & 2:
            return
        m5.simulate(100000)
    raise AssertionError("NPU job/reset failed to drain within 1ms")


def reset():
    w32(REG + 0x10, 2)
    assert r32(REG + 0x14) & 2
    wait()
    assert r32(REG + 0x14) == 1
    assert not pending(DONE_IRQ) and not pending(FAULT_IRQ)


def op(code, dst=0, a=0, b=0, m=0, n=0, k=0, flags=0, extra=0):
    return struct.pack("<HH7I", code, flags, dst, a, b, m, n, k, extra)


def prepare(ops, bindings, harts=3, package_flags=0, variant=1):
    payload = b"".join(ops)
    package = struct.pack(
        "<IHH5I", 0x474B504E, 1, 0, package_flags, 1, 28, 0, 60 + len(payload)
    )
    package += struct.pack(
        "<8I", variant, 7 if variant == 4 else 3, 60, len(payload), 0, 0, 0, 0
    )
    package += payload
    proxy.write(PKG, package)
    proxy.write(
        CTX,
        struct.pack("<4H2Q4I", 2, 0, len(bindings), 0, BIND, 0, 1, 1, 1, 0),
    )
    if bindings:
        proxy.write(BIND, b"".join(struct.pack("<QII", *b) for b in bindings))
    w32(REG + 0x20, PKG)
    w32(REG + 0x24, 0)
    w32(REG + 0x28, len(package))
    w32(REG + 0x30, CTX)
    w32(REG + 0x34, 0)
    w32(REG + 0x38, harts)
    w32(REG + 0x54, 3)
    w32(REG + 0x10, 1)


def run(name, ops, bindings, expected_fault=0, **kwargs):
    reset()
    prepare(ops, bindings, **kwargs)
    w32(REG + 0x40, 1)
    wait()
    status, fault = r32(REG + 0x14), r32(REG + 0x60)
    assert fault == expected_fault, (name, status, fault)
    if expected_fault:
        assert status == 9 and pending(FAULT_IRQ) and not pending(DONE_IRQ)
    else:
        assert status == 5 and pending(DONE_IRQ) and not pending(FAULT_IRQ)
    result = {
        "case": name,
        "status": "PASS",
        "fault": fault,
        "cycles": r64(REG + 0x100),
        "dma_bytes": r64(REG + 0x108),
        "macs": r64(REG + 0x110),
    }
    results.append(result)
    w32(REG + 0x50, 3)
    assert not pending(DONE_IRQ) and not pending(FAULT_IRQ)
    return result


if args.smmu:
    # Disabled SMMU with GBPA.ABORT clear is the native bypass contract.
    w32(SMMU + 0x50, 4)
assert r32(REG) == 0x10001
assert r32(REG + 4) == (0x602 if args.async_dma else 0x202)
assert r32(REG + 8) == (0x302 if args.async_dma else 0x102)
run("END", [op(0)], [])
m5.stats.dump()
initial_stats = (Path(m5.options.outdir) / "stats.txt").read_text()
for counter in ("firstPayloadAcceptedTick", "lastPayloadResponseTick"):
    assert (
        float(
            re.findall(
                r"^system\.npu\." + counter + r"\s+([0-9.eE+-]+)",
                initial_stats,
                re.M,
            )[-1]
        )
        == 0
    )
assert (
    float(
        re.findall(
            r"^system\.npu\.metadataReadBytes\s+([0-9.eE+-]+)",
            initial_stats,
            re.M,
        )[-1]
    )
    > 0
)
reset()
w32(REG + 0x54, 3)
w32(REG + 0x40, 1)
wait()
assert r32(REG + 0x60) == 1 and r32(REG + 0x14) == 9
results.append({"case": "disabled-doorbell", "status": "PASS"})
reset()
prepare([op(0)], [])
w32(REG + 0x40, 1)
w32(REG + 0x40, 1)
assert r32(REG + 0x14) == 10 and r32(REG + 0x60) == 2
wait()
assert r32(REG + 0x14) == 13 and r32(REG + 0x60) == 2
results.append({"case": "busy-doorbell-preserves-job", "status": "PASS"})
reset()
prepare([op(0)], [])
w32(REG + 0x40, 1)
w32(REG + 0x10, 0)
w32(REG + 0x40, 1)
wait()
assert r32(REG + 0x14) == 9 and r32(REG + 0x60) == 1
results.append({"case": "disabled-priority-over-busy", "status": "PASS"})
w32(REG + 0x10, 3)
wait()
assert r32(REG + 0x10) == 1 and r32(REG + 0x14) == 1
prepare([op(0)], [])
w32(REG + 0x40, 1)
wait()
assert r32(REG + 0x14) == 5
results.append({"case": "RESET-retains-requested-ENABLE", "status": "PASS"})
pattern = bytes((i * 37 + 11) & 255 for i in range(4201))
proxy.write(INPUT, pattern)
proxy.write(OUTPUT - 1, bytes([0xA5]) * (len(pattern) + 2))
res = run(
    "unaligned-page-tail-copy",
    [op(1, 3, 0, 0, len(pattern)), op(2, 1, 3, 0, len(pattern)), op(0)],
    [(INPUT, len(pattern), 1), (OUTPUT, len(pattern), 2)],
)
assert proxy.read(OUTPUT, len(pattern)) == pattern
assert proxy.read(OUTPUT - 1, 1) == b"\xa5"
assert proxy.read(OUTPUT + len(pattern), 1) == b"\xa5"
assert res["dma_bytes"] == 2 * len(pattern) and not res["macs"]


def matrix(m, n, k, harts):
    a = [((i * 67) % 256) - 128 for i in range(m * k)]
    b = [((i * 29 + 17) % 256) - 128 for i in range(k * n)]
    proxy.write(INPUT, bytes(x & 255 for x in a))
    proxy.write(WEIGHT, bytes(x & 255 for x in b))
    expected = b"".join(
        struct.pack("<i", sum(a[r * k + t] * b[t * n + c] for t in range(k)))
        for r in range(m)
        for c in range(n)
    )
    res = run(
        f"GEMM-{m}x{n}x{k}-hart{harts}",
        [
            op(1, 0, 0, 0, m * k),
            op(1, 0x1000, 1, 0, k * n),
            op(3, 0x2000, 0, 0x1000, m, n, k),
            op(2, 2, 0x2000, 0, m * n * 4),
            op(0),
        ],
        [(INPUT, m * k, 1), (WEIGHT, k * n, 1), (OUTPUT, m * n * 4, 2)],
        harts=harts,
    )
    assert proxy.read(OUTPUT, len(expected)) == expected
    assert res["macs"] == m * n * k
    assert res["dma_bytes"] == m * k + k * n + m * n * 4
    return res


single = [matrix(8, 8, 8, h) for h in (1, 2, 3)]
assert single[0]["cycles"] == single[1]["cycles"] == single[2]["cycles"]
for harts in (1, 3):
    matrix(9, 11, 7, harts)
run("bad-package", [op(0)], [], package_flags=1, expected_fault=3)
run("bad-opcode", [op(99), op(0)], [], expected_fault=5)
run("missing-END", [op(1, m=0)], [(INPUT, 1, 1)], expected_fault=3)
run(
    "SRAM-bounds",
    [op(1, 2 * 1024 * 1024, 0, 0, 1), op(0)],
    [(INPUT, 1, 1)],
    expected_fault=6,
)
run(
    "binding-permission",
    [op(1, m=1), op(0)],
    [(INPUT, 1, 2)],
    expected_fault=5,
)
run("MMA-overlap", [op(3, 0, 0, 0x100, 8, 8, 8), op(0)], [], expected_fault=5)
run("unsupported-RVV-op", [op(5), op(0)], [], expected_fault=5)
# Preflight rejects the whole stream before the first payload transfer.
res = run(
    "preflight-no-side-effects",
    [op(1, m=64), op(99), op(0)],
    [(INPUT, 64, 1)],
    expected_fault=5,
)
assert res["dma_bytes"] == 0

# Abort/reset must not return terminal status until accepted writes retire.
for action in (2, 4):
    reset()
    size = 65536
    proxy.write(INPUT, b"\x5a" * size)
    proxy.write(OUTPUT, b"\xa5" * size)
    prepare(
        [op(1, 0, 0, 0, size), op(2, 1, 0, 0, size), op(0)],
        [(INPUT, size, 1), (OUTPUT, size, 2)],
    )
    w32(REG + 0x40, 1)
    for _ in range(10000):
        m5.simulate(100000)
        if r64(REG + 0x108) > size:
            break
    else:
        raise AssertionError("No accepted output DMA before stop")
    assert r32(REG + 0x14) & 2
    w32(REG + 0x10, action)
    assert r32(REG + 0x14) & 2
    wait()
    assert r32(REG + 0x14) == (1 if action == 2 else 9)
    if action == 4:
        assert r32(REG + 0x60) == 9 and pending(FAULT_IRQ)
    snapshot = proxy.read(OUTPUT, size)
    m5.simulate(1000000)
    assert (
        proxy.read(OUTPUT, size) == snapshot
    ), "Late write after stop completed"
    results.append(
        {
            "case": "RESET-drain" if action == 2 else "ABORT-drain",
            "status": "PASS",
        }
    )
    run(
        "zero-SRAM-after-reset",
        [op(2, 0, 0, 0, 4096), op(0)],
        [(OUTPUT, 4096, 2)],
    )
    assert proxy.read(OUTPUT, 4096) == bytes(4096)

if not args.async_dma:
    run("async-feature-disabled", [op(0)], [], variant=4, expected_fault=3)
else:
    size = 4096
    a = bytes((i * 17 + 3) & 255 for i in range(size))
    b = bytes((i * 31 + 7) & 255 for i in range(size))
    proxy.write(INPUT, a)
    proxy.write(WEIGHT, b)
    proxy.write(OUTPUT, b"\xa5" * (3 * size))
    run(
        "async-R0-R1-W0",
        [
            op(0x100, 0, 0, 0, size, 0, 0),
            op(0x100, 0x2000, 1, 0, size, 1, 1),
            op(0x101, 2, 0x4000, 0, size, 2, 2),
            op(0x102, m=7),
            op(2, 2, 0, size, size),
            op(2, 2, 0x2000, 2 * size, size),
            op(0),
        ],
        [(INPUT, size, 1), (WEIGHT, size, 1), (OUTPUT, 3 * size, 2)],
        variant=4,
    )
    assert proxy.read(OUTPUT, 3 * size) == bytes(size) + a + b

    k = 256
    ma = [((i * 67) % 256) - 128 for i in range(8 * k)]
    mb = [((i * 29 + 17) % 256) - 128 for i in range(k * 8)]
    proxy.write(INPUT, bytes(x & 255 for x in ma))
    proxy.write(WEIGHT, bytes(x & 255 for x in mb))
    expected = b"".join(
        struct.pack("<i", sum(ma[r * k + t] * mb[t * 8 + c] for t in range(k)))
        for r in range(8)
        for c in range(8)
    )
    bulk0, bulk1, bulkout = RAM + 0x100000, RAM + 0x200000, RAM + 0x300000
    proxy.write(bulk0, b"\x5a" * 65536)
    proxy.write(bulk1, b"\x3c" * 65536)
    proxy.write(bulkout, b"\xa5" * 65536)
    bindings = [
        (INPUT, 8 * k, 1),
        (WEIGHT, 8 * k, 1),
        (OUTPUT, 256, 2),
        (bulk0, 65536, 1),
        (bulk1, 65536, 1),
        (bulkout, 65536, 2),
    ]
    run(
        "async-DMA-MAC-overlap",
        [
            op(1, 0, 0, 0, 8 * k),
            op(1, 0x1000, 1, 0, 8 * k),
            op(0x100, 0x10000, 3, 0, 65536, 0, 0),
            op(0x100, 0x30000, 4, 0, 65536, 1, 1),
            op(0x101, 5, 0x60000, 0, 65536, 2, 2),
            op(3, 0x2000, 0, 0x1000, 8, 8, k),
            op(0x102, m=7),
            op(2, 2, 0x2000, 0, 256),
            op(0),
        ],
        bindings,
        variant=4,
    )
    assert proxy.read(OUTPUT, 256) == expected
    assert proxy.read(bulkout, 65536) == bytes(65536)

    # A pending LOAD must complete before MAC consumes A; pending STORE must
    # finish its SRAM reads before another LOAD overwrites the same C area.
    run(
        "async-SRAM-dependency",
        [
            op(1, 0x1000, 1, 0, 8 * k),
            op(0x100, 0, 0, 0, 8 * k, 0, 0),
            op(3, 0x2000, 0, 0x1000, 8, 8, k),
            op(0x101, 2, 0x2000, 0, 256, 2, 1),
            op(0x100, 0x2000, 0, 0, 256, 1, 2),
            op(0x102, m=7),
            op(0),
        ],
        bindings,
        variant=4,
    )
    assert proxy.read(OUTPUT, 256) == expected
    run(
        "async-token-reuse",
        [
            op(0x100, 0, 0, 0, 64, 0, 0),
            op(0x102, m=1),
            op(0x100, 64, 0, 64, 64, 0, 0),
            op(0x102, m=1),
            op(2, 2, 0, 0, 128),
            op(0),
        ],
        bindings,
        variant=4,
    )
    assert proxy.read(OUTPUT, 128) == proxy.read(INPUT, 128)
    for name, code in [
        (
            "async-reuse-without-WAIT",
            [
                op(0x100, 0, 0, 0, 64, 0, 0),
                op(0x100, 64, 0, 64, 64, 1, 0),
                op(0),
            ],
        ),
        ("async-WAIT-unissued", [op(0x102, m=1), op(0)]),
        ("async-invalid-load-channel", [op(0x100, 0, 0, 0, 64, 2, 0), op(0)]),
        ("async-invalid-store-channel", [op(0x101, 2, 0, 0, 64, 1, 0), op(0)]),
        ("async-token-out-of-range", [op(0x100, 0, 0, 0, 64, 0, 8), op(0)]),
    ]:
        result = run(name, code, bindings, variant=4, expected_fault=5)
        assert result["dma_bytes"] == 0
    run(
        "async-op-in-legacy-variant",
        [op(0x100, 0, 0, 0, 64), op(0)],
        bindings,
        expected_fault=5,
    )
    for action in (2, 4):
        reset()
        prepare(
            [
                op(0x100, 0x10000, 3, 0, 65536, 0, 0),
                op(0x100, 0x30000, 4, 0, 65536, 1, 1),
                op(0x101, 5, 0x60000, 0, 65536, 2, 2),
                op(0x102, m=7),
                op(0),
            ],
            bindings,
            variant=4,
        )
        w32(REG + 0x40, 1)
        for _ in range(10000):
            m5.simulate(100000)
            if r64(REG + 0x108) > 4096:
                break
        else:
            raise AssertionError("Async transfers never progressed")
        w32(REG + 0x10, action)
        wait()
        assert r32(REG + 0x14) == (1 if action == 2 else 9)
        snapshot = proxy.read(bulkout, 65536)
        m5.simulate(1000000)
        assert proxy.read(bulkout, 65536) == snapshot
        results.append({"case": f"async-stop-{action}", "status": "PASS"})

if args.smmu:
    # Enable an invalid stream table entry and verify caller-owned fault reason.
    table = RAM + 0xE00000
    proxy.write(table + 0x100 * 64, bytes(64))
    proxy.write(SMMU + 0x80, struct.pack("<Q", table))
    w32(SMMU + 0x88, 9)
    w32(SMMU + 0x20, 1)
    res = run("SMMU-invalid-STE", [op(0)], [], expected_fault=7)
    assert r64(REG + 0x68) == PKG and res["dma_bytes"] == 0
    w32(SMMU + 0x20, 0)
    run("SMMU-fault-recovery", [op(0)], [])

    cd, pt, cq, eq = (
        RAM + 0xE10000,
        RAM + 0xE20000,
        RAM + 0xE30000,
        RAM + 0xE40000,
    )
    proxy.write(table + 0x100 * 64, struct.pack("<8Q", cd | 11, *([0] * 7)))
    proxy.write(
        cd,
        struct.pack(
            "<8Q", 25 | (1 << 31) | (1 << 41) | (1 << 48), pt, 0, 0, 0, 0, 0, 0
        ),
    )
    proxy.write(pt + 2 * 8, struct.pack("<Q", (pt + 0x1000) | 3))
    proxy.write(pt + 0x1000, struct.pack("<Q", (pt + 0x2000) | 3))
    for page in range(512):
        proxy.write(
            pt + 0x2000 + page * 8,
            struct.pack("<Q", (RAM + page * 4096) | 0x403),
        )
    # Metadata uses identity pages; tensor input crosses noncontiguous PAs.
    proxy.write(
        pt + 0x2000 + 0x10 * 8, struct.pack("<Q", (RAM + 0x600000) | 0x403)
    )
    proxy.write(
        pt + 0x2000 + 0x11 * 8, struct.pack("<Q", (RAM + 0x620000) | 0x403)
    )
    pattern = bytes((i * 13 + 9) & 255 for i in range(64))
    proxy.write(RAM + 0x600FFD, pattern[:3])
    proxy.write(RAM + 0x620000, pattern[3:])
    proxy.write(SMMU + 0x90, struct.pack("<Q", cq | 7))
    proxy.write(SMMU + 0xA0, struct.pack("<Q", eq | 7))
    w32(SMMU + 0x20, 0xD)
    prod = 0

    def invalidate():
        global prod
        for command in (4 | (0x100 << 32), 0x30, 0x46):
            proxy.write(cq + (prod & 127) * 16, struct.pack("<QQ", command, 0))
            prod = (prod + 1) & 255
        w32(SMMU + 0x98, prod)
        for _ in range(10000):
            if r32(SMMU + 0x9C) == prod:
                return
            m5.simulate(10000)
        raise AssertionError("NPU SMMU invalidate timed out")

    invalidate()
    run(
        "SMMU-noncontiguous-input",
        [op(1, 0, 0, 0, 64), op(2, 1, 0, 0, 64), op(0)],
        [(INPUT, 64, 1), (OUTPUT, 64, 2)],
    )
    assert proxy.read(OUTPUT, 64) == pattern
    for page in (0x80, 0x81):
        proxy.write(
            pt + 0x2000 + page * 8,
            struct.pack("<Q", (RAM + page * 4096) | 0x483),
        )
    invalidate()
    before = proxy.read(OUTPUT, 64)
    run(
        "SMMU-cached-readonly-write",
        [op(1, 0, 0, 0, 64), op(2, 0, 0, 0, 64), op(0)],
        [(OUTPUT, 64, 3)],
        expected_fault=8,
    )
    assert proxy.read(OUTPUT, 64) == before
    assert r64(REG + 0x68) in (OUTPUT, OUTPUT + 1)
    # A fast first fault can cancel the second packet before acceptance.
    event_count = r32(SMMU + 0x100A8)
    assert 1 <= event_count <= 2
    fault_addresses = set()
    for index in range(event_count):
        event = struct.unpack("<4Q", proxy.read(eq + index * 32, 32))
        assert event[0] & 255 == 0x13 and event[0] >> 32 == 0x100
        assert event[2] in (OUTPUT, OUTPUT + 1)
        assert event[2] not in fault_addresses and not (event[1] & (1 << 35))
        fault_addresses.add(event[2])
    assert r64(REG + 0x68) in fault_addresses
    for page in (0x80, 0x81):
        proxy.write(
            pt + 0x2000 + page * 8,
            struct.pack("<Q", (RAM + page * 4096) | 0x403),
        )
    invalidate()
    run(
        "SMMU-permission-fault-recovery",
        [op(1, 0, 0, 0, 64), op(2, 1, 0, 0, 64), op(0)],
        [(INPUT, 64, 1), (OUTPUT, 64, 2)],
    )
    assert proxy.read(OUTPUT, 64) == pattern

m5.stats.dump()
stats_text = (Path(m5.options.outdir) / "stats.txt").read_text()


def stat(name):
    return float(
        re.findall(
            r"^system\.npu\." + name + r"\s+([0-9.eE+-]+)", stats_text, re.M
        )[-1]
    )


assert stat("outstandingHighWater") <= max_outstanding
assert stat("acceptedPacketsPerCycleHighWater") <= dma_issue_width
assert stat("acceptedBytesPerCycleHighWater") <= max(dma_width, 64)
if args.experimental_scaling:
    assert stat("outstandingHighWater") > 16
    if args.smmu:
        assert stat("acceptedPacketsPerCycleHighWater") == dma_issue_width
        assert stat("acceptedBytesPerCycleHighWater") == dma_width
assert stat("firstPayloadAcceptedTick") > 0
assert stat("lastPayloadResponseTick") >= stat("firstPayloadAcceptedTick")
assert stat("dmaRetries") > 0
if args.async_dma:
    assert stat("dmaChannelsHighWater") == 3
    assert stat("dmaChannelsInFlightHighWater") == 3
    assert stat("dmaMacOverlapCycles") > 0
    assert stat("dependencyStalls") > 0
report = {
    "status": "PASS",
    "smmu": args.smmu,
    "async_dma": args.async_dma,
    "experimental_scaling": args.experimental_scaling,
    "dma_width": dma_width,
    "dma_issue_width": dma_issue_width,
    "max_outstanding": max_outstanding,
    "cases": results,
    "resources": {
        name: stat(name)
        for name in (
            "firstPayloadAcceptedTick",
            "lastPayloadResponseTick",
            "outstandingHighWater",
            "acceptedPacketsPerCycleHighWater",
            "acceptedBytesPerCycleHighWater",
            "dmaRetries",
            "dmaChannelsHighWater",
            "dmaChannelsInFlightHighWater",
            "dmaMacOverlapCycles",
            "dependencyStalls",
        )
    },
}
(Path(m5.options.outdir) / "npu-result.json").write_text(
    json.dumps(report, indent=2) + "\n"
)
print("NPU_V01_NATIVE_PASS", len(results), "cases")
