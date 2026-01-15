#!/usr/bin/env python3
"""
Spread Capture Observer - ZONE 5-6 PROFITABLE Mode

Captures REAL market data at sub-second speeds for strategy analysis.
Simulates PROFITABLE strategy: winner-only entry, hedge when loser drops OR stop-loss triggers.

PROFITABLE STRATEGY CONFIG (Zone 5-6 + 7% Stop-Loss):
- Zones 5-6 only (velocity >= 0.50 bps): 61% signal accuracy
- Entry on WINNER side only (avoid adverse selection on loser)
- Loser offset: -0.12 (very passive, $1.31 profit per passive hedge)
- Stop-loss: 7% drop triggers immediate hedge at loser ASK ($1.048 pair cost)
- Expected: +$1.17/hr (backtested on 180 markets)

Key Insight - Why Zone 5-6 + Stop-Loss is Profitable:
1. Higher velocity threshold = better accuracy (61% vs 51% for Zone 4-6)
2. Earlier stop-loss = cheaper hedge ($1.048 vs $1.091 at 15%)
3. More passive loser offset = higher per-trade profit ($1.31 vs $0.90)

Flow:
1. Detect zone 5-6 velocity signal (|vel| >= 0.50 bps)
2. Post ONLY on winner side (aggressive +0.01 offset)
3. Wait for entry fill
4. Monitor winner price - if drops 7%, trigger stop-loss hedge
5. Otherwise, passive hedge when loser_ask <= hedge_target

Features:
1. Real Binance WebSocket for velocity (100-200ms)
2. Real Polymarket WebSocket for orderbook (100-500ms)
3. Zone 5-6 filtering (skip low-accuracy zones)
4. 7% stop-loss mechanism (cheaper hedges)
5. Crash-safe CSV streaming with stop-loss tracking

Usage:
    python scripts/spread_capture_observer.py --hours 12
    python scripts/spread_capture_observer.py --continuous
    python scripts/spread_capture_observer.py --hours 8 --interval 100
"""

import asyncio
import csv
import os
import signal
import sys
import time
import json
import argparse
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from enum import Enum

import aiohttp

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.binance_client import BinanceClient
from src.api.websocket_client import WebSocketClient, BookUpdate
from src.services.market_finder import MarketFinder


# =============================================================================
# 6-ZONE VELOCITY CONFIGURATION (MATCHES LIVE TRADING)
# =============================================================================

# PROFITABLE STRATEGY: Only trade zones 5-6 (highest confidence signals)
# Zone 5-6 (vel >= 0.50) has 61% accuracy, +$1.17/hr (vs 51% for zones 4-6)
# Entry on winner side only, hedge when loser price drops to target OR stop-loss triggers
MIN_VELOCITY_BPS = 0.50  # zones 5-6 only (PROFITABLE CONFIG)

# Stop-loss configuration: 7% drop triggers immediate hedge
# Research finding: 7% stop-loss = cheaper hedge ($1.048 pair cost vs $1.091 at 15%)
STOP_LOSS_PCT = 0.07

# Minimum time remaining to enter new trade (seconds)
# Don't enter with <2min left - not enough time for hedge
MIN_TIME = 120

# HYBRID MM: Velocity-biased sizing + offset adjustment
# Higher velocity = more confidence = larger allocation to winner side
# winner_size_ratio: Fraction allocated to predicted winner (0.5 = symmetric, 0.8 = 80% winner)
# PROFITABLE ZONE CONFIGURATION (Zone 5-6 with -0.12 loser offset)
# Research finding: Zone 5-6 (vel >= 0.50) + 7% stop-loss + -0.12 loser = +$52.80 ($1.17/hr)
VELOCITY_ZONES = {
    'neutral':      {'vel_min': 0.00, 'vel_max': 0.05, 'pair_target': 0.97, 'winner_offset': -0.01, 'loser_offset': -0.01, 'winner_size_ratio': 0.50},
    'moderate':     {'vel_min': 0.05, 'vel_max': 0.10, 'pair_target': 0.97, 'winner_offset': -0.01, 'loser_offset': -0.02, 'winner_size_ratio': 0.55},
    'strong':       {'vel_min': 0.10, 'vel_max': 0.30, 'pair_target': 0.96, 'winner_offset':  0.00, 'loser_offset': -0.04, 'winner_size_ratio': 0.60},
    'very_strong':  {'vel_min': 0.30, 'vel_max': 0.50, 'pair_target': 0.95, 'winner_offset': +0.01, 'loser_offset': -0.08, 'winner_size_ratio': 0.70},
    'extreme':      {'vel_min': 0.50, 'vel_max': 1.00, 'pair_target': 0.94, 'winner_offset': +0.01, 'loser_offset': -0.12, 'winner_size_ratio': 0.75},  # PROFITABLE: -0.12
    'super_strong': {'vel_min': 1.00, 'vel_max': 99.0, 'pair_target': 0.93, 'winner_offset': +0.01, 'loser_offset': -0.12, 'winner_size_ratio': 0.80},  # PROFITABLE: -0.12
}

# Use single scenario matching live trading
SCENARIOS = {"live": None}  # Kept for compatibility with position tracking


