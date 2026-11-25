from datetime import datetime, timedelta, timezone

import pytest

from support import bootstrap

bootstrap()

from custom_components.battery_smartflow_ai.core.economic_schedule import (
    EconomicSlot,
    plan_economic_dispatch,
)
from custom_components.battery_smartflow_ai.core.models.market import (
    MarketPrice,
    MarketPriceDirection,
    MarketPriceForecast,
    MarketPricePoint,
    MarketPriceValidity,
)
from custom_components.battery_smartflow_ai.economic_planning import economic_slots, import_price_preview


def plan(loads, prices, *, stored=0.0, max_input_w=1000, max_output_w=1000,
         capacity=2, export_price=0.0, margin_pct=0, allow_grid_charge=True):
    start = datetime(2026, 11, 24, tzinfo=timezone.utc)
    slots = [
        EconomicSlot(
            start + timedelta(hours=index), start + timedelta(hours=index + 1),
            price, load,
        )
        for index, (load, price) in enumerate(zip(loads, prices, strict=True))
    ]
    return plan_economic_dispatch(
        slots, usable_capacity_kwh=capacity, stored_kwh=stored,
        max_input_w=max_input_w, max_output_w=max_output_w,
        export_price=export_price, margin_pct=margin_pct, allow_grid_charge=allow_grid_charge,
    )


def test_reserve_only_the_load_in_the_higher_price_window():
    result = plan([0.9, 0.2, 0.1], [0.40, 0.60, 0.30], stored=1, allow_grid_charge=False)
    assert result.stored_for_later_kwh == pytest.approx(0.2)
    assert result.available_now_kwh == pytest.approx(0.7)
    assert [slot.discharge_kwh for slot in result.dispatch] == pytest.approx([0.7, 0.2, 0])


def test_grid_charge_only_fills_missing_profitable_energy():
    result = plan([0, 0.9], [0.10, 0.60], stored=0.5)
    assert result.dispatch[0].grid_charge_kwh == pytest.approx((0.9 - 0.45) / 0.81)
    assert result.dispatch[1].discharge_kwh == pytest.approx(0.9)
    assert result.dispatch[1].grid_charge_kwh == 0
    assert plan([0, 1], [0.45, 0.49]).grid_top_up_kwh == 0
    assert plan([0, 1], [0.30, 0.45], margin_pct=27).grid_top_up_kwh == 0


def test_solar_before_prices_displaces_grid_charging_but_later_solar_cannot():
    before = plan([0, -1, 0.8], [0.10, 0.25, 0.60])
    after = plan([0, 0.8, -1], [0.10, 0.60, 0.25])
    assert before.grid_top_up_kwh == 0
    assert before.dispatch[-1].discharge_kwh == pytest.approx(0.8)
    assert after.grid_top_up_kwh == pytest.approx(0.8 / 0.81)


def test_storage_power_and_load_are_enforced_between_intervals():
    result = plan([0, 0, 2, 2], [0.01, 0.02, 0.49, 0.50],
                  capacity=1, max_input_w=600, max_output_w=600)
    assert all(slot.grid_charge_kwh <= 0.6 + 1e-8 for slot in result.dispatch)
    assert all(slot.discharge_kwh <= 0.6 + 1e-8 for slot in result.dispatch)
    assert sum(slot.discharge_kwh for slot in result.dispatch) == pytest.approx(0.9)
    assert result.dispatch[-1].discharge_kwh == pytest.approx(0.6)


def test_a_later_cheaper_purchase_wins_over_charging_now():
    result = plan([0, 0, 0.5], [0.20, 0.10, 0.60])
    assert result.dispatch[0].grid_charge_kwh == 0
    assert result.dispatch[1].grid_charge_kwh == pytest.approx(0.5 / 0.81)


def test_negative_price_does_not_buy_without_later_local_demand():
    assert plan([0.3, 0.0], [-0.15, -0.10]).grid_top_up_kwh == 0
    assert plan([0.3, 0.0], [-0.15, 0.50]).grid_top_up_kwh == 0
    assert plan([0, 1], [-0.15, 0.50], stored=2, capacity=2).grid_top_up_kwh == 0
    result = plan([0, 0, 0.5], [-0.10, -0.20, 0.50])
    assert result.dispatch[0].grid_charge_kwh == 0
    assert result.dispatch[1].grid_charge_kwh == pytest.approx(0.5 / 0.81)


def test_a_full_battery_has_no_room_before_first_discharge():
    result = plan([0, 0.4, 0, 0.8], [0.10, 0.30, 0.20, 0.65], stored=1, capacity=1)
    assert result.dispatch[0].grid_charge_kwh == 0
    assert result.dispatch[1].discharge_kwh == pytest.approx(0.4)
    assert result.dispatch[2].grid_charge_kwh == pytest.approx(0.3 / 0.81)


def test_price_preview_shows_published_intervals_even_if_planning_has_a_gap():
    start = datetime(2026, 11, 24, tzinfo=timezone.utc)
    now = start + timedelta(minutes=30)
    points = tuple(
        MarketPricePoint(start + timedelta(hours=i), start + timedelta(hours=i + 1), 0.20 + i / 100)
        for i in range(8)
    )
    market = MarketPrice(
        MarketPriceDirection.IMPORT, 0.21, "EUR", "€/kWh", now, "test",
        MarketPriceValidity.VALID, True, False, MarketPriceForecast(points[:1] + points[2:]),
    )
    preview = import_price_preview(now, market)
    assert preview["current_price"] == 0.21
    assert preview["unit"] == "€/kWh"
    assert len(preview["periods"]) == 7
    assert preview["periods"][1]["start"] == points[2].start.isoformat()
    assert not economic_slots(
        now, market, load_model=None, forecast_base_load_w=300,
        current_net_load_w=500, solar_intervals=(),
    )


def test_schedule_uses_live_load_first_then_timed_solar():
    start = datetime(2026, 11, 24, tzinfo=timezone.utc)
    now = start + timedelta(minutes=30)
    points = tuple(
        MarketPricePoint(start + timedelta(minutes=30 * i), start + timedelta(minutes=30 * (i + 1)), 0.25)
        for i in range(15)
    )
    market = MarketPrice(
        MarketPriceDirection.IMPORT, 0.20, "EUR", "€/kWh", now, "test",
        MarketPriceValidity.VALID, True, False, MarketPriceForecast(points),
    )
    slots = economic_slots(
        now + timedelta(minutes=15), market, load_model=None, forecast_base_load_w=400,
        current_net_load_w=600, solar_intervals=((start + timedelta(hours=1), 1, 1000),),
    )
    assert [slot.price for slot in slots[:2]] == pytest.approx([0.20, 0.25])
    assert [slot.net_load_kwh for slot in slots[:4]] == pytest.approx([0.15, -0.30, -0.30, 0.2])
