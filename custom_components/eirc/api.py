"""API client for EIRC integration."""

import asyncio
from collections.abc import Callable
from json.decoder import JSONDecodeError
import logging
import time
from typing import Any

from aiohttp import ClientResponseError

from homeassistant.core import HomeAssistant 
from homeassistant.exceptions import HomeAssistantError 
from homeassistant.helpers.aiohttp_client import async_get_clientsession 

_LOGGER = logging.getLogger(__name__)

MAX_RETRIES = 5
INITIAL_BACKOFF = 4
BACKOFF_MULTIPLIER = 2


class MaxRetriesExceededError(Exception):
    """Exception raised when the maximum number of retries is reached during an API call."""


class EIRCApiClient:
    """API client for interacting with the EIRC system.

    This class provides methods for authenticating with the EIRC API, fetching accounts,
    retrieving account balances, and sending meter readings. It handles retries, exponential
    backoff, and caching of data.

    """

    API_BASE_URL = "https://ikus.pesc.ru/api"
    AUTH_URL = f"{API_BASE_URL}/v8/users/auth"
    ACCOUNTS_URL = f"{API_BASE_URL}/v8/accounts"
    ACCOUNT_BALANCE_URL = f"{API_BASE_URL}/v7/accounts/{{account_id}}/payments/at/current/amount/discretion"
    METERS_INFO_URL = f"{API_BASE_URL}/v6/accounts/{{account_id}}/meters/info"
    SEND_METER_READING_URL = f"{API_BASE_URL}/v8/accounts/{{account_id}}/meters/{{meter_registration}}/reading"

    def __init__(
        self,
        hass: HomeAssistant | None,
        username: str,
        password: str,
        cache_ttl: int = 300,
    ) -> None:
        """Initialize the EIRCApiClient.

        Args:
            hass: Home Assistant instance to get the session.
            username: Username for authentication.
            password: Password for authentication.
            cache_ttl: Time-to-live for cache in seconds (default: 300).

        """
        self.hass = hass
        self._username = username
        self._password = password
        self._session = None
        self._token = None
        self._cache = {}
        self.cache_ttl = cache_ttl

    async def authenticate(self):
        """Authenticate with the API and store the token.

        This function calls the API to authenticate and retrieve an authentication token,
        storing it for subsequent requests.
        """
        await self._retry_api_call(self._perform_authentication, url=self.AUTH_URL)

    async def get_accounts(self):
        """Fetch accounts from the API.

        This function checks for cached account data and returns it if available.
        Otherwise, it retrieves the data from the API and caches it for future use.

        Returns:
            list: The list of accounts retrieved from the API.

        """
        cache_key = self._get_cache_key(self.ACCOUNTS_URL)
        cached_data = self._get_cached_data(cache_key)
        if cached_data is not None:
            _LOGGER.debug("Serving accounts from cache")
            return cached_data
        result = await self._retry_api_call(
            self._perform_get_accounts, url=self.ACCOUNTS_URL
        )
        self._store_cache(cache_key, result)
        return result

    async def get_account_balance(self, account_id: int) -> float:
        """Fetch the account balance for the given account ID.

        This function checks for cached account balance data and returns it if available.
        Otherwise, it retrieves the data from the API, processes it, and caches it for future use.

        Args:
            account_id (int): The account ID for which the balance is requested.

        Returns:
            float: The total balance of the account.

        """
        url = self.ACCOUNT_BALANCE_URL.format(account_id=account_id)
        cache_key = self._get_cache_key(url)
        cached_data = self._get_cached_data(cache_key)
        if cached_data is not None:
            _LOGGER.debug(
                "Serving account balance from cache for account %d", account_id
            )
            data = cached_data
        else:
            data = await self._retry_api_call(
                self._perform_get_account_balance, account_id, url=url
            )
            self._store_cache(cache_key, data)
        total_balance = sum(
            item["charge"]["accrued"] for item in data if item.get("checked", False)
        )
        _LOGGER.debug(
            "Fetched account balance for account %d: %f", account_id, total_balance
        )
        return total_balance

    async def get_meters_info(self, account_id: int):
        """Fetch meters information for the given account ID.

        This function checks for cached meters information and returns it if available.
        Otherwise, it retrieves the data from the API and caches it for future use.

        Args:
            account_id (int): The account ID for which the meter information is requested.

        Returns:
            dict: The meters information for the specified account.

        """
        url = self.METERS_INFO_URL.format(account_id=account_id)
        cache_key = self._get_cache_key(url)
        cached_data = self._get_cached_data(cache_key)
        if cached_data is not None:
            _LOGGER.debug("Serving meters info from cache for account %d", account_id)
            return cached_data
        result = await self._retry_api_call(
            self._perform_get_meters_info, account_id, url=url
        )
        self._store_cache(cache_key, result)
        return result

    async def _retry_api_call(self, api_call: Callable[..., Any], *args, **kwargs):
        """Retry API calls with exponential backoff.

        This function handles retries for API calls. It retries the call in case of
        503 errors with exponential backoff and re-authenticates in case of 401 errors.

        Args:
            api_call (Callable): The API call to execute.
            *args: Arguments for the API call.
            **kwargs: Keyword arguments for the API call.

        Returns:
            Any: The result of the API call.

        Raises:
            Exception: If the maximum number of retries is reached or if an unexpected error occurs.

        """
        retries = 0
        backoff_time = INITIAL_BACKOFF
        api_endpoint = kwargs.get("url", "Unknown URL")
        kwargs.pop("url", None)
        while retries < MAX_RETRIES:
            try:
                _LOGGER.debug(
                    "Attempting API call to %s (Attempt %d/%d)",
                    api_endpoint,
                    retries + 1,
                    MAX_RETRIES,
                )
                result = await api_call(*args, **kwargs)
                _LOGGER.debug("API call to %s succeeded", api_endpoint)
                return result
            except ClientResponseError as err:
                if err.status in [429,500,503]:
                    _LOGGER.warning(
                        "Received 503 error during API call to %s. Retrying in %d seconds (Attempt %d/%d)",
                        api_endpoint,
                        backoff_time,
                        retries + 1,
                        MAX_RETRIES,
                    )
                    await asyncio.sleep(backoff_time)
                    retries += 1
                    backoff_time *= BACKOFF_MULTIPLIER
                elif err.status == 401:
                    _LOGGER.warning(
                        "Received 401 error during API call to %s. Re-authenticating",
                        api_endpoint,
                    )
                    await self.authenticate()
                else:
                    _LOGGER.error(
                        "Unexpected HTTP error during API call to %s: %s",
                        api_endpoint,
                        err,
                    )
                    raise
            except Exception as err:
                _LOGGER.error(
                    "Unexpected error during API call to %s: %s", api_endpoint, err
                )
                raise

        _LOGGER.error(
            "Max retries reached during API call to %s. Giving up", api_endpoint
        )
        raise MaxRetriesExceededError("Max retries reached during API call")

    async def _perform_authentication(self):
        """Perform the actual authentication request.

        This function sends the authentication request to the API and stores the authentication token.

        Raises:
            HTTPError: If the authentication request fails.

        """
        payload = {"type": "PHONE", "login": self._username, "password": self._password}
        async with self._get_session().post(self.AUTH_URL, json=payload) as response:
            response.raise_for_status()
            data = await response.json()
            self._token = data["auth"]
            _LOGGER.debug("Authentication successful")

    async def _perform_get_accounts(self):
        """Perform the actual accounts fetch request."""
        headers = {"Authorization": f"Bearer {self._token}"}
        async with self._get_session().get(
            self.ACCOUNTS_URL, headers=headers
        ) as response:
            response.raise_for_status()
            return await response.json()

    async def _perform_get_account_balance(self, account_id: int):
        """Perform the actual balance fetch request."""
        headers = {"Authorization": f"Bearer {self._token}"}
        async with self._get_session().get(
            self.ACCOUNT_BALANCE_URL.format(account_id=account_id), headers=headers
        ) as response:
            response.raise_for_status()
            return await response.json()

    async def _perform_get_meters_info(self, account_id: int):
        """Perform the actual meters info fetch request."""
        headers = {"Authorization": f"Bearer {self._token}"}
        async with self._get_session().get(
            self.METERS_INFO_URL.format(account_id=account_id), headers=headers
        ) as response:
            response.raise_for_status()
            return await response.json()

    def _get_cache_key(self, url: str) -> str:
        """Generate a cache key based on the URL.

        Args:
            url (str): The URL to generate the cache key for.

        Returns:
            str: The generated cache key.

        """
        return url

    def _get_cached_data(self, cache_key: str) -> Any:
        """Retrieve cached data if it exists and is not expired.

        Args:
            cache_key (str): The cache key to look up.

        Returns:
            Any: The cached data, or None if no valid cached data exists.

        """
        if cache_key not in self._cache:
            return None
        data, timestamp = self._cache[cache_key]
        current_time = time.time()
        if current_time - timestamp > self.cache_ttl:
            del self._cache[cache_key]
            return None
        return data

    def _store_cache(self, cache_key: str, data: Any) -> None:
        """Store data in the cache with the current timestamp.

        Args:
            cache_key (str): The cache key to store the data under.
            data (Any): The data to cache.

        """
        self._cache[cache_key] = (data, time.time())

    def _get_session(self):
        """Get or create an aiohttp session.

        Returns:
            ClientSession: The aiohttp session used for API requests.

        """
        if self._session is None:
            self._session = async_get_clientsession(self.hass)
        return self._session

    async def send_meter_reading(
        self, account_id: int, meter_registration: str, readings: list[dict]
    ) -> bool:
        """Send meter readings to the API with validation and error handling.

        Args:
            account_id (int): The account ID for which the readings are being sent.
            meter_registration (str): The meter registration ID.
            readings (list[dict]): A list of meter readings to be sent.

        Returns:
            bool: True if the readings were successfully sent, False otherwise.

        Raises:
            HomeAssistantError: If the API request fails.

        """
        url = self.SEND_METER_READING_URL.format(
            account_id=account_id, meter_registration=meter_registration
        )
        headers = {"Authorization": f"Bearer {self._token}"}
        _LOGGER.debug("Sending readings to %s with payload: %s", url, readings)

        try:
            return await self._retry_api_call(
                lambda: self._perform_send_reading(url, headers, readings), url=url
            )
        except ClientResponseError as err:
            raise HomeAssistantError(
                f"Failed to send meter readings: {err.message}"
            ) from err

    async def _perform_send_reading(
        self, url: str, headers: dict, readings: list[dict]
    ) -> bool:
        """Perform the actual send reading request and handle the response."""
        try:
            async with self._get_session().post(
                url, json=readings, headers=headers
            ) as resp:
                if resp.status != 200:
                    if 500 <= resp.status < 600:
                        try:
                            error_data = await resp.json()
                            error_message = error_data.get(
                                "message", "No message provided"
                            )
                        except JSONDecodeError:
                            error_message = "Failed to parse error response"
                        except Exception as e:  # noqa: BLE001
                            error_message = f"Unexpected error occurred: {e!s}"
                            _LOGGER.error(
                                "Unexpected error while processing the response: %s",
                                str(e),
                            )
                        _LOGGER.error(
                            "Failed to send readings: HTTP %d, Error: %s",
                            resp.status,
                            error_message,
                        )
                    else:
                        error_message = f"HTTP {resp.status} error occurred"
                        _LOGGER.error("Failed to send readings: %s", error_message)
                    raise ClientResponseError(  # noqa: TRY301
                        resp.request_info,
                        resp.history,
                        status=resp.status,
                        message=error_message,
                    )

                result = await resp.text()
                _LOGGER.debug("Meter reading successfully sent: %s", result)
                return True
        except Exception as e:
            _LOGGER.error("Error during send meter reading POST request: %s", str(e))
            raise
