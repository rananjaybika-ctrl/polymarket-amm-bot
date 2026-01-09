"""
Calculus MAKER Strategy (Strategy 4)

Dynamic mispricing detection with exponential decay pricing and quadratic size ramp.
Designed for BTC 15-minute Up/Down markets on Polymarket.

Key Features:
- Exponential decay offset: requires 2.5% edge early, 0.5% edge late
- INVERTED quadratic size ramp: 50 shares early, 5 shares late (capture good prices)
- All sizes in multiples of 5 (Polymarket constraint)
- MAKER orders only (posts limit bids)

Mathematical Models:
    Mispricing Threshold: m(t) = 0.005 + 0.02 * e^(-0.004*(900-t))
    Inverted Size: size(t) = 5 + 45 * (t/900)^2  [BIG early, small late]
    Normal Size:   size(t) = 5 + 45 * (1 - t/900)^2  [small early, BIG late - WRONG]

Usage:
    from src.strategies import CalculusMakerStrategy

    strategy = CalculusMakerStrategy(max_shares=50, min_shares=5)

    # Get pricing decision
    price = strategy.get_price(best_bid=0.45, time_remaining=600)

    # Get size decision
    size = strategy.get_size(time_remaining=600)

    # Check if should buy
    should_buy = strategy.should_buy(pair_cost=0.97, time_remaining=600)
"""

import math
from dataclasses import dataclass
from typing import Optional, Tuple

from src.config import FeeConfig


# =============================================================================
# PRIMARY PARAMETERS (Polymarket Constraints)
# =============================================================================

MIN_ORDER_SIZE = 5          # Polymarket minimum order size
SIZE_INCREMENT = 5          # All sizes must be multiples of 5
MARKET_DURATION = 900       # 15-minute markets in seconds
DEFAULT_MAX_SHARES = 50     # Default maximum shares per order
DEFAULT_MIN_SHARES = 5      # Default minimum shares per order

# Velocity-based order management parameters
VELOCITY_PULL_THRESHOLD = 0.05   # bps/sec - pull entry if adverse velocity exceeds this
MAX_HEDGE_WAIT_SECS = 120.0      # Max time to "let it ride" before forcing hedge
MIN_TIME_TO_FORCE_HEDGE = 60.0   # Force hedge if less than this time remaining

# Mispricing threshold parameters
# With maker rebates (~1% per side, ~2% total), we can accept tighter spreads.
# Pre-rebate: M_MIN = 0.005 meant we needed 0.5% raw edge
# Post-rebate: M_MIN = 0.00 means break-even on spread, profit from rebates
# We use 0.00 as minimum since maker rebates (~2%) provide the profit buffer.
M_MIN = 0.00                # Late market: accept break-even on spread (rebates = profit)
M_MAX = 0.025               # Early market threshold (require 2.5% edge → pair_cost < $0.975)
LAMBDA = 0.004              # Decay constant (higher = faster decay to M_MIN)

# For reference, with ~1% maker rebate per side:
# pair_cost = $1.00 + 2% rebate = ~$0.02 profit per pair


# =============================================================================
# CORE FUNCTIONS
# =============================================================================

def get_mispricing_threshold(t: float) -> float:
    """
    Calculate minimum mispricing required to buy based on time remaining.

    Uses exponential decay: early markets need larger mispricing to justify entry.

    Mathematical model:
        m(t) = m_min + (m_max - m_min) * e^(-lambda*(900-t))

    Args:
        t: Time remaining in seconds (0-900)

    Returns:
        Minimum (1.0 - pair_cost) required to buy

    Examples:
        >>> get_mispricing_threshold(900)  # 15 min left
        0.025  # Requires 2.5% edge
        >>> get_mispricing_threshold(60)   # 1 min left
        0.006  # Accepts 0.6% edge

    Values at key times (with M_MAX=0.025, LAMBDA=0.004):
        t=900s (15m):  0.025 -> only buy if pair_cost < $0.975
        t=600s (10m):  0.011 -> only buy if pair_cost < $0.989
        t=300s (5m):   0.007 -> only buy if pair_cost < $0.993
        t=120s (2m):   0.006 -> only buy if pair_cost < $0.994
        t=60s (1m):    0.006 -> only buy if pair_cost < $0.994
    """
    t = max(0, min(t, MARKET_DURATION))
    return M_MIN + (M_MAX - M_MIN) * math.exp(-LAMBDA * (MARKET_DURATION - t))


