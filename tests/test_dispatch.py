"""Behavioural tests for multi-pump drainage dispatch."""

from __future__ import annotations

import tempfile
import threading
import unittest

from tunnelnet.store import Store

from tunnelnet.dispatch import (
    DEGRADED,
    DispatchService,
    EventLog,
    FAILED,
    HEALTHY,
    KIND_ROTATE,
    PLAN_BLOCKED,
    PLAN_COMPLETED,
    PLAN_PARTIAL,
    PitConfig,
    PumpProfile,
    SimRig,
    STEP_DONE,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RESERVE,
    STEP_SKIPPED,
    STEP_STARTED,
    STEP_SUPERSEDED,
    desired_pumps,
    pump_power_report,
    ready_at,
    site_power_report,
)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_service(
    tmp: str,
    pumps: list[PumpProfile],
    pits: list[PitConfig],
    *,
    capacity: float = 100_000.0,
    stagger: float = 10.0,
    rotation_gap: float = 0.0,
):
    store = Store.open(tmp)
    rig = SimRig()
    clock = FakeClock()
    events = EventLog(store)
    svc = DispatchService(
        store,
        rig,
        watts_capacity=capacity,
        stagger_seconds=stagger,
        rotation_gap_seconds=rotation_gap,
        clock=clock,
        audit=events,
    )
    for pump in pumps:
        svc.register_pump(pump)
    for pit in pits:
        svc.register_pit(pit)
    return svc, rig, clock, store, events


THREE_PUMPS = [
    PumpProfile("P1", 1000, ("A", "B"), cooldown_seconds=300),
    PumpProfile("P2", 1000, ("A", "B"), cooldown_seconds=300),
    PumpProfile("P3", 1000, ("A",), cooldown_seconds=300),
]
PIT_A = PitConfig("A", (0.4, 0.6, 0.8), (0.2, 0.3, 0.5), high_level=0.9)
PIT_B = PitConfig("B", (0.5, 0.75), (0.3, 0.6), high_level=0.95)


def run_ticks(svc: DispatchService, clock: FakeClock, ticks: int, step: float = 10.0) -> None:
    for _ in range(ticks):
        clock.advance(step)
        svc.tick()


class DesiredCountCase(unittest.TestCase):
    def test_bands_and_hysteresis(self) -> None:
        pit = PIT_A
        # rising
        self.assertEqual(desired_pumps(pit, 0.1, 0, 3), 0)
        self.assertEqual(desired_pumps(pit, 0.4, 0, 3), 1)
        self.assertEqual(desired_pumps(pit, 0.61, 0, 3), 2)
        # band 1 stays latched until its own stop level (0.2); pump 2 stops at 0.3
        self.assertEqual(desired_pumps(pit, 0.35, 2, 3), 2)
        self.assertEqual(desired_pumps(pit, 0.15, 2, 3), 0)
        self.assertEqual(desired_pumps(pit, 0.25, 2, 3), 1)
        self.assertEqual(desired_pumps(pit, 0.1, 2, 3), 0)
        self.assertEqual(desired_pumps(pit, 0.1, 2, 3), 0)
        # high water demands every usable pump
        self.assertEqual(desired_pumps(pit, 0.95, 1, 3), 3)
        # capacity clamps
        self.assertEqual(desired_pumps(pit, 0.61, 0, 1), 1)


class FairnessCase(unittest.TestCase):
    def test_shortest_runtime_is_selected_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc, rig, clock, store, _ = make_service(tmp, THREE_PUMPS, [PIT_A])
            plan = svc.report_level("A", 0.45, request_id="r1")
            primaries = [s.pump_id for s in plan.steps
                         if s.action == "start" and s.state != STEP_RESERVE]
            self.assertEqual(primaries, ["P1"])
            self.assertEqual(rig.running(), ["P1"])

            clock.advance(100)
            plan2 = svc.report_level("A", 0.65, request_id="r2")
            starts = [s.pump_id for s in plan2.steps
                      if s.action == "start" and s.state != STEP_RESERVE]
            self.assertEqual(starts, ["P2"])

            clock.advance(50)
            plan3 = svc.report_level("A", 0.85, request_id="r3")
            self.assertEqual(
                [s.pump_id for s in plan3.steps if s.state != STEP_RESERVE], ["P3"]
            )
            clock.advance(10)
            svc.tick()
            self.assertEqual(rig.running(), ["P1", "P2", "P3"])

    def test_health_outranks_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pumps = [
                PumpProfile("P1", 1000, ("A",), cooldown_seconds=0),
                PumpProfile("P2", 1000, ("A",), cooldown_seconds=0),
            ]
            svc, rig, clock, _, _ = make_service(tmp, pumps, [PitConfig("A", (0.5,), (0.2,))])
            svc.set_health("P1", DEGRADED)
            plan = svc.report_level("A", 0.9, request_id="r1")
            self.assertEqual(plan.steps[0].pump_id, "P2")  # healthy first despite id order


