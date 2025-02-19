from __future__ import annotations
import logging
from datetime import timedelta
from typing import Any
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    CoordinatorEntity,
)
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from .const import DOMAIN
from .api import EIRCApiClient

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(minutes=15)

EIRC_PREFIX = "eirc_"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from config entry."""
    client = hass.data[DOMAIN][entry.entry_id]
    selected_tenancies = entry.data.get("selected_accounts", [])
    coordinator = EIRCDataUpdateCoordinator(hass, client, SCAN_INTERVAL)
    await coordinator.async_config_entry_first_refresh()

    entity_registry = async_get_entity_registry(hass)
    device_registry = async_get_device_registry(hass)

    active_tenancies = {acc["tenancy"]["register"] for acc in coordinator.data.values()}

    _cleanup_orphaned_devices(device_registry, active_tenancies)
    _cleanup_orphaned_entities(entity_registry, active_tenancies)

    sensors = [
        EIRCSensor(coordinator, account_data)
        for account_data in coordinator.data.values()
        if account_data["tenancy"]["register"] in selected_tenancies
    ]

    for account_data in coordinator.data.values():
        tenancy_id = account_data["tenancy"]["register"]
        if tenancy_id not in selected_tenancies:
            continue

        device_id = next(
            (
                device.id
                for device in device_registry.devices.values()
                if (DOMAIN, tenancy_id) in device.identifiers
            ),
            None,
        )
        if device_id and device_registry.async_get(device_id).disabled:
            _LOGGER.debug("Skipping disabled device: %s", device_id)
            continue

        for meter in account_data.get("meters", []):
            for indication in meter["indications"]:
                unique_id = f"{EIRC_PREFIX}meter_{tenancy_id}_{meter['id']['registration']}_{indication['meterScaleId']}"
                entity = entity_registry.async_get_entity_id(
                    "sensor", DOMAIN, unique_id
                )
                if entity and entity_registry.entities[entity].disabled:
                    _LOGGER.debug("Skipping disabled entity: %s", unique_id)
                    continue
                sensors.append(
                    EIRCMeterSensor(coordinator, account_data, meter, indication)
                )

    _LOGGER.debug("Adding %d new sensors", len(sensors))
    async_add_entities(sensors, True)


def _cleanup_orphaned_devices(device_registry, active_tenancies):
    """Remove devices that are no longer active."""
    for device in list(device_registry.devices.values()):
        for identifier in device.identifiers:
            if identifier[0] == DOMAIN and identifier[1] not in active_tenancies:
                _LOGGER.debug("Removing device: %s", identifier[1])
                device_registry.async_remove_device(device.id)


def _cleanup_orphaned_entities(entity_registry, active_tenancies):
    """Remove orphaned entities from the registry."""
    for entity in list(entity_registry.entities.values()):
        if entity.platform != DOMAIN:
            continue
        if entity.unique_id.startswith(EIRC_PREFIX) and not any(
            identifier[1] == entity.unique_id.split("_")[1]
            for identifier in active_tenancies
        ):
            _LOGGER.debug("Removing orphaned entity: %s", entity.unique_id)
            entity_registry.async_remove(entity.entity_id)


class EIRCDataUpdateCoordinator(DataUpdateCoordinator):
    """Manager for receiving data from EIRC API."""

    def __init__(
        self, hass: HomeAssistant, client: EIRCApiClient, scan_interval: timedelta
    ):
        """Initialize the coordinator."""
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=scan_interval)
        self._client = client

    async def _async_update_data(self):
        """Fetch data from API."""
        try:
            await self._client.authenticate()
            accounts = await self._client.get_accounts()
            for account in accounts:
                account["meters"] = await self._client.get_meters_info(account["id"])
                account["balance"] = await self._client.get_account_balance(
                    account["id"]
                )
            _LOGGER.debug("Updated data: %s", accounts)
            return {
                acc["tenancy"]["register"]: acc for acc in accounts if acc["confirmed"]
            }
        except Exception as err:
            _LOGGER.error("Error fetching data: %s", err)
            raise


class EIRCSensor(CoordinatorEntity, SensorEntity):
    """Representation of an EIRC account balance sensor."""

    _attr_has_entity_name = False
    _attr_translation_key = "eirc_sensor"

    def __init__(
        self, coordinator: EIRCDataUpdateCoordinator, account_data: dict
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._account_data = account_data
        self._attr_unique_id = f"eirc_{account_data['tenancy']['register']}"
        self._attr_name = account_data.get("alias", "Unknown Account")
        self._attr_device_info = {
            "identifiers": {(DOMAIN, account_data["tenancy"]["register"])},
            "name": account_data.get("alias", "Unknown Account"),
            "manufacturer": "ЕИРЦ Санкт-Петербург",
        }

    @property
    def native_value(self):
        """Return the current account balance."""
        account_data = self.coordinator.data.get(
            self._account_data["tenancy"]["register"], {}
        )
        return account_data.get("balance")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return {
            "account_id": self._account_data.get("id"),
            "tenancy": self._account_data.get("tenancy", {}).get("register"),
            "service_provider": self._account_data.get("service", {}).get(
                "providerCode"
            ),
            "delivery_method": self._account_data.get("delivery"),
            "confirmed": self._account_data.get("confirmed"),
            "auto_payment": self._account_data.get("autoPaymentOn"),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._handle_coordinator_update()

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._account_data = self.coordinator.data.get(
            self._account_data["tenancy"]["register"], {}
        )
        _LOGGER.debug("Updated account data: %s", self._account_data)
        self.async_write_ha_state()


class EIRCMeterSensor(CoordinatorEntity, SensorEntity):
    """Representation of an EIRC meter sensor."""

    _attr_has_entity_name = False
    _attr_translation_key = "eirc_meter_sensor"

    def __init__(
        self,
        coordinator: EIRCDataUpdateCoordinator,
        account_data: dict,
        meter_data: dict,
        indication_data: dict,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        self._account_data = account_data
        self._meter_data = meter_data
        self._indication_data = indication_data

        self._attr_unique_id = (
            f"eirc_meter_{account_data['tenancy']['register']}_"
            f"{meter_data['id']['registration']}_{indication_data['meterScaleId']}"
        )

        if meter_data.get("subserviceId") == 54179:
            self._attr_name = (
                f"Электроэнергия {indication_data.get('scaleName', 'Unknown')} "
                f"({meter_data['id']['registration']})"
            )
        else:
            self._attr_name = meter_data.get("name", "Unknown Meter")

        self._attr_device_info = {
            "identifiers": {(DOMAIN, account_data["tenancy"]["register"])},
            "name": account_data.get("alias", "Unknown Account"),
            "manufacturer": "ЕИРЦ Санкт-Петербург",
        }
        self._attr_native_unit_of_measurement = indication_data.get("unit")

    @property
    def native_value(self):
        """Return the meter reading value."""
        account_data = self.coordinator.data.get(
            self._account_data["tenancy"]["register"], {}
        )
        meters = account_data.get("meters", [])
        for meter in meters:
            if meter["id"]["registration"] == self._meter_data["id"]["registration"]:
                for indication in meter.get("indications", []):
                    if (
                        indication["meterScaleId"]
                        == self._indication_data["meterScaleId"]
                    ):
                        return indication.get("previousReading")
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return {
            "account_id": self._account_data.get("id"),
            "tenancy": self._account_data.get("tenancy", {}).get("register"),
            "service_provider": self._account_data.get("service", {}).get(
                "providerCode"
            ),
            "delivery_method": self._account_data.get("delivery"),
            "confirmed": self._account_data.get("confirmed"),
            "auto_payment": self._account_data.get("autoPaymentOn"),
            "meter_id": self._meter_data.get("id", {}).get("registration"),
            "meter_name": self._meter_data.get("name"),
            "scale_name": self._indication_data.get("scaleName"),
            "scale_id": self._indication_data.get("meterScaleId"),
            "last_update": self._indication_data.get("previousReadingDate"),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._handle_coordinator_update()

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        account_data = self.coordinator.data.get(
            self._account_data["tenancy"]["register"], {}
        )
        if not account_data:
            return

        self._account_data = account_data

        meters = account_data.get("meters", [])
        for meter in meters:
            if meter["id"]["registration"] == self._meter_data["id"]["registration"]:
                self._meter_data = meter
                for indication in meter.get("indications", []):
                    if (
                        indication["meterScaleId"]
                        == self._indication_data["meterScaleId"]
                    ):
                        self._indication_data = indication
                        break
                break
        _LOGGER.debug("Updated sensor data: %s", self._account_data)
        self.async_write_ha_state()
