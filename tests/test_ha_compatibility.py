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

    async def test_first_refresh_and_sensor_creation(self):
        entry = self.entry()
        coordinator = await self.setup(entry)
        added = []
        await self.sensor.async_setup_entry(self.hass, entry, added.extend)
        self.assertTrue(coordinator.last_update_success)
        self.assertEqual(len(added), 2)
        self.assertEqual(
            [e.unique_id for e in added],
            ["eirc_test-tenancy-a", "eirc_meter_test-tenancy-a_test-meter_1"],
        )
        self.assertTrue(all(e.available for e in added))

    async def test_options_saved_by_real_flow_manager_then_reload_listener(self):
        entry = self.entry()
        await self.setup(entry)
        flow = self.flow.EIRCOptionsFlowHandler(entry)
        flow.hass = self.hass
        flow.handler = entry.entry_id
        self.assertEqual((await flow.async_step_init())["step_id"], "account_selection")
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
        self.hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)

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
        self.assertEqual(len(added), 1)
        self.assertTrue(added[0].available)
        await coordinator.async_refresh()
        self.assertEqual(len(added), 2)
        self.assertEqual(added[1].unique_id, "eirc_meter_test-tenancy-a_test-meter_1")
        self.assertTrue(added[1].available)
        await coordinator.async_refresh()
        self.assertEqual(len(added), 2)

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
