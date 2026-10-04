# Logitech Litra for Home Assistant

Control USB Logitech Litra lights from Home Assistant (tested on the Litra Glow; see [Supported devices](#supported-devices)).

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
- Other programs can talk to the light at the same time (for example the `litra` CLI).
  On macOS every open HID handle receives every response, so the agent only accepts a
  response that echoes its own request's header, and discards anything else.

## Example: key light follows your Mac's camera

The [Home Assistant Companion App for macOS](https://companion.home-assistant.io/) reports
`binary_sensor.<device>_camera_in_use`. This automation turns the key light on at a
video-call preset when the camera starts, and off once the camera has been off for 10
seconds. Replace the two entity IDs with your own.

<!-- example-automation -->
```yaml
alias: "Desk: Key Light Follows Camera"
description: >-
  Turns the desk key light on at a video-call preset while the Mac's camera is
  in use, and off once the camera has been off for 10 seconds.
mode: restart
triggers:
  - alias: Camera turned on
    trigger: state
    entity_id: binary_sensor.my_mac_camera_in_use
    to: "on"
    id: camera_on
  - alias: Camera off for 10 seconds
    trigger: state
    entity_id: binary_sensor.my_mac_camera_in_use
    to: "off"
    # Rides out brief camera drops (an app switching cameras, a quick reconnect)
    # so the light doesn't flicker mid-call.
    for:
      seconds: 10
    id: camera_off
conditions: []
actions:
  - alias: Follow the camera
    choose:
      - alias: Camera on
        conditions:
          - alias: Triggered by the camera turning on
            condition: trigger
            id: camera_on
        sequence:
          - alias: Key light to video-call preset
            action: light.turn_on
            target:
              entity_id: light.desk_key_light
            data:
              brightness_pct: 45
              color_temp_kelvin: 4500
      - alias: Camera off
        conditions:
          - alias: Triggered by the camera turning off
            condition: trigger
            id: camera_off
        sequence:
          - alias: Key light off
            action: light.turn_off
            target:
              entity_id: light.desk_key_light
```

Ideas for extending it:

- **Skip the preset when the light can't respond.** Check the agent's *Connected* sensor
  and the light's availability before applying the preset. *Connected* off means the agent
  is unreachable; the light `unavailable` while *Connected* is on means the Litra is
  unplugged.
- **Tolerate longer pauses.** If you step away with the camera off but the call still
  connected, wait for the Mac's *Audio Input In Use* sensor to go off before turning the
  light off, rather than lengthening the `for:` delay.

## Example: notify when the agent goes offline

The agent device has a *Connected* sensor. It turns off when Home Assistant loses the
agent's event stream: the agent stopped, the computer is asleep or off, or the firewall
is blocking it after an upgrade. This automation sends a notification to the Mac through
the Companion App after two minutes offline, which rides out a restart. Replace the two
entity IDs with your own. If the computer itself might be asleep or off, target your
phone instead, since the Mac won't see the notification until it wakes.

<!-- example-agent-offline -->
```yaml
alias: "Desk: Litra Agent Offline Alert"
description: >-
  Notifies the Mac when the Litra agent has been unreachable for two minutes,
  so the key light isn't silently unavailable.
mode: single
triggers:
  - alias: Agent disconnected for two minutes
    trigger: state
    entity_id: binary_sensor.litra_agent_connected
    from: "on"
    to: "off"
    # Long enough to ride out an agent restart or a brief network blip.
    for:
      minutes: 2
conditions: []
actions:
  - alias: Notify the Mac
    action: notify.mobile_app_my_mac
    data:
      title: Litra agent offline
      message: >-
        Home Assistant can't reach litra-agent. Check that it's running with
        `launchctl print gui/$(id -u)/com.github.nateut99.litra-agent`.
```

## Acknowledgments and licensing

The agent talks to the hardware through [`litra`](https://github.com/timrogers/litra-rs)
by Tim Rogers (MIT), the library behind the `litra` CLI. It uses the library's device discovery
and its set commands. The agent's own query path reuses the HID++ request layout that
litra-rs documents, adding response matching and timeouts so it works alongside other
processes (see Behavior notes). Thanks to that project for the protocol work that made this
possible.

This repository is MIT-licensed (see `LICENSE`). It contains only its own source.
`litra` and every other Rust dependency are downloaded by Cargo at build time under their own
licenses, all permissive (MIT, Apache-2.0, ISC, BSD). Anyone redistributing a built
`litra-agent` binary must include those dependencies' license notices with it;
[`cargo-about`](https://github.com/EmbarkStudios/cargo-about) can generate them.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements_test.txt ruff
.venv/bin/pytest -q && .venv/bin/ruff check custom_components tests
cd agent && cargo clippy --release && cargo build --release
```

`tests/test_readme_example.py` loads both example automations above into Home Assistant
and runs them, so the README can't drift into invalid YAML.
