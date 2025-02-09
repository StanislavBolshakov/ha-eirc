"""API client for EIRC integration."""

import logging
import asyncio
from aiohttp import ClientSession, ClientResponseError
from typing import Callable, Any

_LOGGER = logging.getLogger(__name__)

MAX_RETRIES = 5
INITIAL_BACKOFF = 2  
BACKOFF_MULTIPLIER = 2 

class EIRCApiClient:
    def __init__(self, hass, username: str, password: str):
        self.hass = hass
        self._username = username
        self._password = password
        self._session = None
        self._token = None

    async def authenticate(self):
        """Authenticate with the API and store the token."""
        await self._retry_api_call(self._perform_authentication)

    async def get_accounts(self):
        """Fetch accounts from the API."""
        return await self._retry_api_call(self._perform_get_accounts)

    async def get_account_balance(self, account_id: int) -> float:
        """
        Fetch the account balance for the given account ID.
        Returns the sum of "accrued" values for items where "checked" is True.
        """

        url = f"https://ikus.pesc.ru/api/v7/accounts/{account_id}/payments/at/current/amount/discretion"
        headers = {"Authorization": f"Bearer {self._token}"}

        async with self._get_session().get(url, headers=headers) as response:
            if response.status != 200:
                response.raise_for_status()
            data = await response.json()

        total_balance = sum(
            item["charge"]["accrued"] for item in data if item.get("checked", False)
        )
        _LOGGER.debug(f"Fetched account balance: {total_balance}")
        return total_balance

    async def get_meters_info(self, account_id: int):
        """Fetch meters information for the given account ID."""
        url = f"https://ikus.pesc.ru/api/v6/accounts/{account_id}/meters/info"
        headers = {"Authorization": f"Bearer {self._token}"}
        async with self._get_session().get(url, headers=headers) as response:
            if response.status != 200:
                response.raise_for_status()
            return await response.json()

    async def _retry_api_call(self, api_call: Callable[[], Any]):
        """
        Generic retry logic for API calls.
        Handles 503 errors with exponential backoff.
        Re-authenticates on 401 errors.
        """
        retries = 0
        backoff_time = INITIAL_BACKOFF

        while retries < MAX_RETRIES:
            try:
                _LOGGER.debug("Attempting API call (Attempt %d)", retries + 1)
                result = await api_call()
                _LOGGER.debug("API call succeeded")
                return result
            except ClientResponseError as err:
                if err.status == 503:  
                    _LOGGER.warning(
                        "Received 503 error during API call. Retrying in %d seconds (Attempt %d/%d)",
                        backoff_time,
                        retries + 1,
                        MAX_RETRIES,
                    )
                    await asyncio.sleep(backoff_time)
                    retries += 1
                    backoff_time *= BACKOFF_MULTIPLIER  
                elif err.status == 401: 
                    _LOGGER.warning(
                        "Received 401 error during API call. Re-authenticating..."
                    )
                    await self.authenticate()  
                else:
                    _LOGGER.error("Unexpected HTTP error during API call: %s", err)
                    raise
            except Exception as err:
                _LOGGER.error("Unexpected error during API call: %s", err)
                raise

        _LOGGER.error("Max retries reached during API call. Giving up.")
        raise Exception("Max retries reached during API call")

    async def _perform_authentication(self):
        """Perform the actual authentication request."""
        url = "https://ikus.pesc.ru/api/v8/users/auth"
        payload = {
            "type": "PHONE", 
            "login": self._username,
            "password": self._password,
        }
        async with self._get_session().post(url, json=payload) as response:
            if response.status != 200:
                response.raise_for_status()
            data = await response.json()
            self._token = data["auth"]
            _LOGGER.debug("Authentication successful")

    async def _perform_get_accounts(self):
        """Perform the actual accounts fetch request."""
        url = "https://ikus.pesc.ru/api/v8/accounts"
        headers = {"Authorization": f"Bearer {self._token}"}
        async with self._get_session().get(url, headers=headers) as response:
            if response.status != 200:
                response.raise_for_status()
            return await response.json()

    def _get_session(self):
        """Get or create an aiohttp session."""
        if self._session is None:
            self._session = self.hass.helpers.aiohttp_client.async_get_clientsession()
        return self._session