# =============================================================================
# THEORETICAL POSITION TRACKER
# =============================================================================

@dataclass
class TheoreticalPosition:
    """Track theoretical position for a scenario."""
    up_shares: float = 0.0
    down_shares: float = 0.0
    up_cost: float = 0.0
    down_cost: float = 0.0

    @property
    def pairs(self) -> int:
        return int(min(self.up_shares, self.down_shares))

    @property
    def up_avg_price(self) -> float:
        return self.up_cost / self.up_shares if self.up_shares > 0 else 0.0

    @property
    def down_avg_price(self) -> float:
        return self.down_cost / self.down_shares if self.down_shares > 0 else 0.0

    @property
    def locked_profit(self) -> float:
        """Profit locked in by pairs (merge value - cost)."""
        if self.pairs == 0:
            return 0.0
        pair_cost = self.up_avg_price + self.down_avg_price
        return self.pairs * (1.00 - pair_cost)

    def add_fill(self, side: str, price: float, size: float):
        """Add a theoretical fill."""
        cost = price * size
        if side == "UP":
            self.up_shares += size
            self.up_cost += cost
        else:
            self.down_shares += size
            self.down_cost += cost

    def reset(self):
        """Reset for new market."""
        self.up_shares = 0.0
        self.down_shares = 0.0
        self.up_cost = 0.0
        self.down_cost = 0.0


@dataclass
class RestingOrders:
    """
    Track resting orders that haven't filled yet.

    Key insight: A hedge bid placed earlier should fill when ask drops to our price,
    even if velocity has since gone neutral. This tracks outstanding orders.
    """
    up_bid: Optional[float] = None
    down_bid: Optional[float] = None

    def reset(self):
        """Reset for new market."""
        self.up_bid = None
        self.down_bid = None


@dataclass
class CycleRecord:
    """Record of a completed entry→hedge→merge cycle."""
    cycle_num: int
    entry_side: str
    entry_price: float
    hedge_price: float
    hedge_type: str  # "passive" or "stoploss"
    pair_cost: float
    pnl: float
    time_remaining_at_entry: float
    time_remaining_at_merge: float


@dataclass
class EntryState:
    """
    Track entry fill and hedge target per scenario (matches live trading).

    Key insight: Hedge target can TIGHTEN but never loosen when velocity strengthens.
    Stop-loss: When winner drops 7% from fill, immediately hedge at loser ASK.
    CYCLING: After both sides fill, merge pair and reset for next cycle.
    """
    entry_filled: bool = False
    entry_side: Optional[str] = None
    entry_price: float = 0.0
    entry_velocity_dir: Optional[str] = None  # Track velocity direction at entry
    entry_zone: str = ""  # Zone at entry time
    initial_hedge_target: float = 0.0  # Target at entry (for logging)
    locked_hedge_target: float = 0.0  # Current (tightened) target
    hedge_filled: bool = False
    hedge_price: float = 0.0  # Price hedge filled at
    tighten_count: int = 0  # Number of times target was tightened
    entry_time_remaining: float = 0.0  # Time remaining when entry filled

    # Stop-loss tracking (7% = profitable config)
    stop_loss_triggered: bool = False  # Whether stop-loss was triggered
    stop_loss_hedge_price: float = 0.0  # Price we hedged at via stop-loss

    # Cycling tracking
    cycles_this_market: int = 0  # Number of complete cycles in current market
    cycles_total: int = 0  # Total cycles across all markets
    cycles_pnl: float = 0.0  # Cumulative PnL from cycles this market
    cycle_records: list = None  # List of CycleRecord for this market

    def __post_init__(self):
        if self.cycle_records is None:
            self.cycle_records = []

    def reset_for_cycle(self):
        """Reset for next cycle within same market (keeps cycle count)."""
        self.entry_filled = False
        self.entry_side = None
        self.entry_price = 0.0
        self.entry_velocity_dir = None
        self.entry_zone = ""
        self.initial_hedge_target = 0.0
        self.locked_hedge_target = 0.0
        self.hedge_filled = False
        self.hedge_price = 0.0
        self.tighten_count = 0
        self.entry_time_remaining = 0.0
        self.stop_loss_triggered = False
        self.stop_loss_hedge_price = 0.0

    def reset(self):
        """Reset for new market (keeps total cycle count)."""
        self.reset_for_cycle()
        # Reset market-specific cycling (keep total)
        self.cycles_this_market = 0
        self.cycles_pnl = 0.0
        self.cycle_records = []


# =============================================================================
# SIGNAL GENERATOR (6-ZONE - MATCHES LIVE TRADING)
# =============================================================================

def get_velocity_zone(velocity_bps: float) -> str:
    """Get velocity zone name based on absolute velocity (matches live trading)."""
    abs_vel = abs(velocity_bps)
    for zone_name, zone in VELOCITY_ZONES.items():
        if zone['vel_min'] <= abs_vel < zone['vel_max']:
            return zone_name
    return 'super_strong'


