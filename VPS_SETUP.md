# Blocky Polymarket VPS Setup Guide

Deploy the bot 24/7 on a Linux VPS such as Ubuntu 22.04 or 24.04.

## 1. Recommended Server Specs

Current single-box setup:
- OS: Ubuntu 22.04 LTS or 24.04 LTS
- CPU: 4 vCPU minimum
- RAM: 8 GB minimum
- Disk: 80 GB+ SSD/NVMe

Safer production setup:
- CPU: 8 vCPU
- RAM: 16 GB
- Disk: 200 GB+ NVMe

Notes:
- The app runs multiple long-lived processes concurrently: bot, brain, executor, and settlement.
- SQLite can run on the same VPS without issue at the current stage.
- For larger scale, especially hundreds to 1,000 users, move from SQLite to PostgreSQL.

## 2. What Runs on the VPS

One VPS can run all of these together:
- Telegram bot
- Python signal/brain process
- Trade executor
- Settlement monitor
- Local SQL database

This is fine for now. Reliability becomes the bigger concern before raw CPU does.

## 3. Environment Setup

SSH into the VPS and install the core dependencies:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl git build-essential python3 python3-pip python3-venv sqlite3

# Install Node.js 20
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs

# Install PM2
sudo npm install -g pm2
```

## 4. Bot Deployment

```bash
git clone <your-repo-link>
cd "Blocky Polymarket"

npm install
pip3 install -r requirements.txt
```

## 5. Configuration

Create your `.env` file:

```bash
nano .env
```

Add the required secrets such as:
- `TELEGRAM_BOT_TOKEN`
- Polymarket API credentials
- wallet-related environment values you use in production

Then lock it down:

```bash
chmod 600 .env
```

## 6. Running 24/7 with PM2

Your current project already starts all services concurrently through `npm start`, so the VPS guide should follow that by default.

Recommended:

```bash
pm2 start npm --name "blocky" -- start
pm2 save
pm2 startup
```

This uses the existing script in `package.json`, which launches:
- Python brain
- Telegram bot
- trade executor
- settlement monitor

Optional fallback:

If you ever want stricter isolation, you can still run them as separate PM2 processes later, but the default setup here matches your current concurrent runtime.

## 7. Monitoring and Logs

Useful commands:

```bash
pm2 status
pm2 logs
pm2 restart all
pm2 stop all
pm2 monit
```

Watch these in particular:
- signal scan duration
- API/network failures
- settlement loop stability
- disk growth from logs and database files

## 8. Database Guidance

Short term:
- SQLite on the same VPS is okay.
- Back up `data/users.db` regularly.

Before major scale:
- Move to PostgreSQL.
- SQLite is file-based and can become a bottleneck with heavier concurrent writes.
- PostgreSQL is the better choice for reliability, recovery, and multi-process workloads.

Practical rule:
- Okay now: SQLite on one VPS
- Before heavy scale: PostgreSQL
- After growth: separate app workers and database

## 9. Security and Reliability

- Keep only SSH open unless you intentionally expose another service.
- Use an SSH key, not password login.
- Enable a firewall such as `ufw`.
- Set up backups for:
  - `.env`
  - `data/users.db`
  - any future PostgreSQL database
- Consider PM2 log rotation if logs grow quickly.

Example firewall:

```bash
sudo ufw allow OpenSSH
sudo ufw enable
sudo ufw status
```

## 10. Recommendation Summary

If reliability and efficiency come first:
- Start on Linux, not Windows RDP.
- Use a VPS, not a desktop-style server.
- Run all current services on one VPS for now.
- Use 4 vCPU / 8 GB RAM minimum.
- Prefer 8 vCPU / 16 GB RAM if you want headroom.
- Plan to migrate from SQLite to PostgreSQL before serious growth.
