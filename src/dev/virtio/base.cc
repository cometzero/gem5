/*
 * Copyright (c) 2014, 2016, 2021 ARM Limited
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

#include "dev/virtio/base.hh"

#include "base/trace.hh"
#include "debug/VIO.hh"
#include "params/VirtIODeviceBase.hh"
#include "params/VirtIODummyDevice.hh"
#include "sim/serialize.hh"

namespace gem5
{

VirtDescriptor::VirtDescriptor(PortProxy &_memProxy, ByteOrder bo,
                               VirtQueue &_queue, Index descIndex)
    : memProxy(&_memProxy), queue(&_queue), byteOrder(bo), _index(descIndex),
      desc{0, 0, 0, 0}
{
}

VirtDescriptor::VirtDescriptor(VirtDescriptor &&other) noexcept
{
    *this = std::forward<VirtDescriptor>(other);
}

VirtDescriptor::~VirtDescriptor() noexcept
{
}

VirtDescriptor &
VirtDescriptor::operator=(VirtDescriptor &&rhs) noexcept
{
    memProxy = std::move(rhs.memProxy);
    queue = std::move(rhs.queue);
    byteOrder = std::move(rhs.byteOrder);
    _index = std::move(rhs._index);
    desc = std::move(rhs.desc);

    return *this;
}

void
VirtDescriptor::update()
{
    const Addr vq_addr(queue->getAddress());
    // Check if the queue has been initialized yet
    if (!queue->isReady()) {
        return;
    }

    assert(_index < queue->getSize());
    const Addr desc_addr(vq_addr + sizeof(desc) * _index);
    vring_desc guest_desc;
    memProxy->readBlob(desc_addr, &guest_desc, sizeof(guest_desc));
    desc = gtoh(guest_desc, byteOrder);
    DPRINTF(VIO,
            "VirtDescriptor(%i): Addr: 0x%x, Len: %i, Flags: 0x%x, "
            "Next: 0x%x\n",
            _index, desc.addr, desc.len, desc.flags, desc.next);
}

void
VirtDescriptor::updateChain()
{
    VirtDescriptor *desc(this);
    unsigned count = 0;
    do {
        panic_if(++count > queue->getSize(), "Loop in descriptor chain!");
        desc->update();
        panic_if(desc->desc.flags & VRING_DESC_F_INDIRECT,
                 "Indirect descriptors were not negotiated");
    } while ((desc = desc->next()) != NULL && desc != this);

    if (desc == this)
        panic("Loop in descriptor chain!\n");
}

void
VirtDescriptor::dump() const
{
    if (!debug::VIO)
        return;

    DPRINTF(VIO, "Descriptor[%i]: "
            "Addr: 0x%x, Len: %i, Flags: 0x%x, Next: 0x%x\n",
            _index, desc.addr, desc.len, desc.flags, desc.next);

    if (isIncoming()) {
        auto data = std::make_unique<uint8_t[]>(desc.len);
        read(0, data.get(), desc.len);
        DDUMP(VIO, data.get(), desc.len);
    }
}

void
VirtDescriptor::dumpChain() const
{
    if (!debug::VIO)
        return;

    const VirtDescriptor *desc(this);
    do {
        desc->dump();
    } while ((desc = desc->next()) != NULL);
}

VirtDescriptor *
VirtDescriptor::next() const
{
    if (hasNext()) {
        return queue->getDescriptor(desc.next);
    } else {
        return NULL;
    }
}

void
VirtDescriptor::read(size_t offset, uint8_t *dst, size_t size) const
{
    DPRINTF(VIO, "VirtDescriptor(%p, 0x%x, %i)::read: offset: %i, dst: 0x%x, size: %i\n",
            this, desc.addr, desc.len, offset, (long)dst, size);
    assert(offset <= desc.len && size <= desc.len - offset);
    if (!isIncoming())
        panic("Trying to read from outgoing buffer\n");

    memProxy->readBlob(desc.addr + offset, dst, size);
}

void
VirtDescriptor::write(size_t offset, const uint8_t *src, size_t size)
{
    DPRINTF(VIO, "VirtDescriptor(%p, 0x%x, %i)::write: offset: %i, src: 0x%x, size: %i\n",
            this, desc.addr, desc.len, offset, (long)src, size);
    assert(offset <= desc.len && size <= desc.len - offset);
    if (!isOutgoing())
        panic("Trying to write to incoming buffer\n");

    memProxy->writeBlob(desc.addr + offset, src, size);
}

void
VirtDescriptor::chainRead(size_t offset, uint8_t *dst, size_t size) const
{
    const VirtDescriptor *desc(this);
    const size_t full_size(size);
    do {
        if (offset < desc->size()) {
            const size_t chunk_size(std::min(desc->size() - offset, size));
            desc->read(offset, dst, chunk_size);
            dst += chunk_size;
            size -= chunk_size;
            offset = 0;
        } else {
            offset -= desc->size();
        }
    } while ((desc = desc->next()) != NULL && desc->isIncoming() && size > 0);

    if (size != 0) {
        panic("Failed to read %i bytes from chain of %i bytes @ offset %i\n",
              full_size, chainSize(), offset);
    }
}

void
VirtDescriptor::chainWrite(size_t offset, const uint8_t *src, size_t size)
{
    VirtDescriptor *desc(this);
    const size_t full_size(size);
    do {
        if (offset < desc->size()) {
            const size_t chunk_size(std::min(desc->size() - offset, size));
            desc->write(offset, src, chunk_size);
            src += chunk_size;
            size -= chunk_size;
            offset = 0;
        } else {
            offset -= desc->size();
        }
    } while ((desc = desc->next()) != NULL && size > 0);

    if (size != 0) {
        panic("Failed to write %i bytes into chain of %i bytes @ offset %i\n",
              full_size, chainSize(), offset);
    }
}

size_t
VirtDescriptor::chainSize() const
{
    size_t size(0);
    const VirtDescriptor *desc(this);
    do {
        size += desc->size();
    } while ((desc = desc->next()) != NULL);

    return size;
}

VirtQueue::VirtQueue(PortProxy &proxy, ByteOrder bo, uint16_t size)
    : byteOrder(bo),
      _maxSize(size),
      _size(size),
      _ready(false),
      _address(0),
      memProxy(proxy),
      avail(proxy, bo, size),
      used(proxy, bo, size),
      _last_avail(0)
{
    fatal_if(!size || (size & (size - 1)),
             "Virtqueue size must be a nonzero power of two");
    descriptors.reserve(_size);
    for (int i = 0; i < _size; ++i)
        descriptors.emplace_back(proxy, bo, *this, i);
}

void
VirtQueue::serialize(CheckpointOut &cp) const
{
    SERIALIZE_SCALAR(_address);
    SERIALIZE_SCALAR(_last_avail);
    SERIALIZE_SCALAR(_size);
    SERIALIZE_SCALAR(_ready);
    paramOut(cp, "availableAddress", avail.getAddress());
    paramOut(cp, "usedAddress", used.getAddress());
}

void
VirtQueue::unserialize(CheckpointIn &cp)
{
    Addr addr_in;

    paramIn(cp, "_address", addr_in);
    UNSERIALIZE_SCALAR(_last_avail);

    // Use the address setter to ensure that the ring buffer addresses
    // are updated as well.
    setAddress(addr_in);
    uint16_t size_in = _maxSize;
    optParamIn(cp, "_size", size_in);
    fatal_if(!setSize(size_in), "Invalid checkpoint virtqueue size");
    Addr available = avail.getAddress();
    Addr used_addr = used.getAddress();
    optParamIn(cp, "availableAddress", available);
    optParamIn(cp, "usedAddress", used_addr);
    setAddresses(addr_in, available, used_addr);
    optParamIn(cp, "_ready", _ready);
}

void
VirtQueue::reset()
{
    _address = 0;
    _last_avail = 0;
    _ready = false;
    _size = _maxSize;
    avail.ring.resize(_size);
    used.ring.resize(_size);

    avail.reset();
    used.reset();
}

void
VirtQueue::setAddress(Addr address)
{
    const Addr addr_avail(address + _size * sizeof(struct vring_desc));
    const Addr addr_avail_end(addr_avail + sizeof(struct vring_avail) +
                              _size * sizeof(uint16_t));
    const Addr addr_used((addr_avail_end + sizeof(uint16_t) +
                          (ALIGN_SIZE - 1)) & ~(ALIGN_SIZE - 1));
    _address = address;
    avail.setAddress(addr_avail);
    used.setAddress(addr_used);
    _ready = address != 0;
}

void
VirtQueue::setAddresses(Addr desc, Addr available, Addr used_addr)
{
    _address = desc;
    avail.setAddress(available);
    used.setAddress(used_addr);
}

bool
VirtQueue::setSize(uint32_t size)
{
    if (!size || size > _maxSize || (size & (size - 1))) {
        return false;
    }
    _size = size;
    avail.ring.resize(size);
    used.ring.resize(size);
    return true;
}

bool
VirtQueue::setReady(bool ready)
{
    if (ready && ((_address & 15) || (avail.getAddress() & 1) ||
                  (used.getAddress() & 3))) {
        return false;
    }
    _ready = ready;
    return true;
}

VirtDescriptor *
VirtQueue::consumeDescriptor()
{
    if (!_ready) {
        return nullptr;
    }
    avail.read();
    DPRINTF(VIO, "consumeDescriptor: _last_avail: %i, avail.idx: %i (->%i)\n",
            _last_avail, avail.header.index,
            avail.ring[_last_avail % used.ring.size()]);
    if (_last_avail == avail.header.index)
        return NULL;
    panic_if(uint16_t(avail.header.index - _last_avail) > _size,
             "Virtqueue available ring overrun");

    VirtDescriptor::Index index(avail.ring[_last_avail % used.ring.size()]);
    ++_last_avail;

    VirtDescriptor *d(getDescriptor(index));
    d->updateChain();

    return d;
}

void
VirtQueue::produceDescriptor(VirtDescriptor *desc, uint32_t len)
{
    used.readHeader();
    DPRINTF(VIO, "produceDescriptor: dscIdx: %i, len: %i, used.idx: %i\n",
            desc->index(), len, used.header.index);

    const size_t index = used.header.index % used.ring.size();
    struct vring_used_elem &e(used.ring[index]);
    e.id = desc->index();
    e.len = len;
    used.header.index += 1;
    used.writeElement(index);
}

void
VirtQueue::dump() const
{
    if (!debug::VIO)
        return;

    for (const VirtDescriptor &d : descriptors)
        d.dump();
}

void
VirtQueue::onNotify()
{
    DPRINTF(VIO, "onNotify\n");

    // Consume all pending descriptors from the input queue.
    VirtDescriptor *d;
    while ((d = consumeDescriptor()) != NULL)
        onNotifyDescriptor(d);
}


VirtIODeviceBase::VirtIODeviceBase(const Params &params, DeviceId id,
                                   size_t config_size, FeatureBits features)
    : SimObject(params),
      guestFeatures(0),
      byteOrder(params.byte_order),
      deviceId(id), configSize(config_size), deviceFeatures(features),
      _deviceStatus(0), _queueSelect(0)
{
}


VirtIODeviceBase::~VirtIODeviceBase()
{
}

void
VirtIODeviceBase::serialize(CheckpointOut &cp) const
{
    SERIALIZE_SCALAR(guestFeatures);
    SERIALIZE_SCALAR(_deviceStatus);
    SERIALIZE_SCALAR(_queueSelect);
    for (QueueID i = 0; i < _queues.size(); ++i)
        _queues[i]->serializeSection(cp, csprintf("_queues.%i", i));
}

void
VirtIODeviceBase::unserialize(CheckpointIn &cp)
{
    UNSERIALIZE_SCALAR(guestFeatures);
    UNSERIALIZE_SCALAR(_deviceStatus);
    UNSERIALIZE_SCALAR(_queueSelect);
    for (QueueID i = 0; i < _queues.size(); ++i)
        _queues[i]->unserializeSection(cp, csprintf("_queues.%i", i));
}

void
VirtIODeviceBase::reset()
{
    _queueSelect = 0;
    guestFeatures = 0;
    _deviceStatus = 0;

    for (QueueID i = 0; i < _queues.size(); ++i)
        _queues[i]->reset();
}

void
VirtIODeviceBase::onNotify(QueueID idx)
{
    DPRINTF(VIO, "onNotify: idx: %i\n", idx);
    if (_modern && (!_deviceStatus.driver_ok || !_deviceStatus.features_ok ||
                    _deviceStatus.failed || idx >= _queues.size() ||
                    !_queues[idx]->isReady())) {
        return;
    }
    if (idx >= _queues.size()) {
        panic("Guest tried to notify queue (%i), but only %i "
              "queues registered.\n",
              idx, _queues.size());
    }
    _queues[idx]->onNotify();
}

void
VirtIODeviceBase::setGuestFeatures(FeatureBits features)
{
    DPRINTF(VIO, "Setting guest features: 0x%x\n", features);
    if (!_modern && (~deviceFeatures & features)) {
        panic("Guest tried to enable unsupported features:\n"
              "Device features: 0x%x\n"
              "Requested features: 0x%x\n",
              deviceFeatures, features);
    }
    guestFeatures = features;
}

void
VirtIODeviceBase::enableModern()
{
    fatal_if(byteOrder != ByteOrder::little,
             "Modern VirtIO requires little-endian device byte order");
    _modern = true;
}

void
VirtIODeviceBase::setDeviceStatus(DeviceStatus status)
{
    const bool reset_requested = status == 0;
    if (_modern && status.features_ok &&
        ((guestFeatures & ~getDeviceFeatures()) ||
         !(guestFeatures & F_VERSION_1))) {
        status.features_ok = 0;
    }
    if (_modern && !status.features_ok) {
        status.driver_ok = 0;
    }
    _deviceStatus = status;
    DPRINTF(VIO, "ACK: %i, DRIVER: %i, DRIVER_OK: %i, FAILED: %i\n",
            status.acknowledge, status.driver, status.driver_ok, status.failed);
    if (reset_requested) {
        reset();
    }
}

void
VirtIODeviceBase::readConfig(PacketPtr pkt, Addr cfgOffset)
{
    panic("Unhandled device config read (offset: 0x%x).\n", cfgOffset);
}

void
VirtIODeviceBase::writeConfig(PacketPtr pkt, Addr cfgOffset)
{
    panic("Unhandled device config write (offset: 0x%x).\n", cfgOffset);
}

void
VirtIODeviceBase::readConfigBlob(PacketPtr pkt, Addr cfgOffset, const uint8_t *cfg)
{
    const unsigned size(pkt->getSize());

    if (cfgOffset + size > configSize)
        panic("Config read out of bounds.\n");

    pkt->makeResponse();
    pkt->setData(const_cast<uint8_t *>(cfg) + cfgOffset);
}

void
VirtIODeviceBase::writeConfigBlob(PacketPtr pkt, Addr cfgOffset, uint8_t *cfg)
{
    const unsigned size(pkt->getSize());

    if (cfgOffset + size > configSize)
        panic("Config write out of bounds.\n");

    pkt->makeResponse();
    pkt->writeData((uint8_t *)cfg + cfgOffset);
}


const VirtQueue &
VirtIODeviceBase::getCurrentQueue() const
{
    if (_queueSelect >= _queues.size())
        panic("Guest tried to access non-existing VirtQueue (%i).\n", _queueSelect);

    return *_queues[_queueSelect];
}

VirtQueue &
VirtIODeviceBase::getCurrentQueue()
{
    if (_queueSelect >= _queues.size())
        panic("Guest tried to access non-existing VirtQueue (%i).\n", _queueSelect);

    return *_queues[_queueSelect];
}

void
VirtIODeviceBase::setQueueAddress(uint32_t address)
{
    getCurrentQueue().setAddress(address * VirtQueue::ALIGN_SIZE);
}

uint32_t
VirtIODeviceBase::getQueueAddress() const
{
    Addr address(getCurrentQueue().getAddress());
    assert(!(address & (VirtQueue::ALIGN_SIZE - 1)));
    return address >> VirtQueue::ALIGN_BITS;
}

void
VirtIODeviceBase::registerQueue(VirtQueue &queue)
{
    _queues.push_back(&queue);
}


VirtIODummyDevice::VirtIODummyDevice(const VirtIODummyDeviceParams &params)
    : VirtIODeviceBase(params, ID_INVALID, 0, 0)
{
}

} // namespace gem5
