# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Finite native NPU saturation diagnostic, not Linux or silicon evidence."""
import argparse
import json
import re
import struct
import sys
from pathlib import Path

import m5
from m5.objects import (
    AddrRange,
    ArmFsWorkload,
    ArmSPI,
    ArmSystem,
    ArmTimingSimpleCPU,
    GicV2,
    NpuV01,
    RealView,
    Root,
    SMMUv3,
    SMMUv3DeviceInterface,
    SrcClockDomain,
    VoltageDomain,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "configs"))
from zena_cmn.memory_backend import build_memory_backend
from zena_cmn.parameters import load_parameters

STAGES = {
    "baseline": (16, 1, 16, 4),
    "dma64": (64, 1, 64, 4),
    "dma128": (128, 2, 128, 8),
    "dma256": (256, 4, 512, 16),
    "dma512": (512, 8, 1024, 32),
}
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--stage", choices=STAGES, default="baseline")
parser.add_argument(
    "--mode", choices=("read", "write", "copy"), default="copy"
)
parser.add_argument("--mib", type=int, choices=(1, 2, 4, 8, 16), default=16)
parser.add_argument("--passes", type=int, choices=(1, 2), default=1)
parser.add_argument(
    "--smmu-mode", choices=("translated", "bypass"), default="translated"
)
parser.add_argument(
    "--smmu-resources", choices=("baseline", "scaled"), default="baseline"
)
parser.add_argument(
    "--smmu-device-slots",
    type=int,
    choices=(128, 256, 512, 1024),
    help="Override translation and write-buffer slots in scaled mode",
)
parser.add_argument("--smmu-clock", default="1GHz")
parser.add_argument("--fabric-clock", default="2GHz")
parser.add_argument(
    "--fabric-width", type=int, choices=(32, 64, 128, 256, 512)
)
args = parser.parse_args()
if args.smmu_device_slots is not None and args.smmu_resources != "scaled":
    parser.error("--smmu-device-slots requires --smmu-resources scaled")
width, issue, credits, bank_width = STAGES[args.stage]
fabric_width = args.fabric_width or max(32, width)
ROOT = Path(__file__).resolve().parents[3]
params = load_parameters(
    ROOT / "configs/zena_cmn/profiles/a720ae_npu_v01_async.yaml"
)
# Only the directed fixture widens the arbiter; native DRAM parameters stay fixed.
params["npu"]["fabric_width"] = fabric_width
RAM, REG, GIC, SMMU = 0x80000000, 0x10000000, 0x20000000, 0x30000000
PKG, CTX, BIND = RAM + 0x10000, RAM + 0x20000, RAM + 0x21000
TABLE, CD, PT, CQ, EQ = (
    RAM + x for x in (0x40000, 0x50000, 0x100000, 0x60000, 0x70000)
)
IN_IOVA, OUT_IOVA = RAM + 0x400000, RAM + 0x2000000
IN_PA, OUT_PA = RAM + 0x4000000, RAM + 0x6000000
SIZE, CHUNK = args.mib * 1024**2, 256 * 1024
system = ArmSystem(
    mem_mode="timing",
    mem_ranges=[
        AddrRange(RAM, size=f'{params["memory"]["capacity_mib_total"]}MiB')
    ],
    workload=ArmFsWorkload(),
    reset_addr=RAM,
    auto_reset_addr=False,
    highest_el_is_64=True,
)
system.voltage_domain = VoltageDomain()
system.clk_domain = SrcClockDomain(
    clock="2GHz", voltage_domain=system.voltage_domain
)
system.npu_clock = SrcClockDomain(
    clock="1GHz", voltage_domain=system.voltage_domain
)
system.smmu_clock = SrcClockDomain(
    clock=args.smmu_clock, voltage_domain=system.voltage_domain
)
backend = build_memory_backend(system, params)
system.fabric_clock = SrcClockDomain(
    clock=args.fabric_clock, voltage_domain=system.voltage_domain
)
system.memory_bus.clk_domain = system.fabric_clock
system.system_port = system.memory_bus.cpu_side_ports
system.platform = RealView()
system.platform.gic = GicV2(dist_addr=GIC, cpu_addr=GIC + 0x10000)
system.platform.gic.pio = system.memory_bus.mem_side_ports
npu_kwargs = dict(
    enable_async_dma=True,
    pio_addr=REG,
    clk_domain=system.npu_clock,
    interrupt=ArmSPI(platform=system.platform, num=100),
    fault_interrupt=ArmSPI(platform=system.platform, num=101),
    dma_width=width,
    max_outstanding=credits,
    sram_bank_width=bank_width,
    bank_queue_depth=4 if args.stage == "baseline" else 16,
)
if args.stage != "baseline":
    npu_kwargs.update(experimental_scaling=True, dma_issue_width=issue)
