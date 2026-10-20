#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Conservative cache-aware Roofline from explicit multi-ROI dumps."""
import argparse
import csv
import json
import math
import re
from pathlib import Path

from summarize_roi import stats_blocks

LEVELS = ("L1", "L2", "L3", "SLC_proxy")


def number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def cache_paths(config):
    system = config["system"]
    cpu = next(cpu for cpu in system["cpu"] if cpu["cpu_id"] == 0)
    ruby = system["ruby"]
    return {
        "L1": [cpu["l1d"]["cache"]["path"]],
        "L2": [cpu["l2"]["cache"]["path"]],
        "L3": [ruby["rnf"][0]["dsu_l3"]["cache"]["path"]],
        "SLC_proxy": [home["cntrl"]["cache"]["path"] for home in ruby["hnf"]],
    }


def counter(stats, path, suffix):
    key = path + suffix
    if key not in stats:
        # Native Ruby statistics drop the zero padding used in config paths.
        key = re.sub(r"\.(cpu|mem_ctrls)0+(\d+)(?=\.)", r".\1\2", key)
    value = stats.get(key)
    return value if number(value) and value >= 0 else None


def cache_metrics(stats, paths):
    result = {}
    for level, names in paths.items():
        hits = [counter(stats, name, ".m_demand_hits") for name in names]
        misses = [counter(stats, name, ".m_demand_misses") for name in names]
        h = sum(hits) if all(v is not None for v in hits) else None
        m = sum(misses) if all(v is not None for v in misses) else None
        result[level] = {
            "hits": h,
            "misses": m,
            "hit_fraction": (
                h / (h + m)
                if h is not None and m is not None and h + m
                else None
            ),
        }
    return result


def residency(level, caches, useful_bytes):
    if level == "DRAM_cold":
        return "COLD_DIAGNOSTIC"
    if level == "compute":
        return "COMPUTE"
    if level not in LEVELS:
        raise ValueError(f"unknown level: {level}")
    target = caches[level]
    if target["hit_fraction"] is None or target["hit_fraction"] < 0.9:
        return "UNVERIFIED_TARGET_HIT"
    if level == "L1":
        return "QUALIFIED_MODEL_PROXY"
    # L1 has multiple subline loads: its miss percentage is not a line miss ratio.
    required_lines = useful_bytes / 64 * 0.9
    if (
        caches["L1"]["misses"] is None
        or caches["L1"]["misses"] < required_lines
    ):
        return "UNVERIFIED_UPPER_CACHE"
    if target["hits"] < required_lines:
        return "UNVERIFIED_LINE_COVERAGE"
    for upper in LEVELS[1 : LEVELS.index(level)]:
        fraction = caches[upper]["hit_fraction"]
        if fraction is None or fraction > 0.1:
            return "UNVERIFIED_UPPER_CACHE"
    return "QUALIFIED_MODEL_PROXY"


def model_description(config):
    system = config["system"]
    ruby = system["ruby"]
    sizes = [home["cntrl"]["cache"].get("size") for home in ruby["hnf"]]
    slc = (
        f"{sum(sizes) / 1024**2:g} MiB SLC"
        if sizes and all(isinstance(size, int) for size in sizes)
        else "SLC capacity unknown"
    )
    network = ruby.get("network", {}).get("type", "network unknown")
    return f"{len(system['cpu'])}-core, one active benchmark core; {slc} / {network} proxy"


def slc_source(stats, config, caches, unique_lines, dram):
    """Conservative aggregate check; zero message counters are native nozero."""
    ruby = config["system"]["ruby"]

    def messages(objects, buffer):
        values = []
        for obj in objects:
            path = obj.get(buffer, {}).get("path")
            if not path:
                return None
            value = stats.get(path + ".m_msg_count", 0)
            if not number(value) or value < 0:
                return None
            values.append(value)
        return sum(values) if values else None

    snoops = messages([home["cntrl"] for home in ruby["hnf"]], "snpOut")
    peer_data = messages([rnf["dsu_l3"] for rnf in ruby["rnf"][1:]], "datOut")
    hits = caches["SLC_proxy"]["hits"]
    upper = caches["L3"]["misses"]
    qualified = (
        hits is not None
        and upper is not None
        and hits >= unique_lines * 0.9
        and upper >= unique_lines * 0.9
        and snoops == 0
        and peer_data == 0
        and dram["Read"] == 0
    )
    return {
        "status": (
            "QUALIFIED_AGGREGATE_SLC" if qualified else "UNVERIFIED_SLC_SOURCE"
        ),
        "hnf_snoop_messages": snoops,
        "peer_dsu_data_messages": peer_data,
        "hnf_hits": hits,
        "benchmark_l3_misses": upper,
        "dram_read_bytes": dram["Read"],
        "limitation": "Aggregate ROI counters include OS/instruction traffic; zero-snoop/data/DRAM criterion has no tolerance.",
    }


