/* SPDX-License-Identifier: BSD-3-Clause */
#ifndef __SIM_POWER_LATCHED_POWER_MODEL_HH__
#define __SIM_POWER_LATCHED_POWER_MODEL_HH__

#include "params/LatchedPowerModel.hh"
#include "params/PowerActor.hh"
#include "sim/clocked_object.hh"
#include "sim/power/power_model.hh"

namespace gem5
{

/** Power supplied at epoch boundaries; setPower affects future time only. */
class LatchedPowerModel : public PowerModelState
{
  public:
    using Params = LatchedPowerModelParams;
    LatchedPowerModel(const Params &p);
    double
    getDynamicPower() const override
    {
        return dynamicW;
    }
    double
    getStaticPower() const override
    {
        return staticW;
    }
    void setPower(double dynamic_w, double static_w);
    double getEnergyJ() const;
    void setAccountingEnabled(bool enabled);
    /** Automatic thermal steps only support constant latched power. */
    void
    requireConstantPower()
    {
        constantPowerOnly = true;
    }
    void serialize(CheckpointOut &cp) const override;
    void unserialize(CheckpointIn &cp) override;

  private:
    double dynamicW;
    double staticW;
    double energyJ = 0;
    Tick lastTick = 0;
    bool accountingEnabled = true;
    // Derived from thermal topology at init, including checkpoint restore.
    bool constantPowerOnly = false;
};

class PowerActor : public ClockedObject
{
  public:
    using Params = PowerActorParams;
    PowerActor(const Params &p) : ClockedObject(p) {}
};

} // namespace gem5
#endif
