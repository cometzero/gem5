# Offline power and thermal profiles

These JSON profiles do not change simulation timing. `unconfigured.json`
contains no measured electrical coefficients; every missing actor remains
`UNCONFIGURED` with null energy. `synthetic-functional.json` contains fabricated
coefficients solely to exercise units, actor accounting, and thermal feedback.
It is not an estimate of A720AE, CMN, or LPDDR5X power.

From the workspace root:

```sh
python3 hsoc-stack/tools/gem5/scripts/zena_cmn/analyze_power.py \
  --run build/gem5-zena/calibration-hynix-linux \
  --profile hsoc-stack/tools/gem5/configs/zena_cmn/profiles/power/synthetic-functional.json \
  --output build/gem5-power/offline/synthetic
```

The source run must have `launch.json` (or reference `run_manifest.json`), passing `result.json`, and
`m5out/{config.json,stats.txt,roi-events.json,exit.json}`. The analyzer records
input hashes and the original manifest. This is reanalysis of recorded model
activity, not a new timing run. Each epoch must match an exact reset-bounded
statistics dump; duplicate dumps are never summed. Only known source-level
`nozero` counters are reconstructed as zero. CPU statistics use the ROI CPU
when Apollo `roi_cpu` (or reference `o3_cpu`) is present; this assumes the runner switched to that CPU at ROI
entry. State residency is retained but never silently corrected or converted
to ON time. Old binaries can include pre-ROI history; the native reset fix
starts a new residency window without changing power state. Default static
energy uses an explicit constant-all-states assumption.

`activity.jsonl` stores epoch, phase, case, start/end ticks, frequency, seconds and
actor event counts with native counter names. Ownership separates CPU pipeline,
L1I/L1D/L2 arrays, DSU/L3, SLC, routers, traversed network flits, DMC/PHY, external
DRAM and an aggregate I/O proxy. A complete ledger means these declared owners
were computed; it does not certify exhaustive silicon component coverage.

Coefficient schema version 1 has `qualification` (`SYNTHETIC`, `UNCALIBRATED`,
`CALIBRATED`), `source`, optional `temperature_c` (25 by default), `kinds`, and
optional per-instance `actors` overriding the kind. Each entry requires:

```json
{
  "event_j": {"commitStats0.numOps": 1e-10},
  "leakage": {"reference_w": 0.01, "reference_c": 25, "alpha_per_k": 0.01},
  "state_assumption": "SYNTHETIC_CONSTANT_ALL_STATES"
}
```

Dynamic energy is sum(count × joules/event). Leakage W is
`reference_w * exp(alpha_per_k * (temperature_c - reference_c))` and is
integrated over seconds. Coefficients apply to the source run's fixed OPP;
there is no implicit voltage scaling or A720AE microarchitecture inference.
`energy.jsonl` contains actor J/W and a configured subtotal. The total is null
when any declared actor is unconfigured. `power-summary.json` retains sources,
qualification, and limitations. No package-level mW is inferred from SCMI cost.
Record and summary qualification retain the least qualified contributing
profile/actor: `SYNTHETIC`, then `UNCALIBRATED`, then `CALIBRATED`.
Filter online ROI energy by phase and exact tick boundaries, not case ID alone.

Optional `leakage.state_factors` maps `ON`, `CLK_GATED`, `SRAM_RETENTION` and
`OFF` to nonnegative factors on `reference_w`. Only states occupied in the
interval require factors. The analyzer requires valid native residency whose
sum equals the exact interval; otherwise the actor remains unconfigured.
It weights the leakage reference once and retains the temperature law for
both online integration and offline replay. Factors need their own electrical
basis; none are inferred from a CPU being idle. The default profile does not
invent state savings.

External DRAM energy can be supplied as `--external-energy FILE.jsonl`, with
one row per actor and epoch:

```json
{"epoch":0,"actor":"dram0","begin_tick":100,"end_tick":1100,"total_j":1e-6,"qualification":"SYNTHETIC","source":"pinned backend + electrical profile"}
```

Remove the corresponding DRAM event model from the coefficient profile.
External energy with duplicate ownership, unmatched boundaries, unknown actors,
or absent provenance is rejected. The external backend must convert to J
before this interface and must integrate all command/background terms over
the exact supplied epoch.

