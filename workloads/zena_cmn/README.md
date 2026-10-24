# Zena CMN guest tests

These are model correctness/diagnostic workloads, not calibrated A720AE code.
Build from the workspace root:

```sh
gem5/workloads/zena_cmn/build.sh build/gem5-cmn/guest
CC=gcc gem5/workloads/zena_cmn/build.sh build/gem5-cmn/guest-host
build/gem5-cmn/guest-host/coherence_smoke --host --workers 8 --iterations 32
build/gem5-cmn/guest-host/memory_bench --host --mode read --workers 4 --passes 2
```

AArch64 builds use static C11/pthreads, `-O2 -march=armv8-a
-mno-outline-atomics`; atomic fetch-add uses LL/SC, with no LSE requirement.
`build-manifest.txt` records compiler, flags, source and binary SHA-256.
`--host` disables m5 instructions and maps worker IDs onto available host CPUs.
For ROI ID 0 it measures `CLOCK_MONOTONIC_RAW` elapsed time and emits
`CMN_HOST_ROI` JSON with `host:true` and `elapsed_ns`. Switch and nonzero ROI
markers do not start this timer. Printing occurs after the end timestamp.
Native execution measures that host's workload time; QEMU user-mode execution
only checks C logic. Neither establishes modeled coherence or Arm weak ordering.

By default the guest emits switch marker `work_begin(0x5a535743, 0)` before
allocation, first-touch and warm-up. For the coherency test only,
`--prefault-before-switch` allocates and touches the eviction buffer's pages
before the marker, reducing Linux page allocation/zeroing under O3. Full
line-by-line initialization/warm-up still occurs after switching, and the ROI
is unchanged. Atomic noncaching does not populate Ruby caches. The flag is
recorded as `prefault_before_switch_marker`. The harness rejects combining
this flag with direct timing boot or streaming benchmarks. This preparation
variant changes the pre-ROI execution trajectory; it does not tune memory
bandwidth.

Worker 0 emits `work_begin(0, 0)` before the measured barrier and
`work_end(0, 0)` after the final measured barrier. Verification and
`CMN_RESULT` JSON follow the ROI. Simulator-side ROI control owns stats reset,
dump and Atomic-to-O3 switching. Workload code never flushes/invalidates caches.

`coherence_smoke --workers 2|4|8|16 --iterations N` tests:

- Release/acquire dirty-owner publication from CPU 0 to CPU 1, then to the
  last CPU. Cross-cluster coverage requires at least 8 workers.
- Barrier-synchronized all-reader / single-writer invalidation.
- Contended atomic increments and distinct atomic counters sharing lines.
- 64 MiB aggregate cache-line footprint with exact post-ROI checksum of
  one 8-byte word in every aligned 64-byte line (1,048,576 distinct lines).
  Initialization, modification and readback use the same 64-byte stride.
  This dirties every line while avoiding eight redundant word operations per
  line; it tests sampled dirty-line preservation, not every byte of memory.
  O3 line initialization/warm-up is retained; only explicit pre-fault mode
  moves page materialization before the switch marker.
  `eviction_line_bytes`, `eviction_lines`, and `checked_bytes_per_line` make
  the coverage explicit. Progress messages before/after ROI identify first-touch,
  warm-up completion, and post-ROI verification without adding ROI console I/O.
  Explicit `--eviction-mib 1` is a fast coherence diagnostic; its JSON reports
  `large_eviction:false` and it does not qualify full-hierarchy eviction.
  The default remains 64 MiB; streaming array sizes are unaffected.
- Barrier-coordinated `mprotect(PROT_NONE)`, unmap and fixed-address remap,
  alternating two retained memfd-backed pages, followed by read-only access
  on every worker. Retaining both pages prevents physical-page reuse from
  masking a stale translation. Mapping updates have no concurrent dereference;
  this tests visibility after successful Linux calls,
  not deliberate permission faults or a userspace TLB invalidation opcode.

`memory_bench --mode read|copy|stream --workers 1|4|8|16 --passes 1|2|4|8`
keeps aggregate arrays fixed: read 256 MiB; copy two 128 MiB arrays; stream
three 128 MiB arrays. `stream` is the STREAM Triad operation (`a=b+3*c`),
not the upstream STREAM package or an official STREAM result. Each pinned
worker initializes and warms its partition; ROI includes a barrier per pass.
Allocation failure does not silently shrink arrays. Read accumulates doubles
and therefore includes a dependency-chain cost; it is not a peak LSU test.

