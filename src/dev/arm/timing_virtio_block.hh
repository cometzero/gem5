/* Copyright (c) 2026 Chanho Park */
/* SPDX-License-Identifier: BSD-3-Clause */
#ifndef __DEV_ARM_TIMING_VIRTIO_BLOCK_HH__
#define __DEV_ARM_TIMING_VIRTIO_BLOCK_HH__

#include <array>
#include <vector>

#include "base/statistics.hh"
#include "dev/dma_device.hh"
#include "params/TimingVirtIOBlock.hh"
#include "sim/eventq.hh"

namespace gem5
{
class ArmInterruptPin;
class DiskImage;

/** Experimental depth-one modern VirtIO block device with coherent DMA.
 * All guest ring, descriptor and data transfers use the DMA port. Service
 * time is a declared synthetic model, not calibrated physical storage.
 */
class TimingVirtIOBlock : public DmaDevice
{
  public:
    PARAMS(TimingVirtIOBlock);
    explicit TimingVirtIOBlock(const Params &p);
    AddrRangeList getAddrRanges() const override;
    Tick read(PacketPtr pkt) override;
    Tick write(PacketPtr pkt) override;
    DrainState drain() override;
    void drainResume() override;
    void serialize(CheckpointOut &cp) const override;
    void unserialize(CheckpointIn &cp) override;

  private:
    static constexpr uint64_t Features =
        (1ULL << 32) | (1ULL << 9) | (1ULL << 2) | (1ULL << 1);
    static constexpr uint32_t SegmentMax = 65536;
    struct Descriptor
    {
        Addr address;
        uint32_t length;
        uint16_t flags;
        uint16_t next;
    };
    enum class Stage
    {
        Idle,
        Available,
        Head,
        Descriptor,
        Header,
        ReadData,
        Service,
        WriteData,
        Status,
        UsedElement,
        UsedIndex
    };

    const Addr pioAddr, pioSize;
    const Tick pioDelay, requestLatency, flushLatency;
    const double ticksPerByte;
    const uint32_t queueMax;
    DiskImage &image;
    ArmInterruptPin *interrupt;
    uint32_t featureSelect = 0, driverFeatureSelect = 0, queueSelect = 0;
    uint64_t driverFeatures = 0;
    uint32_t deviceStatus = 0, interruptStatus = 0, queueSize = 0;
    bool queueReady = false, stopping = false, notified = false;
    Addr descriptorBase = 0, availableBase = 0, usedBase = 0;
    uint16_t availableIndex = 0, usedIndex = 0, head = 0, descriptorIndex = 0;
    uint16_t availableFlags = 0;
    Stage stage = Stage::Idle;
    EventFunctionWrapper completion;
    std::array<uint8_t, 16> scratch{};
    std::vector<Descriptor> chain;
    std::vector<bool> visited;
    std::vector<uint8_t> data;
    size_t dataPart = 0, dataOffset = 0;
    uint32_t requestType = 0;
    uint64_t sector = 0;
    uint8_t responseStatus = 0;
    Tick requestStart = 0;

    struct Stats : statistics::Group
    {
        explicit Stats(statistics::Group *parent);
        statistics::Scalar requests, reads, writes, flushes;
        statistics::Scalar dmaReadBytes, dmaWriteBytes, payloadBytes;
        statistics::Scalar interrupts, requestTicks, serviceTicks;
    } stats;

    bool active() const;
    void reset();
    void kick();
    void fetchAvailable();
    void advance();
    void failQueue(const char *reason);
    void idle();
    void transfer(bool write, Addr address, size_t size, uint8_t *buffer,
                  Stage next);
    void fetchDescriptor();
    void prepareRequest();
    void transferData(bool write);
    void startService();
    void completeService();
    void writeStatus();
};
} // namespace gem5
#endif
