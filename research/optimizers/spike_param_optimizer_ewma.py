#!/usr/bin/env python3
"""
Enhanced Spike Parameter Optimization - PURE EWMA THRESHOLD VERSION

This optimizer uses pure EWMA (Exponential Weighted Moving Average) for adaptive
thresholds instead of OU (Ornstein-Uhlenbeck) process.

EWMA Threshold Logic:
    - EWMA_fast: Short-term volatility (e.g., 60s halflife)
    - EWMA_slow: Baseline volatility (e.g., 300s halflife)
    - ratio = EWMA_fast / EWMA_slow
    - threshold = base_threshold * ratio (bounded by min/max)

Simpler than OU - no calibration needed, self-adapting baseline.

Taker Fee Formula (Polymarket):
    fee_rate = 0.0156 * (1 - abs(2 * price - 1))
    Max 1.56% at 50% probability, decreases toward extremes

Tests all parameter combinations to find optimal settings for the enhanced spike strategy.

Parameter Grid:
- target_shares: 5, 10, 15, 30 (TOTAL shares per trade, split across grid levels)
- grid_levels: 1, 2, 3 (e.g., 30 total / 3 levels = 10 per level)
- grid_spacing: 0.01, 0.02 (only when levels > 1)
- spike_lookback: 18, 30, 36, 48, 60, 72 ticks (300/500/600/800/1000/1200ms at 60Hz)
- stop_loss: 0.03, 0.05, 0.07, 0.12, None (added 3% and 5% for tighter SL testing)
- order_pulling: True, False (entry order pulling)
- entry_order_pull_timeout: 3s, 5s, 7s, 10s, 15s, 20s, 25s, 30s (Path 1: cancel stale entries)

Note: Configurations where target_shares % grid_levels != 0 are skipped.
"""

import argparse
import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from datetime import datetime
import multiprocessing as mp
import time
import sys
import math

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# =============================================================================
# CONSTANTS
# =============================================================================

# Fixed parameters
SIGNAL_TYPE = "enhanced"
MIN_TIME = 60  # Minimum seconds remaining to enter
MIN_RUNTIME_SECS = 300  # Minimum market duration

# Spike detection (fixed)
SPIKE_THRESHOLD = 0.02  # Base threshold

# Enhanced filtering (fixed)
VELOCITY_CONFIRM_THRESHOLD = 0.10
ENHANCED_SCORE_THRESHOLD = 0.005  # v2 formula: spike_mag * velocity_bps (lowered from 0.02)

# Loser bid calculation (v2: based on hedge_pricing_analysis.py regression results)
# Analysis findings: actual 60s drop mean = 0.10, spike_magnitude has no predictive power (r=-0.01)
# Old formula (0.68 * spike + 0.01) severely underpredicted drops (predicted 0.03, actual 0.10)
DROP_MULTIPLIER = 0.50  # Reduced from 0.68 - spike has weak predictive power
DROP_INTERCEPT = 0.08   # Increased from 0.01 - matches actual mean drop better
DROP_REGIME_BONUS = {'LOW': 0.0, 'MEDIUM': 0.01, 'HIGH': 0.02}  # Regime adjustment
TARGET_PAIR_COST = 0.99

# Cycling
MIN_CYCLE_GAP_MS = 200  # Matches observer data rate (5Hz), allows fast cycling

# Capital constraint (for flagging, not blocking)
CAPITAL_LIMIT = 170.0
AVG_PAIR_COST = 0.99  # Approximate cost per share pair

# ATR for adaptive volatility
ATR_PERIOD = 14
ATR_WINDOW = 300
LOW_PERCENTILE = 25
HIGH_PERCENTILE = 75
REGIME_THRESHOLDS = {
    "LOW": 0.010,
    "MEDIUM": 0.020,
    "HIGH": 0.035,
}

# EWMA-based adaptive threshold parameters
EWMA_BASE_THRESHOLD = 0.02
EWMA_MIN_THRESHOLD = 0.010  # Floor
EWMA_MAX_THRESHOLD = 0.10   # Ceiling
EWMA_MIN_RATIO = 0.5        # Minimum multiplier
EWMA_MAX_RATIO = 3.0        # Maximum multiplier


class EWMAThresholdTracker:
    """
    Pure EWMA-based adaptive threshold.

    Uses two EWMA windows:
    - Fast EWMA: tracks current volatility
    - Slow EWMA: tracks baseline volatility
    - Ratio = fast/slow determines threshold multiplier
    """

    def __init__(self, fast_halflife_sec: float = 60, slow_halflife_sec: float = 300,
                 tick_interval_sec: float = 1/60):
        self.fast_halflife = fast_halflife_sec
        self.slow_halflife = slow_halflife_sec
        self.tick_interval = tick_interval_sec

        # Compute decay factors (alpha = 1 - exp(-ln(2) * dt / halflife))
        self.fast_alpha = 1 - math.exp(-math.log(2) * tick_interval_sec / fast_halflife_sec)
        self.slow_alpha = 1 - math.exp(-math.log(2) * tick_interval_sec / slow_halflife_sec)

        self.fast_var = None  # EWMA of squared returns
        self.slow_var = None
        self.last_price = None
        self.initialized = False

    def update(self, price: float) -> float:
        """Update with new price, return adaptive threshold."""
        if self.last_price is None:
            self.last_price = price
            return EWMA_BASE_THRESHOLD

        # Compute return
        ret = (price - self.last_price) / self.last_price
        ret_sq = ret * ret
        self.last_price = price

        # Initialize or update EWMA
        if self.fast_var is None:
            self.fast_var = ret_sq
            self.slow_var = ret_sq
            return EWMA_BASE_THRESHOLD

        self.fast_var = self.fast_alpha * ret_sq + (1 - self.fast_alpha) * self.fast_var
        self.slow_var = self.slow_alpha * ret_sq + (1 - self.slow_alpha) * self.slow_var

        # Compute ratio (with floor to avoid division issues)
        slow_vol = math.sqrt(max(self.slow_var, 1e-12))
        fast_vol = math.sqrt(max(self.fast_var, 1e-12))

        ratio = fast_vol / slow_vol if slow_vol > 1e-8 else 1.0
        ratio = max(EWMA_MIN_RATIO, min(EWMA_MAX_RATIO, ratio))

        # Compute threshold
        threshold = EWMA_BASE_THRESHOLD * ratio
        return max(EWMA_MIN_THRESHOLD, min(EWMA_MAX_THRESHOLD, threshold))

    def get_current_ratio(self) -> float:
        """Get current fast/slow ratio."""
        if self.fast_var is None or self.slow_var is None:
            return 1.0
        slow_vol = math.sqrt(max(self.slow_var, 1e-12))
        fast_vol = math.sqrt(max(self.fast_var, 1e-12))
        return fast_vol / slow_vol if slow_vol > 1e-8 else 1.0


def compute_ewma_thresholds_for_btc(btc_df: pd.DataFrame, fast_halflife: float, slow_halflife: float) -> pd.Series:
    """
    Pre-compute EWMA thresholds for all BTC prices.

    Returns a Series indexed by timestamp_ms with threshold values.
    """
    tracker = EWMAThresholdTracker(fast_halflife_sec=fast_halflife, slow_halflife_sec=slow_halflife)
    thresholds = []

    for price in btc_df['price'].values:
        threshold = tracker.update(price)
        thresholds.append(threshold)

    return pd.Series(thresholds, index=btc_df['timestamp_ms'].values)

# =============================================================================
# TAKER FEE CALCULATION
# =============================================================================

def get_taker_fee_rate(price: float) -> float:
    """
    Calculate taker fee rate based on Polymarket formula.

    Fee = 0.0156 * (1 - |2*price - 1|)

    Examples:
        price = 0.50 -> fee = 1.56%
        price = 0.40 -> fee = 1.25%
        price = 0.90 -> fee = 0.31%
        price = 0.10 -> fee = 0.31%

    Args:
        price: Entry/exit price (0.01 to 0.99)

    Returns:
        Fee rate as decimal (e.g., 0.0156 for 1.56%)
    """
    return 0.0156 * (1 - abs(2 * price - 1))


