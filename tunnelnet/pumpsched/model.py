"""Domain model for the multi-pump drainage scheduler."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Health(str, enum.Enum):
    """Maintenance state of one pump."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAULT = "fault"


class PumpState(str, enum.Enum):
    """Operational state of one pump."""

    STOPPED = "stopped"
    RUNNING = "running"
    FAILED = "failed"


@dataclass
class Pump:
    """One drain pump attached to a sump pit.

    ``protection_until`` is an epoch timestamp: while it lies in the
    future the pump must not be selected (cool-down after a stop, or
    back-off after a failed start).
    """

    id: str
    pit_id: str
    rated_kw: float
    health: Health = Health.HEALTHY
    state: PumpState = PumpState.STOPPED
    protection_until: float = 0.0
    started_ts: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "pit_id": self.pit_id,
            "rated_kw": self.rated_kw,
            "health": self.health.value,
            "state": self.state.value,
            "protection_until": self.protection_until,
            "started_ts": self.started_ts,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Pump":
        started = payload.get("started_ts")
        return cls(
            id=str(payload.get("id", "")),
            pit_id=str(payload.get("pit_id", "")),
            rated_kw=float(payload.get("rated_kw", 0.0)),
            health=Health(str(payload.get("health", Health.HEALTHY.value))),
            state=PumpState(str(payload.get("state", PumpState.STOPPED.value))),
            protection_until=float(payload.get("protection_until", 0.0)),
            started_ts=None if started is None else float(started),
        )


@dataclass
class Pit:
    """One sump pit: current water level plus scheduling priority."""

    id: str
    priority: int = 0
    water_level: float = 0.0


@dataclass(frozen=True)
class SchedulePolicy:
    """Tunables that shape one scheduling round.

    Water level maps to a desired pump count: one pump at
    ``start_level``, one more per extra ``level_band``, and every pump
    off below ``stop_level``. Between ``stop_level`` and
    ``start_level`` the round holds the current count, a hysteresis
    band that keeps pumps from chattering.
    """

    start_level: float = 2.0
    level_band: float = 0.5
    stop_level: float = 1.0
    emergency_level: float = 4.0
    min_on_seconds: float = 300.0
    min_off_seconds: float = 600.0
    failure_backoff_seconds: float = 900.0
    start_stagger_seconds: float = 5.0
    emergency_stagger_seconds: float = 1.0
    supply_limit_kw: float | None = None
