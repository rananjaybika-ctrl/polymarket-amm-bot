#!/bin/bash
# One-command deploy to AWS Ireland
# Usage: ./deploy.sh "commit message"
#
# NOTE: AWS uses python3 (not python). Always use python3 in SSH commands:
#   ssh ... 'python3 scripts/observer.py --hours 15'

set -e

AWS_IP="54.170.244.221"
AWS_KEY="$HOME/Downloads/polymarket-key.pem"
LOCAL_PATH="/Users/rananjaybika/polymarket-amm-bot/"
REMOTE_PATH="ubuntu@$AWS_IP:~/polymarket-amm-bot/"

echo "=== Polymarket Bot Deploy ==="
echo ""

# 1. Git commit & push (if message provided)
if [ -n "$1" ]; then
    echo "[1/4] Committing: $1"
    git add .
    git commit -m "$1" || echo "Nothing to commit"
    git push origin main
else
    echo "[1/4] Skipping git (no message provided)"
fi

# 2. Sync to AWS
echo ""
echo "[2/4] Syncing to AWS Ireland..."
rsync -avz --delete \
    --exclude 'venv' \
    --exclude '__pycache__' \
    --exclude '.git' \
    --exclude '.env' \
    --exclude '*.pyc' \
    --exclude 'logs/*.log' \
    --exclude 'state/*.json' \
    --exclude 'research/observer/grid_obs_*.csv' \
    --exclude 'research/observer/btc_prices_*.csv' \
    --exclude 'research/observer/*.csv' \
    --exclude 'research/binance_hf/*.csv' \
    -e "ssh -i $AWS_KEY" \
    "$LOCAL_PATH" "$REMOTE_PATH"

# 3. NO AUTO-RESTART (safety: don't start trading on deploy)
echo ""
echo "[3/3] Files synced successfully (bot NOT restarted)"
echo ""
echo "=== Deploy Complete ==="
echo "Dashboard: http://$AWS_IP:8000"
echo ""
echo "To restart bot manually:"
echo "  ssh -i $AWS_KEY ubuntu@$AWS_IP 'sudo systemctl restart polymarket-bot'"
echo ""
echo "To check status:"
echo "  ssh -i $AWS_KEY ubuntu@$AWS_IP 'sudo systemctl status polymarket-bot'"
echo ""
echo "To start observer (data collection):"
echo "  ssh -i $AWS_KEY ubuntu@$AWS_IP 'cd ~/polymarket-amm-bot && nohup python3 scripts/observer.py --hours 15 > logs/observer.log 2>&1 &'"
echo ""
echo "To check observer:"
echo "  ssh -i $AWS_KEY ubuntu@$AWS_IP 'ps aux | grep observer; tail -20 ~/polymarket-amm-bot/logs/observer.log'"
