"""Turning Freerouting's log lines into a routing status.

The headless engine reports only through its log. These are the lines of
Freerouting 2.5 that matter, e.g.

  ... Fanout stage started on board '...' with 12 of 12 SMD pins needing fanout ...
  ... Auto-routing stage started on board '...' with baseline score 0.00 for 57 unrouted items.
  ... Auto-routing pass #3 on board '...' was completed in 1.20 seconds with score 812.00 (4 unrouted and 0 violations).
  ... Auto-routing stage completed: started with 57 unrouted nets, ... final score: 1000.00 (0 unrouted and 0 violations), ...
  ... Optimizer pass #2: optimizer score 897.06 -> 926.47 (IMPROVED, 3.2787%), router score: 1000.00, incomplete connections: 0, clearance violations: 0.
  ... Job 'X' finished with state: COMPLETED (elapsed: 0.28 seconds, ...).
  ... Successfully saved output file '/path/out.ses' (2309 bytes).
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional

STAGE_START, STAGE_LOAD, STAGE_FANOUT, STAGE_ROUTE, STAGE_OPTIMIZE, STAGE_SAVE, STAGE_DONE = (
    "starting", "loading", "fanout", "routing", "optimizing", "saving", "finished")

_RE = {
    "load": re.compile(r"Loading board file"),
    "fanout_start": re.compile(r"Fanout stage started"),
    "fanout_pass": re.compile(r"Fanout pass #(\d+)"),
    "route_start": re.compile(r"Auto-routing stage started .*?for (\d+) unrouted items"),
    "route_pass": re.compile(r"Auto-routing pass #(\d+) .*?score ([\d.]+) \((\d+) unrouted and (\d+) violations\)"),
    "route_done": re.compile(r"Auto-routing stage completed: .*?final score: ([\d.]+) \((\d+) unrouted and (\d+) violations\)"),
    "opt_start": re.compile(r"Optimization stage started"),
    "opt_pass": re.compile(r"Optimizer pass #(\d+): .*?incomplete connections: (\d+), clearance violations: (\d+)"),
    "opt_pass_board": re.compile(r"Optimizer pass #(\d+) on board"),
    "job_done": re.compile(r"finished with state: (\w+)"),
    "saving": re.compile(r"Saving output file"),
    "saved": re.compile(r"Successfully saved output file '([^']+)'"),
    "level": re.compile(r"^\S+ \S+ (ERROR|WARN|INFO|DEBUG|TRACE|FATAL)\s"),
}


@dataclass
class RouteStatus:
    stage: str = STAGE_START
    pass_number: int = 0
    total_items: Optional[int] = None       # unrouted items when routing started
    unrouted: Optional[int] = None
    violations: Optional[int] = None
    score: Optional[float] = None
    job_state: Optional[str] = None         # COMPLETED, CANCELLED, ... (Freerouting's word)
    output_saved: Optional[str] = None
    errors: List[str] = field(default_factory=list)

    @property
    def finished_ok(self) -> bool:
        return self.job_state == "COMPLETED" and self.output_saved is not None

    def fraction(self, max_passes: int) -> Optional[float]:
        """A 0..1 estimate for a progress bar, None when it should pulse."""
        if self.stage == STAGE_DONE:
            return 1.0
        if self.stage == STAGE_ROUTE:
            parts = []
            if self.total_items and self.unrouted is not None:
                parts.append(1.0 - self.unrouted / self.total_items)
            if max_passes and self.pass_number:
                parts.append(self.pass_number / max_passes)
            if parts:
                return min(0.99, max(parts))
            return 0.0
        return None

    def summary(self) -> str:
        """One line for the status label."""
        if self.job_state and self.job_state != "COMPLETED":
            return f"Engine stopped: {self.job_state}"
        if self.stage == STAGE_START:
            return "Starting the engine..."
        if self.stage == STAGE_LOAD:
            return "Loading the board..."
        if self.stage == STAGE_FANOUT:
            return f"Fanout pass {self.pass_number}" if self.pass_number else "Fanout..."
        if self.stage == STAGE_ROUTE:
            text = f"Routing pass {self.pass_number}" if self.pass_number else "Routing..."
            if self.unrouted is not None:
                text += f": {self.unrouted} unrouted, {self.violations} violations"
            return text
        if self.stage == STAGE_OPTIMIZE:
            text = f"Optimizing pass {self.pass_number}" if self.pass_number else "Optimizing..."
            if self.unrouted is not None:
                text += f": {self.unrouted} unrouted, {self.violations} violations"
            return text
        if self.stage == STAGE_SAVE:
            return "Saving the session..."
        text = "Finished"
        if self.unrouted is not None:
            text += f": {self.unrouted} unrouted, {self.violations} violations"
        return text


class ProgressParser:
    def __init__(self):
        self.status = RouteStatus()

    def feed(self, line: str) -> RouteStatus:
        s = self.status
        line = line.rstrip()
        level = _RE["level"].match(line)
        if level and level.group(1) in ("ERROR", "FATAL"):
            s.errors.append(line)
        elif "Exception" in line and not level and line and not line[0].isspace():
            s.errors.append(line)

        if _RE["load"].search(line):
            s.stage = STAGE_LOAD
        elif _RE["fanout_start"].search(line):
            s.stage, s.pass_number = STAGE_FANOUT, 0
        elif (m := _RE["fanout_pass"].search(line)):
            s.stage, s.pass_number = STAGE_FANOUT, int(m.group(1))
        elif (m := _RE["route_start"].search(line)):
            s.stage, s.pass_number = STAGE_ROUTE, 0
            s.total_items = int(m.group(1))
            s.unrouted = s.total_items
            s.violations = 0
        elif (m := _RE["route_pass"].search(line)):
            s.stage = STAGE_ROUTE
            s.pass_number = int(m.group(1))
            s.score = float(m.group(2))
            s.unrouted, s.violations = int(m.group(3)), int(m.group(4))
        elif (m := _RE["route_done"].search(line)):
            s.score = float(m.group(1))
            s.unrouted, s.violations = int(m.group(2)), int(m.group(3))
        elif _RE["opt_start"].search(line):
            s.stage, s.pass_number = STAGE_OPTIMIZE, 0
        elif (m := _RE["opt_pass"].search(line)):
            s.stage = STAGE_OPTIMIZE
            s.pass_number = int(m.group(1))
            s.unrouted, s.violations = int(m.group(2)), int(m.group(3))
        elif (m := _RE["opt_pass_board"].search(line)):
            s.stage = STAGE_OPTIMIZE
            s.pass_number = int(m.group(1))
        elif (m := _RE["job_done"].search(line)):
            s.job_state = m.group(1)
        elif _RE["saving"].search(line):
            s.stage = STAGE_SAVE
        elif (m := _RE["saved"].search(line)):
            s.stage = STAGE_DONE
            s.output_saved = m.group(1)
        return s
