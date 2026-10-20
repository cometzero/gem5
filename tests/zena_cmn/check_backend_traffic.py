# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Validate real native backend traffic artifacts; select only explicit ROI."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "scripts/zena_cmn")
)
from summarize_roi import stats_blocks


def check_drain(manifest, blocks):
    """Prove finite completed supply across reset, with mandatory counters."""
    drain = manifest["drain_completion"]
    assert drain["native_drain_returned"] is True
    warm, roi, final = (
        blocks[drain[name + "_dump_index"]]
        for name in ("warmup", "roi", "completed")
    )
    assert manifest["tick_frequency"] == 10**12
    for block in (warm, roi, final):
        assert block["simFreq"] == manifest["tick_frequency"]
    assert warm["finalTick"] == drain["start_tick"] - manifest["roi_ticks"]
    assert roi["finalTick"] == drain["start_tick"]
    assert final["finalTick"] == drain["end_tick"]
    assert warm["simTicks"] == drain["start_tick"] - manifest["roi_ticks"]
    assert roi["simTicks"] == manifest["roi_ticks"]
    assert drain["end_tick"] >= drain["start_tick"]
    assert final["simTicks"] == (
        manifest["roi_ticks"] + drain["end_tick"] - drain["start_tick"]
    )
    generated = completed = read_bytes = write_bytes = 0
    accepted_read_bytes = accepted_write_bytes = 0
    for channel in range(manifest["channels"]):
        prefix = f"system.generators{channel}."

        def lifetime(name):
            return warm[prefix + name] + final[prefix + name]

        count = lifetime("numPackets")
        reads, writes = lifetime("totalReads"), lifetime("totalWrites")
        assert count > 0 and count == reads + writes, "uncompleted supply"
        assert (
            final[prefix + "numPackets"] == roi[prefix + "numPackets"]
        ), "new packets generated during drain"
        assert lifetime("numSuppressed") == 0
        rb, wb = lifetime("bytesRead"), lifetime("bytesWritten")
        assert rb == reads * 64 and wb == writes * 64
        ctrl = f"system.mem_ctrls{channel}."
        for key, expected in (
            ("readReqs", reads),
            ("writeReqs", writes),
            ("bytesReadSys", rb),
            ("bytesWrittenSys", wb),
        ):
            assert warm[ctrl + key] + final[ctrl + key] == expected, (
                "controller acceptance disagrees with completed supply",
                key,
            )
        accepted_read_bytes += (
            warm[ctrl + "bytesReadSys"] + final[ctrl + "bytesReadSys"]
        )
        accepted_write_bytes += (
            warm[ctrl + "bytesWrittenSys"] + final[ctrl + "bytesWrittenSys"]
        )
        generated += count
        completed += reads + writes
        read_bytes += rb
        write_bytes += wb
    extra = {}
    lifetime_dram = {}
    for mode in ("Read", "Written"):
        keys = [
            f"system.mem_ctrls{i}.dram.dramBytes{mode}"
            for i in range(manifest["channels"])
        ]
        # All channels and both directions are mandatory, including zero.
        increments = [final[key] - roi[key] for key in keys]
        assert all(value >= 0 for value in increments)
        extra[mode] = sum(increments)
        lifetime_dram[mode] = sum(warm[key] + final[key] for key in keys)
    return {
        "status": "PASS",
        "start_tick": drain["start_tick"],
        "end_tick": drain["end_tick"],
        "elapsed_ticks": drain["end_tick"] - drain["start_tick"],
        "lifetime_generated_packets": generated,
        "lifetime_completed_packets": completed,
        "lifetime_response_read_bytes": read_bytes,
        "lifetime_response_write_bytes": write_bytes,
        "lifetime_controller_accepted_read_bytes": accepted_read_bytes,
        "lifetime_controller_accepted_write_bytes": accepted_write_bytes,
        "lifetime_dram_burst_bytes": lifetime_dram,
        "additional_dram_burst_bytes": extra,
        "scope": drain["scope"],
    }


