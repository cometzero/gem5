# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Partial Micron Y52Q -023 channel proxy, not a calibrated Zena memory part.

Micron 561b x64 Automotive LPDDR5X, Rev. G 05/2025: pp. 1-2 speed bin,
page 10 Tables 2/3 channel geometry; page 11 Table 4 all-bank refresh.
The device-specific sheet delegates general array/interface timings to
separate specifications. Unlisted timings retain the existing LPDDR5-derived
8533 model; neither temperature-specific refresh nor power is calibrated.
"""

from example.arm.zena_css_models import LPDDR5X_8533_1x16_BG_BL32

SOURCE_URL = (
    "https://mm.digikey.com/Volume0/opasdata/d220001/medias/docus/8885/"
    "561b-y52q-ddp-auto-lpddr5x.pdf"
)
PART = "MT62F512M64D2CZ-023 AAT:E"
QUALIFICATION = "PARTIAL_DATASHEET_GEOMETRY_REFRESH_NOT_TARGET_SILICON"
DOCUMENTED_PARAMETERS = (
    "device_bus_width",
    "devices_per_rank",
    "device_size",
    "ranks_per_channel",
    "banks_per_rank",
    "bank_groups_per_rank",
    "device_rowbuffer_size",
    "tRFC",
)
INHERITED_UNQUALIFIED_TIMINGS = (
    "tAAD",
    "tBURST",
    "tBURST_MIN",
    "tBURST_MAX",
    "tCCD_L",
    "tCCD_L_WR",
    "tCK",
    "tCL",
    "tCS",
    "tCWL",
    "tPPD",
    "tRAS",
    "tRCD",
    "tRCD_WR",
    "tREFI",
    "tRP",
    "tRRD",
    "tRRD_L",
    "tRTP",
    "tRTW",
    "tWR",
    "tWTR",
    "tWTR_L",
    "tXAW",
    "tXP",
    "tXPDLL",
    "tXS",
    "tXSDLL",
)


class MicronY52Q_8533_1x16_BG_BL32_Partial(LPDDR5X_8533_1x16_BG_BL32):
    """One 8-Gbit channel; four channels comprise the 32-Gbit package.

    BL32 is a supported mode, retained from the existing native model;
    the device's native prefetch/burst organization is BL16. All nonlisted
    parameters, including CK-derived burst timing, remain inherited.
    """

    device_bus_width = 16
    devices_per_rank = 1
    device_size = "1GiB"
    ranks_per_channel = 1
    banks_per_rank = 16
    bank_groups_per_rank = 4
    device_rowbuffer_size = "2KiB"
    tRFC = "210ns"
