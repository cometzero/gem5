# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Strict project YAML contract, independent of the gem5 runtime."""

import re
from copy import deepcopy
from pathlib import Path

import yaml

DEFAULT_PROFILE = Path(__file__).with_name("profiles") / "default.yaml"


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ValueError(f"YAML keys must be unique strings: {key!r}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping
)


def _read(path):
    with open(path, encoding="utf-8") as source:
        value = yaml.load(source, Loader=UniqueLoader)
    if not isinstance(value, dict):
        raise ValueError("profile must be a YAML mapping")
    # Accept earlier project profiles while using integer KiB internally.
    cmn = value.get("cmn", {})
    if "slc_size_mib_per_home" in cmn:
        if "slc_size_kib_per_home" in cmn:
            raise ValueError("specify only one per-home SLC size unit")
        size = cmn.pop("slc_size_mib_per_home")
        if type(size) is not int or size < 1:
            raise ValueError(
                "legacy per-home MiB size must be positive integer"
            )
        cmn["slc_size_kib_per_home"] = size * 1024
    return value


def _merge(base, patch, prefix=""):
    if not isinstance(patch, dict):
        raise ValueError(f"{prefix or 'profile'} must be a mapping")
    for key, value in patch.items():
        name = f"{prefix}.{key}" if prefix else key
        if key not in base:
            raise ValueError(f"unknown parameter: {name}")
        expected = base[key]
        if isinstance(expected, dict):
            _merge(expected, value, name)
        elif expected is None:
            if value is not None and (type(value) is not str or not value):
                raise ValueError(f"{name} must be null or a nonempty string")
            base[key] = value
        else:
            if type(value) is not type(expected):
                raise ValueError(
                    f"{name} must have type {type(expected).__name__}"
                )
            if isinstance(value, list):
                if not value or any(
                    type(x) is not type(expected[0]) for x in value
                ):
                    raise ValueError(f"invalid list elements: {name}")
            if isinstance(value, str) and not value:
                raise ValueError(f"{name} must not be empty")
            base[key] = deepcopy(value)
    return base


def _flatten(value, prefix=""):
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            yield from _flatten(item, name)
        else:
            yield name, item


