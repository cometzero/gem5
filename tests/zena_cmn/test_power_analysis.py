# SPDX-License-Identifier: BSD-3-Clause
import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from analyze_power import (
    actor_definitions,
    analyze_run,
    estimate_energy,
    extract_activity,
    leakage_power,
    roi_intervals,
)
from thermal_replay import (
    ThermalNetwork,
    assumed_profile,
    replay,
)


def cache(path):
    return {"cache": {"type": "RubyCache", "path": path + ".cache"}}


CONFIG = {
    "system": {
        "cpu": [
            {
                "cpu_id": 0,
                "path": "system.cpu00",
                **{
                    k: cache("system.cpu00." + k) for k in ("l1i", "l1d", "l2")
                },
            }
        ],
        "roi_cpu": [{"cpu_id": 0, "path": "system.roi_cpu00"}],
        "ruby": {
            "rnf": [{"dsu_l3": cache("system.ruby.rnf0.dsu_l3")}],
            "hnf": [{"cntrl": cache("system.ruby.hnf0.cntrl")}],
            "network": {
                "type": "GarnetNetwork",
                "path": "system.ruby.network",
                "routers": [
                    {
                        "name": "routers0",
                        "path": "system.ruby.network.routers0",
                    }
                ],
            },
        },
        "mem_ctrls": [
            {
                "path": "system.mem_ctrls00",
                "dram": {"path": "system.mem_ctrls00.dram"},
            }
        ],
        "iobus": {"path": "system.iobus"},
    }
}
EVENTS = [
    {"phase": "roi_begin", "tick": 100, "case_id": 1},
    {"phase": "roi_end", "tick": 1100, "case_id": 1},
]
STATS = """---------- Begin Simulation Statistics ----------
simFreq 1000
simTicks 1000
finalTick 1100
system.roi_cpu0.commitStats0.numOps 20
system.roi_cpu0.power_state.pwrStateResidencyTicks::OFF 1100
system.cpu0.l1d.cache.numDataArrayReads 5
system.ruby.network.ext_in_link_utilization 3
system.ruby.network.ext_out_link_utilization 3
system.ruby.network.int_link_utilization 9
system.ruby.network.routers0.buffer_reads 4
system.mem_ctrls0.readReqs 2
system.mem_ctrls0.writeReqs 1
system.mem_ctrls0.dram.readBursts 2
system.mem_ctrls0.dram.writeBursts 1
system.mem_ctrls0.dram.dramBytesRead 128
system.mem_ctrls0.dram.dramBytesWritten 64
---------- End Simulation Statistics ----------
"""
PROFILE = {
    "schema_version": 1,
    "qualification": "SYNTHETIC",
    "source": "unit test",
    "kinds": {
        "cpu_pipeline": {
            "event_j": {"commitStats0.numOps": 0.1},
            "leakage": {"reference_w": 0.5, "alpha_per_k": 0.01},
        }
    },
}


def rc_profile():
    return dict(
        schema_version=1,
        qualification="SYNTHETIC_RC",
        source="test",
        ambient_c=25,
        nodes=[
            dict(
                name="core0",
                initial_c=25,
                capacitance_j_per_k=0.5,
                ambient_resistance_k_per_w=2,
            )
        ],
    )