def get_zone_params(velocity_bps: float) -> Tuple[str, float, float, float, float]:
    """Get zone parameters for current velocity.

    Returns:
        (zone_name, pair_target, winner_offset, loser_offset, winner_size_ratio)
    """
    zone_name = get_velocity_zone(velocity_bps)
    zone = VELOCITY_ZONES[zone_name]
    return (zone_name, zone['pair_target'], zone['winner_offset'],
            zone['loser_offset'], zone.get('winner_size_ratio', 0.50))


def calculate_size_allocation(velocity_bps: float, total_size: float) -> Tuple[float, float]:
    """Calculate size allocation for UP and DOWN based on velocity (HYBRID MM).

    Higher velocity = more shares to predicted winner side.

    Args:
        velocity_bps: Current BTC velocity
        total_size: Total shares to allocate

    Returns:
        (up_size, down_size)
    """
    _, _, _, _, winner_ratio = get_zone_params(velocity_bps)

    winner_size = total_size * winner_ratio
    loser_size = total_size - winner_size

    # Ensure minimum size (5 shares)
    winner_size = max(5.0, winner_size)
    loser_size = max(5.0, loser_size)

    # Apply to correct side based on velocity direction
    if velocity_bps >= 0:
        # BTC rising → UP is winner
        return (winner_size, loser_size)
    else:
        # BTC falling → DOWN is winner
        return (loser_size, winner_size)


def maybe_tighten_hedge_target(entry_state: EntryState, velocity_bps: float) -> bool:
    """
    Check if hedge target should be tightened (matches live trading).

    ONLY TIGHTEN (lower), NEVER LOOSEN.
    Only tighten when velocity strengthens in SAME direction as entry.
    """
    if not entry_state.entry_filled or entry_state.hedge_filled:
        return False

    # Check velocity direction
    current_dir = "UP" if velocity_bps > 0 else "DOWN"
    if current_dir != entry_state.entry_velocity_dir:
        return False  # Velocity flipped, don't tighten

    # Calculate new target based on current zone
    zone_name, pair_target, _, loser_offset, _ = get_zone_params(velocity_bps)
    new_target = pair_target - entry_state.entry_price + loser_offset
    new_target = max(0.01, min(0.95, new_target))

    # ONLY TIGHTEN (lower), NEVER LOOSEN
    if new_target < entry_state.locked_hedge_target:
        entry_state.locked_hedge_target = new_target
        entry_state.tighten_count += 1
        return True
    return False


def check_stop_loss(entry_state: EntryState, winner_bid: float, loser_ask: float, loser_size: float, pos: TheoreticalPosition) -> bool:
    """
    Check if stop-loss should trigger (7% = profitable config).

    When winner drops X% from fill price, immediately hedge at loser ASK.

    Research finding: 7% stop-loss = cheaper hedge ($1.048 pair cost)
    vs 15% stop-loss = expensive hedge ($1.091 pair cost)

    Args:
        entry_state: Current entry state
        winner_bid: Current bid price of winner side
        loser_ask: Current ask price of loser side
        loser_size: Size to hedge (from velocity-biased allocation)
        pos: Position tracker to record fill

    Returns:
        True if stop-loss triggered and hedge filled
    """
    if not entry_state.entry_filled or entry_state.hedge_filled:
        return False

    if entry_state.stop_loss_triggered:
        return False  # Already triggered

    # Calculate drop percentage from fill price
    drop_pct = (entry_state.entry_price - winner_bid) / entry_state.entry_price

    if drop_pct >= STOP_LOSS_PCT:
        # Trigger stop-loss - hedge at loser ASK immediately
        entry_state.stop_loss_triggered = True
        entry_state.stop_loss_hedge_price = loser_ask
        entry_state.hedge_filled = True

        # Record the hedge fill
        loser_side = "DOWN" if entry_state.entry_side == "UP" else "UP"
        pos.add_fill(loser_side, loser_ask, loser_size)

        return True

    return False


def would_fill(our_bid: float, best_bid: float, best_ask: float) -> bool:
    """
    Determine if our bid would likely fill.
    - If our_bid >= best_ask: instant fill (lift the ask)
    - If our_bid >= best_bid + 0.005: very likely fill (top of book)
    """
    if our_bid >= best_ask:
        return True
    if our_bid >= best_bid + 0.005:
        return True
    return False


# =============================================================================
# MAIN OBSERVER
# =============================================================================

