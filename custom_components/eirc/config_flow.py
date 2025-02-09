import voluptuous as vol
import aiohttp
import logging
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import config_validation as cv
from homeassistant.core import callback
from .const import DOMAIN
from .api import EIRCApiClient

_LOGGER = logging.getLogger(__name__)


class EIRCConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self):
        self._username = None
        self._password = None
        self._accounts = []
        self._selected_accounts = []

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                self._username = user_input["username"]
                self._password = user_input["password"]
                client = EIRCApiClient(self.hass, self._username, self._password)
                await client.authenticate()
                self._accounts = await client.get_accounts()
                if self.context.get("source") == config_entries.SOURCE_REAUTH:
                    entry = self.hass.config_entries.async_get_entry(
                        self.context["entry_id"]
                    )
                    new_data = entry.data.copy()
                    new_data.update(
                        {"username": self._username, "password": self._password}
                    )
                    self.hass.config_entries.async_update_entry(entry, data=new_data)
                    await self.hass.config_entries.async_reload(entry.entry_id)
                    return self.async_abort(reason="reauth_successful")
                return await self.async_step_account()
            except ConfigEntryAuthFailed:
                errors["base"] = "invalid_auth"
            except aiohttp.ClientError:
                errors["base"] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception("Unexpected error: %s", err)
                errors["base"] = "unknown"
        return self.async_show_form(
            step_id="user",
            data_schema=self._get_user_schema(),
            errors=errors,
        )

    async def async_step_account(self, user_input=None):
        """Step to select accounts to monitor."""
        errors = {}
        if user_input is not None:
            self._selected_accounts = user_input["accounts"]
            return self.async_create_entry(
                title=self._username,
                data={
                    "username": self._username,
                    "password": self._password,
                    "selected_accounts": self._selected_accounts,
                },
                options={},
            )

        account_options = {
            acc["tenancy"]["register"]: f"{acc['alias']} ({acc['tenancy']['register']})"
            for acc in self._accounts
            if acc["confirmed"]
        }
        if not account_options:
            _LOGGER.warning("No valid accounts found.")
            return self.async_abort(reason="no_valid_accounts")
        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "accounts", default=list(account_options.keys())
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
        """Dialog that informs the user that reauth is required."""
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
        return vol.Schema(
            {
                vol.Required("username"): str,
                vol.Required("password"): str,
            }
        )


class EIRCOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry):
        self.config_entry = config_entry
        self._accounts = []
        self._selected_accounts = config_entry.data.get("selected_accounts", [])

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        try:
            client = EIRCApiClient(
                self.hass,
                self.config_entry.data["username"],
                self.config_entry.data["password"],
            )
            self._accounts = await client.get_accounts()
        except Exception as err:
            _LOGGER.error("Error fetching accounts: %s", err)
            return self.async_abort(reason="cannot_fetch_accounts")
        return await self.async_step_account_selection()

    async def async_step_account_selection(self, user_input=None):
        """Handle account selection options."""
        if user_input is not None:
            new_data = {**self.config_entry.data}
            new_data["selected_accounts"] = user_input["accounts"]
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
                        "accounts", default=self._selected_accounts
                    ): cv.multi_select(account_options)
                }
            ),
        )
