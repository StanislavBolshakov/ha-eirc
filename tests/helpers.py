"""Small HA stand-ins for unit tests; no catalog, credentials or network fixtures."""

import importlib
import sys
from enum import StrEnum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch


class HomeAssistantError(Exception):
    """Stand-in for HA errors."""

    def __init__(
        self,
        message="",
        *,
        translation_domain=None,
        translation_key=None,
        translation_placeholders=None,
    ):
        super().__init__(message or translation_key)
        self.translation_domain = translation_domain
        self.translation_key = translation_key
        self.translation_placeholders = translation_placeholders


class ConfigEntryAuthFailed(HomeAssistantError):
    """Stand-in for HA authentication failures."""


class UpdateFailed(HomeAssistantError):
    """Stand-in for failed refreshes."""


class ServiceValidationError(HomeAssistantError):
    """Stand-in for an actionable service error."""


class EntityCategory(StrEnum):
    DIAGNOSTIC = "diagnostic"


class SensorDeviceClass(StrEnum):
    ENUM = "enum"
    TIMESTAMP = "timestamp"


class ButtonDeviceClass(StrEnum):
    UPDATE = "update"


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

    def async_update_listeners(self):
        for listener in list(self.listeners):
            listener()


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


async def _async_get_integration(hass, domain):
    return SimpleNamespace(version="1.2.5")


def load_modules():
    """Import production modules without executing integration setup or installing HA."""
    exports = {
        "homeassistant": {},
        "homeassistant.core": {"HomeAssistant": object, "callback": lambda f: f},
        "homeassistant.config_entries": {"ConfigEntry": object},
        "homeassistant.const": {
            "EntityCategory": EntityCategory,
            "__version__": "2026.11.1",
        },
        "homeassistant.loader": {"async_get_integration": _async_get_integration},
        "homeassistant.exceptions": {
            "HomeAssistantError": HomeAssistantError,
            "ConfigEntryAuthFailed": ConfigEntryAuthFailed,
            "ServiceValidationError": ServiceValidationError,
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
        "homeassistant.components.sensor": {
            "SensorEntity": SensorEntity,
            "SensorDeviceClass": SensorDeviceClass,
        },
        "homeassistant.components.button": {
            "ButtonEntity": SensorEntity,
            "ButtonDeviceClass": ButtonDeviceClass,
        },
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
            for name in ("api", "coordinator", "sensor", "button", "diagnostics")
        )


api, coordinator, sensor, button, diagnostics = load_modules()


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
