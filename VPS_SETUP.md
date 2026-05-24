# Climeagent VPS Setup

This guide prepares Climeagent for production-style hosting on a Linux VPS with:

- live trading restricted to U.S. markets only
- raw ensemble probability used for live signal decisions
- separate long-running services for brain, bot, executor, and settlement
- hardened SSH, firewalling, backups, and least-privilege runtime

The app uses outbound connections only for normal operation. You do not need to expose any public web port.

## 1. Deployment Shape

Recommended stack:

- Ubuntu 24.04 LTS
- 4 vCPU minimum
- 8 GB RAM minimum
- 100 GB SSD/NVMe minimum

Preferred production headroom:

- 8 vCPU
- 16 GB RAM
- 200 GB NVMe

Processes to run concurrently:

- `brain/main.py`: signal generation
- `pyapp.bot`: Telegram bot
- `pyapp.executor`: order execution and open-trade monitoring
- `pyapp.settlement`: settlement checks, repair, claims, and feedback export

## 2. Security Baseline

Before deploying the app:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y ufw fail2ban unattended-upgrades ca-certificates curl git sqlite3 python3 python3-venv python3-pip
sudo dpkg-reconfigure --priority=low unattended-upgrades
```

Create a dedicated runtime user:

```bash
sudo adduser --disabled-password --gecos "" climeagent
sudo usermod -aG sudo climeagent
```

Use SSH keys only:

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
nano ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

Harden SSH:

```bash
sudo nano /etc/ssh/sshd_config
```

Set or confirm:

```text
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
X11Forwarding no
AllowUsers climeagent
```

Then reload SSH:

```bash
sudo systemctl reload ssh
```

Lock down the firewall. Prefer allowlisting only your admin IP:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from YOUR_ADMIN_IP to any port 22 proto tcp
sudo ufw enable
sudo ufw status verbose
```

If your cloud provider has a network firewall or security group, also restrict SSH there.

## 3. Clone And Install

Switch to the service user:

```bash
sudo -iu climeagent
```

Clone to a stable location:

```bash
git clone <your-repo-url> /opt/climeagent
cd /opt/climeagent
```

Create the virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt
```

Install Node only if you still want the convenience scripts in `package.json`:

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
npm install
```

## 4. Environment Configuration

Create the environment file:

```bash
cp .env.example .env
nano .env
chmod 600 .env
```

Required production values include:

- `TELEGRAM_BOT_TOKEN`
- `MASTER_ENCRYPTION_KEY`
- `POLYGON_RPC_URL` or `POLYGON_RPC_URLS`
- `RELAYER_API_KEY`
- `RELAYER_API_KEY_ADDRESS`
- Polymarket wallet/API values used by your production flow

Set the current live-trading mode explicitly:

```text
BLOCKY_US_ONLY_TRADING=1
```

Current live behavior after the code changes:

- non-U.S. markets are skipped at signal generation time
- live decision probability uses the raw ensemble output
- no separate intelligence/learning layer is used in live decisions

## 5. Directory Permissions

Make sure the runtime user owns the app and writable data:

```bash
sudo chown -R climeagent:climeagent /opt/climeagent
mkdir -p /opt/climeagent/data
chmod 700 /opt/climeagent/data
```

## 6. Preflight Checks

Run one-shot checks before enabling background services:

```bash
cd /opt/climeagent
source .venv/bin/activate
python -m pytest tests/test_brain_signal_location.py tests/test_brain_exact_markets.py tests/test_temperature_analysis.py -q
python -u brain/main.py
```

Stop the brain after one clean scan if needed with `Ctrl+C`.

You can also test the Python app components one by one:

```bash
python -m pyapp.bot --once
python -m pyapp.executor --once
python -m pyapp.settlement --once
```

## 7. Run Concurrent Services With systemd

Copy the provided service units:

