"""
Core Trading Utilities - Single Source of Truth for LOGIC

=============================================================================
BOTH live strategy (src/strategies/) AND backtests (research/) import from here.
This ensures identical logic across live trading and backtesting.
=============================================================================

Usage:
    from src.core import (
        # Fee model
        polymarket_taker_fee,
        calculate_pnl_with_fees,

        # Signal filters
        velocity_confirms_spike,
        obi_confirms_spike,
        should_take_spike_enhanced,
        compute_enhanced_score,

        # Calculations
        calculate_loser_bid,

        # Multi-cycle direction modes
        DIRECTION_MODE_SINGLE,
        DIRECTION_MODE_BUILD,
        DIRECTION_MODE_CLEAR,
        can_enter_direction,

        # Data classes
        TradeResult,
        BacktestCycle,

        # Constants
        VELOCITY_CONFIRM_THRESHOLD,
        ENHANCED_SCORE_THRESHOLD,
    )

For parameters (lookback, time_stop, z_scores, etc.), use:
    from research.reference.TRADING_CONFIGS import AGGRESSIVE
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional, Tuple


# =============================================================================
# FEE MODEL
# =============================================================================

def polymarket_taker_fee(price: float) -> float:
    """
    Polymarket taker fee: 1.56% * (1 - |2*price - 1|).

    Fee is highest at price=0.50 (0.0156) and lowest at extremes (0.00).

    Args:
        price: Trade price (0.01 to 0.99)

    Returns:
        Fee rate as decimal (e.g., 0.0156 for 1.56%)
    """
    return 0.0156 * (1 - abs(2 * price - 1))


def calculate_pnl_with_fees(
    winner_entry: float,
    loser_fill: float,
    shares: int,
    is_taker_entry: bool,
    is_taker_exit: bool
) -> Tuple[float, float, float, float]:
    """
    Calculate PnL with proper maker/taker fee handling.

    Args:
        winner_entry: Price paid for winner side
        loser_fill: Price received for loser side
        shares: Number of shares traded
        is_taker_entry: True if entry was taker (market order)
        is_taker_exit: True if exit was taker (market order)

    Returns:
        Tuple of (pnl_net, pnl_gross, entry_fee, exit_fee)
        - pnl_gross: Raw profit before fees
        - pnl_net: Profit after fees
        - entry_fee: Fee paid on entry
        - exit_fee: Fee paid on exit
    """
    pair_cost = winner_entry + loser_fill
    pnl_gross = (1.0 - pair_cost) * shares

    entry_fee = polymarket_taker_fee(winner_entry) * winner_entry * shares if is_taker_entry else 0
    exit_fee = polymarket_taker_fee(loser_fill) * loser_fill * shares if is_taker_exit else 0

    pnl_net = pnl_gross - entry_fee - exit_fee
    return pnl_net, pnl_gross, entry_fee, exit_fee


# =============================================================================
# SIGNAL FILTERS
# =============================================================================

# Default thresholds (can be overridden by importing module)
VELOCITY_CONFIRM_THRESHOLD = 0.10  # bps/sec threshold for velocity confirmation
ENHANCED_SCORE_THRESHOLD = 0.40    # Minimum composite score to trade


def velocity_confirms_spike(spike_dir: str, velocity_bps: float,
                            threshold: float = VELOCITY_CONFIRM_THRESHOLD) -> bool:
    """
    Check if velocity does NOT contradict spike direction.

    CRITICAL FILTER (Jan 17, 2026 - signal_based_mm_analysis.py):
    - Spike UP + Velocity confirms (v > 0.1): 69% accuracy
    - Spike UP + Velocity contradicts (v < -0.1): 14% accuracy (REJECT!)
    - Spike DOWN + Velocity confirms (v < -0.1): 82% accuracy
    - Spike DOWN + Velocity contradicts (v > 0.1): 43% accuracy (REJECT!)

    This single filter improves hourly rate from $2.37/hr to $7.54/hr (+218%).

    Args:
        spike_dir: "UP" or "DOWN"
        velocity_bps: Velocity in basis points per second
        threshold: Velocity threshold for rejection (default 0.10 bps)

    Returns:
        True if velocity doesn't contradict spike (trade allowed)
        False if velocity strongly contradicts spike (reject trade)

    Note:
        The logic rejects when velocity CONTRADICTS, not when it doesn't confirm.
        - UP spike rejected only if velocity < -threshold (strongly bearish)
        - DOWN spike rejected only if velocity > threshold (strongly bullish)
    """
    if spike_dir == "UP":
        return velocity_bps > -threshold  # Reject if velocity strongly negative
    elif spike_dir == "DOWN":
        return velocity_bps < threshold   # Reject if velocity strongly positive
    return True


def obi_confirms_spike(spike_dir: str, up_imbalance: Optional[float],
                       down_imbalance: Optional[float]) -> Tuple[bool, bool]:
    """
    Check if Order Book Imbalance confirms spike direction (binary check).

    CRITICAL FINDING (Jan 31, 2026 - ML_SPIKE_QUALITY_ANALYSIS.md):
    OBI predicts HEDGE QUALITY, not just direction accuracy.

    Good Spike Rates by OBI Condition:
    - When OBI confirms spike: 49.4% good spike rate
    - When OBI disagrees: 31.0% good spike rate
    - Improvement: +18 percentage points

    OBI Magnitude Bins (Good Spike Rate):
    - Strong Sell (< -0.3): 19.3% (WORST - skip these!)
    - Mild Sell (-0.3 to -0.1): 40.5%
    - Neutral (-0.1 to 0.1): 49.0%
    - Mild Buy (0.1 to 0.3): 54.4% (BEST)
    - Strong Buy (> 0.3): 47.5%

    Args:
        spike_dir: "UP" or "DOWN"
        up_imbalance: OBI for up side (positive = buying pressure)
        down_imbalance: OBI for down side (positive = buying pressure)

    Returns:
        Tuple of (obi_available, obi_confirms)
        - obi_available: True if OBI data is present and valid
        - obi_confirms: True if OBI matches spike direction

    Reference: research/findings/ML_SPIKE_QUALITY_ANALYSIS.md
    """
    if spike_dir == "UP":
        if up_imbalance is not None and not np.isnan(up_imbalance):
            return True, up_imbalance > 0
    elif spike_dir == "DOWN":
        if down_imbalance is not None and not np.isnan(down_imbalance):
            return True, down_imbalance > 0
    return False, True  # Not available = don't filter


def should_take_spike_enhanced(
    spike_direction: str,
    obi_winner: float,
    loser_spread: float = 0.05,
    time_remaining: float = 600.0,
    winner_ask_depth: Optional[float] = None,
) -> Tuple[bool, str]:
    """
    Simple binary OBI check - does orderbook confirm spike direction?

    REVERTED (Jan 31, 2026): Removed spread/time/depth filters.
    Those filters rejected 93% of trades based on weak correlations (+0.19).
    Simple binary OBI check is sufficient:
    - OBI confirms spike: 49% good spike rate
    - OBI disagrees: 31% good spike rate
    - Improvement: +18 percentage points

    Args:
        spike_direction: "UP" or "DOWN"
        obi_winner: Orderbook imbalance for winner side (-1 to +1)
        loser_spread: IGNORED (kept for API compatibility)
        time_remaining: IGNORED (kept for API compatibility)
        winner_ask_depth: IGNORED (kept for API compatibility)

    Returns:
        Tuple of (should_take, reason)
    """
    # Simple binary OBI check: does orderbook agree with spike?
    if obi_winner <= 0:
        return False, f"OBI disagrees (obi={obi_winner:.3f})"

    return True, "OBI confirms"


def compute_enhanced_score(spike_mag: float, velocity_bps: float,
                           spike_dir: str, time_remaining: float) -> float:
    """
    Compute composite score for spike quality (0-1 scale, threshold 0.40).

    Score formula (from backtest optimization, Jan 17, 2026):
        0.40 * spike_magnitude_score +
        0.30 * velocity_strength_score +
        0.20 * velocity_confirmation_bonus +
        0.10 * urgency_score

    Component calculations:
        - spike_magnitude_score: min(magnitude / 0.05, 1.0)
          Maps 0-5% spike to 0-1 scale
        - velocity_strength_score: min(abs(velocity) / 0.50, 1.0)
          Maps 0-50 bps/sec to 0-1 scale
        - confirmation_bonus: 1.0 if velocity matches spike direction, else 0.0
        - urgency_score: 1.0 - min(time_remaining / 900.0, 1.0)
          Higher urgency as market approaches resolution

    Args:
        spike_mag: Absolute BTC percentage change (e.g., 0.05 for 0.05%)
        velocity_bps: Current velocity in basis points per second
        spike_dir: "UP" or "DOWN"
        time_remaining: Seconds until market resolution

    Returns:
        Composite score [0.0, 1.0]. Trade if score >= 0.40.

    Reference: src/strategies/enhanced_spike.py (Jan 17, 2026)
    """
    # Spike magnitude score: 0-5% maps to 0-1
    spike_score = min(spike_mag / 0.05, 1.0)

    # Velocity strength score: 0-0.5 bps maps to 0-1
    velocity_score = min(abs(velocity_bps) / 0.50, 1.0)

    # Confirmation bonus: 1.0 if velocity confirms spike direction
    vel_confirms = (spike_dir == "UP" and velocity_bps > 0) or \
                   (spike_dir == "DOWN" and velocity_bps < 0)
    confirm_bonus = 1.0 if vel_confirms else 0.0

    # Urgency score: higher as market approaches resolution
    urgency = 1.0 - min(time_remaining / 900.0, 1.0)

    # Weighted composite
    score = (0.40 * spike_score +
             0.30 * velocity_score +
             0.20 * confirm_bonus +
             0.10 * urgency)

    return round(score, 3)


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class TradeResult:
    """
    Standard trade result with fee tracking.

    Used by both backtests and live trading for consistent result format.
    """
    market_slug: str
    cycle_num: int
    entry_time_remaining: float
    signal_score: float
    winner_side: str
    winner_fill_price: float
    loser_fill_price: float
    hedge_type: str  # "passive", "time_stop", or "resolution"
    pair_cost: float
    pnl_gross: float
    pnl_net: float
    entry_fee: float
    exit_fee: float
    correct_direction: bool
    spike_magnitude: float
    dataset: str
    # Optional fields with defaults
    offset_name: str = "CURRENT"
    cycle_mode: str = "SINGLE"  # "SINGLE" or "MULTI"
    shares: int = 50


@dataclass
class BacktestCycle:
    """
    Track a single cycle in multi-cycle mode.

    Used for managing multiple concurrent trading cycles.
    """
    cycle_id: int
    entry_ts: int  # Entry timestamp in milliseconds
    winner_side: str
    loser_side: str
    winner_entry: float
    loser_target: float
    entry_time_rem: float
    spike_magnitude: float
    score: float
    shares: int


# =============================================================================
# LOSER BID CALCULATION
# =============================================================================

def calculate_loser_bid(
    winner_entry: float,
    spike_magnitude: float,
    drop_multiplier: float = 0.50,
    drop_intercept: float = 0.08,
    target_pair_cost: float = 0.99,
) -> float:
    """
    Calculate optimal loser bid price based on spike magnitude.

    Uses linear model recalibrated Jan 18, 2026:
        expected_drop = drop_multiplier * spike_magnitude + drop_intercept

    Model changes (v1 → v2):
        - v1 (old): multiplier=0.68, intercept=0.01
          → UNDERPREDICTED drops (predicted 0.03, actual 0.10)
        - v2 (current): multiplier=0.50, intercept=0.08
          → ACCURATE prediction

    The reduced multiplier (0.68→0.50) indicates spike magnitude has weak
    predictive power for actual loser drop. The higher intercept (0.01→0.08)
    matches observed mean drop better.

    Args:
        winner_entry: Price paid for winner side
        spike_magnitude: Absolute BTC % change (e.g., 0.05 for 0.05%)
        drop_multiplier: Linear model slope (default 0.50)
        drop_intercept: Linear model intercept (default 0.08)
        target_pair_cost: Target for winner+loser cost (default 0.99)

    Returns:
        Optimal loser bid price, clamped to [0.01, 0.95]

    Reference: research/HEDGE_PRICING_FINDINGS.md (Jan 18, 2026)
    """
    expected_drop = drop_multiplier * spike_magnitude + drop_intercept
    max_loser = target_pair_cost - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid))


# =============================================================================
# MULTI-CYCLE DIRECTION MODES - DEPRECATED Jan 31, 2026
# =============================================================================
#
# ABANDONED: Multi-cycle destroyed profitability even with direction consistency fix.
# - SINGLE mode: 54.3% win rate, +$1.37/hr
# - MULTI modes: 39.8% win rate, -$26.70/hr (10x trades, 15pp lower win rate)
#
# Root cause: Stacking same-direction trades catches weak follow-on spikes that
# dilute edge. The first spike is a strong signal; subsequent spikes are noise.
#
# LIVE TRADING: Use SINGLE-CYCLE ONLY (enable_multicycle=False in TRADING_CONFIGS.py)
# =============================================================================

# Direction mode constants - DEPRECATED, only SINGLE is live-ready
DIRECTION_MODE_SINGLE = "single"  # LIVE-READY: 1 cycle at a time
DIRECTION_MODE_BUILD = "build"    # DEPRECATED: destroyed profitability
DIRECTION_MODE_CLEAR = "clear"    # DEPRECATED: destroyed profitability


def can_enter_direction(
    spike_direction: str,
    active_cycles: list,
    direction_mode: str = DIRECTION_MODE_SINGLE,
) -> Tuple[bool, str]:
    """
    DEPRECATED - Multi-cycle abandoned Jan 31, 2026.

    Check if new entry is allowed based on direction mode and active cycles.

    WARNING: Multi-cycle modes (BUILD, CLEAR) destroyed profitability:
        - SINGLE: 54.3% win rate, +$1.37/hr (LIVE-READY)
        - MULTI: 39.8% win rate, -$26.70/hr (ABANDONED)

    This function is kept for backwards compatibility but should NOT be used
    in production. Live trading uses SINGLE-CYCLE ONLY via TRADING_CONFIGS.py.

    Args:
        spike_direction: "UP" or "DOWN" - direction of new spike
        active_cycles: List of active BacktestCycle objects (must have winner_side attr)
        direction_mode: "single", "build", or "clear" (only "single" is live-ready)

    Returns:
        Tuple of (can_enter, reason)
            - can_enter: True if entry is allowed
            - reason: Human-readable explanation for logging
    """
    if direction_mode == DIRECTION_MODE_SINGLE:
        return True, "Single-cycle mode"

    if not active_cycles:
        return True, "No active cycles"

    # Get direction from first active cycle (all should be same direction)
    existing_direction = active_cycles[0].winner_side

    if spike_direction == existing_direction:
        return True, f"Same direction as existing ({existing_direction})"

    # Opposite direction - behavior depends on mode
    if direction_mode == DIRECTION_MODE_BUILD:
        return False, f"BUILD mode: Skipping {spike_direction} while holding {existing_direction}"

    elif direction_mode == DIRECTION_MODE_CLEAR:
        return False, f"CLEAR mode: Waiting for {len(active_cycles)} {existing_direction} cycle(s) to close"

    # Unknown mode - allow by default (shouldn't happen)
    return True, f"Unknown mode '{direction_mode}' - allowing"
