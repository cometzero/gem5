# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""One native LPDDR5X backend shared by the CHI SNF endpoints."""

import hashlib
import importlib
import json
import sys
from pathlib import Path

from .address_map import (
    striped_ranges,
    validate_ranges,
)


def resolved_memory_parameters(obj):
    from m5.proxy import isproxy

    # Exclude topology references; Self timing proxies must resolve before hashing.
    excluded = {
        "range",
        "dram",
        "nvm",
        "clk_domain",
        "eventq_index",
        "system",
        "power_state",
        "power_model",
    }
    result = {}
    for name in sorted(obj._params.keys()):
        if name in excluded:
            continue
        value = getattr(obj, name)
        if isproxy(value):
            value = value.unproxy(obj)
        result[name] = str(value)
    return result


def build_memory_backend(system, params):
    from m5.objects import (
        DRAMInterface,
        MemCtrl,
        NoncoherentXBar,
    )

    if "mem_ctrls" in system._children:
        raise ValueError("native memory backend already exists")
    memory = params["memory"]
    regions = system.mem_ranges
    if len(regions) != 1:
        raise ValueError(
            "Zena backend requires one contiguous platform RAM range"
        )
    base = regions[0]
    if base.size() != memory["capacity_mib_total"] * 1024**2:
        raise ValueError("platform RAM size must equal profile capacity")
    module, name = memory["custom_module"], memory["custom_class"]
    if module is None:
        existing = Path(__file__).resolve().parents[1] / "example" / "arm"
        sys.path.insert(0, str(existing))
        module, name = "zena_css_models", "LPDDR5X_8533_1x16_BG_BL32"
    try:
        source_module = importlib.import_module(module)
        interface = getattr(source_module, name)
    except (ImportError, AttributeError) as error:
        raise ValueError(
            f"LPDDR5X model {module}.{name} unavailable; no fallback"
        ) from error
    if not isinstance(interface, type) or not issubclass(
        interface, DRAMInterface
    ):
        raise ValueError("memory custom_class must derive from DRAMInterface")
    if (
        int(interface.device_bus_width.value)
        * int(interface.devices_per_rank.value)
        != memory["channel_width_bits"]
    ):
        raise ValueError("LPDDR5X class channel width does not match profile")
    channels = memory["channels"]
    interleave_bytes = memory["interleave_bytes"]
    ranges = striped_ranges(base, channels, interleave_bytes)
    homes = striped_ranges(base, params["cmn"]["home_count"])
    address_validation = validate_ranges(base, homes, ranges, interleave_bytes)
    # Keep the original synthetic density unless an explicit part is selected.
    geometry = (
        {}
        if memory["preserve_device_geometry"]
        else dict(
            ranks_per_channel=1,
            device_size=f"{base.size() // channels // int(interface.devices_per_rank.value)}B",
        )
    )
    controllers = [
        MemCtrl(
            dram=interface(
                range=region,
                addr_mapping="RoRaBaCoCh",
                **geometry,
                page_policy="close_adaptive",
            ),
            mem_sched_policy="frfcfs",
        )
        for region in ranges
    ]
    system.mem_ctrls = controllers
    npu = params.get("npu", {"enabled": False})
    if npu["enabled"]:
        system.memory_bus = NoncoherentXBar(
            width=npu["fabric_width"],
            frontend_latency=npu["fabric_frontend_latency"],
            forward_latency=npu["fabric_forward_latency"],
            response_latency=npu["fabric_response_latency"],
            clk_domain=system.clk_domain,
        )
        for controller in controllers:
            controller.port = system.memory_bus.mem_side_ports
    values = {
        "dram_class": f"{interface.__module__}.{interface.__name__}",
        "dram": resolved_memory_parameters(controllers[0].dram),
        "controller": resolved_memory_parameters(controllers[0]),
    }
    fingerprint = hashlib.sha256(
        json.dumps(values, sort_keys=True).encode()
    ).hexdigest()
    return {
        "controllers": controllers,
        "ranges": ranges,
        "home_ranges": homes,
        "parameters": values,
        "fingerprint": fingerprint,
        "part_provenance": {
            "qualification": getattr(
                source_module,
                "QUALIFICATION",
                "UNCALIBRATED_SENSITIVITY_MODEL",
            ),
            "part": getattr(source_module, "PART", None),
            "source": getattr(source_module, "SOURCE_URL", None),
            "mode_assumptions": getattr(source_module, "MODE", None),
            "parameter_provenance": getattr(
                source_module, "PARAMETER_PROVENANCE", None
            ),
            "documented_parameters": getattr(
                source_module, "DOCUMENTED_PARAMETERS", ()
            ),
            "inherited_unqualified_timings": getattr(
                source_module, "INHERITED_UNQUALIFIED_TIMINGS", ()
            ),
            "preserve_device_geometry": memory["preserve_device_geometry"],
            "exposed_bytes_per_channel": base.size() // channels,
            "limitation": "Native decoder row count follows exposed aperture; full physical rows and target DMC are not validated",
        },
        "address_validation": address_validation,
        "channel_mapping": address_validation["channel_select"],
        "future_attachment": {
            "npu_present": npu["enabled"],
            "coherent": False,
            "boundary": "SNF memory-side / native MemCtrl ingress",
            "contract": "outside_cpu_home_domain_same_native_dram_backend",
            "arbiter_instantiated": npu["enabled"],
            "visibility_validation": "NOT_RUN: future NC DMA requires cache-maintenance and shadow-store checks",
        },
    }
