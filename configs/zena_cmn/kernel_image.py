# SPDX-License-Identifier: BSD-3-Clause
"""Wrap a Linux arm64 Image for gem5's ELF loader without changing its bytes."""

import struct
from pathlib import Path


def prepare_kernel(source, output, ram_base, limit):
    source = Path(source)
    with source.open("rb") as image:
        header = image.read(64)
        if header.startswith(b"\x7fELF"):
            return str(source)
        data = header + image.read()
    if len(data) < 64 or data[56:60] != b"ARM\x64":
        raise ValueError("kernel must be ELF64 or an uncompressed arm64 Image")
    offset, size, flags = struct.unpack_from("<QQQ", data, 8)
    if flags & 1:
        raise ValueError("big-endian arm64 Image is unsupported")
    if size == 0:
        raise ValueError(
            "legacy Image with unspecified image_size unsupported"
        )
    if ram_base % (2 * 1024 * 1024):
        raise ValueError("arm64 Image RAM base must be 2 MiB aligned")
    address = ram_base + offset
    memory_size = max(size, len(data))
    if address + memory_size > limit:
        raise ValueError("arm64 Image overlaps initrd/boot data")
    # No symbols are invented: panic/oops hooks require a real ELF vmlinux.
    ident = b"\x7fELF\x02\x01\x01" + bytes(9)
    header = struct.pack(
        "<16sHHIQQQIHHHHHH",
        ident,
        2,
        183,
        1,
        address,
        64,
        0,
        0,
        64,
        56,
        1,
        0,
        0,
        0,
    )
    segment = struct.pack(
        "<IIQQQQQQ",
        1,
        7,
        4096,
        address,
        address,
        len(data),
        memory_size,
        1,
    )
    output = Path(output)
    if output.resolve() == source.resolve():
        raise ValueError("kernel wrapper must not overwrite its source")
    output.write_bytes(header + segment + bytes(4096 - 120) + data)
    return str(output)
