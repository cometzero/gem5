# Experimental Zena timing block profile

`TimingVirtIOBlock` is a separate modern VirtIO MMIO v2 block device (ID 2).
The existing functional VirtIO transports and devices remain unchanged.
The Zena `timing-block` profile uses only this block device at `0x30020000`,
INTID 289, with its DMA port connected through a dedicated IOCache to the
coherent system crossbar. The IOCache matches upstream ARM SimpleSystem:
1 KiB, 8-way, 50-cycle tag/data/response latency, 20 MSHRs and 12 targets per
MSHR. These parameters are part of the experiment, not Zena hardware data.
The DT advertises `dma-coherent`. The guest uses the normal Linux
`virtio-mmio`/`virtio-blk` drivers and a private COW disk overlay.

The IOCache is required for Classic coherence: raw DMA WriteReq packets
require writable data but do not carry an invalidation command. Directly
snooping such packets through a nested cache hierarchy violates its snoop
filter contract. The IOCache handles these as ordinary CPU-side writes,
obtains ownership with ReadExReq/UpgradeReq (InvalidateReq for complete
lines), and preserves dirty bytes outside a partial DMA write. Reads use
ReadSharedReq and receive the dirty owner's data. No UNCACHEABLE flag or
functional bypass is used. See `configs/example/arm/devices.py` SimpleSystem,
`src/mem/cache/cache.cc` createMissPacket/handleSnoop, and the default
`ResponsePort::recvAtomicBackdoor` in `src/mem/port.cc`, which provides no
backdoor and dispatches the atomic access through the cache.

The device supports one split queue, 4--128 descriptors (default 128), and
one active request. VERSION_1, SIZE_MAX, SEG_MAX and FLUSH are the only
advertised features. There are no indirect descriptors, EVENT_IDX, packed
rings, multiqueue, discard or write-zeroes features. Each payload descriptor
is limited to 64 KiB. The bounded descriptor walk rejects cycles, bad indices,
unnegotiated flags and DMA outside the AP RAM range.

Every guest access uses `DmaPort`, including the available ring, descriptor
chain, request header, payload, status byte, used element and used index.
The chain is processed asynchronously:

1. Read available index/head, descriptors and block request header.
2. For WRITE, DMA the payload into a private host buffer.
3. Wait the declared simulated service time, then access the disk image.
4. For READ, DMA the payload back to guest buffers.
5. DMA the status byte, then used element, then used index in that order.
6. Assert the used-ring interrupt after the used-index DMA completes,
   unless the available ring requests interrupt suppression.

The service model is explicitly synthetic:

```text
READ/WRITE service = request_latency + ceil(payload_bytes * ticks_per_byte)
FLUSH service      = flush_latency
```

Defaults are 10 us, 100 MiB/s and 100 us respectively. DMA latency and memory
contention are additional to this service time. Queue depth one orders FLUSH
after prior writes. Successful FLUSH is a modeled barrier, not a claim about
physical storage durability, host `fsync` or power-loss protection.
Unknown block commands receive `VIRTIO_BLK_S_UNSUPP`; invalid block payloads
receive IOERR. Malformed queue metadata sets DEVICE_NEEDS_RESET and raises
a configuration interrupt instead of issuing unsafe DMA.

DMA uses the current gem5 memory mode. Atomic boot can exercise functional
correctness and prepare files, but only a timing/O3 ROI measures the timing
DMA path. Reset statistics after CPU takeover and before the measured work.
CPU cache coherence, DMA retry/backpressure, and the effect of configured
service times require runtime validation; compilation alone is insufficient.

The device's `drain()` includes both DMA completion and delayed service
events. It finishes the current request and stops consuming new heads;
`drainResume()` rechecks the available ring. This supports draining for CPU
takeover without discarding submitted guest requests. Initial status-zero
reset before DRIVER_OK and reset while idle are supported. Reset during an
active DMA/service operation is explicitly rejected. Checkpoint/restore is
explicitly rejected by both the profile and the model. Warm reset and an
external network peer are excluded from this profile.

Statistics appear directly under the block device path:

| Statistic | Meaning |
|---|---|
| `requests` | Completed used-index publications, including error requests |
| `reads`, `writes`, `flushes` | Requests that reach the device service stage |
| `dmaReadBytes`, `dmaWriteBytes` | All submitted guest DMA bytes, metadata included |
| `payloadBytes` | Successfully serviced READ/WRITE payload bytes |
| `interrupts` | Used-ring interrupt assertions; not necessarily IRQ handler entries |
| `requestTicks` | Sum from available-head fetch to used-index completion; excludes time waiting behind another request |
| `serviceTicks` | Sum of modeled storage service delays |

Required integration evidence includes dirty CPU-cache data visible to DMA,
read/write checksum after unmount/remount, positive DMA/IRQ counters, queue
wrap, service-time scaling, and drain/resume with work outstanding. Device
latency includes DMA and service; Linux latency additionally includes driver,
scheduling and filesystem work. Network throughput, physical SSD/eMMC/UFS
IOPS, power-loss behavior and arbitrary reset/checkpoint support are outside
this model's contract.
