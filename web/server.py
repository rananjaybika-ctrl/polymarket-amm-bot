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
import asyncio
import logging
import traceback
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, WebSocket, WebSocketDisconnect


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

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Polymarket Trading Bot", version="1.0.0")

# Serve static files
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class AccumulationBotConfig(BaseModel):
    """Configuration for Accumulation strategy from web UI."""
    mode: str  # "paper" or "live"
    market: str  # "btc-15m"
    start_datetime: str  # ISO format (local time from browser)
    end_datetime: str
    starting_balance: float = 100.0
    max_share_price: float = 0.98  # Never buy above this price (Gabagool buys up to $0.98)

    # Accumulation mode parameters
    accum_mode: str = "standard"  # "standard" or "volume_weighted"
    accum_trade_size: int = 1
    accum_target_shares: int = 15
    accum_max_imbalance_pct: float = 0.20  # Max imbalance as % of target (20% = 6 shares)
    accum_pair_cost_target: float = 0.995  # Target for normal trading (buy cheap)
    accum_pair_cost_limit: float = 1.02    # Max for rebalancing only
    accum_buy_both_sides: bool = True

    # Volume Weighted mode parameters - Gabagool-style (only used when accum_mode="volume_weighted")
    vw_imbalance_pct: float = 0.40       # Max 40% imbalance (gabagool avg: 39.6%)
    vw_cheap_threshold: float = 0.45     # Load up aggressively below this
    vw_hedge_trigger_pct: float = 0.30   # Start hedging when imbalance > 30%
    vw_max_hedge_price: float = 0.70     # Never pay > $0.70 for hedge
    vw_bootstrap_pct: float = 0.33       # Bootstrap phase: buy both sides until 33% of target


class DirectionalBotConfig(BaseModel):
    """Configuration for Directional strategy from web UI."""
    mode: str  # "paper" or "live"
    market: str  # "btc-15m"
    start_datetime: str  # ISO format (local time from browser)
    end_datetime: str
    starting_balance: float = 100.0
    initial_bias: str = "BULLISH"  # "BULLISH" or "BEARISH"

    # Flip detection parameters
    flip_cooldown_seconds: float = 180.0
    sigma_threshold: float = 2.0          # 2 sigma for flip detection
    sustained_seconds: float = 45.0
    window_seconds: int = 60
    max_flips_per_market: int = 2
    min_flip_time_remaining_secs: int = 420  # Don't flip with <7 min remaining

    # Position sizing
    max_position_pct: float = 0.17        # 17% of balance per side (final safety limit)
    target_shares: int = 15               # Target shares per side (first line of defense)
    trade_size_pct: float = 0.3333        # Each trade = 33.33% of max shares
    trade_size: int = 5                   # Fallback if not calculated
    hedge_increment: int = 5
    max_share_price: float = 0.98

    # Pricing thresholds
    attractive_price_early: float = 0.75
    attractive_price_late: float = 0.90
    dip_threshold_pct: float = 0.10

    # Hedging
    pair_cost_target: float = 0.95
    emergency_threshold_secs: int = 300
    emergency_max_price: float = 0.65


class CalculusMakerBotConfig(BaseModel):
    """Configuration for Calculus MAKER strategy from web UI.

    Uses exponential decay mispricing threshold and quadratic size ramp.
    Mathematical models:
        Mispricing: m(t) = m_min + (m_max - m_min) * e^(-lambda*(900-t))
        Inverted Size: size(t) = min_shares + (max_shares - min_shares) * (t/900)^2
    """
    mode: str  # "paper" or "live"
    market: str = "btc-15m"
    start_datetime: str  # ISO format (local time from browser)
    end_datetime: str
    starting_balance: float = 500.0

    # Calculus MAKER specific parameters
    max_shares: int = 50                  # Maximum order size per trade
    min_shares: int = 5                   # Minimum order size (Polymarket min = 5)
    m_min: float = 0.01                   # Late market threshold (accept 1% edge)
    m_max: float = 0.04                   # Early market threshold (require 4% edge)
    lambda_decay: float = 0.005           # Decay constant for mispricing threshold
    max_pair_cost: float = 0.995          # Maximum pair cost to accept
    max_imbalance_pct: float = 0.20       # Max imbalance as % of position (20% = 6 shares)
    max_share_price: float = 0.98         # Never buy above this price


    # Gradual Chase: Time-aware price chasing for unfilled orders
    # When True: chases in small steps based on time remaining (reduces slippage)
    # When False: jumps directly to ask price (faster fills, more expensive)
    # To disable gradual chase, set this to False
    gradual_chase_enabled: bool = True

    # Max Daily Loss: Stop trading if cumulative loss exceeds this amount
    # Set to 0 to disable the limit
    # When limit is hit, bot stops placing new orders but keeps existing positions
    max_daily_loss: float = 0.0


class SimpleHedgerBotConfig(BaseModel):
    """Configuration for Simple Hedger strategy from web UI.

    Simple hedging strategy:
    1. Wait 3s, buy expensive side
    2. Hedge at target pair cost
    3. On flip (+20c), double down and rehedge
    """
    mode: str  # "paper" or "live"
    market: str = "btc-15m"
    start_datetime: str
    end_datetime: str
    starting_balance: float = 200.0

    # Simple Hedger parameters
    size: int = 10                        # Shares per trade (10 ensures $1 min after flip)
    target_pair_cost: float = 0.97        # Target pair cost for normal hedge
    emergency_pair_cost: float = 0.98     # Target pair cost after flip
    flip_threshold: float = 0.20          # +20c triggers flip
    wait_seconds: float = 3.0             # Wait to see direction
    order_timeout: float = 30.0           # Seconds before cancel/retry


# Legacy config for backward compatibility
class BotConfig(BaseModel):
    """Configuration from web UI (legacy - use AccumulationBotConfig)."""
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


# Multi-strategy state - supports running multiple accumulation modes simultaneously
strategies = {
    "calculus_maker": StrategyState("calculus_maker"),  # Calculus MAKER (exponential decay + quadratic size)
    "simple_hedger": StrategyState("simple_hedger"),    # Simple Hedger (flip strategy)
    "volume_weighted": StrategyState("volume_weighted"),  # Volume Weighted (Gabagool-style) mode
    "directional": StrategyState("directional"),
    # Legacy - keep for backward compat but not shown in UI
    "standard": StrategyState("standard"),
    "accumulation": None,  # Will point to "standard" for backward compat
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
            if strategy_name == "directional":
                from pydantic import BaseModel
                # Create config object
                dir_config = DirectionalBotConfig(**config)
                strategy.status["running"] = True
                strategy.status["error"] = None
                strategy.status["restarted_at"] = now.isoformat()
                await broadcast_status()
                strategy.task = asyncio.create_task(run_directional_bot(dir_config, strategy))
            elif strategy_name == "calculus_maker":
                # Calculus MAKER strategy - use its own runner, NOT accumulation_bot
                calc_config = CalculusMakerBotConfig(**config)
                strategy.status["running"] = True
                strategy.status["error"] = None
                strategy.status["restarted_at"] = now.isoformat()
                await broadcast_status()
                strategy.task = asyncio.create_task(run_calculus_bot(calc_config, strategy))
            else:
                # Accumulation strategies (standard, volume_weighted) ONLY
                accum_config = AccumulationBotConfig(**config)
                strategy.status["running"] = True
                strategy.status["error"] = None
                strategy.status["restarted_at"] = now.isoformat()
                await broadcast_status()
                strategy.task = asyncio.create_task(run_accumulation_bot(accum_config, strategy))

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


@app.get("/")
async def root():
    """Serve the main HTML page."""
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/status")
async def get_status():
    """Get current bot status for all strategies."""
    return {
        "calculus_maker": strategies["calculus_maker"].status,
        "simple_hedger": strategies["simple_hedger"].status,
        "volume_weighted": strategies["volume_weighted"].status,
        "directional": strategies["directional"].status,
        # Legacy format for backward compatibility
        "standard": strategies["standard"].status,
        "accumulation": strategies["standard"].status,  # Alias to standard
        "running": (
            strategies["calculus_maker"].status["running"] or
            strategies["simple_hedger"].status["running"] or
            strategies["volume_weighted"].status["running"] or
            strategies["directional"].status["running"] or
            strategies["standard"].status["running"]
        ),
    }


@app.post("/api/validate")
async def validate_config(config: BotConfig):
    """Validate configuration without starting the bot."""
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
async def start_bot(config: BotConfig):
    """Start the trading bot with given configuration."""
    global bot_task, bot_status

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
async def stop_bot():
    """Stop the trading bot."""
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

    # Notify connected clients
    await broadcast_status()

    return {"status": "stopped"}


@app.post("/api/emergency-stop")
async def emergency_stop():
    """Emergency sell all positions and stop ALL bots (NUKE ALL)."""
    global bot_task, bot_status, bot_instance

    results = {
        "status": "emergency_stopped",
        "positions_closed": 0,
        "total_proceeds": 0.0,
        "total_cost": 0.0,
        "realized_pnl": 0.0,
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
                # Execute emergency sell
                sell_results = await strategy.instance.emergency_sell_all()
                results["positions_closed"] += sell_results.get("positions_closed", 0)
                results["total_proceeds"] += sell_results.get("total_proceeds", 0.0)
                results["total_cost"] += sell_results.get("total_cost", 0.0)
                results["realized_pnl"] += sell_results.get("realized_pnl", 0.0)
                results["details"].extend(sell_results.get("details", []))
                results["strategies_stopped"].append(strategy_name)
                logger.info(f"[NUKE-ALL] {strategy_name} emergency sell complete")
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

    # === ALSO STOP LEGACY BOT IF RUNNING ===
    if bot_instance:
        try:
            sell_results = await bot_instance.emergency_sell_all()
            results["positions_closed"] += sell_results.get("positions_closed", 0)
            results["total_proceeds"] += sell_results.get("total_proceeds", 0.0)
            results["details"].extend(sell_results.get("details", []))
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

    logger.info(f"[NUKE-ALL] Complete: stopped {len(results['strategies_stopped'])} strategies, closed {results['positions_closed']} positions")
    return results


# =============================================================================
# NEW DUAL-STRATEGY ENDPOINTS
# =============================================================================

@app.post("/api/start/accumulation")
async def start_accumulation(config: AccumulationBotConfig):
    """Start an Accumulation trading strategy (standard or volume_weighted based on config)."""
    # Determine which strategy slot to use based on accum_mode
    strategy_name = config.accum_mode  # "standard" or "volume_weighted"
    if strategy_name not in ["standard", "volume_weighted"]:
        strategy_name = "standard"

    strategy = strategies[strategy_name]

    # Check if actually running (task exists and not done) vs just stale status
    actually_running = (
        strategy.status["running"] and
        strategy.task is not None and
        not strategy.task.done()
    )

    if actually_running:
        return JSONResponse(
            status_code=400,
            content={"error": f"{strategy_name.title()} accumulation is already running"}
        )

    # Reset stale status if task is done but status wasn't updated
    if strategy.status["running"] and (strategy.task is None or strategy.task.done()):
        strategy.status["running"] = False
        strategy.reset_trading_data()  # Clear stale position data
        strategy.task = None

    # Validate datetime
    try:
        start_dt = datetime.fromisoformat(config.start_datetime)
        end_dt = datetime.fromisoformat(config.end_datetime)
        now = datetime.now()
        if end_dt <= start_dt:
            return JSONResponse(status_code=400, content={"error": "End time must be after start time"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": f"Invalid datetime: {e}"})

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
        "strategy": strategy_name,
        "accum_mode": config.accum_mode,
        "error": None,
        "config": config.dict(),
        "start_time": datetime.now(timezone.utc).isoformat(),
        "balance": config.starting_balance,
    }

    # Start bot in background
    strategy.task = asyncio.create_task(run_accumulation_bot(config, strategy))

    await broadcast_status()
    return {"status": "started", "strategy": strategy_name, "accum_mode": config.accum_mode, "config": config.dict()}


@app.post("/api/start/standard")
async def start_standard(config: AccumulationBotConfig):
    """Start Standard accumulation mode."""
    config.accum_mode = "standard"
    return await start_accumulation(config)


@app.post("/api/start/volume_weighted")
async def start_volume_weighted(config: AccumulationBotConfig):
    """Start Volume Weighted (Gabagool-style) accumulation mode."""
    config.accum_mode = "volume_weighted"
    return await start_accumulation(config)


@app.post("/api/start/directional")
async def start_directional(config: DirectionalBotConfig):
    """Start the Directional trading strategy."""
    strategy = strategies["directional"]

    # Check if actually running (task exists and not done) vs just stale status
    actually_running = (
        strategy.status["running"] and
        strategy.task is not None and
        not strategy.task.done()
    )

    if actually_running:
        return JSONResponse(
            status_code=400,
            content={"error": "Directional strategy is already running"}
        )

    # Reset stale status if task is done but status wasn't updated
    if strategy.status["running"] and (strategy.task is None or strategy.task.done()):
        strategy.status["running"] = False
        strategy.reset_trading_data()  # Clear stale position data
        strategy.task = None

    # Validate datetime
    try:
        start_dt = datetime.fromisoformat(config.start_datetime)
        end_dt = datetime.fromisoformat(config.end_datetime)
        if end_dt <= start_dt:
            return JSONResponse(status_code=400, content={"error": "End time must be after start time"})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": f"Invalid datetime: {e}"})

    # Validate bias
    if config.initial_bias not in ["BULLISH", "BEARISH"]:
        return JSONResponse(status_code=400, content={"error": "Initial bias must be BULLISH or BEARISH"})

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
        "strategy": "directional",
        "error": None,
        "config": config.dict(),
        "start_time": datetime.now(timezone.utc).isoformat(),
        "balance": config.starting_balance,
    }

    # Start bot in background
    strategy.task = asyncio.create_task(run_directional_bot(config, strategy))

    await broadcast_status()
    return {"status": "started", "strategy": "directional", "config": config.dict()}


@app.post("/api/start/calculus_maker")
async def start_calculus_maker(config: CalculusMakerBotConfig):
    """Start the Calculus MAKER trading strategy."""
    strategy = strategies["calculus_maker"]

    # Check if actually running (task exists and not done) vs just stale status
    actually_running = (
        strategy.status["running"] and
        strategy.task is not None and
        not strategy.task.done()
    )

    if actually_running:
        return JSONResponse(
            status_code=400,
            content={"error": "Calculus MAKER strategy is already running"}
        )

    # Reset stale status if task is done but status wasn't updated
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

    # Validate Calculus MAKER parameters
    if config.min_shares < 5:
        return JSONResponse(status_code=400, content={"error": "Min shares must be at least 5 (Polymarket minimum)"})
    if config.max_shares < config.min_shares:
        return JSONResponse(status_code=400, content={"error": "Max shares must be >= min shares"})
    if config.m_min >= config.m_max:
        return JSONResponse(status_code=400, content={"error": "m_min must be less than m_max"})

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
        "strategy": "calculus_maker",
        "error": None,
        "config": config.dict(),
        "start_time": datetime.now(timezone.utc).isoformat(),
        "balance": config.starting_balance,
    }

    # Start bot in background
    strategy.task = asyncio.create_task(run_calculus_bot(config, strategy))

    await broadcast_status()
    return {"status": "started", "strategy": "calculus_maker", "config": config.dict()}


@app.post("/api/start/simple_hedger")
async def start_simple_hedger(config: SimpleHedgerBotConfig):
    """Start the Simple Hedger trading strategy."""
    strategy = strategies["simple_hedger"]

    # Check if actually running
    actually_running = (
        strategy.status["running"] and
        strategy.task is not None and
        not strategy.task.done()
    )

    if actually_running:
        return JSONResponse(
            status_code=400,
            content={"error": "Simple Hedger strategy is already running"}
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

    # Validate Simple Hedger parameters
    if config.size < 5:
        return JSONResponse(status_code=400, content={"error": "Size must be at least 5 (Polymarket minimum)"})
    if config.target_pair_cost >= 1.0:
        return JSONResponse(status_code=400, content={"error": "Target pair cost must be < 1.0"})

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
        "strategy": "simple_hedger",
        "error": None,
        "config": config.dict(),
        "start_time": datetime.now(timezone.utc).isoformat(),
        "balance": config.starting_balance,
    }

    # Start bot in background
    strategy.task = asyncio.create_task(run_simple_hedger_bot(config, strategy))

    await broadcast_status()
    return {"status": "started", "strategy": "simple_hedger", "config": config.dict()}


@app.post("/api/stop/{strategy_name}")
async def stop_strategy(strategy_name: str):
    """Gracefully stop a strategy - cancels all open orders first, then stops."""
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

    await broadcast_status()
    return {"status": "stopping", "cancelled_orders": cancelled_orders, "message": "Cancelled open orders and stopping (auto-restart disabled)"}


@app.post("/api/graceful-stop/{strategy_name}")
async def graceful_stop_strategy(strategy_name: str):
    """Request graceful stop - cancels open orders, then stops after current market ends."""
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
        return {"success": True, "cancelled_orders": cancelled_orders, "message": f"Cancelled {cancelled_orders} orders, graceful stop requested for {strategy_name}"}
    else:
        return JSONResponse(status_code=400, content={"error": "Strategy does not support graceful stop", "success": False})


@app.post("/api/emergency-stop/{strategy_name}")
async def emergency_stop_strategy(strategy_name: str):
    """Emergency sell all positions and stop a specific strategy."""
    try:
        if strategy_name not in strategies:
            return JSONResponse(status_code=404, content={"error": f"Unknown strategy: {strategy_name}"})

        strategy = strategies[strategy_name]

        results = {
            "status": "emergency_stopped",
            "strategy": strategy_name,
            "positions_closed": 0,
            "total_proceeds": 0.0,
            "total_cost": 0.0,
            "realized_pnl": 0.0,
            "details": []
        }

        if strategy.instance:
            try:
                sell_results = await strategy.instance.emergency_sell_all()
                results.update(sell_results)
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
    accum_mode = config.accum_mode  # "standard" or "volume_weighted"

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
        # Don't remove from restart_configs - health monitor may restart on crash


async def run_directional_bot(config: DirectionalBotConfig, strategy: StrategyState):
    """Run the Directional trading bot asynchronously with resilience."""
    # Store config for auto-restart
    restart_configs["directional"] = config.dict()

    try:
        from scripts.run_paper_bot import PaperTradingBot

        # CRITICAL: Normalize times to UTC at the API boundary
        # Frontend sends local time (IST) - convert to UTC for consistent handling
        start_dt_utc = normalize_datetime_to_utc(config.start_datetime)
        end_dt_utc = normalize_datetime_to_utc(config.end_datetime)

        logger.info(f"[directional] Session time window (UTC): {start_dt_utc.isoformat()} to {end_dt_utc.isoformat()}")

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
        web_callback = create_web_callback_for_strategy("directional")

        # Create Directional bot with session time window
        bot = PaperTradingBot.from_directional_config(
            config.dict(),
            web_callback=web_callback,
            session_start_utc=start_dt_utc,
            session_end_utc=end_dt_utc,
            trading_mode=config.mode,
        )
        strategy.instance = bot

        logger.info("[directional] Initializing bot...")
        await bot.initialize()

        logger.info(f"[directional] Starting trading loop for {duration_minutes:.1f} minutes")
        await bot.run(duration_minutes=duration_minutes)

        strategy.status["running"] = False
        strategy.status["completed"] = True
        strategy.reset_trading_data()  # Clear stale position data
        strategy.instance = None
        # Clear restart config on normal completion (including graceful stop via Telegram)
        if "directional" in restart_configs:
            del restart_configs["directional"]
            logger.info("[directional] Cleared restart config - session completed normally")
        logger.info("[directional] Trading session completed normally")
        await broadcast_status()

    except asyncio.CancelledError:
        strategy.status["running"] = False
        strategy.status["error"] = "Stopped by user"
        strategy.reset_trading_data()  # Clear stale position data
        strategy.instance = None
        # Clear restart config on user cancellation
        if "directional" in restart_configs:
            del restart_configs["directional"]
            logger.info("[directional] Cleared restart config - stopped by user")
        logger.info("[directional] Stopped by user")
        await broadcast_status()
        raise
    except Exception as e:
        strategy.status["running"] = False
        strategy.status["error"] = str(e)
        strategy.reset_trading_data()  # Clear stale position data
        strategy.instance = None
        logger.error(f"[directional] Error: {e}")
        logger.error(f"[directional] Traceback: {traceback.format_exc()}")
        await broadcast_status()
        # Don't remove from restart_configs - health monitor may restart


async def run_calculus_bot(config: CalculusMakerBotConfig, strategy: StrategyState):
    """Run the Calculus MAKER trading bot asynchronously with resilience.

    Uses exponential decay mispricing threshold and quadratic size ramp.
    """
    # Store config for auto-restart
    restart_configs["calculus_maker"] = config.dict()

    try:
        from scripts.run_paper_bot import PaperTradingBot

        # CRITICAL: Normalize times to UTC at the API boundary
        start_dt_utc = normalize_datetime_to_utc(config.start_datetime)
        end_dt_utc = normalize_datetime_to_utc(config.end_datetime)

        logger.info(f"[calculus_maker] Session time window (UTC): {start_dt_utc.isoformat()} to {end_dt_utc.isoformat()}")

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
        web_callback = create_web_callback_for_strategy("calculus_maker")

        # Create Calculus MAKER bot with strategy-specific config
        # Pass the config as a dict with calculus_maker specific parameters
        bot = PaperTradingBot.from_calculus_config(
            config.dict(),
            web_callback=web_callback,
            session_start_utc=start_dt_utc,
            session_end_utc=end_dt_utc,
            trading_mode=config.mode,
        )
        strategy.instance = bot

        logger.info("[calculus_maker] Initializing bot...")
        await bot.initialize()

        logger.info(f"[calculus_maker] Starting trading loop for {duration_minutes:.1f} minutes")
        await bot.run(duration_minutes=duration_minutes)

        strategy.status["running"] = False
        strategy.status["completed"] = True
        strategy.reset_trading_data()
        strategy.instance = None
        # Clear restart config on normal completion
        if "calculus_maker" in restart_configs:
            del restart_configs["calculus_maker"]
            logger.info("[calculus_maker] Cleared restart config - session completed normally")
        logger.info("[calculus_maker] Trading session completed normally")
        await broadcast_status()

    except asyncio.CancelledError:
        strategy.status["running"] = False
        strategy.status["error"] = "Stopped by user"
        strategy.reset_trading_data()
        strategy.instance = None
        if "calculus_maker" in restart_configs:
            del restart_configs["calculus_maker"]
            logger.info("[calculus_maker] Cleared restart config - stopped by user")
        logger.info("[calculus_maker] Stopped by user")
        await broadcast_status()
        raise
    except Exception as e:
        strategy.status["running"] = False
        strategy.status["error"] = str(e)
        strategy.reset_trading_data()
        strategy.instance = None
        logger.error(f"[calculus_maker] Error: {e}")
        logger.error(f"[calculus_maker] Traceback: {traceback.format_exc()}")
        await broadcast_status()
        # Don't remove from restart_configs - health monitor may restart


async def run_simple_hedger_bot(config: SimpleHedgerBotConfig, strategy: StrategyState):
    """Run the Simple Hedger trading bot asynchronously.

    Uses the Simple Hedger strategy with flip logic.
    """
    # Store config for auto-restart
    restart_configs["simple_hedger"] = config.dict()

    try:
        from scripts.run_simple_hedger import SimpleHedgerBot

        # CRITICAL: Normalize times to UTC at the API boundary
        start_dt_utc = normalize_datetime_to_utc(config.start_datetime)
        end_dt_utc = normalize_datetime_to_utc(config.end_datetime)

        logger.info(f"[simple_hedger] Session time window (UTC): {start_dt_utc.isoformat()} to {end_dt_utc.isoformat()}")

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

        # Create Simple Hedger bot with session time window
        bot = SimpleHedgerBot(
            live_mode=(config.mode == "live"),
            initial_balance=config.starting_balance,
            session_start_utc=start_dt_utc,
            session_end_utc=end_dt_utc,
        )
        strategy.instance = bot

        logger.info("[simple_hedger] Initializing bot...")
        await bot.initialize()

        logger.info(f"[simple_hedger] Starting trading loop for {duration_minutes:.1f} minutes")
        await bot.run(duration_minutes=duration_minutes)

        strategy.status["running"] = False
        strategy.status["completed"] = True
        strategy.reset_trading_data()
        strategy.instance = None
        # Clear restart config on normal completion
        if "simple_hedger" in restart_configs:
            del restart_configs["simple_hedger"]
            logger.info("[simple_hedger] Cleared restart config - session completed normally")
        logger.info("[simple_hedger] Trading session completed normally")
        await broadcast_status()

    except asyncio.CancelledError:
        strategy.status["running"] = False
        strategy.status["error"] = "Stopped by user"
        strategy.reset_trading_data()
        strategy.instance = None
        if "simple_hedger" in restart_configs:
            del restart_configs["simple_hedger"]
            logger.info("[simple_hedger] Cleared restart config - stopped by user")
        logger.info("[simple_hedger] Stopped by user")
        await broadcast_status()
        raise
    except Exception as e:
        strategy.status["running"] = False
        strategy.status["error"] = str(e)
        strategy.reset_trading_data()
        strategy.instance = None
        logger.error(f"[simple_hedger] Error: {e}")
        logger.error(f"[simple_hedger] Traceback: {traceback.format_exc()}")
        await broadcast_status()


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
            "calculus_maker": strategies["calculus_maker"].status,
            "simple_hedger": strategies["simple_hedger"].status,
            "volume_weighted": strategies["volume_weighted"].status,
            "directional": strategies["directional"].status,
            "standard": strategies["standard"].status,
            "accumulation": strategies["standard"].status,  # Legacy alias
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
        "calculus_maker": strategies["calculus_maker"].status,
        "simple_hedger": strategies["simple_hedger"].status,
        "volume_weighted": strategies["volume_weighted"].status,
        "directional": strategies["directional"].status,
        "standard": strategies["standard"].status,
        "accumulation": strategies["standard"].status,  # Legacy alias
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