def calculate_taker_fee(price: float, shares: int) -> float:
    """
    Calculate total taker fee for a trade.

    Args:
        price: Entry/exit price
        shares: Number of shares

    Returns:
        Total fee in dollars
    """
    fee_rate = get_taker_fee_rate(price)
    return fee_rate * price * shares


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class OptConfig:
    """Single parameter configuration to test."""
    target_shares: int    # Total shares per trade (e.g., 30)
    grid_levels: int      # How many levels to split into (e.g., 3)
    grid_spacing: float
    spike_lookback: int   # ticks at 60Hz
    stop_loss_pct: Optional[float]
    order_pulling: bool
    # EWMA adaptive threshold parameters
    ewma_fast_halflife: float = 60.0   # Fast EWMA halflife in seconds
    ewma_slow_halflife: float = 300.0  # Slow EWMA halflife in seconds
    # NOTE: order_pull_timeout (40s) was REMOVED - it was never used for hedge orders
    # Hedge orders either fill passively, get stopped out, or ride to resolution
    entry_order_pull_timeout: float = 10.0  # seconds (entry order timeout - Path 1)
    grid_buycount: int = 1  # How many buy cycles to reach target_shares (1=oneshot, 2=split into 2 buys)
    use_cycling: bool = True  # If True, can re-enter market after completing position

    @property
    def order_size_per_cycle(self) -> int:
        """Calculate order size per buy cycle (target_shares / grid_buycount)."""
        size = self.target_shares // self.grid_buycount
        return max(5, size)  # Minimum 5 shares (Polymarket constraint)

    @property
    def order_size_per_level(self) -> int:
        """Calculate order size per grid level (target_shares / grid_levels)."""
        return self.target_shares // self.grid_levels  # e.g., 30 / 3 = 10 per level

    @property
    def estimated_cost(self) -> float:
        """Estimate max capital needed per trade."""
        return self.target_shares * AVG_PAIR_COST

    @property
    def lookback_ms(self) -> int:
        """Convert lookback ticks to milliseconds (at 60Hz)."""
        return int(self.spike_lookback * 1000 / 60)

    def __hash__(self):
        return hash((self.target_shares, self.grid_levels, self.grid_spacing,
                    self.spike_lookback, self.stop_loss_pct, self.order_pulling,
                    self.ewma_fast_halflife, self.ewma_slow_halflife,
                    self.entry_order_pull_timeout, self.grid_buycount, self.use_cycling))

    def to_dict(self) -> dict:
        return {
            'target_shares': self.target_shares,
            'order_size_per_level': self.order_size_per_level,
            'order_size_per_cycle': self.order_size_per_cycle,
            'grid_levels': self.grid_levels,
            'grid_spacing': self.grid_spacing,
            'grid_buycount': self.grid_buycount,
            'use_cycling': self.use_cycling,
            'spike_lookback': self.spike_lookback,
            'lookback_ms': self.lookback_ms,
            'ewma_fast_halflife': self.ewma_fast_halflife,
            'ewma_slow_halflife': self.ewma_slow_halflife,
            'stop_loss_pct': self.stop_loss_pct,
            'order_pulling': self.order_pulling,
            'entry_order_pull_timeout': self.entry_order_pull_timeout,
            'estimated_cost': self.estimated_cost,
        }


@dataclass
class TradeResult:
    """Result from a single trade."""
    market_slug: str
    cycle_num: int
    entry_time_remaining: float
    signal_score: float
    winner_side: str
    winner_fill_price: float
    loser_fill_price: float
    hedge_type: str  # 'passive', 'stoploss', 'resolution', 'pulled'
    pair_cost: float
    pnl: float
    pnl_gross: float  # PnL before fees
    entry_fee: float  # Taker fee paid on entry
    hedge_fee: float  # Taker fee paid on hedge (if taker exit)
    correct_direction: bool
    spike_magnitude: float
    shares_filled: int
    entry_ts: int
    exit_ts: Optional[int] = None


