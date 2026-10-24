/* SPDX-License-Identifier: BSD-3-Clause */
#include "sim/power/latched_power_model.hh"

#include <cmath>
#include <limits>

#include "sim/core.hh"

namespace gem5
{

LatchedPowerModel::LatchedPowerModel(const Params &p)
    : PowerModelState(p), dynamicW(p.dynamic_power), staticW(p.static_power)
{
    fatal_if(!std::isfinite(dynamicW) || dynamicW < 0 ||
                 !std::isfinite(staticW) || staticW < 0,
             "Invalid latched power");
}

double
LatchedPowerModel::getEnergyJ() const
{
    if (!accountingEnabled) {
        return energyJ;
    }
    return energyJ + (dynamicW + staticW) * (curTick() - lastTick) /
                         sim_clock::as_float::s;
}

void
LatchedPowerModel::setPower(double dynamic_w, double static_w)
{
    fatal_if(!std::isfinite(dynamic_w) || dynamic_w < 0 ||
                 !std::isfinite(static_w) || static_w < 0,
             "Invalid latched power");
    fatal_if(constantPowerOnly &&
                 (dynamic_w != dynamicW || static_w != staticW),
             "Changing latched power with automatic thermal stepping is "
             "unsupported; use automatic_step=False and advance() at "
             "explicit power interval boundaries");
    energyJ = getEnergyJ();
    lastTick = curTick();
    dynamicW = dynamic_w;
    staticW = static_w;
}

void
LatchedPowerModel::setAccountingEnabled(bool enabled)
{
    energyJ = getEnergyJ();
    lastTick = curTick();
    accountingEnabled = enabled;
}

void
LatchedPowerModel::serialize(CheckpointOut &cp) const
{
    auto precision = cp.precision(std::numeric_limits<double>::max_digits10);
    double accumulated = getEnergyJ();
    Tick accountedTick = curTick();
    SERIALIZE_SCALAR(accountingEnabled);
    SERIALIZE_SCALAR(dynamicW);
    SERIALIZE_SCALAR(staticW);
    SERIALIZE_SCALAR(accumulated);
    SERIALIZE_SCALAR(accountedTick);
    cp.precision(precision);
}

void
LatchedPowerModel::unserialize(CheckpointIn &cp)
{
    double accumulated;
    Tick accountedTick;
    UNSERIALIZE_SCALAR(accountingEnabled);
    UNSERIALIZE_SCALAR(dynamicW);
    UNSERIALIZE_SCALAR(staticW);
    UNSERIALIZE_SCALAR(accumulated);
    UNSERIALIZE_SCALAR(accountedTick);
    energyJ = accumulated;
    lastTick = accountedTick;
}

} // namespace gem5