def get_dynamic_size(
    t: float,
    max_shares: int = DEFAULT_MAX_SHARES,
    min_shares: int = DEFAULT_MIN_SHARES,
    inverted: bool = False
) -> int:
    """
    Calculate order size with quadratic ramp, in multiples of 5.

    NORMAL MODE (inverted=False, RECOMMENDED):
        Small orders early, ramp up late.
        - Starts small (5) to test fills and avoid immediate imbalance
        - Ramps up as time runs out to complete position
        - Safer: gives time to hedge if one side doesn't fill

    INVERTED MODE (inverted=True, DEPRECATED):
        BIG orders early, small orders late.
        - Risk: Creates immediate large imbalance if one side doesn't fill
        - Can trigger emergency hedging right at market start

    Mathematical model:
        Normal:   size(t) = S_min + (S_max - S_min) * (1 - t/900)^2
        Inverted: size(t) = S_min + (S_max - S_min) * (t/900)^2
        Final: max(5, 5 * round(raw_size / 5))

    Args:
        t: Time remaining in seconds (0-900)
        max_shares: Maximum order size (default 50)
        min_shares: Minimum order size (default 5)
        inverted: If True, use inverted sizing (DEPRECATED)

    Returns:
        Order size as integer, rounded to multiple of 5

    Examples (NORMAL - recommended):
        >>> get_dynamic_size(900, max_shares=15)  # 15 min left
        5   # Small - test fills first
        >>> get_dynamic_size(300, max_shares=15)  # 5 min left
        9   # Ramping up
        >>> get_dynamic_size(0, max_shares=15)    # 0 min left
        15  # Max - complete position

    Values at key times (inverted=False, max=15, min=5):
        t=900s (15m):  5 shares (small early - test fills)
        t=600s (10m):  6 shares
        t=300s (5m):   9 shares
        t=120s (2m):   13 shares
        t=0s:          15 shares (max late - complete position)
    """
    t = max(0, min(t, MARKET_DURATION))

    if inverted:
        # INVERTED (deprecated): BIG early, small late
        patience = (t / MARKET_DURATION) ** 2
        raw_size = min_shares + (max_shares - min_shares) * patience
    else:
        # NORMAL (recommended): small early, ramp up late
        urgency = (1 - t / MARKET_DURATION) ** 2
        raw_size = min_shares + (max_shares - min_shares) * urgency

    # Round to nearest multiple of SIZE_INCREMENT
    rounded = SIZE_INCREMENT * round(raw_size / SIZE_INCREMENT)

    return max(min_shares, rounded)


