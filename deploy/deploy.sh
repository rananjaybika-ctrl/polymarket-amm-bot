#!/bin/bash
# Deploy script - run from local machine to push code to VPS
# Usage: ./deploy.sh [vps-ip] [user]

set -e

VPS_HOST="${1:-your-vps-ip}"
VPS_USER="${2:-ubuntu}"
REMOTE_DIR="/home/$VPS_USER/polymarket-amm-bot"
LOCAL_DIR="$(dirname "$0")/.."

if [ "$VPS_HOST" = "your-vps-ip" ]; then
    echo "Usage: ./deploy.sh <vps-ip> [user]"
    echo "Example: ./deploy.sh 123.45.67.89 ubuntu"
    exit 1
fi

echo "=== Deploying to $VPS_USER@$VPS_HOST ==="

# Files/folders to exclude from sync
EXCLUDES=(
    ".git"
    ".env"
    "venv"
    "__pycache__"
    "*.pyc"
    ".DS_Store"
    "logs/*.log"
    "web/*trades*.csv"
    "node_modules"
)

# Build exclude args
EXCLUDE_ARGS=""
for exc in "${EXCLUDES[@]}"; do
    EXCLUDE_ARGS="$EXCLUDE_ARGS --exclude=$exc"
done

# Sync code
echo "[1/4] Syncing code..."
rsync -avz --progress $EXCLUDE_ARGS \
    "$LOCAL_DIR/" \
    "$VPS_USER@$VPS_HOST:$REMOTE_DIR/"

# Install dependencies if requirements changed
echo "[2/4] Checking dependencies..."
ssh "$VPS_USER@$VPS_HOST" << 'EOF'
cd ~/polymarket-amm-bot
source venv/bin/activate
pip install -q -r requirements.txt
EOF

# Restart service
echo "[3/4] Restarting service..."
ssh "$VPS_USER@$VPS_HOST" "sudo systemctl restart polymarket-bot"

# Check status
echo "[4/4] Checking status..."
sleep 3
ssh "$VPS_USER@$VPS_HOST" "sudo systemctl status polymarket-bot --no-pager | head -20"

echo ""
echo "=== Deployment Complete ==="
echo "View logs: ssh $VPS_USER@$VPS_HOST 'tail -f ~/polymarket-amm-bot/logs/bot.log'"
