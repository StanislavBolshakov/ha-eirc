"""Manual refresh controls, without changing existing sensor identities."""

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_SELECTED_ACCOUNTS, DOMAIN
from .coordinator import EircDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EircDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    selected = entry.options.get(
        CONF_SELECTED_ACCOUNTS, entry.data.get(CONF_SELECTED_ACCOUNTS, [])
    )
    known_ids = set()

    @callback
    def add_new_buttons() -> None:
        buttons = []
        for account in (coordinator.data or {}).values():
            tenancy = account["tenancy"]["register"]
            if tenancy in selected and tenancy not in known_ids:
                known_ids.add(tenancy)
                buttons.append(EIRCRefreshButton(coordinator, tenancy))
        if buttons:
            async_add_entities(buttons)

    add_new_buttons()
    entry.async_on_unload(coordinator.async_add_listener(add_new_buttons))


class EIRCRefreshButton(CoordinatorEntity, ButtonEntity):
    """All account buttons share their entry's refresh lock and cooldown."""

    _attr_has_entity_name = True
    _attr_translation_key = "refresh"
    _attr_device_class = ButtonDeviceClass.UPDATE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: EircDataUpdateCoordinator, tenancy: str) -> None:
        super().__init__(coordinator)
        self._tenancy_id = tenancy
        self._attr_unique_id = f"eirc_{tenancy}_refresh"
        self._attr_device_info = {"identifiers": {(DOMAIN, tenancy)}}

    @property
    def available(self) -> bool:
        return self._tenancy_id in (self.coordinator.data or {})

    async def async_press(self) -> None:
        await self.coordinator.async_request_manual_refresh()
