/*
 * SPDX-License-Identifier: BSD-3-Clause
 * Experimental PMSAv8-64 execution support for standalone RTOS workloads.
 * This is not a complete Cortex-R82AE architectural or timing model.
 */

#include "arch/arm/faults.hh"
#include "arch/arm/isa.hh"
#include "arch/arm/regs/misc.hh"
#include "cpu/thread_context.hh"
#include "params/ArmISA.hh"

namespace gem5
{
namespace ArmISA
{

bool
ISA::pmsaEnabled() const
{
    return params().pmsa64;
}

RegVal
ISA::readPmsaReg(RegIndex idx) const
{
    if (idx >= MISCREG_IMP_ITCMREGIONR_EL1) {
        return pmsaTcm[idx - MISCREG_IMP_ITCMREGIONR_EL1];
    }
    const unsigned bank = idx >= MISCREG_MPUIR_EL2;
    const unsigned reg = idx - (bank ? MISCREG_MPUIR_EL2 : MISCREG_MPUIR_EL1);
    const unsigned selected = pmsaSel[bank];
    switch (reg) {
        case 0:
            return params().pmsa_regions;
        case 1:
            return selected;
        case 2:
            return selected < params().pmsa_regions
                       ? pmsaBar[bank * 32 + selected]
                       : 0;
        case 3:
            return selected < params().pmsa_regions
                       ? pmsaLar[bank * 32 + selected]
                       : 0;
        case 4: {
            RegVal result = 0;
            for (unsigned i = 0; i < params().pmsa_regions; ++i) {
                result |= (pmsaLar[bank * 32 + i] & 1) << i;
            }
            return result;
        }
        default:
            panic("Invalid PMSA register");
    }
}

void
ISA::writePmsaReg(RegIndex idx, RegVal val)
{
    if (idx >= MISCREG_IMP_ITCMREGIONR_EL1) {
        const unsigned i = idx - MISCREG_IMP_ITCMREGIONR_EL1;
        const RegVal writable = currEL() == EL2 ? 3 : 1;
        pmsaTcm[i] = (pmsaTcm[i] & ~writable) | (val & writable);
        return;
    }
    const unsigned bank = idx >= MISCREG_MPUIR_EL2;
    const unsigned reg = idx - (bank ? MISCREG_MPUIR_EL2 : MISCREG_MPUIR_EL1);
    const unsigned selected = pmsaSel[bank];
    const RegVal pa_mask = mask(physAddrRange);
    switch (reg) {
        case 0:
            break;
        case 1:
            pmsaSel[bank] = val & 0xff;
            break;
        case 2:
            if (selected < params().pmsa_regions) {
                pmsaBar[bank * 32 + selected] = val & pa_mask & ~1ULL;
            }
            break;
        case 3:
            if (selected < params().pmsa_regions) {
                pmsaLar[bank * 32 + selected] = val & pa_mask & ~0x20ULL;
            }
            break;
        case 4:
            for (unsigned i = 0; i < params().pmsa_regions; ++i) {
                auto &lar = pmsaLar[bank * 32 + i];
                lar = (lar & ~1ULL) | ((val >> i) & 1);
            }
            break;
        default:
            panic("Invalid PMSA register");
    }
}

Fault
ISA::translatePmsa(const RequestPtr &req, BaseMMU::Mode mode, bool privileged,
                   SCTLR sctlr)
{
    const CPSR cpsr = readMiscRegNoEffect(MISCREG_CPSR);
    fatal_if(cpsr.width, "Experimental PMSA supports only AArch64 state");
    const Addr addr = req->getVaddr();
    const bool fetch = mode == BaseMMU::Execute;
    const bool write = mode == BaseMMU::Write && !req->isCacheClean();
    const auto abort = [&](ArmFault::FaultSource reason) -> Fault {
        if (fetch) {
            return std::make_shared<PrefetchAbort>(addr, reason, false,
                                                   TranMethod::LpaeTran);
        }
        return std::make_shared<DataAbort>(addr, DomainType::NoAccess, write,
                                           reason, false,
                                           TranMethod::LpaeTran);
    };

    // These unimplemented translation modes must never silently bypass
    // protection. EL2 single-stage PMSA is supported; nested stage 2 is not.
    const HCR hcr = readMiscRegNoEffect(MISCREG_HCR_EL2);
    fatal_if(hcr.vm || hcr.dc ||
                 bits(readMiscRegNoEffect(MISCREG_VTCR_EL2), 31),
             "Experimental PMSA does not implement stage 2 or VMSA");
    if (addr > mask(physAddrRange) || !req->getSize() ||
        req->getSize() - 1 > mask(physAddrRange) - addr) {
        return abort(ArmFault::AddressSizeLL);
    }
    const Addr end = addr + req->getSize() - 1;
    const auto flags = req->getFlags();
    if (!fetch && (sctlr.a || !(flags & MMU::AllowUnaligned)) &&
        (addr & mask(flags & MMU::AlignmentMask))) {
        return abort(ArmFault::AlignmentFault);
    }

    const unsigned bank = currEL() == EL2;
    bool cacheable = fetch && sctlr.i;
    bool device = !fetch;
    if (sctlr.m) {
        int match = -1;
        for (unsigned i = 0; i < params().pmsa_regions; ++i) {
            const RegVal bar = pmsaBar[bank * 32 + i];
            const RegVal lar = pmsaLar[bank * 32 + i];
            const Addr base = bar & ~0x3fULL;
            const Addr limit = lar | 0x3fULL;
            if (!(lar & 1) || end < base || addr > limit) {
                continue;
            }
            // Overlap and crossing a region boundary fault; no priority
            // selection. PMSAv8 differs from the older priority-based MPU.
            if (match >= 0 || addr < base || end > limit) {
                return abort(ArmFault::TranslationLL);
            }
            match = i;
        }
        if (match < 0) {
            return abort(ArmFault::TranslationLL);
        }
        const RegVal bar = pmsaBar[bank * 32 + match];
        const RegVal lar = pmsaLar[bank * 32 + match];
        const unsigned ap = bits(bar, 3, 2);
        if ((!privileged && !(ap & 1)) || (write && (ap & 2)) ||
            (fetch && (bits(bar, 1) || (sctlr.wxn && !(ap & 2))))) {
            return abort(ArmFault::PermissionLL);
        }
        const auto mair =
            readMiscRegNoEffect(bank ? MISCREG_MAIR_EL2 : MISCREG_MAIR_EL1);
        const unsigned attr = (mair >> (bits(lar, 3, 1) * 8)) & 0xff;
        device = (attr & 0xf0) == 0;
        cacheable = !device && attr != 0x44 && (fetch ? sctlr.i : sctlr.c);
        if (fetch && device) {
            return abort(ArmFault::PermissionLL);
        }
        if (device && (addr & mask(flags & MMU::AlignmentMask))) {
            return abort(ArmFault::AlignmentFault);
        }
    }

    for (unsigned i = 0; i < 2; ++i) {
        const auto &range = i ? params().pmsa_dtcm : params().pmsa_itcm;
        if (!range.size() || !range.contains(addr)) {
            continue;
        }
        if (!range.contains(end) || !(pmsaTcm[i] & (bank ? 2 : 1)) ||
            (i && fetch)) {
            return abort(ArmFault::SynchronousExternalAbort);
        }
        cacheable = false;
        device = false;
    }

    if (!fetch) {
        for (const auto &range : params().pmsa_uncached_data_ranges) {
            if (range.contains(addr)) {
                cacheable = false;
            }
        }
    }

    req->setPaddr(addr);
    req->clearFlags(Request::SECURE);
    if (!cacheable && !req->isCacheMaintenance()) {
        req->setFlags(Request::UNCACHEABLE);
    }
    if (device) {
        req->setFlags(Request::STRICT_ORDER);
    }
    return NoFault;
}

} // namespace ArmISA
} // namespace gem5
