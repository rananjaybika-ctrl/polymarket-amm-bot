#!/bin/bash
# VPS Setup Script for Polymarket AMM Bot
# Run as root or with sudo

set -e

echo "=== Polymarket AMM Bot VPS Setup ==="

# Update system
echo "[1/8] Updating system packages..."
apt update && apt upgrade -y

# Install dependencies
echo "[2/8] Installing dependencies..."
apt install -y python3.11 python3.11-venv python3-pip git curl jq

# Create bot user (if not exists)
echo "[3/8] Setting up bot user..."
if ! id "ubuntu" &>/dev/null; then
    useradd -m -s /bin/bash ubuntu
fi

# Create directories
echo "[4/8] Creating directories..."
mkdir -p /home/ubuntu/polymarket-amm-bot/logs
mkdir -p /home/ubuntu/polymarket-amm-bot/web
chown -R ubuntu:ubuntu /home/ubuntu/polymarket-amm-bot

# Clone or update repo (run this part as ubuntu user)
echo "[5/8] Setting up application..."
cat << 'SETUP_SCRIPT' > /tmp/setup_app.sh
#!/bin/bash
cd /home/ubuntu/polymarket-amm-bot

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install requirements
pip install --upgrade pip
pip install -r requirements.txt

# Create .env file template if not exists
if [ ! -f .env ]; then
    cat << 'EOF' > .env
# Polymarket API Keys
POLYMARKET_API_KEY=your_api_key_here
POLYMARKET_API_SECRET=your_api_secret_here
POLYMARKET_PASSPHRASE=your_passphrase_here

# Wallet
PRIVATE_KEY=your_private_key_here

# Optional: Telegram alerts
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
EOF
    echo "Created .env template - EDIT THIS FILE with your credentials!"
fi
SETUP_SCRIPT

chmod +x /tmp/setup_app.sh
su - ubuntu -c "/tmp/setup_app.sh"

# Install systemd service
echo "[6/8] Installing systemd service..."
cp /home/ubuntu/polymarket-amm-bot/deploy/polymarket-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable polymarket-bot

# Setup log rotation
echo "[7/8] Setting up log rotation..."
cat << 'EOF' > /etc/logrotate.d/polymarket-bot
/home/ubuntu/polymarket-amm-bot/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 ubuntu ubuntu
    postrotate
        systemctl reload polymarket-bot > /dev/null 2>&1 || true
    endscript
}
EOF

# Setup health check cron
echo "[8/8] Setting up health monitoring..."
cat << 'EOF' > /etc/cron.d/polymarket-health
# Check bot health every 5 minutes
*/5 * * * * ubuntu /home/ubuntu/polymarket-amm-bot/deploy/health-check.sh >> /home/ubuntu/polymarket-amm-bot/logs/health.log 2>&1
EOF

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "1. Edit /home/ubuntu/polymarket-amm-bot/.env with your API keys"
echo "2. Copy your code: scp -r ./* ubuntu@your-vps:/home/ubuntu/polymarket-amm-bot/"
echo "3. Start the bot: sudo systemctl start polymarket-bot"
echo "4. Check status: sudo systemctl status polymarket-bot"
echo "5. View logs: tail -f /home/ubuntu/polymarket-amm-bot/logs/bot.log"
