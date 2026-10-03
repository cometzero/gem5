/*
 * Copyright (c) 2026 Chanho Park
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include "dev/virtio/net.hh"

#include <algorithm>
#include <cstring>

#include "base/inet.hh"
#include "debug/VIONet.hh"
#include "params/VirtIONet.hh"
#include "sim/system.hh"

namespace gem5
{

VirtIONet::VirtIONet(const Params &params)
    : VirtIODeviceBase(params, ID_NET, ConfigSize, Features),
      interface(*this),
      rxQueue(params.system->physProxy, byteOrder, params.qSize, *this, true),
      txQueue(params.system->physProxy, byteOrder, params.qSize, *this, false),
      rxQueueSize(params.rxQueueSize),
      retryDelay(params.retry_delay),
      txEvent([this] { transmit(); }, name() + ".txEvent")
{
    fatal_if(!rxQueueSize || !retryDelay,
             "VirtIONet requires nonzero RX buffering and retry delay");
    std::copy_n(params.hardware_address.bytes(), mac.size(), mac.begin());
    registerQueue(rxQueue);
    registerQueue(txQueue);
}

VirtIONet::~VirtIONet()
{
    if (txEvent.scheduled()) {
        deschedule(txEvent);
    }
}

VirtIONet::NetInterface::NetInterface(VirtIONet &parent)
    : EtherInt(parent.name() + ".interface"), parent(parent)
{}

bool
VirtIONet::NetInterface::recvPacket(EthPacketPtr packet)
{
    return parent.receive(packet);
}

void
VirtIONet::NetInterface::sendDone()
{
    // EtherTap can invoke this synchronously from sendPacket().
    parent.scheduleTx();
}

VirtIONet::NetQueue::NetQueue(PortProxy &proxy, ByteOrder bo, uint16_t size,
                              VirtIONet &parent, bool receive)
    : VirtQueue(proxy, bo, size), parent(parent), receive(receive)
{}

void
VirtIONet::NetQueue::onNotify()
{
    if (receive) {
        parent.deliverReceive();
    } else {
        parent.scheduleTx();
    }
}

Port &
VirtIONet::getPort(const std::string &if_name, PortID idx)
{
    if (if_name == "interface") {
        return interface;
    }
    return VirtIODeviceBase::getPort(if_name, idx);
}

bool
VirtIONet::active() const
{
    const DeviceStatus status = getDeviceStatus();
    return status.driver_ok && !status.failed &&
           (!isModern() || status.features_ok);
}

void
VirtIONet::readConfig(PacketPtr pkt, Addr cfg_offset)
{
    std::array<uint8_t, ConfigSize> config{};
    std::copy(mac.begin(), mac.end(), config.begin());
    // Port connections are static; no live link/configuration changes.
    const uint16_t status =
        htog<uint16_t>(interface.getPeer() ? 1 : 0, byteOrder);
    std::memcpy(config.data() + mac.size(), &status, sizeof(status));
    readConfigBlob(pkt, cfg_offset, config.data());
}

void
VirtIONet::writeConfig(PacketPtr pkt, Addr cfg_offset)
{
    // Legacy drivers may change the MAC through device configuration.
    // The modern MAC and status fields are read-only.
    if (!isModern() && cfg_offset + pkt->getSize() <= mac.size()) {
        pkt->writeData(mac.data() + cfg_offset);
    }
    pkt->makeResponse();
}

void
VirtIONet::scheduleTx()
{
    if (!txEvent.scheduled() && active() && txQueue.isReady()) {
        schedule(txEvent, curTick() + retryDelay);
    }
}

void
VirtIONet::transmit()
{
    if (!active() || !txQueue.isReady()) {
        txPacket.reset();
        return;
    }

    for (;;) {
        if (!txPacket) {
            VirtDescriptor *desc = txQueue.consumeDescriptor();
            if (!desc) {
                return;
            }

            const size_t size = desc->chainSize();
            bool readable = true;
            for (auto *part = desc; part; part = part->next()) {
                readable &= part->isIncoming();
            }
            if (!readable || size < headerSize() + 14 ||
                size > headerSize() + MaxFrameSize) {
                warn("VirtIONet: discarding invalid TX descriptor chain\n");
                txQueue.produceDescriptor(desc, 0);
                kick();
                continue;
            }

            uint8_t header[12]{};
            desc->chainRead(0, header, headerSize());
            if (header[0] || header[1]) {
                warn("VirtIONet: unnegotiated TX checksum/GSO offload\n");
                txQueue.produceDescriptor(desc, 0);
                kick();
                continue;
            }

            const size_t frame_size = size - headerSize();
            txPacket = std::make_shared<EthPacketData>(frame_size);
            txPacket->length = txPacket->simLength = frame_size;
            desc->chainRead(headerSize(), txPacket->data, frame_size);
            txHead = desc->index();
        }

        if (!interface.sendPacket(txPacket)) {
            scheduleTx();
            return;
        }
        DPRINTF(VIONet, "TX %u bytes\n", txPacket->length);
        txQueue.produceDescriptor(txQueue.getDescriptor(txHead), 0);
        txPacket.reset();
        kick();
    }
}

bool
VirtIONet::receive(EthPacketPtr packet)
{
    if (active() && rxQueue.isReady() && packet->length >= 14 &&
        packet->length <= MaxFrameSize && rxPackets.size() < rxQueueSize) {
        rxPackets.push_back(packet);
        deliverReceive();
    }
    // Network loss is allowed when no buffers are posted or the FIFO is full.
    // Taking ownership always completes the peer's current transmission.
    interface.recvDone();
    return true;
}

void
VirtIONet::deliverReceive()
{
    if (!active() || !rxQueue.isReady()) {
        return;
    }

    while (!rxPackets.empty()) {
        VirtDescriptor *desc = rxQueue.consumeDescriptor();
        if (!desc) {
            return;
        }

        bool writable = true;
        for (auto *part = desc; part; part = part->next()) {
            writable &= part->isOutgoing();
        }
        const auto &packet = rxPackets.front();
        const size_t size = headerSize() + packet->length;
        if (!writable || desc->chainSize() < size) {
            warn("VirtIONet: discarding undersized/read-only RX buffer\n");
            rxQueue.produceDescriptor(desc, 0);
        } else {
            uint8_t header[12]{};
            // Modern headers include num_buffers even without MRG_RXBUF.
            // One available chain always holds the complete packet.
            if (isModern()) {
                header[10] = 1;
            }
            desc->chainWrite(0, header, headerSize());
            desc->chainWrite(headerSize(), packet->data, packet->length);
            rxQueue.produceDescriptor(desc, size);
            DPRINTF(VIONet, "RX %u bytes\n", packet->length);
        }
        rxPackets.pop_front();
        kick();
    }
}

void
VirtIONet::reset()
{
    if (txEvent.scheduled()) {
        deschedule(txEvent);
    }
    txPacket.reset();
    rxPackets.clear();
    VirtIODeviceBase::reset();
}

void
VirtIONet::serialize(CheckpointOut &cp) const
{
    VirtIODeviceBase::serialize(cp);
    arrayParamOut(cp, "mac", mac.data(), mac.size());
    SERIALIZE_SCALAR(txHead);
    const bool tx_pending = bool(txPacket);
    SERIALIZE_SCALAR(tx_pending);
    if (tx_pending) {
        txPacket->serialize("txPacket", cp);
    }
    const size_t rx_count = rxPackets.size();
    SERIALIZE_SCALAR(rx_count);
    for (size_t i = 0; i < rx_count; ++i) {
        rxPackets[i]->serialize(csprintf("rxPacket%u", i), cp);
    }
    const Tick tx_tick = txEvent.scheduled() ? txEvent.when() : 0;
    SERIALIZE_SCALAR(tx_tick);
}

void
VirtIONet::unserialize(CheckpointIn &cp)
{
    VirtIODeviceBase::unserialize(cp);
    txPacket.reset();
    rxPackets.clear();
    arrayParamIn(cp, "mac", mac.data(), mac.size());
    UNSERIALIZE_SCALAR(txHead);
    bool tx_pending;
    UNSERIALIZE_SCALAR(tx_pending);
    if (tx_pending) {
        txPacket = std::make_shared<EthPacketData>();
        txPacket->unserialize("txPacket", cp);
    }
    size_t rx_count;
    UNSERIALIZE_SCALAR(rx_count);
    fatal_if(rx_count > rxQueueSize,
             "Checkpoint exceeds VirtIONet receive queue capacity");
    for (size_t i = 0; i < rx_count; ++i) {
        auto packet = std::make_shared<EthPacketData>();
        packet->unserialize(csprintf("rxPacket%u", i), cp);
        rxPackets.push_back(packet);
    }
    Tick tx_tick;
    UNSERIALIZE_SCALAR(tx_tick);
    if (tx_tick) {
        schedule(txEvent, tx_tick);
    }
}

} // namespace gem5
