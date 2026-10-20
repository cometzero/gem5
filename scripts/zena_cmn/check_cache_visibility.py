#!/usr/bin/env python3
# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Diagnose the current CLIDR/DT cacheinfo mismatch without changing guests."""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def arm64_populated_leaves(clidr, levels, leaves):
    """Mirror Linux populate_cache_leaves; unused allocated leaves stay zero."""
    populated = []
    for level in range(1, levels + 1):
        if len(populated) >= leaves:
            break
        kind = (clidr >> (3 * (level - 1))) & 7
        if kind == 3:
            if len(populated) + 1 >= leaves:
                break
            populated.extend([(level, 2), (level, 1)])
        else:
            populated.append((level, kind))
    return populated + [(0, 0)] * (leaves - len(populated))


def source_clidr(source):
    match = re.search(
        r"case MISCREG_CLIDR:\s*(.*?)case MISCREG_CCSIDR:", source, re.S
    )
    if not match:
        raise ValueError(
            "native CLIDR implementation changed; re-review probe"
        )
    constant = re.search(r"return (0x[0-9a-fA-F]+);", match.group(1))
    if not constant:
        raise ValueError(
            "CLIDR is no longer a constant; guest measurement required"
        )
    return int(constant.group(1), 16)


def diagnose(run_directory):
    from verify_config import verify_dtb

    config = json.loads((run_directory / "config.json").read_text())
    profile = json.loads((run_directory / "resolved-profile.json").read_text())
    dtb = run_directory / "system.dtb"
    topology = verify_dtb(dtb, config, profile)
    sys.path.insert(0, str(ROOT / "src/python/m5/ext/pyfdt"))
    import pyfdt

    with dtb.open("rb") as source:
        tree = pyfdt.FdtBlobParse(source).to_fdt()
    properties = dict(tree.get_rootnode().walk())
    handles = {
        value[0]: path.rsplit("/", 1)[0]
        for path, value in properties.items()
        if path.endswith("/phandle")
    }
    isa_source = ROOT / "src/arch/arm/isa.cc"
    isa = config["system"]["cpu"][0]["isa"][0]
    configured = isa.get("cache_id_registers_enabled", False)
    clidr = (
        isa["clidr"] if configured else source_clidr(isa_source.read_text())
    )
    results = []
    for affinity in config["system"]["cpu_affinities"]:
        node = f"/cpus/cpu@{affinity:x}"
        levels, leaves = 1, 0
        chain = []
        while node:
            count = sum(
                node + "/" + name in properties
                for name in ("cache-size", "i-cache-size", "d-cache-size")
            )
            if not count:
                count = 1 if node + "/cache-unified" in properties else 2
            leaves += count
            level_property = properties.get(node + "/cache-level")
            if level_property is not None:
                levels = level_property[0]
            chain.append({"node": node, "level": levels, "leaves": count})
            next_cache = properties.get(node + "/next-level-cache")
            node = handles[next_cache[0]] if next_cache is not None else None
        populated = arm64_populated_leaves(clidr, levels, leaves)
        uninitialized = sum(level == 0 for level, _ in populated)
        results.append(
            {
                "affinity": affinity,
                "dt_chain": chain,
                "dt_allocated_leaves": leaves,
                "arm64_populated_leaves": populated,
                "uninitialized_leaves": uninitialized,
            }
        )
    confirmed = all(item["uninitialized_leaves"] > 0 for item in results)
    return {
        "status": "UNSUPPORTED" if confirmed else "NOT_PROVEN",
        "diagnosis_confirmed": confirmed,
        "scope": "source-derived cacheinfo regression probe; not a guest register read or sysfs success",
        "clidr": hex(clidr),
        "clidr_origin": (
            "instantiated_parameter" if configured else "source_default"
        ),
        "clidr_source_constant": None if configured else hex(clidr),
        "isa_source_sha256": hashlib.sha256(
            isa_source.read_bytes()
        ).hexdigest(),
        "dtb_sha256": hashlib.sha256(dtb.read_bytes()).hexdigest(),
        "dt_topology_validation": topology,
        "cpus": results,
        "reason": (
            "CLIDR and DT leave uninitialized cache leaves which exhaust "
            "the DT cache chain. See per-CPU leaf counts."
            if confirmed
            else "The current source/DT combination does not reproduce "
            "the uninitialized-leaf mismatch; guest validation is required."
        ),
        "sources": [
            "gem5/src/arch/arm/isa.cc: MISCREG_CLIDR/MISCREG_CCSIDR",
            "linux/arch/arm64/kernel/cacheinfo.c: populate_cache_leaves",
            "linux/drivers/base/cacheinfo.c: init_of_cache_level/cache_setup_of_node",
        ],
        "qualification": "This checker does not modify the guest. Configured cache IDs take precedence over source defaults. Runtime register and sysfs evidence remains separate.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    args = parser.parse_args()
    result = diagnose(args.run_directory)
    output = args.run_directory / "cache-visibility.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        f"{result['status']}: diagnosis_confirmed={result['diagnosis_confirmed']} ({output})"
    )
    return 0 if result["diagnosis_confirmed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
