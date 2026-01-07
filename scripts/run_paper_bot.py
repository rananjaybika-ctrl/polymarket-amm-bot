#!/usr/bin/env python3
"""
Standalone Paper Trading Bot

Runs paper trading with live market data for extended periods.
Logs all trades to CSV and sends Discord notifications.

Usage:
    # Run for 8 hours (480 minutes)
    python scripts/run_paper_bot.py --duration 480

    # Run in background
    nohup python scripts/run_paper_bot.py --duration 480 > paper_bot.log 2>&1 &

    # Run with custom settings
    python scripts/run_paper_bot.py --duration 60 --balance 500 --discord-interval 30
"""

import argparse
import asyncio
import csv
import logging
import math
import signal
import sys
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Deque, Callable, Dict, Any, Tuple
from collections import deque
import random as random_module
from zoneinfo import ZoneInfo  # Python 3.9+ for timezone handling

# Rich library for live terminal display
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.config import Config
from src.api.polymarket_client import PolymarketClient
from src.api.binance_client import BinanceClient
from src.utils.telegram_notifier import TelegramNotifier
from src.services.market_finder import MarketFinder
from src.services.market_rotator import MarketRotator
from src.services.state_persistence import StatePersistence, PersistedState
from src.services.pair_analyzer import PairAnalyzer, AsymmetricOpportunity
from src.services.paper_trading import PaperTradingEngine, SimulationConfig
from src.models.position import Position  # Unified position model for paper and live trading
from src.services.live_trading import LiveTradingEngine
from src.api.polymarket_client import PolymarketClient
from src.services.directional_strategy import (
    DirectionalTradingStrategy,
    DirectionalConfig,
    Bias,
    DirectionalPhase,
    TradeDecision,
)
from src.strategies.calculus_maker import (
    CalculusMakerStrategy,
    OneBuyStrategy,
    check_prospective_pair_cost,
    check_prospective_pair_cost_with_market,
    get_dynamic_target_shares,
    calculate_fair_value,
)
from src.services.trend_detector import TrendDetector, TrendState, TrendDirection
from src.api.websocket_client import UserWebSocketClient, OrderFill
from src.utils.market_detector import MarketTypeDetector
from src.services.health_monitor import get_health_monitor, HealthMonitor
from src.services.auto_redeemer import AutoRedeemer


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


# ==============================================================================
# SHARED STRIKE FILE - Ensures all strategies use same strike for resolution
# ==============================================================================
import json
import fcntl

SHARED_STRIKES_FILE = project_root / "shared_strikes.json"


def _read_shared_strikes() -> dict:
    """Read shared strikes from file with file locking."""
    if not SHARED_STRIKES_FILE.exists():
        return {}
    try:
        with open(SHARED_STRIKES_FILE, 'r') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            data = json.load(f)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return data
    except (json.JSONDecodeError, IOError):
        return {}


