/* SPDX-License-Identifier: BSD-3-Clause */
#include "dev/npu/npu_v01.hh"

#include <algorithm>
#include <cstring>
#include <limits>

#include "base/logging.hh"
#include "dev/arm/base_gic.hh"
#include "dev/arm/smmu_v3.hh"
#include "mem/packet_access.hh"
#include "sim/serialize.hh"
#include "sim/system.hh"

namespace gem5
{
namespace
{
uint16_t
le16(const uint8_t *p)
{
    return p[0] | uint16_t(p[1]) << 8;
}
uint32_t
le32(const uint8_t *p)
{
    return le16(p) | uint32_t(le16(p + 2)) << 16;
}
uint64_t
le64(const uint8_t *p)
{
    return le32(p) | uint64_t(le32(p + 4)) << 32;
}
bool
range(uint64_t offset, uint64_t size, uint64_t limit)
{
    return offset <= limit && size <= limit - offset;
}
bool
addressRange(Addr address, uint64_t size)
{
    return !size || address <= std::numeric_limits<Addr>::max() - (size - 1);
}
std::vector<uint32_t>
localRange(uint32_t start, size_t size)
{
    std::vector<uint32_t> addresses(size);
    for (size_t i = 0; i < size; ++i) {
        addresses[i] = start + i;
    }
    return addresses;
}
} // anonymous namespace

NpuV01::Stats::Stats(statistics::Group *parent)
    : statistics::Group(parent),
      ADD_STAT(jobs, statistics::units::Count::get(), "Completed jobs"),
      ADD_STAT(faults, statistics::units::Count::get(), "Job faults"),
      ADD_STAT(resets, statistics::units::Count::get(), "Completed resets"),
      ADD_STAT(dmaRequests, statistics::units::Count::get(),
               "Accepted DMA packets including metadata"),
      ADD_STAT(dmaRetries, statistics::units::Count::get(),
               "Rejected timing request attempts"),
      ADD_STAT(dmaReadBytes, statistics::units::Byte::get(),
               "Successful payload read response bytes"),
      ADD_STAT(dmaWriteBytes, statistics::units::Byte::get(),
               "Successful payload write response bytes"),
      ADD_STAT(metadataReadBytes, statistics::units::Byte::get(),
               "Successful package/context/binding read bytes"),
      ADD_STAT(dmaFailedBytes, statistics::units::Byte::get(),
               "Error response bytes"),
      ADD_STAT(sramReadBytes, statistics::units::Byte::get(),
               "Completed SRAM consumer bytes"),
      ADD_STAT(sramWriteBytes, statistics::units::Byte::get(),
               "Completed SRAM producer bytes, excluding reset clear"),
      ADD_STAT(sramBankBeats, statistics::units::Count::get(),
               "Accepted SRAM bank beats"),
      ADD_STAT(sramBankStalls, statistics::units::Count::get(),
               "SRAM task blocked attempts at bank arbitration"),
      ADD_STAT(macs, statistics::units::Count::get(),
               "Executed signed INT8 multiply accumulates"),
      ADD_STAT(jobTicks, statistics::units::Tick::get(),
               "Completed job elapsed ticks"),
      ADD_STAT(outstandingHighWater, statistics::units::Count::get(),
               "Maximum occupied DMA/staging entries"),
      ADD_STAT(dmaMacOverlapCycles, statistics::units::Cycle::get(),
               "Cycles with issued DMA and an active IME tile"),
      ADD_STAT(dmaChannelsHighWater, statistics::units::Count::get(),
               "Maximum concurrently active async DMA channels"),
      ADD_STAT(dependencyStalls, statistics::units::Count::get(),
               "Control cycles waiting on channel/token/SRAM dependency"),
      ADD_STAT(dmaChannelsInFlightHighWater, statistics::units::Count::get(),
               "Maximum channels with accepted requests awaiting responses"),
      ADD_STAT(
          firstPayloadAcceptedTick, statistics::units::Tick::get(),
          "First payload request accepted since stats reset; zero if none"),
      ADD_STAT(lastPayloadResponseTick, statistics::units::Tick::get(),
               "Last payload response, including errors, since stats reset"),
      ADD_STAT(acceptedPacketsPerCycleHighWater,
               statistics::units::Count::get(),
               "Maximum accepted DMA packets in one NPU cycle"),
      ADD_STAT(acceptedBytesPerCycleHighWater, statistics::units::Byte::get(),
               "Maximum accepted DMA packet bytes in one NPU cycle")
{}

NpuV01::NpuV01(const Params &p)
    : BasicPioDevice(p, 0x10000),
      dma(*this),
      requestor(p.system->getRequestorId(this)),
      enableAsync(p.enable_async_dma),
      interrupt(p.interrupt ? p.interrupt->get() : nullptr),
      faultInterrupt(p.fault_interrupt ? p.fault_interrupt->get() : nullptr),
      streamId(p.stream_id),
      maxOutstanding(p.max_outstanding),
      dmaWidth(p.dma_width),
      dmaIssueWidth(p.dma_issue_width),
      bankWidth(p.sram_bank_width),
      bankDepth(p.bank_queue_depth),
      sramLatency(p.sram_latency),
      sram(p.sram_size, 0),
      banks(p.sram_banks),
      progress([this] { tick(); }, name() + ".progress"),
      stats(this)
{
    fatal_if(sram.size() != 2 * 1024 * 1024 || banks.size() != 16,
             "NPU v0.1 ABI requires 2MiB/16 banks");
    fatal_if(!p.experimental_scaling &&
                 (bankWidth != 4 || maxOutstanding > 16 || dmaWidth > 64 ||
                  dmaIssueWidth != 1),
             "NPU resource expansion requires experimental_scaling");
    fatal_if(!maxOutstanding || maxOutstanding > 1024 || !dmaWidth ||
                 dmaWidth > 512 || !dmaIssueWidth || dmaIssueWidth > 8 ||
                 !bankWidth || bankWidth > 32 ||
                 (bankWidth & (bankWidth - 1)) || !bankDepth ||
                 !uint64_t(sramLatency),
             "Invalid NPU finite resource parameters");
    fatal_if(p.experimental_scaling && (dmaWidth & (dmaWidth - 1)),
             "Experimental NPU DMA width must be a power of two");
}

Port &
NpuV01::getPort(const std::string &name, PortID id)
{
    if (name == "dma") {
        return dma;
    }
    return BasicPioDevice::getPort(name, id);
}

bool
NpuV01::DmaPort::recvTimingResp(PacketPtr pkt)
{
    owner.response(pkt);
    return true;
}

void
NpuV01::DmaPort::recvReqRetry()
{
    owner.blocked = false;
    owner.wake();
}

void
NpuV01::updateIrqs()
{
    if (interrupt) {
        if (irqStatus & irqMask & 1) {
            interrupt->raise();
        } else {
            interrupt->clear();
        }
    }
    if (faultInterrupt) {
        if (irqStatus & irqMask & 2) {
            faultInterrupt->raise();
        } else {
            faultInterrupt->clear();
        }
    }
}

Tick
NpuV01::read(PacketPtr pkt)
{
    if (pkt->getSize() != 4 || (pkt->getAddr() & 3)) {
        pkt->makeResponse();
        pkt->setBadAddress();
        return pioDelay;
    }
    const Addr off = pkt->getAddr() - pioAddr;
    uint32_t value = 0;
    const uint64_t cycles = (status & 2) && !resetting
                                ? (curTick() - jobStart) / clockPeriod()
                                : perfCycles;
    switch (off) {
        case 0x00:
            value = 0x00010001;
            break;
        case 0x04:
            value = 2 | (1 << 9) | (enableAsync ? 1 << 10 : 0);
            break;
        case 0x08:
            value = 2 | ((enableAsync ? 3 : 1) << 8);
            break;
        case 0x10:
            value = control;
            break;
        case 0x14:
            value = status;
            break;
        case 0x20:
            value = kernelIova;
            break;
        case 0x24:
            value = kernelIova >> 32;
            break;
        case 0x28:
            value = kernelBytes;
            break;
        case 0x30:
            value = contextIova;
            break;
        case 0x34:
            value = contextIova >> 32;
            break;
        case 0x38:
            value = hartMask;
            break;
        case 0x50:
            value = irqStatus;
            break;
        case 0x54:
            value = irqMask;
            break;
        case 0x60:
            value = faultCode;
            break;
        case 0x68:
            value = faultIova;
            break;
        case 0x6c:
            value = faultIova >> 32;
            break;
        case 0x100:
            value = cycles;
            break;
        case 0x104:
            value = cycles >> 32;
            break;
        case 0x108:
            value = perfDma;
            break;
        case 0x10c:
            value = perfDma >> 32;
            break;
        case 0x110:
            value = perfMac;
            break;
        case 0x114:
            value = perfMac >> 32;
            break;
        case 0x118:
            value = perfStalls;
            break;
        case 0x11c:
            value = perfStalls >> 32;
            break;
    }
    pkt->setLE<uint32_t>(value);
    pkt->makeResponse();
    return pioDelay;
}

Tick
NpuV01::write(PacketPtr pkt)
{
    if (pkt->getSize() != 4 || (pkt->getAddr() & 3)) {
        pkt->makeResponse();
        pkt->setBadAddress();
        return pioDelay;
    }
    const uint32_t value = pkt->getLE<uint32_t>();
    switch (pkt->getAddr() - pioAddr) {
        case 0x10:
            if (value & 2) {
                stop(true);
            } else if ((value & 4) && (status & 2)) {
                stop(false, 9);
            }
            control = value & 1;
            break;
        case 0x20:
            kernelIova = (kernelIova & ~Addr(0xffffffff)) | value;
            break;
        case 0x24:
            kernelIova = (kernelIova & 0xffffffff) | Addr(value) << 32;
            break;
        case 0x28:
            kernelBytes = value;
            break;
        case 0x30:
            contextIova = (contextIova & ~Addr(0xffffffff)) | value;
            break;
        case 0x34:
            contextIova = (contextIova & 0xffffffff) | Addr(value) << 32;
            break;
        case 0x38:
            hartMask = value;
            break;
        case 0x40:
            if (value & 1) {
                launch();
            }
            break;
        case 0x50:
            irqStatus &= ~(value & 3);
            updateIrqs();
            break;
        case 0x54:
            irqMask = value & 3;
            updateIrqs();
            break;
    }
    pkt->makeResponse();
    return pioDelay;
}

void
NpuV01::wake()
{
    if (!progress.scheduled()) {
        schedule(progress, clockEdge(Cycles(1)));
    }
}

void
NpuV01::launch()
{
    if (!(status & 2)) {
        jobStart = curTick();
    }
    if (!(control & 1)) {
        fail(1);
        return;
    }
    if (status & 2) {
        status |= 8;
        faultCode = 2;
        faultIova = 0;
        irqStatus |= 2;
        ++perfStalls;
        ++stats.faults;
        updateIrqs();
        return;
    }
    if (!hartMask || (hartMask & ~3)) {
        fail(4, contextIova);
        return;
    }
    if (kernelBytes < 28 || kernelBytes > (1 << 20)) {
        fail(3, kernelIova);
        return;
    }
    launchPackage = kernelIova;
    launchContext = contextIova;
    launchHarts = ((hartMask & 1) != 0) + ((hartMask & 2) != 0);
    status = 2;
    faultCode = 0;
    faultIova = 0;
    jobStart = curTick();
    package.assign(kernelBytes, 0);
    context.assign(40, 0);
    ops.clear();
    bindings.clear();
    opIndex = 0;
    tokens = tokensDone = 0;
    asyncVariant = false;
    phase = Phase::Package;
    transfer(launchPackage, package.size(), false, false, 0, &package,
             [this] { parsePackage(); });
}

void
NpuV01::finish()
{
    phase = Phase::Idle;
    status = 1 | 4 | (status & 8);
    perfCycles = (curTick() - jobStart) / clockPeriod();
    ++stats.jobs;
    stats.jobTicks += curTick() - jobStart;
    irqStatus |= 1;
    updateIrqs();
}

void
NpuV01::fail(uint32_t fault, Addr address)
{
    if (stopping) {
        return;
    }
    faultIova = address;
    stop(false, fault);
}

void
NpuV01::stop(bool reset, uint32_t fault)
{
    stopping = true;
    resetting = reset;
    resetDone = 0;
    stopFault = fault;
    status = 2;
    phase = Phase::Idle;
    transfers.clear();
    for (auto &tile : tiles) {
        tile = Tile{};
    }
    // Unaccepted packets have no external owner; accepted writes must drain.
    for (auto &entry : entries) {
        if (!entry->accepted || !entry->pkt) {
            if (entry->pkt) {
                delete entry->pkt->popSenderState();
                entry->pkt->popSenderState();
                delete entry->pkt;
                entry->pkt = nullptr;
            }
            entry->finished = true;
        }
    }
    // Accepted SRAM beats retire before RESET clears the storage.
    for (auto &task : sramTasks) {
        task->next = task->addresses.size();
        task->complete = nullptr;
    }
    wake();
}

void
NpuV01::stopped()
{
    if (resetting && !resetDone) {
        resetDone =
            clockEdge(Cycles(sram.size() / (banks.size() * bankWidth)));
        schedule(progress, resetDone);
        return;
    }
    if (resetting) {
        std::fill(sram.begin(), sram.end(), 0);
        kernelIova = contextIova = faultIova = 0;
        kernelBytes = faultCode = irqStatus = irqMask = 0;
        hartMask = 3;
        perfCycles = perfDma = perfMac = perfStalls = 0;
        status = 1;
        ++stats.resets;
    } else {
        faultCode = stopFault;
        status = 1 | 8;
        irqStatus |= 2;
        ++perfStalls;
        ++stats.faults;
        perfCycles = (curTick() - jobStart) / clockPeriod();
    }
    stopping = resetting = false;
    updateIrqs();
}

void
NpuV01::transfer(Addr address, size_t size, bool write, bool payload,
                 uint32_t local, std::vector<uint8_t> *buffer,
                 std::function<void()> complete, int channel)
{
    if (!addressRange(address, size)) {
        fail(7, address);
        return;
    }
    auto tx = std::make_shared<Transfer>();
    tx->address = address;
    tx->size = size;
    tx->write = write;
    tx->payload = payload;
    tx->local = local;
    tx->buffer = buffer;
    tx->complete = std::move(complete);
    tx->channel = channel;
    transfers.push_back(tx);
    unsigned active = 0;
    for (const auto &t : transfers) {
        active += t->channel >= 0;
    }
    stats.dmaChannelsHighWater =
        std::max(stats.dmaChannelsHighWater.value(), double(active));
    wake();
}

bool
NpuV01::hazard(uint32_t local, uint64_t bytes, bool write) const
{
    for (const auto &tx : transfers) {
        if (tx->payload && (write || !tx->write) && bytes && tx->size &&
            local < tx->local + tx->size && tx->local < local + bytes) {
            return true;
        }
    }
    return false;
}

void
NpuV01::fillDma()
{
    std::vector<std::shared_ptr<Transfer>> candidates(transfers.begin(),
                                                      transfers.end());
    size_t empty = 0;
    while (entries.size() < maxOutstanding && empty < candidates.size()) {
        const auto &tx = candidates[dmaTurn++ % candidates.size()];
        if (tx->issued == tx->size) {
            ++empty;
            continue;
        }
        empty = 0;
        auto entry = std::make_unique<Entry>();
        Entry *e = entry.get();
        e->transfer = tx;
        e->offset = tx->issued;
        const Addr address = tx->address + e->offset;
        e->size = std::min({tx->size - e->offset, size_t(64 - (address & 63)),
                            size_t(4096 - (address & 4095))});
        auto req = std::make_shared<Request>(address, e->size,
                                             Request::UNCACHEABLE, requestor);
        req->setStreamId(streamId);
        e->pkt =
            new Packet(req, tx->write ? MemCmd::WriteReq : MemCmd::ReadReq);
        e->pkt->allocate();
        e->pkt->pushSenderState(e);
        e->pkt->pushSenderState(new SMMUv3Fault());
        entries.push_back(std::move(entry));
        tx->issued += e->size;
        ++tx->inflight;
        stats.outstandingHighWater = std::max(
            stats.outstandingHighWater.value(), double(entries.size()));
        if (tx->write) {
            auto task =
                sramTask(false, localRange(tx->local + e->offset, e->size),
                         std::vector<uint8_t>(e->size), nullptr);
            task->complete = [this, e, task] {
                if (!stopping) {
                    std::memcpy(e->pkt->getPtr<uint8_t>(), task->bytes.data(),
                                e->size);
                    e->ready = true;
                }
            };
        } else {
            e->ready = true;
        }
    }
}

void
NpuV01::sendDma()
{
    if (blocked || curTick() < linkAvailable) {
        return;
    }
    unsigned packets = 0;
    unsigned channels = 0;
    size_t bytes = 0;
    for (const auto &pending : entries) {
        if (pending->accepted && pending->pkt &&
            pending->transfer->channel >= 0) {
            channels |= 1U << pending->transfer->channel;
        }
    }
    for (auto &entry : entries) {
        Entry *e = entry.get();
        if (!e->ready || e->accepted || e->finished) {
            continue;
        }
        // A narrow link serializes one packet; wide links share one budget
        // across all DMA channels and both data directions in this cycle.
        if (packets && bytes + e->size > dmaWidth) {
            break;
        }
        if (!dma.sendTimingReq(e->pkt)) {
            blocked = true;
            ++stats.dmaRetries;
            return;
        }
        e->accepted = true;
        if (e->transfer->payload && !stats.firstPayloadAcceptedTick.value()) {
            stats.firstPayloadAcceptedTick = curTick();
        }
        if (e->transfer->channel >= 0) {
            channels |= 1U << e->transfer->channel;
        }
        const unsigned count =
            (channels & 1) + ((channels >> 1) & 1) + ((channels >> 2) & 1);
        stats.dmaChannelsInFlightHighWater = std::max(
            stats.dmaChannelsInFlightHighWater.value(), double(count));
        ++stats.dmaRequests;
        ++packets;
        bytes += e->size;
        stats.acceptedPacketsPerCycleHighWater = std::max(
            stats.acceptedPacketsPerCycleHighWater.value(), double(packets));
        stats.acceptedBytesPerCycleHighWater = std::max(
            stats.acceptedBytesPerCycleHighWater.value(), double(bytes));
        linkAvailable = clockEdge(Cycles((bytes + dmaWidth - 1) / dmaWidth));
        if (packets == dmaIssueWidth || bytes >= dmaWidth) {
            break;
        }
    }
}

void
NpuV01::response(PacketPtr pkt)
{
    auto *fault = dynamic_cast<SMMUv3Fault *>(pkt->popSenderState());
    panic_if(!fault, "NPU response missing caller-owned fault state");
    auto *entry = dynamic_cast<Entry *>(pkt->popSenderState());
    panic_if(!entry, "NPU response missing DMA entry");
    entry->pkt = nullptr;
    if (entry->transfer->payload) {
        stats.lastPayloadResponseTick = curTick();
    }
    if (pkt->isError()) {
        stats.dmaFailedBytes += entry->size;
        const uint32_t code = fault->type == 0x13 ? 8 : 7;
        entry->finished = true;
        fail(code, entry->transfer->address + entry->offset);
    } else if (stopping) {
        entry->finished = true;
    } else if (entry->transfer->write) {
        stats.dmaWriteBytes += entry->size;
        perfDma += entry->size;
        entry->finished = true;
    } else if (entry->transfer->payload) {
        stats.dmaReadBytes += entry->size;
        perfDma += entry->size;
        sramTask(
            true,
            localRange(entry->transfer->local + entry->offset, entry->size),
            std::vector<uint8_t>(pkt->getConstPtr<uint8_t>(),
                                 pkt->getConstPtr<uint8_t>() + entry->size),
            [entry] { entry->finished = true; });
    } else {
        stats.metadataReadBytes += entry->size;
        std::memcpy(entry->transfer->buffer->data() + entry->offset,
                    pkt->getConstPtr<uint8_t>(), entry->size);
        entry->finished = true;
    }
    delete fault;
    delete pkt;
    wake();
}

std::shared_ptr<NpuV01::SramTask>
NpuV01::sramTask(bool write, std::vector<uint32_t> addresses,
                 std::vector<uint8_t> bytes, std::function<void()> complete)
{
    auto task = std::make_shared<SramTask>();
    task->write = write;
    task->addresses = std::move(addresses);
    task->bytes = std::move(bytes);
    task->complete = std::move(complete);
    sramTasks.push_back(task);
    return task;
}

void
NpuV01::stepSram()
{
    for (auto &bank : banks) {
        while (!bank.empty() && bank.front().ready <= curTick()) {
            Beat beat = bank.front();
            bank.pop_front();
            auto &task = *beat.task;
            for (size_t i = beat.offset; i < beat.offset + beat.count; ++i) {
                if (task.write) {
                    sram[task.addresses[i]] = task.bytes[i];
                } else {
                    task.bytes[i] = sram[task.addresses[i]];
                }
            }
            if (task.write) {
                stats.sramWriteBytes += beat.count;
            } else {
                stats.sramReadBytes += beat.count;
            }
            --task.pending;
        }
    }
    const size_t clients = sramTasks.size();
    std::vector<bool> used(banks.size(), false);
    for (size_t client = 0; client < clients; ++client) {
        auto task = sramTasks.front();
        sramTasks.pop_front();
        if (task->next == task->addresses.size() && !task->pending) {
            auto complete = std::move(task->complete);
            if (complete) {
                complete();
            }
            continue;
        }
        while (task->next < task->addresses.size()) {
            const size_t begin = task->next;
            const uint32_t word = task->addresses[begin] / bankWidth;
            const size_t index = word % banks.size();
            if (used[index] || banks[index].size() >= bankDepth) {
                ++stats.sramBankStalls;
                break;
            }
            size_t end = begin + 1;
            while (end < task->addresses.size() &&
                   task->addresses[end] / bankWidth == word) {
                ++end;
            }
            banks[index].push_back(
                {task, begin, end - begin, clockEdge(sramLatency)});
            task->next = end;
            ++task->pending;
            ++stats.sramBankBeats;
            used[index] = true;
        }
        sramTasks.push_back(task);
    }
    // Rotate first arbitration grant across DMA and IME consumers.
    if (sramTasks.size() > 1) {
        sramTasks.push_back(sramTasks.front());
        sramTasks.pop_front();
    }
}

void
NpuV01::tick()
{
    stepSram();
    entries.remove_if([](const auto &e) {
        if (e->finished) {
            --e->transfer->inflight;
        }
        return e->finished;
    });
    if (stopping) {
        if (entries.empty() && sramTasks.empty()) {
            stopped();
        }
    } else {
        if (!transfers.empty()) {
            fillDma();
            sendDma();
            for (auto it = transfers.begin(); it != transfers.end();) {
                auto tx = *it;
                if (tx->issued == tx->size && !tx->inflight) {
                    it = transfers.erase(it);
                    auto complete = std::move(tx->complete);
                    complete();
                } else {
                    ++it;
                }
            }
        }
        if (phase == Phase::Mac && !entries.empty()) {
            ++stats.dmaMacOverlapCycles;
        }
        if (phase == Phase::Mac) {
            stepMac();
        } else if (phase == Phase::Issue) {
            issue();
        }
    }
    if (status & 2) {
        wake();
    } else if (drainState() == DrainState::Draining) {
        signalDrainDone();
    }
}

void
NpuV01::parsePackage()
{
    const uint8_t *p = package.data();
    const uint32_t count = le32(p + 12), table = le32(p + 16);
    const uint32_t metadata = le32(p + 20), total = le32(p + 24);
    if (le32(p) != 0x474b504e || le16(p + 4) != 1 || le16(p + 6) ||
        le32(p + 8) || !count || count > 64 || total < 28 ||
        total > package.size() || !range(table, uint64_t(count) * 32, total) ||
        metadata > total) {
        fail(3, launchPackage);
        return;
    }
    for (unsigned i = 0; i < count; ++i) {
        const uint8_t *v = p + table + i * 32;
        const uint32_t offset = le32(v + 8), size = le32(v + 12);
        const uint32_t entry = le32(v + 16);
        if (!range(offset, size, total) || entry > size || le32(v + 20) ||
            le32(v + 24) || le32(v + 28)) {
            fail(3, launchPackage);
            return;
        }
        const uint32_t kind = le32(v), features = le32(v + 4);
        if (kind != 1 && !(enableAsync && kind == 4)) {
            continue;
        }
        if ((kind == 1 && (features & ~3U)) ||
            (kind == 4 && ((features & ~7U) || !(features & 4)))) {
            continue;
        }
        asyncVariant = kind == 4;
        if (size - entry < 32 || (size - entry) % 32) {
            fail(3, launchPackage);
            return;
        }
        for (size_t j = 0; j < size - entry; j += 32) {
            const uint8_t *o = p + offset + entry + j;
            ops.push_back({le16(o), le16(o + 2), le32(o + 4), le32(o + 8),
                           le32(o + 12), le32(o + 16), le32(o + 20),
                           le32(o + 24), le32(o + 28)});
        }
        phase = Phase::Context;
        transfer(launchContext, context.size(), false, false, 0, &context,
                 [this] { parseContext(); });
        return;
    }
    fail(3, launchPackage);
}

void
NpuV01::parseContext()
{
    const uint8_t *c = context.data();
    const unsigned count = le16(c + 4);
    if (le16(c) != 2 || le16(c + 2) || count > 256 || le16(c + 6) ||
        le32(c + 36)) {
        fail(4, launchContext);
        return;
    }
    if (!count) {
        preflight();
        return;
    }
    rawBindings.assign(count * 16, 0);
    phase = Phase::Bindings;
    transfer(le64(c + 8), rawBindings.size(), false, false, 0, &rawBindings,
             [this] { parseBindings(); });
}

void
NpuV01::parseBindings()
{
    for (size_t i = 0; i < rawBindings.size(); i += 16) {
        const uint8_t *b = rawBindings.data() + i;
        Binding binding{le64(b), le32(b + 8), le32(b + 12)};
        if (!binding.flags || (binding.flags & ~3) ||
            !addressRange(binding.iova, binding.bytes)) {
            fail(4, binding.iova);
            return;
        }
        bindings.push_back(binding);
    }
    preflight();
}

void
NpuV01::preflight()
{
    uint64_t totalDma = 0, totalMac = 0, totalPages = 0;
    size_t count = 0;
    uint8_t usedTokens = 0;
    for (const auto &op : ops) {
        if (++count > 4096) {
            fail(10);
            return;
        }
        if (op.flags || op.extra) {
            fail(5);
            return;
        }
        if (op.code == 0) {
            if (op.dst || op.a || op.b || op.m || op.n || op.k) {
                fail(5);
                return;
            }
            phase = Phase::Issue;
            return;
        }
        if (op.code == 0x102 && asyncVariant) {
            if (!op.m || op.m > 255 || (op.m & ~usedTokens) || op.dst ||
                op.a || op.b || op.n || op.k) {
                fail(5);
                return;
            }
            usedTokens &= ~op.m;
            continue;
        }
        const bool asynchronous = op.code == 0x100 || op.code == 0x101;
        if (op.code == 1 || op.code == 2 || asynchronous) {
            const bool write = op.code == 2 || op.code == 0x101;
            const uint32_t local = write ? op.a : op.dst;
            const uint32_t index = write ? op.dst : op.a;
            if (asynchronous) {
                if (!asyncVariant || op.k > 7 || (usedTokens & (1 << op.k)) ||
                    (write ? op.n != 2 : op.n > 1)) {
                    fail(5);
                    return;
                }
                usedTokens |= 1 << op.k;
            } else if (op.n || op.k) {
                fail(5);
                return;
            }
            if (!range(local, op.m, sram.size())) {
                fail(6);
                return;
            }
            if (index >= bindings.size()) {
                fail(5);
                return;
            }
            const auto &binding = bindings[index];
            if (!(binding.flags & (write ? 2 : 1)) ||
                !range(op.b, op.m, binding.bytes) ||
                binding.iova > std::numeric_limits<Addr>::max() - op.b ||
                !addressRange(binding.iova + op.b, op.m)) {
                fail(5);
                return;
            }
            totalDma += op.m;
            if (op.m) {
                totalPages +=
                    (((binding.iova + op.b) & 4095) + op.m + 4095) / 4096;
            }
            if (totalDma > 64 * 1024 * 1024 || totalPages > 32768) {
                fail(10);
                return;
            }
        } else if (op.code == 3) {
            if (!op.m || !op.n || !op.k || op.k > 131071) {
                fail(5);
                return;
            }
            const uint64_t a = uint64_t(op.m) * op.k;
            const uint64_t b = uint64_t(op.k) * op.n;
            const uint64_t outputs = uint64_t(op.m) * op.n;
            // Check before multiplication by four to reject 64-bit overflow.
            if (outputs > std::numeric_limits<uint64_t>::max() / 4) {
                fail(5);
                return;
            }
            const uint64_t bytes = outputs * 4;
            if (!range(op.a, a, sram.size()) || !range(op.b, b, sram.size()) ||
                !range(op.dst, bytes, sram.size())) {
                fail(6);
                return;
            }
            if ((op.dst < op.a + a && op.a < op.dst + bytes) ||
                (op.dst < op.b + b && op.b < op.dst + bytes)) {
                fail(5);
                return;
            }
            totalMac += outputs * op.k;
            if (totalMac > 16 * 1024 * 1024) {
                fail(10);
                return;
            }
        } else {
            fail(5);
            return;
        }
    }
    fail(3);
}

void
NpuV01::issue()
{
    const Op &op = ops.at(opIndex);
    if (!op.code) {
        if (transfers.empty()) {
            finish();
        } else {
            ++stats.dependencyStalls;
        }
        return;
    }
    if (op.code == 0x102) {
        if ((tokensDone & op.m) != op.m) {
            ++stats.dependencyStalls;
            return;
        }
        tokens &= ~op.m;
        tokensDone &= ~op.m;
        ++opIndex;
        return;
    }
    const bool asynchronous = op.code == 0x100 || op.code == 0x101;
    if (op.code == 1 || op.code == 2 || asynchronous) {
        const bool write = op.code == 2 || op.code == 0x101;
        const unsigned binding = write ? op.dst : op.a;
        const uint32_t local = write ? op.a : op.dst;
        if (asynchronous) {
            bool busy = hazard(local, op.m, !write);
            for (const auto &tx : transfers) {
                busy |= tx->channel == int(op.n);
            }
            if (busy) {
                ++stats.dependencyStalls;
                return;
            }
            const uint8_t token = 1 << op.k;
            tokens |= token;
            transfer(
                bindings[binding].iova + op.b, op.m, write, true, local,
                nullptr, [this, token] { tokensDone |= token; }, op.n);
            ++opIndex;
        } else {
            if (!transfers.empty()) {
                ++stats.dependencyStalls;
                return;
            }
            phase = Phase::Dma;
            transfer(bindings[binding].iova + op.b, op.m, write, true, local,
                     nullptr, [this] {
                         ++opIndex;
                         phase = Phase::Issue;
                     });
        }
    } else {
        if (hazard(op.a, uint64_t(op.m) * op.k, false) ||
            hazard(op.b, uint64_t(op.k) * op.n, false) ||
            hazard(op.dst, uint64_t(op.m) * op.n * 4, true)) {
            ++stats.dependencyStalls;
            return;
        }
        nextTile = 0;
        tileCount = ((op.m + 7) / 8) * ((op.n + 7) / 8);
        phase = Phase::Mac;
        for (unsigned engine = 0; engine < launchHarts; ++engine) {
            startTile(engine);
        }
    }
}

void
NpuV01::startTile(unsigned engine)
{
    auto &tile = tiles[engine];
    tile = Tile{};
    if (nextTile == tileCount) {
        return;
    }
    const auto &op = ops[opIndex];
    const unsigned columns = (op.n + 7) / 8;
    tile.row = (nextTile / columns) * 8;
    tile.col = (nextTile % columns) * 8;
    tile.rows = std::min(8U, op.m - tile.row);
    tile.cols = std::min(8U, op.n - tile.col);
    tile.active = true;
    ++nextTile;
    tileOperands(engine);
}

void
NpuV01::tileOperands(unsigned engine)
{
    auto &tile = tiles[engine];
    const auto &op = ops[opIndex];
    std::vector<uint32_t> addresses;
    for (unsigned r = 0; r < tile.rows; ++r) {
        addresses.push_back(op.a + (tile.row + r) * op.k + tile.inner);
    }
    for (unsigned c = 0; c < tile.cols; ++c) {
        addresses.push_back(op.b + tile.inner * op.n + tile.col + c);
    }
    tile.operands =
        sramTask(false, std::move(addresses),
                 std::vector<uint8_t>(tile.rows + tile.cols), [this, engine] {
                     tiles[engine].compute = clockEdge(Cycles(1));
                 });
}

void
NpuV01::stepMac()
{
    const auto &op = ops[opIndex];
    for (unsigned engine = 0; engine < launchHarts; ++engine) {
        auto &tile = tiles[engine];
        if (!tile.active || !tile.compute || tile.compute > curTick()) {
            continue;
        }
        tile.compute = 0;
        for (unsigned r = 0; r < tile.rows; ++r) {
            const int32_t a = int8_t(tile.operands->bytes[r]);
            for (unsigned c = 0; c < tile.cols; ++c) {
                const int32_t b = int8_t(tile.operands->bytes[tile.rows + c]);
                tile.sums[r * 8 + c] += uint32_t(a * b);
            }
        }
        const uint64_t work = tile.rows * tile.cols;
        perfMac += work;
        stats.macs += work;
        ++tile.inner;
        if (tile.inner < op.k) {
            tileOperands(engine);
            continue;
        }
        std::vector<uint32_t> addresses;
        std::vector<uint8_t> bytes;
        for (unsigned r = 0; r < tile.rows; ++r) {
            for (unsigned c = 0; c < tile.cols; ++c) {
                const uint32_t address =
                    op.dst + ((tile.row + r) * op.n + tile.col + c) * 4;
                for (unsigned lane = 0; lane < 4; ++lane) {
                    addresses.push_back(address + lane);
                    bytes.push_back(tile.sums[r * 8 + c] >> (lane * 8));
                }
            }
        }
        sramTask(true, std::move(addresses), std::move(bytes),
                 [this, engine] { startTile(engine); });
    }
    bool active = false;
    for (const auto &tile : tiles) {
        active |= tile.active;
    }
    if (!active) {
        ++opIndex;
        phase = Phase::Issue;
    }
}

DrainState
NpuV01::drain()
{
    return (status & 2) ? DrainState::Draining : DrainState::Drained;
}

void
NpuV01::serialize(CheckpointOut &cp) const
{
    fatal_if(status & 2, "Drain NPU before checkpointing");
    BasicPioDevice::serialize(cp);
    SERIALIZE_SCALAR(control);
    SERIALIZE_SCALAR(status);
    SERIALIZE_SCALAR(kernelBytes);
    SERIALIZE_SCALAR(hartMask);
    SERIALIZE_SCALAR(irqStatus);
    SERIALIZE_SCALAR(irqMask);
    SERIALIZE_SCALAR(faultCode);
    SERIALIZE_SCALAR(kernelIova);
    SERIALIZE_SCALAR(contextIova);
    SERIALIZE_SCALAR(faultIova);
    SERIALIZE_SCALAR(perfCycles);
    SERIALIZE_SCALAR(perfDma);
    SERIALIZE_SCALAR(perfMac);
    SERIALIZE_SCALAR(perfStalls);
    SERIALIZE_CONTAINER(sram);
}

void
NpuV01::unserialize(CheckpointIn &cp)
{
    BasicPioDevice::unserialize(cp);
    UNSERIALIZE_SCALAR(control);
    UNSERIALIZE_SCALAR(status);
    UNSERIALIZE_SCALAR(kernelBytes);
    UNSERIALIZE_SCALAR(hartMask);
    UNSERIALIZE_SCALAR(irqStatus);
    UNSERIALIZE_SCALAR(irqMask);
    UNSERIALIZE_SCALAR(faultCode);
    UNSERIALIZE_SCALAR(kernelIova);
    UNSERIALIZE_SCALAR(contextIova);
    UNSERIALIZE_SCALAR(faultIova);
    UNSERIALIZE_SCALAR(perfCycles);
    UNSERIALIZE_SCALAR(perfDma);
    UNSERIALIZE_SCALAR(perfMac);
    UNSERIALIZE_SCALAR(perfStalls);
    UNSERIALIZE_CONTAINER(sram);
    fatal_if((status & 2) || sram.size() != 2 * 1024 * 1024,
             "Invalid NPU idle checkpoint");
    updateIrqs();
}
} // namespace gem5
