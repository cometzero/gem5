#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Run the NPU Linux checks using the common bounded full-system runner."""
import sys

from run_smoke import main

if __name__ == "__main__":
    sys.argv[1:1] = ["--mode", "npu"]
    raise SystemExit(main())
