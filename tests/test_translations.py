"""Every new control, status and authentication error has RU/EN text."""

import json
import unittest
from pathlib import Path

from helpers import sensor


class TranslationTests(unittest.TestCase):
    def test_diagnostics_controls_and_errors_have_translations(self):
        root = (
            Path(__file__).resolve().parents[1] / "custom_components/eirc/translations"
        )
        for language in ("ru", "en"):
            with self.subTest(language=language):
                data = json.loads(
                    (root / f"{language}.json").read_text(encoding="utf-8")
                )
                self.assertTrue(data["entity"]["button"]["refresh"]["name"])
                for key in (
                    "update_status",
                    "meters_last_success",
                    "balance_last_success",
                ):
                    self.assertTrue(data["entity"]["sensor"][key]["name"])
                states = data["entity"]["sensor"]["update_status"]["state"]
                self.assertEqual(
                    set(states), set(sensor.EIRCUpdateStatusSensor._attr_options)
                )
                for key in (
                    "rate_limited",
                    "session_expired",
                    "invalid_code",
                    "cannot_connect",
                    "invalid_auth",
                    "unknown",
                ):
                    self.assertTrue(data["config"]["error"][key])
                for key in (
                    "2fa_method_not_supported",
                    "2fa_send_failed",
                    "rate_limited",
                ):
                    self.assertTrue(data["config"]["abort"][key])
                for key in ("reauth_required", "cannot_fetch_accounts"):
                    self.assertTrue(data["options"]["abort"][key])
                for key in (
                    "refresh_in_progress",
                    "refresh_rate_limited",
                    "refresh_failed",
                    "connection_error",
                    "update_failed",
                    "authentication_required",
                    "two_factor_required",
                ):
                    self.assertTrue(data["exceptions"][key]["message"])
                self.assertIn(
                    "{seconds}", data["exceptions"]["refresh_rate_limited"]["message"]
                )

    def test_reading_forms_and_results_are_translated(self):
        root = (
            Path(__file__).resolve().parents[1] / "custom_components/eirc/translations"
        )
        for language in ("ru", "en"):
            data = json.loads((root / f"{language}.json").read_text(encoding="utf-8"))
            for step in ("init", "send_readings", "reading_values", "reading_confirm"):
                self.assertTrue(data["options"]["step"][step]["title"])
            for key in (
                "invalid_account",
                "readings_unavailable",
                "invalid_readings",
                "confirmation_required",
            ):
                self.assertTrue(data["options"]["error"][key])
            for key in (
                "readings_unavailable",
                "readings_sent",
                "readings_rejected",
                "readings_result_unknown",
                "readings_partial",
            ):
                self.assertTrue(data["options"]["abort"][key])
