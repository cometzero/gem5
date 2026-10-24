# SPDX-License-Identifier: BSD-3-Clause
"""Externally supplied zero-order-held power, with independent energy state."""

from m5.objects.ClockedObject import ClockedObject
from m5.objects.PowerModelState import PowerModelState
from m5.params import *
from m5.SimObject import PyBindMethod


class LatchedPowerModel(PowerModelState):
    type = "LatchedPowerModel"
    cxx_header = "sim/power/latched_power_model.hh"
    cxx_class = "gem5::LatchedPowerModel"
    cxx_exports = [
        PyBindMethod("setPower"),
        PyBindMethod("getEnergyJ"),
        PyBindMethod("setAccountingEnabled"),
    ]
    dynamic_power = Param.Float(0.0, "Initial dynamic power in watts")
    static_power = Param.Float(0.0, "Initial static power in watts")


class PowerActor(ClockedObject):
    """Clocked ledger entry for externally computed aggregate power."""

    type = "PowerActor"
    cxx_header = "sim/power/latched_power_model.hh"
    cxx_class = "gem5::PowerActor"
    abstract = False
