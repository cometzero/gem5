/* Copyright (c) 2026 Chanho Park */
/* SPDX-License-Identifier: BSD-3-Clause */
#include "dev/arm/timing_virtio_block.hh"

#include <cmath>
#include <cstring>

#include "dev/arm/base_gic.hh"
#include "dev/storage/disk_image.hh"
#include "mem/packet_access.hh"
#include "sim/system.hh"

namespace gem5
{
namespace
{
template <class T>
T
loadLe(const uint8_t *bytes)
{
    T value = 0;
    for (unsigned i = 0; i < sizeof(T); ++i) {
        value |= T(bytes[i]) << (8 * i);
    }
    return value;
}

template <class T>
void
storeLe(uint8_t *bytes, T value)
{
    for (unsigned i = 0; i < sizeof(T); ++i) {
        bytes[i] = value >> (8 * i);
    }
}
} // anonymous namespace

TimingVirtIOBlock::Stats::Stats(statistics::Group *parent)
    : statistics::Group(parent),
      ADD_STAT(requests, statistics::units::Count::get(),
               "Completed requests"),
      ADD_STAT(reads, statistics::units::Count::get(), "Read requests"),
      ADD_STAT(writes, statistics::units::Count::get(), "Write requests"),
      ADD_STAT(flushes, statistics::units::Count::get(), "Flush requests"),
      ADD_STAT(dmaReadBytes, statistics::units::Byte::get(),
               "Guest memory bytes read through DMA, including metadata"),
      ADD_STAT(dmaWriteBytes, statistics::units::Byte::get(),
               "Guest memory bytes written through DMA, including metadata"),
      ADD_STAT(payloadBytes, statistics::units::Byte::get(),
               "Successful block read/write payload bytes"),
      ADD_STAT(interrupts, statistics::units::Count::get(),
               "Used ring interrupt assertions"),
      ADD_STAT(requestTicks, statistics::units::Tick::get(),
               "Sum of head fetch to used-index completion latency"),
      ADD_STAT(serviceTicks, statistics::units::Tick::get(),
               "Sum of declared synthetic storage service delays")
{}

TimingVirtIOBlock::TimingVirtIOBlock(const Params &p)
    : DmaDevice(p),
      pioAddr(p.pio_addr),
      pioSize(p.pio_size),
      pioDelay(p.pio_latency),
      requestLatency(p.request_latency),
      flushLatency(p.flush_latency),
      ticksPerByte(p.bandwidth),
      queueMax(p.queue_size),
      image(*p.image),
      interrupt(p.interrupt->get()),
      completion([this] { advance(); }, name() + ".completion"),
      stats(this)
{
    fatal_if(!interrupt || pioSize < 0x118 || queueMax < 4 || queueMax > 128 ||
                 (queueMax & (queueMax - 1)),
             "Invalid timing VirtIO block window, interrupt or queue size");
    fatal_if(!std::isfinite(ticksPerByte) || ticksPerByte <= 0,
             "Timing VirtIO block bandwidth must be positive");
}

AddrRangeList
TimingVirtIOBlock::getAddrRanges() const
{
    return {AddrRange(pioAddr, pioAddr + pioSize - 1)};
}

bool
TimingVirtIOBlock::active() const
{
    return queueReady && (deviceStatus & 0xf) == 0xf && !(deviceStatus & 0xc0);
}

Tick
TimingVirtIOBlock::read(PacketPtr pkt)
{
    const Addr offset = pkt->getAddr() - pioAddr;
    if (offset >= 0x100) {
        std::array<uint8_t, 24> config{};
        storeLe<uint64_t>(config.data(), uint64_t(image.size()));
        storeLe<uint32_t>(config.data() + 8, SegmentMax);
        storeLe<uint32_t>(config.data() + 12, queueMax - 2);
        std::memset(pkt->getPtr<uint8_t>(), 0, pkt->getSize());
        if (offset - 0x100 + pkt->getSize() <= config.size()) {
            pkt->setData(config.data() + offset - 0x100);
        }
        pkt->makeResponse();
        return pioDelay;
    }
    panic_if(pkt->getSize() != 4 || (offset & 3),
             "Timing VirtIO registers require aligned 32-bit accesses");
    uint32_t value = 0;
    switch (offset) {
        case 0x000:
            value = 0x74726976;
            break;
        case 0x004:
            value = 2;
            break;
        case 0x008:
            value = 2;
            break;
        case 0x00c:
            value = 0x1af4;
            break;
        case 0x010:
            value = featureSelect < 2 ? Features >> (featureSelect * 32) : 0;
            break;
        case 0x014:
            value = featureSelect;
            break;
        case 0x024:
            value = driverFeatureSelect;
            break;
        case 0x030:
            value = queueSelect;
            break;
        case 0x034:
            value = queueSelect == 0 ? queueMax : 0;
            break;
        case 0x038:
            value = queueSelect == 0 ? queueSize : 0;
            break;
        case 0x044:
            value = queueSelect == 0 && queueReady;
            break;
        case 0x060:
            value = interruptStatus;
            break;
        case 0x070:
            value = deviceStatus;
            break;
        case 0x080:
            value = descriptorBase;
            break;
        case 0x084:
            value = descriptorBase >> 32;
            break;
        case 0x090:
            value = availableBase;
            break;
        case 0x094:
            value = availableBase >> 32;
            break;
        case 0x0a0:
            value = usedBase;
            break;
        case 0x0a4:
            value = usedBase >> 32;
            break;
        default:
            break;
    }
    pkt->setLE<uint32_t>(value);
    pkt->makeResponse();
    return pioDelay;
}

Tick
TimingVirtIOBlock::write(PacketPtr pkt)
{
    const Addr offset = pkt->getAddr() - pioAddr;
    panic_if(pkt->getSize() != 4 || (offset & 3),
             "Timing VirtIO registers require aligned 32-bit accesses");
    const uint32_t value = pkt->getLE<uint32_t>();
    auto address = [offset, value](Addr &base) {
        if (offset & 4) {
            base = (base & 0xffffffffULL) | (uint64_t(value) << 32);
        } else {
            base = (base & 0xffffffff00000000ULL) | value;
        }
    };
    switch (offset) {
        case 0x014:
            featureSelect = value;
            break;
        case 0x024:
            driverFeatureSelect = value;
            break;
        case 0x020:
            if (driverFeatureSelect < 2 && !(deviceStatus & 8)) {
                const unsigned shift = driverFeatureSelect * 32;
                driverFeatures &= ~(0xffffffffULL << shift);
                driverFeatures |= uint64_t(value) << shift;
            }
            break;
        case 0x030:
            queueSelect = value;
            break;
        case 0x038:
            if (!queueSelect && !queueReady && value >= 4 &&
                value <= queueMax && !(value & (value - 1))) {
                queueSize = value;
            }
            break;
        case 0x044:
            if (!queueSelect) {
                fatal_if(
                    stage != Stage::Idle,
                    "Timing VirtIO queue reset while DMA/service is active "
                    "is unsupported");
                if (value == 0) {
                    queueReady = false;
                } else if (value == 1 && queueSize && !(descriptorBase & 15) &&
                           !(availableBase & 1) && !(usedBase & 3)) {
                    queueReady = true;
                }
            }
            break;
        case 0x050:
            if (value == 0 && active()) {
                notified = true;
                kick();
            }
            break;
        case 0x064:
            interruptStatus &= ~value;
            if (!interruptStatus) {
                interrupt->clear();
            }
            break;
        case 0x070:
            if (value == 0) {
                reset();
            } else {
                deviceStatus = value & 0xff;
                if ((deviceStatus & 8) && (!(driverFeatures & (1ULL << 32)) ||
                                           (driverFeatures & ~Features))) {
                    deviceStatus &= ~8;
                }
                if ((deviceStatus & 0xb) != 0xb) {
                    deviceStatus &= ~4;
                }
            }
            break;
        case 0x080:
        case 0x084:
            if (!queueSelect && !queueReady) {
                address(descriptorBase);
            }
            break;
        case 0x090:
        case 0x094:
            if (!queueSelect && !queueReady) {
                address(availableBase);
            }
            break;
        case 0x0a0:
        case 0x0a4:
            if (!queueSelect && !queueReady) {
                address(usedBase);
            }
            break;
        default:
            break;
    }
    pkt->makeResponse();
    return pioDelay;
}

void
TimingVirtIOBlock::reset()
{
    fatal_if(stage != Stage::Idle || dmaPending() || completion.scheduled(),
             "Timing VirtIO block reset during DMA/service is unsupported");
    featureSelect = driverFeatureSelect = queueSelect = 0;
    driverFeatures = deviceStatus = interruptStatus = queueSize = 0;
    descriptorBase = availableBase = usedBase = 0;
    availableIndex = usedIndex = 0;
    queueReady = notified = false;
    interrupt->clear();
}

void
TimingVirtIOBlock::transfer(bool write, Addr address, size_t size,
                            uint8_t *buffer, Stage next)
{
    // This AP profile has a single contiguous RAM range. Reject addresses
    // outside RAM before issuing DMA so malformed rings cannot access MMIO.
    if (!size || address + size - 1 < address || !sys->isMemAddr(address) ||
        !sys->isMemAddr(address + size - 1)) {
        failQueue("DMA outside guest RAM");
        return;
    }
    stage = next;
    if (write) {
        stats.dmaWriteBytes += size;
        dmaWrite(address, size, &completion, buffer);
    } else {
        stats.dmaReadBytes += size;
        dmaRead(address, size, &completion, buffer);
    }
}

void
TimingVirtIOBlock::kick()
{
    if (active() && !stopping && stage == Stage::Idle) {
        fetchAvailable();
    }
}

void
TimingVirtIOBlock::fetchAvailable()
{
    notified = false;
    transfer(false, availableBase, 4, scratch.data(), Stage::Available);
}

void
TimingVirtIOBlock::fetchDescriptor()
{
    if (descriptorIndex >= queueSize || visited[descriptorIndex]) {
        failQueue("invalid/cyclic descriptor chain");
        return;
    }
    visited[descriptorIndex] = true;
    transfer(false, descriptorBase + 16 * descriptorIndex, 16, scratch.data(),
             Stage::Descriptor);
}

void
TimingVirtIOBlock::advance()
{
    switch (stage) {
        case Stage::Available: {
            availableFlags = loadLe<uint16_t>(scratch.data());
            const uint16_t count =
                loadLe<uint16_t>(scratch.data() + 2) - availableIndex;
            if (count > queueSize) {
                failQueue("available ring overrun");
            } else if (count == 0 || stopping) {
                idle();
                if (notified) {
                    kick();
                }
            } else {
                requestStart = curTick();
                transfer(false,
                         availableBase + 4 + 2 * (availableIndex % queueSize),
                         2, scratch.data(), Stage::Head);
            }
            break;
        }
        case Stage::Head:
            head = descriptorIndex = loadLe<uint16_t>(scratch.data());
            ++availableIndex;
            chain.clear();
            visited.assign(queueSize, false);
            fetchDescriptor();
            break;
        case Stage::Descriptor: {
            Descriptor desc{loadLe<uint64_t>(scratch.data()),
                            loadLe<uint32_t>(scratch.data() + 8),
                            loadLe<uint16_t>(scratch.data() + 12),
                            loadLe<uint16_t>(scratch.data() + 14)};
            if (desc.flags & ~3) {
                failQueue("indirect/reserved descriptor flags not negotiated");
                break;
            }
            chain.push_back(desc);
            if (desc.flags & 1) {
                descriptorIndex = desc.next;
                fetchDescriptor();
            } else if (chain.size() < 2 || (chain.front().flags & 2) ||
                       chain.front().length != 16 ||
                       !(chain.back().flags & 2) || chain.back().length < 1) {
                failQueue("invalid block header/status descriptors");
            } else {
                transfer(false, chain.front().address, 16, scratch.data(),
                         Stage::Header);
            }
            break;
        }
        case Stage::Header:
            prepareRequest();
            break;
        case Stage::ReadData:
            transferData(false);
            break;
        case Stage::Service:
            completeService();
            break;
        case Stage::WriteData:
            transferData(true);
            break;
        case Stage::Status:
            storeLe<uint32_t>(scratch.data(), head);
            storeLe<uint32_t>(
                scratch.data() + 4,
                1 + (requestType == 0 && !responseStatus ? data.size() : 0));
            transfer(true, usedBase + 4 + 8 * (usedIndex % queueSize), 8,
                     scratch.data(), Stage::UsedElement);
            break;
        case Stage::UsedElement:
            ++usedIndex;
            storeLe<uint16_t>(scratch.data(), usedIndex);
            transfer(true, usedBase + 2, 2, scratch.data(), Stage::UsedIndex);
            break;
        case Stage::UsedIndex:
            ++stats.requests;
            stats.requestTicks += curTick() - requestStart;
            if (!(availableFlags & 1)) {
                interruptStatus |= 1;
                interrupt->raise();
                ++stats.interrupts;
            }
            if (stopping || !active()) {
                idle();
            } else {
                fetchAvailable();
            }
            break;
        case Stage::Idle:
            panic("Unexpected timing VirtIO DMA completion");
    }
}

void
TimingVirtIOBlock::prepareRequest()
{
    requestType = loadLe<uint32_t>(scratch.data());
    sector = loadLe<uint64_t>(scratch.data() + 8);
    responseStatus = 0;
    size_t size = 0;
    for (size_t i = 1; i + 1 < chain.size(); ++i) {
        const auto &desc = chain[i];
        if (!desc.length || desc.length > SegmentMax ||
            bool(desc.flags & 2) != (requestType == 0)) {
            responseStatus = 1;
            break;
        }
        size += desc.length;
    }
    data.clear();
    if (requestType != 0 && requestType != 1 && requestType != 4) {
        responseStatus = 2;
    } else if (requestType == 4) {
        if (!(driverFeatures & (1ULL << 9)) || chain.size() != 2) {
            responseStatus = 1;
        }
    } else if (size % SectorSize || sector > uint64_t(image.size()) ||
               size / SectorSize > uint64_t(image.size()) - sector) {
        responseStatus = 1;
    }
    if (responseStatus) {
        writeStatus();
        return;
    }
    data.resize(size);
    dataPart = 1;
    dataOffset = 0;
    if (requestType == 1) {
        transferData(false);
    } else {
        startService();
    }
}

void
TimingVirtIOBlock::transferData(bool write)
{
    if (dataPart + 1 < chain.size()) {
        const auto &desc = chain[dataPart++];
        uint8_t *buffer = data.data() + dataOffset;
        dataOffset += desc.length;
        transfer(write, desc.address, desc.length, buffer,
                 write ? Stage::WriteData : Stage::ReadData);
    } else if (write) {
        writeStatus();
    } else {
        startService();
    }
}

void
TimingVirtIOBlock::startService()
{
    const long double service =
        requestType == 4
            ? flushLatency
            : requestLatency +
                  std::ceil(data.size() * (long double)ticksPerByte);
    fatal_if(!std::isfinite(service) || service > MaxTick - curTick(),
             "Timing VirtIO service delay exceeds simulation tick range");
    const Tick delay = Tick(service);
    stats.serviceTicks += delay;
    stage = Stage::Service;
    schedule(completion, curTick() + delay);
}

void
TimingVirtIOBlock::completeService()
{
    if (requestType == 4) {
        // Depth one orders this synthetic barrier after all prior writes.
        // The image backend is not a physical durability model.
        ++stats.flushes;
    } else {
        if (requestType == 0) {
            ++stats.reads;
        } else {
            ++stats.writes;
        }
        for (size_t offset = 0; offset < data.size(); offset += SectorSize) {
            auto amount = requestType == 0
                              ? image.read(data.data() + offset,
                                           sector + offset / SectorSize)
                              : image.write(data.data() + offset,
                                            sector + offset / SectorSize);
            if (amount != SectorSize) {
                responseStatus = 1;
                break;
            }
        }
        if (!responseStatus) {
            stats.payloadBytes += data.size();
        }
    }
    if (requestType == 0 && !responseStatus) {
        dataPart = 1;
        dataOffset = 0;
        transferData(true);
    } else {
        writeStatus();
    }
}

void
TimingVirtIOBlock::writeStatus()
{
    transfer(true, chain.back().address, 1, &responseStatus, Stage::Status);
}

void
TimingVirtIOBlock::failQueue(const char *reason)
{
    warn("%s: %s; DEVICE_NEEDS_RESET\n", name(), reason);
    deviceStatus |= 64;
    interruptStatus |= 2;
    interrupt->raise();
    idle();
}

void
TimingVirtIOBlock::idle()
{
    stage = Stage::Idle;
    if (drainState() == DrainState::Draining) {
        signalDrainDone();
    }
}

DrainState
TimingVirtIOBlock::drain()
{
    stopping = true;
    return stage == Stage::Idle ? DrainState::Drained : DrainState::Draining;
}

void
TimingVirtIOBlock::drainResume()
{
    stopping = false;
    // Recheck avail even if its notification arrived while draining.
    kick();
}

void
TimingVirtIOBlock::serialize(CheckpointOut &cp) const
{
    fatal("Timing VirtIO block checkpoint is not supported");
}

void
TimingVirtIOBlock::unserialize(CheckpointIn &cp)
{
    fatal("Timing VirtIO block checkpoint restore is not supported");
}
} // namespace gem5
