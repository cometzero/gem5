# SPDX-License-Identifier: BSD-3-Clause
"""Checkpoint policy and completion publication without a native simulator."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "checkpoint",
    Path(__file__).resolve().parents[2] / "configs/zena_cmn/checkpoint.py",
)
checkpoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkpoint)


class CheckpointTests(unittest.TestCase):
    def test_reject_unsupported_modes(self):
        for atomic, disk, npu in [
            (False, None, False),
            (True, "disk", False),
            (True, None, True),
        ]:
            with self.assertRaises(ValueError):
                checkpoint.validate_options(
                    "/new/cpt", None, atomic, disk, npu
                )
        with self.assertRaises(ValueError):
            checkpoint.validate_options("relative", None, True, None, False)

    def test_reject_after_cpu_switch(self):
        with self.assertRaises(RuntimeError), patch.dict(
            sys.modules, {"m5": SimpleNamespace()}
        ):
            checkpoint.save_checkpoint(
                "/unused", SimpleNamespace(atomic=True, phase="warmup"), {}
            )

    def test_partial_checkpoint_never_published(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "checkpoint"

            def fail(directory):
                (Path(directory) / "m5.cpt").write_text("partial")
                raise RuntimeError("serialization failed")

            with patch.dict(
                sys.modules, {"m5": SimpleNamespace(checkpoint=fail)}
            ):
                with self.assertRaises(RuntimeError):
                    checkpoint.save_checkpoint(
                        path, SimpleNamespace(atomic=True, phase="boot"), {}
                    )
            self.assertFalse((path / checkpoint.METADATA).exists())
            with self.assertRaises(OSError):
                checkpoint.read_metadata(path)
            with self.assertRaises(ValueError):
                checkpoint.validate_options(str(path), None, True, None, False)

    def test_complete_roundtrip(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "checkpoint"

            def save(directory):
                (Path(directory) / "m5.cpt").write_text("serialized")

            with patch.dict(
                sys.modules,
                {"m5": SimpleNamespace(checkpoint=save, curTick=lambda: 123)},
            ):
                checkpoint.save_checkpoint(
                    path,
                    SimpleNamespace(atomic=True, phase="boot"),
                    {"kernel": "digest"},
                )
            self.assertEqual(checkpoint.read_metadata(path)["tick"], 123)
            checkpoint.validate_options(None, str(path), True, None, False)
            data = json.loads((path / checkpoint.METADATA).read_text())
            data["phase"] = "roi_begin"
            (path / checkpoint.METADATA).write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                checkpoint.read_metadata(path)


if __name__ == "__main__":
    unittest.main()
