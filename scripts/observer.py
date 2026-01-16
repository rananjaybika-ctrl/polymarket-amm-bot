#!/usr/bin/env python3
"""
Spread Capture Observer - PASSIVE GRID MM Strategy

Captures REAL market data at sub-second speeds for strategy analysis.
Simulates PASSIVE GRID MM strategy (proven $95/hr in backtest).

PASSIVE GRID MM CONFIG (All Zones, No Stop-Loss):
- All velocity zones active (no filtering)
- Entry on BOTH sides simultaneously (symmetric grid)
- Passive offsets: bid = best_bid - offset (always below market)
- No stop-loss (pure passive market making)
- Fill detection: bid drops to our posted price
- Expected: +$95/hr (backtested)

Key Insight - Why Passive Grid Works:
1. Symmetric entry = capture spread on both sides
2. Deeper loser offset in high velocity = protect against adverse selection
3. No stop-loss = let positions naturally fill via market movement
4. Simple cycling: fill both sides → pair complete → reset → repeat

Flow:
1. Post bids on BOTH UP and DOWN simultaneously at best_bid - offset
2. Track posted prices until filled
3. Fill when next_bid <= posted_price (market drops to us)
4. When both sides fill, merge pair, lock profit, reset for next cycle

Features:
1. Real Binance WebSocket for velocity (100-200ms)
2. Real Polymarket WebSocket for orderbook (100-500ms)
3. All zones active (no velocity filtering)
4. Passive grid offsets (symmetric in low velocity, asymmetric in high)
5. Crash-safe CSV streaming with grid tracking

Usage:
    python scripts/observer.py --hours 12
    python scripts/observer.py --continuous
    python scripts/observer.py --hours 8 --interval 100
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
# PASSIVE GRID MM CONFIGURATION (PROVEN $95/hr)
# =============================================================================

# PASSIVE GRID: Trade ALL zones (no velocity filtering)
# Formula: our_bid = best_bid - offset (positive offset = passive)
MIN_VELOCITY_BPS = 0.0  # Trade all zones (passive grid)

# Stop-loss: DISABLED for passive grid strategy
# Let positions fill naturally via market movement
STOP_LOSS_PCT = 0.0  # Disabled

# Minimum time remaining to enter new trade (seconds)
# Stop posting at 60s remaining
MIN_TIME = 60

# PASSIVE GRID MM ZONES (proven $95/hr)
# Formula: our_bid = best_bid - offset (positive = passive, below market)
# Symmetric in low velocity, asymmetric loser offset in high velocity
VELOCITY_ZONES = {
    'neutral':     {'vel_min': 0.00, 'vel_max': 0.10, 'winner_offset': 0.01, 'loser_offset': 0.01},
    'moderate':    {'vel_min': 0.10, 'vel_max': 0.30, 'winner_offset': 0.01, 'loser_offset': 0.01},
    'strong':      {'vel_min': 0.30, 'vel_max': 0.50, 'winner_offset': 0.01, 'loser_offset': 0.03},
    'very_strong': {'vel_min': 0.50, 'vel_max': 99.0, 'winner_offset': 0.01, 'loser_offset': 0.05},
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
    """Record of a completed grid pair cycle."""
    cycle_num: int
    up_fill_price: float
    down_fill_price: float
    pair_cost: float
    pnl: float
    time_remaining_at_post: float
    time_remaining_at_complete: float


@dataclass
class GridState:
    """
    Track passive grid state - both sides posting simultaneously.

    Passive Grid Flow:
    1. Post bids on BOTH UP and DOWN at best_bid - offset
    2. Track posted prices until filled
    3. Fill when market bid drops to our posted price
    4. When both sides fill, complete pair and reset
    """
    # Grid posted prices (our bids)
    up_posted_bid: float = 0.0
    down_posted_bid: float = 0.0

    # Fill tracking
    up_filled: bool = False
    down_filled: bool = False
    up_fill_price: float = 0.0
    down_fill_price: float = 0.0

    # Offsets used for this grid
    up_offset: float = 0.0
    down_offset: float = 0.0

    # Time tracking
    post_time_remaining: float = 0.0

    # Zone at post time
    post_zone: str = ""

    # Cycling tracking
    cycles_this_market: int = 0
    cycles_total: int = 0
    cycles_pnl: float = 0.0
    cycle_records: list = None

    def __post_init__(self):
        if self.cycle_records is None:
            self.cycle_records = []

    def is_posted(self) -> bool:
        """Check if grid is currently posted (waiting for fills)."""
        return self.up_posted_bid > 0 or self.down_posted_bid > 0

    def is_complete(self) -> bool:
        """Check if both sides have filled."""
        return self.up_filled and self.down_filled

    def reset_for_cycle(self):
        """Reset for next cycle within same market (keeps cycle count)."""
        self.up_posted_bid = 0.0
        self.down_posted_bid = 0.0
        self.up_filled = False
        self.down_filled = False
        self.up_fill_price = 0.0
        self.down_fill_price = 0.0
        self.up_offset = 0.0
        self.down_offset = 0.0
        self.post_time_remaining = 0.0
        self.post_zone = ""

    def reset(self):
        """Reset for new market (keeps total cycle count)."""
        self.reset_for_cycle()
        self.cycles_this_market = 0
        self.cycles_pnl = 0.0
        self.cycle_records = []


# Alias for compatibility
EntryState = GridState


# =============================================================================
# SIGNAL GENERATOR (PASSIVE GRID - 4 ZONES)
# =============================================================================

def get_velocity_zone(velocity_bps: float) -> str:
    """Get velocity zone name based on absolute velocity (passive grid)."""
    abs_vel = abs(velocity_bps)
    for zone_name, zone in VELOCITY_ZONES.items():
        if zone['vel_min'] <= abs_vel < zone['vel_max']:
            return zone_name
    return 'very_strong'  # Highest zone for passive grid


def get_zone_params(velocity_bps: float) -> Tuple[str, float, float]:
    """Get zone parameters for current velocity (passive grid).

    Returns:
        (zone_name, winner_offset, loser_offset)
    """
    zone_name = get_velocity_zone(velocity_bps)
    zone = VELOCITY_ZONES[zone_name]
    return (zone_name, zone['winner_offset'], zone['loser_offset'])


def calculate_size_allocation(velocity_bps: float, total_size: float) -> Tuple[float, float]:
    """Calculate size allocation for UP and DOWN (passive grid = symmetric).

    Passive grid uses equal sizing on both sides.

    Args:
        velocity_bps: Current BTC velocity (used to determine winner/loser)
        total_size: Shares per side

    Returns:
        (up_size, down_size) - both equal for passive grid
    """
    # Passive grid: symmetric sizing on both sides
    return (total_size, total_size)


def maybe_tighten_hedge_target(entry_state: EntryState, velocity_bps: float) -> bool:
    """
    Passive grid: No hedge tightening.

    In passive grid mode, we track posted prices until filled.
    No dynamic target adjustment.
    """
    # Passive grid doesn't tighten - orders stay at posted price
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
        trade_size: float = 5.0,  # Passive grid: 5 shares per side
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
        filename = f"{self.output_dir}/grid_obs_{date_str}.csv"

        file_exists = os.path.exists(filename)
        self.csv_file = open(filename, 'a', newline='')
        self.csv_writer = csv.writer(self.csv_file)

        if not file_exists:
            headers = [
                'timestamp_ms', 'market_slug', 'time_remaining_secs',
                'binance_price', 'velocity_bps',
                'up_bid', 'up_ask', 'down_bid', 'down_ask', 'pair_cost',
                'data_source',
                # Passive grid columns
                'velocity_zone', 'winner_offset', 'loser_offset',
                'grid_up_offset', 'grid_down_offset',  # Offsets used
                'grid_up_bid', 'grid_down_bid',        # Posted bid prices
                'grid_up_filled', 'grid_down_filled',  # Fill flags
                'grid_pair_cost',                       # UP fill + DOWN fill
                'grid_profit',                          # 1.00 - pair_cost per share
                'up_pos', 'down_pos', 'pairs', 'locked_profit',
                # Cycling columns
                'cycles_this_market', 'cycles_total', 'cycles_pnl',
                'cycle_just_completed',
            ]
            self.csv_writer.writerow(headers)
            self.csv_file.flush()

        print(f"Logging to: {filename}")

    async def _write_sample(self):
        """Write a sample row to CSV with passive grid logic."""
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

        # PASSIVE GRID LOGIC
        pos = self.positions["live"]
        grid_state = self.entry_states["live"]

        # Get zone parameters for current velocity (passive grid)
        zone_name, winner_offset, loser_offset = get_zone_params(velocity_bps)

        # Determine winner/loser sides based on velocity
        # BTC rising = UP is winner, DOWN is loser
        # BTC falling = DOWN is winner, UP is loser
        if velocity_bps >= 0:
            up_offset = winner_offset
            down_offset = loser_offset
        else:
            up_offset = loser_offset
            down_offset = winner_offset

        # Calculate passive grid bid prices: best_bid - offset (below market)
        grid_up_bid = up_bid - up_offset
        grid_down_bid = down_bid - down_offset
        grid_up_bid = max(0.01, min(0.95, grid_up_bid))
        grid_down_bid = max(0.01, min(0.95, grid_down_bid))

        cycle_just_completed = False

        # POSTING: Post on BOTH sides if not already posted and time remaining
        if not grid_state.is_posted() and time_remaining >= MIN_TIME:
            grid_state.up_posted_bid = grid_up_bid
            grid_state.down_posted_bid = grid_down_bid
            grid_state.up_offset = up_offset
            grid_state.down_offset = down_offset
            grid_state.post_time_remaining = time_remaining
            grid_state.post_zone = zone_name

        # FILL DETECTION: Check if market bid dropped to our posted price
        # Fill when: current best_bid <= our posted bid (market came to us)
        if grid_state.is_posted():
            # Check UP side fill
            if not grid_state.up_filled and up_bid <= grid_state.up_posted_bid:
                grid_state.up_filled = True
                grid_state.up_fill_price = grid_state.up_posted_bid
                pos.add_fill("UP", grid_state.up_fill_price, self.trade_size)

            # Check DOWN side fill
            if not grid_state.down_filled and down_bid <= grid_state.down_posted_bid:
                grid_state.down_filled = True
                grid_state.down_fill_price = grid_state.down_posted_bid
                pos.add_fill("DOWN", grid_state.down_fill_price, self.trade_size)

        # CYCLING: If both sides filled, complete pair and reset
        if grid_state.is_complete():
            # Calculate PnL for this cycle
            grid_pair_cost = grid_state.up_fill_price + grid_state.down_fill_price
            cycle_pnl = (1.0 - grid_pair_cost) * self.trade_size

            # Record the cycle
            grid_state.cycles_this_market += 1
            grid_state.cycles_total += 1
            grid_state.cycles_pnl += cycle_pnl

            cycle_record = CycleRecord(
                cycle_num=grid_state.cycles_this_market,
                up_fill_price=grid_state.up_fill_price,
                down_fill_price=grid_state.down_fill_price,
                pair_cost=grid_pair_cost,
                pnl=cycle_pnl,
                time_remaining_at_post=grid_state.post_time_remaining,
                time_remaining_at_complete=time_remaining,
            )
            grid_state.cycle_records.append(cycle_record)

            # Reset position for next cycle
            pos.reset()

            # Reset grid state for next cycle (keep cycle counts)
            grid_state.reset_for_cycle()
            cycle_just_completed = True

        # Calculate grid profit (for current posted orders)
        grid_pair_cost_current = 0.0
        grid_profit_current = 0.0
        if grid_state.up_filled and grid_state.down_filled:
            grid_pair_cost_current = grid_state.up_fill_price + grid_state.down_fill_price
            grid_profit_current = 1.0 - grid_pair_cost_current
        elif grid_state.is_posted():
            # Estimate based on posted prices
            grid_pair_cost_current = grid_state.up_posted_bid + grid_state.down_posted_bid
            grid_profit_current = 1.0 - grid_pair_cost_current

        row.extend([
            zone_name,
            round(winner_offset, 4),
            round(loser_offset, 4),
            round(grid_state.up_offset, 4) if grid_state.is_posted() else round(up_offset, 4),
            round(grid_state.down_offset, 4) if grid_state.is_posted() else round(down_offset, 4),
            round(grid_state.up_posted_bid, 4) if grid_state.is_posted() else round(grid_up_bid, 4),
            round(grid_state.down_posted_bid, 4) if grid_state.is_posted() else round(grid_down_bid, 4),
            grid_state.up_filled,
            grid_state.down_filled,
            round(grid_pair_cost_current, 4),
            round(grid_profit_current, 4),
            round(pos.up_shares, 1),
            round(pos.down_shares, 1),
            pos.pairs,
            round(pos.locked_profit, 4),
            # Cycling columns
            grid_state.cycles_this_market,
            grid_state.cycles_total,
            round(grid_state.cycles_pnl, 4),
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
        print(f"PASSIVE GRID OBSERVER - {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'='*70}")
        print(f"Running: {elapsed:.2f} hours | Samples: {self.sample_count}")

        if self.current_market:
            time_left = 0
            if self.market_end_time:
                time_left = max(0, (self.market_end_time - datetime.now(timezone.utc)).total_seconds())
            print(f"Market: {self.current_market.slug} ({time_left:.0f}s remaining)")

        if self.binance and self.binance.current_price > 0:
            velocity = self.binance.calculate_velocity(window_seconds=10)
            zone_name = get_velocity_zone(velocity)
            print(f"Binance: ${self.binance.current_price:.2f} | Velocity: {velocity:.4f} bps/s | Zone: {zone_name}")

        if self.up_book and self.down_book:
            pair_cost = (self.up_book.best_ask or 0) + (self.down_book.best_ask or 0)
            print(f"Pair Cost (ask+ask): ${pair_cost:.4f}")

        # Grid state
        grid_state = self.entry_states.get("live")
        if grid_state:
            print(f"\nGRID STATE:")
            if grid_state.is_posted():
                print(f"  UP bid: ${grid_state.up_posted_bid:.4f} {'[FILLED]' if grid_state.up_filled else '[waiting]'}")
                print(f"  DOWN bid: ${grid_state.down_posted_bid:.4f} {'[FILLED]' if grid_state.down_filled else '[waiting]'}")
            else:
                print(f"  Not posted yet")
            print(f"\nCYCLING:")
            print(f"  This market: {grid_state.cycles_this_market} cycles, ${grid_state.cycles_pnl:.2f}")
            print(f"  Total: {grid_state.cycles_total} cycles")

        print(f"\nTheoretical Positions:")
        for name, pos in self.positions.items():
            print(f"  {name:12}: UP={pos.up_shares:.0f} DOWN={pos.down_shares:.0f} "
                  f"Pairs={pos.pairs} Locked=${pos.locked_profit:.2f}")

    async def run(self):
        """Main observation loop."""
        self.running = True
        self.start_time = datetime.now()

        print("=" * 70)
        print("PASSIVE GRID MM OBSERVER - PROVEN $95/hr STRATEGY")
        print("=" * 70)
        print(f"Duration: {'continuous' if self.continuous else f'{self.duration_hours}h'}")
        print(f"Sample interval: {self.sample_interval_ms}ms")
        print(f"Output: {self.output_dir}/")
        print()
        print("PASSIVE GRID CONFIG (All Zones, No Stop-Loss):")
        print(f"  Starting bal:  ${self.starting_balance:.2f}")
        print(f"  Trade size:    {self.trade_size} shares per side")
        print(f"  Zone filter:   ALL zones active (passive grid)")
        print(f"  Stop-loss:     DISABLED (pure passive)")
        print(f"  Min time:      {MIN_TIME}s (stop posting)")
        print(f"  Mode:          PASSIVE GRID (both sides simultaneous)")
        print(f"  Formula:       our_bid = best_bid - offset")
        print(f"  Expected:      +$95/hr (backtest)")
        print()
        print("Zone Configuration (all zones active):")
        for zone_name, zone in VELOCITY_ZONES.items():
            print(f"  [*] {zone_name:12}: vel={zone['vel_min']:.2f}-{zone['vel_max']:.2f}, "
                  f"winner={zone['winner_offset']:.2f}, loser={zone['loser_offset']:.2f}")
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

        # Save passive grid params
        params_file = f"{self.output_dir}/observer_params.json"
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
        description="Passive Grid Observer - Real WebSocket Data Collection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--hours', type=float, default=12.0, help='Duration in hours')
    parser.add_argument('--continuous', action='store_true', help='Run until stopped')
    parser.add_argument('--interval', type=int, default=200, help='Sample interval in ms')
    parser.add_argument('--output', type=str, default='research/observer', help='Output directory')
    parser.add_argument('--balance', type=float, default=170.0, help='Starting balance in USD')
    parser.add_argument('--shares', type=float, default=5.0, help='Shares per side (passive grid)')

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
