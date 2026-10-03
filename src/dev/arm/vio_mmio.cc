/*
 * Copyright (c) 2016-2018 ARM Limited
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

#include "dev/arm/vio_mmio.hh"

#include <cstring>

#include "debug/VIOIface.hh"
#include "dev/arm/base_gic.hh"
#include "mem/packet_access.hh"
#include "params/MmioVirtIO.hh"

namespace gem5
{

MmioVirtIO::MmioVirtIO(const MmioVirtIOParams &params)
    : BasicPioDevice(params, params.pio_size),
      hostFeaturesSelect(0),
      guestFeaturesSelect(0),
      pageSize(0),
      interruptStatus(0),
      vio(*params.vio),
      interrupt(params.interrupt->get()),
      modern(params.modern)
{
    fatal_if(!interrupt, "No MMIO VirtIO interrupt specified\n");
    if (modern) {
        vio.enableModern();
    }

    vio.registerKickCallback([this]() { kick(); });
}

MmioVirtIO::~MmioVirtIO()
{
}

void
MmioVirtIO::serialize(CheckpointOut &cp) const
{
    BasicPioDevice::serialize(cp);
    SERIALIZE_SCALAR(hostFeaturesSelect);
    SERIALIZE_SCALAR(guestFeaturesSelect);
    SERIALIZE_SCALAR(pageSize);
    SERIALIZE_SCALAR(interruptStatus);
}

void
MmioVirtIO::unserialize(CheckpointIn &cp)
{
    BasicPioDevice::unserialize(cp);
    optParamIn(cp, "hostFeaturesSelect", hostFeaturesSelect);
    optParamIn(cp, "guestFeaturesSelect", guestFeaturesSelect);
    optParamIn(cp, "pageSize", pageSize);
    optParamIn(cp, "interruptStatus", interruptStatus);
}

void
MmioVirtIO::startup()
{
    BasicPioDevice::startup();
    if (interruptStatus) {
        interrupt->raise();
    }
}

Tick
MmioVirtIO::read(PacketPtr pkt)
{
    const Addr offset = pkt->getAddr() - pioAddr;
    const unsigned size(pkt->getSize());

    DPRINTF(VIOIface, "Reading %u bytes @ 0x%x:\n", size, offset);

    // Forward device configuration reads to the device VirtIO model.
    if (offset >= OFF_CONFIG) {
        if (modern && offset - OFF_CONFIG + size > vio.configSize) {
            std::memset(pkt->getPtr<uint8_t>(), 0, size);
            pkt->makeResponse();
            return pioDelay;
        }
        vio.readConfig(pkt, offset - OFF_CONFIG);
        return 0;
    }

    panic_if(size != 4, "Unexpected read size: %u\n", size);

    const uint32_t value = read(offset);
    DPRINTF(VIOIface, "    value: 0x%x\n", value);
    pkt->makeResponse();
    pkt->setLE<uint32_t>(value);

    return 0;
}

uint32_t
MmioVirtIO::read(Addr offset)
{
    const VirtQueue *queue = vio.getQueue(vio.getQueueSelect());
    switch(offset) {
      case OFF_MAGIC:
        return MAGIC;

      case OFF_VERSION:
          return modern ? 2 : VERSION;

      case OFF_DEVICE_ID:
        return vio.deviceId;

      case OFF_VENDOR_ID:
        return VENDOR_ID;

      case OFF_HOST_FEATURES:
          if (hostFeaturesSelect < (modern ? 2 : 1)) {
              return vio.getDeviceFeatures() >> (32 * hostFeaturesSelect);
          } else {
              return 0;
          }

      case OFF_HOST_FEATURES_SELECT:
        return hostFeaturesSelect;

      case OFF_GUEST_FEATURES:
          if (guestFeaturesSelect < (modern ? 2 : 1)) {
              return vio.getGuestFeatures() >> (32 * guestFeaturesSelect);
          } else {
              return 0;
          }

      case OFF_GUEST_FEATURES_SELECT:
          return guestFeaturesSelect;

      case OFF_GUEST_PAGE_SIZE:
          return modern ? 0 : pageSize;

      case OFF_QUEUE_SELECT:
        return vio.getQueueSelect();

      case OFF_QUEUE_NUM_MAX:
          return queue ? queue->getMaxSize() : 0;

      case OFF_QUEUE_NUM:
          return queue ? queue->getSize() : 0;

      case OFF_QUEUE_ALIGN:
        // TODO: Implement this once we support other alignment sizes
        return modern ? 0 : VirtQueue::ALIGN_SIZE;

      case OFF_QUEUE_PFN:
          return modern || !queue ? 0 : vio.getQueueAddress();

      case OFF_QUEUE_READY:
          return modern && queue && queue->isReady();

      case OFF_QUEUE_DESC_LOW:
      case OFF_QUEUE_DESC_HIGH:
          return modern && queue ? queue->getAddress() >>
                                       (offset == OFF_QUEUE_DESC_HIGH ? 32 : 0)
                                 : 0;

      case OFF_QUEUE_AVAIL_LOW:
      case OFF_QUEUE_AVAIL_HIGH:
          return modern && queue
                     ? queue->getAvailableAddress() >>
                           (offset == OFF_QUEUE_AVAIL_HIGH ? 32 : 0)
                     : 0;

      case OFF_QUEUE_USED_LOW:
      case OFF_QUEUE_USED_HIGH:
          return modern && queue ? queue->getUsedAddress() >>
                                       (offset == OFF_QUEUE_USED_HIGH ? 32 : 0)
                                 : 0;

      case OFF_CONFIG_GENERATION:
          return 0;

      case OFF_INTERRUPT_STATUS:
        return interruptStatus;

      case OFF_STATUS:
        return vio.getDeviceStatus();

        // Write-only registers
      case OFF_QUEUE_NOTIFY:
      case OFF_INTERRUPT_ACK:
          warn("Guest is trying to read a write-only register 0x%x\n", offset);
          return 0;

      default:
          if (modern) {
              return 0;
          }
        panic("Unhandled read offset (0x%x)\n", offset);
    }
}

Tick
MmioVirtIO::write(PacketPtr pkt)
{
    const Addr offset = pkt->getAddr() - pioAddr;
    const unsigned size(pkt->getSize());

    DPRINTF(VIOIface, "Writing %u bytes @ 0x%x:\n", size, offset);

    // Forward device configuration writes to the device VirtIO model
    if (offset >= OFF_CONFIG) {
        if (modern && offset - OFF_CONFIG + size > vio.configSize) {
            pkt->makeResponse();
            return pioDelay;
        }
        vio.writeConfig(pkt, offset - OFF_CONFIG);
        return 0;
    }

    panic_if(size != 4, "Unexpected write size @ 0x%x: %u\n", offset, size);
    DPRINTF(VIOIface, "    value: 0x%x\n", pkt->getLE<uint32_t>());
    pkt->makeResponse();
    write(offset, pkt->getLE<uint32_t>());
    return 0;
}

void
MmioVirtIO::write(Addr offset, uint32_t value)
{
    VirtQueue *queue = vio.getQueue(vio.getQueueSelect());
    switch(offset) {
      case OFF_HOST_FEATURES_SELECT:
        hostFeaturesSelect = value;
        return;

      case OFF_GUEST_FEATURES:
          if (modern) {
              if (guestFeaturesSelect < 2 &&
                  !vio.getDeviceStatus().features_ok) {
                  const unsigned shift = 32 * guestFeaturesSelect;
                  const uint64_t mask = uint64_t(0xffffffff) << shift;
                  vio.setGuestFeatures((vio.getGuestFeatures() & ~mask) |
                                       (uint64_t(value) << shift));
              }
          } else if (guestFeaturesSelect == 0) {
              vio.setGuestFeatures(value);
          } else if (value != 0) {
              warn("Setting unimplemented guest features register %u: %u\n",
                   guestFeaturesSelect, value);
          }
        return;

      case OFF_GUEST_FEATURES_SELECT:
        guestFeaturesSelect = value;
        return;

      case OFF_GUEST_PAGE_SIZE:
          if (modern) {
              return;
          }
        // TODO: We only support 4096 byte pages at the moment
        panic_if(value != VirtQueue::ALIGN_SIZE,
                 "Unhandled VirtIO page size: %u", value);
        pageSize = value;
        return;

      case OFF_QUEUE_SELECT:
        vio.setQueueSelect(value);
        return;

      case OFF_QUEUE_NUM:
          if (modern) {
              if (queue && !queue->isReady() && !queue->setSize(value)) {
                  warn("Ignoring invalid VirtIO queue size %u\n", value);
              }
              return;
          }
        // TODO: We don't support queue resizing, so ignore this for now.
        warn_once("Ignoring queue resize hint. Requested size: %u\n", value);
        return;

      case OFF_QUEUE_ALIGN:
          if (modern) {
              return;
          }
        // TODO: We currently only support the hard-coded 4k alignment used
        // in legacy VirtIO.
        panic_if(value != VirtQueue::ALIGN_SIZE,
                 "Unhandled VirtIO alignment size: %u", value);
        return;

      case OFF_QUEUE_PFN:
          if (!modern && queue) {
              vio.setQueueAddress(value);
          }
          return;

      case OFF_QUEUE_READY:
          if (modern && queue && value <= 1 && !queue->setReady(value)) {
              warn("Ignoring invalid VirtIO queue addresses\n");
          }
          return;

      case OFF_QUEUE_DESC_LOW:
      case OFF_QUEUE_DESC_HIGH:
      case OFF_QUEUE_AVAIL_LOW:
      case OFF_QUEUE_AVAIL_HIGH:
      case OFF_QUEUE_USED_LOW:
      case OFF_QUEUE_USED_HIGH:
          if (modern && queue && !queue->isReady()) {
              Addr desc = queue->getAddress();
              Addr available = queue->getAvailableAddress();
              Addr used = queue->getUsedAddress();
              Addr &address =
                  offset < OFF_QUEUE_AVAIL_LOW
                      ? desc
                      : (offset < OFF_QUEUE_USED_LOW ? available : used);
              const unsigned shift = (offset & 4) ? 32 : 0;
              const uint64_t mask = uint64_t(0xffffffff) << shift;
              address = (address & ~mask) | (uint64_t(value) << shift);
              queue->setAddresses(desc, available, used);
          }
          return;

      case OFF_QUEUE_NOTIFY:
        vio.onNotify(value);
        return;

      case OFF_INTERRUPT_ACK:
        setInterrupts(interruptStatus & (~value));
        return;

      case OFF_STATUS:
        panic_if(value > 0xff, "Unexpected status: 0x%x\n", value);
        vio.setDeviceStatus(value);
        if (value == 0) {
            hostFeaturesSelect = guestFeaturesSelect = pageSize = 0;
            setInterrupts(0);
        }
        return;

        /* Read-only registers */
      case OFF_MAGIC:
      case OFF_VERSION:
      case OFF_DEVICE_ID:
      case OFF_VENDOR_ID:
      case OFF_HOST_FEATURES:
      case OFF_QUEUE_NUM_MAX:
      case OFF_INTERRUPT_STATUS:
      case OFF_CONFIG_GENERATION:
          warn("Guest is trying to write a read-only register 0x%x\n", offset);
          return;

      default:
          if (modern) {
              return;
          }
        panic("Unhandled read offset (0x%x)\n", offset);
    }
}

void
MmioVirtIO::kick()
{
    DPRINTF(VIOIface, "kick(): Sending interrupt...\n");
    setInterrupts(interruptStatus | INT_USED_RING);
}

void
MmioVirtIO::setInterrupts(uint32_t value)
{
    const uint32_t old_ints = interruptStatus;
    interruptStatus = value;

    if (!old_ints && interruptStatus) {
        interrupt->raise();
    } else if (old_ints && !interruptStatus) {
        interrupt->clear();
    }
}

} // namespace gem5
