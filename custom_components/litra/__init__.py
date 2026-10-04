"""The Logitech Litra integration.

Talks to litra-agent, a small service running in the logged-in user's session on
the computer the light is plugged into. State arrives over a WebSocket push
stream; commands are single HTTPS requests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN, Platform
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    LitraAgentClient,
    LitraAuthError,
    LitraConnectionError,
    LitraDevice,
    LitraFingerprintError,
)
from .const import API_VERSION, CONF_FINGERPRINT, DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.LIGHT]
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 60.0

type LitraConfigEntry = ConfigEntry[LitraRuntime]


class LitraRuntime:
    """Live connection state shared by the entry's entities."""

    def __init__(self, client: LitraAgentClient, info: dict[str, Any]) -> None:
        """Initialize."""
        self.client = client
        self.info = info
        self.devices: dict[str, LitraDevice] = {}
        self.connected = False
        self.agent_device_id: str | None = None
        self._listeners: list[CALLBACK_TYPE] = []

    @callback
    def async_add_listener(self, listener: CALLBACK_TYPE) -> Callable[[], None]:
        """Call `listener` whenever devices or connectivity change."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def async_set_devices(self, devices: list[LitraDevice]) -> None:
        """Replace the full device list (from the event stream or a fetch)."""
        self.devices = {device.id: device for device in devices}
        self.connected = True
        self._notify()

    @callback
    def async_set_device(self, device: LitraDevice) -> None:
        """Update one device from a command's read-back."""
        self.devices[device.id] = device
        self._notify()

    @callback
    def async_set_disconnected(self) -> None:
        """Mark everything unavailable until the stream reconnects."""
        if self.connected:
            self.connected = False
            self._notify()

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()


async def async_setup_entry(hass: HomeAssistant, entry: LitraConfigEntry) -> bool:
    """Set up a litra-agent connection."""
    client = LitraAgentClient(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data[CONF_TOKEN],
        entry.data[CONF_FINGERPRINT],
    )
    try:
        info = await client.get_info()
        devices = await client.get_devices()
    except (LitraAuthError, LitraFingerprintError) as err:
        # A changed certificate is handled like a bad token: the user re-pairs
        # and explicitly confirms the new fingerprint.
        raise ConfigEntryAuthFailed(str(err)) from err
    except LitraConnectionError as err:
        raise ConfigEntryNotReady(f"litra-agent unreachable: {err}") from err

    if info.get("agent_id") != entry.unique_id:
        raise ConfigEntryError("A different litra-agent is answering at this address")
    if info.get("api_version") != API_VERSION:
        raise ConfigEntryError(
            f"litra-agent API version {info.get('api_version')} is not supported "
            f"(expected {API_VERSION}); update the agent or the integration"
        )

    runtime = LitraRuntime(client, info)
    runtime.async_set_devices(devices)
    entry.runtime_data = runtime

    agent_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, info["agent_id"])},
        name=f"Litra Agent ({info['name']})",
        manufacturer="litra-agent",
        model="Litra Agent",
        sw_version=info["version"],
        entry_type=dr.DeviceEntryType.SERVICE,
    )
    runtime.agent_device_id = agent_device.id

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(
        hass, _async_stream(hass, entry, runtime), f"{DOMAIN} event stream"
    )
    return True


async def _async_stream(
    hass: HomeAssistant, entry: LitraConfigEntry, runtime: LitraRuntime
) -> None:
    """Hold the event stream open, reconnecting with backoff until unload."""
    backoff = INITIAL_BACKOFF
    logged_down = False
    while True:
        try:
            await runtime.client.listen(runtime.async_set_devices)
            reason = "stream closed"
        except (LitraAuthError, LitraFingerprintError) as err:
            _LOGGER.error("litra-agent refused the connection (%s); reauthentication required", err)
            runtime.async_set_disconnected()
            entry.async_start_reauth(hass)
            return
        except LitraConnectionError as err:
            reason = str(err)

        if runtime.connected:
            # The stream delivered data, so this is a fresh outage: reset backoff
            # and allow one new warning. A still-down agent logs only once.
            backoff = INITIAL_BACKOFF
            logged_down = False
        runtime.async_set_disconnected()
        if not logged_down:
            _LOGGER.warning("Lost connection to litra-agent (%s); retrying", reason)
            logged_down = True
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)


async def async_unload_entry(hass: HomeAssistant, entry: LitraConfigEntry) -> bool:
    """Unload; the stream task is cancelled with the entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
