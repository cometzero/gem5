# SPDX-License-Identifier: BSD-3-Clause
"""One reset-bounded activity owner for ROI, DVFS and periodic sampling."""

import json
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "zena_cmn"
sys.path.insert(0, str(SCRIPTS))
from analyze_power import (
    actor_definitions,
    estimate_energy,
    extract_window,
    leakage_power,
    stats_blocks,
)
from thermal_replay import ThermalNetwork


class EpochLedger:
    """Pure bookkeeping: a reset closes an epoch; dumps never overlap it."""

    def __init__(self, begin=0, epoch=0):
        self.begin = begin
        self.epoch = epoch
        self.consumed = None

    def accept(self, tick):
        if tick < self.begin:
            raise ValueError("power epoch time moved backwards")
        if tick == self.begin or tick == self.consumed:
            return None
        if self.consumed is not None:
            raise ValueError("power statistics must reset between dumps")
        result = (self.begin, tick, self.epoch)
        self.consumed = tick
        self.epoch += 1
        return result

    def reset(self, tick):
        if tick < self.begin or (self.consumed not in (None, tick)):
            raise ValueError("reset would lose unaccounted activity")
        self.begin, self.consumed = tick, None


def configure_thermal(system, profile):
    """Use the same explicit RC schema as offline replay, native integration."""
    from m5.objects import (
        LatchedPowerModel,
        PowerActor,
        PowerModel,
        SubSystem,
        ThermalCapacitor,
        ThermalDomain,
        ThermalModel,
        ThermalNode,
        ThermalReference,
        ThermalResistor,
    )

    checked = ThermalNetwork(profile)
    system.power_zones = [
        SubSystem(
            thermal_domain=ThermalDomain(
                initial_temperature=f"{node['initial_c']}C"
            )
        )
        for node in profile["nodes"]
    ]
    system.power_actors = [PowerActor() for _ in checked.names]
    for actor, zone in zip(system.power_actors, system.power_zones):
        actor.power_state.default_state = "ON"
        actor.power_model = [
            PowerModel(
                subsystem=zone,
                pm=[LatchedPowerModel() for _ in range(4)],
            )
        ]
    system.power_thermal = ThermalModel(automatic_step=False)
    nodes = {name: ThermalNode() for name in checked.names}
    ambient = ThermalNode()
    model = system.power_thermal
    model.addReference(
        ThermalReference(temperature=f"{checked.ambient}C"), ambient
    )
    for spec, zone in zip(profile["nodes"], system.power_zones):
        node = nodes[spec["name"]]
        model.addDomain(zone.thermal_domain, node)
        model.addCapacitor(
            ThermalCapacitor(capacitance=spec["capacitance_j_per_k"]),
            node,
            ambient,
        )
        if "ambient_resistance_k_per_w" in spec:
            model.addResistor(
                ThermalResistor(resistance=spec["ambient_resistance_k_per_w"]),
                node,
                ambient,
            )
    for edge in profile.get("edges", []):
        model.addResistor(
            ThermalResistor(resistance=edge["resistance_k_per_w"]),
            nodes[edge["a"]],
            nodes[edge["b"]],
        )
    return checked.names


