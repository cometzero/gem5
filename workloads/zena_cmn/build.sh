#!/usr/bin/env bash
set -euo pipefail
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
output=${1:-"$source_dir/../../build/zena-cmn/guest"}
mkdir -p -- "$output"
compiler=${CC:-aarch64-linux-gnu-gcc}
flags=(-std=c11 -O2 -Wall -Wextra -Werror -pthread -static)
if [[ $($compiler -dumpmachine) == aarch64* ]]; then
    flags+=(-march=armv8-a -mno-outline-atomics)
fi
for program in coherence_smoke memory_bench roofline_probe calibration_probe; do
    "$compiler" "${flags[@]}" "$source_dir/$program.c" -o "$output/$program"
done
{
    "$compiler" --version | head -n 1
    printf '%q ' "$compiler" "${flags[@]}"
    printf '\n'
    sha256sum "$source_dir"/{common.h,coherence_smoke.c,memory_bench.c,roofline_probe.c,calibration_probe.c} \
        "$output"/{coherence_smoke,memory_bench,roofline_probe,calibration_probe}
} > "$output/build-manifest.txt"
