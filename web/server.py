"""
FastAPI Web Server for Polymarket Trading Bot
Run with: uvicorn web.server:app --reload --port 8000

Features:
- Health monitoring for all strategies
- Auto-restart on crash
- Health check endpoints
- Resilient to bot failures
"""

import sys
import os
from pathlib import Path

# Load .env file BEFORE anything else reads os.environ
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import asyncio
import logging
import traceback
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from zoneinfo import ZoneInfo

# Kill switch file - when this exists, bot refuses to start
# IMPORTANT: Use project directory, NOT /tmp (which is cleared on reboot)
KILL_SWITCH_FILE = Path(__file__).parent.parent / ".kill_switch"

# =============================================================================
# AUTO DATA COLLECTION (Feb 5, 2026)
# =============================================================================
# When enabled, paper trading automatically starts observer + 60Hz Binance logger
# for the same duration. Both stop together when trading stops.
# Toggle this flag to enable/disable (not visible on frontend)
ENABLE_AUTO_DATA_COLLECTION = True  # Set to False to disable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets
import hashlib


def normalize_datetime_to_utc(dt_str: str, assume_tz: str = "Asia/Kolkata") -> datetime:
    """
    Convert datetime string to UTC timezone-aware datetime.

    The web UI sends local time (IST) from the browser. This function:
    1. Parses the datetime string
    2. If no timezone info, assumes the specified timezone (default IST)
    3. Converts to UTC for consistent internal handling

    Args:
        dt_str: ISO format datetime string from frontend
        assume_tz: Timezone to assume if dt_str has no tzinfo (default: Asia/Kolkata)

    Returns:
        UTC timezone-aware datetime
    """
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        # Frontend sends local time without timezone - assume IST
        dt = dt.replace(tzinfo=ZoneInfo(assume_tz))
    return dt.astimezone(timezone.utc)


from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.services.health_monitor import (
    HealthMonitor,
    get_health_monitor,
    set_health_monitor,
    HealthStatus,
)
from src.services.auto_redeemer import AutoRedeemer
from src.config import Config

# TRADING_CONFIGS.py is the SINGLE SOURCE OF TRUTH for all trading parameters
# PHOENIX V3 (Feb 18, 2026) replaces AGGRESSIVE as the active config
from research.reference.TRADING_CONFIGS import PHOENIX as AGGRESSIVE_CONFIG

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# =============================================================================
# KILL SWITCH - Prevents bot from restarting after manual stop
# =============================================================================

def activate_kill_switch(reason: str = "manual") -> bool:
    """
    Activate the kill switch to prevent bot from running.

    Creates a kill file AND attempts to disable systemd service.
    This ensures the bot won't restart even if:
    - The process crashes
    - systemd tries to restart it
    - The server restarts

    Args:
        reason: Why the kill switch was activated (logged to file)

    Returns:
        True if kill switch was activated successfully
    """
    try:
        # Write kill file with timestamp and reason
        KILL_SWITCH_FILE.write_text(
            f"Killed at {datetime.now(timezone.utc).isoformat()}\n"
            f"Reason: {reason}\n"
        )
        logger.critical(f"[KILL SWITCH] Activated: {reason}")

        # NOTE: We NO LONGER stop systemd service here!
        # The web server (uvicorn) runs in the same service as the trading tasks.
        # Stopping systemd would kill the frontend, which is NOT what we want.
        # The kill file alone is sufficient to prevent auto-restart of trading.
        #
        # If you need to fully stop everything including web server, use:
        #   sudo systemctl stop polymarket-bot
        # from the command line.
        logger.info("[KILL SWITCH] Kill file created - trading will not auto-restart")

        return True
    except Exception as e:
        logger.error(f"[KILL SWITCH] Failed to activate: {e}")
        return False


def clear_kill_switch() -> bool:
    """
    Clear the kill switch to allow bot to run again.

    Returns:
        True if kill switch was cleared (or didn't exist)
    """
    try:
        if KILL_SWITCH_FILE.exists():
            KILL_SWITCH_FILE.unlink()
            logger.info("[KILL SWITCH] Cleared - bot can start again")
        return True
    except Exception as e:
        logger.error(f"[KILL SWITCH] Failed to clear: {e}")
        return False


def is_kill_switch_active() -> bool:
    """Check if kill switch is currently active."""
    return KILL_SWITCH_FILE.exists()


# =============================================================================
# HTTP BASIC AUTHENTICATION - Protect all endpoints
# =============================================================================
# Set these environment variables on your server:
#   POLYBOT_USERNAME=your_username
#   POLYBOT_PASSWORD=your_secure_password
# =============================================================================

security = HTTPBasic()

# Get credentials from environment (with secure defaults that MUST be changed)
AUTH_USERNAME = os.environ.get("POLYBOT_USERNAME", "admin")
AUTH_PASSWORD = os.environ.get("POLYBOT_PASSWORD", "CHANGE_ME_IMMEDIATELY")

def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    """Verify HTTP Basic Auth credentials."""
    # Debug: log what we're comparing (remove after debugging)
    logger.info(f"[AUTH DEBUG] Expected user: '{AUTH_USERNAME}', got: '{credentials.username}'")
    logger.debug(f"[AUTH DEBUG] Expected pass length: {len(AUTH_PASSWORD)}, got: {len(credentials.password)}")

    # Use constant-time comparison to prevent timing attacks
    username_correct = secrets.compare_digest(
        credentials.username.encode("utf8"),
        AUTH_USERNAME.encode("utf8")
    )
    password_correct = secrets.compare_digest(
        credentials.password.encode("utf8"),
        AUTH_PASSWORD.encode("utf8")
    )

    if not (username_correct and password_correct):
        logger.warning(f"[AUTH] Failed login attempt for user: {credentials.username} (pass_ok={password_correct})")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


app = FastAPI(title="Polymarket Trading Bot", version="1.0.0")

# Serve static files
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# =============================================================================
# LEGACY CONFIG CLASSES - Deprecated, use AggressiveBotConfig/ContrarianBotConfig
# Kept for backward compatibility with existing web UI integrations
# =============================================================================


class AccumulationBotConfig(BaseModel):
    """
    DEPRECATED: Configuration for Accumulation strategy from web UI.

    This config class is deprecated. Use AggressiveBotConfig for Path 1 (spike detection)
    or ContrarianBotConfig for Path 2 (reversal detection) instead.

    Kept for backward compatibility only.
    """
    mode: str  # "paper" or "live"
    market: str  # "btc-15m"
    start_datetime: str  # ISO format (local time from browser)
    end_datetime: str
    starting_balance: float = 100.0
    max_share_price: float = 0.98  # Never buy above this price (Gabagool buys up to $0.98)

    # Accumulation mode parameters
    accum_mode: str = "standard"
    accum_trade_size: int = 1
    accum_target_shares: int = 15
    accum_max_imbalance_pct: float = 0.20  # Max imbalance as % of target (20% = 6 shares)
    hard_max_imbalance: int = AGGRESSIVE_CONFIG.hard_max_imbalance  # From TRADING_CONFIGS
    accum_pair_cost_target: float = 0.995  # Target for normal trading (buy cheap)
    accum_pair_cost_limit: float = 1.02    # Max for rebalancing only
    accum_buy_both_sides: bool = True


class AggressiveBotConfig(BaseModel):
    """Configuration for PHOENIX (formerly AGGRESSIVE) strategy from web UI.

    PHOENIX V3: Maker-prediction with FADE mode, hold to resolution.
    Uses EnhancedSpikeStrategy with entry_offset=0.02, 25 shares/entry.

    ALL DEFAULTS sourced from TRADING_CONFIGS.py (single source of truth).
    """
    mode: str  # "paper" or "live"
    market: str = "btc-15m"
    start_datetime: str  # ISO format (local time from browser)
    end_datetime: str
    starting_balance: float = 170.0

    # Parameters - ALL FROM TRADING_CONFIGS.py (PHOENIX config)
    threshold_method: str = AGGRESSIVE_CONFIG.threshold_method
    zscore_method: str = AGGRESSIVE_CONFIG.zscore_method
    lookback_ms: int = AGGRESSIVE_CONFIG.lookback_ms
    time_stop_seconds: Optional[float] = AGGRESSIVE_CONFIG.time_stop_seconds
    use_cycling: bool = AGGRESSIVE_CONFIG.use_cycling
    z_lo: Optional[float] = AGGRESSIVE_CONFIG.z_lo
    z_hi: Optional[float] = AGGRESSIVE_CONFIG.z_hi
    base_size: int = AGGRESSIVE_CONFIG.shares_per_cycle
    high_entry_threshold: float = AGGRESSIVE_CONFIG.high_entry_threshold
    max_daily_loss: Optional[float] = AGGRESSIVE_CONFIG.max_session_loss
    max_entries_per_market: int = getattr(AGGRESSIVE_CONFIG, 'max_entries_per_market', 0) or 0
    fade_mode: bool = getattr(AGGRESSIVE_CONFIG, 'fade_mode', False)


class ContrarianBotConfig(BaseModel):
    """Configuration for CONTRARIAN (Path 2) strategy from web UI.

    Bet against BTC direction at 15-min scale when reversal detected.
    Uses window-based reversal detection with adaptive vol gate.
    """
    mode: str  # "paper" or "live"
    market: str = "btc-15m"
    start_datetime: str  # ISO format (local time from browser)
    end_datetime: str
    starting_balance: float = 170.0

    # Path 2 parameters
    pullback_threshold: float = 0.0001    # 0.01% pullback from peak to trigger
    retracement_min: float = 0.30         # Must retrace 30% of move
    entry_price_min: float = 0.20         # Entry price floor ($0.20)
    min_delay_seconds: int = 60           # Wait 60s from window start
    z_threshold: float = 0.5              # Minimum z-score for entry
    shares_per_trade: int = 50            # Shares per trade


class VolumeWeightedBotConfig(BaseModel):
    """Configuration for Volume Weighted (VW/Gabagool-style) strategy from web UI.

    Grid maker with aggressive cheap accumulation and conservative hedging.
    """
    mode: str  # "paper" or "live"
    market: str = "btc-15m"
    start_datetime: str  # ISO format (local time from browser)
    end_datetime: str
    starting_balance: float = 170.0

    # VW-specific parameters (Gabagool-style)
    vw_imbalance_pct: float = 0.20        # Max 20% imbalance tolerance
    vw_cheap_threshold: float = 0.45      # Buy aggressively below this price
    vw_hedge_trigger_pct: float = 0.15    # Start hedging at 15% imbalance
    vw_max_hedge_price: float = 0.85      # Max price for hedge buys

    # Shared parameters
    target_shares: int = 50               # Target shares per side
    max_daily_loss: float = 0.0           # Stop trading if loss exceeds (0=disabled)


# Legacy config for backward compatibility - DEPRECATED
class BotConfig(BaseModel):
    """
    DEPRECATED: Legacy configuration from web UI.

    This config class is deprecated. Use AggressiveBotConfig for Path 1 (spike detection)
    or ContrarianBotConfig for Path 2 (reversal detection) instead.

    The /api/start and /api/validate endpoints using this config are deprecated.
    Use /api/start/aggressive or /api/start/contrarian instead.
    """
    mode: str
    market: str
    trading_mode: str = "accumulation"
    start_datetime: str
    end_datetime: str
    starting_balance: float = 100.0
    max_share_price: float = 0.98

    # Accumulation mode parameters
    accum_trade_size: int = 1
    accum_target_shares: int = 15
    accum_max_imbalance_pct: float = 0.20  # 20% of target (6 shares)
    hard_max_imbalance: int = AGGRESSIVE_CONFIG.hard_max_imbalance  # From TRADING_CONFIGS
    accum_pair_cost_target: float = 0.995
    accum_pair_cost_limit: float = 1.02
    accum_buy_both_sides: bool = True

    # Volatility mode parameters (legacy)
    vol_trade_size: int = 2
    vol_ma_window: int = 10
    vol_buy_discount: float = 0.02
    vol_max_imbalance: int = 20
    vol_soft_balance: bool = True


# Global state - Dual strategy support
class StrategyState:
    """State for a single trading strategy."""
    def __init__(self, name: str):
        self.name = name
        self.task: Optional[asyncio.Task] = None
        self.instance = None  # Reference to the actual bot
        self.status = {
            "running": False,
            "strategy": name,
            "error": None,
            "config": None,
            "start_time": None,
            "balance": None,
        }

    def reset_trading_data(self):
        """Clear trading data when strategy stops. Prevents stale data on UI refresh."""
        self.status.pop("latest_trading_update", None)
        self.status["balance"] = None


# Multi-strategy state - supports running multiple strategies simultaneously
strategies = {
    "aggressive": StrategyState("aggressive"),          # PHOENIX V3: Maker-prediction FADE mode
    "contrarian": StrategyState("contrarian"),          # Path 2: Bet against BTC direction
    "volume_weighted": StrategyState("volume_weighted"), # Gabagool-style grid maker
}

# Legacy global state for backward compatibility
bot_task: Optional[asyncio.Task] = None
bot_instance = None
bot_status = {
    "running": False,
    "error": None,
    "config": None,
    "start_time": None,
    "balance": None,
}
connected_websockets: list[WebSocket] = []

# Health monitor instance
health_monitor: Optional[HealthMonitor] = None

# Auto-redeemer instance
auto_redeemer: Optional[AutoRedeemer] = None

# Auto-restart configuration storage
restart_configs: Dict[str, Dict[str, Any]] = {}


async def handle_health_alert(strategy_name: str, message: str, severity: HealthStatus):
    """Handle health alerts - broadcast to websockets and log."""
    logger.warning(f"[HEALTH ALERT] {strategy_name}: {message} (severity: {severity.value})")

    # Broadcast to connected websockets
    alert_data = {
        "type": "health_alert",
        "strategy": strategy_name,
        "message": message,
        "severity": severity.value,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await broadcast_message(alert_data)


async def handle_auto_restart(strategy_name: str):
    """Handle auto-restart for a crashed strategy."""
    logger.info(f"[AUTO-RESTART] Attempting restart for {strategy_name}")

    # CRITICAL: Check kill switch BEFORE any auto-restart
    if is_kill_switch_active():
        logger.warning(f"[AUTO-RESTART] BLOCKED - kill switch is active. No auto-restart for {strategy_name}")
        restart_configs.pop(strategy_name, None)  # Clear config to prevent future attempts
        return

    # Check if we have stored config for this strategy
    if strategy_name not in restart_configs:
        logger.error(f"[AUTO-RESTART] No stored config for {strategy_name}")
        return

    config = restart_configs[strategy_name]
    strategy = strategies.get(strategy_name)

    if not strategy:
        logger.error(f"[AUTO-RESTART] Unknown strategy: {strategy_name}")
        return

    # Cancel existing task if any
    if strategy.task and not strategy.task.done():
        strategy.task.cancel()
        try:
            await strategy.task
        except asyncio.CancelledError:
            pass

    # Wait a bit before restart
    await asyncio.sleep(5)

    # Extend end time to continue from now
    now = datetime.now()
    original_end = datetime.fromisoformat(config.get("end_datetime", now.isoformat()))
    remaining = (original_end - datetime.fromisoformat(config.get("start_datetime", now.isoformat()))).total_seconds()

    if remaining > 0:
        # Restart with remaining duration
        config["start_datetime"] = now.isoformat()
        config["end_datetime"] = (now + timedelta(seconds=remaining)).isoformat()

        logger.info(f"[AUTO-RESTART] Restarting {strategy_name} with {remaining/60:.1f} minutes remaining")

        try:
            # Route to appropriate bot runner based on strategy type
            if strategy_name == "aggressive":
                aggressive_config = AggressiveBotConfig(**config)
                strategy.status["running"] = True
                strategy.status["error"] = None
                strategy.status["restarted_at"] = now.isoformat()
                await broadcast_status()
                strategy.task = asyncio.create_task(run_aggressive_bot(aggressive_config, strategy))
            elif strategy_name == "contrarian":
                contrarian_config = ContrarianBotConfig(**config)
                strategy.status["running"] = True
                strategy.status["error"] = None
                strategy.status["restarted_at"] = now.isoformat()
                await broadcast_status()
                strategy.task = asyncio.create_task(run_contrarian_bot(contrarian_config, strategy))
            elif strategy_name == "volume_weighted":
                vw_config = VolumeWeightedBotConfig(**config)
                strategy.status["running"] = True
                strategy.status["error"] = None
                strategy.status["restarted_at"] = now.isoformat()
                await broadcast_status()
                strategy.task = asyncio.create_task(run_volume_weighted_bot(vw_config, strategy))
            else:
                logger.warning(f"[AUTO-RESTART] Unknown strategy: {strategy_name}")
                return

            logger.info(f"[AUTO-RESTART] Successfully restarted {strategy_name}")

        except Exception as e:
            logger.error(f"[AUTO-RESTART] Failed to restart {strategy_name}: {e}")
            strategy.status["error"] = f"Auto-restart failed: {e}"
            await broadcast_status()
    else:
        logger.info(f"[AUTO-RESTART] Session ended, not restarting {strategy_name}")


@app.on_event("startup")
async def startup_event():
    """Initialize health monitoring and auto-redemption on server startup."""
    global health_monitor, auto_redeemer

    # Create and configure health monitor
    health_monitor = HealthMonitor(
        trade_gap_threshold_minutes=20,
        heartbeat_threshold_seconds=60,
        check_interval_seconds=30,
        on_alert=handle_health_alert,
        on_restart=handle_auto_restart,
    )
    set_health_monitor(health_monitor)

    # Start the health monitor
    await health_monitor.start()
    logger.info("[SERVER] Health monitor started")

    # Start auto-redeemer for winning positions
    try:
        config = Config()
        if config.wallet_type == "gnosis_safe":
            auto_redeemer = AutoRedeemer(
                config=config,
                interval_minutes=5.0,  # Check every 5 minutes
            )
            await auto_redeemer.start()
            logger.info("[SERVER] Auto-redeemer started (5 min interval)")
        else:
            logger.info(f"[SERVER] Auto-redeemer disabled (wallet_type={config.wallet_type}, needs gnosis_safe)")
    except Exception as e:
        logger.error(f"[SERVER] Failed to start auto-redeemer: {e}")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on server shutdown."""
    global health_monitor, auto_redeemer

    if auto_redeemer:
        await auto_redeemer.stop()
        logger.info("[SERVER] Auto-redeemer stopped")

    if health_monitor:
        await health_monitor.stop()
        logger.info("[SERVER] Health monitor stopped")

    # Stop all running strategies
    for name, strategy in strategies.items():
        if strategy and strategy.task and not strategy.task.done():
            strategy.task.cancel()
            try:
                await strategy.task
            except asyncio.CancelledError:
                pass
            logger.info(f"[SERVER] Stopped strategy: {name}")


@app.get("/api/health")
async def get_health():
    """Get health status for all strategies."""
    if health_monitor:
        return health_monitor.get_status_summary()
    return {"overall_status": "unknown", "strategies": {}, "alerts": []}


@app.get("/api/health/{strategy_name}")
async def get_strategy_health(strategy_name: str):
    """Get health status for a specific strategy."""
    if not health_monitor:
        return JSONResponse(
            status_code=503,
            content={"error": "Health monitor not initialized"}
        )

    health = health_monitor.get_health(strategy_name)
    if not health:
        return JSONResponse(
            status_code=404,
            content={"error": f"Strategy {strategy_name} not found"}
        )

    return {
        "strategy": strategy_name,
        "status": health.status.value,
        "last_trade": health.last_trade_time.isoformat() if health.last_trade_time else None,
        "last_heartbeat": health.last_heartbeat.isoformat() if health.last_heartbeat else None,
        "trade_count": health.trade_count,
        "error_count": health.error_count,
        "restart_count": health.restart_count,
        "last_error": health.last_error,
    }


# Track server start time for uptime calculation
_server_start_time: Optional[datetime] = None


@app.on_event("startup")
async def set_start_time():
    """Record server start time for uptime calculation."""
    global _server_start_time
    _server_start_time = datetime.now(timezone.utc)


@app.get("/api/metrics")
async def get_metrics():
    """
    Get comprehensive metrics for monitoring.

    Returns:
        JSON with strategy metrics, uptime, and error counts.
    """
    # Calculate uptime
    uptime_seconds = 0.0
    if _server_start_time:
        uptime_seconds = (datetime.now(timezone.utc) - _server_start_time).total_seconds()

    # Collect strategy metrics
    strategy_metrics = {}
    errors_last_hour = 0

    for name, strategy_state in strategies.items():
        metrics = {
            "running": strategy_state.status.get("running", False),
            "balance": strategy_state.status.get("balance"),
            "start_time": strategy_state.status.get("start_time"),
            "error": strategy_state.status.get("error"),
        }

        # Get additional metrics from strategy instance if available
        if strategy_state.instance:
            instance = strategy_state.instance
            if hasattr(instance, 'get_metrics'):
                try:
                    metrics.update(instance.get_metrics())
                except Exception:
                    pass
            # Try to get strategy-specific stats
            if hasattr(instance, '_trade_count'):
                metrics["trade_count"] = instance._trade_count
            if hasattr(instance, '_total_pairs'):
                metrics["total_pairs"] = instance._total_pairs
            if hasattr(instance, 'cumulative_pnl'):
                metrics["cumulative_pnl"] = instance.cumulative_pnl

        # Count errors from health monitor
        if health_monitor:
            health = health_monitor.get_health(name)
            if health:
                metrics["health_status"] = health.status.value
                metrics["health_trade_count"] = health.trade_count
                metrics["health_error_count"] = health.error_count
                errors_last_hour += health.error_count

        strategy_metrics[name] = metrics

    # Build response
    return {
        "uptime_seconds": round(uptime_seconds, 1),
        "uptime_formatted": _format_uptime(uptime_seconds),
        "errors_last_hour": errors_last_hour,
        "strategies": strategy_metrics,
        "kill_switch_active": is_kill_switch_active(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _format_uptime(seconds: float) -> str:
    """Format uptime as human-readable string."""
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"


@app.get("/")
async def root(username: str = Depends(verify_credentials)):
    """Serve the main HTML page. Requires authentication."""
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/status")
async def get_status(username: str = Depends(verify_credentials)):
    """Get current bot status for all strategies. Requires authentication."""
    return {
        "aggressive": strategies["aggressive"].status,
        "contrarian": strategies["contrarian"].status,
        "volume_weighted": strategies["volume_weighted"].status,
        "running": (
            strategies["aggressive"].status["running"] or
            strategies["contrarian"].status["running"] or
            strategies["volume_weighted"].status["running"]
        ),
        "kill_switch_active": is_kill_switch_active(),
    }


@app.get("/api/kill-switch")
async def get_kill_switch_status():
    """Check if kill switch is active."""
    active = is_kill_switch_active()
    reason = None
    if active and KILL_SWITCH_FILE.exists():
        try:
            reason = KILL_SWITCH_FILE.read_text()
        except Exception:
            pass
    return {"active": active, "reason": reason}


@app.post("/api/kill-switch/clear")
async def clear_kill_switch_endpoint(username: str = Depends(verify_credentials)):
    """Clear the kill switch to allow bot to start again. Requires authentication."""
    success = clear_kill_switch()
    logger.info(f"[KILL-SWITCH] Cleared by user {username}")
    return {"success": success, "active": is_kill_switch_active()}


@app.post("/api/validate")
async def validate_config(config: BotConfig):
    """
    DEPRECATED: Validate legacy configuration without starting the bot.

    Use the new strategy-specific endpoints instead:
    - POST /api/start/aggressive (Path 1 - spike detection)
    - POST /api/start/contrarian (Path 2 - reversal detection)
    """
    logger.warning("[DEPRECATED] /api/validate endpoint is deprecated")
    errors = []

    # Validate datetime format
    try:
        start_dt = datetime.fromisoformat(config.start_datetime)
        end_dt = datetime.fromisoformat(config.end_datetime)

        # Check if start time is in the past
        now = datetime.now()
        if start_dt < now:
            errors.append("Start time cannot be in the past")

        # Check if end time is after start time
        if end_dt <= start_dt:
            errors.append("End time must be after start time")

    except ValueError as e:
        errors.append(f"Invalid datetime format: {e}")

    # Validate balance
    if config.starting_balance <= 0:
        errors.append("Starting balance must be positive")

    # Validate accumulation mode parameters
    if config.accum_trade_size < 1:
        errors.append("Accumulation trade size must be at least 1")
    if config.accum_target_shares < 1:
        errors.append("Accumulation target shares must be at least 1")
    if not (0.01 <= config.accum_max_imbalance_pct <= 1.0):
        errors.append("Max imbalance % must be between 1% and 100%")
    if not (0 < config.accum_pair_cost_target <= 1.0):
        errors.append("Accumulation pair cost target must be between 0 and 1")
    if not (0 < config.accum_pair_cost_limit <= 1.1):
        errors.append("Accumulation pair cost limit must be between 0 and 1.1")
    if config.accum_pair_cost_target > config.accum_pair_cost_limit:
        errors.append("Pair cost target must be <= pair cost limit")

    # Validate volatility mode parameters
    if config.vol_trade_size < 1:
        errors.append("Volatility trade size must be at least 1")
    if config.vol_max_imbalance < 1:
        errors.append("Volatility max imbalance must be at least 1")
    if config.vol_ma_window < 1:
        errors.append("Volatility MA window must be at least 1")
    if not (0 <= config.vol_buy_discount <= 0.5):
        errors.append("Volatility buy discount must be between 0 and 0.5")
    if not (0 < config.max_share_price <= 1.0):
        errors.append("Max share price must be between 0 and 1")

    # Check actual Polymarket balance for live mode
    if config.mode == "live":
        try:
            from src.config import Config
            from src.api.polymarket_client import PolymarketClient

            pm_config = Config()
            pm_config.validate()
            client = PolymarketClient(pm_config)
            await client.connect()
            actual_balance = await client.get_balance()
            await client.disconnect()

            if config.starting_balance > actual_balance:
                errors.append(
                    f"Starting balance ${config.starting_balance:.2f} exceeds "
                    f"Polymarket balance ${actual_balance:.2f}"
                )
            else:
                logger.info(f"[LIVE] Balance check passed: ${actual_balance:.2f} available")
        except Exception as e:
            errors.append(f"Failed to check Polymarket balance: {e}")

    if errors:
        return {"valid": False, "errors": errors}
    return {"valid": True, "errors": []}


@app.post("/api/start")
async def start_bot(config: BotConfig, username: str = Depends(verify_credentials)):
    """
    DEPRECATED: Start the legacy trading bot with given configuration.

    Use the new strategy-specific endpoints instead:
    - POST /api/start/aggressive (Path 1 - spike detection)
    - POST /api/start/contrarian (Path 2 - reversal detection)
    - POST /api/start/volume_weighted (Gabagool-style grid maker)

    Requires authentication.
    """
    global bot_task, bot_status
    logger.warning("[DEPRECATED] /api/start endpoint is deprecated - use /api/start/aggressive or /api/start/contrarian")

    # Don't start if already running
    if bot_status["running"]:
        return JSONResponse(
            status_code=400,
            content={"error": "Bot is already running"}
        )

    # Validate first
    validation = await validate_config(config)
    if not validation["valid"]:
        return JSONResponse(
            status_code=400,
            content={"error": "; ".join(validation["errors"])}
        )

    # Update status
    bot_status = {
        "running": True,
        "error": None,
        "config": config.dict(),
        "start_time": datetime.now(timezone.utc).isoformat(),
        "balance": config.starting_balance,
    }

    # Start bot in background
    bot_task = asyncio.create_task(run_bot_async(config))

    # Notify connected clients
    await broadcast_status()

    return {"status": "started", "config": config.dict()}


@app.post("/api/stop")
async def stop_bot(username: str = Depends(verify_credentials)):
    """Stop the trading bot. Requires authentication."""
    global bot_task, bot_status, bot_instance

    if bot_instance:
        bot_instance.stop()

    if bot_task and not bot_task.done():
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass

    bot_status = {
        "running": False,
        "error": None,
        "config": None,
        "start_time": None,
        "balance": None,
    }
    bot_task = None
    bot_instance = None

    # CRITICAL: Clear ALL restart configs to prevent any auto-restart
    restart_configs.clear()
    logger.info("[MANUAL-STOP] Cleared ALL restart configs - auto-restart disabled for all strategies")

    # ALSO unregister ALL strategies from health monitor
    if health_monitor:
        for strategy_name in list(strategies.keys()):
            health_monitor.unregister_strategy(strategy_name)
        logger.info("[MANUAL-STOP] Unregistered all strategies from health monitor")

    # Activate kill switch to prevent systemd restart
    activate_kill_switch(reason="Manual stop via web UI")

    # Notify connected clients
    await broadcast_status()

    return {"status": "stopped", "kill_switch_activated": True}


@app.post("/api/emergency-stop")
async def emergency_stop(username: str = Depends(verify_credentials)):
    """Cancel all orders and stop ALL bots (NUKE ALL). Requires authentication."""
    global bot_task, bot_status, bot_instance

    results = {
        "status": "emergency_stopped",
        "orders_cancelled": 0,
        "details": [],
        "strategies_stopped": []
    }

    # === STOP ALL STRATEGIES IN THE NEW MULTI-STRATEGY SYSTEM ===
    for strategy_name, strategy in strategies.items():
        if strategy.status.get("running") and strategy.instance:
            try:
                logger.info(f"[NUKE-ALL] Stopping {strategy_name}...")
                # Request graceful stop
                strategy.instance.request_stop()

                # Cancel all open orders (but DO NOT sell positions)
                if hasattr(strategy.instance, 'client'):
                    try:
                        client = strategy.instance.client
                        open_orders = await client.get_open_orders()
                        if open_orders:
                            order_ids = [o.get('id') for o in open_orders if o.get('id')]
                            if order_ids:
                                await client.cancel_orders(order_ids)
                                results["orders_cancelled"] += len(order_ids)
                                logger.info(f"[NUKE-ALL] Cancelled {len(order_ids)} orders for {strategy_name}")
                    except Exception as e:
                        logger.warning(f"[NUKE-ALL] Failed to cancel orders for {strategy_name}: {e}")

                results["strategies_stopped"].append(strategy_name)
                logger.info(f"[NUKE-ALL] {strategy_name} stopped (positions kept)")
            except Exception as e:
                logger.error(f"[NUKE-ALL] Error stopping {strategy_name}: {e}")
                results["details"].append({"strategy": strategy_name, "error": str(e)})

        # Cancel the task
        if strategy.task and not strategy.task.done():
            strategy.task.cancel()
            try:
                await strategy.task
            except asyncio.CancelledError:
                pass

        # Reset strategy status
        strategy.status["running"] = False
        strategy.status["completed"] = True
        strategy.reset_trading_data()  # Clear stale position data
        strategy.instance = None
        strategy.task = None

    # === ALSO STOP LEGACY BOT IF RUNNING (cancel orders only, no sell) ===
    if bot_instance:
        try:
            if hasattr(bot_instance, 'client'):
                client = bot_instance.client
                open_orders = await client.get_open_orders()
                if open_orders:
                    order_ids = [o.get('id') for o in open_orders if o.get('id')]
                    if order_ids:
                        await client.cancel_orders(order_ids)
                        results["orders_cancelled"] += len(order_ids)
                        logger.info(f"[NUKE-ALL] Cancelled {len(order_ids)} orders for legacy bot")
        except Exception as e:
            results["details"].append({"legacy_bot": True, "error": str(e)})

    if bot_task and not bot_task.done():
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass

    # Clear ALL restart configs to prevent any auto-restart after NUKE ALL
    restart_configs.clear()
    logger.info("[NUKE-ALL] Cleared all restart configs - auto-restart disabled for all strategies")

    # ALSO unregister ALL strategies from health monitor to prevent race condition
    if health_monitor:
        for strategy_name in list(strategies.keys()):
            health_monitor.unregister_strategy(strategy_name)
        logger.info("[NUKE-ALL] Unregistered all strategies from health monitor")

    # CRITICAL: Activate kill switch to prevent systemd from restarting
    activate_kill_switch(reason="NUKE ALL - emergency stop via web UI")
    results["kill_switch_activated"] = True

    # Update legacy status
    bot_status = {
        "running": False,
        "error": None,
        "config": None,
        "start_time": None,
        "balance": None,
        "emergency_stopped": True,
        "emergency_results": results,
    }
    bot_task = None
    bot_instance = None

    # Notify connected clients
    await broadcast_status()

    logger.info(f"[NUKE-ALL] Complete: stopped {len(results['strategies_stopped'])} strategies, cancelled {results['orders_cancelled']} orders")
    return results


# =============================================================================
# STRATEGY ENDPOINTS
# =============================================================================

@app.post("/api/start/aggressive")
async def start_aggressive(config: AggressiveBotConfig, username: str = Depends(verify_credentials)):
    """Start the AGGRESSIVE (Path 1) trading strategy. Requires authentication."""
    clear_kill_switch()

    strategy = strategies["aggressive"]

    # Check if actually running (task exists and not done) vs just stale status
    actually_running = (
        strategy.status["running"] and
        strategy.task is not None and
        not strategy.task.done()
    )

    if actually_running:
        return JSONResponse(
            status_code=400,
            content={"error": "AGGRESSIVE strategy is already running"}
        )

    # Reset stale status if task is done but status wasn't updated
    if strategy.status["running"] and (strategy.task is None or strategy.task.done()):
        strategy.status["running"] = False
        strategy.reset_trading_data()
        strategy.task = None

    # DEBUG: Log received config values from frontend
    logger.info(f"[aggressive] RECEIVED from frontend: start={config.start_datetime}, end={config.end_datetime}")

    # Validate datetime
    try:
        start_dt = datetime.fromisoformat(config.start_datetime)
        end_dt = datetime.fromisoformat(config.end_datetime)
        if end_dt <= start_dt:
            return JSONResponse(status_code=400, content={"error": "End time must be after start time"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": f"Invalid datetime: {e}"})

    # Validate AGGRESSIVE parameters
    if config.base_size < 5:
        return JSONResponse(status_code=400, content={"error": "Base size must be at least 5 (Polymarket minimum)"})
    # Only validate z_lo < z_hi if both are set (None = disabled)
    if config.z_lo is not None and config.z_hi is not None and config.z_lo >= config.z_hi:
        return JSONResponse(status_code=400, content={"error": "z_lo must be less than z_hi"})

    # Validate balance for live mode
    if config.mode == "live":
        try:
            from src.config import Config
            from src.api.polymarket_client import PolymarketClient

            pm_config = Config()
            pm_config.validate()
            client = PolymarketClient(pm_config)
            await client.connect()
            actual_balance = await client.get_balance()
            await client.disconnect()

            if config.starting_balance > actual_balance:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": f"Starting balance ${config.starting_balance:.2f} exceeds "
                                 f"Polymarket balance ${actual_balance:.2f}"
                    }
                )
            logger.info(f"[LIVE] Balance check passed: ${actual_balance:.2f} available")
        except Exception as e:
            return JSONResponse(status_code=400, content={"error": f"Failed to check Polymarket balance: {e}"})

    # Update strategy status
    strategy.status = {
        "running": True,
        "strategy": "aggressive",
        "error": None,
        "config": config.dict(),
        "start_time": datetime.now(timezone.utc).isoformat(),
        "balance": config.starting_balance,
    }

    # Start bot in background
    strategy.task = asyncio.create_task(run_aggressive_bot(config, strategy))

    await broadcast_status()
    return {"status": "started", "strategy": "aggressive", "config": config.dict()}


@app.post("/api/start/contrarian")
async def start_contrarian(config: ContrarianBotConfig, username: str = Depends(verify_credentials)):
    """Start the CONTRARIAN (Path 2) trading strategy. Requires authentication."""
    clear_kill_switch()

    strategy = strategies["contrarian"]

    # Check if actually running
    actually_running = (
        strategy.status["running"] and
        strategy.task is not None and
        not strategy.task.done()
    )

    if actually_running:
        return JSONResponse(
            status_code=400,
            content={"error": "CONTRARIAN strategy is already running"}
        )

    # Reset stale status
    if strategy.status["running"] and (strategy.task is None or strategy.task.done()):
        strategy.status["running"] = False
        strategy.reset_trading_data()
        strategy.task = None

    # Validate datetime
    try:
        start_dt = datetime.fromisoformat(config.start_datetime)
        end_dt = datetime.fromisoformat(config.end_datetime)
        if end_dt <= start_dt:
            return JSONResponse(status_code=400, content={"error": "End time must be after start time"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": f"Invalid datetime: {e}"})

    # Validate CONTRARIAN parameters
    if config.shares_per_trade < 5:
        return JSONResponse(status_code=400, content={"error": "Shares per trade must be at least 5"})
    if config.retracement_min < 0.1 or config.retracement_min > 0.9:
        return JSONResponse(status_code=400, content={"error": "Retracement min must be between 0.1 and 0.9"})

    # Validate balance for live mode
    if config.mode == "live":
        try:
            from src.config import Config
            from src.api.polymarket_client import PolymarketClient

            pm_config = Config()
            pm_config.validate()
            client = PolymarketClient(pm_config)
            await client.connect()
            actual_balance = await client.get_balance()
            await client.disconnect()

            if config.starting_balance > actual_balance:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": f"Starting balance ${config.starting_balance:.2f} exceeds "
                                 f"Polymarket balance ${actual_balance:.2f}"
                    }
                )
            logger.info(f"[LIVE] Balance check passed: ${actual_balance:.2f} available")
        except Exception as e:
            return JSONResponse(status_code=400, content={"error": f"Failed to check Polymarket balance: {e}"})

    # Update strategy status
    strategy.status = {
        "running": True,
        "strategy": "contrarian",
        "error": None,
        "config": config.dict(),
        "start_time": datetime.now(timezone.utc).isoformat(),
        "balance": config.starting_balance,
    }

    # Start bot in background
    strategy.task = asyncio.create_task(run_contrarian_bot(config, strategy))

    await broadcast_status()
    return {"status": "started", "strategy": "contrarian", "config": config.dict()}


@app.post("/api/start/volume_weighted")
async def start_volume_weighted(config: VolumeWeightedBotConfig, username: str = Depends(verify_credentials)):
    """Start the Volume Weighted (Gabagool-style) trading strategy. Requires authentication."""
    clear_kill_switch()

    strategy = strategies["volume_weighted"]

    # Check if actually running
    actually_running = (
        strategy.status["running"] and
        strategy.task is not None and
        not strategy.task.done()
    )

    if actually_running:
        return JSONResponse(
            status_code=400,
            content={"error": "Volume Weighted strategy is already running"}
        )

    # Reset stale status
    if strategy.status["running"] and (strategy.task is None or strategy.task.done()):
        strategy.status["running"] = False
        strategy.reset_trading_data()
        strategy.task = None

    # Validate datetime
    try:
        start_dt = datetime.fromisoformat(config.start_datetime)
        end_dt = datetime.fromisoformat(config.end_datetime)
        if end_dt <= start_dt:
            return JSONResponse(status_code=400, content={"error": "End time must be after start time"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": f"Invalid datetime: {e}"})

    # Validate VW parameters
    if config.vw_cheap_threshold < 0.20 or config.vw_cheap_threshold > 0.60:
        return JSONResponse(status_code=400, content={"error": "Cheap threshold must be between $0.20 and $0.60"})
    if config.vw_max_hedge_price < 0.50 or config.vw_max_hedge_price > 0.95:
        return JSONResponse(status_code=400, content={"error": "Max hedge price must be between $0.50 and $0.95"})

    # Validate balance for live mode
    if config.mode == "live":
        try:
            from src.config import Config
            from src.api.polymarket_client import PolymarketClient

            pm_config = Config()
            pm_config.validate()
            client = PolymarketClient(pm_config)
            await client.connect()
            actual_balance = await client.get_balance()
            await client.disconnect()

            if config.starting_balance > actual_balance:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": f"Starting balance ${config.starting_balance:.2f} exceeds "
                                 f"Polymarket balance ${actual_balance:.2f}"
                    }
                )
            logger.info(f"[LIVE] Balance check passed: ${actual_balance:.2f} available")
        except Exception as e:
            return JSONResponse(status_code=400, content={"error": f"Failed to check Polymarket balance: {e}"})

    # Update strategy status
    strategy.status = {
        "running": True,
        "strategy": "volume_weighted",
        "error": None,
        "config": config.dict(),
        "start_time": datetime.now(timezone.utc).isoformat(),
        "balance": config.starting_balance,
    }

    # Start bot in background
    strategy.task = asyncio.create_task(run_volume_weighted_bot(config, strategy))

    await broadcast_status()
    return {"status": "started", "strategy": "volume_weighted", "config": config.dict()}


@app.post("/api/stop/{strategy_name}")
async def stop_strategy(strategy_name: str, username: str = Depends(verify_credentials)):
    """Gracefully stop a strategy. Requires authentication."""
    if strategy_name not in strategies:
        return JSONResponse(status_code=404, content={"error": f"Unknown strategy: {strategy_name}"})

    strategy = strategies[strategy_name]
    cancelled_orders = 0

    # FIRST: Cancel all open orders before stopping
    if strategy.instance and hasattr(strategy.instance, 'client'):
        try:
            client = strategy.instance.client
            open_orders = await client.get_open_orders()
            if open_orders:
                order_ids = [o.get('id') for o in open_orders if o.get('id')]
                if order_ids:
                    await client.cancel_orders(order_ids)
                    cancelled_orders = len(order_ids)
                    logger.info(f"[MANUAL-STOP] Cancelled {cancelled_orders} open orders for {strategy_name}")
        except Exception as e:
            logger.warning(f"[MANUAL-STOP] Failed to cancel orders for {strategy_name}: {e}")

    if strategy.instance:
        # Use graceful stop - bot will finish current market then stop
        strategy.instance.graceful_stop()
        # Update status to indicate stopping
        if strategy.status:
            strategy.status["stopping"] = True

    # CRITICAL: Clear restart config to prevent auto-restart after manual stop
    if strategy_name in restart_configs:
        del restart_configs[strategy_name]
        logger.info(f"[MANUAL-STOP] Cleared restart config for {strategy_name} - auto-restart disabled")

    # ALSO unregister from health monitor to prevent race condition
    if health_monitor:
        health_monitor.unregister_strategy(strategy_name)
        logger.info(f"[MANUAL-STOP] Unregistered {strategy_name} from health monitor")

    # Activate kill switch to prevent systemd restart
    activate_kill_switch(reason=f"Manual stop - {strategy_name} via web UI")

    await broadcast_status()
    return {"status": "stopping", "cancelled_orders": cancelled_orders, "kill_switch_activated": True, "message": "Cancelled open orders and stopping (auto-restart disabled)"}


@app.post("/api/graceful-stop/{strategy_name}")
async def graceful_stop_strategy(strategy_name: str, username: str = Depends(verify_credentials)):
    """Request graceful stop. Requires authentication."""
    if strategy_name not in strategies:
        return JSONResponse(status_code=404, content={"error": f"Unknown strategy: {strategy_name}"})

    strategy = strategies[strategy_name]

    if not strategy.instance:
        return JSONResponse(status_code=400, content={"error": "Strategy not running", "success": False})

    cancelled_orders = 0

    # FIRST: Cancel all open orders before stopping
    if hasattr(strategy.instance, 'client'):
        try:
            client = strategy.instance.client
            open_orders = await client.get_open_orders()
            if open_orders:
                order_ids = [o.get('id') for o in open_orders if o.get('id')]
                if order_ids:
                    await client.cancel_orders(order_ids)
                    cancelled_orders = len(order_ids)
                    logger.info(f"[GRACEFUL-STOP] Cancelled {cancelled_orders} open orders for {strategy_name}")
        except Exception as e:
            logger.warning(f"[GRACEFUL-STOP] Failed to cancel orders for {strategy_name}: {e}")

    # Set graceful stop flag on the bot instance
    if hasattr(strategy.instance, 'graceful_stop'):
        strategy.instance.graceful_stop()
        # Clear restart config to prevent auto-restart after graceful stop
        if strategy_name in restart_configs:
            del restart_configs[strategy_name]
            logger.info(f"[GRACEFUL-STOP] Cleared restart config for {strategy_name} - auto-restart disabled")
        # Activate kill switch to prevent systemd restart
        activate_kill_switch(reason=f"Graceful stop - {strategy_name} via web UI")

        # FIXED: Also cancel the task after a short delay to ensure it actually stops
        # Don't wait forever for graceful stop - force cancel after 10 seconds
        if strategy.task and not strategy.task.done():
            async def force_cancel_after_delay():
                await asyncio.sleep(10)  # Give 10 seconds for graceful stop
                if strategy.task and not strategy.task.done():
                    logger.warning(f"[GRACEFUL-STOP] Force cancelling {strategy_name} task after timeout")
                    strategy.task.cancel()
            asyncio.create_task(force_cancel_after_delay())

        return {"success": True, "cancelled_orders": cancelled_orders, "kill_switch_activated": True, "message": f"Cancelled {cancelled_orders} orders, graceful stop requested for {strategy_name}"}
    else:
        return JSONResponse(status_code=400, content={"error": "Strategy does not support graceful stop", "success": False})


@app.post("/api/emergency-stop/{strategy_name}")
async def emergency_stop_strategy(strategy_name: str, username: str = Depends(verify_credentials)):
    """Cancel all orders and stop a specific strategy. Requires authentication."""
    try:
        if strategy_name not in strategies:
            return JSONResponse(status_code=404, content={"error": f"Unknown strategy: {strategy_name}"})

        strategy = strategies[strategy_name]

        results = {
            "status": "emergency_stopped",
            "strategy": strategy_name,
            "orders_cancelled": 0,
            "details": []
        }

        # Cancel all open orders (but DO NOT sell positions)
        if strategy.instance and hasattr(strategy.instance, 'client'):
            try:
                client = strategy.instance.client
                open_orders = await client.get_open_orders()
                if open_orders:
                    order_ids = [o.get('id') for o in open_orders if o.get('id')]
                    if order_ids:
                        await client.cancel_orders(order_ids)
                        results["orders_cancelled"] = len(order_ids)
                        logger.info(f"[EMERGENCY-STOP] Cancelled {len(order_ids)} orders for {strategy_name}")
            except Exception as e:
                results["error"] = str(e)

        if strategy.task and not strategy.task.done():
            strategy.task.cancel()
            try:
                await strategy.task
            except asyncio.CancelledError:
                pass

        # Clear restart config to prevent auto-restart after emergency stop
        if strategy_name in restart_configs:
            del restart_configs[strategy_name]
            logger.info(f"[EMERGENCY-STOP] Cleared restart config for {strategy_name} - auto-restart disabled")

        # Activate kill switch to prevent systemd restart
        activate_kill_switch(reason=f"Emergency stop - {strategy_name} via web UI")
        results["kill_switch_activated"] = True

        # FIXED: Clear state files to prevent position restoration on restart
        state_dir = Path(__file__).parent.parent / "state"
        state_files_cleared = []
        for mode in ["live", "paper"]:
            state_file = state_dir / f"state_{strategy_name}_{mode}.json"
            if state_file.exists():
                try:
                    state_file.unlink()
                    state_files_cleared.append(str(state_file.name))
                    logger.info(f"[EMERGENCY-STOP] Cleared state file: {state_file.name}")
                except Exception as e:
                    logger.warning(f"[EMERGENCY-STOP] Failed to clear state file {state_file.name}: {e}")
        results["state_files_cleared"] = state_files_cleared

        strategy.status = {
            "running": False,
            "strategy": strategy_name,
            "error": None,
            "config": None,
            "start_time": None,
            "balance": None,
            "emergency_stopped": True,
            "emergency_results": results,
        }
        strategy.task = None
        strategy.instance = None

        await broadcast_status()
        return results

    except Exception as e:
        # Catch-all to prevent 500 Internal Server Errors
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "status": "failed", "strategy": strategy_name}
        )


# =============================================================================
# STRATEGY-SPECIFIC BOT RUNNERS
# =============================================================================

async def run_accumulation_bot(config: AccumulationBotConfig, strategy: StrategyState):
    """Run the Accumulation trading bot asynchronously with resilience."""
    accum_mode = config.accum_mode

    # Store config for auto-restart
    restart_configs[accum_mode] = config.dict()

    try:
        from scripts.run_paper_bot import PaperTradingBot

        # CRITICAL: Normalize times to UTC at the API boundary
        # Frontend sends local time (IST) - convert to UTC for consistent handling
        start_dt_utc = normalize_datetime_to_utc(config.start_datetime)
        end_dt_utc = normalize_datetime_to_utc(config.end_datetime)

        logger.info(f"[{accum_mode}] Session time window (UTC): {start_dt_utc.isoformat()} to {end_dt_utc.isoformat()}")

        # Wait until start time (compare in UTC)
        now_utc = datetime.now(timezone.utc)
        if start_dt_utc > now_utc:
            wait_seconds = (start_dt_utc - now_utc).total_seconds()
            strategy.status["waiting_until"] = start_dt_utc.isoformat()
            await broadcast_status()
            await asyncio.sleep(wait_seconds)

        strategy.status.pop("waiting_until", None)
        strategy.status["trading_started"] = datetime.now(timezone.utc).isoformat()
        strategy.status["end_datetime"] = end_dt_utc.isoformat()
        await broadcast_status()

        duration_minutes = (end_dt_utc - start_dt_utc).total_seconds() / 60.0

        # Use accum_mode as strategy name for callbacks and CSV naming
        web_callback = create_web_callback_for_strategy(accum_mode)

        # Create Accumulation bot with mode-specific settings
        # CRITICAL: Pass session time window to enforce market selection within bounds
        bot = PaperTradingBot.from_web_config(
            config.dict(),
            web_callback=web_callback,
            strategy_name=accum_mode,  # Use mode name for CSV differentiation
            session_start_utc=start_dt_utc,
            session_end_utc=end_dt_utc,
        )
        strategy.instance = bot

        logger.info(f"[{accum_mode}] Initializing bot...")
        await bot.initialize()

        logger.info(f"[{accum_mode}] Starting trading loop for {duration_minutes:.1f} minutes")
        await bot.run(duration_minutes=duration_minutes)

        strategy.status["running"] = False
        strategy.status["completed"] = True
        strategy.reset_trading_data()  # Clear stale position data
        strategy.instance = None
        # Clear restart config on normal completion (including graceful stop via Telegram)
        if accum_mode in restart_configs:
            del restart_configs[accum_mode]
            logger.info(f"[{accum_mode}] Cleared restart config - session completed normally")
        logger.info(f"[{accum_mode}] Trading session completed normally")
        await broadcast_status()

    except asyncio.CancelledError:
        strategy.status["running"] = False
        strategy.status["error"] = "Stopped by user"
        strategy.reset_trading_data()  # Clear stale position data
        strategy.instance = None
        # Clear restart config on user cancellation
        if accum_mode in restart_configs:
            del restart_configs[accum_mode]
            logger.info(f"[{accum_mode}] Cleared restart config - stopped by user")
        logger.info(f"[{accum_mode}] Stopped by user")
        await broadcast_status()
        raise
    except Exception as e:
        strategy.status["running"] = False
        strategy.status["error"] = str(e)
        strategy.reset_trading_data()  # Clear stale position data
        strategy.instance = None
        logger.error(f"[{accum_mode}] Error: {e}")
        logger.error(f"[{accum_mode}] Traceback: {traceback.format_exc()}")
        await broadcast_status()
        # FIXED: Clear restart_configs on crash to prevent rogue auto-restart
        restart_configs.pop(accum_mode, None)
        logger.info(f"[{accum_mode}] Cleared restart config - no auto-restart will occur")


async def run_aggressive_bot(config: AggressiveBotConfig, strategy: StrategyState):
    """Run the AGGRESSIVE (Path 1) trading bot asynchronously.

    Uses EnhancedSpikeStrategy with spike detection, velocity confirmation,
    OU adaptive threshold, and time-stop exit logic.

    If ENABLE_AUTO_DATA_COLLECTION is True, also starts observer + 60Hz logger
    for the same duration. Both stop together when trading stops.
    """
    restart_configs["aggressive"] = config.dict()
    data_collection_manager = None
    data_collection_task = None

    try:
        from scripts.run_paper_bot import PaperTradingBot

        start_dt_utc = normalize_datetime_to_utc(config.start_datetime)
        end_dt_utc = normalize_datetime_to_utc(config.end_datetime)

        logger.info(f"[aggressive] Session time window (UTC): {start_dt_utc.isoformat()} to {end_dt_utc.isoformat()}")

        now_utc = datetime.now(timezone.utc)
        if start_dt_utc > now_utc:
            wait_seconds = (start_dt_utc - now_utc).total_seconds()
            strategy.status["waiting_until"] = start_dt_utc.isoformat()
            await broadcast_status()
            await asyncio.sleep(wait_seconds)

        strategy.status.pop("waiting_until", None)
        strategy.status["trading_started"] = datetime.now(timezone.utc).isoformat()
        strategy.status["end_datetime"] = end_dt_utc.isoformat()
        await broadcast_status()

        duration_minutes = (end_dt_utc - start_dt_utc).total_seconds() / 60.0
        duration_hours = duration_minutes / 60.0
        web_callback = create_web_callback_for_strategy("aggressive")

        # AUTO DATA COLLECTION (Feb 5, 2026)
        # Start observer + 60Hz logger alongside paper trading
        if ENABLE_AUTO_DATA_COLLECTION and config.mode == "paper":
            try:
                from scripts.run_data_collection import DataCollectionManager
                data_collection_manager = DataCollectionManager(
                    output_dir="research",
                    auto_restart=True  # Auto-restart on transient failures
                )
                logger.info(f"[aggressive] Starting auto data collection for {duration_hours:.2f} hours")
                data_collection_task = asyncio.create_task(
                    data_collection_manager.run(duration_hours=duration_hours)
                )
            except Exception as e:
                logger.warning(f"[aggressive] Failed to start data collection: {e}")
                data_collection_manager = None
                data_collection_task = None

        bot = PaperTradingBot.from_aggressive_config(
            config.dict(),
            web_callback=web_callback,
            session_start_utc=start_dt_utc,
            session_end_utc=end_dt_utc,
            trading_mode=config.mode,
        )
        strategy.instance = bot

        logger.info("[aggressive] Initializing bot...")
        await bot.initialize()

        logger.info(f"[aggressive] Starting trading loop for {duration_minutes:.1f} minutes")
        await bot.run(duration_minutes=duration_minutes)

        strategy.status["running"] = False
        strategy.status["completed"] = True
        strategy.reset_trading_data()
        strategy.instance = None
        restart_configs.pop("aggressive", None)
        logger.info("[aggressive] Trading session completed normally")
        await broadcast_status()

    except asyncio.CancelledError:
        strategy.status["running"] = False
        strategy.status["error"] = "Stopped by user"
        strategy.reset_trading_data()
        strategy.instance = None
        restart_configs.pop("aggressive", None)
        logger.info("[aggressive] Stopped by user")
        await broadcast_status()
        raise
    except Exception as e:
        strategy.status["running"] = False
        strategy.status["error"] = str(e)
        strategy.reset_trading_data()
        strategy.instance = None
        logger.error(f"[aggressive] Error: {e}")
        logger.error(f"[aggressive] Traceback: {traceback.format_exc()}")
        await broadcast_status()
        restart_configs.pop("aggressive", None)
    finally:
        # ALWAYS stop data collection when trading stops (any reason)
        if data_collection_manager:
            logger.info("[aggressive] Stopping auto data collection...")
            data_collection_manager.stop()
        if data_collection_task and not data_collection_task.done():
            data_collection_task.cancel()
            try:
                await data_collection_task
            except asyncio.CancelledError:
                pass
            logger.info("[aggressive] Data collection stopped")


async def run_contrarian_bot(config: ContrarianBotConfig, strategy: StrategyState):
    """Run the CONTRARIAN (Path 2) trading bot asynchronously.

    Bets against BTC direction at 15-min scale when reversal detected.

    If ENABLE_AUTO_DATA_COLLECTION is True, also starts observer + 60Hz logger
    for the same duration. Both stop together when trading stops.
    """
    restart_configs["contrarian"] = config.dict()
    data_collection_manager = None
    data_collection_task = None

    try:
        from scripts.run_paper_bot import PaperTradingBot

        start_dt_utc = normalize_datetime_to_utc(config.start_datetime)
        end_dt_utc = normalize_datetime_to_utc(config.end_datetime)

        logger.info(f"[contrarian] Session time window (UTC): {start_dt_utc.isoformat()} to {end_dt_utc.isoformat()}")

        now_utc = datetime.now(timezone.utc)
        if start_dt_utc > now_utc:
            wait_seconds = (start_dt_utc - now_utc).total_seconds()
            strategy.status["waiting_until"] = start_dt_utc.isoformat()
            await broadcast_status()
            await asyncio.sleep(wait_seconds)

        strategy.status.pop("waiting_until", None)
        strategy.status["trading_started"] = datetime.now(timezone.utc).isoformat()
        strategy.status["end_datetime"] = end_dt_utc.isoformat()
        await broadcast_status()

        duration_minutes = (end_dt_utc - start_dt_utc).total_seconds() / 60.0
        duration_hours = duration_minutes / 60.0
        web_callback = create_web_callback_for_strategy("contrarian")

        # AUTO DATA COLLECTION (Feb 5, 2026)
        if ENABLE_AUTO_DATA_COLLECTION and config.mode == "paper":
            try:
                from scripts.run_data_collection import DataCollectionManager
                data_collection_manager = DataCollectionManager(
                    output_dir="research",
                    auto_restart=True
                )
                logger.info(f"[contrarian] Starting auto data collection for {duration_hours:.2f} hours")
                data_collection_task = asyncio.create_task(
                    data_collection_manager.run(duration_hours=duration_hours)
                )
            except Exception as e:
                logger.warning(f"[contrarian] Failed to start data collection: {e}")
                data_collection_manager = None
                data_collection_task = None

        bot = PaperTradingBot.from_contrarian_config(
            config.dict(),
            web_callback=web_callback,
            session_start_utc=start_dt_utc,
            session_end_utc=end_dt_utc,
            trading_mode=config.mode,
        )
        strategy.instance = bot

        logger.info("[contrarian] Initializing bot...")
        await bot.initialize()

        logger.info(f"[contrarian] Starting trading loop for {duration_minutes:.1f} minutes")
        await bot.run(duration_minutes=duration_minutes)

        strategy.status["running"] = False
        strategy.status["completed"] = True
        strategy.reset_trading_data()
        strategy.instance = None
        restart_configs.pop("contrarian", None)
        logger.info("[contrarian] Trading session completed normally")
        await broadcast_status()

    except asyncio.CancelledError:
        strategy.status["running"] = False
        strategy.status["error"] = "Stopped by user"
        strategy.reset_trading_data()
        strategy.instance = None
        restart_configs.pop("contrarian", None)
        logger.info("[contrarian] Stopped by user")
        await broadcast_status()
        raise
    except Exception as e:
        strategy.status["running"] = False
        strategy.status["error"] = str(e)
        strategy.reset_trading_data()
        strategy.instance = None
        logger.error(f"[contrarian] Error: {e}")
        logger.error(f"[contrarian] Traceback: {traceback.format_exc()}")
        await broadcast_status()
        restart_configs.pop("contrarian", None)
    finally:
        # ALWAYS stop data collection when trading stops (any reason)
        if data_collection_manager:
            logger.info("[contrarian] Stopping auto data collection...")
            data_collection_manager.stop()
        if data_collection_task and not data_collection_task.done():
            data_collection_task.cancel()
            try:
                await data_collection_task
            except asyncio.CancelledError:
                pass
            logger.info("[contrarian] Data collection stopped")


async def run_volume_weighted_bot(config: VolumeWeightedBotConfig, strategy: StrategyState):
    """Run the Volume Weighted (Gabagool-style) trading bot asynchronously.

    Grid maker with aggressive cheap accumulation and conservative hedging.
    """
    restart_configs["volume_weighted"] = config.dict()

    try:
        from scripts.run_paper_bot import PaperTradingBot

        start_dt_utc = normalize_datetime_to_utc(config.start_datetime)
        end_dt_utc = normalize_datetime_to_utc(config.end_datetime)

        logger.info(f"[volume_weighted] Session time window (UTC): {start_dt_utc.isoformat()} to {end_dt_utc.isoformat()}")

        now_utc = datetime.now(timezone.utc)
        if start_dt_utc > now_utc:
            wait_seconds = (start_dt_utc - now_utc).total_seconds()
            strategy.status["waiting_until"] = start_dt_utc.isoformat()
            await broadcast_status()
            await asyncio.sleep(wait_seconds)

        strategy.status.pop("waiting_until", None)
        strategy.status["trading_started"] = datetime.now(timezone.utc).isoformat()
        strategy.status["end_datetime"] = end_dt_utc.isoformat()
        await broadcast_status()

        duration_minutes = (end_dt_utc - start_dt_utc).total_seconds() / 60.0
        web_callback = create_web_callback_for_strategy("volume_weighted")

        bot = PaperTradingBot.from_volume_weighted_config(
            config.dict(),
            web_callback=web_callback,
            session_start_utc=start_dt_utc,
            session_end_utc=end_dt_utc,
            trading_mode=config.mode,
        )
        strategy.instance = bot

        logger.info("[volume_weighted] Initializing bot...")
        await bot.initialize()

        logger.info(f"[volume_weighted] Starting trading loop for {duration_minutes:.1f} minutes")
        await bot.run(duration_minutes=duration_minutes)

        strategy.status["running"] = False
        strategy.status["completed"] = True
        strategy.reset_trading_data()
        strategy.instance = None
        restart_configs.pop("volume_weighted", None)
        logger.info("[volume_weighted] Trading session completed normally")
        await broadcast_status()

    except asyncio.CancelledError:
        strategy.status["running"] = False
        strategy.status["error"] = "Stopped by user"
        strategy.reset_trading_data()
        strategy.instance = None
        restart_configs.pop("volume_weighted", None)
        logger.info("[volume_weighted] Stopped by user")
        await broadcast_status()
        raise
    except Exception as e:
        strategy.status["running"] = False
        strategy.status["error"] = str(e)
        strategy.reset_trading_data()
        strategy.instance = None
        logger.error(f"[volume_weighted] Error: {e}")
        logger.error(f"[volume_weighted] Traceback: {traceback.format_exc()}")
        await broadcast_status()
        restart_configs.pop("volume_weighted", None)


def create_web_callback_for_strategy(strategy_name: str):
    """Create a callback function for a specific strategy to send trading updates."""
    def callback(data: dict):
        """Callback that schedules async broadcast with strategy tag."""
        try:
            # Add strategy identifier to the data
            data["strategy"] = strategy_name
            loop = asyncio.get_running_loop()

            # Check if this is a trade event or a regular trading update
            if data.get("type") == "trade_event":
                loop.create_task(broadcast_trade_event(data))
            else:
                logger.debug(f"[WS] Scheduling broadcast for {strategy_name}: {data.get('type')}")
                loop.create_task(broadcast_trading_update(data))
        except RuntimeError as e:
            logger.warning(f"[WS] Failed to get event loop for {strategy_name}: {e}")
        except Exception as e:
            logger.error(f"[WS] Failed to broadcast trading update for {strategy_name}: {e}")
    return callback


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for real-time status updates."""
    await websocket.accept()
    connected_websockets.append(websocket)
    print(f"[WS] Client connected. Total clients: {len(connected_websockets)}")

    try:
        # Send current status for all strategies on connect
        await websocket.send_json({
            "type": "status",
            "aggressive": strategies["aggressive"].status,
            "contrarian": strategies["contrarian"].status,
            "volume_weighted": strategies["volume_weighted"].status,
        })

        # Keep connection alive
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        connected_websockets.remove(websocket)
        print(f"[WS] Client disconnected. Remaining: {len(connected_websockets)}")


async def broadcast_status():
    """Broadcast status to all connected WebSocket clients."""
    status_msg = {
        "type": "status",
        "aggressive": strategies["aggressive"].status,
        "contrarian": strategies["contrarian"].status,
        "volume_weighted": strategies["volume_weighted"].status,
    }
    for ws in connected_websockets[:]:
        try:
            await ws.send_json(status_msg)
        except Exception:
            connected_websockets.remove(ws)


async def broadcast_message(data: dict):
    """Broadcast a generic message to all connected WebSocket clients."""
    for ws in connected_websockets[:]:
        try:
            await ws.send_json(data)
        except Exception:
            connected_websockets.remove(ws)


async def broadcast_trading_update(data: dict):
    """Broadcast trading update to all connected WebSocket clients."""
    # Get strategy name from data (added by create_web_callback_for_strategy)
    strategy_name = data.get("strategy", "accumulation")

    # Update the appropriate strategy's status with latest trading data
    if data.get("type") == "trading_update" and strategy_name in strategies:
        strategy = strategies[strategy_name]
        metrics = data.get("metrics", {})
        if "balance" in metrics:
            strategy.status["balance"] = metrics["balance"]
        strategy.status["latest_trading_update"] = data

    # Log broadcast attempt with key data
    num_clients = len(connected_websockets)
    if num_clients > 0:
        pos = data.get("position", {})
        metrics = data.get("metrics", {})
        logger.info(f"[WS] Broadcasting {data.get('type')} for {data.get('strategy')} to {num_clients} client(s)")
        logger.info(f"[WS] Data: market={data.get('market_slug')}, up_qty={pos.get('up_qty')}, down_qty={pos.get('down_qty')}, balance={metrics.get('balance')}")

    for ws in connected_websockets[:]:
        try:
            await ws.send_json(data)
        except Exception as e:
            logger.warning(f"[WS] Failed to send to client: {e}")
            connected_websockets.remove(ws)


async def broadcast_trade_event(trade_data: dict):
    """Broadcast a trade event to the trade log on all connected clients."""
    msg = {
        "type": "trade_event",
        "strategy": trade_data.get("strategy", "standard"),
        "timestamp": trade_data.get("timestamp", datetime.now().strftime("%H:%M:%S")),
        "action": trade_data.get("action", "BUY"),
        "side": trade_data.get("side", "UP"),
        "size": trade_data.get("size", 0),
        "price": trade_data.get("price", 0.0),
        "position_after": trade_data.get("position_after", {"up": 0, "down": 0})
    }

    for ws in connected_websockets[:]:
        try:
            await ws.send_json(msg)
        except Exception:
            connected_websockets.remove(ws)


def create_web_callback():
    """Create a callback function for the bot to send trading updates."""
    def callback(data: dict):
        """Callback that schedules async broadcast."""
        try:
            # Get the running event loop and create task
            loop = asyncio.get_running_loop()
            loop.create_task(broadcast_trading_update(data))
        except RuntimeError:
            # No running loop - this shouldn't happen in our async context
            pass
        except Exception:
            pass  # Ignore other errors in callback

    return callback


async def run_bot_async(config: BotConfig):
    """Run the trading bot asynchronously."""
    global bot_status, bot_instance

    try:
        # Import here to avoid circular imports
        from scripts.run_paper_bot import PaperTradingBot

        # Calculate run duration
        start_dt = datetime.fromisoformat(config.start_datetime)
        end_dt = datetime.fromisoformat(config.end_datetime)

        # Wait until start time
        now = datetime.now()
        if start_dt > now:
            wait_seconds = (start_dt - now).total_seconds()
            bot_status["waiting_until"] = start_dt.isoformat()
            await broadcast_status()
            await asyncio.sleep(wait_seconds)

        # Clear waiting_until and mark trading as started
        bot_status.pop("waiting_until", None)
        bot_status["trading_started"] = datetime.now(timezone.utc).isoformat()
        bot_status["end_datetime"] = end_dt.isoformat()
        await broadcast_status()

        # Calculate duration in minutes
        duration_minutes = (end_dt - start_dt).total_seconds() / 60.0

        # Create web callback for trading updates
        web_callback = create_web_callback()

        # Create bot from web config with callback
        bot = PaperTradingBot.from_web_config(config.dict(), web_callback=web_callback)

        # Store bot instance for emergency operations
        bot_instance = bot

        # Initialize the bot (connects to API, sets up components)
        await bot.initialize()

        # Run the bot
        await bot.run(duration_minutes=duration_minutes)

        # Update status on completion
        bot_status["running"] = False
        bot_status["completed"] = True
        bot_instance = None
        await broadcast_status()

    except asyncio.CancelledError:
        bot_status["running"] = False
        bot_status["error"] = "Stopped by user"
        bot_instance = None
        await broadcast_status()
        raise
    except Exception as e:
        bot_status["running"] = False
        bot_status["error"] = str(e)
        bot_instance = None
        await broadcast_status()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
