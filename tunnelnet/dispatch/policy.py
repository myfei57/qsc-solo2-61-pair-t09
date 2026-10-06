"""Scheduling policy: how many pumps, who may run, and in what order.

Decisions made here (the two choices required by the spec):

* 高水位抢排 **逐台加泵 (staggered)**, never "all pumps at once": start steps
  carry ``not_before`` timestamps and the rig starts at most one pump per
  stagger window, so motor inrush never stacks under a tight power budget.
* 保护期刚结束的泵 **排到下一轮**: eligibility is snapshotted when the plan is
  built and frozen into its ordered steps; a pump becoming ready mid-plan is
  not injected into the running plan -- it enters the next plan built.
* 轮换 / 补泵优先选 **累计运行时长短** 的健康泵; 水落停机优先停累计时长长的,
  让各泵的累计时长差距收敛。
"""

from __future__ import annotations

from tunnelnet.store import Store

from . import ledger
from .model import (
    ACTION_START,
    ACTION_STOP,
    FAILED,
    HEALTH_ORDER,
    PLAN_SUPERSEDED,
    STEP_PENDING,
    STEP_RESERVE,
    STEP_STARTED,
    STEP_STARTING,
    STEP_STOPPING,
    STEP_SUPERSEDED,
    PlanStep,
    PumpProfile,
    PitConfig,
    RunSession,
    SchedulePlan,
)

# Steps that still physically reserve a pump for a plan.  STARTED steps are
# included even when their plan was later superseded, because the motor is
# physically running until a newer plan stops it.
OPEN_STEP_STATES = {
    STEP_PENDING,
    STEP_RESERVE,
    STEP_STARTING,
    STEP_STARTED,
    STEP_STOPPING,
}


def desired_pumps(pit: PitConfig, level: float, running: int, capacity: int) -> int:
    """Target pump count from the level bands, with rising/falling hysteresis.

    ``capacity`` is the number of healthy, in-service pumps that can serve the
    pit right now.  At/above ``high_level`` every usable pump is demanded.
    """

    if level >= pit.high_level:
        return max(running, capacity)
    rising = sum(1 for start in pit.start_levels if level >= start)
    falling = sum(1 for stop in pit.stop_levels if level >= stop)
    # Rising bands demand pumps even with none running; falling bands only keep
    # pumps already latched above their own stop level, hence the running clamp.
    target = max(rising, min(running, falling))
    # Usable pool caps the demand; the service counts in-service running pumps
    # into capacity so this never forces a healthy running pump off.
    return min(target, capacity)


def ready_at(store: Store, pump_id: str) -> float:
    """Earliest start timestamp after the last shutdown + anti-short-cycle cooldown."""

    latest = 0.0
    for session in ledger.pump_sessions(store, pump_id):
        if session.ended_at is not None and not session.failed:
            latest = max(latest, session.ended_at)
    profile = ledger.load_pump(store, pump_id)
    return latest + (profile.cooldown_seconds if profile else 0.0)


def eligible_pumps(
    store: Store, pumps: list[PumpProfile], pit_id: str, now: float
) -> list[tuple[PumpProfile, float]]:
    """``(pump, cumulative_seconds)`` for pumps that may be selected now.

    Excluded: failed pumps, pumps that cannot serve the pit, and pumps still
    inside their protection cooldown (保护期间不许被选中).
    """

    result = []
    for pump in pumps:
        if pump.health == FAILED:
            continue
        if pump.serves and pit_id not in pump.serves:
            continue
        if ready_at(store, pump.id) > now:
            continue
        result.append((pump, ledger.cumulative_seconds(store, pump.id, now)))
    return result


def rank_candidates(
    store: Store, pumps: list[PumpProfile], pit_id: str, now: float
) -> list[tuple[PumpProfile, float]]:
    """健康优先, 健康相同累计时长短者先上; 编号兜底保证次序确定。"""

    rows = eligible_pumps(store, pumps, pit_id, now)
    rows.sort(key=lambda row: (HEALTH_ORDER[row[0].health], row[1], row[0].id))
    return rows


def busy_pumps(store: Store, ignore_plan: str = "") -> set[str]:
    """Pumps physically occupied.

    Running sessions always count.  On an active plan every open step reserves
    a pump; on a superseded plan only steps already touching hardware
    (STARTED/STARTING/STOPPING) do -- pending/reserve steps were released.
    """

    busy = {session.pump_id for session in ledger.open_sessions(store)}
    for plan in ledger.list_plans(store):
        if plan.id == ignore_plan:
            continue
        reserved = (
            {STEP_STARTED, STEP_STARTING, STEP_STOPPING}
            if plan.status == PLAN_SUPERSEDED
            else OPEN_STEP_STATES
        )
        for step in plan.steps:
            if step.state in reserved:
                busy.add(step.pump_id)
    return busy


def running_on(store: Store, pit_id: str) -> list[RunSession]:
    return [session for session in ledger.open_sessions(store) if session.pit_id == pit_id]


def severity(pit: PitConfig, level: float) -> float:
    """Cross-pit priority for the single global power budget."""

    return level + (10.0 if level >= pit.high_level else 0.0)


