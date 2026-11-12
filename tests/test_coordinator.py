"""Partial failures must not block other data or change meter contracts."""

import asyncio
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from helpers import (
    ConfigEntryAuthFailed,
    UpdateFailed,
    account,
    api,
    coordinator,
    make_coordinator,
    meters,
    sensor,
)


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.coordinator = make_coordinator()
        self.client = self.coordinator.client
        self.client.get_accounts = AsyncMock(return_value=[account()])
        self.client.get_meters_info = AsyncMock(return_value=meters())
        self.client.get_account_balance = AsyncMock(
            return_value={"balance": 12.5, "bill_id": "test-bill"}
        )

    async def test_400_keeps_bill_and_does_not_delay_other_accounts(self):
        self.client.get_accounts.return_value = [
            account(),
            account(102, "test-tenancy-b"),
        ]
        self.client.get_meters_info.side_effect = [
            api.EircApiResponseError(400, "275"),
            meters(),
        ]
        with patch.object(api.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            result = await self.coordinator._async_update_data()
        self.assertEqual(len(result), 2)
        self.assertEqual(result["test-tenancy-a"]["balance"], 12.5)
        self.assertEqual(result["test-tenancy-a"]["meters"], [])
        self.assertEqual(result["test-tenancy-b"]["meters"], meters())
        self.assertEqual(self.coordinator.failed_meters, {"test-tenancy-a"})
        sleep.assert_not_awaited()

    async def test_failed_bill_does_not_hide_meters(self):
        self.client.get_account_balance.side_effect = api.EircApiResponseError(400)
        result = await self.coordinator._async_update_data()
        self.assertEqual(result["test-tenancy-a"]["meters"], meters())
        self.assertIsNone(result["test-tenancy-a"]["balance"])
        self.assertEqual(self.coordinator.failed_balances, {"test-tenancy-a"})

    async def test_cached_readings_are_unavailable_and_recover_without_new_ids(self):
        self.coordinator.data = await self.coordinator._async_update_data()
        data = self.coordinator.data["test-tenancy-a"]
        meter = data["meters"][0]
        entity = sensor.EIRCMeterSensor(
            self.coordinator, data, meter, meter["indications"][0]
        )
        original_id = entity.unique_id
        original_attributes = deepcopy(entity.extra_state_attributes)
        self.client.get_meters_info.side_effect = api.EircApiResponseError(400, "275")
        self.coordinator.data = await self.coordinator._async_update_data()
        self.assertFalse(entity.available)
        self.assertEqual(entity.unique_id, original_id)
        self.assertEqual(entity.extra_state_attributes, original_attributes)
        self.client.get_meters_info.side_effect = None
        self.coordinator.data = await self.coordinator._async_update_data()
        self.assertTrue(entity.available)
        self.assertEqual(entity.unique_id, original_id)
        self.assertEqual(entity.native_value, 100)

    async def test_only_selected_confirmed_accounts_are_polled(self):
        self.coordinator.config_entry.data["selected_accounts"] = ["test-tenancy-a"]
        unconfirmed = account(103, "test-tenancy-c")
        unconfirmed["confirmed"] = False
        self.client.get_accounts.return_value = [
            account(),
            account(102, "test-tenancy-b"),
            unconfirmed,
        ]
        result = await self.coordinator._async_update_data()
        self.assertEqual(list(result), ["test-tenancy-a"])
        self.client.get_meters_info.assert_awaited_once_with(101)

    async def test_complete_failure_is_not_reported_as_success(self):
        self.client.get_meters_info.side_effect = api.EircApiResponseError(400)
        self.client.get_account_balance.side_effect = api.EircApiResponseError(400)
        with self.assertRaises(UpdateFailed):
            await self.coordinator._async_update_data()

    async def test_authentication_failure_is_not_swallowed_by_partial_handling(self):
        for error in (
            api.TwoFactorAuthRequired("test-transaction", ["EMAIL"]),
            api.EircAuthenticationError("Synthetic rejected session"),
        ):
            with self.subTest(error=type(error).__name__):
                self.client.get_meters_info.side_effect = error
                with self.assertRaises(ConfigEntryAuthFailed):
                    await self.coordinator._async_update_data()

    async def test_refresh_has_total_time_budget(self):
        async def slow_accounts():
            await asyncio.sleep(10)

        self.client.get_accounts = slow_accounts
        with (
            patch.object(coordinator, "UPDATE_TIMEOUT", 0.01),
            self.assertRaisesRegex(UpdateFailed, "time budget"),
        ):
            await self.coordinator._async_update_data()

    async def test_api_account_objects_are_not_modified(self):
        original = account()
        self.client.get_accounts.return_value = [original]
        await self.coordinator._async_update_data()
        self.assertEqual(original, account())

    async def test_no_selected_accounts_is_valid(self):
        self.coordinator.config_entry.data["selected_accounts"] = []
        self.assertEqual(await self.coordinator._async_update_data(), {})
        self.client.get_meters_info.assert_not_awaited()

    async def test_options_override_initial_account_selection(self):
        self.coordinator.config_entry.data["selected_accounts"] = ["test-tenancy-a"]
        self.coordinator.config_entry.options["selected_accounts"] = []
        self.assertEqual(await self.coordinator._async_update_data(), {})
        self.client.get_meters_info.assert_not_awaited()
