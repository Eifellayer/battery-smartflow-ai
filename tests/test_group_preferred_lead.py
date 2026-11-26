"""A preferred group member leads whenever it can deliver power."""

from __future__ import annotations

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

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.battery_smartflow_ai.battery_groups import (
    PREFERRED_LEAD_AUTO,
    reconcile_group_config,
    save_group,
)
from custom_components.battery_smartflow_ai.coordinator import (
    ZendureSmartFlowCoordinator,
)

HYPER = "hyper"
SF800 = "sf800"


def _coordinator(now: datetime, lead_state=None) -> ZendureSmartFlowCoordinator:
    coordinator = ZendureSmartFlowCoordinator.__new__(ZendureSmartFlowCoordinator)
    coordinator._clock = SimpleNamespace(utc_now=lambda: now)
    coordinator._persist = {}
    if lead_state is not None:
        coordinator._persist["group_control_lead_state"] = lead_state
    return coordinator


def _allocation(lead: str, limits: dict[str, float]):
    return SimpleNamespace(
        lead_device_id=lead,
        devices=[
            {"device_id": device, "effective_limit_w": limit}
            for device, limit in limits.items()
        ],
    )


def _lead(coordinator, *, natural: str, limits, preferred, socs=None, target=-400.0):
    socs = socs or {HYPER: 89, SF800: 98}
    return coordinator._stable_group_lead_device_id(
        group_id="home",
        target_w=target,
        natural_allocation=_allocation(natural, limits),
        snapshots_by_id={key: {"soc_pct": value} for key, value in socs.items()},
        preferred_lead=preferred,
    )


def test_preferred_lead_replaces_a_retained_current_lead() -> None:
    now = datetime(2026, 11, 26, 13, 0, tzinfo=timezone.utc)
    retained = {
        "group_id": "home",
        "device_id": HYPER,
        "selected_at": (now - timedelta(hours=1)).isoformat(),
        "last_active_at": now.isoformat(),
    }
    limits = {HYPER: 1200.0, SF800: 800.0}

    # Without a preference the SoC hysteresis keeps the Hyper.
    assert _lead(_coordinator(now, dict(retained)), natural=SF800,
                 limits=limits, preferred=None) == HYPER

    coordinator = _coordinator(now, dict(retained))
    assert _lead(coordinator, natural=HYPER, limits=limits, preferred=SF800) == SF800
    assert coordinator._persist["group_control_lead_state"]["device_id"] == SF800
    # Charging follows the same preference.
    assert _lead(coordinator, natural=HYPER, limits=limits,
                 preferred=SF800, target=600.0) == SF800


def test_preferred_lead_yields_while_it_cannot_deliver() -> None:
    now = datetime(2026, 11, 26, 13, 0, tzinfo=timezone.utc)
    coordinator = _coordinator(now, {
        "group_id": "home", "device_id": SF800,
        "selected_at": now.isoformat(), "last_active_at": now.isoformat(),
    })
    # Reserve, full battery or an economic limit of 0 W remove the device.
    blocked = {HYPER: 1200.0, SF800: 0.0}
    assert _lead(coordinator, natural=HYPER, limits=blocked, preferred=SF800) == HYPER

    now += timedelta(seconds=5)
    coordinator._clock = SimpleNamespace(utc_now=lambda: now)
    assert _lead(coordinator, natural=HYPER, limits={HYPER: 1200.0, SF800: 800.0},
                 preferred=SF800) == SF800


def test_group_storage_keeps_preferred_lead_only_for_members() -> None:
    config = save_group(
        {}, group_id="home", name="Hausspeicher",
        members=[HYPER, SF800], preferred_lead=SF800,
    )
    assert config["groups"]["home"]["preferred_lead"] == SF800

    # None keeps the stored preference, "auto" clears it.
    kept = save_group(config, group_id="home", name="Hausspeicher", members=[HYPER, SF800])
    assert kept["groups"]["home"]["preferred_lead"] == SF800
    cleared = save_group(
        config, group_id="home", name="Hausspeicher",
        members=[HYPER, SF800], preferred_lead=PREFERRED_LEAD_AUTO,
    )
    assert cleared["groups"]["home"]["preferred_lead"] is None

    # Removing the device from the group drops the preference.
    removed = save_group(config, group_id="home", name="Hausspeicher", members=[HYPER])
    assert removed["groups"]["home"]["preferred_lead"] is None

    # Groups stored before this option existed stay automatic.
    legacy = reconcile_group_config(
        {"groups": {"home": {"name": "Alt", "members": [HYPER, SF800]}}}
    )
    assert legacy["groups"]["home"]["preferred_lead"] is None