class PowerRuntime:
    """Retrospective P for an elapsed interval is latched before its RC solve.

    Atomic boot and checkpoint cache reconstruction have no timing activity
    estimate. They do not advance this thermal history. Native held-energy
    counters are separate from the activity-derived energy ledger below.
    """

    def __init__(
        self,
        system,
        outdir,
        profile,
        thermal_profile,
        interval_ticks,
        names,
        control,
    ):
        import m5

        self.system, self.outdir = system, Path(outdir)
        self.profile, self.thermal_profile = profile, thermal_profile
        if type(interval_ticks) is not int or interval_ticks < 1:
            raise ValueError(
                "power interval must span at least one simulator tick"
            )
        self.interval = interval_ticks
        self.names, self.control = names, control
        self.config = json.loads((self.outdir / "config.json").read_text())
        self.definitions = actor_definitions(self.config)
        required = {a["thermal_node"] for a in self.definitions}
        if not required <= set(names):
            raise ValueError(
                f"missing native thermal nodes: {required-set(names)}"
            )
        self.ledger = EpochLedger(int(m5.curTick()))
        self.active = False
        self.next_tick = None
        self.energy_j = 0.0
        self.original_dump, self.original_reset = m5.stats.dump, m5.stats.reset
        self.dumping = False
        self.file_position = 0
        self.last_state = None
        self.host_costs = {}
        self.phase_costs = {}
        self.last_host_time = time.perf_counter()
        # Native DVFSHandler calls these Python entry points too.
        m5.stats.dump, m5.stats.reset = self.dump, self.reset
        for actor in system.power_actors:
            actor.power_model[0].pm[0].setAccountingEnabled(False)
        self.write_summary()

    def temperatures(self):
        return {
            name: zone.thermal_domain.getTemperatureC()
            for name, zone in zip(self.names, self.system.power_zones)
        }

    def publish_sensors(self, temperatures):
        sensors = []
        if hasattr(self.system.platform, "scmi"):
            for cluster in range(len(self.system.cpu) // 4):
                value = max(
                    [
                        temperatures[f"core{i}"]
                        for i in range(cluster * 4, cluster * 4 + 4)
                    ]
                    + [temperatures[f"dsu{cluster}"]]
                )
                self.system.platform.scmi.setTemperatureMilliC(
                    cluster, round(value * 1000)
                )
                sensors.append(value)
        return sensors

    def append(self, filename, record):
        with (self.outdir / filename).open("a") as target:
            target.write(json.dumps(record, allow_nan=False) + "\n")

    def write_summary(self):
        record = dict(
            qualification=self.profile["qualification"],
            thermal_qualification=self.thermal_profile["qualification"],
            measurement="ONLINE_MODEL_ACTIVITY_NOT_SILICON",
            cluster_sensor_mapping="SYNTHETIC_MAX_FOUR_CORES_AND_DSU",
            activity_energy_j=self.energy_j,
            epochs=self.ledger.epoch,
            boot_and_checkpoint_thermal_policy="FROZEN_NO_TIMING_ESTIMATE",
            sensor_handoff="NATIVE_RC_AT_TIMING_START_THEN_COMPLETED_EPOCHS",
            native_held_energy="DISABLED_RETROSPECTIVE_LEDGER_IS_OWNER",
            native_power_stats="PREVIOUS_LATCH_NOT_CURRENT_EPOCH",
            host_accounting_seconds=self.host_costs,
            phase_costs=self.phase_costs,
            host_cost_scope="CURRENT_PROCESS_TIMING_INTERVALS",
            temperature_c=self.temperatures(),
            last_epoch=self.last_state,
        )
        (self.outdir / "online-power-summary.json").write_text(
            json.dumps(record, indent=2, allow_nan=False) + "\n"
        )

    def reset(self, *args, **kwargs):
        import m5

        now = int(m5.curTick())
        if (
            self.active
            and now > self.ledger.begin
            and self.ledger.consumed is None
        ):
            self.dump()
        result = self.original_reset(*args, **kwargs)
        self.ledger.reset(now)
        return result

    def dump(self, *args, **kwargs):
        import m5

        if self.dumping:
            raise RuntimeError("recursive power statistics dump")
        self.dumping = True
        try:
            started = time.perf_counter()
            result = self.original_dump(*args, **kwargs)
            dumped = time.perf_counter()
            now = int(m5.curTick())
            path = self.outdir / "stats.txt"
            previous_position = self.file_position
            with path.open() as source:
                if not self.active:
                    source.seek(0, 2)
                    self.file_position = source.tell()
                    return result
                source.seek(self.file_position)
                blocks = stats_blocks(source.read())
                self.file_position = source.tell()
            parsed = time.perf_counter()
            window = self.ledger.accept(now)
            if window is None:
                return result
            begin, end, epoch = window
            matches = [
                b
                for b in blocks
                if b.get("finalTick") == end
                and b.get("simTicks") == end - begin
            ]
            if len(matches) != 1:
                raise ValueError("online power requires one exact stats block")
            activity = extract_window(
                self.config,
                matches[0],
                begin,
                end,
                epoch,
                self.control.active_case,
                definitions=self.definitions,
            )
            activity["phase"] = self.control.phase
            activity["native_power_stats_epoch"] = (
                self.last_state["epoch"] if self.last_state else None
            )
            energy = estimate_energy([activity], self.profile)[0]
            if energy["coverage"] != "COMPLETE_LEDGER":
                raise ValueError(
                    "online feedback requires all actor coefficients; "
                    "use offline partial analysis for missing data"
                )
            initial = self.temperatures()
            dynamic = {name: 0.0 for name in self.names}
            leakage = dict(dynamic)
            # Explicit temperature at interval start: reduce the step to check
            # leakage/RC convergence; offline replay also offers implicit leakage.
            for actor in energy["actors"]:
                name = actor["thermal_node"]
                watts = leakage_power(actor["leakage"], initial[name])
                actor["leakage_j"] = watts * activity["seconds"]
                actor["total_j"] = actor["dynamic_j"] + actor["leakage_j"]
                actor["average_w"] = actor["total_j"] / activity["seconds"]
                dynamic[name] += actor["dynamic_j"] / activity["seconds"]
                leakage[name] += watts
            energy["total_j"] = sum(a["total_j"] for a in energy["actors"])
            energy["configured_subtotal_j"] = energy["total_j"]
            energy["average_w"] = energy["total_j"] / activity["seconds"]
            accounted = time.perf_counter()
            for name, actor in zip(self.names, self.system.power_actors):
                actor.power_model[0].pm[0].setPower(
                    dynamic[name], leakage[name]
                )
            self.system.power_thermal.advance(activity["seconds"])
            final = self.temperatures()
            sensors = self.publish_sensors(final)
            self.energy_j += energy["total_j"]
            state = dict(
                begin_tick=begin,
                end_tick=end,
                epoch=epoch,
                seconds=activity["seconds"],
                temperature_c=final,
                initial_temperature_c=initial,
                cluster_sensor_c=sensors,
                dynamic_w=dynamic,
                leakage_w=leakage,
                phase=self.control.phase,
                clock_period_ticks={
                    k: v
                    for k, v in matches[0].items()
                    if "cluster_clocks" in k and k.endswith(".clock")
                },
            )
            integrated = time.perf_counter()
            self.append("online-activity.jsonl", activity)
            self.append("online-energy.jsonl", energy)
            self.append("online-thermal.jsonl", state)
            self.last_state = state
            written = time.perf_counter()
            for name, seconds in (
                ("stats_dump", dumped - started),
                ("stats_read_parse", parsed - dumped),
                ("activity_energy", accounted - parsed),
                ("native_thermal", integrated - accounted),
                ("jsonl_write", written - integrated),
            ):
                self.host_costs[name] = self.host_costs.get(name, 0) + seconds
            costs = self.phase_costs.setdefault(
                self.control.phase,
                dict(epochs=0, model_ticks=0, host_seconds=0, stats_bytes=0),
            )
            costs["epochs"] += 1
            costs["model_ticks"] += end - begin
            costs["host_seconds"] += written - self.last_host_time
            costs["stats_bytes"] += self.file_position - previous_position
            self.last_host_time = written
            self.write_summary()
            # This owner closes every dump, including native DVFS callbacks.
            self.original_reset()
            self.ledger.reset(now)
            return result
        finally:
            self.dumping = False

    def start(self):
        import m5

        if self.active:
            return
        self.reset()
        self.active = True
        self.publish_sensors(self.temperatures())
        self.next_tick = int(m5.curTick()) + self.interval
        self.last_host_time = time.perf_counter()

    def boundary(self):
        import m5

        if self.active and int(m5.curTick()) >= self.next_tick:
            self.dump()
            self.reset()
            self.next_tick = int(m5.curTick()) + self.interval

    def finish(self):
        if self.active:
            self.dump()
            self.reset()
        self.active = False
        self.next_tick = None
        self.write_summary()

    def freeze(self):
        import m5

        if self.active:
            self.dump()
            self.reset()
            if self.next_tick <= int(m5.curTick()):
                self.next_tick = int(m5.curTick()) + self.interval
        saved = self.state()
        self.active = False
        return saved

    def state(self):
        return dict(
            schema=1,
            active=self.active,
            epoch=self.ledger.epoch,
            next_tick=self.next_tick,
            energy_j=self.energy_j,
            last_state=self.last_state,
        )

    def restore(self, state):
        import m5

        if state.get("schema") != 1:
            raise ValueError("invalid online power checkpoint state")
        self.active = bool(state["active"])
        self.ledger = EpochLedger(int(m5.curTick()), state["epoch"])
        self.next_tick = state["next_tick"]
        if self.active and (
            type(self.next_tick) is not int
            or self.next_tick <= int(m5.curTick())
        ):
            raise ValueError("restored power deadline is not in the future")
        self.energy_j = state["energy_j"]
        self.last_state = state["last_state"]
        self.original_reset()
        self.last_host_time = time.perf_counter()
        self.write_summary()
