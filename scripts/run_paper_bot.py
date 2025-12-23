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
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Deque, Callable
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
from src.services.pair_analyzer import PairAnalyzer, AsymmetricOpportunity
from src.services.paper_trading import PaperTradingEngine, SimulationConfig, PaperPosition
from src.services.live_trading import LiveTradingEngine
from src.api.polymarket_client import PolymarketClient
from src.services.directional_strategy import (
    DirectionalTradingStrategy,
    DirectionalConfig,
    Bias,
    DirectionalPhase,
    TradeDecision,
)


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
        accum_max_imbalance_shares: int = 5,  # Max share difference before forcing rebalance
        accum_target_shares: int = 15,  # Target shares per side per market
        accum_buy_both_sides: bool = True,  # Try to buy both sides each cycle
        accum_max_share_price: float = 0.98,  # Never buy shares above this price (Gabagool buys up to $0.98)
        # VOLUME WEIGHTED MODE - Gabagool-style accumulation
        accum_mode: str = "standard",  # "standard" or "volume_weighted"
        # GABAGOOL-STYLE SETTINGS (reverse-engineered from their Dec 2024 behavior)
        vw_imbalance_pct: float = 0.40,  # Max 40% imbalance (gabagool avg: 39.6%)
        vw_cheap_threshold: float = 0.45,  # Buy aggressively below this (gabagool loads up < $0.45)
        vw_hedge_trigger_pct: float = 0.30,  # Start hedging at 30% imbalance (not 5%!)
        vw_max_hedge_price: float = 0.70,  # NEVER pay > $0.70 for hedge (gabagool max: $0.87, but safer)
        vw_bootstrap_pct: float = 0.33,  # Bootstrap phase: buy both sides until 33% of target (gabagool: ~1.5% but we need more for smaller positions)
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
    ):
        self.initial_balance = initial_balance
        self.trading_mode = trading_mode
        self.csv_path = Path(csv_path)
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
        self.accum_max_imbalance_shares = accum_max_imbalance_shares
        self.accum_target_shares = accum_target_shares
        self.accum_buy_both_sides = accum_buy_both_sides
        self.accum_max_share_price = accum_max_share_price

        # Volume Weighted mode parameters
        self.accum_mode = accum_mode
        self.vw_imbalance_pct = vw_imbalance_pct
        self.vw_cheap_threshold = vw_cheap_threshold
        self.vw_hedge_trigger_pct = vw_hedge_trigger_pct
        self.vw_max_hedge_price = vw_max_hedge_price
        self.vw_bootstrap_pct = vw_bootstrap_pct

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

        # Telegram notifications and remote control
        self._telegram: Optional[TelegramNotifier] = None

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

    @classmethod
    def from_web_config(
        cls,
        config: dict,
        web_callback: Optional[Callable[[dict], None]] = None,
        strategy_name: str = "accumulation",
    ) -> "PaperTradingBot":
        """Create bot instance from web UI configuration.

        Args:
            config: Dictionary with web UI configuration values
            web_callback: Optional callback for web UI updates
            strategy_name: Strategy identifier for Discord and web UI (e.g., "standard", "volume_weighted")

        Returns:
            PaperTradingBot instance configured from web UI
        """
        # Determine accum_mode from config or strategy_name
        accum_mode = config.get("accum_mode", "standard")
        if strategy_name in ["standard", "volume_weighted"]:
            accum_mode = strategy_name

        # Use mode-specific CSV file names
        csv_path = f"paper_trades_{accum_mode}.csv"

        # Get trading mode (paper or live)
        trading_mode = config.get("mode", "paper")

        return cls(
            initial_balance=config.get("starting_balance", 100.0),
            # Accumulation mode params
            accum_mode=accum_mode,
            accum_max_share_price=config.get("max_share_price", 0.95),
            accum_trade_size=config.get("accum_trade_size", 1),
            accum_target_shares=config.get("accum_target_shares", 15),
            accum_max_imbalance_shares=config.get("accum_max_imbalance", 5),
            accum_pair_cost_target=config.get("accum_pair_cost_target", 0.995),
            accum_pair_cost_limit=config.get("accum_pair_cost_limit", 1.02),
            accum_buy_both_sides=config.get("accum_buy_both_sides", True),
            # Volume Weighted mode params (only used when accum_mode="volume_weighted")
            # Gabagool-style settings
            vw_imbalance_pct=config.get("vw_imbalance_pct", 0.40),
            vw_cheap_threshold=config.get("vw_cheap_threshold", 0.45),
            vw_hedge_trigger_pct=config.get("vw_hedge_trigger_pct", 0.30),
            vw_max_hedge_price=config.get("vw_max_hedge_price", 0.70),
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
        )

    @classmethod
    def from_directional_config(
        cls,
        config: dict,
        web_callback: Optional[Callable[[dict], None]] = None,
    ) -> "PaperTradingBot":
        """Create bot instance from Directional web UI configuration.

        Args:
            config: Dictionary with directional configuration values
            web_callback: Optional callback for web UI updates

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
            # Output
            csv_path="paper_trades_directional.csv",
            live_display=True,
            # Web callback
            web_callback=web_callback,
            # Strategy name
            strategy_name="directional",
        )

    async def initialize(self) -> None:
        """Initialize all components."""
        logger.info("Initializing paper trading bot...")

        self._config = Config()
        self._client = PolymarketClient(self._config)
        await self._client.connect()

        # Initialize Telegram for notifications and remote control
        self._telegram = TelegramNotifier(self._config)
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
                self._telegram.on_graceful_stop_standard(_noop)
                self._telegram.on_graceful_stop_volume_weighted(_noop)
            elif self.accum_mode == "volume_weighted":
                mode_label = "Volume Weighted (Gabagool-style)"
                self._telegram.on_graceful_stop_volume_weighted(self._handle_telegram_graceful_stop)
                self._telegram.on_graceful_stop_standard(_noop)
                self._telegram.on_graceful_stop_directional(_noop)
            else:
                mode_label = "Standard"
                self._telegram.on_graceful_stop_standard(self._handle_telegram_graceful_stop)
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
            )
            # Sync balance from chain
            await self._engine.sync_balance()
            logger.info(f"Live balance: ${self._engine.balance:.2f}")
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
        self._rotator = MarketRotator(
            finder=self._finder,
            continuous=True,
            market_window_minutes=60,
        )

        # Check if markets available in continuous mode
        window_markets = await self._finder.get_markets_in_window(hours=1.0)
        if not window_markets:
            logger.info("No markets in 60-min window, using session mode")
            self._rotator = MarketRotator(
                finder=self._finder,
                continuous=False,  # Session mode
                max_markets=100,
                market_window_minutes=60,
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
        position,  # PaperPosition or None
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

    def _build_web_state(self) -> dict:
        """Build trading state as JSON for web UI."""
        market = self._rotator.current_market if self._rotator else None
        position = self._engine.get_position(market) if market and self._engine else None

        # Calculate time remaining
        time_remaining = "N/A"
        time_remaining_secs = 0
        if market and market.end_time:
            remaining = (market.end_time - datetime.now(timezone.utc)).total_seconds()
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
                pos = self._engine.get_position(self._current_market) if self._current_market else None
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
            # Determine mode label
            if self.directional_mode:
                mode = "Directional"
            elif self.accum_mode == "volume_weighted":
                mode = "Volume Weighted"
            else:
                mode = "Standard"
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
            logger.info(f"  - Max position: {self.directional_config.max_position_pct*100:.0f}% per side")
            logger.info(f"  - Attractive price: ${self.directional_config.attractive_price_early:.2f} → ${self.directional_config.attractive_price_late:.2f} (time decay)")
            logger.info(f"  - Sigma threshold: {self.directional_config.sigma_threshold}σ")
            logger.info(f"  - Sustained time: {self.directional_config.sustained_seconds}s")
            logger.info(f"  - Emergency hedge: <{self.directional_config.emergency_threshold_secs}s remaining")
            logger.info(f"  - BTC price: ${self._binance_client.current_price:,.2f}")
        else:
            mode_label = "VOLUME WEIGHTED (Gabagool-style)" if self.accum_mode == "volume_weighted" else "STANDARD"
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
                logger.info(f"  - Max imbalance: {self.accum_max_imbalance_shares} shares")
            logger.info(f"  - Target shares: {self.accum_target_shares} per side")
            logger.info(f"  - Buy both sides: {self.accum_buy_both_sides}")
            logger.info(f"  - Price ceiling: ${self.accum_max_share_price} (never buy above)")
        logger.info("=" * 50)
        if self.live_display_enabled:
            logger.info("LIVE DISPLAY ENABLED - Position updates will show in terminal")
        logger.info(f"Will run until {end_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")

        # Send initial Discord message
        await self._send_discord_update()

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
        """Inner trading loop logic."""
        try:
            while self._running and datetime.now(timezone.utc) < end_time:
                try:
                    # Run appropriate trading cycle
                    if self.directional_mode:
                        await self._directional_trading_cycle()
                    else:
                        await self._accumulation_trading_cycle()
                except Exception as e:
                    # Log but don't crash - the retry logic in trading cycles
                    # handles most errors, this is a safety net
                    logger.error(f"Unexpected error in trading cycle: {e}")
                    if not await self._interruptible_sleep(self.retry_base_delay, check_interval=2.0):
                        break
                    continue

                # Wait before next cycle (interruptible for responsive stop)
                if not await self._interruptible_sleep(check_interval):
                    break

        except asyncio.CancelledError:
            logger.info("Bot cancelled")
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
        """
        max_position = max(current_up, current_down, 1)
        return max(1, int(max_position * self.vw_imbalance_pct))

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
            self._is_new_market = False

        # Get current position
        position = self._engine.get_position(market)
        current_up = position.up_size if position else 0.0
        current_down = position.down_size if position else 0.0
        current_up_cost = position.up_cost if position else 0.0
        current_down_cost = position.down_cost if position else 0.0

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

        up_price = opportunity.up_ask
        down_price = opportunity.down_ask
        pair_cost = up_price + down_price

        # Log pair cost status (buy first, hedge later - no hard limit)
        if self._opportunities_checked % 10 == 0:
            logger.info(f"[ACCUM] UP=${up_price:.3f} DOWN=${down_price:.3f} PairCost=${pair_cost:.4f}")

        # Calculate current average pair cost
        current_avg_pair_cost = 0.0
        if position and position.pair_count > 0:
            current_avg_pair_cost = position.up_avg_price + position.down_avg_price

        # Calculate imbalance in shares
        share_imbalance = abs(current_up - current_down)
        deficit_side = "UP" if current_down > current_up else "DOWN" if current_up > current_down else None

        # Calculate time remaining for dynamic sizing
        time_remaining_secs = 900  # Default 15 min
        if market.end_time:
            time_remaining_secs = max(0, (market.end_time - datetime.now(timezone.utc)).total_seconds())

        # Determine what to buy this cycle
        buy_up = False
        buy_down = False
        # Dynamic sizing: 20% at 15min → 10% at 5min → 2% at 0min
        buy_size = calculate_dynamic_trade_size(
            time_remaining_secs=time_remaining_secs,
            max_target_shares=self.accum_target_shares,
            min_size=1
        )

        # BALANCE-FIRST LOGIC: If imbalanced beyond threshold, prioritize deficit side
        force_rebalance = False
        # Use percentage-based imbalance in volume_weighted mode, absolute in standard
        if self.accum_mode == "volume_weighted":
            max_imbalance = self.get_vw_max_imbalance(current_up, current_down)
        else:
            max_imbalance = self.accum_max_imbalance_shares
        if share_imbalance > max_imbalance:
            force_rebalance = True
            # Force rebalance - only buy deficit side
            if deficit_side == "UP" and current_up < self.accum_target_shares:
                buy_up = True
                buy_down = False
                # Buy more to catch up (up to 2x dynamic size)
                base_size = buy_size  # Already calculated dynamically above
                buy_size = min(base_size * 2, max(base_size, int(share_imbalance / 2)))
                logger.info(f"REBALANCE: Buying {buy_size} UP to reduce imbalance ({share_imbalance:.0f} shares)")
            elif deficit_side == "DOWN" and current_down < self.accum_target_shares:
                buy_up = False
                buy_down = True
                # Buy more to catch up (up to 2x dynamic size)
                base_size = buy_size  # Already calculated dynamically above
                buy_size = min(base_size * 2, max(base_size, int(share_imbalance / 2)))
                logger.info(f"REBALANCE: Buying {buy_size} DOWN to reduce imbalance ({share_imbalance:.0f} shares)")
        else:
            # Normal accumulation - buy both sides if possible
            if self.accum_buy_both_sides:
                buy_up = current_up < self.accum_target_shares
                buy_down = current_down < self.accum_target_shares
            else:
                # Alternate sides or buy cheaper side
                if current_up <= current_down and current_up < self.accum_target_shares:
                    buy_up = True
                elif current_down < self.accum_target_shares:
                    buy_down = True

        # Check pair cost TARGET before buying (normal trading - buy cheap)
        # Use TARGET for normal trading, LIMIT only for rebalancing
        if not force_rebalance:
            if self.accum_mode == "volume_weighted":
                # TRUE GABAGOOL MODE: Continuous accumulation on BOTH sides
                # Analysis of gabagool22 (Dec 2024) revealed:
                # - NO selective buying - they buy at ALL prices ($0.02 to $0.98)
                # - NO cheap threshold - continuous buying both sides
                # - Volume weighting handles balance (buy more when cheap)
                # - Expensive buys on one side offset by cheap buys on other
                # - Final pair cost always ends under $1.00
                #
                # buy_up and buy_down stay True - volume weighting does the work
                logger.debug(f"[VW] True Gabagool: buying both sides UP@${up_price:.3f} DOWN@${down_price:.3f}")
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

        # SAFETY CAP for rebalancing: Even during rebalance, never exceed hard limit
        if force_rebalance and position and position.pair_count > 0:
            if buy_up:
                new_up_cost = current_up_cost + (buy_size * up_price)
                new_up_size = current_up + buy_size
                new_up_avg = new_up_cost / new_up_size if new_up_size > 0 else up_price
                prospective_pair_cost = new_up_avg + position.down_avg_price
                if prospective_pair_cost > self.accum_pair_cost_limit:
                    buy_up = False
                    logger.info(f"REBALANCE BLOCKED: pair cost ${prospective_pair_cost:.4f} > limit ${self.accum_pair_cost_limit}")
            if buy_down:
                new_down_cost = current_down_cost + (buy_size * down_price)
                new_down_size = current_down + buy_size
                new_down_avg = new_down_cost / new_down_size if new_down_size > 0 else down_price
                prospective_pair_cost = position.up_avg_price + new_down_avg
                if prospective_pair_cost > self.accum_pair_cost_limit:
                    buy_down = False
                    logger.info(f"REBALANCE BLOCKED: pair cost ${prospective_pair_cost:.4f} > limit ${self.accum_pair_cost_limit}")

        # PRICE CEILING: Never buy shares above max price (prevents guaranteed losses)
        if buy_up and up_price > self.accum_max_share_price:
            buy_up = False
            logger.info(f"⛔ SKIP UP: price ${up_price:.3f} > ceiling ${self.accum_max_share_price}")
        if buy_down and down_price > self.accum_max_share_price:
            buy_down = False
            logger.info(f"⛔ SKIP DOWN: price ${down_price:.3f} > ceiling ${self.accum_max_share_price}")

        # GABAGOOL-STYLE VOLUME WEIGHTING: Percentage of remaining capacity, weighted by price
        # Only applies in volume_weighted mode
        if self.accum_mode == "volume_weighted":
            up_remaining = max(0, int(self.accum_target_shares - current_up))
            down_remaining = max(0, int(self.accum_target_shares - current_down))
            up_buy_size = self.get_vw_trade_size(up_price, up_remaining)
            down_buy_size = self.get_vw_trade_size(down_price, down_remaining)
            logger.debug(f"[VW] UP: {up_remaining} remaining → buy {up_buy_size} @ ${up_price:.3f} ({int(up_buy_size/max(1,up_remaining)*100) if up_remaining > 0 else 0}%)")
            logger.debug(f"[VW] DOWN: {down_remaining} remaining → buy {down_buy_size} @ ${down_price:.3f} ({int(down_buy_size/max(1,down_remaining)*100) if down_remaining > 0 else 0}%)")
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

        # IMBALANCE ENFORCEMENT: Block trades that would exceed max imbalance
        # Note: max_imbalance was calculated earlier (percentage-based in volume_weighted, absolute in standard)
        if buy_up:
            new_up = current_up + up_buy_size
            new_imbalance = abs(new_up - current_down)
            if new_imbalance > max_imbalance:
                buy_up = False
                mode_label = "[VW]" if self.accum_mode == "volume_weighted" else ""
                logger.info(f"⛔ {mode_label} BLOCKED UP: would create imbalance {new_imbalance:.0f} > limit {max_imbalance}")
        if buy_down:
            new_down = current_down + down_buy_size
            new_imbalance = abs(current_up - new_down)
            if new_imbalance > max_imbalance:
                buy_down = False
                mode_label = "[VW]" if self.accum_mode == "volume_weighted" else ""
                logger.info(f"⛔ {mode_label} BLOCKED DOWN: would create imbalance {new_imbalance:.0f} > limit {max_imbalance}")

        # Execute trades
        trades_made = 0

        if buy_up and self._engine.balance >= up_price * up_buy_size:
            result = await self._engine.execute_single_side_trade(
                market=market,
                side="UP",
                price=up_price,
                size=up_buy_size,
            )
            if result["success"]:
                trades_made += 1
                self._trade_count += 1
                self._profitable_opportunities += 1
                # Log to CSV
                updated_position = self._engine.get_position(market)
                self._log_event_csv(
                    market_slug=market.slug,
                    event_type="TRADE",
                    trade_side="UP",
                    trade_mode="ACCUM",
                    size_requested=up_buy_size,
                    size_filled=result["filled_size"],
                    price=result["filled_price"],
                    cost=result["cost"],
                    position=updated_position,
                    status="SUCCESS",
                )
                # Emit trade event for web UI trade log
                self._send_trade_event("UP", result["filled_size"], result["filled_price"], "BUY")
                # Update local state for DOWN trade
                current_up += result["filled_size"]
                current_up_cost += result["cost"]

        if buy_down and self._engine.balance >= down_price * down_buy_size:
            result = await self._engine.execute_single_side_trade(
                market=market,
                side="DOWN",
                price=down_price,
                size=down_buy_size,
            )
            if result["success"]:
                trades_made += 1
                self._trade_count += 1
                self._profitable_opportunities += 1
                # Log to CSV
                updated_position = self._engine.get_position(market)
                self._log_event_csv(
                    market_slug=market.slug,
                    event_type="TRADE",
                    trade_side="DOWN",
                    trade_mode="ACCUM",
                    size_requested=down_buy_size,
                    size_filled=result["filled_size"],
                    price=result["filled_price"],
                    cost=result["cost"],
                    position=updated_position,
                    status="SUCCESS",
                )
                # Emit trade event for web UI trade log
                self._send_trade_event("DOWN", result["filled_size"], result["filled_price"], "BUY")

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
        up_price = opportunity.up_ask
        down_price = opportunity.down_ask

        # Update display prices
        self._last_up_price = up_price
        self._last_down_price = down_price
        self._last_spread = 1.0 - up_price - down_price
        self._update_live_display()  # Send update to web UI

        # Evaluate trade decision
        decision = self._directional_strategy.evaluate_trade(
            up_ask=up_price,
            down_ask=down_price,
            time_remaining_secs=time_remaining_secs,
        )

        if decision:
            # IMBALANCE ENFORCEMENT: Block trades that would exceed max imbalance
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
            # POLYMARKET $1 MINIMUM ORDER VALUE ENFORCEMENT
            min_size = max(1, math.ceil(1.00 / decision.price)) if decision.price > 0 else 1
            if decision.size < min_size:
                logger.debug(f"[MIN$1] DIR size {decision.size} → {min_size} (${decision.price:.3f} × {min_size} = ${decision.price * min_size:.2f})")
                decision.size = min_size

            # Check balance
            trade_cost = decision.price * decision.size
            if self._engine.balance >= trade_cost:
                result = await self._engine.execute_single_side_trade(
                    market=market,
                    side=decision.side,
                    price=decision.price,
                    size=decision.size,
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

        # Log status periodically
        if self._opportunities_checked % 10 == 0:
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

        # Check for rotation
        if self._rotator.should_rotate():
            self._is_new_market = True
            await self._handle_market_rotation(market)

    def _log_directional_trade_csv(
        self,
        market_slug: str,
        decision: TradeDecision,
        result: dict,
        position: Optional[PaperPosition],
    ) -> None:
        """Log a directional mode trade to CSV."""
        now = datetime.now(timezone.utc)

        # Get directional-specific data
        status = self._directional_strategy.get_status_dict()

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

    async def _handle_market_rotation(self, market) -> None:
        """Handle market rotation and position resolution."""
        logger.info(f"Rotating from {market.slug}")

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
                        import random
                        winner = random.choice(["UP", "DOWN"])
                        resolution_source = "RANDOM_NO_STRIKE"
                        logger.error(f"[RESOLUTION] No strike price! Using RANDOM: {winner}")
                else:
                    import random
                    winner = random.choice(["UP", "DOWN"])
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

            # Resolve the market
            pnl = self._engine.resolve_market(market.slug, winner)
            logger.info(f"Market resolved ({winner}): P&L ${pnl:.4f}, LockedProfit was ${locked:.4f}")

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

        await self._rotator.rotate()

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

        if self._telegram:
            await self._telegram.stop()
        if self._client:
            await self._client.disconnect()
        if self._finder:
            await self._finder.close()
        if self._binance_client:
            await self._binance_client.disconnect()

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
        '--accum-max-imbalance',
        type=int,
        default=20,
        help='Max share difference before forcing rebalance (default: 20)',
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
        choices=['standard', 'volume_weighted'],
        default='standard',
        help='Accumulation strategy mode: standard (strict pair cost) or volume_weighted (Gabagool-style hedging)',
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
        accum_max_imbalance_shares=args.accum_max_imbalance,
        accum_target_shares=args.accum_target_shares,
        accum_buy_both_sides=not args.accum_single_side,
        accum_max_share_price=args.accum_max_share_price,
        accum_mode=args.accum_mode,
        # Directional mode parameters
        directional_mode=args.directional,
        initial_bias=args.bias,
        directional_config=directional_config,
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
