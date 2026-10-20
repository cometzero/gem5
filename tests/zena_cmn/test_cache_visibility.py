# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
import sys
import unittest
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "scripts/zena_cmn")
)
from check_cache_visibility import (
    arm64_populated_leaves,
    source_clidr,
)


class CacheVisibilityTest(unittest.TestCase):
    def test_current_native_clidr_leaves_hole(self):
        self.assertEqual(
            arm64_populated_leaves(0x00200000, 3, 4),
            [(1, 0), (2, 0), (3, 0), (0, 0)],
        )

    def test_split_l1_accounts_for_four_leaves(self):
        self.assertEqual(
            arm64_populated_leaves(0x123, 3, 4),
            [(1, 2), (1, 1), (2, 4), (3, 4)],
        )

    def test_source_change_fails_closed(self):
        with self.assertRaises(ValueError):
            source_clidr(
                "case MISCREG_CLIDR: return params.clidr; case MISCREG_CCSIDR:"
            )


if __name__ == "__main__":
    unittest.main()
