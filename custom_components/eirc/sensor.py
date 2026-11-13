"""Sensor platform for the EIRC integration."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EircDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

EIRC_PREFIX = "eirc_"

METER_ICONS = {
    "ELECTRICITY": "mdi:lightning-bolt",
    "WATER": "mdi:water",
    "HEATING": "mdi:radiator",
    "GAS": "mdi:fire",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EIRC sensors from a config entry."""
    coordinator: EircDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    selected_tenancies = entry.options.get(
        "selected_accounts", entry.data.get("selected_accounts", [])
    )

    known_ids = set()

    @callback
    def add_new_sensors() -> None:
        """Also discover meters which recover after a failed first refresh."""
        sensors_to_add = []
        for account_data in (coordinator.data or {}).values():
            tenancy_id = account_data["tenancy"]["register"]
            if tenancy_id not in selected_tenancies:
                continue

            candidates = [EIRCSensor(coordinator, account_data)]
            for meter in account_data.get("meters", []):
                for indication in meter["indications"]:
                    candidates.append(
                        EIRCMeterSensor(coordinator, account_data, meter, indication)
                    )
            candidates.extend(
                [
                    EIRCUpdateStatusSensor(coordinator, account_data),
                    EIRCLastSuccessSensor(coordinator, account_data, "meters"),
                    EIRCLastSuccessSensor(coordinator, account_data, "balance"),
                ]
            )
            for sensor in candidates:
                if sensor.unique_id not in known_ids:
                    known_ids.add(sensor.unique_id)
                    sensors_to_add.append(sensor)

        if sensors_to_add:
            _LOGGER.debug("Adding %d EIRC sensors", len(sensors_to_add))
            async_add_entities(sensors_to_add)

    add_new_sensors()
    entry.async_on_unload(coordinator.async_add_listener(add_new_sensors))


class EIRCSensor(CoordinatorEntity, SensorEntity):
    """Representation of an EIRC account balance sensor."""

    _attr_has_entity_name = False
    _attr_translation_key = "eirc_sensor"
    _attr_icon = "mdi:cash"

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
    def available(self) -> bool:
        """Do not expose a cached balance as a successful update."""
        return (
            super().available
            and self._tenancy_id in (self.coordinator.data or {})
            and self._tenancy_id not in self.coordinator.failed_balances
        )

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
            "bill_id": account_data.get("bill_id"),
        }


class EIRCMeterSensor(CoordinatorEntity, SensorEntity):
    """Representation of an EIRC meter sensor."""

    _attr_has_entity_name = False
    _attr_translation_key = "eirc_meter_sensor"

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

        subservice = meter_data.get("subservice") or {}
        utility = subservice.get("utility")
        self._attr_icon = METER_ICONS.get(utility, "mdi:counter")
        if utility == "WATER":
            # EIRC uses WATER for both supplies; only the service name
            # distinguishes hot water. Unknown water services keep the water icon.
            service_name = subservice.get("name")
            if isinstance(
                service_name, str
            ) and service_name.strip().casefold().startswith(("горяч", "гвс")):
                self._attr_icon = "mdi:water-thermometer"

        if utility == "ELECTRICITY":
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
    def available(self) -> bool:
        """Keep entity IDs, but mark stale or removed readings unavailable."""
        if not super().available or self._tenancy_id in self.coordinator.failed_meters:
            return False
        _, _, indication = self._get_current_data()
        return indication is not None

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


class EIRCUpdateStatusSensor(CoordinatorEntity, SensorEntity):
    """Explain partial and global failures independently of legacy sensors."""

    _attr_has_entity_name = True
    _attr_translation_key = "update_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_icon = "mdi:cloud-sync"
    _attr_options: ClassVar[list[str]] = [
        "ok",
        "meters_unavailable",
        "balance_unavailable",
        "data_unavailable",
        "authentication_required",
        "two_factor_required",
        "connection_error",
        "update_failed",
    ]

    def __init__(
        self, coordinator: EircDataUpdateCoordinator, account_data: dict
    ) -> None:
        super().__init__(coordinator)
        self._tenancy_id = account_data["tenancy"]["register"]
        self._attr_unique_id = f"eirc_{self._tenancy_id}_update_status"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._tenancy_id)}}

    @property
    def available(self) -> bool:
        """Keep the explanation visible when the API is unavailable."""
        return self._tenancy_id in (self.coordinator.data or {})

    @property
    def native_value(self) -> str:
        if self.coordinator.update_error:
            return self.coordinator.update_error
        errors = self.coordinator.account_errors.get(self._tenancy_id, {})
        if "meters" in errors and "balance" in errors:
            return "data_unavailable"
        if "meters" in errors:
            return "meters_unavailable"
        if "balance" in errors:
            return "balance_unavailable"
        return "ok"

    @property
    def extra_state_attributes(self) -> dict:
        errors = self.coordinator.account_errors.get(self._tenancy_id, {})
        return {
            f"{kind}_{field}": errors.get(kind, {}).get(field)
            for kind in ("meters", "balance")
            for field in ("http_status", "eirc_code")
        }


class EIRCLastSuccessSensor(CoordinatorEntity, SensorEntity):
    """Time of the last successful API read, not the date of a meter reading."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self, coordinator: EircDataUpdateCoordinator, account_data: dict, kind: str
    ) -> None:
        super().__init__(coordinator)
        self._tenancy_id = account_data["tenancy"]["register"]
        self._kind = kind
        self._attr_translation_key = f"{kind}_last_success"
        self._attr_unique_id = f"eirc_{self._tenancy_id}_{kind}_last_success"
        self._attr_device_info = {"identifiers": {(DOMAIN, self._tenancy_id)}}

    @property
    def available(self) -> bool:
        return self._tenancy_id in (self.coordinator.data or {})

    @property
    def native_value(self):
        return self.coordinator.last_success.get(self._tenancy_id, {}).get(self._kind)
