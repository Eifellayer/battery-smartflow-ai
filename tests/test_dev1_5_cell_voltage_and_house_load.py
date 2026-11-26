from __future__ import annotations

import asyncio
import sys
from types import ModuleType

from support import bootstrap

bootstrap()

ha_helpers = ModuleType("homeassistant.helpers")
ha_helpers.__path__ = []
sys.modules.setdefault("homeassistant.helpers", ha_helpers)
ha_event = ModuleType("homeassistant.helpers.event")
ha_event.async_call_later = lambda *_args, **_kwargs: None
ha_event.async_track_state_change_event = lambda *_args, **_kwargs: None
ha_event.async_track_time_interval = lambda *_args, **_kwargs: None
sys.modules.setdefault("homeassistant.helpers.event", ha_event)
ha_storage = ModuleType("homeassistant.helpers.storage")


class _Store:
    def __init__(self, *_args, **_kwargs):
        pass


ha_storage.Store = _Store
sys.modules.setdefault("homeassistant.helpers.storage", ha_storage)
ha_coordinator = ModuleType("homeassistant.helpers.update_coordinator")


class _DataUpdateCoordinator:
    @classmethod
    def __class_getitem__(cls, _item):
        return cls

    def __init__(self, *_args, **_kwargs):
        pass


class _UpdateFailed(Exception):
    pass


ha_coordinator.DataUpdateCoordinator = _DataUpdateCoordinator
ha_coordinator.UpdateFailed = _UpdateFailed
sys.modules.setdefault("homeassistant.helpers.update_coordinator", ha_coordinator)
ha_aiohttp = ModuleType("homeassistant.helpers.aiohttp_client")
ha_aiohttp.async_get_clientsession = lambda *_args, **_kwargs: None
sys.modules.setdefault("homeassistant.helpers.aiohttp_client", ha_aiohttp)

from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

from custom_components.battery_smartflow_ai.const import (
    CONF_BATTERY_GROUPS,
    CONF_CELL_VOLTAGE_PROTECTION_ENABLED,
    CONF_ELECTRICAL_TOPOLOGY,
    CONF_EXPERT_MODE_ENABLED,
    SETTING_MAX_CHARGE,
    SETTING_MAX_DISCHARGE,
)
from custom_components.battery_smartflow_ai.core.models import (
    CommandExecutionResult,
    CommandExecutionStatus,
    DeviceCommand,
    MarketPricePoint,
)
from custom_components.battery_smartflow_ai.coordinator import (
    ZendureSmartFlowCoordinator,
    _charge_price_opportunities,
    _discharge_price_plan,
    _group_power_display,
    _native_battery_power_totals,
)
from custom_components.battery_smartflow_ai.group_feedback import GroupFeedback

# The coordinator only needs the Store adapter as a type here. Let the store
# contract tests install their own controllable FakeHAStore implementation.
sys.modules.pop("custom_components.battery_smartflow_ai.adapters.home_assistant.state_store", None)
sys.modules.pop("homeassistant.helpers.storage", None)
from custom_components.battery_smartflow_ai.core.models.states import (
    MeasuredValue,
    ValueValidity,
)


def test_discharge_price_plan_lists_all_suitable_future_intervals():
    now = datetime(2026, 11, 24, 22, tzinfo=timezone.utc)

    def point(minutes, price):
        start = now + timedelta(minutes=minutes)
        return MarketPricePoint(start, start + timedelta(minutes=15), price)

    intervals = [point(60, 0.60), point(-5, 0.60), point(15, 0.46),
                 point(30, 0.55), point(75, 0.49), point(90, 0.53)]
    result = _discharge_price_plan(intervals, now, 0.52, 0.49)
    assert {key: result[key] for key in ("status", "start", "end", "price")} == {
        "status": "price_opportunity",
        "start": point(30, 0.55).start.isoformat(),
        "end": point(30, 0.55).end.isoformat(),
        "price": 0.55,
    }
    assert [window["price"] for window in result["windows"]] == [0.55, 0.60, 0.53]
    assert _discharge_price_plan(intervals, now, 0.65) == {
        "status": "no_matching_price",
    }
    assert _discharge_price_plan(intervals[1:2], now, 0.49) == {
        "status": "no_price_forecast",
    }


