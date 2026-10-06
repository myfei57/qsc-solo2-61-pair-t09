"""Multi-pump drainage dispatch.

Level- and health-aware scheduling for several pits sharing a pump pool:

* ordered start/stop plans, persisted and fully audited;
* shortest-runtime-first rotation with anti-short-cycle protection cooldown;
* high-water staging: pumps are added one at a time within a stagger window so
  a tight power budget is never hit by stacked inrush (decision: staged, not
  all-at-once);
* cooldown expiry joins the next plan, never the running one (decision: next
  round);
* idempotent requests, partial-failure handling with frozen reserves, and
  crash reconciliation.
"""

from __future__ import annotations

from .audit import AuditEvent, EventLog, link, payload_hash
from .clock import utc_stamp
from .model import (
    ACTION_START,
    ACTION_STOP,
    HEALTHY,
    DEGRADED,
    FAILED,
    HEALTH_ORDER,
    KIND_LEVEL,
    KIND_ROTATE,
    PLAN_BLOCKED,
    PLAN_COMPLETED,
    PLAN_EXECUTING,
    PLAN_PARTIAL,
    PLAN_SUPERSEDED,
    STEP_DONE,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RESERVE,
    STEP_SKIPPED,
    STEP_STARTED,
    STEP_STARTING,
    STEP_STOPPING,
    STEP_SUPERSEDED,
    PitConfig,
    PlanStep,
    PumpProfile,
    RunSession,
    SchedulePlan,
)
from .policy import (
    busy_pumps,
    desired_pumps,
    eligible_pumps,
    rank_candidates,
    ready_at,
    running_on,
    severity,
)
from .reports import pump_card, pump_power_report, site_power_report
from .rig import PumpRig, SimRig
from .service import DispatchService

__all__ = [
    "ACTION_START",
    "ACTION_STOP",
    "AuditEvent",
    "DEGRADED",
    "DispatchService",
    "EventLog",
    "FAILED",
    "HEALTHY",
    "HEALTH_ORDER",
    "KIND_LEVEL",
    "KIND_ROTATE",
    "PLAN_BLOCKED",
    "PLAN_COMPLETED",
    "PLAN_EXECUTING",
    "PLAN_PARTIAL",
    "PLAN_SUPERSEDED",
    "PitConfig",
    "PlanStep",
    "PumpProfile",
    "PumpRig",
    "RunSession",
    "STEP_DONE",
    "STEP_FAILED",
    "STEP_PENDING",
    "STEP_RESERVE",
    "STEP_SKIPPED",
    "STEP_STARTED",
    "STEP_STARTING",
    "STEP_STOPPING",
    "STEP_SUPERSEDED",
    "SchedulePlan",
    "SimRig",
    "busy_pumps",
    "desired_pumps",
    "eligible_pumps",
    "link",
    "payload_hash",
    "pump_card",
    "pump_power_report",
    "rank_candidates",
    "ready_at",
    "running_on",
    "severity",
    "site_power_report",
    "utc_stamp",
]
