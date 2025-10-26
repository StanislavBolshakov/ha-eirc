"""Sensor platform for the EIRC integration."""

from __future__ import annotations
import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EircDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

EIRC_PREFIX = "eirc_"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EIRC sensors from a config entry."""
    coordinator: EircDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    selected_tenancies = entry.data.get("selected_accounts", [])

    if not coordinator.data:
        _LOGGER.warning("No data from coordinator; skipping sensor setup.")
        return

    sensors_to_add = []

    for account_data in coordinator.data.values():
        tenancy_id = account_data["tenancy"]["register"]
        if tenancy_id not in selected_tenancies:
            continue

        sensors_to_add.append(EIRCSensor(coordinator, account_data))

        for meter in account_data.get("meters", []):
            for indication in meter["indications"]:
                sensors_to_add.append(
                    EIRCMeterSensor(coordinator, account_data, meter, indication)
                )

    _LOGGER.debug("Adding %d EIRC sensors", len(sensors_to_add))
    async_add_entities(sensors_to_add)


class EIRCSensor(CoordinatorEntity, SensorEntity):
    """Representation of an EIRC account balance sensor."""

    _attr_has_entity_name = False

    def __init__(
        self, coordinator: EircDataUpdateCoordinator, account_data: dict
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._tenancy_id = account_data["tenancy"]["register"]
        self._attr_unique_id = f"eirc_{self._tenancy_id}"
        self._attr_name = account_data.get("alias", f"EIRC {self._tenancy_id}")
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._tenancy_id)},
            "name": account_data.get("alias", f"EIRC {self._tenancy_id}"),
            "manufacturer": "ЕИРЦ Санкт-Петербург",
        }

    @property
    def native_value(self):
        """Return the current account balance."""
        account_data = self.coordinator.data.get(self._tenancy_id, {})
        return account_data.get("balance")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        account_data = self.coordinator.data.get(self._tenancy_id, {})
        if not account_data:
            return {}
        return {
            "account_id": account_data.get("id"),
            "tenancy": account_data.get("tenancy", {}).get("register"),
            "service_provider": account_data.get("service", {}).get("providerCode"),
            "delivery_method": account_data.get("delivery"),
            "confirmed": account_data.get("confirmed"),
            "auto_payment": account_data.get("autoPaymentOn"),
        }


class EIRCMeterSensor(CoordinatorEntity, SensorEntity):
    """Representation of an EIRC meter sensor."""

    _attr_has_entity_name = False

    def __init__(
        self,
        coordinator: EircDataUpdateCoordinator,
        account_data: dict,
        meter_data: dict,
        indication_data: dict,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        self._tenancy_id = account_data["tenancy"]["register"]
        self._meter_reg_id = meter_data["id"]["registration"]
        self._scale_id = indication_data["meterScaleId"]

        self._attr_unique_id = (
            f"eirc_meter_{self._tenancy_id}_{self._meter_reg_id}_{self._scale_id}"
        )

        if meter_data.get("subserviceId") == 54179:
            self._attr_name = (
                f"Электроэнергия {indication_data.get('scaleName', 'Unknown')} "
                f"({meter_data['id']['registration']})"
            )
        else:
            self._attr_name = meter_data.get("name", "Unknown Meter")

        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._tenancy_id)},
        }
        self._attr_native_unit_of_measurement = indication_data.get("unit")

    def _get_current_data(self) -> tuple[dict | None, dict | None, dict | None]:
        """Get the current data sets from the coordinator."""
        account_data = self.coordinator.data.get(self._tenancy_id)
        if not account_data:
            return None, None, None

        for meter in account_data.get("meters", []):
            if meter["id"]["registration"] == self._meter_reg_id:
                for indication in meter.get("indications", []):
                    if indication["meterScaleId"] == self._scale_id:
                        return account_data, meter, indication
        return account_data, None, None

    @property
    def native_value(self):
        """Return the meter reading value."""
        _, _, indication = self._get_current_data()
        return indication.get("previousReading") if indication else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes, restoring all original fields."""
        account_data, meter_data, indication_data = self._get_current_data()

        if not account_data or not meter_data or not indication_data:
            return {}

        return {
            "account_id": account_data.get("id"),
            "tenancy": account_data.get("tenancy", {}).get("register"),
            "service_provider": account_data.get("service", {}).get("providerCode"),
            "delivery_method": account_data.get("delivery"),
            "confirmed": account_data.get("confirmed"),
            "auto_payment": account_data.get("autoPaymentOn"),
            "meter_id": meter_data.get("id", {}).get("registration"),
            "meter_name": meter_data.get("name"),
            "scale_name": indication_data.get("scaleName"),
            "scale_id": indication_data.get("meterScaleId"),
            "last_update": indication_data.get("previousReadingDate"),
        }
