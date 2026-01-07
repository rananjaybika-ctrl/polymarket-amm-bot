#!/bin/bash
# Health Check Script for Polymarket AMM Bot
# Runs via cron every 5 minutes

BOT_DIR="/home/ubuntu/polymarket-amm-bot"
LOG_FILE="$BOT_DIR/logs/bot.log"
ALERT_FILE="$BOT_DIR/logs/last_alert"
SERVICE_NAME="polymarket-bot"
KILL_SWITCH="$BOT_DIR/.kill_switch"

# CRITICAL: Check kill switch FIRST - if active, do NOT restart anything
if [ -f "$KILL_SWITCH" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Kill switch is ACTIVE - skipping all health checks and restarts"
    exit 0
fi

# Load env for Telegram (optional)
[ -f "$BOT_DIR/.env" ] && source "$BOT_DIR/.env"

send_alert() {
    local message="$1"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')

    echo "[$timestamp] ALERT: $message"

    # Telegram alert (if configured)
    if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
        curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
            -d chat_id="$TELEGRAM_CHAT_ID" \
            -d text="🚨 Polymarket Bot Alert\n\n$message\n\nTime: $timestamp" \
            -d parse_mode="HTML" > /dev/null
    fi
}

# Check 1: Is the service running?
if ! systemctl is-active --quiet $SERVICE_NAME; then
    send_alert "Service $SERVICE_NAME is not running! Attempting restart..."
    sudo systemctl restart $SERVICE_NAME
    sleep 5

    if systemctl is-active --quiet $SERVICE_NAME; then
        send_alert "Service restarted successfully"
    else
        send_alert "CRITICAL: Service failed to restart!"
    fi
    exit 1
fi

# Check 2: Has the log been updated in last 5 minutes?
if [ -f "$LOG_FILE" ]; then
    last_mod=$(stat -c %Y "$LOG_FILE" 2>/dev/null || stat -f %m "$LOG_FILE" 2>/dev/null)
    now=$(date +%s)
    age=$((now - last_mod))

    if [ $age -gt 300 ]; then
        send_alert "Bot appears stuck - no log activity for ${age}s"
    fi
fi

# Check 3: Check for error patterns in recent logs
if [ -f "$LOG_FILE" ]; then
    recent_errors=$(tail -100 "$LOG_FILE" | grep -c -E "(ERROR|CRITICAL|Exception|Traceback)" || true)

    if [ "$recent_errors" -gt 10 ]; then
        send_alert "High error rate detected: $recent_errors errors in last 100 lines"
    fi
fi

# Check 4: Memory usage
mem_usage=$(ps -o %mem= -p $(pgrep -f "run_paper_bot.py") 2>/dev/null | head -1 | tr -d ' ')
if [ -n "$mem_usage" ]; then
    mem_int=${mem_usage%.*}
    if [ "$mem_int" -gt 80 ]; then
        send_alert "High memory usage: ${mem_usage}%"
    fi
fi

# Check 5: Check daily P&L from CSV (optional)
TODAY=$(date +%Y-%m-%d)
CSV_FILE="$BOT_DIR/web/live_trades_calculus_maker_$TODAY.csv"

if [ -f "$CSV_FILE" ]; then
    # Get total realized P&L for today
    total_pnl=$(tail -1 "$CSV_FILE" | cut -d',' -f20)

    # Alert if losing more than $10
    if [ -n "$total_pnl" ]; then
        pnl_check=$(echo "$total_pnl < -10" | bc -l 2>/dev/null || echo "0")
        if [ "$pnl_check" = "1" ]; then
            send_alert "Daily loss limit warning: P&L = \$$total_pnl"
        fi
    fi
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Health check passed"
