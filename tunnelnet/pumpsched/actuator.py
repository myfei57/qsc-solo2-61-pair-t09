"""Pump actuator interface plus a simulation used by tests and demos."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from .model import PumpState


class ActuatorError(Exception):
    """The actuator refused or failed to carry out a command."""


class PumpActuator(Protocol):
    """Hardware gateway; command ids make repeated delivery idempotent."""

    def start(self, command_id: str, pump_id: str) -> None:  # pragma: no cover - structural
        ...

    def stop(self, command_id: str, pump_id: str) -> None:  # pragma: no cover - structural
        ...

    def pump_state(self, pump_id: str) -> PumpState:  # pragma: no cover - structural
        ...


class SimActuator:
    """In-memory actuator that de-duplicates commands and can be told to fail."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._states: dict[str, PumpState] = {}
        self._seen: set[str] = set()
        self._fail_next: dict[str, str] = {}
        self.calls: list[dict[str, object]] = []

    def register(self, pump_id: str) -> None:
        self._states[pump_id] = PumpState.STOPPED

    def fail_next(self, pump_id: str, action: str) -> None:
        """Make the next ``action`` for ``pump_id`` raise ``ActuatorError``."""

        self._fail_next[pump_id] = action

    def pump_state(self, pump_id: str) -> PumpState:
        return self._states.get(pump_id, PumpState.STOPPED)

    def start(self, command_id: str, pump_id: str) -> None:
        self._dispatch(command_id, pump_id, "start")

    def stop(self, command_id: str, pump_id: str) -> None:
        self._dispatch(command_id, pump_id, "stop")

    def _dispatch(self, command_id: str, pump_id: str, action: str) -> None:
        self.calls.append(
            {
                "ts": self._clock(),
                "command_id": command_id,
                "pump_id": pump_id,
                "action": action,
            }
        )
        if command_id in self._seen:
            return  # already carried out: a repeated delivery is a no-op
        self._seen.add(command_id)
        if self._fail_next.get(pump_id) == action:
            del self._fail_next[pump_id]
            raise ActuatorError(f"{pump_id}: {action} refused by contactor")
        self._states[pump_id] = PumpState.RUNNING if action == "start" else PumpState.STOPPED
