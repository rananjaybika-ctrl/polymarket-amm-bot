#!/bin/bash
# Auto-restart wrapper for PolyBot web server
# Usage: ./scripts/run_server_forever.sh
#
# Features:
# 1. Prevents Mac from sleeping (caffeinate)
# 2. Auto-restarts on crash with backoff
# 3. Logs to file for debugging
# 4. SAFETY: Max restart limits to prevent fork bombs

cd "$(dirname "$0")/.."
PROJECT_DIR=$(pwd)
LOG_FILE="$PROJECT_DIR/logs/server_$(date +%Y%m%d).log"
PID_FILE="$PROJECT_DIR/logs/server.pid"
mkdir -p "$PROJECT_DIR/logs"

# ============================================
# SAFETY LIMITS - Prevent runaway processes
# ============================================
MAX_RESTARTS_PER_HOUR=10      # Max restarts in 1 hour window
MAX_RAPID_RESTARTS=5          # Max restarts if each runs <60s
RAPID_RESTART_THRESHOLD=60    # Seconds - if server runs less, it's "rapid"
COOLDOWN_MINUTES=10           # Cooldown if limits hit

# ============================================
# SINGLE INSTANCE CHECK
# ============================================
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
        echo "ERROR: PolyBot is already running (PID: $OLD_PID)"
        echo "To stop it: kill $OLD_PID"
        exit 1
    else
        echo "Removing stale PID file..."
        rm -f "$PID_FILE"
    fi
fi

# Write our PID
echo $$ > "$PID_FILE"

echo "========================================"
echo "PolyBot Server - Auto-Restart Wrapper"
echo "========================================"
echo "Log file: $LOG_FILE"
echo "PID file: $PID_FILE (PID: $$)"
echo "Safety: Max $MAX_RESTARTS_PER_HOUR restarts/hour"
echo "Press Ctrl+C to stop"
echo "========================================"

# Prevent Mac from sleeping
caffeinate -d -i -s &
CAFF_PID=$!
echo "[$(date)] Mac sleep prevention enabled (PID: $CAFF_PID)"

# Cleanup on exit
cleanup() {
    echo "[$(date)] Shutting down..."
    kill $CAFF_PID 2>/dev/null
    rm -f "$PID_FILE"
    # Kill any child uvicorn processes
    pkill -P $$ 2>/dev/null
    exit 0
}
trap cleanup SIGINT SIGTERM EXIT

# Activate virtualenv
source "$PROJECT_DIR/venv/bin/activate"

BACKOFF=5
RESTART_COUNT=0
RAPID_RESTART_COUNT=0
HOUR_START=$(date +%s)
HOUR_RESTART_COUNT=0

while true; do
    RESTART_COUNT=$((RESTART_COUNT + 1))
    START_TIME=$(date +%s)

    # ============================================
    # SAFETY CHECK: Hourly restart limit
    # ============================================
    CURRENT_TIME=$(date +%s)
    HOUR_ELAPSED=$((CURRENT_TIME - HOUR_START))

    if [ $HOUR_ELAPSED -ge 3600 ]; then
        # Reset hourly counter
        HOUR_START=$CURRENT_TIME
        HOUR_RESTART_COUNT=0
        RAPID_RESTART_COUNT=0
        echo "[$(date)] Hourly counters reset" | tee -a "$LOG_FILE"
    fi

    HOUR_RESTART_COUNT=$((HOUR_RESTART_COUNT + 1))

    if [ $HOUR_RESTART_COUNT -gt $MAX_RESTARTS_PER_HOUR ]; then
        echo "" | tee -a "$LOG_FILE"
        echo "!!! SAFETY LIMIT HIT !!!" | tee -a "$LOG_FILE"
        echo "[$(date)] Too many restarts ($HOUR_RESTART_COUNT in 1 hour)" | tee -a "$LOG_FILE"
        echo "[$(date)] Cooling down for $COOLDOWN_MINUTES minutes..." | tee -a "$LOG_FILE"
        echo "Check logs for errors: $LOG_FILE" | tee -a "$LOG_FILE"
        sleep $((COOLDOWN_MINUTES * 60))
        HOUR_START=$(date +%s)
        HOUR_RESTART_COUNT=0
        RAPID_RESTART_COUNT=0
        continue
    fi

    # ============================================
    # SAFETY CHECK: Rapid restart limit
    # ============================================
    if [ $RAPID_RESTART_COUNT -ge $MAX_RAPID_RESTARTS ]; then
        echo "" | tee -a "$LOG_FILE"
        echo "!!! RAPID RESTART LIMIT HIT !!!" | tee -a "$LOG_FILE"
        echo "[$(date)] Server crashed $RAPID_RESTART_COUNT times in <${RAPID_RESTART_THRESHOLD}s each" | tee -a "$LOG_FILE"
        echo "[$(date)] Something is seriously wrong. Cooling down for $COOLDOWN_MINUTES minutes..." | tee -a "$LOG_FILE"
        sleep $((COOLDOWN_MINUTES * 60))
        RAPID_RESTART_COUNT=0
        continue
    fi

    echo ""
    echo "[$(date)] Starting server (restart #$RESTART_COUNT, hourly: $HOUR_RESTART_COUNT/$MAX_RESTARTS_PER_HOUR)..." | tee -a "$LOG_FILE"

    cd "$PROJECT_DIR/web"
    uvicorn server:app --host 0.0.0.0 --port 8000 2>&1 | tee -a "$LOG_FILE" &
    SERVER_PID=$!

    # Wait for server to exit
    wait $SERVER_PID
    EXIT_CODE=$?
    END_TIME=$(date +%s)
    RUNTIME=$((END_TIME - START_TIME))

    echo "[$(date)] Server exited (code=$EXIT_CODE, runtime=${RUNTIME}s)" | tee -a "$LOG_FILE"

    # Track rapid restarts
    if [ $RUNTIME -lt $RAPID_RESTART_THRESHOLD ]; then
        RAPID_RESTART_COUNT=$((RAPID_RESTART_COUNT + 1))
        echo "[$(date)] Rapid restart detected ($RAPID_RESTART_COUNT/$MAX_RAPID_RESTARTS)" | tee -a "$LOG_FILE"
    else
        RAPID_RESTART_COUNT=0  # Reset if server ran long enough
    fi

    # Calculate backoff
    if [ $RUNTIME -gt 300 ]; then
        BACKOFF=5
    else
        BACKOFF=$((BACKOFF * 2))
        [ $BACKOFF -gt 120 ] && BACKOFF=120
    fi

    echo "[$(date)] Restarting in ${BACKOFF}s..." | tee -a "$LOG_FILE"
    sleep $BACKOFF
done