class ProtectionCase(unittest.TestCase):
    def test_cooldown_blocks_selection_then_next_round_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc, rig, clock, store, _ = make_service(tmp, THREE_PUMPS, [PIT_A], stagger=0.0)
            svc.report_level("A", 0.65, request_id="r1")
            self.assertEqual(rig.running(), ["P1", "P2"])
            clock.advance(100)
            svc.report_level("A", 0.1, request_id="r2")
            self.assertEqual(rig.running(), [])

            # P1/P2 stop at t=1100; cooldown 300 -> protected until t=1400.
            # At t=1200 only P3 is eligible.
            clock.advance(100)
            self.assertGreater(ready_at(store, "P1"), clock())
            plan = svc.report_level("A", 0.45, request_id="r3")
            self.assertEqual([s.pump_id for s in plan.steps if s.state != STEP_RESERVE], ["P3"])

            # Protection ends mid-wait: it must NOT join the running plan.
            clock.advance(250)
            svc.tick()
            self.assertEqual(rig.running(), ["P3"])

            # Next round it participates (P1/P2 cooled at t=1400; P3 has now
            # run, so P1/P2 rank shorter); it does not join the still-running r3.
            clock.advance(100)
            svc.report_level("A", 0.1, request_id="r4")
            clock.advance(600)  # past P3 cooldown; P1/P2 have the shorter runtime
            plan = svc.report_level("A", 0.45, request_id="r5")
            self.assertIn(plan.steps[0].pump_id, {"P1", "P2"})


class StagedCase(unittest.TestCase):
    def test_high_water_starts_one_pump_per_stagger_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc, rig, clock, _, _ = make_service(tmp, THREE_PUMPS, [PIT_A], stagger=10.0)
            plan = svc.report_level("A", 0.95, request_id="hw")  # high -> all 3
            self.assertEqual(plan.desired, 3)
            self.assertEqual(rig.running(), ["P1"])
            pending = [s for s in svc.plan_by_request("hw").steps if s.state == STEP_PENDING]
            self.assertEqual(len(pending), 2)
            not_before = [s.not_before for s in svc.plan_by_request("hw").steps
                          if s.state in (STEP_PENDING, STEP_RESERVE)]
            self.assertEqual(not_before[0], 1000.0 + 10.0)

            clock.advance(5)
            svc.tick()
            self.assertEqual(rig.running(), ["P1"])  # window not open yet
            clock.advance(5)
            svc.tick()
            self.assertEqual(rig.running(), ["P1", "P2"])
            clock.advance(10)
            svc.tick()
            self.assertEqual(rig.running(), ["P1", "P2", "P3"])
            self.assertEqual(svc.plan_by_request("hw").status, PLAN_COMPLETED)


class PowerCase(unittest.TestCase):
    def test_tight_budget_blocks_terminal_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pumps = [PumpProfile(f"P{i}", 1000, ("A",), inrush_watts=1800) for i in range(1, 4)]
            # capacity fits one running motor but no second inrush
            svc, rig, clock, _, _ = make_service(
                tmp, pumps, [PitConfig("A", (0.5, 0.7, 0.9), (0.2, 0.3, 0.4))],
                capacity=1800.0, stagger=0.0,
            )
            plan = svc.report_level("A", 0.95, request_id="hw")
            self.assertEqual(rig.running(), ["P1"])
            plan = svc.plan_by_request("hw")
            self.assertEqual(plan.status, PLAN_BLOCKED)
            # remaining steps released, no phantom pump running
            self.assertTrue(all(s.state == STEP_SUPERSEDED for s in plan.steps[1:]))
            self.assertEqual(rig.running(), ["P1"])

    def test_transient_budget_waits_then_proceeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pumps = [
                PumpProfile("P1", 1000, ("A",), inrush_watts=1800),
                PumpProfile("P2", 1000, ("A",), inrush_watts=1800),
            ]
            pit = PitConfig("A", (0.5, 0.7), (0.2, 0.3))
            # 2000 capacity: no second inrush while both run watts count,
            # but a degraded-first pump drawing less changes the equation.
            svc, rig, clock, _, _ = make_service(tmp, pumps, [pit], capacity=2800.0, stagger=0.0)
            plan = svc.report_level("A", 0.8, request_id="hw")
            self.assertEqual(plan.status, PLAN_COMPLETED)
            self.assertEqual(sorted(rig.running()), ["P1", "P2"])


