# SPDX-License-Identifier: BSD-3-Clause

from m5.objects.Device import BasicPioDevice
from m5.params import Param
from m5.proxy import Parent


class SiCl1Mhu(BasicPioDevice):
    """SI CL1 MHUv3 doorbells and a functional PFDI monitor responder.

    HIPC has no remote AP endpoint. PFDI implements only the boot protocol
    version and ONLINE/OOR acknowledgements; it is not an SCP firmware model.
    """

    type = "SiCl1Mhu"
    cxx_header = "dev/arm/css/si_cl1_mhu.hh"
    cxx_class = "gem5::SiCl1Mhu"

    pio_addr = 0x39000000
    system = Param.System(Parent.any, "System containing SCMI shared memory")
    shmem_addr = Param.Addr(0x48000000, "Four 40-byte PFDI SCMI slots")
    hipc_tx_irq = Param.ArmInterruptPin("HIPC PBX combined interrupt")
    hipc_rx_irq = Param.ArmInterruptPin("HIPC MBX combined interrupt")
    pfdi_irq = Param.ArmInterruptPin("PFDI combined interrupt")
