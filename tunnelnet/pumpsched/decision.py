"""Durable decision and command records written by the scheduler."""

from __future__ import annotations

from dataclasses import dataclass

from tunnelnet.store import Store

DECISION_KIND = "scheddecision"
COMMAND_KIND = "schedcommand"

PENDING = "pending"
OK = "ok"
FAILED = "failed"
RECONCILED = "reconciled"


@dataclass(frozen=True)
class Command:
    """One pump command dispatched by a scheduling decision."""

    id: str
    decision_id: str
    pump_id: str
    action: str
    status: str
    error: str
    updated_ts: float

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "decision_id": self.decision_id,
            "pump_id": self.pump_id,
            "action": self.action,
            "status": self.status,
            "error": self.error,
            "updated_ts": self.updated_ts,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Command":
        return cls(
            id=str(payload.get("id", "")),
            decision_id=str(payload.get("decision_id", "")),
            pump_id=str(payload.get("pump_id", "")),
            action=str(payload.get("action", "")),
            status=str(payload.get("status", "")),
            error=str(payload.get("error", "")),
            updated_ts=float(payload.get("updated_ts", 0.0)),
        )


def save_command(store: Store, command: Command) -> None:
    store.save(COMMAND_KIND, command.id, command.as_dict())


def list_commands(store: Store, decision_id: str | None = None) -> list[Command]:
    commands = []
    for record_id in store.list(COMMAND_KIND):
        payload = store.load(COMMAND_KIND, record_id)
        if payload is None:
            continue
        command = Command.from_dict(payload)
        if decision_id is None or command.decision_id == decision_id:
            commands.append(command)
    return sorted(commands, key=lambda command: command.id)


def pending_commands(store: Store) -> list[Command]:
    return [command for command in list_commands(store) if command.status == PENDING]


def save_decision(store: Store, decision: dict[str, object]) -> None:
    store.save(DECISION_KIND, str(decision["id"]), decision)


def load_decision(store: Store, decision_id: str) -> dict[str, object] | None:
    return store.load(DECISION_KIND, decision_id)


def list_decisions(store: Store) -> list[dict[str, object]]:
    found = []
    for record_id in store.list(DECISION_KIND):
        payload = store.load(DECISION_KIND, record_id)
        if payload is not None:
            found.append(payload)
    return sorted(found, key=lambda decision: str(decision.get("id", "")))
