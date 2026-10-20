# SPDX-License-Identifier: BSD-3-Clause
"""Zena AP Linux on an uncalibrated, separate Ruby CHI CMN proxy."""

import argparse
import json
import sys
from pathlib import Path

import m5
from m5.objects import (
    AddrRange,
    ArmFsLinux,
    ArmSemihosting,
    ArmSPI,
    ArmSystem,
    Armv8,
    CowDiskImage,
    IOXBar,
    RawDiskImage,
    Root,
    RubyPortProxy,
    SrcClockDomain,
    TimingVirtIOBlock,
    VoltageDomain,
    ZenaCSS,
)

CONFIGS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONFIGS))
sys.path.insert(0, str(CONFIGS / "example" / "arm"))
from zena_cmn.cpu_profile import (
    cache_id_registers,
    create_cpu,
)
from zena_cmn.device_tree import add_cache_topology
from zena_cmn.npu_v01 import (
    add_npu_dtb,
    create_npu,
)
from zena_cmn.parameters import load_parameters
from zena_cmn.roi_control import RoiControl
from zena_cmn.ruby_system_builder import build_ruby
from zena_css import (
    file_arg,
    file_digest,
    validate_images,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--roofline", action="store_true")
    parser.add_argument("--roofline-trace", action="store_true")
    parser.add_argument("--cpus", type=int, choices=(4, 8, 16))
    parser.add_argument("--channels", type=int, choices=(4, 8, 16))
    parser.add_argument("--kernel", required=True, type=file_arg)
    parser.add_argument("--bootloader", required=True, type=file_arg)
    parser.add_argument("--initrd", required=True, type=file_arg)
    parser.add_argument("--disk-image", type=file_arg)
    parser.add_argument(
        "--boot-mode", choices=("atomic", "timing"), default="atomic"
    )
    parser.add_argument("--terminal-port", type=int, default=3456)
    parser.add_argument("--max-ticks", type=int, default=m5.MaxTick)
    parser.add_argument(
        "--dmt", action=argparse.BooleanOptionalAction, default=None
    )
    parser.add_argument(
        "--dct", action=argparse.BooleanOptionalAction, default=None
    )
    parser.add_argument(
        "--timing-dvm", action=argparse.BooleanOptionalAction, default=None
    )
    parser.add_argument(
        "--command-line",
        default="console=ttyAMA0,115200 earlycon=pl011,0x1a400000 rdinit=/init lpj=1000000",
    )
    args = parser.parse_args()
    if args.max_ticks < 1:
        parser.error("max-ticks must be positive")
    base_profile = load_parameters(args.profile)
    overrides = {}
    if args.cpus is not None:
        groups = args.cpus // 4
        overrides.update(
            {
                "cpu.cores": args.cpus,
                "cpu.clusters": groups,
                "cache.dsu_l3.instances": groups,
                "cmn.mesh.cluster_local_routers": groups,
                "cmn.mesh.cluster_attach": base_profile["cmn"]["mesh"][
                    "cluster_attach"
                ][:groups],
                "full_system.required_linux_cpu_clusters": groups,
            }
        )
    for name, value in (
        ("memory.channels", args.channels),
        ("cmn.dmt", args.dmt),
        ("cmn.dct", args.dct),
        ("ruby.timing_dvm", args.timing_dvm),
    ):
        if value is not None:
            overrides[name] = value
    params = load_parameters(args.profile, overrides=overrides)
    if params["cpu"]["cores_per_cluster"] != 4:
        parser.error(
            "Zena firmware and MPIDR contract require four CPUs per cluster"
        )
    args.cpus = params["cpu"]["cores"]
    args.warm_reset = False
    validate_images(args)
    atomic = args.boot_mode == "atomic"
    system = ArmSystem(
        mem_mode="atomic_noncaching" if atomic else "timing",
        mem_ranges=[AddrRange(ZenaCSS._RAM_BASE, size=ZenaCSS._RAM_SIZE)],
        cpu_affinities=[ZenaCSS.affinity(i) for i in range(args.cpus)],
        release=Armv8(),
        highest_el_is_64=True,
        phys_addr_range_64=48,
        auto_reset_addr=False,
        reset_addr=ZenaCSS._FIRMWARE_BASE,
        semihosting=ArmSemihosting(),
        workload=ArmFsLinux(
            object_file=args.kernel,
            boot_loader=[args.bootloader],
            load_addr_mask=0x7FFFFFFF,
            load_addr_offset=ZenaCSS._RAM_BASE,
            dtb_addr=ZenaCSS._DTB_BASE,
            initrd_filename=args.initrd,
            initrd_addr=ZenaCSS._INITRD_BASE,
            cpu_release_addr=ZenaCSS._FIRMWARE_BASE + 0x10000,
            command_line=args.command_line,
            panic_on_panic=True,
            panic_on_oops=True,
        ),
    )
    system.voltage_domain = VoltageDomain()
    system.clk_domain = SrcClockDomain(
        clock=params["ruby"]["clock"], voltage_domain=system.voltage_domain
    )
    system.cpu_clk_domain = SrcClockDomain(
        clock=params["cpu"]["clock"], voltage_domain=system.voltage_domain
    )
    system.cache_line_size = params["cache"]["line_bytes"]
    system.exit_on_work_items = True
    system.iobus = IOXBar()
    system.platform = ZenaCSS()
    system.platform.gic.cpu_max = args.cpus
    system.platform.terminal.port = args.terminal_port
    system.platform.attach(system.iobus)
    system.cpu = [
        create_cpu(params, i, atomic=atomic) for i in range(args.cpus)
    ]
    for cpu in system.cpu:
        cpu.clk_domain = system.cpu_clk_domain
        cpu.createThreads()
        if params["npu"]["enabled"]:
            clidr, ccsidr = cache_id_registers(params)
            for isa in cpu.isa:
                isa.cache_id_registers_enabled = True
                isa.clidr = clidr
                isa.ccsidr = ccsidr
        cpu.createInterruptController()
    if atomic:
        system.roi_cpu = [
            create_cpu(params, i, switched_out=True) for i in range(args.cpus)
        ]
        for old, new in zip(system.cpu, system.roi_cpu):
            new.clk_domain = old.clk_domain
            new.workload = old.workload
            new.isa = old.isa
            new.createThreads()
    devices, dma_ports = [], []
    if args.disk_image:
        system.platform.block = TimingVirtIOBlock(
            pio_addr=0x30020000,
            pio_size=0x200,
            interrupt=ArmSPI(num=289),
            image=CowDiskImage(
                child=RawDiskImage(image_file=args.disk_image, read_only=True),
                read_only=False,
            ),
            queue_size=128,
            request_latency="10us",
            bandwidth="100MiB/s",
            flush_latency="100us",
        )
        system.platform.block.pio = system.iobus.mem_side_ports
        dma_ports.append(system.platform.block.dma)
        devices.append((0x30020000, 289))
    smmu = create_npu(system, params) if params["npu"]["enabled"] else None
    if smmu is not None:
        dma_ports.append(smmu.walker)
    ruby = build_ruby(system, params, system.cpu, dma_ports=dma_ports)
    if smmu is not None:
        smmu.request = system.memory_bus.cpu_side_ports
    system.sys_port_proxy = RubyPortProxy(ruby_system=ruby)
    system.sys_port_proxy.pio_request_port = system.iobus.cpu_side_ports
    system.system_port = system.sys_port_proxy.in_ports
    system.iobus.mem_side_ports = ruby._io_port.in_ports
    for cpu, sequencer in zip(system.cpu, ruby._cpu_ports):
        sequencer.connectCpuPorts(cpu)
        sequencer.connectIOPorts(system.iobus)
    if params["ruby"]["timing_dvm"] and atomic:
        for cpu in system.roi_cpu:
            for decoder in cpu.decoder:
                decoder.dvm_enabled = True
    outdir = Path(m5.options.outdir)
    dtb = str(outdir / "system.dtb")
    system.platform.write_dtb(dtb, args.cpus, devices)
    add_cache_topology(dtb, params, ZenaCSS.affinity)
    if smmu is not None:
        add_npu_dtb(dtb)
    system.workload.dtb_filename = dtb
    (outdir / "resolved-profile.json").write_text(
        json.dumps(params, indent=2) + "\n"
    )
    manifest = {
        "model": "UNCALIBRATED_PROXY",
        "boot_cpu": "ArmAtomicSimpleCPU" if atomic else "ArmO3CPU",
        "boot_memory_mode": "atomic_noncaching" if atomic else "timing",
        "roi_cpu": "ArmO3CPU",
        "gem5_version": m5.core.gem5Version,
        "source_sha256": {
            str(path.relative_to(CONFIGS.parent)): file_digest(str(path))
            for path in sorted(Path(__file__).parent.glob("*.py"))
        },
        "assets": {
            name: {
                "path": getattr(args, name),
                "sha256": file_digest(getattr(args, name)),
            }
            for name in ("kernel", "bootloader", "initrd", "disk_image")
        },
        "memory_fingerprint": ruby._memory_backend["fingerprint"],
        "memory_part_provenance": ruby._memory_backend["part_provenance"],
        "future_npu": (
            "FUNC_OPS_TIMING_MODEL_UNCALIBRATED"
            if smmu is not None
            else "NOT_PRESENT"
        ),
        "checkpoint_restore": "UNSUPPORTED",
    }
    (outdir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    root = Root(full_system=True, system=system)
    m5.instantiate()
    control = RoiControl(
        system,
        outdir,
        atomic,
        multi_roi=args.roofline,
        trace_roi=args.roofline_trace,
    )
    while m5.curTick() < args.max_ticks:
        event = m5.simulate(args.max_ticks - m5.curTick())
        if control.handle(event.getCause(), event.getCode()):
            continue
        print(
            f"CMN_EXIT cause={event.getCause()} code={event.getCode()} tick={m5.curTick()}"
        )
        completed = (
            event.getCode() == 0
            and event.getCause()
            in (
                "m5_exit instruction encountered",
                "exiting with last active thread context",
            )
            and (
                not args.roofline
                or event.getCause() == "m5_exit instruction encountered"
            )
            and control.can_exit()
        )
        (outdir / "exit.json").write_text(
            json.dumps(
                {
                    "status": "COMPLETED" if completed else "INCOMPLETE",
                    "cause": event.getCause(),
                    "code": event.getCode(),
                    "tick": int(m5.curTick()),
                    "phase": control.phase,
                },
                indent=2,
            )
            + "\n"
        )
        if not completed:
            raise SystemExit(event.getCode() or 2)
        break


if __name__ == "__m5_main__":
    main()
