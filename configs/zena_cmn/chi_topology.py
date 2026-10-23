# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""CMN-like placement using native CustomMesh RN-F local routers."""

from ruby.CHI_config import NoC_Params
from topologies.CustomMesh import CustomMesh

from m5.objects import (
    GarnetExtLink,
    GarnetIntLink,
    GarnetNetworkInterface,
    GarnetRouter,
    SimpleExtLink,
    SimpleIntLink,
    Switch,
)


def noc_parameters(params):
    """Map the profile before CHI constructs its controller message buffers."""
    cmn = params["cmn"]
    mesh = cmn["mesh"]
    noc = NoC_Params()
    noc.network = cmn.get("network", "garnet")
    noc.num_rows = mesh["rows"]
    noc.num_cols = mesh["columns"]
    noc.router_link_latency = mesh["router_link_latency_cycles"]
    noc.node_link_latency = mesh["node_link_latency_cycles"]
    # The profile specifies one routing latency for both router outputs.
    noc.router_int_latency = mesh["router_latency_cycles"]
    noc.router_ext_latency = mesh["router_latency_cycles"]
    noc.data_width = cmn["data_channel_bytes"]
    noc.custom_links = {}
    # Retain the existing shared-vnet Simple diagnostic transport contract.
    # Garnet buffering is configured separately in flits by build_ruby().
    noc.router_buffer_size = 32
    return noc


def place_node_type(node_type, routers, noc):
    node_type.NoC_Params = type(
        "NoC_Params",
        (node_type.NoC_Params,),
        {
            "router_list": routers,
            "num_nodes_per_router": None,
            "gbl_noc_params": noc,
            "inbound_link_latency": noc.node_link_latency,
            "outbound_link_latency": noc.node_link_latency,
        },
    )


def build_topology(network, nodes, params, noc):
    mesh = params["cmn"]["mesh"]
    if noc.network == "garnet":
        CustomMesh(nodes).makeTopology(
            noc, network, GarnetIntLink, GarnetExtLink, GarnetRouter
        )
        network.netifs = [
            GarnetNetworkInterface(id=index)
            for index in range(len(network.ext_links))
        ]
    else:
        network.netifs = []
        CustomMesh(nodes).makeTopology(
            noc, network, SimpleIntLink, SimpleExtLink, Switch
        )
        for link in network.ext_links + network.int_links:
            link.bandwidth_factor = mesh["flit_bytes"]
        network.setup_buffers()
        # Native SimpleIntLink defaults to unbounded queues with shared vnets.
        for link in network.int_links:
            for buffer in link.buffers:
                buffer.buffer_size = network.buffer_size
