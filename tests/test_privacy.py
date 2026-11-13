"""Diagnostic output is allowlisted and contains no private fixture values."""

import json
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from helpers import diagnostics, make_coordinator


class PrivacyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.coordinator = make_coordinator(selected=["private-tenancy"])
        self.entry = self.coordinator.config_entry
        self.entry.entry_id = "private-entry-id"
        self.entry.data.update(
            {
                "username": "private-phone",
                "password": "private-password",
                "session_cookie": "private-cookie",
                "token_auth": "private-token",
                "token_verify": "private-verification",
                "proxy_url": "http://private-proxy/",
                "use_proxy": True,
            }
        )
        self.coordinator.client._username = "private-phone"
        self.coordinator.client._password = "private-password"
        self.coordinator.client._session_cookie = "private-cookie"
        self.coordinator.client._token_auth = "private-token"
        self.coordinator.client._token_verify = "private-verification"
        self.coordinator.data = {
            "private-tenancy": {
                "id": "private-account-id",
                "alias": "private-name",
                "tenancy": {
                    "register": "private-tenancy",
                    "address": "private-address",
                },
                "fullName": "private-full-name",
                "email": "private-email",
                "balance": "private-balance",
                "bill_id": "private-bill",
                "meters": [
                    {
                        "id": {"registration": "private-registration"},
                        "indications": [
                            {
                                "meterScaleId": "private-scale",
                                "previousReading": "private-reading",
                            }
                        ],
                    }
                ],
            }
        }
        self.coordinator.account_errors = {
            "private-tenancy": {
                "meters": {
                    "http_status": 400,
                    "eirc_code": "275",
                    "message": "private-response",
                }
            }
        }
        self.coordinator.last_success = {
            "private-tenancy": {"balance": datetime(2026, 1, 1, tzinfo=UTC)}
        }
        self.hass = self.coordinator.hass
        self.hass.data = {"eirc": {self.entry.entry_id: self.coordinator}}

    async def test_diagnostics_contains_only_versions_counts_health_and_safe_codes(
        self,
    ):
        result = await diagnostics.async_get_config_entry_diagnostics(
            self.hass, self.entry
        )
        encoded = json.dumps(result)
        self.assertNotIn("private-", encoded)
        self.assertEqual(
            set(result), {"versions", "configuration", "coordinator", "accounts"}
        )
        self.assertEqual(
            result["configuration"],
            {"selected_account_count": 1, "proxy_enabled": True},
        )
        self.assertEqual(result["accounts"][0]["account"], 1)
        self.assertEqual(result["accounts"][0]["meter_count"], 1)
        self.assertEqual(result["accounts"][0]["scale_count"], 1)
        self.assertEqual(
            result["accounts"][0]["errors"],
            {"meters": {"http_status": 400, "eirc_code": "275"}},
        )
        self.assertFalse(result["accounts"][0]["meters_available"])
        self.assertTrue(result["accounts"][0]["balance_available"])

    async def test_diagnostics_does_not_contact_eirc(self):
        self.coordinator.client.get_accounts = AsyncMock(
            side_effect=AssertionError("network call")
        )
        self.coordinator.client.get_meters_info = AsyncMock(
            side_effect=AssertionError("network call")
        )
        await diagnostics.async_get_config_entry_diagnostics(self.hass, self.entry)
        self.coordinator.client.get_accounts.assert_not_awaited()
        self.coordinator.client.get_meters_info.assert_not_awaited()

    async def test_unexpected_errors_and_versions_cannot_export_private_values(self):
        self.coordinator.update_error = "private-error"
        self.coordinator.account_errors["private-tenancy"]["meters"] = {
            "http_status": "private-status",
            "eirc_code": "private-code",
        }
        with patch.object(
            diagnostics,
            "async_get_integration",
            new=AsyncMock(
                return_value=type("Integration", (), {"version": "private-version"})()
            ),
        ):
            result = await diagnostics.async_get_config_entry_diagnostics(
                self.hass, self.entry
            )
        self.assertNotIn("private-", json.dumps(result))
        self.assertEqual(result["versions"]["integration"], "unknown")
        self.assertEqual(result["coordinator"]["update_error"], "unknown")

    async def test_failed_setup_still_has_safe_diagnostics(self):
        self.hass.data = {}
        result = await diagnostics.async_get_config_entry_diagnostics(
            self.hass, self.entry
        )
        self.assertEqual(result["coordinator"], {"loaded": False})
        self.assertEqual(result["accounts"], [])
        self.assertNotIn("private-", json.dumps(result))
