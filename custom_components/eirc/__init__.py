"""The EIRC integration."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
import homeassistant.helpers.entity_registry as er

from .const import (
    ATTR_ENTITY_ID,
    ATTR_READINGS,
    DOMAIN,
    SERVICE_SEND_METER_READING,
    SERVICE_DOWNLOAD_BILL,
    ATTR_ACCOUNT_ID,
    ATTR_BILL_ID,
)
from .coordinator import EircDataUpdateCoordinator

import os
from datetime import datetime

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

DOWNLOAD_BILL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ACCOUNT_ID): cv.string,
        vol.Required(ATTR_BILL_ID): cv.string,
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

    async def handle_download_bill(call: ServiceCall) -> ServiceResponse:
        """Handle the service call to download a bill."""
        return await async_download_bill(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_DOWNLOAD_BILL,
        handle_download_bill,
        schema=DOWNLOAD_BILL_SCHEMA,
        supports_response=vol.Schema(
            {
                vol.Required("url"): cv.string,
                vol.Required("filepath"): cv.string,
            }
        ),
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
        hass.services.async_remove(DOMAIN, SERVICE_SEND_METER_READING)
        hass.services.async_remove(DOMAIN, SERVICE_DOWNLOAD_BILL)
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


async def async_download_bill(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Download bill PDF and save it to www dir"""
    account_id = call.data[ATTR_ACCOUNT_ID]
    bill_id = call.data[ATTR_BILL_ID]

    for entry_id, coordinator in hass.data[DOMAIN].items():
        accounts = coordinator.data
        for tenancy_id, account_data in accounts.items():
            if str(account_data.get("id")) == account_id:
                client = coordinator.client
                break
        else:
            continue
        break
    else:
        raise HomeAssistantError(f"Account {account_id} not found")

    try:
        uuid = await client.get_bill_uuid(account_id, bill_id)
        pdf_data = await client.download_bill_pdf(uuid)
        www_dir = hass.config.path("www")
        bills_dir = os.path.join(www_dir, "eirc_bills")
        os.makedirs(bills_dir, exist_ok=True)
        filename = f"bill_{account_id}_{bill_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        filepath = os.path.join(bills_dir, filename)
        await hass.async_add_executor_job(lambda: open(filepath, "wb").write(pdf_data))

        local_url = f"/local/eirc_bills/{filename}"
        _LOGGER.info("Bill downloaded and saved to %s", filepath)
        return {"url": local_url, "filepath": filepath}

    except Exception as err:
        _LOGGER.error(f"Error downloading bill: {err}")
        raise HomeAssistantError(f"Failed to download bill: {err}") from err
