#!/usr/bin/env python3
# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Check instantiated CMN topology and export resolved parameter provenance."""

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def expected_memory(profile_json, executable):
    """Independent native defaults, not the run's recorded parameter manifest."""
    with tempfile.TemporaryDirectory(prefix="zena-memory-oracle-") as tmp:
        directory = Path(tmp)
        (directory / "profile.json").write_text(profile_json)
        execution = subprocess.run(
            [
                executable,
                "--outdir=" + str(directory / "m5out"),
                str(ROOT / "scripts/zena_cmn/export_expected_memory.py"),
                str(directory / "profile.json"),
                str(directory / "expected.json"),
            ],
            timeout=60,
            capture_output=True,
            text=True,
        )
        if execution.returncode:
            raise ValueError(
                "native class resolution failed: " + execution.stderr[-2000:]
            )
        return json.loads((directory / "expected.json").read_text())


def native_expected(profile):
    executable = os.environ.get(
        "ZENA_CMN_GEM5", str(ROOT / "build/ARM/gem5.opt")
    )
    return expected_memory(json.dumps(profile, sort_keys=True), executable)


def period_ticks(clock):
    match = re.fullmatch(r"([0-9.]+)(GHz|MHz|kHz|Hz)", clock)
    if not match:
        raise ValueError("unsupported clock: " + clock)
    return round(
        1e12
        / (
            float(match[1])
            * {"GHz": 1e9, "MHz": 1e6, "kHz": 1e3, "Hz": 1}[match[2]]
        )
    )


def artifact_hashes(directory):
    paths = {
        name: directory / name
        for name in ("config.json", "system.dtb", "resolved-profile.json")
    }
    paths["verifier"] = Path(__file__)
    paths["oracle"] = ROOT / "scripts/zena_cmn/export_expected_memory.py"
    # Bind expected values to the native executable and all model sources.
    paths["gem5"] = Path(
        os.environ.get("ZENA_CMN_GEM5", ROOT / "build/ARM/gem5.opt")
    )
    for path in sorted((ROOT / "configs/zena_cmn").rglob("*.py")):
        paths[str(path.relative_to(ROOT))] = path
    for name in ("DRAMInterface", "MemInterface", "MemCtrl", "AbstractMemory"):
        paths[name] = ROOT / "src/mem" / (name + ".py")
    paths["zena_css_models"] = ROOT / "configs/example/arm/zena_css_models.py"
    hashes = {name: digest(path) for name, path in paths.items()}
    profile = json.loads((directory / "resolved-profile.json").read_text())
    # Include actually loaded custom modules, even outside this checkout.
    hashes["oracle_loaded_sources"] = native_expected(profile)["source_hashes"]
    return hashes


def validate_saved_report(directory, report):
    if report.get("artifact_hashes") != artifact_hashes(directory):
        raise ValueError(
            "stale or unbound configuration verification; rerun verifier"
        )
    if (
        report.get("status") != "PASS"
        or report.get("device_tree", {}).get("status") != "PASS"
    ):
        raise ValueError("configuration/DT verification did not pass")


def objects(value):
    result = {}
    if isinstance(value, dict):
        if "path" in value and "type" in value:
            result[value["path"]] = value
        for child in value.values():
            result.update(objects(child))
    elif isinstance(value, list):
        for child in value:
            result.update(objects(child))
    return result


def partition(ranges, base, count, interleave_bytes=64):
    start, end = map(int, base.split(":"))
    if count not in (4, 8, 16) or interleave_bytes not in (64, 512, 1024):
        return False
    low_bit = interleave_bytes.bit_length() - 1
    masks = [1 << (low_bit + bit) for bit in range(count.bit_length() - 1)]
    return (
        ranges
        == [
            ":".join(map(str, [start, end, index, *masks]))
            for index in range(count)
        ]
        and start % (count * interleave_bytes) == 0
        and (end - start) % (count * interleave_bytes) == 0
    )


