# Copyright (c) 2026 Chanho Park
# SPDX-License-Identifier: BSD-3-Clause
"""Project-local four-core RN-F with a real intermediate, non-home L3."""

from ruby.CHI_config import (
    CHI_RNF,
    CHI_L2Controller,
)

from m5.objects import (
    NULL,
    RubyCache,
    StridePrefetcher,
)


def cache_template(spec, resource_stalls, size_key, unit):
    return RubyCache(
        size=f"{spec[size_key]}{unit}",
        assoc=spec["assoc"],
        tagAccessLatency=spec["tag_latency_cycles"],
        dataAccessLatency=spec["data_latency_cycles"],
        tagArrayBanks=spec["tag_banks"],
        dataArrayBanks=spec["data_banks"],
        resourceStalls=resource_stalls,
    )


class DSUL3Controller(CHI_L2Controller):
    """Reuse inclusive CHI intermediate-cache snoop/directory machinery."""


class ZenaClusterRNF(CHI_RNF):
    def __init__(self, cpus, ruby_system, params):
        caches = params["cache"]
        stalls = caches["resource_stalls"]
        super().__init__(
            cpus,
            ruby_system,
            cache_template(caches["l1i"], stalls, "size_kib_per_core", "KiB"),
            cache_template(caches["l1d"], stalls, "size_kib_per_core", "KiB"),
            caches["line_bytes"],
        )
        prefetch = caches["l2"].get("prefetch", "off")
        if prefetch not in ("off", "stride"):
            raise ValueError(f"unsupported L2 prefetcher: {prefetch}")
        # CHI supports load prefetches; this is not an Arm algorithm model.
        prefetcher = (
            StridePrefetcher(
                degree=1, distance=0, on_write=False, on_inst=False
            )
            if prefetch == "stride"
            else None
        )
        self.addPrivL2Cache(
            cache_template(caches["l2"], stalls, "size_kib_per_core", "KiB"),
            prefetcher,
        )
        policy = caches["l2"].get("policy", "inclusive")
        if policy not in ("inclusive", "exclusive_data"):
            raise ValueError(f"unsupported L2 policy: {policy}")
        for cpu in cpus:
            cpu.l2.read_hit_latency = caches["l2"]["read_hit_pipeline_cycles"]
            if policy == "exclusive_data":
                if not hasattr(cpu.l2, "exclusive_data_upstream_version"):
                    raise ValueError(
                        "exclusive_data requires rebuilt CHI protocol"
                    )
                cpu.l2.exclusive_data_upstream_version = int(cpu.l1d.version)
                cpu.l2.dealloc_backinv_shared = False
                cpu.l2.dealloc_backinv_unique = False
            if caches["integrated_private_pipeline"]:
                # Integrated-cache handoff approximation, only on Simple.
                for controller in (cpu.l1i, cpu.l1d, cpu.l2):
                    controller.request_latency = 0
                    controller.data_latency = 0
                    controller.reqOut.allow_zero_latency = True
                    controller.datOut.allow_zero_latency = True
            for level in ("l1i", "l1d", "l2"):
                getattr(cpu, level).number_of_TBEs = caches[level][
                    "request_tbes"
                ]
            cpu.inst_sequencer.max_outstanding_requests = params["ruby"][
                "instruction_sequencer_outstanding"
            ]
            cpu.data_sequencer.max_outstanding_requests = params["ruby"][
                "data_sequencer_outstanding"
            ]
        self.dsu_l3 = DSUL3Controller(
            ruby_system,
            cache_template(
                caches["dsu_l3"], stalls, "size_mib_per_cluster", "MiB"
            )(start_index_bit=self._block_size_bits),
            NULL,
        )
        self.dsu_l3.number_of_TBEs = caches["dsu_l3"]["request_tbes"]
        self.connectController(self.dsu_l3)
        self._cntrls.append(self.dsu_l3)
        for cpu in cpus:
            cpu.l2.downstream_destinations = [self.dsu_l3]
        # CHI_RNF.setDownstream must connect only the DSU to global Homes.
        self._ll_cntrls = [self.dsu_l3]
