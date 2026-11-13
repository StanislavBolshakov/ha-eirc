"""Exercise real HA APIs when HA is installed; never use live configuration."""

import asyncio
import importlib
import importlib.util
import logging
import sys
import tempfile
import threading
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from homeassistant.config_entries import ConfigEntryState, OptionsFlowManager
    from homeassistant.core import HomeAssistant, ServiceRegistry, SupportsResponse
    from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
    from homeassistant.helpers.entity_platform import PlatformData

    HAS_HA = True
except ModuleNotFoundError:
    HAS_HA = False


@unittest.skipUnless(HAS_HA, "Requires Home Assistant (run inside the HA container)")
class HACompatibilityTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1] / "custom_components" / "eirc"
        name = "custom_components.eirc_compatibility_test"
        spec = importlib.util.spec_from_file_location(
            name, root / "__init__.py", submodule_search_locations=[str(root)]
        )
        cls.integration = importlib.util.module_from_spec(spec)
        sys.modules[name] = cls.integration
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            spec.loader.exec_module(cls.integration)
            cls.api = importlib.import_module(f"{name}.api")
            cls.flow = importlib.import_module(f"{name}.config_flow")
            cls.sensor = importlib.import_module(f"{name}.sensor")
            cls.button = importlib.import_module(f"{name}.button")
            cls.diagnostics = importlib.import_module(f"{name}.diagnostics")
        # Third-party/HA import warnings are not deprecations of EIRC APIs.
        cls.import_warnings = [w for w in caught if str(root) in w.filename]

    async def asyncSetUp(self):
        self.hass = MagicMock(spec=HomeAssistant)
        self.hass.loop = asyncio.get_running_loop()
        self.hass.loop_thread_id = threading.get_ident()
        self.hass.data = {}
        self.hass.is_stopping = False
        self.hass.bus = MagicMock()
        self.hass.states = MagicMock()
        self.hass.config = MagicMock()
        self.hass.config_entries = MagicMock()
        self.hass.config_entries.async_forward_entry_setups = AsyncMock()
        self.hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        self.hass.config_entries.async_reload = AsyncMock()
        self.hass.services = ServiceRegistry(self.hass)
        self.hass.async_run_hass_job.side_effect = lambda job, *args, **kwargs: (
            asyncio.create_task(job.target(*args))
        )
        self.entries = {}
        self.hass.config_entries.async_get_known_entry.side_effect = (
            self.entries.__getitem__
        )
        self.hass.config_entries.async_get_entry.side_effect = self.entries.get

        def update(entry, **kwargs):
            for key, value in kwargs.items():
                setattr(entry, key, value)
            return True

        self.hass.config_entries.async_update_entry.side_effect = update

        async def run_job(func, *args):
            return await asyncio.to_thread(func, *args)

        self.hass.async_add_executor_job = AsyncMock(side_effect=run_job)
        self.account = {
            "id": 101,
            "tenancy": {"register": "test-tenancy-a"},
            "confirmed": True,
            "alias": "Synthetic account",
        }
        self.meters = [
            {
                "id": {"registration": "test-meter"},
                "name": "Synthetic meter",
                "indications": [
                    {"meterScaleId": 1, "previousReading": 100, "unit": "m3"}
                ],
            }
        ]
        for method, value in [
            ("get_accounts", [self.account]),
            ("get_meters_info", self.meters),
            ("get_account_balance", {"balance": 0, "bill_id": "test-bill"}),
        ]:
            patcher = patch.object(
                self.api.EIRCApiClient, method, new=AsyncMock(return_value=value)
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        self.coordinators = []
        self.python_warnings = warnings.catch_warnings(record=True)
        self.caught_warnings = self.python_warnings.__enter__()
        warnings.simplefilter("always")
        self.ha_records = []
        records = self.ha_records

        class Capture(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.WARNING and record.name.startswith(
                    "homeassistant"
                ):
                    records.append(record)

        self.log_handler = Capture()
        logging.getLogger().addHandler(self.log_handler)

    async def asyncTearDown(self):
        for coordinator in self.coordinators:
            await coordinator.async_shutdown()
        self.python_warnings.__exit__(None, None, None)
        logging.getLogger().removeHandler(self.log_handler)
        self.assertEqual(
            [(w.category.__name__, str(w.message)) for w in self.caught_warnings], []
        )
        self.assertEqual([r.getMessage() for r in self.ha_records], [])

    def entry(self, name="test-entry-a", *, options=None):
        entry = SimpleNamespace(
            entry_id=name,
            title="Synthetic EIRC entry",
            state=ConfigEntryState.SETUP_IN_PROGRESS,
            data={
                "username": "test-user",
                "password": "test-password",
                "selected_accounts": ["test-tenancy-a"],
                "session_cookie": "test-cookie",
                "token_auth": "test-auth",
                "token_verify": "test-verification",
                "use_proxy": False,
            },
            options=options or {},
            async_on_unload=MagicMock(),
            add_update_listener=MagicMock(return_value=lambda: None),
            async_start_reauth_if_available=MagicMock(),
            pref_disable_polling=False,
        )
        self.entries[name] = entry
        return entry

    async def setup(self, entry):
        self.assertTrue(await self.integration.async_setup_entry(self.hass, entry))
        coordinator = self.hass.data["eirc"][entry.entry_id]
        self.coordinators.append(coordinator)
        return coordinator

    async def test_no_import_deprecations(self):
        self.assertEqual(self.import_warnings, [])

    async def test_marketplace_accepts_repository_manifest(self):
        import json

        from homeassistant.components.marketplace.repositories.base import (
            RepositoryManifest,
        )

        root = Path(__file__).resolve().parents[1]
        data = json.loads((root / "hacs.json").read_text(encoding="utf-8"))
        manifest = RepositoryManifest.from_dict(data)
        self.assertEqual(manifest.name, "EIRC Integration")
        self.assertFalse(manifest.content_in_root)
        self.assertFalse(manifest.zip_release)

    async def test_first_refresh_and_sensor_creation(self):
        entry = self.entry()
        coordinator = await self.setup(entry)
        added = []
        await self.sensor.async_setup_entry(self.hass, entry, added.extend)
        self.assertTrue(coordinator.last_update_success)
        self.assertEqual(len(added), 5)
        self.assertEqual(
            [e.unique_id for e in added[:2]],
            ["eirc_test-tenancy-a", "eirc_meter_test-tenancy-a_test-meter_1"],
        )
        self.assertTrue(all(e.available for e in added))

    async def test_user_icon_overrides_default_without_changing_entity(self):
        entry = self.entry()
        await self.setup(entry)
        added = []
        await self.sensor.async_setup_entry(self.hass, entry, added.extend)
        added = [
            entity
            for entity in added
            if isinstance(entity, (self.sensor.EIRCSensor, self.sensor.EIRCMeterSensor))
        ]
        for entity, default_icon in zip(
            added, ["mdi:cash", "mdi:counter"], strict=True
        ):
            with self.subTest(entity=entity.unique_id):
                entity.hass = self.hass
                entity.entity_id = "sensor.test_custom_entity"
                entity.platform_data = PlatformData(
                    self.hass,
                    platform_name="eirc",
                    domain="sensor",
                )
                unique_id = entity.unique_id
                value = entity.native_value
                self.assertEqual(entity.icon, default_icon)
                entity.registry_entry = SimpleNamespace(
                    icon="mdi:star", device_class=None
                )
                with patch.object(
                    self.integration.er,
                    "async_get_legacy_friendly_name",
                    return_value="Custom test name",
                ):
                    state = entity._async_calculate_state()
                self.assertEqual(state.attributes["icon"], "mdi:star")
                self.assertEqual(entity.entity_id, "sensor.test_custom_entity")
                self.assertEqual(entity.unique_id, unique_id)
                self.assertEqual(entity.native_value, value)
                self.assertIsNone(entity.device_class)

    async def test_options_saved_by_real_flow_manager_then_reload_listener(self):
        entry = self.entry()
        await self.setup(entry)
        flow = self.flow.EIRCOptionsFlowHandler(entry)
        flow.hass = self.hass
        flow.handler = entry.entry_id
        self.assertEqual((await flow.async_step_init())["type"], "menu")
        self.assertEqual(
            (await flow.async_step_account_selection())["step_id"], "account_selection"
        )
        result = await flow.async_step_account_selection(
            {
                "selected_accounts": [],
                "use_proxy": False,
            }
        )
        self.hass.config_entries.async_reload.assert_not_awaited()
        await OptionsFlowManager(self.hass).async_finish_flow(flow, result)
        self.assertEqual(entry.options["selected_accounts"], [])
        callback = entry.add_update_listener.call_args.args[0]
        await callback(self.hass, entry)
        self.hass.config_entries.async_schedule_reload.assert_called_once_with(
            entry.entry_id
        )

    async def test_runtime_uses_options_instead_of_initial_settings(self):
        entry = self.entry(
            options={
                "selected_accounts": [],
                "use_proxy": True,
                "proxy_url": "http://127.0.0.1:9",
            }
        )
        coordinator = await self.setup(entry)
        self.assertEqual(coordinator.data, {})
        self.assertEqual(coordinator.client._proxy, "http://127.0.0.1:9")
        added = []
        await self.sensor.async_setup_entry(self.hass, entry, added.extend)
        self.assertEqual(added, [])

    async def test_service_lifetime_with_two_entries(self):
        first, second = self.entry(), self.entry("test-entry-b")
        await self.setup(first)
        await self.setup(second)
        await self.integration.async_unload_entry(self.hass, first)
        for service in ("download_bill", "send_meter_reading"):
            self.assertTrue(self.hass.services.has_service("eirc", service))
        await self.integration.async_unload_entry(self.hass, second)
        for service in ("download_bill", "send_meter_reading"):
            self.assertFalse(self.hass.services.has_service("eirc", service))

    async def test_download_service_response_and_executor_file_write(self):
        coordinator = await self.setup(self.entry())
        coordinator.client.get_bill_uuid = AsyncMock(return_value="test-file")
        pdf = b"%PDF-1.4\n%%EOF"
        coordinator.client.download_bill_pdf = AsyncMock(return_value=pdf)
        self.assertIs(
            self.hass.services.supports_response("eirc", "download_bill"),
            SupportsResponse.OPTIONAL,
        )
        with tempfile.TemporaryDirectory(prefix="eirc-pdf-test-") as directory:
            self.hass.config.path.return_value = str(Path(directory) / "www")
            result = await self.hass.services.async_call(
                "eirc",
                "download_bill",
                {"account_id": "101", "bill_id": "test-bill"},
                blocking=True,
                return_response=True,
            )
            self.assertEqual(Path(result["filepath"]).read_bytes(), pdf)
            self.assertTrue(
                result["url"].startswith("/local/eirc_bills/bill_101_test-bill_")
            )
        self.hass.async_add_executor_job.assert_awaited_once()

    async def test_reading_service_payload_is_unchanged(self):
        entry = self.entry()
        coordinator = await self.setup(entry)
        coordinator.client.send_meter_reading = AsyncMock()
        registry = MagicMock()
        registry.async_get.return_value = SimpleNamespace(
            config_entry_id=entry.entry_id
        )
        self.hass.states.get.return_value = SimpleNamespace(
            attributes={
                "account_id": 101,
                "meter_id": "test-meter",
            }
        )
        with patch.object(self.integration.er, "async_get", return_value=registry):
            await self.hass.services.async_call(
                "eirc",
                "send_meter_reading",
                {
                    "entity_id": "sensor.test_meter",
                    "readings": [{"scale_id": 1, "value": 101}],
                },
                blocking=True,
            )
        coordinator.client.send_meter_reading.assert_awaited_once_with(
            101, "test-meter", [{"scaleId": 1, "value": 101.0}]
        )

    async def test_config_and_reauth_forms(self):
        entry = self.entry()
        flow = self.flow.EIRCConfigFlow()
        flow.hass = self.hass
        flow.context = {"source": "user"}
        self.assertEqual((await flow.async_step_user())["step_id"], "user")
        with patch.object(self.api.EIRCApiClient, "authenticate", new=AsyncMock()):
            result = await flow.async_step_user(
                {
                    "username": "test-user",
                    "password": "test-password",
                    "use_proxy": False,
                }
            )
        self.assertEqual(result["step_id"], "account")
        flow.context = {"source": "reauth", "entry_id": entry.entry_id}
        self.assertEqual((await flow.async_step_reauth())["step_id"], "reauth_confirm")

    async def test_bad_credentials_report_invalid_auth(self):
        flow = self.flow.EIRCConfigFlow()
        self.assertEqual(
            flow._handle_auth_error(
                self.api.EircAuthenticationError("test error"), "test-user"
            ),
            {"base": "invalid_auth"},
        )

    async def test_400_first_refresh_and_later_meter_discovery(self):
        entry = self.entry()
        with patch.object(
            self.api.EIRCApiClient,
            "get_meters_info",
            new=AsyncMock(side_effect=self.api.EircApiResponseError(400, "275")),
        ):
            coordinator = await self.setup(entry)
        added = []
        await self.sensor.async_setup_entry(self.hass, entry, added.extend)
        self.assertEqual(len(added), 4)
        self.assertTrue(added[0].available)
        await coordinator.async_refresh()
        self.assertEqual(len(added), 5)
        self.assertEqual(added[-1].unique_id, "eirc_meter_test-tenancy-a_test-meter_1")
        self.assertTrue(added[-1].available)
        await coordinator.async_refresh()
        self.assertEqual(len(added), 5)

    async def test_service_selector_does_not_require_nonexistent_device_class(self):
        import yaml
        from homeassistant.components.sensor import SensorDeviceClass

        path = (
            Path(__file__).resolve().parents[1] / "custom_components/eirc/services.yaml"
        )
        services = yaml.safe_load(path.read_text())
        entity_filter = services["send_meter_reading"]["fields"]["entity_id"][
            "selector"
        ]["entity"]["filter"]
        if "device_class" in entity_filter:
            self.assertIn(
                entity_filter["device_class"], {c.value for c in SensorDeviceClass}
            )
        self.assertEqual(entity_filter["integration"], "eirc")

    async def test_refresh_button_uses_real_coordinator_and_shared_rate_limit(self):
        entry = self.entry()
        coordinator = await self.setup(entry)
        added = []
        await self.button.async_setup_entry(self.hass, entry, added.extend)
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].unique_id, "eirc_test-tenancy-a_refresh")
        self.assertTrue(added[0].available)
        await added[0].async_press()
        self.assertTrue(coordinator.last_update_success)
        with self.assertRaises(ServiceValidationError) as caught:
            await added[0].async_press()
        self.assertEqual(caught.exception.translation_key, "refresh_rate_limited")

    async def test_diagnostics_stay_available_during_real_refresh_failure(self):
        entry = self.entry()
        coordinator = await self.setup(entry)
        status = self.sensor.EIRCUpdateStatusSensor(coordinator, self.account)
        timestamp = self.sensor.EIRCLastSuccessSensor(
            coordinator, self.account, "meters"
        )
        last_success = timestamp.native_value
        coordinator.client.get_accounts = AsyncMock(
            side_effect=self.api.EircApiClientError("Synthetic outage")
        )
        await coordinator.async_refresh()
        self.assertFalse(coordinator.last_update_success)
        self.assertEqual(status.native_value, "connection_error")
        self.assertTrue(status.available)
        self.assertTrue(timestamp.available)
        self.assertEqual(timestamp.native_value, last_success)
        with self.assertRaises(HomeAssistantError) as caught:
            await coordinator.async_request_manual_refresh()
        self.assertEqual(caught.exception.translation_key, "connection_error")

    async def test_token_persistence_does_not_schedule_reload(self):
        entry = self.entry()
        coordinator = await self.setup(entry)
        coordinator.client._token_auth = "test-renewed-auth"
        await coordinator._async_save_tokens()
        callback = entry.add_update_listener.call_args.args[0]
        await callback(self.hass, entry)
        self.hass.config_entries.async_schedule_reload.assert_not_called()
        self.hass.config_entries.async_reload.assert_not_awaited()

    async def test_reauth_and_listener_schedule_only_one_reload(self):
        entry = self.entry()
        await self.setup(entry)
        flow = self.flow.EIRCConfigFlow()
        flow.hass = self.hass
        flow.context = {"source": "reauth", "entry_id": entry.entry_id}
        flow._username = "test-user"
        flow._password = "test-new-password"
        flow._client = SimpleNamespace(
            _session_cookie="test-new-cookie",
            _token_auth="test-new-auth",
            _token_verify="test-new-verification",
        )
        result = await flow._update_reauth_entry()
        self.assertEqual(result["reason"], "reauth_successful")
        callback = entry.add_update_listener.call_args.args[0]
        await callback(self.hass, entry)
        self.hass.config_entries.async_schedule_reload.assert_called_once_with(
            entry.entry_id
        )
        self.assertEqual(entry.entry_id, "test-entry-a")
        self.assertEqual(entry.data["selected_accounts"], ["test-tenancy-a"])

    async def test_verification_errors_are_not_all_reported_as_wrong_code(self):
        for status, key in [
            (400, "invalid_code"),
            (406, "invalid_code"),
            (422, "invalid_code"),
            (401, "session_expired"),
            (403, "session_expired"),
            (429, "rate_limited"),
            (503, "cannot_connect"),
        ]:
            with self.subTest(status=status):
                flow = self.flow.EIRCConfigFlow()
                flow.hass = self.hass
                flow.context = {"source": "user"}
                flow._transaction_id = "test-transaction"
                flow._client = SimpleNamespace(
                    twofa_authentication=AsyncMock(
                        side_effect=self.api.EircApiResponseError(status)
                    )
                )
                result = await flow.async_step_2fa({"code": "test-code"})
                self.assertEqual(result["errors"], {"base": key})

    async def test_accounts_outage_after_verification_is_not_invalid_code(self):
        flow = self.flow.EIRCConfigFlow()
        flow.hass = self.hass
        flow.context = {"source": "user"}
        flow._transaction_id = "test-transaction"
        flow._client = SimpleNamespace(
            twofa_authentication=AsyncMock(),
            get_accounts=AsyncMock(side_effect=self.api.EircApiResponseError(503)),
        )
        result = await flow.async_step_2fa({"code": "test-code"})
        self.assertEqual(result["errors"], {"base": "cannot_connect"})

    async def test_login_timeout_and_rate_limit_are_actionable(self):
        flow = self.flow.EIRCConfigFlow()
        self.assertEqual(
            flow._handle_auth_error(TimeoutError(), "test-user"),
            {"base": "cannot_connect"},
        )
        self.assertEqual(
            flow._handle_auth_error(self.api.EircApiResponseError(429), "test-user"),
            {"base": "rate_limited"},
        )

    async def test_unsupported_verification_and_email_rate_limit_abort_clearly(self):
        flow = self.flow.EIRCConfigFlow()
        flow.hass = self.hass
        flow.context = {"source": "user"}
        result = await flow._handle_2fa_required(
            self.api.TwoFactorAuthRequired("test-transaction", ["TOTP"])
        )
        self.assertEqual(result["reason"], "2fa_method_not_supported")
        flow._client = SimpleNamespace(
            twofa_send_email=AsyncMock(side_effect=self.api.EircApiResponseError(429))
        )
        result = await flow._handle_2fa_required(
            self.api.TwoFactorAuthRequired("test-transaction", ["EMAIL"])
        )
        self.assertEqual(result["reason"], "rate_limited")

    async def test_options_request_reauth_instead_of_generic_accounts_error(self):
        entry = self.entry()
        flow = self.flow.EIRCOptionsFlowHandler(entry)
        flow.hass = self.hass
        flow.handler = entry.entry_id
        flow._fetch_accounts = AsyncMock(
            side_effect=self.api.TwoFactorAuthRequired("test-transaction", ["EMAIL"])
        )
        result = await flow.async_step_account_selection()
        self.assertEqual(result["reason"], "reauth_required")
        entry.async_start_reauth_if_available.assert_called_once_with(self.hass)

    async def test_new_sensor_names_and_timestamp_states_with_real_ha(self):
        import json

        coordinator = await self.setup(self.entry())
        root = (
            Path(__file__).resolve().parents[1] / "custom_components/eirc/translations"
        )
        for language in ("ru", "en"):
            data = json.loads((root / f"{language}.json").read_text(encoding="utf-8"))
            for entity in (
                self.sensor.EIRCUpdateStatusSensor(coordinator, self.account),
                self.sensor.EIRCLastSuccessSensor(coordinator, self.account, "meters"),
                self.sensor.EIRCLastSuccessSensor(coordinator, self.account, "balance"),
            ):
                entity.hass = self.hass
                entity.platform_data = PlatformData(
                    self.hass, domain="sensor", platform_name="eirc"
                )
                expected = data["entity"]["sensor"][entity.translation_key]["name"]
                entity.platform_data.platform_translations = {
                    f"component.eirc.entity.sensor.{entity.translation_key}.name": expected,
                }
                self.assertEqual(entity.name, expected)
                state = entity._async_calculate_state()
                if entity.translation_key.endswith("last_success"):
                    self.assertIn("T", state.state)
                else:
                    self.assertEqual(state.state, "ok")

    async def test_repeated_failure_publishes_changed_authentication_status(self):
        entry = self.entry()
        coordinator = await self.setup(entry)
        status = self.sensor.EIRCUpdateStatusSensor(coordinator, self.account)
        seen = []
        unsubscribe = coordinator.async_add_listener(
            lambda: seen.append(status.native_value)
        )
        try:
            coordinator.client.get_accounts = AsyncMock(
                side_effect=self.api.EircApiClientError("Synthetic outage")
            )
            await coordinator.async_refresh()
            self.assertEqual(seen[-1], "connection_error")
            coordinator.client.get_accounts.side_effect = (
                self.api.TwoFactorAuthRequired("test-transaction", ["EMAIL"])
            )
            await coordinator.async_refresh()
            self.assertEqual(seen[-1], "two_factor_required")
            entry.async_start_reauth_if_available.assert_called_once_with(self.hass)
        finally:
            unsubscribe()

    async def _reading_form(self):
        entry = self.entry()
        coordinator = await self.setup(entry)
        coordinator.client.send_meter_reading = AsyncMock()
        flow = self.flow.EIRCOptionsFlowHandler(entry)
        flow.hass = self.hass
        flow.handler = entry.entry_id
        self.hass.config.language = "ru"
        device_registry = MagicMock()
        device_registry.async_get_device.return_value = None
        readings_flow = importlib.import_module(
            f"{self.flow.__package__}.readings_flow"
        )
        patcher = patch.object(
            readings_flow.dr, "async_get", return_value=device_registry
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        registry = MagicMock()
        registry.async_get.return_value = SimpleNamespace(
            config_entry_id=entry.entry_id
        )
        for target, name, value in [
            (self.integration.er, "async_get", registry),
            (
                self.integration.er,
                "async_entries_for_config_entry",
                [SimpleNamespace(entity_id="sensor.test_meter")],
            ),
        ]:
            patcher = patch.object(target, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.hass.states.get.return_value = SimpleNamespace(
            state="100",
            attributes={"account_id": 101, "meter_id": "test-meter"},
        )
        return flow, coordinator, entry

    def _reading_input(self, flow, values):
        result = {
            target["name"]: {
                field["input_label"]: values[key]
                for key, field in target["fields"].items()
                if key in values
            }
            for target in flow._reading_batch
        }
        if set(values) - set(flow._reading_fields):
            result["unexpected"] = None
        return result

    async def _reading_values(self, flow, user_input):
        return await flow.async_step_reading_values(
            self._reading_input(flow, user_input["readings"])
        )

    async def test_reading_form_requires_explicit_confirmation_and_preserves_options(
        self,
    ):
        flow, coordinator, entry = await self._reading_form()
        before_data = dict(entry.data)
        before_options = dict(entry.options)
        selected = await flow.async_step_send_readings({"account": "101"})
        self.assertEqual(selected["step_id"], "reading_values")
        schema = selected["data_schema"](self._reading_input(flow, {"tariff_1": 101}))
        self.assertEqual(schema[flow._reading_batch[0]["name"]]["Показания"], 101)
        confirmation = await self._reading_values(flow, {"readings": {"tariff_1": 101}})
        self.assertEqual(confirmation["step_id"], "reading_confirm")
        self.assertIn("100 → 101", confirmation["description_placeholders"]["readings"])
        coordinator.client.send_meter_reading.assert_not_awaited()
        unchecked = await flow.async_step_reading_confirm({"confirm": False})
        self.assertEqual(unchecked["errors"], {"base": "confirmation_required"})
        coordinator.client.send_meter_reading.assert_not_awaited()
        result = await flow.async_step_reading_confirm({"confirm": True})
        self.assertEqual(result["reason"], "readings_sent")
        coordinator.client.send_meter_reading.assert_awaited_once_with(
            101, "test-meter", [{"scaleId": 1, "value": 101.0}]
        )
        self.assertEqual(entry.data, before_data)
        self.assertEqual(entry.options, before_options)
        self.hass.config_entries.async_schedule_reload.assert_not_called()

    async def test_reading_form_rejects_invalid_values_without_sending(self):
        flow, coordinator, _ = await self._reading_form()
        await flow.async_step_send_readings({"account": "101"})
        for values in (
            {},
            {"tariff_1": 99},
            {"tariff_1": True},
            {"tariff_1": "bad"},
            {"tariff_1": float("nan")},
            {"tariff_1": float("inf")},
            {"tariff_1": 101, "extra": 1},
        ):
            with self.subTest(values=values):
                result = await self._reading_values(flow, {"readings": values})
                self.assertEqual(result["errors"], {"base": "invalid_readings"})
        coordinator.client.send_meter_reading.assert_not_awaited()

    async def test_reading_form_shows_and_sends_all_tariffs(self):
        self.meters[0]["indications"] = [
            {
                "meterScaleId": 1,
                "previousReading": 100,
                "scaleName": "День",
                "unit": "kWh",
            },
            {
                "meterScaleId": 2,
                "previousReading": 50,
                "scaleName": "Ночь",
                "unit": "kWh",
            },
        ]
        flow, coordinator, _ = await self._reading_form()
        result = await flow.async_step_send_readings({"account": "101"})
        meter_section = next(iter(result["data_schema"].schema.values()))
        import json

        import probatio
        from homeassistant.helpers import config_validation as cv

        serialized = probatio.to_field_list(
            result["data_schema"], custom_serializer=cv.custom_serializer
        )
        json.dumps(serialized)
        self.assertEqual(serialized[0]["type"], "expandable")
        self.assertTrue(serialized[0]["expanded"])
        self.assertEqual(
            [field["name"] for field in serialized[0]["schema"]], ["День", "Ночь"]
        )
        for key, previous in (("tariff_1", 100), ("tariff_2", 50)):
            label = flow._reading_fields[key]["input_label"]
            field = next(
                marker
                for marker in meter_section.schema.schema
                if marker.schema == label
            )
            self.assertNotIn("было", label)
            self.assertEqual(field.default(), previous)
            self.assertEqual(meter_section.schema.schema[field].config["min"], previous)
        await self._reading_values(
            flow, {"readings": {"tariff_1": 101, "tariff_2": 51}}
        )
        result = await flow.async_step_reading_confirm({"confirm": True})
        self.assertEqual(result["reason"], "readings_sent")
        coordinator.client.send_meter_reading.assert_awaited_once_with(
            101,
            "test-meter",
            [{"scaleId": 1, "value": 101.0}, {"scaleId": 2, "value": 51.0}],
        )

    async def test_reading_form_never_replays_an_uncertain_submission(self):
        flow, coordinator, _ = await self._reading_form()
        await flow.async_step_send_readings({"account": "101"})
        await self._reading_values(flow, {"readings": {"tariff_1": 101}})
        coordinator.client.send_meter_reading.side_effect = TimeoutError()
        result = await flow.async_step_reading_confirm({"confirm": True})
        self.assertEqual(result["reason"], "readings_result_unknown")
        await flow.async_step_reading_confirm({"confirm": True})
        self.assertEqual(coordinator.client.send_meter_reading.await_count, 1)

    async def test_reading_form_distinguishes_rejected_submission(self):
        flow, coordinator, _ = await self._reading_form()
        await flow.async_step_send_readings({"account": "101"})
        await self._reading_values(flow, {"readings": {"tariff_1": 101}})
        coordinator.client.send_meter_reading.side_effect = (
            self.api.EircApiResponseError(400)
        )
        result = await flow.async_step_reading_confirm({"confirm": True})
        self.assertEqual(result["reason"], "readings_rejected")
        coordinator.client.send_meter_reading.assert_awaited_once()

    async def test_reading_form_rejects_foreign_or_unavailable_entities(self):
        flow, coordinator, _ = await self._reading_form()
        result = await flow.async_step_send_readings({"account": "foreign-account"})
        self.assertEqual(result["errors"], {"base": "invalid_account"})
        self.hass.states.get.return_value.state = "unavailable"
        result = await flow.async_step_send_readings()
        self.assertEqual(result["reason"], "readings_unavailable")
        coordinator.client.send_meter_reading.assert_not_awaited()

    async def test_reading_preflight_authentication_requests_reauth_without_sending(
        self,
    ):
        flow, coordinator, entry = await self._reading_form()
        coordinator.client.get_meters_info.side_effect = self.api.TwoFactorAuthRequired(
            "test-transaction", ["EMAIL"]
        )
        result = await flow.async_step_send_readings({"account": "101"})
        self.assertEqual(result["reason"], "reauth_required")
        entry.async_start_reauth_if_available.assert_called_once_with(self.hass)
        coordinator.client.send_meter_reading.assert_not_awaited()

    async def test_reading_confirmation_rechecks_target_identity_before_writing(self):
        flow, coordinator, _ = await self._reading_form()
        await flow.async_step_send_readings({"account": "101"})
        await self._reading_values(flow, {"readings": {"tariff_1": 101}})
        self.hass.states.get.return_value.attributes["meter_id"] = "test-replaced-meter"
        result = await flow.async_step_reading_confirm({"confirm": True})
        self.assertEqual(result["reason"], "readings_unavailable")
        coordinator.client.send_meter_reading.assert_not_awaited()

    async def test_reading_form_declines_ambiguous_scale_ids(self):
        flow, coordinator, _ = await self._reading_form()
        self.meters[0]["indications"].append(dict(self.meters[0]["indications"][0]))
        result = await flow.async_step_send_readings({"account": "101"})
        self.assertEqual(result["errors"], {"base": "readings_unavailable"})
        coordinator.client.send_meter_reading.assert_not_awaited()

    async def test_diagnostics_download_function_works_with_real_ha_api(self):
        entry = self.entry()
        coordinator = await self.setup(entry)
        with patch.object(
            self.diagnostics,
            "async_get_integration",
            new=AsyncMock(return_value=SimpleNamespace(version="1.2.5")),
        ):
            report = await self.diagnostics.async_get_config_entry_diagnostics(
                self.hass, entry
            )
        self.assertEqual(report["coordinator"]["last_update_success"], True)
        self.assertEqual(report["accounts"][0]["meter_count"], 1)
        self.assertEqual(coordinator.data["test-tenancy-a"]["id"], 101)

    async def _batch_reading_form(self):
        for index in (2, 3):
            self.meters.append(
                {
                    "id": {"registration": f"test-meter-{index}"},
                    "name": f"Synthetic meter {index}",
                    "indications": [{"meterScaleId": index, "previousReading": 10}],
                }
            )
        flow, coordinator, entry = await self._reading_form()
        states = {
            f"sensor.test_meter_{index}": SimpleNamespace(
                state="100",
                attributes={
                    "account_id": 101,
                    "meter_id": meter["id"]["registration"],
                    "friendly_name": f"Synthetic meter {index}",
                },
            )
            for index, meter in enumerate(self.meters, 1)
        }
        # A second tariff entity must not produce a second POST for its meter.
        states["sensor.test_tariff_duplicate"] = states["sensor.test_meter_1"]
        self.integration.er.async_entries_for_config_entry.return_value = [
            SimpleNamespace(entity_id=entity_id) for entity_id in states
        ]
        self.hass.states.get.side_effect = states.get
        return flow, coordinator, entry, states

    async def test_batch_sends_each_physical_meter_once_after_one_confirmation(self):
        flow, coordinator, _, _ = await self._batch_reading_form()
        result = await flow.async_step_send_readings({"account": "101"})
        self.assertEqual(result["step_id"], "reading_values")
        self.assertEqual(len(flow._reading_batch), 3)
        self.assertEqual(len(flow._reading_fields), 3)
        await self._reading_values(
            flow,
            {
                "readings": {
                    "tariff_1": 101,
                    "tariff_2": 11,
                    "tariff_3": 12,
                }
            },
        )
        coordinator.client.send_meter_reading.assert_not_awaited()
        result = await flow.async_step_reading_confirm({"confirm": True})
        self.assertEqual(result["reason"], "readings_sent")
        self.assertEqual(coordinator.client.send_meter_reading.await_count, 3)
        self.assertEqual(
            [
                call.args[1]
                for call in coordinator.client.send_meter_reading.await_args_list
            ],
            ["test-meter", "test-meter-2", "test-meter-3"],
        )
        await flow.async_step_send_readings({"account": "101"})
        await self._reading_values(flow, {"readings": {"tariff_1": 200}})
        await flow.async_step_reading_confirm({"confirm": True})
        self.assertEqual(coordinator.client.send_meter_reading.await_count, 3)

    async def test_batch_stops_on_rejection_or_uncertainty_and_reports_all_results(
        self,
    ):
        flow, coordinator, entry, _ = await self._batch_reading_form()
        for error, outcome in (
            (self.api.EircApiResponseError(400), "rejected"),
            (TimeoutError(), "unknown"),
        ):
            with self.subTest(outcome=outcome):
                flow = self.flow.EIRCOptionsFlowHandler(entry)
                flow.hass = self.hass
                flow.handler = entry.entry_id
                coordinator.client.send_meter_reading.reset_mock()
                coordinator.client.send_meter_reading.side_effect = [None, error]
                await flow.async_step_send_readings({"account": "101"})
                await self._reading_values(
                    flow,
                    {
                        "readings": {
                            "tariff_1": 101,
                            "tariff_2": 11,
                            "tariff_3": 12,
                        }
                    },
                )
                result = await flow.async_step_reading_confirm({"confirm": True})
                self.assertEqual(result["reason"], "readings_partial")
                summary = result["description_placeholders"]
                self.assertIn("Synthetic meter 1", summary["sent"])
                self.assertIn("Synthetic meter 2", summary[outcome])
                self.assertIn("Synthetic meter 3", summary["unattempted"])
                self.assertEqual(
                    flow._reading_outcomes, ["sent", outcome, "unattempted"]
                )
                await flow.async_step_reading_confirm({"confirm": True})
                self.assertEqual(coordinator.client.send_meter_reading.await_count, 2)

    async def test_batch_validates_all_targets_before_first_post(self):
        flow, coordinator, _, states = await self._batch_reading_form()
        await flow.async_step_send_readings({"account": "101"})
        await self._reading_values(
            flow,
            {
                "readings": {
                    "tariff_1": 101,
                    "tariff_2": 11,
                    "tariff_3": 12,
                }
            },
        )
        states["sensor.test_meter_3"].attributes["meter_id"] = "test-replacement"
        result = await flow.async_step_reading_confirm({"confirm": True})
        self.assertEqual(result["reason"], "readings_unavailable")
        coordinator.client.send_meter_reading.assert_not_awaited()

    async def test_batch_missing_fresh_meter_prevents_submission(self):
        flow, coordinator, _, _ = await self._batch_reading_form()
        coordinator.client.get_meters_info = AsyncMock(return_value=self.meters[:2])
        result = await flow.async_step_send_readings({"account": "101"})
        self.assertEqual(result["errors"], {"base": "readings_unavailable"})
        coordinator.client.send_meter_reading.assert_not_awaited()

    async def test_batch_includes_all_tariffs_without_duplicate_physical_writes(self):
        self.meters[0]["indications"].append(
            {
                "meterScaleId": 4,
                "previousReading": 50,
                "scaleName": "Night",
            }
        )
        flow, coordinator, _, _ = await self._batch_reading_form()
        await flow.async_step_send_readings({"account": "101"})
        self.assertEqual(len(flow._reading_fields), 4)
        await self._reading_values(
            flow,
            {
                "readings": {
                    "tariff_1": 101,
                    "tariff_2": 51,
                    "tariff_3": 11,
                    "tariff_4": 12,
                }
            },
        )
        await flow.async_step_reading_confirm({"confirm": True})
        self.assertEqual(coordinator.client.send_meter_reading.await_count, 3)
        self.assertEqual(
            coordinator.client.send_meter_reading.await_args_list[0].args,
            (
                101,
                "test-meter",
                [{"scaleId": 1, "value": 101.0}, {"scaleId": 4, "value": 51.0}],
            ),
        )

    async def test_batch_cancellation_preserves_unknown_result_and_prevents_replay(
        self,
    ):
        flow, coordinator, _, _ = await self._batch_reading_form()
        await flow.async_step_send_readings({"account": "101"})
        await self._reading_values(
            flow,
            {
                "readings": {
                    "tariff_1": 101,
                    "tariff_2": 11,
                    "tariff_3": 12,
                }
            },
        )
        coordinator.client.send_meter_reading.side_effect = [
            None,
            asyncio.CancelledError(),
        ]
        with self.assertRaises(asyncio.CancelledError):
            await flow.async_step_reading_confirm({"confirm": True})
        result = await flow.async_step_reading_confirm({"confirm": True})
        self.assertEqual(result["reason"], "readings_partial")
        self.assertEqual(flow._reading_outcomes, ["sent", "unknown", "unattempted"])
        self.assertEqual(coordinator.client.send_meter_reading.await_count, 2)

    async def test_reading_account_labels_follow_ha_device_names_without_registry_writes(
        self,
    ):
        flow, coordinator, _ = await self._reading_form()
        readings_flow = importlib.import_module(
            f"{self.flow.__package__}.readings_flow"
        )
        registry = readings_flow.dr.async_get.return_value
        for user_name, device_name, alias, expected in (
            ("My home", "Default home", "Portal alias", "My home — test-tenancy-a"),
            (None, "Default home", "Portal alias", "Default home — test-tenancy-a"),
            (None, None, "Portal alias", "Portal alias — test-tenancy-a"),
            (None, None, None, "Лицевой счёт test-tenancy-a"),
        ):
            with self.subTest(expected=expected):
                registry.async_get_device.return_value = SimpleNamespace(
                    name_by_user=user_name, name=device_name
                )
                coordinator.data["test-tenancy-a"]["alias"] = alias
                result = await flow.async_step_send_readings()
                account_selector = next(iter(result["data_schema"].schema.values()))
                self.assertEqual(
                    account_selector.config["options"],
                    [{"value": "101", "label": expected}],
                )
                await flow.async_step_send_readings({"account": "101"})
                self.assertEqual(flow._reading_account, expected)
        registry.async_get_device.assert_called_with(
            identifiers={("eirc", "test-tenancy-a")}
        )
        registry.async_update_device.assert_not_called()
        registry.async_get_or_create.assert_not_called()
        coordinator.client.send_meter_reading.assert_not_awaited()

    async def test_reading_long_meter_name_is_a_heading_not_an_input_label(self):
        flow, coordinator, _ = await self._reading_form()
        long_name = "Synthetic long meter name " * 10
        self.hass.states.get.return_value.attributes["friendly_name"] = long_name
        result = await flow.async_step_send_readings({"account": "101"})
        heading, meter_section = next(iter(result["data_schema"].schema.items()))
        self.assertIn(long_name, heading.schema)
        self.assertEqual(
            [key.schema for key in meter_section.schema.schema], ["Показания"]
        )
        values = meter_section.schema({})
        self.assertEqual(values, {"Показания": 100})
        coordinator.client.send_meter_reading.assert_not_awaited()

    async def test_reading_repeated_tariff_names_have_distinct_fields(self):
        self.meters[0]["indications"] = [
            {"meterScaleId": index, "previousReading": 10, "scaleName": label}
            for index, label in enumerate(("Day", "Day", "Day (2)"), 1)
        ]
        flow, coordinator, _ = await self._reading_form()
        result = await flow.async_step_send_readings({"account": "101"})
        meter_section = next(iter(result["data_schema"].schema.values()))
        self.assertEqual(len(meter_section.schema.schema), 3)
        await self._reading_values(
            flow, {"readings": {"tariff_1": 11, "tariff_2": 12, "tariff_3": 13}}
        )
        await flow.async_step_reading_confirm({"confirm": True})
        coordinator.client.send_meter_reading.assert_awaited_once_with(
            101,
            "test-meter",
            [
                {"scaleId": 1, "value": 11.0},
                {"scaleId": 2, "value": 12.0},
                {"scaleId": 3, "value": 13.0},
            ],
        )
