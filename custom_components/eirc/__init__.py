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
from .const import (
    ATTR_ENTITY_ID,
    ATTR_READINGS,
    DOMAIN,
    SERVICE_SEND_METER_READING,
    CONF_PROXY,
)
from .sensor import EIRCDataUpdateCoordinator

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

    proxy = (entry.options.get(CONF_PROXY) or entry.data.get(CONF_PROXY) or "").strip() or None

    client = EIRCApiClient(
        hass,
        username=entry.data["username"],
        password=entry.data["password"],
        session_cookie=entry.data.get("session_cookie"),
        token_auth=entry.data.get("token_auth"),
        token_verify=entry.data.get("token_verify"),
        proxy=proxy,
    )

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
        _LOGGER.error("Service registration failed with: %s", service_err)
        raise

    try:
        hass.data[DOMAIN][entry.entry_id] = {"client": client, "coordinator": None}
        entry.async_on_unload(entry.add_update_listener(_update_listener))
        await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    except Exception as e:
        _LOGGER.error("Error setting up entry: %s", e)
        return False
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, ["sensor"]):
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def async_send_meter_reading(hass: HomeAssistant, call: ServiceCall):
    """Send meter reading to the API and refresh coordinator."""
    entity_id = call.data[ATTR_ENTITY_ID]
    readings = call.data[ATTR_READINGS]
    _LOGGER.debug(
        "Service called with entity_id: %s and readings: %s", entity_id, readings
    )
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

    entity_registry = er.async_get(hass)
    entity_entry = entity_registry.async_get(entity_id)
    if not entity_entry:
        raise HomeAssistantError(f"Entity {entity_id} not found in entity registry")

    config_entry_id = entity_entry.config_entry_id
    entry_data = hass.data[DOMAIN].get(config_entry_id)
    if not entry_data:
        raise HomeAssistantError(f"No data found for config entry {config_entry_id}")

    client: EIRCApiClient = entry_data.get("client")
    coordinator: EIRCDataUpdateCoordinator | None = entry_data.get("coordinator")
    if not client:
        raise HomeAssistantError(
            f"No API client found for config entry {config_entry_id}"
        )

    try:
        meters_info = await client.get_meters_info(account_id)
    except Exception as err:
        _LOGGER.error("Error fetching meter information: %s", err)
        raise HomeAssistantError(f"Failed to fetch meter information: {err}")

    meter_info = next(
        (m for m in meters_info if m["id"]["registration"] == meter_id), None
    )
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

    payload = [{"scaleId": r["scale_id"], "value": r["value"]} for r in readings]
    _LOGGER.debug(
        "Sending readings to account %s, meter %s: %s", account_id, meter_id, payload
    )

    try:
        success = await client.send_meter_reading(account_id, meter_id, payload)
        if success:
            _LOGGER.debug("Meter readings sent successfully")

            if coordinator:
                await coordinator.async_request_refresh()
                _LOGGER.debug("Coordinator refreshed after sending meter reading")
            else:
                _LOGGER.warning(
                    "No coordinator found for entry %s, sensor may not update immediately",
                    config_entry_id,
                )
        else:
            raise HomeAssistantError("Failed to send meter readings")
    except Exception as err:
        _LOGGER.error("Error sending meter readings: %s", err)
        raise HomeAssistantError(f"Failed to send meter readings: {err}")

async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options (e.g., proxy) change."""
    await hass.config_entries.async_reload(entry.entry_id)
