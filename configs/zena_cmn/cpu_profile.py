# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Generic Arm CPU queue baseline; no A720AE identity spoofing."""


def create_cpu(params, cpu_id, switched_out=False, atomic=False):
    from m5.objects import (
        ArmAtomicSimpleCPU,
        ArmO3CPU,
    )

    profile = params["cpu"]
    execution = profile.get("execution_profile", "generic")
    if execution not in ("generic", "a720ae_documented"):
        raise ValueError(f"unknown CPU execution profile: {execution}")
    if not 0 <= cpu_id < profile["cores"]:
        raise ValueError("CPU index outside the configured topology")
    cpu_type = ArmAtomicSimpleCPU if atomic else ArmO3CPU
    kwargs = dict(cpu_id=cpu_id, switched_out=switched_out)
    if not atomic:
        kwargs.update(
            numROBEntries=profile["rob_entries"],
            LQEntries=profile["lq_entries"],
            SQEntries=profile["sq_entries"],
        )
    cpu = cpu_type(**kwargs)
    if not atomic:
        writeback_slots = (int(cpu.forwardComSize) + 1) * int(cpu.wbWidth)
        # Merged Ruby load responses can exhaust IEW's native future slots.
        if int(cpu.LQEntries) > writeback_slots:
            raise ValueError(
                f"LQEntries={cpu.LQEntries} exceeds native IEW writeback "
                f"window={writeback_slots}; merged Ruby load responses can "
                "overflow it. Use the native LQ32 baseline."
            )
        if "instQueues" in cpu._params:
            if len(cpu.instQueues) != 1:
                raise ValueError(
                    "queue baseline requires one native instruction queue"
                )
            cpu.instQueues[0].numEntries = profile["iq_entries"]
        else:
            cpu.numIQEntries = profile["iq_entries"]
        if execution == "a720ae_documented":
            from .a720ae_fu import create_execution_pool

            if "instQueues" in cpu._params:
                cpu.instQueues[0].fuPool = create_execution_pool()
            else:
                cpu.fuPool = create_execution_pool()
    return cpu


def cache_id_registers(params):
    """Describe CPU-visible caches so Linux can perform noncoherent DMA sync."""
    cache = params["cache"]
    line = cache["line_bytes"]
    if line < 16 or line & (line - 1):
        raise ValueError("invalid architectural cache line size")
    ccsidr = [0] * 14
    for index, level in ((0, "l1d"), (1, "l1i"), (2, "l2"), (4, "dsu_l3")):
        spec = cache[level]
        size = (
            spec["size_mib_per_cluster"] * 1024**2
            if level == "dsu_l3"
            else spec["size_kib_per_core"] * 1024
        )
        ways = spec["assoc"]
        sets, remainder = divmod(size, ways * line)
        if remainder or not 1 <= sets <= 32768 or not 1 <= ways <= 1024:
            raise ValueError(
                "cache geometry cannot be represented without CCIDX"
            )
        policy = 0x20000000 if level == "l1i" else 0x70000000
        ccsidr[index] = (
            policy
            | ((sets - 1) << 13)
            | ((ways - 1) << 3)
            | (line.bit_length() - 5)
        )
    # L1 split; L2/L3 unified. Unification at L2; coherence through L3.
    clidr = (2 << 27) | (3 << 24) | (2 << 21) | 3 | (4 << 3) | (4 << 6)
    return clidr, ccsidr
