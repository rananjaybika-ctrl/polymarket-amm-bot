"""
Simple Hedger Strategy v1

A minimal hedging strategy for BTC 15-minute Up/Down markets.
~60 lines of actual logic. No overcomplicated math.

Strategy:
1. Wait 3 seconds to see which side is expensive
2. Post expensive side @ best_bid (maker order)
3. On fill, post cheap side @ (TARGET - fill_price)
4. If cheap side moves +20c from start, FLIP (expensive becomes cheap)

Parameters:
    TARGET_PAIR_COST = $0.97 (3c profit per pair)
    SIZE = 5 shares (Polymarket minimum)
    WAIT_SECONDS = 3
    ORDER_TIMEOUT = 30s
    FLIP_THRESHOLD = 0.20 (relative from market start)
"""

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple


# =============================================================================
# PARAMETERS
# =============================================================================

TARGET_PAIR_COST = 0.97       # Target pair cost for normal hedge
EMERGENCY_PAIR_COST = 0.98    # Target pair cost after flip (emergency)
SIZE = 10                     # Shares per trade (10 ensures $1 min value after flip)
WAIT_SECONDS = 3              # Wait to see direction
ORDER_TIMEOUT = 30            # Seconds before cancel/retry
FLIP_THRESHOLD = 0.20         # +20c relative triggers flip

# Polymarket constraints
MIN_ORDER_VALUE = 1.0         # Minimum $1 order value


class Phase(Enum):
    """Simple state machine phases."""
    WAITING = "waiting"                     # Waiting initial 3 seconds
    POSTING_FIRST = "posting_first"         # Posting first side (expensive)
    WAITING_FIRST_FILL = "waiting_first_fill"
    POSTING_HEDGE = "posting_hedge"         # Posting hedge side (normal flow)
    WAITING_HEDGE_FILL = "waiting_hedge_fill"
    # Flip phases
    POSTING_FLIP = "posting_flip"           # After flip: post 2x on side that ran
    WAITING_FLIP_FILL = "waiting_flip_fill"
    POSTING_FINAL = "posting_final"         # Post remaining shares to complete
    WAITING_FINAL_FILL = "waiting_final_fill"
    DONE = "done"


@dataclass
class SimpleHedgerState:
    """State for one market cycle."""
    phase: Phase = Phase.WAITING

    # Initial prices (captured after WAIT_SECONDS, reset on flip)
    initial_up_bid: float = 0.0
    initial_down_bid: float = 0.0

    # First fill (original expensive side - persists across flips)
    first_fill_side: str = ""  # "UP" or "DOWN"
    first_fill_price: float = 0.0
    first_fill_size: float = 0.0

    # Post-flip fill (2x size on side that ran away)
    flip_fill_side: str = ""
    flip_fill_price: float = 0.0
    flip_fill_size: float = 0.0

    # Final hedge fill (remaining shares to complete position)
    hedge_fill_price: float = 0.0
    hedge_fill_size: float = 0.0

    # Current target side (what we're trying to buy now)
    current_target_side: str = ""
    current_target_size: int = 0

    # Order tracking
    order_placed_at: float = 0.0  # timestamp

    # Flip tracking
    flip_count: int = 0
    flipped: bool = False  # True after first flip

    # Paper trading fill simulation
    paper_order_placed_at: float = 0.0  # When paper order was placed
    paper_fill_attempts: int = 0  # Number of fill attempts made

    def can_paper_fill(self, current_time: float, min_delay: float = 0.5) -> bool:
        """
        Check if minimum time has passed for paper fill (simulates queue position).

        Args:
            current_time: Current timestamp
            min_delay: Minimum seconds before fill is possible (default 500ms)

        Returns:
            True if enough time has passed for fill attempt
        """
        if self.paper_order_placed_at == 0:
            return True
        elapsed = current_time - self.paper_order_placed_at
        return elapsed >= min_delay

    def get_total_cost(self) -> float:
        """Get total cost spent so far."""
        cost = self.first_fill_price * self.first_fill_size
        if self.flip_fill_size > 0:
            cost += self.flip_fill_price * self.flip_fill_size
        if self.hedge_fill_size > 0:
            cost += self.hedge_fill_price * self.hedge_fill_size
        return cost


