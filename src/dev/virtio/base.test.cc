/*
 * Copyright (c) 2026 Chanho Park
 * SPDX-License-Identifier: BSD-3-Clause
 */

#include <gtest/gtest.h>

#include <array>
#include <cstring>

#include "base/gtest/logging.hh"
#include "dev/virtio/base.hh"
#include "params/VirtIODeviceBase.hh"
#include "sim/eventq.hh"

namespace gem5
{
namespace
{

class TestQueue : public VirtQueue
{
  public:
    explicit TestQueue(PortProxy &proxy)
        : VirtQueue(proxy, ByteOrder::little, 8)
    {}

    unsigned notifications = 0;
    void
    onNotifyDescriptor(VirtDescriptor *desc) override
    {
        ++notifications;
    }
};

class TestDevice : public VirtIODeviceBase
{
  public:
    TestDevice(const VirtIODeviceBaseParams &params, VirtQueue &queue)
        : VirtIODeviceBase(params, 1, 0, 1ULL << 5)
    {
        registerQueue(queue);
    }
};

class VirtQueueTest : public testing::Test
{
  protected:
    // Non-contiguous rings above 4 GiB detect address truncation and use of
    // the legacy calculated ring layout by the modern transport.
    static constexpr Addr Base = 0x123000000ULL;
    static constexpr Addr Desc = Base + 0x1000;
    static constexpr Addr Available = Base + 0x5000;
    static constexpr Addr Used = Base + 0x8000;
    static constexpr Addr Buffer = Base + 0xc000;
    std::array<uint8_t, 0x10000> memory{};
    PortProxy proxy;
    TestQueue queue;

    VirtQueueTest()
        : proxy([this](PacketPtr pkt) { access(pkt); }, 64), queue(proxy)
    {
        curEventQueue(getEventQueue(0));
    }

    void
    access(PacketPtr pkt)
    {
        ASSERT_GE(pkt->getAddr(), Base);
        ASSERT_LE(pkt->getAddr() + pkt->getSize(), Base + memory.size());
        auto *data = memory.data() + pkt->getAddr() - Base;
        if (pkt->isRead()) {
            std::memcpy(pkt->getPtr<uint8_t>(), data, pkt->getSize());
        } else {
            std::memcpy(data, pkt->getConstPtr<uint8_t>(), pkt->getSize());
        }
        pkt->makeResponse();
    }

    void
    configure()
    {
        ASSERT_TRUE(queue.setSize(4));
        queue.setAddresses(Desc, Available, Used);
        ASSERT_TRUE(queue.setReady(true));
    }

    void
    descriptor(uint16_t index, uint16_t flags = VRING_DESC_F_WRITE,
               uint16_t next = 0)
    {
        const vring_desc desc = {Buffer + index * 16, 16, flags, next};
        proxy.write(Desc + sizeof(desc) * index, desc, ByteOrder::little);
    }

