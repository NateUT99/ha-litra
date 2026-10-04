# Logitech Litra for Home Assistant

Control USB Logitech Litra lights (Glow, Beam, Beam LX) from Home Assistant.

The lights only speak USB HID, so this comes in two parts:

- **`litra-agent`**: a small Rust service on the computer the light is plugged into
  (macOS). It runs as a LaunchAgent in your login session, talks to the light through
  the [`litra`](https://github.com/timrogers/litra-rs) crate, and serves an
  authenticated HTTPS + WebSocket API.
- **`custom_components/litra`**: the Home Assistant integration. It discovers the agent
  over zeroconf, pairs with it, and creates one light entity per attached Litra.

```
Home Assistant ──HTTPS (pinned cert, bearer token)──► litra-agent ──USB HID──► Litra
               ◄──WebSocket push (state on change)───┘   (LaunchAgent, your user)
```

State is pushed, not polled by HA. The agent reads each light about once a second
locally and pushes only changes, so presses of the light's own buttons and USB
unplug/replug show up in HA within about a second.

## Security model

| Control | Detail |
| --- | --- |
| No privilege | The agent runs as your user in your session; that's what grants HID access. No sudo, no service account, no SSH. |
| Transport | TLS with a self-signed certificate generated on first run. HA pins its SHA-256 fingerprint at pairing (trust on first use, confirmed by you against `litra-agent pair` output). |
| Authentication | 256-bit random bearer token on every request, including the WebSocket. The agent stores only its SHA-256 hash and compares in constant time. |
| Command surface | Four endpoints. Requests are typed JSON with unknown fields rejected and a 1 KB body limit. Nothing reaches a shell; values are clamped to the device's range before reaching HID. |
| Files | `~/Library/Application Support/litra-agent/` is `0700`; key, token hash, and agent ID are `0600`. |
| Rotation | `litra-agent pair` issues a new token and invalidates the old one immediately. HA then asks to re-pair. |

The certificate and token protect against other hosts on the LAN. They don't protect
against your own macOS user account, which can already drive the light directly.

## Install the agent (macOS)

Requires Rust (`brew install rust`).

```bash
cd agent
./install.sh          # builds, installs to ~/.local/bin, loads the LaunchAgent
~/.local/bin/litra-agent pair
```

`pair` prints a token and the certificate fingerprint. Keep the output handy for the next step.

If the macOS application firewall is on, `install.sh` adds an allow rule with `sudo`.
Each rebuild gets a new ad-hoc code signature, so re-run `install.sh` (not just
`cargo build`) after upgrading.

Logs: `~/Library/Logs/litra-agent.log`. Restart: `launchctl kickstart -k gui/$(id -u)/com.github.nateut99.litra-agent`.

## Install the integration

Copy `custom_components/litra` to `/config/custom_components/litra` on your HA host and
restart Home Assistant. (Once this repository is public, it can be added to HACS as a
custom repository instead.)

The agent should then show up under **Settings → Devices & services → Discovered**. If
not, add **Logitech Litra** manually with the Mac's hostname and port `47810`. Check that
the fingerprint shown matches the `pair` output exactly, then enter the token.

## Agent API (v1)

All routes require `Authorization: Bearer <token>`.

| Route | Purpose |
| --- | --- |
| `GET /api/v1/info` | Agent ID, name, version, API version |
| `GET /api/v1/devices` | All attached lights with state and native ranges |
| `POST /api/v1/devices/{id}` | `{on?, brightness_lumen?, temperature_kelvin?}`. The agent applies them in the order on → brightness → temperature → off, rounds kelvin to the nearest 100, clamps to range, and returns the state read back |
| `GET /api/v1/events` | WebSocket: `{"type": "devices", "devices": [...]}` on connect and on every change |

## Behavior notes

- `light.turn_on` always sends `on: true` with any brightness/temperature, because a Litra
  that is off stores those values without lighting up.
- If the agent's HID worker stops responding for 30 s, the agent exits and launchd
  restarts it. While it's down, HA shows the lights as unavailable.
- If another program also talks to the light over HID at the same moment (for example
  the `litra` CLI), the two can occasionally read each other's responses. The agent
  corrects itself on the next poll.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements_test.txt ruff
.venv/bin/pytest -q && .venv/bin/ruff check custom_components tests
cd agent && cargo clippy --release && cargo build --release
```
