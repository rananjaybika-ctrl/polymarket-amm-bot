"""
Grid MM Passive Strategy - Simple Two-Sided Market Making

Based on backtesting research showing $95/hr profit potential with 100% profitable pairs.

KEY PRINCIPLES (from backtest):
1. ALL bids are PASSIVE - post BELOW best_bid
2. NO order pulling on velocity zone changes
3. Velocity only adjusts LOSER depth (winner stays at 0.01)
4. Wait for market to drop to our prices (MAKER fills)

Formula: our_bid = best_bid - offset
- Positive offset = bid BELOW best_bid (passive, cheaper fills)
- ALL offsets are positive (0.01 to 0.05)

Velocity Zones:
|v| < 0.10:   winner=0.01, loser=0.01  (symmetric)
|v| 0.10-0.30: winner=0.01, loser=0.01  (same)
|v| 0.30-0.50: winner=0.01, loser=0.03  (loser deeper)
|v| >= 0.50:   winner=0.01, loser=0.05  (loser very deep)

Author: Claude Code
Date: January 16, 2026
Based on: research/grid_mm_velocity_backtest.py
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS
# =============================================================================

# Velocity zone configuration - ALL PASSIVE (positive offsets)
# Formula: our_bid = best_bid - offset
VELOCITY_ZONES = {
    'neutral': {
        'vel_min': 0.00, 'vel_max': 0.10,
        'winner_offset': 0.01,  # best_bid - 0.01 (one tick below)
        'loser_offset': 0.01,   # best_bid - 0.01 (symmetric)
    },
    'moderate': {
        'vel_min': 0.10, 'vel_max': 0.30,
        'winner_offset': 0.01,  # best_bid - 0.01 (one tick below)
        'loser_offset': 0.01,   # best_bid - 0.01 (same)
    },
    'strong': {
        'vel_min': 0.30, 'vel_max': 0.50,
        'winner_offset': 0.01,  # best_bid - 0.01 (one tick below)
        'loser_offset': 0.03,   # best_bid - 0.03 (three ticks below)
    },
    'very_strong': {
        'vel_min': 0.50, 'vel_max': 99.0,
        'winner_offset': 0.01,  # best_bid - 0.01 (one tick below)
        'loser_offset': 0.05,   # best_bid - 0.05 (five ticks below)
    },
}

# Default configuration
DEFAULT_ORDER_SIZE = 15          # Shares per order
DEFAULT_MAX_POSITION = 200       # Max shares per side
DEFAULT_MIN_TIME = 60            # Stop posting at 60s remaining
QUOTE_REFRESH_INTERVAL = 0.5    # Refresh quotes every 500ms
MIN_TICK = 0.01                  # Minimum price tick


# =============================================================================
# STATE DATACLASS
# =============================================================================

@dataclass
class GridMMPassiveState:
    """State tracking for passive grid market making."""

    # Position tracking
    up_shares: int = 0
    down_shares: int = 0
    up_total_cost: float = 0.0
    down_total_cost: float = 0.0

    # Active order tracking - orders stay posted until filled
    # Format: (posted_price, posted_time) or None
    active_up_order: Optional[Tuple[float, float]] = None
    active_down_order: Optional[Tuple[float, float]] = None

    # Fill tracking
    up_fills: List[Dict[str, Any]] = field(default_factory=list)
    down_fills: List[Dict[str, Any]] = field(default_factory=list)

    # Pair tracking
    completed_pairs: List[Dict[str, Any]] = field(default_factory=list)
    total_pairs: int = 0
    total_profit: float = 0.0

    # Timing
    last_quote_time: float = 0.0
    last_velocity: float = 0.0

    @property
    def up_avg_price(self) -> float:
        """Average fill price for UP side."""
        if self.up_shares == 0:
            return 0.0
        return self.up_total_cost / self.up_shares

    @property
    def down_avg_price(self) -> float:
        """Average fill price for DOWN side."""
        if self.down_shares == 0:
            return 0.0
        return self.down_total_cost / self.down_shares

    @property
    def pair_cost(self) -> float:
        """Average pair cost based on current positions."""
        if self.up_shares == 0 or self.down_shares == 0:
            return 0.0
        return self.up_avg_price + self.down_avg_price

    @property
    def matchable_pairs(self) -> int:
        """Number of shares that can be paired."""
        return min(self.up_shares, self.down_shares)


# =============================================================================
# STRATEGY CLASS
# =============================================================================

class GridMMPassiveStrategy:
    """
    Passive Grid Market Making Strategy.

    Posts BID orders BELOW best_bid on both UP and DOWN sides.
    Waits for market to drop to our prices for MAKER fills.
    When both sides fill, pair_cost < $1.00 = profit at settlement.

    Key features:
    - ALL bids passive (below best_bid)
    - NO order pulling on velocity zone changes
    - Velocity only affects LOSER depth
    - Simple position tracking

    Args:
        order_size: Shares per order (default 15)
        max_position: Maximum shares per side (default 200)
        min_time_remaining: Stop posting below this time (default 60s)
    """

    def __init__(
        self,
        order_size: int = DEFAULT_ORDER_SIZE,
        max_position: int = DEFAULT_MAX_POSITION,
        min_time_remaining: int = DEFAULT_MIN_TIME,
    ):
        self.order_size = order_size
        self.max_position = max_position
        self.min_time_remaining = min_time_remaining

        self.state = GridMMPassiveState()

        logger.info(
            f"[GRIDMM] Initialized: order_size={order_size}, "
            f"max_position={max_position}, min_time={min_time_remaining}s"
        )

    # =========================================================================
    # VELOCITY ZONE LOGIC
    # =========================================================================

    def get_velocity_zone(self, velocity: float) -> Dict[str, Any]:
        """Get velocity zone configuration based on current velocity."""
        abs_vel = abs(velocity)
        for zone_name, zone in VELOCITY_ZONES.items():
            if zone['vel_min'] <= abs_vel < zone['vel_max']:
                return zone
        return VELOCITY_ZONES['very_strong']

    def get_offsets(self, velocity: float) -> Tuple[float, float]:
        """
        Get (up_offset, down_offset) based on velocity direction.

        - Positive velocity = BTC rising = UP is winner, DOWN is loser
        - Negative velocity = BTC falling = DOWN is winner, UP is loser

        Returns:
            (up_offset, down_offset) - both positive (passive bids)
        """
        zone = self.get_velocity_zone(velocity)
        winner_offset = zone['winner_offset']
        loser_offset = zone['loser_offset']

        if velocity > 0:  # UP winning, DOWN losing
            return (winner_offset, loser_offset)
        elif velocity < 0:  # DOWN winning, UP losing
            return (loser_offset, winner_offset)
        else:  # Neutral
            return (winner_offset, winner_offset)

    # =========================================================================
    # QUOTE GENERATION
    # =========================================================================

    def get_quotes(
        self,
        up_bid: float,
        up_ask: float,
        down_bid: float,
        down_ask: float,
        velocity_bps: float,
        time_remaining: float,
        current_time: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Generate bid quotes for both sides.

        Args:
            up_bid: Current UP best bid
            up_ask: Current UP best ask
            down_bid: Current DOWN best bid
            down_ask: Current DOWN best ask
            velocity_bps: Current BTC velocity (basis points per second)
            time_remaining: Seconds until market resolution
            current_time: Current timestamp (default: time.time())

        Returns:
            List of quote dicts: [{'side': str, 'price': float, 'size': int}, ...]
        """
        if current_time is None:
            current_time = time.time()

        s = self.state

        # Don't place orders near market end
        if time_remaining < self.min_time_remaining:
            logger.debug(f"[GRIDMM] Skipping: {time_remaining:.0f}s remaining < {self.min_time_remaining}s")
            return []

        # Rate limit
        if current_time - s.last_quote_time < QUOTE_REFRESH_INTERVAL:
            return []

        s.last_quote_time = current_time
        s.last_velocity = velocity_bps

        # Calculate offsets based on velocity
        up_offset, down_offset = self.get_offsets(velocity_bps)

        quotes = []

        # Generate UP quote if we have room and no active order
        if s.up_shares < self.max_position and s.active_up_order is None:
            up_price = up_bid - up_offset
            up_price = max(MIN_TICK, min(up_price, up_ask - MIN_TICK))  # Stay below ask
            up_price = round(up_price, 2)

            if up_price > MIN_TICK:
                quotes.append({
                    'side': 'UP',
                    'price': up_price,
                    'size': self.order_size,
                    'offset': up_offset,
                })
                s.active_up_order = (up_price, current_time)

        # Generate DOWN quote if we have room and no active order
        if s.down_shares < self.max_position and s.active_down_order is None:
            down_price = down_bid - down_offset
            down_price = max(MIN_TICK, min(down_price, down_ask - MIN_TICK))  # Stay below ask
            down_price = round(down_price, 2)

            if down_price > MIN_TICK:
                quotes.append({
                    'side': 'DOWN',
                    'price': down_price,
                    'size': self.order_size,
                    'offset': down_offset,
                })
                s.active_down_order = (down_price, current_time)

        if quotes:
            zone_name = list(VELOCITY_ZONES.keys())[
                next(i for i, z in enumerate(VELOCITY_ZONES.values())
                     if z['vel_min'] <= abs(velocity_bps) < z['vel_max'])
            ] if abs(velocity_bps) < 99.0 else 'very_strong'

            logger.debug(
                f"[GRIDMM] Quotes: {len(quotes)} orders, "
                f"vel={velocity_bps:.3f}bps ({zone_name}), "
                f"offsets=(UP={up_offset:.2f}, DOWN={down_offset:.2f})"
            )

        return quotes

    # =========================================================================
    # FILL HANDLING
    # =========================================================================

    def on_fill(
        self,
        side: str,
        price: float,
        size: int,
        fill_time: Optional[float] = None,
    ) -> None:
        """
        Handle a fill notification.

        Args:
            side: "UP" or "DOWN"
            price: Fill price
            size: Fill size (shares)
            fill_time: Fill timestamp (default: time.time())
        """
        if fill_time is None:
            fill_time = time.time()

        s = self.state
        side_upper = side.upper()

        fill_record = {
            'price': price,
            'size': size,
            'time': fill_time,
        }

        if side_upper == 'UP':
            s.up_shares += size
            s.up_total_cost += price * size
            s.up_fills.append(fill_record)
            s.active_up_order = None  # Clear so we can post new order

            logger.info(
                f"[GRIDMM] UP fill: {size}@${price:.3f} | "
                f"Position: {s.up_shares} shares @ avg ${s.up_avg_price:.3f}"
            )
        else:
            s.down_shares += size
            s.down_total_cost += price * size
            s.down_fills.append(fill_record)
            s.active_down_order = None  # Clear so we can post new order

            logger.info(
                f"[GRIDMM] DOWN fill: {size}@${price:.3f} | "
                f"Position: {s.down_shares} shares @ avg ${s.down_avg_price:.3f}"
            )

        # Check for completed pairs
        self._check_pairs()

    def _check_pairs(self) -> None:
        """Check and record completed pairs."""
        s = self.state
        matchable = s.matchable_pairs

        if matchable == 0:
            return

        pair_cost = s.pair_cost
        profit_per_share = 1.00 - pair_cost
        total_profit = profit_per_share * matchable

        # Record pair
        pair_record = {
            'shares': matchable,
            'up_avg': s.up_avg_price,
            'down_avg': s.down_avg_price,
            'pair_cost': pair_cost,
            'profit': total_profit,
            'time': time.time(),
        }
        s.completed_pairs.append(pair_record)
        s.total_pairs += matchable
        s.total_profit += total_profit

        logger.info(
            f"[GRIDMM] PAIR COMPLETE: {matchable} shares @ ${pair_cost:.4f} | "
            f"Profit: ${total_profit:.4f} | Total: ${s.total_profit:.4f}"
        )

        # Reset matched positions
        if s.up_shares > s.down_shares:
            remaining_up = s.up_shares - matchable
            s.up_shares = remaining_up
            s.up_total_cost = s.up_avg_price * remaining_up if remaining_up > 0 else 0.0
            s.down_shares = 0
            s.down_total_cost = 0.0
        else:
            remaining_down = s.down_shares - matchable
            s.down_shares = remaining_down
            s.down_total_cost = s.down_avg_price * remaining_down if remaining_down > 0 else 0.0
            s.up_shares = 0
            s.up_total_cost = 0.0

        # Clear fills for matched pairs
        s.up_fills = []
        s.down_fills = []

    # =========================================================================
    # FILL DETECTION (for backtesting)
    # =========================================================================

    def check_fills(
        self,
        current_up_bid: float,
        current_down_bid: float,
        current_time: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Check if active orders would be filled based on price movement.

        For backtesting: If market best_bid drops to or below our posted price,
        we consider our order filled.

        Args:
            current_up_bid: Current UP best bid
            current_down_bid: Current DOWN best bid
            current_time: Current timestamp

        Returns:
            List of fills: [{'side': str, 'price': float, 'size': int}, ...]
        """
        if current_time is None:
            current_time = time.time()

        s = self.state
        fills = []

        # Check UP order
        if s.active_up_order is not None:
            posted_price, posted_time = s.active_up_order
            if current_up_bid <= posted_price:
                fills.append({
                    'side': 'UP',
                    'price': posted_price,
                    'size': self.order_size,
                })
                self.on_fill('UP', posted_price, self.order_size, current_time)

        # Check DOWN order
        if s.active_down_order is not None:
            posted_price, posted_time = s.active_down_order
            if current_down_bid <= posted_price:
                fills.append({
                    'side': 'DOWN',
                    'price': posted_price,
                    'size': self.order_size,
                })
                self.on_fill('DOWN', posted_price, self.order_size, current_time)

        return fills

    # =========================================================================
    # STATUS & RESET
    # =========================================================================

    def get_status(self) -> Dict[str, Any]:
        """Get current strategy status."""
        s = self.state
        return {
            'position': {
                'up_shares': s.up_shares,
                'up_avg_price': s.up_avg_price,
                'down_shares': s.down_shares,
                'down_avg_price': s.down_avg_price,
                'pair_cost': s.pair_cost,
                'matchable_pairs': s.matchable_pairs,
            },
            'orders': {
                'active_up': s.active_up_order,
                'active_down': s.active_down_order,
            },
            'statistics': {
                'total_up_fills': len(s.up_fills) + sum(p['shares'] for p in s.completed_pairs),
                'total_down_fills': len(s.down_fills) + sum(p['shares'] for p in s.completed_pairs),
                'total_pairs': s.total_pairs,
                'total_profit': s.total_profit,
            },
            'config': {
                'order_size': self.order_size,
                'max_position': self.max_position,
                'min_time_remaining': self.min_time_remaining,
            },
        }

    def reset(self) -> None:
        """Reset strategy for new market."""
        total_profit = self.state.total_profit
        total_pairs = self.state.total_pairs

        self.state = GridMMPassiveState()
        self.state.total_profit = total_profit
        self.state.total_pairs = total_pairs

        logger.info(f"[GRIDMM] Reset for new market (cumulative profit: ${total_profit:.2f})")

    def __repr__(self) -> str:
        return (
            f"GridMMPassiveStrategy("
            f"order_size={self.order_size}, "
            f"max_position={self.max_position})"
        )
