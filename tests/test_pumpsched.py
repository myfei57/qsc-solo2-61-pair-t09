"""Behavioural tests for the multi-pump drainage scheduler."""

from __future__ import annotations

import tempfile
import unittest

from tunnelnet.audit import AuditRegistry
from tunnelnet.pumpsched import (
    Health,
    Pit,
    Pump,
    PumpScheduler,
    PumpState,
    RuntimeLedger,
    SchedulePolicy,
    SimActuator,
)
from tunnelnet.pumpsched import decision as decisions
from tunnelnet.store import Store

POLICY = SchedulePolicy(
    start_level=2.0,
    level_band=0.5,
    stop_level=1.0,
    emergency_level=4.0,
    min_on_seconds=60.0,
    min_off_seconds=120.0,
    failure_backoff_seconds=180.0,
    start_stagger_seconds=5.0,
    emergency_stagger_seconds=1.0,
)


class FakeClock:
    def __init__(self, ts: float = 1_000_000.0) -> None:
        self.ts = ts

    def __call__(self) -> float:
        return self.ts

    def advance(self, seconds: float) -> None:
        self.ts += seconds


class SchedulerCase(unittest.TestCase):
    limit_kw: float | None = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/data")
        self.ledger = RuntimeLedger(self.store)
        self.clock = FakeClock()
        self.actuator = SimActuator(clock=self.clock)
        self.audit = AuditRegistry()
        policy = SchedulePolicy(**{**POLICY.__dict__, "supply_limit_kw": self.limit_kw})
        self.sched = PumpScheduler(
            self.store,
            self.ledger,
            self.actuator,
            policy=policy,
            audit=self.audit,
            clock=self.clock,
            sleep=self.clock.advance,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def add_pit(self, pit_id: str, priority: int, count: int, kw: float = 22.0) -> list[str]:
        self.sched.add_pit(Pit(id=pit_id, priority=priority, water_level=0.5))
        ids = []
        for index in range(1, count + 1):
            pump_id = f"{pit_id}{index}"
            self.sched.add_pump(Pump(id=pump_id, pit_id=pit_id, rated_kw=kw))
            self.actuator.register(pump_id)
            ids.append(pump_id)
        return ids

    def set_level(self, pit_id: str, level: float) -> None:
        self.assertTrue(self.sched.set_level(pit_id, level))

    def starts_of(self, result: dict) -> list[str]:
        return [c["pump_id"] for c in result["commands"] if c["action"] == "start"]

    def stops_of(self, result: dict) -> list[str]:
        return [c["pump_id"] for c in result["commands"] if c["action"] == "stop"]

    def notes(self, result: dict, pit_id: str) -> list[str]:
        for plan in result["plans"]:
            if plan["pit_id"] == pit_id:
                return plan["notes"]
        return []


class RotationTest(SchedulerCase):
    def test_start_prefers_least_accumulated_runtime(self) -> None:
        self.add_pit("A", 1, 3)
        now = self.clock()
        self.ledger.record_start("A1", now - 7200)
        self.ledger.record_stop("A1", now - 3600)  # A1: 3600s
        self.ledger.record_start("A2", now - 1800)
        self.ledger.record_stop("A2", now - 1)  # A2: 1799s, A3: 0s
        self.set_level("A", 2.3)
        result = self.sched.run_round("level_alarm", idempotency_key="e1")
        self.assertEqual(self.starts_of(result), ["A3"])

    def test_stop_drops_longest_running_pump_first(self) -> None:
        self.add_pit("A", 1, 3)
        self.set_level("A", 4.2)
        self.sched.run_round("level_alarm", idempotency_key="e1")
        self.clock.advance(600)
        self.set_level("A", 0.8)
        result = self.sched.run_round("level_fall", idempotency_key="e2")
        # Staggered starts give A1 the most run time, then A2, then A3.
        self.assertEqual(self.stops_of(result), ["A1", "A2", "A3"])

    def test_fault_tagged_pump_is_never_selected(self) -> None:
        self.add_pit("A", 1, 2)
        self.sched.set_health("A1", Health.FAULT)
        self.set_level("A", 2.3)
        result = self.sched.run_round("level_alarm", idempotency_key="e1")
        self.assertEqual(self.starts_of(result), ["A2"])
        self.assertTrue(any("FAULT" in note for note in self.notes(result, "A")))


class ProtectionTest(SchedulerCase):
    def test_protected_pump_is_not_selected(self) -> None:
        self.add_pit("A", 1, 2)
        self.set_level("A", 2.3)
        self.sched.run_round("level_alarm", idempotency_key="e1")
        self.clock.advance(120)
        self.set_level("A", 0.8)
        self.sched.run_round("level_fall", idempotency_key="e2")  # A1 stops, protected
        self.clock.advance(30)  # still inside the 120s cool-down
        self.set_level("A", 2.3)
        result = self.sched.run_round("level_alarm", idempotency_key="e3")
        self.assertEqual(self.starts_of(result), ["A2"])
        self.assertEqual(self.sched.pump("A1").state, PumpState.STOPPED)
        self.assertTrue(any("protected" in note for note in self.notes(result, "A")))

    def test_protection_release_waits_for_next_round(self) -> None:
        self.add_pit("A", 1, 3)
        self.set_level("A", 2.3)
        self.sched.run_round("level_alarm", idempotency_key="e1")  # A1 starts
        self.clock.advance(120)
        self.set_level("A", 0.8)
        self.sched.run_round("level_fall", idempotency_key="e2")  # A1 stops
        self.clock.advance(119.5)  # protection ends 0.5s into the future
        self.set_level("A", 4.2)
        result = self.sched.run_round("level_alarm", idempotency_key="e3")
        # The emergency stagger (1s) carried the clock past the protection
        # deadline mid-round, yet A1 must stay excluded until the next round.
        self.assertEqual(self.starts_of(result), ["A2", "A3"])
        self.assertEqual(self.sched.pump("A1").state, PumpState.STOPPED)
        followup = self.sched.run_round("level_alarm", idempotency_key="e4")
        self.assertEqual(self.starts_of(followup), ["A1"])


class StagingTest(SchedulerCase):
    def test_emergency_starts_one_pump_at_a_time(self) -> None:
        self.add_pit("A", 1, 3)
        self.set_level("A", 4.2)
        result = self.sched.run_round("level_alarm", idempotency_key="e1")
        self.assertEqual(self.starts_of(result), ["A1", "A2", "A3"])
        starts = [c for c in self.actuator.calls if c["action"] == "start"]
        gaps = [b["ts"] - a["ts"] for a, b in zip(starts, starts[1:])]
        self.assertEqual(gaps, [1.0, 1.0])  # emergency stagger, never together

    def test_normal_starts_use_longer_stagger(self) -> None:
        self.add_pit("A", 1, 3)
        self.set_level("A", 2.7)  # two pumps wanted, not an emergency
        self.sched.run_round("level_alarm", idempotency_key="e1")
        starts = [c for c in self.actuator.calls if c["action"] == "start"]
        gaps = [b["ts"] - a["ts"] for a, b in zip(starts, starts[1:])]
        self.assertEqual(gaps, [5.0])


class SupplyBudgetTest(SchedulerCase):
    limit_kw = 45.0

    def test_supply_budget_caps_concurrent_pumps(self) -> None:
        self.add_pit("A", 1, 3)  # 3 x 22kW = 66kW wanted, 45kW available
        self.set_level("A", 4.2)
        result = self.sched.run_round("level_alarm", idempotency_key="e1")
        self.assertEqual(self.starts_of(result), ["A1", "A2"])
        self.assertEqual(self.sched.pump("A3").state, PumpState.STOPPED)
        self.assertTrue(any("budget" in note for note in self.notes(result, "A")))


class MultiPitTest(SchedulerCase):
    limit_kw = 50.0

    def test_simultaneous_pits_share_one_deterministic_snapshot(self) -> None:
        self.add_pit("A", 1, 2)
        self.add_pit("B", 0, 2)
        self.set_level("A", 3.0)
        self.set_level("B", 3.0)
        result = self.sched.run_round("level_alarm", idempotency_key="e1")
        # Higher priority pit A takes the budget (2 x 22kW of 50kW);
        # pit B's pumps are skipped, not silently started.
        self.assertEqual(self.starts_of(result), ["A1", "A2"])
        self.assertTrue(any("budget" in note for note in self.notes(result, "B")))

    def test_same_snapshot_reproduces_same_plan(self) -> None:
        self.add_pit("A", 1, 2)
        self.add_pit("B", 0, 2)
        self.set_level("A", 3.0)
        self.set_level("B", 3.0)
        first = self.sched.run_round("level_alarm", idempotency_key="e1")

        other = SchedulerCase()
        other.limit_kw = self.limit_kw
        other.setUp()
        try:
            other.add_pit("A", 1, 2)
            other.add_pit("B", 0, 2)
            other.set_level("A", 3.0)
            other.set_level("B", 3.0)
            second = other.sched.run_round("level_alarm", idempotency_key="e1")
        finally:
            other.tearDown()
        self.assertEqual(first["plans"], second["plans"])


class PartialFailureTest(SchedulerCase):
    def test_failed_start_is_partial_and_substitutes(self) -> None:
        self.add_pit("A", 1, 3)
        self.actuator.fail_next("A1", "start")
        self.set_level("A", 2.3)
        result = self.sched.run_round("level_alarm", idempotency_key="e1")
        self.assertTrue(result["partial_failure"])
        self.assertEqual(self.starts_of(result), ["A1", "A2"])  # A1 attempted, A2 took the slot
        self.assertEqual(self.sched.pump("A1").state, PumpState.FAILED)
        self.assertEqual(self.sched.pump("A1").health, Health.DEGRADED)
        self.assertGreater(self.sched.pump("A1").protection_until, self.clock())
        self.assertEqual(self.sched.pump("A2").state, PumpState.RUNNING)
        self.assertEqual(self.sched.pump("A3").state, PumpState.STOPPED)
        stored = decisions.load_decision(self.store, "e1")
        self.assertIsNotNone(stored["result"])
        statuses = {c.pump_id: c.status for c in decisions.list_commands(self.store, "e1")}
        self.assertEqual(statuses, {"A1": decisions.FAILED, "A2": decisions.OK})

    def test_failed_pump_rejoins_rotation_after_backoff_round(self) -> None:
        self.add_pit("A", 1, 3)
        self.actuator.fail_next("A1", "start")
        self.set_level("A", 2.3)
        self.sched.run_round("level_alarm", idempotency_key="e1")  # A1 fails, A2 runs
        self.clock.advance(200)  # backoff (180s) has ended
        self.set_level("A", 3.0)  # three pumps wanted
        result = self.sched.run_round("level_alarm", idempotency_key="e2")
        self.assertEqual(result["released"], ["A1"])
        # Degraded A1 ranks behind healthy A3 but still rejoins.
        self.assertEqual(self.starts_of(result), ["A3", "A1"])
        self.assertEqual(self.sched.pump("A1").state, PumpState.RUNNING)


class IdempotencyTest(SchedulerCase):
    def test_replay_with_same_key_dispatches_nothing(self) -> None:
        self.add_pit("A", 1, 2)
        self.set_level("A", 2.3)
        first = self.sched.run_round("level_alarm", idempotency_key="evt-1")
        calls = len(self.actuator.calls)
        second = self.sched.run_round("level_alarm", idempotency_key="evt-1")
        self.assertTrue(second["replayed"])
        self.assertEqual(second["decision_id"], first["decision_id"])
        self.assertEqual(len(self.actuator.calls), calls)
        self.assertEqual(len(decisions.list_commands(self.store)), 1)

    def test_command_ids_are_deterministic(self) -> None:
        self.add_pit("A", 1, 1)
        self.set_level("A", 2.3)
        result = self.sched.run_round("level_alarm", idempotency_key="evt-9")
        self.assertEqual(result["commands"][0]["command_id"], "evt-9:A1:start")


class LedgerStatsTest(SchedulerCase):
    def test_profile_and_energy_quote_the_same_runtime(self) -> None:
        self.add_pit("A", 1, 1)
        self.set_level("A", 2.3)
        self.sched.run_round("level_alarm", idempotency_key="e1")
        self.clock.advance(3600)
        self.set_level("A", 0.8)
        self.sched.run_round("level_fall", idempotency_key="e2")
        profile = self.sched.profile("A1")
        self.assertAlmostEqual(profile.runtime_hours, 1.0)
        self.assertEqual(profile.starts, 1)
        self.assertAlmostEqual(profile.energy_kwh, 22.0)
        self.assertAlmostEqual(self.sched.energy_kwh("A1", 0.0, self.clock()), 22.0)
        self.assertAlmostEqual(self.ledger.runtime_seconds("A1", self.clock()), 3600.0)


class RecoveryTest(SchedulerCase):
    def test_pending_command_is_reconciled_from_actuator_truth(self) -> None:
        self.add_pit("A", 1, 2)
        # Simulate a crash: the command record is still pending, the pump
        # record says stopped, but the actuator really did start A1.
        command = decisions.Command(
            id="round-000001:A1:start",
            decision_id="round-000001",
            pump_id="A1",
            action="start",
            status=decisions.PENDING,
            error="",
            updated_ts=self.clock(),
        )
        decisions.save_command(self.store, command)
        self.actuator.start("round-000001:A1:start", "A1")
        fixes = self.sched.recover()
        self.assertEqual(fixes, [{"pump_id": "A1", "fix": "marked_running"}])
        self.assertEqual(self.sched.pump("A1").state, PumpState.RUNNING)
        reconciled = {
            c.id: c.status for c in decisions.list_commands(self.store)
        }
        self.assertEqual(reconciled["round-000001:A1:start"], decisions.RECONCILED)
        self.clock.advance(100)
        self.assertAlmostEqual(self.ledger.runtime_seconds("A1", self.clock()), 100.0)

    def test_incomplete_decision_is_closed_as_aborted(self) -> None:
        self.add_pit("A", 1, 1)
        decisions.save_decision(
            self.store,
            {"id": "round-000007", "trigger": "level_alarm", "created_ts": self.clock(),
             "snapshot": {}, "plans": [], "result": None},
        )
        self.sched.recover()
        stored = decisions.load_decision(self.store, "round-000007")
        self.assertEqual(stored["result"]["status"], "aborted")


class MinOnTimeTest(SchedulerCase):
    def test_min_on_time_blocks_stop(self) -> None:
        self.add_pit("A", 1, 1)
        self.set_level("A", 2.3)
        self.sched.run_round("level_alarm", idempotency_key="e1")
        self.clock.advance(10)  # min on-time is 60s
        self.set_level("A", 0.8)
        result = self.sched.run_round("level_fall", idempotency_key="e2")
        self.assertEqual(self.stops_of(result), [])
        self.assertEqual(self.sched.pump("A1").state, PumpState.RUNNING)
        self.assertTrue(any("min on-time" in note for note in self.notes(result, "A")))

    def test_hysteresis_band_holds_current_state(self) -> None:
        self.add_pit("A", 1, 1)
        self.set_level("A", 2.3)
        self.sched.run_round("level_alarm", idempotency_key="e1")
        self.set_level("A", 1.5)  # between stop (1.0) and start (2.0)
        result = self.sched.run_round("periodic", idempotency_key="e2")
        self.assertEqual(result["commands"], [])
        self.assertEqual(self.sched.pump("A1").state, PumpState.RUNNING)


if __name__ == "__main__":
    unittest.main()
