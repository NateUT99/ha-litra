"""Binary sensor platform for Logitech Litra."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LitraConfigEntry, LitraRuntime
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LitraConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the agent connectivity sensor."""
    async_add_entities([AgentConnectivitySensor(entry.runtime_data)])


class AgentConnectivitySensor(BinarySensorEntity):
    """Whether the agent's event stream is up.

    Distinguishes "the agent/computer is unreachable" (this is off) from "a
    light is unplugged" (this is on, that light is unavailable).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "agent_connectivity"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, runtime: LitraRuntime) -> None:
        """Initialize."""
        self._runtime = runtime
        agent_id = runtime.info["agent_id"]
        # Kept from the sensor's original name: changing a unique_id orphans the
        # existing registry entry and creates a new entity.
        self._attr_unique_id = f"{agent_id}_connected"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, agent_id)})

    @property
    def is_on(self) -> bool:
        """Return whether the event stream is connected."""
        return self._runtime.connected

    async def async_added_to_hass(self) -> None:
        """Follow connectivity changes."""
        self.async_on_remove(self._runtime.async_add_listener(self.async_write_ha_state))