@dataclass
class OptResult:
    """Result from a single configuration backtest."""
    config: OptConfig
    total_pnl: float
    total_pnl_gross: float  # Before fees
    total_entry_fees: float
    total_hedge_fees: float
    hourly_rate: float
    hourly_rate_gross: float  # Before fees
    total_trades: int
    win_rate: float
    direction_accuracy: float
    passive_hedge_pct: float
    stoploss_hedge_pct: float
    resolution_pct: float
    pulled_pct: float  # Percentage of trades cancelled due to order pulling
    capital_exceeded: bool  # True if any trade would exceed $170
    max_capital_used: float
    avg_pair_cost: float
    # PnL breakdown
    passive_pnl: float = 0.0
    stoploss_pnl: float = 0.0
    resolution_pnl: float = 0.0
    trades: List[TradeResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        result = self.config.to_dict()
        result.update({
            'total_pnl': self.total_pnl,
            'total_pnl_gross': self.total_pnl_gross,
            'total_entry_fees': self.total_entry_fees,
            'total_hedge_fees': self.total_hedge_fees,
            'hourly_rate': self.hourly_rate,
            'hourly_rate_gross': self.hourly_rate_gross,
            'total_trades': self.total_trades,
            'win_rate': self.win_rate,
            'direction_accuracy': self.direction_accuracy,
            'passive_hedge_pct': self.passive_hedge_pct,
            'stoploss_hedge_pct': self.stoploss_hedge_pct,
            'resolution_pct': self.resolution_pct,
            'pulled_pct': self.pulled_pct,
            'capital_exceeded': self.capital_exceeded,
            'max_capital_used': self.max_capital_used,
            'avg_pair_cost': self.avg_pair_cost,
            'passive_pnl': self.passive_pnl,
            'stoploss_pnl': self.stoploss_pnl,
            'resolution_pnl': self.resolution_pnl,
        })
        return result


# =============================================================================
# PARAMETER GRID GENERATION
# =============================================================================

def generate_param_grid(quick: bool = False, path: str = "both") -> List[OptConfig]:
    """Generate all valid parameter combinations.

    Args:
        quick: If True, use reduced parameter space for quick testing
        path: "path1" for entry pulling experiment,
              "both" for full grid including all experiments

    Returns:
        List of OptConfig instances to test
    """
    configs = []

    # EWMA halflife options to test
    # Fast: 30s, 60s, 120s (1-2min window for current vol)
    # Slow: 180s, 300s, 600s (3-10min window for baseline vol)
    ewma_fast_options = [30.0, 60.0, 120.0]
    ewma_slow_options = [180.0, 300.0, 600.0]

    if quick:
        # Quick test mode: reduced parameter space
        target_shares_list = [30, 50]
        grid_levels_list = [1]
        grid_spacings = [0.01]
        lookbacks = [60, 72]  # 1000ms, 1200ms
        stop_losses = [0.12]
        order_pulling_opts = [False]
        entry_pull_timeouts = [10.0]
        path1_lookbacks = [60, 72]
        grid_buycount_path1 = [1, 2]
        use_cycling_opts = [True]
        ewma_fast_options = [60.0]  # Single for quick
        ewma_slow_options = [300.0]  # Single for quick
    else:
        # Full grid - simplified for EWMA testing
        target_shares_list = [30, 50]  # Reduced from 3 to 2
        grid_levels_list = [1]  # Fixed to 1 (simpler)
        grid_spacings = [0.01]
        lookbacks = [60, 72, 84]  # 1000, 1200, 1400ms at 60Hz
        stop_losses = [0.07, 0.12, 0.15]
        order_pulling_opts = [False]
        use_cycling_opts = [True, False]

        # PATH 1 only for EWMA optimizer
        entry_pull_timeouts = [5.0, 10.0, 15.0]  # Reduced from 4 to 3
        path1_lookbacks = [60, 72, 84]

        grid_buycount_path1 = [1, 2, 3]  # Reduced from 4 to 3

    for target_shares in target_shares_list:
        for grid_levels in grid_levels_list:
            # Skip if target_shares doesn't divide evenly by grid_levels
            if target_shares % grid_levels != 0:
                continue
            # Only use multiple spacings when grid_levels > 1
            spacings = [0.01] if grid_levels == 1 else grid_spacings
            for spacing in spacings:
                for lookback in lookbacks:
                    for stop_loss in stop_losses:
                        for pulling in order_pulling_opts:
                            # Determine which experiments to run
                            if path == "path1":
                                # PATH 1: Entry pulling with EWMA threshold testing
                                if lookback not in path1_lookbacks:
                                    continue
                                for entry_timeout in entry_pull_timeouts:
                                    for buycount in grid_buycount_path1:
                                        for cycling in use_cycling_opts:
                                            for ewma_fast in ewma_fast_options:
                                                for ewma_slow in ewma_slow_options:
                                                    # Skip invalid: slow must be > fast
                                                    if ewma_slow <= ewma_fast:
                                                        continue
                                                    # Skip invalid: order size < 5 shares
                                                    if target_shares // buycount < 5:
                                                        continue
                                                    configs.append(OptConfig(
                                                        target_shares=target_shares,
                                                        grid_levels=grid_levels,
                                                        grid_spacing=spacing,
                                                        spike_lookback=lookback,
                                                        stop_loss_pct=stop_loss,
                                                        order_pulling=pulling,
                                                        ewma_fast_halflife=ewma_fast,
                                                        ewma_slow_halflife=ewma_slow,
                                                        entry_order_pull_timeout=entry_timeout,
                                                        grid_buycount=buycount,
                                                        use_cycling=cycling,
                                                    ))
                            else:
                                # Default: standard grid (backward compatible)
                                configs.append(OptConfig(
                                    target_shares=target_shares,
                                    grid_levels=grid_levels,
                                    grid_spacing=spacing,
                                    spike_lookback=lookback,
                                    stop_loss_pct=stop_loss,
                                    order_pulling=pulling,
                                    entry_order_pull_timeout=10.0,  # Default
                                ))
    return configs


# =============================================================================
# SPIKE DETECTION
# =============================================================================

def calculate_rolling_atr(prices: pd.Series, period: int = ATR_PERIOD) -> pd.Series:
    """Calculate rolling ATR (using price changes as proxy for true range)."""
    tr = prices.diff().abs()
    atr = tr.rolling(window=period).mean()
    return atr


def classify_regime_vectorized(atr_series: pd.Series, window: int = ATR_WINDOW) -> pd.Series:
    """Classify volatility regime for each point based on ATR percentile."""
    percentile = atr_series.rolling(window=window, min_periods=window//2).apply(
        lambda x: (pd.Series(x).rank().iloc[-1] / len(x)) * 100, raw=False
    )

    regime = pd.Series('MEDIUM', index=atr_series.index)
    regime[percentile < LOW_PERCENTILE] = 'LOW'
    regime[percentile > HIGH_PERCENTILE] = 'HIGH'

    return regime


def detect_spikes_for_lookback(btc_df: pd.DataFrame, lookback: int,
                               threshold_method: str = "ewma",
                               ewma_fast_halflife: float = 60.0,
                               ewma_slow_halflife: float = 300.0) -> pd.DataFrame:
    """
    Detect spikes using specified lookback period.

    Args:
        btc_df: DataFrame with timestamp_ms and price columns
        lookback: Number of ticks to look back for spike detection
        threshold_method: One of "fixed", "regime", or "ewma"
            - "fixed": Use fixed SPIKE_THRESHOLD (0.02%)
            - "regime": Use ATR-based regime thresholds (LOW/MEDIUM/HIGH)
            - "ewma": Use pure EWMA adaptive thresholds (fast/slow ratio)
        ewma_fast_halflife: Fast EWMA halflife in seconds (for "ewma" method)
        ewma_slow_halflife: Slow EWMA halflife in seconds (for "ewma" method)

    Returns:
        DataFrame with spike detection results
    """
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    # Calculate % change over lookback period
    df['price_prev'] = df['price'].shift(lookback)
    df['change_pct'] = (df['price'] - df['price_prev']) / df['price_prev'] * 100
    df['magnitude'] = df['change_pct'].abs()

    if threshold_method == "ewma":
        # Pure EWMA adaptive threshold using fast/slow ratio
        tracker = EWMAThresholdTracker(
            fast_halflife_sec=ewma_fast_halflife,
            slow_halflife_sec=ewma_slow_halflife
        )
        thresholds = []
        for price in df['price'].values:
            threshold = tracker.update(price)
            thresholds.append(threshold)

        df['threshold'] = thresholds
        df['regime'] = df['threshold'].apply(lambda t:
            'LOW' if t < 0.015 else ('HIGH' if t > 0.025 else 'MEDIUM'))
        df['spike_detected'] = df['magnitude'] >= df['threshold']

    elif threshold_method == "regime":
        # ATR-based regime thresholds (original adaptive_volatility=True behavior)
        df['atr'] = calculate_rolling_atr(df['price'])
        df['regime'] = classify_regime_vectorized(df['atr'])
        df['threshold'] = df['regime'].map(REGIME_THRESHOLDS)
        df['threshold'] = df['threshold'].fillna(SPIKE_THRESHOLD)
        df['spike_detected'] = df['magnitude'] >= df['threshold']

    else:  # "fixed"
        df['spike_detected'] = df['magnitude'] >= SPIKE_THRESHOLD
        df['regime'] = 'MEDIUM'
        df['threshold'] = SPIKE_THRESHOLD

    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['change_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['change_pct'] < 0), 'spike_direction'] = 'DOWN'
    df['spike_magnitude'] = df['magnitude'].where(df['spike_detected'], 0)

    return df[['timestamp_ms', 'price', 'spike_detected', 'spike_direction',
               'spike_magnitude', 'regime', 'threshold']]


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def velocity_confirms(spike_dir: str, velocity_bps: float) -> bool:
    """Check if velocity confirms spike direction."""
    if spike_dir == "UP":
        return velocity_bps > -VELOCITY_CONFIRM_THRESHOLD
    elif spike_dir == "DOWN":
        return velocity_bps < VELOCITY_CONFIRM_THRESHOLD
    return True


def compute_score(spike_mag: float, velocity_bps: float, spike_dir: str,
                  time_rem: float, regime: str = "MEDIUM") -> float:
    """
    Compute enhanced signal score v2 (interaction-based).

    Based on statistical analysis:
    - Spike × Velocity interaction is significant (p=0.001)
    - Time window 300-600s is optimal (88.9% accuracy)
    - HIGH regime gets bonus, LOW already filtered
    """
    # Regime weight
    if regime == "LOW":
        return 0.0  # Should be filtered upstream
    regime_weight = 1.2 if regime == "HIGH" else 1.0

    # Time window weight
    if 300 <= time_rem <= 600:
        time_weight = 1.0  # Optimal
    elif 180 <= time_rem <= 750:
        time_weight = 0.6
    else:
        time_weight = 0.3

    # Core: interaction effect
    interaction = spike_mag * abs(velocity_bps)

    return interaction * time_weight * regime_weight


def compute_score_v1_legacy(spike_mag: float, velocity_bps: float, spike_dir: str,
                            time_rem: float) -> float:
    """Legacy scoring formula - kept for comparison."""
    spike_score = min(spike_mag / 0.05, 1.0)
    velocity_score = min(abs(velocity_bps) / 0.50, 1.0)

    vel_confirms = (spike_dir == "UP" and velocity_bps > 0) or \
                   (spike_dir == "DOWN" and velocity_bps < 0)
    confirm_bonus = 1.0 if vel_confirms else 0.0

    urgency = 1.0 - min(time_rem / 900.0, 1.0)

    return 0.40 * spike_score + 0.30 * velocity_score + 0.20 * confirm_bonus + 0.10 * urgency


def calc_loser_bid(winner_entry: float, spike_mag: float, regime: str = "MEDIUM") -> float:
    """
    Calculate loser side bid price (v2).

    Based on hedge_pricing_analysis.py regression results:
    - Actual 60s mean drop: 0.10 (not 0.03 as old formula predicted)
    - spike_magnitude correlation with drop: -0.01 (essentially zero)
    - Regime has modest effect on drop magnitude

    Formula: expected_drop = 0.08 + 0.50 * spike_mag + regime_bonus
    FIX: Do NOT divide by 100 - spike_mag is already percentage (0.05 = 0.05%)
    """
    # Base drop (calibrated to actual 60s mean of 0.10)
    base_drop = DROP_INTERCEPT  # 0.08

    # Spike term (small weight - spike has weak predictive power)
    # FIX: spike_mag is already percentage, no /100 needed
    spike_term = DROP_MULTIPLIER * spike_mag  # 0.50 * spike%

    # Regime adjustment
    regime_bonus = DROP_REGIME_BONUS.get(regime, 0.01)

    expected_drop = base_drop + spike_term + regime_bonus

    # Clamp to reasonable range [0.02, 0.20]
    expected_drop = max(0.02, min(0.20, expected_drop))

    max_loser = TARGET_PAIR_COST - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid))


