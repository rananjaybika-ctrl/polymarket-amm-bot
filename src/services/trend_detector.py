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
    """Trend strength levels based on velocity."""
    NEUTRAL = "neutral"    # |velocity| < 0.02 bps/sec - Normal market conditions
    MILD = "mild"          # 0.02 <= |velocity| < 0.05 bps/sec - Some movement
    STRONG = "strong"      # 0.05 <= |velocity| < 0.10 bps/sec - Clear movement
    EXTREME = "extreme"    # |velocity| >= 0.10 bps/sec - Rapid movement


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

    # Velocity thresholds for trend states (bps/sec)
    # 0.05 bps = ~$5 BTC move in 10s (realistic threshold)
    VELOCITY_MILD = 0.02      # Mild movement
    VELOCITY_STRONG = 0.05    # Clear movement - pull threshold
    VELOCITY_EXTREME = 0.10   # Rapid movement

    # Price movement thresholds (percentage from strike)
    PCT_FLAT_THRESHOLD = 0.1   # <0.1% = flat
    PCT_MILD_THRESHOLD = 0.3   # <0.3% = mild
    PCT_STRONG_THRESHOLD = 0.5 # <0.5% = strong

    # Quote pulling parameters
    # 0.05 bps/sec = ~$5 BTC move in 10s (realistic threshold for pulling)
    PULL_VELOCITY_BPS = 0.05  # Pull if velocity > 0.05 bps/sec

    def __init__(
        self,
        binance_client: 'BinanceClient',
        velocity_window_secs: int = 10,
        velocity_pull_threshold: float = None,
    ):
        """
        Initialize TrendDetector.

        Args:
            binance_client: Connected BinanceClient instance
            velocity_window_secs: Window for velocity calculation (default 10s)
            velocity_pull_threshold: Velocity threshold for pulling quotes (default 0.05 bps/sec)
        """
        self._binance = binance_client
        self._velocity_window = velocity_window_secs
        self._velocity_pull_threshold = velocity_pull_threshold or self.PULL_VELOCITY_BPS

        # Track last few prices for velocity calculation
        self._last_price: float = 0.0
        self._last_timestamp: float = 0.0

    def get_trend_signal(self) -> TrendSignal:
        """
        Calculate current trend signal from Binance data.

        Returns:
            TrendSignal with state, direction, and actionable data
        """
        price_vs_strike = self._binance.price_vs_strike_pct

        # Calculate velocity first (bps/sec) - this is now the primary signal
        velocity = self._calculate_velocity()
        abs_velocity = abs(velocity)

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

        # Determine state from velocity (not z-score)
        if abs_velocity >= self.VELOCITY_EXTREME:
            state = TrendState.EXTREME
            confidence = 0.95
        elif abs_velocity >= self.VELOCITY_STRONG:
            state = TrendState.STRONG
            confidence = 0.80
        elif abs_velocity >= self.VELOCITY_MILD:
            state = TrendState.MILD
            confidence = 0.60
        else:  # NEUTRAL
            state = TrendState.NEUTRAL
            confidence = 0.40

        return TrendSignal(
            state=state,
            direction=direction,
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

        Logic: VELOCITY-ONLY
        Pull if price moving fast (> threshold bps/sec) AGAINST our order.
        - 0.05 bps/sec = ~$5 BTC move in 10s (realistic threshold)

        Args:
            side: "UP" or "DOWN" - which side's quote we're evaluating
            velocity_threshold_bps: Minimum velocity to trigger pull (default 0.05 bps/sec)

        Returns:
            True if quote should be cancelled
        """
        if velocity_threshold_bps is None:
            velocity_threshold_bps = self._velocity_pull_threshold

        signal = self.get_trend_signal()
        side_upper = side.upper()

        # Check if velocity is ADVERSE for our order
        # UP order: adverse if velocity < 0 (BTC falling, UP getting expensive)
        # DOWN order: adverse if velocity > 0 (BTC rising, DOWN getting expensive)
        if side_upper == "UP":
            adverse_velocity = signal.velocity_bps < -velocity_threshold_bps
        else:  # DOWN
            adverse_velocity = signal.velocity_bps > velocity_threshold_bps

        if adverse_velocity:
            logger.info(
                f"PULL {side_upper}: vel={signal.velocity_bps:.3f}bps | "
                f"threshold={velocity_threshold_bps:.3f}bps | dir={signal.direction.value}"
            )

        return adverse_velocity

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
            f"vel={signal.velocity_bps:.3f}bps, "
            f"pct={signal.price_vs_strike_pct:.2f}%)"
        )