def test_charge_price_opportunities_use_future_tou_and_include_the_threshold():
    now = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
    prices = {-15: 0.01, 0: 0.10, 15: 0.42, 30: 0.45, 45: 0.52, 1440: 0.20}
    points = [
        MarketPricePoint(now + timedelta(minutes=offset), now + timedelta(minutes=offset + 15), price)
        for offset, price in prices.items()
    ]
    assert [p["price"] for p in _charge_price_opportunities(points, now, 0.25, 0.45)] == [0.42, 0.45]
    assert _charge_price_opportunities(points, now, None, float("nan")) == []


def test_group_discharge_display_uses_fresh_pack_data_when_idle_controller_is_missing():
    now = datetime(2026, 11, 25, 7, 50, tzinfo=timezone.utc)
    empty = MeasuredValue.absent(ValueValidity.MISSING)
    pack = SimpleNamespace(discharge_power_w=MeasuredValue.available(484, observed_at=now))
    member = SimpleNamespace(ac_output_power_w=empty, discharge_power_w=empty, packs=[pack])
    targets = {"hyper": {"direction": "idle", "power_w": 0},
               "sf800": {"direction": "discharge", "power_w": -498}}

    assert _group_power_display(now, targets, {"sf800": member}, None, direction="discharge") == {
        "group_discharge_requested": True, "observed_group_battery_discharge_w": 484,
    }
    assert _group_power_display(now, targets, {"sf800": member}, GroupFeedback(0, 465, 5), direction="discharge") == {
        "group_discharge_requested": True, "observed_group_output_w": 465,
    }
    member.ac_output_power_w = MeasuredValue.available(463, observed_at=now)
    assert _group_power_display(now, targets, {"sf800": member}, None, direction="discharge") == {
        "group_discharge_requested": True, "observed_member_output_w": 463,
    }
    assert _group_power_display(now + timedelta(seconds=31), targets, {"sf800": member}, None, direction="discharge") == {
        "group_discharge_requested": True,
    }
    assert _group_power_display(now, {"hyper": targets["hyper"]}, {"sf800": member}, None, direction="discharge") == {}


def test_group_charge_display_distinguishes_measured_ac_and_pack_charge():
    now = datetime(2026, 11, 25, 7, 50, tzinfo=timezone.utc)
    empty = MeasuredValue.absent(ValueValidity.MISSING)
    pack = SimpleNamespace(charge_power_w=MeasuredValue.available(640, observed_at=now))
    states = {"sf800": SimpleNamespace(ac_input_power_w=empty, charge_power_w=empty, packs=[pack])}
    targets = {"hyper": {"direction": "idle", "power_w": 0},
               "sf800": {"direction": "charge", "power_w": 700}}
    requested = {"group_charge_requested": True}

    def show(feedback=None, *, age=0):
        return _group_power_display(
            now + timedelta(seconds=age), targets, states, feedback, direction="charge",
        )

    assert show(GroupFeedback(734, 0, 5)) == {**requested, "observed_group_input_w": 734}
    assert show() == {**requested, "observed_group_battery_charge_w": 640}
    states["sf800"].ac_input_power_w = MeasuredValue.available(710, observed_at=now)
    assert show() == {**requested, "observed_member_input_w": 710}
    assert show(age=31) == requested
    assert _group_power_display(now, targets, states, None, direction="discharge") == {}


def _device_state(charge: float | None, discharge: float | None):
    def measured(value):
        return (
            MeasuredValue.available(value)
            if value is not None
            else MeasuredValue.absent(ValueValidity.MISSING)
        )

    return SimpleNamespace(
        charge_power_w=measured(charge),
        discharge_power_w=measured(discharge),
    )


def test_native_battery_power_totals_sum_all_systems() -> None:
    states = (
        _device_state(2400.0, 0.0),
        _device_state(2399.0, 0.0),
        _device_state(2399.0, 0.0),
    )

    totals = _native_battery_power_totals(states)
    assert totals is not None
    assert totals == (7198.0, 0.0)
    assert round(max(0.0, 7545.9 + 364.8 + totals[1] - totals[0]), 0) == 713


def test_native_battery_power_totals_require_complete_system_readings() -> None:
    states = (
        _device_state(2400.0, 0.0),
        _device_state(2399.0, 0.0),
        _device_state(None, None),
    )

    assert _native_battery_power_totals(states) is None


