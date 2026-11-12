"""Small HA stand-ins for unit tests; no catalog, credentials or network fixtures."""

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch


class HomeAssistantError(Exception):
    """Stand-in for HA errors."""


class ConfigEntryAuthFailed(HomeAssistantError):
    """Stand-in for HA authentication failures."""


class UpdateFailed(HomeAssistantError):
    """Stand-in for failed refreshes."""


class DataUpdateCoordinator:
    def __init__(self, hass, logger, **kwargs):
        self.hass = hass
        self.config_entry = kwargs.get("config_entry")
        self.data = None
        self.last_update_success = True
        self.listeners = []

    def async_add_listener(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)


class CoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    @property
    def available(self):
        return self.coordinator.last_update_success


class SensorEntity:
    @property
    def unique_id(self):
        return self._attr_unique_id


def load_modules():
    """Import production modules without executing integration setup or installing HA."""
    exports = {
        "homeassistant": {},
        "homeassistant.core": {"HomeAssistant": object, "callback": lambda f: f},
        "homeassistant.config_entries": {"ConfigEntry": object},
        "homeassistant.exceptions": {
            "HomeAssistantError": HomeAssistantError,
            "ConfigEntryAuthFailed": ConfigEntryAuthFailed,
        },
        "homeassistant.helpers": {},
        "homeassistant.helpers.aiohttp_client": {
            "async_get_clientsession": lambda hass: hass.session,
        },
        "homeassistant.helpers.update_coordinator": {
            "DataUpdateCoordinator": DataUpdateCoordinator,
            "CoordinatorEntity": CoordinatorEntity,
            "UpdateFailed": UpdateFailed,
        },
        "homeassistant.helpers.entity_platform": {"AddEntitiesCallback": object},
        "homeassistant.components": {},
        "homeassistant.components.sensor": {"SensorEntity": SensorEntity},
    }
    modules = {}
    for name, attrs in exports.items():
        module = ModuleType(name)
        module.__dict__.update(attrs)
        modules[name] = module
    package = ModuleType("eirc_under_test")
    package.__path__ = [
        str(Path(__file__).resolve().parents[1] / "custom_components" / "eirc")
    ]
    modules[package.__name__] = package
    with patch.dict(sys.modules, modules):
        return tuple(
            importlib.import_module(f"eirc_under_test.{name}")
            for name in ("api", "coordinator", "sensor")
        )


api, coordinator, sensor = load_modules()


def make_coordinator(selected=None):
    data = {"username": "test-user", "password": "test-password"}
    if selected is not None:
        data["selected_accounts"] = selected
    hass = SimpleNamespace(config_entries=MagicMock())
    entry = SimpleNamespace(data=data, options={}, async_on_unload=MagicMock())
    result = coordinator.EircDataUpdateCoordinator(hass, entry)
    return result


def account(number=101, tenancy="test-tenancy-a"):
    return {
        "id": number,
        "tenancy": {"register": tenancy},
        "confirmed": True,
        "alias": "Synthetic account",
    }


def meters():
    return [
        {
            "id": {"registration": "test-meter/a", "provider": 1},
            "name": "Synthetic water meter",
            "indications": [
                {
                    "meterScaleId": 1,
                    "previousReading": 100,
                    "previousReadingDate": "2026-01-01",
                    "unit": "m³",
                    "scaleName": "test-scale",
                }
            ],
            "extraApiField": {"preserved": True},
        }
    ]