class PowerAnalysisTest(unittest.TestCase):
    def test_apollo_manifest_preserves_actual_input_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "m5out").mkdir()
            manifest = {
                "inputs": {"gem5": "/actual/gem5.opt"},
                "command": ["/actual/gem5.opt"],
                "gem5_revision": "test",
            }
            (run / "launch.json").write_text(json.dumps(manifest))
            (run / "result.json").write_text(json.dumps({"status": "PASS"}))
            (run / "m5out/exit.json").write_text(
                json.dumps({"status": "COMPLETED", "code": 0})
            )
            (run / "m5out/config.json").write_text(json.dumps(CONFIG))
            (run / "m5out/roi-events.json").write_text(json.dumps(EVENTS))
            (run / "m5out/stats.txt").write_text(STATS)
            _, _, summary = analyze_run(run, PROFILE)
            self.assertEqual(summary["original_manifest"], manifest)
            self.assertEqual(
                summary["sources"]["manifest"]["path"],
                str(run / "launch.json"),
            )
            self.assertFalse((run / "run_manifest.json").exists())
            (run / "run_manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "one source"):
                analyze_run(run, PROFILE)
            (run / "launch.json").unlink()
            self.assertEqual(
                analyze_run(run, PROFILE)[2]["original_manifest"], manifest
            )

    def activity(self):
        return extract_activity(CONFIG, STATS, EVENTS)

    def test_padded_paths_zero_suppression_and_owned_actors(self):
        row = self.activity()[0]
        actors = {a["actor"]: a for a in row["actors"]}
        self.assertEqual(row["seconds"], 1)
        self.assertEqual(actors["cpu0"]["events"]["commitStats0.numOps"], 20)
        self.assertEqual(actors["cpu0.l1d"]["events"]["numDataArrayReads"], 5)
        self.assertEqual(actors["cpu0.l1d"]["events"]["numDataArrayWrites"], 0)
        self.assertEqual(actors["dram0"]["events"]["dramBytesRead"], 128)
        self.assertIn("dmc_phy0", actors)
        self.assertEqual(len(actors), len(actor_definitions(CONFIG)))
        self.assertEqual(
            actors["cpu0"]["state_status"], "INVALID_INTERVAL_OR_UNDEFINED"
        )

    def test_missing_cpu_counter_not_zero(self):
        text = STATS.replace("system.roi_cpu0.commitStats0.numOps 20\n", "")
        rows = extract_activity(CONFIG, text, EVENTS)
        self.assertIsNone(
            rows[0]["actors"][0]["events"]["commitStats0.numOps"]
        )
        energy = estimate_energy(rows, PROFILE)
        self.assertIsNone(energy[0]["actors"][0]["total_j"])

    def test_active_o3_name_and_legacy_roi_name(self):
        config = copy.deepcopy(CONFIG)
        config["system"]["o3_cpu"] = config["system"].pop("roi_cpu")
        config["system"]["o3_cpu"][0]["path"] = "system.o3_cpu00"
        activity = extract_activity(
            config, STATS.replace("roi_cpu", "o3_cpu"), EVENTS
        )
        cpu = activity[0]["actors"][0]
        self.assertEqual(cpu["path"], "system.o3_cpu00")
        self.assertEqual(cpu["events"]["commitStats0.numOps"], 20)
        self.assertEqual(
            self.activity()[0]["actors"][0]["events"], cpu["events"]
        )

    def test_incomplete_and_overlap_intervals_rejected(self):
        for events in (
            EVENTS[:1],
            EVENTS[1:],
            EVENTS + EVENTS,
            [EVENTS[0], EVENTS[0], EVENTS[1]],
        ):
            with self.assertRaises(ValueError):
                roi_intervals(events)
        with self.assertRaisesRegex(ValueError, "exact reset-bounded"):
            extract_activity(
                CONFIG, STATS.replace("simTicks 1000", "simTicks 999"), EVENTS
            )

    def test_duplicate_dump_never_summed(self):
        rows = extract_activity(CONFIG, STATS + STATS, EVENTS)
        self.assertEqual(rows[0]["duplicate_dumps_ignored"], 1)
        self.assertEqual(
            rows[0]["actors"][0]["events"]["commitStats0.numOps"], 20
        )
        with self.assertRaisesRegex(ValueError, "conflicting"):
            extract_activity(
                CONFIG, STATS + STATS.replace("numOps 20", "numOps 21"), EVENTS
            )

    def test_energy_units_and_partial_coverage(self):
        result = estimate_energy(self.activity(), PROFILE)[0]
        cpu = result["actors"][0]
        self.assertEqual(cpu["dynamic_j"], 2)
        self.assertEqual(cpu["leakage_j"], 0.5)
        self.assertEqual(cpu["average_w"], 2.5)
        self.assertEqual(result["configured_subtotal_j"], 2.5)
        self.assertIsNone(result["total_j"])
        self.assertEqual(result["coverage"], "PARTIAL")
        self.assertEqual(result["phase"], "roi_begin")

    def test_external_qualification_is_never_promoted(self):
        profile = dict(PROFILE, qualification="CALIBRATED")
        row = dict(
            epoch=0,
            actor="dram0",
            begin_tick=100,
            end_tick=1100,
            total_j=1,
            source="external fixture",
            qualification="UNCALIBRATED",
        )
        for qualification in ("CALIBRATED", "UNCALIBRATED", "SYNTHETIC"):
            with self.subTest(qualification=qualification):
                row["qualification"] = qualification
                result = estimate_energy(self.activity(), profile, [row])[0]
                self.assertEqual(result["qualification"], qualification)
        for phase in ("warmup", "roi_begin"):
            activity = self.activity()
            activity[0]["phase"] = phase
            self.assertEqual(
                estimate_energy(activity, profile)[0]["phase"], phase
            )

    def test_external_energy_boundaries_and_double_ownership(self):
        row = dict(
            epoch=0,
            actor="dram0",
            begin_tick=100,
            end_tick=1100,
            total_j=0.000001,
            source="external fixture",
            qualification="SYNTHETIC",
        )
        result = estimate_energy(self.activity(), PROFILE, [row])[0]
        dram = next(a for a in result["actors"] if a["actor"] == "dram0")
        self.assertEqual(dram["total_j"], 1e-6)
        self.assertEqual(dram["average_w"], 1e-6)
        for invalid in (
            [row, row],
            [dict(row, begin_tick=0)],
            [dict(row, actor="unknown")],
        ):
            with self.assertRaises(ValueError):
                estimate_energy(self.activity(), PROFILE, invalid)
        with self.assertRaisesRegex(ValueError, "double owner"):
            estimate_energy(
                self.activity(), PROFILE, [dict(row, actor="cpu0")]
            )

    def test_voltage_squared_scaling_uses_interval_stat(self):
        config = copy.deepcopy(CONFIG)
        config["system"]["roi_cpu"][0]["clk_domain"] = "system.clock"
        config["system"]["clock"] = dict(
            path="system.clock", voltage_domain="system.voltage"
        )
        config["system"]["voltage"] = dict(path="system.voltage")
        activity = extract_activity(config, STATS + "", EVENTS)
        profile = copy.deepcopy(PROFILE)
        profile["kinds"]["cpu_pipeline"]["reference_voltage_v"] = 1
        self.assertIsNone(
            estimate_energy(activity, profile)[0]["actors"][0]["total_j"]
        )
        text = STATS.replace(
            "simFreq 1000",
            "simFreq 1000\nsystem.clock.clock 500\nsystem.voltage.voltage 0.5",
        )
        activity = extract_activity(config, text, EVENTS)
        self.assertEqual(activity[0]["actors"][0]["clock_period_ticks"], 500)
        row = estimate_energy(activity, profile)[0]["actors"][0]
        self.assertEqual(row["dynamic_j"], 0.5)
        self.assertEqual(row["leakage_j"], 0.5)
        self.assertEqual(row["dynamic_voltage_scale"], 0.25)

    def test_private_cache_uses_its_controller_voltage(self):
        config = copy.deepcopy(CONFIG)
        config["system"]["cpu"][0]["l1d"]["clk_domain"] = "system.clock"
        config["system"]["clock"] = dict(
            path="system.clock", voltage_domain="system.voltage"
        )
        config["system"]["voltage"] = dict(path="system.voltage")
        text = STATS.replace(
            "simFreq 1000",
            "simFreq 1000\nsystem.clock.clock 500\nsystem.voltage.voltage 0.5",
        )
        activity = extract_activity(config, text, EVENTS)
        profile = copy.deepcopy(PROFILE)
        profile["kinds"]["l1d"] = dict(
            reference_voltage_v=1,
            event_j={"numDataArrayReads": 1},
            leakage={"reference_w": 0},
        )
        energy = estimate_energy(activity, profile)[0]
        cache = next(a for a in energy["actors"] if a["actor"] == "cpu0.l1d")
        self.assertEqual(cache["dynamic_j"], 5 * 0.5**2)
        dsu = next(a for a in activity[0]["actors"] if a["actor"] == "dsu0")
        self.assertIsNone(dsu["voltage_v"])

    def test_state_leakage_requires_exact_residency_and_keeps_temperature_law(
        self,
    ):
        profile = copy.deepcopy(PROFILE)
        profile["kinds"]["cpu_pipeline"]["leakage"]["state_factors"] = {
            "ON": 1,
            "CLK_GATED": 0.2,
            "SRAM_RETENTION": 0.1,
            "OFF": 0,
        }
        activity = self.activity()
        cpu = activity[0]["actors"][0]
        self.assertIn(
            "state_residency",
            estimate_energy(activity, profile)[0]["actors"][0]["missing"],
        )
        cpu.update(
            state_status="VALID",
            state_ticks={
                "ON": 250,
                "CLK_GATED": 500,
                "OFF": 250,
            },
        )
        entry = estimate_energy(activity, profile)[0]["actors"][0]
        self.assertAlmostEqual(entry["leakage_j"], 0.5 * 0.35)
        self.assertAlmostEqual(
            leakage_power(entry["leakage"], 35), 0.5 * 0.35 * math.exp(0.1)
        )
        self.assertEqual(
            entry["leakage_state_assumption"], "RESIDENCY_WEIGHTED"
        )
        cpu["state_ticks"] = {"OFF": 1000}
        self.assertEqual(
            estimate_energy(activity, profile)[0]["actors"][0]["leakage_j"], 0
        )
        cpu["state_ticks"] = {"OFF": 1100}
        self.assertIsNone(
            estimate_energy(activity, profile)[0]["actors"][0]["total_j"]
        )

    def test_external_core_only_keeps_total_unknown(self):
        ext = dict(
            epoch=0,
            actor="dram0",
            begin_tick=100,
            end_tick=1100,
            total_j=1,
            qualification="SYNTHETIC",
            source="test core",
            backend_energy_scope="DRAM_CORE_ONLY_IO_UNCONFIGURED",
        )
        record = estimate_energy(self.activity(), PROFILE, [ext])[0]
        dram = next(a for a in record["actors"] if a["actor"] == "dram0")
        self.assertIsNone(dram["total_j"])
        self.assertEqual(dram["status"], "PARTIAL")
        self.assertEqual(dram["missing"], ["dram_io"])
        self.assertEqual(record["configured_subtotal_j"], 3.5)
        profile = assumed_profile(a["thermal_node"] for a in record["actors"])
        trace, result = replay(
            [record],
            [dict(epoch=0, seconds=0.01)],
            profile,
            allow_partial=True,
        )
        self.assertEqual(result["coverage"], "PARTIAL")
        self.assertGreater(trace[-1]["temperature_c"]["dram"], 25)

    def test_bad_coefficients_fail_closed(self):
        for value in (-1, float("nan"), "0.1", True):
            profile = copy.deepcopy(PROFILE)
            profile["kinds"]["cpu_pipeline"]["event_j"][
                "commitStats0.numOps"
            ] = value
            with self.assertRaises(ValueError):
                estimate_energy(self.activity(), profile)
        profile = copy.deepcopy(PROFILE)
        profile["kinds"]["cpu_pipeline"]["event_j"]["invented"] = 1
        with self.assertRaisesRegex(ValueError, "unknown event"):
            estimate_energy(self.activity(), profile)

    def test_temperature_leakage(self):
        spec = dict(reference_w=1, alpha_per_k=math.log(2) / 10)
        self.assertAlmostEqual(leakage_power(spec, 35), 2)
        self.assertAlmostEqual(leakage_power(spec, 15), 0.5)
        with self.assertRaisesRegex(ValueError, "runaway"):
            leakage_power(spec, 1e9)


class ThermalReplayTest(unittest.TestCase):
    def test_external_total_cannot_be_scaled_as_dynamic_energy(self):
        record = dict(
            epoch=0,
            seconds=1,
            actors=[
                dict(
                    actor="dram0",
                    thermal_node="core0",
                    total_j=1,
                    average_w=1,
                    backend="external",
                    status="SYNTHETIC",
                )
            ],
        )
        for scale in (0, 0.5, 2):
            with (
                self.subTest(scale=scale),
                self.assertRaisesRegex(ValueError, "separate idle epoch"),
            ):
                replay(
                    [record],
                    [dict(epoch=0, seconds=0.1, dynamic_scale=scale)],
                    rc_profile(),
                )
        trace, summary = replay(
            [record], [dict(epoch=0, seconds=0.1)], rc_profile()
        )
        self.assertAlmostEqual(trace[-1]["input_j"], 0.1)
        self.assertEqual(summary["coverage"], "COMPLETE_LEDGER")

    def test_analytic_heating_step_convergence_and_balance(self):
        exact = 25 + 2 * (1 - math.exp(-0.1))
        errors = []
        for step in (0.001, 0.0005):
            model = ThermalNetwork(rc_profile())
            for _ in range(round(0.1 / step)):
                result = model.step(step, {"core0": 1})
            errors.append(abs(result["temperature_c"]["core0"] - exact))
            self.assertLess(abs(result["balance_error_j"]), 1e-10)
            self.assertAlmostEqual(result["input_j"], 0.1)
        self.assertLess(errors[0], 0.0001)
        self.assertLess(errors[1], errors[0] * 0.51)

    def test_equilibrium_cooling_and_explicit_initial_temperature(self):
        profile = rc_profile()
        profile["nodes"][0]["initial_c"] = 27
        model = ThermalNetwork(profile)
        self.assertAlmostEqual(
            model.step(1, {"core0": 1})["temperature_c"]["core0"], 27
        )
        t = model.step(0.1, {"core0": 0})["temperature_c"]["core0"]
        self.assertGreater(t, 25)
        self.assertLess(t, 27)
        del profile["nodes"][0]["initial_c"]
        with self.assertRaises(KeyError):
            ThermalNetwork(profile)

    def test_two_node_energy_exchange_and_conservation(self):
        p = rc_profile()
        p["nodes"].append(
            dict(name="package", initial_c=25, capacitance_j_per_k=1)
        )
        p["edges"] = [dict(a="core0", b="package", resistance_k_per_w=1)]
        model = ThermalNetwork(p)
        for _ in range(20):
            result = model.step(0.01, {"core0": 2})
        self.assertGreater(result["temperature_c"]["package"], 25)
        self.assertLess(
            result["temperature_c"]["package"],
            result["temperature_c"]["core0"],
        )
        self.assertAlmostEqual(result["input_j"], 0.4)
        self.assertLess(abs(result["balance_error_j"]), 1e-10)

    def test_leakage_recomputed_at_each_temperature(self):
        models = [ThermalNetwork(rc_profile()) for _ in range(2)]
        for _ in range(50):
            for i, model in enumerate(models):
                model.step(
                    0.1,
                    {"core0": 1},
                    {"core0": [dict(reference_w=0.2, alpha_per_k=i * 0.1)]},
                )
        self.assertGreater(models[1].temp[0], models[0].temp[0])
        self.assertGreater(models[1].input_j, models[0].input_j)

    def test_restart_temperature_replays_same_state(self):
        a = ThermalNetwork(rc_profile())
        for _ in range(10):
            a.step(0.01, {"core0": 1})
        b = ThermalNetwork(rc_profile(), initial=dict(zip(a.names, a.temp)))
        for _ in range(10):
            a.step(0.01, {"core0": 1})
            b.step(0.01, {"core0": 1})
        self.assertEqual(a.temp, b.temp)

    def test_partial_energy_requires_explicit_permission(self):
        energy = estimate_energy(
            extract_activity(CONFIG, STATS, EVENTS), PROFILE
        )
        profile = assumed_profile(
            a["thermal_node"] for a in energy[0]["actors"]
        )
        schedule = [dict(epoch=0, seconds=0.01, dynamic_scale=0)]
        with self.assertRaisesRegex(ValueError, "partial replay forbidden"):
            replay(energy, schedule, profile)
        trace, summary = replay(energy, schedule, profile, allow_partial=True)
        self.assertEqual(summary["coverage"], "PARTIAL")
        self.assertGreater(trace[-1]["temperature_c"]["core0"], 25)
        self.assertAlmostEqual(summary["elapsed_s"], 0.01)

    def test_invalid_network_input_and_step_rejected(self):
        for value in (-1, 0, float("nan")):
            p = rc_profile()
            p["nodes"][0]["capacitance_j_per_k"] = value
            with self.assertRaises(ValueError):
                ThermalNetwork(p)
        m = ThermalNetwork(rc_profile())
        for value in (-1, 0, float("nan")):
            with self.assertRaises(ValueError):
                m.step(value, {"core0": 1})
        with self.assertRaisesRegex(ValueError, "unknown thermal power node"):
            m.step(1, {"other": 1})


if __name__ == "__main__":
    unittest.main()
