"""Multi-pump drainage scheduler.

One scheduler owns every pit, so simultaneous requests from several
pits are serialised behind one lock and decided from a single round
snapshot. A round is:

1. snapshot: freeze pump and pit state at ``now``; protection windows
   that ended are released here and only here (policy rule 2);
2. plan: pure functions in :mod:`.policy` rank pumps by health, then
   accumulated run time, then id;
3. persist: the decision and its commands are written to the store
   before anything is dispatched;
4. dispatch: stops first, then starts one at a time with a stagger
   (policy rule 1); a failed command fails only its own pump, which is
   parked in back-off while the next candidate takes its slot;
5. record: command outcomes, ledger segments and the decision result
   are persisted, and an audit entry is appended.

Repeating a round with the same idempotency key returns the stored
result without dispatching anything. After a crash, :meth:`recover`
reconciles pending commands against the actuator's truth.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable

from tunnelnet.audit import AuditRegistry, Record
from tunnelnet.store import Store

from . import decision as decisions
from . import report
from .actuator import ActuatorError, PumpActuator
from .ledger import RuntimeLedger
from .model import Health, Pit, Pump, PumpState, SchedulePolicy
from .policy import PitPlan, plan_pit

PUMP_KIND = "pump"


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class PumpScheduler:
    """Serialised, persistent, idempotent scheduling rounds."""

    def __init__(
        self,
        store: Store,
        ledger: RuntimeLedger,
        actuator: PumpActuator,
        policy: SchedulePolicy | None = None,
        audit: AuditRegistry | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._store = store
        self._ledger = ledger
        self._actuator = actuator
        self._policy = policy or SchedulePolicy()
        self._audit = audit
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()
        self._pits: dict[str, Pit] = {}
        self._pumps: dict[str, Pump] = {}
        for record_id in store.list(PUMP_KIND):
            payload = store.load(PUMP_KIND, record_id)
            if payload is not None:
                pump = Pump.from_dict(payload)
                self._pumps[pump.id] = pump

    # -- inventory ---------------------------------------------------

    def add_pit(self, pit: Pit) -> Pit:
        with self._lock:
            self._pits[pit.id] = pit
            return pit

    def add_pump(self, pump: Pump) -> Pump:
        with self._lock:
            self._pumps[pump.id] = pump
            self._save_pump(pump)
            return pump

    def set_level(self, pit_id: str, level: float) -> bool:
        with self._lock:
            pit = self._pits.get(pit_id)
            if pit is None:
                return False
            pit.water_level = level
            return True

    def set_health(self, pump_id: str, health: Health) -> bool:
        """Maintenance tagging; a FAULT pump is never selected."""

        with self._lock:
            pump = self._pumps.get(pump_id)
            if pump is None:
                return False
            pump.health = health
            self._save_pump(pump)
            self._audit_append(f"pumpsched:{pump_id} health={health.value}", self._clock())
            return True

    def pump(self, pump_id: str) -> Pump | None:
        with self._lock:
            return self._pumps.get(pump_id)

    def pumps(self) -> list[Pump]:
        with self._lock:
            return [self._pumps[key] for key in sorted(self._pumps)]

    def pits(self) -> list[Pit]:
        with self._lock:
            return [self._pits[key] for key in sorted(self._pits)]

    # -- the scheduling round ----------------------------------------

    def run_round(self, trigger: str, idempotency_key: str | None = None) -> dict:
        """Run one global round over every pit, or replay a stored one."""

        with self._lock:
            if idempotency_key is not None:
                stored = decisions.load_decision(self._store, idempotency_key)
                if stored is not None:
                    result = stored.get("result")
                    if result is None:
                        raise RuntimeError(
                            f"decision {idempotency_key} is incomplete; run recover() first"
                        )
                    replayed = dict(result)
                    replayed["replayed"] = True
                    return replayed
            return self._run_locked(trigger, idempotency_key)

    def _run_locked(self, trigger: str, key: str | None) -> dict:
        now = self._clock()

        # Policy rule 2: protection releases only at the round boundary.
        released = []
        for pump in self._pumps.values():
            if pump.state == PumpState.FAILED and pump.protection_until <= now:
                pump.state = PumpState.STOPPED
                self._save_pump(pump)
                released.append(pump.id)

        runtime = {pid: self._ledger.runtime_seconds(pid, now) for pid in self._pumps}
        plans: list[PitPlan] = []
        for pit in self._ordered_pits():
            pumps = [p for p in self._pumps.values() if p.pit_id == pit.id]
            plans.append(plan_pit(pit, pumps, lambda pid: runtime[pid], now, self._policy))

        decision_id = key or f"round-{len(decisions.list_decisions(self._store)) + 1:06d}"
        decision: dict[str, object] = {
            "id": decision_id,
            "trigger": trigger,
            "created_ts": now,
            "snapshot": {
                "pits": [
                    {"id": pit.id, "level": pit.water_level, "priority": pit.priority}
                    for pit in self._ordered_pits()
                ],
                "pumps": [
                    {**pump.as_dict(), "runtime_s": runtime[pump.id]} for pump in self.pumps()
                ],
                "released": released,
            },
            "plans": [plan.as_dict() for plan in plans],
            "result": None,
        }
        decisions.save_decision(self._store, decision)
        self._audit_append(f"pumpsched:{decision_id} planned", now)

        results = self._dispatch(decision_id, plans)

        result = {
            "decision_id": decision_id,
            "trigger": trigger,
            "released": released,
            "plans": [plan.as_dict() for plan in plans],
            "commands": results,
            "partial_failure": any(not entry["ok"] for entry in results),
        }
        decision["result"] = result
        decisions.save_decision(self._store, decision)
        self._audit_append(
            f"pumpsched:{decision_id} done partial_failure={result['partial_failure']}",
            self._clock(),
        )
        return result

    def _ordered_pits(self) -> list[Pit]:
        """Deterministic contention order: priority, then level, then id."""

        return sorted(
            self._pits.values(),
            key=lambda pit: (-pit.priority, -pit.water_level, pit.id),
        )

    def _dispatch(self, decision_id: str, plans: list[PitPlan]) -> list[dict]:
        results: list[dict] = []
        # Stops first: they free supply capacity before any start draws
        # on it. A stop that fails leaves its pump counted below.
        for plan in plans:
            for pump_id in plan.to_stop:
                results.append(self._execute(decision_id, pump_id, "stop"))

        used_kw = sum(p.rated_kw for p in self._pumps.values() if p.state == PumpState.RUNNING)
        limit = self._policy.supply_limit_kw
        started_any = False
        for plan in plans:
            stagger = (
                self._policy.emergency_stagger_seconds
                if plan.emergency
                else self._policy.start_stagger_seconds
            )
            started = 0
            for pump_id in plan.candidates:
                if started >= plan.want_starts:
                    break
                pump = self._pumps[pump_id]
                if limit is not None and used_kw + pump.rated_kw > limit:
                    plan.notes.append(f"{pump_id}: supply budget exhausted, skipped")
                    continue
                if started_any:
                    # Policy rule 1: pumps join one at a time, even in
                    # an emergency; two contactors never close together.
                    self._sleep(stagger)
                outcome = self._execute(decision_id, pump_id, "start")
                results.append(outcome)
                started_any = True
                if outcome["ok"]:
                    used_kw += pump.rated_kw
                    started += 1
                else:
                    plan.notes.append(f"{pump_id}: start failed, trying next candidate")
            if started < plan.want_starts:
                plan.notes.append(f"{plan.pit_id}: started {started}/{plan.want_starts}")
        return results

    def _execute(self, decision_id: str, pump_id: str, action: str) -> dict:
        command_id = f"{decision_id}:{pump_id}:{action}"
        decisions.save_command(
            self._store,
            decisions.Command(
                id=command_id,
                decision_id=decision_id,
                pump_id=pump_id,
                action=action,
                status=decisions.PENDING,
                error="",
                updated_ts=self._clock(),
            ),
        )
        try:
            if action == "start":
                self._actuator.start(command_id, pump_id)
            else:
                self._actuator.stop(command_id, pump_id)
        except ActuatorError as exc:
            self._apply_failure(pump_id, action)
            return self._finish_command(command_id, decision_id, pump_id, action,
                                        decisions.FAILED, str(exc))
        self._apply_success(pump_id, action)
        return self._finish_command(command_id, decision_id, pump_id, action,
                                    decisions.OK, "")

    def _finish_command(
        self, command_id: str, decision_id: str, pump_id: str, action: str,
        status: str, error: str,
    ) -> dict:
        decisions.save_command(
            self._store,
            decisions.Command(
                id=command_id,
                decision_id=decision_id,
                pump_id=pump_id,
                action=action,
                status=status,
                error=error,
                updated_ts=self._clock(),
            ),
        )
        ok = status == decisions.OK
        self._audit_append(f"pumpsched:{command_id} {status}", self._clock())
        return {"command_id": command_id, "pump_id": pump_id, "action": action,
                "ok": ok, "error": error}

    def _apply_success(self, pump_id: str, action: str) -> None:
        now = self._clock()
        pump = self._pumps[pump_id]
        if action == "start":
            pump.state = PumpState.RUNNING
            pump.started_ts = now
            pump.protection_until = 0.0
            self._ledger.record_start(pump_id, now)
        else:
            pump.state = PumpState.STOPPED
            pump.started_ts = None
            pump.protection_until = now + self._policy.min_off_seconds
            self._ledger.record_stop(pump_id, now)
        self._save_pump(pump)

    def _apply_failure(self, pump_id: str, action: str) -> None:
        if action == "stop":
            return  # still running; the next round plans the retry
        now = self._clock()
        pump = self._pumps[pump_id]
        pump.state = PumpState.FAILED
        if pump.health == Health.HEALTHY:
            pump.health = Health.DEGRADED
        pump.protection_until = now + self._policy.failure_backoff_seconds
        self._save_pump(pump)

    # -- recovery ------------------------------------------------------

    def recover(self) -> list[dict]:
        """Reconcile interrupted commands against the actuator's truth."""

        with self._lock:
            now = self._clock()
            fixes = []
            for command in decisions.pending_commands(self._store):
                pump = self._pumps.get(command.pump_id)
                if pump is None:
                    continue
                actual = self._actuator.pump_state(pump.id)
                if actual == PumpState.RUNNING and pump.state != PumpState.RUNNING:
                    pump.state = PumpState.RUNNING
                    pump.started_ts = command.updated_ts
                    self._ledger.record_start(pump.id, command.updated_ts)
                    self._save_pump(pump)
                    fixes.append({"pump_id": pump.id, "fix": "marked_running"})
                elif actual == PumpState.STOPPED and pump.state == PumpState.RUNNING:
                    pump.state = PumpState.STOPPED
                    pump.started_ts = None
                    pump.protection_until = now + self._policy.min_off_seconds
                    self._ledger.record_stop(pump.id, now)
                    self._save_pump(pump)
                    fixes.append({"pump_id": pump.id, "fix": "marked_stopped"})
                decisions.save_command(
                    self._store,
                    decisions.Command(
                        id=command.id,
                        decision_id=command.decision_id,
                        pump_id=command.pump_id,
                        action=command.action,
                        status=decisions.RECONCILED,
                        error=command.error,
                        updated_ts=now,
                    ),
                )
            for decision in decisions.list_decisions(self._store):
                if decision.get("result") is None:
                    decision["result"] = {
                        "decision_id": decision["id"],
                        "status": "aborted",
                        "reason": "scheduler crashed before the round completed",
                    }
                    decisions.save_decision(self._store, decision)
            if fixes:
                self._audit_append(f"pumpsched:recovery fixes={len(fixes)}", now)
            return fixes

    # -- profiles and energy, both read from the ledger -----------------

    def profile(self, pump_id: str) -> report.PumpProfile | None:
        with self._lock:
            pump = self._pumps.get(pump_id)
            if pump is None:
                return None
            return report.pump_profile(pump, self._ledger, self._clock())

    def profiles(self) -> list[report.PumpProfile]:
        with self._lock:
            now = self._clock()
            return [report.pump_profile(pump, self._ledger, now) for pump in self.pumps()]

    def energy_kwh(self, pump_id: str, since: float, until: float) -> float:
        with self._lock:
            pump = self._pumps[pump_id]
            return report.energy_kwh(pump, self._ledger, since, until)

    def pit_energy_kwh(self, pit_id: str, since: float, until: float) -> float:
        with self._lock:
            return report.pit_energy_kwh(pit_id, self.pumps(), self._ledger, since, until)

    def decisions(self) -> list[dict]:
        return decisions.list_decisions(self._store)

    # -- internals -------------------------------------------------------

    def _save_pump(self, pump: Pump) -> None:
        self._store.save(PUMP_KIND, pump.id, pump.as_dict())

    def _audit_append(self, event: str, ts: float) -> None:
        if self._audit is not None:
            self._audit.append(Record(id=uuid.uuid4().hex, event=event, at=_utc(ts)))