def validate(params):
    defaults = _read(DEFAULT_PROFILE)
    result = _merge(deepcopy(defaults), params)
    values = dict(_flatten(result))
    mutable_strings = {
        "cpu.clock",
        "cpu.execution_profile",
        "cache.l2.prefetch",
        "cache.l2.policy",
        "ruby.clock",
        "cmn.mesh.clock",
        "cmn.slc_mode",
        "cmn.network",
        "full_system.kernel",
        "full_system.disk_image",
        "full_system.bootloader",
        "full_system.dtb",
        "memory.custom_module",
        "memory.custom_class",
        "npu.clock",
    }
    for name, default in _flatten(defaults):
        value = values[name]
        minimum = 0 if name == "cache.l2.read_hit_pipeline_cycles" else 1
        if type(value) is int and not minimum <= value <= 2**31 - 1:
            raise ValueError(
                f"{name} must be an integer in [{minimum}, {2**31 - 1}]"
            )
        if (
            isinstance(default, str)
            and name not in mutable_strings
            and value != default
        ):
            raise ValueError(f"unsupported {name}: {value!r}")
        if name.endswith("clock") and not re.fullmatch(
            r"(?:[1-9]\d*(?:\.\d+)?|0\.\d*[1-9]\d*)(?:GHz|MHz|kHz|Hz)", value
        ):
            raise ValueError(f"invalid clock: {name}={value!r}")
    fixed = {
        "cache.line_bytes": 64,
        "cache.l2.prefetch": ("off", "stride"),
        "cache.l2.policy": ("inclusive", "exclusive_data"),
        "memory.channels": (4, 8, 16),
        "memory.capacity_mib_total": 2048,
        "memory.interleave_bytes": 64,
        "memory.data_rate_mt_s": 8533,
        "memory.channel_width_bits": 16,
        "cpu.cores_per_cluster": (1, 2, 4),
        "cpu.clusters": (1, 2, 4),
        "cmn.network": ("garnet", "simple"),
        "cmn.home_count": 8,
        "memory.home_count": 8,
        "cmn.mesh.rows": (4, 6),
        "cpu.execution_profile": ("generic", "a720ae_documented"),
        "cmn.mesh.columns": 4,
        "cmn.mesh.cluster_mesh_edges_per_direction": 1,
        "cmn.slc_mode": ("allocate", "directory_only"),
        "npu.sram_size": 2097152,
        "npu.sram_banks": 16,
    }
    for name, allowed in fixed.items():
        allowed = allowed if isinstance(allowed, tuple) else (allowed,)
        if values[name] not in allowed:
            raise ValueError(f"{name} must be one of {allowed}")
    flexible_bools = {
        "cmn.dmt",
        "cmn.dct",
        "ruby.timing_dvm",
        "memory.preserve_device_geometry",
        "cache.integrated_private_pipeline",
        "npu.enabled",
        "npu.enable_async_dma",
        "npu.experimental_scaling",
        "scope.npu_present",
        "memory.create_future_npu_arbiter_now",
    }
    for name, default in _flatten(defaults):
        if (
            type(default) is bool
            and name not in flexible_bools
            and values[name] != default
        ):
            raise ValueError(f"unsupported change to {name}")
    cpu, cache, cmn, mem = (
        result[k] for k in ("cpu", "cache", "cmn", "memory")
    )
    npu = result["npu"]
    if (
        npu["enabled"] != result["scope"]["npu_present"]
        or npu["enabled"] != mem["create_future_npu_arbiter_now"]
    ):
        raise ValueError(
            "NPU presence and shared-backend attachment must agree"
        )
    if npu["enable_async_dma"] and not npu["enabled"]:
        raise ValueError("NPU async DMA requires NPU enabled")
    if npu["experimental_scaling"] and not npu["enabled"]:
        raise ValueError("NPU experimental scaling requires NPU enabled")
    for field, limit in (
        ("dma_width", 512),
        ("fabric_width", 512),
        ("sram_bank_width", 32),
    ):
        if npu[field] & (npu[field] - 1) or npu[field] > limit:
            raise ValueError(f"npu.{field} must be a power of two <={limit}")
    if (
        npu["max_outstanding"] > 1024
        or npu["dma_issue_width"] > 8
        or npu["bank_queue_depth"] > 64
        or any(value > 1024 for value in npu["smmu"].values())
    ):
        raise ValueError("NPU queues exceed bounded model limits")
    if not npu["experimental_scaling"]:
        for field in (
            "dma_width",
            "dma_issue_width",
            "fabric_width",
            "max_outstanding",
            "sram_bank_width",
            "bank_queue_depth",
            "smmu",
        ):
            if npu[field] != defaults["npu"][field]:
                raise ValueError(f"npu.{field} requires experimental_scaling")
    if cache["integrated_private_pipeline"]:
        if cmn["network"] != "simple":
            raise ValueError(
                "integrated private pipeline requires Simple network"
            )
        if (
            len({cpu["clock"], result["ruby"]["clock"], cmn["mesh"]["clock"]})
            != 1
        ):
            raise ValueError(
                "integrated private pipeline requires identical clocks"
            )
    if cpu["cores"] != cpu["clusters"] * cpu["cores_per_cluster"]:
        raise ValueError("CPU count must equal clusters * cores_per_cluster")
    for value in (
        cache["dsu_l3"]["instances"],
        cmn["mesh"]["cluster_local_routers"],
        result["full_system"]["required_linux_cpu_clusters"],
    ):
        if value != cpu["clusters"]:
            raise ValueError(
                "DSU/router/Linux cluster counts must match CPU clusters"
            )
    if (
        cmn["slc_size_mib_total"] * 1024
        != cmn["home_count"] * cmn["slc_size_kib_per_home"]
    ):
        raise ValueError("SLC total must equal home_count * per-home size")
    mesh = cmn["mesh"]
    for key, count in (
        ("cluster_attach", cpu["clusters"]),
        ("home_attach", cmn["home_count"]),
        ("snf_attach_for_16_channels", 16),
    ):
        entries = mesh[key]
        if (
            len(entries) != count
            or len(set(entries)) != count
            or any(
                not 0 <= x < mesh["rows"] * mesh["columns"] for x in entries
            )
        ):
            raise ValueError(
                f"{key} requires {count} unique IDs within the configured mesh"
            )
    for label in ("l1i", "l1d", "l2", "dsu_l3"):
        item = cache[label]
        size = (
            item["size_mib_per_cluster"] * 1024**2
            if label == "dsu_l3"
            else item["size_kib_per_core"] * 1024
        )
        _cache_geometry(
            label, size, item["assoc"], item["tag_banks"], item["data_banks"]
        )
    _cache_geometry(
        "slc",
        cmn["slc_size_kib_per_home"] * 1024,
        cmn["slc_assoc"],
        cmn["slc_tag_banks"],
        cmn["slc_data_banks"],
    )
    if (mem["custom_module"] is None) != (mem["custom_class"] is None):
        raise ValueError("custom_module and custom_class must be set together")
    if mem["preserve_device_geometry"] and mem["custom_module"] is None:
        raise ValueError(
            "preserve_device_geometry requires an explicit DRAM class"
        )
    if mem["supported_comparison_channels"] != [4, 8, 16]:
        raise ValueError("supported_comparison_channels must be [4, 8, 16]")
    if result["benchmark"] != defaults["benchmark"]:
        raise ValueError(
            "benchmark YAML fields are the design contract, not guest controls; "
            "use the workload runner's explicit worker/pass options"
        )
    if result["full_system"]["dtb"] is not None:
        raise ValueError(
            "full_system.dtb must be null: topology DTB is generated"
        )
    return result


def _cache_geometry(name, size, assoc, tag_banks, data_banks):
    sets, remainder = divmod(size, 64 * assoc)
    if remainder or sets == 0 or sets & (sets - 1):
        raise ValueError(f"{name} requires a power-of-two set count")
    for banks in (tag_banks, data_banks):
        if banks & (banks - 1) or banks > sets:
            raise ValueError(f"invalid bank count for {name}")


def load_parameters(path=None, overrides=None):
    params = (
        _merge(_read(DEFAULT_PROFILE), _read(path))
        if path
        else _read(DEFAULT_PROFILE)
    )
    for name, value in (overrides or {}).items():
        keys = name.split(".")
        patch = value
        for key in reversed(keys):
            patch = {key: patch}
        _merge(params, patch)
    return validate(params)
