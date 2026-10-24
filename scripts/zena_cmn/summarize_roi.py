#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Summarize one explicitly bounded ROI; never sum repeated stats dumps."""

import argparse
import json
import math
import re
from pathlib import Path


def stats_blocks(text):
    blocks = []
    current = None
    for line in text.splitlines():
        if "Begin Simulation Statistics" in line:
            if current is not None:
                raise ValueError("nested statistics block")
            current = {}
        elif "End Simulation Statistics" in line:
            if current is None:
                raise ValueError("statistics end without begin")
            blocks.append(current)
            current = None
        elif current is not None:
            fields = line.split()
            if len(fields) < 2 or fields[0].startswith("#"):
                continue
            if fields[1].startswith("|"):
                if fields[0] in current:
                    raise ValueError(f"duplicate histogram: {fields[0]}")
                current[fields[0]] = {
                    "format": "native_oneline_histogram",
                    "raw": line.split(fields[0], 1)[1].strip(),
                }
                continue
            try:
                value = int(fields[1])
            except ValueError:
                try:
                    value = float(fields[1])
                except ValueError as exc:
                    raise ValueError(f"invalid statistic: {line}") from exc
                if not math.isfinite(value):
                    value = None
            if fields[0] in current:
                if fields[0].rsplit(".", 1)[-1] in (
                    "avg_size",
                    "avg_util",
                    "avg_reserved",
                ):
                    current[fields[0]] = None
                    ambiguous = current.setdefault(
                        "__ambiguous_statistics__", []
                    )
                    if fields[0] not in ambiguous:
                        ambiguous.append(fields[0])
                    continue
                raise ValueError(f"duplicate statistic: {fields[0]}")
            current[fields[0]] = value
    if current is not None:
        raise ValueError("truncated statistics block")
    return blocks


def summarize(stats_text, guest_text, begin_tick, end_tick):
    if not isinstance(begin_tick, int) or not isinstance(end_tick, int):
        raise ValueError("ROI ticks must be integers")
    if begin_tick < 0 or end_tick <= begin_tick:
        raise ValueError("ROI end must follow begin")
    candidates = [
        (index, block)
        for index, block in enumerate(stats_blocks(stats_text))
        if block.get("finalTick") == end_tick
        and block.get("simTicks") == end_tick - begin_tick
    ]
    if not candidates:
        raise ValueError("no stats dump matches the explicit ROI boundaries")
    index, stats = candidates[0]
    semantic = lambda block: {
        key: value
        for key, value in block.items()
        if not key.startswith("host")
    }
    if any(semantic(block) != semantic(stats) for _, block in candidates[1:]):
        raise ValueError("conflicting dumps at the ROI boundary")
    return summarize_block(
        stats, guest_text, begin_tick, end_tick, index, len(candidates) - 1
    )


