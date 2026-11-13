"""Allowlisted diagnostics: never serialize configuration or API payloads."""

import re
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import CONF_SELECTED_ACCOUNTS, CONF_USE_PROXY, DOMAIN

UPDATE_ERRORS = {
    "connection_error",
    "authentication_required",
    "two_factor_required",
    "update_failed",
}


def _timestamp(value) -> str | None:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.isoformat()
    return None


def _error(details: dict) -> dict:
    status = details.get("http_status")
    code = details.get("eirc_code")
    return {
        "http_status": status if type(status) is int and 100 <= status <= 599 else None,
        "eirc_code": code
        if isinstance(code, str) and re.fullmatch(r"[0-9]{1,4}", code)
        else None,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    """Return health, versions and anonymous counts without contacting EIRC."""
    integration = await async_get_integration(hass, DOMAIN)
    version = integration.version
    if not isinstance(version, str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc|\.dev)[0-9]+)?", version
    ):
        version = "unknown"
    selected = entry.options.get(
        CONF_SELECTED_ACCOUNTS, entry.data.get(CONF_SELECTED_ACCOUNTS, [])
    )
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    result = {
        "versions": {"home_assistant": HA_VERSION, "integration": version},
        "configuration": {
            "selected_account_count": len(selected)
            if isinstance(selected, list)
            else 0,
            "proxy_enabled": bool(
                entry.options.get(CONF_USE_PROXY, entry.data.get(CONF_USE_PROXY, False))
            ),
        },
        "coordinator": {"loaded": coordinator is not None},
        "accounts": [],
    }
    if coordinator is None:
        return result
    update_error = coordinator.update_error
    result["coordinator"].update(
        {
            "last_update_success": bool(coordinator.last_update_success),
            "update_error": update_error
            if isinstance(update_error, str) and update_error in UPDATE_ERRORS
            else ("unknown" if update_error is not None else None),
        }
    )
    # Indexes deliberately replace identifiers; do not hash numeric account IDs.
    for index, (tenancy, account) in enumerate((coordinator.data or {}).items(), 1):
        errors = coordinator.account_errors.get(tenancy, {})
        last = coordinator.last_success.get(tenancy, {})
        meters = account.get("meters", [])
        result["accounts"].append(
            {
                "account": index,
                "meter_count": len(meters),
                "scale_count": sum(
                    len(meter.get("indications", [])) for meter in meters
                ),
                "meters_available": bool(
                    coordinator.last_update_success and "meters" not in errors
                ),
                "balance_available": bool(
                    coordinator.last_update_success and "balance" not in errors
                ),
                "last_meter_fetch": _timestamp(last.get("meters")),
                "last_balance_fetch": _timestamp(last.get("balance")),
                "errors": {
                    kind: _error(errors[kind])
                    for kind in ("meters", "balance")
                    if kind in errors
                },
            }
        )
    return result