def test_group_handover_uses_ac_power_when_solar_keeps_charging() -> None:
    async def run():
        from custom_components.battery_smartflow_ai.core.models.market import (
            MarketPrice, MarketPriceDirection, MarketPriceForecast, MarketPricePoint, MarketPriceValidity,
        )

        now = datetime(2026, 11, 24, tzinfo=timezone.utc)
        devices = {"Hyper 2000": 1200, "SolarFlow 800 Plus": 800}
        states = {}
        writes = []

        async def execute(command, *, device_id):
            writes.append((device_id, command))
            return CommandExecutionResult(CommandExecutionStatus.APPLIED, "sent")

        coordinator = ZendureSmartFlowCoordinator.__new__(ZendureSmartFlowCoordinator)
        coordinator._clock = SimpleNamespace(utc_now=lambda: now)
        coordinator._persist = {"group_control_lead_state": {"last_active_at": now.isoformat()}}
        coordinator.entry = SimpleNamespace(options={
            CONF_BATTERY_GROUPS: {"groups": {"home": {
                "name": "Home", "members": list(devices), "control_enabled": True,
            }}},
            CONF_ELECTRICAL_TOPOLOGY: {
                "circuits": {"shared": {"name": "Shared circuit", "max_power_w": 1400}},
                "connections": [
                    {"id": device, "device_id": device, "output_id": "grid",
                     "kind": "grid", "circuit_id": "shared"}
                    for device in devices
                ],
            },
        })
        coordinator._get_grid = lambda: (600.0, 0.0)
        coordinator._selected_energy_scope = lambda _entry: {
            "group_ids": ["home"], "grid_mode": "signed",
        }
        coordinator.native_zendure = SimpleNamespace(
            selected_device_id="Hyper 2000",
            control_enabled=True,
            async_execute_device_command=execute,
            fresh_device_state_map=lambda: states,
            shadow_allocation_snapshots=lambda **_: [
                {"device_id": device, "name": device, "available": True,
                 "configured": True, "supported": True, "protected": False,
                 "hems_blocked": False, "soc_pct": 60, "reserve_soc_pct": 20,
                 "maximum_soc_pct": 95, "capacity_kwh": 2,
                 "max_charge_w": limit, "max_discharge_w": limit}
                for device, limit in devices.items()
            ],
        )

        options = coordinator.entry.options
        circuit = options[CONF_ELECTRICAL_TOPOLOGY]["circuits"]["shared"]
        circuit["max_power_w"] = 2000
        runtime = coordinator.native_zendure
        demand = DeviceCommand("output", output_limit_w=2500)
        for controller, hardware_limit in devices.items():
            runtime.selected_device_id = controller
            for configured_limit in (None, 0, 700, 1300, 2000):
                if configured_limit is not None:
                    options[SETTING_MAX_DISCHARGE] = configured_limit
                expected = 700 if configured_limit is None else configured_limit
                assert coordinator._native_group_power_limit_w(
                    runtime, setting=SETTING_MAX_DISCHARGE,
                    selected_hardware_limit_w=hardware_limit,
                    reserve_soc_pct=20, maximum_soc_pct=95,
                ) == expected
                _, group_targets = coordinator._productive_group_targets(demand, runtime)
                assert sum(cmd.output_limit_w for cmd in group_targets.values()) == expected
                preview = coordinator._shadow_group_allocation(
                    strategy_intent=SimpleNamespace(requested_mode="output"),
                    charge_power_w=0, discharge_power_w=2500, soc_min=20, soc_max=95,
                )
                assert preview["group_max_discharge_w"] == expected
                assert preview["proposed_power_w"] == expected
            options.pop(SETTING_MAX_DISCHARGE)

        runtime.selected_device_id = "Hyper 2000"
        options[SETTING_MAX_DISCHARGE] = 2000
        status, targets = coordinator._productive_group_targets(
            demand, coordinator.native_zendure
        )
        assert status == "group_control_active"
        assert {device: cmd.output_limit_w for device, cmd in targets.items()} == devices

        intent = SimpleNamespace(metadata={"group_selected_max_output_w": 500})
        _, tapered = coordinator._productive_group_targets(
            demand, runtime, strategy_intent=intent
        )
        assert {device: cmd.output_limit_w for device, cmd in tapered.items()} == {
            "Hyper 2000": 500, "SolarFlow 800 Plus": 800,
        }
        options[SETTING_MAX_CHARGE] = 600
        _, charge_targets = coordinator._productive_group_targets(
            DeviceCommand("input", input_limit_w=1800), runtime
        )
        assert sum(cmd.input_limit_w for cmd in charge_targets.values()) == 600
        split_intent = SimpleNamespace(metadata={
            "group_economic_direction": "input",
            "group_economic_member_limits_w": {"SolarFlow 800 Plus": 350},
        })
        status, split_targets = coordinator._productive_group_targets(
            DeviceCommand("input", input_limit_w=1800), runtime, strategy_intent=split_intent,
        )
        assert status == "group_control_active"
        assert split_targets["Hyper 2000"].input_limit_w == 0
        assert split_targets["SolarFlow 800 Plus"].input_limit_w == 350
        options.pop(SETTING_MAX_CHARGE)

        snapshots = runtime.shadow_allocation_snapshots
        runtime.shadow_allocation_snapshots = lambda **kwargs: [
            {**item, "soc_pct": 20} for item in snapshots(**kwargs)
        ]

        async def executor(job):
            return job()

        coordinator.hass = SimpleNamespace(async_add_executor_job=executor)

        async def get_plan(season, price, peak, forecast=None, at=None, cheap_hours=1):
            points = tuple(MarketPricePoint(
                now + timedelta(hours=i), now + timedelta(hours=i + 1), price if i < cheap_hours else peak,
            ) for i in range(8))
            market = MarketPrice(
                MarketPriceDirection.IMPORT, price, "EUR", "€/kWh", now, "test",
                MarketPriceValidity.VALID, True, False, MarketPriceForecast(points),
            )
            return await coordinator._economic_group_plan(
                SimpleNamespace(
                    now=at or now, ai_mode="automatic", season=season, soc_min=20, soc_max=95,
                    grid_sensor_valid=True, pv_sensor_valid=True, soc_limits_valid=True,
                    power_limits_valid=True, forecast=forecast, import_market_price=market,
                    export_market_price=None, profit_margin_pct=27,
                ), runtime,
                current_ac_input_w=1200, load_model=None, learned_ready=False,
                forecast_base_load_w=400, selected_capacity_kwh=2, current_net_load_w=300,
                grid_charge_policy={"Hyper 2000": (27, 0.79, 0), "SolarFlow 800 Plus": (20, 0.87, 10)},
            )

        summary, charge_w = await get_plan("winter", 0.40, 0.48)
        assert summary["status"] == "active_partial_forecast"
        assert summary["grid_charge_kwh"]["members"] == ["SolarFlow 800 Plus"]
        assert summary["_member_limits_w"]["Hyper 2000"] == 0
        assert 0 < charge_w <= 800
        diagnostics = summary["member_charging"]
        assert diagnostics["Hyper 2000"]["state"] == "not_planned"
        assert diagnostics["Hyper 2000"]["planned_w"] == 0
        assert diagnostics["SolarFlow 800 Plus"]["planned_w"] == round(charge_w)
        assert diagnostics["SolarFlow 800 Plus"]["windows"]

        # Five seconds before a tariff boundary the target of the following
        # cheap hour applies; the members are not stopped and restarted.
        boundary, handover_w = await get_plan(
            "winter", 0.40, 0.60, at=now + timedelta(minutes=59, seconds=55), cheap_hours=2,
        )
        assert handover_w > 0
        assert any(limit > 0 for limit in boundary["_member_limits_w"].values())

        # A running print is part of the forecast and shows up in the plan.
        assert "planned_loads" not in summary
        coordinator.entry.options["planned_loads"] = [
            {"power_entity": "sensor.printer_power", "end_entity": "sensor.printer_remaining"},
            {"power_entity": "sensor.aquarium_power", "end_entity": None},
        ]
        states = {
            "sensor.printer_power": SimpleNamespace(
                state="180", attributes={"unit_of_measurement": "W", "friendly_name": "Drucker"}),
            "sensor.printer_remaining": SimpleNamespace(
                state="300", attributes={"unit_of_measurement": "min", "device_class": "duration"}),
            "sensor.aquarium_power": SimpleNamespace(
                state="60", attributes={"unit_of_measurement": "W", "friendly_name": "Aquarium"}),
        }
        coordinator.hass = SimpleNamespace(
            async_add_executor_job=executor, states=SimpleNamespace(get=states.get),
        )
        planned_summary, planned_charge_w = await get_plan("winter", 0.40, 0.48)
        assert [(item["name"], item["kind"], item["power_w"]) for item in planned_summary["planned_loads"]] == [
            ("Drucker", "until_end", 180), ("Aquarium", "average", 60),
        ]
        assert coordinator._persist["planned_load_averages"]["sensor.aquarium_power"]["w"] == 60
        # The small fixture battery is already full-planned; the extra load
        # must not lower the purchase.
        assert planned_summary["top_up_kwh"] == summary["top_up_kwh"]
        assert abs(planned_charge_w - charge_w) < 1e-6

        weak_solar = SimpleNamespace(power_intervals=((now + timedelta(hours=1), 1, 100),))
        summer, charge_w = await get_plan("summer", 0.20, 0.40, weak_solar)
        assert summer["status"] == "active_partial_forecast"
        assert summer["summer_grid_charge"][0]["extra_buffer_pct"] == 10
        assert summer["grid_charge_kwh"]["members"] == ["SolarFlow 800 Plus"]
        assert 0 < charge_w <= 800

        sunny_forecast = SimpleNamespace(power_intervals=((now + timedelta(hours=1), 3, 8000),))
        sunny, charge_w = await get_plan("summer", 0.20, 0.40, sunny_forecast)
        assert sunny["summer_grid_charge"][0]["grid_budget_kwh"] == 0
        assert "grid_charge_kwh" not in sunny
        assert charge_w <= 0

        unconfirmed, charge_w = await get_plan("summer", 0.20, 0.40)
        assert unconfirmed["status"] == "load_history_missing"
        assert charge_w is None
        negative, charge_w = await get_plan("summer", -0.05, 0.40)
        assert negative["grid_charge_kwh"]["members"] == ["SolarFlow 800 Plus"]
        assert 0 < charge_w <= 800
        runtime.shadow_allocation_snapshots = snapshots

        options[SETTING_MAX_DISCHARGE] = 1300
        _, targets = coordinator._productive_group_targets(demand, coordinator.native_zendure)
        assert sum(cmd.output_limit_w for cmd in targets.values()) == 1300
        assert coordinator._native_group_power_limit_w(
            coordinator.native_zendure, setting=SETTING_MAX_DISCHARGE,
            selected_hardware_limit_w=1200, reserve_soc_pct=20, maximum_soc_pct=95,
        ) == 1300

        # The economic request must not undo the technical deadband/ramp.
        intent = SimpleNamespace(requested_mode="output", requested_power_w=550, metadata={})
        _, targets = coordinator._productive_group_targets(
            DeviceCommand("output", output_limit_w=190), coordinator.native_zendure,
            strategy_intent=intent,
        )
        assert sum(cmd.output_limit_w for cmd in targets.values()) == 190
        _, targets = coordinator._productive_group_targets(
            DeviceCommand("output", output_limit_w=0), coordinator.native_zendure,
            strategy_intent=intent,
        )
        assert sum(cmd.output_limit_w for cmd in targets.values()) == 0

        # A pending 200 W correction may not accumulate additional output
        # from repeated observations of the same residual import.
        coordinator._persist["group_control_last_targets"] = {
            "Hyper 2000": {"power_w": -200.0},
            "SolarFlow 800 Plus": {"power_w": 0.0},
        }
        coordinator._persist["group_control_target_changed_at"] = now.isoformat()
        intent.intent = "arbitrage_discharge"
        demand = DeviceCommand("output", output_limit_w=500,
                               metadata={"last_output_limit_w": 200})
        _, targets = coordinator._productive_group_targets(
            demand, coordinator.native_zendure, strategy_intent=intent,
        )
        assert sum(cmd.output_limit_w for cmd in targets.values()) == 200
        assert targets["SolarFlow 800 Plus"].metadata["last_output_limit_w"] == 0
        assert targets["Hyper 2000"].metadata["last_output_limit_w"] == 200
        now += timedelta(seconds=13)
        coordinator._get_grid = lambda: (60.0, 0.0)
        _, targets = coordinator._productive_group_targets(
            demand, coordinator.native_zendure, strategy_intent=intent,
        )
        assert sum(cmd.output_limit_w for cmd in targets.values()) == 250
        coordinator._get_grid = lambda: (0.0, 150.0)
        _, targets = coordinator._productive_group_targets(
            demand, coordinator.native_zendure, strategy_intent=intent,
        )
        assert sum(cmd.output_limit_w for cmd in targets.values()) == 200
        _, targets = coordinator._productive_group_targets(
            DeviceCommand("output", output_limit_w=80), coordinator.native_zendure,
            strategy_intent=intent,
        )
        assert sum(cmd.output_limit_w for cmd in targets.values()) == 80
        coordinator._persist.pop("group_control_last_targets")
        coordinator._persist.pop("group_control_target_changed_at")
        coordinator._get_grid = lambda: (600.0, 0.0)

        options[SETTING_MAX_DISCHARGE] = 2000
        circuit["max_power_w"] = 1400
        started = await coordinator._execute_device_command(
            DeviceCommand("output", output_limit_w=1700)
        )
        assert started.reason == "group_control_active"
        assert {device for device, _ in writes} == set(devices)
        assert sum(command.output_limit_w for _, command in writes) == 1400
        assert coordinator._persist["last_set_output_w"] == 1400
        assert all(item["accepted_at"] == now.isoformat()
                   for item in coordinator._persist["group_control_last_targets"].values())
        assert all(0 < command.output_limit_w <= devices[device] for device, command in writes)

        writes.clear()
        charge = DeviceCommand("input", input_limit_w=1400)
        waiting = await coordinator._execute_device_command(charge)
        assert waiting.reason == "group_control_handover_waiting_for_zero"
        assert {device for device, _ in writes} == set(devices)
        assert all(command.input_limit_w == command.output_limit_w == 0 for _, command in writes)

        preview = coordinator._shadow_group_allocation(
            strategy_intent=SimpleNamespace(requested_mode="input"),
            charge_power_w=1400, discharge_power_w=0, soc_min=20, soc_max=95,
        )
        public_ids = {item["device_id"] for item in preview["devices"]}
        assert preview["dispatch_status"] == "group_control_handover_waiting_for_zero"
        assert set(preview["handover_pending"]["devices"]) == public_ids
        assert public_ids.isdisjoint(devices)

        now += timedelta(seconds=2)
        states.update({device: SimpleNamespace(
            ac_input_power_w=MeasuredValue.available(0, observed_at=now),
            ac_output_power_w=MeasuredValue.available(0, observed_at=now),
            charge_power_w=MeasuredValue.available(400, observed_at=now),
            discharge_power_w=MeasuredValue.available(0, observed_at=now),
        ) for device in devices})
        writes.clear()
        charged = await coordinator._execute_device_command(charge)
        assert charged.reason == "group_control_active"
        assert {device for device, _ in writes} == set(devices)
        assert sum(command.input_limit_w for _, command in writes) == 1400
        assert all(0 < command.input_limit_w <= devices[device] for device, command in writes)

        preview = coordinator._shadow_group_allocation(
            strategy_intent=SimpleNamespace(requested_mode="input"),
            charge_power_w=1400, discharge_power_w=0, soc_min=20, soc_max=95,
        )
        assert set(preview["last_accepted_targets"]) == public_ids
        assert preview["handover_pending"] is None

        # Cloud devices publish power only on change: the zero sample was
        # observed before the stop and is never republished, and no setpoint
        # readback arrives. The repeated stop refreshes requested_at, so only
        # the time of the first stop may end the wait.
        stale_at = now

        def stale_zero():
            return MeasuredValue(0, ValueValidity.STALE, stale_at)

        # An absent reading is never a stop confirmation, however long it waits.
        states.update({device: SimpleNamespace(
            ac_input_power_w=MeasuredValue.absent(ValueValidity.NEVER_RECEIVED),
            ac_output_power_w=stale_zero(),
            charge_power_w=stale_zero(),
            discharge_power_w=stale_zero(),
        ) for device in devices})
        now += timedelta(seconds=5)
        await coordinator._execute_device_command(DeviceCommand("output", output_limit_w=1400))
        now += timedelta(seconds=300)
        never = await coordinator._execute_device_command(DeviceCommand("output", output_limit_w=1400))
        assert never.reason == "group_control_handover_waiting_for_zero"
        coordinator._persist.pop("group_control_handover_pending", None)

        states.update({device: SimpleNamespace(
            ac_input_power_w=stale_zero(),
            ac_output_power_w=stale_zero(),
            charge_power_w=stale_zero(),
            discharge_power_w=stale_zero(),
        ) for device in devices})
        now += timedelta(seconds=5)
        writes.clear()
        discharge = DeviceCommand("output", output_limit_w=1400)
        waiting = await coordinator._execute_device_command(discharge)
        assert waiting.reason == "group_control_handover_waiting_for_zero"
        for _ in range(5):
            now += timedelta(seconds=15)
            still = await coordinator._execute_device_command(discharge)
            assert still.reason == "group_control_handover_waiting_for_zero"
        now += timedelta(seconds=20)
        writes.clear()
        resumed = await coordinator._execute_device_command(discharge)
        assert resumed.reason == "group_control_active"
        assert sum(command.output_limit_w for _, command in writes) == 1400
        assert coordinator._persist.get("group_control_handover_pending") is None

        # A last known active flow is never treated as a stop confirmation.
        writes.clear()
        states.update({device: SimpleNamespace(
            ac_input_power_w=MeasuredValue.available(0, observed_at=now),
            ac_output_power_w=MeasuredValue.available(700, observed_at=now),
            charge_power_w=MeasuredValue.available(0, observed_at=now),
            discharge_power_w=MeasuredValue.available(700, observed_at=now),
        ) for device in devices})
        waiting = await coordinator._execute_device_command(charge)
        assert waiting.reason == "group_control_handover_waiting_for_zero"
        now += timedelta(seconds=300)
        still = await coordinator._execute_device_command(charge)
        assert still.reason == "group_control_handover_waiting_for_zero"

    asyncio.run(run())


