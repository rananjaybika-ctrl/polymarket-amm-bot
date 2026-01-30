"""
Enhanced Momentum Strategy with Partial Hedging

Improves on baseline velocity strategy with:
1. Higher-order derivatives (acceleration, jerk) for signal quality
2. Partial hedging innovation - hedge portion, let rest ride to resolution

Key Innovation - Partial Hedging:
    Instead of hedging 100% at loser_offset, split into tranches:
    - T1 (Safe): hedge_ratio% hedged at loser_offset (guaranteed profit)
    - T2 (Ride): (1-hedge_ratio)% rides to resolution (2x if correct)

Usage:
    strategy = EnhancedMomentumStrategy(
        base_size=15,
        hedge_ratio=0.50,  # 50% hedged, 50% rides
        min_signal_quality=0.40,
    )
    quotes = strategy.get_quotes(up_bid=0.55, up_ask=0.56, ..., binance_price=95000.0)

Author: Claude Code
Date: January 17, 2026
Based on: enhanced_momentum_backtest.py research
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

# Velocity thresholds (zone 5-6)
VELOCITY_THRESHOLD = 0.50
VELOCITY_LOSER_OFFSET = 0.12

# Signal quality thresholds
DEFAULT_MIN_SIGNAL_QUALITY = 0.40
DEFAULT_HEDGE_RATIO = 0.50

# T2 stop-loss (riding tranche)
DEFAULT_T2_STOP_LOSS_PCT = 0.12

# Spike detection - CANONICAL from TRADING_CONFIGS.py (Jan 27, 2026)
SPIKE_LOOKBACK = 72  # 72 ticks ≈ 1200ms (CANONICAL)
SPIKE_THRESHOLD = 0.02
# Hedge pricing (v2: recalibrated Jan 18, 2026 - see HEDGE_PRICING_FINDINGS.md)
DROP_MULTIPLIER = 0.50   # Reduced from 0.68 - spike has weak predictive power
DROP_INTERCEPT = 0.08    # Increased from 0.01 - matches actual mean drop
DROP_REGIME_BONUS = {'LOW': 0.0, 'MEDIUM': 0.01, 'HIGH': 0.02}
SPIKE_HISTORY_SIZE = 50

# Price history for derivatives
PRICE_HISTORY_SIZE = 100
VELOCITY_WINDOW = 50  # ~10 seconds at 5Hz

# Timing
MIN_TIME_REMAINING = 120
QUOTE_REFRESH_INTERVAL = 0.5

# Minimum sizes
MIN_SHARES = 5
DEFAULT_BASE_SIZE = 15


# =============================================================================
# ENUMS
# =============================================================================

class EnhancedMomentumPhase(Enum):
    """Strategy phases."""
    IDLE = "idle"
    QUOTING = "quoting"
    T1_PENDING = "t1_pending"  # Waiting for T1 hedge
    T1_FILLED = "t1_filled"   # T1 hedged, T2 riding
    T2_RIDING = "t2_riding"   # T2 waiting for resolution or stop-loss
    COMPLETE = "complete"


# =============================================================================
# STATE DATACLASS
# =============================================================================

@dataclass
class EnhancedMomentumState:
    """State tracking for enhanced momentum strategy."""
    phase: EnhancedMomentumPhase = EnhancedMomentumPhase.IDLE

    # Entry tracking
    entry_side: Optional[str] = None
    entry_price: float = 0.0
    entry_time: float = 0.0
    entry_signal_quality: float = 0.0

    # T1 (hedged tranche) tracking
    t1_shares: int = 0
    t1_target_bid: float = 0.0
    t1_filled: bool = False
    t1_fill_price: float = 0.0

    # T2 (riding tranche) tracking
    t2_shares: int = 0
    t2_outcome: str = "riding"  # "riding", "stoploss", "resolution"
    t2_fill_price: float = 0.0

    # Signal quality components
    last_velocity: float = 0.0
    last_acceleration: float = 0.0
    last_signal_quality: float = 0.0
    accel_aligned: bool = False

    # Price history for derivatives
    price_history: List[float] = field(default_factory=list)

    # Spike detection
    spike_history: List[float] = field(default_factory=list)
    last_spike_direction: Optional[str] = None
    last_spike_magnitude: float = 0.0

    # Statistics
    total_cycles: int = 0
    total_t1_pnl: float = 0.0
    total_t2_pnl: float = 0.0
    total_pnl: float = 0.0

    # Timing
    last_quote_time: float = 0.0


# =============================================================================
# STRATEGY CLASS
# =============================================================================

class EnhancedMomentumStrategy:
    """
    Enhanced Momentum Strategy with Partial Hedging.

    Uses signal quality scoring and partial hedging for improved returns.
    """

    def __init__(
        self,
        base_size: int = DEFAULT_BASE_SIZE,
        hedge_ratio: float = DEFAULT_HEDGE_RATIO,
        min_signal_quality: float = DEFAULT_MIN_SIGNAL_QUALITY,
        t2_stop_loss_pct: float = DEFAULT_T2_STOP_LOSS_PCT,
        use_dynamic_ratio: bool = False,
        enable_cycling: bool = True,
    ):
        """
        Initialize Enhanced Momentum Strategy.

        Args:
            base_size: Total shares per entry
            hedge_ratio: Fraction to hedge (T1), rest rides (T2)
            min_signal_quality: Minimum quality score to enter (0-1)
            t2_stop_loss_pct: Stop-loss % for T2 riding tranche
            use_dynamic_ratio: Adjust ratio based on signal quality
            enable_cycling: Re-enter after completing cycle
        """
        self.base_size = max(MIN_SHARES, base_size)
        self.hedge_ratio = max(0.0, min(1.0, hedge_ratio))
        self.min_signal_quality = min_signal_quality
        self.t2_stop_loss_pct = t2_stop_loss_pct
        self.use_dynamic_ratio = use_dynamic_ratio
        self.enable_cycling = enable_cycling

        self.state = EnhancedMomentumState()

        logger.info(
            f"[ENHANCED] Initialized: base_size={base_size}, hedge_ratio={hedge_ratio:.0%}, "
            f"min_quality={min_signal_quality}, t2_stoploss={t2_stop_loss_pct:.0%}, "
            f"dynamic={use_dynamic_ratio}, cycling={enable_cycling}"
        )

    # =========================================================================
    # SIGNAL QUALITY CALCULATION
    # =========================================================================

    def calculate_velocity(self, binance_price: float) -> float:
        """Calculate velocity from price history."""
        self.state.price_history.append(binance_price)
        if len(self.state.price_history) > PRICE_HISTORY_SIZE:
            self.state.price_history = self.state.price_history[-PRICE_HISTORY_SIZE:]

        if len(self.state.price_history) < VELOCITY_WINDOW + 1:
            return 0.0

        current = self.state.price_history[-1]
        previous = self.state.price_history[-VELOCITY_WINDOW - 1]

        if previous <= 0:
            return 0.0

        # Velocity in basis points per second
        pct_change = (current - previous) / previous * 10000  # basis points
        velocity = pct_change / (VELOCITY_WINDOW / 5)  # assuming 5Hz

        self.state.last_velocity = velocity
        return velocity

    def calculate_acceleration(self) -> float:
        """Calculate acceleration from velocity history."""
        if len(self.state.price_history) < VELOCITY_WINDOW * 2:
            return 0.0

        # Calculate velocity at two points
        mid = len(self.state.price_history) // 2

        # Early velocity
        early_current = self.state.price_history[mid]
        early_prev = self.state.price_history[mid - VELOCITY_WINDOW // 2] if mid > VELOCITY_WINDOW // 2 else self.state.price_history[0]
        vel_early = (early_current - early_prev) / early_prev * 10000 if early_prev > 0 else 0

        # Late velocity
        late_current = self.state.price_history[-1]
        late_prev = self.state.price_history[-VELOCITY_WINDOW // 2 - 1]
        vel_late = (late_current - late_prev) / late_prev * 10000 if late_prev > 0 else 0

        # Acceleration = change in velocity / time
        acceleration = (vel_late - vel_early) / (VELOCITY_WINDOW / 5)

        self.state.last_acceleration = acceleration
        return acceleration

    def detect_spike(self, binance_price: float) -> Tuple[Optional[str], float]:
        """Detect raw Binance price spike."""
        self.state.spike_history.append(binance_price)
        if len(self.state.spike_history) > SPIKE_HISTORY_SIZE:
            self.state.spike_history = self.state.spike_history[-SPIKE_HISTORY_SIZE:]

        if len(self.state.spike_history) < SPIKE_LOOKBACK + 1:
            return None, 0.0

        current = self.state.spike_history[-1]
        previous = self.state.spike_history[-SPIKE_LOOKBACK - 1]

        if previous <= 0:
            return None, 0.0

        change_pct = (current - previous) / previous * 100
        magnitude = abs(change_pct)

        if magnitude >= SPIKE_THRESHOLD:
            direction = "UP" if change_pct > 0 else "DOWN"
            self.state.last_spike_direction = direction
            self.state.last_spike_magnitude = magnitude
            return direction, magnitude

        return None, 0.0

    def calculate_signal_quality(
        self,
        velocity_bps: float,
        spike_detected: bool,
        spike_direction: Optional[str],
    ) -> float:
        """
        Calculate signal quality score (0-1).

        Components:
        - Velocity magnitude (30%)
        - Acceleration alignment (25%)
        - Spike confirmation (25%)
        - Duration in zone (20%)
        """
        quality = 0.0

        # Velocity magnitude (30%)
        vel_component = min(abs(velocity_bps) / 1.0, 1.0) * 0.30
        quality += vel_component

        # Acceleration alignment (25%)
        accel = self.state.last_acceleration
        vel_dir = 1 if velocity_bps > 0 else -1
        accel_dir = 1 if accel > 0 else -1
        self.state.accel_aligned = (vel_dir == accel_dir)
        if self.state.accel_aligned:
            quality += 0.25

        # Spike confirmation (25%)
        if spike_detected:
            vel_dir_str = "UP" if velocity_bps > 0 else "DOWN"
            if spike_direction == vel_dir_str:
                quality += 0.25
            else:
                quality += 0.10  # Partial credit for any spike

        # Duration (20%) - simplified: based on consistency of velocity sign
        # Could be enhanced with actual streak tracking
        quality += 0.10  # Base duration credit

        self.state.last_signal_quality = quality
        return quality

    def get_dynamic_hedge_ratio(self, signal_quality: float, time_remaining: float) -> float:
        """Calculate dynamic hedge ratio based on signal quality."""
        if not self.use_dynamic_ratio:
            return self.hedge_ratio

        base = 0.50

        # High quality = hedge less (let more ride)
        quality_adj = (0.50 - signal_quality) * 0.30

        # Less time = hedge more
        time_adj = 0.15 if time_remaining < 300 else 0

        ratio = base + quality_adj + time_adj
        return max(0.25, min(0.75, ratio))

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
        Generate quotes for enhanced momentum strategy.

        Returns quotes for entry (T1+T2) or hedge (T1 only).
        T2 rides to resolution or stop-loss.
        """
        if current_time is None:
            current_time = time.time()

        s = self.state

        # Don't enter if market ending soon
        if time_remaining < MIN_TIME_REMAINING:
            return []

        # Update velocity/acceleration if binance_price provided
        if binance_price is not None:
            velocity_bps = self.calculate_velocity(binance_price)
            self.calculate_acceleration()

        # Detect spike
        spike_dir, spike_mag = None, 0.0
        if binance_price is not None:
            spike_dir, spike_mag = self.detect_spike(binance_price)

        # Rate limit
        if current_time - s.last_quote_time < QUOTE_REFRESH_INTERVAL:
            return []
        s.last_quote_time = current_time

        quotes = []

        # PHASE: IDLE - Looking for entry
        if s.phase == EnhancedMomentumPhase.IDLE:
            # Check velocity threshold
            if abs(velocity_bps) < VELOCITY_THRESHOLD:
                return []

            # Calculate signal quality
            signal_quality = self.calculate_signal_quality(
                velocity_bps, spike_dir is not None, spike_dir
            )

            # Quality gate
            if signal_quality < self.min_signal_quality:
                logger.debug(f"[ENHANCED] Signal quality {signal_quality:.3f} < {self.min_signal_quality}")
                return []

            # Determine winner side
            winner_side = "UP" if velocity_bps > 0 else "DOWN"
            loser_side = "DOWN" if winner_side == "UP" else "UP"

            # Calculate effective hedge ratio
            effective_ratio = self.get_dynamic_hedge_ratio(signal_quality, time_remaining)

            # Split into tranches
            t1_shares = max(MIN_SHARES, int(self.base_size * effective_ratio))
            t2_shares = max(0, self.base_size - t1_shares)
            if t2_shares > 0 and t2_shares < MIN_SHARES:
                t1_shares = self.base_size
                t2_shares = 0

            # Winner entry at ask
            if winner_side == "UP":
                entry_price = up_ask
                loser_bid = down_bid
            else:
                entry_price = down_ask
                loser_bid = up_bid

            # Calculate T1 loser target
            t1_target = loser_bid - VELOCITY_LOSER_OFFSET
            t1_target = max(0.01, min(0.95, t1_target))

            # Update state
            s.entry_side = winner_side
            s.entry_price = entry_price
            s.entry_time = current_time
            s.entry_signal_quality = signal_quality
            s.t1_shares = t1_shares
            s.t1_target_bid = t1_target
            s.t2_shares = t2_shares
            s.phase = EnhancedMomentumPhase.T1_PENDING

            # Return entry quote (buy winner at ask)
            quotes.append({
                'side': winner_side,
                'price': entry_price,
                'size': self.base_size,
                'is_entry': True,
                'is_enhanced_momentum': True,
                'signal_quality': signal_quality,
                't1_shares': t1_shares,
                't2_shares': t2_shares,
            })

            logger.info(
                f"[ENHANCED] ENTRY: {winner_side} @ ${entry_price:.3f}, "
                f"quality={signal_quality:.3f}, T1={t1_shares}, T2={t2_shares}"
            )

        # PHASE: T1_PENDING - Waiting for T1 hedge
        elif s.phase == EnhancedMomentumPhase.T1_PENDING:
            loser_side = "DOWN" if s.entry_side == "UP" else "UP"

            # T1 hedge quote at target bid
            if s.t1_shares > 0 and not s.t1_filled:
                quotes.append({
                    'side': loser_side,
                    'price': s.t1_target_bid,
                    'size': s.t1_shares,
                    'is_hedge': True,
                    'is_t1': True,
                })

            # Check T2 stop-loss while T1 pending
            if s.t2_shares > 0:
                if s.entry_side == "UP":
                    winner_bid = up_bid
                    loser_ask = down_ask
                else:
                    winner_bid = down_bid
                    loser_ask = up_ask

                drop_pct = (s.entry_price - winner_bid) / s.entry_price
                if drop_pct >= self.t2_stop_loss_pct:
                    # T2 stop-loss triggered - hedge at loser ask
                    s.t2_outcome = "stoploss"
                    s.t2_fill_price = loser_ask

                    quotes.append({
                        'side': loser_side,
                        'price': loser_ask,
                        'size': s.t2_shares,
                        'is_stop_loss': True,
                        'is_t2': True,
                        'is_market_order': True,
                    })

                    logger.warning(
                        f"[ENHANCED] T2 STOP-LOSS: {loser_side} @ ${loser_ask:.3f} "
                        f"(winner dropped {drop_pct:.1%})"
                    )

        # PHASE: T1_FILLED - T1 hedged, T2 riding
        elif s.phase == EnhancedMomentumPhase.T1_FILLED:
            # T2 continues riding until resolution or stop-loss
            if s.t2_shares > 0 and s.t2_outcome == "riding":
                if s.entry_side == "UP":
                    winner_bid = up_bid
                    loser_ask = down_ask
                else:
                    winner_bid = down_bid
                    loser_ask = up_ask

                # Check T2 stop-loss
                drop_pct = (s.entry_price - winner_bid) / s.entry_price
                if drop_pct >= self.t2_stop_loss_pct:
                    loser_side = "DOWN" if s.entry_side == "UP" else "UP"
                    s.t2_outcome = "stoploss"
                    s.t2_fill_price = loser_ask

                    quotes.append({
                        'side': loser_side,
                        'price': loser_ask,
                        'size': s.t2_shares,
                        'is_stop_loss': True,
                        'is_t2': True,
                        'is_market_order': True,
                    })

                    logger.warning(
                        f"[ENHANCED] T2 STOP-LOSS: {loser_side} @ ${loser_ask:.3f}"
                    )

        return quotes

    # =========================================================================
    # FILL HANDLING
    # =========================================================================

    def on_fill(self, side: str, price: float, size: int, is_t1: bool = False, is_t2: bool = False) -> None:
        """Handle fill notification."""
        s = self.state
        side_upper = side.upper()

        logger.info(f"[ENHANCED] Fill: {side_upper} {size}@${price:.3f} (T1={is_t1}, T2={is_t2})")

        # Entry fill
        if side_upper == s.entry_side and s.phase == EnhancedMomentumPhase.IDLE:
            s.entry_price = price
            s.phase = EnhancedMomentumPhase.T1_PENDING
            logger.info(f"[ENHANCED] Entry filled: {side_upper} @ ${price:.3f}")
            return

        # T1 hedge fill
        if is_t1 or (s.phase == EnhancedMomentumPhase.T1_PENDING and not s.t1_filled):
            s.t1_filled = True
            s.t1_fill_price = price

            # Calculate T1 PnL
            t1_pair_cost = s.entry_price + price
            t1_pnl = (1.0 - t1_pair_cost) * s.t1_shares
            s.total_t1_pnl += t1_pnl

            logger.info(
                f"[ENHANCED] T1 hedged: ${price:.3f}, pair_cost=${t1_pair_cost:.4f}, "
                f"pnl=${t1_pnl:.2f}"
            )

            if s.t2_shares > 0:
                s.phase = EnhancedMomentumPhase.T1_FILLED
            else:
                self._complete_cycle()
            return

        # T2 fill (stop-loss)
        if is_t2 or s.t2_outcome == "stoploss":
            s.t2_fill_price = price

            # Calculate T2 PnL (stop-loss)
            t2_pair_cost = s.entry_price + price
            t2_pnl = (1.0 - t2_pair_cost) * s.t2_shares
            s.total_t2_pnl += t2_pnl

            logger.info(
                f"[ENHANCED] T2 stop-loss: ${price:.3f}, pair_cost=${t2_pair_cost:.4f}, "
                f"pnl=${t2_pnl:.2f}"
            )

            self._complete_cycle()

    def on_resolution(self, winner: str) -> None:
        """Handle market resolution for T2 tranche."""
        s = self.state

        if s.t2_shares == 0 or s.t2_outcome != "riding":
            return

        prediction_correct = (s.entry_side == winner)

        if prediction_correct:
            s.t2_outcome = "resolution_win"
            # Winner pays $1, loser worthless
            t2_pnl = (1.0 - s.entry_price) * s.t2_shares
        else:
            s.t2_outcome = "resolution_loss"
            # Winner worthless
            t2_pnl = (0.0 - s.entry_price) * s.t2_shares

        s.total_t2_pnl += t2_pnl

        logger.info(
            f"[ENHANCED] T2 resolution: {s.t2_outcome}, pnl=${t2_pnl:.2f} "
            f"(predicted={s.entry_side}, actual={winner})"
        )

        self._complete_cycle()

    def _complete_cycle(self) -> None:
        """Complete cycle and update statistics."""
        s = self.state

        # Calculate total PnL for this cycle
        t1_pnl = 0.0
        if s.t1_filled:
            t1_pair_cost = s.entry_price + s.t1_fill_price
            t1_pnl = (1.0 - t1_pair_cost) * s.t1_shares

        t2_pnl = 0.0
        if s.t2_shares > 0:
            if s.t2_outcome == "resolution_win":
                t2_pnl = (1.0 - s.entry_price) * s.t2_shares
            elif s.t2_outcome == "resolution_loss":
                t2_pnl = (0.0 - s.entry_price) * s.t2_shares
            elif s.t2_outcome == "stoploss":
                t2_pair_cost = s.entry_price + s.t2_fill_price
                t2_pnl = (1.0 - t2_pair_cost) * s.t2_shares

        total_pnl = t1_pnl + t2_pnl
        s.total_pnl += total_pnl
        s.total_cycles += 1

        logger.info(
            f"[ENHANCED] Cycle complete: T1=${t1_pnl:.2f}, T2=${t2_pnl:.2f}, "
            f"Total=${total_pnl:.2f} (cumulative=${s.total_pnl:.2f})"
        )

        if self.enable_cycling:
            self.reset_for_cycle()
        else:
            s.phase = EnhancedMomentumPhase.COMPLETE

    # =========================================================================
    # STATUS & RESET
    # =========================================================================

    def get_status(self) -> Dict[str, Any]:
        """Get current strategy status."""
        s = self.state
        return {
            "phase": s.phase.value,
            "entry_side": s.entry_side,
            "entry_price": s.entry_price,
            "entry_signal_quality": s.entry_signal_quality,
            "t1_shares": s.t1_shares,
            "t1_filled": s.t1_filled,
            "t1_fill_price": s.t1_fill_price,
            "t2_shares": s.t2_shares,
            "t2_outcome": s.t2_outcome,
            "total_cycles": s.total_cycles,
            "total_pnl": s.total_pnl,
            "total_t1_pnl": s.total_t1_pnl,
            "total_t2_pnl": s.total_t2_pnl,
            "hedge_ratio": self.hedge_ratio,
            "use_dynamic_ratio": self.use_dynamic_ratio,
        }

    def reset(self) -> None:
        """Reset strategy for new market."""
        total_pnl = self.state.total_pnl
        total_cycles = self.state.total_cycles

        self.state = EnhancedMomentumState()
        self.state.total_pnl = total_pnl
        self.state.total_cycles = total_cycles

        logger.info(f"[ENHANCED] Reset for new market (total_pnl=${total_pnl:.2f})")

    def reset_for_cycle(self) -> None:
        """Reset state for next cycle within same market."""
        s = self.state

        # Preserve statistics and price history
        total_pnl = s.total_pnl
        total_t1_pnl = s.total_t1_pnl
        total_t2_pnl = s.total_t2_pnl
        total_cycles = s.total_cycles
        price_history = s.price_history
        spike_history = s.spike_history

        # Reset trade state
        s.phase = EnhancedMomentumPhase.IDLE
        s.entry_side = None
        s.entry_price = 0.0
        s.entry_time = 0.0
        s.entry_signal_quality = 0.0

        s.t1_shares = 0
        s.t1_target_bid = 0.0
        s.t1_filled = False
        s.t1_fill_price = 0.0

        s.t2_shares = 0
        s.t2_outcome = "riding"
        s.t2_fill_price = 0.0

        # Restore statistics
        s.total_pnl = total_pnl
        s.total_t1_pnl = total_t1_pnl
        s.total_t2_pnl = total_t2_pnl
        s.total_cycles = total_cycles
        s.price_history = price_history
        s.spike_history = spike_history

        logger.info(f"[ENHANCED] Cycle reset (ready for re-entry)")

    def __repr__(self) -> str:
        return (
            f"EnhancedMomentumStrategy("
            f"base_size={self.base_size}, "
            f"hedge_ratio={self.hedge_ratio:.0%}, "
            f"min_quality={self.min_signal_quality}, "
            f"cycling={self.enable_cycling})"
        )
