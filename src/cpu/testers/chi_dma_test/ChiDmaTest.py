# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
from m5.objects.ClockedObject import ClockedObject
from m5.params import *
from m5.proxy import Parent


class ChiDmaTest(ClockedObject):
    type = "ChiDmaTest"
    cxx_header = "cpu/testers/chi_dma_test/chi_dma_test.hh"
    cxx_class = "gem5::ChiDmaTest"
    ports = VectorRequestPort("CPU sequencers and external noncoherent DMA")
    system = Param.System(Parent.any, "Requestor ID owner")
    masters = VectorParam.Unsigned([], "Port index per operation")
    commands = VectorParam.Unsigned(
        [],
        "0 read, 1 write, 2 clean, 3 invalidate, 4 clean+invalidate, 5 NC read, 6 NC write",
    )
    addresses = VectorParam.Addr([], "Physical address per operation")
    values = VectorParam.UInt64([], "Written or expected 8-byte value")
    batches = VectorParam.Unsigned(
        [],
        "Same consecutive batch ID issues concurrently; empty means sequential",
    )
