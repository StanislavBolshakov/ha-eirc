"""DataUpdateCoordinator for the EIRC integration."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from math import ceil
from time import monotonic

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

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
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=15)
UPDATE_TIMEOUT = 60
MANUAL_REFRESH_COOLDOWN = 60


class EircDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the EIRC API."""

    def __init__(self, hass: HomeAssistant, config_entry):
        """Initialize the data update coordinator."""
        self.config_entry = config_entry
        self.failed_meters: set[str] = set()
        self.failed_balances: set[str] = set()
        self.account_errors: dict[str, dict[str, dict]] = {}
        self.last_success: dict[str, dict[str, datetime]] = {}
        self.update_error: str | None = None
        self._manual_refresh_lock = asyncio.Lock()
        self._last_manual_refresh: float | None = None

        settings = {**config_entry.data, **config_entry.options}
        self._reload_settings = self._settings_for_reload(config_entry)
        self.reload_pending = False
        proxy_url = settings.get("proxy_url")
        use_proxy = settings.get("use_proxy", False)

        self.client = EIRCApiClient(
            hass,
            config_entry.data["username"],
            config_entry.data["password"],
            proxy=proxy_url if use_proxy else None,
        )

        if CONF_SESSION_COOKIE in config_entry.data:
            self.client._session_cookie = config_entry.data[CONF_SESSION_COOKIE]
            self.client._token_auth = config_entry.data[CONF_TOKEN_AUTH]
            self.client._token_verify = config_entry.data[CONF_TOKEN_VERIFY]

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> dict:
        """Bound a complete refresh so outages cannot hold setup indefinitely."""
        try:
            async with asyncio.timeout(UPDATE_TIMEOUT):
                return await self._async_fetch_data()
        except TimeoutError as err:
            self._set_update_error("connection_error")
            raise UpdateFailed("EIRC update time budget exceeded") from err

    def _set_update_error(self, error: str) -> None:
        """Publish changed diagnostics even during consecutive failed refreshes."""
        self.update_error = error
        if not self.last_update_success:
            self.async_update_listeners()

    @staticmethod
    def _settings_for_reload(entry) -> dict:
        settings = {**entry.data, **entry.options}
        if (selected := settings.get(CONF_SELECTED_ACCOUNTS)) is not None:
            settings[CONF_SELECTED_ACCOUNTS] = tuple(selected)
        return {
            key: settings.get(key)
            for key in (
                CONF_USERNAME,
                CONF_PASSWORD,
                CONF_SELECTED_ACCOUNTS,
                CONF_USE_PROXY,
                CONF_PROXY_URL,
            )
        }

    def settings_changed(self, entry) -> bool:
        """Session persistence must not reload the running integration."""
        return self._settings_for_reload(entry) != self._reload_settings

    @staticmethod
    def _error_details(error: EircApiClientError) -> dict:
        """Expose only safe API codes, never arbitrary response bodies or URLs."""
        if isinstance(error, EircApiResponseError):
            return {"http_status": error.status, "eirc_code": error.code}
        return {"http_status": None, "eirc_code": None}

    async def async_request_manual_refresh(self) -> None:
        """Refresh via HA's debouncer, with one shared rate limit per entry."""
        if self._manual_refresh_lock.locked():
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="refresh_in_progress"
            )
        async with self._manual_refresh_lock:
            now = monotonic()
            if self._last_manual_refresh is not None:
                remaining = MANUAL_REFRESH_COOLDOWN - (now - self._last_manual_refresh)
                if remaining > 0:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key="refresh_rate_limited",
                        translation_placeholders={"seconds": str(ceil(remaining))},
                    )
            self._last_manual_refresh = now
            await self.async_request_refresh()
            if not self.last_update_success:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key=self.update_error or "refresh_failed",
                )

    async def _async_fetch_data(self) -> dict:
        """Fetch independent data sets without dropping partially working accounts."""
        try:
            accounts = await self.client.get_accounts()
            processed_data = {}
            failed_meters = set()
            failed_balances = set()
            account_errors = {}
            last_success = {}
            successful_requests = 0
            selected = self.config_entry.options.get(
                CONF_SELECTED_ACCOUNTS,
                self.config_entry.data.get(CONF_SELECTED_ACCOUNTS),
            )

            for account in accounts:
                if not account.get("confirmed"):
                    continue

                account_id = account["id"]
                tenancy_id = account["tenancy"]["register"]
                if selected is not None and tenancy_id not in selected:
                    continue

                # Keep the API payload/field names intact. Cached meter metadata
                # retains existing entities, but failed data is marked unavailable.
                account = dict(account)
                previous = (self.data or {}).get(tenancy_id, {})
                errors = account_errors[tenancy_id] = {}
                successful_at = last_success[tenancy_id] = dict(
                    self.last_success.get(tenancy_id, {})
                )
                try:
                    account["meters"] = await self.client.get_meters_info(account_id)
                    successful_requests += 1
                    successful_at["meters"] = datetime.now(UTC)
                except (TwoFactorAuthRequired, EircAuthenticationError):
                    raise
                except EircApiClientError as err:
                    failed_meters.add(tenancy_id)
                    errors["meters"] = self._error_details(err)
                    account["meters"] = previous.get("meters", [])
                    _LOGGER.warning("Could not update meters: %s", err)

                try:
                    balance_data = await self.client.get_account_balance(account_id)
                    account["balance"] = balance_data["balance"]
                    account["bill_id"] = balance_data["bill_id"]
                    successful_requests += 1
                    successful_at["balance"] = datetime.now(UTC)
                except (TwoFactorAuthRequired, EircAuthenticationError):
                    raise
                except EircApiClientError as err:
                    failed_balances.add(tenancy_id)
                    errors["balance"] = self._error_details(err)
                    account["balance"] = previous.get("balance")
                    account["bill_id"] = previous.get("bill_id")
                    _LOGGER.warning("Could not update current bill: %s", err)

                processed_data[tenancy_id] = account

            await self._async_save_tokens()

            self.account_errors = account_errors
            self.last_success = last_success
            self.failed_meters = failed_meters
            self.failed_balances = failed_balances
            if processed_data and not successful_requests:
                raise UpdateFailed("No EIRC account data could be refreshed")

            self.update_error = None
            if failed_meters or failed_balances:
                _LOGGER.warning(
                    "Updated %d accounts; meters unavailable for %d, bills for %d",
                    len(processed_data),
                    len(failed_meters),
                    len(failed_balances),
                )
            else:
                _LOGGER.info(
                    "Successfully updated data for %d accounts", len(processed_data)
                )

            return processed_data

        except TwoFactorAuthRequired as err:
            self._set_update_error("two_factor_required")
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="two_factor_required"
            ) from err

        except EircAuthenticationError as err:
            self._set_update_error("authentication_required")
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="authentication_required"
            ) from err

        except UpdateFailed:
            self._set_update_error("update_failed")
            raise

        except EircApiClientError as err:
            self._set_update_error("connection_error")
            _LOGGER.error("Error communicating with API: %s", err)
            raise UpdateFailed(f"Error communicating with API: {err}") from err

        except Exception as err:
            self._set_update_error("update_failed")
            _LOGGER.exception("Unexpected error fetching EIRC data")
            raise UpdateFailed(f"Unexpected error: {err}") from err

    async def _async_save_tokens(self) -> None:
        """Save current tokens to config entry if changed."""
        current_tokens = self.client.get_token_state()

        if any(
            current_tokens[key] != self.config_entry.data.get(key)
            for key in current_tokens
        ):
            new_data = {**self.config_entry.data, **current_tokens}
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )
            _LOGGER.debug("EIRC tokens persisted")
