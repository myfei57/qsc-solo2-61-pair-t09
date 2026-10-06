"""Read-only projections: pump profiles and energy statistics.

Every figure derives from the RuntimeLedger, so the pump profile and
the energy statistics always quote the same run-time numbers.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ledger import RuntimeLedger
from .model import Pump


@dataclass(frozen=True)
class PumpProfile:
    """One pump's identity plus its accumulated duty."""

    id: str
    pit_id: str
    health: str
    state: str
    rated_kw: float
    runtime_hours: float
    starts: int
    energy_kwh: float

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "pit_id": self.pit_id,
            "health": self.health,
            "state": self.state,
            "rated_kw": self.rated_kw,
            "runtime_hours": self.runtime_hours,
            "starts": self.starts,
            "energy_kwh": self.energy_kwh,
        }


def pump_profile(pump: Pump, ledger: RuntimeLedger, as_of: float) -> PumpProfile:
    runtime_s = ledger.runtime_seconds(pump.id, as_of)
    return PumpProfile(
        id=pump.id,
        pit_id=pump.pit_id,
        health=pump.health.value,
        state=pump.state.value,
        rated_kw=pump.rated_kw,
        runtime_hours=runtime_s / 3600.0,
        starts=ledger.start_count(pump.id),
        energy_kwh=runtime_s * pump.rated_kw / 3600.0,
    )


def energy_kwh(pump: Pump, ledger: RuntimeLedger, since: float, until: float) -> float:
    """Energy drawn in a window, assuming rated power while running."""

    return ledger.runtime_between(pump.id, since, until) * pump.rated_kw / 3600.0


def pit_energy_kwh(
    pit_id: str, pumps: list[Pump], ledger: RuntimeLedger, since: float, until: float
) -> float:
    return sum(energy_kwh(pump, ledger, since, until) for pump in pumps if pump.pit_id == pit_id)
