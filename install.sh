#!/usr/bin/env bash
# PiBuddy one-shot installer for the Raspberry Pi.
#
#   ./install.sh              set up a venv, deps, config with a fresh token
#   ./install.sh --systemd    also install + enable the boot-time service
#
# Afterwards, pair each laptop by running the command this script prints
# (or scan the QR code on the buddy's stats screen).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="${HOME}/.config/pibuddy"
CONFIG="${CONFIG_DIR}/config.json"
VENV="${REPO_DIR}/.venv"
PORT=8765

echo "==> Installing Python dependencies"
if ! python3 -m venv --help >/dev/null 2>&1; then
    echo "python3-venv is missing. Install it with: sudo apt install python3-venv python3-dev"
    exit 1
fi
python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"

if [ ! -f "$CONFIG" ]; then
    echo "==> Writing $CONFIG with a fresh token"
    mkdir -p "$CONFIG_DIR"
    TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
    cat > "$CONFIG" <<EOF
{
  "port": $PORT,
  "token": "$TOKEN",
  "sound": false
}
EOF
else
    echo "==> Keeping existing $CONFIG"
    TOKEN=$(python3 -c "import json; print(json.load(open('$CONFIG')).get('token',''))")
fi

if [ "${1:-}" = "--systemd" ]; then
    echo "==> Installing systemd service (needs sudo)"
    UNIT=/etc/systemd/system/pibuddy.service
    sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=PiBuddy - Claude Code desk companion
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
SupplementaryGroups=video input render
WorkingDirectory=$REPO_DIR
ExecStart=$VENV/bin/python -m pibuddy
Restart=on-failure
RestartSec=3
Environment=SDL_VIDEODRIVER=kmsdrm

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable --now pibuddy
    echo "==> Service running. Logs: journalctl -u pibuddy -f"
else
    echo "==> Start the buddy with: $VENV/bin/python -m pibuddy"
    echo "    (add --systemd to this script to install the boot service)"
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "==> Pair each laptop where you use Claude Code:"
echo "    python3 scripts/install-hooks.py --url http://${IP:-$(hostname).local}:$PORT --token '$TOKEN'"
echo "    (add --approvals 'Bash' for touchscreen permission prompts)"
echo
echo "==> Phone remote: http://${IP:-$(hostname).local}:$PORT/?token=$TOKEN"
