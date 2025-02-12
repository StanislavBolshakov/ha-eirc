"""The EIRC integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
import homeassistant.helpers.entity_registry as er

from .api import EIRCApiClient
from .const import ATTR_ENTITY_ID, ATTR_READINGS, DOMAIN, SERVICE_SEND_METER_READING

_LOGGER = logging.getLogger(__name__)

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
    """Set up from config entry."""
    hass.data.setdefault(DOMAIN, {})
    client = EIRCApiClient(hass, entry.data["username"], entry.data["password"])

    async def handle_send_meter_reading(call: ServiceCall):
        """Handle the service call to send meter readings."""
        _LOGGER.debug("Service call received: %s", call.data)
        await async_send_meter_reading(hass, call)

    _LOGGER.debug("Registering services")
    try:
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEND_METER_READING,
            handle_send_meter_reading,
            schema=SEND_METER_READING_SCHEMA,
        )
        _LOGGER.debug(
            "Service %s.%s registered successfully", DOMAIN, SERVICE_SEND_METER_READING
        )
    except Exception as service_err:
        _LOGGER.error(f"Service registration failed with: {service_err}")
        raise

    try:
        hass.data[DOMAIN][entry.entry_id] = client
        await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    except Exception as e:
        _LOGGER.error("Error setting up entry: %s", e)
        return False
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, ["sensor"]):
        client = hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok

async def async_send_meter_reading(hass: HomeAssistant, call: ServiceCall):
    """Send meter reading to the API."""
    entity_id = call.data[ATTR_ENTITY_ID]
    readings = call.data[ATTR_READINGS]
    _LOGGER.debug(
        f"Service called with entity_id: {entity_id} and readings: {readings}"
    )
    state = hass.states.get(entity_id)
    if not state:
        raise HomeAssistantError(f"Entity {entity_id} not found in state machine")
    attributes = state.attributes
    _LOGGER.debug(f"Entity attributes: {attributes}")
    account_id = attributes.get("account_id")
    meter_id = attributes.get("meter_id")
    if not all([account_id, meter_id]):
        raise HomeAssistantError(
            f"Missing account_id or meter_id for entity {entity_id}"
        )
    entity_registry = er.async_get(hass)
    entity_entry = entity_registry.async_get(entity_id)
    if not entity_entry:
        raise HomeAssistantError(f"Entity {entity_id} not found in entity registry")
    config_entry_id = entity_entry.config_entry_id
    client = hass.data[DOMAIN].get(config_entry_id)
    if not client:
        raise HomeAssistantError(
            f"No API client found for config entry {config_entry_id}"
        )

    try:
        meters_info = await client.get_meters_info(account_id)
    except Exception as err:
        _LOGGER.error(f"Error fetching meter information: {err}")
        raise HomeAssistantError(f"Failed to fetch meter information: {err}")

    meter_info = None
    for meter in meters_info:
        if meter["id"]["registration"] == meter_id:
            meter_info = meter
            break
    if not meter_info:
        raise HomeAssistantError(f"Meter {meter_id} not found in account {account_id}")

    current_scale_values = {
        indication["meterScaleId"]: indication["previousReading"]
        for indication in meter_info["indications"]
        if "meterScaleId" in indication and "previousReading" in indication
    }

    for reading in readings:
        scale_id = reading["scale_id"]
        value = reading["value"]

        if scale_id not in current_scale_values:
            raise HomeAssistantError(
                f"Scale ID {scale_id} does not exist for meter {meter_id}"
            )

        current_value = current_scale_values[scale_id]
        if value < current_value:
            raise HomeAssistantError(
                f"Value {value} for scale {scale_id} is less than current value {current_value}"
            )

    payload = [
        {"scaleId": reading["scale_id"], "value": reading["value"]}
        for reading in readings
    ]
    _LOGGER.debug(
        f"Sending readings to account {account_id}, meter {meter_id}: {payload}"
    )
    try:
        success = await client.send_meter_reading(account_id, meter_id, payload)
        if success:
            _LOGGER.debug("Meter readings sent successfully")
        else:
            _LOGGER.error("Failed to send meter readings")
            raise HomeAssistantError("Failed to send meter readings")
    except Exception as err:
        _LOGGER.error(f"Error sending meter readings: {err}")
        raise HomeAssistantError(f"Failed to send meter readings: {err}")