system.npu = NpuV01(**npu_kwargs)
system.npu.pio = system.memory_bus.mem_side_ports
ifc_kwargs = dict(port_width=width, clk_domain=system.smmu_clock)
smmu_kwargs = dict(request_port_width=width, clk_domain=system.smmu_clock)
if args.smmu_resources == "scaled":
    ifc_kwargs.update(
        wrbuf_slots=128, xlate_slots=128, utlb_slots=8, tlb_slots=16
    )
    smmu_kwargs.update(xlate_slots=256, ptw_slots=64)
if args.smmu_device_slots is not None:
    ifc_kwargs.update(
        xlate_slots=args.smmu_device_slots,
        wrbuf_slots=args.smmu_device_slots,
    )
system.ifc = SMMUv3DeviceInterface(**ifc_kwargs)
system.smmu = SMMUv3(
    reg_map=AddrRange(SMMU, size=0x20000),
    device_interfaces=[system.ifc],
    smmu_idr1=0x00E70009,
    **smmu_kwargs,
)
system.smmu.control = system.memory_bus.mem_side_ports
system.smmu.request = system.memory_bus.cpu_side_ports
system.smmu.walker = system.memory_bus.cpu_side_ports
system.npu.dma = system.ifc.device_port
system.cpu = ArmTimingSimpleCPU()
system.cpu.createThreads()
system.cpu.createInterruptController()
system.cpu.connectAllPorts(
    system.memory_bus.cpu_side_ports,
    system.memory_bus.cpu_side_ports,
    system.memory_bus.mem_side_ports,
)
root = Root(full_system=True, system=system)
m5.instantiate()
proxy = system.getCCObject().physProxy
proxy.write(RAM, struct.pack("<II", 0xD503207F, 0x17FFFFFF))


def w32(addr, value):
    proxy.write(addr, struct.pack("<I", value))


def r32(addr):
    return struct.unpack("<I", proxy.read(addr, 4))[0]


def wait_idle():
    for _ in range(10000):
        if not r32(REG + 0x14) & 2:
            return
        event = m5.simulate(1000000)
        assert event.getCause() == "simulate() limit reached", event.getCause()
    raise AssertionError("NPU did not finish within 10ms")


def op(code, dst=0, a=0, b=0, m=0, n=0, k=0):
    return struct.pack("<HH7I", code, 0, dst, a, b, m, n, k, 0)


def prepare(ops):
    payload = b"".join(ops)
    package = struct.pack(
        "<IHH5I", 0x474B504E, 1, 0, 0, 1, 28, 0, 60 + len(payload)
    )
    package += struct.pack("<8I", 4, 7, 60, len(payload), 0, 0, 0, 0) + payload
    assert len(package) < CTX - PKG
    proxy.write(PKG, package)
    proxy.write(CTX, struct.pack("<4H2Q4I", 2, 0, 2, 0, BIND, 0, 1, 1, 1, 0))
    proxy.write(
        BIND, struct.pack("<QIIQII", input_addr, SIZE, 1, output_addr, SIZE, 2)
    )
    for offset, value in (
        (0x20, PKG),
        (0x24, 0),
        (0x28, len(package)),
        (0x30, CTX),
        (0x34, 0),
        (0x38, 3),
        (0x54, 3),
        (0x10, 1),
    ):
        w32(REG + offset, value)


def launch():
    w32(REG + 0x40, 1)
    wait_idle()
    assert r32(REG + 0x14) == 5, (r32(REG + 0x14), r32(REG + 0x60))
    assert r32(REG + 0x60) == 0 and r32(REG + 0x50) == 1
    assert r32(SMMU + 0x100A8) == 0, "Unexpected SMMU event"
    w32(REG + 0x50, 3)