    void
    available(uint16_t index, uint16_t head = 0)
    {
        proxy.write<uint16_t>(Available + 4 +
                                  2 * ((index - 1) % queue.getSize()),
                              head, ByteOrder::little);
        proxy.write<uint16_t>(Available + 2, index, ByteOrder::little);
    }
};

TEST_F(VirtQueueTest, Separate64BitRingsAndWritableChain)
{
    configure();
    descriptor(0, VRING_DESC_F_WRITE | VRING_DESC_F_NEXT, 1);
    descriptor(1);
    available(1);
    auto *head = queue.consumeDescriptor();
    ASSERT_NE(head, nullptr);
    EXPECT_EQ(head->chainSize(), 32);
    std::array<uint8_t, 32> bytes;
    for (size_t i = 0; i < bytes.size(); ++i) {
        bytes[i] = i + 1;
    }
    head->chainWrite(0, bytes.data(), bytes.size());
    EXPECT_EQ(0, std::memcmp(memory.data() + Buffer - Base, bytes.data(),
                             bytes.size()));
    queue.produceDescriptor(head, bytes.size());
    EXPECT_EQ(proxy.read<uint16_t>(Used + 2, ByteOrder::little), 1);
    auto used = proxy.read<vring_used_elem>(Used + 4, ByteOrder::little);
    EXPECT_EQ(used.id, 0);
    EXPECT_EQ(used.len, bytes.size());
    EXPECT_EQ(queue.consumeDescriptor(), nullptr);
}

TEST_F(VirtQueueTest, CompletionPreservesOtherUsedEntries)
{
    configure();
    descriptor(0);
    available(1);
    const vring_used_elem prior = {3, 0x12345678};
    proxy.write(Used + 4, prior, ByteOrder::little);
    proxy.write<uint16_t>(Used + 2, 2, ByteOrder::little);
    queue.produceDescriptor(queue.consumeDescriptor(), 16);
    const auto saved =
        proxy.read<vring_used_elem>(Used + 4, ByteOrder::little);
    EXPECT_EQ(saved.id, prior.id);
    EXPECT_EQ(saved.len, prior.len);
    EXPECT_EQ(proxy.read<uint16_t>(Used + 2, ByteOrder::little), 3);
}

TEST_F(VirtQueueTest, SizeAndAddressValidation)
{
    EXPECT_FALSE(queue.setSize(0));
    EXPECT_FALSE(queue.setSize(3));
    EXPECT_FALSE(queue.setSize(16));
    // Physical address zero is valid in the modern transport.
    EXPECT_TRUE(queue.setReady(true));
    EXPECT_TRUE(queue.setReady(false));
    queue.setAddresses(Desc + 1, Available, Used);
    EXPECT_FALSE(queue.setReady(true));
    queue.setAddresses(Desc, Available + 1, Used);
    EXPECT_FALSE(queue.setReady(true));
    queue.setAddresses(Desc, Available, Used + 2);
    EXPECT_FALSE(queue.setReady(true));
    configure();
    EXPECT_EQ(queue.getMaxSize(), 8);
    EXPECT_EQ(queue.getSize(), 4);
}

TEST_F(VirtQueueTest, ResetAndLegacyLayout)
{
    configure();
    descriptor(0);
    available(1);
    ASSERT_NE(queue.consumeDescriptor(), nullptr);
    queue.reset();
    EXPECT_FALSE(queue.isReady());
    EXPECT_EQ(queue.consumeDescriptor(), nullptr);
    EXPECT_EQ(queue.getAddress(), 0);
    EXPECT_EQ(queue.getAvailableAddress(), 0);
    EXPECT_EQ(queue.getUsedAddress(), 0);
    EXPECT_EQ(queue.getSize(), 8);
    queue.setAddress(Desc);
    EXPECT_TRUE(queue.isReady());
    EXPECT_EQ(queue.getAvailableAddress(), Desc + 8 * sizeof(vring_desc));
    EXPECT_EQ(queue.getUsedAddress(), Desc + 0x1000);
    queue.setAddress(0);
    EXPECT_FALSE(queue.isReady());
}

TEST_F(VirtQueueTest, AvailableAndUsedIndexRollover)
{
    configure();
    descriptor(0);
    for (uint32_t index = 1; index <= 65538; ++index) {
        const uint16_t wrapped = index;
        // Cast before subtracting so index zero wraps in the 16-bit ring.
        const uint16_t slot = uint16_t(wrapped - 1) % queue.getSize();
        proxy.write<uint16_t>(Available + 4 + 2 * slot, 0, ByteOrder::little);
        proxy.write<uint16_t>(Available + 2, wrapped, ByteOrder::little);
        auto *head = queue.consumeDescriptor();
        ASSERT_NE(head, nullptr);
        queue.produceDescriptor(head, 16);
    }
    EXPECT_EQ(proxy.read<uint16_t>(Used + 2, ByteOrder::little), 2);
    EXPECT_EQ(queue.consumeDescriptor(), nullptr);
}

TEST_F(VirtQueueTest, MalformedDescriptorChains)
{
    configure();
    descriptor(0, VRING_DESC_F_NEXT, 1);
    descriptor(1, VRING_DESC_F_NEXT, 2);
    descriptor(2, VRING_DESC_F_NEXT, 1);
    available(1);
    EXPECT_THROW(queue.consumeDescriptor(), GTestException);
    descriptor(0, VRING_DESC_F_INDIRECT);
    available(2);
    EXPECT_THROW(queue.consumeDescriptor(), GTestException);
    available(3, queue.getSize());
    EXPECT_THROW(queue.consumeDescriptor(), GTestException);
}

TEST_F(VirtQueueTest, ModernFeatureNegotiationAndReset)
{
    VirtIODeviceBaseParams params;
    params.name = "virtio_test";
    params.eventq_index = 0;
    params.byte_order = ByteOrder::little;
    TestDevice device(params, queue);
    EXPECT_EQ(device.getDeviceFeatures(), 1ULL << 5);
    device.setQueueAddress(Desc >> VirtQueue::ALIGN_BITS);
    EXPECT_EQ(device.getQueueAddress(), Desc >> VirtQueue::ALIGN_BITS);
    EXPECT_EQ(queue.getAddress(), Desc);
    device.reset();
    device.enableModern();
    EXPECT_EQ(device.getDeviceFeatures(), (1ULL << 32) | (1ULL << 5));
    device.setGuestFeatures(1ULL << 5);
    device.setDeviceStatus(0xf);
    EXPECT_FALSE(device.getDeviceStatus().features_ok);
    EXPECT_FALSE(device.getDeviceStatus().driver_ok);
    device.setGuestFeatures((1ULL << 32) | (1ULL << 30));
    device.setDeviceStatus(0xf);
    EXPECT_FALSE(device.getDeviceStatus().features_ok);
    device.setGuestFeatures((1ULL << 32) | (1ULL << 5));
    device.setDeviceStatus(0xf);
    EXPECT_TRUE(device.getDeviceStatus().features_ok);
    EXPECT_TRUE(device.getDeviceStatus().driver_ok);
    configure();
    device.setQueueSelect(0x10000);
    EXPECT_EQ(device.getQueueSelect(), 0x10000);
    EXPECT_EQ(device.getQueue(device.getQueueSelect()), nullptr);
    device.onNotify(0x10000);
    device.setDeviceStatus(0);
    EXPECT_EQ(device.getDeviceStatus(), 0);
    EXPECT_EQ(device.getGuestFeatures(), 0);
    EXPECT_EQ(device.getQueueSelect(), 0);
    EXPECT_FALSE(queue.isReady());
}

TEST_F(VirtQueueTest, ModernInactiveQueuesDoNotConsumeBuffers)
{
    VirtIODeviceBaseParams params;
    params.name = "virtio_notify_test";
    params.eventq_index = 0;
    params.byte_order = ByteOrder::little;
    TestDevice device(params, queue);
    device.enableModern();
    configure();
    descriptor(0);
    available(1);
    device.onNotify(0);
    EXPECT_EQ(queue.notifications, 0);
    device.setGuestFeatures(1ULL << 32);
    device.setDeviceStatus(0xb); // FEATURES_OK without DRIVER_OK
    device.onNotify(0);
    EXPECT_EQ(queue.notifications, 0);
    device.setDeviceStatus(0xf);
    queue.setReady(false);
    device.onNotify(0);
    EXPECT_EQ(queue.notifications, 0);
    queue.setReady(true);
    device.onNotify(0);
    EXPECT_EQ(queue.notifications, 1);
    available(2);
    device.setDeviceStatus(0x8f); // FAILED
    device.onNotify(0);
    EXPECT_EQ(queue.notifications, 1);
}

} // anonymous namespace
} // namespace gem5
