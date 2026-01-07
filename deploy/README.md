# VPS Deployment Guide

## Quick Start

### 1. Provision VPS
- **Recommended**: Ubuntu 22.04 LTS
- **Specs**: 1 CPU, 1GB RAM, 20GB SSD (minimum)
- **Providers**: DigitalOcean ($6/mo), Vultr ($5/mo), Hetzner ($4/mo)

### 2. Initial Setup (on VPS)

```bash
# SSH into VPS
ssh root@your-vps-ip

# Upload setup script
scp deploy/setup-vps.sh root@your-vps-ip:/tmp/

# Run setup
chmod +x /tmp/setup-vps.sh
/tmp/setup-vps.sh
```

### 3. Configure Credentials

```bash
# On VPS, edit .env file
nano /home/ubuntu/polymarket-amm-bot/.env
```

Required variables:
```
POLYMARKET_API_KEY=xxx
POLYMARKET_API_SECRET=xxx
POLYMARKET_PASSPHRASE=xxx
PRIVATE_KEY=xxx
```

Optional (for Telegram alerts):
```
TELEGRAM_BOT_TOKEN=xxx
TELEGRAM_CHAT_ID=xxx
```

### 4. Deploy Code

From your local machine:
```bash
cd /Users/rananjaybika/polymarket-amm-bot
chmod +x deploy/deploy.sh
./deploy/deploy.sh your-vps-ip ubuntu
```

### 5. Start Bot

```bash
# On VPS
sudo systemctl start polymarket-bot
sudo systemctl status polymarket-bot
```

## Commands

| Command | Description |
|---------|-------------|
| `sudo systemctl start polymarket-bot` | Start bot |
| `sudo systemctl stop polymarket-bot` | Stop bot |
| `sudo systemctl restart polymarket-bot` | Restart bot |
| `sudo systemctl status polymarket-bot` | Check status |
| `tail -f ~/polymarket-amm-bot/logs/bot.log` | View live logs |
| `journalctl -u polymarket-bot -f` | View systemd logs |

## Monitoring

### Health Checks
- Runs every 5 minutes via cron
- Auto-restarts bot if crashed
- Sends Telegram alerts (if configured)

### Manual Check
```bash
/home/ubuntu/polymarket-amm-bot/deploy/health-check.sh
```

### View Today's Trades
```bash
cat ~/polymarket-amm-bot/web/live_trades_calculus_maker_$(date +%Y-%m-%d).csv
```

## Telegram Alerts Setup

1. Create bot via [@BotFather](https://t.me/botfather)
2. Get your chat ID via [@userinfobot](https://t.me/userinfobot)
3. Add to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-xyz
   TELEGRAM_CHAT_ID=123456789
   ```

## Troubleshooting

### Bot won't start
```bash
# Check logs
journalctl -u polymarket-bot -n 50

# Check Python errors
tail -100 ~/polymarket-amm-bot/logs/bot.log
```

### High memory usage
```bash
# Check memory
ps aux | grep run_paper_bot

# Restart if needed
sudo systemctl restart polymarket-bot
```

### Connection issues
```bash
# Test API connectivity
curl -I https://clob.polymarket.com/health
```

## Files

```
deploy/
├── polymarket-bot.service  # systemd service definition
├── setup-vps.sh           # Initial VPS setup script
├── deploy.sh              # Push code from local to VPS
├── health-check.sh        # Health monitoring script
└── README.md              # This file
```
