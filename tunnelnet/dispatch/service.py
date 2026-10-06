"""Multi-pump drainage dispatch service.

Single serialized entry point for every pit request, so concurrent reports
from several pits cannot double-book one pump and always agree on the global
power budget.  All decisions are persisted as plans with ordered steps, audit
events are appended for each one, and runtime/energy come from the run-session
ledger only.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol

from tunnelnet.audit import Record
from tunnelnet.store import Store

from . import ledger, policy
from .audit import EventLog
from .clock import utc_stamp
from .model import (
    ACTION_START,
    ACTION_STOP,
    FAILED,
    HEALTH_ORDER,
    KIND_LEVEL,
    KIND_ROTATE,
    PLAN_BLOCKED,
    PLAN_COMPLETED,
    PLAN_EXECUTING,
    PLAN_PARTIAL,
    PLAN_SUPERSEDED,
    STEP_DONE,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RESERVE,
    STEP_SKIPPED,
    STEP_STARTED,
    STEP_STARTING,
    STEP_STOPPING,
    STEP_SUPERSEDED,
    PitConfig,
    PumpProfile,
    RunSession,
    SchedulePlan,
)
from .rig import PumpRig

LAST_START_KEY = "last-start-at"


class LegacyAuditSink(Protocol):  # pragma: no cover - structural
    def append(self, record: Record) -> Record: ...


class DispatchService:
    """Level-driven, health-aware, runtime-balancing pump scheduler."""

    def __init__(
        self,
        store: Store,
        rig: PumpRig,
        watts_capacity: float,
        stagger_seconds: float = 10.0,
        rotation_gap_seconds: float = 0.0,
        clock: Callable[[], float] | None = None,
        audit: EventLog | None = None,
        legacy_audit: LegacyAuditSink | None = None,
    ) -> None:
        self._store = store
        self._rig = rig
        self._capacity = watts_capacity
        self._stagger = stagger_seconds
        self._rotation_gap = rotation_gap_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._event_log = audit
        self._legacy = legacy_audit

    # ----- helpers ---------------------------------------------------------
    def _now(self) -> float:
        return self._clock() if self._clock is not None else time.time()

    def _stamp(self) -> str:
        return utc_stamp(self._now())

    def _event(self, event: str, detail: dict[str, object], request_id: str = "", pit_id: str = "") -> None:
        if self._event_log is not None:
            self._event_log.append(event, self._now(), self._stamp(), detail, request_id, pit_id)
        if self._legacy is not None:
            seq = (self._event_log.count() if self._event_log is not None else 0)
            self._legacy.append(Record(id=f"dispatch-{seq}", event=f"dispatch.{event}", at=self._stamp()))

    def _pumps(self) -> list[PumpProfile]:
        return ledger.list_pumps(self._store)

    def _pit_or_raise(self, pit_id: str) -> PitConfig:
        pit = ledger.load_pit(self._store, pit_id)
        if pit is None:
            raise KeyError(f"unknown pit {pit_id!r}")
        return pit

    def _plan_sequence(self) -> int:
        value = int(ledger.get_state(self._store, "plan-seq", 0)) + 1
        ledger.set_state(self._store, "plan-seq", value)
        return value

    # ----- configuration ---------------------------------------------------
    def register_pump(self, pump: PumpProfile) -> PumpProfile:
        with self._lock:
            ledger.save_pump(self._store, pump)
            self._event(
                "pump.registered",
                {
                    "pump_id": pump.id,
                    "watts": pump.watts,
                    "inrush_watts": pump.inrush_watts,
                    "cooldown_seconds": pump.cooldown_seconds,
                    "health": pump.health,
                    "serves": list(pump.serves),
                },
            )
            return pump

    def register_pit(self, pit: PitConfig) -> PitConfig:
        with self._lock:
            ledger.save_pit(self._store, pit)
            self._event(
                "pit.registered",
                {
                    "pit_id": pit.id,
                    "start_levels": list(pit.start_levels),
                    "stop_levels": list(pit.stop_levels),
                    "high_level": pit.high_level,
                },
                pit_id=pit.id,
            )
            return pit

    def set_health(self, pump_id: str, health: str) -> PumpProfile:
        with self._lock:
            pump = ledger.load_pump(self._store, pump_id)
            if pump is None:
                raise KeyError(f"unknown pump {pump_id!r}")
            if health not in HEALTH_ORDER:
                raise ValueError(f"unknown health {health!r}")
            pump.health = health
            ledger.save_pump(self._store, pump)
            self._event("pump.health", {"pump_id": pump_id, "health": health})
            return pump

    # ----- read models -----------------------------------------------------
    def pump_profile(self, pump_id: str) -> PumpProfile:
        with self._lock:
            pump = ledger.load_pump(self._store, pump_id)
            if pump is None:
                raise KeyError(f"unknown pump {pump_id!r}")
            return pump

    def pit_config(self, pit_id: str) -> PitConfig:
        with self._lock:
            return self._pit_or_raise(pit_id)

    def cumulative_seconds(self, pump_id: str) -> float:
        with self._lock:
            return ledger.cumulative_seconds(self._store, pump_id, self._now())

    def energy_wh(self, pump_id: str | None = None) -> float:
        with self._lock:
            return ledger.energy_wh(self._store, pump_id, self._now())

    def pump_statistics(self) -> list[dict[str, object]]:
        with self._lock:
            now = self._now()
            rows = []
            for pump in self._pumps():
                sessions = ledger.pump_sessions(self._store, pump.id)
                rows.append(
                    {
                        "pump_id": pump.id,
                        "health": pump.health,
                        "running": self._rig.is_running(pump.id),
                        "runtime_seconds": ledger.cumulative_seconds(self._store, pump.id, now),
                        "energy_wh": ledger.energy_wh(self._store, pump.id, now),
                        "starts": sum(1 for s in sessions if not s.failed),
                        "failed_starts": sum(1 for s in sessions if s.failed),
                        "ready_at": policy.ready_at(self._store, pump.id),
                        "serves": list(pump.serves),
                    }
                )
            return sorted(rows, key=lambda row: str(row["pump_id"]))

    def running(self, pit_id: str | None = None) -> list[RunSession]:
        with self._lock:
            return policy.running_on(self._store, pit_id) if pit_id else ledger.open_sessions(self._store)

    def active_power_watts(self) -> float:
        with self._lock:
            return sum(session.watts for session in ledger.open_sessions(self._store))

    def plan_by_request(self, request_id: str) -> SchedulePlan | None:
        with self._lock:
            for plan in reversed(ledger.list_plans(self._store)):
                if plan.request_id == request_id:
                    return plan
            return None

    def latest_plan(self, pit_id: str) -> SchedulePlan | None:
        with self._lock:
            plans = [plan for plan in ledger.list_plans(self._store) if plan.pit_id == pit_id]
            return plans[-1] if plans else None

    def audit_verify(self) -> tuple[bool, str]:
        if self._event_log is None:
            return True, "audit disabled"
        with self._lock:
            return self._event_log.verify_chain()

    def describe(self) -> str:
        with self._lock:
            return (
                f"dispatch pumps={len(self._pumps())} pits={len(ledger.list_pits(self._store))} "
                f"running={len(ledger.open_sessions(self._store))} "
                f"plans={len(ledger.list_plans(self._store))}"
            )

    # ----- idempotent requests ---------------------------------------------
    def report_level(self, pit_id: str, level: float, request_id: str = "") -> SchedulePlan:
        """Report a level and (re)plan the pit; repeated request_id is a no-op."""

        with self._lock:
            pit = self._pit_or_raise(pit_id)
            if level < 0:
                raise ValueError("level must be non-negative")
            request_id = request_id or f"level:{pit_id}:{level}@{self._now():.3f}"
            existing = self.plan_by_request(request_id)
            if existing is not None:
                self._event(
                    "request.duplicate",
                    {"pit_id": pit_id, "level": level, "plan_id": existing.id, "status": existing.status},
                    request_id,
                    pit_id,
                )
                return existing

            plan = SchedulePlan(
                id=f"plan-{self._plan_sequence():06d}",
                request_id=request_id,
                pit_id=pit_id,
                kind=KIND_LEVEL,
                level=level,
                desired=0,
                running_before=0,
                severity=policy.severity(pit, level),
                created_at=self._now(),
                created_stamp=self._stamp(),
            )
            policy.build_level_plan(self._store, plan, pit, self._pumps(), self._now(), self._stagger)
            self._event(
                "plan.built",
                {
                    "plan_id": plan.id,
                    "kind": plan.kind,
                    "level": level,
                    "desired": plan.desired,
                    "running_before": plan.running_before,
                    "steps": [
                        {"seq": s.seq, "action": s.action, "pump_id": s.pump_id, "state": s.state}
                        for s in plan.steps
                    ],
                    "note": plan.note,
                },
                request_id,
                pit_id,
            )
            self.tick()
            return self.latest_plan(pit_id) or plan

    def request_rotation(self, pit_id: str, request_id: str = "") -> SchedulePlan:
        """Rotate one pump: longest-running out, shortest-runtime healthy pump in."""

        with self._lock:
            pit = self._pit_or_raise(pit_id)
            level = self.latest_plan(pit_id).level if self.latest_plan(pit_id) else 0.0
            request_id = request_id or f"rotate:{pit_id}@{self._now():.3f}"
            existing = self.plan_by_request(request_id)
            if existing is not None:
                return existing

            plan = SchedulePlan(
                id=f"plan-{self._plan_sequence():06d}",
                request_id=request_id,
                pit_id=pit_id,
                kind=KIND_ROTATE,
                level=level,
                desired=0,
                running_before=0,
                severity=0.0,
                created_at=self._now(),
                created_stamp=self._stamp(),
            )
            policy.build_rotation_plan(
                self._store, plan, pit, self._pumps(), self._now(), self._rotation_gap
            )
            self._event(
                "plan.built",
                {
                    "plan_id": plan.id,
                    "kind": plan.kind,
                    "desired": plan.desired,
                    "steps": [
                        {"seq": step.seq, "action": step.action, "pump_id": step.pump_id, "state": step.state}
                        for step in plan.steps
                    ],
                    "note": plan.note,
                },
                request_id,
                pit_id,
            )
            self.tick()
            return self.latest_plan(pit_id) or plan

    # ----- execution --------------------------------------------------------
    def _power_draw(self) -> float:
        return sum(session.watts for session in ledger.open_sessions(self._store))

    def _mark_failed_pump(self, pump_id: str, reason: str) -> None:
        pump = ledger.load_pump(self._store, pump_id)
        if pump is not None and pump.health != FAILED:
            pump.health = FAILED
            ledger.save_pump(self._store, pump)
        self._event("pump.start-failed", {"pump_id": pump_id, "reason": reason})

    def _do_start(self, plan: SchedulePlan, step) -> bool:
        """Attempt a start; False means the step could not complete this tick."""

        now = self._now()
        pump = ledger.load_pump(self._store, step.pump_id)
        if pump is None or pump.health == FAILED:
            step.state = STEP_SKIPPED
            step.error = "pump unavailable or failed before start"
            ledger.save_plan(self._store, plan)
            self._event("step.skipped", {"plan_id": plan.id, "seq": step.seq, "pump_id": step.pump_id},
                        plan.request_id, plan.pit_id)
            return False

        required = self._power_draw() + pump.inrush_watts
        if required > self._capacity:
            return False  # power budget tight this tick; retry next tick

        step.state = STEP_STARTING
        step.attempts += 1
        ledger.save_plan(self._store, plan)

        # Persist the session BEFORE the hardware call: crash recovery can see it.
        session = RunSession(
            id=f"{plan.id}-s{step.seq}",
            pump_id=pump.id,
            pit_id=plan.pit_id,
            plan_id=plan.id,
            step_seq=step.seq,
            started_at=now,
            started_stamp=self._stamp(),
            watts=pump.watts,
        )
        ledger.save_session(self._store, session)
        try:
            ok = self._rig.start(pump.id)
        except Exception as exc:  # hardware fault == partial failure, not crash
            ok = False
            step.error = repr(exc)

        if not ok:
            ledger.close_session(self._store, session, self._now(), self._stamp(), failed=True)
            step.state = STEP_FAILED
            step.error = step.error or "rig rejected start"
            ledger.save_plan(self._store, plan)
            self._mark_failed_pump(pump.id, step.error)
            # busy set must be evaluated after the failed session is closed,
            # otherwise this pump looks still occupied and skips the reserve.
            self._promote_reserve(plan, step)
            return False

        step.state = STEP_STARTED
        ledger.save_plan(self._store, plan)
        ledger.set_state(self._store, LAST_START_KEY, now)
        self._event(
            "pump.started",
            {"plan_id": plan.id, "seq": step.seq, "pump_id": pump.id, "watts": pump.watts},
            plan.request_id,
            plan.pit_id,
        )
        return True

    def _promote_reserve(self, plan: SchedulePlan, failed_step) -> None:
        """One failed primary promotes one reserve, in frozen order."""

        busy = policy.busy_pumps(self._store, ignore_plan=plan.id)
        promoted = False
        for reserve in (step for step in plan.steps if step.state == STEP_RESERVE):
            if reserve.pump_id in busy:
                reserve.state = STEP_SKIPPED
                reserve.error = "pump taken before promotion"
                continue
            reserve.state = STEP_PENDING
            reserve.not_before = self._now()
            promoted = True
            self._event(
                "step.promoted",
                {"plan_id": plan.id, "seq": reserve.seq, "pump_id": reserve.pump_id},
                plan.request_id,
                plan.pit_id,
            )
            break
        ledger.save_plan(self._store, plan)

    def _do_stop(self, plan: SchedulePlan, step) -> None:
        now = self._now()
        session = ledger.open_session_for(self._store, plan.id, step.seq)
        if session is None:
            # Linked swap stop points at the running incumbent opened elsewhere.
            session = next(
                (
                    s
                    for s in ledger.open_sessions(self._store)
                    if s.pump_id == step.pump_id and s.pit_id == plan.pit_id
                ),
                None,
            )
        step.state = STEP_STOPPING
        step.attempts += 1
        ledger.save_plan(self._store, plan)

        if session is None:
            step.state = STEP_DONE
            ledger.save_plan(self._store, plan)
            return

        try:
            ok = self._rig.stop(step.pump_id)
        except Exception as exc:
            ok = False
            step.error = repr(exc)

        if not ok:
            step.state = STEP_PENDING  # leave for the next tick / manual recovery
            step.error = step.error or "rig rejected stop"
            ledger.save_plan(self._store, plan)
            self._event("pump.stop-failed", {"plan_id": plan.id, "seq": step.seq,
                                            "pump_id": step.pump_id, "error": step.error},
                        plan.request_id, plan.pit_id)
            return

        ledger.close_session(self._store, session, now, self._stamp())
        step.state = STEP_DONE
        step.runtime_seconds = session.seconds
        ledger.save_plan(self._store, plan)
        # A stop (e.g. from a newer plan that superseded the owner) must also
        # retire the STARTED step on the plan that launched this motor, or the
        # pump stays "busy" forever in the busy-pump projection.
        for other in ledger.list_plans(self._store):
            if other.id == plan.id:
                continue
            changed = False
            for other_step in other.steps:
                if (
                    other_step.pump_id == step.pump_id
                    and other_step.state in {STEP_STARTED, STEP_STARTING}
                ):
                    other_step.state = STEP_DONE
                    other_step.runtime_seconds = session.seconds
                    changed = True
            if changed:
                ledger.save_plan(self._store, other)
        self._event(
            "pump.stopped",
            {
                "plan_id": plan.id,
                "seq": step.seq,
                "pump_id": step.pump_id,
                "seconds": session.seconds,
                "energy_wh": session.energy_wh,
            },
            plan.request_id,
            plan.pit_id,
        )

    def _finalize_plans(self) -> list[str]:
        changed = []
        for plan in ledger.active_plans(self._store):
            open_steps = [step for step in plan.steps if step.state in
                          {STEP_PENDING, STEP_RESERVE, STEP_STARTING, STEP_STOPPING}]
            if open_steps:
                pending_starts = [
                    step for step in open_steps
                    if step.action == ACTION_START and step.state == STEP_PENDING
                ]
                if plan.status == PLAN_EXECUTING and pending_starts and self._absolutely_blocked(pending_starts):
                    plan.status = PLAN_BLOCKED
                    for step in open_steps:
                        if step.state in {STEP_PENDING, STEP_RESERVE}:
                            step.state = STEP_SUPERSEDED
                    plan.note = (plan.note + " blocked: power capacity cannot cover another inrush").strip()
                    ledger.save_plan(self._store, plan)
                    self._event("plan.blocked", {"plan_id": plan.id, "desired": plan.desired},
                                plan.request_id, plan.pit_id)
                    changed.append(plan.id)
                continue

            for step in plan.steps:
                if step.state == STEP_RESERVE:
                    step.state = STEP_SUPERSEDED
            # Outcome is measured by running pumps, not by which primaries
            # failed: a frozen reserve that successfully covers a failed slot
            # means the plan still completed.
            running_here = len(policy.running_on(self._store, plan.pit_id))
            if running_here < plan.desired:
                plan.status = PLAN_PARTIAL
            elif plan.status != PLAN_SUPERSEDED:
                plan.status = PLAN_COMPLETED
            ledger.save_plan(self._store, plan)
            self._event(
                "plan.finished",
                {"plan_id": plan.id, "status": plan.status, "desired": plan.desired,
                 "running": running_here},
                plan.request_id,
                plan.pit_id,
            )
            changed.append(plan.id)
        return changed

    def _absolutely_blocked(self, pending_starts) -> bool:
        """Terminal block: even base draw plus the smallest inrush cannot fit."""

        if not pending_starts:
            return False
        base = sum(session.watts for session in ledger.open_sessions(self._store))
        inrushes = []
        for step in pending_starts:
            pump = ledger.load_pump(self._store, step.pump_id)
            if pump is not None:
                inrushes.append(step.inrush_watts or pump.inrush_watts)
        smallest_inrush = min(inrushes, default=0.0)
        return base + smallest_inrush > self._capacity

    def reconcile(self) -> dict[str, object]:
        """Recover after a crash: resolve in-flight steps against the real rig."""

        with self._lock:
            recovered = {"starts": 0, "stops": 0}
            for plan in ledger.list_plans(self._store):
                if plan.status in {PLAN_COMPLETED, PLAN_SUPERSEDED}:
                    continue
                for step in plan.steps:
                    session = ledger.open_session_for(self._store, plan.id, step.seq)
                    if step.state == STEP_STARTING and session is not None:
                        if self._rig.is_running(step.pump_id):
                            step.state = STEP_STARTED
                            ledger.set_state(self._store, LAST_START_KEY, self._now())
                            recovered["starts"] += 1
                        else:
                            ledger.close_session(self._store, session, self._now(), self._stamp(), failed=True)
                            step.state = STEP_FAILED
                            step.error = "crash before start confirmed"
                            self._mark_failed_pump(step.pump_id, step.error)
                            self._promote_reserve(plan, step)
                        ledger.save_plan(self._store, plan)
                    elif step.state == STEP_STOPPING and session is not None:
                        if self._rig.is_running(step.pump_id):
                            step.state = STEP_PENDING
                        else:
                            ledger.close_session(self._store, session, self._now(), self._stamp())
                            step.state = STEP_DONE
                            recovered["stops"] += 1
                        ledger.save_plan(self._store, plan)
            self._event("reconcile", recovered)
            self.tick()
            return recovered

    def tick(self) -> dict[str, object]:
        """Advance every active plan one notch. Idempotent and repeatable."""

        with self._lock:
            now = self._now()
            processed: list[str] = []

            # Pass 1: free-standing due stops.  Linked swap stops wait for the
            # start that runs first in this same tick, so they are handled in
            # pass 3 after the start gate.
            for plan in ledger.active_plans(self._store):
                for step in plan.steps:
                    if step.state != STEP_PENDING or step.action != ACTION_STOP:
                        continue
                    if step.after_seq is not None:
                        continue
                    self._do_stop(plan, step)
                    processed.append(plan.id)

            # Pass 2: one global start gate.  At most one *new* pump starts per
            # stagger window (逐台加泵, shared budget across all pits).  A frozen
            # reserve replacing a just-failed primary is a substitution, not a
            # new inrush event, so it may start in the same tick.  When the
            # configured window is zero the gate reopens immediately.
            failure_replacement = False
            while True:
                last_start = float(ledger.get_state(self._store, LAST_START_KEY, 0.0))
                gate_open = failure_replacement or self._now() >= last_start + self._stagger
                failure_replacement = False
                if not gate_open:
                    break
                candidates = []
                for plan in ledger.active_plans(self._store):
                    for step in plan.steps:
                        if (
                            step.state == STEP_PENDING
                            and step.action == ACTION_START
                            and step.not_before <= self._now()
                        ):
                            candidates.append((plan, step))
                if not candidates:
                    break
                # Higher-water pit first; within a plan the frozen order stands.
                candidates.sort(key=lambda pair: (-pair[0].severity, pair[0].created_at, pair[1].seq))
                plan, step = candidates[0]
                started = self._do_start(plan, step)
                processed.append(plan.id)
                if not started and step.state in {STEP_FAILED, STEP_SKIPPED}:
                    # A promoted replacement is a substitution, not a new
                    # inrush event: it re-enters the gate without waiting out
                    # the stagger window and regardless of its last_start.
                    failure_replacement = True
                    continue
                if not started:
                    break  # power-blocked for this tick
                if self._stagger > 0:
                    break

            # Pass 3: linked swap stops whose predecessor just started.
            for plan in ledger.active_plans(self._store):
                for step in plan.steps:
                    if step.state != STEP_PENDING or step.action != ACTION_STOP:
                        continue
                    if step.after_seq is None:
                        continue
                    predecessor = plan.step(step.after_seq)
                    if predecessor is not None and predecessor.state == STEP_STARTED:
                        self._do_stop(plan, step)
                        processed.append(plan.id)

            # Plans whose starts all failed and which hold no reserve or
            # running pump cannot make progress: terminate them as partial.
            for plan in ledger.active_plans(self._store):
                starts = [step for step in plan.steps if step.action == ACTION_START]
                stops = [step for step in plan.steps if step.action == ACTION_STOP]
                if not starts:
                    continue
                if any(step.state in {STEP_PENDING, STEP_RESERVE, STEP_STARTING} for step in starts):
                    continue
                if any(step.state in {STEP_PENDING, STEP_STOPPING} for step in stops):
                    continue
                if not policy.running_on(self._store, plan.pit_id):
                    for step in plan.steps:
                        if step.state == STEP_RESERVE:
                            step.state = STEP_SUPERSEDED
                    plan.status = PLAN_PARTIAL
                    plan.note = (plan.note + " all requested starts failed").strip()
                    ledger.save_plan(self._store, plan)
                    self._event("plan.finished",
                                {"plan_id": plan.id, "status": PLAN_PARTIAL, "desired": plan.desired,
                                 "running": 0},
                                plan.request_id, plan.pit_id)
                    processed.append(plan.id)

            finished = self._finalize_plans()
            return {
                "at": now,
                "processed": sorted(set(processed)),
                "finished": sorted(set(finished)),
                "running_watts": self._power_draw(),
                "running": len(ledger.open_sessions(self._store)),
            }