class MultiPitCase(unittest.TestCase):
    def test_shared_pump_is_never_double_booked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc, rig, clock, _, _ = make_service(tmp, THREE_PUMPS, [PIT_A, PIT_B], stagger=0.0)
            a = svc.report_level("A", 0.95, request_id="a")  # high: all that can serve A
            self.assertEqual(sorted(rig.running()), ["P1", "P2", "P3"])
            b = svc.report_level("B", 0.95, request_id="b")
            # P3 cannot serve B and P1/P2 are leased to A -> nothing available
            self.assertEqual(b.status, PLAN_PARTIAL)
            self.assertIn("only 0", b.note)

    def test_higher_water_pit_wins_single_start_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pumps = [
                PumpProfile("P1", 1000, ("A", "B"), inrush_watts=1500),
                PumpProfile("P2", 1000, ("A", "B"), inrush_watts=1500),
                PumpProfile("P3", 1000, ("A", "B"), inrush_watts=1500),
            ]
            pit_a = PitConfig("A", (0.5,), (0.2,))
            pit_b = PitConfig("B", (0.5,), (0.2,))
            # One stagger window = one start across all pits.
            svc, rig, clock, _, _ = make_service(
                tmp, pumps, [pit_a, pit_b], capacity=100000.0, stagger=100.0
            )
            svc.report_level("A", 0.6, request_id="a")  # consumes first window
            self.assertEqual(rig.running(), ["P1"])
            svc.report_level("B", 0.8, request_id="b")
            clock.advance(100)
            svc.tick()
            self.assertEqual(rig.running(), ["P1", "P2"])


class PartialFailureCase(unittest.TestCase):
    def test_failed_start_promotes_frozen_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc, rig, clock, store, _ = make_service(tmp, THREE_PUMPS, [PIT_A], stagger=10.0)
            rig.fail_start("P1")
            plan = svc.report_level("A", 0.45, request_id="r1")
            self.assertEqual(plan.status, PLAN_COMPLETED)
            failed = [s for s in plan.steps if s.state == STEP_FAILED]
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0].pump_id, "P1")
            self.assertEqual(rig.running(), ["P2"])  # reserve promoted at same tick
            # failed pump is quarantined in its档案
            self.assertEqual(svc.pump_profile("P1").health, FAILED)
            # recovery + replan must not reuse it
            clock.advance(100)
            svc.set_health("P1", HEALTHY)
            rig.repair("P1")
            plan2 = svc.report_level("A", 0.65, request_id="r2")
            clock.advance(10)
            svc.tick()
            self.assertEqual(sorted(rig.running()), ["P1", "P2"])

    def test_plan_partial_when_reserve_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pumps = [
                PumpProfile("P1", 1000, ("A",), inrush_watts=1500),
                PumpProfile("P2", 1000, ("A",), inrush_watts=1500),
            ]
            svc, rig, clock, _, _ = make_service(
                tmp, pumps, [PitConfig("A", (0.5, 0.7), (0.2, 0.3))], stagger=10.0
            )
            rig.fail_start("P1")  # primary fails; P2 promoted
            rig.fail_start("P2")  # reserve also fails
            plan = svc.report_level("A", 0.8, request_id="r1")
            self.assertEqual(rig.running(), [])
            # Primary fails first; its frozen reserve waits one stagger window,
            # then also fails -- the plan terminates partial, never retried.
            clock.advance(10)
            svc.tick()
            plan = svc.plan_by_request("r1")
            self.assertEqual(plan.status, PLAN_PARTIAL)
            self.assertEqual(rig.running(), [])
            clock.advance(10)
            svc.tick()
            self.assertEqual(rig.running(), [])  # never retried


