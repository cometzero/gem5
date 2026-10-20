# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Check native Garnet framing and one unambiguous private-cache transaction."""
import argparse
import json
import math
import re
from pathlib import Path


def objects(value):
    if isinstance(value, dict):
        if "path" in value:
            yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def contract(config):
    by_path = {obj["path"]: obj for obj in objects(config)}
    net = config["system"]["ruby"]["network"]
    if net["type"] != "GarnetNetwork":
        raise ValueError("requires native Garnet network")
    clock = net["clk_domain"]
    if isinstance(clock, str):
        clock = by_path[clock]
    period = clock["clock"][0]
    payload, header, width = (
        net["data_msg_size"],
        net["control_msg_size"],
        net["ni_flit_size"],
    )
    if min(payload, header, width, period) <= 0:
        raise ValueError("invalid transport dimensions")
    line = config["system"]["ruby"]["block_size_bytes"]
    if line % payload:
        raise ValueError("only whole payload chunks supported")
    data_flits = math.ceil((payload + header) / width)
    cluster_routers = {
        link["int_node"]
        for link in net["ext_links"]
        if link["ext_node"].endswith(".dsu_l3")
    }
    if not cluster_routers:
        raise ValueError("no DSU cluster routers found")
    cuts = []
    for router in sorted(cluster_routers):
        incoming = [
            link for link in net["int_links"] if link["dst_node"] == router
        ]
        if len(incoming) != 1:
            raise ValueError("requires one mesh ingress per cluster")
        link = incoming[0]
        if (
            link["width"] != width
            or link["network_link"]["clk_domain"] != clock["path"]
        ):
            raise ValueError("mixed link width/clock is not supported")
        cuts.append(link["path"])
    return {
        "cluster_ingress_links": cuts,
        "payload_bytes_per_cluster_link_cycle": payload / data_flits,
        "model": "native_garnet_serialized_generic_header",
        "payload_bytes": payload,
        "generic_header_bytes": header,
        "flit_bytes": width,
        "data_flits_per_packet": data_flits,
        "control_flits_per_packet": math.ceil(header / width),
        "data_packets_per_full_line": line // payload,
        "wire_bytes_per_full_line": line // payload * data_flits * width,
        "link_period_ticks": period,
        "cut_scope": "one directed link; excludes own-cluster local L3 hits",
        "qualification": "model accounting, not physical CHI flit encoding",
    }


def framing(stats, expected):
    vectors = {}
    for name in ("packets_injected", "flits_injected"):
        rows = re.findall(
            r"^system\.ruby\.network\." + name + r"\s+\|([^\n]+)",
            stats,
            re.M,
        )
        vectors[name] = [
            [int(cell.split()[0]) for cell in row.split("|")] for row in rows
        ]
    packets, flits = vectors["packets_injected"], vectors["flits_injected"]
    if not packets or len(packets) != len(flits):
        raise ValueError("missing or unmatched injection statistics dumps")
    for ps, fs in zip(packets, flits):
        if len(ps) != 4 or len(fs) != 4 or ps[3] <= 0:
            raise ValueError("requires four active CHI vnets and DAT traffic")
        ratios = [expected["control_flits_per_packet"]] * 3
        ratios.append(expected["data_flits_per_packet"])
        if any(f != p * ratio for p, f, ratio in zip(ps, fs, ratios)):
            raise ValueError("packet/flit framing mismatch")
    return {
        "status": "PASS",
        "dumps": len(packets),
        "packets_injected": packets,
        "flits_injected": flits,
    }