def check_capital_constraint(config: OptConfig, capital: float = CAPITAL_LIMIT) -> bool:
    """Check if trade would exceed capital constraint."""
    return config.estimated_cost > capital


# =============================================================================
# GRID LEVELS SIMULATION - TAKER ENTRY
# =============================================================================

def simulate_entry_with_grid_taker(obs_row: pd.Series, config: OptConfig,
                                    winner_side: str, cycle_shares: int = None,
                                    slippage: float = 0.0) -> List[Dict]:
    """
    Simulate TAKER entry across multiple grid levels.

    TAKER VERSION: Entry fills immediately at the ask price (+ optional slippage).
    No passive order fill simulation needed.

    Returns list of orders, each with price, size, and level.

    Entry price logic (TAKER - matches old cycling_backtest.py):
    - Base: winner_ask + slippage (immediately cross spread, pay taker fee)
    - Grid levels spread downward from base (lower levels get slightly better prices)

    Args:
        obs_row: Observer data row
        config: OptConfig
        winner_side: 'UP' or 'DOWN'
        cycle_shares: Shares for this cycle (uses order_size_per_cycle if None)
        slippage: Additional amount above ask for guaranteed fill (default 0)
    """
    if winner_side == 'UP':
        winner_ask = obs_row['up_ask']
    else:
        winner_ask = obs_row['down_ask']

    # TAKER entry: fill at ask price + slippage (crossing spread)
    base_price = winner_ask + slippage
    base_price = round(base_price, 2)
    base_price = max(0.01, min(0.99, base_price))

    # Determine shares for this cycle
    if cycle_shares is None:
        cycle_shares = config.order_size_per_cycle

    # Split across grid levels
    shares_per_level = max(5, cycle_shares // config.grid_levels)

    orders = []
    for level in range(config.grid_levels):
        # For taker entry, all levels fill at the same ask price
        # (grid spacing doesn't apply to market orders)
        price = base_price
        orders.append({
            'price': price,
            'size': shares_per_level,
            'level': level,
            'placed_at': obs_row['timestamp_ms'],
            'filled': True,  # TAKER: immediate fill
            'fill_price': price,
        })

    return orders


# =============================================================================
# MARKET SIMULATION - TAKER ENTRY VERSION
# =============================================================================

def simulate_market(spikes_df: pd.DataFrame, obs_df: pd.DataFrame,
                    slug: str, resolution: str, config: OptConfig,
                    slippage: float = 0.0) -> List[TradeResult]:
    """Simulate trading on a single market using specified configuration.

    TAKER VERSION: Entry fills immediately at ask + slippage, fees deducted from PnL.
    """
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    # Get spikes in this market's time range
    market_spikes = spikes_df[(spikes_df['timestamp_ms'] >= market_start) &
                               (spikes_df['timestamp_ms'] <= market_end)].copy()

    trades = []
    cycle_num = 0
    last_trade_ts = 0
    cycles_completed = 0  # Successful buy cycles (limited by grid_buycount)
    total_shares_accumulated = 0  # Total shares bought across all cycles

    # Iterate through spike events
    for _, spike_row in market_spikes.iterrows():
        # Check if we've hit the cycle limit for this position
        if cycles_completed >= config.grid_buycount:
            if config.use_cycling:
                # Cycling ON: Reset counters, allow new position in same market
                cycles_completed = 0
                total_shares_accumulated = 0
            else:
                # Cycling OFF: Done with this market
                break

        spike_ts = spike_row['timestamp_ms']
        spike_dir = spike_row['spike_direction']
        spike_mag = spike_row['spike_magnitude']

        # Check cycle gap
        if (spike_ts - last_trade_ts) < MIN_CYCLE_GAP_MS:
            continue

        # Find nearest observer row
        obs_idx = mdf['timestamp_ms'].searchsorted(spike_ts)
        if obs_idx >= len(mdf):
            obs_idx = len(mdf) - 1

        obs_row = mdf.iloc[obs_idx]
        time_rem = obs_row['time_remaining_secs']

        if time_rem < MIN_TIME:
            continue

        velocity_bps = obs_row.get('velocity_bps', 0) or 0
        regime = spike_row.get('regime', 'MEDIUM')

        # Apply enhanced signal filter
        if not velocity_confirms(spike_dir, velocity_bps):
            continue
        score = compute_score(spike_mag, velocity_bps, spike_dir, time_rem, regime)
        if score < ENHANCED_SCORE_THRESHOLD:
            continue

        # Entry signal detected
        cycle_num += 1
        winner_side = spike_dir
        loser_side = "DOWN" if winner_side == "UP" else "UP"

        # Calculate shares for this cycle (may be less if approaching target)
        remaining_shares = config.target_shares - total_shares_accumulated
        cycle_shares = min(config.order_size_per_cycle, remaining_shares)
        if cycle_shares < 5:  # Polymarket minimum
            break  # Can't place valid order

        # Create grid orders for winner side entry - TAKER (immediate fill)
        winner_orders = simulate_entry_with_grid_taker(obs_row, config, winner_side, cycle_shares, slippage)

        # TAKER: All orders fill immediately
        total_winner_filled = sum(o['size'] for o in winner_orders)
        total_winner_cost = sum(o['fill_price'] * o['size'] for o in winner_orders)

        # Calculate average winner entry price
        avg_winner_entry = total_winner_cost / total_winner_filled if total_winner_filled > 0 else 0

        # Calculate entry fee (taker)
        entry_fee = calculate_taker_fee(avg_winner_entry, cycle_shares)

        # Calculate loser bid target (v2: includes regime adjustment)
        loser_target = calc_loser_bid(avg_winner_entry, spike_mag, regime)

        # Scan forward for hedge
        hedge_type = "resolution"
        loser_fill = 0.0
        entry_fill_ts = spike_ts  # For taker, entry fills at signal time
        hedge_fill_ts = market_end  # Default to resolution time

        for j in range(obs_idx + 1, len(mdf)):
            scan_row = mdf.iloc[j]
            scan_ts = scan_row['timestamp_ms']

            if loser_side == "UP":
                curr_loser_ask = scan_row['up_ask']
                winner_bid = scan_row['down_bid']
            else:
                curr_loser_ask = scan_row['down_ask']
                winner_bid = scan_row['up_bid']

            # Passive fill (maker hedge)
            if curr_loser_ask <= loser_target:
                loser_fill = loser_target
                hedge_type = "passive"
                hedge_fill_ts = scan_ts
                break

            # Stop-loss (taker exit)
            if config.stop_loss_pct is not None:
                drop = (avg_winner_entry - winner_bid) / avg_winner_entry
                if drop >= config.stop_loss_pct:
                    loser_fill = curr_loser_ask
                    hedge_type = "stoploss"
                    hedge_fill_ts = scan_ts
                    break

        # Resolution handling - if direction correct, loser MUST fill (goes to $0)
        if hedge_type == "resolution":
            if resolution == winner_side:
                hedge_type = "passive"
                loser_fill = loser_target
            else:
                loser_fill = 1.0

        # Calculate hedge fee (taker fee only for stoploss exits)
        if hedge_type == "stoploss":
            hedge_fee = calculate_taker_fee(loser_fill, cycle_shares)
        else:
            hedge_fee = 0.0  # Passive hedge is maker (no fee)

        # Calculate PnL (full hedge)
        pair_cost = avg_winner_entry + loser_fill
        if hedge_type == "resolution":
            pnl_gross = -avg_winner_entry * cycle_shares
        else:
            pnl_gross = (1.0 - pair_cost) * cycle_shares

        # Net PnL = gross - fees
        pnl = pnl_gross - entry_fee - hedge_fee

        trades.append(TradeResult(
            market_slug=slug, cycle_num=cycle_num, entry_time_remaining=time_rem,
            signal_score=score, winner_side=winner_side,
            winner_fill_price=avg_winner_entry, loser_fill_price=loser_fill,
            hedge_type=hedge_type, pair_cost=pair_cost,
            pnl=pnl, pnl_gross=pnl_gross,
            entry_fee=entry_fee, hedge_fee=hedge_fee,
            correct_direction=(resolution == winner_side),
            spike_magnitude=spike_mag, shares_filled=cycle_shares,
            entry_ts=spike_ts
        ))

        # Track cycle completion for grid_buycount limit
        cycles_completed += 1
        total_shares_accumulated += cycle_shares
        last_trade_ts = hedge_fill_ts  # FIXED: Use hedge fill time, not entry time

    return trades


# =============================================================================
# BACKTEST RUNNER
# =============================================================================

def run_single_config(config: OptConfig, spikes_by_lookback: Dict[int, pd.DataFrame],
                      obs_df: pd.DataFrame, hours: float,
                      market_resolutions: Dict[str, str],
                      slippage: float = 0.0) -> OptResult:
    """Run backtest for a single configuration."""
    # Use pre-computed spikes for this lookback
    spikes_only = spikes_by_lookback[config.spike_lookback]

    # Run simulation across all markets
    all_trades = []
    max_capital = 0.0

    for slug in obs_df['market_slug'].unique():
        mdf = obs_df[obs_df['market_slug'] == slug]
        resolution = market_resolutions.get(slug, 'UP')

        trades = simulate_market(spikes_only, obs_df, slug, resolution, config, slippage)
        all_trades.extend(trades)

        # Track max capital (simplified: single trade capital)
        if trades:
            max_capital = max(max_capital, config.estimated_cost)

    # Calculate results
    if not all_trades:
        return OptResult(
            config=config, total_pnl=0, total_pnl_gross=0,
            total_entry_fees=0, total_hedge_fees=0,
            hourly_rate=0, hourly_rate_gross=0, total_trades=0,
            win_rate=0, direction_accuracy=0, passive_hedge_pct=0,
            stoploss_hedge_pct=0, resolution_pct=0, pulled_pct=0,
            capital_exceeded=check_capital_constraint(config),
            max_capital_used=0, avg_pair_cost=0
        )

    # Filter out pulled trades for main stats (though taker should have none)
    executed_trades = [t for t in all_trades if t.hedge_type != "pulled"]
    pulled_trades = [t for t in all_trades if t.hedge_type == "pulled"]

    total_trades = len(all_trades)
    executed_count = len(executed_trades)

    if executed_count == 0:
        return OptResult(
            config=config, total_pnl=0, total_pnl_gross=0,
            total_entry_fees=0, total_hedge_fees=0,
            hourly_rate=0, hourly_rate_gross=0, total_trades=total_trades,
            win_rate=0, direction_accuracy=0, passive_hedge_pct=0,
            stoploss_hedge_pct=0, resolution_pct=0,
            pulled_pct=len(pulled_trades) / total_trades if total_trades > 0 else 0,
            capital_exceeded=check_capital_constraint(config),
            max_capital_used=max_capital, avg_pair_cost=0
        )

    total_pnl = sum(t.pnl for t in executed_trades)
    total_pnl_gross = sum(t.pnl_gross for t in executed_trades)
    total_entry_fees = sum(t.entry_fee for t in executed_trades)
    total_hedge_fees = sum(t.hedge_fee for t in executed_trades)
    hourly_rate = total_pnl / hours if hours > 0 else 0
    hourly_rate_gross = total_pnl_gross / hours if hours > 0 else 0

    wins = sum(1 for t in executed_trades if t.pnl > 0)
    hedged = [t for t in executed_trades if t.hedge_type != "resolution"]
    avg_pair = np.mean([t.pair_cost for t in hedged]) if hedged else 0

    passive = sum(1 for t in executed_trades if t.hedge_type == "passive")
    stoploss = sum(1 for t in executed_trades if t.hedge_type == "stoploss")
    resolution = sum(1 for t in executed_trades if t.hedge_type == "resolution")
    correct = sum(1 for t in executed_trades if t.correct_direction)

    passive_pnl = sum(t.pnl for t in executed_trades if t.hedge_type == "passive")
    stoploss_pnl = sum(t.pnl for t in executed_trades if t.hedge_type == "stoploss")
    resolution_pnl = sum(t.pnl for t in executed_trades if t.hedge_type == "resolution")

    return OptResult(
        config=config,
        total_pnl=total_pnl,
        total_pnl_gross=total_pnl_gross,
        total_entry_fees=total_entry_fees,
        total_hedge_fees=total_hedge_fees,
        hourly_rate=hourly_rate,
        hourly_rate_gross=hourly_rate_gross,
        total_trades=total_trades,
        win_rate=wins / executed_count,
        direction_accuracy=correct / executed_count,
        passive_hedge_pct=passive / executed_count,
        stoploss_hedge_pct=stoploss / executed_count,
        resolution_pct=resolution / executed_count,
        pulled_pct=len(pulled_trades) / total_trades if total_trades > 0 else 0,
        capital_exceeded=check_capital_constraint(config),
        max_capital_used=max_capital,
        avg_pair_cost=avg_pair,
        passive_pnl=passive_pnl,
        stoploss_pnl=stoploss_pnl,
        resolution_pnl=resolution_pnl,
        trades=all_trades
    )


# Global variables for multiprocessing (set before fork)
_GLOBAL_SPIKES_BY_LOOKBACK = None
_GLOBAL_OBS_DF = None
_GLOBAL_HOURS = None
_GLOBAL_RESOLUTIONS = None
_GLOBAL_SLIPPAGE = 0.0


def _run_config_worker(config: OptConfig) -> OptResult:
    """Worker function that uses global data."""
    return run_single_config(config, _GLOBAL_SPIKES_BY_LOOKBACK, _GLOBAL_OBS_DF,
                            _GLOBAL_HOURS, _GLOBAL_RESOLUTIONS, _GLOBAL_SLIPPAGE)


def precompute_spikes(btc_df: pd.DataFrame, lookbacks: List[int],
                      ewma_fast_halflife: float = 60.0,
                      ewma_slow_halflife: float = 300.0) -> Dict[int, pd.DataFrame]:
    """
    Pre-compute spikes for all lookback values using EWMA threshold.

    Args:
        btc_df: Binance price DataFrame
        lookbacks: List of lookback tick values
        ewma_fast_halflife: Fast EWMA halflife in seconds
        ewma_slow_halflife: Slow EWMA halflife in seconds

    Returns:
        Dict mapping lookback -> spikes DataFrame
    """
    print(f"\nPre-computing spikes (EWMA fast={ewma_fast_halflife}s, slow={ewma_slow_halflife}s)...")
    spikes_by_lookback = {}

    for lookback in lookbacks:
        ms = lookback * 1000 // 60
        print(f"  Lookback {lookback} ticks ({ms}ms)...", end=' ', flush=True)
        spikes_df = detect_spikes_for_lookback(
            btc_df, lookback,
            threshold_method="ewma",
            ewma_fast_halflife=ewma_fast_halflife,
            ewma_slow_halflife=ewma_slow_halflife
        )
        # Filter out LOW regime spikes (worse than coin flip)
        spikes_only = spikes_df[
            (spikes_df['spike_detected'] == True) &
            (spikes_df['regime'] != 'LOW')
        ].copy()
        spikes_by_lookback[lookback] = spikes_only

        if len(spikes_only) > 0:
            mean_thresh = spikes_only['threshold'].mean()
            print(f"{len(spikes_only):,} spikes (mean threshold={mean_thresh:.4f}%)")
        else:
            print(f"0 spikes")

    return spikes_by_lookback


def run_optimization(btc_df: pd.DataFrame, obs_df: pd.DataFrame, hours: float,
                     market_resolutions: Dict[str, str], n_workers: int = 4,
                     quick: bool = False, path: str = "both",
                     slippage: float = 0.0) -> List[OptResult]:
    """Run all configurations in parallel, grouped by EWMA parameters."""
    configs = generate_param_grid(quick=quick, path=path)
    print(f"\nTotal configurations to test: {len(configs)}")
    print(f"Entry slippage: +${slippage:.2f} above ask")
    print(f"Threshold method: EWMA (pure)")

    # Group configs by (ewma_fast, ewma_slow) since spike detection depends on these
    from collections import defaultdict
    configs_by_ewma = defaultdict(list)
    for c in configs:
        key = (c.ewma_fast_halflife, c.ewma_slow_halflife)
        configs_by_ewma[key].append(c)

    ewma_combos = list(configs_by_ewma.keys())
    print(f"Unique EWMA combos to test: {len(ewma_combos)}")
    for fast, slow in sorted(ewma_combos):
        print(f"  fast={fast}s, slow={slow}s: {len(configs_by_ewma[(fast, slow)])} configs")

    # Get unique lookback values
    lookbacks = list(set(c.spike_lookback for c in configs))
    print(f"Unique lookback values: {lookbacks}")

    results = []
    start_time = time.time()
    total_completed = 0

    # Process each EWMA combo separately (spikes depend on EWMA params)
    for combo_idx, (ewma_fast, ewma_slow) in enumerate(sorted(ewma_combos)):
        combo_configs = configs_by_ewma[(ewma_fast, ewma_slow)]
        print(f"\n--- EWMA combo {combo_idx+1}/{len(ewma_combos)}: fast={ewma_fast}s, slow={ewma_slow}s ---")

        # Pre-compute spikes for this EWMA combo
        spikes_by_lookback = precompute_spikes(
            btc_df, lookbacks,
            ewma_fast_halflife=ewma_fast,
            ewma_slow_halflife=ewma_slow
        )

        if n_workers == 1:
            # Sequential execution for debugging
            for i, config in enumerate(combo_configs):
                result = run_single_config(config, spikes_by_lookback, obs_df, hours, market_resolutions, slippage)
                results.append(result)
                total_completed += 1
        else:
            # Parallel execution using fork for data sharing
            ctx = mp.get_context('fork')

            global _GLOBAL_SPIKES_BY_LOOKBACK, _GLOBAL_OBS_DF, _GLOBAL_HOURS, _GLOBAL_RESOLUTIONS, _GLOBAL_SLIPPAGE
            _GLOBAL_SPIKES_BY_LOOKBACK = spikes_by_lookback
            _GLOBAL_OBS_DF = obs_df
            _GLOBAL_HOURS = hours
            _GLOBAL_RESOLUTIONS = market_resolutions
            _GLOBAL_SLIPPAGE = slippage

            with ctx.Pool(processes=n_workers) as pool:
                combo_completed = 0
                for result in pool.imap_unordered(_run_config_worker, combo_configs, chunksize=1):
                    results.append(result)
                    combo_completed += 1
                    total_completed += 1

                    if combo_completed % 20 == 0 or combo_completed == len(combo_configs):
                        elapsed = time.time() - start_time
                        rate = total_completed / elapsed if elapsed > 0 else 1
                        remaining = (len(configs) - total_completed) / rate if rate > 0 else 0
                        pct = total_completed / len(configs) * 100
                        bar_len = 30
                        filled = int(bar_len * total_completed / len(configs))
                        bar = '#' * filled + '-' * (bar_len - filled)
                        print(f"\rProgress: [{bar}] {pct:.0f}% ({total_completed}/{len(configs)}) "
                              f"- ETA: {remaining/60:.1f}m", end='', flush=True)

        print()  # Newline after this combo

    elapsed = time.time() - start_time
    print(f"\nCompleted in {elapsed/60:.1f} minutes ({elapsed/len(configs):.2f}s per config)")

    return results


# =============================================================================
# DATA LOADING
# =============================================================================

def load_data(btc_file=None, obs_file=None, res_file=None, start_ts=None, end_ts=None):
    """Load Binance and observer data with optional time filtering."""
    print("Loading data...")

    # Load Binance - load ALL files if no specific file given
    if btc_file:
        btc_path = Path(btc_file)
        btc_df = pd.read_csv(btc_path)
        print(f"  Binance: {len(btc_df):,} rows ({btc_path.name})")
    else:
        btc_dir = Path("research/binance_hf")
        if not btc_dir.exists():
            btc_dir = Path("/Users/rananjaybika/polymarket-amm-bot/research/binance_hf")

        btc_dfs = []
        for f in sorted(btc_dir.glob("btc_prices_*.csv")):
            df = pd.read_csv(f)
            btc_dfs.append(df)
            print(f"  Binance: {len(df):,} rows ({f.name})")
        btc_df = pd.concat(btc_dfs, ignore_index=True)
        btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
        print(f"  Binance TOTAL: {len(btc_df):,} rows")

    # Load observer
    if obs_file:
        obs_df = pd.read_csv(obs_file, on_bad_lines='skip', low_memory=False)
        obs_dir = Path(obs_file).parent
        print(f"  Observer: {len(obs_df):,} rows ({Path(obs_file).name})")
    else:
        obs_dir = Path("research/observer")
        if not obs_dir.exists():
            obs_dir = Path("/Users/rananjaybika/polymarket-amm-bot/research/observer")

        obs_dfs = []
        for f in sorted(obs_dir.glob("grid_obs_*.csv")):
            df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)
        obs_df = pd.concat(obs_dfs, ignore_index=True)
        obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
        print(f"  Observer: {len(obs_df):,} rows")

    # Load resolutions
    if res_file:
        res_path = Path(res_file)
    else:
        res_path = obs_dir / "market_resolutions_verified.csv"
        if not res_path.exists():
            res_path = Path("/Users/rananjaybika/polymarket-amm-bot/research/observer/market_resolutions_verified.csv")
    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Apply time filtering if specified
    if start_ts is not None or end_ts is not None:
        btc_before = len(btc_df)
        obs_before = len(obs_df)
        if start_ts is not None:
            btc_df = btc_df[btc_df['timestamp_ms'] >= start_ts]
            obs_df = obs_df[obs_df['timestamp_ms'] >= start_ts]
        if end_ts is not None:
            btc_df = btc_df[btc_df['timestamp_ms'] <= end_ts]
            obs_df = obs_df[obs_df['timestamp_ms'] <= end_ts]
        from datetime import datetime
        start_str = datetime.utcfromtimestamp(start_ts/1000).strftime('%Y-%m-%d %H:%M') if start_ts else "start"
        end_str = datetime.utcfromtimestamp(end_ts/1000).strftime('%Y-%m-%d %H:%M') if end_ts else "end"
        print(f"  TIME FILTER: {start_str} to {end_str}")
        print(f"    Binance: {btc_before:,} -> {len(btc_df):,} rows")
        print(f"    Observer: {obs_before:,} -> {len(obs_df):,} rows")

    # Find actual binance time ranges (may have gaps)
    btc_df = btc_df.sort_values('timestamp_ms')

    # Detect gaps > 5 minutes in binance data
    btc_df['time_diff'] = btc_df['timestamp_ms'].diff()
    gap_threshold = 5 * 60 * 1000  # 5 minutes
    gaps = btc_df[btc_df['time_diff'] > gap_threshold]

    if len(gaps) > 0:
        print(f"  WARNING: {len(gaps)} gaps detected in binance data")
        for _, gap in gaps.iterrows():
            gap_hours = gap['time_diff'] / 3600000
            print(f"    Gap of {gap_hours:.2f} hours at {gap['timestamp_ms']}")

    # Build list of valid time ranges (between gaps)
    valid_ranges = []
    range_start = btc_df['timestamp_ms'].iloc[0]
    for _, gap in gaps.iterrows():
        range_end = gap['timestamp_ms'] - gap['time_diff']
        valid_ranges.append((range_start, range_end))
        range_start = gap['timestamp_ms']
    valid_ranges.append((range_start, btc_df['timestamp_ms'].iloc[-1]))

    total_btc_hours = sum((e - s) / 3600000 for s, e in valid_ranges)
    print(f"  Binance coverage: {total_btc_hours:.2f} hours across {len(valid_ranges)} sessions")

    # Add resolutions
    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    # Filter markets that have actual binance coverage
    valid_slugs = []
    for slug, mdf in obs_df.groupby('market_slug'):
        max_time = mdf['time_remaining_secs'].max()
        min_time = mdf['time_remaining_secs'].min()
        duration = max_time - min_time

        if duration < MIN_RUNTIME_SECS or max_time < 840:
            continue

        # Check if market has binance data coverage
        m_start = mdf['timestamp_ms'].min()
        m_end = mdf['timestamp_ms'].max()

        has_coverage = False
        for r_start, r_end in valid_ranges:
            # Market must be mostly within a valid range
            overlap_start = max(m_start, r_start)
            overlap_end = min(m_end, r_end)
            if overlap_end > overlap_start:
                coverage = (overlap_end - overlap_start) / (m_end - m_start)
                if coverage > 0.8:  # At least 80% coverage
                    has_coverage = True
                    break

        if has_coverage:
            valid_slugs.append(slug)

    obs_df = obs_df[obs_df['market_slug'].isin(valid_slugs)]

    # Filter btc_df to valid ranges only
    btc_mask = pd.Series(False, index=btc_df.index)
    for r_start, r_end in valid_ranges:
        btc_mask |= (btc_df['timestamp_ms'] >= r_start) & (btc_df['timestamp_ms'] <= r_end)
    btc_df = btc_df[btc_mask].drop(columns=['time_diff'])

    # Recalculate hours based on valid market coverage
    if len(valid_slugs) > 0:
        hours = total_btc_hours
    else:
        hours = 0

    print(f"  Valid markets (with binance coverage): {len(valid_slugs)}")

    return btc_df, obs_df, hours, res_map


