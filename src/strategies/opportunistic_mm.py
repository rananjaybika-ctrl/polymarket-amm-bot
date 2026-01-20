"""
Opportunistic Market Making Strategy with Inventory Balance

Two-sided passive quoting with dynamic offsets based on inventory imbalance.
Captures spread while staying balanced.

Key Formulas:
    imbalance = (up_shares - down_shares) / (up_shares + down_shares)
    adjustment = imbalance * 0.02
    up_offset = base_offset + adjustment   (wider if excess UP)
    down_offset = base_offset - adjustment (tighter if deficit UP)

Features:
- Posts bids on BOTH sides simultaneously
- Dynamic offset adjustment based on inventory
- Velocity-zone aware base offsets
- Automatic rebalancing when imbalance exceeds threshold
- Spike detection to avoid adverse selection

Usage:
    strategy = OpportunisticMMStrategy(
        base_size=15,
        rebalance_threshold=0.30,
        max_position=200,
    )
    quotes = strategy.get_quotes(up_bid=0.55, up_ask=0.56, ..., velocity_bps=0.2)

Author: Claude Code
Date: January 17, 2026
Based on: mm_backtest.py research, grid_mm_passive.py
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from src.config import FeeConfig

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS
# =============================================================================

# Base offsets per velocity zone
VELOCITY_ZONES = {
    'neutral':     {'vel_min': 0.00, 'vel_max': 0.10, 'base_offset': 0.01},
    'moderate':    {'vel_min': 0.10, 'vel_max': 0.30, 'base_offset': 0.01},
    'strong':      {'vel_min': 0.30, 'vel_max': 0.50, 'base_offset': 0.02},
    'very_strong': {'vel_min': 0.50, 'vel_max': 99.0, 'base_offset': 0.03},
}

# Inventory adjustment
INVENTORY_ADJUSTMENT_FACTOR = 0.02  # Max 2 cents adjustment
MAX_OFFSET = 0.05

# Position limits
DEFAULT_MAX_POSITION = 200
DEFAULT_REBALANCE_THRESHOLD = 0.30  # 30% imbalance

# Spike detection
SPIKE_LOOKBACK = 3
SPIKE_THRESHOLD = 0.02
SPIKE_HISTORY_SIZE = 50

# Timing
MIN_TIME_REMAINING = 60
QUOTE_REFRESH_INTERVAL = 0.5

# Position sizing
MIN_SHARES = 5
DEFAULT_BASE_SIZE = 15


# =============================================================================
# ENUMS
# =============================================================================

class MMPhase(Enum):
    """Strategy phases."""
    IDLE = "idle"
    QUOTING = "quoting"
    REBALANCING = "rebalancing"
    COMPLETE = "complete"


# =============================================================================
# STATE DATACLASS
# =============================================================================

@dataclass
class OpportunisticMMState:
    """State tracking for opportunistic MM strategy."""
    phase: MMPhase = MMPhase.IDLE

    # Position tracking
    up_shares: int = 0
    down_shares: int = 0
    up_cost: float = 0.0
    down_cost: float = 0.0

    # Quote tracking
    up_posted_bid: float = 0.0
    down_posted_bid: float = 0.0
    last_up_offset: float = 0.0
    last_down_offset: float = 0.0

    # Spike detection
    spike_history: List[float] = field(default_factory=list)
    last_spike_direction: Optional[str] = None
    last_spike_magnitude: float = 0.0

    # Statistics
    total_up_fills: int = 0
    total_down_fills: int = 0
    total_pairs_matched: int = 0
    total_pnl: float = 0.0
    total_rebalances: int = 0
    max_imbalance_reached: float = 0.0

    # Timing
    last_quote_time: float = 0.0
    last_velocity: float = 0.0

    @property
    def imbalance(self) -> float:
        """Signed imbalance: positive = UP heavy, negative = DOWN heavy."""
        total = self.up_shares + self.down_shares
        if total == 0:
            return 0.0
        return (self.up_shares - self.down_shares) / total

    @property
    def abs_imbalance(self) -> float:
        return abs(self.imbalance)

    @property
    def pairs(self) -> int:
        return min(self.up_shares, self.down_shares)

    @property
    def up_avg_price(self) -> float:
        return self.up_cost / self.up_shares if self.up_shares > 0 else 0.0

    @property
    def down_avg_price(self) -> float:
        return self.down_cost / self.down_shares if self.down_shares > 0 else 0.0

    @property
    def pair_cost(self) -> float:
        if self.up_shares == 0 or self.down_shares == 0:
            return 0.0
        return self.up_avg_price + self.down_avg_price


# =============================================================================
# STRATEGY CLASS
# =============================================================================

class OpportunisticMMStrategy:
    """
    Opportunistic Market Making Strategy with Inventory Balance.

    Posts two-sided passive quotes with dynamic offsets based on inventory.
    """

    def __init__(
        self,
        base_size: int = DEFAULT_BASE_SIZE,
        max_position: int = DEFAULT_MAX_POSITION,
        rebalance_threshold: float = DEFAULT_REBALANCE_THRESHOLD,
        enable_cycling: bool = True,
    ):
        """
        Initialize Opportunistic MM Strategy.

        Args:
            base_size: Shares per fill
            max_position: Maximum position per side
            rebalance_threshold: Imbalance % to trigger rebalancing
            enable_cycling: Continue after completing pairs
        """
        self.base_size = max(MIN_SHARES, base_size)
        self.max_position = max_position
        self.rebalance_threshold = rebalance_threshold
        self.enable_cycling = enable_cycling

        self.state = OpportunisticMMState()

        logger.info(
            f"[MM] Initialized: base_size={base_size}, max_pos={max_position}, "
            f"rebalance={rebalance_threshold:.0%}, cycling={enable_cycling}"
        )

    # =========================================================================
    # VELOCITY ZONE & OFFSET CALCULATION
    # =========================================================================

    def get_velocity_zone(self, velocity_bps: float) -> str:
        """Get velocity zone name."""
        abs_vel = abs(velocity_bps)
        for zone_name, zone in VELOCITY_ZONES.items():
            if zone['vel_min'] <= abs_vel < zone['vel_max']:
                return zone_name
        return 'very_strong'

    def get_base_offset(self, velocity_bps: float) -> float:
        """Get base offset for current velocity zone."""
        zone = self.get_velocity_zone(velocity_bps)
        return VELOCITY_ZONES[zone]['base_offset']

    def calculate_dynamic_offsets(self, velocity_bps: float) -> Tuple[float, float]:
        """
        Calculate dynamic offsets based on velocity and inventory imbalance.

        Returns:
            (up_offset, down_offset)
        """
        base = self.get_base_offset(velocity_bps)
        imbalance = self.state.imbalance

        # Inventory adjustment
        # If UP heavy (imbalance > 0): widen UP offset, tighten DOWN offset
        # If DOWN heavy (imbalance < 0): widen DOWN offset, tighten UP offset
        adjustment = imbalance * INVENTORY_ADJUSTMENT_FACTOR

        up_offset = base + adjustment
        down_offset = base - adjustment

        # Clamp offsets
        up_offset = max(0.005, min(MAX_OFFSET, up_offset))
        down_offset = max(0.005, min(MAX_OFFSET, down_offset))

        self.state.last_up_offset = up_offset
        self.state.last_down_offset = down_offset

        return up_offset, down_offset

    # =========================================================================
    # SPIKE DETECTION
    # =========================================================================

    def detect_spike(self, binance_price: float) -> Tuple[Optional[str], float]:
        """Detect Binance price spike for adverse selection avoidance."""
        s = self.state

        s.spike_history.append(binance_price)
        if len(s.spike_history) > SPIKE_HISTORY_SIZE:
            s.spike_history = s.spike_history[-SPIKE_HISTORY_SIZE:]

        if len(s.spike_history) < SPIKE_LOOKBACK + 1:
            return None, 0.0

        current = s.spike_history[-1]
        previous = s.spike_history[-SPIKE_LOOKBACK - 1]

        if previous <= 0:
            return None, 0.0

        change_pct = (current - previous) / previous * 100
        magnitude = abs(change_pct)

        if magnitude >= SPIKE_THRESHOLD:
            direction = "UP" if change_pct > 0 else "DOWN"
            s.last_spike_direction = direction
            s.last_spike_magnitude = magnitude
            return direction, magnitude

        s.last_spike_direction = None
        s.last_spike_magnitude = 0.0
        return None, 0.0

    # =========================================================================
    # REBALANCING
    # =========================================================================

    def check_rebalance_needed(self) -> Tuple[bool, Optional[str], int]:
        """
        Check if rebalancing is needed.

        Returns:
            (needs_rebalance, side_to_sell, size_to_sell)
        """
        s = self.state

        if s.abs_imbalance <= self.rebalance_threshold:
            return False, None, 0

        # Determine which side to sell
        if s.imbalance > 0:
            # UP heavy - sell UP
            sell_side = "UP"
            excess = s.up_shares - s.down_shares
        else:
            # DOWN heavy - sell DOWN
            sell_side = "DOWN"
            excess = s.down_shares - s.up_shares

        # Sell half the excess to rebalance
        sell_size = min(self.base_size, excess // 2)
        if sell_size < MIN_SHARES:
            return False, None, 0

        return True, sell_side, sell_size

    # =========================================================================
    # MAIN ENTRY POINT: GET QUOTES
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
        binance_price: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Generate two-sided quotes for market making.

        Posts bids on BOTH sides with inventory-adjusted offsets.
        """
        if current_time is None:
            current_time = time.time()

        s = self.state

        # Don't enter if market ending soon
        if time_remaining < MIN_TIME_REMAINING:
            return []

        # Rate limit
        if current_time - s.last_quote_time < QUOTE_REFRESH_INTERVAL:
            return []
        s.last_quote_time = current_time
        s.last_velocity = velocity_bps

        # Track max imbalance
        if s.abs_imbalance > s.max_imbalance_reached:
            s.max_imbalance_reached = s.abs_imbalance

        # Detect spike for adverse selection avoidance
        spike_dir = None
        if binance_price is not None:
            spike_dir, _ = self.detect_spike(binance_price)

        quotes = []

        # Check for rebalancing first
        needs_rebalance, sell_side, sell_size = self.check_rebalance_needed()
        if needs_rebalance and sell_side and sell_size > 0:
            s.phase = MMPhase.REBALANCING

            # Sell at current bid (aggressive to rebalance quickly)
            sell_price = up_bid if sell_side == "UP" else down_bid

            quotes.append({
                'side': sell_side,
                'price': sell_price,
                'size': -sell_size,  # Negative for sell
                'is_rebalance': True,
                'is_mm': True,
            })

            logger.info(
                f"[MM] REBALANCE: SELL {sell_side} {sell_size}@${sell_price:.3f} "
                f"(imbalance={s.imbalance:.1%})"
            )

            return quotes

        # Normal quoting phase
        s.phase = MMPhase.QUOTING

        # Calculate dynamic offsets
        up_offset, down_offset = self.calculate_dynamic_offsets(velocity_bps)
        zone = self.get_velocity_zone(velocity_bps)

        # UP side quote
        if s.up_shares < self.max_position:
            # Skip if spike in same direction and we're heavy on that side
            skip_up = (spike_dir == "UP" and s.imbalance > 0)
            if not skip_up:
                up_bid_price = up_bid - up_offset
                up_bid_price = max(0.01, min(0.95, up_bid_price))

                quotes.append({
                    'side': "UP",
                    'price': up_bid_price,
                    'size': self.base_size,
                    'is_mm': True,
                    'offset': up_offset,
                    'velocity_zone': zone,
                })

                s.up_posted_bid = up_bid_price

        # DOWN side quote
        if s.down_shares < self.max_position:
            # Skip if spike in same direction and we're heavy on that side
            skip_down = (spike_dir == "DOWN" and s.imbalance < 0)
            if not skip_down:
                down_bid_price = down_bid - down_offset
                down_bid_price = max(0.01, min(0.95, down_bid_price))

                quotes.append({
                    'side': "DOWN",
                    'price': down_bid_price,
                    'size': self.base_size,
                    'is_mm': True,
                    'offset': down_offset,
                    'velocity_zone': zone,
                })

                s.down_posted_bid = down_bid_price

        if quotes:
            logger.debug(
                f"[MM] Quotes: UP@${s.up_posted_bid:.3f} (off={up_offset:.3f}), "
                f"DOWN@${s.down_posted_bid:.3f} (off={down_offset:.3f}), "
                f"imbal={s.imbalance:.1%}, zone={zone}"
            )

        return quotes

    # =========================================================================
    # FILL HANDLING
    # =========================================================================

    def on_fill(self, side: str, price: float, size: int, is_rebalance: bool = False) -> None:
        """Handle fill notification."""
        s = self.state
        side_upper = side.upper()

        if is_rebalance or size < 0:
            # Rebalance sell
            sell_size = abs(size)
            if side_upper == "UP":
                if sell_size >= s.up_shares:
                    s.up_cost = 0.0
                    s.up_shares = 0
                else:
                    avg = s.up_avg_price
                    s.up_shares -= sell_size
                    s.up_cost = avg * s.up_shares
            else:
                if sell_size >= s.down_shares:
                    s.down_cost = 0.0
                    s.down_shares = 0
                else:
                    avg = s.down_avg_price
                    s.down_shares -= sell_size
                    s.down_cost = avg * s.down_shares

            s.total_rebalances += 1
            logger.info(
                f"[MM] Rebalance fill: SOLD {side_upper} {sell_size}@${price:.3f}, "
                f"new imbalance={s.imbalance:.1%}"
            )
            return

        # Normal buy fill
        if side_upper == "UP":
            s.up_cost += price * size
            s.up_shares += size
            s.total_up_fills += size
        else:
            s.down_cost += price * size
            s.down_shares += size
            s.total_down_fills += size

        logger.info(
            f"[MM] Fill: {side_upper} {size}@${price:.3f} | "
            f"Pos: UP={s.up_shares}, DOWN={s.down_shares}, "
            f"imbal={s.imbalance:.1%}"
        )

        # Check for completed pairs
        self._check_completed_pairs()

    def _check_completed_pairs(self) -> None:
        """Check and record matched pairs."""
        s = self.state
        pairs = s.pairs

        if pairs == 0:
            return

        pair_cost = s.pair_cost
        pnl = (1.0 - pair_cost) * pairs

        # Calculate net profit with fees
        net_pnl = FeeConfig.calculate_net_profit(
            entry_price=s.up_avg_price,
            hedge_price=s.down_avg_price,
            size=pairs,
            entry_is_maker=True,
            hedge_is_maker=True,
        )

        s.total_pairs_matched += pairs
        s.total_pnl += net_pnl

        logger.info(
            f"[MM] Pairs: {pairs} @ ${pair_cost:.4f} | "
            f"PnL: ${net_pnl:.4f} | Total: ${s.total_pnl:.4f}"
        )

        # Remove matched pairs
        if s.up_shares > s.down_shares:
            s.up_shares -= pairs
            s.up_cost = s.up_avg_price * s.up_shares
            s.down_shares = 0
            s.down_cost = 0.0
        else:
            s.down_shares -= pairs
            s.down_cost = s.down_avg_price * s.down_shares
            s.up_shares = 0
            s.up_cost = 0.0

        s.phase = MMPhase.QUOTING

    # =========================================================================
    # STATUS & RESET
    # =========================================================================

    def get_status(self) -> Dict[str, Any]:
        """Get current strategy status."""
        s = self.state
        return {
            "phase": s.phase.value,
            "position": {
                "up_shares": s.up_shares,
                "up_avg_price": s.up_avg_price,
                "down_shares": s.down_shares,
                "down_avg_price": s.down_avg_price,
                "imbalance": s.imbalance,
                "pair_cost": s.pair_cost,
                "pairs": s.pairs,
            },
            "quotes": {
                "up_posted_bid": s.up_posted_bid,
                "down_posted_bid": s.down_posted_bid,
                "last_up_offset": s.last_up_offset,
                "last_down_offset": s.last_down_offset,
            },
            "statistics": {
                "total_up_fills": s.total_up_fills,
                "total_down_fills": s.total_down_fills,
                "total_pairs_matched": s.total_pairs_matched,
                "total_pnl": s.total_pnl,
                "total_rebalances": s.total_rebalances,
                "max_imbalance_reached": s.max_imbalance_reached,
            },
        }

    def reset(self) -> None:
        """Reset strategy for new market."""
        total_pnl = self.state.total_pnl
        total_pairs = self.state.total_pairs_matched
        total_rebalances = self.state.total_rebalances

        self.state = OpportunisticMMState()
        self.state.total_pnl = total_pnl
        self.state.total_pairs_matched = total_pairs
        self.state.total_rebalances = total_rebalances

        logger.info(f"[MM] Reset for new market (total_pnl=${total_pnl:.2f})")

    def __repr__(self) -> str:
        return (
            f"OpportunisticMMStrategy("
            f"base_size={self.base_size}, "
            f"max_pos={self.max_position}, "
            f"rebalance={self.rebalance_threshold:.0%}, "
            f"cycling={self.enable_cycling})"
        )
