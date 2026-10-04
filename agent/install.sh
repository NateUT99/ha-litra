#!/bin/zsh
# Build litra-agent, install it for the current user, and (re)load the LaunchAgent.
# Run from the agent/ directory as the user the Litra is plugged in for — not root.
set -euo pipefail

LABEL="com.github.nateut99.litra-agent"
BIN="$HOME/.local/bin/litra-agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ $EUID -eq 0 ]]; then
  echo "Run as your normal user, not root: HID access comes from the user session." >&2
  exit 1
fi

cd "${0:A:h}"
cargo build --release
install -d "$HOME/.local/bin" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
install -m 755 target/release/litra-agent "$BIN"
sed "s|__HOME__|$HOME|g" "launchd/$LABEL.plist" > "$PLIST"

# The application firewall keys its allow-list on the code signature, and each
# rebuild gets a new ad-hoc signature, so the rule is re-applied every install.
FW=/usr/libexec/ApplicationFirewall/socketfilterfw
if "$FW" --getglobalstate | grep -q "enabled"; then
  echo "Allowing litra-agent through the macOS application firewall (sudo):"
  sudo "$FW" --add "$BIN" >/dev/null
  sudo "$FW" --unblockapp "$BIN" >/dev/null
fi

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "litra-agent installed and running. Logs: ~/Library/Logs/litra-agent.log"
echo "Pair with Home Assistant: $BIN pair"
