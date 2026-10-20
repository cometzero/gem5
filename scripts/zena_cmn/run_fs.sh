#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
workspace=$(dirname -- "$repo")
exec "$repo/build/ARM/gem5.opt" --listener-mode=on \
    --outdir="${CMN_OUTDIR:-$workspace/build/gem5-zena/cmn-interactive}" \
    "$repo/configs/zena_cmn/run_fs.py" \
    --kernel="$workspace/build/gem5-zena/linux/vmlinux" \
    --bootloader="$workspace/build/gem5-zena/firmware/zena_css16.elf" \
    --initrd="$workspace/build/buildroot/arm64/images/rootfs.cpio" "$@"
