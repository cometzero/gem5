# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Check positive result and the deliberately hidden-write negative oracle."""
import argparse
import json
import re
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--positive", type=Path, required=True)
parser.add_argument("--negative", type=Path, required=True)
parser.add_argument("--negative-log", type=Path, required=True)
args = parser.parse_args()
result = json.loads((args.positive / "bytecheck-result.json").read_text())
assert result["status"] == "PASS"
assert result["instruction_reads_checked"] == 16
assert result["read_bytes_checked"] == 1024
assert result["instruction_request_flag"] == "INST_FETCH"
assert not (args.negative / "bytecheck-result.json").exists()
log = args.negative_log.read_text()
assert "system.monitors2: read of 0x10000" in log, "not the I-side monitor"
assert "MemChecker violation!" in log, "not a payload checker failure"
mismatch = re.search(
    r"Read transaction for address 0x10000 failed: "
    r"received (0x[0-9a-f]+), expected (0x[0-9a-f]+)",
    log,
)
assert mismatch and mismatch[1] != mismatch[2], "missing unequal byte values"
print(
    json.dumps(
        {
            "status": "PASS",
            "positive": str(args.positive),
            "negative": str(args.negative),
            "oracle": "I-side monitor detected hidden-write mismatch",
        }
    )
)
