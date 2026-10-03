# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause

"""Zena CSS AP Linux: 4/16 CPUs with four cores per cluster.

Use explicit SimObjects because ArmBoard assumes a RealView memory map and
boot chain. Timing/O3 use a generic Classic cache hierarchy, not A720AE timing.

With --roi-cpu-mode, atomic boot uses those same caches. Guest workbegin
0x5a535743 switches all CPUs; workbegin 0x5a524f49 ends guest-defined warm-up
and resets statistics; workend 0x5a524f49 dumps the ROI. The guest may then
shut down normally. Check roi-phases.json and exit.json for actual boundaries;
a warm-up interval alone does not certify steady-state cache occupancy.
"""

import argparse
import hashlib
import json
import os
import struct
import sys

import zena_css_models as models

import m5
from m5.objects import (
    AddrRange,
    ArmAtomicSimpleCPU,
    ArmFsLinux,
    ArmO3CPU,
    ArmSemihosting,
    ArmSPI,
    ArmSystem,
    ArmTimingSimpleCPU,
    Armv8,
    Bridge,
    Cache,
    CowDiskImage,
    EtherTapStub,
    IOXBar,
    L2XBar,
    MmioVirtIO,
    RawDiskImage,
    Root,
    SrcClockDomain,
    SystemXBar,
    VirtIOBlock,
    VirtIORng,
    VoltageDomain,
)
from m5.objects.ZenaCSS import ZenaCSS


def file_arg(path):
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise argparse.ArgumentTypeError(f"Not a file: {path}")
    return path


