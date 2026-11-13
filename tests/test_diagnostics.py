"""Update diagnostics must not change legacy payloads or invent fresh readings."""

import asyncio
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from helpers import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
    UpdateFailed,
    account,
    api,
    coordinator,
    make_coordinator,
    meters,
    sensor,
)


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.coordinator = make_coordinator(selected=["test-tenancy-a"])
        self.client = self.coordinator.client
        self.client.get_accounts = AsyncMock(return_value=[account()])
        self.client.get_meters_info = AsyncMock(return_value=meters())
        self.client.get_account_balance = AsyncMock(
            return_value={"balance": 10, "bill_id": "test-bill"}
        )

    async def refresh(self):
        self.coordinator.data = await self.coordinator._async_update_data()
        return self.coordinator.data["test-tenancy-a"]

    def status(self):
        return sensor.EIRCUpdateStatusSensor(self.coordinator, account())

    def timestamp(self, kind):
        return sensor.EIRCLastSuccessSensor(self.coordinator, account(), kind)

    async def test_success_records_aware_api_fetch_times_outside_legacy_data(self):
        data = await self.refresh()
        self.assertEqual(self.status().native_value, "ok")
        for kind in ("meters", "balance"):
            value = self.timestamp(kind).native_value
            self.assertIsInstance(value, datetime)
            self.assertEqual(value.tzinfo, UTC)
            self.assertGreater(value, datetime(2026, 1, 1, tzinfo=UTC))
        self.assertEqual(set(data), set(account()) | {"meters", "balance", "bill_id"})
        self.assertEqual(data["meters"], meters())
        self.assertEqual(
            data["meters"][0]["indications"][0]["previousReadingDate"], "2026-01-01"
        )

    async def test_partial_failure_preserves_meter_timestamp_and_safe_error_codes(self):
        await self.refresh()
        previous = self.timestamp("meters").native_value
        self.client.get_meters_info.side_effect = api.EircApiResponseError(400, "275")
        await self.refresh()
        status = self.status()
        self.assertEqual(status.native_value, "meters_unavailable")
        self.assertTrue(status.available)
        self.assertEqual(status.extra_state_attributes["meters_http_status"], 400)
        self.assertEqual(status.extra_state_attributes["meters_eirc_code"], "275")
        self.assertEqual(self.timestamp("meters").native_value, previous)
        self.assertIsNone(status.extra_state_attributes["balance_http_status"])
        self.client.get_meters_info.side_effect = None
        await self.refresh()
        self.assertEqual(status.native_value, "ok")
        self.assertIsNone(status.extra_state_attributes["meters_http_status"])

    async def test_failed_first_meter_fetch_has_unknown_timestamp_not_current_time(
        self,
    ):
        self.client.get_meters_info.side_effect = api.EircApiResponseError(400, "275")
        await self.refresh()
        self.assertIsNone(self.timestamp("meters").native_value)
        self.assertIsNotNone(self.timestamp("balance").native_value)

    async def test_failed_balance_keeps_meter_success_independent(self):
        await self.refresh()
        previous = self.timestamp("balance").native_value
        self.client.get_account_balance.side_effect = api.EircApiResponseError(503)
        await self.refresh()
        self.assertEqual(self.status().native_value, "balance_unavailable")
        self.assertEqual(self.timestamp("balance").native_value, previous)

    async def test_both_component_errors_do_not_use_reserved_ha_unavailable_state(self):
        self.coordinator.config_entry.data["selected_accounts"].append("test-tenancy-b")
        self.client.get_accounts.return_value = [
            account(),
            account(102, "test-tenancy-b"),
        ]
        self.client.get_meters_info.side_effect = [
            api.EircApiResponseError(400, "275"),
            meters(),
        ]
        self.client.get_account_balance.side_effect = [
            api.EircApiResponseError(400, "275"),
            {"balance": 0, "bill_id": "test-bill"},
        ]
        await self.refresh()
        self.assertTrue(self.status().available)
        self.assertEqual(self.status().native_value, "data_unavailable")
        self.assertNotIn("unavailable", self.status()._attr_options)

    async def test_connection_failure_keeps_diagnostics_and_last_success_visible(self):
        await self.refresh()
        previous = self.timestamp("meters").native_value
        self.client.get_accounts.side_effect = api.EircApiClientError(
            "Synthetic outage"
        )
        with self.assertRaises(UpdateFailed):
            await self.refresh()
        self.coordinator.last_update_success = False
        self.assertEqual(self.status().native_value, "connection_error")
        self.assertTrue(self.status().available)
        self.assertTrue(self.timestamp("meters").available)
        self.assertEqual(self.timestamp("meters").native_value, previous)

    async def test_authentication_status_distinguishes_required_verification(self):
        await self.refresh()
        for error, key in [
            (
                api.EircAuthenticationError("test rejected session"),
                "authentication_required",
            ),
            (
                api.TwoFactorAuthRequired("test-transaction", ["EMAIL"]),
                "two_factor_required",
            ),
        ]:
            with self.subTest(key=key):
                self.client.get_accounts.side_effect = error
                with self.assertRaises(ConfigEntryAuthFailed) as caught:
                    await self.refresh()
                self.assertEqual(caught.exception.translation_key, key)
                self.assertEqual(self.status().native_value, key)

    async def test_diagnostic_ids_do_not_overlap_legacy_sensor_ids(self):
        await self.refresh()
        self.assertEqual(self.status().unique_id, "eirc_test-tenancy-a_update_status")
        self.assertEqual(
            self.timestamp("meters").unique_id,
            "eirc_test-tenancy-a_meters_last_success",
        )
        self.assertEqual(
            self.timestamp("balance").unique_id,
            "eirc_test-tenancy-a_balance_last_success",
        )

    async def test_manual_refresh_has_shared_cooldown(self):
        self.coordinator.async_request_refresh = AsyncMock()
        with patch.object(coordinator, "monotonic", side_effect=[100, 110, 161]):
            await self.coordinator.async_request_manual_refresh()
            with self.assertRaises(ServiceValidationError) as caught:
                await self.coordinator.async_request_manual_refresh()
            self.assertEqual(caught.exception.translation_key, "refresh_rate_limited")
            self.assertEqual(
                caught.exception.translation_placeholders, {"seconds": "50"}
            )
            await self.coordinator.async_request_manual_refresh()
        self.assertEqual(self.coordinator.async_request_refresh.await_count, 2)

    async def test_concurrent_manual_refresh_is_not_queued(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def refresh():
            started.set()
            await finish.wait()

        self.coordinator.async_request_refresh = refresh
        first = asyncio.create_task(self.coordinator.async_request_manual_refresh())
        await started.wait()
        try:
            with self.assertRaises(ServiceValidationError) as caught:
                await self.coordinator.async_request_manual_refresh()
            self.assertEqual(caught.exception.translation_key, "refresh_in_progress")
        finally:
            finish.set()
            await first

    async def test_failed_manual_refresh_reports_actionable_error(self):
        self.coordinator.async_request_refresh = AsyncMock()
        self.coordinator.last_update_success = False
        self.coordinator.update_error = "authentication_required"
        with self.assertRaises(HomeAssistantError) as caught:
            await self.coordinator.async_request_manual_refresh()
        self.assertEqual(caught.exception.translation_key, "authentication_required")

    async def test_token_updates_are_not_configuration_changes(self):
        entry = self.coordinator.config_entry
        entry.data["token_auth"] = "test-renewed-token"
        entry.data["session_cookie"] = "test-renewed-cookie"
        self.assertFalse(self.coordinator.settings_changed(entry))
        entry.options["selected_accounts"] = []
        self.assertTrue(self.coordinator.settings_changed(entry))
