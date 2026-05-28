#!/usr/bin/env bash
# r/homelabsales monitor — Linux setup script
# Run this once on your server: chmod +x setup.sh && ./setup.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="homelabsales-monitor"

echo "=== r/homelabsales monitor setup ==="
echo

# ── Python check ───────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found."
    echo "  Install it with: sudo apt install python3 python3-pip"
    exit 1
fi

PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
if [ "$PY_MAJOR" -lt 3 ] || [ "$PY_MINOR" -lt 10 ]; then
    echo "ERROR: Python 3.10+ required (found 3.$PY_MINOR)"
    exit 1
fi
echo "Python $(python3 --version) — OK"

# ── Dependencies ───────────────────────────────────────────────────────────────
echo "Installing dependencies..."
pip3 install -r "$SCRIPT_DIR/requirements.txt" --quiet
echo "Dependencies installed."

# ── Credentials ────────────────────────────────────────────────────────────────
if [ -f "$SCRIPT_DIR/.env" ]; then
    echo
    echo ".env already exists — skipping credential setup."
    echo "Edit $SCRIPT_DIR/.env if you need to change anything."
else
    echo
    echo "Enter your credentials (nothing is sent anywhere, stored locally in .env):"
    echo

    read -rp "  Discord webhook URL:    " WEBHOOK_URL
    read -rp "  Reddit client ID:       " CLIENT_ID
    read -rsp "  Reddit client secret:   " CLIENT_SECRET
    echo

    cat > "$SCRIPT_DIR/.env" <<EOF
DISCORD_WEBHOOK_URL=$WEBHOOK_URL
REDDIT_CLIENT_ID=$CLIENT_ID
REDDIT_CLIENT_SECRET=$CLIENT_SECRET
EOF
    chmod 600 "$SCRIPT_DIR/.env"
    echo "  .env created (permissions set to 600)."
fi

# ── Quick test run ─────────────────────────────────────────────────────────────
echo
read -rp "Run a quick test now to verify credentials work? [Y/n] " RUN_TEST
if [[ "$RUN_TEST" =~ ^[Nn]$ ]]; then
    echo "Skipping test."
else
    echo "Running test..."
    set -a; source "$SCRIPT_DIR/.env"; set +a
    python3 "$SCRIPT_DIR/monitor.py" && echo "Test passed." || echo "Test failed — check credentials above."
fi

# ── Systemd service ────────────────────────────────────────────────────────────
echo
echo "Installing systemd service and timer..."

sudo tee /etc/systemd/system/${SERVICE}.service > /dev/null <<EOF
[Unit]
Description=r/homelabsales SATA HDD deal monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$(command -v python3) $SCRIPT_DIR/monitor.py
EnvironmentFile=$SCRIPT_DIR/.env
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE
EOF

sudo tee /etc/systemd/system/${SERVICE}.timer > /dev/null <<EOF
[Unit]
Description=Run r/homelabsales monitor every 5 minutes
Requires=${SERVICE}.service

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ${SERVICE}.timer

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  All done. Timer is active and will run every 5 minutes."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
echo "  Useful commands:"
echo "  sudo systemctl status $SERVICE.timer    # is the timer running?"
echo "  sudo systemctl start  $SERVICE.service  # trigger a manual run"
echo "  sudo journalctl -u $SERVICE.service -f  # watch the logs live"
echo "  sudo systemctl disable --now $SERVICE.timer  # turn it off"
echo