# =============================================================================
# OUTPUT FORMATTING
# =============================================================================

def print_results(results: List[OptResult], hours: float, n_markets: int):
    """Print comprehensive results summary."""
    print()
    print("=" * 110)
    print("ENHANCED SPIKE PARAMETER OPTIMIZATION RESULTS - TAKER ENTRY (with fees)")
    print("=" * 110)
    print(f"\nData: {hours:.2f} hours, {n_markets} markets")
    print(f"Configurations tested: {len(results)}")

    # Sort by hourly rate
    results.sort(key=lambda x: x.hourly_rate, reverse=True)

    # Top 10 configurations
    print()
    print("=" * 110)
    print("TOP 10 CONFIGURATIONS (by $/hr after fees)")
    print("=" * 110)
    print()
    print(f"{'Rank':<5} {'Total':<6} {'Lvls':<5} {'Spc':<6} {'Look':<8} {'SL':<6} "
          f"{'Pull':<5} {'$/hr':>8} {'Gross':>8} {'Fees':>7} {'Trades':>7} {'Acc%':>6}")
    print("-" * 110)

    for i, r in enumerate(results[:10], 1):
        sl_str = f"{r.config.stop_loss_pct*100:.0f}%" if r.config.stop_loss_pct else "None"
        pull_str = "ON" if r.config.order_pulling else "OFF"
        look_str = f"{r.config.lookback_ms}ms"
        total_fees = r.total_entry_fees + r.total_hedge_fees

        print(f"{i:<5} {r.config.target_shares:<6} {r.config.grid_levels:<5} "
              f"{r.config.grid_spacing:<6.2f} {look_str:<8} {sl_str:<6} {pull_str:<5} "
              f"${r.hourly_rate:>7.2f} ${r.hourly_rate_gross:>6.2f} ${total_fees:>6.2f} "
              f"{r.total_trades:>7} {r.direction_accuracy*100:>5.1f}%")

    # Fee analysis
    print()
    print("=" * 110)
    print("FEE ANALYSIS")
    print("=" * 110)

    if results:
        best = results[0]
        total_fees = best.total_entry_fees + best.total_hedge_fees
        fee_impact = (best.total_pnl_gross - best.total_pnl) / best.total_pnl_gross * 100 if best.total_pnl_gross != 0 else 0

        print(f"\nBest Config Fee Breakdown:")
        print(f"  Entry Fees (taker):  ${best.total_entry_fees:.2f}")
        print(f"  Hedge Fees (taker):  ${best.total_hedge_fees:.2f}")
        print(f"  Total Fees:          ${total_fees:.2f}")
        print(f"  Gross PnL:           ${best.total_pnl_gross:.2f}")
        print(f"  Net PnL:             ${best.total_pnl:.2f}")
        print(f"  Fee Impact:          {fee_impact:.1f}% of gross")

    # Capital constraint analysis
    print()
    print("=" * 110)
    print("CAPITAL CONSTRAINT ANALYSIS ($170 limit)")
    print("=" * 110)

    exceeding = [r for r in results if r.capital_exceeded]
    print(f"\nConfigs exceeding $170 per trade: {len(exceeding)}/{len(results)}")

    if exceeding:
        # Group by config type
        exceed_configs = set((r.config.target_shares, r.config.grid_levels) for r in exceeding)
        for target_shares, grid_levels in sorted(exceed_configs):
            cost = target_shares * AVG_PAIR_COST
            per_level = target_shares // grid_levels
            print(f"  - target={target_shares} (split {per_level}x{grid_levels} levels): ~${cost:.2f}/trade")

    # Parameter sensitivity analysis
    print()
    print("=" * 110)
    print("PARAMETER SENSITIVITY ANALYSIS")
    print("=" * 110)

    # Target shares (total per trade)
    print("\nTarget Shares (total per trade):")
    for size in [5, 10, 15, 30]:
        matching = [r for r in results if r.config.target_shares == size]
        if matching:
            avg_rate = np.mean([r.hourly_rate for r in matching])
            avg_gross = np.mean([r.hourly_rate_gross for r in matching])
            print(f"  {size:>2} shares: ${avg_rate:.2f}/hr net (${avg_gross:.2f} gross)")

    # Grid levels
    print("\nGrid Levels:")
    for levels in [1, 2, 3]:
        matching = [r for r in results if r.config.grid_levels == levels]
        if matching:
            avg_rate = np.mean([r.hourly_rate for r in matching])
            print(f"  {levels} level(s): ${avg_rate:.2f}/hr (avg)")

    # Spike lookback
    print("\nSpike Lookback:")
    for lookback in [18, 30, 36, 60]:
        matching = [r for r in results if r.config.spike_lookback == lookback]
        if matching:
            avg_rate = np.mean([r.hourly_rate for r in matching])
            ms = lookback * 1000 // 60
            print(f"  {ms}ms ({lookback} ticks): ${avg_rate:.2f}/hr (avg)")

    # Stop loss
    print("\nStop Loss:")
    for sl in [0.07, 0.12, None]:
        matching = [r for r in results if r.config.stop_loss_pct == sl]
        if matching:
            avg_rate = np.mean([r.hourly_rate for r in matching])
            sl_str = f"{sl*100:.0f}%" if sl else "None"
            print(f"  {sl_str:>5}: ${avg_rate:.2f}/hr (avg)")

    # Order pulling (should not affect taker much, but included for comparison)
    print("\nOrder Pulling:")
    for pulling in [True, False]:
        matching = [r for r in results if r.config.order_pulling == pulling]
        if matching:
            avg_rate = np.mean([r.hourly_rate for r in matching])
            pull_str = "ON" if pulling else "OFF"
            print(f"  {pull_str:>4}: ${avg_rate:.2f}/hr (avg)")

    # Entry Pull Timeout (Path 1 experiment)
    unique_timeouts = sorted(set(r.config.entry_order_pull_timeout for r in results))
    if len(unique_timeouts) > 1:
        print("\nEntry Pull Timeout (Path 1):")
        for timeout in unique_timeouts:
            matching = [r for r in results if r.config.entry_order_pull_timeout == timeout]
            if matching:
                avg_rate = np.mean([r.hourly_rate for r in matching])
                print(f"  {timeout:.0f}s: ${avg_rate:.2f}/hr (avg)")

    # Best config details
    if results:
        print()
        print("=" * 110)
        print("BEST CONFIGURATION DETAILS")
        print("=" * 110)

        best = results[0]
        print(f"\nConfiguration:")
        print(f"  Target Shares:  {best.config.target_shares} (total per trade)")
        print(f"  Grid Levels:    {best.config.grid_levels} ({best.config.order_size_per_level} per level)")
        print(f"  Grid Spacing:   {best.config.grid_spacing:.2f}")
        print(f"  Spike Lookback: {best.config.lookback_ms}ms ({best.config.spike_lookback} ticks)")
        print(f"  Stop Loss:      {best.config.stop_loss_pct*100:.0f}%" if best.config.stop_loss_pct else "  Stop Loss:      None")
        print(f"  Order Pulling:  {'ON' if best.config.order_pulling else 'OFF'}")
        print(f"  Entry Timeout:  {best.config.entry_order_pull_timeout:.0f}s")

        print(f"\nPerformance:")
        print(f"  Total PnL (net):   ${best.total_pnl:.2f}")
        print(f"  Total PnL (gross): ${best.total_pnl_gross:.2f}")
        print(f"  Entry Fees:        ${best.total_entry_fees:.2f}")
        print(f"  Hedge Fees:        ${best.total_hedge_fees:.2f}")
        print(f"  Hourly Rate (net): ${best.hourly_rate:.2f}/hr")
        print(f"  Hourly Rate (gross): ${best.hourly_rate_gross:.2f}/hr")
        print(f"  Total Trades:      {best.total_trades}")
        print(f"  Win Rate:          {best.win_rate*100:.1f}%")
        print(f"  Direction Acc:     {best.direction_accuracy*100:.1f}%")

        print(f"\nHedge Breakdown:")
        print(f"  Passive:        {best.passive_hedge_pct*100:.1f}% (${best.passive_pnl:.2f})")
        print(f"  Stop-Loss:      {best.stoploss_hedge_pct*100:.1f}% (${best.stoploss_pnl:.2f})")
        print(f"  Resolution:     {best.resolution_pct*100:.1f}% (${best.resolution_pnl:.2f})")
        if best.pulled_pct > 0:
            print(f"  Pulled:         {best.pulled_pct*100:.1f}%")

        print(f"\nCapital:")
        print(f"  Est. per trade: ${best.config.estimated_cost:.2f}")
        print(f"  Exceeds $170:   {'Yes' if best.capital_exceeded else 'No'}")


def save_results(results: List[OptResult], output_path: str):
    """Save results to CSV file."""
    rows = [r.to_dict() for r in results]
    df = pd.DataFrame(rows)
    df = df.sort_values('hourly_rate', ascending=False)
    df.to_csv(output_path, index=False)
    print(f"\nResults saved to: {output_path}")


# =============================================================================
# MAIN
# =============================================================================

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Enhanced Spike Parameter Optimization - PURE EWMA THRESHOLD VERSION",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument("--quick", action="store_true",
                        help="Run quick test with reduced parameter space")
    parser.add_argument("--workers", type=int, default=4,
                        help="Number of parallel workers")
    parser.add_argument("--output", type=str, default=None,
                        help="Output CSV file path")
    parser.add_argument("--btc-file", type=str, default=None,
                        help="Path to Binance BTC price CSV file")
    parser.add_argument("--obs-file", type=str, default=None,
                        help="Path to observer CSV file")
    parser.add_argument("--res-file", type=str, default=None,
                        help="Path to market resolutions CSV file")
    parser.add_argument("--path", type=str, default="path1",
                        choices=["path1", "both"],
                        help="Experiment path (default: path1 for EWMA testing)")
    parser.add_argument("--slippage", type=float, default=0.0,
                        help="Entry slippage above ask (e.g., 0.01 means fill at ask+0.01)")
    parser.add_argument("--start-ts", type=int, default=None,
                        help="Filter data to only include timestamps >= this (ms)")
    parser.add_argument("--end-ts", type=int, default=None,
                        help="Filter data to only include timestamps <= this (ms)")

    return parser.parse_args()


