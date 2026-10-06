"""Persistence ledger over the JSON :class:`~tunnelnet.store.Store`.

Kinds kept on disk::

    dispatch-pump/<id>      PumpProfile  (泵档案)
    dispatch-pit/<id>       PitConfig
    dispatch-plan/<id>      SchedulePlan (调度结果落盘)
    dispatch-session/<id>   RunSession   (运行台账: 时长/电量唯一口径)
    dispatch-state/<key>    counters and the last global pump start time

Run sessions are the single source of truth: pump档案中的运行时长、轮换排序和
用电统计全部经过 :func:`cumulative_seconds` / :func:`energy_wh` 计算，没有第二
份计数器。
"""

from __future__ import annotations

from tunnelnet.store import Store

from .model import PumpProfile, PitConfig, RunSession, SchedulePlan

PUMP_KIND = "dispatch-pump"
PIT_KIND = "dispatch-pit"
PLAN_KIND = "dispatch-plan"
SESSION_KIND = "dispatch-session"
STATE_KIND = "dispatch-state"


# --- pump档案 -------------------------------------------------------------
def save_pump(store: Store, pump: PumpProfile) -> None:
    store.save(PUMP_KIND, pump.id, pump.as_dict())


def load_pump(store: Store, pump_id: str) -> PumpProfile | None:
    value = store.load(PUMP_KIND, pump_id)
    return None if value is None else PumpProfile.from_dict(value)


def list_pumps(store: Store) -> list[PumpProfile]:
    return [pump for record in store.list(PUMP_KIND) if (pump := load_pump(store, record))]


# --- pit config -----------------------------------------------------------
def save_pit(store: Store, pit: PitConfig) -> None:
    store.save(PIT_KIND, pit.id, pit.as_dict())


def load_pit(store: Store, pit_id: str) -> PitConfig | None:
    value = store.load(PIT_KIND, pit_id)
    return None if value is None else PitConfig.from_dict(value)


def list_pits(store: Store) -> list[PitConfig]:
    return [pit for record in store.list(PIT_KIND) if (pit := load_pit(store, record))]


# --- plans ----------------------------------------------------------------
def save_plan(store: Store, plan: SchedulePlan) -> None:
    store.save(PLAN_KIND, plan.id, plan.as_dict())


def load_plan(store: Store, plan_id: str) -> SchedulePlan | None:
    value = store.load(PLAN_KIND, plan_id)
    return None if value is None else SchedulePlan.from_dict(value)


def list_plans(store: Store) -> list[SchedulePlan]:
    return [plan for record in store.list(PLAN_KIND) if (plan := load_plan(store, record))]


def active_plans(store: Store, pit_id: str | None = None) -> list[SchedulePlan]:
    from .model import TERMINAL_PLAN_STATES

    plans = [
        plan
        for plan in list_plans(store)
        if plan.status not in TERMINAL_PLAN_STATES and (pit_id is None or plan.pit_id == pit_id)
    ]
    return sorted(plans, key=lambda item: item.created_at)


# --- run sessions (the single runtime/energy ledger) ----------------------
def save_session(store: Store, session: RunSession) -> None:
    store.save(SESSION_KIND, session.id, session.as_dict())


def load_session(store: Store, session_id: str) -> RunSession | None:
    value = store.load(SESSION_KIND, session_id)
    return None if value is None else RunSession.from_dict(value)


def list_sessions(store: Store) -> list[RunSession]:
    return [
        session
        for record in store.list(SESSION_KIND)
        if (session := load_session(store, record))
    ]


def open_sessions(store: Store) -> list[RunSession]:
    return [session for session in list_sessions(store) if session.open]


def open_session_for(store: Store, plan_id: str, step_seq: int) -> RunSession | None:
    for session in open_sessions(store):
        if session.plan_id == plan_id and session.step_seq == step_seq:
            return session
    return None


def pump_sessions(store: Store, pump_id: str) -> list[RunSession]:
    return [session for session in list_sessions(store) if session.pump_id == pump_id]


def cumulative_seconds(store: Store, pump_id: str, now: float) -> float:
    """累计运行时长 (秒), 包含尚未关闭的运行段。唯一口径。"""

    total = 0.0
    for session in pump_sessions(store, pump_id):
        if session.open:
            total += max(0.0, now - session.started_at)
        else:
            total += session.seconds
    return total


def energy_wh(store: Store, pump_id: str | None = None, now: float | None = None) -> float:
    """累计用电 (Wh)。已停机段取台账能量, 运行段按当前时刻推算。"""

    total = 0.0
    for session in list_sessions(store):
        if pump_id is not None and session.pump_id != pump_id:
            continue
        if session.open:
            if now is None:
                continue
            total += session.watts * max(0.0, now - session.started_at) / 3600.0
        else:
            total += session.energy_wh
    return total


def close_session(store: Store, session: RunSession, at: float, stamp: str, failed: bool = False) -> RunSession:
    """Close an open session: seconds and energy are written in one atomic save."""

    if session.open:
        session.ended_at = at
        session.ended_stamp = stamp
        session.seconds = max(0.0, at - session.started_at)
        session.energy_wh = session.watts * session.seconds / 3600.0
    session.failed = failed or session.failed
    save_session(store, session)
    return session


# --- small key/value state ------------------------------------------------
def get_state(store: Store, key: str, default: object) -> object:
    value = store.load(STATE_KIND, key)
    return default if value is None else value.get("value", default)


def set_state(store: Store, key: str, value: object) -> None:
    store.save(STATE_KIND, key, {"key": key, "value": value})
