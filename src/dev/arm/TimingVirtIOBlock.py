# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
from m5.objects.Device import DmaDevice
from m5.objects.Gic import ArmInterruptPin
from m5.params import *


class TimingVirtIOBlock(DmaDevice):
    type = "TimingVirtIOBlock"
    cxx_header = "dev/arm/timing_virtio_block.hh"
    cxx_class = "gem5::TimingVirtIOBlock"

    pio_addr = Param.Addr("VirtIO MMIO version 2 base")
    pio_size = Param.Addr(0x200, "MMIO register window size")
    pio_latency = Param.Latency("100ns", "Register access latency")
    interrupt = Param.ArmInterruptPin("Used ring/configuration interrupt")
    image = Param.DiskImage("Block image, normally a private COW overlay")
    queue_size = Param.Unsigned(128, "Single split queue maximum descriptors")
    request_latency = Param.Latency("10us", "Read/write service latency")
    bandwidth = Param.MemoryBandwidth(
        "100MiB/s", "Read/write service bandwidth"
    )
    flush_latency = Param.Latency("100us", "Ordered simulated flush latency")
