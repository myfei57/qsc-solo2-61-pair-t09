"""Pure scheduling policy: a plan is a function of the round snapshot.

Two rules are fixed by design, because operations asked for a definite
answer on each:

1. Emergency drainage still adds pumps one at a time, never all at
   once. Simultaneous starts stack inrush currents (5-7x rated each);
   on a weak supply that sags the bus or trips the feeder, and every
   pump is lost exactly at high water. Staged starts cap the peak at
   one pump's inrush, and a failed start degrades capacity partially
   instead of totally. The cost is a staggered ramp to full capacity,
   bought back with a shorter emergency stagger.

2. A pump whose protection window ends rejoins rotation at the next
   round, not mid-round. Eligibility is frozen at the round snapshot,
   so a scheduling result is a pure function of that snapshot: replay
   and audit always reproduce the same plan, and a pump is never
   grabbed while its stop is still settling.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .model import Health, Pit, Pump, PumpState, SchedulePolicy


def desired_pumps(level: float, policy: SchedulePolicy) -> int | None:
    """Pumps wanted at ``level``; ``None`` inside the hysteresis band."""

    if level < policy.stop_level:
        return 0
    if level < policy.start_level:
        return None
    return 1 + int((level - policy.start_level) // policy.level_band)


@dataclass
class PitPlan:
    """What one round decided for one pit."""

    pit_id: str
    level: float
    emergency: bool
    desired: int | None
    running: list[str] = field(default_factory=list)
    to_stop: list[str] = field(default_factory=list)
    want_starts: int = 0
    candidates: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "pit_id": self.pit_id,
            "level": self.level,
            "emergency": self.emergency,
            "desired": self.desired,
            "running": list(self.running),
            "to_stop": list(self.to_stop),
            "want_starts": self.want_starts,
            "candidates": list(self.candidates),
            "notes": list(self.notes),
        }


def plan_pit(
    pit: Pit,
    pumps: list[Pump],
    runtime_of: Callable[[str], float],
    now: float,
    policy: SchedulePolicy,
) -> PitPlan:
    """Diff desired against running and pick pumps to stop or start."""

    running = sorted((p for p in pumps if p.state == PumpState.RUNNING), key=lambda p: p.id)
    plan = PitPlan(
        pit_id=pit.id,
        level=pit.water_level,
        emergency=pit.water_level >= policy.emergency_level,
        desired=desired_pumps(pit.water_level, policy),
        running=[p.id for p in running],
    )
    if plan.desired is None:
        plan.notes.append("hysteresis band: holding current count")
        return plan

    if len(running) > plan.desired:
        # Stop the longest-run pumps first to even out wear; a pump
        # inside its minimum on-time is left running.
        need = len(running) - plan.desired
        by_runtime = sorted(running, key=lambda p: (-runtime_of(p.id), p.id))
        for pump in by_runtime:
            if need == 0:
                break
            if pump.started_ts is not None and now - pump.started_ts < policy.min_on_seconds:
                plan.notes.append(f"{pump.id}: min on-time not reached, kept running")
                continue
            plan.to_stop.append(pump.id)
            need -= 1
        return plan

    need = plan.desired - len(running)
    if need == 0:
        plan.notes.append("running count already matches demand")
        return plan

    # Eligible means stopped, not tagged FAULT, and protection already
    # over at the snapshot (rule 2 above). Rotation prefers the pump
    # with the least accumulated run time; degraded pumps rank last.
    eligible: list[Pump] = []
    for pump in sorted(pumps, key=lambda p: p.id):
        if pump.state != PumpState.STOPPED:
            continue
        if pump.health == Health.FAULT:
            plan.notes.append(f"{pump.id}: tagged FAULT, excluded")
            continue
        if pump.protection_until > now:
            plan.notes.append(
                f"{pump.id}: protected until {pump.protection_until:.0f}, excluded this round"
            )
            continue
        eligible.append(pump)
    health_rank = {Health.HEALTHY: 0, Health.DEGRADED: 1, Health.FAULT: 2}
    eligible.sort(key=lambda p: (health_rank[p.health], runtime_of(p.id), p.id))
    plan.want_starts = need
    plan.candidates = [p.id for p in eligible]
    if len(eligible) < need:
        plan.notes.append(f"short of pumps: want {need}, only {len(eligible)} eligible")
    return plan
