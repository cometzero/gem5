#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$repo"
# Retain the project's configured protocol set and ISA build.
if [[ ! -f build/ARM/gem5.build/config ]]; then
    echo 'Configure build/ARM with ARM and CHI before running this script.' >&2
    exit 1
fi
exec scons build/ARM/gem5.opt -j"${JOBS:-2}"
