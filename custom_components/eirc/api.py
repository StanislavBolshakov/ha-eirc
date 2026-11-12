"""API client for EIRC integration."""

import asyncio
import logging
from json import JSONDecodeError
from typing import Any, Self
from urllib.parse import quote, urlparse

import aiohttp
from aiohttp import ClientResponseError, ClientSession
from aiohttp.client_exceptions import ContentTypeError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_SESSION_COOKIE, CONF_TOKEN_AUTH, CONF_TOKEN_VERIFY

_LOGGER = logging.getLogger(__name__)

MAX_RETRIES = 5
INITIAL_BACKOFF = 4
BACKOFF_MULTIPLIER = 2
REQUEST_TIMEOUT = 30
API_REQUEST_TIMEOUT = 45


class EircApiClientError(HomeAssistantError):
    """Base exception for EIRC API client errors."""


class EircApiResponseError(EircApiClientError):
    """An HTTP error without response bodies, credentials or account URLs."""

    def __init__(self, status: int, code: str | None = None) -> None:
        self.status = status
        self.code = code
        message = f"API request failed with status {status}"
        if code:
            message += f" (EIRC code {code})"
        super().__init__(message)


class EircAuthenticationError(EircApiClientError):
    """Authentication still fails after one attempt to renew the session."""


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
    _REQUEST_TIMEOUT = REQUEST_TIMEOUT

    API_BASE_URL = "https://ikus.pesc.ru/api"
    AUTH_URL = f"{API_BASE_URL}/v8/users/auth"
    ACCOUNTS_URL = f"{API_BASE_URL}/v8/accounts"
    ACCOUNT_BALANCE_URL = f"{API_BASE_URL}/v7/accounts/{{account_id}}/payments/at/current/amount/discretion"
    CURRENT_BILL_URL = (
        f"{API_BASE_URL}/v8/accounts/{{account_id}}/payments/bills/current"
    )
    BILL_UUID_URL = (
        f"{API_BASE_URL}/v7/accounts/{{account_id}}/payments/bills/{{bill_id}}/uuid"
    )
    FILE_URL = f"{API_BASE_URL}/v1/file/{{uuid}}"
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
        proxy: str | None = None,
    ) -> None:
        """Initialize the API client for a new login."""
        self.hass = hass
        self._username = username
        self._password = password
        self._proxy = proxy if self._validate_proxy_url(proxy) else None
        self._session_cookie: str | None = None
        self._token_auth: str | None = None
        self._token_verify: str | None = None
        self._session: ClientSession | None = None

        if self._proxy:
            _LOGGER.debug("EIRC client initialized with proxy: %s", self._proxy)

    def get_token_state(self) -> dict[str, str | None]:
        """Get current token state for persistence."""
        return {
            CONF_SESSION_COOKIE: self._session_cookie,
            CONF_TOKEN_AUTH: self._token_auth,
            CONF_TOKEN_VERIFY: self._token_verify,
        }

    @staticmethod
    def _validate_proxy_url(url: str | None) -> bool:
        """Validate proxy URL format without breaking existing functionality."""
        if not url:
            return False

        parsed = urlparse(url)

        if not parsed.scheme or parsed.scheme not in ("http", "https"):
            _LOGGER.warning(
                "Proxy URL '%s' may be invalid - missing http/https scheme", url
            )
        elif not parsed.netloc:
            _LOGGER.warning("Proxy URL '%s' may be invalid - missing host", url)

        return True

    @classmethod
    def from_saved_tokens(
        cls,
        hass: HomeAssistant,
        username: str,
        password: str,
        session_cookie: str,
        token_auth: str,
        token_verify: str,
        proxy: str | None = None,
    ) -> Self:
        """Instantiate the API client from saved session data."""
        client = cls(hass, username, password, proxy=proxy)
        client._session_cookie = session_cookie
        client._token_auth = token_auth
        client._token_verify = token_verify

        if client._proxy:
            _LOGGER.debug("EIRC client restored with proxy: %s", client._proxy)

        return client

    @property
    def _get_session(self) -> ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None:
            self._session = async_get_clientsession(self.hass)
        return self._session

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

    def _log_proxy_debug(self, action: str) -> None:
        """Log debug message about proxy usage if proxy is configured."""
        if self._proxy:
            _LOGGER.debug("%s via proxy: %s", action, self._proxy)

    async def _execute_request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        *,
        raw: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Execute a single HTTP request with consistent handling."""
        self._log_proxy_debug(f"Making {method.upper()} request")

        timeout = aiohttp.ClientTimeout(total=self._REQUEST_TIMEOUT)

        async with self._get_session.request(
            method, url, headers=headers, proxy=self._proxy, timeout=timeout, **kwargs
        ) as resp:
            if resp.status >= 400:
                # Read while the response is open. ClientResponseError has no
                # response attribute. Only retain a numeric business error code:
                # arbitrary server messages/bodies can contain personal data.
                code = None
                try:
                    data = await resp.json()
                    value = str(data.get("code", "")) if isinstance(data, dict) else ""
                    if value.isascii() and value.isdecimal() and len(value) <= 16:
                        code = value
                except (JSONDecodeError, ContentTypeError, UnicodeDecodeError):
                    pass
                raise EircApiResponseError(resp.status, code)
            if resp.status == 204:
                return None
            if raw:
                return await resp.read()
            try:
                return await resp.json()
            except (JSONDecodeError, ContentTypeError):
                try:
                    return await resp.text()
                except UnicodeDecodeError:
                    return await resp.read()

    async def _api_request(self, method: str, url: str, **kwargs: Any) -> Any:
        """Bound the entire request, including retries and authentication."""
        try:
            async with asyncio.timeout(API_REQUEST_TIMEOUT):
                return await self._request_with_retries(method, url, **kwargs)
        except TimeoutError as err:
            raise EircApiClientError("API request time budget exceeded") from err

    async def _request_with_retries(self, method: str, url: str, **kwargs: Any) -> Any:
        """Make a resilient API request with retry and re-authentication."""
        retries = 0
        backoff_time = INITIAL_BACKOFF
        reauthenticated = False
        # A failed write may already have been accepted by the server.
        can_retry = method.lower() in ("get", "head")

        if not self._session_cookie:
            await self._fetch_session_cookie()

        while retries < MAX_RETRIES:
            try:
                headers = self._craft_headers()
                return await self._execute_request(method, url, headers, **kwargs)

            except EircApiResponseError as err:
                if err.status in (401, 403) and url != self.AUTH_URL:
                    if reauthenticated:
                        raise EircAuthenticationError(
                            "API rejected the renewed session"
                        ) from err
                    reauthenticated = True
                    _LOGGER.warning("Received %d. Re-authenticating once.", err.status)
                    self._token_auth = None
                    if err.status == 403:
                        self._token_verify = None
                        self._session_cookie = None
                    await self.authenticate()
                    continue
                if can_retry and err.status in (429, 500, 502, 503, 504):
                    retries += 1
                    if retries >= MAX_RETRIES:
                        raise MaxRetriesExceededError(
                            f"API read retries exhausted (status {err.status})"
                        ) from err
                    _LOGGER.warning(
                        "API error %d. Retrying in %d seconds (attempt %d/%d).",
                        err.status,
                        backoff_time,
                        retries,
                        MAX_RETRIES,
                    )
                    await asyncio.sleep(backoff_time)
                    backoff_time *= BACKOFF_MULTIPLIER
                else:
                    raise

            except (TimeoutError, aiohttp.ClientConnectionError) as err:
                retries += 1
                if not can_retry or retries >= MAX_RETRIES:
                    raise EircApiClientError(
                        "API connection failed or timed out"
                    ) from err
                _LOGGER.warning(
                    "API connection failed. Retrying in %d seconds (attempt %d/%d).",
                    backoff_time,
                    retries,
                    MAX_RETRIES,
                )
                await asyncio.sleep(backoff_time)
                backoff_time *= BACKOFF_MULTIPLIER

        raise MaxRetriesExceededError("API read retries exhausted")

    async def _simple_post(self, url: str, payload: dict | None = None) -> Any:
        """Perform a simple POST that EXPECTS a JSON response."""
        headers = self._craft_headers()

        try:
            self._log_proxy_debug("Making simple POST")

            timeout = aiohttp.ClientTimeout(total=self._REQUEST_TIMEOUT)
            async with self._get_session.post(
                url, json=payload, headers=headers, proxy=self._proxy, timeout=timeout
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

        except ClientResponseError as err:
            raise EircApiClientError(f"API call failed: {err.message}") from err
        except (JSONDecodeError, ContentTypeError) as err:
            raise EircApiClientError(
                "Failed to decode server response as JSON."
            ) from err

    async def _fetch_session_cookie(self) -> None:
        """Fetch and store the session cookie, clearing old cookies first."""
        headers = {"User-Agent": self._USER_AGENT}

        try:
            self._log_proxy_debug("Fetching session cookie")

            jar = self._get_session.cookie_jar
            jar.clear_domain(urlparse(self.COOKIE_URL).hostname)
            self._session_cookie = None

            timeout = aiohttp.ClientTimeout(total=self._REQUEST_TIMEOUT)
            async with self._get_session.get(
                self.COOKIE_URL, headers=headers, proxy=self._proxy, timeout=timeout
            ) as resp:
                resp.raise_for_status()

                if self._COOKIE_NAME not in resp.cookies:
                    _LOGGER.error(
                        "Session cookie missing from response. Available cookies: %s, "
                        "Status: %s",
                        list(resp.cookies.keys()),
                        resp.status,
                    )
                    raise MissingSessionCookieError(
                        f"Cookie '{self._COOKIE_NAME}' not in response"
                    )

                self._session_cookie = resp.cookies[self._COOKIE_NAME].value
                _LOGGER.debug("Session cookie fetched successfully")

        except ClientResponseError as err:
            _LOGGER.error("HTTP error fetching session cookie: %s", err)
            raise MissingSessionCookieError("Could not fetch session cookie") from err
        except Exception as err:
            _LOGGER.exception("Unexpected error fetching session cookie")
            raise MissingSessionCookieError("Could not fetch session cookie") from err

    async def authenticate(self) -> None:
        """Authenticate and establish a session."""
        if self._token_auth and self._token_verify:
            _LOGGER.debug(
                "Authentication tokens already present, skipping authentication"
            )
            return

        if not self._session_cookie:
            await self._fetch_session_cookie()

        payload = {"type": "PHONE", "login": self._username, "password": self._password}

        try:
            headers = self._craft_headers()
            self._log_proxy_debug("Authenticating")

            timeout = aiohttp.ClientTimeout(total=self._REQUEST_TIMEOUT)
            async with self._get_session.post(
                self.AUTH_URL,
                json=payload,
                headers=headers,
                proxy=self._proxy,
                timeout=timeout,
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
            if err.status in (401, 403):
                raise EircAuthenticationError("EIRC authentication failed") from err
            raise EircApiClientError(
                f"Authentication failed with status {err.status}"
            ) from err

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
            self._log_proxy_debug("Sending 2FA email")

            timeout = aiohttp.ClientTimeout(total=self._REQUEST_TIMEOUT)
            async with self._get_session.post(
                url, headers=headers, proxy=self._proxy, timeout=timeout
            ) as resp:
                resp.raise_for_status()
            _LOGGER.debug("2FA verification email sent successfully.")
        except ClientResponseError as err:
            raise EircApiClientError("Failed to send 2FA email") from err

    async def get_accounts(self) -> list[dict[str, Any]]:
        """Fetch all accounts from the API."""
        data = await self._api_request("get", self.ACCOUNTS_URL)
        if not isinstance(data, list) or any(
            not isinstance(account, dict)
            or (
                account.get("confirmed")
                and (
                    "id" not in account
                    or not isinstance(account.get("tenancy"), dict)
                    or "register" not in account["tenancy"]
                )
            )
            for account in data
        ):
            raise EircApiClientError("Unexpected accounts response structure")
        return data

    async def get_account_balance(self, account_id: int) -> dict[str, Any]:
        """Fetch the account balance and bill ID for a given account ID."""
        url = self.CURRENT_BILL_URL.format(account_id=account_id)
        data = await self._api_request("get", url)
        if not isinstance(data, dict) or "amount" not in data:
            raise EircApiClientError("Unexpected current bill response structure")
        return {
            "balance": data["amount"],
            "bill_id": data.get("id"),
        }

    async def get_bill_uuid(self, account_id: int, bill_id: str) -> str:
        """Fetch the UUID for a specific bill."""
        url = self.BILL_UUID_URL.format(
            account_id=account_id, bill_id=quote(str(bill_id), safe="")
        )
        data = await self._api_request("get", url)
        return data

    async def download_bill_pdf(self, uuid: str) -> bytes:
        """Download the bill PDF using the UUID."""
        url = self.FILE_URL.format(uuid=uuid)
        response = await self._api_request("get", url, raw=True)
        if not isinstance(response, bytes) or not response.startswith(b"%PDF-"):
            raise EircApiClientError("Unexpected bill PDF response")
        return response

    async def get_meters_info(self, account_id: int) -> list[dict[str, Any]]:
        """Fetch meters information for a given account ID."""
        url = self.METERS_INFO_URL.format(account_id=account_id)
        data = await self._api_request("get", url)
        if not isinstance(data, list):
            raise EircApiClientError("Unexpected meters response structure")
        for meter in data:
            if (
                not isinstance(meter, dict)
                or not isinstance(meter.get("id"), dict)
                or "registration" not in meter["id"]
                or not isinstance(meter.get("indications"), list)
                or any(
                    not isinstance(indication, dict) or "meterScaleId" not in indication
                    for indication in meter["indications"]
                )
            ):
                raise EircApiClientError("Unexpected meter response structure")
        # Validate only: never normalize or rename fields from EIRC.
        return data

    async def send_meter_reading(
        self, account_id: int, meter_registration: str, readings: list[dict]
    ) -> None:
        """Send meter readings to the API."""
        url = self.SEND_METER_READING_URL.format(
            account_id=account_id,
            meter_registration=quote(str(meter_registration), safe=""),
        )
        await self._api_request("post", url, json=readings)
        _LOGGER.debug(
            "Meter reading successfully sent for meter %s.", meter_registration
        )
