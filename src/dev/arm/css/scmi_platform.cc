/*
 * Copyright (c) 2020 ARM Limited
 * All rights reserved
 *
 * The license below extends only to copyright in the software and shall
 * not be construed as granting a license to any other intellectual
 * property including but not limited to intellectual property relating
 * to a hardware implementation of the functionality of the software
 * licensed hereunder.  You may use the software subject to the license
 * terms below provided that you ensure that this notice is replicated
 * unmodified and in its entirety in all distributions of the software,
 * modified or unmodified, in source code or in binary form.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are
 * met: redistributions of source code must retain the above copyright
 * notice, this list of conditions and the following disclaimer;
 * redistributions in binary form must reproduce the above copyright
 * notice, this list of conditions and the following disclaimer in the
 * documentation and/or other materials provided with the distribution;
 * neither the name of the copyright holders nor the names of its
 * contributors may be used to endorse or promote products derived from
 * this software without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 * "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 * LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
 * A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
 * OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
 * SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
 * LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
 * DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
 * THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 * OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */

#include "dev/arm/css/scmi_platform.hh"

#include <cmath>
#include <cstddef>

#include "debug/SCMI.hh"
#include "dev/arm/css/mhu.hh"
#include "dev/arm/doorbell.hh"
#include "mem/packet_access.hh"
#include "mem/port_proxy.hh"
#include "sim/dvfs_handler.hh"
#include "sim/power/thermal_domain.hh"

