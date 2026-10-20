# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Documented Hynix channel candidate; not Zena silicon calibration.

PDF pages are 1-based. Public mirror carries SK hynix Confidential marking.
CK is rounded upward to 938 ps, within the table's 1066.5 MHz upper bound.
Analog minima are rounded upward to whole CK; no tuned controller queues.
"""

from example.arm.zena_css_models import LPDDR5X_8533_1x16_BG_BL32

SOURCE_URL = (
    "https://www.uttc.com.tw/wp-content/uploads/2026/02/"
    "H58G56CK8BX146_Rev1.0-3.pdf"
)
PART = "H58G56CK8BX146"
QUALIFICATION = "DOCUMENTED_MODE_CANDIDATE_NOT_TARGET_SILICON"
MODE = {
    "CK_ps": 938,
    "WCK_CK_ratio": 4,
    "RL_set": 0,
    "WL_set": "A",
    "DVFSC": False,
    "enhanced_DVFSC": False,
    "link_ECC": False,
    "read_DBI_data_copy_byte_mode": False,
    "RDQS": True,
    "DQ_ODT_NT_ODT_DFE_training": False,
    "WCK_frequency_mode": "HF",
    "refresh": "1x, Tcase <= 85 C",
}


def ck(cycles):
    return f"{cycles * 938}ps"


# Each entry records the native value and the document-to-model derivation.
PARAMETER_PROVENANCE = {
    "device_bus_width": (16, "p3 x16"),
    "devices_per_rank": (1, "p3 one device/channel"),
    "device_size": ("2GiB", "p3 4GiB package / 2 channels"),
    "ranks_per_channel": (1, "p3 1CS"),
    "banks_per_rank": (16, "p22 Table6"),
    "bank_groups_per_rank": (4, "p22 Table6"),
    "device_rowbuffer_size": ("2KiB", "p22 Table6"),
    "tCK": (ck(1), "p269 max CK1066.5MHz; ceiling to ps"),
    "tCL": (ck(23), "p269 Table225 RLset0"),
    "tCWL": (ck(12), "p273 Table229 WLsetA"),
    "tRCD": (ck(20), "p532 Table396 ceil(18ns/CK)"),
    "tRCD_WR": (ck(9), "p532 Table396 ceil(8ns/CK)"),
    "tRP": (ck(20), "p532 Table396 per-bank ceil(18ns/CK)"),
    "tRAS": (ck(45), "p532 Table396 ceil(42ns/CK)"),
    "tWR": (
        ck(38),
        "Table340 native readyTime excludes +1CK; 1+ceil(34ns/CK)",
    ),
    "tRRD": (ck(4), "p532 Table396 ceil(3.75ns/CK)"),
    "tRRD_L": (ck(4), "p532 Table396 same tRRD for all banks"),
    "tXAW": (ck(16), "p532 Table396 ceil(15ns/CK)"),
    "activation_limit": (4, "p532 Table396 four ACT window"),
    "tWTR": (ck(7), "p532 Table396 ceil(6.25ns/CK)"),
    "tWTR_L": (ck(13), "p532 Table396 ceil(12ns/CK)"),
    "tPPD": (ck(2), "p532 Table396"),
    "tAAD": (ck(8), "section7.3.1 ACT-1 to ACT-2 maximum gap 8CK"),
    "tBURST": (ck(6), "section8.1 BG BL32 effective burst min=6CK"),
    "tBURST_MIN": (ck(2), "section8.1 interleaved start=2CK"),
    "tBURST_MAX": (ck(8), "section8.1 effective burst max=8CK"),
    "tCCD_L": (ck(8), "section8.1 same BG start=8CK"),
    "tCCD_L_WR": (ck(8), "section8.1 same BG start=8CK"),
    "tRTW": (ck(13), "Tables347/348/462: RL23-WL12+ceil(1600ps/CK)"),
    "tRTP": (ck(12), "Table340: BL/n_min6+ceil(tRBTP/CK)6"),
    "tRFC": ("280ns", "p295 Table240 16Gb/channel"),
    "tREFI": ("3.906us", "p295 Table240 1x refresh"),
    "tXS": ("287.5ns", "p305 Table242 tRFC+7.5ns"),
}
DOCUMENTED_PARAMETERS = tuple(PARAMETER_PROVENANCE)
INHERITED_UNQUALIFIED_TIMINGS = (
    "tCS",
    "tXP",
    "tXPDLL",
    "tXSDLL",
)


class HynixH58G56CK8BX146_8533_1x16_BG_BL32_Documented(
    LPDDR5X_8533_1x16_BG_BL32
):
    """Single channel, fixed documented mode; PHY/power/refresh policy partial.

    Native tRP cannot distinguish all-bank from per-bank precharge. tRTP
    is command-relative, unlike datasheet burst-end-relative tRBTP.
    Remaining inherited timings and native WCK resync are not qualified.
    """

    for _parameter, (_value, _source) in PARAMETER_PROVENANCE.items():
        locals()[_parameter] = _value
    del _parameter, _value, _source
