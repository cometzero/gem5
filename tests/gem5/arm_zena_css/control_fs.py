# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Stock VExpress ARM Linux control; successful boot is not a Zena PASS.

Reuse starter_fs.create(), injecting its supported SimpleSystem platform
argument at the factory boundary because create() itself has no platform
option. No device/CPU model or explicit CPU-affinity override is installed.
"""

import argparse
import functools
import json
import os
import subprocess
import sys
from pathlib import Path

import m5
from m5.objects import (
    Root,
    VExpress_GEM5_V2,
)
from m5.util.fdthelper import FdtState

GEM5_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(GEM5_ROOT / "configs/example/arm"))
import starter_fs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", required=True)
    parser.add_argument("--initrd", required=True)
    parser.add_argument("--binaries", required=True)
    parser.add_argument("--cpus", type=int, choices=(1, 4), default=4)
    parser.add_argument("--terminal-port", type=int, default=3956)
    parser.add_argument("--max-ticks", type=int, default=m5.MaxTick)
    parser.add_argument("--dtb-only", action="store_true")
    options = parser.parse_args()
    for name in ("kernel", "initrd"):
        value = str(Path(getattr(options, name)).resolve(strict=True))
        setattr(options, name, value)
    binaries = Path(options.binaries).resolve(strict=True)
    if not (binaries / "boot_v2.arm64").is_file():
        parser.error("binaries must contain the stock boot_v2.arm64")
    os.environ["M5_PATH"] = str(binaries.parent)

    # starter_fs.create() unconditionally attaches a PCI disk. Its original
    # read-only-base/COW path is preserved, although this control uses initrd.
    disk = Path(m5.options.outdir).resolve() / "unused-disk.img"
    if not disk.exists():
        with disk.open("xb") as stream:
            stream.truncate(1024 * 1024)
    args = argparse.Namespace(
        script="",
        cpu="atomic",
        mem_size="2GiB",
        mem_type="SimpleMemory",
        mem_channels=1,
        kernel=options.kernel,
        disk_image=str(disk),
        num_cores=options.cpus,
        cpu_freq="2GHz",
        tarmac_gen=False,
        tarmac_dest="file",
        dtb=None,
        initrd=options.initrd,
        root_device="/dev/ram0",
        with_pmu=False,
    )
    original = starter_fs.devices.SimpleSystem
    try:
        starter_fs.devices.SimpleSystem = functools.partial(
            original, platform=VExpress_GEM5_V2()
        )
        system = starter_fs.create(args)
    finally:
        starter_fs.devices.SimpleSystem = original
    # The common minimal Zena kernel has no CLK_VEXPRESS config-bus driver.
    # Keep the stock models but describe the UART's fixed nominal clocks.
    # This is an explicit control DT fixture adaptation, not a model change.
    clocks = []
    for section, name in (("mcc", "osc_peripheral"), ("dcc", "osc_smb")):
        oscillator = getattr(getattr(system.realview, section), name)
        phandle = FdtState().phandle(oscillator)
        node = f"/{section}/osc@{phandle:x}"
        frequency = round(1.0 / oscillator.freq.value)
        subprocess.run(
            [
                "fdtput",
                "-t",
                "s",
                system.workload.dtb_filename,
                node,
                "compatible",
                "fixed-clock",
            ],
            check=True,
        )
        subprocess.run(
            [
                "fdtput",
                "-t",
                "i",
                system.workload.dtb_filename,
                node,
                "clock-frequency",
                str(frequency),
            ],
            check=True,
        )
        clocks.append({"node": node, "frequency": frequency})
    with open(
        Path(m5.options.outdir) / "control-dt-adaptations.json", "w"
    ) as out:
        json.dump(
            {
                "reason": "minimal kernel lacks CLK_VEXPRESS",
                "uart_fixed_clocks": clocks,
            },
            out,
            indent=2,
        )
    if list(system.cpu_affinities):
        raise RuntimeError("Control must use the default ARM affinity path")
    system.terminal.port = options.terminal_port
    system.workload.load_addr_mask = 0x0FFFFFFF
    system.workload.command_line = (
        "console=ttyAMA0 earlycon=pl011,0x1c090000 "
        "rdinit=/bin/sh lpj=1000000 panic=-1"
    )
    root = Root(full_system=True, system=system)
    if options.dtb_only:
        return
    m5.instantiate()
    event = m5.simulate(options.max_ticks)
    result = {
        "platform": "VExpress_GEM5_V2",
        "scope": "ARM control only; not Zena validation",
        "cpus": options.cpus,
        "explicit_affinities": list(system.cpu_affinities),
        "cause": event.getCause(),
        "code": event.getCode(),
        "tick": m5.curTick(),
    }
    with open(Path(m5.options.outdir) / "control-exit.json", "w") as out:
        json.dump(result, out, indent=2)
    print(json.dumps(result))


if __name__ == "__m5_main__":
    main()