def verify(config, profile):
    failures = []

    def check(condition, description):
        if not condition:
            failures.append(description)

    system = config["system"]
    index = objects(config)
    cpus = system["cpu"]
    cores = profile["cpu"]["cores"]
    clusters = profile["cpu"]["clusters"]
    group = profile["cpu"]["cores_per_cluster"]
    channels = profile["memory"]["channels"]
    ruby = system["ruby"]
    clocks = {
        "system.clk_domain": profile["ruby"]["clock"],
        "system.cpu_clk_domain": profile["cpu"]["clock"],
        "system.ruby.clk_domain": profile["ruby"]["clock"],
        "system.ruby.network.clk_domain": profile["cmn"]["mesh"]["clock"],
    }
    npu_enabled = profile.get("npu", {}).get("enabled", False)
    if npu_enabled:
        clocks["system.npu_clk_domain"] = profile["npu"]["clock"]
    for path, clock in clocks.items():
        check(
            index.get(path, {}).get("clock") == [period_ticks(clock)],
            f"effective clock: {path}",
        )
    for cpu in cpus + system.get("roi_cpu", []):
        check(
            cpu.get("clk_domain") == "system.cpu_clk_domain",
            f"CPU clock binding: {cpu['path']}",
        )
    for obj in index.values():
        if obj["type"] == "CHI_Cache_Controller":
            domain = (
                "system.cpu_clk_domain"
                if obj["path"].startswith("system.cpu")
                else "system.ruby.clk_domain"
            )
            check(
                obj.get("clk_domain") == domain,
                f"cache clock binding: {obj['path']}",
            )
        if obj["type"] in ("GarnetRouter", "Switch"):
            check(
                obj.get("clk_domain") == "system.ruby.network.clk_domain",
                f"router clock binding: {obj['path']}",
            )
        if obj["type"] in ("MemCtrl", "DRAMInterface"):
            check(
                obj.get("clk_domain") == "system.clk_domain",
                f"memory clock binding: {obj['path']}",
            )
    try:
        memory_expected = native_expected(profile)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        memory_expected = None
        check(False, f"native memory oracle unavailable: {error}")
    rnfs, homes, snfs = ruby["rnf"], ruby["hnf"], ruby["snf"]
    mcs = system["mem_ctrls"]
    check(len(cpus) == cores, "CPU count")
    check(len(rnfs) == clusters, "RN-F cluster count")
    check(len(homes) == 8, "global Home count")
    check(len(snfs) == len(mcs) == channels, "SNF/MC count")
    check(
        sum(x["type"] == "MemCtrl" for x in index.values()) == channels,
        "no duplicate native memory backend",
    )
    check(
        sum(
            x["type"] == "CHI_Cache_Controller" and x["path"].endswith(".l2")
            for x in index.values()
        )
        == cores,
        "private L2 count",
    )
    check(
        sum(x["path"].endswith(".dsu_l3") for x in index.values()) == clusters,
        "shared DSU count",
    )
    timed_cpus = system.get("roi_cpu", cpus)
    check(len(timed_cpus) == cores, "timing CPU count")
    for cpu in timed_cpus:
        check(cpu["type"] == "BaseO3CPU", "timing CPU must be generic ArmO3")
        for field, key in (
            ("numROBEntries", "rob_entries"),
            ("LQEntries", "lq_entries"),
            ("SQEntries", "sq_entries"),
        ):
            check(
                cpu.get(field) == profile["cpu"][key], f"{cpu['path']} {field}"
            )
        iq = (
            sum(q["numEntries"] for q in cpu["instQueues"])
            if "instQueues" in cpu
            else cpu.get("numIQEntries")
        )
        check(
            iq == profile["cpu"]["iq_entries"],
            "effective instruction queue entries",
        )

    if profile["cpu"].get("execution_profile") == "a720ae_documented":
        for cpu in timed_cpus:
            units = cpu["instQueues"][0]["fuPool"]["FUList"]
            for operation, latency in (
                ("SimdFloatAdd", 2),
                ("SimdFloatMultAcc", 4),
            ):
                matches = [
                    (unit, op)
                    for unit in units
                    if unit["count"]
                    for op in unit["opList"]
                    if op["opClass"] == operation
                ]
                check(len(matches) == 1, f"{operation} single execution path")
                if len(matches) == 1:
                    unit, op = matches[0]
                    check(
                        unit["count"] == 2
                        and op["opLat"] == latency
                        and op["pipelined"],
                        f"{operation} documented timing",
                    )

    def cache_profile(controller, spec, bytes_count):
        resolved = controller["cache"]
        check(
            resolved["size"] == bytes_count, f"{controller['path']} capacity"
        )
        for field, key in (
            ("assoc", "assoc"),
            ("tagAccessLatency", "tag_latency_cycles"),
            ("dataAccessLatency", "data_latency_cycles"),
            ("tagArrayBanks", "tag_banks"),
            ("dataArrayBanks", "data_banks"),
        ):
            check(
                resolved[field] == spec[key], f"{controller['path']} {field}"
            )
        check(
            resolved["resourceStalls"] == profile["cache"]["resource_stalls"],
            "cache resource stalls",
        )
        check(
            controller["number_of_TBEs"] == spec["request_tbes"],
            f"{controller['path']} request TBEs",
        )

    home_paths = [node["cntrl"]["path"] for node in homes]
    snf_paths = [node["cntrl"]["path"] for node in snfs]
    cache_controllers = []
    for i, cpu in enumerate(cpus):
        if i // group >= len(rnfs):
            continue
        dsu = rnfs[i // group]["dsu_l3"]
        for level in ("l1i", "l1d"):
            check(
                cpu[level]["downstream_destinations"] == [cpu["l2"]["path"]],
                f"CPU{i} {level} -> private L2",
            )
            cache_controllers.append(cpu[level])
        check(
            cpu["l2"]["downstream_destinations"] == [dsu["path"]],
            f"CPU{i} L2 -> own DSU",
        )
        cache_controllers.append(cpu["l2"])
        check(
            cpu["l2"]["read_hit_latency"]
            == profile["cache"]["l2"].get("read_hit_pipeline_cycles", 0),
            f"CPU{i} L2 read hit pipeline",
        )
        for level in ("l1i", "l1d", "l2"):
            integrated = profile["cache"].get(
                "integrated_private_pipeline", False
            )
            check(
                cpu[level]["request_latency"] == (0 if integrated else 1)
                and cpu[level]["data_latency"] == (0 if integrated else 1)
                and cpu[level]["reqOut"]["allow_zero_latency"] == integrated
                and cpu[level]["datOut"]["allow_zero_latency"] == integrated,
                f"CPU{i} {level} private pipeline handoff",
            )
            spec = profile["cache"][level]
            cache_profile(cpu[level], spec, spec["size_kib_per_core"] * 1024)
            check(
                cpu[level]["use_prefetcher"]
                == (level == "l2" and spec.get("prefetch", "off") == "stride"),
                f"CPU{i} {level} prefetch selection",
            )
        if profile["cache"]["l2"].get("policy") == "exclusive_data":
            check(
                cpu["l2"].get("exclusive_data_upstream_version")
                == cpu["l1d"]["version"],
                f"CPU{i} exclusive data upstream",
            )
            check(
                cpu["l2"].get("dealloc_backinv_shared") is False
                and cpu["l2"].get("dealloc_backinv_unique") is False,
                f"CPU{i} exclusive data back-invalidation",
            )
        for kind in ("instruction", "data"):
            key = "inst" if kind == "instruction" else "data"
            check(
                cpu[key + "_sequencer"]["max_outstanding_requests"]
                == profile["ruby"][kind + "_sequencer_outstanding"],
                f"CPU{i} {kind} sequencer outstanding",
            )

        check(
            cpu["icache_port"]["peer"].startswith(
                cpu["inst_sequencer"]["path"] + "."
            ),
            f"CPU{i} instruction port",
        )
        check(
            cpu["dcache_port"]["peer"].startswith(
                cpu["data_sequencer"]["path"] + "."
            ),
            f"CPU{i} data port",
        )
    for node in rnfs:
        dsu = node["dsu_l3"]
        check(not dsu["is_HN"], "DSU must not be a Home")
        check(dsu["downstream_destinations"] == home_paths, "DSU -> Homes")
        cache_controllers.append(dsu)
        spec = profile["cache"]["dsu_l3"]
        cache_profile(dsu, spec, spec["size_mib_per_cluster"] * 1024**2)

    for node in homes:
        hn = node["cntrl"]
        check(hn["is_HN"], "global Home identity")
        check(hn["downstream_destinations"] == snf_paths, "Home -> SNF")
        check(
            hn["cache"]["start_index_bit"] == 9,
            "Home index excludes home-select bits",
        )
        cache_controllers.append(hn)
        for flag in ("DMT", "DCT"):
            check(
                hn["enable_" + flag] == profile["cmn"][flag.lower()],
                f"Home {flag}",
            )
        check(
            hn["number_of_TBEs"] == profile["cmn"]["home_request_tbes"],
            "Home request TBEs",
        )
        check(
            hn["cache"]["size"]
            == profile["cmn"].get(
                "slc_size_kib_per_home",
                profile["cmn"].get("slc_size_mib_per_home", 0) * 1024,
            )
            * 1024,
            "Home SLC size",
        )

    for node, mc in zip(snfs, mcs):
        snf = node["cntrl"]
        check(
            snf.get("wait_for_write_ack", False) == npu_enabled,
            "SNF write completion ordering",
        )
        check(
            mc["dram"]["type"] == "DRAMInterface", "native DRAMInterface type"
        )
        if memory_expected is not None:
            for kind, actual in (("dram", mc["dram"]), ("controller", mc)):
                for key, value in memory_expected[kind].items():
                    check(
                        actual.get(key) == value,
                        f"native {kind} parameter: {mc['path']} {key}",
                    )
        if npu_enabled:
            check(
                snf["memory_out_port"]["peer"].startswith(
                    "system.memory_bus.cpu_side_ports["
                )
                and mc["port"]["peer"].startswith(
                    "system.memory_bus.mem_side_ports["
                ),
                "SNF/shared arbiter/native MC path",
            )
        else:
            check(
                snf["memory_out_port"]["peer"] == mc["path"] + ".port",
                "SNF -> native MC",
            )
            check(
                mc["port"]["peer"] == snf["path"] + ".memory_out_port",
                "native MC -> SNF",
            )
        check(
            snf["addr_ranges"] == [mc["dram"]["range"]],
            "SNF/MC address agreement",
        )
        check(
            mc["dram"]["addr_mapping"] == "RoRaBaCoCh", "DRAM mapping baseline"
        )
        check(
            mc["mem_sched_policy"] == "frfcfs"
            and mc["dram"]["page_policy"] == "close_adaptive",
            "DRAM scheduler/page baseline",
        )
    base = system["mem_ranges"]
    check(len(base) == 1, "single RAM range")
    if len(base) == 1:
        start, end = map(int, base[0].split(":"))
        check(end - start == 2 * 1024**3, "2 GiB RAM capacity")
        check(
            partition(
                [n["cntrl"]["addr_ranges"][0] for n in homes], base[0], 8
            ),
            "Home partition",
        )
        check(
            partition(
                [m["dram"]["range"] for m in mcs],
                base[0],
                channels,
                profile["memory"]["interleave_bytes"],
            ),
            "MC partition",
        )
        for obj in index.values():
            if "pio_addr" in obj and "pio_size" in obj:
                check(
                    obj["pio_addr"] + obj["pio_size"] <= start
                    or obj["pio_addr"] >= end,
                    f"MMIO/RAM overlap: {obj['path']}",
                )
    network = ruby["network"]
    mesh = profile["cmn"]["mesh"]
    check(
        network["type"]
        == (
            "SimpleNetwork"
            if profile["cmn"].get("network") == "simple"
            else "GarnetNetwork"
        ),
        "selected network implementation",
    )
    check(
        len(network["routers"]) == mesh["rows"] * mesh["columns"] + clusters,
        "mesh plus cluster router count",
    )
    if profile["cmn"].get("network") == "simple":
        check(network.get("netifs") == [], "Simple network has no Garnet NI")
        check(
            network.get("endpoint_bandwidth") == 1000
            and network.get("physical_vnets_channels") == []
            and network.get("physical_vnets_bandwidth") == [],
            "Simple shared-vnet bandwidth model",
        )
        check(network.get("buffer_size") == 32, "Simple finite buffer default")
        mesh_routers = mesh["rows"] * mesh["columns"]
        for router in network["routers"]:
            latency = (
                0
                if router["router_id"] >= mesh_routers
                else mesh["router_latency_cycles"]
            )
            check(
                router.get("int_routing_latency") == latency
                and router.get("ext_routing_latency") == latency,
                f"Simple routing latency: {router['path']}",
            )
            buffers = router.get("port_buffers", [])
            check(
                bool(buffers) and all(b["buffer_size"] == 32 for b in buffers),
                f"Simple finite switch buffers: {router['path']}",
            )
        for kind, latency in (
            ("ext_links", mesh["node_link_latency_cycles"]),
            ("int_links", mesh["router_link_latency_cycles"]),
        ):
            for link in network[kind]:
                check(
                    link.get("bandwidth_factor") == mesh["flit_bytes"]
                    and link["latency"] == latency,
                    f"Simple link bandwidth/latency: {link['path']}",
                )
                if kind == "int_links":
                    buffers = link.get("buffers", [])
                    check(
                        len(buffers) == 4
                        and all(b["buffer_size"] == 32 for b in buffers),
                        f"Simple finite link buffers: {link['path']}",
                    )
    attachments = {
        link["ext_node"]: link["int_node"] for link in network["ext_links"]
    }
    for controller in cache_controllers:
        check(
            controller["path"] in attachments,
            f"network registration: {controller['path']}",
        )
    for i, node in enumerate(rnfs):
        router = attachments.get(node["dsu_l3"]["path"])
        check(
            all(
                attachments.get(cpu[level]["path"]) == router
                for cpu in cpus[i * group : (i + 1) * group]
                for level in ("l1i", "l1d", "l2")
            ),
            f"cluster{i} single local router",
        )
        in_links = [
            link for link in network["int_links"] if link["dst_node"] == router
        ]
        out_links = [
            link for link in network["int_links"] if link["src_node"] == router
        ]
        check(
            len(in_links) == len(out_links) == 1,
            f"cluster{i} one bidirectional mesh edge",
        )
        expected = network["routers"][
            profile["cmn"]["mesh"]["cluster_attach"][i]
        ]["path"]
        check(
            len(in_links) == 1 and in_links[0]["src_node"] == expected,
            f"cluster{i} mesh placement",
        )
    check(
        not ruby["access_backing_store"], "no automatic backing-store update"
    )
    if npu_enabled:
        npu = index.get("system.platform.npu", {})
        smmu = index.get("system.platform.npu_smmu", {})
        bus = index.get("system.memory_bus", {})
        check(npu.get("type") == "NpuV01", "NPU model present")
        sys.path.insert(0, str(ROOT / "configs"))
        from zena_cmn.cpu_profile import cache_id_registers

        clidr, ccsidr = cache_id_registers(profile)
        for cpu in cpus:
            for isa in cpu["isa"]:
                check(
                    isa.get("cache_id_registers_enabled") is True
                    and isa.get("clidr") == clidr
                    and isa.get("ccsidr") == ccsidr,
                    "CPU architectural cache geometry for NC DMA",
                )
        check(npu.get("pio_addr") == 0x0B000000, "NPU MMIO address")
        check(npu.get("stream_id") == 0x100, "NPU stream ID")
        check(
            npu.get("enable_async_dma", False)
            == profile["npu"].get("enable_async_dma", False),
            "NPU explicit async capability",
        )
        for key in (
            "sram_size",
            "sram_banks",
            "sram_bank_width",
            "sram_latency",
            "bank_queue_depth",
            "max_outstanding",
            "dma_width",
        ):
            check(npu.get(key) == profile["npu"][key], f"NPU {key}")
        check(
            npu.get("clk_domain") == "system.npu_clk_domain",
            "NPU clock binding",
        )
        for key, default in (
            ("experimental_scaling", False),
            ("dma_issue_width", 1),
        ):
            check(
                npu.get(key, default) == profile["npu"].get(key, default),
                f"NPU {key}",
            )
        smmu_spec = profile["npu"].get("smmu", {})
        check(
            smmu.get("request_port_width") == profile["npu"]["dma_width"],
            "SMMU request port width",
        )
        for key, default in (("xlate_slots", 64), ("ptw_slots", 16)):
            check(
                smmu.get(key) == smmu_spec.get(key, default),
                f"SMMU {key}",
            )
        interfaces = smmu.get("device_interfaces", [])
        check(len(interfaces) == 1, "SMMU one NPU interface")
        for interface in interfaces:
            check(
                interface.get("port_width") == profile["npu"]["dma_width"],
                "SMMU device port width",
            )
            for key, default in (
                ("xlate_slots", 16),
                ("wrbuf_slots", 16),
                ("utlb_slots", 1),
                ("tlb_slots", 3),
            ):
                check(
                    interface.get(key)
                    == smmu_spec.get("device_" + key, default),
                    f"SMMU device {key}",
                )
        check(smmu.get("type") == "SMMUv3", "native SMMUv3 present")
        check(smmu.get("smmu_idr1") == 0x00E70009, "SMMU 9-bit StreamID")
        check(
            smmu.get("smmu_idr0") == ((0x094C100F & ~(3 << 27)) | 16),
            "SMMU linear STE and coherent maintenance",
        )
        check(
            smmu.get("request", {})
            .get("peer", "")
            .startswith("system.memory_bus.cpu_side_ports["),
            "NPU payload outside CPU Home",
        )
        check(
            smmu.get("walker", {})
            .get("peer", "")
            .startswith("system.ruby.dma_rni"),
            "SMMU maintenance through coherent RNI",
        )
        check(
            npu.get("dma", {})
            .get("peer", "")
            .startswith("system.platform.npu_smmu.device_interfaces"),
            "NPU DMA through SMMU",
        )
        check(bus.get("type") == "NoncoherentXBar", "shared native MC arbiter")
        for key in (
            "width",
            "frontend_latency",
            "forward_latency",
            "response_latency",
        ):
            check(
                bus.get(key) == profile["npu"]["fabric_" + key],
                f"shared arbiter {key}",
            )
    else:
        check(
            not any("npu" in path.lower() for path in index),
            "no NPU SimObject",
        )
        check(
            "system.memory_bus" not in index, "no NPU arbiter in CPU baseline"
        )
    return {
        "status": "FAIL" if failures else "PASS",
        "failures": failures,
        "counts": {
            "cpu": len(cpus),
            "clusters": len(rnfs),
            "homes": len(homes),
            "snf": len(snfs),
            "native_mc": len(mcs),
        },
        "selected_memory_class": (
            memory_expected["selected_class"] if memory_expected else None
        ),
        "memory_class_evidence": "Effective native parameter equivalence; config.json erases Python subclass identity",
        "evidence_tier": "instantiated configuration, not runtime coherency",
        "not_proven": [
            "Linux online CPUs and cache sysfs",
            "coherency correctness",
            "absence of arbitrary C++ shadow-store hacks",
        ],
    }


def verify_dtb(filename, config, profile):
    sys.path.insert(0, str(ROOT / "src/python/m5/ext/pyfdt"))
    import pyfdt

    with open(filename, "rb") as source:
        tree = pyfdt.FdtBlobParse(source).to_fdt()
    properties = dict(tree.get_rootnode().walk())
    cores = profile["cpu"]["cores"]
    group = profile["cpu"]["cores_per_cluster"]
    handles = {
        value[0]: path.rsplit("/", 1)[0]
        for path, value in properties.items()
        if path.endswith("/phandle")
    }
    l2, l3 = [], []
    for affinity in config["system"]["cpu_affinities"]:
        cpu = f"/cpus/cpu@{affinity:x}"
        reg = list(properties[cpu + "/reg"])
        if (reg[0] << 32) | reg[1] != affinity:
            raise ValueError("DT CPU affinity mismatch")
        l2path = handles[properties[cpu + "/next-level-cache"][0]]
        l3path = handles[properties[l2path + "/next-level-cache"][0]]
        if (
            properties[l2path + "/cache-level"][0] != 2
            or properties[l3path + "/cache-level"][0] != 3
        ):
            raise ValueError("DT cache levels mismatch")
        l2.append(l2path)
        l3.append(l3path)
    if (
        len(l2) != cores
        or len(set(l2)) != cores
        or len(set(l3)) != cores // group
    ):
        raise ValueError("DT cache sharing counts mismatch")
    if any(len(set(l3[i : i + group])) != 1 for i in range(0, cores, group)):
        raise ValueError("DT L3 sharing crosses cluster boundary")
    reservation = list(properties["/reserved-memory/firmware@ffe00000/reg"])
    if reservation != [0, 0xFFE00000, 0, 0x200000]:
        raise ValueError("firmware reservation changed")
    if any("e8000000" in path for path in properties):
        raise ValueError("removed WB/NC reservation was reintroduced")
    if profile.get("npu", {}).get("enabled", False):
        npu, smmu = "/npu@b000000", "/iommu@b100000"
        expected = {
            npu + "/reg": [0, 0x0B000000, 0, 0x10000],
            smmu + "/reg": [0, 0x0B100000, 0, 0x20000],
            npu + "/interrupts": [0, 80, 4, 0, 81, 4],
            smmu + "/interrupts": [0, 82, 4],
            npu + "/iommus": [properties[smmu + "/phandle"][0], 0x100],
        }
        for path, value in expected.items():
            if list(properties[path]) != value:
                raise ValueError("NPU DT mismatch: " + path)
        if (
            npu + "/dma-coherent" in properties
            or smmu + "/dma-coherent" not in properties
        ):
            raise ValueError(
                "NPU payload / SMMU maintenance coherency mismatch"
            )
    elif any(
        path.startswith("/npu@") or path.startswith("/iommu@b100000")
        for path in properties
    ):
        raise ValueError("NPU DT unexpectedly present")
    return {
        "status": "PASS",
        "private_l2": len(set(l2)),
        "shared_l3": len(set(l3)),
        "firmware_reservation": reservation,
    }


def export_provenance(config, filename):
    with open(filename, "w", newline="") as target:
        writer = csv.writer(target)
        writer.writerow(
            ["object", "parameter", "resolved_value", "provenance"]
        )
        for path, obj in sorted(objects(config).items()):
            for key, value in sorted(obj.items()):
                if (
                    key in ("type", "cxx_class", "name", "path")
                    or isinstance(value, dict)
                    or (
                        isinstance(value, list)
                        and any(isinstance(v, dict) for v in value)
                    )
                ):
                    continue
                source = "inherited_existing"
                assumed = {
                    "RubyCache": {
                        "size",
                        "assoc",
                        "tagAccessLatency",
                        "dataAccessLatency",
                        "tagArrayBanks",
                        "dataArrayBanks",
                        "resourceStalls",
                        "start_index_bit",
                    },
                    "CHI_Cache_Controller": {
                        "read_hit_latency",
                        "number_of_TBEs",
                        "enable_DMT",
                        "enable_DCT",
                        "data_channel_size",
                    },
                    "CHI_Memory_Controller": {
                        "number_of_TBEs",
                        "data_channel_size",
                    },
                    "RubySequencer": {"max_outstanding_requests"},
                    "GarnetNetwork": {
                        "ni_flit_size",
                        "vcs_per_vnet",
                        "buffers_per_ctrl_vc",
                        "buffers_per_data_vc",
                        "control_msg_size",
                        "data_msg_size",
                    },
                    "GarnetRouter": {"latency", "router_id", "width"},
                    "GarnetExtLink": {
                        "latency",
                        "ext_node",
                        "int_node",
                        "width",
                    },
                    "GarnetIntLink": {
                        "latency",
                        "src_node",
                        "dst_node",
                        "width",
                        "weight",
                    },
                    "NetworkLink": {"link_latency", "width"},
                    "CreditLink": {"link_latency", "width"},
                }
                if key in assumed.get(obj["type"], set()):
                    source = "uncalibrated_assumption"
                if (
                    (
                        obj["type"] == "BaseO3CPU"
                        and key in ("numROBEntries", "LQEntries", "SQEntries")
                    )
                    or (obj["type"] == "IQUnit" and key == "numEntries")
                    or (path.endswith(".l1d.cache") and key == "size")
                    or (path == "system.cpu_clk_domain" and key == "clock")
                ):
                    source = "user_fixed"
                if obj["type"] == "BaseO3CPU" and key == "LQEntries":
                    source = (
                        "inherited_existing"
                        if value == 32
                        else "uncalibrated_assumption"
                    )
                writer.writerow(
                    [path, key, json.dumps(value, sort_keys=True), source]
                )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    parser.add_argument(
        "--output", type=Path, help="preserve historical verification"
    )
    parser.add_argument("--gem5", type=Path)
    args = parser.parse_args()
    directory = args.run_directory
    if args.gem5:
        os.environ["ZENA_CMN_GEM5"] = str(args.gem5.resolve())
    config = json.loads((directory / "config.json").read_text())
    profile = json.loads((directory / "resolved-profile.json").read_text())
    report = verify(config, profile)
    try:
        report["device_tree"] = verify_dtb(
            directory / "system.dtb", config, profile
        )
    except (OSError, KeyError, ValueError) as error:
        report["device_tree"] = {"status": "FAIL", "reason": str(error)}
        report["status"] = "FAIL"
    report["artifact_hashes"] = artifact_hashes(directory)
    report["memory_oracle"] = (
        "independent native class defaults + explicit board overrides; not silicon calibration"
    )
    if not args.output:
        export_provenance(config, directory / "parameter_provenance.csv")
    (args.output or directory / "config-verification.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report, indent=2))
    return int(report["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
