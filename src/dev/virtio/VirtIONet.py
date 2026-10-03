# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause

from m5.objects.Ethernet import EtherInt
from m5.objects.VirtIO import VirtIODeviceBase
from m5.params import (
    NextEthernetAddr,
    Param,
)


class VirtIONet(VirtIODeviceBase):
    type = "VirtIONet"
    cxx_header = "dev/virtio/net.hh"
    cxx_class = "gem5::VirtIONet"

    interface = EtherInt("Ethernet interface")
    hardware_address = Param.EthernetAddr(NextEthernetAddr, "MAC address")
    qSize = Param.Unsigned(256, "RX and TX virtqueue size")
    rxQueueSize = Param.Unsigned(64, "Maximum buffered receive packets")
    retry_delay = Param.Latency("1us", "Ethernet transmit retry interval")
