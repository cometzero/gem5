# SPDX-License-Identifier: BSD-3-Clause
"""Instantiate the AP4/AP16 SCMI/DVFS helper without a guest image."""

import argparse
import json
import sys
from pathlib import Path

import m5
from m5.objects import (
    AddrRange,
    ArmAtomicSimpleCPU,
    ArmFsWorkload,
    ArmSystem,
    Armv8,
    IOXBar,
    Root,
    SimpleMemory,
    SrcClockDomain,
    StubWorkload,
    VoltageDomain,
    ZenaCSS,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "configs"))
from zena_cmn.power_control import configure_power_control

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--cpus", type=int, choices=(4, 16), required=True)
parser.add_argument(
    "--firmware",
    help="Linked power_control_smoke.S ELF for SCMI/DVFS execution",
)
args = parser.parse_args()
system = ArmSystem(
    mem_mode="atomic",
    mem_ranges=[AddrRange(ZenaCSS._RAM_BASE, size="4KiB")],
    workload=(
        ArmFsWorkload(object_file=args.firmware)
        if args.firmware
        else StubWorkload()
    ),
    release=Armv8(),
    highest_el_is_64=True,
    auto_reset_addr=False,
    reset_addr=ZenaCSS._RAM_BASE,
    cpu_affinities=[ZenaCSS.affinity(i) for i in range(args.cpus)],
)
system.voltage_domain = VoltageDomain(voltage="1V")
system.clk_domain = SrcClockDomain(
    clock="2GHz", voltage_domain=system.voltage_domain
)
system.iobus = IOXBar()
system.platform = ZenaCSS()
system.platform.gic.cpu_max = args.cpus
system.platform.attach(system.iobus)
system.memory = SimpleMemory(range=system.mem_ranges[0])
system.memory.port = system.iobus.mem_side_ports
system.system_port = system.iobus.cpu_side_ports
system.cpu = [ArmAtomicSimpleCPU(cpu_id=i) for i in range(args.cpus)]
for cpu in system.cpu:
    cpu.createThreads()
    cpu.createInterruptController()
    cpu.icache_port = system.iobus.cpu_side_ports
    cpu.dcache_port = system.iobus.cpu_side_ports
service = configure_power_control(
    system, {"cpu": {"cores": args.cpus, "cores_per_cluster": 4}}
)
root = Root(full_system=True, system=system)
m5.instantiate()
assert service.idle()
config = json.loads(Path(m5.options.outdir, "config.json").read_text())[
    "system"
]
shmem = config["platform"]["scmi_shmem"]
assert not shmem["in_addr_map"] and not shmem["kvm_map"]
assert "dvfs" not in config
assert config["dvfs_handler"]["enable"]
assert len(config["dvfs_handler"]["domains"]) == args.cpus // 4
assert config["platform"]["scmi"]["dvfs"] == "system.dvfs_handler"
for cluster in range(args.cpus // 4):
    assert config["cluster_clocks"][cluster]["clock"] == [400, 500, 556]
    assert config["cluster_voltages"][cluster]["voltage"] == [0.95, 0.85, 0.75]
    assert service.getTemperatureMilliC(cluster) == 40000
    service.setTemperatureMilliC(cluster, 41000 + cluster)
    assert service.getTemperatureMilliC(cluster) == 41000 + cluster
    for cpu in system.cpu[cluster * 4 : cluster * 4 + 4]:
        assert cpu.clk_domain is system.cluster_clocks[cluster]
result = dict(
    status="PASS",
    cpus=args.cpus,
    domains=args.cpus // 4,
    qualification="NATIVE_CONFIGURATION_NOT_GUEST_CONTROL",
)
if args.firmware:
    event = m5.simulate(
        10_000_000_000
    )  # 10 ms bounds four 1.2 ms transitions.
    assert event.getCause() == "m5_exit instruction encountered", (
        event.getCause(),
        event.getCode(),
    )
    assert event.getCode() == 0
    # Polling may observe channel-free before the final DMA completion event.
    m5.simulate(1_000_000)
    assert service.idle()
    m5.stats.dump()
    clock_periods = {}
    for line in Path(m5.options.outdir, "stats.txt").read_text().splitlines():
        words = line.split()
        if words and words[0].startswith("system.cluster_clocks"):
            if words[0].endswith(".clock"):
                clock_periods[words[0]] = int(words[1])
    assert len(clock_periods) == args.cpus // 4
    assert set(clock_periods.values()) == {400}
    assert m5.curTick() >= args.cpus // 4 * 1_200_000_000
    result.update(
        qualification="BARE_SCMI_NATIVE_DVFS_NOT_LINUX",
        tick=int(m5.curTick()),
        verified_level=2500,
        clock_periods=clock_periods,
    )
Path(m5.options.outdir, "result.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result))