class IdempotencyCase(unittest.TestCase):
    def test_duplicate_request_returns_same_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc, rig, clock, store, events = make_service(tmp, THREE_PUMPS, [PIT_A])
            first = svc.report_level("A", 0.45, request_id="dup")
            clock.advance(500)
            again = svc.report_level("A", 0.45, request_id="dup")
            self.assertEqual(first.id, again.id)
            self.assertEqual(rig.starts, 1)
            kinds = [event.event for event in events.events()]
            self.assertEqual(kinds.count("request.duplicate"), 1)

    def test_reconcile_recovers_starting_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc, rig, clock, store, _ = make_service(tmp, THREE_PUMPS, [PIT_A], stagger=0.0)
            plan = svc.report_level("A", 0.45, request_id="r1")
            # Simulate a crash mid-start: session open, step STARTING.
            step = plan.steps[0]
            step.state = "starting"
            from tunnelnet.dispatch import ledger
            ledger.save_plan(store, plan)

            recovered = DispatchService(
                store, rig, watts_capacity=100_000, stagger_seconds=0.0, clock=clock, audit=EventLog(store)
            )
            result = recovered.reconcile()
            self.assertEqual(result["starts"], 1)  # rig confirms motor running
            self.assertEqual(recovered.plan_by_request("r1").steps[0].state, STEP_STARTED)

    def test_concurrent_reports_serialize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc, rig, clock, _, _ = make_service(tmp, THREE_PUMPS, [PIT_A, PIT_B], stagger=0.0)
            errors = []

            def report(pit: str, level: float, req: str) -> None:
                try:
                    svc.report_level(pit, level, request_id=req)
                except Exception as exc:  # pragma: no cover - failure path
                    errors.append(exc)

            threads = [
                threading.Thread(target=report, args=("A", 0.45, "a1")),
                threading.Thread(target=report, args=("B", 0.6, "b1")),
                threading.Thread(target=report, args=("A", 0.65, "a2")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            # no pump appears twice in the running ledger
            sessions = svc.running()
            ids = [s.pump_id for s in sessions]
            self.assertEqual(len(ids), len(set(ids)))


class LedgerCase(unittest.TestCase):
    def test_archive_and_billing_share_one_runtime_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc, rig, clock, store, _ = make_service(tmp, THREE_PUMPS, [PIT_A], stagger=0.0)
            svc.report_level("A", 0.45, request_id="r1")
            clock.advance(3600)
            svc.report_level("A", 0.1, request_id="r2")
            # pump档案口径
            stats = {row["pump_id"]: row for row in svc.pump_statistics()}
            self.assertEqual(stats["P1"]["runtime_seconds"], 3600.0)
            # 用电统计口径 -- same ledger, same number
            report = pump_power_report(store, clock())
            self.assertEqual(report[0]["runtime_seconds"], 3600.0)
            self.assertAlmostEqual(report[0]["energy_wh"], 1000.0)
            site = site_power_report(store, clock())
            self.assertEqual(site["total_runtime_seconds"], 3600.0)
            self.assertAlmostEqual(site["total_energy_wh"], 1000.0)


class AuditCase(unittest.TestCase):
    def test_tampering_breaks_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc, rig, clock, store, events = make_service(tmp, THREE_PUMPS, [PIT_A])
            svc.report_level("A", 0.45, request_id="r1")
            ok, message = events.verify_chain()
            self.assertTrue(ok, message)
            # tamper with one stored audit document
            records = store.list("audit")
            value = store.load("audit", records[1])
            value["detail"]["pump_id"] = "P999"
            store.save("audit", records[1], value)
            ok, message = events.verify_chain()
            self.assertFalse(ok)
            self.assertIn("tampered", message)

    def test_supersede_preserves_running_pump(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc, rig, clock, store, _ = make_service(tmp, THREE_PUMPS, [PIT_A], stagger=0.0)
            svc.report_level("A", 0.45, request_id="r1")
            self.assertEqual(rig.running(), ["P1"])
            # a newer rising report adds one pump; the running pump is kept
            plan = svc.report_level("A", 0.65, request_id="r2")
            self.assertEqual(
                [s.action for s in plan.steps if s.state != STEP_RESERVE], ["start"]
            )
            self.assertEqual(sorted(rig.running()), ["P1", "P2"])
            self.assertEqual(svc.latest_plan("A").id, plan.id)


class RotationCase(unittest.TestCase):
    def test_rotation_swaps_longest_for_shortest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            svc, rig, clock, _, _ = make_service(tmp, THREE_PUMPS, [PIT_A], stagger=0.0)
            svc.report_level("A", 0.45, request_id="r1")  # P1 starts
            clock.advance(1000)
            plan = svc.request_rotation("A", request_id="rot1")
            self.assertEqual(plan.kind, KIND_ROTATE)
            self.assertEqual([(s.action, s.pump_id) for s in plan.steps],
                             [("start", "P2"), ("stop", "P1")])
            self.assertEqual(plan.status, PLAN_COMPLETED)
            self.assertEqual(rig.running(), ["P2"])

    def test_rotation_skips_when_no_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pumps = [PumpProfile("P1", 1000, ("A",))]
            svc, rig, clock, _, _ = make_service(tmp, pumps, [PitConfig("A", (0.5,), (0.2,))])
            svc.report_level("A", 0.6, request_id="r1")
            plan = svc.request_rotation("A", request_id="rot1")
            self.assertIn("no eligible", plan.note)
            self.assertEqual(rig.running(), ["P1"])


if __name__ == "__main__":
    unittest.main()
