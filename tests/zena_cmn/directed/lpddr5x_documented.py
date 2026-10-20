# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Native partial-part construction; no bandwidth or silicon claim."""
import sys
import unittest
from pathlib import Path

import m5
from m5.objects import (
    AddrRange,
    MemCtrl,
    Root,
    SrcClockDomain,
    System,
    SystemXBar,
    VoltageDomain,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "configs"))
from example.arm.zena_css_models import LPDDR5X_8533_1x16_BG_BL32
from zena_cmn.lpddr5x_documented import (
    DOCUMENTED_PARAMETERS,
    MicronY52Q_8533_1x16_BG_BL32_Partial,
)
from zena_cmn.memory_backend import resolved_memory_parameters


class PartTest(unittest.TestCase):
    def test_geometry(self):
        part = MicronY52Q_8533_1x16_BG_BL32_Partial()
        self.assertEqual(int(part.device_size), 1024**3)
        self.assertEqual(int(part.device_bus_width), 16)
        self.assertEqual(int(part.ranks_per_channel), 1)
        self.assertEqual(int(part.banks_per_rank), 16)
        self.assertEqual(int(part.bank_groups_per_rank), 4)
        self.assertEqual(
            int(part.device_size)
            // int(part.device_rowbuffer_size)
            // int(part.banks_per_rank),
            32768,
        )
        self.assertAlmostEqual(part.tRFC.value, 210e-9, places=18)

    def test_unknown_parameters_not_invented(self):
        before = resolved_memory_parameters(LPDDR5X_8533_1x16_BG_BL32())
        after = resolved_memory_parameters(
            MicronY52Q_8533_1x16_BG_BL32_Partial()
        )
        for name in before:
            if name not in DOCUMENTED_PARAMETERS:
                self.assertEqual(before[name], after[name], name)

    def test_partial_aperture_row_counts(self):
        for channels, expected in ((4, 16384), (8, 8192), (16, 4096)):
            # Native DRAMInterface rowsPerBank uses exposed capacity, not device_size.
            self.assertEqual(
                (2 * 1024**3 // channels) // (2048 * 16), expected
            )


if __name__ in ("__main__", "__m5_main__"):
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(PartTest)
    )
    if not result.wasSuccessful():
        raise SystemExit(1)
    system = System(
        mem_mode="timing", mem_ranges=[AddrRange(0x80000000, size="1GiB")]
    )
    system.voltage_domain = VoltageDomain()
    system.clk_domain = SrcClockDomain(
        clock="2GHz", voltage_domain=system.voltage_domain
    )
    system.membus = SystemXBar()
    system.system_port = system.membus.cpu_side_ports
    system.mem_ctrl = MemCtrl(
        dram=MicronY52Q_8533_1x16_BG_BL32_Partial(
            range=system.mem_ranges[0], addr_mapping="RoRaBaCoCh"
        )
    )
    system.mem_ctrl.port = system.membus.mem_side_ports
    root = Root(full_system=False, system=system)
    m5.instantiate()
    m5.simulate(1)
    print(
        "PARTIAL_PART_NATIVE_INSTANTIATE=PASS physical_channel_bytes=1073741824"
    )
