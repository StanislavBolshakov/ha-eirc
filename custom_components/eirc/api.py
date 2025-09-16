"""API client for EIRC integration."""

import asyncio
from collections.abc import Callable
from json.decoder import JSONDecodeError
import logging
from typing import Any, Optional, Dict

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


class MissingSessionCookieError(Exception):
    """Exception raised when session-cookie cannot be fetched from the API."""


class TwoFactorAuthRequired(HomeAssistantError):
    """Raised when 2FA is required during authentication."""

    def __init__(self, transaction_id: str, methods: list[str]) -> None:
        super().__init__(
            f"2FA required. Transaction ID: {transaction_id}, Methods: {methods}"
        )
        self.transaction_id = transaction_id
        self.methods = methods


class EIRCApiClient:
    """API client for interacting with the EIRC system."""

    API_BASE_URL = "https://ikus.pesc.ru/api"
    AUTH_URL = f"{API_BASE_URL}/v8/users/auth"
    ACCOUNTS_URL = f"{API_BASE_URL}/v8/accounts"
    ACCOUNT_BALANCE_URL = f"{API_BASE_URL}/v7/accounts/{{account_id}}/payments/at/current/amount/discretion"
    METERS_INFO_URL = f"{API_BASE_URL}/v6/accounts/{{account_id}}/meters/info"
    SEND_METER_READING_URL = f"{API_BASE_URL}/v8/accounts/{{account_id}}/meters/{{meter_registration}}/reading"
    COOKIE_URL = f"{API_BASE_URL}/v6/users/manual/existence"
    EMAIL_CHECK_URL = (
        f"{API_BASE_URL}/v7/users/{{transaction_id}}/email/check/confirmation/send"
    )
    EMAIL_VERIFY_URL = (
        f"{API_BASE_URL}/v7/users/{{transaction_id}}/email/check/verification"
    )

    def __init__(
        self,
        hass: HomeAssistant | None,
        username: str,
        password: str,
        session_cookie: str | None = None,
        token_auth: str | None = None,
        token_verify: str | None = None,
    ) -> None:
        self.hass = hass
        self._username = username
        self._password = password
        self._session_cookie = session_cookie
        self._token_auth = token_auth
        self._token_verify = token_verify
        self._session = None

    async def authenticate(self):
        """Authenticate with the API and store session/auth info."""
        if self._token_auth and self._token_verify and self._session_cookie:
            _LOGGER.debug("Using existing auth and verify token and session cookie")
            return

        if not self._session_cookie:
            await self._fetch_session_cookie()

        try:
            await self._retry_api_call(self._perform_authentication, url=self.AUTH_URL)
        except TwoFactorAuthRequired as e:
            self._transaction_id = e.transaction_id
            self._methods = e.methods
            _LOGGER.warning(
                "2FA required. Transaction ID: %s, available methods: %s",
                self._transaction_id,
                self._methods,
            )
            raise

        if self._token_auth:
            _LOGGER.debug("Authentication successful, auth token stored")

    async def get_accounts(self):
        """Fetch accounts from the API."""
        return await self._retry_api_call(
            self._perform_get_accounts, url=self.ACCOUNTS_URL
        )

    async def get_account_balance(self, account_id: int) -> float:
        """Fetch the account balance for the given account ID."""
        url = self.ACCOUNT_BALANCE_URL.format(account_id=account_id)
        data = await self._retry_api_call(
            self._perform_get_account_balance, account_id, url=url
        )
        total_balance = sum(
            item["charge"]["accrued"] for item in data if item.get("checked", False)
        )
        _LOGGER.debug(
            "Fetched account balance for account %d: %f", account_id, total_balance
        )
        return total_balance

    async def get_meters_info(self, account_id: int):
        """Fetch meters information for the given account ID."""
        url = self.METERS_INFO_URL.format(account_id=account_id)
        return await self._retry_api_call(
            self._perform_get_meters_info, account_id, url=url
        )

    async def _fetch_session_cookie(self) -> None:
        """Fetch and store cookies."""

        _LOGGER.debug("Setting session cookie via GET request to: %s", self.COOKIE_URL)

        async with self._get_session().get(self.COOKIE_URL) as resp:
            _LOGGER.debug("Cookie endpoint response status: %d", resp.status)
            if not resp.cookies:
                raise MissingSessionCookieError(
                    f"No cookies found in response from {self.COOKIE_URL}. "
                    f"Status: {resp.status}"
                )
            cookies_dict = {name: cookie.value for name, cookie in resp.cookies.items()}
            _LOGGER.debug("Cookie endpoint response result: %s", cookies_dict)
            self._session_cookie = cookies_dict.get("session-cookie")
            return cookies_dict.get("session-cookie")

    def _craft_headers(self) -> Dict[str, str]:
        """Generate headers with session cookie, Bearer and verification tokens."""

        if not self._session_cookie:
            raise MissingSessionCookieError("Session cookie is missing")

        headers = {}
        headers["Cookie"] = f"session-cookie={self._session_cookie}"
        headers["Content-Type"] = "application/json"

        if self._token_auth:
            headers["Authorization"] = f"Bearer {self._token_auth}"

        if self._token_verify:
            headers["Auth-Verification"] = self._token_verify

        return headers

    async def twofa_send_email(self, transaction_id: str) -> None:
        """Trigger sending of the verification email for 2FA."""
        url = self.EMAIL_CHECK_URL.format(transaction_id=transaction_id)
        headers = self._craft_headers()
        async with self._get_session().post(url, headers=headers) as resp:
            resp.raise_for_status()
        _LOGGER.debug("2FA verification email sent")

    async def twofa_authentication(self, transaction_id: str, code: str) -> None:
        """Verify the email code and store authentication + verification tokens."""
        url = self.EMAIL_VERIFY_URL.format(transaction_id=transaction_id)
        headers = self._craft_headers()
        payload = {"code": code}

        async with self._get_session().post(url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()

            token_auth = data.get("auth")
            if not token_auth:
                raise RuntimeError(f"No authentication token found in response: {data}")

            token_verify = data.get("verified")
            if not token_verify:
                raise RuntimeError(f"No verification token found in response: {data}")

            self._token_auth = token_auth
            self._token_verify = token_verify
            _LOGGER.info("Authentication and verification tokens are set")
            _LOGGER.debug("Email verification successful, token stored")
            return token_auth, token_verify

    async def _retry_api_call(self, api_call: Callable[..., Any], *args, **kwargs):
        """Retry API calls with exponential backoff."""
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
                if err.status in [429, 500, 503]:
                    _LOGGER.warning(
                        "Received %d error during API call to %s. Retrying in %d seconds (Attempt %d/%d)",
                        err.status,
                        api_endpoint,
                        backoff_time,
                        retries + 1,
                        MAX_RETRIES,
                    )
                    await asyncio.sleep(backoff_time)
                    retries += 1
                    backoff_time *= BACKOFF_MULTIPLIER
                elif err.status == 424:
                    raise
                elif err.status == 401:
                    _LOGGER.warning(
                        "Received 401 error during API call to %s. Will try to regenerate auth token",
                        api_endpoint,
                    )
                    self._token_auth = None
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
        """Perform the actual authentication request."""
        payload = {"type": "PHONE", "login": self._username, "password": self._password}
        headers = self._craft_headers()

        async with self._get_session().post(
            self.AUTH_URL, json=payload, headers=headers
        ) as response:
            if response.status == 424:
                data = await response.json()
                transaction_id = data.get("transactionId")
                methods = data.get("types", [])

                _LOGGER.warning(
                    "Authentication requires verification. Transaction ID: %s, Raw response: %s",
                    transaction_id,
                    methods,
                )

                if not transaction_id:
                    raise RuntimeError(
                        "Auth returned 424 but no transactionId in response"
                    )

                if "EMAIL" not in methods:
                    raise RuntimeError(
                        f"2FA method EMAIL not available. Supported: {methods}"
                    )

                raise TwoFactorAuthRequired(transaction_id, methods)

            response.raise_for_status()
            data = await response.json()
            self._token_auth = data["auth"]
            _LOGGER.debug("Authentication successful")

    async def _perform_get_accounts(self):
        """Perform the actual accounts fetch request."""
        headers = self._craft_headers()
        async with self._get_session().get(
            self.ACCOUNTS_URL, headers=headers
        ) as response:
            response.raise_for_status()
            return await response.json()

    async def _perform_get_account_balance(self, account_id: int):
        """Perform the actual balance fetch request."""
        headers = self._craft_headers()
        async with self._get_session().get(
            self.ACCOUNT_BALANCE_URL.format(account_id=account_id), headers=headers
        ) as response:
            response.raise_for_status()
            return await response.json()

    async def _perform_get_meters_info(self, account_id: int):
        """Perform the actual meters info fetch request."""
        headers = self._craft_headers()
        async with self._get_session().get(
            self.METERS_INFO_URL.format(account_id=account_id), headers=headers
        ) as response:
            response.raise_for_status()
            return await response.json()

    def _get_session(self):
        """Get or create an aiohttp session."""
        if self._session is None:
            self._session = async_get_clientsession(self.hass)
        return self._session

    async def send_meter_reading(
        self, account_id: int, meter_registration: str, readings: list[dict]
    ) -> bool:
        """Send meter readings to the API with validation and error handling."""
        url = self.SEND_METER_READING_URL.format(
            account_id=account_id, meter_registration=meter_registration
        )
        headers = self._craft_headers()
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
