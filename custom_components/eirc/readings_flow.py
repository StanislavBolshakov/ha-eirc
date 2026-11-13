"""Account-wide readings with one confirmation and no automatic write replay."""

import re
from math import isfinite

import homeassistant.helpers.device_registry as dr
import homeassistant.helpers.entity_registry as er
import voluptuous as vol
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector

from .api import (
    EircApiClientError,
    EircApiResponseError,
    EircAuthenticationError,
    TwoFactorAuthRequired,
)
from .const import DOMAIN, SERVICE_SEND_METER_READING


class ReadingsFlowMixin:
    """Send readings without changing config options or existing entities."""

    def _reading_entities(self) -> dict:
        registry = er.async_get(self.hass)
        choices = {}
        for entry in er.async_entries_for_config_entry(
            registry, self.config_entry.entry_id
        ):
            if not entry.entity_id.startswith("sensor."):
                continue
            state = self.hass.states.get(entry.entity_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                continue
            account_id = state.attributes.get("account_id")
            registration = state.attributes.get("meter_id")
            if account_id is not None and registration:
                choices[entry.entity_id] = {
                    "account_id": account_id,
                    "registration": registration,
                }
        return choices

    def _reading_account_label(self, account) -> str:
        """Use the existing HA device name without modifying the registry."""
        tenancy = str(account.get("tenancy", {}).get("register") or "")
        device = (
            dr.async_get(self.hass).async_get_device(identifiers={(DOMAIN, tenancy)})
            if tenancy
            else None
        )
        name = (device.name_by_user or device.name) if device else None
        name = name or account.get("alias")
        number = tenancy or str(account["id"])
        if name and str(name) != number:
            return f"{name} — {number}"
        prefix = "Лицевой счёт" if self.hass.config.language == "ru" else "Account"
        return f"{prefix} {number}"

    async def async_step_send_readings(self, user_input=None):
        """Choose an account and fetch all of its available physical meters."""
        if getattr(self, "_reading_submitted", False):
            return self._reading_result()
        self._reading_values = None
        self._reading_fields = {}
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator is None:
            return self.async_abort(reason="readings_unavailable")
        choices = self._reading_entities()
        accounts = {
            str(account["id"]): account
            for account in (coordinator.data or {}).values()
            if any(choice["account_id"] == account["id"] for choice in choices.values())
        }
        if not accounts:
            return self.async_abort(reason="readings_unavailable")
        errors = {}
        if user_input is not None:
            account_key = user_input.get("account")
            if not isinstance(account_key, str) or account_key not in accounts:
                errors["base"] = "invalid_account"
            else:
                account = accounts[account_key]
                try:
                    meters = await coordinator.client.get_meters_info(account["id"])
                    await coordinator._async_save_tokens()
                except (EircAuthenticationError, TwoFactorAuthRequired):
                    self.config_entry.async_start_reauth_if_available(self.hass)
                    return self.async_abort(reason="reauth_required")
                except EircApiClientError:
                    errors["base"] = "readings_unavailable"
                else:
                    targets = {}
                    for entity_id, choice in choices.items():
                        if choice["account_id"] == account["id"]:
                            targets.setdefault(choice["registration"], entity_id)
                    fresh = {m["id"]["registration"]: m for m in meters}
                    batch = []
                    fields = {}
                    for registration, entity_id in targets.items():
                        meter_fields = self._prepare_reading_fields(
                            fresh.get(registration)
                        )
                        if not meter_fields:
                            break
                        state = self.hass.states.get(entity_id)
                        name = (
                            (state.attributes.get("friendly_name") or entity_id)
                            if state
                            else entity_id
                        )
                        target = {
                            "entity_id": entity_id,
                            "choice": dict(choices[entity_id]),
                            "name": f"{name} ({registration})",
                            "fields": {},
                        }
                        for field in meter_fields.values():
                            key = f"tariff_{len(fields) + 1}"
                            label = field["label"]
                            if len(meter_fields) == 1:
                                label = (
                                    "Показания"
                                    if self.hass.config.language == "ru"
                                    else "Reading"
                                )
                            # Duplicate scale names still need distinct input keys.
                            used = {f["input_label"] for f in target["fields"].values()}
                            candidate = label
                            suffix = 2
                            while candidate in used:
                                candidate = f"{label} ({suffix})"
                                suffix += 1
                            field["input_label"] = candidate
                            fields[key] = field
                            target["fields"][key] = field
                        batch.append(target)
                    else:
                        if batch and len(fresh) == len(meters):
                            self._reading_batch = batch
                            self._reading_fields = fields
                            self._reading_account = self._reading_account_label(account)
                            return await self.async_step_reading_values()
                    errors["base"] = "readings_unavailable"
        return self.async_show_form(
            step_id="send_readings",
            data_schema=vol.Schema(
                {
                    vol.Required("account"): selector.SelectSelector(
                        {
                            "options": [
                                {
                                    "value": key,
                                    "label": self._reading_account_label(account),
                                }
                                for key, account in accounts.items()
                            ],
                            "mode": "dropdown",
                        }
                    ),
                }
            ),
            errors=errors,
        )

    def _prepare_reading_fields(self, meter) -> dict:
        fields = {}
        if not meter or not meter.get("indications"):
            return fields
        scale_ids = set()
        for index, indication in enumerate(meter["indications"], 1):
            previous = indication.get("previousReading")
            scale_id = indication.get("meterScaleId")
            if type(previous) not in (int, float):
                return {}
            try:
                previous = float(previous)
            except OverflowError:
                return {}
            if (
                not isfinite(previous)
                or previous < 0
                or type(scale_id) is not int
                or scale_id < 0
                or scale_id in scale_ids
            ):
                return {}
            scale_ids.add(scale_id)
            label = indication.get("scaleName")
            unit = indication.get("unit")
            fields[f"tariff_{index}"] = {
                "scale_id": scale_id,
                "previous": previous,
                "label": label if isinstance(label, str) and label else str(index),
                "unit": unit if isinstance(unit, str) else "",
            }
        return fields

    async def async_step_reading_values(self, user_input=None):
        if getattr(self, "_reading_submitted", False):
            return self._reading_result()
        if not getattr(self, "_reading_fields", None):
            return await self.async_step_send_readings()
        errors = {}
        if user_input is not None:
            try:
                if set(user_input) != {
                    target["name"] for target in self._reading_batch
                }:
                    raise ValueError
                values = {}
                for target in self._reading_batch:
                    supplied = user_input[target["name"]]
                    if not isinstance(supplied, dict) or set(supplied) != {
                        field["input_label"] for field in target["fields"].values()
                    }:
                        raise ValueError
                    values.update(
                        {
                            key: supplied[field["input_label"]]
                            for key, field in target["fields"].items()
                        }
                    )
                converted = {}
                for key, field in self._reading_fields.items():
                    if isinstance(values[key], bool):
                        raise TypeError
                    value = float(values[key])
                    if not isfinite(value) or value < field["previous"]:
                        raise ValueError
                    converted[key] = value
            except (KeyError, TypeError, ValueError, OverflowError):
                errors["base"] = "invalid_readings"
            else:
                self._reading_values = converted
                return await self.async_step_reading_confirm()
        sections = {}
        for target in self._reading_batch:
            fields = {}
            for field in target["fields"].values():
                number = {"min": field["previous"], "step": "any", "mode": "box"}
                if field["unit"]:
                    number["unit_of_measurement"] = field["unit"]
                fields[
                    vol.Required(field["input_label"], default=field["previous"])
                ] = selector.NumberSelector(number)
            sections[vol.Required(target["name"])] = section(
                vol.Schema(fields), {"collapsed": False}
            )
        return self.async_show_form(
            step_id="reading_values",
            data_schema=vol.Schema(sections),
            errors=errors,
        )

    async def async_step_reading_confirm(self, user_input=None):
        if getattr(self, "_reading_submitted", False):
            return self._reading_result()
        if not getattr(self, "_reading_values", None):
            return await self.async_step_send_readings()
        errors = {}
        if user_input is not None:
            if user_input.get("confirm") is not True:
                errors["base"] = "confirmation_required"
            else:
                choices = self._reading_entities()
                if any(
                    choices.get(target["entity_id"]) != target["choice"]
                    for target in self._reading_batch
                ):
                    return self.async_abort(reason="readings_unavailable")
                self._reading_submitted = True
                self._reading_outcomes = ["unattempted"] * len(self._reading_batch)
                for index, target in enumerate(self._reading_batch):
                    # Recheck immediately before each write as well as before the batch.
                    if (
                        self._reading_entities().get(target["entity_id"])
                        != target["choice"]
                    ):
                        break
                    readings = [
                        {
                            "scale_id": field["scale_id"],
                            "value": self._reading_values[key],
                        }
                        for key, field in target["fields"].items()
                    ]
                    self._reading_outcomes[index] = "unknown"
                    try:
                        await self.hass.services.async_call(
                            DOMAIN,
                            SERVICE_SEND_METER_READING,
                            {"entity_id": target["entity_id"], "readings": readings},
                            blocking=True,
                        )
                    except Exception as err:  # noqa: BLE001 - Never replay an ambiguous write.
                        cause = err
                        for _ in range(8):
                            if cause is None:
                                break
                            if (
                                isinstance(cause, EircApiResponseError)
                                and 400 <= cause.status < 500
                            ):
                                self._reading_outcomes[index] = "rejected"
                                break
                            cause = cause.__cause__
                        break
                    self._reading_outcomes[index] = "sent"
                return self._reading_result()
        summary = "\n".join(
            f"- {self._reading_text(target['name'])} — {self._reading_text(field['input_label'])}: **{field['previous']:g} → {self._reading_values[key]:g} {self._reading_text(field['unit'])}**"
            for target in self._reading_batch
            for key, field in target["fields"].items()
        )
        return self.async_show_form(
            step_id="reading_confirm",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            description_placeholders={
                "readings": summary,
                "account": self._reading_text(self._reading_account),
            },
            errors=errors,
        )

    def _reading_result(self):
        outcomes = getattr(
            self, "_reading_outcomes", ["unknown"] * len(self._reading_batch)
        )
        if all(outcome == "sent" for outcome in outcomes):
            reason = "readings_sent"
        elif len(outcomes) == 1 and outcomes[0] in ("rejected", "unknown"):
            reason = (
                "readings_rejected"
                if outcomes[0] == "rejected"
                else "readings_result_unknown"
            )
        else:
            reason = "readings_partial"
        return self.async_abort(
            reason=reason,
            description_placeholders={
                outcome: ", ".join(
                    self._reading_text(target["name"])
                    for target, status in zip(
                        self._reading_batch, outcomes, strict=True
                    )
                    if status == outcome
                )
                or "—"
                for outcome in ("sent", "rejected", "unknown", "unattempted")
            },
        )

    @staticmethod
    def _reading_text(value) -> str:
        """Display labels as text, not executable Markdown supplied by an API."""
        text = " ".join(str(value).split())
        return re.sub(r"([\\`*_{}\[\]()#+.!|~<>-])", r"\\\1", text)
