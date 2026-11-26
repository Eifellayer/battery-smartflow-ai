"""Decide when group members must stop before power moves between them."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

_EPSILON_W = 0.5


def _direction(power_w: float) -> str | None:
    if power_w > _EPSILON_W:
        return "charge"
    if power_w < -_EPSILON_W:
        return "discharge"
    return None


def handover_barrier_devices(
    previous: Mapping[str, Any],
    signed_targets: Mapping[str, float],
    measured_flow: Callable[[str], tuple[str | None, float, Any]],
    *,
    tolerance_w: float,
) -> set[str]:
    """Return the members that must reach 0 W before others may rise.

    The barrier keeps one member from charging out of another while power
    changes direction. Members that share a load in the same direction only
    rebalance: stopping one of them would drop the whole group output each
    time a few watts move between them.
    """

    reductions: set[str] = set()
    increases: set[str] = set()
    directions: set[str] = set()
    for device_id, target_value in signed_targets.items():
        old_item = previous.get(device_id)
        old_value = float(old_item.get("power_w", 0.0)) if isinstance(old_item, Mapping) else 0.0
        if isinstance(old_item, Mapping) and (
            old_value * target_value < -_EPSILON_W
            or abs(target_value) + _EPSILON_W < abs(old_value)
        ):
            reductions.add(device_id)
        if abs(target_value) > abs(old_value) + _EPSILON_W or old_value * target_value < -_EPSILON_W:
            increases.add(device_id)
        observed_direction, observed_w, _observed_at = measured_flow(device_id)
        target_direction = _direction(target_value)
        # A lagging same-direction measurement is not a new handover: an
        # increasing setpoint on this device must not be interrupted by a
        # zero command.
        if observed_direction is not None and (
            observed_direction != target_direction
            or (device_id not in increases and observed_w > abs(target_value) + tolerance_w)
        ):
            reductions.add(device_id)
        directions.update(
            item for item in (_direction(old_value), target_direction, observed_direction)
            if item is not None
        )
    if not (reductions and increases):
        return set()
    if not {"charge", "discharge"} <= directions:
        return set()
    return reductions
