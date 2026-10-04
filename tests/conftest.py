"""Fixtures for Logitech Litra tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.litra.api import LitraDevice
from custom_components.litra.const import CONF_FINGERPRINT, DOMAIN
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN

AGENT_ID = "0123456789abcdef0123456789abcdef"
FINGERPRINT = ":".join(["AB"] * 32)
TOKEN = "a" * 64
INFO = {"agent_id": AGENT_ID, "name": "mac-mini", "version": "0.1.0", "api_version": 1}

GLOW = LitraDevice(
    id="TESTSERIAL01",
    serial="TESTSERIAL01",
    model="Litra Glow",
    is_on=False,
    brightness_lumen=124,
    temperature_kelvin=4500,
    min_brightness_lumen=20,
    max_brightness_lumen=250,
    min_temperature_kelvin=2700,
    max_temperature_kelvin=6500,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


class FakeClient:
    """Stands in for LitraAgentClient; tests drive the event stream by hand."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.devices = [GLOW]
        self.info = dict(INFO)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error: Exception | None = None
        self.listen_error: Exception | None = None
        self._push: Callable[[list[LitraDevice]], None] | None = None
        self._closed = asyncio.Event()

    async def get_info(self) -> dict[str, Any]:
        if self.error:
            raise self.error
        return self.info

    async def get_devices(self) -> list[LitraDevice]:
        if self.error:
            raise self.error
        return self.devices

    async def set_state(self, device_id: str, **changes: Any) -> LitraDevice:
        if self.error:
            raise self.error
        self.calls.append((device_id, changes))
        device = next(d for d in self.devices if d.id == device_id)
        updated = replace(
            device,
            is_on=changes["on"] if changes.get("on") is not None else device.is_on,
            brightness_lumen=changes.get("brightness_lumen") or device.brightness_lumen,
            temperature_kelvin=changes.get("temperature_kelvin") or device.temperature_kelvin,
        )
        self.devices = [updated if d.id == device_id else d for d in self.devices]
        return updated

    async def listen(self, on_devices: Callable[[list[LitraDevice]], None]) -> None:
        if self.listen_error:
            raise self.listen_error
        self._push = on_devices
        self._closed.clear()
        on_devices(self.devices)
        await self._closed.wait()

    def push(self, devices: list[LitraDevice]) -> None:
        assert self._push is not None
        self.devices = devices
        self._push(devices)

    def close_stream(self) -> None:
        self._closed.set()


@pytest.fixture
def fake_client() -> Generator[FakeClient]:
    """Patch the client everywhere it's constructed."""
    client = FakeClient()
    with (
        patch("custom_components.litra.LitraAgentClient", return_value=client),
        patch("custom_components.litra.config_flow.LitraAgentClient", return_value=client),
        patch("custom_components.litra.config_flow.fetch_fingerprint", return_value=FINGERPRINT),
        patch("custom_components.litra.INITIAL_BACKOFF", 0),
    ):
        yield client


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A paired agent."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=AGENT_ID,
        title="Litra Agent (mac-mini)",
        data={
            CONF_HOST: "192.0.2.20",
            CONF_PORT: 47810,
            CONF_TOKEN: TOKEN,
            CONF_FINGERPRINT: FINGERPRINT,
        },
    )