# Distinct IOVA/PA tensors prove the payload uses the stage-1 mappings.
if args.smmu_mode == "translated":
    input_addr, output_addr = IN_IOVA, OUT_IOVA
    proxy.write(TABLE + 0x100 * 64, struct.pack("<8Q", CD | 11, *([0] * 7)))
    proxy.write(
        CD,
        struct.pack(
            "<8Q", 25 | (1 << 31) | (1 << 41) | (1 << 48), PT, 0, 0, 0, 0, 0, 0
        ),
    )
    proxy.write(PT + 2 * 8, struct.pack("<Q", (PT + 0x1000) | 3))
    for region in range(24):
        l3 = PT + 0x2000 + region * 4096
        proxy.write(PT + 0x1000 + region * 8, struct.pack("<Q", l3 | 3))
        entries = []
        for page in range(512):
            iova = RAM + region * 2 * 1024**2 + page * 4096
            physical = iova
            if IN_IOVA <= iova < IN_IOVA + SIZE:
                physical = IN_PA + iova - IN_IOVA
            elif OUT_IOVA <= iova < OUT_IOVA + SIZE:
                physical = OUT_PA + iova - OUT_IOVA
            entries.append(physical | 0x403)
        proxy.write(l3, struct.pack("<512Q", *entries))
    proxy.write(SMMU + 0x80, struct.pack("<Q", TABLE))
    w32(SMMU + 0x88, 9)
    proxy.write(SMMU + 0x90, struct.pack("<Q", CQ | 7))
    proxy.write(SMMU + 0xA0, struct.pack("<Q", EQ | 7))
    w32(SMMU + 0x20, 0xD)
else:
    input_addr, output_addr = IN_PA, OUT_PA
    w32(SMMU + 0x50, 4)

