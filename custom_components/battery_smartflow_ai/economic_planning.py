"""Build dispatch intervals from the existing price, load and solar forecasts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math

from .core.economic_schedule import EconomicSlot
from .core.models.market import MarketPrice
from .learned_planning import LearnedSlotModel, expected_consumption_until


def import_price_preview(now: datetime, market: MarketPrice) -> dict:
    """Expose published tariffs, retaining gaps and the source currency."""

    horizon = now + timedelta(hours=24)
    points = market.forecast.points if market.valid and market.forecast else ()
    return {
        "unit": market.unit,
        "current_price": market.current_price if market.valid else None,
        "periods": [
            {"start": point.start.isoformat(), "end": point.end.isoformat(), "price": point.price}
            for point in sorted(points, key=lambda point: point.start)
            if point.end > now and point.start < horizon
            and point.start < point.end and math.isfinite(point.price)
        ],
    }


def economic_slots(
    now: datetime,
    market: MarketPrice,
    *,
    load_model: LearnedSlotModel | None,
    forecast_base_load_w: float,
    current_net_load_w: float,
    solar_intervals: tuple[tuple[datetime, float, float], ...],
) -> tuple[EconomicSlot, ...]:
    """Return up to 24 hours of contiguous tariffs, including the live slot."""

    if not market.valid or market.forecast is None or not market.is_dynamic:
        return ()
    if market.current_price is None or not math.isfinite(market.current_price):
        return ()
    now = now.astimezone(timezone.utc)
    horizon = now + timedelta(hours=24)
    cursor = now
    slots = []
    for point in sorted(market.forecast.points, key=lambda point: point.start):
        end = min(horizon, point.end.astimezone(timezone.utc))
        start = point.start.astimezone(timezone.utc)
        if end <= now:
            continue
        if (slots and start != cursor) or start > cursor or end <= cursor:
            break
        if not math.isfinite(point.price):
            break
        hours = (end - cursor).total_seconds() / 3600
        if not slots:
            net_kwh = current_net_load_w * hours / 1000
            price = market.current_price
        else:
            load_kwh = (
                expected_consumption_until(load_model, cursor, end)
                if load_model is not None
                else forecast_base_load_w * hours / 1000
            )
            solar_kwh = 0.0
            for solar_start, solar_hours, solar_wh in solar_intervals:
                if solar_hours <= 0 or not math.isfinite(solar_wh):
                    continue
                solar_start = solar_start.astimezone(timezone.utc)
                solar_end = solar_start + timedelta(hours=solar_hours)
                seconds = (min(end, solar_end) - max(cursor, solar_start)).total_seconds()
                if seconds > 0:
                    solar_kwh += solar_wh * (seconds / 3600 / solar_hours) / 1000
            net_kwh = load_kwh - solar_kwh
            price = point.price
        slots.append(EconomicSlot(cursor, end, price, net_kwh))
        cursor = end
        if cursor >= horizon:
            break
    # A short or broken feed must not tell the battery to empty before the
    # next as-yet-unknown price window.
    return tuple(slots) if cursor - now >= timedelta(hours=6) else ()
