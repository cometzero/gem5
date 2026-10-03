/* Copyright (c) 2026 Chanho Park */
/* SPDX-License-Identifier: BSD-3-Clause */
#ifndef __ARCH_ARM_ZENA_FS_WORKLOAD_HH__
#define __ARCH_ARM_ZENA_FS_WORKLOAD_HH__

#include "arch/arm/linux/fs_workload.hh"
#include "params/ZenaFsLinux.hh"

namespace gem5
{
class GenericTimer;
class Gicv3;
class Pl011;
class PL031;

namespace ArmISA
{

/** AP-only software warm reset, after drain and cache writeback/invalidation.
 */
class ZenaFsLinux : public FsLinux
{
  private:
    GenericTimer *resetTimer = nullptr;
    Gicv3 *resetGic = nullptr;
    Pl011 *resetUart = nullptr;
    PL031 *resetRtc = nullptr;

  public:
    PARAMS(ZenaFsLinux);
    explicit ZenaFsLinux(const Params &p) : FsLinux(p) {}
    void init() override;
    void resetGuest();
};

} // namespace ArmISA
} // namespace gem5
#endif
