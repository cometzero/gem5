# SPDX-License-Identifier: BSD-3-Clause
"""Opt-in NPU payload outside CHI, coherent SMMU maintenance inside CHI."""

NPU_BASE = 0x0B000000
NPU_SIZE = 0x10000
SMMU_BASE = 0x0B100000
SMMU_SIZE = 0x20000
COMPLETION_INTID = 112
FAULT_INTID = 113
EVENT_INTID = 114
STREAM_ID = 0x100
SMMU_IDR0 = (0x094C100F & ~(3 << 27)) | (1 << 4)
SMMU_IDR1 = (7 << 21) | (7 << 16) | 9


def create_npu(system, params):
    from m5.objects import (
        AddrRange,
        ArmSPI,
        NpuV01,
        SMMUv3,
        SrcClockDomain,
    )

    spec = params["npu"]
    if not spec["enabled"]:
        return None
    system.npu_clk_domain = SrcClockDomain(
        clock=spec["clock"], voltage_domain=system.voltage_domain
    )
    system.platform.npu = NpuV01(
        pio_addr=NPU_BASE,
        clk_domain=system.npu_clk_domain,
        interrupt=ArmSPI(num=COMPLETION_INTID),
        fault_interrupt=ArmSPI(num=FAULT_INTID),
        stream_id=STREAM_ID,
        sram_size=str(spec["sram_size"]) + "B",
        **{
            name: spec[name]
            for name in (
                "sram_banks",
                "sram_bank_width",
                "sram_latency",
                "bank_queue_depth",
                "max_outstanding",
                "dma_width",
                "experimental_scaling",
                "dma_issue_width",
            )
        },
    )
    if spec["enable_async_dma"]:
        system.platform.npu.enable_async_dma = True
    system.platform.npu.pio = system.iobus.mem_side_ports
    system.platform.npu_smmu = SMMUv3(
        reg_map=AddrRange(SMMU_BASE, size=SMMU_SIZE),
        clk_domain=system.npu_clk_domain,
        smmu_idr0=SMMU_IDR0,
        smmu_idr1=SMMU_IDR1,
        request_port_width=spec["dma_width"],
        xlate_slots=spec["smmu"]["xlate_slots"],
        ptw_slots=spec["smmu"]["ptw_slots"],
        eventq_irq=ArmSPI(num=EVENT_INTID),
    )
    smmu = system.platform.npu_smmu
    smmu.control = system.iobus.mem_side_ports
    smmu.connect(system.platform.npu)
    for interface in smmu.device_interfaces:
        interface.clk_domain = system.npu_clk_domain
        interface.port_width = spec["dma_width"]
        for field in ("xlate_slots", "wrbuf_slots", "utlb_slots", "tlb_slots"):
            setattr(interface, field, spec["smmu"]["device_" + field])
    return smmu


def add_npu_dtb(filename):
    from pathlib import Path

    from m5.ext.pyfdt import pyfdt

    with open(filename, "rb") as source:
        tree = pyfdt.FdtBlobParse(source).to_fdt()
    root = tree.get_rootnode()
    used = [
        value[0]
        for path, value in root.walk()
        if path.endswith("/phandle") or path.endswith("/linux,phandle")
    ]
    handle = max(used, default=0) + 1

    def words(node, name, values):
        node.append(pyfdt.FdtPropertyWords(name, values))

    def strings(node, name, values):
        node.append(pyfdt.FdtPropertyStrings(name, values))

    smmu = pyfdt.FdtNode(f"iommu@{SMMU_BASE:x}")
    strings(smmu, "compatible", ["arm,smmu-v3"])
    words(smmu, "reg", [0, SMMU_BASE, 0, SMMU_SIZE])
    words(smmu, "#iommu-cells", [1])
    words(smmu, "phandle", [handle])
    words(smmu, "interrupts", [0, EVENT_INTID - 32, 4])
    strings(smmu, "interrupt-names", ["eventq"])
    smmu.append(pyfdt.FdtProperty("dma-coherent"))
    root.append(smmu)
    npu = pyfdt.FdtNode(f"npu@{NPU_BASE:x}")
    strings(npu, "compatible", ["qemu,npu-v0.1"])
    words(npu, "reg", [0, NPU_BASE, 0, NPU_SIZE])
    words(npu, "iommus", [handle, STREAM_ID])
    words(
        npu,
        "interrupts",
        [
            0,
            COMPLETION_INTID - 32,
            4,
            0,
            FAULT_INTID - 32,
            4,
        ],
    )
    strings(npu, "interrupt-names", ["completion", "fault"])
    root.append(npu)
    Path(filename).write_bytes(tree.to_dtb())
    Path(filename).with_suffix(".dts").write_text(tree.to_dts())