Apollo's `gem5-apollo-guest` Yocto recipe installs this program as
`/usr/bin/apollo-memory-bench`. `--array-mib 64 --workers 16 --mode read
--kernel neon --passes 1 --initialize-before-switch` uses one 64 MiB array
split into sixteen disjoint 4 MiB partitions. Atomic initializes the array;
each pinned worker performs one O3 warmup pass before the measured pass. The
read warmup sum is checked and reported as `warmup_checksum`, preventing its
loads from being optimized away. Per-worker `DSB SY` completes warmup and
final measured accesses before the collective ROI boundaries. A post-ROI
barrier prevents worker teardown from starting before the end marker. These
barriers do not flush dirty cache lines to DRAM.

For a 12-core hardware board, `--host --workers 12` is also supported. The
fixed total array is split in whole 16-double NEON iterations; unequal worker
partitions cover every element exactly once. Other supported worker counts
keep their equal partitions. The default worker count remains 16, and gem5
mode still accepts only 1/4/8/16. On a 12-core board, for example:

```sh
./memory_bench --host --workers 12 --mode read --kernel neon \
  --array-mib 64 --passes 1 --initialize-before-switch
```

The host timer uses the same collective ROI boundaries as gem5 and excludes
allocation, initialization and warmup. Divide `useful_bytes` by the emitted
`elapsed_ns` for decimal GB/s. Pinning follows the process's allowed CPU set;
`affinity:first-available-workers` records this host-mode policy. CPU clocks,
prefetchers, physical channel mapping and cache topology remain hardware
properties and need separate capture for comparisons.

`--channel-phase-spread` adds a 64-byte offset between worker partitions;
this selects successive channels only for a 64-byte interleave. Leave it off
when comparing 64-, 512- and 1024-byte interleave with identical placement.

Useful bytes/pass are 256 MiB for read/copy and 384 MiB for Triad, counting
both input and output for copy/Triad. DRAM bytes can differ due to writeback,
cache state, page walks and OS activity. Store completion does not guarantee
all dirty data reached DRAM. No bandwidth target is asserted.

`gem5/scripts/zena_cmn/run_smoke.py` builds a disposable appended initramfs,
boots one bounded simulation, captures console/logs, requires guest correctness
and exact ROI stats, and records input hashes. It reuses the existing workspace
cpio helper. `--direct-timing` selects `run_fs.py --boot-mode=timing`; default
uses Atomic noncaching boot then switches before guest allocation, except for
the explicitly requested eviction-buffer pre-fault described above.
Optional `--disk-image` adds a post-ROI timing-mode 64 KiB write/sync/unmount/
read-only-remount/compare test through the board's COW disk. It leaves the base
image unchanged and does not include I/O time in the CPU ROI. `CMN_DISK=PASS`
is required only when that option is supplied. Missing Linux cache sysfs entries
are explicitly reported as unavailable, not proof of topology correctness.

```sh
python3 gem5/scripts/zena_cmn/summarize_roi.py \
  --stats RUN/m5out/stats.txt --guest RUN/guest_console.log \
  --events RUN/m5out/roi-events.json --output RUN/roi_summary.json
python3 -m unittest discover -s gem5/tests/zena_cmn -p test_roi.py
```

The parser selects the dump with exactly matching begin/end ticks and ignores
identical duplicate dumps, instead of summing them. Missing counters remain
JSON null with availability metadata. Native counters retain names/units;
arrival-sampled histograms are not converted to time-weighted occupancy.

Native CHI TBE pools currently repeat `avg_size`, `avg_util` and
`avg_reserved` names within a controller. Such values are null and listed in
`ambiguous_counters`; no last-pool value is silently accepted. Native oneline
histograms retain their raw representation and are not interpreted as scalars.

## Bounded cache-aware Roofline probe

`roofline_probe` pins one worker to CPU 0, initializes all buffers before the
Atomic-to-O3 switch, then measures register FP32 FMA and five memory candidates.
It emits 26 `ROOF_CASE` records with checksum and useful-byte/FLOP counts.
Cache warmup and correctness checks are outside each ROI. `--l2-check` instead
runs two L2-only cases (IDs 12/13), each traversing the same 256 KiB buffer twice.
`--host` checks arithmetic only. Normal guest completion uses `m5_exit`, not
Linux poweroff. Neither mode changes cache, CPU queue or DRAM parameters.

