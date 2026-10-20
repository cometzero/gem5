# SPDX-License-Identifier: BSD-3-Clause
from m5.objects.Device import BasicPioDevice
from m5.objects.Gic import ArmInterruptPin
from m5.params import *


class NpuV01(BasicPioDevice):
    type = "NpuV01"
    cxx_header = "dev/npu/npu_v01.hh"
    cxx_class = "gem5::NpuV01"

    enable_async_dma = Param.Bool(
        False, "Enable explicit FUNC_OPS_ASYNC variant"
    )
    experimental_scaling = Param.Bool(
        False, "Permit diagnostic resource scaling beyond the v0.1 baseline"
    )
    dma = RequestPort("Noncoherent IOVA requests through SMMUv3")
    interrupt = Param.ArmInterruptPin(NULL, "Job completion IRQ")
    fault_interrupt = Param.ArmInterruptPin(NULL, "Job fault IRQ")
    stream_id = Param.Unsigned(0x100, "SMMUv3 stream ID")
    max_outstanding = Param.Unsigned(16, "DMA packet and staging credits")
    dma_width = Param.Unsigned(16, "External data bytes per NPU cycle")
    dma_issue_width = Param.Unsigned(1, "Accepted DMA packets per NPU cycle")
    sram_size = Param.MemorySize("2MiB", "Private SRAM capacity")
    sram_banks = Param.Unsigned(16, "Interleaved SRAM banks")
    sram_bank_width = Param.Unsigned(4, "Bytes per bank beat")
    sram_latency = Param.Cycles(2, "SRAM response latency")
    bank_queue_depth = Param.Unsigned(4, "Accepted beats per bank")
