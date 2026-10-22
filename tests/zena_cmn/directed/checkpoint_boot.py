# SPDX-License-Identifier: BSD-3-Clause
"""Host runner: real diskless AP/Ruby checkpoint and O3 continuation."""
import argparse
import json
import os
import struct
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--manifest", required=True)
parser.add_argument("--outdir", required=True)
args = parser.parse_args()
out = Path(args.outdir).resolve()
out.mkdir(parents=True, exist_ok=False)
repo = Path(__file__).resolve().parents[3]
manifest = json.loads(Path(args.manifest).read_text())
env = os.environ.copy()
env["PYTHONHOME"] = manifest["python_home"]
env["LD_LIBRARY_PATH"] = ":".join(manifest["library_path"])
subprocess.run(
    [
        "aarch64-linux-gnu-as",
        str(Path(__file__).with_suffix(".S")),
        "-o",
        str(out / "boot.o"),
    ],
    check=True,
)
(out / "boot.ld").write_text(
    "ENTRY(_start)\nSECTIONS { . = 0xffe00000; .text : { *(.text*) } }\n"
)
subprocess.run(
    [
        "aarch64-linux-gnu-ld",
        "-T",
        str(out / "boot.ld"),
        "-e",
        "_start",
        str(out / "boot.o"),
        "-o",
        str(out / "boot.elf"),
    ],
    check=True,
)
# Only a valid loader header; firmware probe does not execute this image.
header = bytearray(64)
struct.pack_into("<Q", header, 16, 4096)
header[56:60] = b"ARM\x64"
(out / "Image").write_bytes(header)
(out / "initrd").write_bytes(bytes(512))
(out / "save-readfile").write_text("S")
(out / "restore-readfile").write_text("R")
for mode in ("save", "restore"):
    dest = out / mode
    command = [
        manifest["executable"],
        "--outdir=" + str(dest),
        str(repo / "configs/zena_cmn/run_fs.py"),
        "--kernel=" + str(out / "Image"),
        "--bootloader=" + str(out / "boot.elf"),
        "--initrd=" + str(out / "initrd"),
        "--cpus=4",
        "--channels=4",
        "--terminal-port=57931",
        "--max-ticks=1000000000",
        "--readfile=" + str(out / (mode + "-readfile")),
        ("--checkpoint-dir=" if mode == "save" else "--restore=")
        + str(out / "checkpoint"),
    ]
    with (out / (mode + ".log")).open("w") as log:
        subprocess.run(
            command,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=180,
        )
    result = json.loads((dest / "exit.json").read_text())
    assert result["status"] == (
        "CHECKPOINT_SAVED" if mode == "save" else "COMPLETED"
    ), result
assert (
    json.loads((out / "restore/exit.json").read_text())["phase"] == "roi_end"
)
(out / "result.json").write_text(
    json.dumps(
        {
            "status": "PASS",
            "scope": "native baremetal AP/Ruby save and restore",
            "checks": [
                "register",
                "RAM",
                "new readfile",
                "O3 switch",
                "ROI completion",
            ],
            "linux_boot": "NOT_TESTED",
        },
        indent=2,
    )
    + "\n"
)
print("PASS: checkpoint RAM/register/readfile/O3 continuation")
