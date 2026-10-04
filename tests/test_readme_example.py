"""The README's example automation loads in Home Assistant and behaves as described."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)
import yaml

from homeassistant.components.automation import DOMAIN as AUTOMATION_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

README = Path(__file__).parent.parent / "README.md"
CAMERA = "binary_sensor.my_mac_camera_in_use"
LIGHT = "light.desk_key_light"


def _readme_yaml(marker: str) -> dict:
    """The first yaml block after an HTML-comment marker in the README."""
    text = README.read_text().split(f"<!-- {marker} -->", 1)[1]
    block = text.split("```yaml\n", 1)[1].split("```", 1)[0]
    return yaml.safe_load(block)


async def test_readme_example(hass: HomeAssistant) -> None:
    """Camera on → preset; camera off for 10 s → light off; a short drop → nothing."""
    hass.states.async_set(CAMERA, "off")
    turn_on = async_mock_service(hass, "light", "turn_on")
    turn_off = async_mock_service(hass, "light", "turn_off")
    assert await async_setup_component(
        hass, AUTOMATION_DOMAIN, {AUTOMATION_DOMAIN: [_readme_yaml("example-automation")]}
    )
    await hass.async_block_till_done()
    assert hass.states.get("automation.desk_key_light_follows_camera").state == "on"

    hass.states.async_set(CAMERA, "on")
    await hass.async_block_till_done()
    assert len(turn_on) == 1
    assert turn_on[0].data == {
        "entity_id": [LIGHT],
        "brightness_pct": 45,
        "color_temp_kelvin": 4500,
    }

    # A brief drop shorter than the 10 s debounce leaves the light alone.
    hass.states.async_set(CAMERA, "off")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    hass.states.async_set(CAMERA, "on")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=15))
    await hass.async_block_till_done()
    assert turn_off == []

    hass.states.async_set(CAMERA, "off")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()
    assert len(turn_off) == 1
    assert turn_off[0].data == {"entity_id": [LIGHT]}


async def test_readme_agent_offline_example(hass: HomeAssistant) -> None:
    """Offline for 2 minutes notifies once; a shorter outage doesn't."""
    sensor = "binary_sensor.litra_agent_connectivity"
    hass.states.async_set(sensor, "on")
    notify = async_mock_service(hass, "notify", "mobile_app_my_mac")
    assert await async_setup_component(
        hass, AUTOMATION_DOMAIN, {AUTOMATION_DOMAIN: [_readme_yaml("example-agent-offline")]}
    )
    await hass.async_block_till_done()

    # An agent restart: back within a minute, no alert.
    hass.states.async_set(sensor, "off")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=60))
    hass.states.async_set(sensor, "on")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=3))
    await hass.async_block_till_done()
    assert notify == []

    hass.states.async_set(sensor, "off")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2, seconds=1))
    await hass.async_block_till_done()
    assert len(notify) == 1
    assert notify[0].data["title"] == "Litra agent offline"
