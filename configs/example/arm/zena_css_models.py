# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause

"""Explicit experimental cache, fabric and memory settings for Zena AP.

These are generic sensitivity-study models, not calibrated Zena hardware.
DRAM timing presets use synthetic device capacities to retain exactly 2 GiB.
"""

import math

from m5.objects import (
    AddrRange,
    DDR3_1600_8x8,
    DDR4_2400_8x8,
    MemCtrl,
    SimpleMemory,
)
from m5.util.convert import (
    toFrequency,
    toLatency,
    toMemoryBandwidth,
    toMemorySize,
    toVoltage,
)


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise ValueError("must be a positive integer")
    return number


def add_options(parser):
    parser.add_argument(
        "--fabric-clock",
        help="Independent cache/XBar clock; default follows --cpu-clock",
    )
    parser.add_argument("--cpu-voltage", default="1.0V")
    parser.add_argument("--fabric-voltage", default="1.0V")
    parser.add_argument("--l1i-size", default="64KiB")
    parser.add_argument("--l1d-size", default="64KiB")
    parser.add_argument("--l1-assoc", type=positive_int, default=4)
    parser.add_argument("--l1-latency", type=positive_int, default=2)
    parser.add_argument("--l1-mshrs", type=positive_int, default=8)
    parser.add_argument("--l2-size", default="4MiB")
    parser.add_argument("--l2-assoc", type=positive_int, default=16)
    parser.add_argument("--l2-latency", type=positive_int, default=20)
    parser.add_argument("--l2-mshrs", type=positive_int, default=32)
    parser.add_argument(
        "--snoop-filter-size",
        default="auto",
        help="Capacity per coherent XBar; auto covers cache+inflight entries",
    )
    parser.add_argument(
        "--memory-model",
        choices=("simple", "ddr3-1600", "ddr4-2400"),
        default="simple",
    )
    parser.add_argument("--memory-latency", default="30ns")
    parser.add_argument("--memory-bandwidth", default="12.8GiB/s")
    parser.add_argument(
        "--memory-channels", type=int, choices=(1, 2, 4), default=1
    )
    parser.add_argument(
        "--dram-page-policy",
        choices=("open", "open_adaptive", "close", "close_adaptive"),
        default="open_adaptive",
    )
    parser.add_argument(
        "--dram-scheduler", choices=("fcfs", "frfcfs"), default="frfcfs"
    )


def add_block_options(parser):
    parser.add_argument("--block-request-latency", default="10us")
    parser.add_argument("--block-bandwidth", default="100MiB/s")
    parser.add_argument("--block-flush-latency", default="100us")