def firmware_symbol(image, header, symbol):
    """Read a monitor contract symbol without host binutils."""
    offset = struct.unpack_from("<Q", header, 40)[0]
    size, count = struct.unpack_from("<HH", header, 58)
    sections = []
    for index in range(count):
        image.seek(offset + index * size)
        sections.append(struct.unpack("<IIQQQQIIQQ", image.read(64)))
    for section in sections:
        if section[1] != 2:  # SHT_SYMTAB
            continue
        strings = sections[section[6]]
        image.seek(strings[4])
        names = image.read(strings[5])
        for index in range(section[5] // section[9]):
            image.seek(section[4] + index * section[9])
            name, info, other, shndx, value, length = struct.unpack(
                "<IBBHQQ", image.read(24)
            )
            if names[name:].split(b"\0", 1)[0] == symbol.encode():
                if shndx != 0xFFF1:  # SHN_ABS
                    raise ValueError(f"{symbol} must be absolute")
                return value
    raise ValueError(f"Bootloader lacks the {symbol} contract symbol")


def validate_images(args):
    """Reject non-ELF kernels and image overlaps before instantiation."""
    with open(args.kernel, "rb") as image:
        header = image.read(64)
        if header[:6] != b"\x7fELF\x02\x01":
            raise ValueError("--kernel requires a little-endian ELF64 vmlinux")
        if struct.unpack_from("<H", header, 18)[0] != 183:
            raise ValueError("--kernel must contain AArch64 code")
        offset = struct.unpack_from("<Q", header, 32)[0]
        size, count = struct.unpack_from("<HH", header, 54)
        for index in range(count):
            image.seek(offset + index * size)
            segment = image.read(size)
            if struct.unpack_from("<I", segment)[0] != 1:
                continue
            address = struct.unpack_from("<Q", segment, 24)[0]
            length = struct.unpack_from("<Q", segment, 40)[0]
            address = (address & 0x7FFFFFFF) + ZenaCSS._RAM_BASE
            if address + length > ZenaCSS._INITRD_BASE:
                raise ValueError("Kernel PT_LOAD overlaps initrd/boot data")
    if args.initrd and os.path.getsize(args.initrd) >= (
        ZenaCSS._DTB_BASE - ZenaCSS._INITRD_BASE
    ):
        raise ValueError("Initrd overlaps the DTB")
    with open(args.bootloader, "rb") as image:
        header = image.read(64)
        if (
            header[:6] != b"\x7fELF\x02\x01"
            or struct.unpack_from("<Q", header, 24)[0]
            != ZenaCSS._FIRMWARE_BASE
        ):
            raise ValueError("Bootloader must be Zena ELF at 0xffe00000")
        if firmware_symbol(image, header, "zena_cpu_count") != args.cpus:
            raise ValueError("Bootloader CPU count does not match --cpus")
        if firmware_symbol(image, header, "zena_warm_reset") != int(
            args.warm_reset
        ):
            raise ValueError(
                "Bootloader reset contract mismatches --warm-reset"
            )
        offset = struct.unpack_from("<Q", header, 32)[0]
        size, count = struct.unpack_from("<HH", header, 54)
        for index in range(count):
            image.seek(offset + index * size)
            segment = image.read(size)
            if struct.unpack_from("<I", segment)[0] != 1:
                continue
            address = struct.unpack_from("<Q", segment, 24)[0]
            length = struct.unpack_from("<Q", segment, 40)[0]
            if address < ZenaCSS._FIRMWARE_BASE or address + length > (
                ZenaCSS._FIRMWARE_BASE + ZenaCSS._FIRMWARE_SIZE
            ):
                raise ValueError("Bootloader PT_LOAD outside reserved RAM")


def create(args):
    validate_images(args)
    model = models.settings(args)
    if args.warm_reset and (
        args.cpu_mode != "atomic"
        or args.io_profile not in ("minimal", "legacy-io")
    ):
        raise ValueError("Warm reset requires atomic and minimal/legacy-io")
    workload_class = ArmFsLinux
    if args.warm_reset:
        from m5.objects import ZenaFsLinux

        workload_class = ZenaFsLinux
    cpu_classes = {
        "atomic": ArmAtomicSimpleCPU,
        "timing": ArmTimingSimpleCPU,
        "o3": ArmO3CPU,
    }
    cpu_class = cpu_classes[args.cpu_mode]
    want_caches = args.cpu_mode != "atomic" or bool(args.roi_cpu_mode)
    system = ArmSystem(
        mem_mode=cpu_class.memory_mode(),
        mem_ranges=[AddrRange(ZenaCSS._RAM_BASE, size=ZenaCSS._RAM_SIZE)],
        cpu_affinities=[ZenaCSS.affinity(i) for i in range(args.cpus)],
        release=Armv8(),
        highest_el_is_64=True,
        phys_addr_range_64=48,
        auto_reset_addr=False,
        reset_addr=ZenaCSS._FIRMWARE_BASE,
        semihosting=ArmSemihosting(),
        workload=workload_class(
            object_file=args.kernel,
            boot_loader=[args.bootloader],
            load_addr_mask=0x7FFFFFFF,
            load_addr_offset=ZenaCSS._RAM_BASE,
            dtb_addr=ZenaCSS._DTB_BASE,
            initrd_filename=args.initrd or "",
            initrd_addr=ZenaCSS._INITRD_BASE,
            cpu_release_addr=ZenaCSS._FIRMWARE_BASE + 0x10000,
            command_line=args.command_line,
            panic_on_panic=True,
            panic_on_oops=True,
        ),
    )
    system.voltage_domain = VoltageDomain(voltage=args.fabric_voltage)
    system.clk_domain = SrcClockDomain(
        clock=model["fabric_clock"], voltage_domain=system.voltage_domain
    )
    system.cpu_voltage_domain = VoltageDomain(voltage=args.cpu_voltage)
    system.cpu_clk_domain = SrcClockDomain(
        clock=args.cpu_clock, voltage_domain=system.cpu_voltage_domain
    )
    system.cache_line_size = 64
    system.membus = SystemXBar()
    system.membus.snoop_filter.max_capacity = f'{model["snoop_filter_bytes"]}B'
    system.iobus = IOXBar()
    system.iobridge = Bridge(
        delay="50ns", ranges=[AddrRange(0, size=ZenaCSS._RAM_BASE)]
    )
    system.iobridge.cpu_side_port = system.membus.mem_side_ports
    system.iobridge.mem_side_port = system.iobus.cpu_side_ports
    system.system_port = system.membus.cpu_side_ports
    models.configure_memory(system, model)
    system.platform = ZenaCSS()
    system.platform.gic.cpu_max = args.cpus
    system.platform.terminal.port = args.terminal_port
    system.platform.attach(system.iobus)
    if args.warm_reset:
        system.exit_on_work_items = True
        system.workload.enable_warm_reset = True
    system.cpu = [
        cpu_class(cpu_id=i, socket_id=i // 4, clk_domain=system.cpu_clk_domain)
        for i in range(args.cpus)
    ]
    if want_caches:
        # Four CPUs share each generic last-level cache. These are experimental
        # Classic cache parameters, not the Zena CMN/DSU implementation.
        system.cluster_bus = [L2XBar() for _ in range(args.cpus // 4)]
        system.cluster_cache = [
            Cache(
                size=args.l2_size,
                assoc=args.l2_assoc,
                tag_latency=args.l2_latency,
                data_latency=args.l2_latency,
                response_latency=args.l2_latency,
                mshrs=args.l2_mshrs,
                tgts_per_mshr=16,
            )
            for _ in range(args.cpus // 4)
        ]
        for bus, cache in zip(system.cluster_bus, system.cluster_cache):
            bus.snoop_filter.max_capacity = f'{model["snoop_filter_bytes"]}B'
            cache.cpu_side = bus.mem_side_ports
            cache.mem_side = system.membus.cpu_side_ports
    for i, cpu in enumerate(system.cpu):
        cpu.createThreads()
        cpu.createInterruptController()
        if not want_caches:
            cpu.connectBus(system.membus)
        else:
            icache = Cache(
                size=args.l1i_size,
                assoc=args.l1_assoc,
                tag_latency=args.l1_latency,
                data_latency=args.l1_latency,
                response_latency=args.l1_latency,
                mshrs=args.l1_mshrs,
                clk_domain=system.clk_domain,
                tgts_per_mshr=8,
                is_read_only=True,
            )
            dcache = Cache(
                size=args.l1d_size,
                assoc=args.l1_assoc,
                tag_latency=args.l1_latency,
                data_latency=args.l1_latency,
                response_latency=args.l1_latency,
                mshrs=args.l1_mshrs,
                clk_domain=system.clk_domain,
                tgts_per_mshr=8,
            )
            cpu.addPrivateSplitL1Caches(icache, dcache)
            cpu.connectAllPorts(
                system.cluster_bus[i // 4].cpu_side_ports,
                system.membus.cpu_side_ports,
                system.membus.mem_side_ports,
            )

    if args.roi_cpu_mode:
        system.exit_on_work_items = True
        target_class = cpu_classes[args.roi_cpu_mode]
        # takeOverFrom transfers the connected ports, MMU state, interrupts,
        # and registered thread contexts. Do not attach duplicate ports/IRQs.
        system.roi_cpu = [
            target_class(cpu_id=i, socket_id=i // 4, switched_out=True)
            for i in range(args.cpus)
        ]
        for old_cpu, new_cpu in zip(system.cpu, system.roi_cpu):
            new_cpu.clk_domain = old_cpu.clk_domain
            new_cpu.workload = old_cpu.workload
            new_cpu.isa = old_cpu.isa
            new_cpu.createThreads()

    devices = []
    if args.io_profile == "timing-block":
        from m5.objects import TimingVirtIOBlock

        system.platform.block = TimingVirtIOBlock(
            pio_addr=0x30020000,
            pio_size=0x200,
            interrupt=ArmSPI(num=289),
            image=CowDiskImage(
                child=RawDiskImage(image_file=args.disk_image, read_only=True),
                read_only=False,
            ),
            queue_size=128,
            request_latency=args.block_request_latency,
            bandwidth=args.block_bandwidth,
            flush_latency=args.block_flush_latency,
        )
        system.platform.block.pio = system.iobus.mem_side_ports
        # Match upstream ARM full-system DMA wiring: raw DmaPort writes are
        # not coherent cache requests. IOCache acquires ownership and merges
        # partial writes before sending coherent requests to the SystemXBar.
        system.io_cache = Cache(
            size="1KiB",
            assoc=8,
            tag_latency=50,
            data_latency=50,
            response_latency=50,
            mshrs=20,
            tgts_per_mshr=12,
            addr_ranges=system.mem_ranges,
        )
        system.platform.block.dma = system.io_cache.cpu_side
        system.io_cache.mem_side = system.membus.cpu_side_ports
        devices.append((0x30020000, 289))
    elif args.io_profile != "minimal":
        modern = args.io_profile == "qemu-compatible"
        system.platform.rng = MmioVirtIO(
            pio_addr=0x30080000,
            pio_size=0x200,
            modern=modern,
            interrupt=ArmSPI(num=295),
            vio=VirtIORng(),
        )
        system.platform.rng.pio = system.iobus.mem_side_ports
        devices.append((0x30080000, 295))
        if args.disk_image:
            system.platform.block = MmioVirtIO(
                pio_addr=0x30020000,
                pio_size=0x200,
                modern=modern,
                interrupt=ArmSPI(num=289),
                vio=VirtIOBlock(
                    image=CowDiskImage(
                        child=RawDiskImage(
                            image_file=args.disk_image, read_only=True
                        ),
                        read_only=False,
                    )
                ),
            )
            system.platform.block.pio = system.iobus.mem_side_ports
            devices.append((0x30020000, 289))
        if modern:
            from m5.objects import VirtIONet

            system.platform.net = MmioVirtIO(
                pio_addr=0x30060000,
                pio_size=0x200,
                modern=True,
                interrupt=ArmSPI(num=293),
                vio=VirtIONet(),
            )
            system.platform.net.pio = system.iobus.mem_side_ports
            system.ethertap = EtherTapStub(port=args.ethernet_port)
            system.ethertap.tap = system.platform.net.vio.interface
            devices.append((0x30060000, 293))
    if args.warm_reset:
        system.workload.reset_virtio_bases = [addr for addr, _ in devices]
    dtb = os.path.join(m5.options.outdir, "system.dtb")
    system.platform.write_dtb(dtb, args.cpus, devices)
    system.workload.dtb_filename = dtb
    return system


def file_digest(path):
    if not path:
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def checkpoint_contract(args, system):
    return {
        "format": 2,
        "model": models.settings(args),
        "model_helper_sha256": file_digest(models.__file__),
        "gem5_version": m5.core.gem5Version,
        "gem5_build": m5.core.compileDate,
        "config_sha256": file_digest(__file__),
        "cpus": args.cpus,
        "affinities": [ZenaCSS.affinity(i) for i in range(args.cpus)],
        "cpu_mode": args.cpu_mode,
        "roi_cpu_mode": args.roi_cpu_mode,
        "cpu_clock": args.cpu_clock,
        "io_profile": args.io_profile,
        "warm_reset": args.warm_reset,
        "seed": args.seed,
        "command_line": args.command_line,
        "kernel_sha256": file_digest(args.kernel),
        "firmware_sha256": file_digest(args.bootloader),
        "initrd_sha256": file_digest(args.initrd),
        "disk_sha256": file_digest(args.disk_image),
        "dtb_sha256": file_digest(str(system.workload.dtb_filename)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpus", type=int, choices=(4, 16), default=4)
    parser.add_argument(
        "--cpu-mode", choices=("atomic", "timing", "o3"), default="atomic"
    )
    parser.add_argument(
        "--roi-cpu-mode",
        choices=("timing", "o3"),
        help="Atomic boot, workbegin ZSWC switch, ZROI begin/end statistics",
    )
    parser.add_argument("--cpu-clock", default="2GHz")
    parser.add_argument("--boot-profile", choices=("psci",), default="psci")
    parser.add_argument("--kernel", required=True, type=file_arg)
    parser.add_argument("--bootloader", required=True, type=file_arg)
    parser.add_argument("--initrd", type=file_arg)
    parser.add_argument("--disk-image", type=file_arg)
    parser.add_argument(
        "--io-profile",
        default="minimal",
        choices=("minimal", "legacy-io", "qemu-compatible", "timing-block"),
    )
    parser.add_argument("--terminal-port", type=int, default=3456)
    parser.add_argument("--ethernet-port", type=int, default=3500)
    parser.add_argument(
        "--command-line",
        default=(
            "console=ttyAMA0,115200 earlycon=pl011,0x1a400000 "
            "rdinit=/init lpj=1000000"
        ),
    )
    parser.add_argument("--max-ticks", type=int, default=m5.MaxTick)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--warm-reset", action="store_true")
    parser.add_argument("--max-warm-resets", type=int, default=1)
    parser.add_argument("--checkpoint")
    parser.add_argument("--restore")
    parser.add_argument("--dtb-only", action="store_true")
    models.add_options(parser)
    models.add_block_options(parser)
    args = parser.parse_args()
    try:
        model = models.settings(args)
    except (ValueError, TypeError) as error:
        parser.error(str(error))
    if (
        args.checkpoint or args.restore
    ) and args.io_profile == "qemu-compatible":
        parser.error(
            "Checkpoint/restore does not preserve Ethernet peer state"
        )
    if args.io_profile == "timing-block" and (
        not args.disk_image
        or args.warm_reset
        or args.checkpoint
        or args.restore
    ):
        parser.error(
            "Timing block requires a disk and excludes reset/checkpoint/restore"
        )
    if args.max_ticks < 1 or args.max_warm_resets < 0:
        parser.error("max-ticks must be positive; max-warm-resets nonnegative")
    if args.roi_cpu_mode and (
        args.cpu_mode != "atomic"
        or args.io_profile not in ("minimal", "timing-block")
        or args.warm_reset
        or args.checkpoint
        or args.restore
    ):
        parser.error(
            "ROI switch requires atomic minimal/timing-block and excludes warm reset "
            "and checkpoint/restore"
        )
    m5.core.seedRandom(args.seed)
    system = create(args)
    root = Root(full_system=True, system=system)
    if args.warm_reset:
        system.workload.reset_uart_path = system.platform.uart.path()
        system.workload.reset_rtc_path = system.platform.rtc.path()
    with open(os.path.join(m5.options.outdir, "model.json"), "w") as output:
        json.dump(model, output, indent=2)
        output.write("\n")
    if args.dtb_only:
        return
    contract = checkpoint_contract(args, system)
    if args.restore:
        path = os.path.join(args.restore, "zena-contract.json")
        with open(path) as source:
            saved_contract = json.load(source)
        differences = [
            key for key in contract if contract[key] != saved_contract.get(key)
        ]
        if differences:
            raise ValueError(
                "Incompatible checkpoint: " + ", ".join(differences)
            )
    if args.checkpoint and os.path.exists(args.checkpoint):
        raise ValueError("Checkpoint destination already exists")
    m5.instantiate(args.restore)
    limit = min(m5.MaxTick, m5.curTick() + args.max_ticks)
    resets = 0
    roi_phase = "boot" if args.roi_cpu_mode else "disabled"
    phases = []

    def record_phase(name):
        phases.append(
            {
                "phase": name,
                "tick": m5.curTick(),
                "cpu_mode": (
                    args.cpu_mode if name == "boot" else args.roi_cpu_mode
                ),
            }
        )
        with open(
            os.path.join(m5.options.outdir, "roi-phases.json"), "w"
        ) as out:
            json.dump(phases, out, indent=2)
            out.write("\n")

    if args.roi_cpu_mode:
        record_phase("boot")
    while True:
        event = m5.simulate(limit - m5.curTick())
        if args.roi_cpu_mode and event.getCause() in ("workbegin", "workend"):
            cause, code = event.getCause(), event.getCode()
            if (
                cause == "workbegin"
                and code == 0x5A535743
                and roi_phase == "boot"
            ):
                m5.drain()
                m5.stats.dump()
                m5.switchCpus(system, list(zip(system.cpu, system.roi_cpu)))
                roi_phase = "warmup"
                record_phase(roi_phase)
                m5.stats.reset()
            elif (
                cause == "workbegin"
                and code == 0x5A524F49
                and roi_phase == "warmup"
            ):
                # The guest controls warm-up work. This boundary excludes its
                # statistics without assuming that all caches are warm.
                m5.stats.dump()
                m5.stats.reset()
                roi_phase = "roi"
                record_phase(roi_phase)
            elif (
                cause == "workend"
                and code == 0x5A524F49
                and roi_phase == "roi"
            ):
                m5.stats.dump()
                m5.stats.reset()
                roi_phase = "complete"
                record_phase(roi_phase)
            else:
                raise RuntimeError(
                    f"Unexpected ROI event {cause}/{code:#x} in {roi_phase}"
                )
            if m5.curTick() >= limit:
                raise RuntimeError("Tick limit reached at ROI boundary")
            continue
        if event.getCause() != "workbegin" or event.getCode() != 0x5A525354:
            break
        if not args.warm_reset or resets >= args.max_warm_resets:
            raise RuntimeError("Unexpected/excessive guest warm-reset request")
        m5.drain()
        m5.memWriteback(system)
        m5.memInvalidate(system)
        system.workload.resetGuest()
        resets += 1
        if m5.curTick() >= limit:
            raise RuntimeError("Tick limit reached during guest warm reset")
    if args.checkpoint:
        m5.checkpoint(args.checkpoint)
        with open(
            os.path.join(args.checkpoint, "zena-contract.json"), "w"
        ) as out:
            json.dump(contract, out, indent=2)
            out.write("\n")
    result = {
        "tick": m5.curTick(),
        "cause": event.getCause(),
        "code": event.getCode(),
        "cpus": args.cpus,
        "cpu_mode": args.cpu_mode,
        "io_profile": args.io_profile,
        "seed": args.seed,
        "warm_resets": resets,
        "final_cpu_mode": (
            args.roi_cpu_mode
            if phases and roi_phase != "boot"
            else args.cpu_mode
        ),
        "roi_phase": roi_phase,
        "roi_phases": phases,
        "roi_phase_ticks": {
            phases[i]["phase"]: phases[i + 1]["tick"] - phases[i]["tick"]
            for i in range(len(phases) - 1)
        },
    }
    with open(os.path.join(m5.options.outdir, "exit.json"), "w") as output:
        json.dump(result, output, indent=2)
        output.write("\n")
    print(f"ZENA_GEM5_EXIT: {json.dumps(result)}")
    sys.exit(
        event.getCode()
        or int(bool(args.roi_cpu_mode) and roi_phase != "complete")
    )


if __name__ == "__m5_main__":
    main()