For thermal replay, write an explicit schedule, for example:

```json
[{"epoch":0,"seconds":0.1,"dynamic_scale":1},
 {"epoch":0,"seconds":0.1,"dynamic_scale":0}]
```

```sh
python3 hsoc-stack/tools/gem5/scripts/zena_cmn/thermal_replay.py \
  --energy build/gem5-power/offline/synthetic/energy.jsonl \
  --schedule build/gem5-power/offline/schedule.json \
  --step-s 0.001 --output build/gem5-power/offline/thermal
```

The second phase suppresses dynamic activity while keeping leakage. This is
an explicit duty-cycle scenario, not measured idle behavior. Without a profile,
the tool saves an `ASSUMED_RC` profile linking each core+private-cache, DSU,
fabric, DMC/PHY, DRAM, I/O node to a passive package. It does not derive a
floorplan from logical mesh coordinates. A supplied `--profile` defines named
nodes, `capacitance_j_per_k`, `initial_c`, optional
`ambient_resistance_k_per_w`, and edges with `a`, `b`,
`resistance_k_per_w`, plus `ambient_c`, `qualification`, and `source`.
All nodes require explicit initial temperature. Backward Euler solves the
passive network and iterates temperature-dependent leakage at each step.
`input_j = stored_j + ambient_j` is checked at every step. Partial actor
coverage is rejected unless explicitly permitted with `--allow-partial`.

Output includes `thermal-profile.json`, `thermal-summary.json`, and
`temperature.csv`. Compare step and half-step runs before using results.
External-backend energy is replayed as fixed average power; its static/dynamic
split and temperature response are not inferred. Such epochs require
`dynamic_scale=1`: use a separate measured idle epoch for duty-cycle replay.
Scaling the entire external total would incorrectly scale background energy.
Offline temperature does not
change performance, refresh, OPP, or guest policy; those require online timing
runs.

Focused validation:

```sh
PYTHONPATH=hsoc-stack/tools/gem5/scripts/zena_cmn python3 -m unittest discover \
  -s hsoc-stack/tools/gem5/tests/zena_cmn -p test_power_analysis.py -v
```

Clocked actors may optionally specify `reference_voltage_v`. The analyzer then
reads the completed interval's native voltage-domain statistic and scales
dynamic joules/event by `(voltage_v / reference_voltage_v)^2`. Missing voltage
keeps that actor unconfigured. Clock period is retained as metadata; there is
no extra frequency multiplier because measured event count and interval
already determine event rate. CPU and private-cache actors use their actual
controller clock/voltage domain. DSU/fabric domains are resolved separately.
The synthetic profile uses 1 V for CPU and private-cache reference voltage.
This is an explicit ideal V-squared assumption, not calibrated DVFS power.

Online output keeps `phase` in the energy ledger. Native power statistics
written before the retrospective calculation describe the previous latch;
`native_power_stats_epoch` identifies that epoch in activity output.
Use `online-energy.jsonl` for current interval J/W. `online-power-summary.json`
records host time for dump/parse/accounting/RC/output and timing-phase costs.
Those costs cover this process's measured intervals, not saved host history.

A DRAM external row with
`backend_energy_scope: DRAM_CORE_ONLY_IO_UNCONFIGURED` remains `PARTIAL`:
`total_j` and `average_w` stay null; `configured_subtotal_j` and
`average_configured_w` retain known core energy. A partial thermal replay uses
that known heat only when explicitly permitted and records missing DRAM I/O.

## Apollo integration boundary

Use the workspace `run_gem5_linux.sh` launcher and Yocto artifacts. See
`doc/gem5/power-model-guide.md` in the workspace for CLI, kernel requirements,
provenance and validation. Online runs use reset-bounded ledgers and the
root collector, not the offline single-block ROI command above.

The external DRAMPower bridge and AP hot-checkpoint runners from the reference
checkout are not integrated here. Apollo supports Atomic boot checkpoints
with matching power/thermal inputs. Native thermal serialization tests are
not AP hot-restore qualification. LPDDR5X command energy and DRAM I/O/WCK
calibration remain separate work; synthetic event coefficients do not fill
those electrical gaps.