def _write_shared_strikes(data: dict) -> None:
    """Write shared strikes to file with file locking."""
    try:
        with open(SHARED_STRIKES_FILE, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            json.dump(data, f, indent=2)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except IOError as e:
        logger.error(f"Failed to write shared strikes: {e}")


def get_shared_strike(market_slug: str) -> Optional[float]:
    """Get shared strike price for a market (if set by another strategy)."""
    data = _read_shared_strikes()
    entry = data.get(market_slug)
    if entry:
        return entry.get("strike_price")
    return None


def set_shared_strike(market_slug: str, strike_price: float, source: str) -> None:
    """Set shared strike price for a market (first writer wins)."""
    data = _read_shared_strikes()
    if market_slug not in data:
        data[market_slug] = {
            "strike_price": strike_price,
            "source": source,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _write_shared_strikes(data)
        logger.info(f"[SHARED_STRIKE] Set {market_slug}: ${strike_price:,.2f} (source: {source})")
    else:
        existing = data[market_slug]["strike_price"]
        logger.debug(f"[SHARED_STRIKE] {market_slug} already set: ${existing:,.2f}")


def cleanup_old_strikes(max_age_hours: int = 24) -> None:
    """Remove old strike entries to prevent file from growing indefinitely."""
    data = _read_shared_strikes()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    cleaned = {}
    for slug, entry in data.items():
        try:
            ts = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
            if ts > cutoff:
                cleaned[slug] = entry
        except (KeyError, ValueError):
            pass  # Remove malformed entries
    if len(cleaned) < len(data):
        _write_shared_strikes(cleaned)
        logger.debug(f"[SHARED_STRIKE] Cleaned {len(data) - len(cleaned)} old entries")


def calculate_dynamic_trade_size(
    time_remaining_secs: float,
    max_target_shares: int,
    min_size: int = 1
) -> int:
    """
    Calculate trade size based on time remaining using two-phase decay.

    Phase 1 (>5min): Smooth sqrt decay from 20% to 10% of max_target_shares
    Phase 2 (<5min): Sharp quadratic decay from 10% to 2% of max_target_shares

    This mimics gabagool22's pattern of front-loading accumulation early
    in the market and reducing size as resolution approaches.

    Args:
        time_remaining_secs: Seconds until market resolution
        max_target_shares: Maximum target shares per side
        min_size: Minimum trade size floor

    Returns:
        Trade size as integer (at least min_size)

    Note:
        Currently hardcoded for 15-min markets (900s total, 300s sharp boundary).
        To generalize for 60-min markets: add market_duration_secs parameter,
        set sharp_boundary = T/3 (20min for 60min markets), and normalize:
        - Phase 1: (t - sharp_boundary) / (T - sharp_boundary)
        - Phase 2: t / sharp_boundary
        Same decay pattern, just scaled to longer timeframe.
    """
    t = max(0, min(time_remaining_secs, 900))  # Clamp to [0, 900]
    M = max_target_shares

    if t >= 300:  # Phase 1: >5 min remaining
        # Smooth sqrt decay: 20% at 15min → 10% at 5min
        percent = 0.10 + 0.10 * math.sqrt((t - 300) / 600)
    else:  # Phase 2: <5 min remaining
        # Sharp quadratic decay: 10% at 5min → 2% at 0min
        percent = 0.02 + 0.08 * ((t / 300) ** 2)

    size = int(percent * M)
    return max(min_size, size)


def get_patient_price(
    best_bid: float,
    best_ask: float,
    time_remaining_secs: float,
    is_emergency: bool = False
) -> float:
    """
    Calculate patient bid price based on time window.

    Graduated pricing relative to best_bid - gets more aggressive as time runs out.

    Time windows:
    - Early (10-15 min): best_bid - 0.03 (very patient)
    - Mid (5-10 min): best_bid - 0.02 (patient)
    - Late (2-5 min): best_bid - 0.01 (moderate)
    - Final (0-2 min): best_bid (competitive)
    - Emergency: best_ask (taker, immediate fill)

    Args:
        best_bid: Current best bid price
        best_ask: Current best ask price
        time_remaining_secs: Seconds until market resolution
        is_emergency: If True, use aggressive pricing (best_ask)

    Returns:
        Patient bid price (clamped to minimum 0.01)
    """
    if is_emergency:
        return best_ask

    if time_remaining_secs >= 600:  # 10-15 min (early)
        price = best_bid - 0.03
    elif time_remaining_secs >= 300:  # 5-10 min (mid)
        price = best_bid - 0.02
    elif time_remaining_secs >= 120:  # 2-5 min (late)
        price = best_bid - 0.01
    else:  # 0-2 min (final)
        price = best_bid

    return max(0.01, price)


def get_emergency_threshold(time_remaining_secs: float) -> int:
    """
    Get time-based emergency imbalance threshold.

    Early in market: Higher threshold (10) - let patient orders work
    Late in market:  Lower threshold (5)  - must hedge before resolution

    Args:
        time_remaining_secs: Seconds until market resolution

    Returns:
        Minimum imbalance (in shares) before emergency triggers
    """
    if time_remaining_secs > 420:  # > 7 minutes
        return 10  # Patient - let chased orders fill
    else:
        return 5   # Urgent - must hedge before resolution


def calculate_gradual_chase_price(
    original_price: float,
    current_bid: float,
    current_ask: float,
    time_remaining_secs: float,
    order_age_secs: float,
    max_chase_per_step: float = None,
    chase_count: int = 0,
    is_hedge_side: bool = False,
) -> tuple[float, bool, bool]:
    """
    Calculate gradual chase price for unfilled orders.

    V2: Improved with shorter waits, smaller steps, and chase exhaustion.

    Instead of jumping directly to market price, moves in controlled steps
    based on time remaining. After MAX_CHASE_ITERATIONS, stops chasing and
    leaves order at final price (emergency hedge handles severe imbalance).

    Chase Strategy by Time Remaining (V3 - patient, consistent):
    ┌─────────────────┬────────────────┬─────────────┬──────────────────┐
    │ Time Remaining  │ Wait Before    │ Step Size   │ Price Ceiling    │
    │                 │ First Chase    │ Per Step    │ (normal/hedge)   │
    ├─────────────────┼────────────────┼─────────────┼──────────────────┤
    │ >10 min         │ 60s            │ $0.02       │ $0.50 / $0.65    │
    │ 5-10 min        │ 30s            │ $0.02       │ $0.55 / $0.70    │
    │ 2-5 min         │ 15s            │ $0.02       │ $0.60 / $0.75    │
    │ <2 min          │ 10s            │ $0.03       │ $0.65 / $0.75    │
    └─────────────────┴────────────────┴─────────────┴──────────────────┘

    Args:
        original_price: The price of the pending order
        current_bid: Current best bid price
        current_ask: Current best ask price
        time_remaining_secs: Seconds until market resolution
        order_age_secs: How long the order has been pending
        max_chase_per_step: Override max step size (optional)
        chase_count: Number of chase iterations already performed
        is_hedge_side: True if this is the deficit side needing hedge (allows higher ceiling)

    Returns:
        Tuple of (new_price, should_chase, chase_exhausted):
        - new_price: The price to use (may equal original if no chase yet)
        - should_chase: True if price changed, False if staying put
        - chase_exhausted: True if max iterations reached, order should stay at final price
    """
    # Maximum chase iterations before we stop (leave order at final price)
    MAX_CHASE_ITERATIONS = 5

    # Check if chase exhausted first
    if chase_count >= MAX_CHASE_ITERATIONS:
        return original_price, False, True  # Stop chasing, leave order

    # Determine chase parameters based on time remaining (V3 - patient, consistent steps)
    if time_remaining_secs >= 600:  # >10 min: very patient
        wait_before_chase = 60.0    # Wait 60s before first chase
        step_size = 0.02            # $0.02 per chase
        max_chase_price = 0.50      # Price ceiling
    elif time_remaining_secs >= 300:  # 5-10 min
        wait_before_chase = 30.0    # Wait 30s before first chase
        step_size = 0.02            # $0.02 per chase
        max_chase_price = 0.55
    elif time_remaining_secs >= 120:  # 2-5 min
        wait_before_chase = 15.0    # Wait 15s before first chase
        step_size = 0.02            # $0.02 per chase
        max_chase_price = 0.60
    else:  # <2 min: more urgent
        wait_before_chase = 10.0    # Wait 10s before first chase
        step_size = 0.03            # $0.03 per chase (slightly faster)
        max_chase_price = 0.65

    # Hedge side gets higher ceiling (+$0.15, max $0.75)
    if is_hedge_side:
        max_chase_price = min(max_chase_price + 0.15, 0.75)

    # Override step size if provided
    if max_chase_per_step is not None:
        step_size = max_chase_per_step

    # Check if we should chase yet (order must be old enough)
    if order_age_secs < wait_before_chase:
        return original_price, False, False

    # Calculate new price based on chase count (incremental)
    # Each chase adds one step_size to original price
    new_price = original_price + (chase_count + 1) * step_size

    # Apply ceilings
    new_price = min(new_price, max_chase_price)      # Price ceiling
    new_price = min(new_price, current_ask - 0.01)   # Stay below ask
    new_price = min(new_price, 0.98)                 # Hard cap

    # Only chase if the new price is actually higher
    if new_price <= original_price:
        return original_price, False, False

    return round(new_price, 4), True, False


def should_enter_at_open(
    up_price: float,
    down_price: float,
    time_elapsed: float,
    balance_min: float = 0.35,
    balance_max: float = 0.65,
    gate_duration: float = 60.0,
) -> Tuple[bool, str]:
    """
    Market Open Gate: Wait for balanced prices before first entry.

    Based on Gabagool/Baguette research (Jan 6, 2026):
    - They enter EARLY (15-113s) when prices are BALANCED (~50/50)
    - They WAIT (223-889s) when prices are TRENDING (lopsided)

    This gate only applies for the first 60s before TrendDetector has
    enough Binance price history to calculate meaningful z-scores.

    Args:
        up_price: Current UP share price
        down_price: Current DOWN share price
        time_elapsed: Seconds since market opened
        balance_min: Minimum price for "balanced" (default 0.35)
        balance_max: Maximum price for "balanced" (default 0.65)
        gate_duration: How long gate is active (default 60s)

    Returns:
        (can_enter, reason): Whether to enter and explanation
    """
    # After gate_duration, TrendDetector has data - skip this gate
    if time_elapsed >= gate_duration:
        return True, "TrendDetector active (60s+ elapsed)"

    # First 60s: require balanced prices (like Gabagool)
    up_balanced = balance_min <= up_price <= balance_max
    down_balanced = balance_min <= down_price <= balance_max

    if up_balanced and down_balanced:
        return True, f"BALANCED (UP=${up_price:.2f}, DOWN=${down_price:.2f}) - enter now"

    # Prices are lopsided - wait
    return False, (
        f"TRENDING at open (UP=${up_price:.2f}, DOWN=${down_price:.2f}) - "
        f"wait for balanced prices or TrendDetector ({gate_duration - time_elapsed:.0f}s remaining)"
    )


class PaperTradingBot:
    """
    Standalone paper trading bot with CSV logging and Discord notifications.
    """

    def __init__(
        self,
        initial_balance: float = 100.0,
        csv_path: str = "paper_trades.csv",
        discord_interval_minutes: float = 30.0,
        max_retries: int = 5,
        retry_base_delay: float = 2.0,
        retry_max_delay: float = 60.0,
        # Terminal display
        live_display: bool = False,
        # ACCUMULATION MODE - High frequency trading
        accum_trade_size: int = 1,  # Shares per trade (small, frequent)
        accum_pair_cost_target: float = 0.995,  # Target pair cost for normal trading
        accum_pair_cost_limit: float = 1.02,  # Max pair cost for rebalancing
        accum_max_imbalance_pct: float = 0.20,  # Max imbalance as % of target (20% = 6 shares)
        hard_max_imbalance: int = 10,  # HARD LIMIT: Stop ALL trading if imbalance >= this
        accum_target_shares: int = 50,  # Target shares per side per market (capped by max_position_pct)
        accum_buy_both_sides: bool = True,  # Try to buy both sides each cycle
        max_position_pct: float = 0.17,  # Max shares per side as % of balance (17% of $100 = 17 shares)
        accum_max_share_price: float = 0.98,  # Never buy shares above this price (Gabagool buys up to $0.98)
        # VOLUME WEIGHTED MODE - Gabagool-style accumulation
        accum_mode: str = "standard",  # "standard" or "volume_weighted"
        # GABAGOOL-STYLE SETTINGS (reverse-engineered from their Dec 2024 behavior)
        vw_imbalance_pct: float = 0.20,  # Max 20% imbalance (gabagool: 10-20%)
        vw_cheap_threshold: float = 0.45,  # Buy aggressively below this (gabagool loads up < $0.45)
        vw_hedge_trigger_pct: float = 0.15,  # Start hedging at 15% imbalance (gabagool: ~15-20%)
        vw_max_hedge_price: float = 0.85,  # Max hedge price $0.85 (gabagool: up to $0.87)
        vw_bootstrap_pct: float = 0.33,  # Bootstrap phase: buy both sides until 33% of target (gabagool: ~1.5% but we need more for smaller positions)
        # CALCULUS MAKER MODE - Exponential decay pricing with quadratic size ramp
        calc_m_min: float = 0.005,  # Late threshold: need 0.5% edge (pair_cost < $0.995)
        calc_m_max: float = 0.025,  # Early threshold: need 2.5% edge (pair_cost < $0.975)
        calc_lambda: float = 0.004,  # Decay constant (higher = faster decay to m_min)
        calc_max_shares: int = 50,  # Max order size
        calc_min_shares: int = 5,  # Min order size (Polymarket min)
        calc_max_pair_cost: float = 0.995,  # Max pair cost to accept
        # ENHANCED ONE BUY MODE - Single order per side, no chasing, no emergency mode
        one_buy_threshold: float = 0.47,  # Buy when price <= this (default $0.47)
        one_buy_size: int = 15,  # Target size per side (default 15 shares)
        one_buy_dynamic_threshold: bool = False,  # Use time-based threshold like calculus
        one_buy_max_pair_cost: float = 0.98,  # Max pair cost for hedging (skip if too expensive)
        # FAIR VALUE MM MODE - Price based on Binance signal, like real MMs
        fv_edge: float = 0.02,  # Edge below fair value (2 cents)
        fv_sensitivity_early: float = 0.10,  # Price sensitivity at market open
        fv_sensitivity_late: float = 0.50,  # Price sensitivity near resolution
        fv_reprice_threshold: float = 0.03,  # Reprice if fair value changes 3c
        # GRADUAL CHASE - Time-aware price chasing for unfilled orders
        # Set to False to revert to instant chase (jump to ask immediately)
        # When True: chases in small steps based on time remaining
        # See calculate_gradual_chase_price() for step sizes and wait times
        gradual_chase_enabled: bool = True,  # FEATURE FLAG: Set False to disable
        # SEQUENTIAL ORDERING - Place expensive side first, wait for fill before placing cheap side
        # Set to False to revert to parallel ordering (place both sides simultaneously)
        # When True: prevents asymmetric fills but may reduce total volume
        # Risk: expensive side may stall, blocking all accumulation
        # NOTE: Changed to True after 30/10 disaster - parallel mode caused asymmetric fills
        sequential_ordering_enabled: bool = True,  # ENABLED - prevents asymmetric fills
        # MAX DAILY LOSS - Stop trading if cumulative loss exceeds this amount
        # Set to 0 to disable the limit
        # When limit is hit, bot stops placing new orders but keeps existing positions
        max_daily_loss: float = 10.0,  # Stop trading if cumulative loss exceeds $10
        # DIRECTIONAL MODE - Bias-based trading with Binance price feed
        directional_mode: bool = False,
        initial_bias: Optional[str] = None,  # "BULLISH" or "BEARISH"
        directional_config: Optional[DirectionalConfig] = None,
        # Web UI callback
        web_callback: Optional[Callable[[dict], None]] = None,
        # Strategy name for Discord and web UI
        strategy_name: str = "accumulation",
        # Trading mode: "paper" or "live"
        trading_mode: str = "paper",
        # Quiet mode: suppress per-second status logs
        quiet_mode: bool = False,
        # Session time window (UTC) - only trade markets ending within this window
        session_start_utc: Optional[datetime] = None,
        session_end_utc: Optional[datetime] = None,
    ):
        self.initial_balance = initial_balance
        self.trading_mode = trading_mode
        self.quiet_mode = quiet_mode

        # CRITICAL: Session time window for market selection enforcement
        self.session_start_utc = session_start_utc
        self.session_end_utc = session_end_utc
        if session_start_utc and session_end_utc:
            logger.info(
                f"Session time window configured: "
                f"{session_start_utc.isoformat()} to {session_end_utc.isoformat()}"
            )
        # Daily CSV rotation: extract base name and add date
        self._csv_base_name = Path(csv_path).stem  # e.g., "paper_trades_directional"
        self._csv_dir = Path(csv_path).parent or Path(".")
        self._csv_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.csv_path = self._csv_dir / f"{self._csv_base_name}_{self._csv_date}.csv"
        self.discord_interval = timedelta(minutes=discord_interval_minutes)

        # Retry configuration for network resilience
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay
        self._consecutive_failures = 0

        # Accumulation mode parameters
        self.accum_trade_size = accum_trade_size
        self.accum_pair_cost_target = accum_pair_cost_target
        self.accum_pair_cost_limit = accum_pair_cost_limit
        self.accum_max_imbalance_pct = accum_max_imbalance_pct
        self.hard_max_imbalance = hard_max_imbalance  # HARD LIMIT: Stop ALL trading if imbalance >= this
        self.max_position_pct = max_position_pct
        # Calculate max allowed shares from max_position_pct (15% of $100 = 15 shares)
        max_shares_from_pct = int(max_position_pct * initial_balance)
        # Use the minimum of explicit target and calculated max
        self.accum_target_shares = min(accum_target_shares, max_shares_from_pct)
        self.accum_buy_both_sides = accum_buy_both_sides
        self.accum_max_share_price = accum_max_share_price

        # Volume Weighted mode parameters
        self.accum_mode = accum_mode
        self.vw_imbalance_pct = vw_imbalance_pct
        self.vw_cheap_threshold = vw_cheap_threshold
        self.vw_hedge_trigger_pct = vw_hedge_trigger_pct
        self.vw_max_hedge_price = vw_max_hedge_price
        self.vw_bootstrap_pct = vw_bootstrap_pct

        # Calculus MAKER mode parameters
        self.calc_m_min = calc_m_min
        self.calc_m_max = calc_m_max
        self.calc_lambda = calc_lambda
        self.calc_max_shares = calc_max_shares
        self.calc_min_shares = calc_min_shares
        self.calc_max_pair_cost = calc_max_pair_cost
        self.gradual_chase_enabled = gradual_chase_enabled
        self.sequential_ordering_enabled = sequential_ordering_enabled

        # ONE BUY mode parameters
        self.one_buy_threshold = one_buy_threshold
        self.one_buy_size = one_buy_size
        self.one_buy_dynamic_threshold = one_buy_dynamic_threshold
        self.one_buy_max_pair_cost = one_buy_max_pair_cost

        # FAIR VALUE MM mode parameters
        self.fv_edge = fv_edge
        self.fv_sensitivity_early = fv_sensitivity_early
        self.fv_sensitivity_late = fv_sensitivity_late
        self.fv_reprice_threshold = fv_reprice_threshold
        # Track fair value when order was placed for repricing
        self._order_fair_value: dict[str, tuple[float, float]] = {}  # "market_slug_SIDE" -> (fair_up, fair_down)

        # Track order replacements per side for chase count
        self._replacement_count: dict[str, int] = {}  # "market_slug_SIDE" -> count
        self._chase_exhausted_logged: set[str] = set()  # Track which keys already logged

        # Emergency cooldown tracking (30s between emergency orders per market)
        self._last_emergency_time: dict[str, float] = {}  # "market_slug" -> timestamp

        # Emergency ceiling tracking - reprice when ceiling changes (e.g., $0.75 → $0.88)
        self._emergency_ceiling_used: dict[str, float] = {}  # "market_slug_SIDE" -> ceiling when placed

        # Post-pull stabilization: track sides that were pulled, wait for z < 1.0 before re-entering
        self._pull_cooldown: dict[str, float] = {}  # "market_slug_SIDE" -> time when pulled

        # Market type detection for adaptive parameters
        self._market_detector: Optional[MarketTypeDetector] = None
        self._detected_market_type: str = "UNKNOWN"

        # MAX DAILY LOSS PROTECTION
        self.max_daily_loss = max_daily_loss
        self.cumulative_pnl = 0.0  # Track cumulative P&L across sessions
        self.loss_limit_reached = False  # Flag to stop trading when limit hit
        if self.max_daily_loss > 0:
            logger.info(f"Max daily loss protection enabled: ${self.max_daily_loss:.2f}")

        # Directional mode parameters
        self.directional_mode = directional_mode
        self.initial_bias = Bias[initial_bias] if initial_bias else None
        self.directional_config = directional_config or DirectionalConfig()

        # Strategy name for Discord and web UI
        self.strategy_name = strategy_name

        # Components
        self._config: Optional[Config] = None
        self._client: Optional[PolymarketClient] = None
        self._finder: Optional[MarketFinder] = None
        self._rotator: Optional[MarketRotator] = None
        self._analyzer: Optional[PairAnalyzer] = None
        self._engine: Optional[PaperTradingEngine | LiveTradingEngine] = None

        # Directional mode components
        self._binance_client: Optional[BinanceClient] = None
        self._directional_strategy: Optional[DirectionalTradingStrategy] = None
        self._is_new_market: bool = True

        # Trend detection for quote pulling and direction-aware trading
        self._trend_detector: Optional[TrendDetector] = None

        # Event-driven quote pull tracking (100-200ms reaction time)
        self._event_pull_market: Optional[str] = None  # Current market for event callbacks
        self._event_pull_callback: Optional[Callable] = None  # Stored callback reference

        # User WebSocket for instant fill notifications (replaces 2-second polling)
        self._user_ws: Optional[UserWebSocketClient] = None
        self._user_ws_task: Optional[asyncio.Task] = None
        self._ws_fill_queue: asyncio.Queue = asyncio.Queue()  # For async fill notifications

        # HARD STOP log throttling (once per 60s to avoid log spam)
        self._last_hard_stop_log: float = 0.0

        # Calculus MAKER strategy instance (also used by fair_value_mm mode)
        self._calculus_strategy: Optional[CalculusMakerStrategy] = None
        if self.accum_mode in ("calculus_maker", "fair_value_mm"):
            self._calculus_strategy = CalculusMakerStrategy(
                max_shares=self.calc_max_shares,
                min_shares=self.calc_min_shares,
                max_pair_cost=self.calc_max_pair_cost,
                m_min=self.calc_m_min,
                m_max=self.calc_m_max,
                lambda_decay=self.calc_lambda,
            )
            logger.info(f"[CALCULUS] Sizing mode: NORMAL (5 early → 15 late)")

        # ONE BUY strategy instance (Enhanced: no chasing, no emergency mode)
        self._one_buy_strategy: Optional[OneBuyStrategy] = None
        if self.accum_mode == "one_buy":
            self._one_buy_strategy = OneBuyStrategy(
                threshold=self.one_buy_threshold,
                size=self.one_buy_size,
                dynamic_threshold=self.one_buy_dynamic_threshold,
                max_pair_cost=self.one_buy_max_pair_cost,
            )
            threshold_mode = "dynamic" if self.one_buy_dynamic_threshold else f"fixed@${self.one_buy_threshold}"
            logger.info(f"[ONE_BUY] Enhanced strategy: {threshold_mode}, size={self.one_buy_size}, max_pair=${self.one_buy_max_pair_cost}")

        # Telegram notifications and remote control
        self._telegram: Optional[TelegramNotifier] = None

        # State persistence for crash recovery
        self._state_persistence: Optional[StatePersistence] = None
        self._persisted_state: Optional[PersistedState] = None

        # Auto-redemption for winning positions (Gnosis Safe only)
        self._auto_redeemer: Optional[AutoRedeemer] = None

        # Auto-merge state (merge pairs near market end)
        self._merged_this_market: bool = False  # Prevent multiple merges per market

        # State
        self._running = False
        self._graceful_stop_requested = False  # Stop after current market ends
        self._start_time: Optional[datetime] = None
        self._last_discord_update: Optional[datetime] = None
        self._trade_count = 0
        self._total_pairs = 0

        # Stats
        self._opportunities_checked = 0
        self._profitable_opportunities = 0

        # Live terminal display
        self.live_display_enabled = live_display
        self._console = Console() if live_display else None
        self._live: Optional[Live] = None
        self._last_up_price: float = 0.0
        self._last_down_price: float = 0.0
        self._last_spread: float = 0.0

        # Web UI callback
        self._web_callback = web_callback

    async def _interruptible_sleep(self, total_seconds: float, check_interval: float = 5.0) -> bool:
        """Sleep that can be interrupted by stop signal.

        Args:
            total_seconds: Total time to sleep
            check_interval: How often to check the stop signal

        Returns:
            True if sleep completed normally, False if interrupted by stop
        """
        elapsed = 0.0
        while elapsed < total_seconds and self._running:
            sleep_time = min(check_interval, total_seconds - elapsed)
            await asyncio.sleep(sleep_time)
            elapsed += sleep_time
        return self._running

    async def _maybe_auto_merge(self, market, time_remaining_secs: float) -> None:
        """
        Auto-merge pairs near market end to lock in $1.00/pair.

        Triggers:
        - 10 seconds before market end
        - 20 seconds after market end (before rotation)

        Only runs in live mode and only once per market.
        """
        # Only in live mode
        if self.trading_mode != "live":
            return

        # Only merge once per market
        if self._merged_this_market:
            return

        # Check if we're in the merge window: -20s to +10s around market end
        if not (-20 <= time_remaining_secs <= 10):
            return

        # Get position
        position = self._engine.get_position(market) if self._engine else None
        if not position:
            return

        # Calculate mergeable pairs
        pairs_to_merge = int(min(position.up_size, position.down_size))
        if pairs_to_merge < 5:  # Minimum 5 pairs to bother
            return

        logger.info(
            f"[AUTO-MERGE] Triggering merge: {pairs_to_merge} pairs @ "
            f"time_remaining={time_remaining_secs:.0f}s"
        )

        try:
            tx_hash = await self._execute_merge(market.condition_id, pairs_to_merge)
            self._merged_this_market = True

            if tx_hash:
                logger.info(f"[AUTO-MERGE] SUCCESS! TX: {tx_hash}")
                logger.info(f"[AUTO-MERGE] Received: ${pairs_to_merge:.2f} USDC")

                # Send Telegram notification
                if self._telegram:
                    await self._telegram.send_message(
                        f"Auto-Merged {pairs_to_merge} pairs\n"
                        f"Received: ${pairs_to_merge:.2f} USDC\n"
                        f"TX: {tx_hash[:20]}..."
                    )
        except Exception as e:
            logger.error(f"[AUTO-MERGE] Failed: {e}")

    async def _execute_merge(self, condition_id: str, amount: int) -> Optional[str]:
        """Execute merge based on wallet type."""
        if not self._config:
            raise ValueError("Config not initialized")

        if self._config.wallet_type == "gnosis_safe":
            return await self._merge_via_builder_relayer(condition_id, amount)
        else:
            return await self._merge_via_web3(condition_id, amount)

    async def _merge_via_builder_relayer(self, condition_id: str, amount: int) -> str:
        """Execute merge via Builder Relayer (gasless, Safe wallet only)."""
        from py_builder_signing_sdk.config import BuilderConfig
        from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
        from py_builder_relayer_client.client import RelayClient
        from py_builder_relayer_client.models import SafeTransaction, OperationType
        from web3 import Web3
        import os

        CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
        USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

        api_key = os.getenv("BUILDER_API_KEY")
        secret = os.getenv("BUILDER_SECRET")
        passphrase = os.getenv("BUILDER_PASSPHRASE")

        if not all([api_key, secret, passphrase]):
            raise ValueError("Missing Builder credentials")

        # Create RelayClient
        builder_creds = BuilderApiKeyCreds(key=api_key, secret=secret, passphrase=passphrase)
        builder_config = BuilderConfig(local_builder_creds=builder_creds)

        client = RelayClient(
            relayer_url="https://relayer-v2.polymarket.com",
            chain_id=137,
            private_key=self._config.wallet_private_key,
            builder_config=builder_config
        )

        # Build merge transaction
        calldata = self._encode_merge_calldata(condition_id, amount, CTF_ADDRESS, USDC_ADDRESS)

        tx = SafeTransaction(
            to=Web3.to_checksum_address(CTF_ADDRESS),
            operation=OperationType.Call,
            data=calldata,
            value="0"
        )

        # Execute
        response = client.execute([tx], f"Auto-Merge {amount} pairs")

        # Extract tx hash
        if isinstance(response, dict):
            return response.get('txHash') or response.get('transaction_hash') or response.get('tx_hash')
        return getattr(response, 'transaction_hash', None) or getattr(response, 'tx_hash', None)

    async def _merge_via_web3(self, condition_id: str, amount: int) -> str:
        """Execute merge via direct web3 call (pays gas, Magic/EOA wallet)."""
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware
        import os

        CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
        USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

        CTF_MERGE_ABI = [{
            "name": "mergePositions",
            "type": "function",
            "inputs": [
                {"name": "collateralToken", "type": "address"},
                {"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId", "type": "bytes32"},
                {"name": "partition", "type": "uint256[]"},
                {"name": "amount", "type": "uint256"}
            ]
        }]

        w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        account = w3.eth.account.from_key(self._config.wallet_private_key)
        signer_address = account.address

        # Build contract
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=CTF_MERGE_ABI
        )

        # Convert condition_id to bytes32
        cond_bytes = bytes.fromhex(condition_id[2:] if condition_id.startswith("0x") else condition_id)

        # Build transaction
        tx = contract.functions.mergePositions(
            Web3.to_checksum_address(USDC_ADDRESS),
            bytes(32),  # parentCollectionId = 0
            cond_bytes,
            [1, 2],  # partition
            amount * 1_000_000  # Convert to base units
        ).build_transaction({
            'from': signer_address,
            'gas': 200000,
            'gasPrice': w3.eth.gas_price,
            'nonce': w3.eth.get_transaction_count(signer_address),
            'chainId': 137
        })

        # Sign and send
        signed = w3.eth.account.sign_transaction(tx, self._config.wallet_private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

        # Wait for confirmation
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt['status'] != 1:
            raise Exception(f"Transaction failed: {tx_hash.hex()}")

        return tx_hash.hex()

    def _encode_merge_calldata(self, condition_id: str, amount: int, ctf_address: str, usdc_address: str) -> str:
        """Encode mergePositions calldata."""
        from web3 import Web3

        CTF_MERGE_ABI = [{
            "name": "mergePositions",
            "type": "function",
            "inputs": [
                {"name": "collateralToken", "type": "address"},
                {"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId", "type": "bytes32"},
                {"name": "partition", "type": "uint256[]"},
                {"name": "amount", "type": "uint256"}
            ]
        }]

        w3 = Web3()
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(ctf_address),
            abi=CTF_MERGE_ABI
        )

        # Convert condition_id to bytes32
        cond_bytes = bytes.fromhex(condition_id[2:] if condition_id.startswith("0x") else condition_id)

        calldata = contract.encode_abi(
            "mergePositions",
            [
                Web3.to_checksum_address(usdc_address),
                bytes(32),  # parentCollectionId = 0
                cond_bytes,
                [1, 2],  # partition: YES + NO
                amount * 1_000_000  # Convert to base units
            ]
        )

        return calldata if isinstance(calldata, str) else "0x" + calldata.hex()

    @classmethod
    def from_web_config(
        cls,
        config: dict,
        web_callback: Optional[Callable[[dict], None]] = None,
        strategy_name: str = "accumulation",
        session_start_utc: Optional[datetime] = None,
        session_end_utc: Optional[datetime] = None,
    ) -> "PaperTradingBot":
        """Create bot instance from web UI configuration.

        Args:
            config: Dictionary with web UI configuration values
            web_callback: Optional callback for web UI updates
            strategy_name: Strategy identifier for Discord and web UI (e.g., "standard", "volume_weighted")
            session_start_utc: UTC start time - only trade markets ending AFTER this
            session_end_utc: UTC end time - only trade markets ending BEFORE this

        Returns:
            PaperTradingBot instance configured from web UI
        """
        # Determine accum_mode from config or strategy_name
        accum_mode = config.get("accum_mode", "standard")
        if strategy_name in ["standard", "volume_weighted"]:
            accum_mode = strategy_name

        # Get trading mode (paper or live)
        trading_mode = config.get("mode", "paper")

        # Use mode-specific CSV file names with trading_mode prefix
        csv_prefix = "live_trades" if trading_mode == "live" else "paper_trades"
        csv_path = f"{csv_prefix}_{accum_mode}.csv"

        return cls(
            initial_balance=config.get("starting_balance", 100.0),
            # Accumulation mode params
            accum_mode=accum_mode,
            accum_max_share_price=config.get("max_share_price", 0.95),
            accum_trade_size=config.get("accum_trade_size", 1),
            accum_target_shares=config.get("accum_target_shares", 15),
            accum_max_imbalance_pct=config.get("accum_max_imbalance_pct", 0.15),
            hard_max_imbalance=config.get("hard_max_imbalance", 10),
            accum_pair_cost_target=config.get("accum_pair_cost_target", 0.995),
            accum_pair_cost_limit=config.get("accum_pair_cost_limit", 1.02),
            accum_buy_both_sides=config.get("accum_buy_both_sides", True),
            # Volume Weighted mode params (only used when accum_mode="volume_weighted")
            # Gabagool-style settings
            vw_imbalance_pct=config.get("vw_imbalance_pct", 0.20),
            vw_cheap_threshold=config.get("vw_cheap_threshold", 0.45),
            vw_hedge_trigger_pct=config.get("vw_hedge_trigger_pct", 0.15),
            vw_max_hedge_price=config.get("vw_max_hedge_price", 0.85),
            vw_bootstrap_pct=config.get("vw_bootstrap_pct", 0.33),
            # Output
            csv_path=csv_path,
            live_display=True,
            # Web callback
            web_callback=web_callback,
            # Strategy name
            strategy_name=strategy_name,
            # Trading mode
            trading_mode=trading_mode,
            # CRITICAL: Session time window for market selection enforcement
            session_start_utc=session_start_utc,
            session_end_utc=session_end_utc,
        )

    @classmethod
    def from_directional_config(
        cls,
        config: dict,
        web_callback: Optional[Callable[[dict], None]] = None,
        session_start_utc: Optional[datetime] = None,
        session_end_utc: Optional[datetime] = None,
        trading_mode: str = "paper",
    ) -> "PaperTradingBot":
        """Create bot instance from Directional web UI configuration.

        Args:
            config: Dictionary with directional configuration values
            web_callback: Optional callback for web UI updates
            session_start_utc: UTC start time - only trade markets ending AFTER this
            session_end_utc: UTC end time - only trade markets ending BEFORE this
            trading_mode: "paper" or "live" - determines CSV file prefix

        Returns:
            PaperTradingBot instance configured for directional mode
        """
        # Build DirectionalConfig from web config
        directional_config = DirectionalConfig(
            flip_cooldown_seconds=config.get("flip_cooldown_seconds", 120.0),
            sigma_threshold=config.get("sigma_threshold", 2.0),
            sustained_seconds=config.get("sustained_seconds", 30.0),
            window_seconds=config.get("window_seconds", 60),
            max_position_pct=config.get("max_position_pct", 0.15),
            target_shares=config.get("target_shares", 15),
            trade_size_pct=config.get("trade_size_pct", 0.3333),
            trade_size=config.get("trade_size", 5),
            hedge_increment=config.get("hedge_increment", 5),
            max_share_price=config.get("max_share_price", 0.95),
            attractive_price_early=config.get("attractive_price_early", 0.75),
            attractive_price_late=config.get("attractive_price_late", 0.90),
            dip_threshold_pct=config.get("dip_threshold_pct", 0.10),
            pair_cost_target=config.get("pair_cost_target", 0.95),
            emergency_threshold_secs=config.get("emergency_threshold_secs", 300),
            emergency_max_price=config.get("emergency_max_price", 0.65),
        )

        return cls(
            initial_balance=config.get("starting_balance", 100.0),
            # Directional mode
            directional_mode=True,
            initial_bias=config.get("initial_bias", "BULLISH"),
            directional_config=directional_config,
            # Output - use trading_mode to determine CSV prefix
            csv_path=f"{'live_trades' if trading_mode == 'live' else 'paper_trades'}_directional.csv",
            live_display=True,
            # Web callback
            web_callback=web_callback,
            # Strategy name
            strategy_name="directional",
            # CRITICAL: Session time window for market selection enforcement
            session_start_utc=session_start_utc,
            session_end_utc=session_end_utc,
        )

    @classmethod
    def from_calculus_config(
        cls,
        config: dict,
        web_callback: Optional[Callable[[dict], None]] = None,
        session_start_utc: Optional[datetime] = None,
        session_end_utc: Optional[datetime] = None,
        trading_mode: str = "paper",
    ) -> "PaperTradingBot":
        """Create bot instance from Calculus MAKER web UI configuration.

        Uses exponential decay mispricing threshold and quadratic size ramp.

        Args:
            config: Dictionary with calculus maker configuration values
            web_callback: Optional callback for web UI updates
            session_start_utc: UTC start time - only trade markets ending AFTER this
            session_end_utc: UTC end time - only trade markets ending BEFORE this

        Returns:
            PaperTradingBot instance configured for calculus maker mode
        """
        # Get trading mode (paper or live)
        trading_mode = config.get("mode", "paper")

        return cls(
            initial_balance=config.get("starting_balance", 500.0),
            # Set accum_mode to calculus_maker
            accum_mode="calculus_maker",
            # Calculus MAKER specific parameters
            calc_m_min=config.get("m_min", 0.005),  # 0.5% edge late
            calc_m_max=config.get("m_max", 0.025),  # 2.5% edge early
            calc_lambda=config.get("lambda_decay", 0.004),
            calc_max_shares=config.get("max_shares", 50),
            calc_min_shares=config.get("min_shares", 5),
            calc_max_pair_cost=config.get("max_pair_cost", 0.995),
            # Gradual chase: disabled by default to prevent stranded positions
            # When OFF: jumps to ask immediately, fills both sides or neither
            gradual_chase_enabled=config.get("gradual_chase_enabled", False),
            # Sequential ordering: place expensive side first, wait for fill before cheap side
            # When ON (default): prevents asymmetric fills (30/10 disaster fix)
            # When OFF: place both sides simultaneously - faster but risky
            sequential_ordering_enabled=config.get("sequential_ordering_enabled", True),
            # Max daily loss: stop trading if cumulative loss exceeds this amount
            # Stops placing new orders but keeps existing positions
            max_daily_loss=config.get("max_daily_loss", 10.0),
            # General parameters
            accum_max_share_price=config.get("max_share_price", 0.98),
            accum_max_imbalance_pct=config.get("max_imbalance_pct", 0.20),
            hard_max_imbalance=config.get("hard_max_imbalance", 10),
            accum_target_shares=config.get("max_shares", 50),
            # Output - use trading_mode to determine CSV prefix
            csv_path=f"{'live_trades' if trading_mode == 'live' else 'paper_trades'}_calculus_maker.csv",
            live_display=True,
            # Web callback
            web_callback=web_callback,
            # Strategy name
            strategy_name="calculus_maker",
            # Trading mode
            trading_mode=trading_mode,
            # CRITICAL: Session time window for market selection enforcement
            session_start_utc=session_start_utc,
            session_end_utc=session_end_utc,
        )

    @classmethod
    def from_fair_value_mm_config(
        cls,
        config: dict,
        web_callback: Optional[Callable[[dict], None]] = None,
        session_start_utc: Optional[datetime] = None,
        session_end_utc: Optional[datetime] = None,
        trading_mode: str = "paper",
    ) -> "PaperTradingBot":
        """Create bot instance from Fair Value MM web UI configuration.

        Uses Binance price feed to calculate fair value for UP/DOWN shares.
        Posts at fair_value - edge (like real market makers).

        Args:
            config: Dictionary with fair value MM configuration values
            web_callback: Optional callback for web UI updates
            session_start_utc: UTC start time - only trade markets ending AFTER this
            session_end_utc: UTC end time - only trade markets ending BEFORE this

        Returns:
            PaperTradingBot instance configured for fair value MM mode
        """
        # Get trading mode (paper or live)
        trading_mode = config.get("mode", "paper")

        return cls(
            initial_balance=config.get("starting_balance", 500.0),
            # Set accum_mode to fair_value_mm
            accum_mode="fair_value_mm",
            # Fair Value MM specific parameters
            fv_edge=config.get("fv_edge", 0.02),  # 2 cent edge
            fv_sensitivity_early=config.get("fv_sensitivity_early", 0.10),
            fv_sensitivity_late=config.get("fv_sensitivity_late", 0.50),
            fv_reprice_threshold=config.get("fv_reprice_threshold", 0.03),
            # Reuse calculus maker parameters for should_buy() and get_size()
            calc_m_min=config.get("m_min", 0.005),
            calc_m_max=config.get("m_max", 0.025),
            calc_lambda=config.get("lambda_decay", 0.004),
            calc_max_shares=config.get("max_shares", 50),
            calc_min_shares=config.get("min_shares", 5),
            calc_max_pair_cost=config.get("max_pair_cost", 0.995),
            # Max daily loss protection
            max_daily_loss=config.get("max_daily_loss", 10.0),
            # General parameters
            accum_max_share_price=config.get("max_share_price", 0.98),
            accum_max_imbalance_pct=config.get("max_imbalance_pct", 0.20),
            hard_max_imbalance=config.get("hard_max_imbalance", 10),
            accum_target_shares=config.get("max_shares", 50),
            # Output
            csv_path=f"{'live_trades' if trading_mode == 'live' else 'paper_trades'}_fair_value_mm.csv",
            live_display=True,
            # Web callback
            web_callback=web_callback,
            # Strategy name
            strategy_name="fair_value_mm",
            # Trading mode
            trading_mode=trading_mode,
            # CRITICAL: Session time window for market selection enforcement
            session_start_utc=session_start_utc,
            session_end_utc=session_end_utc,
        )

    @classmethod
    def from_one_buy_config(
        cls,
        config: dict,
        web_callback: Optional[Callable[[dict], None]] = None,
        session_start_utc: Optional[datetime] = None,
        session_end_utc: Optional[datetime] = None,
        trading_mode: str = "paper",
    ) -> "PaperTradingBot":
        """Create bot instance from ONE BUY web UI configuration.

        Simple fixed threshold strategy with max 1 buy per side per market.
        No emergency mode, no size ramping, no complexity.

        Args:
            config: Dictionary with one_buy configuration values
            web_callback: Optional callback for web UI updates
            session_start_utc: UTC start time - only trade markets ending AFTER this
            session_end_utc: UTC end time - only trade markets ending BEFORE this

        Returns:
            PaperTradingBot instance configured for one_buy mode
        """
        # Get trading mode (paper or live)
        trading_mode = config.get("mode", "paper")

        return cls(
            initial_balance=config.get("starting_balance", 500.0),
            # Set accum_mode to one_buy
            accum_mode="one_buy",
            # ENHANCED ONE BUY parameters
            one_buy_threshold=config.get("threshold", 0.47),
            one_buy_size=config.get("size", 15),  # Default 15 shares
            one_buy_dynamic_threshold=config.get("dynamic_threshold", False),
            one_buy_max_pair_cost=config.get("max_pair_cost", 0.98),
            # Max daily loss: stop trading if cumulative loss exceeds this amount
            max_daily_loss=config.get("max_daily_loss", 0.0),
            # General parameters - use defaults
            accum_max_share_price=0.98,
            # Output - use trading_mode to determine CSV prefix
            csv_path=f"{'live_trades' if trading_mode == 'live' else 'paper_trades'}_one_buy.csv",
            live_display=True,
            # Web callback
            web_callback=web_callback,
            # Strategy name
            strategy_name="one_buy",
            # Trading mode
            trading_mode=trading_mode,
            # CRITICAL: Session time window for market selection enforcement
            session_start_utc=session_start_utc,
            session_end_utc=session_end_utc,
        )

    async def initialize(self) -> None:
        """Initialize all components."""
        logger.info("Initializing paper trading bot...")

        self._config = Config()
        self._client = PolymarketClient(self._config)
        await self._client.connect()

        # Initialize Telegram for notifications and remote control
        self._telegram = TelegramNotifier(self._config, trading_mode=self.trading_mode)
        if self._telegram.enabled:
            # Register command handlers
            self._telegram.on_stop(self._handle_telegram_stop)
            self._telegram.on_sell_all(self._handle_telegram_sell_all)
            self._telegram.on_status(self._handle_telegram_status)
            self._telegram.on_balance(self._handle_telegram_balance)

            # Register graceful stop handlers for ALL strategy types
            # This ensures whichever TelegramNotifier receives the callback can route it
            # Each bot only responds to its own strategy button (others are no-ops)
            async def _noop():
                pass  # No-op for non-matching strategies

            if self.directional_mode:
                mode_label = "Directional"
                self._telegram.on_graceful_stop_directional(self._handle_telegram_graceful_stop)
                self._telegram.on_graceful_stop_calculus_maker(_noop)
                self._telegram.on_graceful_stop_simple_hedger(_noop)
                self._telegram.on_graceful_stop_volume_weighted(_noop)
            elif self.accum_mode == "volume_weighted":
                mode_label = "Volume Weighted (Gabagool-style)"
                self._telegram.on_graceful_stop_volume_weighted(self._handle_telegram_graceful_stop)
                self._telegram.on_graceful_stop_calculus_maker(_noop)
                self._telegram.on_graceful_stop_simple_hedger(_noop)
                self._telegram.on_graceful_stop_directional(_noop)
            elif self.accum_mode == "calculus_maker":
                mode_label = "Calculus MAKER"
                self._telegram.on_graceful_stop_calculus_maker(self._handle_telegram_graceful_stop)
                self._telegram.on_graceful_stop_simple_hedger(_noop)
                self._telegram.on_graceful_stop_volume_weighted(_noop)
                self._telegram.on_graceful_stop_directional(_noop)
            elif self.accum_mode == "fair_value_mm":
                mode_label = "Fair Value MM"
                # Fair Value MM uses calculus_maker button since they're similar
                self._telegram.on_graceful_stop_calculus_maker(self._handle_telegram_graceful_stop)
                self._telegram.on_graceful_stop_simple_hedger(_noop)
                self._telegram.on_graceful_stop_volume_weighted(_noop)
                self._telegram.on_graceful_stop_directional(_noop)
            elif self.accum_mode == "simple_hedger":
                mode_label = "Simple Hedger"
                self._telegram.on_graceful_stop_simple_hedger(self._handle_telegram_graceful_stop)
                self._telegram.on_graceful_stop_calculus_maker(_noop)
                self._telegram.on_graceful_stop_volume_weighted(_noop)
                self._telegram.on_graceful_stop_directional(_noop)
            else:
                mode_label = "Unknown Mode"
                self._telegram.on_graceful_stop_calculus_maker(_noop)
                self._telegram.on_graceful_stop_simple_hedger(_noop)
                self._telegram.on_graceful_stop_volume_weighted(_noop)
                self._telegram.on_graceful_stop_directional(_noop)

            await self._telegram.start()
            await self._telegram.send_info(
                "Bot Starting",
                f"Paper trading bot initializing...",
                {"Mode": mode_label}
            )
            # Send control panel with inline buttons
            await self._telegram.send_control_panel()
            logger.info("Telegram remote control enabled")

        # Initialize state persistence for crash recovery
        self._state_persistence = StatePersistence(
            state_dir=project_root / "state",
            strategy_name=self.strategy_name,
            trading_mode=self.trading_mode,
        )

        # Try to load previous state (for crash recovery)
        loaded_state = self._state_persistence.load()
        if loaded_state:
            logger.info(
                f"[STATE] Recovered state: balance=${loaded_state.balance:.2f}, "
                f"positions={len(loaded_state.positions)}, trades={loaded_state.trade_count}"
            )
            self._persisted_state = loaded_state
            # TODO: Could restore balance/positions from loaded state for live mode
        else:
            # Create fresh state
            self._persisted_state = PersistedState(
                strategy_name=self.strategy_name,
                trading_mode=self.trading_mode,
                balance=self.initial_balance,
                initial_balance=self.initial_balance,
                realized_pnl=0.0,
                session_start=datetime.now(timezone.utc).isoformat(),
                last_save=datetime.now(timezone.utc).isoformat(),
                trade_count=0,
            )
            logger.info("[STATE] Starting with fresh state")

        self._finder = MarketFinder()
        self._analyzer = PairAnalyzer(self._client)

        # Create trading engine based on mode
        if self.trading_mode == "live":
            logger.warning("=" * 60)
            logger.warning("LIVE TRADING MODE - Real money at risk!")
            logger.warning("=" * 60)
            self._engine = LiveTradingEngine(
                client=self._client,
                starting_balance=self.initial_balance,
                on_fill_callback=self._web_callback,
            )
            # Sync balance from chain
            await self._engine.sync_balance()
            logger.info(f"Live balance: ${self._engine.balance:.2f}")

            # Check for existing positions from previous sessions
            existing = await self._check_existing_positions()
            if existing["total"] > 0:
                logger.warning("=" * 60)
                logger.warning(f"WARNING: {existing['total']} existing positions found!")
                logger.warning(f"  Total UP shares: {existing['up']:.2f}")
                logger.warning(f"  Total DOWN shares: {existing['down']:.2f}")
                logger.warning(f"  Imbalance: {abs(existing['up'] - existing['down']):.2f}")
                logger.warning("These may accumulate with new trades.")
                logger.warning("Use 'python scripts/sell_all_positions.py' to clear first.")
                logger.warning("=" * 60)

                # Send Telegram alert if enabled
                if self._telegram and self._telegram.enabled:
                    await self._telegram.send_warning(
                        f"Existing positions detected!\n"
                        f"UP: {existing['up']:.2f}, DOWN: {existing['down']:.2f}\n"
                        f"Imbalance: {abs(existing['up'] - existing['down']):.2f}"
                    )
        else:
            sim_config = SimulationConfig(
                fill_probability=0.90,
                partial_fill_rate=0.10,
                slippage_bps=5.0,
            )
            self._engine = PaperTradingEngine(
                config=sim_config,
                initial_balance=self.initial_balance,
            )

        # Try continuous mode first, fall back to session mode
        # CRITICAL: Pass session time window to enforce market selection
        self._rotator = MarketRotator(
            finder=self._finder,
            continuous=True,
            market_window_minutes=60,
            session_start_utc=self.session_start_utc,
            session_end_utc=self.session_end_utc,
        )

        # Check if markets available in continuous mode
        # Use time-range based check if session window is configured
        if self.session_start_utc and self.session_end_utc:
            window_markets = await self._finder.get_markets_in_time_range(
                start_utc=self.session_start_utc,
                end_utc=self.session_end_utc,
            )
        else:
            window_markets = await self._finder.get_markets_in_window(hours=1.0)

        if not window_markets:
            logger.info("No markets in configured time window, using session mode")
            self._rotator = MarketRotator(
                finder=self._finder,
                continuous=False,  # Session mode
                max_markets=100,
                market_window_minutes=60,
                session_start_utc=self.session_start_utc,
                session_end_utc=self.session_end_utc,
            )

        # Initialize CSV
        self._init_csv()

        # Connect to Binance for price feed (needed for market resolution in all modes)
        window_seconds = self.directional_config.window_seconds if self.directional_mode else 60
        self._binance_client = BinanceClient(window_seconds=window_seconds)
        await self._binance_client.connect()

        # Wait for initial price
        for _ in range(50):  # 5 seconds max
            if self._binance_client.current_price > 0:
                break
            await asyncio.sleep(0.1)

        if self._binance_client.current_price <= 0:
            logger.warning("Could not get initial Binance price, continuing anyway")
        else:
            logger.info(f"Binance connected: BTC=${self._binance_client.current_price:,.2f}")

        # Initialize TrendDetector for quote pulling and direction-aware trading
        # Based on Telegram alpha: MMs monitor Binance to react BEFORE Polymarket updates
        if self._binance_client.is_connected:
            self._trend_detector = TrendDetector(
                binance_client=self._binance_client,
                z_score_mild=1.0,
                z_score_strong=2.0,
                z_score_extreme=3.0,
                velocity_window_secs=10,
            )
            logger.info("[TREND] TrendDetector initialized - quote pulling and trend-aware trading enabled")

        # Initialize directional mode components if enabled
        if self.directional_mode:
            if not self.initial_bias:
                raise ValueError("Directional mode requires --bias (BULLISH or BEARISH)")

            # Create directional strategy
            self._directional_strategy = DirectionalTradingStrategy(
                binance_client=self._binance_client,
                initial_bias=self.initial_bias,
                config=self.directional_config,
                starting_balance=self.initial_balance,
            )

            logger.info(f"Directional mode: Bias={self.initial_bias.value}")

        logger.info(f"Bot initialized with ${self.initial_balance:.2f} balance")

    async def _check_existing_positions(self) -> Dict[str, Any]:
        """
        Check for existing positions on startup (live mode only).

        Warns user if positions exist from previous sessions to avoid
        unexpected accumulation across sessions.

        Returns:
            Dict with total UP/DOWN shares and position count
        """
        import aiohttp

        result = {"up": 0.0, "down": 0.0, "total": 0, "positions": []}

        try:
            wallet = self._client.get_wallet_address()
            url = f"https://gamma-api.polymarket.com/positions?user={wallet}"

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status != 200:
                        return result
                    positions = await response.json()

            for pos in positions:
                size = float(pos.get("size", 0))
                if size <= 0:
                    continue

                outcome = pos.get("outcome", "").upper()
                market = pos.get("title", pos.get("slug", "Unknown"))

                result["total"] += 1
                result["positions"].append({
                    "market": market[:50],
                    "outcome": outcome,
                    "size": size,
                })

                if outcome in ["YES", "UP"]:
                    result["up"] += size
                elif outcome in ["NO", "DOWN"]:
                    result["down"] += size

        except Exception as e:
            logger.debug(f"Could not check existing positions: {e}")

        return result

    def _init_csv(self) -> None:
        """Initialize CSV file with headers."""
        if not self.csv_path.exists():
            with open(self.csv_path, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp',
                    'market_slug',
                    'event_type',        # TRADE, RESOLUTION, POSITION_UPDATE
                    'trade_side',        # UP, DOWN, PAIR, or N/A
                    'trade_mode',        # VOLATILITY, ASYMMETRIC, REBALANCE, RECOVERY, PAIR
                    'size_requested',
                    'size_filled',
                    'price',
                    'cost',
                    # Position state after trade
                    'pos_up_size',
                    'pos_up_avg_price',
                    'pos_down_size',
                    'pos_down_avg_price',
                    'pos_hedged_pairs',
                    'pos_pair_cost',
                    'pos_locked_profit',
                    'pos_imbalance',
                    # P&L metrics
                    'pnl_min',           # Worst case P&L
                    'pnl_max',           # Best case P&L
                    'pnl_realized',      # For resolutions
                    # Account state
                    'balance_after',
                    'status',            # SUCCESS, PARTIAL, FAILED, RESOLVED
                ])
            logger.info(f"Created CSV log: {self.csv_path}")

    def _check_csv_rotation(self) -> None:
        """Check if date changed and rotate to new CSV file if needed.

        This enables seamless overnight trading - when midnight UTC passes,
        the next trade automatically goes to a new dated file.
        """
        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if current_date != self._csv_date:
            logger.info(f"Date changed {self._csv_date} -> {current_date}, rotating CSV file")
            self._csv_date = current_date
            self.csv_path = self._csv_dir / f"{self._csv_base_name}_{self._csv_date}.csv"
            self._init_csv()  # Create new file with headers

    def _save_state_if_needed(self, force: bool = False) -> None:
        """
        Save state to disk if enough time has passed or if forced.

        Called after trades and periodically during trading loop.

        Args:
            force: If True, save regardless of time elapsed
        """
        if not self._state_persistence or not self._persisted_state:
            return

        # Save every 60 seconds, or immediately if forced (after trades)
        if force or self._state_persistence.should_save(interval_seconds=60.0):
            # Update state with current values
            if self._engine:
                self._persisted_state.balance = self._engine.balance
            self._persisted_state.trade_count = self._trade_count
            if self._rotator and self._rotator.current_market:
                self._persisted_state.current_market_slug = self._rotator.current_market.slug

            # Update position for current market
            if self._rotator and self._rotator.current_market and self._engine:
                market = self._rotator.current_market
                position = self._engine.get_position(market)
                if position:
                    self._state_persistence.update_position(
                        state=self._persisted_state,
                        market_slug=market.slug,
                        up_shares=position.up_size,
                        up_avg_price=position.up_avg_price,
                        down_shares=position.down_size,
                        down_avg_price=position.down_avg_price,
                        hedged_pairs=position.pair_count,
                        pair_cost=position.up_avg_price + position.down_avg_price if position.pair_count > 0 else 0,
                        locked_profit=position.locked_profit,
                    )

            # Save to disk
            if self._state_persistence.save(self._persisted_state):
                logger.debug("[STATE] State saved to disk")

    def _log_trade_csv(
        self,
        market_slug: str,
        up_size: float,
        up_price: float,
        down_size: float,
        down_price: float,
        status: str,
    ) -> None:
        """Log legacy pair trade to CSV (for backward compatibility)."""
        position = self._engine.get_position(self._rotator.current_market) if self._rotator.current_market else None
        self._log_event_csv(
            market_slug=market_slug,
            event_type="TRADE",
            trade_side="PAIR",
            trade_mode="PAIR",
            size_requested=max(up_size, down_size),
            size_filled=min(up_size, down_size),
            price=up_price + down_price,
            cost=up_size * up_price + down_size * down_price,
            position=position,
            pnl_realized=0,
            status=status,
        )

    def _log_event_csv(
        self,
        market_slug: str,
        event_type: str,
        trade_side: str,
        trade_mode: str,
        size_requested: float,
        size_filled: float,
        price: float,
        cost: float,
        position,  # Position or None
        pnl_realized: float = 0,
        status: str = "SUCCESS",
    ) -> None:
        """
        Log comprehensive trade/event to CSV.

        Args:
            market_slug: Market identifier
            event_type: TRADE, RESOLUTION, POSITION_UPDATE
            trade_side: UP, DOWN, PAIR, or N/A
            trade_mode: VOLATILITY, ASYMMETRIC, REBALANCE, RECOVERY, PAIR
            size_requested: Shares requested
            size_filled: Shares actually filled
            price: Execution price
            cost: Total cost of trade
            position: Current position state (or None)
            pnl_realized: Realized P&L (for resolutions)
            status: SUCCESS, PARTIAL, FAILED, RESOLVED
        """
        # Calculate position metrics
        if position:
            pos_up_size = position.up_size
            pos_up_avg = position.up_avg_price
            pos_down_size = position.down_size
            pos_down_avg = position.down_avg_price
            pos_pairs = position.pair_count
            pos_pair_cost = pos_up_avg + pos_down_avg if pos_pairs > 0 else 0
            pos_locked = position.locked_profit
            pos_imbalance = position.imbalance
            min_pnl, max_pnl, _ = position.calculate_expected_pnl_range()
        else:
            pos_up_size = pos_up_avg = pos_down_size = pos_down_avg = 0
            pos_pairs = pos_pair_cost = pos_locked = pos_imbalance = 0
            min_pnl = max_pnl = 0

        # Check for daily rotation before writing
        self._check_csv_rotation()

        with open(self.csv_path, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                market_slug,
                event_type,
                trade_side,
                trade_mode,
                f"{size_requested:.0f}",
                f"{size_filled:.0f}",
                f"{price:.4f}",
                f"{cost:.4f}",
                f"{pos_up_size:.0f}",
                f"{pos_up_avg:.4f}",
                f"{pos_down_size:.0f}",
                f"{pos_down_avg:.4f}",
                f"{pos_pairs:.0f}",
                f"{pos_pair_cost:.4f}",
                f"{pos_locked:.4f}",
                f"{pos_imbalance:.2f}",
                f"{min_pnl:.4f}",
                f"{max_pnl:.4f}",
                f"{pnl_realized:.4f}",
                f"{self._engine.balance:.2f}",
                status,
            ])

        # Save state after every trade event
        if event_type in ("TRADE", "RESOLUTION"):
            self._save_state_if_needed(force=True)

    def _build_live_display(self) -> Panel:
        """Build the rich live display panel showing current position."""
        market = self._rotator.current_market if self._rotator else None
        position = self._engine.get_position(market) if market else None

        # Calculate time remaining
        time_remaining = "N/A"
        if market and market.end_time:
            remaining = (market.end_time - datetime.now(timezone.utc)).total_seconds()
            if remaining > 0:
                mins = int(remaining // 60)
                secs = int(remaining % 60)
                time_remaining = f"{mins}:{secs:02d}"
            else:
                time_remaining = "EXPIRED"

        # Market header
        market_slug = market.slug if market else "No market"
        market_short = market_slug[-20:] if len(market_slug) > 20 else market_slug

        # Build position table
        table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1))
        table.add_column("Side", style="bold", width=6)
        table.add_column("Qty", justify="right", width=6)
        table.add_column("Avg Price", justify="right", width=10)
        table.add_column("Cost", justify="right", width=10)
        table.add_column("Current", justify="right", width=10)

        if position:
            up_cost = position.up_size * position.up_avg_price
            down_cost = position.down_size * position.down_avg_price

            # UP row
            up_current = f"${self._last_up_price:.4f}" if self._last_up_price > 0 else "-"
            table.add_row(
                Text("UP", style="green bold"),
                f"{position.up_size:.0f}",
                f"${position.up_avg_price:.4f}",
                f"${up_cost:.2f}",
                up_current,
            )

            # DOWN row
            down_current = f"${self._last_down_price:.4f}" if self._last_down_price > 0 else "-"
            table.add_row(
                Text("DOWN", style="red bold"),
                f"{position.down_size:.0f}",
                f"${position.down_avg_price:.4f}",
                f"${down_cost:.2f}",
                down_current,
            )
        else:
            table.add_row(Text("UP", style="green"), "0", "-", "-", "-")
            table.add_row(Text("DOWN", style="red"), "0", "-", "-", "-")

        # Calculate P&L info
        if position:
            min_pnl, max_pnl, locked = position.calculate_expected_pnl_range()
            pair_cost = position.up_avg_price + position.down_avg_price if position.pair_count > 0 else 0
            imbalance_pct = position.imbalance * 100

            # Color code locked profit
            if locked >= 0:
                locked_str = f"[green]${locked:.4f}[/green]"
            else:
                locked_str = f"[red]${locked:.4f}[/red]"

            # Color code spread
            spread = self._last_spread
            if spread > 0.02:
                spread_str = f"[green]{spread:.4f}[/green]"
            elif spread > 0:
                spread_str = f"[yellow]{spread:.4f}[/yellow]"
            else:
                spread_str = f"[red]{spread:.4f}[/red]"

            summary = (
                f"[bold]Pairs:[/bold] {position.pair_count}  "
                f"[bold]Pair Cost:[/bold] ${pair_cost:.4f}  "
                f"[bold]Locked:[/bold] {locked_str}  "
                f"[bold]Imbal:[/bold] {imbalance_pct:.0f}%\n"
                f"[bold]Spread:[/bold] {spread_str}  "
                f"[bold]P&L Range:[/bold] ${min_pnl:.2f} to ${max_pnl:.2f}  "
                f"[bold]Balance:[/bold] ${self._engine.balance:.2f}"
            )
        else:
            spread = self._last_spread
            spread_str = f"[green]{spread:.4f}[/green]" if spread > 0 else f"[red]{spread:.4f}[/red]"
            summary = (
                f"[bold]No position[/bold]  "
                f"[bold]Spread:[/bold] {spread_str}  "
                f"[bold]Balance:[/bold] ${self._engine.balance:.2f}"
            )

        # Combine into layout
        from rich.console import Group
        content = Group(table, Text(""), Text.from_markup(summary))

        # Create panel with market info in title
        title = f"[bold white on blue] {market_short} [/bold white on blue] [dim]Time: {time_remaining}[/dim]"
        panel = Panel(
            content,
            title=title,
            title_align="left",
            border_style="blue",
            padding=(0, 1),
        )

        return panel

    def _update_live_display(self) -> None:
        """Update the live display if enabled."""
        if self._live and self.live_display_enabled:
            self._live.update(self._build_live_display())
        # Also send web update
        self._send_web_update()

    def _get_end_time_from_slug(self, slug: str) -> Optional[datetime]:
        """
        Extract end time from market slug's embedded Unix timestamp.

        Slug format: btc-updown-15m-1766521800
        The last segment is a Unix timestamp representing the market START time.
        End time = start time + 15 minutes (900 seconds).
        This is more reliable than cached API data which can vary between requests.
        """
        if not slug:
            return None
        try:
            # Extract the last segment (Unix timestamp = START time)
            parts = slug.split("-")
            if len(parts) >= 4:
                start_timestamp = int(parts[-1])
                # Add 15 minutes to get end time
                return datetime.fromtimestamp(start_timestamp + 900, tz=timezone.utc)
        except (ValueError, IndexError):
            pass
        return None

    def _build_web_state(self) -> dict:
        """Build trading state as JSON for web UI."""
        market = self._rotator.current_market if self._rotator else None
        position = self._engine.get_position(market) if market and self._engine else None

        # Calculate time remaining from slug timestamp (consistent across all bots)
        time_remaining = "N/A"
        time_remaining_secs = 0
        if market:
            # Prefer slug-based end time for consistency across bot instances
            end_time = self._get_end_time_from_slug(market.slug) or market.end_time
            if end_time:
                remaining = (end_time - datetime.now(timezone.utc)).total_seconds()
                if remaining > 0:
                    mins = int(remaining // 60)
                    secs = int(remaining % 60)
                    time_remaining = f"{mins}:{secs:02d}"
                    time_remaining_secs = remaining
                else:
                    time_remaining = "EXPIRED"

        # Position data
        pos_data = {
            "up_qty": 0,
            "up_avg_price": 0,
            "up_cost": 0,
            "up_current": self._last_up_price,
            "down_qty": 0,
            "down_avg_price": 0,
            "down_cost": 0,
            "down_current": self._last_down_price,
        }

        # Metrics
        metrics = {
            "pairs": 0,
            "pair_cost": 0,
            "locked_profit": 0,
            "imbalance_pct": 0,
            "spread": self._last_spread,
            "pnl_min": 0,
            "pnl_max": 0,
            "balance": self._engine.balance if self._engine else 0,
            "target_shares": self.accum_target_shares,
            "realized_pnl": self._engine.get_realized_pnl() if self._engine else 0,
        }

        if position:
            pos_data["up_qty"] = position.up_size
            pos_data["up_avg_price"] = position.up_avg_price
            pos_data["up_cost"] = position.up_size * position.up_avg_price
            pos_data["down_qty"] = position.down_size
            pos_data["down_avg_price"] = position.down_avg_price
            pos_data["down_cost"] = position.down_size * position.down_avg_price

            min_pnl, max_pnl, locked = position.calculate_expected_pnl_range()
            pair_cost = position.up_avg_price + position.down_avg_price if position.pair_count > 0 else 0

            metrics["pairs"] = position.pair_count
            metrics["pair_cost"] = pair_cost
            metrics["locked_profit"] = locked
            metrics["imbalance_pct"] = position.imbalance * 100
            metrics["pnl_min"] = min_pnl
            metrics["pnl_max"] = max_pnl

        # Get directional status if in directional mode
        directional_status = None
        if self.directional_mode and self._directional_strategy:
            status = self._directional_strategy.get_status_dict()
            directional_status = {
                "bias": status.get("bias", "BULLISH"),
                "flip_count": status.get("flip_count", 0),
            }

        return {
            "type": "trading_update",
            "strategy": self.strategy_name,  # Strategy identifier for web UI routing
            "market_slug": market.slug if market else "No market",
            "time_remaining": time_remaining,
            "time_remaining_secs": time_remaining_secs,
            "position": pos_data,
            "metrics": metrics,
            "trade_count": self._trade_count,
            "total_pairs": self._total_pairs,
            "directional_status": directional_status,
        }

    def _send_web_update(self) -> None:
        """Send trading state to web UI if callback is set."""
        if self._web_callback:
            try:
                state = self._build_web_state()
                self._web_callback(state)
            except Exception as e:
                logger.warning(f"Failed to send web update: {e}")

    def _send_trade_event(self, side: str, size: float, price: float, action: str = "BUY") -> None:
        """Send trade event to web UI for the trade log."""
        if self._web_callback:
            try:
                # Get current position for the after state
                market = self._rotator.current_market if self._rotator else None
                pos = self._engine.get_position(market) if market else None
                position_after = {
                    "up": int(pos.up_size) if pos else 0,
                    "down": int(pos.down_size) if pos else 0
                }

                trade_event = {
                    "type": "trade_event",
                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                    "action": action,
                    "side": side,
                    "size": int(size),
                    "price": price,
                    "position_after": position_after
                }
                self._web_callback(trade_event)
            except Exception as e:
                logger.warning(f"Failed to send trade event: {e}")

    async def _send_telegram_update(self, is_final: bool = False) -> None:
        """Send PnL summary to Telegram."""
        if not self._telegram or not self._telegram.enabled:
            return

        try:
            now = datetime.now(timezone.utc)
            runtime = now - self._start_time if self._start_time else timedelta(0)
            hours = runtime.total_seconds() / 3600

            realized = self._engine.get_realized_pnl()
            total_locked = sum(p.calculate_expected_pnl_range()[2] for p in self._engine.positions)
            total_pairs = sum(p.pair_count for p in self._engine.positions)

            status_emoji = "+" if total_locked >= 0 else ""
            # Determine mode label from strategy_name
            mode_labels = {
                "one_buy": "ONE BUY",
                "calculus_maker": "Calculus Maker",
                "fair_value_mm": "Fair Value MM",
                "simple_hedger": "Simple Hedger",
                "directional": "Directional",
                "volume_weighted": "Volume Weighted",
                "standard": "Standard",
            }
            mode = mode_labels.get(self.strategy_name, self.strategy_name.replace("_", " ").title())
            title = f"Final Report - {mode}" if is_final else f"Update - {mode}"

            await self._telegram.send_pnl(
                f"{title}",
                {
                    "Runtime": f"{hours:.1f}h",
                    "Balance": f"${self._engine.balance:.2f}",
                    "Locked P&L": f"{status_emoji}${total_locked:.2f}",
                    "Realized": f"${realized:.2f}",
                    "Trades": str(self._trade_count),
                    "Pairs": str(total_pairs),
                }
            )
            logger.info("Telegram update sent")

        except Exception as e:
            logger.error(f"Failed to send Telegram update: {e}")

    async def _send_discord_update(self, is_final: bool = False) -> None:
        """Send PnL summary to Discord (deprecated - use Telegram)."""
        # Also send to Telegram
        await self._send_telegram_update(is_final)

        if not self._config or not self._config.discord_webhook_pnl:
            return

        try:
            import aiohttp

            now = datetime.now(timezone.utc)
            runtime = now - self._start_time if self._start_time else timedelta(0)
            hours = runtime.total_seconds() / 3600

            # Calculate P&L properly
            realized = self._engine.get_realized_pnl()

            # Calculate position-based P&L (locked profit + expected range)
            total_locked_profit = 0.0
            total_min_pnl = 0.0
            total_max_pnl = 0.0
            position_details = []

            for pos in self._engine.positions:
                min_pnl, max_pnl, locked = pos.calculate_expected_pnl_range()
                total_locked_profit += locked
                total_min_pnl += min_pnl
                total_max_pnl += max_pnl

                if pos.up_size > 0 or pos.down_size > 0:
                    exposure = pos.unhedged_exposure_side or "Hedged"
                    unhedged_qty = pos.unhedged_up_size or pos.unhedged_down_size
                    position_details.append(
                        f"{pos.market_slug[:20]}: {pos.pair_count}p, {exposure}({unhedged_qty:.0f})"
                    )

            # Total P&L = realized + locked profit (guaranteed)
            # Show range for unhedged exposure
            total_pnl = realized + total_locked_profit

            # Build message with strategy label
            status_emoji = "🟢" if total_locked_profit >= 0 else "🔴"
            strategy_label = "[Accumulation]" if not self.directional_mode else "[Directional]"
            title = f"📊 {strategy_label} Paper Trading - Final Report" if is_final else f"📊 {strategy_label} Paper Trading Update"

            # P&L display with range if there's unhedged exposure
            if total_min_pnl != total_max_pnl:
                pnl_display = f"{status_emoji} ${total_locked_profit:.2f} locked\n(Range: ${total_min_pnl:.2f} to ${total_max_pnl:.2f})"
            else:
                pnl_display = f"{status_emoji} ${total_locked_profit:.2f} locked"

            fields = [
                {
                    "name": "Runtime",
                    "value": f"{hours:.1f} hours",
                    "inline": True,
                },
                {
                    "name": "Cash Balance",
                    "value": f"${self._engine.balance:.2f}",
                    "inline": True,
                },
                {
                    "name": "Expected P&L",
                    "value": pnl_display,
                    "inline": True,
                },
                {
                    "name": "Trades",
                    "value": str(self._trade_count),
                    "inline": True,
                },
                {
                    "name": "Hedged Pairs",
                    "value": str(sum(p.pair_count for p in self._engine.positions)),
                    "inline": True,
                },
                {
                    "name": "Realized P&L",
                    "value": f"${realized:.2f}",
                    "inline": True,
                },
            ]

            # Add position details if any
            if position_details:
                fields.append({
                    "name": "Positions",
                    "value": "\n".join(position_details[:5]) or "None",
                    "inline": False,
                })

            message = {
                "embeds": [{
                    "title": title,
                    "color": 0x00FF00 if total_locked_profit >= 0 else 0xFF0000,
                    "fields": fields,
                    "footer": {
                        "text": f"Paper Trading Bot | {now.strftime('%Y-%m-%d %H:%M UTC')}",
                    },
                }]
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._config.discord_webhook_pnl,
                    json=message,
                ) as resp:
                    if resp.status == 204:
                        logger.info("Discord update sent")
                    else:
                        logger.warning(f"Discord webhook returned {resp.status}")

            self._last_discord_update = now

        except Exception as e:
            logger.error(f"Failed to send Discord update: {e}")

    async def _send_loss_notification(
        self,
        loss_type: str,
        details: str,
        loss_amount: float,
        market_slug: Optional[str] = None,
        winner: Optional[str] = None,
        up_qty: float = 0,
        up_avg_price: float = 0,
        down_qty: float = 0,
        down_avg_price: float = 0,
        pair_cost: float = 0,
    ) -> None:
        """
        Send loss notification to DISCORD_WEBHOOK_LOSSES.

        Args:
            loss_type: Type of loss (e.g., "UNHEDGED_LOSS", "RESOLUTION_LOSS", "SAFETY_STOP")
            details: Description of what caused the loss
            loss_amount: Amount of the loss (negative number)
            market_slug: Optional market identifier
            winner: Which side won ("UP" or "DOWN") for resolution losses
            up_qty: Number of UP shares held
            up_avg_price: Average price paid for UP shares
            down_qty: Number of DOWN shares held
            down_avg_price: Average price paid for DOWN shares
            pair_cost: Average pair cost (up_avg + down_avg)
        """
        # Send to Telegram first
        if self._telegram and self._telegram.enabled:
            try:
                # Determine mode label
                if self.directional_mode:
                    mode = "Directional"
                elif self.accum_mode == "volume_weighted":
                    mode = "Volume Weighted"
                elif self.accum_mode == "calculus_maker":
                    mode = "Calculus Maker"
                elif self.accum_mode == "fair_value_mm":
                    mode = "Fair Value MM"
                elif self.accum_mode == "simple_hedger":
                    mode = "Simple Hedger"
                else:
                    mode = "Standard"
                hedged_pairs = int(min(up_qty, down_qty))
                await self._telegram.send_loss(
                    f"{loss_type}: -${abs(loss_amount):.2f}",
                    {
                        "Mode": mode,
                        "Market": market_slug or "N/A",
                        "Winner": winner or "N/A",
                        "Shares": f"{int(up_qty)} UP / {int(down_qty)} DOWN",
                        "Hedged Pairs": str(hedged_pairs),
                        "Pair Cost": f"${pair_cost:.4f}",
                        "Balance": f"${self._engine.balance:.2f}",
                    }
                )
            except Exception as e:
                logger.error(f"Failed to send Telegram loss notification: {e}")

        webhook_url = getattr(self._config, 'discord_webhook_losses', None) if self._config else None
        if not webhook_url:
            logger.debug("No DISCORD_WEBHOOK_LOSSES configured, skipping loss notification")
            return

        try:
            import aiohttp

            now = datetime.now(timezone.utc)

            # Title format with strategy label
            strategy_label = "[Accumulation]" if not self.directional_mode else "[Directional]"
            if winner:
                title = f"🔴 {strategy_label} MARKET LOST: {winner}"
            else:
                title = f"🔴 {strategy_label} {loss_type}"

            message = {
                "embeds": [{
                    "title": title,
                    "color": 0xFF0000,
                    "fields": [
                        {
                            "name": "Loss Amount",
                            "value": f"**-${abs(loss_amount):.4f}**",
                            "inline": True,
                        },
                        {
                            "name": "Market",
                            "value": market_slug or "N/A",
                            "inline": True,
                        },
                        {
                            "name": "Current Balance",
                            "value": f"${self._engine.balance:.2f}",
                            "inline": True,
                        },
                        {
                            "name": "UP Position",
                            "value": f"{up_qty:.0f} shares @ ${up_avg_price:.3f}",
                            "inline": True,
                        },
                        {
                            "name": "DOWN Position",
                            "value": f"{down_qty:.0f} shares @ ${down_avg_price:.3f}",
                            "inline": True,
                        },
                        {
                            "name": "Avg Pair Cost",
                            "value": f"${pair_cost:.4f}",
                            "inline": True,
                        },
                        {
                            "name": "Details",
                            "value": details,
                            "inline": False,
                        },
                    ],
                    "footer": {
                        "text": f"Paper Trading Bot | {now.strftime('%Y-%m-%d %H:%M UTC')}",
                    },
                }]
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(webhook_url, json=message) as resp:
                    if resp.status == 204:
                        logger.info(f"Loss notification sent: {loss_type} ${abs(loss_amount):.4f}")
                    else:
                        logger.warning(f"Loss webhook returned {resp.status}")

        except Exception as e:
            logger.error(f"Failed to send loss notification: {e}")

    async def _send_win_notification(
        self,
        market_slug: str,
        pnl: float,
        winner: str,
        up_qty: float,
        up_avg_price: float,
        down_qty: float,
        down_avg_price: float,
        pair_cost: float,
    ) -> None:
        """
        Send win notification to DISCORD_WEBHOOK_PNL.

        Args:
            market_slug: Market identifier
            pnl: Profit amount (positive number)
            winner: Which side won ("UP" or "DOWN")
            up_qty: Number of UP shares held
            up_avg_price: Average price paid for UP shares
            down_qty: Number of DOWN shares held
            down_avg_price: Average price paid for DOWN shares
            pair_cost: Average pair cost (up_avg + down_avg)
        """
        # Send to Telegram first
        if self._telegram and self._telegram.enabled:
            try:
                # Determine mode label
                if self.directional_mode:
                    mode = "Directional"
                elif self.accum_mode == "volume_weighted":
                    mode = "Volume Weighted"
                elif self.accum_mode == "calculus_maker":
                    mode = "Calculus Maker"
                elif self.accum_mode == "fair_value_mm":
                    mode = "Fair Value MM"
                elif self.accum_mode == "simple_hedger":
                    mode = "Simple Hedger"
                else:
                    mode = "Standard"
                hedged_pairs = int(min(up_qty, down_qty))
                await self._telegram.send_pnl(
                    f"Market Won: +${pnl:.2f}",
                    {
                        "Mode": mode,
                        "Market": market_slug[-25:],
                        "Winner": winner,
                        "Shares": f"{int(up_qty)} UP / {int(down_qty)} DOWN",
                        "Hedged Pairs": str(hedged_pairs),
                        "Pair Cost": f"${pair_cost:.4f}",
                        "Balance": f"${self._engine.balance:.2f}",
                    }
                )
            except Exception as e:
                logger.error(f"Failed to send Telegram win notification: {e}")

        webhook_url = self._config.discord_webhook_pnl if self._config else None
        if not webhook_url:
            logger.debug("No DISCORD_WEBHOOK_PNL configured, skipping win notification")
            return

        try:
            import aiohttp

            now = datetime.now(timezone.utc)

            # Title with strategy label
            strategy_label = "[Accumulation]" if not self.directional_mode else "[Directional]"

            message = {
                "embeds": [{
                    "title": f"🟢 {strategy_label} MARKET WON: {winner}",
                    "color": 0x00FF00,
                    "fields": [
                        {
                            "name": "Profit",
                            "value": f"**+${pnl:.4f}**",
                            "inline": True,
                        },
                        {
                            "name": "Market",
                            "value": market_slug,
                            "inline": True,
                        },
                        {
                            "name": "Current Balance",
                            "value": f"${self._engine.balance:.2f}",
                            "inline": True,
                        },
                        {
                            "name": "UP Position",
                            "value": f"{up_qty:.0f} shares @ ${up_avg_price:.3f}",
                            "inline": True,
                        },
                        {
                            "name": "DOWN Position",
                            "value": f"{down_qty:.0f} shares @ ${down_avg_price:.3f}",
                            "inline": True,
                        },
                        {
                            "name": "Avg Pair Cost",
                            "value": f"${pair_cost:.4f}",
                            "inline": True,
                        },
                    ],
                    "footer": {
                        "text": f"Paper Trading Bot | {now.strftime('%Y-%m-%d %H:%M UTC')}",
                    },
                }]
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(webhook_url, json=message) as resp:
                    if resp.status == 204:
                        logger.info(f"Win notification sent: {winner} +${pnl:.4f}")
                    else:
                        logger.warning(f"Win webhook returned {resp.status}")

        except Exception as e:
            logger.error(f"Failed to send win notification: {e}")

    async def run(
        self,
        duration_minutes: float = None,
        check_interval: float = 5.0,
        end_time: datetime = None,
    ) -> None:
        """
        Run the paper trading bot.

        Args:
            duration_minutes: How long to run (ignored if end_time is provided)
            check_interval: Seconds between opportunity checks
            end_time: Specific datetime to stop (overrides duration_minutes)
        """
        self._running = True
        self._start_time = datetime.now(timezone.utc)

        # Clean up old shared strikes on startup
        cleanup_old_strikes(max_age_hours=24)

        # Determine end time - explicit end_time takes priority
        if end_time is not None:
            # Convert to UTC if it has timezone info
            if end_time.tzinfo is not None:
                end_time = end_time.astimezone(timezone.utc)
            duration_minutes = (end_time - self._start_time).total_seconds() / 60.0
            logger.info(f"Starting paper trading bot until {end_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        else:
            if duration_minutes is None:
                duration_minutes = 60  # Default 1 hour
            end_time = self._start_time + timedelta(minutes=duration_minutes)
            logger.info(f"Starting paper trading bot for {duration_minutes:.1f} minutes")
        # Log mode info
        logger.info("=" * 50)
        if self.directional_mode:
            logger.info("DIRECTIONAL MODE - Bias-Based Trading")
            logger.info("=" * 50)
            logger.info(f"  - Initial bias: {self.initial_bias.value}")
            # Calculate effective target (min of config target and max_position_pct limit)
            max_shares_from_pct = int(self.directional_config.max_position_pct * self.initial_balance)
            effective_target = min(self.directional_config.target_shares, max_shares_from_pct)
            logger.info(f"  - Target shares: {effective_target} per side (max {self.directional_config.max_position_pct*100:.0f}% of ${self.initial_balance:.0f})")
            logger.info(f"  - Attractive price: ${self.directional_config.attractive_price_early:.2f} → ${self.directional_config.attractive_price_late:.2f} (time decay)")
            logger.info(f"  - Sigma threshold: {self.directional_config.sigma_threshold}σ")
            logger.info(f"  - Sustained time: {self.directional_config.sustained_seconds}s")
            logger.info(f"  - Emergency hedge: <{self.directional_config.emergency_threshold_secs}s remaining")
            logger.info(f"  - BTC price: ${self._binance_client.current_price:,.2f}")
        else:
            if self.accum_mode == "volume_weighted":
                mode_label = "VOLUME WEIGHTED (Gabagool-style)"
            elif self.accum_mode == "calculus_maker":
                mode_label = "CALCULUS MAKER"
            elif self.accum_mode == "fair_value_mm":
                mode_label = "FAIR VALUE MM (Binance-priced)"
            else:
                mode_label = "STANDARD"
            logger.info(f"ACCUMULATION MODE [{mode_label}] - High Frequency Trading")
            logger.info("=" * 50)
            logger.info(f"  - Trade size: {self.accum_trade_size} shares per trade")
            logger.info(f"  - Pair cost limit: ${self.accum_pair_cost_limit}")
            if self.accum_mode == "volume_weighted":
                logger.info(f"  - Max imbalance: {self.vw_imbalance_pct*100:.0f}% of position (dynamic)")
                logger.info(f"  - Cheap threshold: ${self.vw_cheap_threshold} (always buy below)")
                logger.info(f"  - Hedge trigger: {self.vw_hedge_trigger_pct*100:.0f}% imbalance")
                logger.info(f"  - Max hedge price: ${self.vw_max_hedge_price}")
            else:
                # Calculate actual max imbalance from percentage
                max_imbal = max(int(self.accum_max_imbalance_pct * self.accum_target_shares), 2)
                logger.info(f"  - Max imbalance: {self.accum_max_imbalance_pct*100:.0f}% of target ({max_imbal} shares)")
            logger.info(f"  - Target shares: {self.accum_target_shares} per side (max {self.max_position_pct*100:.0f}% of ${self.initial_balance:.0f})")
            logger.info(f"  - Buy both sides: {self.accum_buy_both_sides}")
            logger.info(f"  - Price ceiling: ${self.accum_max_share_price} (never buy above)")
        logger.info("=" * 50)
        if self.live_display_enabled:
            logger.info("LIVE DISPLAY ENABLED - Position updates will show in terminal")
        logger.info(f"Will run until {end_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")

        # Send initial Discord message
        await self._send_discord_update()

        # Start auto-redemption if enabled and in live trading mode with Gnosis Safe
        if (
            self.trading_mode == "live"
            and self._config.wallet_type == "gnosis_safe"
            and self._config.auto_redeem_enabled
        ):
            self._auto_redeemer = AutoRedeemer(
                config=self._config,
                notifier=self._telegram,
                interval_minutes=self._config.auto_redeem_interval_minutes,
            )
            await self._auto_redeemer.start()
            logger.info(
                f"Auto-redemption enabled: checking every "
                f"{self._config.auto_redeem_interval_minutes:.0f} minutes"
            )

        # Start user WebSocket for instant fill notifications (live mode only)
        if self.trading_mode == "live":
            await self._setup_user_websocket()

        # Start rotator session
        if not await self._rotator.start_session():
            logger.error("Failed to start session - no markets available")
            return

        # Main trading loop - with optional live display
        await self._run_trading_loop(end_time, check_interval)

    async def _run_trading_loop(self, end_time: datetime, check_interval: float) -> None:
        """Run the main trading loop, optionally with live display."""
        if self.live_display_enabled:
            # Use Rich Live display
            with Live(
                self._build_live_display(),
                console=self._console,
                refresh_per_second=2,
                transient=False,
            ) as live:
                self._live = live
                await self._trading_loop_inner(end_time, check_interval)
                self._live = None
        else:
            # Run without live display
            await self._trading_loop_inner(end_time, check_interval)

    async def _trading_loop_inner(self, end_time: datetime, check_interval: float) -> None:
        """Inner trading loop logic with health monitoring and resilience."""
        # Get health monitor for recording heartbeats
        health_monitor = get_health_monitor()
        health_monitor.register_strategy(self.strategy_name)

        # Track consecutive errors for exponential backoff
        consecutive_errors = 0
        max_consecutive_errors = 10
        last_successful_cycle = datetime.now(timezone.utc)

        try:
            while self._running and datetime.now(timezone.utc) < end_time:
                cycle_start = datetime.now(timezone.utc)

                try:
                    # Record heartbeat at start of each cycle
                    health_monitor.record_heartbeat(self.strategy_name)

                    # Run appropriate trading cycle
                    if self.directional_mode:
                        await self._directional_trading_cycle()
                    else:
                        await self._accumulation_trading_cycle()

                    # Reset error counter on success
                    consecutive_errors = 0
                    last_successful_cycle = datetime.now(timezone.utc)

                except asyncio.CancelledError:
                    raise  # Don't catch cancellation
                except Exception as e:
                    consecutive_errors += 1
                    health_monitor.record_error(self.strategy_name, str(e))

                    # Log with increasing severity based on consecutive errors
                    if consecutive_errors >= max_consecutive_errors:
                        logger.critical(f"[{self.strategy_name}] CRITICAL: {consecutive_errors} consecutive errors: {e}")
                    elif consecutive_errors >= 5:
                        logger.error(f"[{self.strategy_name}] Multiple errors ({consecutive_errors}): {e}")
                    else:
                        logger.warning(f"[{self.strategy_name}] Error in trading cycle: {e}")

                    # Exponential backoff with cap
                    backoff = min(self.retry_base_delay * (2 ** min(consecutive_errors, 5)), 60.0)
                    logger.info(f"[{self.strategy_name}] Backing off for {backoff:.1f}s before retry")

                    if not await self._interruptible_sleep(backoff, check_interval=2.0):
                        break
                    continue

                # Wait before next cycle (interruptible for responsive stop)
                if not await self._interruptible_sleep(check_interval):
                    break

                # Periodic health log
                elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds()
                if int(elapsed) % 300 == 0:  # Every 5 minutes
                    logger.info(f"[{self.strategy_name}] Heartbeat: trades={self._trade_count}, pairs={self._total_pairs}, balance=${self._engine.balance:.2f}")

        except asyncio.CancelledError:
            logger.info(f"[{self.strategy_name}] Bot cancelled")
        except Exception as e:
            logger.critical(f"[{self.strategy_name}] Fatal error in trading loop: {e}")
            health_monitor.record_error(self.strategy_name, f"FATAL: {e}")
        finally:
            # Send final report
            try:
                await self._send_discord_update(is_final=True)
            except Exception as e:
                logger.error(f"Failed to send final Discord update: {e}")
            await self.cleanup()

    def should_buy_vw(
        self,
        side: str,
        price: float,
        current_up: float,
        current_down: float,
    ) -> bool:
        """
        Volume Weighted (Gabagool-style) buy decision.

        Gabagool's strategy (reverse-engineered from Dec 2024):
        1. Buy aggressively when price < $0.45 (load up on cheap side)
        2. Only hedge when imbalance > 30% (not panic at 5%)
        3. NEVER pay > $0.70 for hedge buys (was $0.99 before)
        4. Accept 40% imbalance tolerance (not 10%)
        """
        # Always buy if cheap - this is where gabagool loads up
        if price < self.vw_cheap_threshold:
            return True

        # Check if we need to hedge
        max_position = max(current_up, current_down, 1)
        if max_position == 0:
            return price < self.vw_cheap_threshold

        imbalance = abs(current_up - current_down)
        imbalance_pct = imbalance / max_position

        deficit_side = "UP" if current_down > current_up else "DOWN"

        # Only hedge if:
        # 1. This is the deficit side
        # 2. Imbalance exceeds trigger (30%, not 5%)
        # 3. Price is below max hedge price ($0.70, not $0.99)
        if side == deficit_side and imbalance_pct > self.vw_hedge_trigger_pct:
            return price < self.vw_max_hedge_price

        return False

    def get_vw_trade_size(self, price: float, remaining_capacity: int) -> int:
        """
        Volume-weighted trade size based on remaining capacity.

        Uses percentage of remaining capacity, weighted by price:
        - Cheaper prices = larger % of remaining capacity per trade
        - Prevents one side from filling too fast
        - Works with any target size (15, 50, 100+)

        Thresholds:
        - Price < $0.20: 25% of remaining (load up when cheap!)
        - Price $0.20-0.40: 15% of remaining
        - Price $0.40-0.60: 10% of remaining
        - Price > $0.60: 5% of remaining (go slow when expensive)
        """
        if remaining_capacity <= 0:
            return 0

        if price < 0.20:
            pct = 0.25
        elif price < 0.40:
            pct = 0.15
        elif price < 0.60:
            pct = 0.10
        else:
            pct = 0.05

        size = max(1, int(remaining_capacity * pct))
        return min(size, remaining_capacity)

    def get_vw_max_imbalance(self, current_up: float, current_down: float) -> int:
        """
        Calculate max allowed imbalance in volume_weighted mode.

        Returns percentage-based imbalance limit instead of absolute.

        Minimum of 10 to allow initial bootstrap trades:
        - Polymarket $1 minimum order requires ceil(1.00/price) shares
        - At $0.20 price, that's 5 shares per trade
        - Need to allow both sides to trade independently during bootstrap
        - Minimum 10 allows first trades on both sides without blocking

        Once position is established, the 40% rule takes over:
        - At 50 shares: max imbalance = 20 shares
        - At 100 shares: max imbalance = 40 shares
        """
        max_position = max(current_up, current_down, 1)
        # Minimum of 5 (one order) - tighter control to prevent large directional exposure
        return max(5, int(max_position * self.vw_imbalance_pct))

    async def _accumulation_trading_cycle(self) -> None:
        """
        High-frequency accumulation trading cycle.

        Based on the successful trading pattern from the chart:
        - Trade small amounts (10 shares) very frequently
        - Buy BOTH sides each cycle to stay balanced
        - Don't wait for "attractive" prices - accumulate continuously
        - Only constraint: keep average pair cost under limit
        - Prioritize the deficit side to maintain balance

        This generates profit through volume of balanced pairs,
        not through getting the "best" prices.
        """
        market = self._rotator.current_market
        if not market:
            logger.warning("No current market")
            return

        # Handle new market - use shared strike or set from previous candle
        if self._is_new_market:
            if self._binance_client:
                # Check for shared strike first (another strategy may have set it)
                shared_strike = get_shared_strike(market.slug)
                if shared_strike:
                    self._binance_client._strike_price = shared_strike
                    self._binance_client._strike_timestamp = datetime.now(timezone.utc)
                    logger.info(f"[STRIKE] Using shared strike for {market.slug}: ${shared_strike:,.2f}")
                else:
                    # First strategy to run - set and share the strike
                    strike = await self._binance_client.set_strike_from_previous_candle(interval="15m")
                    if strike > 0:
                        set_shared_strike(market.slug, strike, f"{self.strategy_name}_accum")

                # Set up event-driven quote pulling for 100-200ms reaction time
                self._setup_event_driven_pull(market.slug)

            self._is_new_market = False

        # Get current position
        position = self._engine.get_position(market)
        current_up = position.up_size if position else 0.0
        current_down = position.down_size if position else 0.0
        current_up_cost = position.up_cost if position else 0.0
        current_down_cost = position.down_cost if position else 0.0

        # ============================================================================
        # HARD MAX IMBALANCE LIMIT (CRITICAL SAFETY - First line of defense)
        # ============================================================================
        # If imbalance exceeds this, STOP ALL trading immediately.
        # This prevents runaway accumulation like the 30 UP / 10 DOWN disaster.
        # The predictive blocking at line ~3500 failed because:
        #   1. It only blocked ONE side (the surplus side)
        #   2. It was predictive (assumed both orders fill) not reactive
        #   3. VPS speed meant cycle completed before fills, using stale position data
        # This hard limit catches all edge cases by checking ACTUAL position.
        # ============================================================================
        hard_max = self.hard_max_imbalance  # Configurable via web UI (default 10)
        current_imbalance = abs(current_up - current_down)
        if current_imbalance >= hard_max:
            # Throttle log to once per 60 seconds to avoid spam
            now = time.time()
            if now - self._last_hard_stop_log >= 60:
                logger.critical(
                    f"🛑 HARD STOP: Imbalance {current_imbalance:.0f} >= {hard_max} "
                    f"(UP={current_up:.0f}, DOWN={current_down:.0f}). "
                    f"NO NEW ORDERS until balanced. Manual intervention may be required."
                )
                self._last_hard_stop_log = now
            # Update display to show we're stopped
            self._update_live_display()
            return  # Don't place ANY orders - wait for fills or manual intervention

        # Always update display at start of cycle to keep time_remaining in sync
        # This is critical - without it, early returns cause stale time to be shown
        self._update_live_display()

        # Check if we've hit target shares
        if current_up >= self.accum_target_shares and current_down >= self.accum_target_shares:
            # Already at target - just wait for rotation
            if self._rotator.should_rotate():
                await self._handle_market_rotation(market)
            return

        # Get current prices with retry logic
        opportunity = None
        for attempt in range(self.max_retries):
            try:
                opportunity = await self._analyzer.analyze_asymmetric_opportunity(
                    market=market,
                    current_up_size=current_up,
                    current_down_size=current_down,
                    current_up_cost=current_up_cost,
                    current_down_cost=current_down_cost,
                    pair_cost_threshold=1.00,  # We use our own limit
                )
                self._consecutive_failures = 0
                break
            except Exception as e:
                self._consecutive_failures += 1
                delay = min(self.retry_base_delay * (2 ** attempt), self.retry_max_delay)
                if attempt < self.max_retries - 1:
                    if not await self._interruptible_sleep(delay, check_interval=2.0):
                        return  # Stop requested, exit early

        if opportunity is None or opportunity.up_ask is None or opportunity.down_ask is None:
            if self._rotator.should_rotate():
                await self._handle_market_rotation(market)
            return

        self._opportunities_checked += 1

        # Update display prices
        self._last_up_price = opportunity.up_ask
        self._last_down_price = opportunity.down_ask
        self._last_spread = 1.0 - opportunity.up_ask - opportunity.down_ask
        self._update_live_display()

        # Get raw orderbook prices (ask = taker price)
        raw_up_ask = opportunity.up_ask
        raw_down_ask = opportunity.down_ask
        raw_up_bid = opportunity.up_bid or (raw_up_ask * 0.98 if raw_up_ask else 0.48)
        raw_down_bid = opportunity.down_bid or (raw_down_ask * 0.98 if raw_down_ask else 0.48)

        # MARKET TYPE DETECTION: Feed prices and detect after ~2 min of data
        if self._market_detector is None:
            self._market_detector = MarketTypeDetector()
        self._market_detector.add_price("UP", raw_up_ask)
        self._market_detector.add_price("DOWN", raw_down_ask)

        # Detect market type once we have enough data (only detect once)
        if not self._market_detector.detected and self._market_detector.has_enough_data():
            self._detected_market_type = self._market_detector.detect()
            params = self._market_detector.get_recommended_params()
            logger.info(
                f"[MARKET_DETECTOR] Detected: {self._detected_market_type} | "
                f"Recommended: chase<=${params['max_chase_price']:.2f}, "
                f"emergency>{params['emergency_threshold']}, "
                f"max_buys={params['max_buys_per_side']}"
            )

        # Calculate current average pair cost
        current_avg_pair_cost = 0.0
        if position and position.pair_count > 0:
            current_avg_pair_cost = position.up_avg_price + position.down_avg_price

        # Calculate imbalance in shares
        share_imbalance = abs(current_up - current_down)
        deficit_side = "UP" if current_down > current_up else "DOWN" if current_up > current_down else None

        # Calculate time remaining for dynamic sizing and patient pricing
        time_remaining_secs = 900  # Default 15 min
        if market.end_time:
            time_remaining_secs = max(0, (market.end_time - datetime.now(timezone.utc)).total_seconds())

        # Calculate max imbalance for emergency determination
        if self.accum_mode == "volume_weighted":
            max_imbalance_check = self.get_vw_max_imbalance(current_up, current_down)
        else:
            max_imbalance_check = max(int(self.accum_max_imbalance_pct * self.accum_target_shares), 2)

        # Determine if this is an emergency hedge situation
        # Emergency triggers:
        # 1. Imbalance exceeds time-based threshold (10 early, 5 late)
        # 2. Emergency cooldown has passed (30s between emergency orders)
        #
        # NOTE: Chase exhaustion is handled in calculate_gradual_chase_price()
        # which leaves order at final price. Emergency only triggers for severe imbalance.
        #
        # IMPORTANT: ONE_BUY mode NEVER uses emergency mode
        # - Single order at threshold, no chasing, accept outcome
        if self.accum_mode == "one_buy":
            is_emergency_up = False
            is_emergency_down = False
        else:
            # Get time-based emergency threshold (10 early, 5 late)
            emergency_threshold = get_emergency_threshold(time_remaining_secs)

            # Check imbalance against time-based threshold
            is_emergency_up = share_imbalance > emergency_threshold and deficit_side == "UP"
            is_emergency_down = share_imbalance > emergency_threshold and deficit_side == "DOWN"

            # Apply emergency cooldown (30s between emergency orders per market)
            EMERGENCY_COOLDOWN_SECS = 30.0
            current_time = time.time()

            if is_emergency_up or is_emergency_down:
                last_emergency = self._last_emergency_time.get(market.slug, 0)
                if current_time - last_emergency < EMERGENCY_COOLDOWN_SECS:
                    # Cooldown not passed - skip emergency
                    logger.debug(
                        f"[EMERGENCY_COOLDOWN] Skipping emergency, "
                        f"{EMERGENCY_COOLDOWN_SECS - (current_time - last_emergency):.0f}s remaining"
                    )
                    is_emergency_up = False
                    is_emergency_down = False
                else:
                    # Log emergency trigger
                    emergency_side = "UP" if is_emergency_up else "DOWN"
                    logger.info(
                        f"[EMERGENCY] {emergency_side}: imbalance={share_imbalance} > threshold={emergency_threshold} "
                        f"(time_left={time_remaining_secs:.0f}s)"
                    )
                    self._last_emergency_time[market.slug] = current_time

                    # Cancel any pending chased order for the emergency side (prevent double fill)
                    if hasattr(self._engine, 'cancel_pending_order'):
                        pending_key = f"{market.slug}_{emergency_side}"
                        try:
                            cancelled = await self._engine.cancel_pending_order(pending_key)
                            if cancelled:
                                logger.info(f"[EMERGENCY] Cancelled chased order for {emergency_side} before emergency fill")
                                # Reset replacement count since we're starting fresh
                                self._replacement_count.pop(pending_key, None)
                                self._chase_exhausted_logged.discard(pending_key)
                        except Exception as e:
                            logger.warning(f"[EMERGENCY] Failed to cancel pending order: {e}")

        # Initialize fair values (used by FV_MM mode for buy decisions)
        fair_up = 0.50
        fair_down = 0.50

        # PATIENT PRICING - Graduated pricing relative to best_bid
        # Early: bid-0.03, Mid: bid-0.02, Late: bid-0.01, Final: bid, Emergency: ask
        up_price = get_patient_price(raw_up_bid, raw_up_ask, time_remaining_secs, is_emergency_up)
        down_price = get_patient_price(raw_down_bid, raw_down_ask, time_remaining_secs, is_emergency_down)

        # ONE BUY MODE: Use threshold price as limit order (not patient pricing)
        # This places orders at exactly the threshold - fills at ask or better
        # Supports fixed OR dynamic threshold (time-based)
        if self.accum_mode == "one_buy" and self._one_buy_strategy:
            threshold = self._one_buy_strategy.get_threshold(time_remaining_secs)
            up_price = threshold
            down_price = threshold
            price_mode = "1BUY_DYN" if self._one_buy_strategy.dynamic_threshold else "1BUY_FIX"
        # FAIR VALUE MM MODE: Price based on Binance signal, like real MMs
        # Posts at fair_value - edge, not blind bid-X
        elif self.accum_mode == "fair_value_mm" and self._binance_client:
            # Get price vs strike from Binance
            price_vs_strike = self._binance_client.price_vs_strike_pct

            # Calculate fair values with time-decaying sensitivity
            fair_up, fair_down = calculate_fair_value(
                price_vs_strike_pct=price_vs_strike,
                time_remaining_secs=time_remaining_secs,
                sensitivity_early=self.fv_sensitivity_early,
                sensitivity_late=self.fv_sensitivity_late,
            )

            # Post at fair value - edge (e.g., 55c - 2c = 53c)
            up_price = max(0.01, fair_up - self.fv_edge)
            down_price = max(0.01, fair_down - self.fv_edge)

            # CRITICAL: Cap at ask price - never post ABOVE the ask
            # If fair value > ask, that's a great deal - buy at ask!
            # This happens when market is mispriced in our favor.
            if up_price > raw_up_ask:
                up_price = raw_up_ask
            if down_price > raw_down_ask:
                down_price = raw_down_ask

            # Emergency: use ask for immediate fill (override fair value)
            if is_emergency_up:
                up_price = raw_up_ask
            if is_emergency_down:
                down_price = raw_down_ask

            # Track fair value for repricing (cancel if fair value changes significantly)
            current_fair = (fair_up, fair_down)

            # Log fair value pricing
            price_mode = "FV_MM"
            if self._opportunities_checked % 10 == 0 and not self.quiet_mode:
                logger.info(
                    f"[FV_MM] BTC vs strike: {price_vs_strike:+.4f}% | "
                    f"Fair: UP=${fair_up:.3f} DOWN=${fair_down:.3f} | "
                    f"Post: UP=${up_price:.3f} DOWN=${down_price:.3f} (edge=${self.fv_edge:.2f})"
                )
        else:
            # Determine price mode for logging (non-one_buy modes)
            if is_emergency_up or is_emergency_down:
                price_mode = "EMERGENCY"
            elif time_remaining_secs >= 600:
                price_mode = "EARLY"
            elif time_remaining_secs >= 300:
                price_mode = "MID"
            elif time_remaining_secs >= 120:
                price_mode = "LATE"
            else:
                price_mode = "FINAL"

        pair_cost = up_price + down_price

        # Log pair cost status with price mode
        # Show spread info: how far our price is from best_ask
        up_spread = raw_up_ask - up_price
        down_spread = raw_down_ask - down_price
        if self._opportunities_checked % 10 == 0 and not self.quiet_mode:
            logger.info(
                f"[ACCUM] [{price_mode}] UP=${up_price:.3f} (ask=${raw_up_ask:.3f}, spread=${up_spread:.3f}) "
                f"DOWN=${down_price:.3f} (ask=${raw_down_ask:.3f}, spread=${down_spread:.3f}) PairCost=${pair_cost:.4f}"
            )

        # Check if loss limit has been reached - stop trading new orders
        if self.loss_limit_reached:
            if self._opportunities_checked % 30 == 0:  # Log every 30 checks
                logger.warning(f"[LOSS LIMIT] Trading stopped. Cumulative P&L: ${self.cumulative_pnl:.2f}")
            # Still check for rotation so existing positions can resolve
            if self._rotator and self._rotator.should_rotate():
                await self._handle_market_rotation(market)
            return

        # =================================================================
        # MARKET OPEN GATE (first 60s) - APPLIES TO ALL STRATEGIES
        # =================================================================
        # Wait for balanced prices before first entry (Gabagool/Baguette behavior)
        # This check runs BEFORE mode-specific should_buy() checks
        if current_up == 0 and current_down == 0:
            time_elapsed_secs = 900 - time_remaining_secs
            can_enter, gate_reason = should_enter_at_open(
                up_price=raw_up_ask,
                down_price=raw_down_ask,
                time_elapsed=time_elapsed_secs,
                balance_min=0.35,
                balance_max=0.65,
                gate_duration=60.0,
            )
            if not can_enter:
                if self._opportunities_checked % 10 == 0:
                    logger.info(f"[OPEN_GATE] {gate_reason}")
                return

        # Determine what to buy this cycle
        buy_up = False
        buy_down = False

        # ENHANCED ONE BUY MODE: Single order per side, no chasing, pair cost gating
        if self.accum_mode == "one_buy" and self._one_buy_strategy:
            threshold = self._one_buy_strategy.get_threshold(time_remaining_secs)
            status = self._one_buy_strategy.get_status()

            # Check if both sides filled (hedged) or both attempted
            if status["fully_hedged"]:
                if self._opportunities_checked % 30 == 0:
                    pair_cost = status.get("pair_cost", 0)
                    logger.info(f"[1BUY] HEDGED: pair_cost=${pair_cost:.2f} - waiting for resolution")
                if self._rotator.should_rotate():
                    await self._handle_market_rotation(market)
                return

            # Use should_buy() which checks: not filled, price <= threshold, pair cost OK
            buy_up = self._one_buy_strategy.should_buy("UP", up_ask, time_remaining_secs)
            buy_down = self._one_buy_strategy.should_buy("DOWN", down_ask, time_remaining_secs)

            # Log status
            if buy_up:
                logger.info(f"[1BUY] BUY UP: ${up_ask:.2f} <= ${threshold:.2f}")
            if buy_down:
                logger.info(f"[1BUY] BUY DOWN: ${down_ask:.2f} <= ${threshold:.2f}")

            # If neither side buyable, explain why
            if not buy_up and not buy_down:
                reasons = []
                if status["up_filled"]:
                    reasons.append(f"UP filled@${status['up_fill_price']:.2f}")
                elif up_ask > threshold:
                    reasons.append(f"UP ${up_ask:.2f}>${threshold:.2f}")
                if status["down_filled"]:
                    reasons.append(f"DOWN filled@${status['down_fill_price']:.2f}")
                elif down_ask > threshold:
                    reasons.append(f"DOWN ${down_ask:.2f}>${threshold:.2f}")

                # Check pair cost rejection
                if status["up_filled"] and not status["down_filled"] and down_ask <= threshold:
                    proj_pair = status["up_fill_price"] + down_ask
                    if proj_pair > self._one_buy_strategy.max_pair_cost:
                        reasons.append(f"pair ${proj_pair:.2f}>${self._one_buy_strategy.max_pair_cost}")
                if status["down_filled"] and not status["up_filled"] and up_ask <= threshold:
                    proj_pair = status["down_fill_price"] + up_ask
                    if proj_pair > self._one_buy_strategy.max_pair_cost:
                        reasons.append(f"pair ${proj_pair:.2f}>${self._one_buy_strategy.max_pair_cost}")

                if self._opportunities_checked % 10 == 0:
                    logger.info(f"[1BUY] Skip: {', '.join(reasons)}")
                if self._rotator.should_rotate():
                    await self._handle_market_rotation(market)
                return

            # Fixed size for the order
            buy_size = self._one_buy_strategy.get_size()

        # CALCULUS MAKER MODE: Use strategy's should_buy() and get_size()
        elif self.accum_mode == "calculus_maker" and self._calculus_strategy:
            threshold = self._calculus_strategy.get_threshold(time_remaining_secs)
            mispricing = 1.0 - pair_cost
            max_pair_cost = 1.0 - threshold

            # Check if pair cost meets mispricing threshold for current time
            # BUT: Skip this check in EMERGENCY - must hedge at all costs
            is_emergency = is_emergency_up or is_emergency_down
            if not is_emergency and not self._calculus_strategy.should_buy(pair_cost, time_remaining_secs):
                # Log every 10 checks at INFO so user sees why no trades
                if self._opportunities_checked % 10 == 0:
                    logger.info(
                        f"[CALC] Waiting: pair_cost=${pair_cost:.3f} > max=${max_pair_cost:.3f} "
                        f"(need {threshold:.1%} edge, have {mispricing:.1%}) time={time_remaining_secs:.0f}s"
                    )
                # Check for rotation even when not trading
                if self._rotator.should_rotate():
                    await self._handle_market_rotation(market)
                return

            if is_emergency:
                logger.info(f"[CALC] 🚨 EMERGENCY HEDGE: imbalance={share_imbalance}, forcing trade at pair cost ${pair_cost:.3f}")

            # Use calculus strategy's size (quadratic ramp: 5 early → 50 late)
            buy_size = self._calculus_strategy.get_size(time_remaining_secs)
            logger.info(f"[CALC] ✅ TRADING: mispricing {mispricing:.1%} >= threshold {threshold:.1%}, size={buy_size}")
        # FAIR VALUE MM MODE: Reuses calculus strategy's should_buy() and get_size()
        # Pricing differs (fair value instead of patient bid), but threshold/sizing reused
        elif self.accum_mode == "fair_value_mm" and self._calculus_strategy:
            threshold = self._calculus_strategy.get_threshold(time_remaining_secs)
            mispricing = 1.0 - pair_cost
            max_pair_cost = 1.0 - threshold

            # Check if pair cost meets mispricing threshold for current time
            is_emergency = is_emergency_up or is_emergency_down
            if not is_emergency and not self._calculus_strategy.should_buy(pair_cost, time_remaining_secs):
                if self._opportunities_checked % 10 == 0:
                    logger.info(
                        f"[FV_MM] Waiting: pair_cost=${pair_cost:.3f} > max=${max_pair_cost:.3f} "
                        f"(need {threshold:.1%} edge, have {mispricing:.1%}) time={time_remaining_secs:.0f}s"
                    )
                if self._rotator.should_rotate():
                    await self._handle_market_rotation(market)
                return

            if is_emergency:
                logger.info(f"[FV_MM] 🚨 EMERGENCY HEDGE: imbalance={share_imbalance}, forcing trade at pair cost ${pair_cost:.3f}")

            # REPRICING: Check if fair value changed significantly since order was placed
            # Cancel pending orders if fair value moved more than fv_reprice_threshold
            for side in ["UP", "DOWN"]:
                order_key = f"{market.slug}_{side}"
                if order_key in self._order_fair_value:
                    old_fair_up, old_fair_down = self._order_fair_value[order_key]
                    # Get current fair value (already calculated in pricing section)
                    new_fair_up, new_fair_down = current_fair if 'current_fair' in dir() else (0.5, 0.5)
                    fair_change = abs(new_fair_up - old_fair_up)
                    if fair_change > self.fv_reprice_threshold:
                        logger.info(
                            f"[FV_MM] REPRICING {side}: fair value changed ${fair_change:.3f} > ${self.fv_reprice_threshold:.3f} "
                            f"(old=${old_fair_up:.3f}, new=${new_fair_up:.3f})"
                        )
                        # Cancel the pending order for repricing
                        if hasattr(self._engine, 'cancel_pending_order'):
                            try:
                                cancelled = await self._engine.cancel_pending_order(order_key)
                                if cancelled:
                                    logger.info(f"[FV_MM] Cancelled {side} order for repricing")
                                    self._replacement_count.pop(order_key, None)
                            except Exception as e:
                                logger.warning(f"[FV_MM] Failed to cancel for repricing: {e}")
                        # Remove from tracking
                        self._order_fair_value.pop(order_key, None)

            # Use calculus strategy's size (quadratic ramp: 5 early → 50 late)
            buy_size = self._calculus_strategy.get_size(time_remaining_secs)
            logger.info(f"[FV_MM] ✅ TRADING: mispricing {mispricing:.1%} >= threshold {threshold:.1%}, size={buy_size}")
        else:
            # Dynamic sizing: 20% at 15min → 10% at 5min → 2% at 0min
            # Minimum 5 shares per Polymarket requirement
            buy_size = calculate_dynamic_trade_size(
                time_remaining_secs=time_remaining_secs,
                max_target_shares=self.accum_target_shares,
                min_size=5  # Polymarket minimum order size
            )

        # NEAR-TARGET CHECK: If one side reached target, be careful about final orders
        max_side = max(current_up, current_down)
        near_target = max_side >= self.accum_target_shares

        # Initialize trend_signal here so it's defined in ALL code paths
        # (fixes UnboundLocalError when near_target branch is taken)
        trend_signal = None

        if near_target:
            # One side is at target - check if perfectly balanced or need more
            if share_imbalance == 0:
                # PERFECTLY BALANCED - STOP
                logger.info(
                    f"✓ PERFECTLY BALANCED: {current_up:.0f} UP / {current_down:.0f} DOWN. DONE."
                )
                self._send_web_update()
                return  # Don't place any more orders

            # Need to balance - send ONE order at a time (5 shares min)
            # This prevents overshooting by sending multiple orders
            if current_up > current_down:
                # Need more DOWN to balance - cap at target
                room_to_target = max(0, self.accum_target_shares - current_down)
                if room_to_target == 0:
                    # DOWN already at target, can't buy more - STOP
                    logger.info(
                        f"✓ BOTH AT TARGET: {current_up:.0f} UP / {current_down:.0f} DOWN. DONE."
                    )
                    self._send_web_update()
                    return
                shares_needed = min(5, int(share_imbalance), room_to_target)
                shares_needed = max(5, shares_needed)  # Polymarket min
                logger.info(
                    f"⚠️ BALANCING: {current_up:.0f} UP / {current_down:.0f} DOWN, "
                    f"buying {shares_needed} DOWN (capped at target {self.accum_target_shares})"
                )
                buy_up = False
                buy_down = True
                buy_size = shares_needed
            else:
                # Need more UP to balance - cap at target
                room_to_target = max(0, self.accum_target_shares - current_up)
                if room_to_target == 0:
                    # UP already at target, can't buy more - STOP
                    logger.info(
                        f"✓ BOTH AT TARGET: {current_up:.0f} UP / {current_down:.0f} DOWN. DONE."
                    )
                    self._send_web_update()
                    return
                shares_needed = min(5, int(share_imbalance), room_to_target)
                shares_needed = max(5, shares_needed)  # Polymarket min
                logger.info(
                    f"⚠️ BALANCING: {current_up:.0f} UP / {current_down:.0f} DOWN, "
                    f"buying {shares_needed} UP (capped at target {self.accum_target_shares})"
                )
                buy_up = True
                buy_down = False
                buy_size = shares_needed

            # Skip the normal rebalance logic below - we've handled it
            force_rebalance = True
            # Jump to order execution (skip the else block below)

        # BALANCE-FIRST LOGIC: If imbalanced beyond threshold, prioritize deficit side
        if not near_target:
            force_rebalance = False
        # max_imbalance_check already calculated above for patient pricing
        max_imbalance = max_imbalance_check
        if not near_target and share_imbalance > max_imbalance:
            force_rebalance = True
            # Force rebalance - only buy deficit side
            #
            # FIX: Buy EXACTLY the imbalance amount to reach balance (not more!)
            # Old logic tried to buy 2x dynamic size which could flip imbalance
            # and then get blocked by the imbalance enforcement check.
            #
            # Example: 10 UP / 5 DOWN (imbalance=5)
            #   Old: buy 10 DOWN → 10/15 → imbalance=5 (flipped!) → BLOCKED
            #   New: buy 5 DOWN  → 10/10 → imbalance=0 ✓
            #
            rebalance_size = int(share_imbalance)  # Exact amount to balance
            rebalance_size = max(5, rebalance_size)  # Polymarket min is 5 shares

            if deficit_side == "UP" and current_up < self.accum_target_shares:
                buy_up = True
                buy_down = False
                buy_size = min(rebalance_size, self.accum_target_shares - current_up)
                logger.info(f"REBALANCE: Buying {buy_size} UP to fix imbalance ({share_imbalance:.0f} → ~0)")
            elif deficit_side == "DOWN" and current_down < self.accum_target_shares:
                buy_up = False
                buy_down = True
                buy_size = min(rebalance_size, self.accum_target_shares - current_down)
                logger.info(f"REBALANCE: Buying {buy_size} DOWN to fix imbalance ({share_imbalance:.0f} → ~0)")
        elif not near_target:
            # =================================================================
            # TRENDING MARKET IMPROVEMENTS (from Telegram alpha)
            # =================================================================
            # NOTE: Market Open Gate check moved earlier (before mode-specific checks)
            # 1. Get trend signal from Binance
            # 2. Reduce target in strong trends (15→10)
            # 3. Buy expensive side FIRST in trends (prevents leg loss)
            # 4. Check prospective pair cost with MARKET prices
            trend_signal = None
            dynamic_target = self.accum_target_shares

            if self._trend_detector:
                trend_signal = self._trend_detector.get_trend_signal()

                # Dynamic target reduction: 15→10 in strong trends
                if trend_signal.state in (TrendState.STRONG, TrendState.EXTREME):
                    dynamic_target = self._trend_detector.get_dynamic_target(
                        self.accum_target_shares, time_remaining_secs
                    )
                    if dynamic_target < self.accum_target_shares:
                        logger.info(
                            f"[TREND] Reducing target: {self.accum_target_shares} → {dynamic_target} "
                            f"(state={trend_signal.state.value}, z={trend_signal.z_score:.2f})"
                        )

            # Check if already at dynamic target (early stop in trends)
            if current_up >= dynamic_target and current_down >= dynamic_target:
                logger.info(f"[TREND] At dynamic target {dynamic_target}/{dynamic_target}, stopping accumulation")
                buy_up = False
                buy_down = False
            elif self.accum_mode == "fair_value_mm":
                # FAIR VALUE MM: Only buy UNDERVALUED sides (fair_value > market_price)
                # This is fundamentally different from calculus_maker which buys both sides
                # We skip TREND_GATE since we're being selective based on fair value
                buy_up = current_up < dynamic_target and fair_up > raw_up_ask
                buy_down = current_down < dynamic_target and fair_down > raw_down_ask

                if buy_up or buy_down:
                    logger.info(
                        f"[FV_MM] Undervalued: "
                        f"{'UP ' if buy_up else ''}(fair={fair_up:.2f} > ask={raw_up_ask:.2f}) "
                        f"{'DOWN ' if buy_down else ''}(fair={fair_down:.2f} > ask={raw_down_ask:.2f})"
                    )
                else:
                    # Both sides overvalued - don't buy anything
                    if self._opportunities_checked % 20 == 0:
                        logger.info(
                            f"[FV_MM] No undervalued sides: "
                            f"UP (fair={fair_up:.2f} <= ask={raw_up_ask:.2f}), "
                            f"DOWN (fair={fair_down:.2f} <= ask={raw_down_ask:.2f})"
                        )
            elif self.accum_buy_both_sides:
                # Normal accumulation - buy both sides if possible
                buy_up = current_up < dynamic_target
                buy_down = current_down < dynamic_target

                # DIRECTION-AWARE SIDE PRIORITY (Telegram alpha)
                # In strong trends, buy the WINNING side first (it's getting expensive)
                if trend_signal and trend_signal.state in (TrendState.STRONG, TrendState.EXTREME):
                    priority_side = self._trend_detector.get_priority_side()
                    if priority_side:
                        if priority_side == "UP" and buy_up and buy_down:
                            # Prioritize UP - buy it first, then DOWN
                            logger.debug(f"[TREND_PRIORITY] UP first (trending UP, z={trend_signal.z_score:.2f})")
                        elif priority_side == "DOWN" and buy_up and buy_down:
                            # Prioritize DOWN - buy it first, then UP
                            logger.debug(f"[TREND_PRIORITY] DOWN first (trending DOWN, z={trend_signal.z_score:.2f})")

                # PROSPECTIVE PAIR COST WITH MARKET CHECK (trending market protection)
                # Block buys if hedge at current market would be unprofitable
                # Use same threshold as main pair cost limit for consistency
                trend_gate_threshold = self.calc_max_pair_cost  # Default 0.995
                if buy_up and self._trend_detector and trend_signal:
                    if trend_signal.state in (TrendState.STRONG, TrendState.EXTREME):
                        can_buy, proj_cost, reason = check_prospective_pair_cost_with_market(
                            side="UP",
                            buy_price=up_price,
                            other_side_best_ask=raw_down_ask,  # What DOWN hedge would cost NOW
                            max_pair_cost=trend_gate_threshold
                        )
                        if not can_buy:
                            buy_up = False
                            logger.info(f"[TREND_GATE] BLOCKING UP: {reason}")

                if buy_down and self._trend_detector and trend_signal:
                    if trend_signal.state in (TrendState.STRONG, TrendState.EXTREME):
                        can_buy, proj_cost, reason = check_prospective_pair_cost_with_market(
                            side="DOWN",
                            buy_price=down_price,
                            other_side_best_ask=raw_up_ask,  # What UP hedge would cost NOW
                            max_pair_cost=trend_gate_threshold
                        )
                        if not can_buy:
                            buy_down = False
                            logger.info(f"[TREND_GATE] BLOCKING DOWN: {reason}")
            else:
                # Single side mode: Use direction-aware priority
                priority_side = None
                if trend_signal and trend_signal.state in (TrendState.STRONG, TrendState.EXTREME):
                    priority_side = self._trend_detector.get_priority_side()

                if priority_side == "UP" and current_up < dynamic_target:
                    buy_up = True
                    buy_down = False
                    logger.debug(f"[TREND_PRIORITY] Buying UP first (trending UP)")
                elif priority_side == "DOWN" and current_down < dynamic_target:
                    buy_up = False
                    buy_down = True
                    logger.debug(f"[TREND_PRIORITY] Buying DOWN first (trending DOWN)")
                else:
                    # Neutral: Standard alternating/cheaper-side logic
                    if current_up <= current_down and current_up < dynamic_target:
                        buy_up = True
                    elif current_down < dynamic_target:
                        buy_down = True

        # Check pair cost TARGET before buying (normal trading - buy cheap)
        # Use TARGET for normal trading, LIMIT only for rebalancing
        if not force_rebalance:
            if self.accum_mode in ("calculus_maker", "fair_value_mm"):
                # CALCULUS MAKER / FAIR VALUE MM MODE: Already checked via should_buy() above
                # Just log what we're doing
                threshold = self._calculus_strategy.get_threshold(time_remaining_secs) if self._calculus_strategy else 0
                mode_tag = "FV_MM" if self.accum_mode == "fair_value_mm" else "CALC"
                logger.debug(
                    f"[{mode_tag}] Trading: threshold={threshold:.1%}, pair_cost=${pair_cost:.4f}, "
                    f"buy_up={buy_up}, buy_down={buy_down}"
                )
            elif self.accum_mode == "volume_weighted":
                # TRUE GABAGOOL MODE: Continuous accumulation on BOTH sides
                # Analysis of gabagool22 (Dec 2024) revealed:
                # - NO selective buying - they buy at ALL prices ($0.02 to $0.98)
                # - NO cheap threshold - continuous buying both sides
                # - Volume weighting handles balance (buy more when cheap)
                # - Expensive buys on one side offset by cheap buys on other
                # - Final pair cost always ends under $1.00
                #
                # NO pair cost blocking in VW mode - allows temporary imbalances like Gabagool
                # Volume weighting naturally keeps final pair cost reasonable

                logger.debug(f"[VW] Gabagool mode: UP@${up_price:.3f} buy={buy_up}, DOWN@${down_price:.3f} buy={buy_down}")
            else:
                # STANDARD MODE: Use pair cost threshold
                if position and position.pair_count > 0:
                    # Calculate prospective pair cost if we buy
                    if buy_up:
                        new_up_cost = current_up_cost + (buy_size * up_price)
                        new_up_size = current_up + buy_size
                        new_up_avg = new_up_cost / new_up_size if new_up_size > 0 else up_price
                        prospective_pair_cost = new_up_avg + position.down_avg_price
                        if prospective_pair_cost > self.accum_pair_cost_target:
                            buy_up = False
                            logger.debug(f"Skip UP: prospective pair cost ${prospective_pair_cost:.4f} > target ${self.accum_pair_cost_target}")

                    if buy_down:
                        new_down_cost = current_down_cost + (buy_size * down_price)
                        new_down_size = current_down + buy_size
                        new_down_avg = new_down_cost / new_down_size if new_down_size > 0 else down_price
                        prospective_pair_cost = position.up_avg_price + new_down_avg
                        if prospective_pair_cost > self.accum_pair_cost_target:
                            buy_down = False
                            logger.debug(f"Skip DOWN: prospective pair cost ${prospective_pair_cost:.4f} > target ${self.accum_pair_cost_target}")
                else:
                    # No position yet - just check if pair cost is reasonable
                    if pair_cost > self.accum_pair_cost_target + 0.02:  # Give some slack for first trades
                        buy_up = False
                        buy_down = False
                        logger.debug(f"Skip: current pair cost ${pair_cost:.4f} too high")

        # FIX #4: ALLOW REBALANCING REGARDLESS OF PAIR COST
        # When imbalanced, reducing directional exposure is more important than pair cost
        # We log a warning but DO NOT block the rebalance
        if force_rebalance and position and position.pair_count > 0:
            if buy_up:
                new_up_cost = current_up_cost + (buy_size * up_price)
                new_up_size = current_up + buy_size
                new_up_avg = new_up_cost / new_up_size if new_up_size > 0 else up_price
                prospective_pair_cost = new_up_avg + position.down_avg_price
                if prospective_pair_cost > self.accum_pair_cost_limit:
                    # WARN but DO NOT block - rebalancing is critical
                    logger.warning(f"⚠️ REBALANCE PROCEEDING despite pair cost ${prospective_pair_cost:.4f} > limit ${self.accum_pair_cost_limit}")
            if buy_down:
                new_down_cost = current_down_cost + (buy_size * down_price)
                new_down_size = current_down + buy_size
                new_down_avg = new_down_cost / new_down_size if new_down_size > 0 else down_price
                prospective_pair_cost = position.up_avg_price + new_down_avg
                if prospective_pair_cost > self.accum_pair_cost_limit:
                    # WARN but DO NOT block - rebalancing is critical
                    logger.warning(f"⚠️ REBALANCE PROCEEDING despite pair cost ${prospective_pair_cost:.4f} > limit ${self.accum_pair_cost_limit}")

        # PRICE CEILING: Never buy shares above max price (prevents guaranteed losses)
        # EMERGENCY has higher ceiling ONLY in final minutes:
        #   - > 7 min: No emergency ceiling benefit (use normal ceiling)
        #   - ≤ 7 min: $0.75
        #   - ≤ 5 min: $0.88
        def get_emergency_price_ceiling(time_remaining: float) -> float:
            if time_remaining <= 300:  # Last 5 mins - more urgent
                return 0.88
            elif time_remaining <= 420:  # Last 7 mins
                return 0.75
            else:
                return None  # No emergency benefit when > 7 min

        if buy_up and up_price > self.accum_max_share_price:
            emergency_ceiling = get_emergency_price_ceiling(time_remaining_secs) if is_emergency_up else None
            if emergency_ceiling and up_price <= emergency_ceiling:
                logger.warning(f"⚠️ EMERGENCY HEDGE: UP ${up_price:.3f} (ceiling ${emergency_ceiling}, {time_remaining_secs:.0f}s left)")
            else:
                buy_up = False
                ceiling_used = emergency_ceiling if emergency_ceiling else self.accum_max_share_price
                logger.info(f"⛔ SKIP UP: price ${up_price:.3f} > ceiling ${ceiling_used}")
        if buy_down and down_price > self.accum_max_share_price:
            emergency_ceiling = get_emergency_price_ceiling(time_remaining_secs) if is_emergency_down else None
            if emergency_ceiling and down_price <= emergency_ceiling:
                logger.warning(f"⚠️ EMERGENCY HEDGE: DOWN ${down_price:.3f} (ceiling ${emergency_ceiling}, {time_remaining_secs:.0f}s left)")
            else:
                buy_down = False
                ceiling_used = emergency_ceiling if emergency_ceiling else self.accum_max_share_price
                logger.info(f"⛔ SKIP DOWN: price ${down_price:.3f} > ceiling ${ceiling_used}")

        # GABAGOOL-STYLE VOLUME WEIGHTING: Percentage of remaining capacity, weighted by price
        # Only applies in volume_weighted mode
        if self.accum_mode == "volume_weighted":
            up_remaining = max(0, int(self.accum_target_shares - current_up))
            down_remaining = max(0, int(self.accum_target_shares - current_down))
            up_buy_size = self.get_vw_trade_size(up_price, up_remaining)
            down_buy_size = self.get_vw_trade_size(down_price, down_remaining)
            logger.debug(f"[VW] UP: {up_remaining} remaining → buy {up_buy_size} @ ${up_price:.3f} ({int(up_buy_size/max(1,up_remaining)*100) if up_remaining > 0 else 0}%)")
            logger.debug(f"[VW] DOWN: {down_remaining} remaining → buy {down_buy_size} @ ${down_price:.3f} ({int(down_buy_size/max(1,down_remaining)*100) if down_remaining > 0 else 0}%)")
        elif self.accum_mode == "one_buy" and self._one_buy_strategy:
            # ONE BUY: Use remaining quantity for each side (handles partial fills)
            up_buy_size = self._one_buy_strategy.get_remaining("UP")
            down_buy_size = self._one_buy_strategy.get_remaining("DOWN")
            logger.debug(f"[1BUY] UP: need {up_buy_size}, DOWN: need {down_buy_size}")
        else:
            up_buy_size = buy_size
            down_buy_size = buy_size

        # POLYMARKET $1 MINIMUM ORDER VALUE ENFORCEMENT
        # Orders must have value >= $1.00, so round up size if needed
        # Formula: min_size = ceil(1.00 / price)
        up_min_size = max(1, math.ceil(1.00 / up_price)) if up_price > 0 else 1
        down_min_size = max(1, math.ceil(1.00 / down_price)) if down_price > 0 else 1

        if buy_up and up_buy_size < up_min_size:
            logger.debug(f"[MIN$1] UP size {up_buy_size} → {up_min_size} (${up_price:.3f} × {up_min_size} = ${up_price * up_min_size:.2f})")
            up_buy_size = up_min_size

        if buy_down and down_buy_size < down_min_size:
            logger.debug(f"[MIN$1] DOWN size {down_buy_size} → {down_min_size} (${down_price:.3f} × {down_min_size} = ${down_price * down_min_size:.2f})")
            down_buy_size = down_min_size

        # POLYMARKET MINIMUM 5 SHARES ENFORCEMENT
        # Polymarket requires minimum 5 shares per order
        MIN_SHARES = 5
        if buy_up and up_buy_size < MIN_SHARES:
            logger.debug(f"[MIN5] UP size {up_buy_size} → {MIN_SHARES} (Polymarket minimum)")
            up_buy_size = MIN_SHARES

        if buy_down and down_buy_size < MIN_SHARES:
            logger.debug(f"[MIN5] DOWN size {down_buy_size} → {MIN_SHARES} (Polymarket minimum)")
            down_buy_size = MIN_SHARES

        # TARGET CAP: Don't exceed target even with minimum enforcement
        # This prevents MIN_SHARES from pushing us over the target
        up_remaining = max(0, self.accum_target_shares - current_up)
        down_remaining = max(0, self.accum_target_shares - current_down)

        if buy_up and up_buy_size > up_remaining:
            if up_remaining >= 1:
                logger.debug(f"[TARGET_CAP] UP size {up_buy_size} → {up_remaining} (approaching target)")
                up_buy_size = up_remaining
            else:
                logger.debug(f"[TARGET] UP at target ({current_up}/{self.accum_target_shares}), skipping buy")
                buy_up = False
                up_buy_size = 0

        if buy_down and down_buy_size > down_remaining:
            if down_remaining >= 1:
                logger.debug(f"[TARGET_CAP] DOWN size {down_buy_size} → {down_remaining} (approaching target)")
                down_buy_size = down_remaining
            else:
                logger.debug(f"[TARGET] DOWN at target ({current_down}/{self.accum_target_shares}), skipping buy")
                buy_down = False
                down_buy_size = 0

        # HARD CAP: Final check - skip if at or above target (safety net)
        if current_up >= self.accum_target_shares:
            buy_up = False
            up_buy_size = 0

        if current_down >= self.accum_target_shares:
            buy_down = False
            down_buy_size = 0

        # IMBALANCE ENFORCEMENT: Applies to ALL modes to prevent one-sided exposure
        # VW mode uses more permissive limit via get_vw_max_imbalance (min 10, or 20% of position)
        # Standard mode uses accum_max_imbalance_pct (default 15%)
        #
        # KEY FIX: When buying BOTH sides, check NET imbalance after both trades
        # This allows balanced pair buying even when each side individually exceeds limit
        mode_label = "[VW]" if self.accum_mode == "volume_weighted" else ""

        if self.accum_mode == "fair_value_mm":
            # FAIR VALUE MM: Still enforce HARD MAX imbalance limit for safety
            # Even directional strategies must respect absolute risk limits
            current_imbalance = abs(current_up - current_down)
            # Hard limit: max 10 shares imbalance OR 200% of target (whichever is larger)
            fv_hard_limit = max(10, int(self.accum_target_shares * 2))

            if current_imbalance >= fv_hard_limit:
                # Determine which side is surplus
                surplus_side = "UP" if current_up > current_down else "DOWN"

                # Block the surplus side, allow deficit side
                if surplus_side == "UP" and buy_up:
                    buy_up = False
                    logger.warning(f"⛔ [FV_MM] BLOCKED UP: hard limit {current_imbalance:.0f} >= {fv_hard_limit}")
                elif surplus_side == "DOWN" and buy_down:
                    buy_down = False
                    logger.warning(f"⛔ [FV_MM] BLOCKED DOWN: hard limit {current_imbalance:.0f} >= {fv_hard_limit}")
            else:
                logger.debug(f"[FV_MM] Imbalance {current_imbalance:.0f} < hard limit {fv_hard_limit}")
        else:
            # ============================================================================
            # IMBALANCE BLOCKING (FIXED: Block BOTH sides when imbalanced)
            # ============================================================================
            # BUG FIX: Previously only blocked the surplus side, letting the other through.
            # This caused 30 UP / 10 DOWN because UP kept filling while DOWN didn't.
            #
            # NEW LOGIC:
            # 1. If currently imbalanced, block BOTH sides unless trade REDUCES imbalance
            # 2. Never let one side run ahead while waiting for the other to catch up
            # 3. Only trades that bring us closer to balance are allowed
            # ============================================================================
            current_imbalance = abs(current_up - current_down)

            # Calculate what imbalance would be after each trade
            new_up_imbalance = abs((current_up + up_buy_size) - current_down) if buy_up else current_imbalance
            new_down_imbalance = abs(current_up - (current_down + down_buy_size)) if buy_down else current_imbalance

            # If already over limit, only allow trades that REDUCE imbalance
            if current_imbalance > max_imbalance:
                # We're already imbalanced - be very strict
                if buy_up:
                    if new_up_imbalance >= current_imbalance:
                        buy_up = False
                        logger.info(f"⛔ {mode_label} BLOCKED UP: imbalanced {current_imbalance:.0f} > limit, UP doesn't reduce")
                    else:
                        logger.info(f"✅ {mode_label} ALLOWING UP: reduces imbalance {current_imbalance:.0f} → {new_up_imbalance:.0f}")

                if buy_down:
                    if new_down_imbalance >= current_imbalance:
                        buy_down = False
                        logger.info(f"⛔ {mode_label} BLOCKED DOWN: imbalanced {current_imbalance:.0f} > limit, DOWN doesn't reduce")
                    else:
                        logger.info(f"✅ {mode_label} ALLOWING DOWN: reduces imbalance {current_imbalance:.0f} → {new_down_imbalance:.0f}")

            # If buying both sides, check net result
            elif buy_up and buy_down:
                new_up = current_up + up_buy_size
                new_down = current_down + down_buy_size
                net_imbalance = abs(new_up - new_down)

                if net_imbalance > max_imbalance:
                    # CRITICAL FIX: Block BOTH sides, not just surplus side
                    # This prevents runaway accumulation when one side fills and other doesn't
                    buy_up = False
                    buy_down = False
                    surplus_side = "UP" if new_up > new_down else "DOWN"
                    logger.info(
                        f"⛔ {mode_label} BLOCKED BOTH: net imbalance {net_imbalance:.0f} > limit {max_imbalance} "
                        f"({surplus_side} surplus). Waiting for balance."
                    )

            # Buying only one side - check if it would exceed limit
            else:
                if buy_up:
                    if new_up_imbalance > max_imbalance:
                        buy_up = False
                        logger.info(f"⛔ {mode_label} BLOCKED UP: would create imbalance {new_up_imbalance:.0f} > limit {max_imbalance}")

                if buy_down:
                    if new_down_imbalance > max_imbalance:
                        buy_down = False
                        logger.info(f"⛔ {mode_label} BLOCKED DOWN: would create imbalance {new_down_imbalance:.0f} > limit {max_imbalance}")

        # Execute trades with DYNAMIC ORDERING
        # Buy cheaper side first to minimize slippage risk
        trades_made = 0

        # Get health monitor for trade recording
        health_monitor = get_health_monitor()

        # Build order objects
        up_order = {
            "side": "UP",
            "price": up_price,
            "size": up_buy_size,
            "best_ask": raw_up_ask,
            "best_bid": raw_up_bid,
        } if buy_up else None

        down_order = {
            "side": "DOWN",
            "price": down_price,
            "size": down_buy_size,
            "best_ask": raw_down_ask,
            "best_bid": raw_down_bid,
        } if buy_down else None

        pending_trades = []

        if self.sequential_ordering_enabled:
            # ============================================================================
            # FILL-VERIFIED SEQUENTIAL PAIRING (30/10 disaster fix)
            # ============================================================================
            # BUG FIX: Previous logic used position to decide ordering, but position
            # doesn't update until fills propagate (2+ seconds). On fast VPS, multiple
            # cycles could run before position updated, placing duplicate expensive orders.
            #
            # NEW LOGIC:
            # 1. Track pending orders per market
            # 2. If expensive side has pending order, DON'T place anything new
            # 3. Only when expensive side CONFIRMED FILLED, place hedge
            # 4. Uses pending order tracking instead of stale position data
            # ============================================================================

            up_is_expensive = raw_up_ask >= raw_down_ask
            expensive_side = "UP" if up_is_expensive else "DOWN"
            cheap_side = "DOWN" if up_is_expensive else "UP"

            # Initialize pending order tracking if not exists
            if not hasattr(self, '_pending_expensive_orders'):
                self._pending_expensive_orders = {}  # market_slug -> {"side": str, "placed_at": time}

            pending_key = market.slug
            pending_info = self._pending_expensive_orders.get(pending_key)

            # Check if we have a pending expensive order
            if pending_info:
                pending_side = pending_info["side"]
                pending_time = pending_info.get("placed_at", 0)
                pending_age = time.time() - pending_time

                # Check if it filled (position increased for that side)
                if pending_side == "UP":
                    expected_fill = pending_info.get("expected_size", 5)
                    # Check against last known position
                    last_pos = pending_info.get("position_when_placed", 0)
                    if current_up > last_pos:
                        # Expensive side filled! Check if instant hedge already placed
                        if pending_info.get("hedge_placed"):
                            # WebSocket instant hedge already handled this - just clear tracking
                            del self._pending_expensive_orders[pending_key]
                            logger.info(
                                f"[SEQ_PAIR] ✓ UP filled, instant hedge already placed via WebSocket. Skipping."
                            )
                            # Don't place duplicate hedge - instant hedge handled it
                        else:
                            # No instant hedge yet - place hedge via main cycle
                            del self._pending_expensive_orders[pending_key]
                            filled_amount = current_up - last_pos
                            logger.info(
                                f"[SEQ_PAIR] ✓ Expensive UP filled +{filled_amount:.0f} "
                                f"(position {last_pos:.0f} → {current_up:.0f}). Now placing DOWN hedge."
                            )
                            # FORCE hedge even if down_order is None (buy_down was blocked)
                            if down_order:
                                pending_trades.append(down_order)
                            else:
                                # Create forced hedge order with PROFIT CEILING
                                hedge_size = min(filled_amount, down_buy_size) if down_buy_size > 0 else filled_amount
                                hedge_size = max(5, int(hedge_size))
                                hedge_price = down_price if down_price > 0 else raw_down_ask

                                # Apply profit ceiling: never pay more than would wipe out edge
                                expensive_price = pending_info.get("expensive_price", 0)
                                if expensive_price > 0:
                                    MIN_PROFIT = 0.005
                                    max_hedge_price = 1.00 - expensive_price - MIN_PROFIT
                                    if hedge_price > max_hedge_price:
                                        logger.warning(f"[FORCED_HEDGE] DOWN capped @ ${max_hedge_price:.4f} (was ${hedge_price:.4f})")
                                        hedge_price = max_hedge_price

                                forced_order = {
                                    "side": "DOWN",
                                    "price": hedge_price,
                                    "size": hedge_size,
                                    "best_ask": raw_down_ask,
                                    "best_bid": raw_down_bid,
                                }
                                pending_trades.append(forced_order)
                                logger.info(f"[SEQ_PAIR] FORCED DOWN hedge @ ${hedge_price:.4f} (buy_down was blocked)")
                    elif pending_age > 30:
                        # Timeout - expensive side didn't fill in 30s, cancel and reset
                        del self._pending_expensive_orders[pending_key]
                        logger.warning(
                            f"[SEQ_PAIR] ⏱ Expensive UP timed out after {pending_age:.0f}s. "
                            f"Resetting - will retry next cycle."
                        )
                        # Don't place anything this cycle
                    else:
                        # Still waiting for fill
                        logger.debug(
                            f"[SEQ_PAIR] Waiting for UP to fill ({pending_age:.0f}s elapsed, "
                            f"pos still {current_up:.0f})"
                        )
                        # Don't place anything
                else:  # pending_side == "DOWN"
                    last_pos = pending_info.get("position_when_placed", 0)
                    if current_down > last_pos:
                        # Expensive side filled! Check if instant hedge already placed
                        if pending_info.get("hedge_placed"):
                            # WebSocket instant hedge already handled this - just clear tracking
                            del self._pending_expensive_orders[pending_key]
                            logger.info(
                                f"[SEQ_PAIR] ✓ DOWN filled, instant hedge already placed via WebSocket. Skipping."
                            )
                            # Don't place duplicate hedge - instant hedge handled it
                        else:
                            # No instant hedge yet - place hedge via main cycle
                            del self._pending_expensive_orders[pending_key]
                            filled_amount = current_down - last_pos
                            logger.info(
                                f"[SEQ_PAIR] ✓ Expensive DOWN filled +{filled_amount:.0f} "
                                f"(position {last_pos:.0f} → {current_down:.0f}). Now placing UP hedge."
                            )
                            # FORCE hedge even if up_order is None (buy_up was blocked)
                            if up_order:
                                pending_trades.append(up_order)
                            else:
                                # Create forced hedge order with PROFIT CEILING
                                hedge_size = min(filled_amount, up_buy_size) if up_buy_size > 0 else filled_amount
                                hedge_size = max(5, int(hedge_size))
                                hedge_price = up_price if up_price > 0 else raw_up_ask

                                # Apply profit ceiling: never pay more than would wipe out edge
                                expensive_price = pending_info.get("expensive_price", 0)
                                if expensive_price > 0:
                                    MIN_PROFIT = 0.005
                                    max_hedge_price = 1.00 - expensive_price - MIN_PROFIT
                                    if hedge_price > max_hedge_price:
                                        logger.warning(f"[FORCED_HEDGE] UP capped @ ${max_hedge_price:.4f} (was ${hedge_price:.4f})")
                                        hedge_price = max_hedge_price

                                forced_order = {
                                    "side": "UP",
                                    "price": hedge_price,
                                    "size": hedge_size,
                                    "best_ask": raw_up_ask,
                                    "best_bid": raw_up_bid,
                                }
                                pending_trades.append(forced_order)
                                logger.info(f"[SEQ_PAIR] FORCED UP hedge @ ${hedge_price:.4f} (buy_up was blocked)")
                    elif pending_age > 30:
                        # Timeout
                        del self._pending_expensive_orders[pending_key]
                        logger.warning(
                            f"[SEQ_PAIR] ⏱ Expensive DOWN timed out after {pending_age:.0f}s. "
                            f"Resetting - will retry next cycle."
                        )
                    else:
                        logger.debug(
                            f"[SEQ_PAIR] Waiting for DOWN to fill ({pending_age:.0f}s elapsed, "
                            f"pos still {current_down:.0f})"
                        )
            else:
                # No pending order - check if balanced or need to start expensive side
                if current_up == current_down:
                    # Balanced - place expensive side first
                    expensive_order = up_order if expensive_side == "UP" else down_order
                    if expensive_order:
                        pending_trades.append(expensive_order)
                        # Track this as pending (includes cheap side info for instant hedge)
                        cheap_order = down_order if expensive_side == "UP" else up_order
                        self._pending_expensive_orders[pending_key] = {
                            "side": expensive_side,
                            "placed_at": time.time(),
                            "position_when_placed": current_up if expensive_side == "UP" else current_down,
                            "expected_size": expensive_order["size"],
                            # INSTANT HEDGE: Store cheap side info for WebSocket trigger
                            "cheap_side": cheap_side,
                            "cheap_price": cheap_order["price"] if cheap_order else (down_price if expensive_side == "UP" else up_price),
                            "cheap_size": cheap_order["size"] if cheap_order else buy_size,
                            "market_slug": market.slug,
                            "up_token_id": market.up_token_id,
                            "down_token_id": market.down_token_id,
                            # PROFIT CEILING: Store expensive price to calculate max hedge price
                            "expensive_price": expensive_order["price"],
                        }
                        logger.info(
                            f"[SEQ_PAIR] Placing expensive {expensive_side} @ ${expensive_order['price']:.4f} "
                            f"(will wait for fill before hedging)"
                        )
                elif current_up > current_down:
                    # UP ahead - need DOWN to catch up (DOWN is the hedge)
                    # FORCE hedge order even if buy_down was blocked - hedging is mandatory!
                    if down_order:
                        pending_trades.append(down_order)
                        logger.debug(f"[SEQ_PAIR] Placing DOWN hedge (UP={current_up:.0f}, DOWN={current_down:.0f})")
                    else:
                        # buy_down was blocked, but we MUST hedge - create forced order
                        imbal = current_up - current_down
                        hedge_size = min(imbal, buy_size, down_buy_size) if buy_size > 0 else min(imbal, down_buy_size)
                        hedge_size = max(5, int(hedge_size))  # Polymarket minimum
                        forced_order = {
                            "side": "DOWN",
                            "price": down_price if down_price > 0 else raw_down_ask,
                            "size": hedge_size,
                            "best_ask": raw_down_ask,
                            "best_bid": raw_down_bid,
                        }
                        pending_trades.append(forced_order)
                        logger.info(
                            f"[SEQ_PAIR] FORCED DOWN hedge (UP={current_up:.0f}, DOWN={current_down:.0f}) "
                            f"- buy_down was blocked but hedge is mandatory!"
                        )
                else:
                    # DOWN ahead - need UP to catch up (UP is the hedge)
                    # FORCE hedge order even if buy_up was blocked - hedging is mandatory!
                    if up_order:
                        pending_trades.append(up_order)
                        logger.debug(f"[SEQ_PAIR] Placing UP hedge (UP={current_up:.0f}, DOWN={current_down:.0f})")
                    else:
                        # buy_up was blocked, but we MUST hedge - create forced order
                        imbal = current_down - current_up
                        hedge_size = min(imbal, buy_size, up_buy_size) if buy_size > 0 else min(imbal, up_buy_size)
                        hedge_size = max(5, int(hedge_size))  # Polymarket minimum
                        forced_order = {
                            "side": "UP",
                            "price": up_price if up_price > 0 else raw_up_ask,
                            "size": hedge_size,
                            "best_ask": raw_up_ask,
                            "best_bid": raw_up_bid,
                        }
                        pending_trades.append(forced_order)
                        logger.info(
                            f"[SEQ_PAIR] FORCED UP hedge (UP={current_up:.0f}, DOWN={current_down:.0f}) "
                            f"- buy_up was blocked but hedge is mandatory!"
                        )
        else:
            # PARALLEL ORDER EXECUTION (DEFAULT - sequential_ordering_enabled=False)
            # Place both sides simultaneously, sorted by price (expensive first)
            # Faster accumulation but may result in asymmetric fills
            if up_order:
                pending_trades.append(up_order)
            if down_order:
                pending_trades.append(down_order)
            # Sort expensive first to prioritize harder-to-fill side
            pending_trades.sort(key=lambda t: t["price"], reverse=True)

        # QUOTE PULLING: Cancel stale quotes when Binance moves against them
        # This is the key latency advantage from Telegram alpha:
        # "You get rolled over if you're not quick enough to pull quotes when Binance moves"
        # Now works for BOTH live and paper modes (paper simulates the protective benefit)
        if (
            self._trend_detector and
            hasattr(self._engine, 'check_and_pull_stale_quotes')
        ):
            try:
                # Paper mode gets longer timeout to allow tick-based fills to complete
                stale_timeout = 20.0 if self.trading_mode == "paper" else 10.0
                pulled = await self._engine.check_and_pull_stale_quotes(
                    market=market,
                    trend_detector=self._trend_detector,
                    max_age_secs=stale_timeout,  # MM standard: 5-20 seconds
                    velocity_threshold_bps=15.0,  # Pull if price moving >15 bps/sec
                )
                if any(pulled.values()):
                    logger.info(f"[QUOTE_PULL] Pulled stale quotes: UP={pulled['UP']}, DOWN={pulled['DOWN']}")
                    # Mark pulled sides for stabilization - wait for z < 1.0 before re-entering
                    for side, was_pulled in pulled.items():
                        if was_pulled:
                            self._pull_cooldown[f"{market.slug}_{side}"] = time.time()
            except Exception as e:
                logger.debug(f"[QUOTE_PULL] Error: {e}")

        # PAPER MODE: Check pending orders for tick-based fills
        # This simulates orders sitting in the orderbook and gradually filling
        if self.trading_mode == "paper" and hasattr(self._engine, 'check_pending_fills'):
            try:
                # Get current prices for fill probability adjustment
                current_prices = {"UP": up_ask, "DOWN": down_ask} if up_ask and down_ask else None
                fills = await self._engine.check_pending_fills(current_prices=current_prices)
                for fill in fills:
                    logger.info(
                        f"[PAPER_FILL] {fill['side']} filled: {fill['filled_size']} @ ${fill['filled_price']:.4f} "
                        f"after {fill['order_age']:.1f}s"
                    )
            except Exception as e:
                logger.debug(f"[PAPER_FILL] Error checking fills: {e}")

        # Execute trades in order
        # For calculus_maker/fair_value_mm LIVE: use cancel-and-replace for patient MAKER orders
        # For paper mode: use instant fills (simpler, more reliable)
        use_cancel_replace = (
            self.trading_mode == "live" and
            self.accum_mode in ("calculus_maker", "fair_value_mm") and
            hasattr(self._engine, 'cancel_and_replace')
        )

        for trade in pending_trades:
            side = trade["side"]
            price = trade["price"]
            size = trade["size"]
            best_ask = trade["best_ask"]
            best_bid = trade["best_bid"]

            # POST-PULL STABILIZATION: Wait for z < 1.0 (NEUTRAL) before re-entering
            cooldown_key = f"{market.slug}_{side}"
            if cooldown_key in self._pull_cooldown:
                # Check if z-score has returned to NEUTRAL
                if trend_signal and trend_signal.state != TrendState.NEUTRAL:
                    logger.debug(f"[STABILIZE] {side} waiting for NEUTRAL, z={trend_signal.z_score:.2f}")
                    continue  # Skip this side until stabilized
                else:
                    # Stabilized - clear cooldown and proceed
                    del self._pull_cooldown[cooldown_key]
                    z_str = f"z={trend_signal.z_score:.2f}" if trend_signal else "no signal"
                    logger.info(f"[STABILIZE] {side} stabilized ({z_str}), resuming orders")

            if self._engine.balance < price * size:
                logger.debug(f"Skip {side}: insufficient balance for ${price * size:.2f}")
                continue

            # PAIR COST GATING: Block trades that would push pair cost above max threshold
            # Applies to both live and paper modes for realistic simulation
            if not (is_emergency_up or is_emergency_down):
                pos = self._engine.get_position(market)
                if pos:
                    should_buy, prospective_cost, reason = check_prospective_pair_cost(
                        side=side,
                        buy_price=price,
                        buy_size=size,
                        current_up_size=pos.up_shares,
                        current_down_size=pos.down_shares,
                        current_up_avg=pos.up_avg_price,
                        current_down_avg=pos.down_avg_price,
                        max_pair_cost=0.98  # Block if would exceed 98%
                    )
                    if not should_buy:
                        logger.warning(f"[PAIR_COST_GATE] Blocking {side} @ ${price:.4f}: {reason}")
                        continue

            if use_cancel_replace:
                # Calculus_maker: Use cancel-and-replace for dynamic repricing (live & paper)
                #
                # GRADUAL CHASE LOGIC (controlled by self.gradual_chase_enabled)
                # Instead of jumping to current market price, we chase in small steps
                # based on time remaining. This preserves fill guarantee while
                # reducing the cost of chasing.
                #
                # To disable: set gradual_chase_enabled=False in bot config
                #
                chase_price = price  # Default to patient price
                pending_key = f"{market.slug}_{side}"

                # EMERGENCY CEILING CHANGE DETECTION
                # If emergency ceiling increased (e.g., $0.75 → $0.88), cancel old order
                # and reprice at current ask (up to new ceiling)
                is_this_emergency = (side == "UP" and is_emergency_up) or (side == "DOWN" and is_emergency_down)
                current_emergency_ceiling = get_emergency_price_ceiling(time_remaining_secs) if is_this_emergency else None
                prev_ceiling = self._emergency_ceiling_used.get(pending_key)

                if is_this_emergency and current_emergency_ceiling and prev_ceiling:
                    if current_emergency_ceiling > prev_ceiling:
                        # Ceiling increased! Cancel old order and reprice at ask
                        logger.info(
                            f"[EMERGENCY_CEILING_CHANGE] {side}: ceiling ${prev_ceiling:.2f} → ${current_emergency_ceiling:.2f}, "
                            f"repricing at ask ${best_ask:.4f}"
                        )
                        # Cancel old order explicitly
                        if hasattr(self._engine, 'cancel_pending_order'):
                            await self._engine.cancel_pending_order(pending_key)
                        # Reset chase state
                        self._replacement_count.pop(pending_key, None)
                        self._chase_exhausted_logged.discard(pending_key)
                        self._emergency_ceiling_used.pop(pending_key, None)
                        # Use current ask (capped at new ceiling)
                        chase_price = min(best_ask, current_emergency_ceiling)
                        # Skip gradual chase - go direct to order placement
                        result = await self._engine.cancel_and_replace(
                            market=market,
                            side=side,
                            new_price=chase_price,
                            new_size=size,
                            price_tolerance=0.0,  # Force replace
                            stale_seconds=0.0,
                        )
                        # Track the new ceiling
                        self._emergency_ceiling_used[pending_key] = current_emergency_ceiling
                        if result["action"] in ["filled", "placed", "replaced"]:
                            result["success"] = True
                            result["filled_size"] = result.get("filled_size", size)
                            result["filled_price"] = result.get("price", chase_price)
                            result["cost"] = result.get("filled_size", size) * result.get("price", chase_price)
                        else:
                            result["success"] = False
                            result["filled_size"] = 0
                            result["filled_price"] = 0
                            result["cost"] = 0
                        # Jump to trade logging
                        if result.get("success"):
                            trades_made += 1
                        continue  # Skip normal chase logic

                # Track emergency ceiling for new emergency orders
                if is_this_emergency and current_emergency_ceiling:
                    if pending_key not in self._emergency_ceiling_used:
                        self._emergency_ceiling_used[pending_key] = current_emergency_ceiling

                if self.gradual_chase_enabled:
                    # Check if there's a pending order for this side
                    pending_orders = self._engine.get_pending_orders()
                    pending = pending_orders.get(pending_key)

                    if pending:
                        # We have a pending order - calculate gradual chase price
                        original_price = pending["price"]
                        order_age = asyncio.get_event_loop().time() - pending["placed_at"]

                        # Determine if this is the hedge side (deficit side needs hedge)
                        is_hedge_side = (
                            (side == "UP" and current_down > current_up) or
                            (side == "DOWN" and current_up > current_down)
                        )

                        # Get current chase count for this side
                        chase_count = self._replacement_count.get(pending_key, 0)

                        chase_price, should_chase, chase_exhausted = calculate_gradual_chase_price(
                            original_price=original_price,
                            current_bid=best_bid,
                            current_ask=best_ask,
                            time_remaining_secs=time_remaining_secs,
                            order_age_secs=order_age,
                            chase_count=chase_count,
                            is_hedge_side=is_hedge_side,
                        )

                        # Handle chase exhaustion - stop chasing, leave order at final price
                        if chase_exhausted:
                            if pending_key not in self._chase_exhausted_logged:
                                logger.info(
                                    f"[CHASE_EXHAUSTED] {side}: Stopped at ${original_price:.4f} after "
                                    f"{chase_count} iterations, order remains live"
                                )
                                self._chase_exhausted_logged.add(pending_key)
                            # Skip this side - don't try to replace, let order sit
                            continue

                        if should_chase:
                            logger.info(
                                f"[GRADUAL_CHASE] {side}: ${original_price:.4f} → ${chase_price:.4f} "
                                f"(chase #{chase_count + 1}, age={order_age:.0f}s, time_left={time_remaining_secs:.0f}s)"
                            )
                        else:
                            # Not ready to chase yet - keep original price
                            chase_price = original_price

                # PROFIT-PRESERVING CHASE CEILING
                # Never chase hedge above price that would wipe out profit
                if hasattr(self, '_pending_expensive_orders') and self._pending_expensive_orders:
                    pending = self._pending_expensive_orders.get(market.slug)
                    if pending:
                        # Check if this is the hedge side (not the expensive side)
                        if pending.get("cheap_side") == side:
                            max_hedge_price = pending.get("max_hedge_price")
                            expensive_price = pending.get("expensive_price", 0)

                            # Calculate max if not already stored
                            if not max_hedge_price and expensive_price > 0:
                                MIN_PROFIT = 0.005
                                max_hedge_price = 1.00 - expensive_price - MIN_PROFIT

                            if max_hedge_price and chase_price > max_hedge_price:
                                logger.warning(
                                    f"[CHASE_CEILING] {side} capped @ ${max_hedge_price:.4f} "
                                    f"(chase wanted ${chase_price:.4f}, exp=${expensive_price:.4f})"
                                )
                                chase_price = max_hedge_price

                # CALC: depth-based timeout (5-30s), FV: fixed 10s
                if self.accum_mode == "calculus_maker":
                    from src.services.live_trading import calculate_price_depth_timeout
                    stale_secs = calculate_price_depth_timeout(chase_price, best_bid)
                else:
                    stale_secs = 10.0  # FV: fixed 10s

                result = await self._engine.cancel_and_replace(
                    market=market,
                    side=side,
                    new_price=chase_price,
                    new_size=size,
                    price_tolerance=0.005,  # Replace if price moved > 0.5%
                    stale_seconds=stale_secs,
                )

                # Track replacements for chase exhaustion detection
                side_key = f"{market.slug}_{side}"
                if result["action"] == "filled":
                    # Order filled - reset counter and allow re-logging
                    self._replacement_count.pop(side_key, None)
                    self._chase_exhausted_logged.discard(side_key)
                    self._emergency_ceiling_used.pop(side_key, None)  # Clear emergency ceiling tracking
                    # Fill rate monitoring: record fill
                    if hasattr(self._engine, 'record_order_filled'):
                        self._engine.record_order_filled(
                            market.slug, result.get("filled_size", size),
                            result.get("price", chase_price)
                        )
                elif result["action"] == "replaced":
                    # Order replaced but not filled - increment counter
                    self._replacement_count[side_key] = self._replacement_count.get(side_key, 0) + 1
                    logger.debug(f"[REPLACE_COUNT] {side}: {self._replacement_count[side_key]} replacements")
                    # Fill rate monitoring: record chase order placed
                    if hasattr(self._engine, 'record_order_placed'):
                        self._engine.record_order_placed(market.slug, size, chase_price, is_chase=True)
                elif result["action"] == "placed":
                    # Fill rate monitoring: record new order placed
                    if hasattr(self._engine, 'record_order_placed'):
                        self._engine.record_order_placed(market.slug, size, chase_price)

                # Convert cancel_and_replace result to execute_single_side_trade format
                if result["action"] in ["filled", "placed", "replaced"]:
                    result["success"] = True
                    result["filled_size"] = result.get("filled_size", size)
                    result["filled_price"] = result.get("price", chase_price)
                    result["cost"] = result.get("filled_size", size) * result.get("price", chase_price)
                elif result["action"] == "kept":
                    # Order kept - not a new trade, skip logging
                    logger.debug(f"[CALC] Order kept @ ${result.get('price', chase_price):.4f}")
                    continue
                else:
                    result["success"] = False
                    result["filled_size"] = 0
                    result["filled_price"] = 0
                    result["cost"] = 0
            else:
                # Paper mode or non-calculus: Use standard execution
                result = await self._engine.execute_single_side_trade(
                    market=market,
                    side=side,
                    price=price,
                    size=size,
                    best_ask=best_ask,
                )

            if result["success"]:
                trades_made += 1
                self._trade_count += 1
                self._profitable_opportunities += 1
                # Record trade in health monitor
                health_monitor.record_trade(self.strategy_name, market.slug)

                # Log to CSV
                updated_position = self._engine.get_position(market)
                self._log_event_csv(
                    market_slug=market.slug,
                    event_type="TRADE",
                    trade_side=side,
                    trade_mode="ACCUM",
                    size_requested=size,
                    size_filled=result["filled_size"],
                    price=result["filled_price"],
                    cost=result["cost"],
                    position=updated_position,
                    status="SUCCESS",
                )
                # Emit trade event for web UI trade log
                self._send_trade_event(side, result["filled_size"], result["filled_price"], "BUY")
                # Push real-time update to frontend immediately after trade
                self._send_web_update()
                # Update local state for next trade
                if side == "UP":
                    current_up += result["filled_size"]
                    current_up_cost += result["cost"]
                    # Track fill for one_buy mode (record price for pair cost calculation)
                    if self._one_buy_strategy:
                        fill_price = result["filled_price"]
                        self._one_buy_strategy.mark_filled("UP", fill_price)
                        status = self._one_buy_strategy.get_status()
                        if status["fully_hedged"]:
                            logger.info(f"[1BUY] ✅ UP FILLED at ${fill_price:.2f} → FULLY HEDGED pair_cost=${status['pair_cost']:.2f}")
                        else:
                            logger.info(f"[1BUY] ✅ UP FILLED at ${fill_price:.2f} → directional (waiting for DOWN)")
                else:
                    current_down += result["filled_size"]
                    current_down_cost += result["cost"]
                    # Track fill for one_buy mode (record price for pair cost calculation)
                    if self._one_buy_strategy:
                        fill_price = result["filled_price"]
                        self._one_buy_strategy.mark_filled("DOWN", fill_price)
                        status = self._one_buy_strategy.get_status()
                        if status["fully_hedged"]:
                            logger.info(f"[1BUY] ✅ DOWN FILLED at ${fill_price:.2f} → FULLY HEDGED pair_cost=${status['pair_cost']:.2f}")
                        else:
                            logger.info(f"[1BUY] ✅ DOWN FILLED at ${fill_price:.2f} → directional (waiting for UP)")
            else:
                # PAPER MODE FIX: Clear sequential pairing tracking if expensive order failed to fill
                # This prevents 30-second timeout waiting for a fill that already failed
                if (
                    self.sequential_ordering_enabled and
                    hasattr(self, '_pending_expensive_orders') and
                    self.trading_mode == "paper"
                ):
                    pending_key = market.slug
                    pending_info = self._pending_expensive_orders.get(pending_key)
                    if pending_info and pending_info.get("side") == side:
                        del self._pending_expensive_orders[pending_key]
                        logger.debug(
                            f"[SEQ_PAIR] Paper fill failed for {side}, cleared tracking. "
                            f"Will retry next cycle."
                        )

        # Log position status periodically
        if self._opportunities_checked % 10 == 0 or trades_made > 0:
            pos = self._engine.get_position(market)
            if pos:
                pairs = pos.pair_count
                avg_pair_cost = pos.up_avg_price + pos.down_avg_price if pairs > 0 else 0
                locked = pos.locked_profit
                imbal = abs(pos.up_size - pos.down_size)

                time_remaining = "?"
                if market.end_time:
                    secs_left = (market.end_time - datetime.now(timezone.utc)).total_seconds()
                    time_remaining = f"{int(secs_left)}s"

                logger.info(
                    f"[ACCUM] UP:{pos.up_size:.0f}@${pos.up_avg_price:.3f} "
                    f"DOWN:{pos.down_size:.0f}@${pos.down_avg_price:.3f} | "
                    f"Pairs:{pairs} PairCost:${avg_pair_cost:.4f} Locked:${locked:.2f} "
                    f"Imbal:{imbal:.0f} | Time:{time_remaining} Bal:${self._engine.balance:.2f}"
                )

        # Auto-merge near market end (live mode only)
        if market.end_time:
            secs_to_end = (market.end_time - datetime.now(timezone.utc)).total_seconds()
            await self._maybe_auto_merge(market, secs_to_end)

        # Check for rotation
        if self._rotator.should_rotate():
            await self._handle_market_rotation(market)

    async def _directional_trading_cycle(self) -> None:
        """
        Directional trading cycle with Binance price feed.

        Strategy:
        1. Start with a bias (BULLISH or BEARISH)
        2. Accumulate shares on the bias side
        3. Hedge (rebalance) when imbalance grows
        4. Flip bias on sustained impulsive moves
        5. Emergency hedge with <5 mins remaining
        """
        market = self._rotator.current_market
        if not market:
            logger.warning("No current market")
            return

        # Handle new market - use shared strike or set from previous candle
        if self._is_new_market:
            # Check for shared strike first (another strategy may have set it)
            shared_strike = get_shared_strike(market.slug)
            if shared_strike:
                self._binance_client._strike_price = shared_strike
                self._binance_client._strike_timestamp = datetime.now(timezone.utc)
                logger.info(f"[STRIKE] Using shared strike for {market.slug}: ${shared_strike:,.2f}")
            else:
                # First strategy to run - set and share the strike
                strike = await self._binance_client.set_strike_from_previous_candle(interval="15m")
                if strike > 0:
                    set_shared_strike(market.slug, strike, "directional")
            self._directional_strategy.reset_for_new_market()
            self._is_new_market = False

        # Get time remaining
        time_remaining_secs = 900  # Default 15 mins
        if market.end_time:
            time_remaining_secs = int((market.end_time - datetime.now(timezone.utc)).total_seconds())
            if time_remaining_secs < 0:
                time_remaining_secs = 0

        # Get current position
        position = self._engine.get_position(market)
        current_up = int(position.up_size) if position else 0
        current_down = int(position.down_size) if position else 0
        current_up_cost = position.up_cost if position else 0.0
        current_down_cost = position.down_cost if position else 0.0
        up_avg = position.up_avg_price if position else 0.0
        down_avg = position.down_avg_price if position else 0.0

        # HARD MAX IMBALANCE LIMIT (same as accumulation cycle)
        hard_max = self.hard_max_imbalance  # Configurable via web UI (default 10)
        current_imbalance = abs(current_up - current_down)
        if current_imbalance >= hard_max:
            logger.critical(
                f"🛑 [DIRECTIONAL] HARD STOP: Imbalance {current_imbalance} >= {hard_max} "
                f"(UP={current_up}, DOWN={current_down}). NO NEW ORDERS."
            )
            self._update_live_display()
            return

        # Update strategy with current position
        self._directional_strategy.update_position(
            up_shares=current_up,
            down_shares=current_down,
            up_avg_price=up_avg,
            down_avg_price=down_avg,
        )

        # Always update display at start of cycle to keep time_remaining in sync
        # This is critical - without it, early returns cause stale time to be shown
        self._update_live_display()

        # Get Polymarket prices with retry logic
        opportunity = None
        for attempt in range(self.max_retries):
            try:
                opportunity = await self._analyzer.analyze_asymmetric_opportunity(
                    market=market,
                    current_up_size=current_up,
                    current_down_size=current_down,
                    current_up_cost=current_up_cost,
                    current_down_cost=current_down_cost,
                    pair_cost_threshold=1.00,
                )
                self._consecutive_failures = 0
                break
            except Exception as e:
                self._consecutive_failures += 1
                delay = min(self.retry_base_delay * (2 ** attempt), self.retry_max_delay)
                if attempt < self.max_retries - 1:
                    if not await self._interruptible_sleep(delay, check_interval=2.0):
                        return  # Stop requested, exit early

        if opportunity is None or opportunity.up_ask is None or opportunity.down_ask is None:
            if self._rotator.should_rotate():
                self._is_new_market = True
                await self._handle_market_rotation(market)
            return

        self._opportunities_checked += 1

        # Get raw orderbook prices
        raw_up_ask = opportunity.up_ask
        raw_down_ask = opportunity.down_ask
        raw_up_bid = opportunity.up_bid or (raw_up_ask * 0.98 if raw_up_ask else 0.48)
        raw_down_bid = opportunity.down_bid or (raw_down_ask * 0.98 if raw_down_ask else 0.48)

        # Update display prices (use raw ask for display)
        self._last_up_price = raw_up_ask
        self._last_down_price = raw_down_ask
        self._last_spread = 1.0 - raw_up_ask - raw_down_ask
        self._update_live_display()  # Send update to web UI

        # Evaluate trade decision (pass raw ask prices for decision logic)
        decision = self._directional_strategy.evaluate_trade(
            up_ask=raw_up_ask,
            down_ask=raw_down_ask,
            time_remaining_secs=time_remaining_secs,
        )

        if decision:
            # TARGET SHARES ENFORCEMENT (first line of defense)
            # Calculate effective target: min of config target and max_position_pct safety limit
            max_shares_from_pct = int(self.directional_config.max_position_pct * self.initial_balance)
            effective_target = min(self.directional_config.target_shares, max_shares_from_pct)

            if decision.side == "UP":
                new_up = current_up + decision.size
                if new_up > effective_target:
                    logger.info(f"⛔ [TARGET] BLOCKED UP: would exceed target {new_up:.0f} > {effective_target}")
                    decision = None
            else:
                new_down = current_down + decision.size
                if new_down > effective_target:
                    logger.info(f"⛔ [TARGET] BLOCKED DOWN: would exceed target {new_down:.0f} > {effective_target}")
                    decision = None

        if decision:
            # IMBALANCE ENFORCEMENT (final safety limit)
            max_imbalance = self.directional_config.max_position_pct * self.initial_balance / decision.price
            max_imbalance = max(max_imbalance, 10)  # At least 10 shares allowed

            if decision.side == "UP":
                new_up = current_up + decision.size
                new_imbalance = abs(new_up - current_down)
            else:
                new_down = current_down + decision.size
                new_imbalance = abs(current_up - new_down)

            if new_imbalance > max_imbalance:
                logger.info(f"⛔ BLOCKED {decision.side}: would create imbalance {new_imbalance:.0f} > limit {max_imbalance:.0f}")
                decision = None  # Cancel the trade

        if decision:
            # PATIENT PRICING: Apply time-based maker pricing
            # Check if in emergency phase (< 5 min remaining, unhedged)
            is_emergency = self._directional_strategy.state.phase == DirectionalPhase.EMERGENCY_HEDGE

            # Get raw prices for patient pricing
            if decision.side == "UP":
                raw_bid = raw_up_bid
                raw_ask = raw_up_ask
            else:
                raw_bid = raw_down_bid
                raw_ask = raw_down_ask

            # Apply patient pricing to decision.price
            original_price = decision.price
            decision.price = get_patient_price(
                best_bid=raw_bid,
                best_ask=raw_ask,
                time_remaining_secs=time_remaining_secs,
                is_emergency=is_emergency,
            )

            price_mode = "EMERGENCY" if is_emergency else (
                "EARLY" if time_remaining_secs >= 600 else
                "MID" if time_remaining_secs >= 300 else
                "LATE" if time_remaining_secs >= 120 else "FINAL"
            )
            spread_from_ask = raw_ask - decision.price
            logger.info(
                f"[PATIENT-DIR] {price_mode} {decision.side}: price=${decision.price:.3f} "
                f"(ask=${raw_ask:.3f}, spread=${spread_from_ask:.3f})"
            )

            # POLYMARKET $1 MINIMUM ORDER VALUE ENFORCEMENT
            min_size = max(1, math.ceil(1.00 / decision.price)) if decision.price > 0 else 1
            if decision.size < min_size:
                logger.debug(f"[MIN$1] DIR size {decision.size} → {min_size} (${decision.price:.3f} × {min_size} = ${decision.price * min_size:.2f})")
                decision.size = min_size

            # POLYMARKET MINIMUM 5 SHARES ENFORCEMENT
            MIN_SHARES = 5
            if decision.size < MIN_SHARES:
                logger.debug(f"[MIN5] DIR size {decision.size} → {MIN_SHARES} (Polymarket minimum)")
                decision.size = MIN_SHARES

            # Check balance
            trade_cost = decision.price * decision.size
            if self._engine.balance >= trade_cost:
                result = await self._engine.execute_single_side_trade(
                    market=market,
                    side=decision.side,
                    price=decision.price,
                    size=decision.size,
                    best_ask=raw_ask,  # For realistic fill probability simulation
                )

                if result["success"]:
                    self._trade_count += 1
                    self._profitable_opportunities += 1

                    # Record trade in strategy
                    self._directional_strategy.record_trade(decision.side, result["filled_price"])

                    # Log to CSV
                    updated_position = self._engine.get_position(market)
                    self._log_directional_trade_csv(
                        market_slug=market.slug,
                        decision=decision,
                        result=result,
                        position=updated_position,
                    )

                    # Emit trade event for web UI trade log
                    self._send_trade_event(decision.side, result["filled_size"], result["filled_price"], "BUY")
                    # Push real-time update to frontend immediately after trade
                    self._send_web_update()

        # Log status periodically (suppressed in quiet mode)
        if self._opportunities_checked % 10 == 0 and not self.quiet_mode:
            status = self._directional_strategy.get_status_dict()
            pos = self._engine.get_position(market)
            pair_cost = (pos.up_avg_price + pos.down_avg_price) if pos and pos.pair_count > 0 else 0

            logger.info(
                f"[DIRECTIONAL] Bias={status['bias']} Phase={status['phase']} Flips={status['flip_count']} | "
                f"UP:{current_up}@${up_avg:.3f} DOWN:{current_down}@${down_avg:.3f} | "
                f"PairCost:${pair_cost:.3f} Imbal:{status['imbalance']} | "
                f"BTC:${status['btc_price']:,.0f} vs ${status['strike_price']:,.0f} ({status['price_vs_strike_pct']:+.3f}%) | "
                f"Time:{time_remaining_secs}s"
            )

        # Auto-merge near market end (live mode only)
        await self._maybe_auto_merge(market, time_remaining_secs)

        # Check for rotation
        if self._rotator.should_rotate():
            self._is_new_market = True
            await self._handle_market_rotation(market)

    def _log_directional_trade_csv(
        self,
        market_slug: str,
        decision: TradeDecision,
        result: dict,
        position: Optional[Position],
    ) -> None:
        """Log a directional mode trade to CSV."""
        now = datetime.now(timezone.utc)

        # Get directional-specific data
        status = self._directional_strategy.get_status_dict()

        # Check for daily rotation before writing
        self._check_csv_rotation()

        with open(self.csv_path, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                now.isoformat(),
                market_slug,
                "TRADE",
                decision.side,
                f"DIR_{decision.phase.value.upper()}",
                decision.size,
                result["filled_size"],
                result["filled_price"],
                result["cost"],
                # Position state
                position.up_size if position else 0,
                position.up_avg_price if position else 0,
                position.down_size if position else 0,
                position.down_avg_price if position else 0,
                position.pair_count if position else 0,
                position.up_avg_price + position.down_avg_price if position and position.pair_count > 0 else 0,
                position.locked_profit if position else 0,
                abs(position.up_size - position.down_size) if position else 0,
                # P&L
                0, 0, 0,  # pnl_min, pnl_max, pnl_realized
                self._engine.balance,
                "SUCCESS",
                # Directional-specific columns
                status["bias"],
                status["phase"],
                status["flip_count"],
                status["btc_price"],
                status["strike_price"],
                status["price_vs_strike_pct"],
                status["z_score"],
            ])

    def _setup_event_driven_pull(self, market_slug: str) -> None:
        """
        Set up event-driven quote pulling for a market.

        Registers a callback with BinanceClient that fires when z-score crosses
        the STRONG threshold (2.0). This enables ~100-200ms reaction time to
        Binance price moves, compared to 1-2 second polling.

        Only active in LIVE mode with LiveTradingEngine.

        Args:
            market_slug: The market to set up event-driven pulling for
        """
        # Only for live mode with LiveTradingEngine
        if self.trading_mode != "live":
            return
        if not self._binance_client:
            return
        if not hasattr(self._engine, 'event_driven_pull'):
            return

        # Clear any existing callback
        if self._event_pull_callback:
            self._binance_client.remove_z_threshold_callback(self._event_pull_callback)
            self._event_pull_callback = None

        # Reset z-state for new market
        self._binance_client.reset_z_state()

        # Store market slug for closure
        self._event_pull_market = market_slug
        engine = self._engine

        def on_z_threshold(z_score: float, direction: str, trend_state: str) -> None:
            """
            Callback fired when z-score crosses STRONG threshold.

            Schedules async cancel on the event loop. This runs within ~100ms
            of the Binance price tick that crossed the threshold.
            """
            if direction == "FLAT":
                return  # No clear direction, don't pull

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    engine.event_driven_pull(direction, market_slug, z_score)
                )
            except RuntimeError:
                # No running loop (shouldn't happen but safety)
                logger.debug("[EVENT_PULL] No running loop for callback")

        # Register the callback
        self._event_pull_callback = on_z_threshold
        self._binance_client.on_z_threshold_crossed(on_z_threshold)
        logger.info(f"[EVENT_PULL] Enabled for {market_slug} (100-200ms reaction)")

    def _teardown_event_driven_pull(self) -> None:
        """Remove event-driven quote pull callback."""
        if self._event_pull_callback and self._binance_client:
            self._binance_client.remove_z_threshold_callback(self._event_pull_callback)
            logger.debug(f"[EVENT_PULL] Disabled for {self._event_pull_market}")
        self._event_pull_callback = None
        self._event_pull_market = None

    async def _setup_user_websocket(self) -> bool:
        """
        Set up user WebSocket for instant fill notifications.

        Replaces 2-second polling with ~100ms WebSocket callbacks.
        Only active in LIVE mode.

        Returns:
            True if connected successfully
        """
        if self.trading_mode != "live":
            return False

        if self._user_ws is not None:
            return self._user_ws.connected

        # Get API credentials from config
        try:
            config = Config()
            api_key = config.polymarket_api_key
            api_secret = config.polymarket_secret
            api_passphrase = config.polymarket_passphrase

            if not all([api_key, api_secret, api_passphrase]):
                logger.warning("[USER_WS] Missing API credentials, skipping WebSocket")
                return False

            # Create client
            self._user_ws = UserWebSocketClient(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            )

            # Register fill callback
            def on_fill(fill: OrderFill):
                """
                Handle fill notification from WebSocket.

                INSTANT HEDGE: When expensive order fills, spawn async task
                to place hedge immediately (~100-200ms total).
                """
                try:
                    # Map outcome to side
                    outcome_upper = fill.outcome.upper() if fill.outcome else ""
                    if outcome_upper in ("YES", "UP"):
                        fill_side = "UP"
                    elif outcome_upper in ("NO", "DOWN"):
                        fill_side = "DOWN"
                    else:
                        fill_side = fill.side

                    # Put fill info in queue for main loop to process
                    self._ws_fill_queue.put_nowait({
                        "side": fill_side,
                        "size": fill.size_matched,
                        "price": fill.price,
                        "status": fill.status,
                        "order_id": fill.order_id,
                        "timestamp": fill.timestamp,
                    })

                    # INSTANT HEDGE: Check if this fill is for our pending expensive order
                    if hasattr(self, '_pending_expensive_orders') and self._pending_expensive_orders:
                        for market_slug, info in list(self._pending_expensive_orders.items()):
                            if info.get("side") == fill_side:
                                # This fill matches our expensive side!
                                if fill.size_matched > 0:
                                    logger.info(
                                        f"[USER_WS] 🔔 Expensive {fill_side} fill detected! "
                                        f"{fill.size_matched:.0f} shares @ ${fill.price:.4f}. "
                                        f"Spawning INSTANT hedge..."
                                    )
                                    # Spawn instant hedge task - this is the key to sub-second hedging!
                                    try:
                                        loop = asyncio.get_event_loop()
                                        loop.create_task(
                                            self._instant_hedge_from_ws(
                                                fill_side=fill_side,
                                                fill_size=fill.size_matched,
                                                pending_info=info.copy(),  # Copy to avoid mutation
                                            )
                                        )
                                    except RuntimeError:
                                        # No event loop - fall back to next cycle
                                        logger.warning(
                                            f"[USER_WS] No event loop for instant hedge, "
                                            f"falling back to next cycle"
                                        )
                                break

                except Exception as e:
                    logger.error(f"[USER_WS] Fill callback error: {e}")

            self._user_ws.on_fill(on_fill)

            # Connect
            connected = await self._user_ws.connect()
            if not connected:
                logger.warning("[USER_WS] Failed to connect")
                return False

            # Start the WebSocket loop in background
            self._user_ws_task = asyncio.create_task(self._user_ws.run())
            logger.info("[USER_WS] Started - instant fill notifications enabled (~100ms)")

            return True

        except Exception as e:
            logger.error(f"[USER_WS] Setup failed: {e}")
            return False

    async def _teardown_user_websocket(self) -> None:
        """Disconnect user WebSocket."""
        if self._user_ws:
            await self._user_ws.disconnect()
            self._user_ws = None

        if self._user_ws_task:
            self._user_ws_task.cancel()
            try:
                await self._user_ws_task
            except asyncio.CancelledError:
                pass
            self._user_ws_task = None

        logger.debug("[USER_WS] Disconnected")

    async def _instant_hedge_from_ws(self, fill_side: str, fill_size: float, pending_info: dict) -> None:
        """
        Place hedge order INSTANTLY when expensive order fill is detected via WebSocket.

        This is the key to Gabagool-style sub-second hedging (~100-200ms total).
        Called directly from WebSocket fill callback via asyncio.create_task().

        Uses PROFIT-PRESERVING CEILING: Never chase hedge above the price that
        would wipe out the edge. Formula: max_hedge = $1.00 - expensive_price - min_profit

        Args:
            fill_side: Which side filled ("UP" or "DOWN")
            fill_size: How many shares filled
            pending_info: The pending expensive order info with cheap side details
        """
        try:
            start_time = time.time()

            cheap_side = pending_info.get("cheap_side")
            cheap_price = pending_info.get("cheap_price", 0)
            cheap_size = pending_info.get("cheap_size", 0)
            market_slug = pending_info.get("market_slug")
            expensive_price = pending_info.get("expensive_price", 0)

            if not all([cheap_side, cheap_price, market_slug]):
                logger.warning(f"[INSTANT_HEDGE] Missing info: side={cheap_side}, price={cheap_price}, slug={market_slug}")
                return

            # PROFIT-PRESERVING CEILING
            # Never pay more for hedge than would wipe out profit
            MIN_PROFIT = 0.005  # Half cent minimum profit per pair
            max_hedge_price = 1.00 - expensive_price - MIN_PROFIT if expensive_price > 0 else 0.99

            # Start with stored price (fastest, most profitable)
            hedge_price = cheap_price

            # Cap at profit ceiling if needed
            if hedge_price > max_hedge_price:
                logger.warning(
                    f"[INSTANT_HEDGE] Stored ${cheap_price:.4f} > max ${max_hedge_price:.4f}, "
                    f"capping to preserve profit"
                )
                hedge_price = max_hedge_price

            # Use the filled size for hedge (match what actually filled)
            hedge_size = min(fill_size, cheap_size) if cheap_size > 0 else fill_size
            hedge_size = max(5, int(hedge_size))  # Minimum 5 shares

            # Get current market for token IDs
            current_market = self._rotator.current_market if hasattr(self, '_rotator') else None
            if not current_market or current_market.slug != market_slug:
                logger.warning(f"[INSTANT_HEDGE] Market mismatch: {market_slug} vs current")
                return

            expected_profit = 1.00 - expensive_price - hedge_price if expensive_price > 0 else 0
            logger.info(
                f"[INSTANT_HEDGE] 🚀 WebSocket triggered! {cheap_side} hedge "
                f"{hedge_size} @ ${hedge_price:.4f} (max=${max_hedge_price:.4f}, exp_profit=${expected_profit:.4f})"
            )

            # Place hedge order via engine
            if self.trading_mode == "live" and hasattr(self._engine, 'cancel_and_replace'):
                result = await self._engine.cancel_and_replace(
                    market=current_market,
                    side=cheap_side,
                    new_price=hedge_price,
                    new_size=hedge_size,
                    price_tolerance=0.01,  # Accept 1% price movement
                    stale_seconds=0.0,  # Force immediate placement
                )

                elapsed_ms = (time.time() - start_time) * 1000

                if result.get("action") in ("placed", "replaced", "filled"):
                    actual_price = result.get("price", hedge_price)
                    actual_profit = 1.00 - expensive_price - actual_price if expensive_price > 0 else 0
                    logger.info(
                        f"[INSTANT_HEDGE] ✅ {cheap_side} hedge {result['action']} in {elapsed_ms:.0f}ms! "
                        f"Profit=${actual_profit:.4f}/pair"
                    )
                    # DON'T clear pending tracking yet - let regular cycle verify fill
                    # Store max_hedge_price for chase ceiling
                    if hasattr(self, '_pending_expensive_orders') and market_slug in self._pending_expensive_orders:
                        self._pending_expensive_orders[market_slug]["max_hedge_price"] = max_hedge_price
                        self._pending_expensive_orders[market_slug]["hedge_placed"] = True
                else:
                    logger.warning(
                        f"[INSTANT_HEDGE] ⚠️ Hedge placement failed: {result.get('action')} "
                        f"in {elapsed_ms:.0f}ms"
                    )
            else:
                # Paper mode or no engine - just log
                elapsed_ms = (time.time() - start_time) * 1000
                logger.info(
                    f"[INSTANT_HEDGE] 📝 Paper mode - would place {cheap_side} {hedge_size} "
                    f"@ ${cheap_price:.4f} in {elapsed_ms:.0f}ms"
                )
                # Clear tracking
                if hasattr(self, '_pending_expensive_orders') and market_slug in self._pending_expensive_orders:
                    del self._pending_expensive_orders[market_slug]

        except Exception as e:
            logger.error(f"[INSTANT_HEDGE] Error: {e}")
            import traceback
            traceback.print_exc()

    async def _handle_market_rotation(self, market) -> None:
        """Handle market rotation and position resolution with resilience."""
        old_market_slug = market.slug
        logger.info(f"[{self.strategy_name}] Rotating from {market.slug}")

        # Teardown event-driven quote pulling for old market
        self._teardown_event_driven_pull()

        # Cancel any pending orders for this market before rotation
        if self.trading_mode == "live" and hasattr(self._engine, 'cancel_all_pending'):
            try:
                cancelled = await self._engine.cancel_all_pending(market_slug=market.slug)
                if cancelled > 0:
                    logger.info(f"[ROTATION] Cancelled {cancelled} pending orders for {market.slug}")
            except Exception as e:
                logger.warning(f"[ROTATION] Failed to cancel pending orders: {e}")

        # Get health monitor for tracking
        health_monitor = get_health_monitor()

        # Resolve positions for this market
        pos = self._engine.get_position(market)
        if pos and (pos.up_size > 0 or pos.down_size > 0):
            # Determine winner from Polymarket API (actual resolution)
            # This is the SINGLE SOURCE OF TRUTH for both accumulation and directional
            winner = None
            resolution_source = None

            if self._client and (market.slug or market.condition_id):
                try:
                    # Use get_winning_side which returns "UP" or "DOWN" directly
                    # Pass slug (preferred) for reliable outcomePrices from Gamma API
                    winner = await self._client.get_winning_side(
                        condition_id=market.condition_id,
                        slug=market.slug,  # Slug-based query is more reliable
                        max_retries=3
                    )
                    if winner:
                        resolution_source = "POLYMARKET_API"
                        logger.info(f"[RESOLUTION] Polymarket API: {winner} won")
                    else:
                        logger.warning(f"[RESOLUTION] Polymarket API returned no winner for {market.slug}")
                except Exception as e:
                    logger.warning(f"[RESOLUTION] Polymarket API error: {e}")

            # Fallback to Binance ONLY if Polymarket API fails
            if winner is None:
                logger.warning(f"[RESOLUTION] Polymarket API failed, falling back to Binance strike comparison")
                if self._binance_client and self._binance_client.current_price > 0:
                    current_price = self._binance_client.current_price
                    # Use shared strike for consistency across all strategies
                    shared_strike = get_shared_strike(market.slug)
                    strike_price = shared_strike if shared_strike else self._binance_client.strike_price
                    if strike_price > 0:
                        winner = "UP" if current_price >= strike_price else "DOWN"
                        resolution_source = "BINANCE_FALLBACK_SHARED" if shared_strike else "BINANCE_FALLBACK"
                        logger.warning(
                            f"[RESOLUTION] Binance fallback: BTC ${current_price:,.2f} vs "
                            f"strike ${strike_price:,.2f} → {winner} (shared={shared_strike is not None})"
                        )
                    else:
                        winner = random_module.choice(["UP", "DOWN"])
                        resolution_source = "RANDOM_NO_STRIKE"
                        logger.error(f"[RESOLUTION] No strike price! Using RANDOM: {winner}")
                else:
                    winner = random_module.choice(["UP", "DOWN"])
                    resolution_source = "RANDOM_NO_BINANCE"
                    logger.error(f"[RESOLUTION] No Binance data! Using RANDOM: {winner}")

            # Log resolution source for debugging
            logger.info(f"[RESOLUTION] Final: {winner} (source: {resolution_source})")

            # Calculate expected P&L range before resolution
            min_pnl, max_pnl, locked = pos.calculate_expected_pnl_range()

            # Capture position state before resolution for CSV
            pre_resolution_pos = {
                "up_size": pos.up_size,
                "up_avg": pos.up_avg_price,
                "down_size": pos.down_size,
                "down_avg": pos.down_avg_price,
                "pairs": pos.pair_count,
                "locked": locked,
            }

            # Log fill rate stats before resolution (live mode only)
            if hasattr(self._engine, 'log_fill_stats'):
                self._engine.log_fill_stats(market.slug)

            # Resolve the market (paper P&L tracking)
            pnl = self._engine.resolve_market(market.slug, winner)
            logger.info(f"Market resolved ({winner}): P&L ${pnl:.4f}, LockedProfit was ${locked:.4f}")

            # Update cumulative P&L and check loss limit
            self.cumulative_pnl += pnl
            logger.info(f"[CUMULATIVE P&L] Session total: ${self.cumulative_pnl:.2f}")

            if self.max_daily_loss > 0 and self.cumulative_pnl <= -self.max_daily_loss:
                self.loss_limit_reached = True
                logger.warning("=" * 50)
                logger.warning(f"MAX DAILY LOSS LIMIT REACHED: ${abs(self.cumulative_pnl):.2f} >= ${self.max_daily_loss:.2f}")
                logger.warning("Bot will stop placing new orders but keep existing positions")
                logger.warning("=" * 50)

            # Log resolution to CSV - pass pre_resolution_pos data
            # Note: We create a simple object to hold the pre-resolution state
            # since the actual position was deleted by resolve_market
            class PreResolutionPosition:
                """Temp object to hold position state for CSV logging."""
                def __init__(self, data):
                    self.up_size = data["up_size"]
                    self.up_avg_price = data["up_avg"]
                    self.down_size = data["down_size"]
                    self.down_avg_price = data["down_avg"]
                    self.pair_count = data["pairs"]
                    self.locked_profit = data["locked"]
                    self.imbalance = abs(self.up_size - self.down_size) / max(self.up_size, self.down_size, 1)

                def calculate_expected_pnl_range(self):
                    # For resolved positions, actual P&L is known
                    return (0, 0, self.locked_profit)

            pre_pos_obj = PreResolutionPosition(pre_resolution_pos)

            self._log_event_csv(
                market_slug=market.slug,
                event_type="RESOLUTION",
                trade_side=winner,  # Which side won
                trade_mode=resolution_source,  # Track which source determined winner
                size_requested=pre_resolution_pos["up_size"] + pre_resolution_pos["down_size"],
                size_filled=pre_resolution_pos["up_size"] + pre_resolution_pos["down_size"],
                price=1.0,  # Winning side pays $1
                cost=pre_resolution_pos["up_size"] * pre_resolution_pos["up_avg"] + pre_resolution_pos["down_size"] * pre_resolution_pos["down_avg"],
                position=pre_pos_obj,  # Pass pre-resolution position state
                pnl_realized=pnl,
                status="RESOLVED",
            )

            # Calculate pair cost from pre-resolution data
            pair_cost = pre_resolution_pos["up_avg"] + pre_resolution_pos["down_avg"]

            # Send loss notification if P&L is negative
            if pnl < 0:
                unhedged_side = pos.unhedged_exposure_side
                unhedged_qty = pos.unhedged_up_size or pos.unhedged_down_size

                if unhedged_side and unhedged_side != winner:
                    details = (
                        f"Unhedged {unhedged_side} position ({unhedged_qty:.0f} shares) lost when {winner} won.\n"
                        f"Locked profit was ${locked:.4f}, but unhedged exposure caused net loss.\n"
                        f"Position had {pos.pair_count} hedged pairs."
                    )
                else:
                    details = f"Market resolved with unexpected loss. P&L range was ${min_pnl:.4f} to ${max_pnl:.4f}."

                await self._send_loss_notification(
                    loss_type="RESOLUTION_LOSS",
                    details=details,
                    loss_amount=pnl,
                    market_slug=market.slug,
                    winner=winner,
                    up_qty=pre_resolution_pos["up_size"],
                    up_avg_price=pre_resolution_pos["up_avg"],
                    down_qty=pre_resolution_pos["down_size"],
                    down_avg_price=pre_resolution_pos["down_avg"],
                    pair_cost=pair_cost,
                )
            elif pnl > 0:
                # Send win notification to PnL Summary
                await self._send_win_notification(
                    market_slug=market.slug,
                    pnl=pnl,
                    winner=winner,
                    up_qty=pre_resolution_pos["up_size"],
                    up_avg_price=pre_resolution_pos["up_avg"],
                    down_qty=pre_resolution_pos["down_size"],
                    down_avg_price=pre_resolution_pos["down_avg"],
                    pair_cost=pair_cost,
                )

        # Rotate to next market with retry logic
        rotation_success = False
        for attempt in range(3):
            try:
                rotation_success = await self._rotator.rotate()
                if rotation_success:
                    new_market = self._rotator.current_market
                    new_slug = new_market.slug if new_market else "None"
                    health_monitor.record_market_transition(self.strategy_name, old_market_slug, new_slug)
                    logger.info(f"[{self.strategy_name}] Successfully rotated to {new_slug}")
                    self._is_new_market = True  # Flag for next cycle
                    self._merged_this_market = False  # Reset merge flag for new market
                    self._replacement_count.clear()  # Reset replacement tracking for new market
                    self._chase_exhausted_logged.clear()  # Reset log tracking for new market
                    self._last_emergency_time.clear()  # Reset emergency cooldown for new market
                    # Clear fill rate stats for old market (live mode only)
                    if hasattr(self._engine, 'clear_fill_stats'):
                        self._engine.clear_fill_stats(old_market_slug)
                    # Reset market type detector for new market
                    self._market_detector = MarketTypeDetector()
                    self._detected_market_type = "UNKNOWN"
                    logger.info(f"[MARKET_DETECTOR] Reset for new market {new_slug}")
                    # Reset ONE BUY strategy for new market
                    if self._one_buy_strategy:
                        self._one_buy_strategy.reset()
                        logger.info(f"[1BUY] Strategy reset for new market {new_slug}")
                    break
                else:
                    logger.warning(f"[{self.strategy_name}] Rotation returned False (no next market?)")
                    break
            except Exception as e:
                logger.error(f"[{self.strategy_name}] Rotation attempt {attempt+1}/3 failed: {e}")
                health_monitor.record_error(self.strategy_name, f"Rotation failed: {e}")
                if attempt < 2:
                    await asyncio.sleep(5)  # Wait before retry
                else:
                    logger.critical(f"[{self.strategy_name}] All rotation attempts failed!")

        # Check for graceful stop after market ends
        if self._graceful_stop_requested:
            logger.info(f"Graceful stop requested - stopping after market {market.slug}")
            self._running = False
            if self._telegram:
                await self._telegram.send_message(
                    f"\u2705 <b>Graceful Stop Complete</b>\n\n"
                    f"Strategy: {self.strategy_name}\n"
                    f"Stopped after: {market.slug}"
                )

    # NOTE: The following methods were removed during simplification:
    # - _asymmetric_trading_cycle (gabagool strategy)
    # - _volatility_capture_cycle (MA-based dip buying)
    # - _trading_cycle (legacy pair mode)
    # - Related helper functions
    # See polymarket-amm-bot-full-version/ for the complete implementation.

    async def cleanup(self) -> None:
        """Cleanup resources."""
        logger.info("Cleaning up...")

        # Stop auto-redeemer first
        if self._auto_redeemer:
            await self._auto_redeemer.stop()

        if self._telegram:
            await self._telegram.stop()
        if self._client:
            await self._client.disconnect()
        if self._finder:
            await self._finder.close()
        if self._binance_client:
            await self._binance_client.disconnect()

        # Disconnect user WebSocket
        await self._teardown_user_websocket()

        # Final stats
        runtime = datetime.now(timezone.utc) - self._start_time if self._start_time else timedelta(0)
        logger.info("=" * 50)
        logger.info("PAPER TRADING BOT - FINAL SUMMARY")
        logger.info("=" * 50)
        logger.info(f"Runtime: {runtime}")
        logger.info(f"Initial Balance: ${self.initial_balance:.2f}")
        logger.info(f"Final Balance: ${self._engine.balance:.2f}")
        logger.info(f"Realized P&L: ${self._engine.get_realized_pnl():.4f}")
        logger.info(f"Unrealized P&L: ${self._engine.get_total_pnl():.4f}")
        logger.info(f"Total Trades: {self._trade_count}")
        logger.info(f"Total Pairs: {self._total_pairs}")
        logger.info(f"Opportunities Checked: {self._opportunities_checked}")
        logger.info(f"Profitable Opportunities: {self._profitable_opportunities}")
        logger.info(f"CSV Log: {self.csv_path}")
        logger.info("=" * 50)

    def stop(self) -> None:
        """Stop the bot immediately."""
        logger.info("Stop signal received...")
        self._running = False

    def graceful_stop(self) -> None:
        """Request graceful stop - will stop after current market ends."""
        logger.info(f"Graceful stop requested for {self.strategy_name} - will stop after current market")
        self._graceful_stop_requested = True

    # === Telegram Command Handlers ===

    async def _handle_telegram_stop(self) -> None:
        """Handle /stop command from Telegram - immediate stop."""
        logger.info("Stop command received from Telegram")
        self.stop()

    async def _handle_telegram_graceful_stop(self) -> None:
        """Handle graceful stop from Telegram - stop after current market."""
        logger.info(f"Graceful stop command received from Telegram for {self.strategy_name}")
        self.graceful_stop()

    async def _handle_telegram_sell_all(self) -> None:
        """Handle /sell_all command from Telegram."""
        logger.info("Emergency sell command received from Telegram")
        await self.emergency_sell_all()

    async def _handle_telegram_status(self) -> str:
        """Handle /status command from Telegram."""
        runtime = datetime.now(timezone.utc) - self._start_time if self._start_time else timedelta(0)
        hours = runtime.total_seconds() / 3600

        market = self._rotator.current_market if self._rotator else None
        market_name = market.slug[-25:] if market else "None"

        # Get position info
        total_pairs = sum(p.pair_count for p in self._engine.positions) if self._engine else 0
        realized = self._engine.get_realized_pnl() if self._engine else 0

        # Determine mode label
        if self.directional_mode:
            mode = "Directional"
        elif self.accum_mode == "volume_weighted":
            mode = "Volume Weighted"
        elif self.accum_mode == "calculus_maker":
            mode = "Calculus Maker"
        elif self.accum_mode == "fair_value_mm":
            mode = "Fair Value MM"
        elif self.accum_mode == "simple_hedger":
            mode = "Simple Hedger"
        else:
            mode = "Standard"

        status = f"""Mode: {mode}
Running: {self._running}
Runtime: {hours:.1f}h
Market: {market_name}
Trades: {self._trade_count}
Pairs: {total_pairs}
Realized: ${realized:.2f}"""
        return status

    async def _handle_telegram_balance(self) -> str:
        """Handle /balance command from Telegram."""
        balance = self._engine.balance if self._engine else self.initial_balance
        realized = self._engine.get_realized_pnl() if self._engine else 0
        return f"${balance:.2f} USDC\nRealized P&L: ${realized:.2f}"

    async def emergency_sell_all(self) -> dict:
        """
        Emergency sell all positions and stop the bot.

        In paper trading mode, this calculates what the P&L would be
        if all positions were sold at current market prices.

        Returns:
            Dict with sell summary including positions closed and P&L
        """
        logger.warning("=" * 50)
        logger.warning("EMERGENCY SELL ALL TRIGGERED")
        logger.warning("=" * 50)

        # Stop the bot first
        self._running = False

        results = {
            "positions_closed": 0,
            "total_up_sold": 0,
            "total_down_sold": 0,
            "total_proceeds": 0.0,
            "total_cost": 0.0,
            "realized_pnl": 0.0,
            "details": []
        }

        if not self._engine:
            logger.warning("No trading engine - nothing to sell")
            return results

        # Get all positions
        for position in self._engine.positions:
            if position.up_size <= 0 and position.down_size <= 0:
                continue

            results["positions_closed"] += 1

            # Calculate proceeds at current prices (or use last known prices)
            # In paper trading, we simulate selling at current bid prices
            # For simplicity, use the last known prices with some slippage
            up_sell_price = self._last_up_price * 0.995 if self._last_up_price > 0 else 0.45
            down_sell_price = self._last_down_price * 0.995 if self._last_down_price > 0 else 0.45

            up_proceeds = position.up_size * up_sell_price
            down_proceeds = position.down_size * down_sell_price
            up_cost = position.up_size * position.up_avg_price
            down_cost = position.down_size * position.down_avg_price

            total_proceeds = up_proceeds + down_proceeds
            total_cost = up_cost + down_cost
            pnl = total_proceeds - total_cost

            detail = {
                "market": position.market_slug,
                "up_size": position.up_size,
                "up_avg_price": position.up_avg_price,
                "up_sell_price": up_sell_price,
                "up_proceeds": up_proceeds,
                "down_size": position.down_size,
                "down_avg_price": position.down_avg_price,
                "down_sell_price": down_sell_price,
                "down_proceeds": down_proceeds,
                "cost": total_cost,
                "proceeds": total_proceeds,
                "pnl": pnl
            }
            results["details"].append(detail)

            results["total_up_sold"] += position.up_size
            results["total_down_sold"] += position.down_size
            results["total_proceeds"] += total_proceeds
            results["total_cost"] += total_cost
            results["realized_pnl"] += pnl

            logger.warning(
                f"SOLD: {position.market_slug} - "
                f"UP: {position.up_size:.0f} @ ${up_sell_price:.4f} = ${up_proceeds:.2f}, "
                f"DOWN: {position.down_size:.0f} @ ${down_sell_price:.4f} = ${down_proceeds:.2f}, "
                f"P&L: ${pnl:.2f}"
            )

        logger.warning("=" * 50)
        logger.warning(f"EMERGENCY SELL COMPLETE")
        logger.warning(f"Positions closed: {results['positions_closed']}")
        logger.warning(f"Total proceeds: ${results['total_proceeds']:.2f}")
        logger.warning(f"Total cost: ${results['total_cost']:.2f}")
        logger.warning(f"Realized P&L: ${results['realized_pnl']:.2f}")
        logger.warning("=" * 50)

        return results


def parse_time(time_str: str) -> Optional[datetime]:
    """
    Parse time string into datetime object.

    Supports formats:
    - "HH:MM TZ" (e.g., "13:00 EST") - assumes today's date
    - "YYYY-MM-DD HH:MM TZ" (e.g., "2025-12-20 13:00 EST")
    - "HH:MM" without TZ assumes local time

    Supported timezones: EST, PST, UTC, IST, CST, MST, EDT, PDT
    """
    # Timezone mappings
    tz_map = {
        "EST": "America/New_York",
        "EDT": "America/New_York",
        "PST": "America/Los_Angeles",
        "PDT": "America/Los_Angeles",
        "CST": "America/Chicago",
        "CDT": "America/Chicago",
        "MST": "America/Denver",
        "MDT": "America/Denver",
        "UTC": "UTC",
        "GMT": "UTC",
        "IST": "Asia/Kolkata",
    }

    time_str = time_str.strip()

    # Extract timezone if present
    tz_name = None
    for tz_abbrev in tz_map.keys():
        if time_str.upper().endswith(tz_abbrev):
            tz_name = tz_map[tz_abbrev]
            time_str = time_str[:-len(tz_abbrev)].strip()
            break

    # Try to parse
    parsed_dt = None
    today = datetime.now().date()

    # Try full datetime format first
    for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"]:
        try:
            parsed_dt = datetime.strptime(time_str, fmt)
            break
        except ValueError:
            continue

    # Try time-only format (use today's date, or tomorrow if time has passed)
    if parsed_dt is None:
        for fmt in ["%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"]:
            try:
                time_part = datetime.strptime(time_str, fmt).time()
                parsed_dt = datetime.combine(today, time_part)
                break
            except ValueError:
                continue

    if parsed_dt is None:
        return None

    # Apply timezone
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
            parsed_dt = parsed_dt.replace(tzinfo=tz)
        except Exception:
            return None
    else:
        # Assume local timezone
        parsed_dt = parsed_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)

    # If the time is in the past (for time-only input), assume tomorrow
    now = datetime.now(parsed_dt.tzinfo)
    if parsed_dt < now and (now - parsed_dt).total_seconds() < 86400:
        parsed_dt = parsed_dt + timedelta(days=1)

    return parsed_dt


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='Paper Trading Bot (Accumulation Mode)')
    parser.add_argument(
        '--duration', '-d',
        type=float,
        default=None,
        help='Duration in minutes (default: 60 if no --end-time)',
    )
    parser.add_argument(
        '--end-time', '-e',
        type=str,
        default=None,
        help='End time in format "HH:MM" or "YYYY-MM-DD HH:MM" with optional timezone (EST/PST/UTC/IST). '
             'Examples: "13:00 EST", "2025-12-20 01:00 UTC", "14:30 IST"',
    )
    parser.add_argument(
        '--start-time',
        type=str,
        default=None,
        help='Start time - bot waits until this time to begin. Same format as --end-time. '
             'Examples: "14:15 EST", "2025-12-20 09:00 UTC"',
    )
    parser.add_argument(
        '--balance', '-b',
        type=float,
        default=100.0,
        help='Initial paper balance (default: $100)',
    )
    parser.add_argument(
        '--csv', '-c',
        type=str,
        default='paper_trades.csv',
        help='CSV output file (default: paper_trades.csv)',
    )
    parser.add_argument(
        '--discord-interval', '-i',
        type=float,
        default=30.0,
        help='Discord update interval in minutes (default: 30)',
    )
    parser.add_argument(
        '--check-interval',
        type=float,
        default=0.0,
        help='Seconds between opportunity checks (default: 0 for max speed)',
    )

    # Terminal display
    parser.add_argument(
        '--live-display', '-l',
        action='store_true',
        default=False,
        help='Enable live terminal display showing position updates in real-time',
    )
    parser.add_argument(
        '--quiet', '-q',
        action='store_true',
        default=False,
        help='Quiet mode - suppress HTTP logs and reduce noise (useful with --live-display)',
    )
    parser.add_argument(
        '--trading-mode',
        type=str,
        choices=['paper', 'live'],
        default='paper',
        help='Trading mode: paper (simulated) or live (real orders). Default: paper',
    )

    # ACCUMULATION MODE parameters (now the only mode)
    parser.add_argument(
        '--accum-trade-size',
        type=int,
        default=2,
        help='Shares per trade (default: 2)',
    )
    parser.add_argument(
        '--accum-pair-cost-limit',
        type=float,
        default=0.995,
        help='Max average pair cost allowed (default: 0.995)',
    )
    parser.add_argument(
        '--accum-max-imbalance-pct',
        type=float,
        default=0.15,
        help='Max imbalance as %% of target before forcing rebalance (default: 0.15 = 15%%)',
    )
    parser.add_argument(
        '--accum-target-shares',
        type=int,
        default=50,
        help='Target shares per side per market (default: 50)',
    )
    parser.add_argument(
        '--accum-single-side',
        action='store_true',
        default=False,
        help='Only buy one side per cycle instead of both (default: buy both)',
    )
    parser.add_argument(
        '--accum-max-share-price',
        type=float,
        default=0.95,
        help='Never buy shares above this price (default: 0.95)',
    )
    parser.add_argument(
        '--accum-mode',
        type=str,
        choices=['standard', 'volume_weighted', 'calculus_maker'],
        default='standard',
        help='Accumulation strategy mode: standard, volume_weighted (Gabagool-style), or calculus_maker (exponential decay)',
    )

    # DIRECTIONAL MODE parameters
    parser.add_argument(
        '--directional',
        action='store_true',
        default=False,
        help='Enable directional trading mode (requires --bias)',
    )
    parser.add_argument(
        '--bias',
        type=str,
        choices=['BULLISH', 'BEARISH'],
        default=None,
        help='Initial trading bias for directional mode',
    )
    parser.add_argument(
        '--sigma-threshold',
        type=float,
        default=2.0,
        help='Standard deviations for impulsive move detection (default: 2.0)',
    )
    parser.add_argument(
        '--sustained-seconds',
        type=float,
        default=30.0,
        help='Seconds price must sustain opposite direction before flip (default: 30)',
    )
    parser.add_argument(
        '--window-seconds',
        type=int,
        default=60,
        help='Rolling window for price statistics (default: 60)',
    )
    parser.add_argument(
        '--attractive-price-early',
        type=float,
        default=0.75,
        help='Max price early in market (>10 mins left) (default: 0.75)',
    )
    parser.add_argument(
        '--attractive-price-late',
        type=float,
        default=0.90,
        help='Max price late in market (<2 mins left) (default: 0.90)',
    )
    parser.add_argument(
        '--hedge-increment',
        type=int,
        default=5,
        help='Shares to buy per hedge cycle (default: 5)',
    )
    parser.add_argument(
        '--flip-cooldown',
        type=float,
        default=60.0,
        help='Seconds between allowed flips (default: 60)',
    )
    parser.add_argument(
        '--max-position-pct',
        type=float,
        default=0.15,
        help='Max shares per side as %% of balance (default: 15%% → $100 = 15 shares)',
    )
    parser.add_argument(
        '--dir-target-shares',
        type=int,
        default=15,
        help='Target shares per side for directional mode (default: 15)',
    )
    parser.add_argument(
        '--pair-cost-target',
        type=float,
        default=0.95,
        help='Target pair cost for hedging (default: 0.95)',
    )

    args = parser.parse_args()

    # Apply quiet mode if requested
    if args.quiet:
        # Suppress HTTP request logs
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        # Reduce main logger to only show important messages
        logging.getLogger().setLevel(logging.WARNING)
        # But keep our bot logger at INFO for trade notifications
        logger.setLevel(logging.INFO)

    # Create directional config if in directional mode
    directional_config = None
    if args.directional:
        if not args.bias:
            logger.error("Directional mode requires --bias (BULLISH or BEARISH)")
            return
        directional_config = DirectionalConfig(
            sigma_threshold=args.sigma_threshold,
            sustained_seconds=args.sustained_seconds,
            window_seconds=args.window_seconds,
            flip_cooldown_seconds=args.flip_cooldown,
            max_position_pct=args.max_position_pct,
            target_shares=args.dir_target_shares,
            attractive_price_early=args.attractive_price_early,
            attractive_price_late=args.attractive_price_late,
            hedge_increment=args.hedge_increment,
            pair_cost_target=args.pair_cost_target,
            max_share_price=args.accum_max_share_price,  # Reuse same price ceiling
        )

    # Create bot
    bot = PaperTradingBot(
        initial_balance=args.balance,
        csv_path=args.csv,
        discord_interval_minutes=args.discord_interval,
        live_display=args.live_display,
        # Accumulation mode parameters
        accum_trade_size=args.accum_trade_size,
        accum_pair_cost_limit=args.accum_pair_cost_limit,
        accum_max_imbalance_pct=args.accum_max_imbalance_pct,
        accum_target_shares=args.accum_target_shares,
        accum_buy_both_sides=not args.accum_single_side,
        accum_max_share_price=args.accum_max_share_price,
        accum_mode=args.accum_mode,
        max_position_pct=args.max_position_pct,
        # Directional mode parameters
        directional_mode=args.directional,
        initial_bias=args.bias,
        directional_config=directional_config,
        # Quiet mode
        quiet_mode=args.quiet,
        # Trading mode (paper or live)
        trading_mode=args.trading_mode,
    )

    # Handle signals
    def signal_handler(sig, frame):
        logger.info(f"Received signal {sig}")
        bot.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Parse end-time if provided
    end_time_dt = None
    if args.end_time:
        end_time_dt = parse_time(args.end_time)
        if end_time_dt is None:
            logger.error(f"Could not parse end time: {args.end_time}")
            logger.error("Use format: 'HH:MM TZ' or 'YYYY-MM-DD HH:MM TZ' (e.g., '13:00 EST', '2025-12-20 01:00 UTC')")
            return

    # Parse start-time if provided and wait
    if args.start_time:
        start_time_dt = parse_time(args.start_time)
        if start_time_dt is None:
            logger.error(f"Could not parse start time: {args.start_time}")
            logger.error("Use format: 'HH:MM TZ' or 'YYYY-MM-DD HH:MM TZ' (e.g., '14:15 EST', '2025-12-20 09:00 UTC')")
            return

        now = datetime.now(start_time_dt.tzinfo)
        if start_time_dt > now:
            wait_seconds = (start_time_dt - now).total_seconds()
            local_start = start_time_dt.astimezone()
            logger.info(f"Waiting until {local_start.strftime('%Y-%m-%d %H:%M:%S %Z')} to start...")
            logger.info(f"({wait_seconds:.0f} seconds / {wait_seconds/60:.1f} minutes)")

            # Wait with periodic updates
            while datetime.now(start_time_dt.tzinfo) < start_time_dt:
                remaining = (start_time_dt - datetime.now(start_time_dt.tzinfo)).total_seconds()
                if remaining > 60:
                    logger.info(f"Starting in {remaining/60:.1f} minutes...")
                    await asyncio.sleep(min(60, remaining))
                elif remaining > 0:
                    await asyncio.sleep(remaining)
                else:
                    break

            logger.info("Start time reached! Beginning trading...")
        else:
            logger.info(f"Start time {args.start_time} has already passed, starting immediately")

    # Run
    try:
        await bot.initialize()
        await bot.run(
            duration_minutes=args.duration,
            check_interval=args.check_interval,
            end_time=end_time_dt,
        )
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
        bot.stop()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
