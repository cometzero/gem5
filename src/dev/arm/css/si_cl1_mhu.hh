/* SPDX-License-Identifier: BSD-3-Clause */
#ifndef __DEV_ARM_CSS_SI_CL1_MHU_HH__
#define __DEV_ARM_CSS_SI_CL1_MHU_HH__

#include <array>

#include "dev/io_device.hh"

namespace gem5
{

class ArmInterruptPin;
struct SiCl1MhuParams;
class System;

/** Functional DBE subset, not an MHU-320AE timing, RAS or SCP model. */
class SiCl1Mhu : public BasicPioDevice
{
  public:
    explicit SiCl1Mhu(const SiCl1MhuParams &p);
    void initState() override;
    AddrRangeList getAddrRanges() const override;
    Tick read(PacketPtr pkt) override;
    Tick write(PacketPtr pkt) override;
    void serialize(CheckpointOut &cp) const override;
    void unserialize(CheckpointIn &cp) override;

  private:
    static constexpr unsigned Channels = 32;
    static constexpr Addr FrameSize = 0x30000;
    static constexpr unsigned ScmiChannelBase = 2;
    static constexpr unsigned ScmiChannels = 4;
    static constexpr Addr ScmiStride = 40;
    static constexpr std::array<Addr, 4> FrameOffsets = {0, 0x40000, 0x200000,
                                                         0x240000};
    struct Frame
    {
        std::array<uint32_t, Channels> status{};
        std::array<uint32_t, Channels> mask{};
        std::array<uint32_t, Channels> intStatus{};
        std::array<uint32_t, Channels> intEnable{};
        std::array<uint32_t, Channels> control{};
        uint32_t opControl = 0;
    };
    std::array<Frame, 4> frames{};
    System *system;
    const Addr shmemAddr;
    std::array<ArmInterruptPin *, 3> interrupts;

    unsigned decode(Addr addr, Addr &offset) const;
    uint32_t combined(unsigned frame) const;
    void updateInterrupts();
    uint32_t readRegister(unsigned frame, Addr offset) const;
    void writeRegister(unsigned frame, Addr offset, uint32_t value);
    void respondPfdi(unsigned channel, uint32_t bits);
    uint32_t readMemory(Addr addr) const;
    void writeMemory(Addr addr, uint32_t value);
};

} // namespace gem5
#endif
