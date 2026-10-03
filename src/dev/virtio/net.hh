/*
 * Copyright (c) 2026 Chanho Park
 * SPDX-License-Identifier: BSD-3-Clause
 */

#ifndef __DEV_VIRTIO_NET_HH__
#define __DEV_VIRTIO_NET_HH__

#include <array>
#include <deque>

#include "dev/net/etherint.hh"
#include "dev/net/etherpkt.hh"
#include "dev/virtio/base.hh"
#include "sim/eventq.hh"

namespace gem5
{

struct VirtIONetParams;

/**
 * Functional VirtIO network device with one RX/TX queue pair.
 *
 * Only MAC and link status are advertised. There are no offloads, mergeable
 * receive buffers, control queue, or packet timing guarantees. Like the
 * other VirtIO models, guest buffers are accessed through a PortProxy.
 */
class VirtIONet : public VirtIODeviceBase
{
  public:
    using Params = VirtIONetParams;
    explicit VirtIONet(const Params &params);
    ~VirtIONet() override;

    Port &getPort(const std::string &if_name,
                  PortID idx = InvalidPortID) override;
    void readConfig(PacketPtr pkt, Addr cfg_offset) override;
    void writeConfig(PacketPtr pkt, Addr cfg_offset) override;
    void reset() override;
    void serialize(CheckpointOut &cp) const override;
    void unserialize(CheckpointIn &cp) override;

  private:
    static constexpr DeviceId ID_NET = 1;
    static constexpr FeatureBits Features = (1ULL << 5) | (1ULL << 16);
    static constexpr size_t ConfigSize = 8;
    static constexpr size_t MaxFrameSize = 65535;

    class NetInterface : public EtherInt
    {
      public:
        explicit NetInterface(VirtIONet &parent);
        bool recvPacket(EthPacketPtr packet) override;
        void sendDone() override;

      private:
        VirtIONet &parent;
    };

    class NetQueue : public VirtQueue
    {
      public:
        NetQueue(PortProxy &proxy, ByteOrder bo, uint16_t size,
                 VirtIONet &parent, bool receive);
        void onNotify() override;

      private:
        VirtIONet &parent;
        const bool receive;
    };

    bool active() const;
    size_t
    headerSize() const
    {
        return isModern() ? 12 : 10;
    }
    void scheduleTx();
    void transmit();
    bool receive(EthPacketPtr packet);
    void deliverReceive();

    NetInterface interface;
    NetQueue rxQueue;
    NetQueue txQueue;
    std::array<uint8_t, 6> mac;
    const unsigned rxQueueSize;
    const Tick retryDelay;
    EventFunctionWrapper txEvent;
    std::deque<EthPacketPtr> rxPackets;
    EthPacketPtr txPacket;
    VirtDescriptor::Index txHead = 0;
};

} // namespace gem5

#endif // __DEV_VIRTIO_NET_HH__
