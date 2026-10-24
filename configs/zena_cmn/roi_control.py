# SPDX-License-Identifier: BSD-3-Clause
"""Strict guest-controlled boot/switch/warmup/ROI lifecycle."""

import json
from pathlib import Path


class RoiControl:
    def __init__(
        self, system, outdir, atomic, multi_roi=False, trace_roi=False
    ):
        self.system = system
        self.outdir = Path(outdir)
        self.atomic = atomic
        self.phase = "boot"
        self.events = []
        self.multi_roi = multi_roi
        self.active_case = None
        self.completed_cases = set()
        self.trace_roi = trace_roi
        if trace_roi:
            import m5

            m5.trace.output(str(self.outdir / "roi-trace.log"))
            for name in ("ProtocolTrace", "TLBVerbose"):
                m5.debug.flags[name].enable()
            m5.trace.disable()

    def handle(self, cause, code):
        import m5

        if cause not in ("workbegin", "workend"):
            return False
        if cause == "workbegin" and code == 0x5A535743:
            if self.phase != "boot":
                raise RuntimeError("duplicate CPU switch marker")
            if self.atomic:
                m5.switchCpus(
                    self.system,
                    list(zip(self.system.cpu, self.system.roi_cpu)),
                )
            self.phase = "warmup"
        elif self.multi_roi:
            if cause == "workbegin" and code > 0 and self.phase == "warmup":
                if code in self.completed_cases:
                    raise RuntimeError("duplicate ROI case")
                m5.stats.reset()
                self.active_case = code
                self.phase = "roi_begin"
            elif (
                cause == "workend" and code == 0 and self.phase == "roi_begin"
            ):
                m5.stats.dump()
                self.phase = "roi_end"
                self.completed_cases.add(self.active_case)
            else:
                raise RuntimeError(
                    f"unexpected marker {cause}:{code} in {self.phase}"
                )
        elif cause == "workbegin" and code == 0 and self.phase == "warmup":
            m5.stats.reset()
            self.phase = "roi_begin"
        elif cause == "workend" and code == 0 and self.phase == "roi_begin":
            m5.stats.dump()
            self.phase = "roi_end"
        else:
            raise RuntimeError(
                f"unexpected marker {cause}:{code} in {self.phase}"
            )
        if self.trace_roi:
            if self.phase == "roi_begin":
                m5.trace.enable()
            elif self.phase == "roi_end":
                m5.trace.disable()
        record = {"phase": self.phase, "tick": int(m5.curTick())}
        if self.active_case is not None:
            record["case_id"] = self.active_case
        self.events.append(record)
        (self.outdir / "roi-events.json").write_text(
            json.dumps(self.events, indent=2) + "\n"
        )
        print(f"CMN_PHASE={self.phase} tick={m5.curTick()}", flush=True)
        if self.multi_roi and self.phase == "roi_end":
            self.phase = "warmup"
            self.active_case = None
        return True

    def can_exit(self):
        if self.multi_roi:
            return self.phase == "warmup" and bool(self.completed_cases)
        return self.phase in ("boot", "roi_end")