def check(directory):
    manifest = json.loads((directory / "traffic-manifest.json").read_text())
    if manifest.get("hynix"):
        # Verify resolved native parameters, not just the Python class defaults.
        controllers = json.loads((directory / "config.json").read_text())[
            "system"
        ]["mem_ctrls"]
        assert len(controllers) == manifest["channels"]
        for controller in controllers:
            dram = controller["dram"]
            diagnostic = manifest.get("diagnostic", "baseline")
            assert dram["tREFI"] == (
                1953000 if diagnostic == "mr4-half-interval" else 3906000
            )
            queue_size = 16 if diagnostic == "queue16" else 64
            assert dram["read_buffer_size"] == queue_size
            assert dram["write_buffer_size"] == queue_size
            assert controller["mem_sched_policy"] == "frfcfs"
            assert dram["device_size"] == 2 * 1024**3
            assert dram["ranks_per_channel"] == 1
            assert dram["tCK"] == 938
            assert dram["tCL"] == 23 * 938
            assert dram["tCWL"] == 12 * 938
            assert dram["tRCD_WR"] == 9 * 938
            # Table340 uses command-relative PRE; native readyTime already
            # includes WL and BL/n_min, but not the extra 1 CK for writes.
            assert dram["tRTP"] == (6 + 6) * 938
            assert dram["tWR"] == (1 + 37) * 938
            assert dram["tRTW"] == (23 - 12 + 2) * 938
    blocks = stats_blocks((directory / "stats.txt").read_text())
    drain_result = (
        check_drain(manifest, blocks)
        if manifest.get("drain_completion")
        else None
    )
    matches = [
        block for block in blocks if block["simTicks"] == manifest["roi_ticks"]
    ]
    if drain_result:
        stats = blocks[manifest["drain_completion"]["roi_dump_index"]]
    else:
        assert len(matches) == 1, "must identify exactly one ROI dump"
        stats = matches[0]
    modes = (
        ["Read", "Written"]
        if manifest["read_percent"] == 50
        else ["Read" if manifest["read_percent"] else "Written"]
    )
    byte_counters = {}
    serviced = {}
    for mode in modes:
        counters = {
            key: value
            for key, value in stats.items()
            if key.endswith(".dram.dramBytes" + mode)
        }
        assert len(counters) == manifest["channels"]
        assert all(value > 0 for value in counters.values())
        for key, value in counters.items():
            burst_key = key.rsplit(".", 1)[0] + (
                ".readBursts" if mode == "Read" else ".writeBursts"
            )
            assert value == stats[burst_key] * 64
        byte_counters.update(counters)
        serviced[mode] = sum(counters.values())
    suppressed = [
        value for key, value in stats.items() if key.endswith(".numSuppressed")
    ]
    assert len(suppressed) == manifest["channels"] and not any(suppressed)
    packets = sum(
        value for key, value in stats.items() if key.endswith(".numPackets")
    )
    responses = sum(
        value
        for key, value in stats.items()
        if ".generators" in key
        and any(key.endswith(".bytes" + mode) for mode in modes)
    )
    dram_bytes = sum(byte_counters.values())
    seconds = manifest["roi_seconds"]
    bandwidth = dram_bytes / seconds / 1e9
    raw = manifest["theoretical_raw_GBps"]
    assert 0 < bandwidth < raw, "traffic exceeds nominal raw pin bandwidth"
    final = (
        blocks[manifest["drain_completion"]["completed_dump_index"]]
        if drain_result
        else blocks[-1]
    )
    tail_bytes = {
        mode: sum(
            value - stats[key]
            for key, value in final.items()
            if key.endswith(".dram.dramBytes" + mode)
        )
        for mode in modes
    }
    tail_packets = sum(
        value - stats[key]
        for key, value in final.items()
        if key.endswith(".numPackets")
    )
    read_bursts = sum(
        value
        for key, value in stats.items()
        if key.endswith(".dram.readBursts")
    )
    read_latency_ticks = sum(
        value
        for key, value in stats.items()
        if key.endswith(".dram.totMemAccLat")
    )
    read_queue_ticks = sum(
        value for key, value in stats.items() if key.endswith(".dram.totQLat")
    )
    return {
        "status": "PASS",
        "native_drain": drain_result,
        "run": str(directory),
        "channels": manifest["channels"],
        "mode": "mixed" if len(modes) == 2 else modes[0].lower(),
        "serviced_bytes_by_direction": serviced,
        "generated_bytes": packets * 64,
        "nominal_offered_bytes": int(
            seconds * 1e12 / manifest["period_ticks_per_generator"]
        )
        * 64
        * manifest["channels"],
        "residual_queue_entries": None,
        "residual_queue_availability": "NOT_EXPORTED: native averages/histograms are not end-of-ROI occupancy",
        "synthetic": manifest["synthetic"],
        "hynix": manifest.get("hynix", False),
        "diagnostic": manifest.get("diagnostic", "baseline"),
        "dram_read_latency_ns": (
            read_latency_ticks / read_bursts / 1000 if read_bursts else None
        ),
        "dram_read_queue_latency_ns": (
            read_queue_ticks / read_bursts / 1000 if read_bursts else None
        ),
        "read_retries": sum(
            value
            for key, value in stats.items()
            if key.endswith(".numRdRetry")
        ),
        "write_retries": sum(
            value
            for key, value in stats.items()
            if key.endswith(".numWrRetry")
        ),
        "roi_seconds": seconds,
        "post_roi_service_bytes": tail_bytes,
        "post_roi_generated_packets": tail_packets,
        "post_roi_seconds": (final["simTicks"] - stats["simTicks"])
        / manifest["tick_frequency"],
        "generator_packets": packets,
        "generator_response_bytes": responses,
        "generator_response_bytes_by_direction": {
            mode: sum(
                value
                for key, value in stats.items()
                if ".generators" in key and key.endswith(".bytes" + mode)
            )
            for mode in modes
        },
        "dram_burst_bytes": dram_bytes,
        "dram_GBps": bandwidth,
        "nominal_raw_GBps": raw,
        "efficiency": bandwidth / raw,
        "bytes_per_channel": list(byte_counters.values()),
        "exposed_rows_per_bank": manifest["exposed_rows_per_bank"],
        "physical_device_bytes": int(
            manifest["parameters"]["dram"]["device_size"]
        ),
        "tRFC_seconds": float(manifest["parameters"]["dram"]["tRFC"]),
        "tREFI_seconds": float(manifest["parameters"]["dram"]["tREFI"]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = [check(run) for run in args.runs]
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    for result in results:
        print(
            result["channels"],
            result["mode"],
            result["synthetic"],
            result["dram_burst_bytes"],
            f'{result["dram_GBps"]:.6f} GB/s',
            result["status"],
        )
