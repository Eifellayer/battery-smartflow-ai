from datetime import datetime, timedelta, timezone

import pytest

from support import bootstrap

bootstrap()

from custom_components.battery_smartflow_ai.core.economic_schedule import (
    EconomicBattery,
    EconomicSlot,
    plan_economic_dispatch,
    plan_split_economic_dispatch,
)
from custom_components.battery_smartflow_ai.core.models.market import (
    MarketPrice,
    MarketPriceDirection,
    MarketPriceForecast,
    MarketPricePoint,
    MarketPriceValidity,
)
from custom_components.battery_smartflow_ai.economic_planning import economic_slots, import_price_preview
from custom_components.battery_smartflow_ai.group_grid_charging import (
    MEMBER_GRID_CHARGE_SPREADS,
    eligible_grid_charge_members,
    member_grid_charge_policy,
    summer_grid_charge_budget_kwh,
)


def split_plan(
    prices, loads, *, budget=None, max_grid_price=None, input_w=2200, output_w=2000, hyper_grid=True,
):
    start = datetime(2026, 11, 25, tzinfo=timezone.utc)
    slots = [EconomicSlot(
        start + timedelta(hours=i), start + timedelta(hours=i + 1), price, load,
    ) for i, (price, load) in enumerate(zip(prices, loads, strict=True))]
    batteries = [
        EconomicBattery("Hyper", 2, 0, 1200, 1200, 0.79, 27, hyper_grid),
        EconomicBattery("800", 2, 0, 1000, 800, 0.87, 20, True, budget, max_grid_price),
    ]
    return plan_split_economic_dispatch(
        slots, batteries, max_input_w=input_w, max_output_w=output_w, export_price=0.0,
    )


def test_split_plan_at_20_percent_buys_only_on_the_800():
    result = split_plan([0.40, 0.48], [0, 0.60])
    first, peak = result.dispatch
    assert first.grid_charge_by_device == pytest.approx({"Hyper": 0, "800": 0.60 / 0.87})
    assert peak.discharge_by_device == pytest.approx({"Hyper": 0, "800": 0.60})
    assert split_plan([0.40, 0.479], [0, 0.60]).grid_top_up_kwh == 0


def test_split_plan_covers_shared_demand_once_and_obeys_group_output_limit():
    result = split_plan([0.30, 0.50], [0, 1.80], output_w=1000)
    assert result.dispatch[1].discharge_by_device == pytest.approx({"Hyper": 0.20, "800": 0.80})
    assert result.grid_top_up_kwh == pytest.approx(0.20 / 0.79 + 0.80 / 0.87)
    assert split_plan([0.30, 0.50], [0, 0]).grid_top_up_kwh == 0


def test_summer_grid_budget_keeps_pv_forecast_and_ten_percentage_points_in_reserve():
    efficiency = 0.87 ** 0.5
    budget = summer_grid_charge_budget_kwh(
        capacity_kwh=2, soc_pct=40, maximum_soc_pct=100,
        extra_buffer_pct=10, expected_solar_input_kwh=0.60 / efficiency,
        round_trip_efficiency=0.87,
    )
    assert budget * efficiency == pytest.approx(0.40)
    result = split_plan([0.20, 0.20, 0.50, 0.50], [0, 0, 1.6, 1.6], budget=budget, input_w=1_000)
    assert sum(slot.grid_charge_by_device["800"] for slot in result.dispatch) == pytest.approx(budget)
    assert all(slot.grid_charge_kwh <= 1.0 + 1e-8 for slot in result.dispatch)


@pytest.mark.parametrize("price", [-0.05, 0.0, 0.20])
def test_summer_offer_can_buy_at_negative_zero_or_cheap_price(price):
    result = split_plan([price, 0.40], [0, 0.60], budget=0.5, hyper_grid=False)
    assert result.dispatch[0].grid_charge_by_device["800"] == pytest.approx(0.5)


def test_solar_can_fill_reserved_space_and_displace_cheap_grid_charging():
    no_timed_forecast = split_plan([0.20, 0.50], [0, 0.60], budget=0.6, max_grid_price=0)
    assert no_timed_forecast.dispatch[0].grid_charge_by_device["800"] == 0
    solar = split_plan([0.20, 0.30, 0.50], [0, -1.60, 1.25], budget=0)
    assert solar.grid_top_up_kwh == 0
    assert solar.dispatch[2].discharge_by_device["800"] > 0