```bash
sudo cp deploy/systemd/climeagent-*.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Enable and start them:

```bash
sudo systemctl enable --now climeagent-brain.service
sudo systemctl enable --now climeagent-bot.service
sudo systemctl enable --now climeagent-executor.service
sudo systemctl enable --now climeagent-settlement.service
```

Check status:

```bash
sudo systemctl status climeagent-brain.service
sudo systemctl status climeagent-bot.service
sudo systemctl status climeagent-executor.service
sudo systemctl status climeagent-settlement.service
```

Follow logs:

```bash
journalctl -u climeagent-brain.service -f
journalctl -u climeagent-bot.service -f
journalctl -u climeagent-executor.service -f
journalctl -u climeagent-settlement.service -f
```

Restart after a deploy:

```bash
sudo systemctl restart climeagent-brain.service climeagent-bot.service climeagent-executor.service climeagent-settlement.service
```

Why `systemd` instead of one bundled launcher:

- each process restarts independently
- failures are easier to isolate
- logs are separate
- service ordering is clearer
- security controls are stronger than a single shared process tree

## 8. Operational Notes

Brain:

- generates `data/signals.json`
- now only emits live signals for U.S. markets

Executor:

- reads `data/signals.json`
- places real or paper trades
- monitors open trades against fresh market states

Settlement:

- checks closed markets
- records settlement analysis
- exports feedback files

Bot:

- handles Telegram onboarding, controls, stats, and wallet flows

## 9. Backups And Recovery

Back up at least:

- `.env`
- `data/users.db`
- `data/signals.json`
- `data/forecast_history.json`
- `data/learning_feedback.jsonl`

Create a simple backup directory:

```bash
mkdir -p /opt/climeagent-backups
chmod 700 /opt/climeagent-backups
```

Example daily backup script:

```bash
cat > /opt/climeagent/deploy/backup.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="/opt/climeagent-backups/$STAMP"
mkdir -p "$DEST"
cp /opt/climeagent/.env "$DEST/"
cp /opt/climeagent/data/users.db "$DEST/"
cp /opt/climeagent/data/signals.json "$DEST/" 2>/dev/null || true
cp /opt/climeagent/data/forecast_history.json "$DEST/" 2>/dev/null || true
cp /opt/climeagent/data/learning_feedback.jsonl "$DEST/" 2>/dev/null || true
find /opt/climeagent-backups -maxdepth 1 -mindepth 1 -type d | sort | head -n -14 | xargs -r rm -rf
EOF
chmod 700 /opt/climeagent/deploy/backup.sh
```

Schedule it:

```bash
crontab -e
```

Add:

```text
15 2 * * * /opt/climeagent/deploy/backup.sh
```

Prefer also syncing encrypted backups to off-box storage.

## 10. Deployment Flow For Updates

On the VPS:

```bash
cd /opt/climeagent
git pull
source .venv/bin/activate
pip install -r requirements.txt
npm install
python -m pytest tests/test_brain_signal_location.py tests/test_brain_exact_markets.py tests/test_temperature_analysis.py -q
sudo systemctl restart climeagent-brain.service climeagent-bot.service climeagent-executor.service climeagent-settlement.service
```

If Python dependencies did not change, you can skip `pip install`.
If Node dependencies did not change, you can skip `npm install`.

## 11. Network Exposure Strategy

Because the bot uses Telegram polling and outbound API calls:

- do not expose a public HTTP app port
- keep inbound access limited to SSH only
- if you want an extra control panel later, place it behind Tailscale, Cloudflare Access, or an IP allowlist

For your current partial rollout:

- keep the VPS itself private except for SSH
- keep live trading U.S.-only with `BLOCKY_US_ONLY_TRADING=1`
- continue validating non-U.S. signal quality offline or in paper mode until ready

## 12. Final Production Checklist

- SSH keys only
- root login disabled
- password auth disabled
- firewall enabled
- fail2ban enabled
- unattended upgrades enabled
- dedicated `climeagent` user created
- `.env` populated and chmod `600`
- U.S.-only live trading enabled
- tests passing on the VPS
- four `systemd` services enabled
- backups scheduled
- restore procedure tested at least once
