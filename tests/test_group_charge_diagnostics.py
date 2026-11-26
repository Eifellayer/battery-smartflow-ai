"""Regression tests for the slot handover and per-member charge diagnostics.

The price series is the published quarter-hour tariff of one winter night
followed by the next day. With it the planner must reproduce the recorded
top-up and must not stop the members at tariff boundaries.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from support import bootstrap

bootstrap()

from custom_components.battery_smartflow_ai.core.economic_schedule import (  # noqa: E402
    EconomicBattery, EconomicSlot, plan_split_economic_dispatch,
)
from custom_components.battery_smartflow_ai.group_charge_diagnostics import (  # noqa: E402
    STATE_CHARGE_REQUESTED, STATE_CHARGING, STATE_FINISHED, STATE_NOT_PLANNED,
    STATE_PLANNED_LATER, STATE_STOP_REQUESTED, STATE_STOPPED,
    active_dispatch_index, evaluate_member, member_charge_windows,
)

UTC = timezone.utc
NIGHT_START = datetime(2026, 1, 15, 1, 45, tzinfo=UTC)
PRICES = [
    0.4361, 0.4375, 0.4363, 0.4350, 0.4334, 0.4396, 0.4383, 0.4380, 0.4379, 0.4416,
    0.4456, 0.4480, 0.4647, 0.4494, 0.4669, 0.4861, 0.5274, 0.5187, 0.5460, 0.5577,
    0.5812, 0.5681, 0.5965, 0.6002, 0.5897, 0.5936, 0.5943, 0.5978, 0.5812, 0.5948,
    0.5814, 0.5806, 0.5723, 0.5865, 0.5734, 0.5733, 0.5739, 0.5740, 0.5696, 0.5701,
    0.5672, 0.5798, 0.5838, 0.6024, 0.6008, 0.5798, 0.5919, 0.6008, 0.6214, 0.6000,
    0.6176, 0.6453, 0.6509, 0.6528, 0.6379, 0.6453, 0.6416, 0.6399, 0.6556, 0.6578,
    0.6621, 0.6511, 0.6494, 0.6525, 0.6383, 0.6380, 0.6355, 0.6367, 0.6297, 0.6050,
    0.5859, 0.5783, 0.5604, 0.5757, 0.5628, 0.5517, 0.5282, 0.5410, 0.5336, 0.5220,
    0.5017, 0.5064, 0.4860, 0.4809, 0.4728,
]
QUARTER = timedelta(minutes=15)


def slots_at(now, *, base_load_w=300.0):
    result = []
    for index, price in enumerate(PRICES):
        start = NIGHT_START + index * QUARTER
        end = start + QUARTER
        if end <= now:
            continue
        begin = max(start, now)
        hours = (end - begin).total_seconds() / 3600
        result.append(EconomicSlot(begin, end, price, base_load_w * hours / 1000))
    return result


def plan_at(now, *, soc_a, soc_b):
    """Two equal batteries that differ only in efficiency and price spread."""

    batteries = [
        EconomicBattery("A", 1.92 * 0.95, 1.92 * max(0, soc_a - 5) / 100, 1200, 1200, 0.79, 27, True),
        EconomicBattery("B", 1.92 * 0.95, 1.92 * max(0, soc_b - 5) / 100, 900, 800, 0.87, 20, True),
    ]
    return plan_split_economic_dispatch(
        slots_at(now), batteries, max_input_w=2100, max_output_w=2000, export_price=0.0,
    )


def at(hour, minute, second=0):
    return datetime(2026, 1, 15, hour, minute, second, tzinfo=UTC)


def window(start, end, kwh=0.2):
    return {"start": start, "end": end, "energy_kwh": kwh}


# --- replay of the recorded night -------------------------------------------------

def test_01_replay_reproduces_the_recorded_top_up():
    schedule = plan_at(at(1, 45), soc_a=39, soc_b=4)
    assert round(schedule.grid_top_up_kwh, 3) == 3.273


def test_02_replay_plans_the_less_efficient_member_for_five_quarters_not_one():
    schedule = plan_at(at(1, 45), soc_a=39, soc_b=4)
    windows = member_charge_windows(schedule.dispatch, "A")
    assert windows[0]["start"] == at(1, 45)
    assert windows[0]["end"] == at(3, 0)


def test_03_replay_plans_the_efficient_member_through_the_whole_window():
    schedule = plan_at(at(1, 45), soc_a=39, soc_b=4)
    windows = member_charge_windows(schedule.dispatch, "B")
    assert windows[0]["start"] == at(1, 45)
    assert windows[-1]["end"] >= at(3, 45)


def test_04_replay_keeps_both_members_planned_in_the_second_quarter():
    schedule = plan_at(at(2, 0, 2), soc_a=47, soc_b=12)
    first = schedule.dispatch[0]
    assert first.grid_charge_by_device["A"] > 0.2
    assert first.grid_charge_by_device["B"] > 0.2


def test_05_replay_releases_the_first_member_when_its_share_is_bought():
    schedule = plan_at(at(3, 1, 13), soc_a=85, soc_b=55)
    first = schedule.dispatch[0]
    assert first.grid_charge_by_device["A"] < 0.001
    assert first.grid_charge_by_device["B"] > 0.15


def test_06_replay_moves_unbought_energy_into_a_later_window():
    # Charging slower than planned leaves energy that a later slot must buy.
    schedule = plan_at(at(3, 1, 13), soc_a=85, soc_b=55)
    later = member_charge_windows(schedule.dispatch, "A")
    assert later and later[0]["start"] >= at(3, 15)


# --- handover between tariff slots -----------------------------------------------

def test_07_running_slot_with_time_left_stays_active():
    schedule = plan_at(at(2, 14, 30), soc_a=58, soc_b=21)
    assert active_dispatch_index(schedule.dispatch) == 0


def test_08_last_seconds_of_a_slot_hand_over_to_the_next_slot():
    schedule = plan_at(at(2, 14, 53), soc_a=58, soc_b=21)
    index = active_dispatch_index(schedule.dispatch)
    assert index == 1
    following = schedule.dispatch[index]
    assert following.slot.hours == 0.25
    # Both members keep a charge target across the boundary.
    assert following.grid_charge_by_device["A"] * 1000 / following.slot.hours > 800
    assert following.grid_charge_by_device["B"] * 1000 / following.slot.hours > 800


def test_09_handover_stops_a_member_that_is_not_in_the_next_slot():
    schedule = plan_at(at(2, 59, 55), soc_a=84, soc_b=54)
    following = schedule.dispatch[active_dispatch_index(schedule.dispatch)]
    assert following.grid_charge_by_device["A"] < 0.001
    assert following.grid_charge_by_device["B"] > 0.15


def test_10_single_remaining_slot_is_not_replaced():
    schedule = plan_at(at(2, 14, 53), soc_a=58, soc_b=21)
    assert active_dispatch_index(schedule.dispatch[:1]) == 0


# --- per-member diagnostic states ------------------------------------------------

def test_11_member_without_any_window_is_not_planned():
    result, _ = evaluate_member(None, now=at(1, 0), windows=[], target_w=0, measured_ac_input_w=0)
    assert result["state"] == STATE_NOT_PLANNED
    assert result["windows"] == []


def test_12_future_window_is_planned_later_and_listed_with_its_energy():
    result, _ = evaluate_member(
        None, now=at(1, 0), windows=[window(at(1, 45), at(3, 0), 1.3)],
        target_w=0, measured_ac_input_w=0,
    )
    assert result["state"] == STATE_PLANNED_LATER
    assert result["windows"] == [{
        "start": at(1, 45).isoformat(), "end": at(3, 0).isoformat(), "energy_kwh": 1.3,
    }]


def test_13_request_is_only_confirmed_by_measured_ac_input():
    windows = [window(at(1, 45), at(3, 0))]
    requested, memory = evaluate_member(
        None, now=at(1, 45, 5), windows=windows, target_w=1100, measured_ac_input_w=0,
    )
    assert requested["state"] == STATE_CHARGE_REQUESTED
    unknown, _ = evaluate_member(
        memory, now=at(1, 45, 8), windows=windows, target_w=1100, measured_ac_input_w=None,
    )
    assert unknown["state"] == STATE_CHARGE_REQUESTED and unknown["ac_input_w"] is None
    confirmed, _ = evaluate_member(
        memory, now=at(1, 45, 20), windows=windows, target_w=1100, measured_ac_input_w=812,
    )
    assert confirmed["state"] == STATE_CHARGING
    assert confirmed["requested_w"] == 1100 and confirmed["ac_input_w"] == 812


def test_14_stop_is_reported_until_the_ac_input_is_really_gone_then_finished():
    windows = [window(at(1, 45), at(3, 0))]
    _, memory = evaluate_member(
        None, now=at(2, 59), windows=windows, target_w=1100, measured_ac_input_w=820,
    )
    stopping, memory = evaluate_member(
        memory, now=at(3, 0, 3), windows=[], target_w=0, measured_ac_input_w=640,
    )
    assert stopping["state"] == STATE_STOP_REQUESTED
    finished, memory = evaluate_member(
        memory, now=at(3, 0, 20), windows=[], target_w=0, measured_ac_input_w=0,
    )
    assert finished["state"] == STATE_FINISHED
    # A pause before a later window is a confirmed stop, not the end.
    _, memory = evaluate_member(
        None, now=at(2, 59), windows=[window(at(1, 45), at(3, 0)), window(at(3, 30), at(3, 45))],
        target_w=1100, measured_ac_input_w=820,
    )
    paused, _ = evaluate_member(
        memory, now=at(3, 0, 20), windows=[window(at(3, 30), at(3, 45))],
        target_w=0, measured_ac_input_w=0,
    )
    assert paused["state"] == STATE_STOPPED


def test_15_changed_windows_are_marked_but_elapsed_windows_are_not():
    first = [window(at(1, 45), at(2, 0)), window(at(2, 0), at(2, 15))]
    _, memory = evaluate_member(None, now=at(1, 0), windows=first, target_w=0, measured_ac_input_w=0)
    # The first window has simply ended.
    same, memory = evaluate_member(
        memory, now=at(2, 0, 1), windows=first[1:], target_w=900, measured_ac_input_w=800,
    )
    assert "plan_changed_at" not in same
    # A replan adds a window that was not announced before.
    changed, memory = evaluate_member(
        memory, now=at(2, 5), windows=[first[1], window(at(3, 30), at(3, 45))],
        target_w=900, measured_ac_input_w=800,
    )
    assert changed["plan_changed_at"] == at(2, 5).isoformat()
    # The mark stays visible while the new plan runs.
    kept, _ = evaluate_member(
        memory, now=at(2, 6), windows=[first[1], window(at(3, 30), at(3, 45))],
        target_w=900, measured_ac_input_w=800,
    )
    assert kept["plan_changed_at"] == at(2, 5).isoformat()
