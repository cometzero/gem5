# SPDX-License-Identifier: BSD-3-Clause
"""SI CL1 integrated boot and benchmarks with experimental PMSA."""

import argparse
import json
from pathlib import Path

import m5
from m5.objects import (
    AddrRange,
    ArmAtomicSimpleCPU,
    ArmFsWorkload,
    ArmISA,
    ArmMinorCPU,
    ArmPPI,
    ArmRelease,
    ArmSPI,
    ArmSystem,
    ArmTimingSimpleCPU,
    Cache,
    GenericTimer,
    Gicv3,
    L2XBar,
    NoncoherentXBar,
    Pl011,
    RealView,
    Root,
    SimpleMemDelay,
    SimpleMemory,
    SrcClockDomain,
    SystemCounter,
    SystemXBar,
    Terminal,
    VoltageDomain,
)
from m5.params import NULL


def route():
    return NoncoherentXBar(
        frontend_latency=0,
        forward_latency=0,
        response_latency=0,
        header_latency=1,
        width=16,
    )


def ccsidr(size, ways):
    return ((size // (64 * ways) - 1) << 13) | ((ways - 1) << 3) | 2


def build(args):
    # Lower-level wiring is intentional: stdlib ArmBoard assumes VMSA/Linux
    # and cannot express private TCMs or an LLRAM path outside the L2.
    integrated = getattr(args, "mode", "benchmark") == "integrated"
    workload = ArmFsWorkload(object_file=str(args.elf))
    if integrated:
        # Preserve the binary-only post-build CRC; keep ELF entry and symbols.
        workload.extras = [str(args.image)]
        workload.extras_addrs = [0x140000000]
    system = ArmSystem(
        mem_mode="atomic" if args.cpu == "atomic" else "timing",
        cache_line_size=64,
        highest_el_is_64=True,
        phys_addr_range_64=40,
        multi_proc=True,
        cpu_affinities=[0x10000 + (i << 8) for i in range(4)],
        release=ArmRelease(extensions=["LPAE", "VIRTUALIZATION", "FEAT_LSE"]),
        workload=workload,
    )
    system.clk_domain = SrcClockDomain(
        clock=args.clock, voltage_domain=VoltageDomain()
    )
    # Several L1I misses to one line may share this upstream port. The
    # downstream snoop filter assumes one outstanding miss per line/port;
    # use broadcast here, with per-L1 filtering retained on l2bus.
    system.fabric = SystemXBar(width=32, snoop_filter=NULL)
    system.mm_fabric = SystemXBar(width=32)
    mm = AddrRange(0x40000000, size="8MiB")
    nc = AddrRange(0x48000000, size="8MiB")
    llram = AddrRange(0x140000000, size="8MiB")
    system.mem_ranges = [mm, nc, llram]
    system.mm_sram = SimpleMemory(
        range=mm, latency=args.sram_latency, bandwidth="32000000000B/s"
    )
    system.nc_sram = SimpleMemory(
        range=nc, latency=args.sram_latency, bandwidth="32000000000B/s"
    )
    system.llram = SimpleMemory(
        range=llram, latency="5ns", bandwidth="32000000000B/s"
    )
    system.mm_sram.port = system.mm_fabric.mem_side_ports
    for memory in (system.nc_sram, system.llram):
        memory.port = system.fabric.mem_side_ports
    if integrated:
        hipc = AddrRange(0xE0130000, size="512KiB")
        system.mem_ranges = [mm, nc, hipc, llram]
        system.hipc_sram = SimpleMemory(range=hipc, latency=args.sram_latency)
        system.hipc_sram.port = system.fabric.mem_side_ports

    system.platform = RealView()
    platform = system.platform
    platform.gic = Gicv3(
        dist_addr=0x30200000,
        redist_addr=0x30260000,
        cpu_max=4,
        it_lines=512,
        maint_int=ArmPPI(num=25),
    )
    platform.gic.pio = system.fabric.mem_side_ports
    counter_hz = 125000000 if integrated else 100000000
    platform.counter = SystemCounter(freqs=[counter_hz])
    platform.timer = GenericTimer(
        counter=platform.counter,
        cntfrq=counter_hz,
        int_el1_phys=ArmPPI(num=29),
        int_el1_virt=ArmPPI(num=27),
        int_el2_ns_phys=ArmPPI(num=26),
        int_el2_ns_virt=ArmPPI(num=28),
        int_el2_s_phys=ArmPPI(num=20),
        int_el2_s_virt=ArmPPI(num=19),
        int_el3_phys=ArmPPI(num=29),
    )
    platform.terminal = Terminal(
        port=getattr(args, "terminal_port", None)
        or str(Path(m5.options.outdir).resolve() / "console.sock"),
        outfile="file",
    )
    platform.uart = Pl011(
        pio_addr=0x2A410000,
        interrupt=ArmSPI(num=39),
        device=platform.terminal,
        end_on_eot=not integrated,
    )
    platform.uart.pio = system.fabric.mem_side_ports
    if integrated:
        from m5.objects import SiCl1Mhu

        platform.mhu = SiCl1Mhu(
            hipc_tx_irq=ArmSPI(num=72),
            hipc_rx_irq=ArmSPI(num=73),
            pfdi_irq=ArmSPI(num=82),
        )
        platform.mhu.pio = system.fabric.mem_side_ports

    # MM is the only L2 allocation path. Its PoC is below L2. LLRAM,
    # peripherals and NC SRAM have a distinct, cache-free PoC fabric.
    system.l2bus = L2XBar(width=32)
    system.system_port = system.l2bus.cpu_side_ports
    system.l2 = Cache(
        size="512KiB",
        assoc=8,
        tag_latency=8,
        data_latency=8,
        response_latency=1,
        mshrs=16,
        tgts_per_mshr=8,
        addr_ranges=[mm],
    )
    system.l2.cpu_side = system.l2bus.mem_side_ports
    system.l2.mem_side = system.mm_fabric.cpu_side_ports
    # SimpleMemDelay supports cache-clean tryTiming and snoop forwarding;
    # a noncoherent Bridge cannot connect these coherent fabrics. Its
    # queues are generic buffering, not calibrated hardware credits.
    system.l2_bypass = SimpleMemDelay(
        read_req="1ns", read_resp="1ns", write_req="1ns", write_resp="1ns"
    )
    system.l2_bypass.cpu_side_port = system.l2bus.mem_side_ports
    system.l2_bypass.mem_side_port = system.fabric.cpu_side_ports

    cpu_class = {
        "atomic": ArmAtomicSimpleCPU,
        "timing": ArmTimingSimpleCPU,
        "minor": ArmMinorCPU,
    }[args.cpu]
    system.cpu = [cpu_class(cpu_id=i) for i in range(4)]
    ids = [ccsidr(65536, 4), ccsidr(65536, 4), ccsidr(524288, 8)]
    ids += [0] * (14 - len(ids))
    for cpu in system.cpu:
        cpu.isa = [
            ArmISA(
                pmsa64=True,
                pmsa_regions=32,
                pmsa_uncached_data_ranges=[llram],
                midr=0x410FD141,
                id_aa64mmfr0_el1=0x001F000000000002,
                cache_id_registers_enabled=True,
                clidr=(2 << 27) | (2 << 24) | (2 << 21) | (4 << 3) | 3,
                ccsidr=ids,
                pmsa_itcm=AddrRange(0x100000000, size="128KiB"),
                pmsa_dtcm=AddrRange(0x100100000, size="128KiB"),
            )
        ]
        cpu.createInterruptController()
        cpu.createThreads()
        cpu.iroute = route()
        cpu.droute = route()
        cpu.tcmbus = route()
        cpu.icache_port = cpu.iroute.cpu_side_ports
        cpu.dcache_port = cpu.droute.cpu_side_ports
        cpu.itcm = SimpleMemory(
            range=AddrRange(0x100000000, size="128KiB"),
            latency=args.tcm_latency,
            bandwidth="16000000000B/s",
            in_addr_map=False,
        )
        cpu.dtcm = SimpleMemory(
            range=AddrRange(0x100100000, size="128KiB"),
            latency=args.tcm_latency,
            bandwidth="16000000000B/s",
            in_addr_map=False,
        )
        cpu.iroute.mem_side_ports = cpu.tcmbus.cpu_side_ports
        cpu.droute.mem_side_ports = cpu.tcmbus.cpu_side_ports
        cpu.itcm.port = cpu.tcmbus.mem_side_ports
        cpu.dtcm.port = cpu.tcmbus.mem_side_ports
        cpu.icache = Cache(
            size="64KiB",
            assoc=4,
            tag_latency=2,
            data_latency=2,
            response_latency=1,
            mshrs=4,
            tgts_per_mshr=8,
            is_read_only=True,
        )
        cpu.dcache = Cache(
            size="64KiB",
            assoc=4,
            tag_latency=2,
            data_latency=2,
            response_latency=1,
            mshrs=8,
            tgts_per_mshr=8,
        )
        cpu.iroute.default = cpu.icache.cpu_side
        cpu.droute.default = cpu.dcache.cpu_side
        cpu.icache.mem_side = system.l2bus.cpu_side_ports
        cpu.dcache.mem_side = system.l2bus.cpu_side_ports
        # Walk ports remain connected for SimObject consistency. PMSA
        # translation never issues a table walk.
        cpu.mmu.connectWalkerPorts(
            system.l2bus.cpu_side_ports, system.l2bus.cpu_side_ports
        )
    return system


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", required=True, type=Path)
    parser.add_argument("--image", type=Path)
    parser.add_argument(
        "--mode", choices=["integrated", "benchmark"], default="benchmark"
    )
    parser.add_argument(
        "--cpu", choices=["atomic", "timing", "minor"], default="minor"
    )
    parser.add_argument("--clock", default="1GHz")
    parser.add_argument("--sram-latency", default="20ns")
    parser.add_argument("--tcm-latency", default="1ns")
    parser.add_argument("--max-ticks", type=int, default=200000000000)
    parser.add_argument("--roi-schedule", type=Path)
    parser.add_argument("--terminal-port", type=int)
    args = parser.parse_args()
    if args.terminal_port is not None and not 1 <= args.terminal_port <= 65535:
        parser.error("terminal-port must be 1..65535")
    if args.max_ticks < 0 or (
        args.mode == "benchmark" and args.max_ticks == 0
    ):
        parser.error("max-ticks must be nonnegative (positive for benchmark)")
    if args.mode == "integrated":
        if args.image is None or not args.image.is_file():
            parser.error(
                "integrated mode requires --image with "
                "the original deployed binary"
            )
        if args.roi_schedule:
            parser.error("ROI replay is only available in benchmark mode")
    elif args.image:
        parser.error("--image requires integrated mode")
    if args.max_ticks == 0:
        args.max_ticks = m5.MaxTick
    if not args.elf.is_file():
        parser.error(f"ELF does not exist: {args.elf}")
    # Validate architecture before gem5's workload loader reports a less
    # useful failure (ELF64, little endian, EM_AARCH64).
    header = args.elf.read_bytes()[:20]
    if header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\xb7\x00":
        parser.error("--elf must be an AArch64 little-endian ELF64 image")
    schedule = []
    if args.roi_schedule:
        schedule = json.loads(args.roi_schedule.read_text())
        previous = 0
        for roi in schedule:
            if not previous <= roi["begin_tick"] < roi["end_tick"]:
                parser.error("ROI schedule must be ordered and nonoverlapping")
            if roi["end_tick"] > args.max_ticks:
                parser.error("ROI schedule exceeds max-ticks")
            previous = roi["end_tick"]
    m5.ticks.setGlobalFrequency("1THz")
    root = Root(full_system=True, system=build(args))
    m5.instantiate()
    events = []
    stopped = False
    for roi in schedule:
        for phase, tick in [
            ("begin", roi["begin_tick"]),
            ("end", roi["end_tick"]),
        ]:
            event = m5.simulate(tick - m5.curTick())
            if m5.curTick() != tick:
                stopped = True
                break
            if phase == "begin":
                m5.stats.reset()
            else:
                m5.stats.dump()
            events.append(
                {"name": roi["name"], "phase": phase, "tick": m5.curTick()}
            )
        if stopped:
            break
    if not stopped:
        event = m5.simulate(max(0, args.max_ticks - m5.curTick()))
    result = {
        "cpu": args.cpu,
        "exit_cause": event.getCause(),
        "tick": m5.curTick(),
        "counter_hz": 125000000 if args.mode == "integrated" else 100000000,
        "mode": args.mode,
        "sim_hz": 1000000000000,
        "counter_epoch_tick": 0,
        "roi_events": events,
        "roi_complete": len(events) == 2 * len(schedule),
        "model": (
            "experimental PMSA64; generic pipeline; no silicon calibration"
        ),
    }
    (Path(m5.options.outdir) / "run-result.json").write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps(result))
    if event.getCause() != "UART received EOT":
        raise SystemExit(1)


if __name__ == "__m5_main__":
    main()
