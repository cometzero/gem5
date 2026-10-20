# SPDX-License-Identifier: BSD-3-Clause
"""Account one explicit Linux NPU test ROI, including syscall overhead."""
import json

from summarize_roi import stats_blocks


def measurement(text, begin, end, channels=1):
    candidates = [
        s
        for s in stats_blocks(text)
        if s.get("finalTick") == end and s.get("simTicks") == end - begin
    ]
    if end <= begin or not candidates:
        raise ValueError("missing exact NPU ROI stats")
    stats = candidates[0]
    semantic = lambda block: {
        key: value
        for key, value in block.items()
        if not key.startswith("host")
    }
    if any(semantic(block) != semantic(stats) for block in candidates[1:]):
        raise ValueError("ambiguous NPU ROI stats")
    if (
        not isinstance(stats.get("simFreq"), (int, float))
        or stats["simFreq"] <= 0
    ):
        raise ValueError("invalid simulation frequency")
    prefix = "system.platform.npu."
    counters = {
        key[len(prefix) :]: value
        for key, value in stats.items()
        if key.startswith(prefix)
    }
    expected = {
        f"system.mem_ctrls{i}.dram.{suffix}"
        for i in range(channels)
        for suffix in ("dramBytesRead", "dramBytesWritten")
    }
    actual = {
        key
        for key in stats
        if key.startswith("system.mem_ctrls")
        and key.endswith((".dramBytesRead", ".dramBytesWritten"))
    }
    if channels <= 0 or actual != expected:
        raise ValueError("missing or unexpected DRAM burst counters")
    seconds = (end - begin) / stats["simFreq"]
    dram = {
        direction: sum(
            value
            for key, value in stats.items()
            if key.startswith("system.mem_ctrls") and key.endswith(suffix)
        )
        for direction, suffix in (
            ("read", ".dramBytesRead"),
            ("write", ".dramBytesWritten"),
        )
    }
    return {
        "status": "PASS",
        "qualification": "MODEL_ONLY",
        "roi_begin_tick": begin,
        "roi_end_tick": end,
        "roi_seconds": seconds,
        "npu": counters,
        "dram_bytes": dram,
    }


def summarize_npu(text, console, begin, end, channels=1):
    if "NPU v0.1 reused BO visibility PASS: 3 submissions" not in console:
        raise ValueError("missing repeated NC visibility check")
    if console.count("NPU v0.1 GEMM PASS:") != 3:
        raise ValueError("expected three verified GEMM submissions")
    result = measurement(text, begin, end, channels)
    counters = result["npu"]
    for key, expected in {
        "jobs": 3,
        "macs": 1536,
        "dmaReadBytes": 384,
        "dmaWriteBytes": 768,
    }.items():
        if counters.get(key) != expected:
            raise ValueError(f"NPU {key}: {counters.get(key)} != {expected}")
    result["measurement"] = (
        "Linux test suite including ioctl/map/reset/sync overhead"
    )
    result["payload_bytes_per_second"] = 1152 / result["roi_seconds"]
    result["not_proven"] = [
        "silicon performance",
        "DMA/compute overlap",
        "CPU/NPU concurrent bandwidth",
    ]
    return result


def payload_phase_overlap(result, record):
    """Bracket guest/sim clock offset; this is not per-burst attribution."""
    if record["mode"] != "concurrent":
        return {"status": "NOT_APPLICABLE"}
    if record.get("jobs", 1) != 1:
        return {
            "status": "UNAVAILABLE",
            "reason": "first/last payload envelope includes inter-job gaps",
        }
    if "roi_clock_before_ns" not in record:
        return {"status": "UNAVAILABLE", "reason": "no marker clock bracket"}
    before, after = record["roi_clock_before_ns"], record["roi_begin_ns"]
    if before > after:
        raise ValueError("invalid ROI clock bracket")
    tick_ns = (
        result["roi_seconds"]
        * 1e9
        / (result["roi_end_tick"] - result["roi_begin_tick"])
    )
    origin = result["roi_begin_tick"] * tick_ns
    # Fresh boot/no NTP: retain a conservative guard beyond timer quantization.
    guard = 1000
    low, high = origin - after - guard, origin - before + guard
    first = result["npu"].get("firstPayloadAcceptedTick", 0) * tick_ns
    last = result["npu"].get("lastPayloadResponseTick", 0) * tick_ns
    if not first or last < first:
        raise ValueError("missing NPU payload interval")
    workers = record.get("cpu_workers")
    if not workers or any(w["end_ns"] <= w["begin_ns"] for w in workers):
        raise ValueError("missing CPU worker intervals")

    def union_overlap(start_offset, end_offset):
        windows = sorted(
            (
                max(w["begin_ns"] + start_offset, first),
                min(w["end_ns"] + end_offset, last),
            )
            for w in workers
        )
        total, covered = 0, first
        for start, end in windows:
            start = max(start, covered)
            if end > start:
                total += end - start
                covered = end
        return total

    minimum = union_overlap(high, low)
    maximum = union_overlap(low, high)
    return {
        "status": (
            "ESTIMATED_NO_PHASE_OVERLAP"
            if maximum == 0
            else "ESTIMATED_PHASE_OVERLAP" if minimum > 0 else "UNCERTAIN"
        ),
        "overlap_min_ns": minimum,
        "overlap_max_ns": maximum,
        "clock_guard_ns": guard,
        "clock_offset_bracket_ns": [low, high],
        "payload_simulator_ns": [first, last],
        "scope": "union of CPU worker windows versus NPU payload phase; not DRAM bursts",
        "clock_assumption": "constant offset within heuristic 1us guard; fresh boot/no NTP",
    }


