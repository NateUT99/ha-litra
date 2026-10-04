"""Light platform and connection lifecycle tests."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.litra.api import LitraAuthError, LitraConnectionError, LitraError
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_MAX_COLOR_TEMP_KELVIN,
    ATTR_MIN_COLOR_TEMP_KELVIN,
    DOMAIN as LIGHT_DOMAIN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from .conftest import GLOW, FakeClient

ENTITY = "light.litra_glow"
AGENT_CONNECTED = "binary_sensor.litra_agent_mac_mini_connected"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_state_and_ranges(
    hass: HomeAssistant, fake_client: FakeClient, config_entry: MockConfigEntry
) -> None:
    """The entity reflects the device and its native kelvin range."""
    await _setup(hass, config_entry)
    state = hass.states.get(ENTITY)
    assert state.state == STATE_OFF
    assert state.attributes[ATTR_MIN_COLOR_TEMP_KELVIN] == 2700
    assert state.attributes[ATTR_MAX_COLOR_TEMP_KELVIN] == 6500

    device = dr.async_get(hass).async_get_device_by_identifier(
        ("litra", GLOW.id), config_entry.entry_id
    )
    assert device.manufacturer == "Logitech"
    assert device.serial_number == GLOW.serial
    assert device.via_device_id is not None


async def test_turn_on_sends_everything_in_one_request(
    hass: HomeAssistant, fake_client: FakeClient, config_entry: MockConfigEntry
) -> None:
    """Brightness and temperature land together, always with on=True."""
    await _setup(hass, config_entry)
    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: ENTITY, ATTR_BRIGHTNESS: 255, ATTR_COLOR_TEMP_KELVIN: 4500},
        blocking=True,
    )
    assert fake_client.calls == [
        (GLOW.id, {"on": True, "brightness_lumen": 250, "temperature_kelvin": 4500})
    ]
    state = hass.states.get(ENTITY)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 255
    assert state.attributes[ATTR_COLOR_TEMP_KELVIN] == 4500


@pytest.mark.parametrize("brightness", [1, 64, 115, 128, 200, 255])
async def test_brightness_round_trip(
    hass: HomeAssistant, fake_client: FakeClient, config_entry: MockConfigEntry, brightness: int
) -> None:
    """Lumen conversion stays in range and reads back within one step."""
    await _setup(hass, config_entry)
    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: ENTITY, ATTR_BRIGHTNESS: brightness},
        blocking=True,
    )
    lumen = fake_client.calls[-1][1]["brightness_lumen"]
    assert 20 <= lumen <= 250
    assert abs(hass.states.get(ENTITY).attributes[ATTR_BRIGHTNESS] - brightness) <= 1


async def test_turn_on_bare_and_off(
    hass: HomeAssistant, fake_client: FakeClient, config_entry: MockConfigEntry
) -> None:
    """A bare turn_on sends only on; turn_off sends only off."""
    await _setup(hass, config_entry)
    await hass.services.async_call(
        LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: ENTITY}, blocking=True
    )
    await hass.services.async_call(
        LIGHT_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: ENTITY}, blocking=True
    )
    assert fake_client.calls == [
        (GLOW.id, {"on": True, "brightness_lumen": None, "temperature_kelvin": None}),
        (GLOW.id, {"on": False}),
    ]
    assert hass.states.get(ENTITY).state == STATE_OFF


async def test_command_failure_raises(
    hass: HomeAssistant, fake_client: FakeClient, config_entry: MockConfigEntry
) -> None:
    """Agent errors surface as HomeAssistantError."""
    await _setup(hass, config_entry)
    fake_client.error = LitraError("HTTP 502")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            LIGHT_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: ENTITY}, blocking=True
        )


async def test_push_updates_state(
    hass: HomeAssistant, fake_client: FakeClient, config_entry: MockConfigEntry
) -> None:
    """A physical button press pushed by the agent updates HA."""
    await _setup(hass, config_entry)
    fake_client.push([replace(GLOW, is_on=True, temperature_kelvin=3000)])
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_COLOR_TEMP_KELVIN] == 3000


async def test_unplug_and_hotplug(
    hass: HomeAssistant, fake_client: FakeClient, config_entry: MockConfigEntry
) -> None:
    """Unplugged → unavailable; a new light appears without a reload."""
    await _setup(hass, config_entry)
    fake_client.push([])
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == STATE_UNAVAILABLE
    # The agent is still up; only the light is gone.
    assert hass.states.get(AGENT_CONNECTED).state == STATE_ON

    beam = replace(GLOW, id="BEAM1", serial="BEAM1", model="Litra Beam")
    fake_client.push([GLOW, beam])
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == STATE_OFF
    assert hass.states.get("light.litra_beam") is not None


async def test_stream_drop_marks_unavailable(
    hass: HomeAssistant, fake_client: FakeClient, config_entry: MockConfigEntry
) -> None:
    """Losing the agent makes entities unavailable until it reconnects."""
    await _setup(hass, config_entry)
    assert hass.states.get(AGENT_CONNECTED).state == STATE_ON
    fake_client.listen_error = LitraConnectionError("gone")
    fake_client.close_stream()
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == STATE_UNAVAILABLE
    assert hass.states.get(AGENT_CONNECTED).state == STATE_OFF


async def test_stream_auth_failure_starts_reauth(
    hass: HomeAssistant, fake_client: FakeClient, config_entry: MockConfigEntry
) -> None:
    """A token revoked while running triggers reauth."""
    await _setup(hass, config_entry)
    fake_client.listen_error = LitraAuthError("revoked")
    fake_client.close_stream()
    # The stream task exits after starting reauth, so it can be awaited.
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get(ENTITY).state == STATE_UNAVAILABLE
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"]["source"] == "reauth" for flow in flows)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (LitraConnectionError("down"), ConfigEntryState.SETUP_RETRY),
        (LitraAuthError("bad"), ConfigEntryState.SETUP_ERROR),
    ],
)
async def test_setup_errors(
    hass: HomeAssistant,
    fake_client: FakeClient,
    config_entry: MockConfigEntry,
    error: Exception,
    expected: ConfigEntryState,
) -> None:
    """Unreachable retries; bad credentials go to reauth."""
    fake_client.error = error
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is expected


async def test_wrong_agent_at_address(
    hass: HomeAssistant, fake_client: FakeClient, config_entry: MockConfigEntry
) -> None:
    """A different agent at the stored address is refused."""
    fake_client.info["agent_id"] = "someoneelse"
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR


async def test_unload(
    hass: HomeAssistant, fake_client: FakeClient, config_entry: MockConfigEntry
) -> None:
    """Unload stops the stream task cleanly."""
    await _setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED
