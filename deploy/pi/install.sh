#!/usr/bin/env bash
# Install the deck power relay tools on the Pi.
#
#   sudo deploy/pi/install.sh
#
# Leaves behind:
#   /usr/local/bin/deck-power        the CLI, for a person at a terminal
#   deck-power-api.service           the HTTP shim the nnf container uses
#   /var/lib/deck-power/actions.log  every press, refusal and reason
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/deck-power
# Whoever is running this, not whoever wrote it.
USER_NAME="${SUDO_USER:-$(id -un)}"

install -d -m 755 "$DEST"
install -m 755 "$HERE/deck_power.py" "$DEST/deck_power.py"
install -m 755 "$HERE/deck_power_api.py" "$DEST/deck_power_api.py"
ln -sf "$DEST/deck_power.py" /usr/local/bin/deck-power

# The log has to be writable by both the CLI (run as that user) and the
# service, so it is group-owned by gpio -- which is already the group
# allowed to touch the pins.
install -d -m 775 -o "$USER_NAME" -g gpio /var/lib/deck-power

TOKEN_FILE=/etc/deck-power.env
if [[ ! -f "$TOKEN_FILE" ]]; then
  printf 'DECK_POWER_TOKEN=%s\n' "$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)" > "$TOKEN_FILE"
  chmod 640 "$TOKEN_FILE"
  chgrp gpio "$TOKEN_FILE"
  echo "generated $TOKEN_FILE"
fi

cat > /etc/systemd/system/deck-power-api.service <<UNIT
[Unit]
Description=Power relay shim for the deck (nnf)
After=network.target

[Service]
Type=simple
User=$USER_NAME
SupplementaryGroups=gpio
EnvironmentFile=$TOKEN_FILE
WorkingDirectory=$DEST
ExecStart=/usr/bin/python3 $DEST/deck_power_api.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now deck-power-api.service
systemctl --no-pager -l status deck-power-api.service | head -5

echo
echo "token for the API (put in ~/nnf/.env as DECK_POWER_TOKEN):"
grep DECK_POWER_TOKEN "$TOKEN_FILE"
