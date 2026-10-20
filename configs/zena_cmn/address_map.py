# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Explicit 64-byte channel/Home striping, checked against native AddrRange."""

import random


def striped_ranges(base_range, count):
    from m5.objects import AddrRange

    if count not in (4, 8, 16):
        raise ValueError("striping count must be 4, 8 or 16")
    if (
        base_range.intlvBits
        or int(base_range.start) % (64 * count)
        or base_range.size() % (64 * count)
    ):
        raise ValueError(
            "RAM must be contiguous and aligned to a stripe cycle"
        )
    bits = count.bit_length() - 1
    return [
        AddrRange(
            int(base_range.start),
            size=base_range.size(),
            intlvHighBit=6 + bits - 1,
            xorHighBit=0,
            intlvBits=bits,
            intlvMatch=index,
        )
        for index in range(count)
    ]


def validate_ranges(base_range, home_ranges, channel_ranges):
    """Prove partition by masks; compare samples with C++ range membership."""
    from m5.objects import AddrRange

    start, end = int(base_range.start), int(base_range.end)
    if base_range.intlvBits or start >= end:
        raise ValueError("base RAM range must be nonempty and contiguous")
    samples = {start, start + 1, end - 1, end - 64}
    samples.update(range(start, min(start + 65536, end), 64))
    samples.update(range(start, min(start + 1024**2, end), 1024))
    rng = random.Random(8533)
    samples.update(rng.randrange(start, end) for _ in range(1024))
    for kind, ranges in (("home", home_ranges), ("channel", channel_ranges)):
        count = len(ranges)
        if count not in (4, 8, 16) or (kind == "home" and count != 8):
            raise ValueError(f"invalid {kind} count")
        bits = count.bit_length() - 1
        if start % (64 * count) or (end - start) % (64 * count):
            raise ValueError("RAM must cover whole stripe cycles")
        expected_masks = [1 << (6 + bit) for bit in range(bits)]
        if sum(region.size() for region in ranges) != base_range.size():
            raise ValueError(f"{kind} ranges have incorrect total capacity")
        for index, region in enumerate(ranges):
            if (
                int(region.start),
                int(region.end),
                region.masks,
                region.intlvMatch,
            ) != (start, end, expected_masks, index):
                raise ValueError(
                    f"{kind} range {index} is not the specified partition"
                )
        native_ranges = [region.getValue() for region in ranges]
        for address in samples:
            point = AddrRange(address, size=1).getValue()
            matches = [
                index
                for index, region in enumerate(native_ranges)
                if point.isSubset(region)
            ]
            if matches != [(address >> 6) & (count - 1)]:
                raise ValueError(
                    f"native {kind} selection mismatch at {address:#x}"
                )
    return {
        "status": "PASS",
        "sample_count": len(samples),
        "capacity_bytes": base_range.size(),
        "home_count": len(home_ranges),
        "channels": len(channel_ranges),
        "home_select": "(PA >> 6) & 7",
        "channel_select": "(PA >> 6) & (channels - 1)",
        "xor_enabled": False,
        "partition_proof": "identical bounds; complete disjoint low-bit selectors",
        "membership_check": "native AddrRange::isSubset for one-byte ranges",
    }
