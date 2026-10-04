"""Config flow tests."""

from __future__ import annotations

from ipaddress import ip_address
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.litra.api import LitraAuthError, LitraConnectionError, LitraFingerprintError
from custom_components.litra.const import CONF_FINGERPRINT, DOMAIN
from homeassistant.config_entries import SOURCE_USER, SOURCE_ZEROCONF
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .conftest import AGENT_ID, FINGERPRINT, TOKEN, FakeClient

DISCOVERY = ZeroconfServiceInfo(
    ip_address=ip_address("192.0.2.20"),
    ip_addresses=[ip_address("192.0.2.20")],
    hostname="mac-mini.local.",
    name="Litra Agent on mac-mini._litra-agent._tcp.local.",
    port=47810,
    type="_litra-agent._tcp.local.",
    properties={"id": AGENT_ID, "api": "1"},
)


async def test_manual_pairing(hass: HomeAssistant, fake_client: FakeClient) -> None:
    """Host → fingerprint shown → token → entry with the pinned fingerprint."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.20", CONF_PORT: 47810}
    )
    assert result["step_id"] == "pair"
    assert result["description_placeholders"]["fingerprint"] == FINGERPRINT

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: TOKEN})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == AGENT_ID
    assert result["data"] == {
        CONF_HOST: "192.0.2.20",
        CONF_PORT: 47810,
        CONF_TOKEN: TOKEN,
        CONF_FINGERPRINT: FINGERPRINT,
    }


async def test_manual_unreachable(hass: HomeAssistant, fake_client: FakeClient) -> None:
    """Unreachable host stays on the form."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with patch(
        "custom_components.litra.config_flow.fetch_fingerprint",
        side_effect=LitraConnectionError("refused"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.0.2.99", CONF_PORT: 47810}
        )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_bad_token_then_good(hass: HomeAssistant, fake_client: FakeClient) -> None:
    """A rejected token shows invalid_auth and lets the user retry."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.20", CONF_PORT: 47810}
    )
    fake_client.error = LitraAuthError("nope")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: "bad"})
    assert result["errors"] == {"base": "invalid_auth"}

    fake_client.error = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: TOKEN})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_fingerprint_changed_mid_flow(hass: HomeAssistant, fake_client: FakeClient) -> None:
    """A certificate swap between fetch and validate is refused."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.20", CONF_PORT: 47810}
    )
    fake_client.error = LitraFingerprintError("mismatch")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: TOKEN})
    assert result["errors"] == {"base": "fingerprint_changed"}


async def test_unsupported_api_version(hass: HomeAssistant, fake_client: FakeClient) -> None:
    """An agent speaking another API major version is refused."""
    fake_client.info["api_version"] = 2
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.20", CONF_PORT: 47810}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: TOKEN})
    assert result["errors"] == {"base": "unsupported_version"}


async def test_zeroconf_pairing(hass: HomeAssistant, fake_client: FakeClient) -> None:
    """Discovery goes straight to the pairing step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=DISCOVERY
    )
    assert result["step_id"] == "pair"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: TOKEN})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "192.0.2.20"


async def test_zeroconf_updates_moved_agent(
    hass: HomeAssistant, fake_client: FakeClient, config_entry: MockConfigEntry
) -> None:
    """Rediscovery of a configured agent at a new address updates the host."""
    config_entry.add_to_hass(hass)
    moved = ZeroconfServiceInfo(
        ip_address=ip_address("192.0.2.21"),
        ip_addresses=[ip_address("192.0.2.21")],
        hostname=DISCOVERY.hostname,
        name=DISCOVERY.name,
        port=47810,
        type=DISCOVERY.type,
        properties=DISCOVERY.properties,
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=moved
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry.data[CONF_HOST] == "192.0.2.21"


async def test_reauth(
    hass: HomeAssistant, fake_client: FakeClient, config_entry: MockConfigEntry
) -> None:
    """Reauth stores the new token and current fingerprint."""
    config_entry.add_to_hass(hass)
    new_fp = ":".join(["CD"] * 32)
    with patch("custom_components.litra.config_flow.fetch_fingerprint", return_value=new_fp):
        result = await config_entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        assert result["description_placeholders"]["status"] == "CHANGED since pairing"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_TOKEN: "b" * 64}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_TOKEN] == "b" * 64
    assert config_entry.data[CONF_FINGERPRINT] == new_fp
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_reauth_wrong_agent(
    hass: HomeAssistant, fake_client: FakeClient, config_entry: MockConfigEntry
) -> None:
    """Reauth refuses a different agent answering at the old address."""
    config_entry.add_to_hass(hass)
    fake_client.info["agent_id"] = "someoneelse"
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: TOKEN})
    assert result["reason"] == "wrong_agent"
    assert config_entry.data[CONF_TOKEN] == TOKEN


async def test_reconfigure(
    hass: HomeAssistant, fake_client: FakeClient, config_entry: MockConfigEntry
) -> None:
    """Reconfigure changes the address, keeping token and pinned fingerprint."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.30", CONF_PORT: 47810}
    )
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_HOST] == "192.0.2.30"
    assert config_entry.data[CONF_FINGERPRINT] == FINGERPRINT
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(config_entry.entry_id)
