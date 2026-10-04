"""Client for the litra-agent HTTPS/WebSocket API."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields
import hashlib
import socket
import ssl
from typing import Any

import aiohttp
from yarl import URL

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


class LitraError(Exception):
    """Base error talking to the agent."""


class LitraConnectionError(LitraError):
    """The agent could not be reached."""


class LitraAuthError(LitraError):
    """The agent rejected the token."""


class LitraFingerprintError(LitraError):
    """The agent's certificate does not match the pinned fingerprint."""


@dataclass(frozen=True, slots=True)
class LitraDevice:
    """State of one light attached to the agent."""

    id: str
    serial: str | None
    model: str
    is_on: bool
    brightness_lumen: int
    temperature_kelvin: int
    min_brightness_lumen: int
    max_brightness_lumen: int
    min_temperature_kelvin: int
    max_temperature_kelvin: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LitraDevice:
        """Build from an API payload, ignoring fields this version doesn't know."""
        return cls(**{field.name: data[field.name] for field in fields(cls)})


def format_fingerprint(der: bytes) -> str:
    """SHA-256 of a DER certificate as colon-separated uppercase hex."""
    return ":".join(f"{byte:02X}" for byte in hashlib.sha256(der).digest())


def fetch_fingerprint(host: str, port: int) -> str:
    """Read the agent's certificate fingerprint (blocking; run in an executor).

    Verification is deliberately off here: this is the trust-on-first-use step,
    and the user confirms the result against `litra-agent pair` output.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with (
            socket.create_connection((host, port), timeout=10) as sock,
            context.wrap_socket(sock) as tls,
        ):
            der = tls.getpeercert(binary_form=True)
    except OSError as err:
        raise LitraConnectionError(str(err)) from err
    if der is None:
        raise LitraConnectionError("agent presented no certificate")
    return format_fingerprint(der)


class LitraAgentClient:
    """Authenticated client pinned to one agent certificate."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        token: str,
        fingerprint: str,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._base = URL.build(scheme="https", host=host, port=port, path="/api/v1")
        self._headers = {"Authorization": f"Bearer {token}"}
        # aiohttp checks the peer certificate's SHA-256 against this before any
        # request data is sent, replacing CA validation for a self-signed cert.
        self._ssl = aiohttp.Fingerprint(bytes.fromhex(fingerprint.replace(":", "")))

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            async with self._session.request(
                method,
                self._base / path,
                json=body,
                headers=self._headers,
                ssl=self._ssl,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status == 401:
                    raise LitraAuthError("token rejected")
                if response.status >= 400:
                    raise LitraError(
                        f"{method} {path}: HTTP {response.status} {await response.text()}"
                    )
                return await response.json()
        except aiohttp.ServerFingerprintMismatch as err:
            raise LitraFingerprintError("certificate fingerprint changed") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise LitraConnectionError(str(err) or type(err).__name__) from err

    async def get_info(self) -> dict[str, Any]:
        """Return agent identity and version."""
        return await self._request("GET", "info")

    async def get_devices(self) -> list[LitraDevice]:
        """Return every attached light."""
        return [LitraDevice.from_dict(device) for device in await self._request("GET", "devices")]

    async def set_state(
        self,
        device_id: str,
        *,
        on: bool | None = None,
        brightness_lumen: int | None = None,
        temperature_kelvin: int | None = None,
    ) -> LitraDevice:
        """Apply a change in one request; returns the state read back from the device."""
        body: dict[str, Any] = {}
        if on is not None:
            body["on"] = on
        if brightness_lumen is not None:
            body["brightness_lumen"] = brightness_lumen
        if temperature_kelvin is not None:
            body["temperature_kelvin"] = temperature_kelvin
        return LitraDevice.from_dict(await self._request("POST", f"devices/{device_id}", body))

    async def listen(self, on_devices: Callable[[list[LitraDevice]], None]) -> None:
        """Stream device state until the connection closes.

        The agent sends the full device list on connect and after every change.
        """
        try:
            async with self._session.ws_connect(
                self._base / "events",
                headers=self._headers,
                ssl=self._ssl,
                heartbeat=30,
                timeout=aiohttp.ClientWSTimeout(ws_close=10),
            ) as ws:
                async for message in ws:
                    if message.type is aiohttp.WSMsgType.TEXT:
                        payload = message.json()
                        if payload.get("type") == "devices":
                            on_devices([LitraDevice.from_dict(d) for d in payload["devices"]])
                    elif message.type is aiohttp.WSMsgType.ERROR:
                        raise LitraConnectionError(str(ws.exception()))
        except aiohttp.WSServerHandshakeError as err:
            if err.status == 401:
                raise LitraAuthError("token rejected") from err
            raise LitraConnectionError(str(err)) from err
        except aiohttp.ServerFingerprintMismatch as err:
            raise LitraFingerprintError("certificate fingerprint changed") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise LitraConnectionError(str(err) or type(err).__name__) from err
