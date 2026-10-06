"""Hardware seam for the pump rig.

The dispatch service only knows the :class:`PumpRig` protocol; production code
adapts it to PLC/IO wiring.  Commands must be idempotent from the rig's side
(issuing start twice does not double-start a motor), which lets the dispatcher
retry after a crash without harm.
"""

from __future__ import annotations

from typing import Protocol


class PumpRig(Protocol):
    """Physical pump commands. Both commands report success explicitly."""

    def start(self, pump_id: str) -> bool: ...  # pragma: no cover - structural

    def stop(self, pump_id: str) -> bool: ...  # pragma: no cover - structural

    def is_running(self, pump_id: str) -> bool: ...  # pragma: no cover - structural


class SimRig:
    """In-memory rig for tests and demos; pumps can be forced to fail."""

    def __init__(self) -> None:
        self._running: set[str] = set()
        self._broken: set[str] = set()
        self.starts = 0
        self.stops = 0

    def fail_start(self, pump_id: str) -> None:
        self._broken.add(pump_id)

    def repair(self, pump_id: str) -> None:
        self._broken.discard(pump_id)

    def start(self, pump_id: str) -> bool:
        if pump_id in self._broken:
            return False
        self._running.add(pump_id)
        self.starts += 1
        return True

    def stop(self, pump_id: str) -> bool:
        if pump_id in self._broken:
            return False
        self._running.discard(pump_id)
        self.stops += 1
        return True

    def is_running(self, pump_id: str) -> bool:
        return pump_id in self._running

    def running(self) -> list[str]:
        return sorted(self._running)
