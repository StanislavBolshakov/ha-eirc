"""HTTP handling and API contracts using synthetic data only."""

import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from helpers import api, meters
from yarl import URL


@asynccontextmanager
async def server(handler):
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = aiohttp.ClientSession(trust_env=False)
        self.client = api.EIRCApiClient(
            SimpleNamespace(session=self.session), "test-user", "test-password"
        )
        self.client._session_cookie = "test-cookie"
        self.client._token_auth = "test-auth"
        self.client._token_verify = "test-verification"

    async def asyncTearDown(self):
        await self.session.close()

    async def test_400_preserves_only_numeric_error_code_and_never_retries(self):
        handler = AsyncMock(
            return_value=web.json_response(
                {"code": "275", "message": "test-secret", "data": ["test-secret"]},
                status=400,
            )
        )
        async with server(handler) as url:
            with (
                patch.object(api.asyncio, "sleep", new_callable=AsyncMock) as sleep,
                self.assertRaises(api.EircApiResponseError) as caught,
            ):
                await self.client._api_request("get", url)
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.code, "275")
        self.assertNotIn("test-secret", str(caught.exception))
        self.assertNotIn(url, str(caught.exception))
        self.assertEqual(handler.await_count, 1)
        sleep.assert_not_awaited()

    async def test_non_json_error_is_not_retried(self):
        async def handler(request):
            return web.Response(
                status=400, text="test-secret", content_type="text/html"
            )

        async with server(handler) as url:
            with self.assertRaises(api.EircApiResponseError) as caught:
                await self.client._api_request("get", url)
        self.assertIsNone(caught.exception.code)
        self.assertNotIn("test-secret", str(caught.exception))

    async def test_transient_get_error_retries_and_recovers(self):
        handler = AsyncMock(
            side_effect=[
                web.json_response({}, status=503),
                web.json_response({"ok": True}),
            ]
        )
        async with server(handler) as url:
            with patch.object(api.asyncio, "sleep", new_callable=AsyncMock) as sleep:
                result = await self.client._api_request("get", url)
        self.assertEqual(result, {"ok": True})
        sleep.assert_awaited_once_with(api.INITIAL_BACKOFF)

    async def test_no_sleep_after_last_attempt(self):
        self.client._execute_request = AsyncMock(
            side_effect=api.EircApiResponseError(503)
        )
        with (
            patch.object(api.asyncio, "sleep", new_callable=AsyncMock) as sleep,
            self.assertRaises(api.MaxRetriesExceededError),
        ):
            await self.client._api_request("get", self.client.ACCOUNTS_URL)
        self.assertEqual(self.client._execute_request.await_count, api.MAX_RETRIES)
        self.assertEqual(sleep.await_count, api.MAX_RETRIES - 1)

    async def test_post_is_not_replayed_after_server_error_or_timeout(self):
        for error in (api.EircApiResponseError(503), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                self.client._execute_request = AsyncMock(side_effect=error)
                with (
                    patch.object(api.asyncio, "sleep", new_callable=AsyncMock) as sleep,
                    self.assertRaises(api.EircApiClientError),
                ):
                    await self.client._api_request("post", self.client.ACCOUNTS_URL)
                self.client._execute_request.assert_awaited_once()
                sleep.assert_not_awaited()

    async def test_rejected_write_does_not_reauthenticate_or_clear_session(self):
        for status in (401, 403):
            with self.subTest(status=status):
                before = self.client.get_token_state()
                self.client._execute_request = AsyncMock(
                    side_effect=api.EircApiResponseError(status)
                )
                self.client.authenticate = AsyncMock()
                with self.assertRaises(api.EircApiResponseError):
                    await self.client._api_request(
                        "post", self.client.SEND_METER_READING_URL
                    )
                self.client._execute_request.assert_awaited_once()
                self.client.authenticate.assert_not_awaited()
                self.assertEqual(self.client.get_token_state(), before)

    async def test_authentication_is_attempted_only_once(self):
        async def renew_session():
            self.client._session_cookie = "test-renewed-cookie"

        for status in (401, 403):
            with self.subTest(status=status):
                self.client._session_cookie = "test-cookie"
                self.client._execute_request = AsyncMock(
                    side_effect=api.EircApiResponseError(status)
                )
                self.client.authenticate = AsyncMock(side_effect=renew_session)
                with self.assertRaises(api.EircAuthenticationError):
                    await self.client._api_request("get", self.client.ACCOUNTS_URL)
                self.client.authenticate.assert_awaited_once()
                self.assertEqual(self.client._execute_request.await_count, 2)

    async def test_get_recovers_after_authentication(self):
        self.client._execute_request = AsyncMock(
            side_effect=[api.EircApiResponseError(401), []]
        )
        self.client.authenticate = AsyncMock()
        self.assertEqual(await self.client.get_accounts(), [])
        self.client.authenticate.assert_awaited_once()

    async def test_request_budget_includes_authentication(self):
        self.client._execute_request = AsyncMock(
            side_effect=api.EircApiResponseError(401)
        )

        async def slow_auth():
            await asyncio.sleep(10)

        self.client.authenticate = slow_auth
        with (
            patch.object(api, "API_REQUEST_TIMEOUT", 0.01),
            self.assertRaisesRegex(api.EircApiClientError, "time budget"),
        ):
            await self.client.get_accounts()

    async def test_cancellation_is_not_swallowed(self):
        self.client._execute_request = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.client.get_accounts()

    async def test_connection_error_is_retried_for_reads(self):
        self.client._execute_request = AsyncMock(
            side_effect=[aiohttp.ClientConnectionError(), []]
        )
        with patch.object(api.asyncio, "sleep", new_callable=AsyncMock):
            self.assertEqual(await self.client.get_accounts(), [])

    async def test_meter_payload_is_returned_unchanged(self):
        payload = meters()
        self.client._api_request = AsyncMock(return_value=payload)
        self.assertIs(await self.client.get_meters_info(101), payload)

    async def test_schema_drift_is_reported_without_body(self):
        for method, args, payload in [
            ("get_accounts", (), {"newAccounts": []}),
            ("get_accounts", (), [{"confirmed": True}]),
            ("get_meters_info", (101,), {"newMeters": []}),
            ("get_meters_info", (101,), [{"id": {"registration": "test-meter"}}]),
            (
                "get_meters_info",
                (101,),
                [{"id": {"registration": "test-meter"}, "indications": [{}]}],
            ),
            ("get_account_balance", (101,), {"newAmount": 123}),
        ]:
            with self.subTest(method=method, payload=payload):
                self.client._api_request = AsyncMock(return_value=payload)
                with self.assertRaisesRegex(
                    api.EircApiClientError, "response structure"
                ):
                    await getattr(self.client, method)(*args)

    async def test_reading_url_is_encoded_and_body_is_unchanged(self):
        payload = [{"scaleId": 1, "value": 101.5}]
        self.client._api_request = AsyncMock()
        await self.client.send_meter_reading(101, "test-meter/a b", payload)
        self.client._api_request.assert_awaited_once_with(
            "post",
            "https://ikus.pesc.ru/api/v8/accounts/101/meters/test-meter%2Fa%20b/reading",
            json=payload,
        )

    async def test_current_bill_contract_and_zero_balance(self):
        self.client._api_request = AsyncMock(
            return_value={"amount": 0, "id": "test-bill"}
        )
        self.assertEqual(
            await self.client.get_account_balance(101),
            {"balance": 0, "bill_id": "test-bill"},
        )

    async def test_rejected_login_is_an_auth_error(self):
        self.client._token_auth = None
        handler = AsyncMock(return_value=web.json_response({}, status=401))
        async with server(handler) as url:
            self.client.AUTH_URL = url
            with self.assertRaises(api.EircAuthenticationError):
                await self.client.authenticate()
        self.assertEqual(handler.await_count, 1)

    async def test_unconfirmed_accounts_need_not_have_meter_fields(self):
        payload = [{"confirmed": False}]
        self.client._api_request = AsyncMock(return_value=payload)
        self.assertIs(await self.client.get_accounts(), payload)

    async def test_pdf_is_always_bytes_including_ascii_only_files(self):
        for payload in (b"%PDF-1.4\n%%EOF", b"%PDF-1.7\n\xff\x00%%EOF"):
            with self.subTest(payload=payload):

                async def handler(request, payload=payload):
                    return web.Response(body=payload, content_type="application/pdf")

                async with server(handler) as url:
                    self.client.FILE_URL = url + "/{uuid}"
                    result = await self.client.download_bill_pdf("test-file")
                self.assertIsInstance(result, bytes)
                self.assertEqual(result, payload)

    async def test_non_pdf_response_is_rejected(self):
        async def handler(request):
            return web.Response(text="<html>test login page</html>")

        async with server(handler) as url:
            self.client.FILE_URL = url + "/{uuid}"
            with self.assertRaisesRegex(api.EircApiClientError, "bill PDF response"):
                await self.client.download_bill_pdf("test-file")

    async def test_cookie_refresh_preserves_other_integrations_cookies(self):
        self.session.cookie_jar.update_cookies(
            {"test-other-cookie": "test-value"}, URL("https://other.invalid/")
        )

        async def handler(request):
            response = web.json_response(False)
            response.set_cookie("session-cookie", "test-renewed-cookie")
            return response

        async with server(handler) as url:
            self.client.COOKIE_URL = url
            await self.client._fetch_session_cookie()
        self.assertEqual(self.client._session_cookie, "test-renewed-cookie")
        self.assertEqual(
            self.session.cookie_jar.filter_cookies(URL("https://other.invalid/"))[
                "test-other-cookie"
            ].value,
            "test-value",
        )

    async def test_authentication_rate_limit_preserves_http_status(self):
        self.client._token_auth = None

        async def handler(request):
            return web.json_response({}, status=429)

        async with server(handler) as url:
            self.client.AUTH_URL = url
            with self.assertRaises(api.EircApiResponseError) as caught:
                await self.client.authenticate()
        self.assertEqual(caught.exception.status, 429)

    async def test_verification_preserves_error_status_for_ui(self):
        for status in (400, 401, 429, 503):
            with self.subTest(status=status):

                async def handler(request, status=status):
                    return web.json_response({}, status=status)

                async with server(handler) as url:
                    self.client.EMAIL_VERIFY_URL = url + "/{transaction_id}"
                    with self.assertRaises(api.EircApiResponseError) as caught:
                        await self.client.twofa_authentication(
                            "test-transaction", "test-code"
                        )
                self.assertEqual(caught.exception.status, status)

    async def test_malformed_authentication_response_is_not_success(self):
        self.client._token_auth = None

        async def handler(request):
            return web.json_response({"unexpected": "test-response"})

        async with server(handler) as url:
            self.client.AUTH_URL = url
            with self.assertRaisesRegex(
                api.EircApiClientError, "authentication response"
            ):
                await self.client.authenticate()
