# SPDX-License-Identifier: BSD-3-Clause
"""NPU DT contract independent of a long Linux simulation."""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import (
    ModuleType,
    SimpleNamespace,
)
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "configs"))
sys.path.insert(0, str(ROOT / "src/python/m5/ext/pyfdt"))
import pyfdt
from zena_cmn.cpu_profile import cache_id_registers
from zena_cmn.npu_v01 import (
    add_npu_dtb,
    create_npu,
)
from zena_cmn.parameters import load_parameters


class NpuDtbTest(unittest.TestCase):
    def test_scaled_resources_reach_native_objects(self):
        class Smmu(SimpleNamespace):
            def connect(self, device):
                self.device_interfaces = [SimpleNamespace()]

        objects = ModuleType("m5.objects")
        for name in ("ArmSPI", "NpuV01", "SrcClockDomain"):
            setattr(objects, name, SimpleNamespace)
        objects.AddrRange = lambda address, size: (address, size)
        objects.SMMUv3 = Smmu
        profile_dir = ROOT / "configs/zena_cmn/profiles"
        for profile in ("a720ae_npu_v01.yaml", "a720ae_npu_saturation.yaml"):
            with self.subTest(profile=profile), patch.dict(
                sys.modules, {"m5": ModuleType("m5"), "m5.objects": objects}
            ):
                params = load_parameters(profile_dir / profile)
                spec = params["npu"]
                system = SimpleNamespace(
                    voltage_domain=object(),
                    platform=SimpleNamespace(),
                    iobus=SimpleNamespace(mem_side_ports=object()),
                )
                smmu = create_npu(system, params)
                npu = system.platform.npu
                for key in (
                    "experimental_scaling",
                    "dma_issue_width",
                    "dma_width",
                    "max_outstanding",
                    "sram_bank_width",
                    "bank_queue_depth",
                ):
                    self.assertEqual(getattr(npu, key), spec[key])
                self.assertEqual(smmu.request_port_width, spec["dma_width"])
                for key in ("xlate_slots", "ptw_slots"):
                    self.assertEqual(getattr(smmu, key), spec["smmu"][key])
                interface = smmu.device_interfaces[0]
                self.assertEqual(interface.port_width, spec["dma_width"])
                for key in (
                    "xlate_slots",
                    "wrbuf_slots",
                    "utlb_slots",
                    "tlb_slots",
                ):
                    self.assertEqual(
                        getattr(interface, key), spec["smmu"]["device_" + key]
                    )
                self.assertIs(interface.clk_domain, system.npu_clk_domain)

    def test_architectural_cache_geometry(self):
        p = load_parameters()
        clidr, ids = cache_id_registers(p)
        self.assertEqual((clidr >> 24) & 7, 3)
        self.assertEqual([clidr >> (3 * i) & 7 for i in range(3)], [3, 4, 4])
        for index, size in (
            (0, 64 * 1024),
            (1, 64 * 1024),
            (2, 512 * 1024),
            (4, 4 * 1024**2),
        ):
            value = ids[index]
            line = 1 << ((value & 7) + 4)
            ways = ((value >> 3) & 1023) + 1
            sets = ((value >> 13) & 32767) + 1
            self.assertEqual(line * ways * sets, size)
        self.assertEqual(ids[3], 0)
        p["cache"]["dsu_l3"]["size_mib_per_cluster"] = 1024
        with self.assertRaises(ValueError):
            cache_id_registers(p)

    def test_nc_payload_coherent_maintenance(self):
        modules = {
            name: ModuleType(name) for name in ("m5", "m5.ext", "m5.ext.pyfdt")
        }
        modules["m5.ext.pyfdt"].pyfdt = pyfdt
        root = pyfdt.FdtNode("/")
        root.append(pyfdt.FdtPropertyWords("phandle", [7]))
        tree = pyfdt.Fdt()
        tree.add_rootnode(root)
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            sys.modules, modules
        ):
            path = Path(directory) / "system.dtb"
            path.write_bytes(tree.to_dtb())
            add_npu_dtb(path)
            with path.open("rb") as source:
                properties = dict(
                    pyfdt.FdtBlobParse(source).to_fdt().get_rootnode().walk()
                )
            self.assertEqual(
                list(properties["/npu@b000000/iommus"]), [8, 0x100]
            )
            self.assertEqual(
                list(properties["/npu@b000000/interrupts"]),
                [0, 80, 4, 0, 81, 4],
            )
            self.assertIn("/iommu@b100000/dma-coherent", properties)
            self.assertNotIn("/npu@b000000/dma-coherent", properties)
            self.assertEqual(
                list(properties["/iommu@b100000/reg"]),
                [0, 0x0B100000, 0, 0x20000],
            )
            self.assertTrue(path.with_suffix(".dts").exists())


@unittest.skipUnless(os.environ.get("ZENA_CMN_RUN"), "requires actual config")
class NpuResourceVerificationTest(unittest.TestCase):
    def test_resource_mismatch_rejected(self):
        sys.path.insert(0, str(ROOT / "scripts/zena_cmn"))
        from verify_config import verify

        directory = Path(os.environ["ZENA_CMN_RUN"])
        config = json.loads((directory / "config.json").read_text())
        profile = json.loads((directory / "resolved-profile.json").read_text())
        if not profile.get("npu", {}).get("enabled", False):
            self.skipTest("requires NPU profile")
        self.assertEqual(verify(config, profile)["status"], "PASS")
        bad = copy.deepcopy(config)
        npu = bad["system"]["platform"]["npu"]
        smmu = bad["system"]["platform"]["npu_smmu"]
        npu["experimental_scaling"] = not profile["npu"].get(
            "experimental_scaling", False
        )
        npu["dma_issue_width"] = 999
        smmu["request_port_width"] = 999
        smmu["xlate_slots"] = 999
        interface = smmu["device_interfaces"][0]
        interface["port_width"] = 999
        for key in ("xlate_slots", "wrbuf_slots", "utlb_slots", "tlb_slots"):
            interface[key] = 999
        failures = verify(bad, profile)["failures"]
        for label in (
            "NPU experimental_scaling",
            "NPU dma_issue_width",
            "SMMU request port width",
            "SMMU xlate_slots",
            "SMMU device port width",
            "SMMU device xlate_slots",
            "SMMU device wrbuf_slots",
            "SMMU device utlb_slots",
            "SMMU device tlb_slots",
        ):
            self.assertIn(label, failures)


if __name__ == "__main__":
    unittest.main()