def analyze(stats_text, guest_text, events, config, allow_partial=False):
    if "ROOF_SLC_CHECK" in guest_text and any(
        cpu["l2"].get("use_prefetcher") for cpu in config["system"]["cpu"]
    ):
        raise ValueError("SLC source check requires prefetch off")
    cases = {}
    for line in guest_text.splitlines():
        if line.startswith("ROOF_CASE "):
            case = json.loads(line[len("ROOF_CASE ") :])
            identifier = case["id"]
            if (
                type(identifier) is not int
                or identifier <= 0
                or identifier in cases
            ):
                raise ValueError("invalid or duplicate case id")
            if case.get("result") != "PASS":
                raise ValueError(f"case {identifier} did not pass")
            if (
                not number(case.get("checksum"))
                or not number(case.get("expected"))
                or case["checksum"] != case["expected"]
            ):
                raise ValueError(f"case {identifier} checksum mismatch")
            for field in ("useful_bytes", "flops", "working_set_bytes"):
                if type(case.get(field)) is not int or case[field] < 0:
                    raise ValueError(f"invalid {field}")
            cases[identifier] = case
    bounds = {}
    for event in events:
        if event.get("phase") not in ("roi_begin", "roi_end"):
            continue
        identifier = event.get("case_id")
        if type(identifier) is not int or identifier <= 0:
            raise ValueError("missing or invalid event case_id")
        entry = bounds.setdefault(identifier, {})
        phase = event["phase"]
        if phase in entry:
            raise ValueError("duplicate case event")
        tick = event.get("tick")
        if type(tick) is not int or tick < 0:
            raise ValueError("invalid event tick")
        entry[phase] = tick
    if not cases:
        raise ValueError("no completed cases")
    if not allow_partial and set(cases) != set(bounds):
        raise ValueError("case/event set mismatch")
    blocks = stats_blocks(stats_text)
    paths = cache_paths(config)
    dram_paths = [
        controller["dram"]["path"]
        for controller in config["system"]["mem_ctrls"]
    ]
    rows = []
    previous_end = -1
    for identifier, case in sorted(cases.items()):
        entry = bounds.get(identifier, {})
        if set(entry) != {"roi_begin", "roi_end"}:
            raise ValueError("missing case event")
        begin, end = entry["roi_begin"], entry["roi_end"]
        if end <= begin or begin < previous_end:
            raise ValueError("invalid or overlapping case interval")
        previous_end = end
        matches = [
            block
            for block in blocks
            if block.get("finalTick") == end
            and block.get("simTicks") == end - begin
        ]
        if len(matches) != 1:
            raise ValueError(
                "require exactly one matching stats block per case"
            )
        stats = matches[0]
        frequency = stats.get("simFreq")
        if not number(frequency) or frequency <= 0:
            raise ValueError("invalid simFreq")
        seconds = (end - begin) / frequency
        caches = cache_metrics(stats, paths)
        raw_dram = {
            key: value
            for key, value in stats.items()
            if re.search(r"\.dramBytes(Read|Written)$", key)
        }
        dram = {}
        for direction in ("Read", "Written"):
            values = [
                counter(stats, path, ".dramBytes" + direction)
                for path in dram_paths
            ]
            dram[direction] = (
                sum(values)
                if values and all(number(v) and v >= 0 for v in values)
                else None
            )
        rows.append(
            {
                **case,
                "begin_tick": begin,
                "end_tick": end,
                "seconds": seconds,
                "GFLOP_s": case["flops"] / seconds / 1e9,
                "useful_GB_s": case["useful_bytes"] / seconds / 1e9,
                "useful_AI": (
                    case["flops"] / case["useful_bytes"]
                    if case["useful_bytes"]
                    else None
                ),
                "cache": caches,
                "dram_bytes": dram,
                "raw_dram": raw_dram,
                "raw_demand_counters": {
                    key: value
                    for key, value in stats.items()
                    if key.endswith((".m_demand_hits", ".m_demand_misses"))
                },
                "residency": residency(
                    case["level"], caches, case["useful_bytes"]
                ),
            }
        )
        if "ROOF_SLC_CHECK" in guest_text:
            source = slc_source(
                stats, config, caches, case["measured_unique_lines"], dram
            )
            rows[-1]["slc_source"] = source
            if source["status"] != "QUALIFIED_AGGREGATE_SLC":
                rows[-1]["residency"] = "UNVERIFIED_SLC_SOURCE"
    read_repeatability = read_roofs(rows)
    return {
        "qualification": "MODEL_ONLY",
        "partial": allow_partial,
        "read_repeatability": read_repeatability,
        "model": model_description(config),
        "hierarchical_AI": "UNAVAILABLE: cache boundary byte counters not verified",
        "cache_paths": paths,
        "dram_paths": dram_paths,
        "cases": rows,
        "limitations": [
            "Useful-byte cache-aware Roofline, not hierarchical transferred-byte Roofline.",
            "Native demand counters include instruction, kernel and page walk traffic at shared caches.",
            "CHI Profile_Hit requires valid cache data; directory/peer-only ReadShared states count as misses.",
            "Residency thresholds are diagnostic model proxies, not physical-address-filtered proof.",
            "Cold DRAM samples do not establish a steady-state bandwidth roof.",
            "Read roof and register FMA are observed kernel throughput, not guaranteed hardware peak.",
            "Cache roofs require read_a/read_b residency qualification and relative bandwidth spread <= 5%.",
        ],
    }