class SimpleHedgerStrategy:
    """
    Simple Hedger Strategy v1.

    Usage:
        strategy = SimpleHedgerStrategy()

        # In trading loop:
        action = strategy.decide(
            time_in_market=5.0,
            up_bid=0.55, up_ask=0.56,
            down_bid=0.44, down_ask=0.45,
            current_time=time.time()
        )

        if action:
            side, price, size = action
            # Place order...

        # On fill callback:
        strategy.on_fill(side="UP", price=0.55, size=5)
    """

    def __init__(
        self,
        target_pair_cost: float = TARGET_PAIR_COST,
        emergency_pair_cost: float = EMERGENCY_PAIR_COST,
        size: int = SIZE,
        wait_seconds: float = WAIT_SECONDS,
        order_timeout: float = ORDER_TIMEOUT,
        flip_threshold: float = FLIP_THRESHOLD,
    ):
        self.target_pair_cost = target_pair_cost
        self.emergency_pair_cost = emergency_pair_cost
        self.size = size
        self.wait_seconds = wait_seconds
        self.order_timeout = order_timeout
        self.flip_threshold = flip_threshold

        self.state = SimpleHedgerState()

    def _get_bid(self, side: str, up_bid: float, down_bid: float) -> float:
        """Get bid price for a side."""
        return up_bid if side == "UP" else down_bid

    def _opposite_side(self, side: str) -> str:
        """Get opposite side."""
        return "DOWN" if side == "UP" else "UP"

    def decide(
        self,
        time_in_market: float,
        up_bid: float,
        up_ask: float,
        down_bid: float,
        down_ask: float,
        current_time: float,
    ) -> Optional[Tuple[str, float, int]]:
        """
        Main decision function. Call every tick.

        Returns:
            (side, price, size) to place order, or None if no action
        """
        s = self.state

        # =====================================================================
        # Phase: WAITING (first 3 seconds)
        # =====================================================================
        if s.phase == Phase.WAITING:
            if time_in_market < self.wait_seconds:
                return None

            # Capture initial prices for flip detection
            s.initial_up_bid = up_bid
            s.initial_down_bid = down_bid

            # Expensive side = higher bid
            if up_bid > down_bid:
                s.current_target_side = "UP"
            else:
                s.current_target_side = "DOWN"

            s.current_target_size = self.size
            s.phase = Phase.POSTING_FIRST

        # =====================================================================
        # Phase: POSTING_FIRST (post on expensive side)
        # =====================================================================
        if s.phase == Phase.POSTING_FIRST:
            price = self._get_bid(s.current_target_side, up_bid, down_bid)
            s.phase = Phase.WAITING_FIRST_FILL
            s.order_placed_at = current_time
            return (s.current_target_side, price, s.current_target_size)

        # =====================================================================
        # Phase: WAITING_FIRST_FILL
        # =====================================================================
        if s.phase == Phase.WAITING_FIRST_FILL:
            elapsed = current_time - s.order_placed_at
            if elapsed >= self.order_timeout:
                s.phase = Phase.POSTING_FIRST
                return None  # Caller cancels, we repost next tick
            return None

        # =====================================================================
        # Phase: POSTING_HEDGE (normal flow - no flip yet)
        # =====================================================================
        if s.phase == Phase.POSTING_HEDGE:
            hedge_side = self._opposite_side(s.first_fill_side)
            max_price = self.target_pair_cost - s.first_fill_price

            s.current_target_side = hedge_side
            s.current_target_size = self.size
            s.phase = Phase.WAITING_HEDGE_FILL
            s.order_placed_at = current_time
            return (hedge_side, max_price, s.current_target_size)

        # =====================================================================
        # Phase: WAITING_HEDGE_FILL (check for flip)
        # =====================================================================
        if s.phase == Phase.WAITING_HEDGE_FILL:
            elapsed = current_time - s.order_placed_at
            if elapsed >= self.order_timeout:
                s.phase = Phase.POSTING_HEDGE
                return None

            # Check flip condition: hedge side ran +20c from initial
            hedge_side = s.current_target_side
            initial_bid = s.initial_down_bid if hedge_side == "DOWN" else s.initial_up_bid
            current_bid = self._get_bid(hedge_side, up_bid, down_bid)

            if current_bid >= initial_bid + self.flip_threshold:
                # FLIP! Hedge side ran away
                # - Cancel current order (caller handles)
                # - Buy 2x on the side that ran (new expensive)
                # - Then buy remaining shares on old expensive (now cheap)
                s.flipped = True
                s.flip_count += 1
                s.current_target_side = hedge_side  # Side that ran = new target
                s.current_target_size = self.size * 2  # 2x size
                s.initial_up_bid = up_bid  # Reset for next flip detection
                s.initial_down_bid = down_bid
                s.phase = Phase.POSTING_FLIP
                return None  # Caller cancels, we post flip order next tick

            return None

        # =====================================================================
        # Phase: POSTING_FLIP (post 2x on side that ran away)
        # =====================================================================
        if s.phase == Phase.POSTING_FLIP:
            price = self._get_bid(s.current_target_side, up_bid, down_bid)
            s.phase = Phase.WAITING_FLIP_FILL
            s.order_placed_at = current_time
            return (s.current_target_side, price, s.current_target_size)

        # =====================================================================
        # Phase: WAITING_FLIP_FILL
        # =====================================================================
        if s.phase == Phase.WAITING_FLIP_FILL:
            elapsed = current_time - s.order_placed_at
            if elapsed >= self.order_timeout:
                s.phase = Phase.POSTING_FLIP
                return None
            return None

        # =====================================================================
        # Phase: POSTING_FINAL (post remaining shares to complete hedge)
        # =====================================================================
        if s.phase == Phase.POSTING_FINAL:
            # After flip: we have first_fill + flip_fill
            # Need remaining shares on first_fill_side to complete position
            #
            # Example: 5 UP @ $0.49 + 10 DOWN @ $0.70
            # Need 5 more UP, target 10 pairs @ $0.98
            # max_price = (0.98 * 10 - 2.45 - 7.00) / 5 = $0.07

            total_pairs = int(s.flip_fill_size)  # 10 pairs
            first_cost = s.first_fill_price * s.first_fill_size
            flip_cost = s.flip_fill_price * s.flip_fill_size
            remaining_shares = int(total_pairs - s.first_fill_size)

            remaining_budget = (self.emergency_pair_cost * total_pairs) - first_cost - flip_cost
            max_price = remaining_budget / remaining_shares if remaining_shares > 0 else 0

            # Polymarket constraint: min $1 order value
            # With SIZE=10, final hedge is 10 shares which meets $1 at $0.10+
            # If somehow we can't meet $1, the execution layer will reject
            if max_price <= 0:
                max_price = 0.01  # Fallback, will likely be rejected

            s.current_target_side = s.first_fill_side  # Same side as first fill
            s.current_target_size = remaining_shares
            s.phase = Phase.WAITING_FINAL_FILL
            s.order_placed_at = current_time
            return (s.current_target_side, max_price, s.current_target_size)

        # =====================================================================
        # Phase: WAITING_FINAL_FILL
        # =====================================================================
        if s.phase == Phase.WAITING_FINAL_FILL:
            elapsed = current_time - s.order_placed_at
            if elapsed >= self.order_timeout:
                s.phase = Phase.POSTING_FINAL
                return None
            return None

        # Phase: DONE
        return None

    def on_fill(self, side: str, price: float, size: float) -> None:
        """Call when an order fills."""
        s = self.state

        if s.phase == Phase.WAITING_FIRST_FILL and side == s.current_target_side:
            s.first_fill_side = side
            s.first_fill_price = price
            s.first_fill_size = size
            s.phase = Phase.POSTING_HEDGE

        elif s.phase == Phase.WAITING_HEDGE_FILL and side == s.current_target_side:
            s.hedge_fill_price = price
            s.hedge_fill_size = size
            s.phase = Phase.DONE

        elif s.phase == Phase.WAITING_FLIP_FILL and side == s.current_target_side:
            s.flip_fill_side = side
            s.flip_fill_price = price
            s.flip_fill_size = size
            s.phase = Phase.POSTING_FINAL

        elif s.phase == Phase.WAITING_FINAL_FILL and side == s.current_target_side:
            s.hedge_fill_price = price
            s.hedge_fill_size = size
            s.phase = Phase.DONE

    def should_cancel_pending(self, current_time: float) -> bool:
        """Check if pending order should be cancelled (timeout or flip)."""
        s = self.state
        waiting_phases = (
            Phase.WAITING_FIRST_FILL,
            Phase.WAITING_HEDGE_FILL,
            Phase.WAITING_FLIP_FILL,
            Phase.WAITING_FINAL_FILL,
        )
        if s.phase in waiting_phases:
            elapsed = current_time - s.order_placed_at
            return elapsed >= self.order_timeout
        return False

    def should_cancel_for_flip(self) -> bool:
        """Check if we just triggered a flip and need to cancel."""
        return self.state.phase == Phase.POSTING_FLIP

    def get_pair_cost(self) -> Optional[float]:
        """Get average pair cost if position is complete."""
        s = self.state
        if s.phase != Phase.DONE:
            return None

        if s.flipped:
            # After flip: (first + flip + final) / num_pairs
            total_cost = s.get_total_cost()
            num_pairs = s.flip_fill_size  # e.g., 10 pairs
            return total_cost / num_pairs if num_pairs > 0 else None
        else:
            # Normal: first + hedge
            return s.first_fill_price + s.hedge_fill_price

    def is_done(self) -> bool:
        """Check if strategy completed."""
        return self.state.phase == Phase.DONE

    def get_status(self) -> str:
        """Get human-readable status."""
        s = self.state

        if s.flipped:
            return (
                f"Phase: {s.phase.value} | "
                f"First: {s.first_fill_side} {s.first_fill_size:.0f}@${s.first_fill_price:.2f} | "
                f"Flip: {s.flip_fill_side} {s.flip_fill_size:.0f}@${s.flip_fill_price:.2f} | "
                f"Final: {s.first_fill_side} @${s.hedge_fill_price:.2f} | "
                f"Flips: {s.flip_count}"
            )
        else:
            hedge_side = "DOWN" if s.first_fill_side == "UP" else "UP"
            return (
                f"Phase: {s.phase.value} | "
                f"First: {s.first_fill_side} @ ${s.first_fill_price:.2f} | "
                f"Hedge: {hedge_side} @ ${s.hedge_fill_price:.2f} | "
                f"Flips: {s.flip_count}"
            )

    def reset(self) -> None:
        """Reset strategy for new market."""
        self.state = SimpleHedgerState()
