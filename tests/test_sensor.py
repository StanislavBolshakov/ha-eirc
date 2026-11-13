"""Freeze existing identifiers/attributes and cover recovery after startup."""

import unittest
from unittest.mock import MagicMock

from helpers import account, make_coordinator, meters, sensor


class SensorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.coordinator = make_coordinator(selected=["test-tenancy-a"])
        self.data = {
            **account(),
            "meters": meters(),
            "balance": 12.5,
            "bill_id": "test-bill",
        }
        self.coordinator.data = {"test-tenancy-a": self.data}

    def meter_entity(self):
        meter = self.data["meters"][0]
        return sensor.EIRCMeterSensor(
            self.coordinator, self.data, meter, meter["indications"][0]
        )

    async def test_existing_meter_id_value_name_unit_and_attributes_are_unchanged(self):
        entity = self.meter_entity()
        self.assertEqual(entity.unique_id, "eirc_meter_test-tenancy-a_test-meter/a_1")
        self.assertEqual(entity.native_value, 100)
        self.assertEqual(entity._attr_name, "Synthetic water meter")
        self.assertEqual(entity._attr_native_unit_of_measurement, "m³")
        self.assertEqual(
            entity.extra_state_attributes,
            {
                "account_id": 101,
                "tenancy": "test-tenancy-a",
                "service_provider": None,
                "delivery_method": None,
                "confirmed": True,
                "auto_payment": None,
                "meter_id": "test-meter/a",
                "meter_name": "Synthetic water meter",
                "scale_name": "test-scale",
                "scale_id": 1,
                "last_update": "2026-01-01",
            },
        )

    async def test_electricity_name_and_tariff_ids_are_unchanged(self):
        meter = self.data["meters"][0]
        meter["subservice"] = {"utility": "ELECTRICITY"}
        meter["indications"][0]["unit"] = "kWh"
        entity = self.meter_entity()
        self.assertEqual(entity._attr_name, "Электроэнергия test-scale (test-meter/a)")
        self.assertEqual(entity._attr_native_unit_of_measurement, "kWh")
        self.assertEqual(entity.unique_id, "eirc_meter_test-tenancy-a_test-meter/a_1")
        self.assertEqual(entity.extra_state_attributes["scale_id"], 1)

    async def test_balance_default_icon_does_not_change_identity_or_value(self):
        entity = sensor.EIRCSensor(self.coordinator, self.data)
        self.assertEqual(entity._attr_icon, "mdi:cash")
        self.assertEqual(entity.unique_id, "eirc_test-tenancy-a")
        self.assertEqual(entity.native_value, 12.5)

    async def test_meter_icons_follow_service_without_changing_meter_data(self):
        for subservice, expected in [
            ({"utility": "ELECTRICITY"}, "mdi:lightning-bolt"),
            ({"utility": "GAS"}, "mdi:fire"),
            ({"utility": "HEATING"}, "mdi:radiator"),
            ({"utility": "WATER", "name": "Холодное водоснабжение"}, "mdi:water"),
            (
                {"utility": "WATER", "name": "Горячее водоснабжение"},
                "mdi:water-thermometer",
            ),
            (
                {"utility": "WATER", "name": "  гВс (тестовая услуга) "},
                "mdi:water-thermometer",
            ),
            ({"utility": "WATER", "name": None}, "mdi:water"),
            ({"utility": "WATER"}, "mdi:water"),
            ({"utility": "UNKNOWN"}, "mdi:counter"),
            ({"utility": "FUTURE_SERVICE"}, "mdi:counter"),
            ({}, "mdi:counter"),
            (None, "mdi:counter"),
        ]:
            with self.subTest(subservice=subservice):
                meter = self.data["meters"][0]
                meter["subservice"] = subservice
                before = dict(meter)
                entity = self.meter_entity()
                self.assertEqual(entity._attr_icon, expected)
                self.assertEqual(
                    entity.unique_id, "eirc_meter_test-tenancy-a_test-meter/a_1"
                )
                self.assertEqual(entity.native_value, 100)
                self.assertEqual(entity.extra_state_attributes["scale_id"], 1)
                self.assertEqual(meter, before)
                self.assertNotIn("_attr_device_class", vars(entity))

    async def test_removed_meter_is_unavailable(self):
        entity = self.meter_entity()
        self.data["meters"] = []
        self.assertFalse(entity.available)
        self.assertIsNone(entity.native_value)

    async def test_balance_is_unavailable_when_bill_failed(self):
        entity = sensor.EIRCSensor(self.coordinator, self.data)
        self.assertTrue(entity.available)
        self.coordinator.failed_balances.add("test-tenancy-a")
        self.assertFalse(entity.available)
        self.assertEqual(entity.unique_id, "eirc_test-tenancy-a")

    async def test_global_update_failure_marks_all_sensors_unavailable(self):
        self.coordinator.last_update_success = False
        self.assertFalse(self.meter_entity().available)
        self.assertFalse(sensor.EIRCSensor(self.coordinator, self.data).available)

    async def test_meter_recovery_after_startup_adds_entities_once(self):
        self.data["meters"] = []
        entry = self.coordinator.config_entry
        hass = MagicMock()
        entry.entry_id = "test-entry"
        hass.data = {"eirc": {entry.entry_id: self.coordinator}}
        add_entities = MagicMock()
        await sensor.async_setup_entry(hass, entry, add_entities)
        self.assertEqual(len(add_entities.call_args.args[0]), 4)
        self.data["meters"] = meters()
        listener = self.coordinator.listeners[0]
        listener()
        self.assertEqual(len(add_entities.call_args.args[0]), 1)
        self.assertIsInstance(add_entities.call_args.args[0][0], sensor.EIRCMeterSensor)
        listener()
        self.assertEqual(add_entities.call_count, 2)
        entry.async_on_unload.assert_called_once()