Use `scripts/zena_cmn/run_roofline.py` with the existing FS assets and a new
output directory; analyze it with `scripts/zena_cmn/analyze_roofline.py`.
The latter needs matplotlib only when rendering plots (`--no-plot` skips it).
Read roofs require both residency checks and <=5% sample spread. Cold DRAM
is diagnostic only; useful-byte AI is not cache-boundary transferred-byte AI.
See the workspace [result report](../../../doc/gem5/roofline_20min_results_ko.md)
for exact commands, hardware differences, figures and measurements.

## CPU/cache calibration diagnostics

`calibration_probe` is a separate, single-worker diagnostic. Build it with the
same `build.sh`; `calibration_probe --host` checks expected arithmetic and ring
construction without issuing m5 instructions. All buffers are initialized under
Atomic noncaching before switching to O3. The topology is not changed.

| ID | Diagnostic | Measured work |
|---|---|---|
| 101 | Eight independent FP32 NEON FMA accumulators | 16,384 FMA instructions |
| 102 | One dependent FP32 NEON FMA accumulator | 16,384 FMA instructions |
| 103 | One dependent FP32 NEON FADD accumulator | 16,384 FADD instructions |
| 104 | Independent `LDR Q` load-only, 32 KiB | 8,192 vector loads / 128 KiB |
| 105 | Random pointer chase, 32 KiB candidate L1 | 4,096 dependent loads |
| 106 | Random pointer chase, 256 KiB candidate L2 | 4,096 dependent loads |
| 107 | Random pointer chase, 2 MiB candidate L3 | 1,024 dependent loads |
| 108 | Random pointer chase, 4 MiB cold candidate DRAM | 1,024 dependent loads |

Case 108 runs before 105–107. Each warm pointer case traverses its entire ring
once outside the ROI. Rings use a deterministic shuffled permutation of 64-byte
lines, one pointer per line, and validate the complete cycle before switching.
The cold case has no O3 ring warmup. Full working-set sizes are candidates, not
proof of the serving cache; use the matching cache/controller statistics.

`ROOF_CASE` retains the Roofline runner fields and adds `instructions`
(target arithmetic/load instructions, excluding loop/checksum instructions),
`dependent_loads`, and `warmup_loads`. Pointer useful bytes count eight bytes
per dereference, not 64-byte fills or DRAM bursts. Algorithmic FLOPs exclude
checksum reduction. Eight FP32 FLOPs per FMA instruction means four lanes times
two operations; FADD counts four. Load-only checks the final eight vectors after
executing every volatile inline-assembly load; it does not checksum all data.
All measured loops include their loop and function overhead. Pointer latency
therefore estimates the complete load-to-use path plus loop overhead, including
translation and any OS interruption; it is not an isolated cache RAM latency.
The dependent FMA case follows the accumulator dependency specifically.

Normal completion prints `ROOF_DONE=PASS` and exits with the native m5 exit
instruction. This does not test Linux shutdown, hardware latency, SLC residency,
store completion, prefetch quality, or multicore saturation. Host results only
validate functional checks; AArch64 disassembly and actual simulation are
separate evidence.

The independent FMA loop is explicitly aligned to a 64-byte fetch line and
contains eight FMA instructions plus decrement/branch within that line. This
prevents a compiler-dependent branch at a fetch-line boundary from dominating
an intended FU-throughput test. Earlier calibration source snapshots lack this
alignment; compare baseline/profile using the same rebuilt binary. Dependence
chains remain separate tests and are not expected to reach independent peak.

`calibration_probe --memory-patterns` runs seven alternative ROIs (201–207),
without the default eight cases. IDs 201–203 traverse deterministic 1 MiB rings
in sequential-line, stride-17-line and shuffled-random order. All three use the
same dependent-load instruction PC, one full traversal for O3 warmup followed
by one full measured traversal. Each reads 16,384 pointers (128 KiB useful,
1 MiB distinct cache-line footprint). The 1 MiB working set is an L3-backed
prefetch diagnostic candidate, not DRAM saturation or confirmed residency.

