"""Config flow for Logitech Litra.

Pairing is trust-on-first-use: the flow reads the agent's self-signed
certificate fingerprint, shows it, and the user confirms it matches the
`litra-agent pair` output before entering the token. The fingerprint is then
pinned for every connection.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api import (
    LitraAgentClient,
    LitraAuthError,
    LitraConnectionError,
    LitraError,
    LitraFingerprintError,
    fetch_fingerprint,
)
from .const import API_VERSION, CONF_FINGERPRINT, DEFAULT_PORT, DOMAIN

TOKEN_SCHEMA = vol.Schema({vol.Required(CONF_TOKEN): str})


def _usable_host(discovery_info: ZeroconfServiceInfo) -> str | None:
    """Pick the address to store from a discovery: IPv4 first, then a routable IPv6.

    Link-local IPv6 (fe80::/10) is never usable as a stored host: it needs an
    interface scope that isn't valid from another machine.
    """
    addresses = discovery_info.ip_addresses or [discovery_info.ip_address]
    for address in sorted(addresses, key=lambda a: a.version):
        if address.version == 4 or not address.is_link_local:
            return str(address)
    return None


class LitraConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pair with a litra-agent."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._host: str = ""
        self._port: int = DEFAULT_PORT
        self._fingerprint: str = ""

    async def _async_fetch_fingerprint(self) -> None:
        self._fingerprint = await self.hass.async_add_executor_job(
            fetch_fingerprint, self._host, self._port
        )

    async def _async_validate(self, token: str) -> tuple[dict[str, Any] | None, str | None]:
        """Return (agent info, None) or (None, error key)."""
        client = LitraAgentClient(
            async_get_clientsession(self.hass), self._host, self._port, token, self._fingerprint
        )
        try:
            info = await client.get_info()
        except LitraAuthError:
            return None, "invalid_auth"
        except LitraFingerprintError:
            return None, "fingerprint_changed"
        except LitraConnectionError:
            return None, "cannot_connect"
        except LitraError:
            return None, "unknown"
        if info.get("api_version") != API_VERSION:
            return None, "unsupported_version"
        return info, None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manual setup: ask for the agent's address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = user_input[CONF_HOST]
            self._port = user_input[CONF_PORT]
            try:
                await self._async_fetch_fingerprint()
            except LitraConnectionError:
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_pair()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_HOST): str,
                        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                    }
                ),
                user_input,
            ),
            errors=errors,
        )

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """Agent discovered on the network."""
        if not (agent_id := discovery_info.properties.get("id")):
            return self.async_abort(reason="invalid_discovery")
        await self.async_set_unique_id(agent_id)
        if (host := _usable_host(discovery_info)) is None:
            return self.async_abort(reason="no_usable_address")
        # Follow the agent if its address changed; credentials stay valid.
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: host, CONF_PORT: discovery_info.port}
        )
        self._host = host
        self._port = discovery_info.port or DEFAULT_PORT
        try:
            await self._async_fetch_fingerprint()
        except LitraConnectionError:
            return self.async_abort(reason="cannot_connect")
        self.context["title_placeholders"] = {"name": discovery_info.name.split(".")[0]}
        return await self.async_step_pair()

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm the fingerprint and enter the token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            info, error = await self._async_validate(user_input[CONF_TOKEN])
            if info is not None:
                await self.async_set_unique_id(info["agent_id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Litra Agent ({info['name']})",
                    data={
                        CONF_HOST: self._host,
                        CONF_PORT: self._port,
                        CONF_TOKEN: user_input[CONF_TOKEN],
                        CONF_FINGERPRINT: self._fingerprint,
                    },
                )
            errors["base"] = error or "unknown"

        return self.async_show_form(
            step_id="pair",
            data_schema=TOKEN_SCHEMA,
            description_placeholders={
                "host": f"{self._host}:{self._port}",
                "fingerprint": self._fingerprint,
            },
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Token rejected or certificate changed: re-pair."""
        self._host = entry_data[CONF_HOST]
        self._port = entry_data[CONF_PORT]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the current fingerprint and take a new token."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            info, error = await self._async_validate(user_input[CONF_TOKEN])
            if info is not None:
                if info["agent_id"] != entry.unique_id:
                    return self.async_abort(reason="wrong_agent")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_TOKEN: user_input[CONF_TOKEN],
                        CONF_FINGERPRINT: self._fingerprint,
                    },
                )
            errors["base"] = error or "unknown"
        else:
            try:
                await self._async_fetch_fingerprint()
            except LitraConnectionError:
                return self.async_abort(reason="cannot_connect")

        changed = self._fingerprint != entry.data[CONF_FINGERPRINT]
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=TOKEN_SCHEMA,
            description_placeholders={
                "fingerprint": self._fingerprint,
                "status": "CHANGED since pairing" if changed else "unchanged since pairing",
            },
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the agent's address. The pinned fingerprint must still match."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = user_input[CONF_HOST]
            self._port = user_input[CONF_PORT]
            self._fingerprint = entry.data[CONF_FINGERPRINT]
            info, error = await self._async_validate(entry.data[CONF_TOKEN])
            if info is not None:
                if info["agent_id"] != entry.unique_id:
                    return self.async_abort(reason="wrong_agent")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_HOST: self._host, CONF_PORT: self._port}
                )
            errors["base"] = error or "unknown"

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Required(CONF_HOST): str, vol.Required(CONF_PORT): int}),
                user_input or entry.data,
            ),
            errors=errors,
        )
