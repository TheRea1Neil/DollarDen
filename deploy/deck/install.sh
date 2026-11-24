#!/usr/bin/env bash
# Install the GPU probe on the gaming PC, as a USER service.
#
# No sudo and nothing written outside $HOME: SteamOS mounts / read-only
# and its sudo wants a password, so a system unit cannot be installed
# unattended. `loginctl Linger` is already enabled on this machine, so a
# user unit still runs when nobody is logged in -- which is the case
# that matters, since the deck is usually woken remotely.
#
# Idempotent. Re-run after a SteamOS major update if the probe stops
# answering.
set -euo pipefail

TOKEN="${1:-}"
[ -n "$TOKEN" ] || { echo "usage: install.sh <shared-token>" >&2; exit 2; }

mkdir -p ~/.local/bin ~/.config/systemd/user
install -m 0755 /tmp/dollarden-gpu-probe.py ~/.local/bin/dollarden-gpu-probe.py
printf '%s' "$TOKEN" > ~/.config/dollarden-gpu-probe.token
chmod 0600 ~/.config/dollarden-gpu-probe.token

cat > ~/.config/systemd/user/dollarden-gpu-probe.service <<UNIT
[Unit]
Description=dollarden GPU headroom probe
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 %h/.local/bin/dollarden-gpu-probe.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now dollarden-gpu-probe
sleep 1
systemctl --user is-active dollarden-gpu-probe
