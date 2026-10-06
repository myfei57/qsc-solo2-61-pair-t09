"""Persistent, append-only audit trail for dispatch decisions.

Records live under ``audit/<seq>-<event>.json`` so store listing returns them
in write order.  Each entry carries the SHA-256 link of the previous entry, so
a missing or modified record breaks the chain and is reported by
:func:`verify_chain`.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass

from tunnelnet.store import Store

GENESIS = "0" * 64


@dataclass(frozen=True)
class AuditEvent:
    """One audit entry; detail is arbitrary JSON-serialisable context."""

    seq: int
    id: str
    event: str
    at: float
    stamp: str
    request_id: str
    pit_id: str
    prev_hash: str
    payload_hash: str
    detail: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def payload_hash(event: str, at: float, detail: dict[str, object]) -> str:
    body = json.dumps(
        {"event": event, "at": round(at, 6), "detail": detail},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def link(seq: int, event_id: str, prev_hash: str, payload: str) -> str:
    return hashlib.sha256(f"{seq}|{event_id}|{prev_hash}|{payload}".encode("utf-8")).hexdigest()


class EventLog:
    """Durable hash-chained audit log."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = threading.RLock()

    def events(self) -> list[AuditEvent]:
        with self._lock:
            result = []
            for record in self._store.list("audit"):
                value = self._store.load("audit", record)
                if value is not None:
                    result.append(AuditEvent(**value))  # type: ignore[arg-type]
            result.sort(key=lambda item: item.seq)
            return result

    def count(self) -> int:
        with self._lock:
            return len(self._store.list("audit"))

    def latest(self) -> AuditEvent | None:
        events = self.events()
        return events[-1] if events else None

    def append(
        self,
        event: str,
        at: float,
        stamp: str,
        detail: dict[str, object],
        request_id: str = "",
        pit_id: str = "",
    ) -> AuditEvent:
        with self._lock:
            events = self.events()
            seq = len(events) + 1
            event_id = f"{seq:06d}-{event}"
            previous = (
                link(events[-1].seq, events[-1].id, events[-1].prev_hash, events[-1].payload_hash)
                if events
                else GENESIS
            )
            payload = payload_hash(event, at, detail)
            entry = AuditEvent(
                seq=seq,
                id=event_id,
                event=event,
                at=at,
                stamp=stamp,
                request_id=request_id,
                pit_id=pit_id,
                prev_hash=previous,
                payload_hash=payload,
                detail=detail,
            )
            self._store.save("audit", f"{seq:06d}-{event}", entry.as_dict())
            return entry

    def verify_chain(self) -> tuple[bool, str]:
        """Return (ok, message); a break reports the first offending entry."""

        with self._lock:
            previous = GENESIS
            expected_seq = 1
            for entry in self.events():
                if entry.seq != expected_seq:
                    return False, f"gap before seq {entry.seq}"
                if payload_hash(entry.event, entry.at, entry.detail) != entry.payload_hash:
                    return False, f"payload tampered at {entry.id}"
                if entry.prev_hash != previous:
                    return False, f"chain broken at {entry.id}"
                previous = link(entry.seq, entry.id, previous, entry.payload_hash)
                expected_seq += 1
            return True, f"{expected_seq - 1} entries verified"