def supersede_active(store: Store, pit_id: str) -> None:
    """Close out the pit's prior plans.

    Un-started steps are released (superseded); started/in-flight steps are
    kept so the rig still owns the pumps physically attached to them.
    """

    for plan in ledger.active_plans(store, pit_id):
        for step in plan.steps:
            if step.state in {STEP_PENDING, STEP_RESERVE}:
                step.state = STEP_SUPERSEDED
        plan.status = PLAN_SUPERSEDED
        plan.note = (plan.note + " superseded by newer request").strip()
        ledger.save_plan(store, plan)


def _start_step(seq: int, pump: PumpProfile, seconds: float, not_before: float, rank: int) -> PlanStep:
    return PlanStep(
        seq=seq,
        action=ACTION_START,
        pump_id=pump.id,
        not_before=not_before,
        rank=rank,
        runtime_seconds=seconds,
        watts=pump.watts,
        inrush_watts=pump.inrush_watts,
    )


def _stop_step(seq: int, pump_id: str, seconds: float, after_seq: int | None = None) -> PlanStep:
    return PlanStep(
        seq=seq,
        action=ACTION_STOP,
        pump_id=pump_id,
        runtime_seconds=seconds,
        after_seq=after_seq,
    )


def build_level_plan(
    store: Store,
    plan: SchedulePlan,
    pit: PitConfig,
    pumps: list[PumpProfile],
    now: float,
    stagger_seconds: float,
) -> SchedulePlan:
    """Populate a level plan with ordered stop/start steps (high-water staged)."""

    supersede_active(store, pit.id)
    sessions = running_on(store, pit.id)
    running_ids = [session.pump_id for session in sessions]
    usable = rank_candidates(store, pumps, pit.id, now)
    # Pumps already serving the pit count as capacity even mid-cooldown, so a
    # high-water demand while cooldown is active never under-counts them.
    capacity = len({pump.id for pump, _ in usable} | set(running_ids))
    plan.desired = desired_pumps(pit, plan.level, len(running_ids), capacity)
    plan.running_before = len(running_ids)
    plan.severity = severity(pit, plan.level)

    busy = busy_pumps(store)
    steps: list[PlanStep] = []
    seq = 1

    if plan.desired < len(running_ids):
        # 水落了: 先停累计时长长的; 水位触发的停机是强制的, 不设最短运行时间。
        order = sorted(running_ids, key=lambda pid: ledger.cumulative_seconds(store, pid, now), reverse=True)
        for pump_id in order[: len(running_ids) - plan.desired]:
            steps.append(_stop_step(seq, pump_id, ledger.cumulative_seconds(store, pump_id, now)))
            seq += 1
    elif plan.desired > len(running_ids):
        deficit = plan.desired - len(running_ids)
        available = [(pump, s) for pump, s in usable if pump.id not in busy]
        for rank, (pump, seconds) in enumerate(available):
            step = _start_step(seq, pump, seconds, now + stagger_seconds * rank, rank)
            # 与缺口数量相等的冻结后备: 主选启动失败时按同一次序顶上 (部分失败)。
            if rank >= deficit:
                if rank >= 2 * deficit:
                    break
                step.state = STEP_RESERVE
            steps.append(step)
            seq += 1
        if len(available) < deficit:
            plan.note = f"only {len(available)} of {deficit} requested pumps available"

    plan.steps = steps
    if not steps and not plan.note:
        plan.note = "no change required"
    ledger.save_plan(store, plan)
    return plan


def build_rotation_plan(
    store: Store,
    plan: SchedulePlan,
    pit: PitConfig,
    pumps: list[PumpProfile],
    now: float,
    min_gap_seconds: float = 0.0,
) -> SchedulePlan:
    """Swap the longest-running pump for the shortest-runtime healthy idle pump.

    Order is start-new *then* stop-old (linked via ``after_seq``) so the pit
    never loses drainage capacity during the swap.
    """

    supersede_active(store, pit.id)
    sessions = running_on(store, pit.id)
    plan.running_before = len(sessions)
    plan.desired = len(sessions)
    plan.severity = severity(pit, plan.level)

    steps: list[PlanStep] = []
    if not sessions:
        plan.note = "rotation skipped: no running pump"
    else:
        busy = busy_pumps(store)
        candidates = [
            (pump, seconds)
            for pump, seconds in rank_candidates(store, pumps, pit.id, now)
            if pump.id not in busy
        ]
        incumbent = max(
            sessions, key=lambda session: ledger.cumulative_seconds(store, session.pump_id, now)
        )
        incumbent_seconds = ledger.cumulative_seconds(store, incumbent.pump_id, now)
        if candidates:
            fresh, fresh_seconds = candidates[0]
            if incumbent_seconds - fresh_seconds >= min_gap_seconds:
                steps.append(_start_step(1, fresh, fresh_seconds, now, 0))
                steps.append(
                    _stop_step(2, incumbent.pump_id, incumbent_seconds, after_seq=1)
                )
            else:
                plan.note = "rotation skipped: runtime gap below threshold"
        else:
            plan.note = "rotation skipped: no eligible idle pump"
    plan.steps = steps
    ledger.save_plan(store, plan)
    return plan