def get_calculus_price(
    best_bid: float,
    time_remaining: float,
    is_emergency: bool = False,
    best_ask: Optional[float] = None
) -> Optional[float]:
    """
    Calculate patient bid price using dynamic Calculus MAKER offset.

    Uses exponential decay offset from best_bid - the offset equals
    the mispricing threshold, so we bid at a price that would give us
    the required margin if filled.

    Args:
        best_bid: Current best bid price (0.0-1.0)
        time_remaining: Seconds until market resolution (0-900)
        is_emergency: If True, use aggressive pricing (best_ask)
        best_ask: Current best ask price (required if is_emergency=True)

    Returns:
        Patient bid price, or None if price would be <= 0 (don't place order)

    Examples:
        >>> get_calculus_price(0.50, 900)   # 15 min, bid at 0.50
        0.46  # 0.50 - 0.04 offset
        >>> get_calculus_price(0.50, 60)    # 1 min, bid at 0.50
        0.49  # 0.50 - 0.01 offset

    Dynamic pricing:
        Early (t=900s): best_bid - 0.040 (require 4% edge)
        Mid (t=600s):   best_bid - 0.017 (require 1.7% edge)
        Late (t=300s):  best_bid - 0.012 (require 1.2% edge)
        Final (t=60s):  best_bid - 0.010 (require 1% edge)
        Emergency:      best_ask (taker, immediate fill)
    """
    if is_emergency:
        if best_ask is None:
            raise ValueError("best_ask required for emergency pricing")
        return best_ask

    threshold = get_mispricing_threshold(time_remaining)
    price = best_bid - threshold

    # Don't place order if price would be invalid (market near resolution with cheap side)
    if price <= 0:
        return None

    return price


def should_buy_calculus(pair_cost: float, time_remaining: float) -> bool:
    """
    Determine if we should buy based on current pair cost and time.

    Only buy if the mispricing (1.0 - pair_cost) exceeds the threshold
    for the current time remaining.

    Args:
        pair_cost: Current pair cost (UP_ask + DOWN_ask)
        time_remaining: Seconds until market resolution (0-900)

    Returns:
        True if should buy, False otherwise

    Examples:
        >>> should_buy_calculus(0.95, 900)  # 5% mispricing, 15 min left
        True  # 5% > 4% threshold
        >>> should_buy_calculus(0.98, 900)  # 2% mispricing, 15 min left
        False  # 2% < 4% threshold
        >>> should_buy_calculus(0.98, 60)   # 2% mispricing, 1 min left
        True  # 2% > 1% threshold
    """
    threshold = get_mispricing_threshold(time_remaining)
    mispricing = 1.0 - pair_cost
    return mispricing >= threshold


def check_prospective_pair_cost(
    side: str,
    buy_price: float,
    buy_size: float,
    current_up_size: float,
    current_down_size: float,
    current_up_avg: float,
    current_down_avg: float,
    max_pair_cost: float = 0.98
) -> Tuple[bool, float, str]:
    """
    Check if buying would push pair cost above maximum threshold.

    This is a PRE-BUY gating check to block trades that would make
    the position unprofitable even if the market resolves in our favor.

    Args:
        side: "UP" or "DOWN"
        buy_price: Price we're about to buy at
        buy_size: Number of shares to buy
        current_up_size: Current UP shares held
        current_down_size: Current DOWN shares held
        current_up_avg: Current UP average price (0 if no shares)
        current_down_avg: Current DOWN average price (0 if no shares)
        max_pair_cost: Maximum acceptable pair cost (default $0.98)

    Returns:
        Tuple of (should_buy, prospective_pair_cost, reason)

    Example:
        >>> check_prospective_pair_cost(
        ...     side="UP", buy_price=0.60, buy_size=10,
        ...     current_up_size=5, current_down_size=10,
        ...     current_up_avg=0.40, current_down_avg=0.45,
        ...     max_pair_cost=0.98
        ... )
        (False, 1.01, "Would push pair cost to $1.010 > $0.980")
    """
    side_upper = side.upper()

    # Calculate new weighted average after this buy
    if side_upper == "UP":
        new_up_size = current_up_size + buy_size
        if new_up_size > 0:
            new_up_avg = (current_up_size * current_up_avg + buy_size * buy_price) / new_up_size
        else:
            new_up_avg = buy_price
        new_down_avg = current_down_avg
    else:
        new_down_size = current_down_size + buy_size
        if new_down_size > 0:
            new_down_avg = (current_down_size * current_down_avg + buy_size * buy_price) / new_down_size
        else:
            new_down_avg = buy_price
        new_up_avg = current_up_avg

    # Calculate prospective pair cost
    # Pair cost = avg cost of UP + avg cost of DOWN
    prospective_pair_cost = new_up_avg + new_down_avg

    # RULE 1: Always allow first buy of this side
    # If one side is 0, we NEED to buy that side to create pairs - never block it
    if side_upper == "UP" and current_up_size == 0:
        return (True, prospective_pair_cost, "OK - creating first UP position")
    if side_upper == "DOWN" and current_down_size == 0:
        return (True, prospective_pair_cost, "OK - creating first DOWN position")

    # RULE 2: Always allow buying the DEFICIT side (reduces imbalance)
    # This is critical - we can't block hedge buys just because pair cost is high
    is_deficit_side = (
        (side_upper == "UP" and current_up_size < current_down_size) or
        (side_upper == "DOWN" and current_down_size < current_up_size)
    )
    if is_deficit_side:
        return (True, prospective_pair_cost, "OK - buying deficit side to reduce imbalance")

    # RULE 3: Only gate SURPLUS side buys on pair cost
    # (buying the side with MORE shares would increase imbalance)
    if prospective_pair_cost > max_pair_cost:
        return (
            False,
            prospective_pair_cost,
            f"Would push pair cost to ${prospective_pair_cost:.3f} > ${max_pair_cost:.3f}"
        )

    return (True, prospective_pair_cost, "OK")


