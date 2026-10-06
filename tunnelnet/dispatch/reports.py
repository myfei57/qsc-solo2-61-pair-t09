"""Read-only reports.

泵档案里的累计时长、轮换排序、用电统计引用同一份运行时长口径: 全部数字都由
``run_session`` 台账即时计算 (``ledger.cumulative_seconds`` /
``ledger.energy_wh``), 没有冗余计数器, 因此不可能对不上账。
"""

from __future__ import annotations

from tunnelnet.store import Store

from . import ledger


def pump_power_report(store: Store, now: float) -> list[dict[str, object]]:
    """Per-pump runtime/energy rows ordered by pump id."""

    rows = []
    for pump in ledger.list_pumps(store):
        sessions = ledger.pump_sessions(store, pump.id)
        rows.append(
            {
                "pump_id": pump.id,
                "health": pump.health,
                "runtime_seconds": ledger.cumulative_seconds(store, pump.id, now),
                "energy_wh": ledger.energy_wh(store, pump.id, now),
                "completed_runs": sum(1 for session in sessions if session.ended_at is not None),
                "failed_runs": sum(1 for session in sessions if session.failed),
                "avg_watts_while_run": (
                    ledger.energy_wh(store, pump.id, now) * 3600.0
                    / ledger.cumulative_seconds(store, pump.id, now)
                    if ledger.cumulative_seconds(store, pump.id, now) > 0
                    else 0.0
                ),
            }
        )
    return sorted(rows, key=lambda row: str(row["pump_id"]))


def site_power_report(store: Store, now: float) -> dict[str, object]:
    """Site totals plus the current live draw (open sessions only)."""

    live_watts = sum(session.watts for session in ledger.open_sessions(store))
    return {
        "pumps": len(ledger.list_pumps(store)),
        "running": len(ledger.open_sessions(store)),
        "live_watts": live_watts,
        "total_runtime_seconds": sum(
            ledger.cumulative_seconds(store, pump.id, now) for pump in ledger.list_pumps(store)
        ),
        "total_energy_wh": ledger.energy_wh(store, now=now),
    }


def pump_card(store: Store, pump_id: str, now: float) -> dict[str, object]:
    """The single card a pump档案 UI and the billing UI both quote."""

    pump = ledger.load_pump(store, pump_id)
    if pump is None:
        raise KeyError(f"unknown pump {pump_id!r}")
    return {
        "pump_id": pump.id,
        "watts": pump.watts,
        "inrush_watts": pump.inrush_watts,
        "cooldown_seconds": pump.cooldown_seconds,
        "health": pump.health,
        "serves": list(pump.serves),
        "runtime_seconds": ledger.cumulative_seconds(store, pump_id, now),
        "energy_wh": ledger.energy_wh(store, pump_id, now),
    }
