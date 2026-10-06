"""End-to-end demo: two pits share a pump pool under a tight power budget.

Run with::

    python -m tunnelnet.dispatch.demo
"""

from __future__ import annotations

import tempfile

from tunnelnet.store import Store

from .audit import EventLog
from .model import PitConfig, PumpProfile
from .rig import SimRig
from .service import DispatchService


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def show(title: str, svc: DispatchService, clock: Clock) -> None:
    print(f"t={clock.t:>8.0f}  {title}")
    for row in svc.pump_statistics():
        guard = ""
        if row["ready_at"] > clock():
            guard = f" 保护中->{row['ready_at']:.0f}"
        print(
            f"    {row['pump_id']} health={row['health']:<8} running={row['running']!s:<5} "
            f"runtime={row['runtime_seconds']:>7.0f}s energy={row['energy_wh']:>8.1f}Wh{guard}"
        )
    print(f"    live draw: {svc.active_power_watts():.0f} W")
    print()


def main() -> None:
    clock = Clock()
    with tempfile.TemporaryDirectory() as tmp:
        store = Store.open(tmp)
        rig = SimRig()
        svc = DispatchService(
            store,
            rig,
            watts_capacity=4000,          # tight site budget
            stagger_seconds=10,           # one start per 10s window
            clock=clock,
            audit=EventLog(store),
        )

        # Shared pool: P3 cannot serve pit B.
        svc.register_pump(PumpProfile("P1", 1000, ("A", "B"), inrush_watts=1800, cooldown_seconds=300))
        svc.register_pump(PumpProfile("P2", 1000, ("A", "B"), inrush_watts=1800, cooldown_seconds=300))
        svc.register_pump(PumpProfile("P3", 1000, ("A",), inrush_watts=1800, cooldown_seconds=300))
        svc.register_pit(PitConfig("A", (0.4, 0.6, 0.8), (0.2, 0.3, 0.5), high_level=0.9))
        svc.register_pit(PitConfig("B", (0.5, 0.75), (0.3, 0.6), high_level=0.95))

        print("== pit A water rises to high level: pumps stage one at a time ==")
        svc.report_level("A", 0.95, request_id="A-hw")
        show("first window: only P1 inrushes", svc, clock)
        clock.advance(10); svc.tick(); show("+10s: P2 added", svc, clock)
        clock.advance(10); svc.tick(); show("+20s: P3 added", svc, clock)

        print("== pit B also goes high, but the pool and budget are leased ==")
        plan_b = svc.report_level("B", 0.97, request_id="B-hw")
        print(f"    B plan: {plan_b.status} - {plan_b.note}\n")

        print("== water recedes below every stop band: surplus pumps stop ==")
        svc.report_level("A", 0.1, request_id="A-low")
        show("A drained", svc, clock)

        print("== rotation: shortest-runtime healthy idle pump takes over ==")
        clock.advance(600)
        svc.report_level("A", 0.45, request_id="A-on")  # P3 has zero runtime now
        clock.advance(900)
        plan_r = svc.request_rotation("A", request_id="A-rot")
        print("    rotation:", [(s.action, s.pump_id, s.state) for s in plan_r.steps])
        show("after rotation", svc, clock)

        ok, message = svc.audit_verify()
        print(f"== audit chain: {'OK' if ok else 'BROKEN'} ({message}) ==")


if __name__ == "__main__":
    main()
