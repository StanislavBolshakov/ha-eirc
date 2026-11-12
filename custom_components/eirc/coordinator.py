"""DataUpdateCoordinator for the EIRC integration."""

import asyncio
import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    EIRCApiClient,
    EircApiClientError,
    EircAuthenticationError,
    TwoFactorAuthRequired,
)
from .const import (
    CONF_SELECTED_ACCOUNTS,
    CONF_SESSION_COOKIE,
    CONF_TOKEN_AUTH,
    CONF_TOKEN_VERIFY,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=15)
UPDATE_TIMEOUT = 60


class EircDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the EIRC API."""

    def __init__(self, hass: HomeAssistant, config_entry):
        """Initialize the data update coordinator."""
        self.config_entry = config_entry
        self.failed_meters: set[str] = set()
        self.failed_balances: set[str] = set()

        settings = {**config_entry.data, **config_entry.options}
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
            raise UpdateFailed("EIRC update time budget exceeded") from err

    async def _async_fetch_data(self) -> dict:
        """Fetch independent data sets without dropping partially working accounts."""
        try:
            accounts = await self.client.get_accounts()
            processed_data = {}
            failed_meters = set()
            failed_balances = set()
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
                try:
                    account["meters"] = await self.client.get_meters_info(account_id)
                    successful_requests += 1
                except (TwoFactorAuthRequired, EircAuthenticationError):
                    raise
                except EircApiClientError as err:
                    failed_meters.add(tenancy_id)
                    account["meters"] = previous.get("meters", [])
                    _LOGGER.warning("Could not update meters: %s", err)

                try:
                    balance_data = await self.client.get_account_balance(account_id)
                    account["balance"] = balance_data["balance"]
                    account["bill_id"] = balance_data["bill_id"]
                    successful_requests += 1
                except (TwoFactorAuthRequired, EircAuthenticationError):
                    raise
                except EircApiClientError as err:
                    failed_balances.add(tenancy_id)
                    account["balance"] = previous.get("balance")
                    account["bill_id"] = previous.get("bill_id")
                    _LOGGER.warning("Could not update current bill: %s", err)

                processed_data[tenancy_id] = account

            await self._async_save_tokens()

            if processed_data and not successful_requests:
                raise UpdateFailed("No EIRC account data could be refreshed")

            self.failed_meters = failed_meters
            self.failed_balances = failed_balances
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

        except (TwoFactorAuthRequired, EircAuthenticationError) as err:
            raise ConfigEntryAuthFailed("EIRC reauthentication required") from err

        except UpdateFailed:
            raise

        except EircApiClientError as err:
            _LOGGER.error("Error communicating with API: %s", err)
            raise UpdateFailed(f"Error communicating with API: {err}") from err

        except Exception as err:
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