def summarize_npu_bench(text, console, begin, end, channels=1):
    records = [
        json.loads(line.split("=", 1)[1])
        for line in console.splitlines()
        if line.startswith("NPU_BENCH_JSON=")
    ]
    if len(records) != 1:
        raise ValueError("expected one NPU benchmark result")
    record = records[0]
    if record.get("result") != "PASS" or record.get("checksums") != "PASS":
        raise ValueError("benchmark correctness failed")
    if record.get("mode") not in ("cpu", "npu", "concurrent"):
        raise ValueError("invalid benchmark mode")
    cpu, npu = record["mode"] != "npu", record["mode"] != "cpu"
    jobs = record.get("jobs", 1)
    if type(jobs) is not int or not 1 <= jobs <= 32:
        raise ValueError("invalid job count")
    per_job = record["mib"] * 1024**2 * record["passes"]
    size = per_job * jobs
    if size <= 0:
        raise ValueError("nonpositive payload")
    expected = {
        "cpu_bytes": size if cpu else 0,
        "npu_read_bytes": size if npu else 0,
        "npu_write_bytes": size if npu else 0,
    }
    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("guest byte accounting mismatch")
    if "jobs" in record:
        windows = record.get("npu_job_windows", [])
        expected_jobs = jobs if npu else 0
        if (
            record.get("jobs_completed") != expected_jobs
            or len(windows) != expected_jobs
        ):
            raise ValueError("incomplete job accounting")
        previous_end = record["npu_begin_ns"]
        for window in windows:
            if (
                not (
                    previous_end
                    <= window["begin_ns"]
                    < window["end_ns"]
                    <= record["npu_end_ns"]
                )
                or window["dma_bytes"] != 2 * per_job
            ):
                raise ValueError("invalid per-job window or byte accounting")
            previous_end = window["end_ns"]
    result = measurement(text, begin, end, channels)
    for key, value in {
        "jobs": jobs if npu else 0,
        "dmaReadBytes": expected["npu_read_bytes"],
        "dmaWriteBytes": expected["npu_write_bytes"],
        "macs": 0,
    }.items():
        if result["npu"].get(key) != value:
            raise ValueError("model/guest mismatch: " + key)
    rates = {}
    for name, active, total in (
        ("cpu", cpu, expected["cpu_bytes"]),
        ("npu", npu, expected["npu_read_bytes"] * 2),
    ):
        elapsed = record[name + "_end_ns"] - record[name + "_begin_ns"]
        if active and elapsed <= 0:
            raise ValueError("invalid participant time")
        rates[name + "_bytes_per_second"] = (
            total / (elapsed / 1e9) if active else 0
        )
    if cpu and npu and record["overlap_ns"] <= 0:
        raise ValueError("concurrent run did not overlap")
    if cpu and npu and "jobs" in record:
        workers = record.get("cpu_workers", [])
        if not workers or any(w["end_ns"] <= w["begin_ns"] for w in workers):
            raise ValueError("missing CPU worker intervals")
        overlap = 0
        for job in record["npu_job_windows"]:
            covered = job["begin_ns"]
            for worker in sorted(workers, key=lambda w: w["begin_ns"]):
                start = max(worker["begin_ns"], covered)
                end = min(worker["end_ns"], job["end_ns"])
                if end > start:
                    overlap += end - start
                    covered = end
        result["submission_windows_overlap"] = {
            "nanoseconds": overlap,
            "scope": "CPU worker union versus per-job submit/wait windows; not DMA bursts",
        }
        if overlap == 0:
            raise ValueError("concurrent jobs did not overlap CPU work")
    result["payload_phase_overlap"] = payload_phase_overlap(result, record)
    result.update(
        {
            "guest": record,
            "rates": rates,
            "measurement": "prepared buffers; submit/cache sync/IRQ/join included",
            "not_proven": ["silicon performance", "overlap of DRAM bursts"],
        }
    )
    return result