def test_native_cell_voltage_measurements_include_all_systems_when_protection_off() -> None:
    coordinator = ZendureSmartFlowCoordinator.__new__(ZendureSmartFlowCoordinator)
    states = {
        "system-a": SimpleNamespace(
            packs=(SimpleNamespace(cell_min_v=MeasuredValue.available(3.09)),)
        ),
        "system-b": SimpleNamespace(
            packs=(SimpleNamespace(cell_min_v=MeasuredValue.available(3.02)),)
        ),
    }
    coordinator.entry = SimpleNamespace(
        options={
            CONF_EXPERT_MODE_ENABLED: True,
            CONF_CELL_VOLTAGE_PROTECTION_ENABLED: False,
        },
        data={"connection_type": "native"},
    )
    coordinator.entities = SimpleNamespace(lowest_cell_voltage_entities=())
    coordinator.native_zendure = SimpleNamespace(
        control_enabled=False,
        fresh_device_state_map=lambda: states,
        cell_voltage_minimum_by_device=lambda: {
            "system-a": 3.09,
            "system-b": 3.02,
        },
    )

    assert coordinator._get_lowest_cell_voltage_values() == [3.09, 3.02]


def test_cell_voltage_emergency_latches_are_independent_per_system() -> None:
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    coordinator = ZendureSmartFlowCoordinator.__new__(ZendureSmartFlowCoordinator)
    coordinator._persist = {"cell_voltage_emergency_devices": {}}
    coordinator._clock = SimpleNamespace(utc_now=lambda: now)
    coordinator._cell_voltage_protection_enabled = lambda: True
    coordinator._get_setting = lambda _key, default: default

    first = coordinator._update_per_device_cell_voltage_emergencies(
        {"system-a": 3.05, "system-b": 3.25}
    )
    assert first == {"system-a": True, "system-b": False}

    coordinator._clock = SimpleNamespace(
        utc_now=lambda: now + timedelta(minutes=21)
    )
    second = coordinator._update_per_device_cell_voltage_emergencies(
        {"system-a": 3.18, "system-b": 3.25}
    )
    assert second == {"system-a": False, "system-b": False}
