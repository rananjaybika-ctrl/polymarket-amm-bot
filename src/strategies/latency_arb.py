"""
Latency Arbitrage Strategy

Exploits the measured 0.6-2.35 second lag between Binance BTC price moves
and Polymarket orderbook reaction.

Key Insight from Research:
- 78.8% of lags are <= 1.0 seconds
- Mean lag: 2.35s, Median: 0.81s
- Actionable window: ~800ms

Strategy Flow:
1. Detect Binance BTC spike (3-tick, ~50ms at 60Hz)
2. Immediately buy predicted winner on Polymarket (before orderbook reacts)
3. Post loser hedge bid based on spike magnitude
4. Stop-loss hedge if winner drops 7%

Usage:
    strategy = LatencyArbStrategy(base_size=15, spike_threshold=0.02)
    quotes = strategy.get_quotes(up_bid=0.55, up_ask=0.56, ..., binance_price=95000.0)

Author: Claude Code
Date: January 17, 2026
Based on: latency_arb_backtest.py research
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

# Spike detection - INTENTIONALLY SHORT for latency arb (NOT CANONICAL)
# Latency arb needs faster reaction than AGGRESSIVE strategy
SPIKE_LOOKBACK = 3           # 3 ticks (~50ms at 60Hz) - intentionally short for speed
DEFAULT_SPIKE_THRESHOLD = 0.02  # 0.02% minimum
SPIKE_HISTORY_SIZE = 50

# Magnitude-based loser bid (v2: recalibrated Jan 18, 2026)
# See research/HEDGE_PRICING_FINDINGS.md for analysis details
DROP_MULTIPLIER = 0.50   # Reduced from 0.68 - spike has weak predictive power
DROP_INTERCEPT = 0.08    # Increased from 0.01 - matches actual mean drop
DROP_REGIME_BONUS = {'LOW': 0.0, 'MEDIUM': 0.01, 'HIGH': 0.02}
TARGET_PAIR_COST = 0.99

# Stop-loss (7% optimal from research)
DEFAULT_STOP_LOSS_PCT = 0.07

# Timing
MIN_TIME_REMAINING = 120  # More conservative for latency arb
QUOTE_REFRESH_INTERVAL = 0.1  # Faster refresh for latency-sensitive strategy

# Position sizing
MIN_SHARES = 5
DEFAULT_BASE_SIZE = 15

# Cooldown between entries (prevent over-trading on same spike)
SPIKE_COOLDOWN_MS = 500


# =============================================================================
# ENUMS
# =============================================================================

class LatencyArbPhase(Enum):
    """Strategy phases."""
    IDLE = "idle"
    ENTRY_PENDING = "entry_pending"
    HEDGE_PENDING = "hedge_pending"
    COMPLETE = "complete"


# =============================================================================
# STATE DATACLASS
# =============================================================================

@dataclass
class LatencyArbState:
    """State tracking for latency arbitrage strategy."""
    phase: LatencyArbPhase = LatencyArbPhase.IDLE

    # Entry tracking
    entry_side: Optional[str] = None
    entry_price: float = 0.0
    entry_time: float = 0.0
    entry_spike_magnitude: float = 0.0

    # Hedge tracking
    loser_target_bid: float = 0.0
    hedge_filled: bool = False
    hedge_price: float = 0.0
    hedge_type: str = ""  # "passive", "stoploss"

    # Stop-loss tracking
    stop_loss_triggered: bool = False

    # Spike detection
    spike_history: List[float] = field(default_factory=list)
    last_spike_time: float = 0.0
    last_spike_direction: Optional[str] = None
    last_spike_magnitude: float = 0.0

    # Statistics
    total_cycles: int = 0
    total_pnl: float = 0.0
    passive_fills: int = 0
    stoploss_fills: int = 0

    # Latency tracking
    spike_to_entry_ms: float = 0.0
    spike_to_hedge_ms: float = 0.0

    # Timing
    last_quote_time: float = 0.0


# =============================================================================
# STRATEGY CLASS
# =============================================================================

class LatencyArbStrategy:
    """
    Latency Arbitrage Strategy.

    Exploits the lag between Binance price moves and Polymarket orderbook.
    Requires fast execution to capture the arbitrage window (~800ms).
    """

    def __init__(
        self,
        base_size: int = DEFAULT_BASE_SIZE,
        spike_threshold: float = DEFAULT_SPIKE_THRESHOLD,
        stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
        enable_cycling: bool = True,
    ):
        """
        Initialize Latency Arbitrage Strategy.

        Args:
            base_size: Shares per entry
            spike_threshold: Minimum % change to trigger (0.02% = $20 on $100k)
            stop_loss_pct: Stop-loss % (7% optimal from research)
            enable_cycling: Re-enter after completing cycle
        """
        self.base_size = max(MIN_SHARES, base_size)
        self.spike_threshold = spike_threshold
        self.stop_loss_pct = stop_loss_pct
        self.enable_cycling = enable_cycling

        self.state = LatencyArbState()

        logger.info(
            f"[LATARB] Initialized: base_size={base_size}, "
            f"spike_threshold={spike_threshold:.2f}%, "
            f"stop_loss={stop_loss_pct:.0%}, cycling={enable_cycling}"
        )

    # =========================================================================
    # SPIKE DETECTION
    # =========================================================================

    def detect_spike(self, binance_price: float, current_time: float) -> Tuple[Optional[str], float]:
        """
        Detect raw Binance price spike.

        Optimized for 60Hz data - detects spikes in ~50ms.

        Returns:
            (direction, magnitude_pct) or (None, 0) if no spike
        """
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

        if magnitude >= self.spike_threshold:
            # Check cooldown to prevent duplicate spikes
            if current_time - s.last_spike_time < SPIKE_COOLDOWN_MS / 1000:
                return None, 0.0

            direction = "UP" if change_pct > 0 else "DOWN"
            s.last_spike_time = current_time
            s.last_spike_direction = direction
            s.last_spike_magnitude = magnitude

            logger.debug(
                f"[LATARB] Spike detected: {direction} {magnitude:.4f}% "
                f"(${previous:.2f} -> ${current:.2f})"
            )
            return direction, magnitude

        return None, 0.0

    def calculate_loser_bid(self, magnitude_pct: float, loser_ask: float, winner_entry: float,
                             regime: str = "MEDIUM") -> float:  # regime kept for API compat, NOT used
        """
        Calculate optimal loser bid based on spike magnitude (v2).

        Formula: expected_drop = 0.50 * magnitude + 0.08
        See research/HEDGE_PRICING_FINDINGS.md for analysis.
        """
        # NOTE: regime_bonus REMOVED Feb 5, 2026 to match backtest/grid search
        expected_drop = DROP_MULTIPLIER * magnitude_pct + DROP_INTERCEPT
        expected_drop = max(0.02, min(0.20, expected_drop))
        max_loser = TARGET_PAIR_COST - winner_entry
        # FIX Feb 2, 2026: Use theoretical loser (1.0 - winner_entry), NOT loser_ask
        theoretical_loser = 1.0 - winner_entry
        loser_bid = min(theoretical_loser - expected_drop, max_loser)
        return max(0.01, min(0.95, loser_bid))

    # =========================================================================
    # STOP-LOSS CHECK
    # =========================================================================

    def check_stop_loss(self, winner_bid: float, loser_ask: float) -> Tuple[bool, Optional[float]]:
        """
        Check if stop-loss should trigger.

        Returns (should_trigger, hedge_price)
        """
        s = self.state

        if s.phase != LatencyArbPhase.HEDGE_PENDING:
            return False, None

        if s.stop_loss_triggered or s.entry_price <= 0:
            return False, None

        drop_pct = (s.entry_price - winner_bid) / s.entry_price

        if drop_pct >= self.stop_loss_pct:
            s.stop_loss_triggered = True
            logger.warning(
                f"[LATARB] STOP-LOSS: winner dropped {drop_pct:.1%} "
                f"(${s.entry_price:.3f} -> ${winner_bid:.3f}), "
                f"hedging at loser_ask=${loser_ask:.3f}"
            )
            return True, loser_ask

        return False, None

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
        Generate quotes for latency arbitrage strategy.

        CRITICAL: This strategy requires binance_price to detect spikes.
        Without it, no entries will be made.
        """
        if current_time is None:
            current_time = time.time()

        s = self.state

        # Don't enter if market ending soon
        if time_remaining < MIN_TIME_REMAINING:
            return []

        # Rate limit (but faster than other strategies)
        if current_time - s.last_quote_time < QUOTE_REFRESH_INTERVAL:
            return []
        s.last_quote_time = current_time

        quotes = []

        # PHASE: IDLE - Looking for spike to enter
        if s.phase == LatencyArbPhase.IDLE:
            if binance_price is None:
                logger.warning("[LATARB] No binance_price - cannot detect spikes")
                return []

            spike_dir, spike_mag = self.detect_spike(binance_price, current_time)

            if spike_dir is None:
                return []

            # SPIKE DETECTED - Enter immediately!
            winner_side = spike_dir
            loser_side = "DOWN" if winner_side == "UP" else "UP"

            # Buy winner at ASK (aggressive, to beat latency)
            if winner_side == "UP":
                entry_price = up_ask
                loser_ask = down_ask
                loser_bid = down_bid
            else:
                entry_price = down_ask
                loser_ask = up_ask
                loser_bid = up_bid

            # Calculate loser target based on magnitude
            loser_target = self.calculate_loser_bid(spike_mag, loser_ask, entry_price)

            # Update state
            s.entry_side = winner_side
            s.entry_price = entry_price
            s.entry_time = current_time
            s.entry_spike_magnitude = spike_mag
            s.loser_target_bid = loser_target
            s.phase = LatencyArbPhase.ENTRY_PENDING

            # Return aggressive entry quote
            quotes.append({
                'side': winner_side,
                'price': entry_price,
                'size': self.base_size,
                'is_entry': True,
                'is_latency_arb': True,
                'spike_magnitude': spike_mag,
                'is_market_order': True,  # Execute immediately
            })

            logger.info(
                f"[LATARB] SPIKE ENTRY: {winner_side} @ ${entry_price:.3f} "
                f"(spike={spike_mag:.4f}%, loser_target=${loser_target:.3f})"
            )

        # PHASE: ENTRY_PENDING - Entry submitted, waiting for fill
        elif s.phase == LatencyArbPhase.ENTRY_PENDING:
            # Entry should fill immediately (market order)
            # Move to hedge phase once filled (handled by on_fill)
            pass

        # PHASE: HEDGE_PENDING - Entry filled, waiting for hedge
        elif s.phase == LatencyArbPhase.HEDGE_PENDING:
            loser_side = "DOWN" if s.entry_side == "UP" else "UP"

            if s.entry_side == "UP":
                winner_bid = up_bid
                loser_ask = down_ask
            else:
                winner_bid = down_bid
                loser_ask = up_ask

            # Check stop-loss
            should_stop, stop_price = self.check_stop_loss(winner_bid, loser_ask)
            if should_stop and stop_price:
                s.hedge_type = "stoploss"
                quotes.append({
                    'side': loser_side,
                    'price': stop_price,
                    'size': self.base_size,
                    'is_stop_loss': True,
                    'is_latency_arb': True,
                    'is_market_order': True,
                })
                return quotes

            # Post passive hedge bid
            quotes.append({
                'side': loser_side,
                'price': s.loser_target_bid,
                'size': self.base_size,
                'is_hedge': True,
                'is_latency_arb': True,
            })

        return quotes

    # =========================================================================
    # FILL HANDLING
    # =========================================================================

    def on_fill(self, side: str, price: float, size: int) -> None:
        """Handle fill notification."""
        s = self.state
        side_upper = side.upper()

        logger.info(f"[LATARB] Fill: {side_upper} {size}@${price:.3f}")

        # Entry fill
        if side_upper == s.entry_side and s.phase == LatencyArbPhase.ENTRY_PENDING:
            s.entry_price = price
            s.spike_to_entry_ms = (time.time() - s.entry_time) * 1000
            s.phase = LatencyArbPhase.HEDGE_PENDING

            logger.info(
                f"[LATARB] Entry filled: {side_upper} @ ${price:.3f} "
                f"(latency={s.spike_to_entry_ms:.0f}ms)"
            )
            return

        # Hedge fill
        if s.phase == LatencyArbPhase.HEDGE_PENDING:
            s.hedge_filled = True
            s.hedge_price = price
            s.spike_to_hedge_ms = (time.time() - s.entry_time) * 1000

            # Determine hedge type
            if s.stop_loss_triggered:
                s.hedge_type = "stoploss"
                s.stoploss_fills += 1
            else:
                s.hedge_type = "passive"
                s.passive_fills += 1

            # Calculate PnL
            pair_cost = s.entry_price + price
            pnl = (1.0 - pair_cost) * self.base_size
            s.total_pnl += pnl
            s.total_cycles += 1

            logger.info(
                f"[LATARB] Hedge filled ({s.hedge_type}): ${price:.3f}, "
                f"pair_cost=${pair_cost:.4f}, pnl=${pnl:.2f}, "
                f"latency={s.spike_to_hedge_ms:.0f}ms"
            )

            if self.enable_cycling:
                self.reset_for_cycle()
            else:
                s.phase = LatencyArbPhase.COMPLETE

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
            "entry_spike_magnitude": s.entry_spike_magnitude,
            "loser_target_bid": s.loser_target_bid,
            "hedge_filled": s.hedge_filled,
            "hedge_type": s.hedge_type,
            "stop_loss_triggered": s.stop_loss_triggered,
            "total_cycles": s.total_cycles,
            "total_pnl": s.total_pnl,
            "passive_fills": s.passive_fills,
            "stoploss_fills": s.stoploss_fills,
            "last_spike_direction": s.last_spike_direction,
            "last_spike_magnitude": s.last_spike_magnitude,
        }

    def reset(self) -> None:
        """Reset strategy for new market."""
        total_pnl = self.state.total_pnl
        total_cycles = self.state.total_cycles
        passive_fills = self.state.passive_fills
        stoploss_fills = self.state.stoploss_fills

        self.state = LatencyArbState()
        self.state.total_pnl = total_pnl
        self.state.total_cycles = total_cycles
        self.state.passive_fills = passive_fills
        self.state.stoploss_fills = stoploss_fills

        logger.info(f"[LATARB] Reset for new market (total_pnl=${total_pnl:.2f})")

    def reset_for_cycle(self) -> None:
        """Reset state for next cycle within same market."""
        s = self.state

        # Preserve statistics
        total_pnl = s.total_pnl
        total_cycles = s.total_cycles
        passive_fills = s.passive_fills
        stoploss_fills = s.stoploss_fills
        spike_history = s.spike_history

        # Reset trade state
        s.phase = LatencyArbPhase.IDLE
        s.entry_side = None
        s.entry_price = 0.0
        s.entry_time = 0.0
        s.entry_spike_magnitude = 0.0
        s.loser_target_bid = 0.0
        s.hedge_filled = False
        s.hedge_price = 0.0
        s.hedge_type = ""
        s.stop_loss_triggered = False
        s.spike_to_entry_ms = 0.0
        s.spike_to_hedge_ms = 0.0

        # Restore statistics
        s.total_pnl = total_pnl
        s.total_cycles = total_cycles
        s.passive_fills = passive_fills
        s.stoploss_fills = stoploss_fills
        s.spike_history = spike_history

        logger.info(f"[LATARB] Cycle reset (ready for next spike)")

    def clear_spike_history(self) -> None:
        """Clear spike history (call on new market)."""
        self.state.spike_history = []
        self.state.last_spike_time = 0.0
        self.state.last_spike_direction = None
        self.state.last_spike_magnitude = 0.0

    def __repr__(self) -> str:
        return (
            f"LatencyArbStrategy("
            f"base_size={self.base_size}, "
            f"spike_threshold={self.spike_threshold:.2f}%, "
            f"stop_loss={self.stop_loss_pct:.0%}, "
            f"cycling={self.enable_cycling})"
        )
