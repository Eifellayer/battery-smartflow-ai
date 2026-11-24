"""Exercise native and portal group options using the shared flow harness."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from test_native_beta2 import const, load_flow_namespace
from custom_components.battery_smartflow_ai import battery_groups, energy_scopes
from custom_components.battery_smartflow_ai.electrical_topology import (
    normalize_electrical_topology,
    save_electrical_topology,
)


class GroupWizardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        namespace = load_flow_namespace()
        namespace.update(
            {
                name: getattr(battery_groups, name)
                for name in (
                    "DEFAULT_GROUP_OBJECTIVE", "DEFAULT_PEAK_IMPORT_LIMIT_W",
                    "DEFAULT_UPS_CHARGE_TARGET_SOC_PCT", "DEFAULT_UPS_RESERVE_SOC_PCT",
                    "GROUP_OBJECTIVES", "GROUP_OBJECTIVE_UPS_PRICE_ARBITRAGE",
                    "migrate_single_device_group", "save_group",
                )
            }
        )
        namespace.update(
            json=json,
            uuid4=uuid4,
            normalize_electrical_topology=normalize_electrical_topology,
            save_electrical_topology=save_electrical_topology,
            normalize_energy_scope_config=energy_scopes.normalize_energy_scope_config,
            save_energy_scope=energy_scopes.save_energy_scope,
            ENERGY_GRID_MODE_NONE=energy_scopes.GRID_MODE_NONE,
            ENERGY_GRID_MODE_SIGNED=energy_scopes.GRID_MODE_SIGNED,
            ENERGY_GRID_MODE_SPLIT=energy_scopes.GRID_MODE_SPLIT,
            PV_SOURCE_AGGREGATE=energy_scopes.PV_SOURCE_AGGREGATE,
            PV_SOURCE_COMPONENTS=energy_scopes.PV_SOURCE_COMPONENTS,
        )
        self.flow = namespace["ZendureSmartFlowOptionsFlow"]()
        self.flow.config_entry = SimpleNamespace(data={}, options={})
        self.flow._group_device_choices = lambda **_: [
            {"value": "hyper", "label": "Hyper 2000"},
            {"value": "solar", "label": "SolarFlow 800 Plus"},
        ]

        # Mock only the HA Energy boundary, never the options-flow helper.
        self.flow.hass = SimpleNamespace()
        self.forecast_provider = AsyncMock(return_value=[
            {"value": "forecast-1", "label": "Forecast.Solar"}
        ])
        namespace["async_energy_forecast_sources"] = self.forecast_provider

    def test_options_flow_defines_its_own_forecast_options(self):
        """The PV source step calls this helper; the config flow's copy is not inherited."""

        flow_class = load_flow_namespace()["ZendureSmartFlowOptionsFlow"]
        self.assertIn("_forecast_options", vars(flow_class))

    async def test_pv_form_uses_real_forecast_options_and_allows_no_circuits(self):
        flow = self.flow
        await flow.async_step_group_create({"name": "Home", "members": ["hyper", "solar"]})
        menu = await flow.async_step_group_wizard_strategy({"control_enabled": True})
        self.assertIn("topology_skip_to_sensors", menu["menu_options"])
        form = await flow.async_step_topology_skip_to_sensors()
        self.assertEqual(form["step_id"], "group_wizard_sensors")
        # Configuring sensors without topology must not relax output checks.
        self.assertEqual(len(flow._missing_group_outputs(flow._group_wizard_topology)), 2)
        await flow.async_step_group_wizard_sensors({
            "scope_name": "Home", "grid_mode": "signed",
            "grid_power_entities": ["sensor.grid"],
        })
        form = await flow.async_step_group_wizard_pv_add()
        self.assertEqual(form["step_id"], "group_wizard_pv_add")
        self.forecast_provider.assert_awaited_once_with(flow.hass)
        self.assertEqual(await flow._forecast_options(), self.forecast_provider.return_value)
        await flow.async_step_group_wizard_pv_add({
            "source_name": "PV", "source_mode": "components",
            "power_entities": ["sensor.pv_1", "sensor.pv_2"],
            "forecast_entry_ids": ["forecast-1"],
        })
        saved = await flow.async_step_group_wizard_save()
        scope = next(iter(saved["data"][const.CONF_ENERGY_SCOPES]["scopes"].values()))
        self.assertEqual(scope["pv_sources"][0]["forecast_entry_ids"], ["forecast-1"])
        self.assertFalse(saved["data"][const.CONF_ELECTRICAL_TOPOLOGY]["circuits"])

    async def test_ups_cannot_skip_circuits(self):
        flow = self.flow
        await flow.async_step_group_create({"name": "UPS", "members": ["hyper"]})
        menu = await flow.async_step_group_wizard_strategy({
            "control_enabled": True, "objective": battery_groups.GROUP_OBJECTIVE_UPS_PRICE_ARBITRAGE,
        })
        self.assertNotIn("topology_skip_to_sensors", menu["menu_options"])
        self.assertIn("topology_continue_domains", menu["menu_options"])

    async def test_group_creation_preserves_pv_and_checks_both_outputs(self):
        flow = self.flow
        menu = await flow.async_step_init()
        self.assertIn("systems_groups", menu["menu_options"])
        await flow.async_step_group_create({"name": "Home", "members": ["hyper", "solar"]})
        await flow.async_step_group_wizard_strategy({"control_enabled": True})
        await flow.async_step_topology_circuit_add({"name": "Test", "phase": "l1"})
        topology = flow._electrical_topology_config()
        circuit = next(iter(topology["circuits"]))

        incomplete = await flow.async_step_topology_save()
        self.assertNotIn("topology_save", incomplete["menu_options"])
        self.assertEqual(incomplete["description_placeholders"]["missing_outputs"], "2")
        for device_id in ("hyper", "solar"):
            await flow.async_step_topology_connection_add(
                {"output_id": "grid", "kind": "grid", "device_id": device_id, "circuit_id": circuit}
            )
        await flow.async_step_topology_save()

        missing_grid = await flow.async_step_group_wizard_sensors({
            "scope_name": "Home", "grid_mode": "none",
        })
        self.assertEqual(missing_grid["errors"], {"base": "invalid_energy_scope"})

        await flow.async_step_group_wizard_sensors({
            "scope_name": "Home", "grid_mode": "signed",
            "grid_power_entities": ["sensor.grid"],
            "grid_import_entities": ["sensor.old_import"],
        })
        source = {"source_name": "PV", "source_mode": "aggregate",
                  "power_entities": ["sensor.pv_1", "sensor.pv_2"]}
        invalid_pv = await flow.async_step_group_wizard_pv_add(source)
        self.assertEqual(invalid_pv["errors"], {"power_entities": "group_pv_aggregate_one_sensor"})
        await flow.async_step_group_wizard_pv_add({**source, "source_mode": "components"})
        duplicate = await flow.async_step_group_wizard_pv_add({
            **source, "source_name": "PV 2", "source_mode": "components",
        })
        self.assertEqual(duplicate["errors"], {"base": "group_pv_source_conflict"})
        self.assertEqual(duplicate["description_placeholders"]["conflicting_source"], "PV")

        await flow.async_step_group_wizard_back_sensors()
        await flow.async_step_group_wizard_sensors({
            "scope_name": "Home", "grid_mode": "split",
            "grid_import_entities": ["sensor.import"], "grid_export_entities": ["sensor.export"],
            "grid_power_entities": ["sensor.grid"],
        })
        saved = await flow.async_step_group_wizard_save()
        options = saved["data"]
        scope = next(iter(options[const.CONF_ENERGY_SCOPES]["scopes"].values()))
        self.assertEqual(scope["grid_power_entities"], [])
        self.assertEqual(scope["grid_mode"], "split")
        self.assertEqual(scope["pv_sources"][0]["power_entities"], source["power_entities"])
        group = next(iter(options[const.CONF_BATTERY_GROUPS]["groups"].values()))
        self.assertEqual(group["members"], ["hyper", "solar"])
        self.assertTrue(group["control_enabled"])
