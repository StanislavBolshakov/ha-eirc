"""Sensor platform for EIRC integration."""

from __future__ import annotations
import logging
from datetime import timedelta
from typing import Any
from homeassistant.components.sensor import SensorEntity, SCAN_INTERVAL
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry
from homeassistant.helpers.device_registry import async_get as async_get_device_registry
from .const import DOMAIN
from .api import EIRCApiClient
from homeassistant.helpers.translation import async_get_translations

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=5)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from config entry."""
    client = hass.data[DOMAIN][entry.entry_id]
    selected_accounts = entry.data.get("selected_accounts", [])
    _LOGGER.debug("Selected accounts from config entry: %s", selected_accounts)
    try:
        await client.authenticate()
        accounts = await client.get_accounts()
    except Exception as err:
        _LOGGER.error("Failed to authenticate or fetch accounts after retries: %s", err)
        return
    current_accounts = {
        acc["tenancy"]["register"] for acc in accounts if acc["confirmed"]
    }
    selected_accounts_set = set(selected_accounts)
    _LOGGER.debug("Current confirmed accounts: %s", current_accounts)
    _LOGGER.debug("Selected accounts set: %s", selected_accounts_set)

    entity_registry = async_get_entity_registry(hass)
    device_registry = async_get_device_registry(hass)
    entities_to_remove = []
    devices_to_remove = []

    for entity in entity_registry.entities.values():
        if entity.domain == "sensor" and entity.platform == DOMAIN:
            unique_id_parts = entity.unique_id.split("_")
            if len(unique_id_parts) >= 2:
                tenancy_register = unique_id_parts[0]
                _LOGGER.debug(
                    "Processing entity %s with tenancy register: %s",
                    entity.entity_id,
                    tenancy_register,
                )
                if (
                    tenancy_register not in selected_accounts_set
                    or tenancy_register not in current_accounts
                ):
                    _LOGGER.debug("Marking entity for removal: %s", entity.entity_id)
                    entities_to_remove.append(entity.entity_id)

                    device = device_registry.async_get_device(
                        {(DOMAIN, tenancy_register)}
                    )
                    if device:
                        _LOGGER.debug("Marking device for removal: %s", device.id)
                        devices_to_remove.append(device.id)

    for entity_id in entities_to_remove:
        _LOGGER.info("Removing sensor entity: %s", entity_id)
        entity_registry.async_remove(entity_id)

    for device_id in devices_to_remove:
        device = device_registry.async_get(device_id)
        if device:
            remaining_entities = device_registry.async_entries_for_device(device_id)
            _LOGGER.debug(
                "Device %s has %d remaining entities",
                device_id,
                len(remaining_entities),
            )
            if not remaining_entities:
                _LOGGER.info("Removing device: %s", device_id)
                device_registry.async_remove_device(device_id)
            else:
                _LOGGER.debug(
                    "Device %s still has entities, skipping removal", device_id
                )

    sensors = []
    for acc in accounts:
        if acc["tenancy"]["register"] in selected_accounts_set:
            sensors.append(EIRCSensor(client, acc))
            try:
                meters_info = await client.get_meters_info(acc["id"])
                for meter in meters_info:
                    for indication in meter["indications"]:
                        sensors.append(EIRCMeterSensor(client, acc, meter, indication))
            except Exception as err:
                _LOGGER.error(
                    "Failed to fetch meters info for account %s: %s", acc["id"], err
                )
    _LOGGER.debug("Adding %d new sensors", len(sensors))
    async_add_entities(sensors, True)


class EIRCSensor(SensorEntity):
    """Representation of an EIRC service sensor."""

    _attr_has_entity_name = False
    _attr_translation_key = "eirc_sensor"

    def __init__(self, client: EIRCApiClient, account_data: dict) -> None:
        """Initialize the sensor."""
        self._client = client
        self._account_data = account_data
        self._attr_unique_id = f"eirc_{account_data['alias']}"
        self._attr_name = f"{account_data['alias']}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, account_data["tenancy"]["register"])},
            "name": account_data["alias"],
            "manufacturer": "ЕИРЦ Санкт-Петербург",
        }
        self._attr_native_unit_of_measurement = "RUB"
        _LOGGER.debug(
            "Initialized sensor for account: %s", account_data["tenancy"]["register"]
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return {
            "account_id": self._account_data["id"],
            "tenancy": self._account_data["tenancy"]["register"],
            "service_provider": self._account_data["service"]["providerCode"],
            "delivery_method": self._account_data["delivery"],
            "confirmed": self._account_data["confirmed"],
            "auto_payment": self._account_data["autoPaymentOn"],
        }

    async def async_update(self) -> None:
        """Update sensor data."""
        try:
            accounts = await self._client.get_accounts()
            account = next(
                (
                    a
                    for a in accounts
                    if a["tenancy"]["register"]
                    == self._account_data["tenancy"]["register"]
                ),
                None,
            )
            if not account:
                _LOGGER.warning(
                    "Account %s not found in API response",
                    self._account_data["tenancy"]["register"],
                )
                self._attr_available = False
                return
            self._account_data = account
            self._attr_available = True
            account_id = self._account_data["id"]
            balance = await self._client.get_account_balance(account_id)

            self._attr_native_value = balance
        except Exception as err:
            _LOGGER.error("Error updating sensor: %s", err)
            self._attr_available = False
            raise


class EIRCMeterSensor(SensorEntity):
    """Representation of an EIRC meter sensor."""

    _attr_has_entity_name = False
    _attr_translation_key = "eirc_meter_sensor"

    def __init__(
        self,
        client: EIRCApiClient,
        account_data: dict,
        meter_data: dict,
        indication_data: dict,
    ) -> None:
        """Initialize the sensor."""
        self._client = client
        self._account_data = account_data
        self._meter_data = meter_data
        self._indication_data = indication_data
        self._attr_unique_id = f"eirc_meter_{account_data['alias']}_{meter_data['id']['registration']}_{indication_data['meterScaleId']}"
        if meter_data["subserviceId"] == 54179:
            self._attr_name = f"Электроэнергия {indication_data['scaleName']} ({meter_data['id']['registration']})"
        else:
            self._attr_name = f"{meter_data['name']}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, account_data["tenancy"]["register"])},
            "name": account_data["alias"],
            "manufacturer": "ЕИРЦ Санкт-Петербург",
        }
        self._attr_native_unit_of_measurement = indication_data["unit"]
        self._attr_device_class = "meter"
        _LOGGER.debug(
            "Initialized meter sensor for account: %s, meter: %s, scale: %s",
            account_data["tenancy"]["register"],
            meter_data["id"]["registration"],
            indication_data["scaleName"],
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return {
            "account_id": self._account_data["id"],
            "tenancy": self._account_data["tenancy"]["register"],
            "service_provider": self._account_data["service"]["providerCode"],
            "delivery_method": self._account_data["delivery"],
            "confirmed": self._account_data["confirmed"],
            "auto_payment": self._account_data["autoPaymentOn"],
            "meter_id": self._meter_data["id"]["registration"],
            "meter_name": self._meter_data["name"],
            "scale_name": self._indication_data["scaleName"],
            "scale_id": self._indication_data["meterScaleId"],
            "last_update": self._indication_data["previousReadingDate"],
        }

    @property
    def device_class(self):
        return "meter"

    async def async_update(self) -> None:
        """Update sensor data."""
        try:
            _LOGGER.debug(
                "Updating meter sensor for account: %s, meter: %s, scale: %s",
                self._account_data["tenancy"]["register"],
                self._meter_data["id"]["registration"],
                self._indication_data["scaleName"],
            )

            meters_info = await self._client.get_meters_info(self._account_data["id"])
            meter = next(
                (
                    m
                    for m in meters_info
                    if m["id"]["registration"] == self._meter_data["id"]["registration"]
                ),
                None,
            )
            if not meter:
                _LOGGER.warning(
                    "Meter %s not found in API response",
                    self._meter_data["id"]["registration"],
                )
                self._attr_available = False
                return
            self._meter_data = meter
            self._attr_available = True

            indication = next(
                (
                    i
                    for i in meter["indications"]
                    if i["meterScaleId"] == self._indication_data["meterScaleId"]
                ),
                None,
            )
            if not indication:
                _LOGGER.warning(
                    "Indication %s not found in API response",
                    self._indication_data["meterScaleId"],
                )
                self._attr_available = False
                return
            self._indication_data = indication

            self._attr_native_value = indication["previousReading"]
        except Exception as err:
            _LOGGER.error("Error updating meter sensor: %s", err)
            self._attr_available = False
            raise