def summarize_block(
    stats, guest_text, begin_tick, end_tick, index=None, duplicate_dumps=0
):
    """Summarize a selected block or explicitly aggregated additive counters."""
    if type(begin_tick) is not int or type(end_tick) is not int:
        raise ValueError("ROI ticks must be integers")
    if begin_tick < 0 or end_tick <= begin_tick:
        raise ValueError("ROI end must follow begin")
    frequency = stats.get("simFreq")
    if frequency is None or frequency <= 0:
        raise ValueError("missing positive simFreq")
    seconds = (end_tick - begin_tick) / frequency
    results = []
    for line in guest_text.splitlines():
        if line.startswith("CMN_RESULT "):
            results.append(json.loads(line[len("CMN_RESULT ") :]))
    if len(results) != 1 or results[0].get("result") != "PASS":
        raise ValueError("require one passing guest result")
    guest = results[0]
    useful = guest.get("useful_bytes")
    if not isinstance(useful, int) or isinstance(useful, bool) or useful < 0:
        raise ValueError("guest useful_bytes must be a nonnegative integer")

    def available(values):
        valid = sum(value is not None for value in values.values())
        return {
            "value": values or None,
            "availability": (
                "unavailable"
                if not valid
                else (
                    "available"
                    if valid == len(values)
                    else "partially_available"
                )
            ),
        }

    channels = {}
    for key, value in stats.items():
        match = re.fullmatch(r"(.+)\.dramBytes(Read|Written)", key)
        if match:
            channels.setdefault(match[1], {})[match[2]] = value
    reads = [channel.get("Read") for channel in channels.values()]
    writes = [channel.get("Written") for channel in channels.values()]

    def total(values):
        return (
            sum(values)
            if values and all(v is not None for v in values)
            else None
        )

    read_bytes, write_bytes = total(reads), total(writes)
    dram_bytes = (
        read_bytes + write_bytes
        if read_bytes is not None and write_bytes is not None
        else None
    )
    patterns = {
        "cpu": r"(?:rename|iew|lsq|commit|rob|iq|fu|numCycles|numInsts)",
        "cache": r"(?:cache|l1|l2|l3|home|hnf).*(?:hit|miss|access|stall|array)",
        "tbe": r"tbe",
        "network": r"(?:network|link|router).*(?:flit|credit|packet|utilization|stall)",
        "dram_detail": r"(?:perBank|avgQLat|avgMemAccLat|rdQLen|wrQLen|rowHit|numRdRetry|numWrRetry)",
    }
    counters = {
        name: available(
            {
                key: value
                for key, value in stats.items()
                if not key.startswith("__")
                and re.search(pattern, key, re.IGNORECASE)
            }
        )
        for name, pattern in patterns.items()
    }
    return {
        "qualification": "MODEL_ONLY",
        "guest": guest,
        "roi": {
            "begin_tick": begin_tick,
            "end_tick": end_tick,
            "ticks": end_tick - begin_tick,
            "frequency_hz": frequency,
            "seconds": seconds,
            "stats_block_index": index,
            "duplicate_dumps_ignored": duplicate_dumps,
        },
        "application": {
            "useful_bytes": useful,
            "GB_s": useful / seconds / 1e9,
        },
        "dram": {
            "read_bytes": read_bytes,
            "write_bytes": write_bytes,
            "GB_s": (
                dram_bytes / seconds / 1e9 if dram_bytes is not None else None
            ),
            "availability": (
                "available" if dram_bytes is not None else "unavailable"
            ),
            "channels": channels,
        },
        "counters": counters,
        "ambiguous_counters": stats.get("__ambiguous_statistics__", []),
        "limitations": [
            "DRAM statistics include kernel/IRQ/page-walk traffic; no PA filtering.",
            "Write bytes count serviced bursts, not all dirty cache lines drained.",
            "Counters retain native names and units; no unmeasured occupancy inferred.",
        ],
    }


def event_bounds(events):
    begins = [
        event["tick"] for event in events if event.get("phase") == "roi_begin"
    ]
    ends = [
        event["tick"] for event in events if event.get("phase") == "roi_end"
    ]
    if len(begins) != 1 or len(ends) != 1:
        raise ValueError("require exactly one ROI begin and end event")
    return begins[0], ends[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stats", type=Path, required=True)
    parser.add_argument("--guest", type=Path, required=True)
    parser.add_argument("--begin-tick", type=int)
    parser.add_argument("--end-tick", type=int)
    parser.add_argument("--events", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.events:
        if args.begin_tick is not None or args.end_tick is not None:
            parser.error("--events cannot be combined with explicit ticks")
        events = json.loads(args.events.read_text())
        args.begin_tick, args.end_tick = event_bounds(events)
    if args.begin_tick is None or args.end_tick is None:
        parser.error("require --events or both --begin-tick/--end-tick")
    result = summarize(
        args.stats.read_text(),
        args.guest.read_text(),
        args.begin_tick,
        args.end_tick,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, allow_nan=False) + "\n"
    )


if __name__ == "__main__":
    main()
