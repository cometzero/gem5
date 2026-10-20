// Copyright (c) 2026 Chanho Park
// SPDX-License-Identifier: BSD-3-Clause
#ifndef __CPU_TESTERS_CHI_DMA_TEST_HH__
#define __CPU_TESTERS_CHI_DMA_TEST_HH__
#include <deque>
#include <memory>
#include <vector>

#include "mem/port.hh"
#include "params/ChiDmaTest.hh"
#include "sim/clocked_object.hh"

namespace gem5
{
class ChiDmaTest : public ClockedObject
{
    class TestPort : public RequestPort
    {
        ChiDmaTest &owner;
        unsigned index;

      public:
        TestPort(ChiDmaTest &o, unsigned i)
            : RequestPort(o.name() + ".ports" + std::to_string(i)),
              owner(o),
              index(i)
        {}
        bool
        recvTimingResp(PacketPtr pkt) override
        {
            owner.complete(pkt);
            return true;
        }
        void
        recvReqRetry() override
        {
            owner.retry(index);
        }
    };
    const ChiDmaTestParams &config;
    std::vector<std::unique_ptr<TestPort>> ports;
    RequestorID requestor;
    size_t index = 0;
    struct Operation : Packet::SenderState
    {
        size_t index;
        explicit Operation(size_t i) : index(i) {}
    };
    size_t remaining = 0;
    std::vector<std::deque<PacketPtr>> pending;
    EventFunctionWrapper nextEvent;
    void issue();
    void retry(unsigned port);
    void complete(PacketPtr pkt);

  public:
    ChiDmaTest(const ChiDmaTestParams &p);
    Port &getPort(const std::string &name,
                  PortID idx = InvalidPortID) override;
    void startup() override;
};
} // namespace gem5
#endif
