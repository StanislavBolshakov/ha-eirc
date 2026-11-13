"""Config Flow for EIRC integration."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv

from . import async_request_reload
from .api import (
    EIRCApiClient,
    EircApiClientError,
    EircApiResponseError,
    EircAuthenticationError,
    TwoFactorAuthRequired,
)
from .const import (
    CONF_PASSWORD,
    CONF_PROXY_URL,
    CONF_SELECTED_ACCOUNTS,
    CONF_SESSION_COOKIE,
    CONF_TOKEN_AUTH,
    CONF_TOKEN_VERIFY,
    CONF_USE_PROXY,
    CONF_USERNAME,
    DOMAIN,
    ERROR_2FA_METHOD_NOT_SUPPORTED,
    ERROR_2FA_SEND_FAILED,
    ERROR_CANNOT_CONNECT,
    ERROR_CANNOT_FETCH_ACCOUNTS,
    ERROR_ENTRY_NOT_FOUND,
    ERROR_INVALID_AUTH,
    ERROR_INVALID_CODE,
    ERROR_INVALID_PROXY_URL,
    ERROR_NO_VALID_ACCOUNTS,
    ERROR_PROXY_URL_REQUIRED,
    ERROR_RATE_LIMITED,
    ERROR_REAUTH_REQUIRED,
    ERROR_REAUTH_SUCCESSFUL,
    ERROR_SESSION_EXPIRED,
    ERROR_UNKNOWN,
)
from .readings_flow import ReadingsFlowMixin

_LOGGER = logging.getLogger(__name__)


class InvalidProxyUrl(Exception):
    """Indicate invalid proxy URL format."""


def validate_proxy_url(url: str) -> None:
    """Basic proxy URL validation."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise InvalidProxyUrl


def build_account_options(accounts: list[dict[str, Any]]) -> dict[str, str]:
    """Build account options mapping."""
    return {
        acc["tenancy"]["register"]: f"{acc['alias']} ({acc['tenancy']['register']})"
        for acc in accounts
        if acc.get("confirmed")
    }


class EIRCCommonFlowMixin:
    """Mixin for common flow logic."""

    def __init__(self) -> None:
        """Initialize mixin."""
        self._client: EIRCApiClient | None = None
        self._accounts: list[dict[str, Any]] = []
        self._selected_accounts: list[str] = []
        self._use_proxy: bool = False
        self._proxy_url: str | None = None

    def _get_proxy_for_client(self) -> str | None:
        """Get proxy URL for API client initialization."""
        return self._proxy_url if self._use_proxy else None

    async def _authenticate(self, username: str, password: str) -> None:
        """Authenticate and fetch accounts."""
        proxy = self._get_proxy_for_client()
        self._client = EIRCApiClient(self.hass, username, password, proxy=proxy)
        await self._client.authenticate()
        self._accounts = await self._client.get_accounts()

    def _restore_client_session(self, data: dict[str, Any]) -> None:
        """Restore client session from config entry data."""
        if CONF_SESSION_COOKIE in data:
            self._client._session_cookie = data[CONF_SESSION_COOKIE]
            self._client._token_auth = data[CONF_TOKEN_AUTH]
            self._client._token_verify = data[CONF_TOKEN_VERIFY]

    def _handle_auth_error(
        self, error: Exception, username: str
    ) -> dict[str, str] | None:
        """Handle authentication errors and return error key."""
        if isinstance(error, TwoFactorAuthRequired):
            return None

        if isinstance(error, (ConfigEntryAuthFailed, EircAuthenticationError)):
            _LOGGER.warning("EIRC authentication was rejected")
            return {"base": ERROR_INVALID_AUTH}

        if isinstance(error, (TimeoutError, aiohttp.ClientConnectionError)):
            return {"base": ERROR_CANNOT_CONNECT}

        if isinstance(error, (aiohttp.ClientResponseError, EircApiResponseError)):
            if error.status == 429:
                return {"base": ERROR_RATE_LIMITED}
            return {"base": ERROR_CANNOT_CONNECT}

        if isinstance(error, EircApiClientError):
            return {"base": ERROR_CANNOT_CONNECT}

        _LOGGER.exception("Unexpected error during authentication")
        return {"base": ERROR_UNKNOWN}