IDs 204–207 split a fixed 256 KiB aggregate into 1/2/4/8 disjoint shuffled
rings. Each performs 4,096 total pointer loads (32 KiB useful), interleaving
independent pointer chains with explicit inline assembly. One full traversal
warms every ring before measurement. Every final pointer must equal its ring's
start; the checksum counts individually verified rings. `independent_streams`
is explicit metadata. Per-stream partition size and order vary with stream
count; cache/bank/translation effects are part of this MLP diagnostic.
The PC-stable prefetch test still has address dependence, and the MLP test is
L2-resident candidate traffic, not a measurement of peak external bandwidth.

Compare identical binaries with prefetch off/on, checking useful throughput,
actual L2/L3 hit and DRAM traffic, prefetch outcomes and queue pressure together.
Do not interpret an improvement in sequential pointers as a model of Arm's
undisclosed prefetch algorithms. Host validation:

```sh
build/gem5-cmn/guest-host/calibration_probe --host --memory-patterns
```

`calibration_probe --load-streams` is an alternative 15-ROI diagnostic
(301–315): L1/L2/L3 candidates, each with 1/2/4/8/16 independent scalar
load streams. Working sets are 32 KiB, 256 KiB and 2 MiB, initialized before
O3 and warmed once per level. Each ROI executes 2,048 explicit 8-byte loads
(16 KiB useful). L1 revisits its 512 lines four times; L2 alternates 128 KiB
windows; L3 uses five disjoint 128 KiB windows so preceding ROIs do not warm
its measured lines into the private L2. Streams partition the measured window.
The analyzer requires prefetch disabled and reports actual cache residency.

The loaded value does not determine the next address. One volatile inline
assembly load PC serves every stream count; the final value is checked against
one, not a checksum of every access. Address arithmetic, branch and function
cost are included. Outer-loop iterations decrease with stream count, so this
comparison changes loop overhead as well as address interleaving; it does not
isolate outstanding capacity or establish peak LSU throughput. Cache-line
traffic can be eight times the useful 8-byte-per-line traffic. Compare the
same binary and actual hit/miss counters across profiles, not only GB/s.

`roofline_probe --slc-check` runs only IDs 22–26. A 1 MiB buffer is
initialized in Atomic mode. After O3 switch, the task migrates to CPU4 and
CPU8 to read each disjoint 128 KiB window, establishing clean sharing in two
DSUs, then migrates to CPU0 for its measured read/FMA ROI. These are sequential
warmup roles, not concurrent helper threads. CPU0 does not read a window
before its ROI. Host mode skips affinity migration and validates arithmetic
only. Default 26 cases and `--l2-check` are unchanged.

Use prefetch off. The analyzer additionally reports HNF hits, benchmark L3
misses, HNF snoop messages, peer-DSU data messages and DRAM read bytes.
`QUALIFIED_AGGREGATE_SLC` requires >=90% unique-line hit/miss coverage and
exactly zero snoops, peer data messages and DRAM reads. Native zero-suppressed
MessageBuffer counters are treated as zero only for configured buffer paths.
Shared counters include instruction/OS traffic: this conservative check has
no noise tolerance and is not physical-address-filtered silicon evidence.

The SLC check also runs the same kernel on a separate 4 KiB dummy buffer on
CPU0 after each migration back, before its ROI. This warms read/FMA instruction
paths and stack/control state, and writes/reads `observed` on CPU0. In the
initial pilot ELF, `observed` and the markers' `host` flag occupied the same
cache line, previously written on CPU8. A `dsb ish` completes this CPU0 setup;
it neither flushes caches nor warms the measured target. Initial and revised
pilot artifacts remain separate so source qualification is not retrospectively
assigned to contaminated measurements.

`run_roofline.py --slc-check --slc-trace` enables native `ProtocolTrace` and
`TLBVerbose` only between ROI markers, writing `m5out/roi-trace.log`. The guest
logs each target's virtual range and allocation span. The supplied kernel has
`CONFIG_PROC_PAGE_MONITOR` disabled, so this diagnostic uses native CPU0 data
TLB VA-to-PA observations instead of `/proc/self/pagemap`.

After normal Roofline analysis, run `scripts/zena_cmn/analyze_slc_trace.py RUN`.
It requires complete unambiguous target-line mapping, exact HNF profiled
hit/miss counter reconciliation against the trace, HNF hits and upstream L3
misses for every target line, and no target snoop, downstream read or peer data
transition. It writes a separate `slc-target-attribution.json`; the aggregate
strict source status and Roofline qualification are never replaced. Outside
addresses remain listed for attribution. CHI state/event profiling rules are
read from the generated native `Cache_Transitions.cc`, with its hash recorded.
