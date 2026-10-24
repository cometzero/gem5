# SPDX-License-Identifier: BSD-3-Clause
"""Checkpoint policy and completion publication without a native simulator."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import (
    Mock,
    patch,
)

spec = importlib.util.spec_from_file_location(
    "checkpoint",
    Path(__file__).resolve().parents[2] / "configs/zena_cmn/checkpoint.py",
)
checkpoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkpoint)


class CheckpointTests(unittest.TestCase):
    def test_scmi_idle_checked_before_and_after_drain(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "checkpoint"
            calls = []

            def idle():
                calls.append("idle")
                return True

            def save(directory):
                calls.append("checkpoint")
                (Path(directory) / "m5.cpt").write_text("serialized")

            control = SimpleNamespace(
                atomic=True,
                phase="boot",
                system=SimpleNamespace(
                    platform=SimpleNamespace(scmi=SimpleNamespace(idle=idle))
                ),
            )
            with patch.dict(
                sys.modules,
                {
                    "m5": SimpleNamespace(
                        drain=lambda: calls.append("drain"),
                        checkpoint=save,
                        curTick=lambda: 123,
                    )
                },
            ):
                checkpoint.save_checkpoint(path, control, {})
            self.assertEqual(calls, ["idle", "drain", "idle", "checkpoint"])
            self.assertEqual(checkpoint.read_metadata(path)["tick"], 123)

    def test_scmi_busy_or_drain_failure_never_published(self):
        for states, drain_error, expected in [
            ([False], None, "before checkpoint drain"),
            ([True, False], None, "after checkpoint drain"),
            ([True], RuntimeError("drain failed"), "drain failed"),
        ]:
            with self.subTest(states=states, error=drain_error):
                with tempfile.TemporaryDirectory() as root:
                    path = Path(root) / "checkpoint"
                    idle = Mock(side_effect=states)
                    drain = Mock(side_effect=drain_error)
                    save = Mock()
                    control = SimpleNamespace(
                        atomic=True,
                        phase="boot",
                        system=SimpleNamespace(
                            platform=SimpleNamespace(
                                scmi=SimpleNamespace(idle=idle)
                            )
                        ),
                    )
                    with patch.dict(
                        sys.modules,
                        {
                            "m5": SimpleNamespace(
                                drain=drain,
                                checkpoint=save,
                            )
                        },
                    ):
                        with self.assertRaisesRegex(RuntimeError, expected):
                            checkpoint.save_checkpoint(path, control, {})
                    save.assert_not_called()
                    if states == [False]:
                        drain.assert_not_called()
                    self.assertFalse(path.exists())

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
        with (
            self.assertRaises(RuntimeError),
            patch.dict(sys.modules, {"m5": SimpleNamespace()}),
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
                    SimpleNamespace(
                        atomic=True,
                        phase="boot",
                        system=SimpleNamespace(platform=SimpleNamespace()),
                    ),
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
