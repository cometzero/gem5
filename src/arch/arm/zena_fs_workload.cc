/* Copyright (c) 2026 Chanho Park */
/* SPDX-License-Identifier: BSD-3-Clause */
#include "arch/arm/zena_fs_workload.hh"

#include "arch/arm/system.hh"
#include "arch/generic/mmu.hh"
#include "cpu/simple/atomic.hh"
#include "cpu/thread_context.hh"
#include "dev/arm/generic_timer.hh"
#include "dev/arm/gic_v3.hh"
#include "dev/arm/pl011.hh"
#include "dev/arm/rtc_pl031.hh"
#include "sim/drain.hh"

namespace gem5::ArmISA
{

void
ZenaFsLinux::init()
{
    FsLinux::init();
    if (!params().enable_warm_reset) {
        return;
    }

    // init() runs after every SimObject has been constructed. Device Params
    // here would form workload -> device -> system -> workload cycles.
    auto *arm_system = dynamic_cast<ArmSystem *>(system);
    fatal_if(!arm_system, "Zena warm reset requires an ArmSystem");
    resetGic = dynamic_cast<Gicv3 *>(arm_system->getGIC());
    resetTimer = arm_system->getGenericTimer();
    resetUart = dynamic_cast<Pl011 *>(
        SimObject::find(params().reset_uart_path.c_str()));
    resetRtc = dynamic_cast<PL031 *>(
        SimObject::find(params().reset_rtc_path.c_str()));
    fatal_if(!resetGic || !resetTimer || !resetUart || !resetRtc,
             "Zena warm reset requires Gicv3, GenericTimer, Pl011 and PL031 "
             "devices (UART path '%s', RTC path '%s')",
             params().reset_uart_path, params().reset_rtc_path);
}

void
ZenaFsLinux::resetGuest()
{
    fatal_if(!DrainManager::instance().isDrained(),
             "Zena warm reset requires a fully drained simulation");
    fatal_if(!params().enable_warm_reset, "Zena warm reset is not enabled");
    // CPU activation during a drained reset is only audited for this model.
    for (auto *tc : system->threads) {
        fatal_if(!dynamic_cast<AtomicSimpleCPU *>(tc->getCpuPtr()),
                 "Zena warm reset currently requires AtomicSimpleCPU");
    }

    resetTimer->resetForWarmBoot();
    resetUart->resetForWarmBoot();
    resetRtc->resetForWarmBoot();
    for (Addr base : params().reset_virtio_bases) {
        // The Zena block/RNG transport processes requests synchronously.
        // Status zero resets negotiated features, queues and its IRQ line.
        system->physProxy.write<uint32_t>(base + 0x70, 0, ByteOrder::little);
    }
    resetGic->resetForWarmBoot();

    for (auto *tc : system->threads) {
        tc->getMMUPtr()->flushAll();
        // FsWorkload::initState calls activate(). Mark Active directly so
        // no tick is scheduled while drained; drainResume schedules it.
        tc->setStatus(ThreadContext::Active);
    }
    // Reload the original ELF (including BSS), firmware, initrd and DT;
    // Reset::invoke clears all architectural CPU/ICC registers and sets PC.
    FsLinux::initState();
    inform("ZENA_GEM5_WARM_RESET: images reloaded, CPUs and devices reset");
}

} // namespace gem5::ArmISA
