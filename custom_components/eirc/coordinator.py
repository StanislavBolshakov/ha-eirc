"""DataUpdateCoordinator for the EIRC integration."""

from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EIRCApiClient, EircApiClientError, TwoFactorAuthRequired
from .const import DOMAIN, CONF_SESSION_COOKIE, CONF_TOKEN_AUTH, CONF_TOKEN_VERIFY

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=15)


class EircDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the EIRC API."""

    def __init__(self, hass: HomeAssistant, config_entry):
        """Initialize the data update coordinator."""
        self.config_entry = config_entry

        proxy_url = config_entry.data.get("proxy_url")
        use_proxy = config_entry.data.get("use_proxy", False)

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
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> dict:
        """Fetch data from API endpoint."""
        try:
            accounts = await self.client.get_accounts()
            processed_data = {}
            failed_accounts = []

            for account in accounts:
                if not account.get("confirmed"):
                    continue

                account_id = account["id"]
                tenancy_id = account["tenancy"]["register"]

                try:
                    # Fetch data for this account
                    meters_info = await self.client.get_meters_info(account_id)
                    balance_data = await self.client.get_account_balance(account_id)

                    account["meters"] = meters_info
                    account["balance"] = balance_data["balance"]
                    account["bill_id"] = balance_data["bill_id"]
                    processed_data[tenancy_id] = account

                    _LOGGER.debug(
                        "Successfully updated account %s (ID: %s)",
                        tenancy_id,
                        account_id,
                    )

                except TwoFactorAuthRequired as err:
                    # 2FA errors should still fail the entire update
                    _LOGGER.warning(
                        "2FA required for account %s (ID: %s): %s",
                        tenancy_id,
                        account_id,
                        err,
                    )
                    raise ConfigEntryAuthFailed(
                        "Reauthentication required due to 2FA"
                    ) from err

                except (EircApiClientError, Exception) as err:
                    # Log the error but continue processing other accounts
                    _LOGGER.error(
                        "Failed to update account %s (ID: %s): %s",
                        tenancy_id,
                        account_id,
                        err,
                    )
                    failed_accounts.append(
                        {
                            "tenancy_id": tenancy_id,
                            "account_id": account_id,
                            "error": str(err),
                        }
                    )

            await self._async_save_tokens()

            # Log summary of update results
            if failed_accounts:
                _LOGGER.warning(
                    "Updated %d accounts successfully, %d accounts failed: %s",
                    len(processed_data),
                    len(failed_accounts),
                    [
                        f"{acc['tenancy_id']} ({acc['account_id']})"
                        for acc in failed_accounts
                    ],
                )
            else:
                _LOGGER.info(
                    "Successfully updated data for %d accounts", len(processed_data)
                )

            _LOGGER.debug("EIRC coordinator data updated: %s", processed_data)

            return processed_data

        except TwoFactorAuthRequired as err:
            _LOGGER.warning("2FA required during reauthentication: %s", err)
            raise ConfigEntryAuthFailed("Reauthentication required due to 2FA") from err

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
            _LOGGER.debug(
                "Tokens persisted for user: %s", self.config_entry.data["username"]
            )
