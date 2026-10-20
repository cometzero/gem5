# SPDX-License-Identifier: BSD-3-Clause
"""Describe the implemented private-L2 / cluster-L3 cache sharing."""

from m5.ext.pyfdt import pyfdt


def add_cache_topology(filename, params, affinity):
    with open(filename, "rb") as source:
        tree = pyfdt.FdtBlobParse(source).to_fdt()
    root = tree.get_rootnode()
    used = [
        value[0]
        for path, value in root.walk()
        if path.endswith("/phandle") or path.endswith("/linux,phandle")
    ]
    first = max(used, default=0) + 1
    cores = params["cpu"]["cores"]
    group = params["cpu"]["cores_per_cluster"]
    for cluster in range(cores // group):
        cache = params["cache"]["dsu_l3"]
        node = pyfdt.FdtNode(f"l3-cache-{cluster}")
        for name, value in (
            ("phandle", first + cluster),
            ("cache-level", 3),
            ("cache-size", cache["size_mib_per_cluster"] * 1048576),
            ("cache-line-size", 64),
            (
                "cache-sets",
                cache["size_mib_per_cluster"]
                * 1048576
                // (64 * cache["assoc"]),
            ),
        ):
            node.append(pyfdt.FdtPropertyWords(name, [value]))
        node.append(pyfdt.FdtPropertyStrings("compatible", ["cache"]))
        node.append(pyfdt.FdtProperty("cache-unified"))
        root.append(node)
    for cpu in range(cores):
        handle = first + cores // group + cpu
        cache = params["cache"]["l2"]
        node = pyfdt.FdtNode(f"l2-cache-{cpu}")
        for name, value in (
            ("phandle", handle),
            ("cache-level", 2),
            ("cache-size", cache["size_kib_per_core"] * 1024),
            ("cache-line-size", 64),
            (
                "cache-sets",
                cache["size_kib_per_core"] * 1024 // (64 * cache["assoc"]),
            ),
            ("next-level-cache", first + cpu // group),
        ):
            node.append(pyfdt.FdtPropertyWords(name, [value]))
        node.append(pyfdt.FdtPropertyStrings("compatible", ["cache"]))
        node.append(pyfdt.FdtProperty("cache-unified"))
        root.append(node)
        cpu_node = tree.resolve_path(f"/cpus/cpu@{affinity(cpu):x}")
        cpu_node.append(pyfdt.FdtPropertyWords("next-level-cache", [handle]))
        for prefix, level in (("i", "l1i"), ("d", "l1d")):
            spec = params["cache"][level]
            size = spec["size_kib_per_core"] * 1024
            for suffix, value in (
                ("size", size),
                ("line-size", 64),
                ("sets", size // (64 * spec["assoc"])),
            ):
                cpu_node.append(
                    pyfdt.FdtPropertyWords(f"{prefix}-cache-{suffix}", [value])
                )
    with open(filename, "wb") as target:
        target.write(tree.to_dtb())

    from pathlib import Path

    Path(filename).with_suffix(".dts").write_text(tree.to_dts())
