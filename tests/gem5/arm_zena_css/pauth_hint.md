# PAuth HINT destination preservation

`pauth_hint_probe.S` executes all 13 PAC/AUT/XPACLRI HINT encodings on a
CPU without `FEAT_PAuth`. Each instruction must leave its destination
unchanged. The program uses Linux syscalls directly and needs no C library,
dynamic loader, Linux kernel image, board model, or CPU takeover in SE mode.

Run from the gem5 checkout after building an ARM-capable simulator:

```sh
python3 tests/gem5/arm_zena_css/run_pauth_hint.py \
    --gem5 build/ARM/gem5.opt --output /tmp/pauth-hint-test
```

The output directory must be new. `--cc` selects an AArch64 Linux compiler;
the default is `aarch64-linux-gnu-gcc`. The runner compiles the probe and
runs AtomicSimpleCPU, TimingSimpleCPU, and O3CPU separately with `Armv8`
features, which exclude PAuth. Use `--cpu o3` for a single model.

Success requires exit status zero and this exact guest line for every model:

```text
PAUTH_HINT_NO_PAUTH cases=13 failures=0x0000
```

Failure-mask bits 0 through 12 correspond to `pacia1716`, `paciasp`,
`paciaz`, `pacib1716`, `pacibsp`, `pacibz`, `autia1716`, `autiasp`,
`autiaz`, `autib1716`, `autibsp`, `autibz`, and `xpaclri`.
`result.json` records simulator/probe hashes, per-model exit statuses,
and the combined result. Each model directory contains its command and log.

This test catches a disabled HINT returning before the normal destination
writeback: an O3 renamed destination must receive the original source value
even though the architectural operation is a no-op. It does not qualify
PAuth cryptography, key handling, timing accuracy, or full-system CPU
switching; those require separate tests.
