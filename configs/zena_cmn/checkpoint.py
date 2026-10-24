# SPDX-License-Identifier: BSD-3-Clause
"""Fail-closed checkpoints of diskless Atomic boot, before CPU switching."""

import json
from pathlib import Path

METADATA = "apollo-checkpoint.json"


def validate_options(save, restore, atomic, disk, npu):
    if not (save or restore):
        return
    if not atomic or disk or npu:
        raise ValueError(
            "checkpoints require diskless Atomic boot with NPU disabled"
        )
    for value in (save, restore):
        if value and not Path(value).is_absolute():
            raise ValueError("checkpoint paths must be absolute")
    if save and Path(save).exists():
        raise ValueError("checkpoint destination must not exist")
    if restore:
        read_metadata(restore)


def read_metadata(directory):
    path = Path(directory)
    data = json.loads((path / METADATA).read_text())
    if (
        data.get("schema") != 1
        or data.get("status") != "COMPLETE"
        or data.get("phase") != "boot"
        or data.get("memory_mode") != "atomic_noncaching"
        or not (path / "m5.cpt").is_file()
    ):
        raise ValueError("incomplete or unsupported AP checkpoint")
    return data


def save_checkpoint(directory, control, contract):
    import m5

    if not directory or not control.atomic or control.phase != "boot":
        raise RuntimeError(
            "checkpoint requested outside authorized Atomic boot"
        )
    path = Path(directory)
    platform = getattr(getattr(control, "system", None), "platform", None)
    scmi = getattr(platform, "scmi", None)
    if scmi is not None:
        # The guest must stop thermal/governor requests before its checkpoint
        # marker. Running the guest here could consume readfile before restore.
        if not scmi.idle():
            raise RuntimeError("SCMI channels busy before checkpoint drain")
        m5.drain()
        # DMA draining alone does not cover SCMI replies or MHU doorbells.
        # Keep the native serialize guards as the final authority as well.
        if not scmi.idle():
            raise RuntimeError("SCMI channels busy after checkpoint drain")
    # Reserve exclusively: never overwrite a previous or partial checkpoint.
    path.mkdir(parents=True, exist_ok=False)
    m5.checkpoint(str(path))
    data = {
        "schema": 1,
        "status": "COMPLETE",
        "phase": "boot",
        "memory_mode": "atomic_noncaching",
        "tick": int(m5.curTick()),
        "contract": contract,
    }
    temporary = path / (METADATA + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path / METADATA)
    return data
