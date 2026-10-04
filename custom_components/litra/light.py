"""Light platform for Logitech Litra."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util.color import brightness_to_value, value_to_brightness

from . import LitraConfigEntry, LitraRuntime
from .api import LitraDevice, LitraError
from .const import DOMAIN

# Commands are serialized by the agent's single HID worker anyway.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LitraConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a light per attached Litra, including ones plugged in later."""
    runtime = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new_devices() -> None:
        new = [device_id for device_id in runtime.devices if device_id not in known]
        if new:
            known.update(new)
            async_add_entities(LitraLight(runtime, runtime.devices[device_id]) for device_id in new)

    _add_new_devices()
    entry.async_on_unload(runtime.async_add_listener(_add_new_devices))


class LitraLight(LightEntity):
    """A Litra key light."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_color_mode = ColorMode.COLOR_TEMP
    _attr_supported_color_modes = {ColorMode.COLOR_TEMP}

    def __init__(self, runtime: LitraRuntime, device: LitraDevice) -> None:
        """Initialize."""
        self._runtime = runtime
        self._device_id = device.id
        # Last state seen, kept so ranges stay valid while the device is unplugged.
        self._last = device
        self._attr_unique_id = device.id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.id)},
            name=device.model,
            manufacturer="Logitech",
            model=device.model,
            serial_number=device.serial,
            via_device_id=runtime.agent_device_id,
        )

    @property
    def _device(self) -> LitraDevice:
        if (device := self._runtime.devices.get(self._device_id)) is not None:
            self._last = device
        return self._last

    @property
    def _lumen_range(self) -> tuple[int, int]:
        return (self._device.min_brightness_lumen, self._device.max_brightness_lumen)

    @property
    def available(self) -> bool:
        """Available while the agent stream is up and the light is plugged in."""
        return self._runtime.connected and self._device_id in self._runtime.devices

    @property
    def is_on(self) -> bool:
        """Return whether the light is on."""
        return self._device.is_on

    @property
    def brightness(self) -> int:
        """Return brightness on HA's 1-255 scale."""
        return value_to_brightness(self._lumen_range, self._device.brightness_lumen)

    @property
    def color_temp_kelvin(self) -> int:
        """Return the color temperature."""
        return self._device.temperature_kelvin

    @property
    def min_color_temp_kelvin(self) -> int:
        """Return the warmest supported temperature."""
        return self._device.min_temperature_kelvin

    @property
    def max_color_temp_kelvin(self) -> int:
        """Return the coolest supported temperature."""
        return self._device.max_temperature_kelvin

    async def async_added_to_hass(self) -> None:
        """Subscribe to pushed state."""
        self.async_on_remove(self._runtime.async_add_listener(self.async_write_ha_state))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on, applying brightness and temperature in the same request.

        Always sends `on`: a Litra that is off stores brightness/temperature
        changes without lighting up.
        """
        lumen = None
        if ATTR_BRIGHTNESS in kwargs:
            lumen = math.ceil(brightness_to_value(self._lumen_range, kwargs[ATTR_BRIGHTNESS]))
        await self._async_send(
            on=True,
            brightness_lumen=lumen,
            temperature_kelvin=kwargs.get(ATTR_COLOR_TEMP_KELVIN),
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off."""
        await self._async_send(on=False)

    async def _async_send(self, **changes: Any) -> None:
        try:
            device = await self._runtime.client.set_state(self._device_id, **changes)
        except LitraError as err:
            raise HomeAssistantError(f"Failed to control {self._device.model}: {err}") from err
        # The agent also broadcasts this, but applying the read-back now keeps the
        # UI from flickering back to the old state while the push is in flight.
        self._runtime.async_set_device(device)