def check_prospective_pair_cost_with_market(
    side: str,
    buy_price: float,
    other_side_best_ask: float,
    max_pair_cost: float = 0.98
) -> Tuple[bool, float, str]:
    """
    Check if buying would result in unprofitable hedge at CURRENT market prices.

    This is the key protection against trending markets. Instead of checking
    against average price already held, we check what the hedge would actually
    cost RIGHT NOW.

    Based on Telegram alpha insight:
    "If you are trying to buy the cheap side first while the expensive goes up
    you'll lose the leg"

    Args:
        side: "UP" or "DOWN" - which side we're about to buy
        buy_price: Price we're about to pay for this side
        other_side_best_ask: Current best ask for the OTHER side (hedge cost)
        max_pair_cost: Maximum acceptable pair cost (default $0.98)

    Returns:
        Tuple of (should_buy, prospective_pair_cost, reason)

    Example:
        # Market trending: UP at 85c, DOWN at 20c
        # We're about to buy DOWN at 20c
        >>> check_prospective_pair_cost_with_market(
        ...     side="DOWN",
        ...     buy_price=0.20,
        ...     other_side_best_ask=0.85,  # UP ask
        ...     max_pair_cost=0.98
        ... )
        (False, 1.05, "Hedge at market would cost $1.050 > $0.980")

        # Market neutral: UP at 52c, DOWN at 47c
        >>> check_prospective_pair_cost_with_market(
        ...     side="DOWN",
        ...     buy_price=0.47,
        ...     other_side_best_ask=0.52,  # UP ask
        ...     max_pair_cost=0.98
        ... )
        (True, 0.99, "OK - hedge at market costs $0.990")
    """
    # Calculate what pair cost would be if we bought this AND hedged at market
    prospective_pair_cost = buy_price + other_side_best_ask

    # BLOCK if completing hedge at market would exceed max pair cost
    if prospective_pair_cost > max_pair_cost:
        return (
            False,
            prospective_pair_cost,
            f"Hedge at market would cost ${prospective_pair_cost:.3f} > ${max_pair_cost:.3f}"
        )

    # Allow if hedge is still profitable
    profit = 1.0 - prospective_pair_cost
    return (
        True,
        prospective_pair_cost,
        f"OK - hedge at market costs ${prospective_pair_cost:.3f} (profit ${profit:.3f})"
    )


