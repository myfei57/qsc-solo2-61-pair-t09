"""Run-time ledger: the single source of truth for pump run hours.

Pump profiles and energy statistics both derive from these segments,
so the two can never quote different run-time figures. Nothing else
in the system may keep its own run-time counter.
"""

from __future__ import annotations

from dataclasses import dataclass

from tunnelnet.store import Store

KIND = "runseg"


@dataclass(frozen=True)
class Segment:
    """One run interval; ``stopped_ts`` is ``None`` while the pump runs."""

    id: str
    pump_id: str
    started_ts: float
    stopped_ts: float | None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "pump_id": self.pump_id,
            "started_ts": self.started_ts,
            "stopped_ts": self.stopped_ts,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Segment":
        stopped = payload.get("stopped_ts")
        return cls(
            id=str(payload.get("id", "")),
            pump_id=str(payload.get("pump_id", "")),
            started_ts=float(payload.get("started_ts", 0.0)),
            stopped_ts=None if stopped is None else float(stopped),
        )


def _segment_id(pump_id: str, started_ts: float) -> str:
    return f"{pump_id}:{started_ts:.3f}"


class RuntimeLedger:
    """Append-only record of pump run segments backed by the store."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def record_start(self, pump_id: str, ts: float) -> Segment | None:
        """Open a segment, ignoring the call when one is already open."""

        if self.open_segment(pump_id) is not None:
            return None
        segment = Segment(
            id=_segment_id(pump_id, ts), pump_id=pump_id, started_ts=ts, stopped_ts=None
        )
        self._store.save(KIND, segment.id, segment.as_dict())
        return segment

    def record_stop(self, pump_id: str, ts: float) -> Segment | None:
        """Close the open segment, ignoring the call when there is none."""

        segment = self.open_segment(pump_id)
        if segment is None:
            return None
        closed = Segment(
            id=segment.id, pump_id=pump_id, started_ts=segment.started_ts, stopped_ts=ts
        )
        self._store.save(KIND, closed.id, closed.as_dict())
        return closed

    def open_segment(self, pump_id: str) -> Segment | None:
        for segment in self.segments(pump_id):
            if segment.stopped_ts is None:
                return segment
        return None

    def segments(self, pump_id: str | None = None) -> list[Segment]:
        found = []
        for record_id in self._store.list(KIND):
            payload = self._store.load(KIND, record_id)
            if payload is None:
                continue
            segment = Segment.from_dict(payload)
            if pump_id is None or segment.pump_id == pump_id:
                found.append(segment)
        return sorted(found, key=lambda segment: (segment.started_ts, segment.id))

    def runtime_seconds(self, pump_id: str, as_of: float) -> float:
        """Total run time up to ``as_of``, including any open segment."""

        total = 0.0
        for segment in self.segments(pump_id):
            if segment.started_ts >= as_of:
                continue
            end = segment.stopped_ts if segment.stopped_ts is not None else as_of
            total += min(end, as_of) - segment.started_ts
        return total

    def runtime_between(self, pump_id: str, since: float, until: float) -> float:
        """Run time inside the window ``[since, until]``."""

        total = 0.0
        for segment in self.segments(pump_id):
            end = segment.stopped_ts if segment.stopped_ts is not None else until
            overlap = min(end, until) - max(segment.started_ts, since)
            if overlap > 0:
                total += overlap
        return total

    def start_count(self, pump_id: str) -> int:
        return len(self.segments(pump_id))