class EIRCConfigFlow(config_entries.ConfigFlow, EIRCCommonFlowMixin, domain=DOMAIN):
    """Handle the configuration flow for EIRC integration."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        super().__init__()
        self._username: str | None = None
        self._password: str | None = None
        self._transaction_id: str | None = None
        self._methods: list[str] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 1: Collect credentials and optional proxy settings."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]
            self._use_proxy = user_input.get(CONF_USE_PROXY, False)

            proxy_url_submitted = CONF_PROXY_URL in user_input
            if proxy_url_submitted:
                self._proxy_url = user_input[CONF_PROXY_URL]
                if self._use_proxy and not self._proxy_url:
                    errors = {"base": ERROR_PROXY_URL_REQUIRED}
                elif self._use_proxy and self._proxy_url:
                    try:
                        validate_proxy_url(self._proxy_url)
                    except InvalidProxyUrl:
                        errors = {"base": ERROR_INVALID_PROXY_URL}

            if errors or (self._use_proxy and not proxy_url_submitted):
                return self._show_user_form(errors=errors, show_proxy_url=True)

            try:
                await self._authenticate(self._username, self._password)
            except TwoFactorAuthRequired as exc:
                return await self._handle_2fa_required(exc)
            except Exception as exc:  # pylint: disable=broad-except
                auth_error = self._handle_auth_error(exc, self._username)
                if auth_error is None:
                    raise
                return self._show_user_form(
                    errors=auth_error, show_proxy_url=proxy_url_submitted
                )

            if self.context.get("source") == config_entries.SOURCE_REAUTH:
                return await self._update_reauth_entry()

            return await self.async_step_account()

        return self._show_user_form(errors={}, show_proxy_url=False)

    def _show_user_form(
        self, errors: dict[str, str], show_proxy_url: bool
    ) -> config_entries.ConfigFlowResult:
        """Show the user form with consistent schema."""
        schema_dict = {
            vol.Required(CONF_USERNAME, default=self._username or ""): str,
            vol.Required(CONF_PASSWORD, default=self._password or ""): str,
            vol.Optional(CONF_USE_PROXY, default=self._use_proxy): bool,
        }

        if show_proxy_url:
            schema_dict[vol.Required(CONF_PROXY_URL, default=self._proxy_url or "")] = (
                str
            )

        return self.async_show_form(
            step_id="user", data_schema=vol.Schema(schema_dict), errors=errors
        )

    async def _handle_2fa_required(
        self, exc: TwoFactorAuthRequired
    ) -> config_entries.ConfigFlowResult:
        """Handle 2FA requirement."""
        self._transaction_id = exc.transaction_id
        self._methods = exc.methods

        if "EMAIL" not in self._methods:
            return self.async_abort(reason=ERROR_2FA_METHOD_NOT_SUPPORTED)

        try:
            await self._client.twofa_send_email(self._transaction_id)
            _LOGGER.debug("2FA email sent")
        except EircApiResponseError as err:
            reason = ERROR_RATE_LIMITED if err.status == 429 else ERROR_2FA_SEND_FAILED
            return self.async_abort(reason=reason)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Failed to send 2FA email")
            return self.async_abort(reason=ERROR_2FA_SEND_FAILED)

        return await self.async_step_2fa()

    async def _update_reauth_entry(self) -> config_entries.ConfigFlowResult:
        """Update existing entry with new credentials and tokens."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if not entry:
            return self.async_abort(reason=ERROR_ENTRY_NOT_FOUND)

        new_data = {
            **entry.data,
            CONF_USERNAME: self._username,
            CONF_PASSWORD: self._password,
            CONF_SESSION_COOKIE: self._client._session_cookie,
            CONF_TOKEN_AUTH: self._client._token_auth,
            CONF_TOKEN_VERIFY: self._client._token_verify,
            CONF_USE_PROXY: self._use_proxy,
            CONF_PROXY_URL: self._proxy_url,
        }

        new_options = {
            **entry.options,
            CONF_USE_PROXY: self._use_proxy,
            CONF_PROXY_URL: self._proxy_url,
        }
        self.hass.config_entries.async_update_entry(
            entry, data=new_data, options=new_options
        )
        async_request_reload(self.hass, entry)
        _LOGGER.debug("EIRC reauthentication successful")
        return self.async_abort(reason=ERROR_REAUTH_SUCCESSFUL)

    async def async_step_2fa(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle 2FA code entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            code = user_input["code"]
            try:
                await self._client.twofa_authentication(self._transaction_id, code)
            except EircApiResponseError as err:
                if err.status in (400, 406, 422):
                    errors["base"] = ERROR_INVALID_CODE
                elif err.status in (401, 403):
                    errors["base"] = ERROR_SESSION_EXPIRED
                elif err.status == 429:
                    errors["base"] = ERROR_RATE_LIMITED
                else:
                    errors["base"] = ERROR_CANNOT_CONNECT
            except (EircApiClientError, aiohttp.ClientError, TimeoutError):
                errors["base"] = ERROR_CANNOT_CONNECT
            except Exception:
                _LOGGER.exception("Unexpected verification error")
                errors["base"] = ERROR_UNKNOWN
            else:
                # A subsequent accounts outage is not an incorrect email code.
                try:
                    self._accounts = await self._client.get_accounts()
                except TwoFactorAuthRequired as err:
                    return await self._handle_2fa_required(err)
                except EircAuthenticationError:
                    errors["base"] = ERROR_SESSION_EXPIRED
                except (EircApiClientError, aiohttp.ClientError, TimeoutError) as err:
                    errors = self._handle_auth_error(err, self._username)
                except Exception:
                    _LOGGER.exception("Unexpected accounts error after verification")
                    errors = {"base": ERROR_UNKNOWN}
                else:
                    if self.context.get("source") == config_entries.SOURCE_REAUTH:
                        return await self._update_reauth_entry()
                    return await self.async_step_account()

        return self.async_show_form(
            step_id="2fa",
            data_schema=vol.Schema({vol.Required("code"): str}),
            errors=errors,
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Step 2: Select accounts to monitor."""
        if user_input is not None:
            self._selected_accounts = user_input[CONF_SELECTED_ACCOUNTS]

            return self.async_create_entry(
                title=self._username,
                data={
                    CONF_USERNAME: self._username,
                    CONF_PASSWORD: self._password,
                    CONF_SESSION_COOKIE: self._client._session_cookie,
                    CONF_TOKEN_AUTH: self._client._token_auth,
                    CONF_TOKEN_VERIFY: self._client._token_verify,
                    CONF_USE_PROXY: self._use_proxy,
                    CONF_PROXY_URL: self._proxy_url,
                    CONF_SELECTED_ACCOUNTS: self._selected_accounts,
                },
            )

        account_options = build_account_options(self._accounts)

        if not account_options:
            return self.async_abort(reason=ERROR_NO_VALID_ACCOUNTS)

        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SELECTED_ACCOUNTS, default=list(account_options.keys())
                    ): cv.multi_select(account_options)
                }
            ),
        )

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle reauthentication request."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Confirm reauthentication."""
        if user_input is None:
            entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
            if not entry:
                return self.async_abort(reason=ERROR_ENTRY_NOT_FOUND)
            settings = {**entry.data, **entry.options}
            self._use_proxy = settings.get(CONF_USE_PROXY, False)
            self._proxy_url = settings.get(CONF_PROXY_URL)
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> EIRCOptionsFlowHandler:
        """Create the options flow."""
        return EIRCOptionsFlowHandler(config_entry)


class EIRCOptionsFlowHandler(
    config_entries.OptionsFlow, EIRCCommonFlowMixin, ReadingsFlowMixin
):
    """Handle options flow for the integration."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        super().__init__()

        entry_data = config_entry.data
        entry_options = config_entry.options

        self._selected_accounts = entry_options.get(
            CONF_SELECTED_ACCOUNTS, entry_data.get(CONF_SELECTED_ACCOUNTS, [])
        )
        self._use_proxy = entry_options.get(
            CONF_USE_PROXY, entry_data.get(CONF_USE_PROXY, False)
        )
        self._proxy_url = entry_options.get(
            CONF_PROXY_URL, entry_data.get(CONF_PROXY_URL, "")
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Choose configuration or an explicit meter-reading action."""
        return self.async_show_menu(
            step_id="init", menu_options=["account_selection", "send_readings"]
        )

    async def _async_load_accounts(self):
        try:
            await self._fetch_accounts()
        except (TwoFactorAuthRequired, EircAuthenticationError):
            self.config_entry.async_start_reauth_if_available(self.hass)
            return self.async_abort(reason=ERROR_REAUTH_REQUIRED)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Failed to fetch accounts for options flow")
            return self.async_abort(reason=ERROR_CANNOT_FETCH_ACCOUNTS)

        return None

    async def _fetch_accounts(self) -> None:
        """Fetch available accounts."""
        proxy = self._get_proxy_for_client()
        self._client = EIRCApiClient(
            self.hass,
            self.config_entry.data[CONF_USERNAME],
            self.config_entry.data[CONF_PASSWORD],
            proxy=proxy,
        )

        self._restore_client_session(self.config_entry.data)
        self._accounts = await self._client.get_accounts()

    async def async_step_account_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Select accounts and proxy settings."""
        if not self._accounts and (result := await self._async_load_accounts()):
            return result
        errors: dict[str, str] = {}

        if user_input is not None:
            self._selected_accounts = user_input[CONF_SELECTED_ACCOUNTS]
            self._use_proxy = user_input.get(CONF_USE_PROXY, False)

            proxy_url_submitted = CONF_PROXY_URL in user_input
            if proxy_url_submitted:
                self._proxy_url = user_input[CONF_PROXY_URL]
                if self._use_proxy and not self._proxy_url:
                    errors = {"base": ERROR_PROXY_URL_REQUIRED}
                elif self._use_proxy and self._proxy_url:
                    try:
                        validate_proxy_url(self._proxy_url)
                    except InvalidProxyUrl:
                        errors = {"base": ERROR_INVALID_PROXY_URL}

            if errors or (self._use_proxy and not proxy_url_submitted):
                return self._show_options_form(errors=errors, show_proxy_url=True)

            new_options = {
                CONF_SELECTED_ACCOUNTS: self._selected_accounts,
                CONF_USE_PROXY: self._use_proxy,
                CONF_PROXY_URL: self._proxy_url,
            }

            return self.async_create_entry(title="", data=new_options)

        return self._show_options_form(errors={}, show_proxy_url=self._use_proxy)

    def _show_options_form(
        self, errors: dict[str, str], show_proxy_url: bool
    ) -> config_entries.ConfigFlowResult:
        """Show the options form with consistent schema."""
        schema_dict = {
            vol.Required(
                CONF_SELECTED_ACCOUNTS, default=self._selected_accounts
            ): cv.multi_select(build_account_options(self._accounts)),
            vol.Optional(CONF_USE_PROXY, default=self._use_proxy): bool,
        }

        if show_proxy_url:
            schema_dict[vol.Required(CONF_PROXY_URL, default=self._proxy_url or "")] = (
                str
            )

        return self.async_show_form(
            step_id="account_selection",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )
