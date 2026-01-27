#!/bin/bash
# Auto-restart wrapper for Observer
# Usage: ./scripts/run_observer_forever.sh [observer args]
#
# STAYS STOPPED on:
#   - Ctrl+C (manual stop)
#   - SIGTERM (Claude/system stop)
#   - Clean exit (reached --hours limit)
#
# RESTARTS on:
#   - Crash (non-zero exit)
#   - WebSocket disconnect
#   - Unexpected termination

cd "$(dirname "$0")/.."
PROJECT_DIR=$(pwd)
LOG_FILE="$PROJECT_DIR/logs/observer_$(date +%Y%m%d).log"
PID_FILE="$PROJECT_DIR/logs/observer.pid"
STOP_FILE="$PROJECT_DIR/logs/observer.stop"
mkdir -p "$PROJECT_DIR/logs"

# ============================================
# SAFETY LIMITS
# ============================================
MAX_RESTARTS_PER_HOUR=10
MAX_RAPID_RESTARTS=5
RAPID_RESTART_THRESHOLD=60
COOLDOWN_MINUTES=10

# ============================================
# SINGLE INSTANCE CHECK
# ============================================
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
        echo "ERROR: Observer is already running (PID: $OLD_PID)"
        echo "To stop it: kill $OLD_PID"
        exit 1
    else
        echo "Removing stale PID file..."
        rm -f "$PID_FILE"
    fi
fi

# Remove any leftover stop file from previous run
rm -f "$STOP_FILE"

# Write our PID
echo $$ > "$PID_FILE"

echo "========================================"
echo "Observer - Auto-Restart Wrapper"
echo "========================================"
echo "Log file: $LOG_FILE"
echo "PID file: $PID_FILE (PID: $$)"
echo "Safety: Max $MAX_RESTARTS_PER_HOUR restarts/hour"
echo ""
echo "STAYS STOPPED on: Ctrl+C, SIGTERM, clean exit"
echo "RESTARTS on: crash, disconnect"
echo ""
echo "Observer args: $@"
echo "========================================"

# Prevent Mac from sleeping
caffeinate -d -i -s &
CAFF_PID=$!
echo "[$(date)] Mac sleep prevention enabled (PID: $CAFF_PID)"

# Track if we received a stop signal
STOP_REQUESTED=false

# Cleanup on exit
cleanup() {
    echo ""
    echo "[$(date)] Manual stop requested - staying stopped" | tee -a "$LOG_FILE"
    STOP_REQUESTED=true
    # Create stop file to signal we want to stay stopped
    touch "$STOP_FILE"
    # Kill observer if running
    if [ -n "$OBSERVER_PID" ] && ps -p "$OBSERVER_PID" > /dev/null 2>&1; then
        kill $OBSERVER_PID 2>/dev/null
        wait $OBSERVER_PID 2>/dev/null
    fi
    kill $CAFF_PID 2>/dev/null
    rm -f "$PID_FILE"
    echo "[$(date)] Shutdown complete" | tee -a "$LOG_FILE"
    exit 0
}
trap cleanup SIGINT SIGTERM

# Activate virtualenv
source "$PROJECT_DIR/venv/bin/activate"

BACKOFF=5
RESTART_COUNT=0
RAPID_RESTART_COUNT=0
HOUR_START=$(date +%s)
HOUR_RESTART_COUNT=0

while true; do
    # Check if stop was requested
    if [ "$STOP_REQUESTED" = true ] || [ -f "$STOP_FILE" ]; then
        echo "[$(date)] Stop file detected - staying stopped" | tee -a "$LOG_FILE"
        break
    fi

    RESTART_COUNT=$((RESTART_COUNT + 1))
    START_TIME=$(date +%s)

    # ============================================
    # SAFETY CHECK: Hourly restart limit
    # ============================================
    CURRENT_TIME=$(date +%s)
    HOUR_ELAPSED=$((CURRENT_TIME - HOUR_START))

    if [ $HOUR_ELAPSED -ge 3600 ]; then
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
        echo "[$(date)] Observer crashed $RAPID_RESTART_COUNT times in <${RAPID_RESTART_THRESHOLD}s each" | tee -a "$LOG_FILE"
        echo "[$(date)] Cooling down for $COOLDOWN_MINUTES minutes..." | tee -a "$LOG_FILE"
        sleep $((COOLDOWN_MINUTES * 60))
        RAPID_RESTART_COUNT=0
        continue
    fi

    echo ""
    echo "[$(date)] Starting observer (restart #$RESTART_COUNT, hourly: $HOUR_RESTART_COUNT/$MAX_RESTARTS_PER_HOUR)..." | tee -a "$LOG_FILE"

    # Run observer with all passed arguments
    python "$PROJECT_DIR/scripts/observer.py" "$@" 2>&1 | tee -a "$LOG_FILE" &
    OBSERVER_PID=$!

    # Wait for observer to exit
    wait $OBSERVER_PID
    EXIT_CODE=$?
    END_TIME=$(date +%s)
    RUNTIME=$((END_TIME - START_TIME))

    echo "[$(date)] Observer exited (code=$EXIT_CODE, runtime=${RUNTIME}s)" | tee -a "$LOG_FILE"

    # ============================================
    # KEY: Check if we should stay stopped
    # ============================================

    # Stop file means manual/Claude stop
    if [ -f "$STOP_FILE" ]; then
        echo "[$(date)] Stop file detected - staying stopped" | tee -a "$LOG_FILE"
        break
    fi

    # Exit code 0 = clean completion (reached --hours limit)
    if [ $EXIT_CODE -eq 0 ]; then
        echo "[$(date)] Clean exit (reached time limit) - staying stopped" | tee -a "$LOG_FILE"
        break
    fi

    # Exit code 130 = SIGINT (Ctrl+C passed through)
    if [ $EXIT_CODE -eq 130 ]; then
        echo "[$(date)] SIGINT received - staying stopped" | tee -a "$LOG_FILE"
        break
    fi

    # Exit code 143 = SIGTERM
    if [ $EXIT_CODE -eq 143 ]; then
        echo "[$(date)] SIGTERM received - staying stopped" | tee -a "$LOG_FILE"
        break
    fi

    # ============================================
    # CRASH: Restart with backoff
    # ============================================
    echo "[$(date)] Crash detected (exit code $EXIT_CODE) - will restart" | tee -a "$LOG_FILE"

    # Track rapid restarts
    if [ $RUNTIME -lt $RAPID_RESTART_THRESHOLD ]; then
        RAPID_RESTART_COUNT=$((RAPID_RESTART_COUNT + 1))
        echo "[$(date)] Rapid restart detected ($RAPID_RESTART_COUNT/$MAX_RAPID_RESTARTS)" | tee -a "$LOG_FILE"
    else
        RAPID_RESTART_COUNT=0
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

# Cleanup
kill $CAFF_PID 2>/dev/null
rm -f "$PID_FILE"
rm -f "$STOP_FILE"
echo "[$(date)] Observer wrapper exited" | tee -a "$LOG_FILE"