# A block-specific pattern catches address, stride and buffer-selection errors.
blocks = [
    bytes((i * 37 + block * 11 + (i >> 8)) & 255 for i in range(CHUNK))
    for block in range(SIZE // CHUNK)
]
for block, data in enumerate(blocks):
    proxy.write(IN_PA + block * CHUNK, data)
proxy.write(OUT_PA, b"\xa5" * SIZE)
w32(REG + 0x10, 2)
wait_idle()
assert r32(REG + 0x14) == 1
m5.simulate(1000000)  # CPU enters WFI before the measured job.
ops = []
for sequence in range(args.passes * SIZE // CHUNK):
    slot, offset = sequence % 2, (sequence * CHUNK) % SIZE
    if args.mode == "read":
        if sequence >= 2:
            ops.append(op(0x102, m=1 << slot))
        ops.append(op(0x100, slot * CHUNK, 0, offset, CHUNK, slot, slot))
    elif args.mode == "write":
        if sequence:
            ops.append(op(0x102, m=1 << 2))
        ops.append(op(0x101, 1, 0, offset, CHUNK, 2, 2))
    else:
        if sequence >= 2:
            ops.append(op(0x102, m=1 << (slot + 2)))
        ops.extend(
            (
                op(0x100, slot * CHUNK, 0, offset, CHUNK, slot, slot),
                op(0x102, m=1 << slot),
                op(0x101, 1, slot * CHUNK, offset, CHUNK, 2, slot + 2),
            )
        )
ops.append(op(0))
prepare(ops)
m5.stats.reset()
start = m5.curTick()
launch()
end = m5.curTick()
m5.stats.dump()
# Preserve the job-boundary stats separately from drain and validation traffic.
stats_path = Path(m5.options.outdir) / "stats.txt"
job_stats = stats_path.read_text()
(Path(m5.options.outdir) / "job-stats.txt").write_text(job_stats)
m5.drain()
drain_end = m5.curTick()
m5.stats.dump()
all_stats = stats_path.read_text()
(Path(m5.options.outdir) / "drained-stats.txt").write_text(all_stats)


def values(text):
    block = text.split("---------- Begin Simulation Statistics ----------")[-1]
    return {
        name: float(value)
        for name, value in re.findall(
            r"^(\S+)\s+([0-9.eE+-]+)(?:\s|$)", block, re.M
        )
    }


job, drained = values(job_stats), values(all_stats)
npu = {
    name.removeprefix("system.npu."): value
    for name, value in job.items()
    if name.startswith("system.npu.")
}
expected_read = SIZE * args.passes if args.mode != "write" else 0
expected_write = SIZE * args.passes if args.mode != "read" else 0
assert npu["dmaReadBytes"] == expected_read
assert npu["dmaWriteBytes"] == expected_write
assert npu["faults"] == 0 and npu["jobs"] == 1
assert npu["outstandingHighWater"] <= credits
assert backend["address_validation"]["status"] == "PASS"
if args.mode == "read":
    # Read-only ROI has no stores. Verify the last two SRAM blocks afterwards.
    prepare([op(2, 1, 0, 0, CHUNK), op(2, 1, CHUNK, CHUNK, CHUNK), op(0)])
    launch()
    m5.drain()
    assert proxy.read(OUT_PA, 2 * CHUNK) == blocks[-2] + blocks[-1]
    verification = "last_two_SRAM_blocks_and_all_payload_byte_counters"
else:
    for block in range(SIZE // CHUNK):
        expected = blocks[block] if args.mode == "copy" else bytes(CHUNK)
        assert proxy.read(OUT_PA + block * CHUNK, CHUNK) == expected
    verification = "all_output_bytes"


def dram_bytes(stats, suffix):
    return sum(
        value
        for name, value in stats.items()
        if name.startswith("system.mem_ctrls") and name.endswith(suffix)
    )


report = {
    "status": "PASS",
    "args": vars(args),
    "qualification": "NATIVE_DIRECTED_DIAGNOSTIC_NOT_LINUX_OR_SILICON",
    "limitations": [
        "CPU idle; no CHI or CPU contention",
        "SMMU walker uses native XBar, not Linux coherent CHI maintenance path",
        "Read ROI validates final two SRAM blocks and total bytes, not every overwritten intermediate SRAM block",
        "DRAM burst bytes are controller accounting, not an independent physical DQ completion trace",
    ],
    "settings": {
        "memory_controller_clock": "2GHz",
        "npu_clock": "1GHz",
        "smmu_clock": args.smmu_clock,
        "fabric_clock": args.fabric_clock,
        "dma_width": width,
        "dma_issue_width": issue,
        "max_outstanding": credits,
        "sram_banks": 16,
        "sram_bank_width": bank_width,
        "bank_queue_depth": 4 if args.stage == "baseline" else 16,
        "fabric_width": fabric_width,
        "ifc": {
            k: str(getattr(system.ifc, k))
            for k in (
                "port_width",
                "wrbuf_slots",
                "xlate_slots",
                "utlb_slots",
                "tlb_slots",
            )
        },
        "smmu": {
            k: str(getattr(system.smmu, k))
            for k in (
                "request_port_width",
                "xlate_slots",
                "ptw_slots",
                "cfg_slots",
                "walk_slots",
            )
        },
    },
    "dram_fingerprint": backend["fingerprint"],
    "dram_parameters": backend["parameters"],
    "part_provenance": backend["part_provenance"],
    "verification": verification,
    "payload_read_bytes": expected_read,
    "payload_write_bytes": expected_write,
    "job_ticks": npu["jobTicks"],
    "roi_start_tick": start,
    "polled_roi_end_tick": end,
    "drained_roi_end_tick": drain_end,
    "polled_roi_ticks": end - start,
    "drain_extra_ticks": drain_end - end,
    "device_payload_gbps": (expected_read + expected_write)
    * 1000
    / npu["jobTicks"],
    "dram_job_read_bytes": dram_bytes(job, ".dram.dramBytesRead"),
    "dram_job_write_bytes": dram_bytes(job, ".dram.dramBytesWritten"),
    "dram_drained_read_bytes": dram_bytes(drained, ".dram.dramBytesRead"),
    "dram_drained_write_bytes": dram_bytes(drained, ".dram.dramBytesWritten"),
    "npu": npu,
    "smmu_stats": {
        k: v
        for k, v in job.items()
        if k.startswith(("system.smmu.", "system.ifc."))
    },
}
# Native DRAM stat names are checked rather than silently reporting zero.
assert report["dram_drained_read_bytes"] > 0
if expected_write:
    assert report["dram_drained_write_bytes"] >= expected_write
(Path(m5.options.outdir) / "npu-saturation.json").write_text(
    json.dumps(report, indent=2) + "\n"
)
print(
    "NPU_SATURATION_PASS", args.stage, args.mode, report["device_payload_gbps"]
)