def settings(args):
    """Validate and return the complete, JSON-serializable model contract."""
    fabric_clock = args.fabric_clock or args.cpu_clock
    for latency in (args.block_request_latency, args.block_flush_latency):
        if toLatency(latency) < 0:
            raise ValueError("Block latency must be nonnegative")
    if toMemoryBandwidth(args.block_bandwidth) <= 0:
        raise ValueError("Block bandwidth must be positive")
    if args.io_profile != "timing-block" and (
        args.block_request_latency != "10us"
        or args.block_bandwidth != "100MiB/s"
        or args.block_flush_latency != "100us"
    ):
        raise ValueError(
            "Timing block options require --io-profile=timing-block"
        )
    for clock in (args.cpu_clock, fabric_clock):
        if toFrequency(clock) <= 0:
            raise ValueError("Clock frequency must be positive")
    for voltage in (args.cpu_voltage, args.fabric_voltage):
        if toVoltage(voltage) <= 0:
            raise ValueError("Voltage must be positive")
    if toLatency(args.memory_latency) < 0:
        raise ValueError("Memory latency must be nonnegative")
    if toMemoryBandwidth(args.memory_bandwidth) <= 0:
        raise ValueError("Memory bandwidth must be positive")
    sizes = {
        "l1i_size": int(toMemorySize(args.l1i_size)),
        "l1d_size": int(toMemorySize(args.l1d_size)),
        "l2_size": int(toMemorySize(args.l2_size)),
    }
    for name, size in sizes.items():
        assoc = args.l2_assoc if name == "l2_size" else args.l1_assoc
        sets, remainder = divmod(size, 64 * assoc)
        if remainder or sets < 1 or sets & (sets - 1):
            raise ValueError(f"{name} must provide a power-of-two set count")
    # Include all L1 and shared cache lines: Classic mostly-inclusive caches
    # are not strictly inclusive. Add conservative transient-entry headroom.
    cache_bytes = args.cpus * (sizes["l1i_size"] + sizes["l1d_size"])
    cache_bytes += args.cpus // 4 * sizes["l2_size"]
    if args.io_profile == "timing-block":
        cache_bytes += 1024
    transient_bytes = 64 * (
        args.cpus * (2 * args.l1_mshrs + 32)
        + args.cpus // 4 * (args.l2_mshrs + 32)
    )
    minimum = cache_bytes + max(1024 * 1024, transient_bytes)
    automatic = 1 << (minimum - 1).bit_length()
    snoop_bytes = (
        automatic
        if args.snoop_filter_size == "auto"
        else int(toMemorySize(args.snoop_filter_size))
    )
    if snoop_bytes < minimum or snoop_bytes % 64:
        raise ValueError(
            f"Snoop filter requires >= {minimum} bytes, aligned to 64 bytes"
        )
    if args.memory_model == "simple" and args.memory_channels != 1:
        raise ValueError("SimpleMemory supports exactly one channel")
    if args.memory_model != "simple" and (
        args.memory_latency != "30ns" or args.memory_bandwidth != "12.8GiB/s"
    ):
        raise ValueError("SimpleMemory latency/bandwidth do not apply to DRAM")
    if args.memory_model == "simple" and (
        args.dram_page_policy != "open_adaptive"
        or args.dram_scheduler != "frfcfs"
    ):
        raise ValueError("DRAM page/scheduler options require a DRAM model")
    return {
        "qualification": "GENERIC_MODEL_NOT_CALIBRATED",
        "cpu_clock": args.cpu_clock,
        "fabric_clock": fabric_clock,
        "cpu_voltage": args.cpu_voltage,
        "fabric_voltage": args.fabric_voltage,
        "cache_line_size": 64,
        "caches_enabled": args.cpu_mode != "atomic" or bool(args.roi_cpu_mode),
        **sizes,
        "l1_assoc": args.l1_assoc,
        "l1_latency_cycles": args.l1_latency,
        "l1_mshrs": args.l1_mshrs,
        "l2_assoc": args.l2_assoc,
        "l2_latency_cycles": args.l2_latency,
        "l2_mshrs": args.l2_mshrs,
        "snoop_filter_bytes": snoop_bytes,
        "snoop_filter_minimum_bytes": minimum,
        "timing_block": (
            {
                "io_cache_size_bytes": 1024,
                "io_cache_assoc": 8,
                "io_cache_latency_cycles": 50,
                "io_cache_mshrs": 20,
                "io_cache_targets_per_mshr": 12,
                "dma_path": "platform.block.dma -> io_cache -> membus",
                "queue_size": 128,
                "service_depth": 1,
                "request_latency": args.block_request_latency,
                "bandwidth": args.block_bandwidth,
                "flush_latency": args.block_flush_latency,
                "qualification": "SYNTHETIC_DEVICE_NOT_CALIBRATED",
            }
            if args.io_profile == "timing-block"
            else None
        ),
        "memory_model": args.memory_model,
        "memory_capacity_bytes": 2 * 1024**3,
        "memory_latency": args.memory_latency,
        "memory_bandwidth": args.memory_bandwidth,
        "memory_channels": args.memory_channels,
        "dram_page_policy": args.dram_page_policy,
        "dram_scheduler": args.dram_scheduler,
        "dram_ranks_per_channel": 1 if args.memory_model != "simple" else None,
        "dram_device_size_bytes": (
            2 * 1024**3 // args.memory_channels // 8
            if args.memory_model != "simple"
            else None
        ),
        "dram_capacity_note": (
            "Synthetic density with preset timing; not a physical DIMM"
            if args.memory_model != "simple"
            else None
        ),
    }


def configure_memory(system, model):
    if model["memory_model"] == "simple":
        system.memory = SimpleMemory(
            range=system.mem_ranges[0],
            latency=model["memory_latency"],
            bandwidth=model["memory_bandwidth"],
        )
        system.memory.port = system.membus.mem_side_ports
        return
    interface = {
        "ddr3-1600": DDR3_1600_8x8,
        "ddr4-2400": DDR4_2400_8x8,
    }[model["memory_model"]]
    channels = model["memory_channels"]
    bits = int(math.log2(channels))
    controllers = []
    for channel in range(channels):
        region = system.mem_ranges[0]
        if bits:
            region = AddrRange(
                region.start,
                size=region.size(),
                intlvHighBit=6 + bits - 1,
                xorHighBit=0,
                intlvBits=bits,
                intlvMatch=channel,
            )
        dram = interface(
            range=region,
            addr_mapping="RoRaBaCoCh",
            ranks_per_channel=1,
            device_size=f'{model["dram_device_size_bytes"]}B',
            page_policy=model["dram_page_policy"],
        )
        controller = MemCtrl(
            dram=dram, mem_sched_policy=model["dram_scheduler"]
        )
        controllers.append(controller)
        controller.port = system.membus.mem_side_ports
    system.mem_ctrls = controllers
