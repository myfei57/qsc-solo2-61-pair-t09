"""Domain model for multi-pump drainage dispatch.

A pump is leased to at most one pit at a time.  A :class:`SchedulePlan` is the
durable result of one scheduling request and its ordered steps are the single
start/stop sequence executed by the rig.  Everything in this module is a plain
dataclass that round-trips through the JSON :class:`~tunnelnet.store.Store`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# --- pump health ---------------------------------------------------------
HEALTHY = "healthy"
DEGRADED = "degraded"
FAILED = "FAILED"
HEALTH_ORDER = {HEALTHY: 0, DEGRADED: 1, FAILED: 2}

# --- plan / step lifecycle ----------------------------------------------
PLAN_EXECUTING = "executing"
PLAN_COMPLETED = "completed"
PLAN_PARTIAL = "partial"
PLAN_BLOCKED = "blocked"
PLAN_SUPERSEDED = "superseded"
TERMINAL_PLAN_STATES = {PLAN_COMPLETED, PLAN_PARTIAL, PLAN_BLOCKED, PLAN_SUPERSEDED}

STEP_PENDING = "pending"      # primary, claimed, waiting its turn to start
STEP_RESERVE = "reserve"      # frozen backup, claimed only when promoted
STEP_STARTING = "starting"    # start command in flight / before confirmation
STEP_STARTED = "started"      # pump confirmed running
STEP_STOPPING = "stopping"    # stop command in flight
STEP_DONE = "done"
STEP_FAILED = "failed"        # hardware rejected the command, pump quarantined
STEP_SKIPPED = "skipped"      # pump unavailable when its turn came
STEP_SUPERSEDED = "superseded"

ACTION_START = "start"
ACTION_STOP = "stop"

KIND_LEVEL = "level"
KIND_ROTATE = "rotate"


@dataclass
class PumpProfile:
    """Static pump档案: power draw, anti-short-cycle cooldown and eligibility."""

    id: str
    watts: float
    serves: tuple[str, ...]
    inrush_watts: float = 0.0
    cooldown_seconds: float = 0.0
    health: str = HEALTHY

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("pump id is required")
        if self.watts < 0:
            raise ValueError("watts must be non-negative")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be non-negative")
        if self.health not in HEALTH_ORDER:
            raise ValueError(f"unknown health {self.health!r}")
        if self.inrush_watts <= 0:
            self.inrush_watts = self.watts * 1.8
        self.serves = tuple(self.serves)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "PumpProfile":
        return cls(
            id=str(value["id"]),
            watts=float(value["watts"]),
            serves=tuple(value.get("serves", ())),  # type: ignore[arg-type]
            inrush_watts=float(value.get("inrush_watts", 0.0)),
            cooldown_seconds=float(value.get("cooldown_seconds", 0.0)),
            health=str(value.get("health", HEALTHY)),
        )


@dataclass
class PitConfig:
    """Level bands for one pit.

    ``start_levels[i]`` adds the (i+1)-th pump while water is rising; water has
    to fall below ``stop_levels[i]`` (strictly lower) before that pump is taken
    off again, giving hysteresis.  At or above ``high_level`` every pump that
    can serve the pit is demanded (抢排).
    """

    id: str
    start_levels: tuple[float, ...]
    stop_levels: tuple[float, ...]
    high_level: float = 0.0
    cabin: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("pit id is required")
        if len(self.start_levels) != len(self.stop_levels) or not self.start_levels:
            raise ValueError("start_levels and stop_levels must be non-empty and equal length")
        starts = tuple(self.start_levels)
        stops = tuple(self.stop_levels)
        if any(b <= a for a, b in zip(starts, starts[1:])):
            raise ValueError("start_levels must be strictly ascending")
        if any(b <= a for a, b in zip(stops, stops[1:])):
            raise ValueError("stop_levels must be strictly ascending")
        if any(stop >= start for start, stop in zip(starts, stops)):
            raise ValueError("each stop level must sit below its start level")
        self.start_levels = starts
        self.stop_levels = stops
        if self.high_level <= 0:
            self.high_level = starts[-1]
        if self.high_level < starts[-1]:
            raise ValueError("high_level must cover the top start band")

    @property
    def max_band_pumps(self) -> int:
        return len(self.start_levels)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "PitConfig":
        return cls(
            id=str(value["id"]),
            start_levels=tuple(value["start_levels"]),  # type: ignore[arg-type]
            stop_levels=tuple(value["stop_levels"]),  # type: ignore[arg-type]
            high_level=float(value.get("high_level", 0.0)),
            cabin=str(value.get("cabin", "")),
        )


@dataclass
class PlanStep:
    """One ordered start/stop command in a schedule plan."""

    seq: int
    action: str
    pump_id: str
    state: str = STEP_PENDING
    not_before: float = 0.0
    rank: int = -1
    runtime_seconds: float = 0.0
    after_seq: int | None = None
    attempts: int = 0
    error: str = ""
    watts: float = 0.0
    inrush_watts: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "PlanStep":
        return cls(
            seq=int(value["seq"]),
            action=str(value["action"]),
            pump_id=str(value["pump_id"]),
            state=str(value.get("state", STEP_PENDING)),
            not_before=float(value.get("not_before", 0.0)),
            rank=int(value.get("rank", -1)),
            runtime_seconds=float(value.get("runtime_seconds", 0.0)),
            after_seq=value.get("after_seq"),
            attempts=int(value.get("attempts", 0)),
            error=str(value.get("error", "")),
            watts=float(value.get("watts", 0.0)),
            inrush_watts=float(value.get("inrush_watts", 0.0)),
        )

    @property
    def open(self) -> bool:
        return self.state in {STEP_PENDING, STEP_RESERVE, STEP_STARTING, STEP_STOPPING}


@dataclass
class SchedulePlan:
    """Durable scheduling result for one idempotent request."""

    id: str
    request_id: str
    pit_id: str
    kind: str
    level: float
    desired: int
    running_before: int
    severity: float
    status: str = PLAN_EXECUTING
    note: str = ""
    created_at: float = 0.0
    created_stamp: str = ""
    steps: list[PlanStep] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "request_id": self.request_id,
            "pit_id": self.pit_id,
            "kind": self.kind,
            "level": self.level,
            "desired": self.desired,
            "running_before": self.running_before,
            "severity": self.severity,
            "status": self.status,
            "note": self.note,
            "created_at": self.created_at,
            "created_stamp": self.created_stamp,
            "steps": [step.as_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "SchedulePlan":
        return cls(
            id=str(value["id"]),
            request_id=str(value["request_id"]),
            pit_id=str(value["pit_id"]),
            kind=str(value["kind"]),
            level=float(value["level"]),
            desired=int(value["desired"]),
            running_before=int(value["running_before"]),
            severity=float(value["severity"]),
            status=str(value.get("status", PLAN_EXECUTING)),
            note=str(value.get("note", "")),
            created_at=float(value.get("created_at", 0.0)),
            created_stamp=str(value.get("created_stamp", "")),
            steps=[PlanStep.from_dict(step) for step in value.get("steps", ())],  # type: ignore[arg-type]
        )

    def step(self, seq: int) -> PlanStep | None:
        for step in self.steps:
            if step.seq == seq:
                return step
        return None

    def has_failure(self) -> bool:
        return any(step.state in {STEP_FAILED, STEP_SKIPPED} for step in self.steps)


@dataclass
class RunSession:
    """One continuous pump run -- the only source of runtime/electricity truth."""

    id: str
    pump_id: str
    pit_id: str
    plan_id: str
    step_seq: int
    started_at: float
    started_stamp: str
    watts: float
    ended_at: float | None = None
    ended_stamp: str = ""
    seconds: float = 0.0
    energy_wh: float = 0.0
    failed: bool = False

    @property
    def open(self) -> bool:
        return self.ended_at is None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "RunSession":
        ended = value.get("ended_at")
        return cls(
            id=str(value["id"]),
            pump_id=str(value["pump_id"]),
            pit_id=str(value["pit_id"]),
            plan_id=str(value["plan_id"]),
            step_seq=int(value["step_seq"]),
            started_at=float(value["started_at"]),
            started_stamp=str(value.get("started_stamp", "")),
            watts=float(value["watts"]),
            ended_at=None if ended is None else float(ended),
            ended_stamp=str(value.get("ended_stamp", "")),
            seconds=float(value.get("seconds", 0.0)),
            energy_wh=float(value.get("energy_wh", 0.0)),
            failed=bool(value.get("failed", False)),
        )
