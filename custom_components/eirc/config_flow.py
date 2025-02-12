"""Config Flow for EIRC integration."""

import logging

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryAuthFailed
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .api import EIRCApiClient
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

USERNAME = "username"
PASSWORD = "password"
SELECTED_ACCOUNTS = "selected_accounts"


class EIRCConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the configuration flow for EIRC integration."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self):
        """Initialize the configuration flow."""
        self._username = None
        self._password = None
        self._accounts = []
        self._selected_accounts = []

    async def async_step_user(self, user_input=None):
        """Handle the initial step where the user provides credentials."""
        errors = {}
        if user_input is not None:
            try:
                self._username = user_input[USERNAME]
                self._password = user_input[PASSWORD]
                _LOGGER.debug("Attempting authentication for user: %s", self._username)

                client = EIRCApiClient(self.hass, self._username, self._password)
                await client.authenticate()
                self._accounts = await client.get_accounts()

                if self.context.get("source") == config_entries.SOURCE_REAUTH:
                    entry = self.hass.config_entries.async_get_entry(
                        self.context["entry_id"]
                    )
                    new_data = entry.data.copy()
                    new_data.update(
                        {USERNAME: self._username, PASSWORD: self._password}
                    )
                    self.hass.config_entries.async_update_entry(entry, data=new_data)
                    await self.hass.config_entries.async_reload(entry.entry_id)
                    _LOGGER.debug(
                        "Reauthentication successful for user: %s", self._username
                    )
                    return self.async_abort(reason="reauth_successful")

                return await self.async_step_account()
            except ConfigEntryAuthFailed:
                _LOGGER.warning("Authentication failed for user: %s", self._username)
                errors["base"] = "invalid_auth"
            except aiohttp.ClientResponseError as err:
                if err.status == 403:
                    _LOGGER.warning(
                        "Invalid authentication credentials for user: %s",
                        self._username,
                    )
                    errors["base"] = "invalid_auth"
                else:
                    _LOGGER.error(
                        "HTTP error %s while authenticating user: %s",
                        err.status,
                        self._username,
                    )
                    errors["base"] = "cannot_connect"
            except aiohttp.ClientError:
                _LOGGER.error(
                    "Connection error while authenticating user: %s", self._username
                )
                errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception("Unexpected error during authentication: %s", err)
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=self._get_user_schema(), errors=errors
        )

    async def async_step_account(self, user_input=None):
        """Step where the user selects accounts to monitor."""
        errors = {}
        if user_input is not None:
            self._selected_accounts = user_input[SELECTED_ACCOUNTS]
            return self.async_create_entry(
                title=self._username,
                data={
                    USERNAME: self._username,
                    PASSWORD: self._password,
                    SELECTED_ACCOUNTS: self._selected_accounts,
                },
                options={},
            )

        account_options = {
            acc["tenancy"]["register"]: f"{acc['alias']} ({acc['tenancy']['register']})"
            for acc in self._accounts
            if acc["confirmed"]
        }
        if not account_options:
            _LOGGER.warning("No valid accounts found for user: %s", self._username)
            return self.async_abort(reason="no_valid_accounts")

        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        SELECTED_ACCOUNTS, default=list(account_options.keys())
                    ): cv.multi_select(account_options)
                }
            ),
            description_placeholders={
                "account_count": str(len(account_options)),
                "username": self._username,
            },
            errors=errors,
        )

    async def async_step_reauth(self, user_input=None):
        """Handle re-authentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Dialog that informs the user that re-authentication is required."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                description_placeholders={
                    "username": self.context["title_placeholders"]["username"]
                },
            )
        return await self.async_step_user()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return EIRCOptionsFlowHandler(config_entry)

    def _get_user_schema(self):
        return vol.Schema({vol.Required(USERNAME): str, vol.Required(PASSWORD): str})


class EIRCOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for the integration."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize the options flow handler."""
        self.entry_id = config_entry.entry_id
        self._accounts = []
        self._selected_accounts = config_entry.data.get(SELECTED_ACCOUNTS, [])

    async def async_step_init(self, user_input=None):
        """Initialize options flow and fetch accounts."""
        config_entry = self.hass.config_entries.async_get_entry(self.entry_id)
        try:
            client = EIRCApiClient(
                self.hass, config_entry.data[USERNAME], config_entry.data[PASSWORD]
            )
            self._accounts = await client.get_accounts()
        except Exception as err:
            _LOGGER.error("Error fetching accounts: %s", err)
            return self.async_abort(reason="cannot_fetch_accounts")

        return await self.async_step_account_selection()

    async def async_step_account_selection(self, user_input=None):
        """Allow the user to modify account selection."""
        if user_input is not None:
            new_data = {
                **self.config_entry.data,
                SELECTED_ACCOUNTS: user_input[SELECTED_ACCOUNTS],
            }
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data={})

        account_options = {
            acc["tenancy"]["register"]: f"{acc['alias']} ({acc['tenancy']['register']})"
            for acc in self._accounts
            if acc["confirmed"]
        }
        return self.async_show_form(
            step_id="account_selection",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        SELECTED_ACCOUNTS, default=self._selected_accounts
                    ): cv.multi_select(account_options)
                }
            ),
        )
