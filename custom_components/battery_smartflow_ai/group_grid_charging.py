"""Per-system price thresholds for automatic AC purchases in a group."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from .const import CONF_NATIVE_ZENDURE_DEVICE_SETUPS
from .native_device_setup import native_device_profile_key

MEMBER_GRID_CHARGE_SPREADS = "member_grid_charge_spreads"
MAX_GRID_CHARGE_SPREAD_PCT = 100
SF800_PLUS_SUMMER_PV_BUFFER_PCT = 10.0


def member_grid_charge_policy(options, members, default_spread_pct):
    """Return (gross price spread, AC efficiency, extra summer PV buffer) by device."""

    saved = options.get(MEMBER_GRID_CHARGE_SPREADS)
    saved = saved if isinstance(saved, Mapping) else {}
    setups = options.get(CONF_NATIVE_ZENDURE_DEVICE_SETUPS)
    profiles = {key: native_device_profile_key(setups, key) for key in members}
    if "SF800Plus" not in profiles.values() and not saved.keys() & profiles.keys():
        return {}

    policies = {}
    for key, profile in profiles.items():
        default = 20.0 if profile == "SF800Plus" else default_spread_pct
        try:
            spread = float(saved.get(key, default))
        except (TypeError, ValueError):
            spread = default
        if not math.isfinite(spread):
            spread = default
        efficiency = {"SF800Plus": 0.87, "Hyper 2000": 0.79}.get(profile, 0.81)
        policies[key] = (
            min(MAX_GRID_CHARGE_SPREAD_PCT, max(0.0, spread)),
            efficiency,
            SF800_PLUS_SUMMER_PV_BUFFER_PCT if profile == "SF800Plus" else 0.0,
        )
    return policies


def minimum_future_price(price: float, spread_pct: float, efficiency: float) -> float:
    """Apply the gross spread once and never buy below physical break-even."""

    if price <= 0:
        return 0.0
    return max(price * (1 + spread_pct / 100), price / efficiency)


def eligible_grid_charge_members(
    policies: Mapping, price: float | None, future_prices: Sequence[float], *, winter: bool,
) -> tuple[str, ...]:
    """Allow classic price charging only where no summer PV budget is required."""

    peak = max((value for value in future_prices if math.isfinite(value)), default=0.0)
    if price is None or not math.isfinite(price) or peak <= 0 or peak <= price:
        return ()
    return tuple(
        key for key, (spread, efficiency, summer_pv_buffer_pct) in policies.items()
        if (winter or not summer_pv_buffer_pct)
        and peak + 1e-9 >= minimum_future_price(price, spread, efficiency)
    )


def summer_grid_charge_budget_kwh(
    *, capacity_kwh: float, soc_pct: float, maximum_soc_pct: float,
    extra_buffer_pct: float, expected_solar_input_kwh: float, round_trip_efficiency: float,
) -> float:
    """AC energy that fits below the SoC limit after forecast PV and its buffer."""

    charge_efficiency = math.sqrt(round_trip_efficiency)
    space_in_battery_kwh = capacity_kwh * (maximum_soc_pct - soc_pct - extra_buffer_pct) / 100
    return max(0.0, space_in_battery_kwh / charge_efficiency - expected_solar_input_kwh)
