# SPDX-License-Identifier: BSD-3-Clause
"""Opt-in synthetic SCMI control plane; coefficients are not silicon data."""

from pathlib import Path

from m5.ext.pyfdt import pyfdt
from m5.objects import (
    AddrRange,
    ArmSPI,
    DVFSHandler,
    ScmiAgentChannel,
    ScmiCommunication,
    ScmiPlatform,
    ScmiPlatformChannel,
    SrcClockDomain,
    VoltageDomain,
)
from m5.objects.RealView import (
    GenericMHU,
    ParentMem,
)

MHU_BASE = 0x2A000000
SHMEM_BASE = 0x2A020000


def configure_power_control(system, params, temperature_mc=40000):
    """Call after CPU creation; service DMA stays on the MMIO bus."""
    cores = params["cpu"]["cores"]
    if cores not in (4, 16) or params["cpu"]["cores_per_cluster"] != 4:
        raise ValueError("SCMI requires AP4/AP16, four cores per cluster")
    system.cluster_voltages = [
        VoltageDomain(voltage=["0.95V", "0.85V", "0.75V"])
        for _ in range(cores // 4)
    ]
    system.cluster_clocks = [
        SrcClockDomain(
            clock=["2.5GHz", "2GHz", "1.8GHz"],
            voltage_domain=voltage,
            domain_id=i,
            init_perf_level=1,
        )
        for i, voltage in enumerate(system.cluster_voltages)
    ]
    for i, cpu in enumerate(system.cpu):
        cpu.clk_domain = system.cluster_clocks[i // 4]
    if hasattr(system, "roi_cpu"):
        for i, cpu in enumerate(system.roi_cpu):
            cpu.clk_domain = system.cluster_clocks[i // 4]
    # DVFSHandler's update events use one native handler owner. Replace the
    # System default instead of constructing an additional empty handler.
    system.dvfs_handler = DVFSHandler(
        domains=system.cluster_clocks,
        sys_clk_domain=system.clk_domain,
        enable=True,
        transition_latency="1200us",
    )
    platform = system.platform
    platform.scmi_shmem = ParentMem(
        range=AddrRange(SHMEM_BASE, size=0x1000),
        latency="10ns",
        conf_table_reported=False,
        # This SRAM is on the MMIO bus, outside Ruby's DRAM/SNF ranges.
        in_addr_map=False,
        kvm_map=False,
    )
    platform.scmi_shmem.port = system.iobus.mem_side_ports
    platform.mailbox = GenericMHU(pio_addr=MHU_BASE)
    for direction, offsets in (
        ("scp2ap", (0x0, 0x20, 0x200)),
        ("ap2scp", (0x100, 0x120, 0x300)),
    ):
        for name, offset, irq in zip(
            ("lowp", "highp", "sec"), offsets, (92, 93, 94)
        ):
            doorbell = getattr(platform.mailbox, f"{name}_{direction}")
            doorbell.set_address = MHU_BASE + offset + 8
            doorbell.clear_address = MHU_BASE + offset + 0x10
            if direction == "scp2ap":
                doorbell.interrupt = ArmSPI(num=irq)
    platform.mailbox.pio = system.iobus.mem_side_ports
    communication = ScmiCommunication(
        agent_channel=ScmiAgentChannel(
            shmem=platform.scmi_shmem,
            shmem_range=AddrRange(SHMEM_BASE, size=0x200),
            doorbell=platform.mailbox.highp_ap2scp,
        ),
        platform_channel=ScmiPlatformChannel(
            shmem=platform.scmi_shmem,
            shmem_range=AddrRange(SHMEM_BASE, size=0x200),
            doorbell=platform.mailbox.highp_scp2ap,
        ),
    )
    platform.scmi = ScmiPlatform(
        comms=[communication],
        dvfs=system.dvfs_handler,
        initial_temperature_mc=temperature_mc,
    )
    platform.scmi.dma = system.iobus.cpu_side_ports
    platform.mailbox.scp = platform.scmi
    return platform.scmi


def add_power_control_dtb(filename, cores, affinity):
    """One bidirectional MHUv1 mailbox and one synchronous shared buffer."""
    with open(filename, "rb") as source:
        tree = pyfdt.FdtBlobParse(source).to_fdt()
    root = tree.get_rootnode()
    used = [v[0] for p, v in root.walk() if p.endswith("/phandle")]
    first = max(used, default=0) + 1
    clk, mhu, shmem, perf, sensor = range(first, first + 5)

    def node(name, compatible=None, words=()):
        result = pyfdt.FdtNode(name)
        if compatible:
            result.append(pyfdt.FdtPropertyStrings("compatible", compatible))
        for key, value in words:
            result.append(pyfdt.FdtPropertyWords(key, value))
        return result

    root.append(
        node(
            "scmi-apb-clock",
            ["fixed-clock"],
            (
                ("phandle", [clk]),
                ("#clock-cells", [0]),
                ("clock-frequency", [24000000]),
            ),
        )
    )
    mailbox = node(
        f"mailbox@{MHU_BASE:x}",
        ["arm,mhu", "arm,primecell"],
        (
            ("reg", [0, MHU_BASE, 0, 0x1000]),
            ("#mbox-cells", [1]),
            ("phandle", [mhu]),
            ("interrupts", [0, 60, 4, 0, 61, 4, 0, 62, 4]),
            ("clocks", [clk]),
        ),
    )
    mailbox.append(pyfdt.FdtPropertyStrings("clock-names", ["apb_pclk"]))
    root.append(mailbox)
    root.append(
        node(
            f"scp-shmem@{SHMEM_BASE:x}",
            ["arm,scmi-shmem"],
            (
                ("reg", [0, SHMEM_BASE, 0, 0x200]),
                ("phandle", [shmem]),
            ),
        )
    )
    firmware = node("firmware")
    scmi = node(
        "scmi",
        ["arm,scmi"],
        (
            ("mboxes", [mhu, 1]),
            ("shmem", [shmem]),
            ("#address-cells", [1]),
            ("#size-cells", [0]),
        ),
    )
    scmi.append(
        node(
            "protocol@13",
            words=(
                ("reg", [0x13]),
                ("#clock-cells", [1]),
                ("phandle", [perf]),
            ),
        )
    )
    scmi.append(
        node(
            "protocol@15",
            words=(
                ("reg", [0x15]),
                ("#thermal-sensor-cells", [1]),
                ("phandle", [sensor]),
            ),
        )
    )
    firmware.append(scmi)
    root.append(firmware)
    zones = node("thermal-zones")
    for cluster in range(cores // 4):
        cpu_handle = None
        for i in range(cluster * 4, cluster * 4 + 4):
            cpu = tree.resolve_path(f"/cpus/cpu@{affinity(i):x}")
            cpu.append(pyfdt.FdtPropertyWords("clocks", [perf, cluster]))
            cpu.append(pyfdt.FdtPropertyWords("#cooling-cells", [2]))
            if i == cluster * 4:
                cpu_handle = next(p[0] for p in cpu if p.name == "phandle")
        zone = node(
            f"cluster{cluster}-thermal",
            words=(
                ("polling-delay", [100]),
                ("polling-delay-passive", [100]),
                ("thermal-sensors", [sensor, cluster]),
            ),
        )
        trips = node("trips")
        trip_handle = first + 5 + cluster
        trip = node(
            "passive",
            words=(
                ("temperature", [75000]),
                ("hysteresis", [5000]),
                ("phandle", [trip_handle]),
            ),
        )
        trip.append(pyfdt.FdtPropertyStrings("type", ["passive"]))
        trips.append(trip)
        zone.append(trips)
        maps = node("cooling-maps")
        maps.append(
            node(
                "map0",
                words=(
                    ("trip", [trip_handle]),
                    ("cooling-device", [cpu_handle, 0xFFFFFFFF, 0xFFFFFFFF]),
                ),
            )
        )
        zone.append(maps)
        zones.append(zone)
    root.append(zones)
    with open(filename, "wb") as target:
        target.write(tree.to_dtb())
    Path(filename).with_suffix(".dts").write_text(tree.to_dts())
