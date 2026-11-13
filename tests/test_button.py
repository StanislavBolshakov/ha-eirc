"""Refresh controls use existing devices and one coordinator-wide rate limit."""

import unittest
from unittest.mock import AsyncMock, MagicMock

from helpers import account, button, make_coordinator


class ButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_button_keeps_existing_device_and_works_during_outage(self):
        coordinator = make_coordinator()
        coordinator.data = {"test-tenancy-a": account()}
        coordinator.last_update_success = False
        coordinator.async_request_manual_refresh = AsyncMock()
        entity = button.EIRCRefreshButton(coordinator, "test-tenancy-a")
        self.assertEqual(entity.unique_id, "eirc_test-tenancy-a_refresh")
        self.assertEqual(
            entity._attr_device_info, {"identifiers": {("eirc", "test-tenancy-a")}}
        )
        self.assertTrue(entity.available)
        await entity.async_press()
        coordinator.async_request_manual_refresh.assert_awaited_once()

    async def test_button_setup_filters_accounts_and_does_not_duplicate_entities(self):
        coordinator = make_coordinator(selected=["test-tenancy-a"])
        coordinator.data = {
            "test-tenancy-a": account(),
            "test-tenancy-b": account(102, "test-tenancy-b"),
        }
        entry = coordinator.config_entry
        entry.entry_id = "test-entry"
        hass = MagicMock()
        hass.data = {"eirc": {entry.entry_id: coordinator}}
        added = []
        await button.async_setup_entry(hass, entry, added.extend)
        self.assertEqual(len(added), 1)
        for listener in coordinator.listeners:
            listener()
        self.assertEqual(len(added), 1)
        entry.async_on_unload.assert_called_once()