def read_roofs(rows):
    summaries = {}
    for level in (*LEVELS, "DRAM_cold"):
        reads = [
            row
            for row in rows
            if row["level"] == level and row["kernel"].startswith("read")
        ]
        rates = [row["useful_GB_s"] for row in reads]
        spread = (
            (max(rates) - min(rates)) / (sum(rates) / len(rates))
            if len(rates) > 1 and sum(rates)
            else None
        )
        if level == "DRAM_cold":
            status = "COLD_DIAGNOSTIC_NO_ROOF"
        elif len(reads) != 2 or {row["kernel"] for row in reads} != {
            "read_a",
            "read_b",
        }:
            status = "UNVERIFIED_MISSING_READ_PAIR"
        elif any(row["residency"] != "QUALIFIED_MODEL_PROXY" for row in reads):
            status = "UNVERIFIED_RESIDENCY"
        elif spread is None or spread > 0.05:
            status = "UNVERIFIED_STABILITY"
        else:
            status = "QUALIFIED_EMPIRICAL_ROOF"
        summaries[level] = {
            "samples": len(reads),
            "relative_spread": spread,
            "maximum_relative_spread": 0.05,
            "roof_status": status,
            "roof_GB_s": (
                max(rates) if status == "QUALIFIED_EMPIRICAL_ROOF" else None
            ),
        }
    return summaries


def plot(result, output):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    rows = result["cases"]
    compute = [row["GFLOP_s"] for row in rows if row["residency"] == "COMPUTE"]
    ceiling = max(compute, default=None)
    fig, (ax, bars) = plt.subplots(1, 2, figsize=(14, 5), layout="constrained")
    colors = dict(zip((*LEVELS, "DRAM_cold"), plt.get_cmap("tab10").colors))
    x = np.logspace(-2, 3, 200)
    if ceiling:
        ax.axhline(
            ceiling,
            color="black",
            linestyle=":",
            label=f"Register FMA {ceiling:.2f} GFLOP/s",
        )
    labels, rates, bar_colors = [], [], []
    for level, color in colors.items():
        selected = [row for row in rows if row["level"] == level]
        reads = [row for row in selected if row["kernel"].startswith("read")]
        roof = result["read_repeatability"][level]
        if roof["roof_status"] == "QUALIFIED_EMPIRICAL_ROOF" and ceiling:
            bandwidth = roof["roof_GB_s"]
            ax.plot(
                x,
                np.minimum(x * bandwidth, ceiling),
                color=color,
                label=f"{level} empirical read roof",
            )
        for row in selected:
            if row["useful_AI"] and row["GFLOP_s"]:
                good = row["residency"] == "QUALIFIED_MODEL_PROXY"
                ax.scatter(
                    row["useful_AI"],
                    row["GFLOP_s"],
                    color=color,
                    marker="o" if good else "x",
                    label=(
                        f"{level} samples (no qualified roof)"
                        if row["kernel"] == "fma_r1"
                        and roof["roof_status"] != "QUALIFIED_EMPIRICAL_ROOF"
                        else None
                    ),
                )
        for row in reads:
            labels.append(f"{level}\n{row['kernel']}")
            rates.append(row["useful_GB_s"])
            bar_colors.append(color)
    ax.set(
        xscale="log",
        yscale="log",
        xlabel="Useful arithmetic intensity [FLOP/useful B]",
        ylabel="Measured GFLOP/s",
        title="Cache-aware empirical Roofline (single active core)",
    )
    ax.grid(True, which="both", alpha=0.2)
    ax.legend(fontsize=8)
    bars.bar(range(len(rates)), rates, color=bar_colors)
    bars.set_xticks(
        range(len(rates)), labels, rotation=55, ha="right", fontsize=8
    )
    bars.set(
        ylabel="Useful read GB/s",
        title="Read samples (cold DRAM is diagnostic only)",
    )
    fig.suptitle(
        result["model"] + " | x = residency unverified or cold diagnostic"
    )
    for extension in ("svg", "png"):
        fig.savefig(output / f"roofline.{extension}", dpi=160)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--guest-log", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--no-plot", action="store_true")
    args = parser.parse_args()
    m5out = args.run / "m5out"
    guest = args.guest_log or args.run / "guest_console.log"
    output = args.output or args.run / "roofline-analysis"
    result = analyze(
        (m5out / "stats.txt").read_text(),
        guest.read_text(),
        json.loads((m5out / "roi-events.json").read_text()),
        json.loads((m5out / "config.json").read_text()),
        args.allow_partial,
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / "roofline.json").write_text(json.dumps(result, indent=2) + "\n")
    fields = [
        "id",
        "level",
        "kernel",
        "working_set_bytes",
        "useful_bytes",
        "flops",
        "seconds",
        "GFLOP_s",
        "useful_GB_s",
        "useful_AI",
        "residency",
    ]
    with (output / "cases.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=fields, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(result["cases"])
    if not args.no_plot:
        plot(result, output)
    print(json.dumps({"cases": len(result["cases"]), "output": str(output)}))


if __name__ == "__main__":
    main()
