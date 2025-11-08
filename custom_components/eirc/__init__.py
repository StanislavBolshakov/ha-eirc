"""The EIRC integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
import homeassistant.helpers.entity_registry as er

from .const import ATTR_ENTITY_ID, ATTR_READINGS, DOMAIN, SERVICE_SEND_METER_READING
from .coordinator import EircDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

SEND_METER_READING_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required(ATTR_READINGS): vol.All(
            cv.ensure_list,
            [
                vol.Schema(
                    {
                        vol.Required("scale_id"): cv.positive_int,
                        vol.Required("value"): vol.Coerce(float),
                    }
                )
            ],
        ),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EIRC from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = EircDataUpdateCoordinator(hass, config_entry=entry)

    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def handle_send_meter_reading(call: ServiceCall):
        """Handle the service call to send meter readings."""
        await async_send_meter_reading(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_METER_READING,
        handle_send_meter_reading,
        schema=SEND_METER_READING_SCHEMA,
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
        hass.services.async_remove(DOMAIN, SERVICE_SEND_METER_READING)
    return unload_ok


async def async_send_meter_reading(hass: HomeAssistant, call: ServiceCall):
    """Send meter reading to the API."""
    entity_id = call.data[ATTR_ENTITY_ID]
    readings = call.data[ATTR_READINGS]

    entity_registry = er.async_get(hass)
    entity_entry = entity_registry.async_get(entity_id)
    if not entity_entry:
        raise HomeAssistantError(f"Entity {entity_id} not found in entity registry")

    config_entry_id = entity_entry.config_entry_id

    coordinator: EircDataUpdateCoordinator = hass.data[DOMAIN].get(config_entry_id)
    if not coordinator:
        raise HomeAssistantError(
            f"No coordinator found for config entry {config_entry_id}"
        )

    client = coordinator.client

    state = hass.states.get(entity_id)
    if not state:
        raise HomeAssistantError(f"Entity {entity_id} not found in state machine")

    attributes = state.attributes
    account_id = attributes.get("account_id")
    meter_id = attributes.get("meter_id")
    if not all([account_id, meter_id]):
        raise HomeAssistantError(
            f"Missing account_id or meter_id for entity {entity_id}"
        )

    try:
        meters_info = await client.get_meters_info(account_id)
        meter_info = next(
            (m for m in meters_info if m["id"]["registration"] == meter_id), None
        )
        if not meter_info:
            raise HomeAssistantError(
                f"Meter {meter_id} not found in account {account_id}"
            )

        current_scale_values = {
            ind["meterScaleId"]: ind["previousReading"]
            for ind in meter_info["indications"]
            if "meterScaleId" in ind and "previousReading" in ind
        }

        for reading in readings:
            scale_id = reading["scale_id"]
            value = reading["value"]
            if scale_id not in current_scale_values:
                raise HomeAssistantError(
                    f"Scale ID {scale_id} does not exist for meter {meter_id}"
                )
            if value < current_scale_values[scale_id]:
                raise HomeAssistantError(
                    f"Value {value} for scale {scale_id} is less than current value {current_scale_values[scale_id]}"
                )

        payload = [{"scaleId": r["scale_id"], "value": r["value"]} for r in readings]
        await client.send_meter_reading(account_id, meter_id, payload)
        _LOGGER.info("Meter readings sent successfully for %s", entity_id)

    except Exception as err:
        _LOGGER.error(f"Error sending meter readings: {err}")
        raise HomeAssistantError(f"Failed to send meter readings: {err}") from err
