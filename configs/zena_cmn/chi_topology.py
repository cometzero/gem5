# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""CMN-like placement using native CustomMesh RN-F local routers."""

from types import SimpleNamespace

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


def place_node_type(node_type, routers):
    node_type.NoC_Params = type(
        "NoC_Params",
        (node_type.NoC_Params,),
        {"router_list": routers, "num_nodes_per_router": None},
    )


def build_topology(network, nodes, params):
    mesh = params["cmn"]["mesh"]
    options = SimpleNamespace(
        num_rows=mesh["rows"],
        num_cols=mesh["columns"],
        network=params["cmn"].get("network", "garnet"),
        router_link_latency=mesh["router_link_latency_cycles"],
        node_link_latency=mesh["node_link_latency_cycles"],
        router_latency=mesh["router_latency_cycles"],
        cross_links=[],
        cross_link_latency=0,
    )
    if options.network == "garnet":
        CustomMesh(nodes).makeTopology(
            options, network, GarnetIntLink, GarnetExtLink, GarnetRouter
        )
        network.netifs = [
            GarnetNetworkInterface(id=index)
            for index in range(len(network.ext_links))
        ]
    else:
        network.netifs = []
        CustomMesh(nodes).makeTopology(
            options, network, SimpleIntLink, SimpleExtLink, Switch
        )
        for router in network.routers:
            # Switch defaults capture BasicRouter.latency, not this instance.
            router.int_routing_latency = router.latency
            router.ext_routing_latency = router.latency
        for link in network.ext_links + network.int_links:
            link.bandwidth_factor = mesh["flit_bytes"]
        network.setup_buffers()
        # Native SimpleIntLink defaults to unbounded queues with shared vnets.
        for link in network.int_links:
            for buffer in link.buffers:
                buffer.buffer_size = network.buffer_size