class SpreadCaptureObserver:
    """Real-time spread capture observation with WebSocket data."""

    def __init__(
        self,
        duration_hours: float = 12.0,
        continuous: bool = False,
        sample_interval_ms: int = 200,
        output_dir: str = "research/observer",
        trade_size: float = 15.0,  # ONE-SHOT mode: 15 shares at once (backtest optimal)
        starting_balance: float = 170.0,  # Starting balance in USD
    ):
        self.duration_hours = duration_hours
        self.continuous = continuous
        self.sample_interval_ms = sample_interval_ms
        self.output_dir = output_dir
        self.trade_size = trade_size
        self.starting_balance = starting_balance

        self.running = False
        self.start_time = None

        # WebSocket clients
        self.binance: Optional[BinanceClient] = None
        self.poly_ws: Optional[WebSocketClient] = None

        # Market tracking
        self.current_market = None
        self.up_token_id = None
        self.down_token_id = None
        self.market_end_time = None

        # Orderbook cache (updated by WebSocket)
        self.up_book: Optional[BookUpdate] = None
        self.down_book: Optional[BookUpdate] = None
        self._subscription_time = 0.0

        # Theoretical positions per scenario
        self.positions: Dict[str, TheoreticalPosition] = {
            name: TheoreticalPosition() for name in SCENARIOS.keys()
        }

        # Resting orders per scenario (tracks bids that haven't filled yet)
        self.resting_orders: Dict[str, RestingOrders] = {
            name: RestingOrders() for name in SCENARIOS.keys()
        }

        # Entry states per scenario (tracks fixed hedge targets)
        self.entry_states: Dict[str, EntryState] = {
            name: EntryState() for name in SCENARIOS.keys()
        }

        # CSV logging
        self.csv_file = None
        self.csv_writer = None
        self.current_date = None
        self.sample_count = 0

        # Create output directory
        os.makedirs(output_dir, exist_ok=True)

        # Signal handling
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        print("\n\nShutdown signal received...")
        self.running = False

    def _on_book_update(self, update: BookUpdate):
        """Handle Polymarket orderbook update."""
        # Only accept updates for current market's tokens
        if update.token_id == self.up_token_id:
            self.up_book = update
        elif update.token_id == self.down_token_id:
            self.down_book = update
        # Ignore updates for old tokens (after market rotation)

    def _is_garbage_orderbook(self, up_bid: float, up_ask: float, down_bid: float, down_ask: float) -> bool:
        """
        Detect garbage orderbook data.

        Only flag truly invalid data:
        - Zeros/empty orderbook
        - No price movement over many samples (track via _last_prices)

        NOTE: Static $0.49/$0.51 early in market is VALID.
        NOTE: Extreme prices ($0.01 or $0.99) near end are VALID.
        """
        # Check for empty/zero prices (definitely garbage)
        if up_bid <= 0 or down_bid <= 0 or up_ask <= 0 or down_ask <= 0:
            return True
        return False

    async def _fetch_clob_prices(self) -> Optional[Tuple[float, float, float, float]]:
        """Fetch prices from CLOB REST endpoint as fallback."""
        if not self.current_market:
            return None
        url = f"https://clob.polymarket.com/markets/{self.current_market.condition_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return self._extract_prices_from_clob(data)
        except Exception as e:
            logger.error(f"CLOB REST fetch failed: {e}")
        return None

    def _extract_prices_from_clob(self, clob_data: dict) -> Tuple[float, float, float, float]:
        """Extract UP/DOWN prices from CLOB response, create synthetic bid/ask."""
        up_price = down_price = 0.5
        for t in clob_data.get('tokens', []):
            outcome = t.get('outcome', '').upper()
            price = float(t.get('price', 0.5))
            if outcome in ('YES', 'UP'):
                up_price = price
            elif outcome in ('NO', 'DOWN'):
                down_price = price
        return (
            max(0.01, up_price - 0.005),
            min(0.99, up_price + 0.005),
            max(0.01, down_price - 0.005),
            min(0.99, down_price + 0.005),
        )

    async def _get_validated_orderbook(self) -> Optional[Tuple[float, float, float, float, str]]:
        """Get validated orderbook with garbage detection and REST fallback."""
        if self.up_book and self.down_book:
            up_bid = self.up_book.best_bid or 0.0
            up_ask = self.up_book.best_ask or 0.0
            down_bid = self.down_book.best_bid or 0.0
            down_ask = self.down_book.best_ask or 0.0
            if not self._is_garbage_orderbook(up_bid, up_ask, down_bid, down_ask):
                return (up_bid, up_ask, down_bid, down_ask, "websocket")
        # Fallback to REST
        prices = await self._fetch_clob_prices()
        if prices:
            return (*prices, "rest_fallback")
        return None

    def _init_csv(self):
        """Initialize CSV file for current date."""
        today = datetime.now().date()
        if self.csv_file and self.current_date == today:
            return

        if self.csv_file:
            self.csv_file.close()

        self.current_date = today
        date_str = today.strftime("%Y%m%d")
        filename = f"{self.output_dir}/spread_capture_obs_{date_str}.csv"

        file_exists = os.path.exists(filename)
        self.csv_file = open(filename, 'a', newline='')
        self.csv_writer = csv.writer(self.csv_file)

        if not file_exists:
            headers = [
                'timestamp_ms', 'market_slug', 'time_remaining_secs',
                'binance_price', 'velocity_bps',
                'up_bid', 'up_ask', 'down_bid', 'down_ask', 'pair_cost',
                'data_source',
                # Hybrid MM columns (6-zone with velocity-biased sizing)
                'velocity_zone', 'pair_target', 'winner_offset',
                'winner_size_ratio', 'up_size', 'down_size',  # Size allocation
                'entry_signal', 'entry_side', 'entry_bid',
                'initial_hedge_target', 'locked_hedge_target', 'tighten_count',
                'stop_loss_triggered', 'stop_loss_price',  # Stop-loss tracking
                'would_fill_entry', 'would_fill_hedge',
                'up_pos', 'down_pos', 'pairs', 'locked_profit',
                # Cycling columns
                'cycles_this_market', 'cycles_total', 'cycles_pnl',
                'cycle_just_completed',
            ]
            self.csv_writer.writerow(headers)
            self.csv_file.flush()

        print(f"Logging to: {filename}")

    async def _write_sample(self):
        """Write a sample row to CSV with validated orderbook."""
        if not self.csv_writer:
            return
        if not self.binance or self.binance.current_price <= 0:
            return

        # Get validated orderbook (with garbage detection and REST fallback)
        orderbook = await self._get_validated_orderbook()
        if not orderbook:
            return

        up_bid, up_ask, down_bid, down_ask, data_source = orderbook

        now = datetime.now(timezone.utc)
        timestamp_ms = int(now.timestamp() * 1000)
        pair_cost = up_ask + down_ask

        # Time remaining
        time_remaining = 0.0
        if self.market_end_time:
            time_remaining = max(0, (self.market_end_time - now).total_seconds())

        # Binance data
        velocity_bps = self.binance.calculate_velocity(window_seconds=10)

        row = [
            timestamp_ms,
            self.current_market.slug if self.current_market else "",
            round(time_remaining, 1),
            round(self.binance.current_price, 2),
            round(velocity_bps, 4),
            round(up_bid, 4),
            round(up_ask, 4),
            round(down_bid, 4),
            round(down_ask, 4),
            round(pair_cost, 4),
            data_source,
        ]

        # Generate signals using 6-zone system (matches live trading)
        pos = self.positions["live"]
        entry_state = self.entry_states["live"]

        # Get zone parameters for current velocity (HYBRID MM with size ratios)
        zone_name, pair_target, winner_offset, loser_offset, winner_size_ratio = get_zone_params(velocity_bps)

        # Calculate velocity-biased size allocation (HYBRID MM)
        up_size, down_size = calculate_size_allocation(velocity_bps, self.trade_size)

        # Determine entry side based on velocity direction
        # ZONE 4-6 FILTER: Only enter when |velocity| >= 0.30 BPS (90% accuracy)
        # Zones 1-3 (velocity < 0.30) are skipped - lower accuracy, more noise
        if velocity_bps >= MIN_VELOCITY_BPS:
            entry_side = "UP"  # BTC rising fast, bet UP
        elif velocity_bps <= -MIN_VELOCITY_BPS:
            entry_side = "DOWN"  # BTC falling fast, bet DOWN
        else:
            entry_side = "NONE"  # Zones 1-3, no entry (skip low-velocity noise)

        # Entry signal: directional velocity signal
        entry_signal = entry_side != "NONE"

        # Calculate entry bid using CORRECT formula: best_bid + winner_offset
        # (Matches live trading logic in spread_capture.py)
        if entry_side == "UP":
            entry_bid = up_bid + winner_offset
            entry_bid = min(entry_bid, up_ask - 0.001)  # Stay below ask (maker)
            entry_bid = max(0.01, min(0.95, entry_bid))
        elif entry_side == "DOWN":
            entry_bid = down_bid + winner_offset
            entry_bid = min(entry_bid, down_ask - 0.001)
            entry_bid = max(0.01, min(0.95, entry_bid))
        else:
            entry_bid = 0.0

        # Fill tracking
        up_filled = False
        down_filled = False
        entry_would_fill = False
        hedge_would_fill = False

        # ENTRY LOGIC: Fill with VELOCITY-BIASED sizing
        # HYBRID MM: Always post both sides, size biased toward predicted winner
        if not entry_state.entry_filled and entry_side != "NONE" and time_remaining >= MIN_TIME:
            if entry_side == "UP":
                entry_would_fill = would_fill(entry_bid, up_bid, up_ask)
                if entry_would_fill:
                    entry_state.entry_filled = True
                    entry_state.entry_side = "UP"
                    entry_state.entry_price = up_ask  # Fill at ask
                    entry_state.entry_velocity_dir = "UP"
                    entry_state.entry_zone = zone_name
                    entry_state.entry_time_remaining = time_remaining  # Track for cycling
                    # Hedge target = pair_target - entry_price + loser_offset
                    entry_state.initial_hedge_target = pair_target - entry_state.entry_price + loser_offset
                    entry_state.initial_hedge_target = max(0.01, min(0.95, entry_state.initial_hedge_target))
                    entry_state.locked_hedge_target = entry_state.initial_hedge_target
                    # HYBRID: Use velocity-biased size (up_size is winner size when vel > 0)
                    pos.add_fill("UP", entry_state.entry_price, up_size)
                    up_filled = True
            elif entry_side == "DOWN":
                entry_would_fill = would_fill(entry_bid, down_bid, down_ask)
                if entry_would_fill:
                    entry_state.entry_filled = True
                    entry_state.entry_side = "DOWN"
                    entry_state.entry_price = down_ask
                    entry_state.entry_velocity_dir = "DOWN"
                    entry_state.entry_zone = zone_name
                    entry_state.entry_time_remaining = time_remaining  # Track for cycling
                    # Hedge target = pair_target - entry_price + loser_offset
                    entry_state.initial_hedge_target = pair_target - entry_state.entry_price + loser_offset
                    entry_state.initial_hedge_target = max(0.01, min(0.95, entry_state.initial_hedge_target))
                    entry_state.locked_hedge_target = entry_state.initial_hedge_target
                    # HYBRID: Use velocity-biased size (down_size is winner size when vel < 0)
                    pos.add_fill("DOWN", entry_state.entry_price, down_size)
                    down_filled = True

        # HEDGE TIGHTENING: Check if velocity strengthened (matches live trading)
        if entry_state.entry_filled and not entry_state.hedge_filled:
            maybe_tighten_hedge_target(entry_state, velocity_bps)

        # STOP-LOSS CHECK: If winner dropped 7%, immediately hedge at loser ASK
        # This takes priority over normal passive hedge fill
        if entry_state.entry_filled and not entry_state.hedge_filled:
            if entry_state.entry_side == "UP":
                winner_bid = up_bid
                loser_ask = down_ask
                loser_size = down_size
            else:
                winner_bid = down_bid
                loser_ask = up_ask
                loser_size = up_size

            stop_loss_triggered = check_stop_loss(entry_state, winner_bid, loser_ask, loser_size, pos)
            if stop_loss_triggered:
                if entry_state.entry_side == "UP":
                    down_filled = True
                else:
                    up_filled = True
                hedge_would_fill = True

        # HEDGE FILL: Check against locked (possibly tightened) target
        # HYBRID MM: Hedge side gets loser_size (smaller allocation)
        # NOTE: Only check if not already filled by stop-loss
        if entry_state.entry_filled and not entry_state.hedge_filled:
            if entry_state.entry_side == "UP":
                # Hedge is DOWN side - check if DOWN ask <= locked target
                hedge_would_fill = down_ask <= entry_state.locked_hedge_target
                if hedge_would_fill:
                    entry_state.hedge_filled = True
                    entry_state.hedge_price = down_ask  # Track for cycling
                    # HYBRID: Use loser size for hedge (down_size when vel > 0)
                    pos.add_fill("DOWN", down_ask, down_size)
                    down_filled = True
            else:
                # Hedge is UP side - check if UP ask <= locked target
                hedge_would_fill = up_ask <= entry_state.locked_hedge_target
                if hedge_would_fill:
                    entry_state.hedge_filled = True
                    entry_state.hedge_price = up_ask  # Track for cycling
                    # HYBRID: Use loser size for hedge (up_size when vel < 0)
                    pos.add_fill("UP", up_ask, up_size)
                    up_filled = True

        # CYCLING: If both entry and hedge filled, complete cycle and reset
        cycle_just_completed = False
        if entry_state.entry_filled and entry_state.hedge_filled:
            # Calculate PnL for this cycle
            hedge_price = entry_state.hedge_price
            if entry_state.stop_loss_triggered:
                hedge_price = entry_state.stop_loss_hedge_price
                hedge_type = "stoploss"
            else:
                hedge_type = "passive"

            pair_cost_cycle = entry_state.entry_price + hedge_price
            cycle_pnl = (1.0 - pair_cost_cycle) * self.trade_size

            # Record the cycle
            entry_state.cycles_this_market += 1
            entry_state.cycles_total += 1
            entry_state.cycles_pnl += cycle_pnl

            cycle_record = CycleRecord(
                cycle_num=entry_state.cycles_this_market,
                entry_side=entry_state.entry_side,
                entry_price=entry_state.entry_price,
                hedge_price=hedge_price,
                hedge_type=hedge_type,
                pair_cost=pair_cost_cycle,
                pnl=cycle_pnl,
                time_remaining_at_entry=entry_state.entry_time_remaining,
                time_remaining_at_merge=time_remaining,
            )
            entry_state.cycle_records.append(cycle_record)

            # Reset position for next cycle
            pos.reset()

            # Reset entry state for next cycle (keep cycle counts)
            entry_state.reset_for_cycle()
            cycle_just_completed = True

        # Update fill flags for logging
        if entry_state.entry_side == "UP":
            entry_would_fill = up_filled
            hedge_would_fill = down_filled
        elif entry_state.entry_side == "DOWN":
            entry_would_fill = down_filled
            hedge_would_fill = up_filled

        row.extend([
            zone_name,
            round(pair_target, 2),
            round(winner_offset, 2),
            round(winner_size_ratio, 2),  # Size ratio
            round(up_size, 1),  # Biased up size
            round(down_size, 1),  # Biased down size
            entry_signal,
            entry_side,
            round(entry_bid, 4),
            round(entry_state.initial_hedge_target, 4) if entry_state.entry_filled else 0.0,
            round(entry_state.locked_hedge_target, 4) if entry_state.entry_filled else 0.0,
            entry_state.tighten_count,
            entry_state.stop_loss_triggered,  # Stop-loss tracking
            round(entry_state.stop_loss_hedge_price, 4) if entry_state.stop_loss_triggered else 0.0,
            entry_would_fill,
            hedge_would_fill,
            round(pos.up_shares, 1),
            round(pos.down_shares, 1),
            pos.pairs,
            round(pos.locked_profit, 4),
            # Cycling columns
            entry_state.cycles_this_market,
            entry_state.cycles_total,
            round(entry_state.cycles_pnl, 4),
            cycle_just_completed,
        ])

        self.csv_writer.writerow(row)
        self.sample_count += 1

        # Flush periodically
        if self.sample_count % 50 == 0:
            self.csv_file.flush()

    async def _find_current_market(self) -> bool:
        """Find and subscribe to current 15-min BTC market using REST API."""
        finder = MarketFinder()
        # Use slug-based method - find_btc_15min_markets() doesn't work for btc-updown
        markets = await finder.get_current_and_upcoming_markets(count=3)

        if not markets:
            print("No active BTC 15-min markets found")
            return False

        # Pick market ending SOONEST with >60s remaining
        now = datetime.now(timezone.utc)
        valid = [(m, (m.end_time - now).total_seconds()) for m in markets if m.end_time and (m.end_time - now).total_seconds() > 60]
        if not valid:
            print("No markets with >60s remaining")
            return False
        valid.sort(key=lambda x: x[1])
        market, remaining = valid[0]
        print(f"Selected market with {remaining:.0f}s remaining (of {len(markets)} found)")

        # Check if we need to switch
        if self.current_market and self.current_market.condition_id == market.condition_id:
            return True

        print(f"\nSwitching to market: {market.slug}", flush=True)
        print(f"  UP token: {market.up_token_id[:16]}...", flush=True)
        print(f"  DOWN token: {market.down_token_id[:16]}...", flush=True)

        # Reset positions, resting orders, and entry states for new market
        for pos in self.positions.values():
            pos.reset()
        for resting in self.resting_orders.values():
            resting.reset()
        for entry_state in self.entry_states.values():
            entry_state.reset()

        # Update state to new market
        self.up_token_id = market.up_token_id
        self.down_token_id = market.down_token_id
        self.current_market = market
        self.market_end_time = market.end_time
        self.up_book = None
        self.down_book = None

        # CRITICAL: Polymarket WebSocket doesn't properly switch subscriptions
        # Must reconnect to get fresh subscription for new tokens
        if self.poly_ws:
            print("  Reconnecting WebSocket for new market...", flush=True)
            await self.poly_ws.disconnect()
            await asyncio.sleep(0.5)
            await self.poly_ws.connect()
            # Restart the WebSocket event loop
            if hasattr(self, '_ws_task') and self._ws_task:
                self._ws_task.cancel()
                try:
                    await self._ws_task
                except asyncio.CancelledError:
                    pass
            self._ws_task = asyncio.create_task(self.poly_ws.run())
            # Subscribe to new market tokens
            result = await self.poly_ws.subscribe([self.up_token_id, self.down_token_id])
            print(f"  Subscribe result: {result}", flush=True)

        # Wait for first orderbook data
        print("  Waiting for orderbook data...", flush=True)
        for i in range(30):  # 3 seconds max
            if self.up_book and self.down_book:
                print(f"  Got orderbook after {i * 100}ms", flush=True)
                break
            await asyncio.sleep(0.1)
        else:
            print("  No WebSocket data after 3s, using REST fallback", flush=True)

        return True

    async def _print_status(self):
        """Print current status."""
        elapsed = (datetime.now() - self.start_time).total_seconds() / 3600

        print(f"\n{'='*70}")
        print(f"SPREAD CAPTURE OBSERVER - {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'='*70}")
        print(f"Running: {elapsed:.2f} hours | Samples: {self.sample_count}")

        if self.current_market:
            time_left = 0
            if self.market_end_time:
                time_left = max(0, (self.market_end_time - datetime.now(timezone.utc)).total_seconds())
            print(f"Market: {self.current_market.slug} ({time_left:.0f}s remaining)")

        if self.binance and self.binance.current_price > 0:
            velocity = self.binance.calculate_velocity(window_seconds=10)
            print(f"Binance: ${self.binance.current_price:.2f} | Velocity: {velocity:.4f} bps/s")

        if self.up_book and self.down_book:
            pair_cost = (self.up_book.best_ask or 0) + (self.down_book.best_ask or 0)
            print(f"Pair Cost: ${pair_cost:.4f}")

        # Cycling stats
        entry_state = self.entry_states.get("live")
        if entry_state:
            print(f"\nCYCLING:")
            print(f"  This market: {entry_state.cycles_this_market} cycles, ${entry_state.cycles_pnl:.2f}")
            print(f"  Total: {entry_state.cycles_total} cycles")

        print(f"\nTheoretical Positions:")
        for name, pos in self.positions.items():
            print(f"  {name:12}: UP={pos.up_shares:.0f} DOWN={pos.down_shares:.0f} "
                  f"Pairs={pos.pairs} Locked=${pos.locked_profit:.2f}")

    async def run(self):
        """Main observation loop."""
        self.running = True
        self.start_time = datetime.now()

        print("=" * 70)
        print("SPREAD CAPTURE OBSERVER - ZONE 5-6 PROFITABLE MODE")
        print("=" * 70)
        print(f"Duration: {'continuous' if self.continuous else f'{self.duration_hours}h'}")
        print(f"Sample interval: {self.sample_interval_ms}ms")
        print(f"Output: {self.output_dir}/")
        print()
        print("PROFITABLE STRATEGY CONFIG (Zone 5-6 + 7% Stop-Loss + CYCLING):")
        print(f"  Starting bal:  ${self.starting_balance:.2f}")
        print(f"  Trade size:    {self.trade_size} shares (winner-only entry)")
        print(f"  Zone filter:   >= {MIN_VELOCITY_BPS} BPS (zones 5-6 only, 61% accuracy)")
        print(f"  Stop-loss:     {STOP_LOSS_PCT*100:.0f}% drop triggers hedge ($1.048 pair cost)")
        print(f"  Loser offset:  -0.12 (passive, $1.31/trade profit)")
        print(f"  Min time:      {MIN_TIME}s (no entry with <2min remaining)")
        print(f"  Mode:          DIRECTIONAL + STOP-LOSS + CYCLING")
        print(f"  Cycling:       ENABLED (merge pair → reset → re-enter)")
        print(f"  Expected:      +$3.70/hr (cycling backtest on 218 markets, 7.96 cycles/market)")
        print()
        print("Zone Configuration (only zones 5-6 trigger entries):")
        for zone_name, zone in VELOCITY_ZONES.items():
            active = "✓" if zone['vel_min'] >= MIN_VELOCITY_BPS else " "
            print(f"  [{active}] {zone_name:12}: vel={zone['vel_min']:.2f}-{zone['vel_max']:.2f}, "
                  f"pair_target={zone['pair_target']:.2f}, loser_offset={zone['loser_offset']:.2f}")
        print()

        # Initialize Binance WebSocket
        print("Connecting to Binance WebSocket...")
        self.binance = BinanceClient(window_seconds=60)
        await self.binance.connect()

        # Wait for first price
        for _ in range(50):
            if self.binance.current_price > 0:
                break
            await asyncio.sleep(0.1)
        print(f"  Binance connected: ${self.binance.current_price:.2f}")

        # Initialize Polymarket WebSocket
        print("Connecting to Polymarket WebSocket...")
        self.poly_ws = WebSocketClient(auto_reconnect=True)
        self.poly_ws.on_book_update(self._on_book_update)
        await self.poly_ws.connect()

        # Start WebSocket event loop in background
        self._ws_task = asyncio.create_task(self.poly_ws.run())
        print("  Polymarket WebSocket connected")

        # Find initial market
        if not await self._find_current_market():
            print("Failed to find market. Exiting.")
            return

        # Wait for orderbook data
        print("Waiting for orderbook data...")
        for _ in range(50):
            if self.up_book and self.down_book:
                break
            await asyncio.sleep(0.1)

        if not self.up_book or not self.down_book:
            print("Failed to get orderbook data. Exiting.")
            return

        print("Ready! Starting observation...\n")

        # Initialize CSV
        self._init_csv()

        # Save 6-zone params (matches live trading)
        params_file = f"{self.output_dir}/spread_capture_params.json"
        with open(params_file, 'w') as f:
            json.dump(VELOCITY_ZONES, f, indent=2)

        last_status_time = time.time()
        last_market_check = time.time()
        end_time = None
        if not self.continuous:
            end_time = self.start_time + timedelta(hours=self.duration_hours)

        try:
            while self.running:
                now = datetime.now()

                # Check duration
                if end_time and now >= end_time:
                    print("\nDuration reached.")
                    break

                # Periodic market check (every 30s)
                if time.time() - last_market_check > 30:
                    await self._find_current_market()
                    last_market_check = time.time()

                # Write sample
                self._init_csv()  # Handle date rollover
                await self._write_sample()

                # Status update (every 60s)
                if time.time() - last_status_time > 60:
                    await self._print_status()
                    last_status_time = time.time()

                await asyncio.sleep(self.sample_interval_ms / 1000)

        except Exception as e:
            print(f"\nError: {e}")
            import traceback
            traceback.print_exc()

        finally:
            # Cleanup
            if hasattr(self, '_ws_task') and self._ws_task:
                self._ws_task.cancel()
            if self.csv_file:
                self.csv_file.flush()
                self.csv_file.close()

            await self._print_status()
            print(f"\nObservation complete. {self.sample_count} samples recorded.")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Spread Capture Observer - Real WebSocket Data Collection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--hours', type=float, default=12.0, help='Duration in hours')
    parser.add_argument('--continuous', action='store_true', help='Run until stopped')
    parser.add_argument('--interval', type=int, default=200, help='Sample interval in ms')
    parser.add_argument('--output', type=str, default='research/observer', help='Output directory')
    parser.add_argument('--balance', type=float, default=170.0, help='Starting balance in USD')
    parser.add_argument('--shares', type=float, default=15.0, help='Target shares per trade (ONE-SHOT)')

    args = parser.parse_args()

    observer = SpreadCaptureObserver(
        duration_hours=args.hours,
        continuous=args.continuous,
        sample_interval_ms=args.interval,
        output_dir=args.output,
        trade_size=args.shares,
        starting_balance=args.balance,
    )

    asyncio.run(observer.run())


if __name__ == "__main__":
    main()
