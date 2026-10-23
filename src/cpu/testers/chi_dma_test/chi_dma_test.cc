// Copyright (c) 2026 Chanho Park
// SPDX-License-Identifier: BSD-3-Clause
#include "cpu/testers/chi_dma_test/chi_dma_test.hh"

#include "base/logging.hh"
#include "mem/packet_access.hh"
#include "sim/sim_exit.hh"
#include "sim/system.hh"

namespace gem5
{
ChiDmaTest::ChiDmaTest(const ChiDmaTestParams &p)
    : ClockedObject(p),
      config(p),
      requestor(p.system->getRequestorId(this)),
      nextEvent([this] { issue(); }, name() + ".next")
{
    fatal_if(p.commands.empty() || p.masters.size() != p.commands.size() ||
                 p.addresses.size() != p.commands.size() ||
                 p.values.size() != p.commands.size() ||
                 (!p.batches.empty() && p.batches.size() != p.commands.size()),
             "Invalid test sequence");
    for (unsigned i = 0; i < p.port_ports_connection_count; i++) {
        ports.emplace_back(std::make_unique<TestPort>(*this, i));
    }
    pending.resize(ports.size());
}
Port &
ChiDmaTest::getPort(const std::string &name, PortID idx)
{
    if (name == "ports" && idx >= 0 && idx < ports.size()) {
        return *ports[idx];
    }
    return ClockedObject::getPort(name, idx);
}
void
ChiDmaTest::startup()
{
    schedule(nextEvent, clockEdge(Cycles(1)));
}
void
ChiDmaTest::issue()
{
    if (index == config.commands.size()) {
        inform("CHI_DMA_TEST PASS operations=%u", index);
        exitSimLoop("CHI_DMA_TEST PASS");
        return;
    }
    const auto batch = config.batches.empty() ? index : config.batches[index];
    do {
        unsigned op = config.commands[index];
        Request::Flags flags = 0;
        MemCmd cmd = MemCmd::ReadReq;
        if (op == 1 || op == 6) {
            cmd = MemCmd::WriteReq;
        }
        if (op == 2) {
            cmd = MemCmd::CleanSharedReq;
            flags = Request::CLEAN;
        }
        if (op == 3) {
            cmd = MemCmd::InvalidateReq;
            flags = Request::INVALIDATE;
        }
        if (op == 4) {
            cmd = MemCmd::CleanInvalidReq;
            flags = Request::CLEAN | Request::INVALIDATE;
        }
        if (op == 5 || op == 6) {
            flags = Request::UNCACHEABLE;
        }
        fatal_if(op > 6 || config.masters[index] >= ports.size(),
                 "Invalid test op");
        auto req = std::make_shared<Request>(config.addresses[index], 8, flags,
                                             requestor);
        PacketPtr pkt = new Packet(req, cmd);
        pkt->allocate();
        if (cmd.isWrite()) {
            pkt->setLE<uint64_t>(config.values[index]);
        }
        pkt->pushSenderState(new Operation(index));
        pending[config.masters[index]].push_back(pkt);
        remaining++;
        index++;
    } while (index < config.commands.size() && !config.batches.empty() &&
             config.batches[index] == batch);
    for (unsigned port = 0; port < ports.size(); port++) {
        retry(port);
    }
}
void
ChiDmaTest::retry(unsigned port)
{
    auto &queue = pending[port];
    while (!queue.empty() && ports[port]->sendTimingReq(queue.front())) {
        queue.pop_front();
    }
}

void
ChiDmaTest::complete(PacketPtr pkt)
{
    auto state = static_cast<Operation *>(pkt->popSenderState());
    const auto completed = state->index;
    delete state;
    fatal_if(pkt->isError(), "Test op %u returned error", completed);
    // Maintenance and NC operations use CHI rather than the DMA bypass port.
    fatal_if(config.commands[completed] >= 2 && pkt->completerBusy < 0,
             "Test op %u omitted CHI completer occupancy", completed);
    if (pkt->isRead()) {
        auto got = pkt->getLE<uint64_t>();
        fatal_if(got != config.values[completed],
                 "Test op %u address %#x got %#x expected %#x", completed,
                 config.addresses[completed], got, config.values[completed]);
    }
    delete pkt;
    assert(remaining > 0);
    if (--remaining == 0) {
        schedule(nextEvent, clockEdge(Cycles(1)));
    }
}
} // namespace gem5