def get_dynamic_target_shares(
    base_target: int,
    trend_state: 'TrendState',
    time_remaining_secs: float = 900.0,
) -> int:
    """
    Reduce target shares in trending conditions.

    Answers user question: "How to stop at 10/10 instead of 15/15 if
    prospective pair cost exceeds limit?"

    Args:
        base_target: Normal target shares (e.g., 15)
        trend_state: Current trend state from TrendDetector
        time_remaining_secs: Time left in market (for additional late reduction)

    Returns:
        Adjusted target shares

    Examples:
        >>> from src.services.trend_detector import TrendState
        >>> get_dynamic_target_shares(15, TrendState.NEUTRAL)
        15
        >>> get_dynamic_target_shares(15, TrendState.STRONG)
        10
        >>> get_dynamic_target_shares(15, TrendState.EXTREME)
        5  # Rounded down to multiple of 5
        >>> get_dynamic_target_shares(15, TrendState.STRONG, time_remaining_secs=120)
        5  # Further reduced late in market, rounded to multiple of 5
    """
    # Import here to avoid circular dependency
    try:
        from src.services.trend_detector import TrendState
    except ImportError:
        # Fallback if TrendDetector not available
        return base_target

    # Reduction factors by trend state
    reductions = {
        TrendState.NEUTRAL: 1.0,   # 15/15 - full target
        TrendState.MILD: 0.85,     # ~12.75 → 10 (rounded down)
        TrendState.STRONG: 0.67,   # ~10 - significant reduction
        TrendState.EXTREME: 0.50,  # ~7.5 → 5 (rounded down)
    }

    factor = reductions.get(trend_state, 1.0)

    # Further reduce if late in market (less time to hedge safely)
    if time_remaining_secs < 300:  # Last 5 minutes
        factor *= 0.8

    raw_target = int(base_target * factor)

    # Round DOWN to nearest multiple of SIZE_INCREMENT (5)
    # e.g., 12 → 10, 7 → 5, 10 → 10
    rounded_target = (raw_target // SIZE_INCREMENT) * SIZE_INCREMENT

    return max(MIN_ORDER_SIZE, rounded_target)


def calculate_fair_value(
    price_vs_strike_pct: float,
    time_remaining_secs: float,
    sensitivity_early: float = 0.10,
    sensitivity_late: float = 0.50,
) -> Tuple[float, float]:
    """
    Calculate fair value for UP and DOWN based on Binance signal.

    For 15-min BTC binary markets:
    - BTC above strike → UP wins ($1), DOWN loses ($0)
    - Fair value = probability of winning

    The sensitivity increases as time runs out:
    - Early: small adjustment (time for reversal)
    - Late: large adjustment (direction likely final)

    Args:
        price_vs_strike_pct: BTC % change from strike (e.g., +0.5 means BTC up 0.5%)
        time_remaining_secs: Seconds until resolution (0-900)
        sensitivity_early: Price sensitivity at market open (default 0.10)
        sensitivity_late: Price sensitivity near resolution (default 0.50)

    Returns:
        (fair_up, fair_down): Fair values for each side

    Examples:
        >>> calculate_fair_value(0.0, 900)  # At strike, 15 min left
        (0.50, 0.50)
        >>> calculate_fair_value(0.5, 900)  # BTC +0.5%, 15 min left
        (0.55, 0.45)
        >>> calculate_fair_value(0.5, 300)  # BTC +0.5%, 5 min left
        (0.65, 0.35)
        >>> calculate_fair_value(1.0, 120)  # BTC +1%, 2 min left
        (0.80, 0.20)
    """
    # Clamp time to valid range
    time_remaining_secs = max(0, min(MARKET_DURATION, time_remaining_secs))

    # Sensitivity increases as time runs out (linear interpolation)
    # Early (900s): sensitivity_early, Late (0s): sensitivity_late
    time_factor = 1 - (time_remaining_secs / MARKET_DURATION)
    sensitivity = sensitivity_early + (sensitivity_late - sensitivity_early) * time_factor

    # Calculate fair value adjustment
    # price_vs_strike_pct is already a percentage (e.g., 0.5 = 0.5%)
    # We convert to probability adjustment (e.g., +0.5% with sensitivity 0.10 = +0.05 probability)
    adjustment = price_vs_strike_pct * sensitivity

    # Calculate fair values
    fair_up = 0.50 + adjustment
    fair_down = 0.50 - adjustment

    # Clamp to valid range [0.05, 0.95] - never go to extremes
    fair_up = max(0.05, min(0.95, fair_up))
    fair_down = max(0.05, min(0.95, fair_down))

    return fair_up, fair_down


# =============================================================================
# STRATEGY CLASS
# =============================================================================

@dataclass
class StrategyDecision:
    """Decision output from strategy."""
    should_trade: bool
    price: float
    size: int
    reason: str
    threshold: float
    mispricing: float


class CalculusMakerStrategy:
    """
    Calculus MAKER Strategy (Strategy 4)

    A modular trading strategy using exponential decay pricing
    and quadratic size ramp for BTC 15-minute markets.

    Attributes:
        max_shares: Maximum order size per trade
        min_shares: Minimum order size per trade (Polymarket minimum = 5)
        max_pair_cost: Maximum pair cost to accept (default 1.0)

    SIZING: Small early (5 shares), ramp up late (15 shares max)
        - Start small to test fills and avoid immediate imbalance
        - Ramp up as time runs out to complete position

    Example:
        strategy = CalculusMakerStrategy(max_shares=15)

        decision = strategy.evaluate(
            best_bid=0.45,
            best_ask=0.48,
            pair_cost=0.97,
            time_remaining=300
        )

        if decision.should_trade:
            place_order(price=decision.price, size=decision.size)
    """

    def __init__(
        self,
        max_shares: int = DEFAULT_MAX_SHARES,
        min_shares: int = DEFAULT_MIN_SHARES,
        max_pair_cost: float = 1.0,
        m_min: float = None,
        m_max: float = None,
        lambda_decay: float = None,
    ):
        self.max_shares = max_shares
        # Ensure min_shares is a valid multiple of SIZE_INCREMENT
        raw_min = max(min_shares, MIN_ORDER_SIZE)
        self.min_shares = max(MIN_ORDER_SIZE, SIZE_INCREMENT * round(raw_min / SIZE_INCREMENT))
        self.max_pair_cost = max_pair_cost

        # Instance-level threshold params (avoids global mutation)
        self.m_min = m_min if m_min is not None else M_MIN
        self.m_max = m_max if m_max is not None else M_MAX
        self.lambda_decay = lambda_decay if lambda_decay is not None else LAMBDA

    def get_threshold(self, time_remaining: float) -> float:
        """Get mispricing threshold using instance parameters."""
        t = max(0, min(time_remaining, MARKET_DURATION))
        return self.m_min + (self.m_max - self.m_min) * math.exp(
            -self.lambda_decay * (MARKET_DURATION - t)
        )

    def get_size(self, time_remaining: float) -> int:
        """Get order size for current time (small early, ramp up late)."""
        return get_dynamic_size(
            time_remaining, self.max_shares, self.min_shares, inverted=False
        )

    def get_price(
        self,
        best_bid: float,
        time_remaining: float,
        is_emergency: bool = False,
        best_ask: Optional[float] = None
    ) -> float:
        """Get patient bid price for current time."""
        return get_calculus_price(best_bid, time_remaining, is_emergency, best_ask)

    def should_buy(self, pair_cost: float, time_remaining: float) -> bool:
        """Check if should buy at current conditions (uses instance params)."""
        if pair_cost > self.max_pair_cost:
            return False
        threshold = self.get_threshold(time_remaining)
        mispricing = 1.0 - pair_cost
        return mispricing >= threshold

    def evaluate(
        self,
        best_bid: float,
        best_ask: float,
        pair_cost: float,
        time_remaining: float,
        is_emergency: bool = False
    ) -> StrategyDecision:
        """
        Evaluate market conditions and return trading decision.

        Args:
            best_bid: Current best bid price
            best_ask: Current best ask price
            pair_cost: Current pair cost (UP_ask + DOWN_ask)
            time_remaining: Seconds until market resolution
            is_emergency: If True, use aggressive TAKER pricing

        Returns:
            StrategyDecision with should_trade, price, size, and reason
        """
        threshold = self.get_threshold(time_remaining)
        mispricing = 1.0 - pair_cost

        # Check pair cost limit
        if pair_cost > self.max_pair_cost:
            return StrategyDecision(
                should_trade=False,
                price=0.0,
                size=0,
                reason=f"Pair cost ${pair_cost:.3f} > max ${self.max_pair_cost:.3f}",
                threshold=threshold,
                mispricing=mispricing
            )

        # Check mispricing threshold
        if mispricing < threshold:
            return StrategyDecision(
                should_trade=False,
                price=0.0,
                size=0,
                reason=f"Mispricing {mispricing:.1%} < threshold {threshold:.1%}",
                threshold=threshold,
                mispricing=mispricing
            )

        # Calculate price and size
        price = self.get_price(best_bid, time_remaining, is_emergency, best_ask)
        size = self.get_size(time_remaining)

        return StrategyDecision(
            should_trade=True,
            price=price,
            size=size,
            reason=f"Mispricing {mispricing:.1%} >= threshold {threshold:.1%}",
            threshold=threshold,
            mispricing=mispricing
        )

    def get_curve_values(self) -> dict:
        """
        Get strategy curve values at key time points.

        Useful for visualization and debugging.

        Returns:
            Dict with time points and corresponding values
        """
        times = [900, 750, 600, 450, 300, 120, 60, 30, 0]
        values = []

        for t in times:
            values.append({
                "time_remaining": t,
                "time_label": f"{t // 60}m {t % 60}s" if t % 60 else f"{t // 60}m",
                "threshold": round(self.get_threshold(t), 4),
                "max_pair_cost": round(1.0 - self.get_threshold(t), 4),
                "size": self.get_size(t),
            })

        return {
            "strategy": "calculus_maker",
            "parameters": {
                "m_min": M_MIN,
                "m_max": M_MAX,
                "lambda": LAMBDA,
                "max_shares": self.max_shares,
                "min_shares": self.min_shares,
            },
            "curve": values
        }

    # =========================================================================
    # VELOCITY-BASED ORDER MANAGEMENT
    # =========================================================================

    def should_pull_entry(self, velocity_bps: float, entry_side: str) -> bool:
        """
        Check if entry order should be pulled due to adverse velocity.

        Pull entry if velocity is moving AGAINST our entry side:
        - UP entry: adverse if velocity < 0 (BTC falling, UP getting expensive)
        - DOWN entry: adverse if velocity > 0 (BTC rising, DOWN getting expensive)

        Args:
            velocity_bps: Current velocity in basis points per second
            entry_side: "UP" or "DOWN" - which side we entered

        Returns:
            True if entry should be pulled
        """
        if entry_side.upper() == "UP":
            return velocity_bps < -VELOCITY_PULL_THRESHOLD
        else:  # DOWN
            return velocity_bps > VELOCITY_PULL_THRESHOLD

    def should_hedge_now(
        self,
        velocity_bps: float,
        hedge_side: str,
        time_since_entry_fill: float,
        time_remaining: float,
    ) -> tuple[bool, str]:
        """
        Check if we should hedge now or "let it ride".

        "Let it ride" logic:
        - After entry fills, DON'T hedge immediately
        - Wait while velocity is FAVORABLE (hedge getting cheaper)
        - Hedge when velocity REVERSES (hedge getting expensive)
        - Force hedge on timeout or near market expiry

        Args:
            velocity_bps: Current velocity in basis points per second
            hedge_side: "UP" or "DOWN" - which side we need to hedge
            time_since_entry_fill: Seconds since entry was filled
            time_remaining: Seconds until market resolution

        Returns:
            (should_hedge, reason) tuple
        """
        # Safety: Force hedge near market expiry
        if time_remaining < MIN_TIME_TO_FORCE_HEDGE:
            return True, f"Force hedge: time_remaining={time_remaining:.0f}s < {MIN_TIME_TO_FORCE_HEDGE}s"

        # Safety: Force hedge after max wait time
        if time_since_entry_fill > MAX_HEDGE_WAIT_SECS:
            return True, f"Force hedge: waited {time_since_entry_fill:.0f}s > {MAX_HEDGE_WAIT_SECS}s"

        # "Let it ride" logic based on velocity direction
        # Hedge when velocity REVERSES (no longer favorable)
        if hedge_side.upper() == "DOWN":
            # DOWN hedge: wait while velocity negative (BTC falling, DOWN cheap)
            # Hedge when velocity turns positive (reversal - DOWN getting expensive)
            if velocity_bps > VELOCITY_PULL_THRESHOLD:
                return True, f"Hedge now: velocity reversal vel={velocity_bps:.3f}bps (DOWN getting expensive)"
            else:
                return False, f"Let it ride: vel={velocity_bps:.3f}bps (DOWN still cheap)"
        else:  # hedge_side == "UP"
            # UP hedge: wait while velocity positive (BTC rising, UP cheap)
            # Hedge when velocity turns negative (reversal - UP getting expensive)
            if velocity_bps < -VELOCITY_PULL_THRESHOLD:
                return True, f"Hedge now: velocity reversal vel={velocity_bps:.3f}bps (UP getting expensive)"
            else:
                return False, f"Let it ride: vel={velocity_bps:.3f}bps (UP still cheap)"

    def __repr__(self) -> str:
        return (
            f"CalculusMakerStrategy("
            f"max_shares={self.max_shares}, "
            f"min_shares={self.min_shares}, "
            f"max_pair_cost={self.max_pair_cost})"
        )


# =============================================================================
# COMPARISON HELPER (for frontend)
# =============================================================================

def compare_with_vw(time_remaining: float, best_bid: float = 0.50) -> dict:
    """
    Compare Calculus MAKER vs Standard VW MAKER at a given time.

    Args:
        time_remaining: Seconds until market resolution
        best_bid: Reference bid price for comparison

    Returns:
        Dict comparing both strategies
    """
    # Calculus MAKER
    calc_threshold = get_mispricing_threshold(time_remaining)
    calc_price = best_bid - calc_threshold
    calc_size = get_dynamic_size(time_remaining)

    # Standard VW MAKER (step function)
    if time_remaining >= 600:
        vw_offset = 0.03
    elif time_remaining >= 300:
        vw_offset = 0.02
    elif time_remaining >= 120:
        vw_offset = 0.01
    else:
        vw_offset = 0.00

    vw_price = best_bid - vw_offset

    # VW size (decay - larger early) - approximate
    if time_remaining >= 300:
        vw_percent = 0.10 + 0.10 * math.sqrt((time_remaining - 300) / 600)
    else:
        vw_percent = 0.02 + 0.08 * ((time_remaining / 300) ** 2)
    vw_size = max(5, int(vw_percent * 50))

    return {
        "time_remaining": time_remaining,
        "best_bid": best_bid,
        "calculus_maker": {
            "offset": round(calc_threshold, 4),
            "price": round(calc_price, 4),
            "size": calc_size,
            "max_pair_cost": round(1.0 - calc_threshold, 4),
        },
        "vw_maker": {
            "offset": vw_offset,
            "price": round(vw_price, 4),
            "size": vw_size,
            "max_pair_cost": round(1.0 - vw_offset, 4) if vw_offset > 0 else 1.0,
        }
    }