def latency(trace, address, l1, l2, period, start_tick=0, end_tick=None):
    """Reject overlap/retry rather than pairing same-address events heuristically."""
    events = []
    pattern = re.compile(
        r"^\s*(\d+)\s+(\d+)\s+(Seq|Cache)\s+(\w+).*"
        r"\[0x[0-9a-f]+, line (0x[0-9a-f]+)\]"
    )
    for row in trace.splitlines():
        match = pattern.match(row)
        if (
            match
            and int(match[5], 16) == address
            and int(match[1]) >= start_tick
            and (end_tick is None or int(match[1]) <= end_tick)
        ):
            events.append((int(match[1]), int(match[2]), match[3], match[4]))

    def one(controller, kind, event):
        values = [
            t
            for t, c, k, e in events
            if (c, k, e) == (controller, kind, event)
        ]
        if len(values) != 1:
            raise ValueError(f"ambiguous/missing {controller}:{kind}:{event}")
        return values[0]

    times = {
        "seq_begin": one(l1, "Seq", "Begin"),
        "l1_send": one(l1, "Cache", "SendReadShared"),
        "l2_allocate": one(l2, "Cache", "AllocRequest"),
        "l2_data_read": one(l2, "Cache", "DataArrayRead"),
        "l2_send": one(l2, "Cache", "SendCompData"),
        "seq_done": one(l1, "Seq", "Done"),
    }
    responses = [
        t
        for t, c, k, e in events
        if c == l1 and k == "Cache" and e.startswith("CompData_")
    ]
    if len(responses) != 2:
        raise ValueError("requires exactly two response chunks")
    ordered = list(times.values())[:-1] + responses + [times["seq_done"]]
    if ordered != sorted(ordered) or period <= 0:
        raise ValueError("invalid event order/clock")
    if any(c == l2 and e == "SendReadShared" for _, c, _, e in events):
        raise ValueError("not an isolated L2 hit")
    times.update(response_first=responses[0], response_last=responses[-1])
    return {
        "status": "PASS",
        "line": hex(address),
        "ticks": times,
        "selected_window_ticks": [start_tick, end_tick],
        "cycles": {
            "sequencer_to_l1_send": (times["l1_send"] - times["seq_begin"])
            / period,
            "request_delivery": (times["l2_allocate"] - times["l1_send"])
            / period,
            "l2_tag_pipeline": (times["l2_data_read"] - times["l2_allocate"])
            / period,
            "l2_data_pipeline": (times["l2_send"] - times["l2_data_read"])
            / period,
            "response_first": (responses[0] - times["l2_send"]) / period,
            "response_last": (responses[-1] - times["l2_send"]) / period,
            "total": (times["seq_done"] - times["seq_begin"]) / period,
        },
        "qualification": "one transaction, not unloaded mean or NI breakdown",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--stats", type=Path, required=True)
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--line", type=lambda x: int(x, 0))
    parser.add_argument("--start-tick", type=int, default=0)
    parser.add_argument("--end-tick", type=int)
    parser.add_argument("--l1", type=int)
    parser.add_argument("--l2", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    result = {"contract": contract(cfg)}
    stats = args.stats.read_text()
    result["framing"] = framing(stats, result["contract"])
    frequencies = set(re.findall(r"^simFreq\s+(\d+)", stats, re.M))
    if len(frequencies) != 1:
        raise ValueError("missing or inconsistent simulation tick frequency")
    c = result["contract"]
    gbps = (
        int(frequencies.pop())
        / c["link_period_ticks"]
        * c["payload_bytes_per_cluster_link_cycle"]
        / 1e9
    )
    c["cluster_payload_cut_GBps"] = gbps
    c["aggregate_cluster_payload_cut_GBps"] = gbps * len(
        c["cluster_ingress_links"]
    )
    if args.trace:
        if None in (args.line, args.l1, args.l2):
            parser.error("trace requires --line, --l1 and --l2")
        controllers = {
            obj.get("version"): obj["path"]
            for obj in objects(cfg)
            if obj.get("type") == "CHI_Cache_Controller"
        }
        l1_path = controllers.get(args.l1, "")
        l2_path = controllers.get(args.l2, "")
        if not l1_path.endswith(".l1d") or l2_path != l1_path[:-4] + ".l2":
            raise ValueError("trace IDs must be same-core L1D and L2")
        result["latency"] = latency(
            args.trace.read_text(),
            args.line,
            args.l1,
            args.l2,
            result["contract"]["link_period_ticks"],
            args.start_tick,
            args.end_tick,
        )
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
