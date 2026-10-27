/* SPDX-License-Identifier: BSD-3-Clause */
#include "dev/arm/css/si_cl1_mhu.hh"

#include "base/logging.hh"
#include "base/trace.hh"
#include "debug/MHU.hh"
#include "dev/arm/base_gic.hh"
#include "mem/packet_access.hh"
#include "params/SiCl1Mhu.hh"
#include "sim/serialize.hh"
#include "sim/system.hh"

namespace gem5
{

SiCl1Mhu::SiCl1Mhu(const SiCl1MhuParams &p)
    : BasicPioDevice(p, FrameSize),
      system(p.system),
      shmemAddr(p.shmem_addr),
      interrupts{p.hipc_tx_irq->get(), p.hipc_rx_irq->get(), p.pfdi_irq->get()}
{
    for (auto &frame : frames) {
        frame.mask.fill(~0U);
    }
}

void
SiCl1Mhu::initState()
{
    BasicPioDevice::initState();
    // SCMI transport starts free. Firmware owns the remaining slot contents.
    for (unsigned slot = 0; slot < ScmiChannels; ++slot) {
        writeMemory(shmemAddr + slot * ScmiStride + 4, 1);
    }
}

AddrRangeList
SiCl1Mhu::getAddrRanges() const
{
    AddrRangeList ranges;
    for (const auto offset : FrameOffsets) {
        ranges.push_back(RangeSize(pioAddr + offset, FrameSize));
    }
    return ranges;
}

unsigned
SiCl1Mhu::decode(Addr addr, Addr &offset) const
{
    for (unsigned i = 0; i < frames.size(); ++i) {
        const Addr base = pioAddr + FrameOffsets[i];
        if (addr >= base && addr - base < FrameSize) {
            offset = addr - base;
            return i;
        }
    }
    panic("%s: invalid MHU address %#x", name(), addr);
}

uint32_t
SiCl1Mhu::combined(unsigned index) const
{
    const auto &f = frames[index];
    uint32_t value = 0;
    for (unsigned ch = 0; ch < Channels; ++ch) {
        const bool pending =
            (index & 1) ? (f.status[ch] & ~f.mask[ch]) != 0
                        : (f.intStatus[ch] & f.intEnable[ch] & 1) != 0;
        if ((f.control[ch] & 1) && pending) {
            value |= 1U << ch;
        }
    }
    return value;
}

void
SiCl1Mhu::updateInterrupts()
{
    const std::array<bool, 3> pending = {combined(0) != 0, combined(1) != 0,
                                         (combined(2) | combined(3)) != 0};
    for (unsigned i = 0; i < interrupts.size(); ++i) {
        if (pending[i] && !interrupts[i]->active()) {
            interrupts[i]->raise();
        } else if (!pending[i] && interrupts[i]->active()) {
            interrupts[i]->clear();
        }
    }
}

uint32_t
SiCl1Mhu::readRegister(unsigned index, Addr offset) const
{
    const auto &f = frames[index];
    const bool mbx = index & 1;
    switch (offset) {
        case 0x000:
            return mbx ? 1 : 0;
        case 0x010:
            return 1; // Doorbell extension only.
        case 0x014:
            return 1; // Operational request handshake supported.
        case 0x020:
            return Channels - 1;
        case 0x100:
            return f.opControl;
        case 0x400:
            return combined(index);
        case 0xfc8:
            return 0x0000043b; // Arm implementer, functional model.
        case 0xfcc:
            return 0x20; // MHUv3 architecture major version encoding.
        default:
            break;
    }
    if (offset < 0x1000 || offset >= 0x1000 + Channels * 0x20) {
        return 0; // Unimplemented features and reserved registers are RAZ.
    }
    const unsigned ch = (offset - 0x1000) / 0x20;
    const unsigned reg = offset & 0x1f;
    switch (reg) {
        case 0x00:
            return f.status[ch];
        case 0x04:
            return mbx ? f.status[ch] & ~f.mask[ch] : 0;
        case 0x10:
            return mbx ? f.mask[ch] : f.intStatus[ch];
        case 0x18:
            return mbx ? 0 : f.intEnable[ch];
        case 0x1c:
            return f.control[ch];
        default:
            return 0;
    }
}

void
SiCl1Mhu::writeRegister(unsigned index, Addr offset, uint32_t value)
{
    auto &f = frames[index];
    const bool mbx = index & 1;
    if (offset == 0x100) {
        f.opControl = value & 3;
        return;
    }
    if (offset < 0x1000 || offset >= 0x1000 + Channels * 0x20) {
        return;
    }
    const unsigned ch = (offset - 0x1000) / 0x20;
    switch (offset & 0x1f) {
        case 0x08:
            if (mbx) {
                f.status[ch] &= ~value;
            }
            break;
        case 0x0c:
            if (!mbx) {
                f.status[ch] |= value;
                // HIPC intentionally retains pending bits: AP is offline.
                if (index == 2 && value) {
                    respondPfdi(ch, value);
                }
            }
            break;
        case 0x14:
            if (mbx) {
                f.mask[ch] |= value;
            } else {
                f.intStatus[ch] &= ~(value & 1);
            }
            break;
        case 0x18:
            if (mbx) {
                f.mask[ch] &= ~value;
            } else {
                f.intEnable[ch] = value & 1;
            }
            break;
        case 0x1c:
            f.control[ch] = value & 1;
            break;
        default:
            break;
    }
    updateInterrupts();
}

uint32_t
SiCl1Mhu::readMemory(Addr addr) const
{
    return system->physProxy.read<uint32_t>(addr, ByteOrder::little);
}

void
SiCl1Mhu::writeMemory(Addr addr, uint32_t value)
{
    system->physProxy.write<uint32_t>(addr, value, ByteOrder::little);
}

void
SiCl1Mhu::respondPfdi(unsigned channel, uint32_t bits)
{
    if (channel < ScmiChannelBase ||
        channel >= ScmiChannelBase + ScmiChannels) {
        warn("%s: unsupported PFDI channel %u; leaving doorbell pending",
             name(), channel);
        return;
    }
    const Addr addr = shmemAddr + (channel - ScmiChannelBase) * ScmiStride;
    // Do not overwrite an already free response on a duplicate doorbell.
    if (readMemory(addr + 4) & 1) {
        return;
    }
    const uint32_t length = readMemory(addr + 0x14);
    const uint32_t header = readMemory(addr + 0x18);
    const unsigned protocol = (header >> 10) & 0xff;
    const unsigned message = header & 0xff;
    const unsigned type = (header >> 8) & 3;
    uint32_t status = 0;
    bool version = false;
    // Bounds include the message header. Never read variable guest lengths.
    if (length < 4 || length > ScmiStride - 0x18 || (length & 3) || type) {
        status = static_cast<uint32_t>(-10); // SCMI_PROTOCOL_ERROR
    } else if (protocol != 0x90) {
        status = static_cast<uint32_t>(-1); // SCMI_NOT_SUPPORTED
    } else if (message == 0) {
        version = length == 4;
        if (!version) {
            status = static_cast<uint32_t>(-10);
        }
    } else if (message == 3 || message == 4) {
        if (length != 8) {
            status = static_cast<uint32_t>(-10);
        }
    } else {
        status = static_cast<uint32_t>(-1);
    }
    writeMemory(addr + 0x14, version ? 12 : 8);
    writeMemory(addr + 0x1c, status);
    if (version) {
        writeMemory(addr + 0x20, 0x00020000);
    }
    writeMemory(addr + 4, 1);
    // Consume the transmitted doorbell and latch transfer acknowledgement.
    frames[2].status[channel] &= ~bits;
    frames[2].intStatus[channel] |= 1;
    // The integrated image polls TX completion. RX is available only if the
    // guest explicitly unmasks it; otherwise it cannot generate an IRQ.
    frames[3].status[channel] |= bits;
    DPRINTF(MHU, "PFDI channel=%u header=%#x status=%#x length=%u\n", channel,
            header, status, version ? 12 : 8);
}

Tick
SiCl1Mhu::read(PacketPtr pkt)
{
    panic_if(pkt->getSize() != 4 || (pkt->getAddr() & 3),
             "%s: MHU registers require aligned 32-bit accesses", name());
    Addr offset;
    const unsigned frame = decode(pkt->getAddr(), offset);
    pkt->setLE<uint32_t>(readRegister(frame, offset));
    pkt->makeAtomicResponse();
    return pioDelay;
}

Tick
SiCl1Mhu::write(PacketPtr pkt)
{
    panic_if(pkt->getSize() != 4 || (pkt->getAddr() & 3),
             "%s: MHU registers require aligned 32-bit accesses", name());
    Addr offset;
    const unsigned frame = decode(pkt->getAddr(), offset);
    writeRegister(frame, offset, pkt->getLE<uint32_t>());
    pkt->makeAtomicResponse();
    return pioDelay;
}

void
SiCl1Mhu::serialize(CheckpointOut &cp) const
{
    BasicPioDevice::serialize(cp);
    for (unsigned i = 0; i < frames.size(); ++i) {
        ScopedCheckpointSection section(cp, csprintf("frame%u", i));
        const auto &f = frames[i];
        arrayParamOut(cp, "status", f.status);
        arrayParamOut(cp, "mask", f.mask);
        arrayParamOut(cp, "int_status", f.intStatus);
        arrayParamOut(cp, "int_enable", f.intEnable);
        arrayParamOut(cp, "control", f.control);
        paramOut(cp, "op_control", f.opControl);
    }
}

void
SiCl1Mhu::unserialize(CheckpointIn &cp)
{
    BasicPioDevice::unserialize(cp);
    for (unsigned i = 0; i < frames.size(); ++i) {
        ScopedCheckpointSection section(cp, csprintf("frame%u", i));
        auto &f = frames[i];
        arrayParamIn(cp, "status", f.status.data(), Channels);
        arrayParamIn(cp, "mask", f.mask.data(), Channels);
        arrayParamIn(cp, "int_status", f.intStatus.data(), Channels);
        arrayParamIn(cp, "int_enable", f.intEnable.data(), Channels);
        arrayParamIn(cp, "control", f.control.data(), Channels);
        paramIn(cp, "op_control", f.opControl);
    }
    updateInterrupts();
}

} // namespace gem5
