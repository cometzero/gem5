# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause

"""Functional Zena CSS AP platform; no safety or management domains.

The generic gem5 CPUs and coherent interconnect are not A720AE/CMN timing
models. This platform preserves the AP Linux address and affinity contract.
"""

from m5.objects.GenericTimer import (
    GenericTimer,
    SystemCounter,
)
from m5.objects.Gic import (
    ArmPPI,
    ArmSPI,
    Gicv3,
)
from m5.objects.RealView import (
    PL031,
    Pl011,
    RealView,
)
from m5.objects.Terminal import Terminal
from m5.util.fdthelper import (
    Fdt,
    FdtNode,
    FdtProperty,
    FdtPropertyStrings,
    FdtPropertyWords,
    FdtState,
)


class ZenaCSS(RealView):
    _RAM_BASE = 0x80000000
    _RAM_SIZE = 0x80000000
    _FIRMWARE_BASE = 0xFFE00000
    _FIRMWARE_SIZE = 0x200000
    _DTB_BASE = 0xF0000000
    _INITRD_BASE = 0xC0000000

    gic = Gicv3(
        dist_addr=0x20800000,
        redist_addr=0x20880000,
        it_lines=512,
        gicv4=True,
        maint_int=ArmPPI(num=25),
    )
    terminal = Terminal()
    uart = Pl011(
        pio_addr=0x1A400000,
        interrupt=ArmSPI(num=84),
        end_on_eot=False,
    )
    counter = SystemCounter(freqs=[125000000])
    timer = GenericTimer(
        cntfrq=125000000,
        int_el3_phys=ArmPPI(num=29),
        int_el1_phys=ArmPPI(num=30),
        int_el1_virt=ArmPPI(num=27),
        int_el2_ns_phys=ArmPPI(num=26),
        int_el2_ns_virt=ArmPPI(num=28),
        int_el2_s_phys=ArmPPI(num=20),
        int_el2_s_virt=ArmPPI(num=19),
    )
    rtc = PL031(
        pio_addr=0x300D0000,
        interrupt=ArmSPI(num=300),
        time="01/01/2024",
    )

    @classmethod
    def affinity(cls, cpu):
        return ((cpu // 4) << 16) | ((cpu % 4) << 8)

    def attach(self, bus):
        self.uart.device = self.terminal
        for dev in (self.gic, self.uart, self.rtc):
            dev.pio = bus.mem_side_ports

    def write_dtb(self, filename, cpus, devices):
        """Generate only implemented devices, independent of RealView DT."""
        state = FdtState(addr_cells=2, size_cells=2, cpu_cells=2)
        root = FdtNode("/")
        root.append(state.addrCellsProperty())
        root.append(state.sizeCellsProperty())
        root.appendCompatible(["gem5,zena-css-ap", "arm,zena-css"])
        root.append(FdtPropertyStrings("model", "gem5 Zena CSS AP"))
        root.append(
            FdtPropertyWords("interrupt-parent", state.phandle(self.gic))
        )

        def regnode(name, addr, size, compat=None):
            node = FdtNode(f"{name}@{addr:x}")
            node.append(FdtPropertyWords("reg", [0, addr, 0, size]))
            if compat:
                node.appendCompatible(compat)
            return node

        memory = regnode("memory", self._RAM_BASE, self._RAM_SIZE)
        memory.append(FdtPropertyStrings("device_type", "memory"))
        root.append(memory)
        reserved = FdtNode("reserved-memory")
        reserved.append(state.addrCellsProperty())
        reserved.append(state.sizeCellsProperty())
        reserved.append(FdtProperty("ranges"))
        firmware = regnode(
            "firmware", self._FIRMWARE_BASE, self._FIRMWARE_SIZE
        )
        firmware.append(FdtProperty("no-map"))
        reserved.append(firmware)
        root.append(reserved)

        cpu_nodes = FdtNode("cpus")
        cpu_nodes.append(FdtPropertyWords("#address-cells", [2]))
        cpu_nodes.append(FdtPropertyWords("#size-cells", [0]))
        topology = FdtNode("cpu-map")
        for cluster_id in range(cpus // 4):
            cluster = FdtNode(f"cluster{cluster_id}")
            for core_id in range(4):
                cpu_id = cluster_id * 4 + core_id
                affinity = self.affinity(cpu_id)
                key = f"zena-cpu-{cpu_id}"
                cpu = FdtNode(f"cpu@{affinity:x}")
                cpu.appendCompatible(["arm,arm-v8"])
                cpu.append(FdtPropertyStrings("device_type", "cpu"))
                cpu.append(FdtPropertyStrings("enable-method", "psci"))
                cpu.append(FdtPropertyWords("reg", [0, affinity]))
                cpu.appendPhandle(key)
                cpu_nodes.append(cpu)
                core = FdtNode(f"core{core_id}")
                core.append(FdtPropertyWords("cpu", state.phandle(key)))
                cluster.append(core)
            topology.append(cluster)
        cpu_nodes.append(topology)
        root.append(cpu_nodes)
        psci = FdtNode("psci")
        psci.appendCompatible(["arm,psci-1.0", "arm,psci-0.2"])
        psci.append(FdtPropertyStrings("method", "smc"))
        root.append(psci)

        gic = regnode("interrupt-controller", 0x20800000, 0x10000)
        gic.appendCompatible(["arm,gic-v3"])
        gic.append(
            FdtPropertyWords(
                "reg",
                [0, 0x20800000, 0, 0x10000, 0, 0x20880000, 0, cpus * 0x40000],
            )
        )
        gic.append(FdtPropertyWords("#interrupt-cells", [3]))
        gic.append(FdtProperty("interrupt-controller"))
        gic.append(FdtPropertyWords("redistributor-stride", [0, 0x40000]))
        gic.append(FdtPropertyWords("#redistributor-regions", [1]))
        gic.append(FdtPropertyWords("interrupts", [1, 9, 4]))
        gic.appendPhandle(self.gic)
        root.append(gic)
        timer = FdtNode("timer")
        timer.appendCompatible(["arm,armv8-timer"])
        timer.append(
            FdtPropertyWords(
                "interrupts", [1, 13, 4, 1, 14, 4, 1, 11, 4, 1, 10, 4]
            )
        )
        timer.append(FdtPropertyWords("clock-frequency", [125000000]))
        timer.append(FdtProperty("always-on"))
        root.append(timer)

        clock = FdtNode("clock-24mhz")
        clock.appendCompatible(["fixed-clock"])
        clock.append(FdtPropertyWords("#clock-cells", [0]))
        clock.append(FdtPropertyWords("clock-frequency", [24000000]))
        clock.appendPhandle("zena-clock")
        root.append(clock)
        phandle = state.phandle("zena-clock")
        uart = regnode(
            "uart", 0x1A400000, 0x1000, ["arm,pl011", "arm,primecell"]
        )
        uart.append(FdtPropertyWords("interrupts", [0, 52, 4]))
        uart.append(FdtPropertyWords("clocks", [phandle, phandle]))
        uart.append(FdtPropertyStrings("clock-names", ["uartclk", "apb_pclk"]))
        root.append(uart)
        chosen = FdtNode("chosen")
        chosen.append(FdtPropertyStrings("stdout-path", "/uart@1a400000"))
        root.append(chosen)
        rtc = regnode(
            "rtc", 0x300D0000, 0x1000, ["arm,pl031", "arm,primecell"]
        )
        rtc.append(FdtPropertyWords("interrupts", [0, 268, 4]))
        rtc.append(FdtPropertyWords("clocks", [phandle]))
        rtc.append(FdtPropertyStrings("clock-names", ["apb_pclk"]))
        root.append(rtc)
        for address, intid in devices:
            dev = regnode("virtio_mmio", address, 0x200, ["virtio,mmio"])
            dev.append(FdtPropertyWords("interrupts", [0, intid - 32, 4]))
            dev.append(FdtProperty("dma-coherent"))
            root.append(dev)
        fdt = Fdt()
        fdt.add_rootnode(root)
        fdt.writeDtbFile(filename)
        fdt.writeDtsFile(filename[:-4] + ".dts")
