"""
Trend Detection Module for Calculus Maker Strategy.

Detects trending conditions using Binance price feed to:
1. Pull stale quotes when Binance moves against pending orders
2. Prioritize buying the side that's getting expensive (prevent leg loss)
3. Reduce position targets in strong trends

Based on Telegram alpha insights:
- "If you are trying to buy the cheap side first while the expensive goes up you'll lose the leg"
- "Cancel all unfilled orders after 5-20 seconds"
- MMs monitor Binance to react BEFORE Polymarket updates
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from src.api.binance_client import BinanceClient

logger = logging.getLogger(__name__)


class TrendState(Enum):
    """Trend strength levels based on z-score."""
    NEUTRAL = "neutral"    # |z| < 1.0 - Normal market conditions
    MILD = "mild"          # 1.0 <= |z| < 2.0 - Some directional bias
    STRONG = "strong"      # 2.0 <= |z| < 3.0 - Clear trend, reduce targets
    EXTREME = "extreme"    # |z| >= 3.0 - Strong trend, minimal targets


class TrendDirection(Enum):
    """Market direction based on price vs strike."""
    UP = "up"       # BTC above strike - UP shares winning
    DOWN = "down"   # BTC below strike - DOWN shares winning
    FLAT = "flat"   # Within noise threshold


@dataclass
class TrendSignal:
    """Actionable trend signal for trading decisions."""
    state: TrendState
    direction: TrendDirection
    z_score: float
    price_vs_strike_pct: float
    velocity_bps: float  # Rate of change in basis points per second
    favored_side: str    # "UP" or "DOWN" - which side will win if trend continues
    confidence: float    # 0.0-1.0 confidence in the signal


class TrendDetector:
    """
    Detects trending conditions using Binance price feed.

    Key capabilities:
    - get_trend_signal(): Current trend state and direction
    - should_pull_quote(side): Whether to cancel a pending bid
    - get_priority_side(): Which side to buy FIRST in trending markets

    Usage:
        detector = TrendDetector(binance_client)

        signal = detector.get_trend_signal()
        if signal.state == TrendState.STRONG:
            # Reduce target from 15 to 10
            target = get_dynamic_target_shares(15, signal.state)

        if detector.should_pull_quote("DOWN"):
            # Cancel pending DOWN bid - price spiking UP
            await engine.cancel_order(down_order_id)

        priority = detector.get_priority_side()
        if priority == "UP":
            # Buy UP first - it's getting expensive
            buy_up = True
    """

    # Z-score thresholds for trend states
    Z_MILD = 1.0
    Z_STRONG = 2.0
    Z_EXTREME = 3.0

    # Price movement thresholds (percentage from strike)
    PCT_FLAT_THRESHOLD = 0.1   # <0.1% = flat
    PCT_MILD_THRESHOLD = 0.3   # <0.3% = mild
    PCT_STRONG_THRESHOLD = 0.5 # <0.5% = strong

    # Quote pulling parameters
    PULL_VELOCITY_BPS = 10.0   # Pull if velocity > 10 bps/sec

    def __init__(
        self,
        binance_client: 'BinanceClient',
        z_score_mild: float = Z_MILD,
        z_score_strong: float = Z_STRONG,
        z_score_extreme: float = Z_EXTREME,
        velocity_window_secs: int = 10,
    ):
        """
        Initialize TrendDetector.

        Args:
            binance_client: Connected BinanceClient instance
            z_score_mild: Z-score threshold for MILD state (default 1.0)
            z_score_strong: Z-score threshold for STRONG state (default 2.0)
            z_score_extreme: Z-score threshold for EXTREME state (default 3.0)
            velocity_window_secs: Window for velocity calculation (default 10s)
        """
        self._binance = binance_client
        self._z_thresholds = (z_score_mild, z_score_strong, z_score_extreme)
        self._velocity_window = velocity_window_secs

        # Track last few prices for velocity calculation
        self._last_price: float = 0.0
        self._last_timestamp: float = 0.0

    def get_trend_signal(self) -> TrendSignal:
        """
        Calculate current trend signal from Binance data.

        Returns:
            TrendSignal with state, direction, and actionable data
        """
        z_score = self._binance.calculate_z_score()
        price_vs_strike = self._binance.price_vs_strike_pct

        # Determine direction from price vs strike
        if price_vs_strike > self.PCT_FLAT_THRESHOLD:
            direction = TrendDirection.UP
            favored_side = "UP"
        elif price_vs_strike < -self.PCT_FLAT_THRESHOLD:
            direction = TrendDirection.DOWN
            favored_side = "DOWN"
        else:
            direction = TrendDirection.FLAT
            # In flat conditions, no clear favorite
            favored_side = "UP" if price_vs_strike >= 0 else "DOWN"

        # Determine state from z-score
        abs_z = abs(z_score)
        if abs_z >= self._z_thresholds[2]:  # EXTREME
            state = TrendState.EXTREME
            confidence = 0.95
        elif abs_z >= self._z_thresholds[1]:  # STRONG
            state = TrendState.STRONG
            confidence = 0.80
        elif abs_z >= self._z_thresholds[0]:  # MILD
            state = TrendState.MILD
            confidence = 0.60
        else:  # NEUTRAL
            state = TrendState.NEUTRAL
            confidence = 0.40

        # Calculate velocity (bps/sec)
        velocity = self._calculate_velocity()

        return TrendSignal(
            state=state,
            direction=direction,
            z_score=z_score,
            price_vs_strike_pct=price_vs_strike,
            velocity_bps=velocity,
            favored_side=favored_side,
            confidence=confidence,
        )

    def should_pull_quote(self, side: str, velocity_threshold_bps: float = None) -> bool:
        """
        Check if a pending quote should be pulled (cancelled).

        Implements professional MM behavior from Telegram alpha:
        "You get rolled over if you're not quick enough to pull your quotes when Binance moves"

        Logic (OR filter - pull if EITHER condition true):
        1. Z-SCORE: |z| >= 2.0 (STRONG/EXTREME) AND trending against our order
        2. VELOCITY: Price moving fast (> threshold bps/sec) against our order

        Why OR filter needed:
        - Velocity catches rapid spikes (e.g., BTC jumps $50 in 2 seconds)
        - Z-score catches sustained positions (e.g., BTC $100 above strike, velocity low after stabilizing)
        - Lost $1.90 on Jan 7 because z=2.56 but velocity was low - order filled on losing side

        Args:
            side: "UP" or "DOWN" - which side's quote we're evaluating
            velocity_threshold_bps: Minimum velocity to trigger pull (default 10 bps/sec)

        Returns:
            True if quote should be cancelled
        """
        if velocity_threshold_bps is None:
            velocity_threshold_bps = self.PULL_VELOCITY_BPS

        signal = self.get_trend_signal()

        # Don't pull in neutral conditions - no urgency
        if signal.state == TrendState.NEUTRAL:
            return False

        side_upper = side.upper()

        # Check if trend direction is AGAINST our order
        trend_against_down = (side_upper == "DOWN" and signal.direction == TrendDirection.UP)
        trend_against_up = (side_upper == "UP" and signal.direction == TrendDirection.DOWN)

        if not (trend_against_down or trend_against_up):
            return False  # Trend is WITH our order, keep it

        # OR FILTER: Pull if EITHER condition is true
        # Condition 1: Z-SCORE - Strong/Extreme trend against us (immediate pull)
        z_score_trigger = signal.state in (TrendState.STRONG, TrendState.EXTREME)

        # Condition 2: VELOCITY - Rapid movement against us
        velocity_trigger = abs(signal.velocity_bps) > velocity_threshold_bps

        should_pull = z_score_trigger or velocity_trigger

        if should_pull:
            reason = []
            if z_score_trigger:
                reason.append(f"z={signal.z_score:.2f}")
            if velocity_trigger:
                reason.append(f"vel={signal.velocity_bps:.1f}bps")
            logger.info(f"PULL {side_upper}: {' + '.join(reason)} | dir={signal.direction.value}")

        return should_pull

    def get_priority_side(self) -> Optional[str]:
        """
        Get which side should be bought FIRST in current conditions.

        Implements Telegram alpha insight:
        "If you are trying to buy the cheap side first while the expensive goes up you'll lose the leg"

        In trending markets: Buy the side that's about to get EXPENSIVE first.
        In neutral markets: Return None (use standard logic - buy cheaper side).

        Returns:
            "UP" or "DOWN" if trending, None if neutral
        """
        signal = self.get_trend_signal()

        # Only apply priority logic in STRONG or EXTREME trends
        if signal.state in (TrendState.STRONG, TrendState.EXTREME):
            # Return the WINNING side - it's about to get more expensive
            # If BTC trending UP, UP shares are winning, buy UP first
            # If BTC trending DOWN, DOWN shares are winning, buy DOWN first
            return signal.favored_side

        # In NEUTRAL/MILD, let caller use default logic
        return None

    def get_dynamic_target(self, base_target: int, time_remaining_secs: float = 900) -> int:
        """
        Calculate reduced target shares based on trend strength.

        Answers user question: "How to stop at 10/10 instead of 15/15?"

        Args:
            base_target: Normal target shares (e.g., 15)
            time_remaining_secs: Time left in market (for additional reduction late)

        Returns:
            Adjusted target (reduced in trending conditions), always multiple of 5
        """
        signal = self.get_trend_signal()

        # Reduction factors by trend state
        reductions = {
            TrendState.NEUTRAL: 1.0,   # 15/15
            TrendState.MILD: 0.85,     # ~12.75 → 10
            TrendState.STRONG: 0.67,   # 10/10
            TrendState.EXTREME: 0.50,  # ~7.5 → 5
        }

        factor = reductions.get(signal.state, 1.0)

        # Further reduce if late in market (less time to hedge)
        if time_remaining_secs < 300:  # Last 5 minutes
            factor *= 0.8

        raw_target = int(base_target * factor)

        # Round DOWN to nearest multiple of 5 (Polymarket constraint)
        rounded_target = (raw_target // 5) * 5

        return max(5, rounded_target)  # Minimum 5 shares

    def _calculate_velocity(self) -> float:
        """
        Calculate price velocity in basis points per second.

        Uses Binance price changes over velocity window.
        Positive = price rising, Negative = price falling.

        Returns:
            Velocity in bps/sec
        """
        changes = self._binance.get_price_changes(self._velocity_window)

        if not changes:
            return 0.0

        # Sum of percentage changes over window
        total_change_pct = sum(changes)

        # Convert to bps/sec
        # changes are % per tick, sum gives total % change
        # divide by window to get rate
        velocity_pct_per_sec = total_change_pct / self._velocity_window

        # Convert % to bps (1% = 100 bps)
        return velocity_pct_per_sec * 100

    def __repr__(self) -> str:
        signal = self.get_trend_signal()
        return (
            f"TrendDetector("
            f"state={signal.state.value}, "
            f"direction={signal.direction.value}, "
            f"z={signal.z_score:.2f}, "
            f"pct={signal.price_vs_strike_pct:.2f}%)"
        )