def test_existing_setup_gets_model_specific_defaults_and_can_override_the_800():
    options = {"native_zendure_device_setups": {"devices": {
        "h": {"setup_status": "configured", "profile_key": "Hyper 2000"},
        "s": {"setup_status": "configured", "profile_key": "SF800Plus"},
    }}}
    policies = member_grid_charge_policy(options, ("h", "s"), 27.0)
    assert policies == {"h": (27, 0.79, 0), "s": (20, 0.87, 10)}
    assert eligible_grid_charge_members(policies, 0.40, [0.48], winter=True) == ("s",)
    assert eligible_grid_charge_members(policies, 0.40, [0.48], winter=False) == ()
    assert eligible_grid_charge_members(policies, 0.40, [], winter=True) == ()
    options[MEMBER_GRID_CHARGE_SPREADS] = {"s": 18}
    policies = member_grid_charge_policy(options, ("h", "s"), 27.0)
    assert eligible_grid_charge_members(policies, 0.40, [0.472], winter=True) == ("s",)


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
    assert [period["current"] for period in preview["periods"]] == [True] + [False] * 6
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


def _stored_split(prices, loads, stored=0.5):
    start = datetime(2026, 11, 27, 16, 45, tzinfo=timezone.utc)
    slots = [EconomicSlot(
        start + timedelta(minutes=15 * i), start + timedelta(minutes=15 * (i + 1)), price, load,
    ) for i, (price, load) in enumerate(zip(prices, loads, strict=True))]
    batteries = [
        EconomicBattery("Hyper", 1.92, stored, 1200, 1200, 0.79, 27, False),
        EconomicBattery("800", 1.92, stored, 1000, 800, 0.87, 20, False),
    ]
    return plan_split_economic_dispatch(
        slots, batteries, max_input_w=2200, max_output_w=2000, export_price=0.0,
    )


def test_live_slot_is_covered_when_later_slots_are_only_slightly_dearer():
    # 61 ct now, 62 ct later: holding energy back would save ~1 ct/kWh.
    result = _stored_split([0.61, 0.62, 0.62, 0.62], [0.175] * 4, stored=0.2)
    assert result.dispatch[0].discharge_kwh == pytest.approx(0.175)


def test_energy_is_still_saved_for_clearly_dearer_slots():
    result = _stored_split([0.61, 0.65, 0.65, 0.65], [0.175] * 4, stored=0.2)
    assert result.dispatch[0].discharge_kwh == pytest.approx(0.0, abs=1e-6)


def test_single_plan_prefers_live_slot_below_three_cents():
    start = datetime(2026, 11, 27, 16, 45, tzinfo=timezone.utc)
    slots = [EconomicSlot(
        start + timedelta(minutes=15 * i), start + timedelta(minutes=15 * (i + 1)), price, 0.2,
    ) for i, price in enumerate([0.61, 0.62, 0.62])]
    kwargs = dict(usable_capacity_kwh=2, max_input_w=1000, max_output_w=2000,
                  export_price=0.0, margin_pct=27)
    near = plan_economic_dispatch(slots, stored_kwh=0.25, **kwargs)
    assert near.dispatch[0].discharge_kwh == pytest.approx(0.2)
    far = plan_economic_dispatch(
        [replace_price(slot, 0.66) if i else slot for i, slot in enumerate(slots)],
        stored_kwh=0.25, **kwargs,
    )
    assert far.dispatch[0].discharge_kwh < 0.2


def test_live_slot_bonus_never_buys_and_discharges_at_once():
    start = datetime(2026, 11, 27, 3, tzinfo=timezone.utc)
    slots = [EconomicSlot(start, start + timedelta(hours=1), 0.02, 0.3),
             EconomicSlot(start + timedelta(hours=1), start + timedelta(hours=2), 0.02, 0.3)]
    result = plan_economic_dispatch(
        slots, usable_capacity_kwh=2, stored_kwh=0, max_input_w=1000, max_output_w=2000,
        export_price=0.0, margin_pct=0,
    )
    assert result.grid_top_up_kwh == pytest.approx(0.0, abs=1e-9)


def replace_price(slot, price):
    return EconomicSlot(slot.start, slot.end, price, slot.net_load_kwh)
