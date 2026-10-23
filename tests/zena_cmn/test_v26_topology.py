# SPDX-License-Identifier: BSD-3-Clause
"""Run with gem5.opt: actual SimObject configuration, without instantiate()."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "configs"))
from ruby.CHI_config import (
    CHI_HNF,
    Versions,
)
from zena_cmn.parameters import load_parameters
from zena_cmn.ruby_system_builder import build_ruby

from m5.objects import (
    AddrRange,
    PyTrafficGen,
    SimpleMemory,
    SrcClockDomain,
    SubSystem,
    System,
    VoltageDomain,
)


class TopologyV26Test(unittest.TestCase):
    def check_profile(self, network_type, nondefault=False):
        overrides = {"cmn.network": network_type}
        if nondefault:
            overrides.update(
                {
                    "cmn.mesh.router_latency_cycles": 3,
                    "cmn.mesh.router_link_latency_cycles": 2,
                    "cmn.mesh.node_link_latency_cycles": 4,
                    "cmn.data_channel_bytes": 64,
                    "memory.channels": 4,
                }
            )
        params = load_parameters(overrides=overrides)
        mesh = params["cmn"]["mesh"]
        # Each test constructs an independent configuration in this process.
        Versions._seqs, Versions._version, CHI_HNF._addr_ranges = 0, {}, {}
        system = System(mem_mode="timing", mem_ranges=[AddrRange("2GiB")])
        system.voltage_domain = VoltageDomain()
        system.clk_domain = SrcClockDomain(
            clock=params["ruby"]["clock"], voltage_domain=system.voltage_domain
        )
        system.endpoints = [SubSystem() for _ in range(params["cpu"]["cores"])]
        system.dma = PyTrafficGen()
        system.bootmem = SimpleMemory(range=AddrRange(0x80000000, size="2MiB"))
        ruby = build_ruby(
            system,
            params,
            system.endpoints,
            dma_ports=[system.dma.port],
            bootmem=system.bootmem,
        )
        nodes = (
            list(ruby.rnf)
            + list(ruby.mn)
            + list(ruby.hnf)
            + list(ruby.snf)
            + list(ruby.rom_snf)
            + list(ruby.dma_rni)
            + [ruby.io_rni]
        )
        simple = network_type == "simple"
        node_latency = mesh["node_link_latency_cycles"]
        for node in nodes:
            self.assertEqual(
                node.NoC_Params.gbl_noc_params.network, network_type
            )
            for ctrl in node.getAllControllers():
                self.assertEqual(
                    int(ctrl.data_channel_size),
                    params["cmn"]["data_channel_bytes"],
                )
                for name in ("request", "response", "snoop", "data"):
                    self.assertEqual(
                        int(getattr(ctrl, name + "_latency")), node_latency
                    )
                for port in ("req", "snp", "rsp", "dat"):
                    for direction in ("In", "Out"):
                        buffer = getattr(ctrl, port + direction)
                        bounded = simple and (
                            direction == "In"
                            or ctrl.type == "CHI_Cache_Controller"
                        )
                        self.assertEqual(int(buffer.max_dequeue_rate), 1)
                        self.assertEqual(
                            int(buffer.buffer_size),
                            node_latency + 1 if bounded else 0,
                        )
        network = ruby.network
        mesh_count = mesh["rows"] * mesh["columns"]
        self.assertEqual(
            len(network.routers), mesh_count + params["cpu"]["clusters"]
        )
        self.assertEqual(
            int(network.data_msg_size), params["cmn"]["data_channel_bytes"]
        )
        for index, router in enumerate(network.routers):
            latency = (
                mesh["router_latency_cycles"]
                if index < mesh_count
                else (0 if simple else 1)
            )
            fields = (
                ("int_routing_latency", "ext_routing_latency")
                if simple
                else ("latency",)
            )
            for field in fields:
                self.assertEqual(int(getattr(router, field)), latency)
        for link in network.ext_links:
            self.assertEqual(int(link.latency), node_latency)
        for link in network.int_links:
            local = (
                max(int(link.src_node.router_id), int(link.dst_node.router_id))
                >= mesh_count
            )
            self.assertEqual(
                int(link.latency),
                node_latency if local else mesh["router_link_latency_cycles"],
            )
        if simple:
            self.assertEqual(list(network.netifs), [])
            self.assertEqual(list(network.physical_vnets_channels), [])
            self.assertEqual(list(network.physical_vnets_bandwidth), [])
            self.assertEqual(int(network.buffer_size), 32)
            for link in list(network.ext_links) + list(network.int_links):
                self.assertEqual(
                    int(link.bandwidth_factor), mesh["flit_bytes"]
                )
            for link in network.int_links:
                self.assertEqual(len(link.buffers), 4)
                self.assertTrue(
                    all(
                        int(buffer.buffer_size) == 32
                        for buffer in link.buffers
                    )
                )
        else:
            self.assertEqual(int(network.ni_flit_size), mesh["flit_bytes"])
            self.assertEqual(len(network.netifs), len(network.ext_links))

    def test_garnet_default(self):
        self.check_profile("garnet")

    def test_garnet_nondefault(self):
        self.check_profile("garnet", nondefault=True)

    def test_simple_default(self):
        self.check_profile("simple")

    def test_simple_nondefault(self):
        self.check_profile("simple", nondefault=True)


if __name__ in ("__main__", "__m5_main__"):
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TopologyV26Test)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
