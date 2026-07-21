#!/usr/bin/env bash
# One-shot installer for the signal agent on a DigitalOcean droplet.
# Usage (one line, with your values):
#   TELEGRAM_BOT_TOKEN=8709225118:AAG4Pr1gEWAkftT1k-JgDD4YufczufcCI_c TELEGRAM_CHAT_ID=5196922172 REPO_URL=https://github.com/YOU/YOURREPO.git bash setup-digitalocean.sh
set -euo pipefail

: "${TELEGRAM_BOT_TOKEN:?Set TELEGRAM_BOT_TOKEN=... before running}"
: "${TELEGRAM_CHAT_ID:?Set TELEGRAM_CHAT_ID=... before running}"
: "${REPO_URL:?Set REPO_URL=https://github.com/YOU/YOURREPO.git before running}"

APP=/opt/btc-agent
mkdir -p "$APP"

# 1) clone (or refresh) the repo - GitHub stays your deployment mechanism
if [ -d "$APP/repo/.git" ]; then
  git -C "$APP/repo" pull --ff-only
else
  git clone "$REPO_URL" "$APP/repo"
fi

# 2) deploy a copy OUTSIDE the repo so local state never fights git
cp "$APP/repo/btc_alert_agent.py" "$APP/btc_alert_agent.py"
echo '{}' > "$APP/btc_agent_state.json"

# 3) credentials
cat > "$APP/env" << ENVEOF
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
ENVEOF
chmod 600 "$APP/env"

# 4) systemd service: starts on boot, restarts on any crash
cat > /etc/systemd/system/btc-agent.service << 'UNITEOF'
[Unit]
Description=Signal alert agent (loop mode)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/btc-agent
EnvironmentFile=/opt/btc-agent/env
ExecStart=/usr/bin/python3 /opt/btc-agent/btc_alert_agent.py --loop
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
UNITEOF

# 5) auto-deploy: pull the repo every 5 min; restart only when the agent changed
cat > "$APP/update.sh" << 'UPDEOF'
#!/usr/bin/env bash
set -e
APP=/opt/btc-agent
git -C "$APP/repo" fetch -q origin
LOCAL=$(git -C "$APP/repo" rev-parse @)
REMOTE=$(git -C "$APP/repo" rev-parse @{u})
[ "$LOCAL" = "$REMOTE" ] && exit 0
git -C "$APP/repo" pull -q --ff-only
if ! cmp -s "$APP/repo/btc_alert_agent.py" "$APP/btc_alert_agent.py"; then
  cp "$APP/repo/btc_alert_agent.py" "$APP/btc_alert_agent.py"
  echo '{}' > "$APP/btc_agent_state.json"   # new code = fresh state
  systemctl restart btc-agent
  echo "$(date -u) agent updated + restarted" >> "$APP/deploys.log"
fi
UPDEOF
chmod +x "$APP/update.sh"
echo "*/5 * * * * root /opt/btc-agent/update.sh >/dev/null 2>&1" > /etc/cron.d/btc-agent-update

# 6) go
systemctl daemon-reload
systemctl enable --now btc-agent
sleep 3
systemctl --no-pager status btc-agent | head -8
echo
echo "Done. Live logs: journalctl -u btc-agent -f"
