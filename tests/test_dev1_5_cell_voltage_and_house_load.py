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
    SETTING_MAX_DISCHARGE,
)
from custom_components.battery_smartflow_ai.core.models import (
    CommandExecutionResult,
    CommandExecutionStatus,
    DeviceCommand,
)
from custom_components.battery_smartflow_ai.coordinator import (
    ZendureSmartFlowCoordinator,
    _native_battery_power_totals,
)

# The coordinator only needs the Store adapter as a type here. Let the store
# contract tests install their own controllable FakeHAStore implementation.
sys.modules.pop("custom_components.battery_smartflow_ai.adapters.home_assistant.state_store", None)
sys.modules.pop("homeassistant.helpers.storage", None)
from custom_components.battery_smartflow_ai.core.models.states import (
    MeasuredValue,
    ValueValidity,
)


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
        options[SETTING_MAX_DISCHARGE] = 2000
        demand = DeviceCommand("output", output_limit_w=2500)
        status, targets = coordinator._productive_group_targets(
            demand, coordinator.native_zendure
        )
        assert status == "group_control_active"
        assert {device: cmd.output_limit_w for device, cmd in targets.items()} == devices

        options[SETTING_MAX_DISCHARGE] = 1300
        _, targets = coordinator._productive_group_targets(demand, coordinator.native_zendure)
        assert sum(cmd.output_limit_w for cmd in targets.values()) == 1300
        assert coordinator._native_group_power_limit_w(
            coordinator.native_zendure, setting=SETTING_MAX_DISCHARGE,
            selected_limit_w=1200, reserve_soc_pct=20, maximum_soc_pct=95,
        ) == 1300

        options.pop(SETTING_MAX_DISCHARGE)
        circuit["max_power_w"] = 1400
        started = await coordinator._execute_device_command(
            DeviceCommand("output", output_limit_w=1700)
        )
        assert started.reason == "group_control_active"
        assert {device for device, _ in writes} == set(devices)
        assert sum(command.output_limit_w for _, command in writes) == 1400
        assert all(0 < command.output_limit_w <= devices[device] for device, command in writes)

        writes.clear()
        charge = DeviceCommand("input", input_limit_w=1400)
        waiting = await coordinator._execute_device_command(charge)
        assert waiting.reason == "group_control_handover_waiting_for_zero"
        assert {device for device, _ in writes} == set(devices)
        assert all(command.input_limit_w == command.output_limit_w == 0 for _, command in writes)

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
