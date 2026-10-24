# SPDX-License-Identifier: BSD-3-Clause
"""Check the single-mailbox SCMI DT ABI against an existing AP4/AP16 DTB."""

import argparse
import sys
from pathlib import Path

from m5.ext.pyfdt import pyfdt
from m5.objects import ZenaCSS

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "configs"))
from zena_cmn.power_control import add_power_control_dtb

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("baseline", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
with args.baseline.open("rb") as source:
    original = pyfdt.FdtBlobParse(source).to_fdt()
cores = sum(
    node.name.startswith("cpu@") for node in original.resolve_path("/cpus")
)
assert cores in (4, 16)
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_bytes(args.baseline.read_bytes())
add_power_control_dtb(args.output, cores, ZenaCSS.affinity)
with args.output.open("rb") as source:
    tree = pyfdt.FdtBlobParse(source).to_fdt()


def words(node, key):
    return next(prop.words for prop in node if prop.name == key)


scmi = tree.resolve_path("/firmware/scmi")
assert len(words(scmi, "mboxes")) == 2
assert len(words(scmi, "shmem")) == 1
perf = words(tree.resolve_path("/firmware/scmi/protocol@13"), "phandle")[0]
sensor = words(tree.resolve_path("/firmware/scmi/protocol@15"), "phandle")[0]
for cluster in range(cores // 4):
    for index in range(cluster * 4, cluster * 4 + 4):
        cpu = tree.resolve_path(f"/cpus/cpu@{ZenaCSS.affinity(index):x}")
        assert words(cpu, "clocks") == [perf, cluster]
        assert words(cpu, "#cooling-cells") == [2]
    first_cpu = tree.resolve_path(
        f"/cpus/cpu@{ZenaCSS.affinity(cluster * 4):x}"
    )
    zone_path = f"/thermal-zones/cluster{cluster}-thermal"
    assert words(tree.resolve_path(zone_path), "thermal-sensors") == [
        sensor,
        cluster,
    ]
    cooling = tree.resolve_path(zone_path + "/cooling-maps/map0")
    assert words(cooling, "cooling-device") == [
        words(first_cpu, "phandle")[0],
        0xFFFFFFFF,
        0xFFFFFFFF,
    ]
print(f"POWER_CONTROL_DTB_PASS cores={cores} domains={cores // 4}")
