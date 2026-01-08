"""
Spread Capture Strategy

Z-score based spread capture for Polymarket BTC 15-minute markets.
Dynamically adjusts entry/hedge offsets based on trend strength.

Key Features:
- Z-score tier-based pricing (strong z>2, slight 1-2, neutral <1)
- Live z-score recalculation (offsets adjust as z changes)
- Entry at best_bid - offset, hedge at best_bid - hedge_offset
- Profit ceiling enforcement on hedge orders
- State machine for order lifecycle
- Defers to emergency logic when imbalance exceeds threshold

Usage:
    from src.strategies.spread_capture import SpreadCaptureStrategy

    strategy = SpreadCaptureStrategy(entry_size=5, min_profit=0.005)

    # In trading loop:
    action = strategy.decide(
        up_bid=0.55, up_ask=0.56,
        down_bid=0.44, down_ask=0.45,
        z_score=2.5,
        trend_direction="UP",
        time_remaining=600,
        current_imbalance=5,
        current_time=time.time()
    )

    if action:
        side, price, size = action
        # Place order via LiveTradingEngine

    # On fill callback:
    strategy.on_fill(side="UP", price=0.55, size=5)
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

# Z-score tier boundaries
Z_STRONG_THRESHOLD = 2.0    # Strong trend
Z_SLIGHT_THRESHOLD = 1.0    # Slight trend
# z < 1.0 = Neutral

# Default offsets per tier (from best_bid)
DEFAULT_ENTRY_OFFSETS = {
    "strong": 0.00,    # At best bid (aggressive)
    "slight": 0.01,    # Best bid - 0.01
    "neutral": 0.01,   # Best bid - 0.01
}

DEFAULT_HEDGE_OFFSETS = {
    "strong": 0.03,    # Best bid - 0.03 (0.06 spread target)
    "slight": 0.02,    # Best bid - 0.02
    "neutral": 0.01,   # Best bid - 0.01
}

# Wait times per tier (seconds)
DEFAULT_WAIT_TIMES = {
    "strong": 5.0,
    "slight": 10.0,
    "neutral": 10.0,
}

# Retry configuration
DEFAULT_MAX_ENTRY_RETRIES = 3
DEFAULT_MAX_HEDGE_RETRIES = 10  # More patience for hedge
RETRY_ESCALATION_FACTOR = 1.5  # Each retry multiplies wait time
MAX_WAIT_TIME = 60.0  # Cap wait time

# Polymarket constraints
MIN_SHARES = 5
DEFAULT_ENTRY_SIZE = 5
DEFAULT_TARGET_SHARES = 15
DEFAULT_MIN_PROFIT = 0.005
DEFAULT_MAX_SHARE_PRICE = 0.95
DEFAULT_EMERGENCY_IMBALANCE = 10


# =============================================================================
# ENUMS
# =============================================================================

class SpreadCapturePhase(Enum):
    """Order lifecycle phases for SpreadCaptureStrategy."""
    IDLE = "idle"                           # No active orders
    ENTRY_PENDING = "entry_pending"         # Entry order placed, awaiting fill
    ENTRY_FILLED = "entry_filled"           # Entry filled, preparing hedge
    HEDGE_PENDING = "hedge_pending"         # Hedge order placed, awaiting fill
    HEDGE_REPRICING = "hedge_repricing"     # Hedge didn't fill, repricing
    HEDGE_AT_CEILING = "hedge_at_ceiling"   # Hedge at max price, waiting
    COMPLETE = "complete"                   # Both sides filled
    ABORTED = "aborted"                     # Entry failed max retries
    EMERGENCY_DEFERRED = "emergency"        # Deferred to emergency logic


# =============================================================================
# STATE DATACLASS
# =============================================================================

@dataclass
class SpreadCaptureState:
    """State tracking for one entry+hedge cycle."""
    phase: SpreadCapturePhase = SpreadCapturePhase.IDLE

    # Entry order tracking
    entry_side: str = ""                    # "UP" or "DOWN"
    entry_price: float = 0.0
    entry_size: int = 0
    entry_placed_at: float = 0.0            # timestamp
    entry_retry_count: int = 0
    entry_z_score_at_place: float = 0.0
    entry_tier_at_place: str = ""           # "strong", "slight", "neutral"

    # Entry fill info
    entry_fill_price: float = 0.0
    entry_fill_size: int = 0
    entry_fill_time: float = 0.0

    # Hedge order tracking
    hedge_side: str = ""                    # Opposite of entry_side
    hedge_price: float = 0.0
    hedge_size: int = 0
    hedge_placed_at: float = 0.0
    hedge_retry_count: int = 0
    hedge_z_score_at_place: float = 0.0

    # Hedge fill info
    hedge_fill_price: float = 0.0
    hedge_fill_size: int = 0
    hedge_fill_time: float = 0.0

    # Profit tracking
    target_spread: float = 0.0              # Expected spread capture
    actual_pair_cost: float = 0.0           # Entry fill + hedge fill

    # Abort/emergency tracking
    abort_reason: str = ""
    deferred_to_emergency: bool = False

    # Cycle tracking (for multiple entry cycles per market)
    cycles_completed: int = 0
    total_entry_fills: int = 0
    total_hedge_fills: int = 0


# =============================================================================
# STRATEGY CLASS
# =============================================================================

class SpreadCaptureStrategy:
    """
    Spread Capture Strategy for Polymarket BTC 15-minute markets.

    Core concept: Enter on favored side first based on z-score, then hedge
    at a wider spread. Live z-score recalculation adjusts offsets continuously.

    Z-Score Tiers:
        z >= 2.0 (strong): Entry at best_bid, hedge offset 0.03
        1.0 <= z < 2.0 (slight): Entry offset 0.01, hedge offset 0.02
        z < 1.0 (neutral): Entry offset 0.01, hedge offset 0.01

    Attributes:
        entry_size: Shares per entry order (default 5)
        target_shares: Total target per market (default 15, = 3 cycles)
        max_entry_retries: Abort entry after this many failures (default 3)
        min_profit: Minimum profit per pair - profit ceiling (default 0.005)
        max_share_price: Never buy above this (default 0.95)
        emergency_imbalance_threshold: Defer to emergency above this (default 10)
    """

    def __init__(
        self,
        entry_size: int = DEFAULT_ENTRY_SIZE,
        target_shares: int = DEFAULT_TARGET_SHARES,
        max_entry_retries: int = DEFAULT_MAX_ENTRY_RETRIES,
        max_hedge_retries: int = DEFAULT_MAX_HEDGE_RETRIES,
        min_profit: float = DEFAULT_MIN_PROFIT,
        max_share_price: float = DEFAULT_MAX_SHARE_PRICE,
        emergency_imbalance_threshold: int = DEFAULT_EMERGENCY_IMBALANCE,
        retry_escalation: float = RETRY_ESCALATION_FACTOR,
    ):
        self.entry_size = max(MIN_SHARES, entry_size)
        self.target_shares = target_shares
        self.max_entry_retries = max_entry_retries
        self.max_hedge_retries = max_hedge_retries
        self.min_profit = min_profit
        self.max_share_price = max_share_price
        self.emergency_imbalance_threshold = emergency_imbalance_threshold
        self.retry_escalation = retry_escalation

        self.state = SpreadCaptureState()

        # Track history for logging/analysis
        self._completed_cycles: List[Dict[str, Any]] = []

    # =========================================================================
    # FORMULA METHODS
    # =========================================================================

    def get_tier(self, z_score: float) -> str:
        """Get tier name from z-score magnitude."""
        abs_z = abs(z_score)
        if abs_z >= Z_STRONG_THRESHOLD:
            return "strong"
        elif abs_z >= Z_SLIGHT_THRESHOLD:
            return "slight"
        return "neutral"

    def is_z_favorable(
        self,
        entry_side: str,
        trend_direction: str,
        z_score: float
    ) -> bool:
        """
        Check if z-score is favorable for our entry side.

        "z in favour" means trend direction aligns with entry side.

        Args:
            entry_side: "UP" or "DOWN"
            trend_direction: From TrendDetector ("UP", "DOWN", "FLAT")
            z_score: Current z-score magnitude

        Returns:
            True if z is favorable for our entry side
        """
        if abs(z_score) < Z_SLIGHT_THRESHOLD:
            return False  # Neutral - neither favorable nor unfavorable

        # z is favorable when trend direction matches entry side
        return entry_side.upper() == trend_direction.upper()

    def calculate_entry_offset(
        self,
        z_score: float,
        is_z_favorable: bool
    ) -> float:
        """
        Calculate entry offset from best_bid based on z-score.

        When z is favorable, we can be more aggressive (lower offset).
        When z is unfavorable, we add extra patience.

        Args:
            z_score: Current absolute z-score
            is_z_favorable: True if trend aligns with entry side

        Returns:
            Offset to subtract from best_bid (0.00 = at best_bid)

        Formula:
            z >= 2.0: offset = 0.00 (at best_bid)
            1.0 <= z < 2.0: offset = 0.01 * (2.0 - z) linear interpolation
            z < 1.0: offset = 0.01

            If z is UNFAVORABLE, add extra 0.01 patience
        """
        abs_z = abs(z_score)

        if abs_z >= Z_STRONG_THRESHOLD:
            base_offset = 0.00
        elif abs_z >= Z_SLIGHT_THRESHOLD:
            # Linear interpolation: 0.00 at z=2, 0.01 at z=1
            t = (Z_STRONG_THRESHOLD - abs_z) / (Z_STRONG_THRESHOLD - Z_SLIGHT_THRESHOLD)
            base_offset = 0.01 * t
        else:
            base_offset = 0.01

        # If z is unfavorable, add extra patience
        if not is_z_favorable and abs_z >= Z_SLIGHT_THRESHOLD:
            base_offset += 0.01

        return round(base_offset, 4)

    def calculate_hedge_offset(
        self,
        z_score: float,
    ) -> float:
        """
        Calculate hedge offset from best_bid based on z-score.

        Spread target scales with trend strength.

        Args:
            z_score: Current absolute z-score

        Returns:
            Offset from best_bid for hedge order

        Formula:
            z >= 2.0: offset = 0.03 (targeting ~0.06 spread)
            1.0 <= z < 2.0: offset = 0.02 + 0.01*(z-1) linear 0.02->0.03
            z < 1.0: offset = 0.01 (minimal spread target)
        """
        abs_z = abs(z_score)

        if abs_z >= Z_STRONG_THRESHOLD:
            base_offset = 0.03
        elif abs_z >= Z_SLIGHT_THRESHOLD:
            # Linear interpolation: 0.02 at z=1, 0.03 at z=2
            t = (abs_z - Z_SLIGHT_THRESHOLD) / (Z_STRONG_THRESHOLD - Z_SLIGHT_THRESHOLD)
            base_offset = 0.02 + 0.01 * t
        else:
            base_offset = 0.01

        return round(base_offset, 4)

    def calculate_max_hedge_price(
        self,
        entry_fill_price: float,
    ) -> float:
        """
        Calculate maximum hedge price to maintain profitability.

        This is the PROFIT CEILING - hedge price must never exceed this.

        Args:
            entry_fill_price: What we paid for entry

        Returns:
            Maximum acceptable hedge price
        """
        return round(1.00 - entry_fill_price - self.min_profit, 4)

    def calculate_wait_time(
        self,
        z_score: float,
        attempt: int,
        is_entry: bool = True
    ) -> float:
        """
        Calculate wait time before next retry.

        Higher z-score = less patience (market moving in our favor)
        More attempts = more patience (exponential backoff)

        Args:
            z_score: Current z-score
            attempt: Current retry attempt (0 = first try)
            is_entry: True for entry phase, False for hedge phase

        Returns:
            Wait time in seconds
        """
        abs_z = abs(z_score)

        if abs_z >= Z_STRONG_THRESHOLD:
            base_wait = 5.0
        elif abs_z >= Z_SLIGHT_THRESHOLD:
            base_wait = 10.0
        else:
            base_wait = 10.0

        # Hedge gets more patience
        if not is_entry:
            base_wait *= 1.5

        # Exponential backoff
        wait = base_wait * (self.retry_escalation ** attempt)

        return min(wait, MAX_WAIT_TIME)

    # =========================================================================
    # MAIN DECISION METHOD
    # =========================================================================

    def decide(
        self,
        up_bid: float,
        up_ask: float,
        down_bid: float,
        down_ask: float,
        z_score: float,
        trend_direction: str,
        time_remaining: float,
        current_imbalance: int,
        current_time: float,
    ) -> Optional[Tuple[str, float, int]]:
        """
        Main decision function. Call every tick.

        Args:
            up_bid: Current UP best bid
            up_ask: Current UP best ask
            down_bid: Current DOWN best bid
            down_ask: Current DOWN best ask
            z_score: Current z-score magnitude (always positive)
            trend_direction: "UP", "DOWN", or "FLAT"
            time_remaining: Seconds until market resolution
            current_imbalance: abs(up_shares - down_shares)
            current_time: Current timestamp (time.time())

        Returns:
            (side, price, size) to place order, or None if no action
        """
        s = self.state

        # =================================================================
        # PRE-CHECK: Target reached
        # =================================================================
        if s.total_entry_fills >= self.target_shares:
            # Allow hedge phases to continue (entry reached target but hedge still needed)
            if s.phase not in (SpreadCapturePhase.ENTRY_FILLED,
                               SpreadCapturePhase.HEDGE_PENDING,
                               SpreadCapturePhase.HEDGE_REPRICING,
                               SpreadCapturePhase.HEDGE_AT_CEILING,
                               SpreadCapturePhase.COMPLETE):
                logger.info(f"[SPREADCAP] Target reached: {s.total_entry_fills}/{self.target_shares}")
                return None

        # =================================================================
        # PRE-CHECK: Emergency imbalance deferral
        # =================================================================
        if current_imbalance >= self.emergency_imbalance_threshold:
            if s.phase not in (SpreadCapturePhase.EMERGENCY_DEFERRED,
                               SpreadCapturePhase.COMPLETE):
                logger.warning(
                    f"[SPREADCAP] Deferring to emergency: imbalance {current_imbalance} "
                    f">= {self.emergency_imbalance_threshold}"
                )
                s.phase = SpreadCapturePhase.EMERGENCY_DEFERRED
                s.deferred_to_emergency = True
            return None  # Let emergency logic handle it

        # =================================================================
        # Phase: IDLE - Determine entry side
        # =================================================================
        if s.phase == SpreadCapturePhase.IDLE:
            return self._handle_idle(
                up_bid, up_ask, down_bid, down_ask,
                z_score, trend_direction, current_time
            )

        # =================================================================
        # Phase: ENTRY_PENDING - Wait for entry fill
        # =================================================================
        if s.phase == SpreadCapturePhase.ENTRY_PENDING:
            return self._handle_entry_pending(
                up_bid, up_ask, down_bid, down_ask,
                z_score, trend_direction, current_time
            )

        # =================================================================
        # Phase: ENTRY_FILLED - Prepare hedge
        # =================================================================
        if s.phase == SpreadCapturePhase.ENTRY_FILLED:
            return self._handle_entry_filled(
                up_bid, up_ask, down_bid, down_ask,
                z_score, current_time
            )

        # =================================================================
        # Phase: HEDGE_PENDING - Wait for hedge fill
        # =================================================================
        if s.phase == SpreadCapturePhase.HEDGE_PENDING:
            return self._handle_hedge_pending(
                up_bid, up_ask, down_bid, down_ask,
                z_score, current_time
            )

        # =================================================================
        # Phase: HEDGE_REPRICING - Reprice hedge at better price
        # =================================================================
        if s.phase == SpreadCapturePhase.HEDGE_REPRICING:
            return self._handle_hedge_repricing(
                up_bid, up_ask, down_bid, down_ask,
                z_score, current_time
            )

        # =================================================================
        # Phase: HEDGE_AT_CEILING - Wait at ceiling for fill or emergency
        # =================================================================
        if s.phase == SpreadCapturePhase.HEDGE_AT_CEILING:
            return self._handle_hedge_at_ceiling(
                up_bid, up_ask, down_bid, down_ask,
                z_score, current_time
            )

        return None

    # =========================================================================
    # PHASE HANDLERS
    # =========================================================================

    def _handle_idle(
        self,
        up_bid: float, up_ask: float,
        down_bid: float, down_ask: float,
        z_score: float, trend_direction: str,
        current_time: float
    ) -> Optional[Tuple[str, float, int]]:
        """
        IDLE phase: Determine which side to enter based on z-score.

        Entry side selection:
        - If z is favorable for UP (z>1 and trending UP), enter UP
        - If z is favorable for DOWN (z>1 and trending DOWN), enter DOWN
        - If neutral, enter the cheaper side
        """
        s = self.state
        abs_z = abs(z_score)

        # Determine entry side based on trend
        if abs_z >= Z_SLIGHT_THRESHOLD and trend_direction.upper() in ("UP", "DOWN"):
            # Follow the trend - enter on favored side
            s.entry_side = trend_direction.upper()
        else:
            # Neutral - enter cheaper side
            s.entry_side = "UP" if up_ask < down_ask else "DOWN"

        s.hedge_side = "DOWN" if s.entry_side == "UP" else "UP"

        # Calculate entry price
        is_favorable = self.is_z_favorable(s.entry_side, trend_direction, z_score)
        offset = self.calculate_entry_offset(z_score, is_favorable)

        best_bid = up_bid if s.entry_side == "UP" else down_bid
        entry_price = max(0.01, round(best_bid - offset, 4))

        # Price ceiling check
        if entry_price > self.max_share_price:
            logger.debug(
                f"[SPREADCAP] Skip entry: ${entry_price:.4f} > ceiling ${self.max_share_price}"
            )
            return None

        # Set state
        s.entry_price = entry_price
        s.entry_size = self.entry_size
        s.entry_placed_at = current_time
        s.entry_z_score_at_place = z_score
        s.entry_tier_at_place = self.get_tier(z_score)
        s.entry_retry_count = 0
        s.phase = SpreadCapturePhase.ENTRY_PENDING

        logger.info(
            f"[SPREADCAP] Entry: {s.entry_side} {s.entry_size} @ ${entry_price:.4f} | "
            f"z={z_score:.2f} tier={s.entry_tier_at_place} offset=${offset:.4f}"
        )

        return (s.entry_side, entry_price, s.entry_size)

    def _handle_entry_pending(
        self,
        up_bid: float, up_ask: float,
        down_bid: float, down_ask: float,
        z_score: float, trend_direction: str,
        current_time: float
    ) -> Optional[Tuple[str, float, int]]:
        """
        ENTRY_PENDING: Wait for fill, check for timeout/reprice.

        Key behaviors:
        1. Wait for configured time based on z-tier
        2. If timeout and z changed, recalculate price (LIVE z recalculation)
        3. If max retries exceeded, abort
        """
        s = self.state
        wait_time = self.calculate_wait_time(z_score, s.entry_retry_count, is_entry=True)
        elapsed = current_time - s.entry_placed_at

        if elapsed < wait_time:
            return None  # Still waiting

        # Timeout - check retry count
        s.entry_retry_count += 1
        if s.entry_retry_count > self.max_entry_retries:
            s.phase = SpreadCapturePhase.ABORTED
            s.abort_reason = f"Entry failed after {self.max_entry_retries} retries"
            logger.warning(f"[SPREADCAP] ABORT: {s.abort_reason}")
            return None

        # LIVE Z RECALCULATION: Price may need to adjust
        is_favorable = self.is_z_favorable(s.entry_side, trend_direction, z_score)
        new_offset = self.calculate_entry_offset(z_score, is_favorable)
        best_bid = up_bid if s.entry_side == "UP" else down_bid
        new_price = max(0.01, round(best_bid - new_offset, 4))

        # Only reprice if significantly different (>0.5c)
        if abs(new_price - s.entry_price) > 0.005:
            logger.info(
                f"[SPREADCAP] Entry reprice: ${s.entry_price:.4f} -> ${new_price:.4f} | "
                f"z={z_score:.2f} (was {s.entry_z_score_at_place:.2f}) retry={s.entry_retry_count}"
            )
            s.entry_price = new_price
            s.entry_z_score_at_place = z_score
        else:
            logger.info(
                f"[SPREADCAP] Entry retry {s.entry_retry_count}: {s.entry_side} @ ${s.entry_price:.4f}"
            )

        s.entry_placed_at = current_time
        return (s.entry_side, s.entry_price, s.entry_size)

    def _handle_entry_filled(
        self,
        up_bid: float, up_ask: float,
        down_bid: float, down_ask: float,
        z_score: float,
        current_time: float
    ) -> Optional[Tuple[str, float, int]]:
        """
        ENTRY_FILLED: Calculate and place hedge order.

        Key behaviors:
        1. Calculate hedge offset based on current z
        2. Apply profit ceiling (never exceed max hedge price)
        3. Place hedge as maker order
        """
        s = self.state

        # Calculate hedge price
        offset = self.calculate_hedge_offset(z_score)
        best_bid = down_bid if s.hedge_side == "DOWN" else up_bid

        hedge_price = max(0.01, round(best_bid - offset, 4))

        # Apply profit ceiling
        max_hedge_price = self.calculate_max_hedge_price(s.entry_fill_price)
        if hedge_price > max_hedge_price:
            logger.info(
                f"[SPREADCAP] Hedge capped: ${hedge_price:.4f} -> ${max_hedge_price:.4f} (profit ceiling)"
            )
            hedge_price = max_hedge_price

        # Calculate target spread
        s.target_spread = round(max_hedge_price - hedge_price + self.min_profit, 4)

        s.hedge_price = hedge_price
        s.hedge_size = s.entry_fill_size  # Match entry size
        s.hedge_placed_at = current_time
        s.hedge_z_score_at_place = z_score
        s.hedge_retry_count = 0
        s.phase = SpreadCapturePhase.HEDGE_PENDING

        logger.info(
            f"[SPREADCAP] Hedge: {s.hedge_side} {s.hedge_size} @ ${hedge_price:.4f} | "
            f"z={z_score:.2f} offset=${offset:.4f} max_hedge=${max_hedge_price:.4f}"
        )

        return (s.hedge_side, hedge_price, s.hedge_size)

    def _handle_hedge_pending(
        self,
        up_bid: float, up_ask: float,
        down_bid: float, down_ask: float,
        z_score: float,
        current_time: float
    ) -> Optional[Tuple[str, float, int]]:
        """
        HEDGE_PENDING: Wait for fill, reprice if timeout.

        Key behavior on non-fill: RE-QUEUE at better price (not market take).
        """
        s = self.state
        wait_time = self.calculate_wait_time(z_score, s.hedge_retry_count, is_entry=False)
        elapsed = current_time - s.hedge_placed_at

        if elapsed < wait_time:
            return None  # Still waiting

        # Timeout - transition to repricing
        s.hedge_retry_count += 1
        s.phase = SpreadCapturePhase.HEDGE_REPRICING

        logger.debug(f"[SPREADCAP] Hedge timeout, transitioning to reprice (retry {s.hedge_retry_count})")
        return None  # Next tick will handle repricing

    def _handle_hedge_repricing(
        self,
        up_bid: float, up_ask: float,
        down_bid: float, down_ask: float,
        z_score: float,
        current_time: float
    ) -> Optional[Tuple[str, float, int]]:
        """
        HEDGE_REPRICING: Calculate new hedge price and re-queue.

        Strategy: Improve by one tick ($0.01) but respect profit ceiling.
        """
        s = self.state

        # Improve price by one tick
        new_price = round(s.hedge_price + 0.01, 4)

        # But respect profit ceiling
        max_hedge_price = self.calculate_max_hedge_price(s.entry_fill_price)

        if new_price >= max_hedge_price:
            new_price = max_hedge_price
            s.phase = SpreadCapturePhase.HEDGE_AT_CEILING
            logger.info(
                f"[SPREADCAP] Hedge at ceiling: {s.hedge_side} @ ${new_price:.4f} "
                f"(retry {s.hedge_retry_count}) - waiting for fill or emergency"
            )
        else:
            s.phase = SpreadCapturePhase.HEDGE_PENDING
            logger.info(
                f"[SPREADCAP] Hedge reprice: {s.hedge_side} @ ${new_price:.4f} "
                f"(retry {s.hedge_retry_count})"
            )

        s.hedge_price = new_price
        s.hedge_placed_at = current_time

        return (s.hedge_side, new_price, s.hedge_size)

    def _handle_hedge_at_ceiling(
        self,
        up_bid: float, up_ask: float,
        down_bid: float, down_ask: float,
        z_score: float,
        current_time: float
    ) -> Optional[Tuple[str, float, int]]:
        """
        HEDGE_AT_CEILING: Hedge at max price, waiting for fill or emergency.

        At this point we just keep the order active.
        Emergency logic will take over if imbalance gets too high.
        """
        s = self.state

        # Check if we should refresh the order (every 30s)
        elapsed = current_time - s.hedge_placed_at
        if elapsed > 30.0:
            s.hedge_placed_at = current_time
            logger.debug(
                f"[SPREADCAP] Hedge at ceiling refresh: {s.hedge_side} @ ${s.hedge_price:.4f}"
            )
            return (s.hedge_side, s.hedge_price, s.hedge_size)

        return None

    # =========================================================================
    # FILL CALLBACKS
    # =========================================================================

    def on_fill(self, side: str, price: float, size: int) -> None:
        """
        Call when an order fills.

        Args:
            side: "UP" or "DOWN"
            price: Fill price
            size: Fill size
        """
        s = self.state
        side_upper = side.upper()

        if s.phase == SpreadCapturePhase.ENTRY_PENDING and side_upper == s.entry_side:
            s.entry_fill_price = price
            s.entry_fill_size = size
            s.entry_fill_time = time.time()
            s.total_entry_fills += size
            s.phase = SpreadCapturePhase.ENTRY_FILLED
            logger.info(
                f"[SPREADCAP] Entry filled: {side_upper} {size} @ ${price:.4f} | "
                f"total={s.total_entry_fills}/{self.target_shares}"
            )

        elif s.phase in (SpreadCapturePhase.HEDGE_PENDING,
                         SpreadCapturePhase.HEDGE_REPRICING,
                         SpreadCapturePhase.HEDGE_AT_CEILING) and side_upper == s.hedge_side:
            s.hedge_fill_price = price
            s.hedge_fill_size = size
            s.hedge_fill_time = time.time()
            s.total_hedge_fills += size
            s.actual_pair_cost = round(s.entry_fill_price + s.hedge_fill_price, 4)
            s.cycles_completed += 1

            profit = round(1.00 - s.actual_pair_cost, 4)
            logger.info(
                f"[SPREADCAP] Hedge filled: {side_upper} {size} @ ${price:.4f} | "
                f"Pair cost: ${s.actual_pair_cost:.4f} | Profit: ${profit:.4f} | "
                f"Cycle {s.cycles_completed}"
            )

            # Record completed cycle
            self._completed_cycles.append({
                "cycle": s.cycles_completed,
                "entry_side": s.entry_side,
                "entry_price": s.entry_fill_price,
                "entry_size": s.entry_fill_size,
                "hedge_side": s.hedge_side,
                "hedge_price": s.hedge_fill_price,
                "hedge_size": s.hedge_fill_size,
                "pair_cost": s.actual_pair_cost,
                "profit": profit,
                "entry_retries": s.entry_retry_count,
                "hedge_retries": s.hedge_retry_count,
            })

            # Check if we should continue or complete
            if s.total_entry_fills >= self.target_shares:
                s.phase = SpreadCapturePhase.COMPLETE
                logger.info(
                    f"[SPREADCAP] COMPLETE: {s.cycles_completed} cycles, "
                    f"{s.total_entry_fills} shares filled"
                )
            else:
                # Reset for next cycle
                self._reset_for_next_cycle()

    def _reset_for_next_cycle(self) -> None:
        """Reset state for next entry+hedge cycle while preserving totals."""
        s = self.state

        # Preserve totals
        cycles = s.cycles_completed
        total_entry = s.total_entry_fills
        total_hedge = s.total_hedge_fills

        # Reset entry state
        s.entry_side = ""
        s.entry_price = 0.0
        s.entry_size = 0
        s.entry_placed_at = 0.0
        s.entry_retry_count = 0
        s.entry_z_score_at_place = 0.0
        s.entry_tier_at_place = ""
        s.entry_fill_price = 0.0
        s.entry_fill_size = 0
        s.entry_fill_time = 0.0

        # Reset hedge state
        s.hedge_side = ""
        s.hedge_price = 0.0
        s.hedge_size = 0
        s.hedge_placed_at = 0.0
        s.hedge_retry_count = 0
        s.hedge_z_score_at_place = 0.0
        s.hedge_fill_price = 0.0
        s.hedge_fill_size = 0
        s.hedge_fill_time = 0.0

        # Reset cycle state
        s.target_spread = 0.0
        s.actual_pair_cost = 0.0
        s.abort_reason = ""
        s.deferred_to_emergency = False

        # Restore totals
        s.cycles_completed = cycles
        s.total_entry_fills = total_entry
        s.total_hedge_fills = total_hedge

        # Back to idle
        s.phase = SpreadCapturePhase.IDLE

        logger.debug(f"[SPREADCAP] Reset for next cycle ({cycles + 1})")

    # =========================================================================
    # QUOTE PULLING
    # =========================================================================

    def should_pull_entry(
        self,
        z_score: float,
        trend_direction: str,
    ) -> bool:
        """
        Check if entry order should be pulled due to adverse z-score movement.

        Integrates with existing quote pulling in live_trading.py
        """
        s = self.state
        if s.phase != SpreadCapturePhase.ENTRY_PENDING:
            return False

        # Pull if z became strongly unfavorable
        if not self.is_z_favorable(s.entry_side, trend_direction, z_score):
            if abs(z_score) >= Z_STRONG_THRESHOLD:
                logger.info(
                    f"[SPREADCAP] Pull entry: z={z_score:.2f} strongly unfavorable for {s.entry_side}"
                )
                return True

        return False

    def on_entry_pulled(self) -> None:
        """Call when entry order is pulled. Increments retry count."""
        s = self.state
        if s.phase == SpreadCapturePhase.ENTRY_PENDING:
            s.entry_retry_count += 1
            logger.info(f"[SPREADCAP] Entry pulled, retry count now {s.entry_retry_count}")

    # =========================================================================
    # STATUS & RESET
    # =========================================================================

    def get_status(self) -> Dict[str, Any]:
        """Get current strategy status for logging/debugging."""
        s = self.state
        return {
            "phase": s.phase.value,
            "entry_side": s.entry_side,
            "entry_price": s.entry_price,
            "entry_retries": s.entry_retry_count,
            "hedge_side": s.hedge_side,
            "hedge_price": s.hedge_price,
            "hedge_retries": s.hedge_retry_count,
            "target_spread": s.target_spread,
            "cycles_completed": s.cycles_completed,
            "total_entry_fills": s.total_entry_fills,
            "total_hedge_fills": s.total_hedge_fills,
            "is_complete": s.phase == SpreadCapturePhase.COMPLETE,
            "is_aborted": s.phase == SpreadCapturePhase.ABORTED,
        }

    def get_completed_cycles(self) -> List[Dict[str, Any]]:
        """Get list of completed cycles with profit info."""
        return self._completed_cycles.copy()

    def reset(self) -> None:
        """Reset strategy for new market."""
        self.state = SpreadCaptureState()
        self._completed_cycles = []
        logger.debug("[SPREADCAP] Strategy reset for new market")

    def __repr__(self) -> str:
        return (
            f"SpreadCaptureStrategy("
            f"entry_size={self.entry_size}, "
            f"target_shares={self.target_shares}, "
            f"min_profit={self.min_profit})"
        )