def main():
    args = parse_args()

    print("=" * 110)
    print("ENHANCED SPIKE PARAMETER OPTIMIZATION - PURE EWMA THRESHOLD")
    print("=" * 110)
    print()
    print("Entry Logic: TAKER (fill at ask, immediate execution)")
    print("Threshold:   EWMA ratio (fast_vol / slow_vol)")
    print("  - fast_vol: Short-term EWMA (30s, 60s, 120s halflife)")
    print("  - slow_vol: Baseline EWMA (180s, 300s, 600s halflife)")
    print("  - threshold = base × clamp(ratio, 0.5, 3.0)")
    print()

    # Load data
    btc_df, obs_df, hours, res_map = load_data(
        btc_file=args.btc_file,
        obs_file=args.obs_file,
        res_file=args.res_file,
        start_ts=args.start_ts,
        end_ts=args.end_ts
    )

    n_markets = obs_df['market_slug'].nunique()

    # Print configuration info
    configs = generate_param_grid(quick=args.quick, path=args.path)

    print(f"\nOptimization Settings:")
    print(f"  Mode:         {'Quick (reduced grid)' if args.quick else 'Full EWMA grid'}")
    print(f"  Workers:      {args.workers}")
    print(f"  Configs:      {len(configs)}")
    print(f"  Signal:       enhanced (fixed)")
    print(f"  Threshold:    Pure EWMA (fast/slow ratio)")
    print(f"  EWMA Fast:    30s, 60s, 120s halflife")
    print(f"  EWMA Slow:    180s, 300s, 600s halflife")
    print(f"  Min Time:     {MIN_TIME}s (fixed)")
    print(f"  Cycling:      Testing ON and OFF")
    if args.path == "path1":
        print(f"  Lookbacks:    Testing 1000ms, 1200ms, 1400ms")
        print(f"  Stop Losses:  Testing None, 12%, 15%")
        print(f"  Buycounts:    Testing 1, 2, 3, 5")
        print(f"  Entry Pull:   Testing 3s, 5s, 7s, 10s, 15s timeouts")
        print(f"  Order Pull:   OFF (fixed - irrelevant for taker)")

    # Run optimization
    print("\n" + "-" * 110)
    print("Running optimization...")
    results = run_optimization(btc_df, obs_df, hours, res_map,
                              n_workers=args.workers, quick=args.quick,
                              path=args.path, slippage=args.slippage)

    # Print results
    print_results(results, hours, n_markets)

    # Save to CSV if requested
    if args.output:
        save_results(results, args.output)

    print()
    print("=" * 110)


if __name__ == "__main__":
    main()
