#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Address-filtered native CHI evidence; never replaces aggregate qualification."""
import argparse
import csv
import hashlib
import json
import re
from pathlib import Path

PROTO = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+Cache\s+(\w+)\s+(\w+)>(\w+)\s+\[0x([0-9a-f]+),"
)
TLB = re.compile(
    r"^(\d+): (\S+): Lookup (0x[0-9a-f]+),.* -> hit .*size: (0x[0-9a-f]+) pa: (0x[0-9a-f]+)"
)


def profile_actions(text):
    actions, pending, kind = {}, [], None
    for line in text.splitlines():
        match = re.search(
            r"case HASH_FUN\(Cache_State_(\w+), Cache_Event_(\w+)\)", line
        )
        if match:
            pending.append(match.groups())
        if "Profile_Hit(" in line:
            kind = "hit"
        elif "Profile_Miss(" in line:
            kind = "miss"
        if "return TransitionResult_" in line:
            if kind:
                for key in pending:
                    actions[key] = kind
            pending, kind = [], None
    return actions


def analyze(run, transitions):
    config = json.loads((run / "m5out/config.json").read_text())["system"]
    ruby = config["ruby"]
    homes = {h["cntrl"]["version"] for h in ruby["hnf"]}
    peers = {r["dsu_l3"]["version"] for r in ruby["rnf"][1:]}
    local = ruby["rnf"][0]["dsu_l3"]["version"]
    actions = profile_actions(transitions.read_text())
    if (
        not actions
        or actions.get(("SC", "ReadShared")) != "hit"
        or actions.get(("I", "ReadShared")) != "miss"
    ):
        raise ValueError(
            "native CHI profile-action map missing or incompatible"
        )
    summary = json.loads((run / "roofline-analysis/roofline.json").read_text())
    targets = {}
    for line in (run / "guest_console.log").read_text().splitlines():
        if line.startswith("ROOF_TARGET "):
            item = json.loads(line[12:])
            targets[item["id"]] = item
    rows = []
    lines = (run / "m5out/roi-trace.log").read_text().splitlines()
    for roi in summary["cases"]:
        target = targets[roi["id"]]
        start, end = (
            target["virtual_start"],
            target["virtual_start"] + target["bytes"],
        )
        mappings, events = set(), []
        for line in lines:
            match = TLB.search(line)
            if match:
                tick, path, va, mask, pa = match.groups()
                if not roi["begin_tick"] <= int(tick) <= roi["end_tick"]:
                    continue
                va, mask, pa = int(va, 16), int(mask, 16), int(pa, 16)
                if (
                    re.fullmatch(r"system\.roi_cpu0*0\.mmu\.dtb", path)
                    and start <= va < end
                ):
                    mappings.add((va & ~mask, pa & ~mask, mask))
            match = PROTO.search(line)
            if match:
                tick, version, event, state, next_state, addr = match.groups()
                if roi["begin_tick"] <= int(tick) <= roi["end_tick"]:
                    events.append(
                        (int(version), event, state, int(addr, 16) & ~63)
                    )
        physical, ambiguous = set(), False
        for va in range(start, end, 64):
            candidates = {
                pa + va - base
                for base, pa, mask in mappings
                if base <= va <= base + mask
            }
            if len(candidates) != 1:
                ambiguous = True
            else:
                physical.update(candidates)
        hits, misses, forbidden, outside = set(), set(), [], []
        hnf_totals = {"hit": 0, "miss": 0}
        for version, event, state, address in events:
            kind = actions.get((state, event))
            if version in homes and kind:
                hnf_totals[kind] += 1
            bad = (
                version in homes
                and (
                    kind == "miss"
                    or event.startswith(("SendSnp", "SendReadNoSnp"))
                )
            ) or (
                version in peers
                and event.startswith("Send")
                and "Data" in event
            )
            if address in physical:
                if version in homes and kind == "hit":
                    hits.add(address)
                if version == local and kind == "miss":
                    misses.add(address)
                if bad:
                    forbidden.append([version, event, state, address])
            elif bad:
                outside.append([version, event, state, address])
        reconciled = (
            hnf_totals["hit"] == roi["cache"]["SLC_proxy"]["hits"]
            and hnf_totals["miss"] == roi["cache"]["SLC_proxy"]["misses"]
        )
        qualified = (
            reconciled
            and not ambiguous
            and len(physical) == target["bytes"] // 64
            and hits == physical
            and misses == physical
            and not forbidden
        )
        rows.append(
            dict(
                id=roi["id"],
                kernel=roi.get("kernel"),
                useful_GB_s=roi.get("useful_GB_s"),
                GFLOP_s=roi.get("GFLOP_s"),
                useful_AI=roi.get("useful_AI"),
                status=(
                    "QUALIFIED_TARGET_ADDRESS_SLC"
                    if qualified
                    else "UNVERIFIED_TARGET_ADDRESS_SLC"
                ),
                aggregate_status=roi["slc_source"]["status"],
                aggregate_profile_counts=hnf_totals,
                trace_counter_reconciled=reconciled,
                target=target,
                mapped_lines=len(physical),
                ambiguous_mapping=ambiguous,
                target_hnf_hit_lines=len(hits),
                target_local_l3_miss_lines=len(misses),
                target_forbidden_events=forbidden,
                outside_forbidden_events=outside,
                mappings=sorted(mappings),
            )
        )
    reads = [r for r in rows if r["kernel"] in ("read_a", "read_b")]
    pair_qualified = len(reads) == 2 and all(
        r["status"] == "QUALIFIED_TARGET_ADDRESS_SLC" for r in reads
    )
    values = [r["useful_GB_s"] for r in reads]
    spread = (
        ((max(values) - min(values)) / (sum(values) / 2))
        if len(values) == 2
        and all(isinstance(v, (int, float)) and v > 0 for v in values)
        else None
    )
    return dict(
        target_filtered_read_pair={
            "samples": len(reads),
            "relative_spread": spread,
            "maximum_relative_spread": 0.05,
            "status": (
                "QUALIFIED_TARGET_ADDRESS_READ_PAIR"
                if pair_qualified and spread is not None and spread <= 0.05
                else "UNVERIFIED_TARGET_ADDRESS_READ_PAIR"
            ),
            "roof_GB_s": (
                sum(values) / 2
                if pair_qualified and spread is not None and spread <= 0.05
                else None
            ),
            "boundary": "Target address source-qualified; runtime includes all CPU/OS/loop overhead. Aggregate roof unchanged.",
        },
        qualification="NATIVE_MODEL_ADDRESS_FILTERED",
        cases=rows,
        transitions_sha256=hashlib.sha256(
            transitions.read_bytes()
        ).hexdigest(),
        limitations=[
            "Native TLB VA-to-PA observations, not Linux pagemap (kernel feature disabled).",
            "Target events within ROI only; aggregate strict result is retained.",
            "Checks native CHI profiled hits/misses and snoop/read-downstream/peer-data transition events, not silicon or a memory-bus byte trace.",
        ],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument(
        "--transitions",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / "build/ARM/mem/ruby/protocol/CHI/Cache_Transitions.cc",
    )
    args = parser.parse_args()
    result = analyze(args.run, args.transitions)
    output = args.run / "slc-target-attribution.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    fields = (
        "id",
        "kernel",
        "status",
        "aggregate_status",
        "useful_GB_s",
        "GFLOP_s",
        "useful_AI",
        "mapped_lines",
        "target_hnf_hit_lines",
        "target_local_l3_miss_lines",
        "trace_counter_reconciled",
    )
    with (args.run / "slc-target-attribution.csv").open("w") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=fields, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(result["cases"])
    print(output)
