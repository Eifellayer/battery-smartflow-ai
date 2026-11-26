"""Known consumers that the economic plan adds on top of the base load.

Two kinds are supported:

* A consumer with a remaining-time or end-time sensor (a 3D printer) keeps
  its present power until it finishes. Afterwards it counts as 0 W.
* A consumer without such a sensor (an aquarium, a grow box) counts with its
  running average power, so on/off cycles of a light or a heater are
  included roughly. The average follows the measured power with a time
  constant of one day.

The base load setting should then cover everything else. Spontaneous
consumers are deliberately not forecast.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any

MAX_PLANNED_LOADS = 6
AVERAGE_TIME_CONSTANT_S = 24 * 3600.0
# A running consumer must draw more than this to be held until its end time.
ACTIVE_THRESHOLD_W = 15.0
MAX_CONSUMER_W = 10000.0
MAX_REMAINING = timedelta(days=7)

KIND_UNTIL_END = "until_end"
KIND_AVERAGE = "average"


@dataclass(frozen=True)
class PlannedLoad:
    """One configured consumer."""

    power_entity: str
    end_entity: str | None = None


@dataclass(frozen=True)
class PlannedLoadInterval:
    """Expected extra power of one consumer between two times."""

    power_entity: str
    kind: str
    start: datetime
    end: datetime | None
    power_w: float

    def energy_kwh(self, start: datetime, end: datetime) -> float:
        """Energy of this consumer inside ``[start, end)``."""

        begin = max(start, self.start)
        finish = end if self.end is None else min(end, self.end)
        if finish <= begin:
            return 0.0
        return self.power_w * (finish - begin).total_seconds() / 3_600_000


def power_field(index: int) -> str:
    return f"planned_load_{index}_power"


def end_field(index: int) -> str:
    return f"planned_load_{index}_end"


def parse_planned_loads(value: Any) -> tuple[PlannedLoad, ...]:
    """Read the stored list; skip broken or duplicate entries."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    loads: list[PlannedLoad] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        power = item.get("power_entity")
        if not isinstance(power, str) or not power.strip() or power in seen:
            continue
        end = item.get("end_entity")
        end = end.strip() if isinstance(end, str) and end.strip() else None
        seen.add(power)
        loads.append(PlannedLoad(power.strip(), end))
        if len(loads) >= MAX_PLANNED_LOADS:
            break
    return tuple(loads)


def planned_loads_from_form(user_input: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Turn the options form back into the stored list."""

    loads: list[dict[str, Any]] = []
    for index in range(1, MAX_PLANNED_LOADS + 1):
        power = user_input.get(power_field(index))
        if not isinstance(power, str) or not power:
            continue
        end = user_input.get(end_field(index))
        loads.append({
            "power_entity": power,
            "end_entity": end if isinstance(end, str) and end else None,
        })
    return [
        {"power_entity": load.power_entity, "end_entity": load.end_entity}
        for load in parse_planned_loads(loads)
    ]


def parse_power_w(state: Any) -> float | None:
    """Return a plausible power reading in W, or None."""

    if state is None:
        return None
    try:
        value = float(getattr(state, "state", state))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    unit = str(getattr(state, "attributes", {}).get("unit_of_measurement", "W") or "W")
    if unit.lower() == "kw":
        value *= 1000
    if value < 0 or value > MAX_CONSUMER_W:
        return None
    return value


def parse_end_time(state: Any, now: datetime) -> datetime | None:
    """Read a remaining duration or an end timestamp.

    Returns ``now`` when the consumer is finished and None when unknown.
    """

    if state is None:
        return None
    raw = getattr(state, "state", None)
    attributes = getattr(state, "attributes", {}) or {}
    if raw in (None, "", "unknown", "unavailable"):
        return None
    if attributes.get("device_class") == "timestamp":
        try:
            end = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if end - now > MAX_REMAINING:
            return None
        return max(now, end)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    unit = str(attributes.get("unit_of_measurement", "min") or "min").lower()
    seconds = {"s": 1, "min": 60, "h": 3600, "d": 86400}.get(unit)
    if seconds is None:
        return None
    remaining = timedelta(seconds=value * seconds)
    if remaining > MAX_REMAINING:
        return None
    return now + remaining


def update_average(
    previous: Mapping[str, Any] | None, *, now: datetime, power_w: float,
) -> dict[str, Any]:
    """Exponential average with a one-day time constant, persisted as ISO."""

    if not isinstance(previous, Mapping):
        return {"w": power_w, "at": now.isoformat()}
    try:
        last_w = float(previous["w"])
        last_at = datetime.fromisoformat(str(previous["at"]))
    except (KeyError, TypeError, ValueError):
        return {"w": power_w, "at": now.isoformat()}
    if not math.isfinite(last_w) or last_at.tzinfo is None:
        return {"w": power_w, "at": now.isoformat()}
    elapsed = (now - last_at).total_seconds()
    if elapsed <= 0:
        return {"w": last_w, "at": last_at.isoformat()}
    weight = 1 - math.exp(-elapsed / AVERAGE_TIME_CONSTANT_S)
    return {"w": last_w + (power_w - last_w) * weight, "at": now.isoformat()}


def planned_load_intervals(
    loads: Iterable[PlannedLoad],
    *,
    now: datetime,
    states: Mapping[str, Any],
    averages: Mapping[str, Any],
    include_average_loads: bool,
) -> tuple[PlannedLoadInterval, ...]:
    """Expected extra load per consumer from now on."""

    intervals: list[PlannedLoadInterval] = []
    for load in loads:
        power_w = parse_power_w(states.get(load.power_entity))
        if load.end_entity is not None:
            end = parse_end_time(states.get(load.end_entity), now)
            if power_w is None or end is None or end <= now or power_w < ACTIVE_THRESHOLD_W:
                continue
            intervals.append(PlannedLoadInterval(load.power_entity, KIND_UNTIL_END, now, end, power_w))
            continue
        if not include_average_loads:
            continue
        average = averages.get(load.power_entity)
        try:
            average_w = float(average["w"]) if isinstance(average, Mapping) else None
        except (KeyError, TypeError, ValueError):
            average_w = None
        if average_w is None or not math.isfinite(average_w):
            average_w = power_w
        if average_w is None or average_w <= 0:
            continue
        intervals.append(PlannedLoadInterval(load.power_entity, KIND_AVERAGE, now, None, average_w))
    return tuple(intervals)


def planned_energy_kwh(
    intervals: Iterable[PlannedLoadInterval], start: datetime, end: datetime,
) -> float:
    return sum(interval.energy_kwh(start, end) for interval in intervals)


def describe_intervals(
    intervals: Iterable[PlannedLoadInterval], names: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Short attribute form for the plan."""

    return [
        {
            "name": names.get(interval.power_entity, interval.power_entity),
            "kind": interval.kind,
            "power_w": round(interval.power_w),
            "until": interval.end.isoformat() if interval.end is not None else None,
        }
        for interval in intervals
    ]
