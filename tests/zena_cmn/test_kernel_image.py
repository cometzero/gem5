# SPDX-License-Identifier: BSD-3-Clause
import importlib.util
import struct
import tempfile
import unittest
from pathlib import Path

SOURCE = (
    Path(__file__).resolve().parents[2] / "configs/zena_cmn/kernel_image.py"
)
SPEC = importlib.util.spec_from_file_location("kernel_image", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class KernelImageTests(unittest.TestCase):
    def test_image_payload_and_memory_extent(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "Image"
            output = Path(directory) / "kernel.elf"
            data = bytearray(128)
            struct.pack_into("<QQQ", data, 8, 0x80000, 0x100000, 0)
            data[56:60] = b"ARM\x64"
            source.write_bytes(data)
            MODULE.prepare_kernel(source, output, 0x80000000, 0xC0000000)
            elf = output.read_bytes()
            self.assertEqual(elf[4096:], data)
            self.assertEqual(source.read_bytes(), data)
            self.assertEqual(struct.unpack_from("<Q", elf, 24)[0], 0x80080000)
            self.assertEqual(
                struct.unpack_from("<IIQQQQQQ", elf, 64),
                (1, 7, 4096, 0x80080000, 0x80080000, 128, 0x100000, 1),
            )
            with self.assertRaisesRegex(ValueError, "overlaps"):
                MODULE.prepare_kernel(source, output, 0x80000000, 0x80090000)
            with self.assertRaisesRegex(ValueError, "overwrite"):
                MODULE.prepare_kernel(source, source, 0x80000000, 0xC0000000)
            for offset, value, message in (
                (24, 1, "big-endian"),
                (16, 0, "unspecified"),
            ):
                invalid = bytearray(data)
                struct.pack_into("<Q", invalid, offset, value)
                source.write_bytes(invalid)
                with self.assertRaisesRegex(ValueError, message):
                    MODULE.prepare_kernel(
                        source, output, 0x80000000, 0xC0000000
                    )

    def test_elf_passthrough_and_invalid_input(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "kernel"
            output = Path(directory) / "wrapper"
            source.write_bytes(b"\x7fELF" + bytes(60))
            self.assertEqual(
                MODULE.prepare_kernel(source, output, 0x80000000, 0xC0000000),
                str(source),
            )
            self.assertFalse(output.exists())
            source.write_bytes(b"invalid")
            with self.assertRaisesRegex(ValueError, "uncompressed"):
                MODULE.prepare_kernel(source, output, 0x80000000, 0xC0000000)


if __name__ == "__main__":
    unittest.main()
