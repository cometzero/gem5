# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""CHI orchestration with one native DRAM backend."""

from ruby.CHI_config import (
    CHI_HNF,
    CHI_MN,
    CHI_RNI_DMA,
    CHI_RNI_IO,
    CHI_SNF_BootMem,
    CHI_SNF_MainMem,
    Versions,
)
from zena_cmn.chi_topology import (
    build_topology,
    noc_parameters,
    place_node_type,
)
from zena_cmn.cluster_rnf import ZenaClusterRNF
from zena_cmn.memory_backend import build_memory_backend

from m5.defines import buildEnv
from m5.objects import (
    GarnetNetwork,
    RubyCache,
    RubySystem,
    SimpleNetwork,
    SrcClockDomain,
)


def build_ruby(
    system, params, cpus, dma_ports=(), bootmem=None, full_system=True
):
    if buildEnv["PROTOCOL"] != "CHI":
        raise ValueError("Zena CMN requires CHI to be selected in this build")
    if len(cpus) != params["cpu"]["cores"]:
        raise ValueError("CPU count does not match the resolved profile")
    system.ruby = RubySystem()
    ruby = system.ruby
    ruby.clk_domain = SrcClockDomain(
        clock=params["ruby"]["clock"], voltage_domain=system.voltage_domain
    )
    ruby.block_size_bytes = params["cache"]["line_bytes"]
    cmn = params["cmn"]
    mesh = cmn["mesh"]
    noc = noc_parameters(params)
    network_type = noc.network
    network_args = dict(
        ruby_system=ruby,
        topology="CustomMesh",
        number_of_virtual_networks=4,
        control_msg_size=noc.cntrl_msg_size,
        data_msg_size=noc.data_width,
        clk_domain=SrcClockDomain(
            clock=mesh["clock"], voltage_domain=system.voltage_domain
        ),
    )
    if network_type == "garnet":
        ruby.network = GarnetNetwork(
            ni_flit_size=mesh["flit_bytes"],
            vcs_per_vnet=mesh["vcs_per_vnet"],
            buffers_per_ctrl_vc=mesh["control_buffer_flits"],
            buffers_per_data_vc=mesh["data_buffer_flits"],
            # RN-F routers make this a non-rectangular table-routed graph.
            num_rows=0,
            **network_args,
        )
    elif network_type == "simple":
        # Message-level diagnostic: no Garnet VC/tail-flit timing equivalence.
        ruby.network = SimpleNetwork(
            buffer_size=noc.router_buffer_size,
            endpoint_bandwidth=1000,
            **network_args,
        )
    else:
        raise ValueError(f"unsupported CHI network: {network_type}")
    # v26 CHI consumes the node's NoC settings inside connectController(),
    # before topology construction. Set every node type before creating it.
    place_node_type(ZenaClusterRNF, mesh["cluster_attach"], noc)
    place_node_type(CHI_HNF, mesh["home_attach"], noc)
    channels = params["memory"]["channels"]
    # Reduced channel layouts retain even spacing across the same mesh.
    place_node_type(
        CHI_SNF_MainMem,
        mesh["snf_attach_for_16_channels"][:: 16 // channels],
        noc,
    )
    for auxiliary in (CHI_MN, CHI_SNF_BootMem, CHI_RNI_DMA, CHI_RNI_IO):
        place_node_type(auxiliary, [0], noc)
    backend = build_memory_backend(system, params)
    ruby._memory_backend = backend
    group_size = params["cpu"]["cores_per_cluster"]
    ruby.rnf = [
        ZenaClusterRNF(cpus[start : start + group_size], ruby, params)
        for start in range(0, len(cpus), group_size)
    ]
    ruby.mn = [CHI_MN(ruby, [cpu.l1d for cpu in cpus])]
    all_ranges = list(system.mem_ranges)
    if bootmem is not None:
        all_ranges.append(bootmem.range)
    CHI_HNF.createAddrRanges(
        all_ranges, params["cache"]["line_bytes"], range(cmn["home_count"])
    )
    home_cache = RubyCache(
        size=f'{cmn["slc_size_kib_per_home"]}KiB',
        assoc=cmn["slc_assoc"],
        tagAccessLatency=cmn["slc_tag_latency_cycles"],
        dataAccessLatency=cmn["slc_data_latency_cycles"],
        tagArrayBanks=cmn["slc_tag_banks"],
        dataArrayBanks=cmn["slc_data_banks"],
        resourceStalls=params["cache"]["resource_stalls"],
    )
    ruby.hnf = [
        CHI_HNF(index, ruby, home_cache, None)
        for index in range(cmn["home_count"])
    ]
    for node in ruby.hnf:
        node.cntrl.number_of_TBEs = cmn["home_request_tbes"]
        node.cntrl.enable_DMT = cmn["dmt"]
        node.cntrl.enable_DCT = cmn["dct"]
        if cmn["slc_mode"] == "directory_only":
            for operation in (
                "seq_acc",
                "seq_line_write",
                "readshared",
                "readunique",
                "readonce",
                "writeback",
                "atomic",
            ):
                setattr(node.cntrl, "alloc_on_" + operation, False)
    shared = params["npu"]["enabled"]
    ruby.snf = [
        CHI_SNF_MainMem(ruby, None, None if shared else controller)
        for controller in backend["controllers"]
    ]
    if shared:
        for node, region in zip(ruby.snf, backend["ranges"]):
            node.cntrl.memory_out_port = system.memory_bus.cpu_side_ports
            node.cntrl.addr_ranges = [region]
    for node in ruby.snf:
        node.cntrl.wait_for_write_ack = shared
        node.cntrl.number_of_TBEs = params["memory"]["snf_request_tbes"]
    nodes = list(ruby.rnf) + list(ruby.mn) + list(ruby.hnf) + list(ruby.snf)
    mem_destinations = [node.cntrl for node in ruby.snf]
    if bootmem is not None:
        ruby.rom_snf = [CHI_SNF_BootMem(ruby, None, bootmem)]
        nodes.extend(ruby.rom_snf)
        mem_destinations.extend(node.cntrl for node in ruby.rom_snf)
    dma_nodes = [CHI_RNI_DMA(ruby, port, None) for port in dma_ports]
    if dma_nodes:
        ruby.dma_rni = dma_nodes
    nodes.extend(dma_nodes)
    if full_system:
        ruby.io_rni = CHI_RNI_IO(ruby, None)
        nodes.append(ruby.io_rni)
    homes = [node.cntrl for node in ruby.hnf]
    for node in list(ruby.rnf) + dma_nodes:
        node.setDownstream(homes)
    if full_system:
        ruby.io_rni.setDownstream(homes)
    for node in ruby.hnf:
        node.setDownstream(mem_destinations)
    for node in nodes:
        for controller in node.getAllControllers():
            # Upstream connectController uses the module-level data_width,
            # so resolve the project's width on every assembled controller.
            controller.data_channel_size = noc.data_width
    if params["ruby"]["timing_dvm"]:
        for cpu in cpus:
            for decoder in cpu.decoder:
                decoder.dvm_enabled = True
    build_topology(ruby.network, nodes, params, noc)
    ruby.number_of_virtual_networks = 4
    ruby._cpu_ports = [
        seq for node in ruby.rnf for seq in node.getSequencers()
    ]
    ruby.num_of_sequencers = Versions._seqs
    return ruby
