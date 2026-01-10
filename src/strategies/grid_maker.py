"""
Grid Maker Strategy - Gabagool-style two-sided passive market making.

Key Principles (from reverse-engineering Gabagool22):
1. Post grid orders on BOTH sides simultaneously at market open
2. $0.01 spacing across price range
3. Complementary pairs: UP @ $X + DOWN @ $(1-X)
4. NO velocity timing - pure passive maker
5. Let imbalances build naturally from market flow
6. Profit from: (a) pair cost < $1.00, (b) imbalanced positions winning (71.2% win rate)

Win Mechanism:
- Grid passively captures market flow
- Trending markets fill more on trending side
- 71% of imbalanced positions win (from analysis)
- Average profit: $12/market from imbalances

Usage:
    from src.strategies.grid_maker import GridMakerStrategy

    strategy = GridMakerStrategy(
        order_size=20,
        min_price=0.05,
        max_price=0.95,
        tick_size=0.01,
    )

    # Generate grid levels
    levels = strategy.generate_grid_levels()

    # Check if should post grid
    if strategy.should_post_grid(time_into_market=30):
        for level in levels:
            place_order(side=level.side, price=level.price, size=level.size)
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Tuple, Any
from datetime import datetime, timezone
import time

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS (from Gabagool analysis)
# =============================================================================

# Grid parameters (Gabagool uses ~24 shares BTC, ~11 shares ETH)
DEFAULT_ORDER_SIZE = 20          # Shares per grid level
MIN_ORDER_SIZE = 5               # Polymarket minimum
SIZE_INCREMENT = 5               # Polymarket constraint

# Price range
DEFAULT_MIN_PRICE = 0.05         # Don't post below 5c
DEFAULT_MAX_PRICE = 0.95         # Don't post above 95c
DEFAULT_TICK_SIZE = 0.01         # $0.01 grid spacing

# Timing
DEFAULT_POST_DELAY = 0.0         # No delay - post immediately at market open (Gabagool: <1s)
DEFAULT_REFRESH_INTERVAL = 60.0  # Seconds between grid refreshes

# Risk limits
DEFAULT_MAX_POSITION = 2000.0    # Max shares per side
DEFAULT_MAX_IMBALANCE = 1000.0   # Max imbalance before pausing


# =============================================================================
# ENUMS
# =============================================================================

class GridPhase(Enum):
    """Grid lifecycle phases."""
    IDLE = "idle"                    # Waiting for market
    POSTING = "posting"              # Posting grid orders
    ACTIVE = "active"                # Grid active, monitoring fills
    PAUSED = "paused"                # Paused due to risk limits
    COMPLETE = "complete"            # Market resolved


# =============================================================================
# DATACLASSES
# =============================================================================

@dataclass
class GridLevel:
    """A single level in the grid."""
    price: float
    size: int
    side: str  # "UP" or "DOWN"
    order_id: Optional[str] = None
    filled_size: int = 0
    status: str = "pending"  # pending, posted, partial, filled, cancelled


@dataclass
class GridState:
    """Current state of the grid for a market."""
    market_slug: str = ""
    phase: GridPhase = GridPhase.IDLE

    # Grid levels
    up_levels: List[GridLevel] = field(default_factory=list)
    down_levels: List[GridLevel] = field(default_factory=list)

    # Position tracking (accumulated fills)
    up_shares: float = 0
    up_cost: float = 0
    down_shares: float = 0
    down_cost: float = 0

    # Timing
    market_start_time: float = 0
    grid_posted_at: float = 0
    last_refresh_at: float = 0

    # Cycle tracking
    total_up_fills: int = 0
    total_down_fills: int = 0
    total_trades: int = 0

    @property
    def imbalance(self) -> float:
        """UP shares - DOWN shares."""
        return self.up_shares - self.down_shares

    @property
    def imbalance_pct(self) -> float:
        """Imbalance as percentage of total position."""
        total = self.up_shares + self.down_shares
        if total == 0:
            return 0
        return (self.imbalance / total) * 100

    @property
    def pair_cost(self) -> float:
        """Average pair cost (UP avg + DOWN avg)."""
        if self.up_shares > 0 and self.down_shares > 0:
            avg_up = self.up_cost / self.up_shares
            avg_down = self.down_cost / self.down_shares
            return avg_up + avg_down
        return 0

    @property
    def matched_pairs(self) -> float:
        """Number of matched pairs."""
        return min(self.up_shares, self.down_shares)

    @property
    def avg_up_price(self) -> float:
        """Average UP fill price."""
        return self.up_cost / self.up_shares if self.up_shares > 0 else 0

    @property
    def avg_down_price(self) -> float:
        """Average DOWN fill price."""
        return self.down_cost / self.down_shares if self.down_shares > 0 else 0


# =============================================================================
# STRATEGY CLASS
# =============================================================================

class GridMakerStrategy:
    """
    Gabagool-style two-sided grid market making strategy.

    Core Algorithm:
    1. At market open + delay, generate grid levels
    2. Post all orders simultaneously (UP and DOWN sides)
    3. Monitor fills via position tracking
    4. Let imbalances build naturally from market flow
    5. Profit from resolution (71.2% win rate on imbalances)

    No velocity timing. No price prediction. Pure passive maker.

    Attributes:
        order_size: Shares per grid level (default 20)
        min_price: Minimum grid price (default 0.05)
        max_price: Maximum grid price (default 0.95)
        tick_size: Grid spacing (default 0.01)
        post_delay: Seconds to wait after market open (default 5)
        max_position: Max shares per side (default 2000)
        max_imbalance: Max imbalance before pausing (default 1000)
    """

    def __init__(
        self,
        order_size: int = DEFAULT_ORDER_SIZE,
        min_price: float = DEFAULT_MIN_PRICE,
        max_price: float = DEFAULT_MAX_PRICE,
        tick_size: float = DEFAULT_TICK_SIZE,
        post_delay: float = DEFAULT_POST_DELAY,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
        max_position: float = DEFAULT_MAX_POSITION,
        max_imbalance: float = DEFAULT_MAX_IMBALANCE,
        post_complementary: bool = True,
        reduce_size_at_extremes: bool = True,
    ):
        # Ensure order_size is valid multiple
        raw_size = max(order_size, MIN_ORDER_SIZE)
        self.order_size = max(MIN_ORDER_SIZE, SIZE_INCREMENT * round(raw_size / SIZE_INCREMENT))

        self.min_price = min_price
        self.max_price = max_price
        self.tick_size = tick_size
        self.post_delay = post_delay
        self.refresh_interval = refresh_interval
        self.max_position = max_position
        self.max_imbalance = max_imbalance
        self.post_complementary = post_complementary
        self.reduce_size_at_extremes = reduce_size_at_extremes

        self.state = GridState()

        # History for analysis
        self._completed_markets: List[Dict[str, Any]] = []

    # =========================================================================
    # GRID GENERATION
    # =========================================================================

    def generate_grid_levels(
        self,
        up_best_bid: float = 0.50,
        down_best_bid: float = 0.50,
    ) -> Tuple[List[GridLevel], List[GridLevel]]:
        """
        Generate grid levels for both sides.

        Args:
            up_best_bid: Current UP best bid (for reference)
            down_best_bid: Current DOWN best bid (for reference)

        Returns:
            (up_levels, down_levels) - Lists of GridLevel objects
        """
        up_levels = []
        down_levels = []

        # Generate price levels
        price = self.min_price
        while price <= self.max_price:
            # Calculate size (optionally reduce at extremes)
            if self.reduce_size_at_extremes and (price < 0.15 or price > 0.85):
                size = max(MIN_ORDER_SIZE, self.order_size // 2)
            else:
                size = self.order_size

            # Round size to Polymarket constraint
            size = max(MIN_ORDER_SIZE, SIZE_INCREMENT * round(size / SIZE_INCREMENT))

            # Create UP level
            up_levels.append(GridLevel(
                price=round(price, 2),
                size=size,
                side="UP",
            ))

            # Create complementary DOWN level at (1 - price)
            if self.post_complementary:
                comp_price = round(1.0 - price, 2)
                if self.min_price <= comp_price <= self.max_price:
                    # Check if we already have this price
                    existing = [l for l in down_levels if abs(l.price - comp_price) < 0.001]
                    if not existing:
                        down_levels.append(GridLevel(
                            price=comp_price,
                            size=size,
                            side="DOWN",
                        ))

            price = round(price + self.tick_size, 2)

        # Sort levels by price
        up_levels.sort(key=lambda x: x.price)
        down_levels.sort(key=lambda x: x.price)

        logger.info(
            f"[GRID] Generated {len(up_levels)} UP levels, {len(down_levels)} DOWN levels | "
            f"Price range: ${self.min_price:.2f} - ${self.max_price:.2f}"
        )

        return up_levels, down_levels

    # =========================================================================
    # DECISION METHODS
    # =========================================================================

    def should_post_grid(
        self,
        time_into_market: float,
        time_remaining: float,
    ) -> bool:
        """
        Determine if we should post the grid now.

        Args:
            time_into_market: Seconds since market opened
            time_remaining: Seconds until market closes

        Returns:
            True if should post grid
        """
        # Wait for delay after market open
        if time_into_market < self.post_delay:
            return False

        # Don't post if market is about to close
        if time_remaining < 60:  # 1 minute buffer
            return False

        # Don't post if already active
        if self.state.phase in (GridPhase.ACTIVE, GridPhase.POSTING):
            return False

        return True

    def should_refresh_grid(self, current_time: float) -> bool:
        """
        Check if grid should be refreshed (re-post cancelled orders).

        Args:
            current_time: Current timestamp

        Returns:
            True if should refresh
        """
        if self.state.phase != GridPhase.ACTIVE:
            return False

        elapsed = current_time - self.state.last_refresh_at
        return elapsed >= self.refresh_interval

    def check_risk_limits(self) -> Tuple[bool, str]:
        """
        Check if we're within risk limits.

        Returns:
            (is_ok, reason)
        """
        s = self.state

        # Check position limits
        if s.up_shares > self.max_position:
            return False, f"UP position {s.up_shares:.0f} > max {self.max_position}"

        if s.down_shares > self.max_position:
            return False, f"DOWN position {s.down_shares:.0f} > max {self.max_position}"

        # Check imbalance
        if abs(s.imbalance) > self.max_imbalance:
            return False, f"Imbalance {s.imbalance:.0f} > max {self.max_imbalance}"

        return True, "OK"

    # =========================================================================
    # ORDER DECISION (for integration with paper bot)
    # =========================================================================

    def get_orders_to_place(
        self,
        up_best_bid: float,
        up_best_ask: float,
        down_best_bid: float,
        down_best_ask: float,
        time_into_market: float,
        time_remaining: float,
        current_time: float,
    ) -> List[Dict[str, Any]]:
        """
        Get list of orders to place.

        This is the main interface for integration with the paper bot.

        Args:
            up_best_bid: Current UP best bid
            up_best_ask: Current UP best ask
            down_best_bid: Current DOWN best bid
            down_best_ask: Current DOWN best ask
            time_into_market: Seconds since market opened
            time_remaining: Seconds until market closes
            current_time: Current timestamp

        Returns:
            List of order dicts: [{"side": "UP", "price": 0.50, "size": 20}, ...]
        """
        orders = []

        # Check if should post grid
        if self.state.phase == GridPhase.IDLE:
            if not self.should_post_grid(time_into_market, time_remaining):
                return []

            # Generate grid
            up_levels, down_levels = self.generate_grid_levels(up_best_bid, down_best_bid)
            self.state.up_levels = up_levels
            self.state.down_levels = down_levels
            self.state.phase = GridPhase.POSTING
            self.state.grid_posted_at = current_time
            self.state.last_refresh_at = current_time

        # Check risk limits
        is_ok, reason = self.check_risk_limits()
        if not is_ok:
            if self.state.phase != GridPhase.PAUSED:
                logger.warning(f"[GRID] Risk limit hit: {reason}")
                self.state.phase = GridPhase.PAUSED
            return []

        # If paused, check if we can resume
        if self.state.phase == GridPhase.PAUSED:
            is_ok, _ = self.check_risk_limits()
            if is_ok:
                self.state.phase = GridPhase.ACTIVE
                logger.info("[GRID] Resuming after risk limit cleared")
            else:
                return []

        # Generate orders from pending levels
        if self.state.phase in (GridPhase.POSTING, GridPhase.ACTIVE):
            # Check for refresh
            if self.state.phase == GridPhase.ACTIVE and self.should_refresh_grid(current_time):
                self.state.last_refresh_at = current_time
                # Re-post any cancelled levels
                for level in self.state.up_levels + self.state.down_levels:
                    if level.status == "cancelled":
                        level.status = "pending"

            # Calculate current exposure: filled + posted (unfilled) orders
            # This ensures we don't over-post orders that could all fill
            up_posted = sum(
                level.size - level.filled_size
                for level in self.state.up_levels
                if level.status == "posted"
            )
            down_posted = sum(
                level.size - level.filled_size
                for level in self.state.down_levels
                if level.status == "posted"
            )

            up_exposure = self.state.up_shares + up_posted
            down_exposure = self.state.down_shares + down_posted

            # Collect pending orders, respecting position limits
            for level in self.state.up_levels:
                if level.status == "pending":
                    # Don't post if filled + posted + this order > max_position
                    if up_exposure + level.size > self.max_position:
                        continue
                    # Also check imbalance limit
                    potential_imbalance = abs((up_exposure + level.size) - down_exposure)
                    if potential_imbalance > self.max_imbalance:
                        continue
                    up_exposure += level.size  # Track for next iteration
                    orders.append({
                        "side": "UP",
                        "price": level.price,
                        "size": level.size,
                    })

            for level in self.state.down_levels:
                if level.status == "pending":
                    if down_exposure + level.size > self.max_position:
                        continue
                    potential_imbalance = abs(up_exposure - (down_exposure + level.size))
                    if potential_imbalance > self.max_imbalance:
                        continue
                    down_exposure += level.size
                    orders.append({
                        "side": "DOWN",
                        "price": level.price,
                        "size": level.size,
                    })

            if orders and self.state.phase == GridPhase.POSTING:
                self.state.phase = GridPhase.ACTIVE

        return orders

    # =========================================================================
    # FILL HANDLING
    # =========================================================================

    def on_fill(self, side: str, price: float, size: int, level_price: float = None) -> None:
        """
        Process a fill event.

        Args:
            side: "UP" or "DOWN"
            price: Actual fill price (may differ from level due to price improvement)
            size: Fill size
            level_price: Original grid level price (for matching). If None, uses price.
        """
        s = self.state
        side_upper = side.upper()

        # Use level_price for matching grid levels, price for cost tracking
        match_price = level_price if level_price is not None else price

        if side_upper == "UP":
            s.up_shares += size
            s.up_cost += price * size  # Use actual fill price for cost
            s.total_up_fills += size

            # Update level status - match by level_price, not fill price
            for level in s.up_levels:
                if abs(level.price - match_price) < 0.001:
                    level.filled_size += size
                    if level.filled_size >= level.size:
                        level.status = "filled"
                    else:
                        level.status = "partial"
                    break
        else:
            s.down_shares += size
            s.down_cost += price * size  # Use actual fill price for cost
            s.total_down_fills += size

            for level in s.down_levels:
                if abs(level.price - match_price) < 0.001:
                    level.filled_size += size
                    if level.filled_size >= level.size:
                        level.status = "filled"
                    else:
                        level.status = "partial"
                    break

        s.total_trades += 1

        logger.info(
            f"[GRID] Fill: {side_upper} {size} @ ${price:.2f} | "
            f"Position: UP={s.up_shares:.0f} DOWN={s.down_shares:.0f} | "
            f"Imbalance: {s.imbalance:+.0f} ({s.imbalance_pct:+.1f}%)"
        )

    def on_order_posted(self, side: str, price: float, order_id: str) -> None:
        """Mark a level as posted with its order ID."""
        levels = self.state.up_levels if side.upper() == "UP" else self.state.down_levels
        for level in levels:
            if abs(level.price - price) < 0.001 and level.status == "pending":
                level.order_id = order_id
                level.status = "posted"
                break

    def on_order_cancelled(self, side: str, price: float) -> None:
        """Mark a level as cancelled."""
        levels = self.state.up_levels if side.upper() == "UP" else self.state.down_levels
        for level in levels:
            if abs(level.price - price) < 0.001:
                level.status = "cancelled"
                level.order_id = None
                break

    # =========================================================================
    # PROFIT ESTIMATION
    # =========================================================================

    def estimate_pnl(self, winner: str) -> float:
        """
        Estimate PnL if market resolves with given winner.

        Args:
            winner: "UP" or "DOWN"

        Returns:
            Estimated profit/loss
        """
        s = self.state

        if s.up_shares == 0 and s.down_shares == 0:
            return 0

        # Matched pairs profit/loss
        matched = s.matched_pairs
        if matched > 0 and s.pair_cost > 0:
            matched_pnl = matched * (1.0 - s.pair_cost)
        else:
            matched_pnl = 0

        # Unmatched position
        unmatched = abs(s.imbalance)
        if s.imbalance > 0:  # UP heavy
            if winner == "UP":
                unmatched_pnl = unmatched * (1.0 - s.avg_up_price)
            else:
                unmatched_pnl = -unmatched * s.avg_up_price
        else:  # DOWN heavy
            if winner == "DOWN":
                unmatched_pnl = unmatched * (1.0 - s.avg_down_price)
            else:
                unmatched_pnl = -unmatched * s.avg_down_price

        return matched_pnl + unmatched_pnl

    # =========================================================================
    # STATUS & RESET
    # =========================================================================

    def get_status(self) -> Dict[str, Any]:
        """Get current strategy status."""
        s = self.state
        return {
            "phase": s.phase.value,
            "market_slug": s.market_slug,
            "up_shares": s.up_shares,
            "down_shares": s.down_shares,
            "imbalance": s.imbalance,
            "imbalance_pct": s.imbalance_pct,
            "pair_cost": s.pair_cost,
            "matched_pairs": s.matched_pairs,
            "total_trades": s.total_trades,
            "up_levels_count": len(s.up_levels),
            "down_levels_count": len(s.down_levels),
            "up_levels_filled": len([l for l in s.up_levels if l.status == "filled"]),
            "down_levels_filled": len([l for l in s.down_levels if l.status == "filled"]),
        }

    def reset(self, market_slug: str = "") -> None:
        """Reset strategy for new market."""
        # Save completed market data
        if self.state.total_trades > 0:
            self._completed_markets.append({
                "market_slug": self.state.market_slug,
                "up_shares": self.state.up_shares,
                "down_shares": self.state.down_shares,
                "imbalance": self.state.imbalance,
                "pair_cost": self.state.pair_cost,
                "total_trades": self.state.total_trades,
            })

        self.state = GridState(market_slug=market_slug)
        logger.debug(f"[GRID] Strategy reset for market: {market_slug}")

    def get_completed_markets(self) -> List[Dict[str, Any]]:
        """Get list of completed markets with stats."""
        return self._completed_markets.copy()

    def __repr__(self) -> str:
        return (
            f"GridMakerStrategy("
            f"order_size={self.order_size}, "
            f"price_range=${self.min_price}-${self.max_price}, "
            f"tick=${self.tick_size})"
        )
