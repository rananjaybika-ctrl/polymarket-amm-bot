"""
Spread Capture Strategy

Velocity-based spread capture for Polymarket BTC 15-minute markets.
Uses fixed entry/hedge offsets with velocity-based quote pulling.

Key Features:
- Fixed entry offset (0.01 from best_bid)
- Fixed hedge offset (0.02 from best_bid)
- Velocity-based quote pulling (0.05 bps/sec threshold)
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

from src.config import FeeConfig

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS
# =============================================================================

# Fixed offsets (from best_bid) - simplified from z-score tiers
DEFAULT_ENTRY_OFFSET = 0.01    # Best bid - 0.01
DEFAULT_HEDGE_OFFSET = 0.02    # Best bid - 0.02 (target ~$0.03 spread)

# Wait times (seconds)
DEFAULT_ENTRY_WAIT = 8.0       # Time before repricing entry
DEFAULT_HEDGE_WAIT = 5.0       # Time before repricing hedge

# Velocity threshold for quote pulling (bps/sec)
VELOCITY_PULL_THRESHOLD = 0.05  # ~$5 BTC move in 10s

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
    # Entry fill info
    entry_fill_price: float = 0.0
    entry_fill_size: int = 0
    entry_fill_time: float = 0.0
    entry_order_type: str = "maker"  # "maker" or "taker" - for fee/rebate calculation

    # Hedge order tracking
    hedge_side: str = ""                    # Opposite of entry_side
    hedge_price: float = 0.0
    hedge_size: int = 0
    hedge_placed_at: float = 0.0
    hedge_retry_count: int = 0

    # Hedge fill info
    hedge_fill_price: float = 0.0
    hedge_fill_size: int = 0
    hedge_fill_time: float = 0.0
    hedge_order_type: str = "maker"  # "maker" or "taker" - for fee/rebate calculation

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

    Core concept: Enter expensive side first, then hedge at target spread.
    Uses velocity-based quote pulling for adverse move protection.

    Fixed Offsets:
        Entry: best_bid - 0.01
        Hedge: best_bid - 0.02 (target ~$0.03 spread)

    Velocity Pulling:
        Pull entry if velocity > 0.05 bps/sec adverse

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
        speed_mode: bool = False,
    ):
        self.entry_size = max(MIN_SHARES, entry_size)
        self.target_shares = target_shares
        self.max_entry_retries = max_entry_retries
        self.max_hedge_retries = max_hedge_retries
        self.min_profit = min_profit
        self.max_share_price = max_share_price
        self.emergency_imbalance_threshold = emergency_imbalance_threshold
        self.retry_escalation = retry_escalation

        # SPEED MODE: 10x faster wait times for aggressive latency-sensitive trading
        # Normal mode: Entry 5-30s, Hedge 3-45s
        # Speed mode:  Entry 0.5-3s, Hedge 0.3-5s
        # Use when trying to capture fleeting opportunities before MMs react
        self.speed_mode = speed_mode

        self.state = SpreadCaptureState()

        # Track history for logging/analysis
        self._completed_cycles: List[Dict[str, Any]] = []

    # =========================================================================
    # FORMULA METHODS
    # =========================================================================

    def calculate_entry_offset(self) -> float:
        """
        Calculate entry offset from best_bid.

        Returns:
            Fixed offset of 0.01 (entry at best_bid - 0.01)
        """
        return DEFAULT_ENTRY_OFFSET

    def calculate_hedge_offset(self) -> float:
        """
        Calculate hedge offset from best_bid.

        Returns:
            Fixed offset of 0.02 (hedge at best_bid - 0.02)
        """
        return DEFAULT_HEDGE_OFFSET

    def calculate_max_hedge_price(
        self,
        entry_fill_price: float,
        hedge_is_taker: bool = False,
    ) -> float:
        """
        Calculate maximum hedge price to maintain profitability.

        This is the PROFIT CEILING - hedge price must never exceed this.
        Now accounts for maker rebates and taker fees:
        - Maker-maker: Can accept higher pair cost due to ~2% total rebates
        - Maker-taker: Must be stricter due to ~1.56% taker fee

        Args:
            entry_fill_price: What we paid for entry (assumed maker)
            hedge_is_taker: True if hedge will be taker order (emergency)

        Returns:
            Maximum acceptable hedge price
        """
        if hedge_is_taker:
            # Use FeeConfig's taker-aware calculation
            return FeeConfig.get_max_taker_hedge_price(
                entry_price=entry_fill_price,
                min_profit=self.min_profit,
            )
        else:
            # Maker-maker: Account for rebates on actual prices
            # profit = 1.00 - pair_cost + (pair_cost × rebate_rate)
            #        = 1.00 - pair_cost × (1 - rebate_rate)
            #        = 1.00 - pair_cost × 0.99
            #
            # For profit >= min_profit:
            #   pair_cost <= (1.00 - min_profit) / 0.99
            #
            # With min_profit = 0.005: max pair_cost ≈ $1.005
            # So max_hedge = max_pair_cost - entry_fill_price
            effective_rate = 1.0 - FeeConfig.MAKER_REBATE_RATE  # 0.99
            max_pair_cost = (1.00 - self.min_profit) / effective_rate
            return round(max_pair_cost - entry_fill_price, 4)

    def calculate_wait_time(
        self,
        attempt: int,
        is_entry: bool = True,
        price_room: Optional[float] = None,
        velocity_bps: Optional[float] = None,
        hedge_side: Optional[str] = None,
    ) -> float:
        """
        Calculate wait time before next retry.

        ENTRY: Fixed base wait with exponential backoff
        HEDGE: Price-room based with velocity adjustment (no backoff)

        SPEED MODE: 10x faster wait times for aggressive latency-sensitive trading

        Args:
            attempt: Current retry attempt (0 = first try)
            is_entry: True for entry phase, False for hedge phase
            price_room: Distance from current price to max price (ceiling)
            velocity_bps: Current velocity in basis points per second
            hedge_side: "UP" or "DOWN" - which side we're hedging

        Returns:
            Wait time in seconds
        """
        # Speed multiplier: 10x faster in speed mode
        speed_mult = 0.1 if self.speed_mode else 1.0

        # HEDGE PHASE: Price-room based with velocity adjustment (no backoff!)
        if not is_entry and price_room is not None:
            # Base wait proportional to price room
            base_wait = max(0.3 if self.speed_mode else 3.0, price_room * 300 * speed_mult)

            # Velocity adjustment (if available)
            if velocity_bps is not None and hedge_side:
                # Determine if velocity is adverse for our hedge side
                if hedge_side.upper() == "DOWN":
                    if velocity_bps > 0.05:   # Adverse: BTC rising
                        base_wait *= 0.5
                    elif velocity_bps < -0.05:  # Favorable: BTC falling
                        base_wait *= 1.5
                else:  # hedge_side == "UP"
                    if velocity_bps < -0.05:  # Adverse: BTC falling
                        base_wait *= 0.5
                    elif velocity_bps > 0.05:  # Favorable: BTC rising
                        base_wait *= 1.5

            # Cap: Normal 3-45s, Speed 0.3-5s
            max_wait = 5.0 if self.speed_mode else 45.0
            min_wait = 0.3 if self.speed_mode else 3.0
            return min(max(min_wait, base_wait), max_wait)

        # ENTRY PHASE: Fixed base wait with exponential backoff
        base_wait = 0.8 if self.speed_mode else DEFAULT_ENTRY_WAIT

        # Exponential backoff for entry (want to abort if market not favorable)
        wait = base_wait * (1.3 ** attempt)

        # Cap: Normal 30s, Speed 3s
        max_entry_wait = 3.0 if self.speed_mode else 30.0
        return min(wait, max_entry_wait)

    # =========================================================================
    # MAIN DECISION METHOD
    # =========================================================================

    def decide(
        self,
        up_bid: float,
        up_ask: float,
        down_bid: float,
        down_ask: float,
        time_remaining: float,
        current_imbalance: int,
        current_time: float,
        velocity_bps: Optional[float] = None,
    ) -> Optional[Tuple[str, float, int]]:
        """
        Main decision function. Call every tick.

        Args:
            up_bid: Current UP best bid
            up_ask: Current UP best ask
            down_bid: Current DOWN best bid
            down_ask: Current DOWN best ask
            time_remaining: Seconds until market resolution
            current_imbalance: abs(up_shares - down_shares)
            current_time: Current timestamp (time.time())
            velocity_bps: Current velocity in bps/sec (for hedge timing)

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
        # Exit emergency deferral if imbalance resolved
        if s.phase == SpreadCapturePhase.EMERGENCY_DEFERRED:
            if current_imbalance < self.emergency_imbalance_threshold:
                s.phase = SpreadCapturePhase.IDLE
                s.deferred_to_emergency = False
                logger.info(
                    f"[SPREADCAP] Exiting emergency deferral: imbalance {current_imbalance} "
                    f"< {self.emergency_imbalance_threshold} - returning to IDLE"
                )
            else:
                return None  # Still in emergency, let emergency logic handle it

        # Enter emergency deferral if imbalance too high
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
                up_bid, up_ask, down_bid, down_ask, current_time
            )

        # =================================================================
        # Phase: ENTRY_PENDING - Wait for entry fill
        # =================================================================
        if s.phase == SpreadCapturePhase.ENTRY_PENDING:
            return self._handle_entry_pending(
                up_bid, up_ask, down_bid, down_ask, current_time
            )

        # =================================================================
        # Phase: ENTRY_FILLED - Prepare hedge
        # =================================================================
        if s.phase == SpreadCapturePhase.ENTRY_FILLED:
            return self._handle_entry_filled(
                up_bid, up_ask, down_bid, down_ask, current_time
            )

        # =================================================================
        # Phase: HEDGE_PENDING - Wait for hedge fill
        # =================================================================
        if s.phase == SpreadCapturePhase.HEDGE_PENDING:
            return self._handle_hedge_pending(
                up_bid, up_ask, down_bid, down_ask,
                current_time, velocity_bps
            )

        # =================================================================
        # Phase: HEDGE_REPRICING - Reprice hedge at better price
        # =================================================================
        if s.phase == SpreadCapturePhase.HEDGE_REPRICING:
            return self._handle_hedge_repricing(
                up_bid, up_ask, down_bid, down_ask, current_time
            )

        # =================================================================
        # Phase: HEDGE_AT_CEILING - Wait at ceiling for fill or emergency
        # =================================================================
        if s.phase == SpreadCapturePhase.HEDGE_AT_CEILING:
            return self._handle_hedge_at_ceiling(
                up_bid, up_ask, down_bid, down_ask, current_time
            )

        return None

    # =========================================================================
    # PHASE HANDLERS
    # =========================================================================

    def _handle_idle(
        self,
        up_bid: float, up_ask: float,
        down_bid: float, down_ask: float,
        current_time: float
    ) -> Optional[Tuple[str, float, int]]:
        """
        IDLE phase: Determine which side to enter.

        Entry side selection:
        - ALWAYS enter the EXPENSIVE side first (higher ask price)
        - This minimizes leg risk: if expensive side fills, cheap side is easy to hedge
        - If cheap side filled first and price moves, expensive side becomes harder to fill
        """
        s = self.state

        # ALWAYS enter EXPENSIVE side first (higher ask = harder to fill)
        # This is critical for spread capture risk management
        s.entry_side = "UP" if up_ask > down_ask else "DOWN"

        s.hedge_side = "DOWN" if s.entry_side == "UP" else "UP"

        # Calculate entry price (fixed offset)
        offset = self.calculate_entry_offset()

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
        s.entry_retry_count = 0
        s.phase = SpreadCapturePhase.ENTRY_PENDING

        logger.info(
            f"[SPREADCAP] Entry: {s.entry_side} {s.entry_size} @ ${entry_price:.4f} | "
            f"offset=${offset:.4f}"
        )

        return (s.entry_side, entry_price, s.entry_size)

    def _handle_entry_pending(
        self,
        up_bid: float, up_ask: float,
        down_bid: float, down_ask: float,
        current_time: float
    ) -> Optional[Tuple[str, float, int]]:
        """
        ENTRY_PENDING: Wait for fill, check for timeout/reprice.

        Key behaviors:
        1. Wait for configured time
        2. If timeout, reprice at current best_bid - offset
        3. If max retries exceeded, abort
        """
        s = self.state
        # Calculate price room for price-dependent wait time
        price_room = self.max_share_price - s.entry_price if s.entry_price > 0 else None
        wait_time = self.calculate_wait_time(s.entry_retry_count, is_entry=True, price_room=price_room)
        elapsed = current_time - s.entry_placed_at

        if elapsed < wait_time:
            return None  # Still waiting

        # Timeout - check retry count
        s.entry_retry_count += 1
        if s.entry_retry_count > self.max_entry_retries:
            # If we have partial fills, hedge what we got instead of aborting
            if s.entry_fill_size > 0:
                s.phase = SpreadCapturePhase.ENTRY_FILLED
                logger.info(
                    f"[SPREADCAP] Max retries exceeded but have partial fills: "
                    f"{s.entry_fill_size}/{s.entry_size} @ ${s.entry_fill_price:.4f} - proceeding to hedge"
                )
                return None  # Next tick will handle ENTRY_FILLED -> place hedge
            else:
                s.phase = SpreadCapturePhase.ABORTED
                s.abort_reason = f"Entry failed after {self.max_entry_retries} retries (no fills)"
                logger.warning(f"[SPREADCAP] ABORT: {s.abort_reason}")
                return None

        # Reprice at current best_bid - offset
        new_offset = self.calculate_entry_offset()
        best_bid = up_bid if s.entry_side == "UP" else down_bid
        new_price = max(0.01, round(best_bid - new_offset, 4))

        # Only reprice if significantly different (>0.5c)
        if abs(new_price - s.entry_price) > 0.005:
            logger.info(
                f"[SPREADCAP] Entry reprice: ${s.entry_price:.4f} -> ${new_price:.4f} | "
                f"retry={s.entry_retry_count}"
            )
            s.entry_price = new_price
        else:
            logger.info(
                f"[SPREADCAP] Entry retry {s.entry_retry_count}: {s.entry_side} @ ${s.entry_price:.4f}"
            )

        s.entry_placed_at = current_time
        # Only order remaining unfilled shares (not full entry_size)
        remaining_size = s.entry_size - s.entry_fill_size
        if remaining_size <= 0:
            # All filled - shouldn't happen, but just in case
            s.phase = SpreadCapturePhase.ENTRY_FILLED
            return None
        return (s.entry_side, s.entry_price, remaining_size)

    def _handle_entry_filled(
        self,
        up_bid: float, up_ask: float,
        down_bid: float, down_ask: float,
        current_time: float
    ) -> Optional[Tuple[str, float, int]]:
        """
        ENTRY_FILLED: Calculate and place hedge order.

        Key behaviors:
        1. Calculate hedge offset (fixed)
        2. Apply profit ceiling (never exceed max hedge price)
        3. Place hedge as maker order
        """
        s = self.state

        # Calculate hedge price (fixed offset)
        offset = self.calculate_hedge_offset()
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
        s.hedge_retry_count = 0
        s.phase = SpreadCapturePhase.HEDGE_PENDING

        logger.info(
            f"[SPREADCAP] Hedge: {s.hedge_side} {s.hedge_size} @ ${hedge_price:.4f} | "
            f"offset=${offset:.4f} max_hedge=${max_hedge_price:.4f}"
        )

        return (s.hedge_side, hedge_price, s.hedge_size)

    def _handle_hedge_pending(
        self,
        up_bid: float, up_ask: float,
        down_bid: float, down_ask: float,
        current_time: float,
        velocity_bps: Optional[float] = None
    ) -> Optional[Tuple[str, float, int]]:
        """
        HEDGE_PENDING: Wait for fill, reprice if timeout.

        Key behavior on non-fill: RE-QUEUE at better price (not market take).
        Uses price-room based wait time with velocity adjustment.
        """
        s = self.state
        # Calculate price room for price-dependent wait time
        max_hedge_price = self.calculate_max_hedge_price(s.entry_fill_price) if s.entry_fill_price > 0 else 0.99
        price_room = max_hedge_price - s.hedge_price if s.hedge_price > 0 else None
        wait_time = self.calculate_wait_time(
            s.hedge_retry_count, is_entry=False,
            price_room=price_room, velocity_bps=velocity_bps, hedge_side=s.hedge_side
        )
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

        # Only order remaining unfilled shares
        remaining_size = s.hedge_size - s.hedge_fill_size
        if remaining_size <= 0:
            # All filled - shouldn't happen, but transition to complete
            s.phase = SpreadCapturePhase.COMPLETE
            return None
        return (s.hedge_side, new_price, remaining_size)

    def _handle_hedge_at_ceiling(
        self,
        up_bid: float, up_ask: float,
        down_bid: float, down_ask: float,
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
            # Only order remaining unfilled shares
            remaining_size = s.hedge_size - s.hedge_fill_size
            if remaining_size <= 0:
                s.phase = SpreadCapturePhase.COMPLETE
                return None
            logger.debug(
                f"[SPREADCAP] Hedge at ceiling refresh: {s.hedge_side} @ ${s.hedge_price:.4f} "
                f"remaining={remaining_size}/{s.hedge_size}"
            )
            return (s.hedge_side, s.hedge_price, remaining_size)

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
            # Accumulate partial fills with weighted average price
            old_cost = s.entry_fill_price * s.entry_fill_size
            new_cost = price * size
            s.entry_fill_size += size  # Accumulate, not overwrite
            s.entry_fill_price = round((old_cost + new_cost) / s.entry_fill_size, 4)
            s.entry_fill_time = time.time()
            s.total_entry_fills += size

            # Only transition to ENTRY_FILLED when full size filled
            if s.entry_fill_size >= s.entry_size:
                s.phase = SpreadCapturePhase.ENTRY_FILLED
                logger.info(
                    f"[SPREADCAP] Entry FULLY filled: {side_upper} {s.entry_fill_size} @ ${s.entry_fill_price:.4f} | "
                    f"total={s.total_entry_fills}/{self.target_shares}"
                )
            else:
                logger.info(
                    f"[SPREADCAP] Entry PARTIAL fill: {side_upper} +{size} @ ${price:.4f} | "
                    f"filled={s.entry_fill_size}/{s.entry_size} avg=${s.entry_fill_price:.4f}"
                )

        elif s.phase in (SpreadCapturePhase.HEDGE_PENDING,
                         SpreadCapturePhase.HEDGE_REPRICING,
                         SpreadCapturePhase.HEDGE_AT_CEILING) and side_upper == s.hedge_side:
            # Accumulate partial fills with weighted average price
            old_cost = s.hedge_fill_price * s.hedge_fill_size
            new_cost = price * size
            s.hedge_fill_size += size  # Accumulate, not overwrite
            s.hedge_fill_price = round((old_cost + new_cost) / s.hedge_fill_size, 4)
            s.hedge_fill_time = time.time()
            s.total_hedge_fills += size

            # Only complete cycle when full hedge size filled
            if s.hedge_fill_size >= s.hedge_size:
                s.actual_pair_cost = round(s.entry_fill_price + s.hedge_fill_price, 4)
                s.cycles_completed += 1

                # Calculate profit including fees/rebates
                base_profit = round(1.00 - s.actual_pair_cost, 4)
                net_profit = FeeConfig.calculate_net_profit(
                    entry_price=s.entry_fill_price,
                    hedge_price=s.hedge_fill_price,
                    size=s.entry_fill_size,
                    entry_is_maker=(s.entry_order_type == "maker"),
                    hedge_is_maker=(s.hedge_order_type == "maker"),
                )
                net_profit = round(net_profit, 4)

                logger.info(
                    f"[SPREADCAP] Hedge FULLY filled: {side_upper} {s.hedge_fill_size} @ ${s.hedge_fill_price:.4f} | "
                    f"Pair cost: ${s.actual_pair_cost:.4f} | "
                    f"Base profit: ${base_profit:.4f} | Net profit: ${net_profit:.4f} | "
                    f"Cycle {s.cycles_completed}"
                )

                # Record completed cycle (include both base and net profit)
                self._completed_cycles.append({
                    "cycle": s.cycles_completed,
                    "entry_side": s.entry_side,
                    "entry_price": s.entry_fill_price,
                    "entry_size": s.entry_fill_size,
                    "entry_order_type": s.entry_order_type,
                    "hedge_side": s.hedge_side,
                    "hedge_price": s.hedge_fill_price,
                    "hedge_size": s.hedge_fill_size,
                    "hedge_order_type": s.hedge_order_type,
                    "pair_cost": s.actual_pair_cost,
                    "base_profit": base_profit,
                    "net_profit": net_profit,
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
            else:
                logger.info(
                    f"[SPREADCAP] Hedge PARTIAL fill: {side_upper} +{size} @ ${price:.4f} | "
                    f"filled={s.hedge_fill_size}/{s.hedge_size} avg=${s.hedge_fill_price:.4f}"
                )

        # Handle fills on ABORTED orders - still need to hedge!
        elif s.phase == SpreadCapturePhase.ABORTED and side_upper == s.entry_side:
            logger.warning(
                f"[SPREADCAP] Aborted order filled! Must hedge: {side_upper} {size} @ ${price:.4f}"
            )
            s.entry_fill_price = price
            s.entry_fill_size = size
            s.entry_fill_time = time.time()
            s.total_entry_fills += size
            s.phase = SpreadCapturePhase.ENTRY_FILLED  # Force transition to hedge

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
        s.entry_fill_price = 0.0
        s.entry_fill_size = 0
        s.entry_fill_time = 0.0
        s.entry_order_type = "maker"

        # Reset hedge state
        s.hedge_side = ""
        s.hedge_price = 0.0
        s.hedge_size = 0
        s.hedge_placed_at = 0.0
        s.hedge_retry_count = 0
        s.hedge_fill_price = 0.0
        s.hedge_fill_size = 0
        s.hedge_fill_time = 0.0
        s.hedge_order_type = "maker"

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
        velocity_bps: Optional[float] = None,
    ) -> bool:
        """
        Check if entry order should be pulled due to rapid market movement.

        Uses velocity-only trigger (z-score removed from codebase).
        Pull when velocity exceeds threshold to avoid fills during fast moves.

        Args:
            velocity_bps: Current velocity in basis points per second

        Returns:
            True if entry should be cancelled and repriced
        """
        s = self.state
        if s.phase != SpreadCapturePhase.ENTRY_PENDING:
            return False

        # Velocity trigger: Rapid movement
        # 0.05 bps/sec = ~$5 BTC move in 10s (realistic threshold)
        VELOCITY_PULL_THRESHOLD = 0.05  # bps/sec
        should_pull = velocity_bps is not None and abs(velocity_bps) > VELOCITY_PULL_THRESHOLD

        if should_pull:
            logger.info(f"[SPREADCAP] Pull entry: vel={velocity_bps:.3f}bps")

        return should_pull

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