namespace gem5
{

using namespace scmi;

AgentChannel::AgentChannel(const ScmiChannelParams &p)
  : VirtualChannel(p),
    readLengthEvent([this]{ readLength(); }, name()),
    readMessageEvent([this]{ readMessage(); }, name()),
    handleMessageEvent([this]{ handleMessage(); }, name())
{}

void
AgentChannel::initiateRead()
{
    if (!pendingMessage) {
        pendingMessage = true;
        msgBuffer = Message();
        readStatus();
    } else {
        DPRINTF(SCMI, "Pending message\n");
    }
}

void
AgentChannel::readStatus()
{
    const auto offset = offsetof(Message, channelStatus);
    const Addr address = shmem.start() + offset;

    // Reading the the mailbox to check the
    // channel status. The value will be handled by the readLength
    // event/method
    dmaPort->dmaAction(MemCmd::ReadReq, address, 16, &readLengthEvent,
                       (uint8_t *)&msgBuffer.channelStatus, 0,
                       Request::UNCACHEABLE);
}

void
AgentChannel::readLength()
{
    DPRINTF(SCMI, "SCMI Virtual channel %u, channel.status: %u\n",
            virtID, msgBuffer.channelStatus);

    // Check if the channel is busy. If it is busy it means there is a
    // message so we need to process it. Abort the reads otherwise
    if (msgBuffer.channelStatus & 0x1) {
        // Channel is free: Terminate: reset message buffer
        pendingMessage = false;
        msgBuffer = Message();
    } else {
        // Read mailbox length
        const auto offset = offsetof(Message, length);
        const Addr address = shmem.start() + offset;

        dmaPort->dmaAction(MemCmd::ReadReq, address, sizeof(msgBuffer.length),
                           &readMessageEvent, (uint8_t*)&msgBuffer.length,
                           0, Request::UNCACHEABLE);
    }
}

void
AgentChannel::readMessage()
{
    const auto offset = offsetof(Message, header);
    const Addr address = shmem.start() + offset;

    DPRINTF(SCMI, "SCMI Virtual channel %u, message.length: %u\n",
            virtID, msgBuffer.length);

    if (msgBuffer.length < sizeof(uint32_t) ||
        msgBuffer.length > sizeof(msgBuffer.header) + sizeof(Payload) ||
        offset + msgBuffer.length > shmem.size() || msgBuffer.length % 4) {
        msgBuffer.header = 0;
        msgBuffer.length = 8;
        msgBuffer.payload.status = INVALID_PARAMETERS;
        platform->find(this)->writeBackMessage(msgBuffer);
        return;
    }

    dmaPort->dmaAction(MemCmd::ReadReq, address,
                       msgBuffer.length,
                       &handleMessageEvent, (uint8_t*)&msgBuffer.header,
                       0, Request::UNCACHEABLE);
}

void
AgentChannel::handleMessage()
{
    DPRINTF(SCMI,
            "SCMI Virtual channel %u, message.header: %#x\n",
            virtID, msgBuffer.header);

    // Send the message to the platform which is gonna handle it
    // We are also forwarding a pointer to the agent channel so
    // the platform can retrieve the platform channel
    platform->handleMessage(this, msgBuffer);
}

PlatformChannel::PlatformChannel(const ScmiChannelParams &p)
    : VirtualChannel(p),
      clearDoorbellEvent([this] { clearDoorbell(); }, name()),
      releaseChannelEvent([this] { releaseChannel(); }, name()),
      notifyAgentEvent([this] { notifyAgent(); }, name()),
      completeEvent([this] { complete(); }, name()),
      agentDoorbellVal(0),
      platformDoorbellVal(0)
{}

void
PlatformChannel::writeBackMessage(const Message &msg)
{
    DPRINTF(SCMI,
            "SCMI Virtual channel %u, writing back message %u"
            " with status code: %d\n",
            virtID, Platform::messageID(msg), msg.payload.status);

    pendingMessage = true;
    // Field by field copy of the message
    msgBuffer = msg;

    // Publish the free flag only after every response byte and the doorbell
    // clear have completed. A polling agent may reuse the mailbox immediately.
    msgBuffer.channelStatus = 0;

    dmaPort->dmaAction(MemCmd::WriteReq, shmem.start(),
                       offsetof(Message, header) + msg.length,
                       &clearDoorbellEvent, (uint8_t *)&msgBuffer, 0,
                       Request::UNCACHEABLE);
}

void
PlatformChannel::clearDoorbell()
{
    DPRINTF(SCMI,
            "SCMI Virtual channel %u, clearing doorbell\n",
            virtID);

    AgentChannel *agent_ch = platform->find(this);
    agentDoorbellVal = 0xffffffff;
    dmaPort->dmaAction(MemCmd::WriteReq, agent_ch->doorbell->clearAddress(),
                       sizeof(uint32_t), &releaseChannelEvent,
                       (uint8_t *)&agentDoorbellVal, 0, Request::UNCACHEABLE);
}

void
PlatformChannel::releaseChannel()
{
    platform->find(this)->pendingMessage = false;
    msgBuffer.channelStatus = 1;
    dmaPort->dmaAction(
        MemCmd::WriteReq, shmem.start() + offsetof(Message, channelStatus),
        sizeof(msgBuffer.channelStatus), &notifyAgentEvent,
        (uint8_t *)&msgBuffer.channelStatus, 0, Request::UNCACHEABLE);
}

void
PlatformChannel::notifyAgent()
{
    DPRINTF(SCMI,
            "SCMI Virtual channel %u, notifying agent\n",
            virtID);

    if (!(msgBuffer.mailboxFlags & 1)) {
        complete();
        return;
    }
    platformDoorbellVal = 1 << virtID;
    dmaPort->dmaAction(MemCmd::WriteReq, doorbell->setAddress(),
                       sizeof(uint32_t),
                       &completeEvent, (uint8_t*)&platformDoorbellVal,
                       0, Request::UNCACHEABLE);
}

void
PlatformChannel::complete()
{
    pendingMessage = false;
    msgBuffer = Message();
}

Platform::Platform(const ScmiPlatformParams &p)
    : Scp(p),
      dvfs(p.dvfs),
      thermalDomains(p.thermal_domains),
      levels(p.perf_levels),
      costs(p.perf_costs),
      comms(p.comms),
      agents(p.agents),
      protocols({{BASE, new BaseProtocol(*this)}}),
      dmaPort(this, p.sys),
      temperatureMilliC(p.dvfs ? p.dvfs->numDomains() : 0,
                        p.initial_temperature_mc),
      replyEvent(
          [this] {
              find(replyChannel)->writeBackMessage(replyMessage);
              replyChannel = nullptr;
          },
          name(), false, Event::DVFS_Update_Pri + 1)
{
    for (const auto &agent_name : agents) {
        fatal_if(agent_name.length() > Protocol::MAX_STRING_SIZE,
                 "Invalid BASE_PROTOCOL AGENT size\n");
    }

    if (dvfs) {
        fatal_if(comms.size() != 1,
                 "SCMI control requires one synchronous communication");
        fatal_if(!dvfs->isEnabled() || levels.empty() ||
                     levels.size() != costs.size() || levels.size() > 20,
                 "Invalid SCMI DVFS backend");
        for (unsigned i = 0; i < dvfs->numDomains(); ++i) {
            fatal_if(dvfs->domainID(i) != i ||
                         dvfs->numPerfLevels(i) != levels.size(),
                     "SCMI requires contiguous DVFS domains and common OPPs");
        }
        fatal_if(!thermalDomains.empty() &&
                     thermalDomains.size() != dvfs->numDomains(),
                 "SCMI needs one thermal sensor per DVFS domain");
        sensorConfig.assign(dvfs->numDomains(), 1);
        fatal_if(p.initial_temperature_mc < -40000 ||
                     p.initial_temperature_mc > 150000,
                 "Invalid initial synthetic sensor temperature");
        for (unsigned i = 1; i < levels.size(); ++i) {
            fatal_if(levels[i] >= levels[i - 1],
                     "SCMI performance levels must be strictly descending");
        }
        minLevels.assign(dvfs->numDomains(), levels.back());
        maxLevels.assign(dvfs->numDomains(), levels.front());
        protocols.emplace(PERFORMANCE_DOMAIN,
                          new ControlProtocol(*this, true));
        protocols.emplace(SENSOR, new ControlProtocol(*this, false));
    }
    for (auto comm : comms) {
        fatal_if(comm->agentChan->shmem.size() < sizeof(Message) ||
                     comm->platformChan->shmem.size() < sizeof(Message),
                 "SCMI shared memory is too small");
        comm->agentChan->dmaPort = &dmaPort;
        comm->agentChan->setPlatform(this);

        comm->platformChan->dmaPort = &dmaPort;
        comm->platformChan->setPlatform(this);
    }

    fatal_if(numProtocols() >= PROTOCOL_MAX,
        "The number of instantiated protocols are not matching the"
        " architected limit");
}

Platform::~Platform()
{
    for (auto& kv : protocols) {
        delete kv.second;
    }
}

Port &
Platform::getPort(const std::string &if_name, PortID idx)
{
    if (if_name == "dma") {
        return dmaPort;
    }
    return Scp::getPort(if_name, idx);
}

void
Platform::handleMessage(AgentChannel *agent_ch, Message &msg)
{
    dispatchMessage(msg);
    if (replyAt > curTick()) {
        panic_if(replyEvent.scheduled(), "Overlapping SCMI response");
        replyChannel = agent_ch;
        replyMessage = msg;
        schedule(replyEvent, replyAt);
    } else {
        find(agent_ch)->writeBackMessage(msg);
    }
}

void
Platform::dispatchMessage(Message &msg)
{
    replyAt = curTick();
    auto it = protocols.find(protocolID(msg));
    if (messageType(msg) != 0 || it == protocols.end()) {
        msg.payload.status = NOT_SUPPORTED;
        msg.length = 8;
        return;
    }
    // All Base commands with arguments consume one word.
    if (protocolID(msg) == BASE) {
        unsigned id = messageID(msg);
        bool argument = id == 2 || id == 6 || id == 7;
        if (msg.length != (argument ? 8 : 4) && id <= 7) {
            msg.payload.status = INVALID_PARAMETERS;
            msg.length = 8;
            return;
        }
    }
    it->second->handleMessage(msg);
}

bool
Platform::idle() const
{
    if (dmaPort.dmaPending() || replyEvent.scheduled() ||
        replyAt > curTick()) {
        return false;
    }
    for (auto comm : comms) {
        auto agent =
            dynamic_cast<const MhuDoorbell *>(comm->agentChan->doorbell);
        auto platform =
            dynamic_cast<const MhuDoorbell *>(comm->platformChan->doorbell);
        if ((agent && !agent->idle()) || (platform && !platform->idle())) {
            return false;
        }
        if (comm->agentChan->pendingMessage ||
            comm->platformChan->pendingMessage) {
            return false;
        }
    }
    return true;
}

void
Platform::initState()
{
    PortProxy proxy(dmaPort, 64);
    uint32_t free = 1;
    for (auto comm : comms) {
        proxy.writeBlob(comm->agentChan->shmem.start() + 4, &free, 4);
    }
}

void
Platform::setTemperatureMilliC(unsigned id, int64_t value)
{
    fatal_if(id >= temperatureMilliC.size() || value < -40000 ||
                 value > 150000,
             "Invalid SCMI synthetic sensor value");
    fatal_if(!thermalDomains.empty(), "Native thermal sensors are read-only");
    temperatureMilliC[id] = value;
}

int64_t
Platform::getTemperatureMilliC(unsigned id) const
{
    return thermalDomains.empty()
               ? temperatureMilliC.at(id)
               : std::llround(
                     thermalDomains.at(id)->currentTemperature().toCelsius() *
                     1000);
}

void
Platform::serialize(CheckpointOut &cp) const
{
    fatal_if(!idle(), "SCMI checkpoint requires idle control channels");
    SERIALIZE_CONTAINER(temperatureMilliC);
    SERIALIZE_CONTAINER(sensorConfig);
    SERIALIZE_CONTAINER(minLevels);
    SERIALIZE_CONTAINER(maxLevels);
}

void
Platform::unserialize(CheckpointIn &cp)
{
    UNSERIALIZE_CONTAINER(temperatureMilliC);
    UNSERIALIZE_CONTAINER(sensorConfig);
    UNSERIALIZE_CONTAINER(minLevels);
    UNSERIALIZE_CONTAINER(maxLevels);
}

void
Platform::raiseInterrupt(const Doorbell *doorbell)
{
    DPRINTF(SCMI, "Raise interrupt in SCMI platform\n");

    // Now we need to read the physical channel in the mailbox
    // to get the virtual channel, we avoid this

    // Select the associated virtual channel with the doorbell
    for (auto comm : comms) {
        auto channel = comm->agentChan;
        if (channel->doorbell == doorbell) {
            // There is a matching virtual channel: make it
            // start reading the message the shared memory area
            channel->initiateRead();
            return;
        }
    }

    panic("No matching virtual channel\n");
}

void
Platform::clearInterrupt(const Doorbell *doorbell)
{
    DPRINTF(SCMI, "Clear interrupt in SCMI platform\n");
}

AgentChannel*
Platform::find(PlatformChannel* platform) const
{
    for (auto comm : comms) {
        if (comm->platformChan == platform) {
            return comm->agentChan;
        }
    }

    return nullptr;
}

PlatformChannel*
Platform::find(AgentChannel* agent) const
{
    for (auto comm : comms) {
        if (comm->agentChan == agent) {
            return comm->platformChan;
        }
    }

    return nullptr;
}

} // namespace gem5
