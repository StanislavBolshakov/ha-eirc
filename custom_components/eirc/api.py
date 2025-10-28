"""API client for EIRC integration."""

import asyncio
from json import JSONDecodeError
import logging
from typing import Any, Self
from urllib.parse import urlparse

from aiohttp import ClientResponseError, ClientSession
from aiohttp.client_exceptions import ContentTypeError
import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_PROXY_URL, CONF_PROXY_TYPE

_LOGGER = logging.getLogger(__name__)

MAX_RETRIES = 5
INITIAL_BACKOFF = 4
BACKOFF_MULTIPLIER = 2


class EircApiClientError(HomeAssistantError):
    """Base exception for EIRC API client errors."""


class MaxRetriesExceededError(EircApiClientError):
    """Exception raised when the maximum number of retries is reached."""


class MissingSessionCookieError(EircApiClientError):
    """Exception raised when session-cookie cannot be fetched."""


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

    _USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.93 Safari/537.36"
    _COOKIE_NAME = "session-cookie"

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
        hass: HomeAssistant,
        username: str,
        password: str,
        proxy_url: str = None,
        proxy_type: str = "http"
    ) -> None:
        """Initialize the API client for a new login."""
        self.hass = hass
        self._username = username
        self._password = password
        self._proxy_url = proxy_url
        self._proxy_type = proxy_type
        self._session_cookie: str | None = None
        self._token_auth: str | None = None
        self._token_verify: str | None = None
        self._session: ClientSession | None = None
        self._connector: aiohttp.TCPConnector | None = None
        self._socks_connector = None

    @classmethod
    def from_saved_tokens(
        cls,
        hass: HomeAssistant,
        username: str,
        password: str,
        session_cookie: str,
        token_auth: str,
        token_verify: str,
        proxy_url: str = None,
        proxy_type: str = "http"
    ) -> Self:
        """Instantiate the API client from saved session data."""
        client = cls(hass, username, password, proxy_url, proxy_type)
        client._session_cookie = session_cookie
        client._token_auth = token_auth
        client._token_verify = token_verify
        return client

    @property
    def _get_session(self) -> ClientSession:
        """Get or create an aiohttp session with proxy support."""
        if self._session is None:
            if self._proxy_url and self._proxy_type.startswith('socks'):
                self._create_socks_connector()
                if self._socks_connector:
                    self._session = ClientSession(connector=self._socks_connector)
                else:
                    self._session = async_get_clientsession(self.hass)
            else:
                self._session = async_get_clientsession(self.hass)
        return self._session

    def _create_socks_connector(self):
        """Create SOCKS connector with auth support."""
        if not self._proxy_url or not self._proxy_type.startswith('socks'):
            return

        try:
            from aiohttp_socks import ProxyConnector, ProxyType

            parsed = urlparse(self._proxy_url)

            if not parsed.scheme:
                self._proxy_url = f"socks5://{self._proxy_url}"
                parsed = urlparse(self._proxy_url)

            host = parsed.hostname
            port = parsed.port or 1080
            username = parsed.username
            password = parsed.password

            _LOGGER.debug("Setting up %s proxy: %s:%s (user: %s)",
                         self._proxy_type, host, port, username)

            proxy_type = {
                'socks4': ProxyType.SOCKS4,
                'socks5': ProxyType.SOCKS5,
                'socks5h': ProxyType.SOCKS5
            }.get(self._proxy_type, ProxyType.SOCKS5)

            self._socks_connector = ProxyConnector(
                proxy_type=proxy_type,
                host=host,
                port=port,
                username=username,
                password=password,
                rdns=True,
                verify_ssl=False
            )

            _LOGGER.info("SOCKS proxy connector created successfully")

        except ImportError:
            _LOGGER.error("aiohttp_socks not installed. SOCKS proxy will not work.")
        except Exception as e:
            _LOGGER.error("Error creating SOCKS proxy connector: %s", e)

    def _craft_headers(self) -> dict[str, str]:
        """Generate headers for API requests."""
        if not self._session_cookie:
            raise MissingSessionCookieError("Session cookie is missing")

        headers = {
            "Cookie": f"{self._COOKIE_NAME}={self._session_cookie}",
            "Content-Type": "application/json",
            "User-Agent": self._USER_AGENT,
        }
        if self._token_auth:
            headers["Authorization"] = f"Bearer {self._token_auth}"
        if self._token_verify:
            headers["Auth-Verification"] = self._token_verify
        return headers

    async def _api_request(self, method: str, url: str, **kwargs: Any) -> Any:
        """Make a resilient API request with retry and re-authentication."""
        retries = 0
        backoff_time = INITIAL_BACKOFF

        if not self._session_cookie:
            await self._fetch_session_cookie()

        while retries < MAX_RETRIES:
            try:
                headers = self._craft_headers()
                request_kwargs = {"headers": headers, **kwargs}

                if self._proxy_url and not self._proxy_type.startswith('socks'):
                    request_kwargs["proxy"] = self._proxy_url
                    _LOGGER.debug("Using HTTP proxy: %s", self._proxy_url)

                async with self._get_session.request(
                    method, url, **request_kwargs
                ) as resp:
                    resp.raise_for_status()
                    if resp.status == 204:
                        return None
                    try:
                        return await resp.json()
                    except (JSONDecodeError, ContentTypeError):
                        return await resp.text()

            except ClientResponseError as err:
                if err.status == 401 and url != self.AUTH_URL:
                    _LOGGER.warning("Received 401 Unauthorized. Re-authenticating.")
                    self._token_auth = None
                    await self.authenticate()
                elif err.status in [400, 429, 500, 503]:
                    _LOGGER.warning(
                        "API error %d. Retrying in %d seconds.",
                        err.status,
                        backoff_time,
                    )
                    await asyncio.sleep(backoff_time)
                    backoff_time *= BACKOFF_MULTIPLIER
                else:
                    raise EircApiClientError(
                        f"API request failed with status {err.status}"
                    ) from err
            except Exception as err:
                _LOGGER.error("Caught unexpected exception in _api_request: %s", err)
                raise EircApiClientError(
                    "An unexpected error occurred during API request"
                ) from err
            retries += 1

        raise MaxRetriesExceededError(f"Max retries reached for API call to {url}")

    async def _simple_post(self, url: str, payload: dict | None = None) -> Any:
        """Perform a simple POST that EXPECTS a JSON response."""
        headers = self._craft_headers()
        try:
            request_kwargs = {"headers": headers}

            if self._proxy_url and not self._proxy_type.startswith('socks'):
                request_kwargs["proxy"] = self._proxy_url

            async with self._get_session.post(
                url, json=payload, **request_kwargs
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

        except ClientResponseError as err:
            raise EircApiClientError(f"API call failed: {err.message}") from err
        except (JSONDecodeError, ContentTypeError):
            raise EircApiClientError("Failed to decode server response as JSON.")

    async def _fetch_session_cookie(self) -> None:
        """Fetch and store the session cookie."""
        headers = {"User-Agent": self._USER_AGENT}
        try:
            request_kwargs = {"headers": headers}

            if self._proxy_url and not self._proxy_type.startswith('socks'):
                request_kwargs["proxy"] = self._proxy_url

            async with self._get_session.get(self.COOKIE_URL, **request_kwargs) as resp:
                resp.raise_for_status()
                if self._COOKIE_NAME not in resp.cookies:
                    raise MissingSessionCookieError(
                        f"Cookie '{self._COOKIE_NAME}' not in response"
                    )
                self._session_cookie = resp.cookies[self._COOKIE_NAME].value
                _LOGGER.debug("Session cookie fetched successfully.")
        except ClientResponseError as err:
            raise MissingSessionCookieError("Could not fetch session cookie") from err

    async def authenticate(self) -> None:
        """Authenticate and establish a session."""
        if self._token_auth and self._token_verify:
            return

        if not self._session_cookie:
            await self._fetch_session_cookie()

        payload = {"type": "PHONE", "login": self._username, "password": self._password}

        try:
            headers = self._craft_headers()
            request_kwargs = {"headers": headers}

            if self._proxy_url and not self._proxy_type.startswith('socks'):
                request_kwargs["proxy"] = self._proxy_url

            async with self._get_session.post(
                self.AUTH_URL, json=payload, **request_kwargs
            ) as response:
                if response.status == 424:
                    data = await response.json()
                    raise TwoFactorAuthRequired(
                        data.get("transactionId"), data.get("types", [])
                    )

                response.raise_for_status()
                data = await response.json()
                self._token_auth = data.get("auth")
                _LOGGER.debug("Authentication successful.")
        except ClientResponseError as err:
            raise EircApiClientError(f"Authentication failed: {err}") from err

    async def twofa_authentication(
        self, transaction_id: str, code: str
    ) -> tuple[str, str]:
        """Verify the 2FA code. This call expects a JSON response."""
        url = self.EMAIL_VERIFY_URL.format(transaction_id=transaction_id)
        data = await self._simple_post(url, {"code": code})

        if data is None:
            raise EircApiClientError(
                "Received an empty response during 2FA verification."
            )

        token_auth = data.get("auth")
        token_verify = data.get("verified")

        if not token_auth or not token_verify:
            raise EircApiClientError("2FA response missing tokens")

        self._token_auth = token_auth
        self._token_verify = token_verify
        _LOGGER.info("2FA authentication successful.")

        return token_auth, token_verify

    async def twofa_send_email(self, transaction_id: str) -> None:
        """Trigger the 2FA email. This call does NOT expect a JSON response."""
        url = self.EMAIL_CHECK_URL.format(transaction_id=transaction_id)
        headers = self._craft_headers()

        try:
            request_kwargs = {"headers": headers}

            if self._proxy_url and not self._proxy_type.startswith('socks'):
                request_kwargs["proxy"] = self._proxy_url

            async with self._get_session.post(url, **request_kwargs) as resp:
                resp.raise_for_status()
            _LOGGER.debug("2FA verification email sent successfully.")
        except ClientResponseError as err:
            raise EircApiClientError("Failed to send 2FA email") from err

    async def get_accounts(self) -> list[dict[str, Any]]:
        """Fetch all accounts from the API."""
        return await self._api_request("get", self.ACCOUNTS_URL)

    async def get_account_balance(self, account_id: int) -> float:
        """Fetch the account balance for a given account ID."""
        url = self.ACCOUNT_BALANCE_URL.format(account_id=account_id)
        data = await self._api_request("get", url)
        return sum(
            item.get("charge", {}).get("accrued", 0)
            for item in data
            if item.get("checked")
        )

    async def get_meters_info(self, account_id: int) -> dict[str, Any]:
        """Fetch meters information for a given account ID."""
        url = self.METERS_INFO_URL.format(account_id=account_id)
        return await self._api_request("get", url)

    async def send_meter_reading(
        self, account_id: int, meter_registration: str, readings: list[dict]
    ) -> None:
        """Send meter readings to the API."""
        url = self.SEND_METER_READING_URL.format(
            account_id=account_id, meter_registration=meter_registration
        )
        await self._api_request("post", url, json=readings)
        _LOGGER.debug(
            "Meter reading successfully sent for meter %s.", meter_registration
        )

    async def close(self):
        """Close the session and connector."""
        if self._session:
            await self._session.close()
        if self._socks_connector:
            await self._socks_connector.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Cleanup on exit."""
        await self.close()
