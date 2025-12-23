"""
FastAPI Web Server for Polymarket Trading Bot
Run with: uvicorn web.server:app --reload --port 8000
"""

import sys
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

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
    accum_max_imbalance: int = 5
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
    max_position_pct: float = 0.15        # 15% of balance per side
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
    accum_max_imbalance: int = 5
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


# Multi-strategy state - supports running multiple accumulation modes simultaneously
strategies = {
    "standard": StrategyState("standard"),       # Standard accumulation mode
    "volume_weighted": StrategyState("volume_weighted"),  # Volume Weighted (Gabagool-style) mode
    "directional": StrategyState("directional"),
    # Legacy alias
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


@app.get("/")
async def root():
    """Serve the main HTML page."""
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/status")
async def get_status():
    """Get current bot status for all strategies."""
    return {
        "standard": strategies["standard"].status,
        "volume_weighted": strategies["volume_weighted"].status,
        "directional": strategies["directional"].status,
        # Legacy format for backward compatibility
        "accumulation": strategies["standard"].status,  # Alias to standard
        "running": (
            strategies["standard"].status["running"] or
            strategies["volume_weighted"].status["running"] or
            strategies["directional"].status["running"]
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
    if config.accum_max_imbalance < 1:
        errors.append("Accumulation max imbalance must be at least 1")
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

    # TODO: If mode is "live", check actual wallet balance
    if config.mode == "live":
        # Placeholder for live balance check
        # In production, query the actual wallet balance here
        pass

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

    # Notify connected clients
    await broadcast_status()

    return {"status": "stopped"}


@app.post("/api/emergency-stop")
async def emergency_stop():
    """Emergency sell all positions and stop the bot."""
    global bot_task, bot_status, bot_instance

    results = {
        "status": "emergency_stopped",
        "positions_closed": 0,
        "total_proceeds": 0.0,
        "total_cost": 0.0,
        "realized_pnl": 0.0,
        "details": []
    }

    # Execute emergency sell if bot is running
    if bot_instance:
        try:
            sell_results = await bot_instance.emergency_sell_all()
            results.update(sell_results)
        except Exception as e:
            results["error"] = str(e)

    # Cancel the bot task
    if bot_task and not bot_task.done():
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass

    # Update status
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


@app.post("/api/stop/{strategy_name}")
async def stop_strategy(strategy_name: str):
    """Stop a specific trading strategy."""
    if strategy_name not in strategies:
        return JSONResponse(status_code=404, content={"error": f"Unknown strategy: {strategy_name}"})

    strategy = strategies[strategy_name]

    if strategy.instance:
        strategy.instance.stop()

    if strategy.task and not strategy.task.done():
        strategy.task.cancel()
        try:
            await strategy.task
        except asyncio.CancelledError:
            pass

    strategy.status = {
        "running": False,
        "strategy": strategy_name,
        "error": None,
        "config": None,
        "start_time": None,
        "balance": None,
    }
    strategy.task = None
    strategy.instance = None

    await broadcast_status()
    return {"status": "stopped", "strategy": strategy_name}


@app.post("/api/graceful-stop/{strategy_name}")
async def graceful_stop_strategy(strategy_name: str):
    """Request graceful stop - bot will stop after current market ends."""
    if strategy_name not in strategies:
        return JSONResponse(status_code=404, content={"error": f"Unknown strategy: {strategy_name}"})

    strategy = strategies[strategy_name]

    if not strategy.instance:
        return JSONResponse(status_code=400, content={"error": "Strategy not running", "success": False})

    # Set graceful stop flag on the bot instance
    if hasattr(strategy.instance, 'graceful_stop'):
        strategy.instance.graceful_stop()
        return {"success": True, "message": f"Graceful stop requested for {strategy_name}"}
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
    """Run the Accumulation trading bot asynchronously."""
    try:
        from scripts.run_paper_bot import PaperTradingBot

        start_dt = datetime.fromisoformat(config.start_datetime)
        end_dt = datetime.fromisoformat(config.end_datetime)

        # Wait until start time
        now = datetime.now()
        if start_dt > now:
            wait_seconds = (start_dt - now).total_seconds()
            strategy.status["waiting_until"] = start_dt.isoformat()
            await broadcast_status()
            await asyncio.sleep(wait_seconds)

        strategy.status.pop("waiting_until", None)
        strategy.status["trading_started"] = datetime.now(timezone.utc).isoformat()
        strategy.status["end_datetime"] = end_dt.isoformat()
        await broadcast_status()

        duration_minutes = (end_dt - start_dt).total_seconds() / 60.0

        # Use accum_mode as strategy name for callbacks and CSV naming
        accum_mode = config.accum_mode  # "standard" or "volume_weighted"
        web_callback = create_web_callback_for_strategy(accum_mode)

        # Create Accumulation bot with mode-specific settings
        bot = PaperTradingBot.from_web_config(
            config.dict(),
            web_callback=web_callback,
            strategy_name=accum_mode  # Use mode name for CSV differentiation
        )
        strategy.instance = bot

        await bot.initialize()
        await bot.run(duration_minutes=duration_minutes)

        strategy.status["running"] = False
        strategy.status["completed"] = True
        strategy.instance = None
        await broadcast_status()

    except asyncio.CancelledError:
        strategy.status["running"] = False
        strategy.status["error"] = "Stopped by user"
        strategy.instance = None
        await broadcast_status()
        raise
    except Exception as e:
        strategy.status["running"] = False
        strategy.status["error"] = str(e)
        strategy.instance = None
        await broadcast_status()


async def run_directional_bot(config: DirectionalBotConfig, strategy: StrategyState):
    """Run the Directional trading bot asynchronously."""
    try:
        from scripts.run_paper_bot import PaperTradingBot

        start_dt = datetime.fromisoformat(config.start_datetime)
        end_dt = datetime.fromisoformat(config.end_datetime)

        # Wait until start time
        now = datetime.now()
        if start_dt > now:
            wait_seconds = (start_dt - now).total_seconds()
            strategy.status["waiting_until"] = start_dt.isoformat()
            await broadcast_status()
            await asyncio.sleep(wait_seconds)

        strategy.status.pop("waiting_until", None)
        strategy.status["trading_started"] = datetime.now(timezone.utc).isoformat()
        strategy.status["end_datetime"] = end_dt.isoformat()
        await broadcast_status()

        duration_minutes = (end_dt - start_dt).total_seconds() / 60.0
        web_callback = create_web_callback_for_strategy("directional")

        # Create Directional bot
        bot = PaperTradingBot.from_directional_config(config.dict(), web_callback=web_callback)
        strategy.instance = bot

        await bot.initialize()
        await bot.run(duration_minutes=duration_minutes)

        strategy.status["running"] = False
        strategy.status["completed"] = True
        strategy.instance = None
        await broadcast_status()

    except asyncio.CancelledError:
        strategy.status["running"] = False
        strategy.status["error"] = "Stopped by user"
        strategy.instance = None
        await broadcast_status()
        raise
    except Exception as e:
        strategy.status["running"] = False
        strategy.status["error"] = str(e)
        strategy.instance = None
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
                loop.create_task(broadcast_trading_update(data))
        except RuntimeError:
            pass
        except Exception:
            pass
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
            "standard": strategies["standard"].status,
            "volume_weighted": strategies["volume_weighted"].status,
            "directional": strategies["directional"].status,
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
        "standard": strategies["standard"].status,
        "volume_weighted": strategies["volume_weighted"].status,
        "directional": strategies["directional"].status,
        "accumulation": strategies["standard"].status,  # Legacy alias
    }
    for ws in connected_websockets[:]:
        try:
            await ws.send_json(status_msg)
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

    for ws in connected_websockets[:]:
        try:
            await ws.send_json(data)
        except Exception:
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
