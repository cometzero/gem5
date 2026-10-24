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

#include "dev/arm/css/scmi_protocols.hh"

#include <algorithm>
#include <cstring>

#include "debug/SCMI.hh"
#include "dev/arm/css/scmi_platform.hh"
#include "sim/dvfs_handler.hh"

namespace gem5
{

using namespace scmi;

const std::string
Protocol::name() const
{
    return platform.name();
}

BaseProtocol::BaseProtocol(Platform &_platform)
  : Protocol(_platform),
    vendor(platform.params().base_vendor),
    subvendor(platform.params().base_subvendor),
    implementationVersion(platform.params().base_impl_version)
{
    fatal_if(vendor.length() > MAX_STRING_SIZE,
        "Invalid BASE_PROTOCOL VENDOR size\n");
    fatal_if(subvendor.length() > MAX_STRING_SIZE,
        "Invalid BASE_PROTOCOL SUBVENDOR size\n");
}

void
BaseProtocol::handleMessage(Message &msg)
{
    auto message_id = Platform::messageID(msg);

    DPRINTF(SCMI, "Handling SCMI message:\n");
    DPRINTF(SCMI, "# Message Protocol = BASE_PROTOCOL\n");
    DPRINTF(SCMI, "# Message ID = %u\n", message_id);

    switch (static_cast<Commands>(message_id)) {
      case Commands::VERSION:
        version(msg);
        break;
      case Commands::ATTRIBUTES:
        attributes(msg);
        break;
      case Commands::MESSAGE_ATTRIBUTES:
        messageAttributes(msg);
        break;
      case Commands::DISCOVER_VENDOR:
        discoverVendor(msg);
        break;
      case Commands::DISCOVER_SUB_VENDOR:
        discoverSubVendor(msg);
        break;
      case Commands::DISCOVER_IMPLEMENTATION_VERSION:
        discoverImplVersion(msg);
        break;
      case Commands::DISCOVER_LIST_PROTOCOLS:
        discoverListProtocols(msg);
        break;
      case Commands::DISCOVER_AGENT:
        discoverAgent(msg);
        break;
      case Commands::NOTIFY_ERRORS:
      case Commands::SET_DEVICE_PERMISSIONS:
      case Commands::SET_PROTOCOL_PERMISSIONS:
      case Commands::RESET_AGENT_CONFIGURATION:
      default:
        invalidCommand(msg);
        warn("Unimplemented SCMI command: %u\n", message_id);
    }
}

void
BaseProtocol::version(Message &msg)
{
    auto& payload = msg.payload.baseProtocolVersion;
    payload.status = SUCCESS;
    payload.version = PROTOCOL_VERSION;

    // header + status + return
    msg.length = sizeof(uint32_t) * 3;
}

void
BaseProtocol::attributes(Message &msg)
{
    uint32_t _attributes = 0;

    replaceBits(_attributes, 15, 8, platform.numAgents());
    replaceBits(_attributes, 7, 0, platform.numProtocols());

    auto& payload = msg.payload.baseProtocolAttributes;
    payload.status = SUCCESS;
    payload.attributes = _attributes;

    // header + status + return
    msg.length = sizeof(uint32_t) * 3;
}

bool
BaseProtocol::implementedProtocol(Commands message_id) const
{
    switch (message_id) {
      case Commands::VERSION:
      case Commands::ATTRIBUTES:
      case Commands::MESSAGE_ATTRIBUTES:
      case Commands::DISCOVER_VENDOR:
      case Commands::DISCOVER_SUB_VENDOR:
      case Commands::DISCOVER_IMPLEMENTATION_VERSION:
      case Commands::DISCOVER_LIST_PROTOCOLS:
      case Commands::DISCOVER_AGENT:
        return true;
      default:
        return false;
    }
}

void
BaseProtocol::messageAttributes(Message &msg)
{
    auto& payload = msg.payload.baseProtocolMessageAttributes;
    const auto message_id = static_cast<Commands>(
        payload.messageId);

    if (!implementedProtocol(message_id)) {
        payload.status = NOT_FOUND;
    } else {
        payload.status = SUCCESS;
    }

    // For all messages in the Base protocol, 0 must be returned
    payload.attributes = 0;

    // header + status + return
    msg.length = sizeof(uint32_t) * 3;
}

void
BaseProtocol::discoverVendor(Message &msg)
{
    auto& payload = msg.payload.baseDiscoverVendor;
    payload.status = SUCCESS;

    std::memset(payload.vendorIdentifier, 0, 16);
    vendor.copy((char *)&payload.vendorIdentifier, MAX_STRING_SIZE);

    // header + status + payload
    msg.length = sizeof(uint32_t) * 2 + 16;
}

void
BaseProtocol::discoverSubVendor(Message &msg)
{
    auto& payload = msg.payload.baseDiscoverSubVendor;
    payload.status = SUCCESS;

    std::memset(payload.vendorIdentifier, 0, 16);
    subvendor.copy((char *)&payload.vendorIdentifier, MAX_STRING_SIZE);

    // header + status + payload
    msg.length = sizeof(uint32_t) * 2 + 16;
}

void
BaseProtocol::discoverImplVersion(Message &msg)
{
    auto& payload = msg.payload.baseDiscoverImplementationVersion;
    payload.status = SUCCESS;
    payload.implementationVersion = implementationVersion;

    // header + status + return
    msg.length = sizeof(uint32_t) * 3;
}

void
BaseProtocol::discoverListProtocols(Message &msg)
{
    auto& payload = msg.payload.baseDiscoverListProtocols;
    const uint32_t skip = payload.skip;
    const auto num_protocols = platform.numProtocols();

    if (skip > num_protocols) {
        payload.status = INVALID_PARAMETERS;
        msg.length = sizeof(uint32_t) * 2;

    } else {
        const auto& protocol_list = platform.protocolList();
        auto *protocols = (uint8_t*)payload.protocols;
        uint32_t num_implemented = 0;

        uint32_t seen = 0;
        for (unsigned protoc_id = START; protoc_id <= END; protoc_id++) {
            auto it = protocol_list.find(protoc_id);
            if (it != protocol_list.end()) {
                if (seen++ < skip) {
                    continue;
                }
                num_implemented++;

                *protocols = it->first;
                protocols++;
            }
        }

        payload.status = SUCCESS;
        payload.numProtocols = num_implemented;

        // Protocol IDs are packed bytes after numProtocols.
        msg.length = sizeof(uint32_t) * (3 + (num_implemented + 3) / 4);
    }
}

void
BaseProtocol::discoverAgent(Message &msg)
{
    auto& payload = msg.payload.baseDiscoverAgent;
    const uint32_t agent_id =
        payload.agentId == 0xffffffff ? 1 : payload.agentId;

    if (agent_id > platform.numAgents()) {
        payload.status = NOT_FOUND;
        msg.length = sizeof(uint32_t) * 2;

    } else {
        auto agent_name = std::string();

        if (agent_id)  {
            // Subtracting one to the agent_id, since agent_id 0 is reserved
            // for the platform.
            agent_name = platform.getAgent(agent_id - 1);
        } else {
            agent_name = "platform";
        }

        std::memset(payload.name, 0, 16);
        std::memcpy(payload.name, agent_name.c_str(),
                    std::min(agent_name.size(), size_t(MAX_STRING_SIZE)));

        payload.status = SUCCESS;
        payload.discoveredAgentId = agent_id;
        // header + status + agent ID + name
        msg.length = sizeof(uint32_t) * 3 + 16;
    }
}

void
BaseProtocol::invalidCommand(Message &msg)
{
    auto& payload = msg.payload.invalidCommand;
    payload.status = NOT_SUPPORTED;
    msg.length = sizeof(uint32_t) * 2;
}

namespace
{

void
reply(Message &msg, StatusCode status,
      std::initializer_list<uint32_t> words = {})
{
    msg.payload = Payload();
    msg.payload.status = status;
    unsigned i = 1;
    for (auto word : words) {
        msg.payload.words[i++] = word;
    }
    msg.length = (i + 1) * sizeof(uint32_t);
}

void
replyName(Message &msg, unsigned offset, const std::string &name)
{
    std::memcpy(&msg.payload.words[offset], name.c_str(),
                std::min(name.size(), size_t(15)));
    msg.length = (offset + 5) * sizeof(uint32_t);
}

} // anonymous namespace

bool
ControlProtocol::supported(uint32_t command) const
{
    return performance
               ? command <= 8
               : command <= 3 || command == 6 || command == 9 || command == 10;
}

void
ControlProtocol::version(Message &msg)
{
    reply(msg, SUCCESS, {0x30000});
}

void
ControlProtocol::attributes(Message &msg)
{
    // Abstract policy cost; no shared statistics area or asynchronous reads.
    reply(msg, SUCCESS, {platform.dvfs->numDomains(), 0, 0, 0});
}

void
ControlProtocol::messageAttributes(Message &msg)
{
    reply(msg, supported(msg.payload.words[0]) ? SUCCESS : NOT_FOUND, {0});
}

void
ControlProtocol::handleMessage(Message &msg)
{
    const auto command = Platform::messageID(msg);
    if (!supported(command)) {
        reply(msg, NOT_SUPPORTED);
        return;
    }
    unsigned words = command < 2 ? 0 : 1;
    if (performance && (command == 4 || command == 7)) {
        words = 2;
    }
    if (performance && command == 5) {
        words = 3;
    }
    if (!performance && (command == 6 || command == 10)) {
        words = 2;
    }
    if (msg.length != 4 * (words + 1)) {
        reply(msg, INVALID_PARAMETERS);
        return;
    }
    if (command == 0) {
        version(msg);
        return;
    }
    if (command == 1) {
        attributes(msg);
        return;
    }
    if (command == 2) {
        messageAttributes(msg);
        return;
    }
    const uint32_t id = msg.payload.words[0];
    const uint32_t arg = msg.payload.words[1];
    const uint32_t arg2 = msg.payload.words[2];
    if (!performance) {
        if (id >= platform.dvfs->numDomains()) {
            reply(msg, command == 3 ? OUT_OF_RANGE : NOT_FOUND);
            return;
        }
        switch (command) {
            case 3:
                reply(msg, SUCCESS,
                      {1 | ((platform.dvfs->numDomains() - id - 1) << 16), id,
                       0, (29 << 11) | 2});
                replyName(msg, 5, "cluster" + std::to_string(id));
                return;
            case 6: {
                if (arg) {
                    reply(msg, INVALID_PARAMETERS);
                    return;
                }
                if (!(platform.sensorConfig[id] & 1)) {
                    reply(msg, DENIED);
                    return;
                }
                uint64_t value = platform.getTemperatureMilliC(id);
                reply(msg, SUCCESS, {uint32_t(value), uint32_t(value >> 32)});
                return;
            }
            case 9:
                reply(msg, SUCCESS, {platform.sensorConfig[id]});
                return;
            case 10:
                if (arg & ~1U) {
                    reply(msg, INVALID_PARAMETERS);
                    return;
                }
                platform.sensorConfig[id] = arg;
                reply(msg, SUCCESS);
                return;
        }
    }
    auto dvfs = platform.dvfs;
    if (id >= dvfs->numDomains()) {
        reply(msg, NOT_FOUND);
        return;
    }
    const auto &levels = platform.levels;
    const auto &costs = platform.costs;
    auto findLevel = [&](uint32_t level) {
        return std::find(levels.begin(), levels.end(), level);
    };
    auto applyLevel = [&](uint32_t level) {
        unsigned index = findLevel(level) - levels.begin();
        if (dvfs->perfLevel(id) != index) {
            if (!dvfs->perfLevel(id, index)) {
                return false;
            }
            platform.replyAt = curTick() + dvfs->transLatency();
        }
        return true;
    };
    switch (command) {
        case 3:
            reply(msg, SUCCESS, {0xc0000000, 0, 2000000, 2000});
            replyName(msg, 5, "cluster" + std::to_string(id));
            return;
        case 4:
            if (arg >= levels.size()) {
                reply(msg, INVALID_PARAMETERS);
                return;
            }
            reply(msg, SUCCESS, {uint32_t(levels.size() - arg)});
            for (unsigned i = arg, w = 2; i < levels.size(); ++i) {
                unsigned index = levels.size() - i - 1;
                msg.payload.words[w++] = levels[index];
                msg.payload.words[w++] = costs[index];
                msg.payload.words[w++] =
                    dvfs->transLatency() / sim_clock::as_int::us;
            }
            msg.length = (3 + 3 * (levels.size() - arg)) * 4;
            return;
        case 5: {
            if (arg < arg2 || findLevel(arg) == levels.end() ||
                findLevel(arg2) == levels.end()) {
                reply(msg, INVALID_PARAMETERS);
                return;
            }
            uint32_t level = levels[dvfs->perfLevel(id)];
            level = std::clamp(level, arg2, arg);
            if (!applyLevel(level)) {
                reply(msg, BUSY);
                return;
            }
            platform.maxLevels[id] = arg;
            platform.minLevels[id] = arg2;
            reply(msg, SUCCESS);
            return;
        }
        case 6:
            reply(msg, SUCCESS,
                  {platform.maxLevels[id], platform.minLevels[id]});
            return;
        case 7:
            if (findLevel(arg) == levels.end() ||
                arg > platform.maxLevels[id] || arg < platform.minLevels[id]) {
                reply(msg, INVALID_PARAMETERS);
                return;
            }
            reply(msg, applyLevel(arg) ? SUCCESS : BUSY);
            return;
        case 8:
            reply(msg, SUCCESS, {levels[dvfs->perfLevel(id)]});
            return;
    }
}

} // namespace gem5
