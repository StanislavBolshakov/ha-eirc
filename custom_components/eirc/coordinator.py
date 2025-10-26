"""DataUpdateCoordinator for the EIRC integration."""

from datetime import timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EIRCApiClient, EircApiClientError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=15)


class EircDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the EIRC API."""

    def __init__(self, hass: HomeAssistant, client: EIRCApiClient):
        """Initialize the data update coordinator."""
        self.client = client
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> dict:
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            accounts = await self.client.get_accounts()
            processed_data = {}

            for account in accounts:
                if not account.get("confirmed"):
                    continue

                account_id = account["id"]
                tenancy_id = account["tenancy"]["register"]

                meters_info = await self.client.get_meters_info(account_id)
                balance = await self.client.get_account_balance(account_id)

                account["meters"] = meters_info
                account["balance"] = balance
                processed_data[tenancy_id] = account

            _LOGGER.debug(
                "Successfully updated data for %d accounts", len(processed_data)
            )
            _LOGGER.debug("EIRC coordinator data updated: %s", processed_data)
            return processed_data

        except EircApiClientError as err:
            _LOGGER.error("Error communicating with API: %s", err)
            raise UpdateFailed(f"Error communicating with API: {err}") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error fetching EIRC data")
            raise UpdateFailed(f"Unexpected error: {err}") from err
