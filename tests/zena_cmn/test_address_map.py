# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Run with gem5.opt to exercise C++ AddrRange membership, not a fake."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "configs"))
from zena_cmn.address_map import (
    striped_ranges,
    validate_ranges,
)
from zena_cmn.memory_backend import build_memory_backend
from zena_cmn.parameters import load_parameters

from m5.objects import (
    AddrRange,
    System,
)


class AddressMapTest(unittest.TestCase):
    def test_native_cpu_profile(self):
        from zena_cmn.cpu_profile import create_cpu

        from m5.objects import ArmO3CPU

        params = load_parameters()
        cpu = create_cpu(params, 0)
        other = create_cpu(params, 1, switched_out=True)
        atomic = create_cpu(params, 0, atomic=True)
        self.assertEqual(int(cpu.numROBEntries), 192)
        self.assertEqual(int(cpu.LQEntries), 32)
        self.assertEqual(int(cpu.SQEntries), 64)
        self.assertTrue(bool(other.switched_out))
        self.assertEqual(atomic.type, "BaseAtomicSimpleCPU")
        if "instQueues" in cpu._params:
            self.assertEqual(len(cpu.instQueues), 1)
            self.assertEqual(int(cpu.instQueues[0].numEntries), 128)
            self.assertEqual(
                type(cpu.instQueues[0].fuPool),
                type(ArmO3CPU().instQueues[0].fuPool),
            )
            cpu.instQueues[0].numEntries = 96
            self.assertEqual(int(other.instQueues[0].numEntries), 128)
        else:
            self.assertEqual(int(cpu.numIQEntries), 128)

    def test_lq_beyond_native_writeback_window_rejected(self):
        from zena_cmn.cpu_profile import create_cpu

        params = load_parameters(overrides={"cpu.lq_entries": 64})
        with self.assertRaisesRegex(
            ValueError, "native IEW writeback window=48"
        ):
            create_cpu(params, 0)
        with self.assertRaisesRegex(
            ValueError, "native IEW writeback window=48"
        ):
            create_cpu(params, 0, switched_out=True)

    def test_native_partition(self):
        for channels in (4, 8, 16):
            for stripe in (64, 512, 1024):
                with self.subTest(channels=channels, stripe=stripe):
                    base = AddrRange(0x80000000, size="2GiB")
                    result = validate_ranges(
                        base,
                        striped_ranges(base, 8),
                        striped_ranges(base, channels, stripe),
                        stripe,
                    )
                    self.assertEqual(result["status"], "PASS")
                    self.assertEqual(result["capacity_bytes"], 2 * 1024**3)
                    self.assertEqual(result["home_select"], "(PA >> 6) & 7")
                    self.assertEqual(
                        result["channel_interleave_bytes"], stripe
                    )

    def test_invalid_stripe(self):
        base = AddrRange(0x80000000, size="2GiB")
        for stripe in (0, 32, 128, 513, 2048, True, 64.0):
            with self.subTest(stripe=stripe), self.assertRaises(ValueError):
                striped_ranges(base, 16, stripe)

    def test_channel_and_home_selectors_are_independent(self):
        base = AddrRange(0x80000000, size="2GiB")
        homes = striped_ranges(base, 8)
        channels = striped_ranges(base, 16, 512)
        with self.assertRaises(ValueError):
            validate_ranges(base, homes, channels, 64)
        with self.assertRaises(ValueError):
            validate_ranges(base, striped_ranges(base, 8, 512), channels, 512)

    def test_wrong_selector(self):
        base = AddrRange(0x80000000, size="2GiB")
        homes, channels = striped_ranges(base, 8), striped_ranges(base, 16)
        channels[0].intlvMatch = 1
        with self.assertRaises(ValueError):
            validate_ranges(base, homes, channels)

    def test_backend_once(self):
        for count in (4, 8, 16):
            fingerprint = None
            for stripe in (64, 512, 1024):
                with self.subTest(channels=count, stripe=stripe):
                    system = System(
                        mem_ranges=[AddrRange(0x80000000, size="2GiB")]
                    )
                    params = load_parameters(
                        overrides={
                            "memory.channels": count,
                            "memory.interleave_bytes": stripe,
                        }
                    )
                    backend = build_memory_backend(system, params)
                    if fingerprint is None:
                        fingerprint = backend["fingerprint"]
                    self.assertEqual(backend["fingerprint"], fingerprint)
                    self.assertEqual(len(backend["controllers"]), count)
                    self.assertAlmostEqual(
                        backend["controllers"][0].dram.tCK.value,
                        0.9375e-9,
                        places=20,
                    )
                    self.assertEqual(
                        backend["channel_mapping"],
                        f"(PA >> {stripe.bit_length() - 1}) & (channels - 1)",
                    )
                    with self.assertRaises(ValueError):
                        build_memory_backend(system, params)

    def test_existing_factory_parameters_preserved(self):
        from zena_cmn.memory_backend import resolved_memory_parameters

        from m5.objects import SystemXBar

        # First import through backend discovery, exactly like production.
        for count in (4, 8, 16):
            system = System(mem_ranges=[AddrRange(0x80000000, size="2GiB")])
            new = build_memory_backend(
                system, load_parameters(overrides={"memory.channels": count})
            )
            from zena_css_models import configure_memory

            old = System(
                mem_ranges=[AddrRange(0x80000000, size="2GiB")],
                membus=SystemXBar(),
            )
            configure_memory(
                old,
                {
                    "memory_model": "lpddr5x-8533",
                    "memory_channels": count,
                    "dram_device_size_bytes": 2 * 1024**3 // count,
                    "dram_page_policy": "close_adaptive",
                    "dram_scheduler": "frfcfs",
                },
            )
            for before, after in zip(old.mem_ctrls, new["controllers"]):
                self.assertEqual(
                    resolved_memory_parameters(before),
                    resolved_memory_parameters(after),
                )
                self.assertEqual(
                    resolved_memory_parameters(before.dram),
                    resolved_memory_parameters(after.dram),
                )
                self.assertEqual(str(before.dram.range), str(after.dram.range))

    def test_historical_cpu_profile_without_npu(self):
        params = load_parameters()
        del params["npu"]
        system = System(mem_ranges=[AddrRange(0x80000000, size="2GiB")])
        backend = build_memory_backend(system, params)
        self.assertFalse(backend["future_attachment"]["arbiter_instantiated"])
        self.assertNotIn("memory_bus", system._children)

    def test_no_missing_class_fallback(self):
        system = System(mem_ranges=[AddrRange(0x80000000, size="2GiB")])
        params = load_parameters(
            overrides={
                "memory.custom_module": "no_such_lpddr5x_module",
                "memory.custom_class": "Missing",
            }
        )
        with self.assertRaises(ValueError):
            build_memory_backend(system, params)


if __name__ in ("__main__", "__m5_main__"):
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AddressMapTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
