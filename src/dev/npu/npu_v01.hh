/* SPDX-License-Identifier: BSD-3-Clause */
#ifndef __DEV_NPU_NPU_V01_HH__
#define __DEV_NPU_NPU_V01_HH__

#include <array>
#include <deque>
#include <functional>
#include <list>
#include <memory>
#include <vector>

#include "base/statistics.hh"
#include "dev/io_device.hh"
#include "mem/port.hh"
#include "params/NpuV01.hh"
#include "sim/eventq.hh"

namespace gem5
{
class ArmInterruptPin;

/** Ordered FUNC_OPS timing interpreter; does not execute RV32 instructions. */
class NpuV01 : public BasicPioDevice
{
  public:
    PARAMS(NpuV01);
    explicit NpuV01(const Params &p);
    Port &getPort(const std::string &name, PortID id = InvalidPortID) override;
    Tick read(PacketPtr pkt) override;
    Tick write(PacketPtr pkt) override;
    DrainState drain() override;
    void serialize(CheckpointOut &cp) const override;
    void unserialize(CheckpointIn &cp) override;

  private:
    class DmaPort : public RequestPort
    {
      public:
        NpuV01 &owner;
        DmaPort(NpuV01 &n) : RequestPort(n.name() + ".dma"), owner(n) {}
        bool recvTimingResp(PacketPtr pkt) override;
        void recvReqRetry() override;
    } dma;
    struct Binding
    {
        Addr iova;
        uint32_t bytes, flags;
    };
    struct Op
    {
        uint16_t code, flags;
        uint32_t dst, a, b, m, n, k, extra;
    };
    enum class Phase
    {
        Idle,
        Package,
        Context,
        Bindings,
        Issue,
        Dma,
        Mac
    };
    struct SramTask
    {
        bool write;
        std::vector<uint32_t> addresses;
        std::vector<uint8_t> bytes;
        size_t next = 0, pending = 0;
        std::function<void()> complete;
    };
    struct Beat
    {
        std::shared_ptr<SramTask> task;
        size_t offset, count;
        Tick ready;
    };
    struct Transfer
    {
        Addr address;
        size_t size, issued = 0, inflight = 0;
        bool write, payload;
        uint32_t local;
        int channel = -1;
        std::vector<uint8_t> *buffer;
        std::function<void()> complete;
    };
    struct Entry : Packet::SenderState
    {
        std::shared_ptr<Transfer> transfer;
        PacketPtr pkt = nullptr;
        size_t offset = 0, size = 0;
        bool ready = false, accepted = false, finished = false;
    };
    struct Tile
    {
        bool active = false;
        uint32_t row = 0, col = 0, rows = 0, cols = 0, inner = 0;
        std::array<uint32_t, 64> sums{};
        std::shared_ptr<SramTask> operands;
        Tick compute = 0;
    };

    const RequestorID requestor;
    const bool enableAsync;
    ArmInterruptPin *interrupt, *faultInterrupt;
    const unsigned streamId, maxOutstanding, dmaWidth, dmaIssueWidth;
    const unsigned bankWidth, bankDepth;
    const Cycles sramLatency;
    std::vector<uint8_t> sram;
    std::vector<std::deque<Beat>> banks;
    std::deque<std::shared_ptr<SramTask>> sramTasks;
    std::list<std::unique_ptr<Entry>> entries;
    std::array<Tile, 2> tiles;
    EventFunctionWrapper progress;
    Phase phase = Phase::Idle;
    uint32_t control = 0, status = 1, kernelBytes = 0, hartMask = 3;
    uint32_t irqStatus = 0, irqMask = 0, faultCode = 0;
    Addr kernelIova = 0, contextIova = 0, faultIova = 0;
    Addr launchPackage = 0, launchContext = 0;
    unsigned launchHarts = 0, dmaTurn = 0;
    uint64_t perfCycles = 0, perfDma = 0, perfMac = 0, perfStalls = 0;
    Tick jobStart = 0, linkAvailable = 0, resetDone = 0;
    bool blocked = false, stopping = false, resetting = false;
    uint32_t stopFault = 0;
    std::vector<uint8_t> package, context, rawBindings;
    std::vector<Binding> bindings;
    std::vector<Op> ops;
    size_t opIndex = 0;
    std::list<std::shared_ptr<Transfer>> transfers;
    uint32_t nextTile = 0, tileCount = 0;
    uint8_t tokens = 0, tokensDone = 0;
    bool asyncVariant = false;

    struct Stats : statistics::Group
    {
        explicit Stats(statistics::Group *parent);
        statistics::Scalar jobs, faults, resets, dmaRequests, dmaRetries;
        statistics::Scalar dmaReadBytes, dmaWriteBytes, metadataReadBytes;
        statistics::Scalar dmaFailedBytes, sramReadBytes, sramWriteBytes;
        statistics::Scalar sramBankBeats, sramBankStalls, macs, jobTicks;
        statistics::Scalar outstandingHighWater, dmaMacOverlapCycles;
        statistics::Scalar dmaChannelsHighWater, dependencyStalls;
        statistics::Scalar dmaChannelsInFlightHighWater;
        statistics::Scalar firstPayloadAcceptedTick, lastPayloadResponseTick;
        statistics::Scalar acceptedPacketsPerCycleHighWater;
        statistics::Scalar acceptedBytesPerCycleHighWater;
    } stats;

    void wake();
    void tick();
    void updateIrqs();
    void launch();
    void finish();
    void fail(uint32_t fault, Addr address = 0);
    void stop(bool reset, uint32_t fault = 0);
    void stopped();
    void transfer(Addr address, size_t size, bool write, bool payload,
                  uint32_t local, std::vector<uint8_t> *buffer,
                  std::function<void()> complete, int channel = -1);
    bool hazard(uint32_t local, uint64_t bytes, bool write) const;
    void fillDma();
    void sendDma();
    void response(PacketPtr pkt);
    std::shared_ptr<SramTask> sramTask(bool write,
                                       std::vector<uint32_t> addresses,
                                       std::vector<uint8_t> bytes,
                                       std::function<void()> complete);
    void stepSram();
    void parsePackage();
    void parseContext();
    void parseBindings();
    void preflight();
    void issue();
    void startTile(unsigned engine);
    void tileOperands(unsigned engine);
    void stepMac();
};
} // namespace gem5
#endif
