"""Planned consumers in the load forecast and smooth sharing inside a group."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from support import bootstrap

bootstrap()

from custom_components.battery_smartflow_ai.core.models.market import (  # noqa: E402
    MarketPrice, MarketPriceDirection, MarketPriceForecast, MarketPricePoint, MarketPriceValidity,
)
from custom_components.battery_smartflow_ai.economic_planning import economic_slots  # noqa: E402
from custom_components.battery_smartflow_ai.group_handover import handover_barrier_devices  # noqa: E402
from custom_components.battery_smartflow_ai.planned_loads import (  # noqa: E402
    KIND_AVERAGE, KIND_UNTIL_END, MAX_PLANNED_LOADS, PlannedLoad, end_field, parse_end_time,
    parse_planned_loads, planned_energy_kwh, planned_load_intervals, planned_loads_from_form,
    power_field, update_average,
)

UTC = timezone.utc
NOW = datetime(2026, 1, 15, 14, 7, tzinfo=UTC)


def state(value, **attributes):
    return SimpleNamespace(state=str(value), attributes=attributes)


def watts(value):
    return state(value, unit_of_measurement="W", device_class="power")


def minutes(value):
    return state(value, unit_of_measurement="min", device_class="duration")


PRINTER = PlannedLoad("sensor.printer_power", "sensor.printer_remaining")
AQUARIUM = PlannedLoad("sensor.aquarium_power")


# --- configuration ---------------------------------------------------------------

def test_01_stored_list_skips_broken_and_duplicate_entries():
    loads = parse_planned_loads([
        {"power_entity": "sensor.a", "end_entity": "sensor.a_rest"},
        {"power_entity": "sensor.a"},
        {"power_entity": ""},
        "sensor.b",
        {"power_entity": "sensor.c", "end_entity": "  "},
    ])
    assert loads == (PlannedLoad("sensor.a", "sensor.a_rest"), PlannedLoad("sensor.c", None))


def test_02_form_round_trip_keeps_order_and_limit():
    form = {power_field(i): f"sensor.p{i}" for i in range(1, MAX_PLANNED_LOADS + 1)}
    form[end_field(2)] = "sensor.rest2"
    stored = planned_loads_from_form(form)
    assert len(stored) == MAX_PLANNED_LOADS
    assert stored[1] == {"power_entity": "sensor.p2", "end_entity": "sensor.rest2"}
    assert stored[0]["end_entity"] is None


# --- end times -----------------------------------------------------------------

def test_03_remaining_time_in_minutes_hours_and_timestamp():
    assert parse_end_time(minutes(90), NOW) == NOW + timedelta(minutes=90)
    assert parse_end_time(state(2, unit_of_measurement="h"), NOW) == NOW + timedelta(hours=2)
    end = (NOW + timedelta(hours=3)).isoformat()
    assert parse_end_time(state(end, device_class="timestamp"), NOW) == NOW + timedelta(hours=3)


def test_04_unknown_or_implausible_end_time_is_ignored():
    assert parse_end_time(state("unavailable"), NOW) is None
    assert parse_end_time(state(5, unit_of_measurement="fortnight"), NOW) is None
    assert parse_end_time(minutes(60 * 24 * 30), NOW) is None
    assert parse_end_time(minutes(0), NOW) == NOW


# --- forecast contributions -------------------------------------------------------

def test_05_running_printer_counts_until_its_job_ends():
    intervals = planned_load_intervals(
        [PRINTER], now=NOW,
        states={"sensor.printer_power": watts(150), "sensor.printer_remaining": minutes(120)},
        averages={}, include_average_loads=True,
    )
    assert [item.kind for item in intervals] == [KIND_UNTIL_END]
    # 14:07-16:07: the 15:00-16:00 hour is fully covered, 16:00-17:00 only 7 minutes.
    assert planned_energy_kwh(intervals, NOW.replace(minute=0, hour=15), NOW.replace(minute=0, hour=16)) \
        == pytest.approx(0.150)
    assert planned_energy_kwh(intervals, NOW.replace(minute=0, hour=16), NOW.replace(minute=0, hour=17)) \
        == pytest.approx(0.150 * 7 / 60)


def test_06_idle_or_finished_printer_adds_nothing():
    for power, rest in ((3, 120), (150, 0)):
        intervals = planned_load_intervals(
            [PRINTER], now=NOW,
            states={"sensor.printer_power": watts(power), "sensor.printer_remaining": minutes(rest)},
            averages={}, include_average_loads=True,
        )
        assert intervals == ()


def test_07_continuous_consumer_uses_its_daily_average_not_the_current_spike():
    intervals = planned_load_intervals(
        [AQUARIUM], now=NOW, states={"sensor.aquarium_power": watts(380)},
        averages={"sensor.aquarium_power": {"w": 50.0, "at": NOW.isoformat()}},
        include_average_loads=True,
    )
    assert [(item.kind, item.power_w, item.end) for item in intervals] == [(KIND_AVERAGE, 50.0, None)]
    assert planned_energy_kwh(intervals, NOW, NOW + timedelta(hours=24)) == pytest.approx(1.2)


def test_08_learned_profile_already_contains_continuous_consumers():
    intervals = planned_load_intervals(
        [AQUARIUM, PRINTER], now=NOW,
        states={
            "sensor.aquarium_power": watts(380),
            "sensor.printer_power": watts(150), "sensor.printer_remaining": minutes(30),
        },
        averages={"sensor.aquarium_power": {"w": 50.0, "at": NOW.isoformat()}},
        include_average_loads=False,
    )
    assert [item.power_entity for item in intervals] == ["sensor.printer_power"]


def test_09_average_follows_on_off_cycles_with_a_one_day_time_constant():
    average = update_average(None, now=NOW, power_w=0.0)
    for hour in range(1, 97):
        on = hour % 24 < 18  # light 18 h on, 6 h off
        average = update_average(average, now=NOW + timedelta(hours=hour), power_w=200.0 if on else 0.0)
    assert 120 < average["w"] < 175
    assert update_average({"w": "broken"}, now=NOW, power_w=40.0)["w"] == 40.0


def test_10_planned_energy_goes_into_future_slots_only():
    start = NOW.replace(minute=0)
    points = tuple(
        MarketPricePoint(start + timedelta(minutes=15 * i), start + timedelta(minutes=15 * (i + 1)), 0.40)
        for i in range(40)
    )
    market = MarketPrice(
        MarketPriceDirection.IMPORT, 0.40, "EUR", "€/kWh", NOW, "test",
        MarketPriceValidity.VALID, True, False, MarketPriceForecast(points),
    )
    planned = planned_load_intervals(
        [PRINTER], now=NOW,
        states={"sensor.printer_power": watts(200), "sensor.printer_remaining": minutes(38)},
        averages={}, include_average_loads=True,
    )
    plain = economic_slots(NOW, market, load_model=None, forecast_base_load_w=400,
                           current_net_load_w=600, solar_intervals=())
    with_loads = economic_slots(NOW, market, load_model=None, forecast_base_load_w=400,
                                current_net_load_w=600, solar_intervals=(), planned_loads=planned)
    extra = [b.net_load_kwh - a.net_load_kwh for a, b in zip(plain, with_loads)]
    # Live slot 14:07-14:15 is measured; 14:15-14:30 and 14:30-14:45 get 200 W,
    # 14:45-15:00 nothing because the job ends at 14:45.
    assert extra[:4] == pytest.approx([0.0, 0.05, 0.05, 0.0])


# --- sharing between group members ------------------------------------------------

def flows(observed):
    return lambda device_id: observed.get(device_id, (None, 0.0, None))


def test_11_moving_discharge_between_members_does_not_stop_anyone():
    previous = {"sf": {"power_w": -800.0}, "hyper": {"power_w": 0.0}}
    targets = {"sf": -400.0, "hyper": -450.0}
    observed = {"sf": ("discharge", 800.0, NOW)}
    assert handover_barrier_devices(previous, targets, flows(observed), tolerance_w=50) == set()


def test_12_lagging_measurement_during_rebalancing_does_not_stop_anyone():
    previous = {"sf": {"power_w": -800.0}, "hyper": {"power_w": -100.0}}
    targets = {"sf": -100.0, "hyper": -800.0}
    observed = {"sf": ("discharge", 790.0, NOW), "hyper": ("discharge", 110.0, NOW)}
    assert handover_barrier_devices(previous, targets, flows(observed), tolerance_w=50) == set()


def test_13_one_member_charging_while_another_discharges_still_needs_the_barrier():
    previous = {"sf": {"power_w": 600.0}, "hyper": {"power_w": 0.0}}
    targets = {"sf": 0.0, "hyper": -500.0}
    observed = {"sf": ("charge", 600.0, NOW)}
    assert handover_barrier_devices(previous, targets, flows(observed), tolerance_w=50) == {"sf"}


def test_14_group_changing_direction_still_waits_for_zero():
    previous = {"sf": {"power_w": -500.0}, "hyper": {"power_w": -300.0}}
    targets = {"sf": 700.0, "hyper": 0.0}
    observed = {"sf": ("discharge", 500.0, NOW), "hyper": ("discharge", 300.0, NOW)}
    assert handover_barrier_devices(previous, targets, flows(observed), tolerance_w=50) == {"sf", "hyper"}


def test_15_plain_reduction_without_another_member_rising_needs_no_barrier():
    previous = {"sf": {"power_w": -800.0}, "hyper": {"power_w": -400.0}}
    targets = {"sf": -300.0, "hyper": -400.0}
    observed = {"sf": ("discharge", 800.0, NOW), "hyper": ("discharge", 400.0, NOW)}
    assert handover_barrier_devices(previous, targets, flows(observed), tolerance_w=50) == set()
