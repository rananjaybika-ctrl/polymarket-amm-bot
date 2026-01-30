#!/usr/bin/env python3
"""
AGGRESSIVE Mode Grid Search - Parameter Optimization

Tests combinations of time-stop, hedge, and filter parameters to find optimal
configurations for AGGRESSIVE mode.

MIRRORS LIVE AGGRESSIVE CONFIG:
- threshold_method: "ou" or "ewma" (NOT "regime"/"fixed")
- zscore_method: "ewma" (fixed)

GRID PARAMETERS (7 dimensions, heavily pruned):
┌───────────────────┬─────────────────────────────┬───────┬─────────────────────┐
│ Parameter         │ Values                      │ Count │ Rationale           │
├───────────────────┼─────────────────────────────┼───────┼─────────────────────┤
│ time_stop         │ [20, 40, 60, 80, 120] sec   │ 5     │ Core volume question│
│ lookback_ms       │ [1200, 1600, 2000] ms       │ 3     │ Dropped redundant   │
│ drop_intercept    │ [0.04, 0.06, 0.08]          │ 3     │ Hedge aggression    │
│ target_pair_cost  │ [0.98, 0.99]                │ 2     │ Dropped aggressive  │
│ cycling           │ [True, False]               │ 2     │ Re-entry or not     │
│ threshold_method  │ ["ou", "ewma"]              │ 2     │ OU sigmoid vs EWMA  │
│ velocity_mode     │ ["all", "none"]             │ 2     │ Filter or not       │
└───────────────────┴─────────────────────────────┴───────┴─────────────────────┘

FIXED AT VALIDATED VALUES (not in grid):
- z_score_zone = (0.0, 1.5)  → Validated best
- zscore_method = "ewma"      → Mirrors live (no drift risk)
- skip_low_regime = True      → 48% accuracy is trash

Total: 5 × 3 × 3 × 2 × 2 × 2 × 2 = 720 configurations

PRODUCTION CONFIG:
- BASE_SIZE = 50 shares
- HIGH_ENTRY_THRESHOLD = 0.90

Follows SCRIPT_RULES.md:
- Progress bar (tqdm) ✓
- ETA estimate ✓
- Checkpoint saves every 10 configs ✓

Usage:
    python research/optimizers/aggressive_grid_search.py
    python research/optimizers/aggressive_grid_search.py --quick  # Reduced grid for testing
"""

import argparse
import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from collections import deque
from itertools import product
from tqdm import tqdm
import time
import sys

# =============================================================================
# PRODUCTION CONFIGURATION (NOT TESTING)
# =============================================================================

BASE_SIZE = 50  # PRODUCTION: 50 shares (NOT 10)
HIGH_ENTRY_THRESHOLD = 0.90  # PRODUCTION: skip >= $0.90 (NOT $0.80)

# Fixed parameters
MIN_TIME_REMAINING = 180  # seconds
MIN_RUNTIME_SECS = 300  # 5 minutes minimum market duration

# Spike detection - REGIME-BASED THRESHOLDS (not fixed 0.02%)
# LOW regime has 48% accuracy = worse than coin flip, MUST FILTER OUT
ATR_PERIOD = 14
ATR_WINDOW = 300
LOW_PERCENTILE = 25
HIGH_PERCENTILE = 75
REGIME_THRESHOLDS = {
    "LOW": 0.010,    # 0.01% in low vol
    "MEDIUM": 0.020,  # 0.02% in medium vol
    "HIGH": 0.035,   # 0.035% in high vol
}

# OU-based adaptive threshold parameters (MIRRORS LIVE)
# See PLAN_OU_ADAPTIVE_THRESHOLD.md for derivation
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5          # Multiplier at low vol (z << 0)
OU_K_HIGH = 1.75        # Multiplier at high vol (z >> 0)
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015  # Floor (raised from 0.005 to filter noise)
OU_MAX_THRESHOLD = 0.10   # Ceiling

# EWMA-based adaptive threshold parameters
# Uses fast/slow volatility ratio (no calibration needed)
EWMA_BASE_THRESHOLD = 0.02
EWMA_MIN_THRESHOLD = 0.010
EWMA_MAX_THRESHOLD = 0.10
EWMA_MIN_RATIO = 0.5
EWMA_MAX_RATIO = 3.0
EWMA_FAST_HALFLIFE = 60    # seconds
EWMA_SLOW_HALFLIFE = 300   # seconds

# Z-SCORE FILTER (validated: best zone 0 < z < 1.5)
ZSCORE_EWMA_HALFLIFE_TICKS = 300  # 5 seconds at 60Hz

# Enhanced signal filtering
VELOCITY_CONFIRM_THRESHOLD = 0.10
ENHANCED_SCORE_THRESHOLD = 0.40

# Cycling
MIN_CYCLE_GAP_MS = 200  # Allow fast re-entry after hedge

# Loser bid calculation (base values - grid search will override)
DROP_MULTIPLIER = 0.50

# =============================================================================
# GRID SEARCH PARAMETERS (HEAVILY PRUNED - 720 configs)
# =============================================================================

# CORE PARAMETERS (vary in grid)
TIME_STOPS = [20, 40, 60, 80, 120]  # seconds - Core volume question
LOOKBACKS_MS = [1200, 1600, 2000]   # ms - Dropped 1400 (too close to 1200)
DROP_INTERCEPTS = [0.04, 0.06, 0.08]  # LOWER = HIGHER loser_bid = FASTER fills
TARGET_PAIRS = [0.98, 0.99]         # Dropped 0.97 (too aggressive)
CYCLING_OPTIONS = [True, False]     # Re-enter after hedge or not

# FILTER PARAMETERS (vary in grid)
# MIRRORS LIVE: AGGRESSIVE uses threshold_method="ou", zscore_method="ewma"
THRESHOLD_METHODS = [
    "ou",             # OU-calibrated sigmoid: threshold = base * sigmoid(z-score)
    "ewma",           # EWMA fast/slow ratio: threshold = base * (fast_vol / slow_vol)
]
VELOCITY_MODES = [
    "all",            # Reject only contradicting velocity (validated best)
    "none",           # No velocity filter
]

# FIXED AT VALIDATED VALUES (not in grid)
# These were validated in previous research and don't need re-testing
FIXED_Z_LO = 0.0              # Z-score lower bound (validated: 0 < z < 1.5)
FIXED_Z_HI = 1.5              # Z-score upper bound
FIXED_ZSCORE_METHOD = "ewma"  # No drift risk (OU can drift if market changes)
FIXED_SKIP_LOW_REGIME = True  # LOW regime = 48% accuracy = trash

# =============================================================================
# FEE MODEL (from spike_param_optimizer_taker.py)
# =============================================================================

def polymarket_taker_fee(price: float) -> float:
    """
    Polymarket taker fee: 1.56% * (1 - |2*price - 1|)

    Examples:
        price = 0.50 -> fee = 1.56%
        price = 0.40 -> fee = 1.25%
        price = 0.90 -> fee = 0.31%
    """
    return 0.0156 * (1 - abs(2 * price - 1))


def calculate_pnl_with_fees(winner_entry: float, loser_fill: float, shares: int,
                            is_taker_entry: bool, is_taker_exit: bool) -> Tuple[float, float, float, float]:
    """
    Calculate PnL with proper maker/taker fee handling.

    Args:
        winner_entry: Entry price for winner side
        loser_fill: Fill price for loser hedge
        shares: Number of shares
        is_taker_entry: True if entry was taker (crossed spread)
        is_taker_exit: True if exit was taker (time-stop market order)

    Returns:
        Tuple of (net_pnl, gross_pnl, entry_fee, exit_fee)
    """
    pair_cost = winner_entry + loser_fill
    gross_pnl = (1.0 - pair_cost) * shares

    entry_fee = polymarket_taker_fee(winner_entry) * winner_entry * shares if is_taker_entry else 0
    exit_fee = polymarket_taker_fee(loser_fill) * loser_fill * shares if is_taker_exit else 0

    net_pnl = gross_pnl - entry_fee - exit_fee
    return net_pnl, gross_pnl, entry_fee, exit_fee


# =============================================================================
# VOLATILITY & REGIME CLASSIFICATION (from spike_param_optimizer_taker.py)
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


def compute_ewma_volatility(prices: pd.Series, halflife_ticks: int = ZSCORE_EWMA_HALFLIFE_TICKS) -> pd.Series:
    """
    Compute EWMA volatility for z-score calculation.

    Returns rolling EWMA standard deviation of returns.
    """
    returns = prices.pct_change() * 100  # % returns
    alpha = 1 - 0.5 ** (1.0 / halflife_ticks)

    # EWMA variance
    variance = returns.ewm(alpha=alpha, adjust=False).var()
    volatility = np.sqrt(variance)

    return volatility


def compute_zscore_ewma(volatility: pd.Series) -> pd.Series:
    """
    Compute EWMA-based z-score of log volatility.

    Uses rolling statistics - fully adaptive, no drift.
    z = (log(vol) - rolling_mean(log(vol))) / rolling_std(log(vol))
    """
    log_vol = np.log(volatility.clip(lower=1e-6))
    rolling_mean = log_vol.rolling(window=ZSCORE_EWMA_HALFLIFE_TICKS * 2, min_periods=60).mean()
    rolling_std = log_vol.rolling(window=ZSCORE_EWMA_HALFLIFE_TICKS * 2, min_periods=60).std()

    zscore = (log_vol - rolling_mean) / rolling_std.clip(lower=1e-6)
    return zscore


def load_ou_params(filepath: str = "research/ou_params.json") -> Optional[dict]:
    """Load OU parameters from JSON file."""
    import json
    try:
        with open(filepath, 'r') as f:
            params = json.load(f)
        print(f"  [OU] Loaded: mu={params['mu']:.4f}, sigma_stat={params['sigma_stat']:.4f}")
        return params
    except Exception as e:
        print(f"  [OU] WARNING: Could not load params from {filepath}: {e}")
        return None


def compute_zscore_ou(volatility: pd.Series, ou_params: dict) -> pd.Series:
    """
    Compute OU-based z-score using pre-calibrated parameters.

    Uses fixed mu and sigma_stat from OU calibration.
    z = (log(vol) - mu) / sigma_stat

    NOTE: Can drift if market regime changes from calibration period.
    """
    if ou_params is None:
        return pd.Series(0.0, index=volatility.index)

    mu = ou_params['mu']
    sigma_stat = ou_params['sigma_stat']

    log_vol = np.log(volatility.clip(lower=1e-6))
    zscore = (log_vol - mu) / sigma_stat
    return zscore


def compute_ou_threshold(volatility: float, ou_params: dict) -> float:
    """
    Compute OU-based adaptive threshold from volatility.

    Uses sigmoid mapping on z-score: threshold = base * multiplier
    where multiplier = k_low + (k_high - k_low) / (1 + exp(-steepness * z))

    MIRRORS LIVE: src/strategies/volatility_regime.py
    """
    import math
    if ou_params is None:
        return OU_BASE_THRESHOLD

    vol = max(volatility, 1e-6)
    log_vol = math.log(vol)
    z_score = (log_vol - ou_params['mu']) / ou_params['sigma_stat']

    # Sigmoid mapping
    z_clamped = max(-10, min(10, z_score * OU_SIGMOID_STEEPNESS))
    sigmoid = 1.0 / (1.0 + math.exp(-z_clamped))
    multiplier = OU_K_LOW + (OU_K_HIGH - OU_K_LOW) * sigmoid

    threshold = OU_BASE_THRESHOLD * multiplier
    return max(OU_MIN_THRESHOLD, min(OU_MAX_THRESHOLD, threshold))


class EWMAThresholdTracker:
    """
    Pure EWMA-based adaptive threshold (MIRRORS LIVE).

    Uses two EWMA windows:
    - Fast EWMA: tracks current volatility (60s halflife)
    - Slow EWMA: tracks baseline volatility (300s halflife)
    - Ratio = fast/slow determines threshold multiplier
    """
    import math

    def __init__(self, fast_halflife_sec: float = EWMA_FAST_HALFLIFE,
                 slow_halflife_sec: float = EWMA_SLOW_HALFLIFE,
                 tick_interval_sec: float = 1/60):
        import math
        self.fast_halflife = fast_halflife_sec
        self.slow_halflife = slow_halflife_sec
        self.tick_interval = tick_interval_sec

        # Compute decay factors
        self.fast_alpha = 1 - math.exp(-math.log(2) * tick_interval_sec / fast_halflife_sec)
        self.slow_alpha = 1 - math.exp(-math.log(2) * tick_interval_sec / slow_halflife_sec)

        self.fast_var = None
        self.slow_var = None
        self.last_price = None

    def update(self, price: float) -> float:
        """Update with new price, return adaptive threshold."""
        import math
        if self.last_price is None:
            self.last_price = price
            return EWMA_BASE_THRESHOLD

        ret = (price - self.last_price) / self.last_price
        ret_sq = ret * ret
        self.last_price = price

        if self.fast_var is None:
            self.fast_var = ret_sq
            self.slow_var = ret_sq
            return EWMA_BASE_THRESHOLD

        self.fast_var = self.fast_alpha * ret_sq + (1 - self.fast_alpha) * self.fast_var
        self.slow_var = self.slow_alpha * ret_sq + (1 - self.slow_alpha) * self.slow_var

        slow_vol = math.sqrt(max(self.slow_var, 1e-12))
        fast_vol = math.sqrt(max(self.fast_var, 1e-12))

        ratio = fast_vol / slow_vol if slow_vol > 1e-8 else 1.0
        ratio = max(EWMA_MIN_RATIO, min(EWMA_MAX_RATIO, ratio))

        threshold = EWMA_BASE_THRESHOLD * ratio
        return max(EWMA_MIN_THRESHOLD, min(EWMA_MAX_THRESHOLD, threshold))


def compute_ewma_thresholds(btc_df: pd.DataFrame) -> pd.Series:
    """Pre-compute EWMA thresholds for all BTC prices."""
    tracker = EWMAThresholdTracker()
    thresholds = []
    for price in btc_df['price'].values:
        threshold = tracker.update(price)
        thresholds.append(threshold)
    return pd.Series(thresholds, index=btc_df.index)


def precompute_volatility_data(btc_df: pd.DataFrame) -> pd.DataFrame:
    """
    Pre-compute volatility, regime, z-scores, and BOTH threshold methods for all Binance data.

    Computes:
    - zscore_ewma: EWMA-based z-score (adaptive, no drift) - USED FOR Z-SCORE FILTER
    - threshold_ou: OU-based adaptive threshold (sigmoid mapping)
    - threshold_ewma: EWMA-based adaptive threshold (fast/slow ratio)

    This is done ONCE before the grid search to avoid repeated computation.
    """
    print("Pre-computing volatility data...")

    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    # ATR for regime classification (used for LOW regime skip)
    df['atr'] = calculate_rolling_atr(df['price'])
    df['regime'] = classify_regime_vectorized(df['atr'])

    # EWMA volatility (used for z-score filter)
    df['ewma_vol'] = compute_ewma_volatility(df['price'])

    # Z-score for filtering (FIXED at EWMA method - mirrors live)
    df['zscore_ewma'] = compute_zscore_ewma(df['ewma_vol'])

    # Load OU params for OU threshold
    ou_params = load_ou_params()

    # Threshold method 1: OU-based adaptive (MIRRORS LIVE AGGRESSIVE)
    print("  Computing OU thresholds...")
    df['threshold_ou'] = df['ewma_vol'].apply(lambda v: compute_ou_threshold(v, ou_params))

    # Threshold method 2: EWMA-based adaptive (fast/slow ratio)
    print("  Computing EWMA thresholds...")
    df['threshold_ewma'] = compute_ewma_thresholds(df)

    # Legacy regime threshold (for reference)
    df['threshold_regime'] = df['regime'].map(REGIME_THRESHOLDS)
    df['threshold_regime'] = df['threshold_regime'].fillna(REGIME_THRESHOLDS['MEDIUM'])

    print(f"  Regime distribution: {df['regime'].value_counts().to_dict()}")
    print(f"  Z-score EWMA range: [{df['zscore_ewma'].min():.2f}, {df['zscore_ewma'].max():.2f}]")
    print(f"  OU threshold range: [{df['threshold_ou'].min():.4f}, {df['threshold_ou'].max():.4f}]")
    print(f"  EWMA threshold range: [{df['threshold_ewma'].min():.4f}, {df['threshold_ewma'].max():.4f}]")

    return df


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class GridConfig:
    """Single grid search configuration (heavily pruned, mirrors live)."""
    # Core parameters (vary in grid)
    time_stop_sec: int
    lookback_ms: int
    drop_intercept: float
    target_pair_cost: float
    cycling: bool                     # Re-enter after hedge completes

    # Filter parameters (vary in grid)
    threshold_method: str             # "ou" or "ewma" (MIRRORS LIVE)
    velocity_mode: str                # "all" or "none"

    # Fixed at validated values (not in grid)
    z_lo: float = FIXED_Z_LO          # 0.0 (validated)
    z_hi: float = FIXED_Z_HI          # 1.5 (validated)
    zscore_method: str = FIXED_ZSCORE_METHOD  # "ewma" (no drift)
    skip_low_regime: bool = FIXED_SKIP_LOW_REGIME  # True (48% = trash)

    @property
    def lookback_ticks(self) -> int:
        """Convert lookback ms to ticks at 60Hz."""
        return max(1, int(self.lookback_ms * 60 / 1000))

    @property
    def z_zone_label(self) -> str:
        return f"{self.z_lo}<z<{self.z_hi}"

    def __str__(self) -> str:
        return (f"TS{self.time_stop_sec}_LB{self.lookback_ms}_DI{self.drop_intercept:.2f}_"
                f"TP{self.target_pair_cost:.2f}_CY{'Y' if self.cycling else 'N'}_"
                f"TH{self.threshold_method[:3]}_VE{self.velocity_mode[:3]}")


@dataclass
class TradeResult:
    """Result from a single trade cycle."""
    market_slug: str
    cycle_num: int
    entry_time_remaining: float
    winner_side: str
    winner_entry: float
    loser_fill: float
    hedge_type: str  # 'passive', 'time_stop', 'resolution'
    pair_cost: float
    pnl_net: float
    pnl_gross: float
    entry_fee: float
    exit_fee: float
    correct_direction: bool
    spike_magnitude: float
    entry_ts: int
    hedge_ts: int


@dataclass
class BacktestResult:
    """Aggregated results for one configuration."""
    config: GridConfig
    total_trades: int
    total_pnl_net: float
    total_pnl_gross: float
    total_entry_fees: float
    total_exit_fees: float
    hourly_rate_net: float
    hourly_rate_gross: float
    win_rate: float
    direction_accuracy: float
    passive_pct: float
    time_stop_pct: float
    resolution_pct: float
    avg_pair_cost: float
    trades: List[TradeResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'config': str(self.config),
            # Core parameters (varied)
            'time_stop': self.config.time_stop_sec,
            'lookback_ms': self.config.lookback_ms,
            'drop_intercept': self.config.drop_intercept,
            'target_pair': self.config.target_pair_cost,
            'cycling': self.config.cycling,
            # Filter parameters (varied)
            'threshold_method': self.config.threshold_method,
            'velocity_mode': self.config.velocity_mode,
            # Fixed parameters (for reference)
            'z_zone': self.config.z_zone_label,
            'zscore_method': self.config.zscore_method,
            'skip_low_regime': self.config.skip_low_regime,
            # Results
            'trades': self.total_trades,
            'pnl_net': self.total_pnl_net,
            'pnl_gross': self.total_pnl_gross,
            'entry_fees': self.total_entry_fees,
            'exit_fees': self.total_exit_fees,
            'pnl_hr_net': self.hourly_rate_net,
            'pnl_hr_gross': self.hourly_rate_gross,
            'win_rate': self.win_rate,
            'dir_acc': self.direction_accuracy,
            'passive_pct': self.passive_pct,
            'time_stop_pct': self.time_stop_pct,
            'resolution_pct': self.resolution_pct,
            'avg_pair_cost': self.avg_pair_cost,
        }


# =============================================================================
# SPIKE DETECTION (supports both REGIME and FIXED thresholds)
# =============================================================================

def detect_spikes_for_lookback(btc_vol_df: pd.DataFrame, lookback_ticks: int,
                                threshold_method: str = "ou") -> pd.DataFrame:
    """
    Detect spikes using specified threshold method.

    MIRRORS LIVE: Uses "ou" or "ewma" threshold methods (not "regime"/"fixed").

    Args:
        btc_vol_df: DataFrame with pre-computed volatility data (threshold_ou, threshold_ewma, zscore_ewma)
        lookback_ticks: Number of ticks to look back for spike detection
        threshold_method: "ou" (OU sigmoid) or "ewma" (fast/slow ratio)

    Returns:
        DataFrame with ALL spikes including zscore_ewma column.
        Filtering (z-score, LOW regime) done during simulation based on config.
    """
    df = btc_vol_df.copy()

    # Calculate % change over lookback period
    df['price_prev'] = df['price'].shift(lookback_ticks)
    df['change_pct'] = (df['price'] - df['price_prev']) / df['price_prev'] * 100
    df['magnitude'] = df['change_pct'].abs()

    # Choose threshold based on method (MIRRORS LIVE)
    if threshold_method == "ou":
        df['threshold_used'] = df['threshold_ou']
    elif threshold_method == "ewma":
        df['threshold_used'] = df['threshold_ewma']
    else:
        raise ValueError(f"Unknown threshold_method: {threshold_method}. Use 'ou' or 'ewma'.")

    # Spike detected if magnitude >= threshold
    df['spike_detected'] = df['magnitude'] >= df['threshold_used']

    # Direction
    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['change_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['change_pct'] < 0), 'spike_direction'] = 'DOWN'
    df['spike_magnitude'] = df['magnitude'].where(df['spike_detected'], 0)

    # Return ALL spikes with zscore_ewma (used for z-score filter)
    spikes_only = df[df['spike_detected'] == True].copy()

    return spikes_only[['timestamp_ms', 'price', 'spike_direction', 'spike_magnitude',
                        'regime', 'threshold_used', 'zscore_ewma']]


def precompute_spikes_all_lookbacks(btc_vol_df: pd.DataFrame,
                                    lookbacks_ms: List[int],
                                    threshold_methods: List[str]) -> Dict[Tuple[int, str], pd.DataFrame]:
    """
    Pre-compute spikes for all lookback + threshold_method combinations.

    This is done ONCE before grid search to avoid repeated computation.

    Returns:
        Dict mapping (lookback_ms, threshold_method) -> spikes DataFrame
    """
    print("\nPre-computing spikes for all lookback + threshold combinations...")
    spikes_cache = {}

    for lb_ms in lookbacks_ms:
        lookback_ticks = max(1, int(lb_ms * 60 / 1000))
        for th_method in threshold_methods:
            spikes_df = detect_spikes_for_lookback(btc_vol_df, lookback_ticks, th_method)
            spikes_cache[(lb_ms, th_method)] = spikes_df
            print(f"  {lb_ms}ms + {th_method}: {len(spikes_df):,} spikes")

    return spikes_cache


class SpikeDetector:
    """60Hz spike detection with adaptive threshold support."""

    def __init__(self, lookback_ticks: int):
        self.lookback = lookback_ticks
        self.price_history = deque(maxlen=max(100, lookback_ticks + 10))

    def detect(self, price: float, threshold: float) -> Tuple[Optional[str], float]:
        """
        Detect spike from 60Hz price data using given threshold.

        Returns: (direction, magnitude_pct) or (None, 0)
        """
        self.price_history.append(price)

        if len(self.price_history) < self.lookback + 1:
            return None, 0.0

        current = self.price_history[-1]
        previous = self.price_history[-(self.lookback + 1)]

        if previous <= 0:
            return None, 0.0

        change_pct = (current - previous) / previous * 100
        magnitude = abs(change_pct)

        if magnitude >= threshold:
            direction = "UP" if change_pct > 0 else "DOWN"
            return direction, magnitude

        return None, 0.0

    def reset(self):
        self.price_history.clear()


def velocity_confirms_spike(spike_dir: str, velocity_bps: float, velocity_mode: str) -> bool:
    """
    Check if velocity confirms spike direction.

    Args:
        spike_dir: "UP" or "DOWN"
        velocity_bps: Current velocity in basis points
        velocity_mode: "all" (reject contradicting) or "none" (no filter)

    Returns:
        True if trade should proceed, False if rejected
    """
    if velocity_mode == "none":
        return True  # No velocity filter

    # velocity_mode == "all": Reject only contradicting velocity
    if spike_dir == "UP":
        # REJECT if spike UP but velocity < -0.10 (14% accuracy)
        return velocity_bps > -VELOCITY_CONFIRM_THRESHOLD
    elif spike_dir == "DOWN":
        # REJECT if spike DOWN but velocity > +0.10 (43% accuracy)
        return velocity_bps < VELOCITY_CONFIRM_THRESHOLD
    return True


def compute_enhanced_score(spike_mag: float, velocity_bps: float,
                           spike_dir: str, time_remaining: float) -> float:
    """
    Compute composite score (matching live strategy).

    Formula: 0.40*spike + 0.30*velocity + 0.20*confirmation + 0.10*urgency
    """
    spike_score = min(spike_mag / 0.05, 1.0)
    velocity_score = min(abs(velocity_bps) / 0.50, 1.0)

    vel_confirms = (spike_dir == "UP" and velocity_bps > 0) or \
                   (spike_dir == "DOWN" and velocity_bps < 0)
    confirm_bonus = 1.0 if vel_confirms else 0.0

    urgency = 1.0 - min(time_remaining / 900.0, 1.0)

    score = (0.40 * spike_score +
             0.30 * velocity_score +
             0.20 * confirm_bonus +
             0.10 * urgency)

    return round(score, 3)


def obi_confirms_spike(spike_dir: str, up_imbalance, down_imbalance) -> bool:
    """
    Check if Order Book Imbalance confirms spike direction.

    OBI CONFIRMATION FILTER (January 28, 2026):
    When OBI confirms spike direction: 89% accuracy vs 77% when disagrees (+4.1pp)

    Args:
        spike_dir: "UP" or "DOWN"
        up_imbalance: OBI for UP token (-1 to +1, positive = buying pressure)
        down_imbalance: OBI for DOWN token (-1 to +1, positive = buying pressure)

    Returns:
        True if OBI confirms or is unavailable, False if OBI disagrees
    """
    if spike_dir == "UP" and up_imbalance is not None and not np.isnan(up_imbalance):
        # UP spike needs positive UP imbalance (buying pressure on UP)
        return up_imbalance > 0
    elif spike_dir == "DOWN" and down_imbalance is not None and not np.isnan(down_imbalance):
        # DOWN spike needs positive DOWN imbalance (buying pressure on DOWN)
        return down_imbalance > 0
    # If imbalance not available, don't filter
    return True


def calculate_loser_bid(winner_entry: float, spike_magnitude: float,
                        drop_intercept: float, target_pair_cost: float) -> float:
    """
    Calculate loser bid based on spike magnitude and config parameters.

    Formula: expected_drop = DROP_MULTIPLIER * magnitude + drop_intercept
    loser_bid = loser_ask - expected_drop (bounded by target_pair_cost)

    IMPORTANT: LOWER drop_intercept -> SMALLER expected_drop -> HIGHER loser_bid -> FASTER fills
    """
    # FIX: Do NOT divide by 100 - magnitude is already percentage (0.05 = 0.05%)
    # Matches enhanced_spike.py:526
    expected_drop = DROP_MULTIPLIER * spike_magnitude + drop_intercept
    max_loser = target_pair_cost - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid))


# =============================================================================
# DATA LOADING (from aggressive_main_backtest.py)
# =============================================================================

def load_data(dataset: str = "is_oos2"):
    """
    Load 60Hz Binance data and observer orderbook data.

    Args:
        dataset: Which dataset to load
            - "is_oos2": IS+OOS2 (Jan 16-19, ~82h) - for grid search
            - "oos34": OOS3+OOS4 (Jan 22-24, ~47h) - for validation
            - "oos5": OOS5 (Jan 26, ~42h)
            - "all": All data combined (~229h)
    """
    print(f"Loading data (dataset={dataset})...")

    obs_dir = Path("research/observer")
    if not obs_dir.exists():
        obs_dir = Path("/Users/rananjaybika/polymarket-amm-bot/research/observer")

    btc_dir = Path("research/binance_hf")
    if not btc_dir.exists():
        btc_dir = Path("/Users/rananjaybika/polymarket-amm-bot/research/binance_hf")

    # Select files based on dataset
    if dataset == "is_oos2":
        # IS+OOS2: Jan 16-19 (~82h)
        obs_files = [
            obs_dir / "grid_obs_20260116.csv",
            obs_dir / "grid_obs_20260117.csv",
            obs_dir / "grid_obs_20260118.csv",
            obs_dir / "grid_obs_20260119.csv",
        ]
        btc_files = [btc_dir / "btc_prices_combined.csv"]
        print("  Dataset: IS+OOS2 (Jan 16-19, ~82h)")

    elif dataset == "oos34":
        # OOS3+OOS4: Jan 22-24 (~47h)
        obs_files = [obs_dir / "grid_obs_oos3_oos4_combined.csv"]
        btc_files = [obs_dir / "btc_prices_oos3_oos4_combined.csv"]
        print("  Dataset: OOS3+OOS4 (Jan 22-24, ~47h)")

    elif dataset == "oos5":
        # OOS5: Jan 26 (~42h)
        obs_files = [obs_dir / "grid_obs_oos5.csv"]
        btc_files = [btc_dir / "btc_prices_20260124_recovered.csv"]
        print("  Dataset: OOS5 (Jan 26, ~42h)")

    else:  # "all"
        # Load ALL files
        obs_files = list(obs_dir.glob("grid_obs_*.csv"))
        btc_files = list(btc_dir.glob("btc_prices_*.csv"))
        print("  Dataset: ALL combined")

    # Load Binance data
    btc_dfs = []
    for f in btc_files:
        if f.exists():
            df = pd.read_csv(f)
            btc_dfs.append(df)
            print(f"  Binance: {len(df):,} rows ({f.name})")

    if not btc_dfs:
        raise FileNotFoundError(f"No Binance files found for dataset={dataset}")

    btc_df = pd.concat(btc_dfs, ignore_index=True)
    btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
    print(f"  Binance TOTAL: {len(btc_df):,} rows")

    # Load observer data
    obs_dfs = []
    for f in obs_files:
        if f.exists():
            df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)
            print(f"  Observer: {len(df):,} rows ({f.name})")

    if not obs_dfs:
        raise FileNotFoundError(f"No observer files found for dataset={dataset}")

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    print(f"  Observer TOTAL: {len(obs_df):,} rows")

    # Load resolutions
    res_path = obs_dir / "market_resolutions_verified.csv"
    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Find overlap period
    btc_start, btc_end = btc_df['timestamp_ms'].min(), btc_df['timestamp_ms'].max()
    obs_start, obs_end = obs_df['timestamp_ms'].min(), obs_df['timestamp_ms'].max()

    overlap_start = max(btc_start, obs_start)
    overlap_end = min(btc_end, obs_end)

    duration_hours = (overlap_end - overlap_start) / 3600000
    print(f"\nOverlap period: {duration_hours:.2f} hours")

    # Filter to overlap
    btc_df = btc_df[(btc_df['timestamp_ms'] >= overlap_start) &
                     (btc_df['timestamp_ms'] <= overlap_end)].copy()
    obs_df = obs_df[(obs_df['timestamp_ms'] >= overlap_start) &
                     (obs_df['timestamp_ms'] <= overlap_end)].copy()

    # Add resolutions
    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    # Filter valid markets
    valid_slugs = []
    for slug, mdf in obs_df.groupby('market_slug'):
        max_time = mdf['time_remaining_secs'].max()
        min_time = mdf['time_remaining_secs'].min()
        duration = max_time - min_time
        if duration >= MIN_RUNTIME_SECS and max_time >= 840:
            valid_slugs.append(slug)

    obs_df = obs_df[obs_df['market_slug'].isin(valid_slugs)]

    print(f"Valid markets: {len(valid_slugs)}")
    print(f"Observer rows: {len(obs_df):,}")
    print(f"Binance rows: {len(btc_df):,}")

    return btc_df, obs_df, duration_hours, res_map


# =============================================================================
# BACKTEST SIMULATION (uses pre-computed spikes with z-score filter)
# =============================================================================

def simulate_market_with_spikes(spikes_df: pd.DataFrame, obs_df: pd.DataFrame,
                                 slug: str, resolution: str, config: GridConfig) -> List[TradeResult]:
    """
    Simulate trading on a single market using PRE-COMPUTED spikes.

    Applies filters based on config:
    1. Z-score filter: config.z_lo < z < config.z_hi
    2. LOW regime skip: config.skip_low_regime
    3. Velocity confirmation filter: config.velocity_mode
    4. High entry skip (>= $0.90)
    5. Cycling: config.cycling (True = re-enter, False = one trade per market)

    CRITICAL: Uses last_hedge_ts for cycling gap (NOT entry timestamp).
    """
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    # Get market time range
    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    # Get spikes in this market's time range
    market_spikes = spikes_df[
        (spikes_df['timestamp_ms'] >= market_start) &
        (spikes_df['timestamp_ms'] <= market_end)
    ].copy()

    if len(market_spikes) == 0:
        return []

    trades = []
    cycle_num = 0
    last_hedge_ts = 0  # CRITICAL: Track hedge fill time, NOT entry time
    time_stop_ms = config.time_stop_sec * 1000
    has_traded = False  # For cycling=False mode

    # Iterate through pre-computed spikes
    for _, spike_row in market_spikes.iterrows():
        spike_ts = spike_row['timestamp_ms']
        spike_dir = spike_row['spike_direction']
        spike_mag = spike_row['spike_magnitude']
        regime = spike_row.get('regime', 'MEDIUM')

        # Z-score filter uses EWMA method (FIXED - mirrors live AGGRESSIVE config)
        zscore = spike_row.get('zscore_ewma', 0.0)

        # CYCLING CHECK: If cycling=False and already traded, skip
        if not config.cycling and has_traded:
            continue

        # Enforce minimum gap after HEDGE (not entry) - only matters if cycling
        if config.cycling and (spike_ts - last_hedge_ts) < MIN_CYCLE_GAP_MS:
            continue

        # LOW REGIME FILTER (based on config)
        if config.skip_low_regime and regime == 'LOW':
            continue

        # Z-SCORE FILTER (based on config)
        if pd.notna(zscore):
            if config.z_lo is not None and zscore < config.z_lo:
                continue
            if config.z_hi is not None and zscore > config.z_hi:
                continue

        # Find nearest observer row
        obs_idx = mdf['timestamp_ms'].searchsorted(spike_ts)
        if obs_idx >= len(mdf):
            obs_idx = len(mdf) - 1

        obs_row = mdf.iloc[obs_idx]
        time_rem = obs_row['time_remaining_secs']
        velocity_bps = obs_row.get('velocity_bps', 0.0) or 0.0

        # Skip if too close to end
        if time_rem < MIN_TIME_REMAINING:
            continue

        # Apply velocity confirmation filter (based on config.velocity_mode)
        if not velocity_confirms_spike(spike_dir, velocity_bps, config.velocity_mode):
            continue

        # Apply enhanced score filter
        score = compute_enhanced_score(spike_mag, velocity_bps, spike_dir, time_rem)
        if score < ENHANCED_SCORE_THRESHOLD:
            continue

        # OBI CONFIRMATION FILTER (Jan 28, 2026): +4.1pp accuracy improvement
        # When OBI confirms spike direction: 89% accuracy vs 77% when disagrees
        up_imbalance = obs_row.get('up_imbalance', None)
        down_imbalance = obs_row.get('down_imbalance', None)
        if not obi_confirms_spike(spike_dir, up_imbalance, down_imbalance):
            continue

        # Check high entry threshold (PRODUCTION: $0.90)
        winner_side = spike_dir
        if winner_side == "UP":
            winner_ask = obs_row['up_ask']
        else:
            winner_ask = obs_row['down_ask']

        if winner_ask >= HIGH_ENTRY_THRESHOLD:
            continue

        # ENTRY SIGNAL - take position
        cycle_num += 1
        has_traded = True  # Mark that we've traded (for cycling=False)
        loser_side = "DOWN" if winner_side == "UP" else "UP"
        winner_entry = winner_ask

        # Calculate loser bid target using config parameters
        loser_target = calculate_loser_bid(
            winner_entry, spike_mag,
            config.drop_intercept, config.target_pair_cost
        )

        # Scan forward for hedge fill or time-stop
        hedge_type = "resolution"
        loser_fill = 0.0
        hedge_fill_ts = market_end

        for j in range(obs_idx + 1, len(mdf)):
            scan_row = mdf.iloc[j]
            scan_ts = scan_row['timestamp_ms']

            if loser_side == "UP":
                loser_ask = scan_row['up_ask']
            else:
                loser_ask = scan_row['down_ask']

            # Check passive fill (ask crosses through our bid)
            if loser_ask <= loser_target:
                loser_fill = loser_target
                hedge_type = "passive"
                hedge_fill_ts = scan_ts
                break

            # Check time-stop (ONLY if NOT in profit - matches live enhanced_spike.py)
            elapsed_ms = scan_ts - spike_ts
            if elapsed_ms >= time_stop_ms:
                # Get current winner bid to check if in profit
                if winner_side == "UP":
                    winner_bid_current = scan_row['up_bid']
                else:
                    winner_bid_current = scan_row['down_bid']

                # Check if in profit: winner_bid >= entry price
                in_profit = pd.notna(winner_bid_current) and winner_bid_current >= winner_entry

                if not in_profit:
                    # NOT in profit - execute time-stop
                    loser_fill = loser_ask  # Market order at current ask
                    hedge_type = "time_stop"
                    hedge_fill_ts = scan_ts
                    break
                # else: in profit, keep waiting for passive fill

        # Resolution handling
        if hedge_type == "resolution":
            if resolution == winner_side:
                hedge_type = "passive"  # Direction correct, loser goes to $0
                loser_fill = loser_target
            else:
                loser_fill = 1.0  # Direction wrong, loser goes to $1

        # Calculate PnL with fees
        is_taker_exit = (hedge_type == "time_stop")
        pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
            winner_entry, loser_fill, BASE_SIZE,
            is_taker_entry=True, is_taker_exit=is_taker_exit
        )

        trades.append(TradeResult(
            market_slug=slug,
            cycle_num=cycle_num,
            entry_time_remaining=time_rem,
            winner_side=winner_side,
            winner_entry=winner_entry,
            loser_fill=loser_fill,
            hedge_type=hedge_type,
            pair_cost=winner_entry + loser_fill,
            pnl_net=pnl_net,
            pnl_gross=pnl_gross,
            entry_fee=entry_fee,
            exit_fee=exit_fee,
            correct_direction=(resolution == winner_side),
            spike_magnitude=spike_mag,
            entry_ts=spike_ts,
            hedge_ts=hedge_fill_ts,
        ))

        # Update last hedge timestamp for cycling
        last_hedge_ts = hedge_fill_ts

    return trades


# Legacy simulation function (not used, kept for reference)
def simulate_market(btc_df: pd.DataFrame, obs_df: pd.DataFrame,
                    slug: str, resolution: str, config: GridConfig) -> List[TradeResult]:
    """
    LEGACY: Simulate trading with tick-by-tick spike detection.
    Use simulate_market_with_spikes() instead for better performance.
    """
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    # Get Binance data for this market's time range
    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    btc_market = btc_df[(btc_df['timestamp_ms'] >= market_start - 1000) &
                        (btc_df['timestamp_ms'] <= market_end + 1000)].copy()
    btc_market = btc_market.sort_values('timestamp_ms').reset_index(drop=True)

    if len(btc_market) == 0:
        return []

    trades = []
    detector = SpikeDetector(lookback_ticks=config.lookback_ticks)

    cycle_num = 0
    last_hedge_ts = 0  # CRITICAL: Track hedge fill time, NOT entry time
    in_position = False
    position_data = None

    # Process each Binance tick
    btc_idx = 0
    obs_idx = 0

    time_stop_ms = config.time_stop_sec * 1000

    while btc_idx < len(btc_market):
        btc_row = btc_market.iloc[btc_idx]
        btc_ts = btc_row['timestamp_ms']
        btc_price = btc_row['price']
        threshold = btc_row.get('threshold', REGIME_THRESHOLDS['MEDIUM'])
        regime = btc_row.get('regime', 'MEDIUM')

        # Skip LOW regime (48% accuracy = worse than coin flip)
        if regime == 'LOW':
            btc_idx += 1
            continue

        # Find nearest observer row
        while obs_idx < len(mdf) - 1 and mdf.iloc[obs_idx + 1]['timestamp_ms'] <= btc_ts:
            obs_idx += 1

        if obs_idx >= len(mdf):
            break

        obs_row = mdf.iloc[obs_idx]
        time_rem = obs_row['time_remaining_secs']
        velocity_bps = obs_row.get('velocity_bps', 0.0) or 0.0

        # Skip if too close to end
        if time_rem < MIN_TIME_REMAINING:
            btc_idx += 1
            continue

        # If in position, check for hedge/time-stop
        if in_position and position_data is not None:
            winner_side = position_data['winner_side']
            loser_side = position_data['loser_side']
            winner_entry = position_data['winner_entry']
            loser_target = position_data['loser_target']
            entry_ts = position_data['entry_ts']
            spike_mag = position_data['spike_magnitude']

            # Get current prices
            if loser_side == "UP":
                loser_ask = obs_row['up_ask']
            else:
                loser_ask = obs_row['down_ask']

            # Check passive fill (ask crosses through our bid)
            if loser_ask <= loser_target:
                # PASSIVE HEDGE FILL (maker - no exit fee)
                loser_fill = loser_target
                pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                    winner_entry, loser_fill, BASE_SIZE,
                    is_taker_entry=True, is_taker_exit=False  # Passive hedge = maker
                )

                trades.append(TradeResult(
                    market_slug=slug,
                    cycle_num=cycle_num,
                    entry_time_remaining=position_data['entry_time_rem'],
                    winner_side=winner_side,
                    winner_entry=winner_entry,
                    loser_fill=loser_fill,
                    hedge_type="passive",
                    pair_cost=winner_entry + loser_fill,
                    pnl_net=pnl_net,
                    pnl_gross=pnl_gross,
                    entry_fee=entry_fee,
                    exit_fee=exit_fee,
                    correct_direction=(resolution == winner_side),
                    spike_magnitude=spike_mag,
                    entry_ts=entry_ts,
                    hedge_ts=btc_ts,
                ))

                in_position = False
                position_data = None
                last_hedge_ts = btc_ts  # CRITICAL: Use hedge fill time
                detector.reset()  # Clear history for fresh signals
                btc_idx += 1
                continue

            # Check time-stop (ONLY if NOT in profit - matches live enhanced_spike.py)
            elapsed_ms = btc_ts - entry_ts
            if elapsed_ms >= time_stop_ms:
                # Get current winner bid to check if in profit
                if winner_side == "UP":
                    winner_bid_current = obs_row['up_bid']
                else:
                    winner_bid_current = obs_row['down_bid']

                # Check if in profit: winner_bid >= entry price
                in_profit = pd.notna(winner_bid_current) and winner_bid_current >= winner_entry

                if not in_profit:
                    # TIME-STOP EXIT (taker - pay exit fee)
                    loser_fill = loser_ask  # Market order at current ask
                    pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                        winner_entry, loser_fill, BASE_SIZE,
                        is_taker_entry=True, is_taker_exit=True  # Taker exit
                    )

                    trades.append(TradeResult(
                        market_slug=slug,
                        cycle_num=cycle_num,
                        entry_time_remaining=position_data['entry_time_rem'],
                        winner_side=winner_side,
                        winner_entry=winner_entry,
                        loser_fill=loser_fill,
                        hedge_type="time_stop",
                        pair_cost=winner_entry + loser_fill,
                        pnl_net=pnl_net,
                        pnl_gross=pnl_gross,
                        entry_fee=entry_fee,
                        exit_fee=exit_fee,
                        correct_direction=(resolution == winner_side),
                        spike_magnitude=spike_mag,
                        entry_ts=entry_ts,
                        hedge_ts=btc_ts,
                    ))

                    in_position = False
                    position_data = None
                    last_hedge_ts = btc_ts  # CRITICAL: Use hedge fill time
                    detector.reset()
                    btc_idx += 1
                    continue
                # else: in profit, keep waiting for passive fill

            btc_idx += 1
            continue

        # Not in position - look for entry signal
        # Enforce minimum gap after HEDGE (not entry) - allows fast cycling
        if (btc_ts - last_hedge_ts) < MIN_CYCLE_GAP_MS:
            btc_idx += 1
            continue

        # Detect spike with regime-based threshold
        spike_dir, spike_mag = detector.detect(btc_price, threshold)

        if spike_dir is not None:
            # Apply velocity confirmation filter (218% improvement per PLAN)
            if not velocity_confirms_spike(spike_dir, velocity_bps):
                btc_idx += 1
                continue

            # Apply enhanced score filter
            score = compute_enhanced_score(spike_mag, velocity_bps, spike_dir, time_rem)
            if score < ENHANCED_SCORE_THRESHOLD:
                btc_idx += 1
                continue

            # OBI CONFIRMATION FILTER (Jan 28, 2026): +4.1pp accuracy improvement
            up_imbalance = obs_row.get('up_imbalance', None)
            down_imbalance = obs_row.get('down_imbalance', None)
            if not obi_confirms_spike(spike_dir, up_imbalance, down_imbalance):
                btc_idx += 1
                continue

            # Check high entry threshold (PRODUCTION: $0.90)
            winner_side = spike_dir
            if winner_side == "UP":
                winner_ask = obs_row['up_ask']
            else:
                winner_ask = obs_row['down_ask']

            if winner_ask >= HIGH_ENTRY_THRESHOLD:
                btc_idx += 1
                continue

            # ENTRY SIGNAL - take position
            cycle_num += 1
            loser_side = "DOWN" if winner_side == "UP" else "UP"

            # Entry at ask (taker)
            winner_entry = winner_ask

            # Calculate loser bid target using config parameters
            loser_target = calculate_loser_bid(
                winner_entry, spike_mag,
                config.drop_intercept, config.target_pair_cost
            )

            in_position = True
            position_data = {
                'winner_side': winner_side,
                'loser_side': loser_side,
                'winner_entry': winner_entry,
                'loser_target': loser_target,
                'entry_ts': btc_ts,
                'entry_time_rem': time_rem,
                'spike_magnitude': spike_mag,
            }

        btc_idx += 1

    # Handle unresolved position at market end
    if in_position and position_data is not None:
        winner_side = position_data['winner_side']
        winner_entry = position_data['winner_entry']
        spike_mag = position_data['spike_magnitude']
        entry_ts = position_data['entry_ts']

        if resolution == winner_side:
            # Winner wins - unhedged profit
            loser_fill = 0.0
            pnl_gross = (1.0 - winner_entry) * BASE_SIZE
            entry_fee = polymarket_taker_fee(winner_entry) * winner_entry * BASE_SIZE
            exit_fee = 0
            pnl_net = pnl_gross - entry_fee
        else:
            # Winner loses - unhedged loss
            loser_fill = 1.0
            pnl_gross = -winner_entry * BASE_SIZE
            entry_fee = polymarket_taker_fee(winner_entry) * winner_entry * BASE_SIZE
            exit_fee = 0
            pnl_net = pnl_gross - entry_fee

        trades.append(TradeResult(
            market_slug=slug,
            cycle_num=cycle_num,
            entry_time_remaining=position_data['entry_time_rem'],
            winner_side=winner_side,
            winner_entry=winner_entry,
            loser_fill=loser_fill,
            hedge_type="resolution",
            pair_cost=winner_entry + loser_fill,
            pnl_net=pnl_net,
            pnl_gross=pnl_gross,
            entry_fee=entry_fee,
            exit_fee=exit_fee,
            correct_direction=(resolution == winner_side),
            spike_magnitude=spike_mag,
            entry_ts=entry_ts,
            hedge_ts=market_end,
        ))

    return trades


def run_backtest(spikes_df: pd.DataFrame, obs_df: pd.DataFrame,
                 res_map: dict, config: GridConfig, hours: float) -> BacktestResult:
    """
    Run backtest for a single configuration across all markets.

    Uses pre-computed spikes with regime-based thresholds and z-score filter.
    """
    all_trades = []

    for slug in obs_df['market_slug'].unique():
        resolution = res_map.get(slug, 'UP')
        trades = simulate_market_with_spikes(spikes_df, obs_df, slug, resolution, config)
        all_trades.extend(trades)

    if not all_trades:
        return BacktestResult(
            config=config,
            total_trades=0,
            total_pnl_net=0,
            total_pnl_gross=0,
            total_entry_fees=0,
            total_exit_fees=0,
            hourly_rate_net=0,
            hourly_rate_gross=0,
            win_rate=0,
            direction_accuracy=0,
            passive_pct=0,
            time_stop_pct=0,
            resolution_pct=0,
            avg_pair_cost=0,
        )

    total_trades = len(all_trades)
    total_pnl_net = sum(t.pnl_net for t in all_trades)
    total_pnl_gross = sum(t.pnl_gross for t in all_trades)
    total_entry_fees = sum(t.entry_fee for t in all_trades)
    total_exit_fees = sum(t.exit_fee for t in all_trades)

    hourly_rate_net = total_pnl_net / hours if hours > 0 else 0
    hourly_rate_gross = total_pnl_gross / hours if hours > 0 else 0

    wins = sum(1 for t in all_trades if t.pnl_net > 0)
    win_rate = wins / total_trades

    correct = sum(1 for t in all_trades if t.correct_direction)
    direction_accuracy = correct / total_trades

    passive = sum(1 for t in all_trades if t.hedge_type == "passive")
    time_stop = sum(1 for t in all_trades if t.hedge_type == "time_stop")
    resolution = sum(1 for t in all_trades if t.hedge_type == "resolution")

    hedged = [t for t in all_trades if t.hedge_type != "resolution"]
    avg_pair_cost = np.mean([t.pair_cost for t in hedged]) if hedged else 0

    return BacktestResult(
        config=config,
        total_trades=total_trades,
        total_pnl_net=total_pnl_net,
        total_pnl_gross=total_pnl_gross,
        total_entry_fees=total_entry_fees,
        total_exit_fees=total_exit_fees,
        hourly_rate_net=hourly_rate_net,
        hourly_rate_gross=hourly_rate_gross,
        win_rate=win_rate,
        direction_accuracy=direction_accuracy,
        passive_pct=passive / total_trades,
        time_stop_pct=time_stop / total_trades,
        resolution_pct=resolution / total_trades,
        avg_pair_cost=avg_pair_cost,
        trades=all_trades,
    )


# =============================================================================
# GRID SEARCH RUNNER
# =============================================================================

def generate_configs(quick: bool = False) -> List[GridConfig]:
    """
    Generate all configuration combinations (heavily pruned).

    Grid includes 7 varied parameters:
    - Core: time_stop, lookback, drop_intercept, target_pair, cycling
    - Filters: threshold_method, velocity_mode

    Fixed at validated values (not in grid):
    - z_score_zone = (0.0, 1.5)
    - zscore_method = "ewma"
    - skip_low_regime = True

    Full: 5 × 3 × 3 × 2 × 2 × 2 × 2 = 720 configs
    """
    if quick:
        # Reduced grid for testing
        time_stops = [60, 120]
        lookbacks = [1200, 2000]
        drop_intercepts = [0.06, 0.08]
        target_pairs = [0.99]
        cycling_opts = [True]
        threshold_methods = ["ou"]  # MIRRORS LIVE
        velocity_modes = ["all"]
    else:
        # Full pruned grid
        time_stops = TIME_STOPS
        lookbacks = LOOKBACKS_MS
        drop_intercepts = DROP_INTERCEPTS
        target_pairs = TARGET_PAIRS
        cycling_opts = CYCLING_OPTIONS
        threshold_methods = THRESHOLD_METHODS
        velocity_modes = VELOCITY_MODES

    configs = []

    for ts, lb, di, tp, cyc, th_method, vel_mode in product(
        time_stops, lookbacks, drop_intercepts, target_pairs,
        cycling_opts, threshold_methods, velocity_modes
    ):
        configs.append(GridConfig(
            time_stop_sec=ts,
            lookback_ms=lb,
            drop_intercept=di,
            target_pair_cost=tp,
            cycling=cyc,
            threshold_method=th_method,
            velocity_mode=vel_mode,
            # Fixed at validated values
            z_lo=FIXED_Z_LO,
            z_hi=FIXED_Z_HI,
            zscore_method=FIXED_ZSCORE_METHOD,
            skip_low_regime=FIXED_SKIP_LOW_REGIME,
        ))

    print(f"Generated {len(configs)} configs")
    return configs


def run_grid_search(btc_df: pd.DataFrame, obs_df: pd.DataFrame,
                    res_map: dict, hours: float, quick: bool = False) -> List[BacktestResult]:
    """
    Run grid search across all configurations.

    Follows SCRIPT_RULES.md:
    - Progress bar (tqdm)
    - ETA estimate
    - Checkpoint saves every 10 configs
    - Print "Running X of Y"

    Pre-computes:
    1. Volatility data (regime, z-score) - ONCE for all configs
    2. Spikes for each (lookback, threshold_method) combo - ONCE per combo
    """
    configs = generate_configs(quick=quick)

    # Get unique combinations for pre-computation
    lookbacks_used = list(set(c.lookback_ms for c in configs))
    threshold_methods_used = list(set(c.threshold_method for c in configs))

    print(f"\nTotal configurations: {len(configs)}")
    print(f"PRODUCTION CONFIG: {BASE_SIZE} shares, skip >= ${HIGH_ENTRY_THRESHOLD:.2f}")
    print(f"\nPRE-COMPUTATION:")
    print(f"  Lookbacks:          {sorted(lookbacks_used)}")
    print(f"  Threshold methods:  {threshold_methods_used}")
    print(f"  Fixed z-zone:       ({FIXED_Z_LO}, {FIXED_Z_HI})")
    print(f"  Fixed zscore:       {FIXED_ZSCORE_METHOD}")
    print()

    # Step 1: Pre-compute volatility data (ONCE)
    btc_vol_df = precompute_volatility_data(btc_df)

    # Step 2: Pre-compute spikes for each (lookback, threshold_method) combo
    spikes_cache = precompute_spikes_all_lookbacks(btc_vol_df, lookbacks_used, threshold_methods_used)

    # Step 3: Run grid search
    print(f"\nRunning {len(configs)} configurations (7D pruned grid)...")
    results = []
    start_time = time.time()
    checkpoint_path = Path("research/volume_grid_checkpoint.csv")

    for i, config in tqdm(enumerate(configs), total=len(configs), desc="Grid Search (720 configs)"):
        # Use pre-computed spikes for this (lookback, threshold_method) combo
        cache_key = (config.lookback_ms, config.threshold_method)
        spikes_df = spikes_cache[cache_key]
        result = run_backtest(spikes_df, obs_df, res_map, config, hours)
        results.append(result)

        # Checkpoint every 10 configs
        if (i + 1) % 10 == 0:
            checkpoint_df = pd.DataFrame([r.to_dict() for r in results])
            checkpoint_df.to_csv(checkpoint_path, index=False)

    elapsed = time.time() - start_time
    print(f"\nCompleted in {elapsed / 60:.1f} minutes ({elapsed / len(configs):.2f}s per config)")

    return results


# =============================================================================
# OUTPUT
# =============================================================================

def print_results(results: List[BacktestResult], hours: float):
    """Print comprehensive results summary."""
    print()
    print("=" * 100)
    print("VOLUME STRATEGY GRID SEARCH RESULTS (720 configs)")
    print("=" * 100)
    print(f"\nData: {hours:.2f} hours")
    print(f"Production Config: {BASE_SIZE} shares, skip >= ${HIGH_ENTRY_THRESHOLD:.2f}")
    print(f"Configurations tested: {len(results)} (expected: 720)")

    # Sort by hourly rate (net)
    results.sort(key=lambda x: x.hourly_rate_net, reverse=True)

    # Top 20 configurations
    print()
    print("=" * 100)
    print("TOP 20 CONFIGURATIONS (by $/hr net)")
    print("=" * 100)
    print()
    print(f"{'#':<3} {'Config':<30} {'Trades':>7} {'$/hr':>8} {'Gross':>8} {'Fees':>7} "
          f"{'Win%':>6} {'Pass%':>6} {'TS%':>5}")
    print("-" * 100)

    for i, r in enumerate(results[:20], 1):
        total_fees = r.total_entry_fees + r.total_exit_fees
        print(f"{i:<3} {str(r.config):<30} {r.total_trades:>7} ${r.hourly_rate_net:>7.2f} "
              f"${r.hourly_rate_gross:>7.2f} ${total_fees:>6.2f} "
              f"{r.win_rate*100:>5.1f}% {r.passive_pct*100:>5.1f}% {r.time_stop_pct*100:>4.1f}%")

    # Parameter sensitivity
    print()
    print("=" * 100)
    print("PARAMETER SENSITIVITY ANALYSIS")
    print("=" * 100)

    # Time-stop
    print("\nTime-Stop (CORE - volume vs quality):")
    for ts in TIME_STOPS:
        matching = [r for r in results if r.config.time_stop_sec == ts]
        if matching:
            avg_rate = np.mean([r.hourly_rate_net for r in matching])
            avg_trades = np.mean([r.total_trades for r in matching])
            avg_passive = np.mean([r.passive_pct for r in matching])
            print(f"  {ts:>3}s: ${avg_rate:.2f}/hr, {avg_trades:.0f} trades, {avg_passive*100:.1f}% passive")

    # Lookback
    print("\nLookback (signal detection window):")
    for lb in LOOKBACKS_MS:
        matching = [r for r in results if r.config.lookback_ms == lb]
        if matching:
            avg_rate = np.mean([r.hourly_rate_net for r in matching])
            avg_trades = np.mean([r.total_trades for r in matching])
            print(f"  {lb:>4}ms: ${avg_rate:.2f}/hr, {avg_trades:.0f} trades")

    # DROP_INTERCEPT
    print("\nDROP_INTERCEPT (LOWER = FASTER hedge fills):")
    for di in DROP_INTERCEPTS:
        matching = [r for r in results if r.config.drop_intercept == di]
        if matching:
            avg_rate = np.mean([r.hourly_rate_net for r in matching])
            avg_passive = np.mean([r.passive_pct for r in matching])
            print(f"  {di:.2f}: ${avg_rate:.2f}/hr, {avg_passive*100:.1f}% passive")

    # TARGET_PAIR_COST
    print("\nTARGET_PAIR_COST (LOWER = earlier profit taking):")
    for tp in TARGET_PAIRS:
        matching = [r for r in results if r.config.target_pair_cost == tp]
        if matching:
            avg_rate = np.mean([r.hourly_rate_net for r in matching])
            avg_pair = np.mean([r.avg_pair_cost for r in matching])
            print(f"  {tp:.2f}: ${avg_rate:.2f}/hr, avg pair ${avg_pair:.3f}")

    # Cycling
    print("\nCycling (re-enter after hedge):")
    for cyc in CYCLING_OPTIONS:
        matching = [r for r in results if r.config.cycling == cyc]
        if matching:
            avg_rate = np.mean([r.hourly_rate_net for r in matching])
            avg_trades = np.mean([r.total_trades for r in matching])
            print(f"  {str(cyc):>5}: ${avg_rate:.2f}/hr, {avg_trades:.0f} trades")

    # Threshold Method
    print("\nThreshold Method (spike detection):")
    for th in THRESHOLD_METHODS:
        matching = [r for r in results if r.config.threshold_method == th]
        if matching:
            avg_rate = np.mean([r.hourly_rate_net for r in matching])
            avg_trades = np.mean([r.total_trades for r in matching])
            print(f"  {th:>8}: ${avg_rate:.2f}/hr, {avg_trades:.0f} trades")

    # Velocity Mode
    print("\nVelocity Mode (confirmation filter):")
    for vel in VELOCITY_MODES:
        matching = [r for r in results if r.config.velocity_mode == vel]
        if matching:
            avg_rate = np.mean([r.hourly_rate_net for r in matching])
            avg_dir_acc = np.mean([r.direction_accuracy for r in matching])
            print(f"  {vel:>6}: ${avg_rate:.2f}/hr, {avg_dir_acc*100:.1f}% dir accuracy")

    # Best config details
    print()
    print("=" * 100)
    print("BEST CONFIGURATION DETAILS")
    print("=" * 100)

    best = results[0]
    total_fees = best.total_entry_fees + best.total_exit_fees

    print(f"\nConfiguration: {best.config}")
    print(f"\nCore Parameters (varied):")
    print(f"  Time-Stop:      {best.config.time_stop_sec}s")
    print(f"  Lookback:       {best.config.lookback_ms}ms ({best.config.lookback_ticks} ticks)")
    print(f"  DROP_INTERCEPT: {best.config.drop_intercept:.2f}")
    print(f"  TARGET_PAIR:    {best.config.target_pair_cost:.2f}")
    print(f"  Cycling:        {best.config.cycling}")

    print(f"\nFilter Parameters (varied):")
    print(f"  Threshold:      {best.config.threshold_method}")
    print(f"  Velocity Mode:  {best.config.velocity_mode}")

    print(f"\nFixed Parameters (validated):")
    print(f"  Z-Score Zone:   {best.config.z_zone_label}")
    print(f"  Z-Score Method: {best.config.zscore_method}")
    print(f"  Skip LOW:       {best.config.skip_low_regime}")

    print(f"\nPerformance:")
    print(f"  Total PnL (net):   ${best.total_pnl_net:.2f}")
    print(f"  Total PnL (gross): ${best.total_pnl_gross:.2f}")
    print(f"  Total Fees:        ${total_fees:.2f}")
    print(f"  Hourly Rate (net): ${best.hourly_rate_net:.2f}/hr")
    print(f"  Total Trades:      {best.total_trades}")
    print(f"  Win Rate:          {best.win_rate*100:.1f}%")
    print(f"  Direction Acc:     {best.direction_accuracy*100:.1f}%")

    print(f"\nHedge Breakdown:")
    print(f"  Passive:     {best.passive_pct*100:.1f}%")
    print(f"  Time-Stop:   {best.time_stop_pct*100:.1f}%")
    print(f"  Resolution:  {best.resolution_pct*100:.1f}%")


def save_results(results: List[BacktestResult], output_path: str):
    """Save results to CSV."""
    rows = [r.to_dict() for r in results]
    df = pd.DataFrame(rows)
    df = df.sort_values('pnl_hr_net', ascending=False)
    df.to_csv(output_path, index=False)
    print(f"\nResults saved to: {output_path}")


# =============================================================================
# MAIN
# =============================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="Volume Strategy Grid Search",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("--quick", action="store_true",
                        help="Run quick test with reduced grid (8 configs)")
    parser.add_argument("--dataset", type=str, default="is_oos2",
                        choices=["is_oos2", "oos34", "oos5", "all"],
                        help="Dataset: is_oos2 (Jan 16-19, ~82h), oos34 (Jan 22-24, ~47h), oos5 (Jan 26), all")
    parser.add_argument("--output", type=str,
                        default="research/volume_grid_results.csv",
                        help="Output CSV file path")
    return parser.parse_args()


def main():
    args = parse_args()

    print("=" * 100)
    print("VOLUME STRATEGY GRID SEARCH (HEAVILY PRUNED)")
    print("=" * 100)
    print()
    print("VARIED PARAMETERS (7 dimensions):")
    print(f"  Time-stops:      {TIME_STOPS} ({len(TIME_STOPS)})")
    print(f"  Lookbacks (ms):  {LOOKBACKS_MS} ({len(LOOKBACKS_MS)})")
    print(f"  DROP_INTERCEPT:  {DROP_INTERCEPTS} ({len(DROP_INTERCEPTS)})")
    print(f"  TARGET_PAIR:     {TARGET_PAIRS} ({len(TARGET_PAIRS)})")
    print(f"  Cycling:         {CYCLING_OPTIONS} ({len(CYCLING_OPTIONS)})")
    print(f"  Threshold:       {THRESHOLD_METHODS} ({len(THRESHOLD_METHODS)})")
    print(f"  Velocity Mode:   {VELOCITY_MODES} ({len(VELOCITY_MODES)})")
    print()
    print("FIXED AT VALIDATED VALUES:")
    print(f"  Z-Score Zone:    ({FIXED_Z_LO}, {FIXED_Z_HI})")
    print(f"  Z-Score Method:  {FIXED_ZSCORE_METHOD}")
    print(f"  Skip LOW Regime: {FIXED_SKIP_LOW_REGIME}")
    print()
    total_configs = (len(TIME_STOPS) * len(LOOKBACKS_MS) * len(DROP_INTERCEPTS) *
                     len(TARGET_PAIRS) * len(CYCLING_OPTIONS) *
                     len(THRESHOLD_METHODS) * len(VELOCITY_MODES))
    print(f"TOTAL CONFIGURATIONS: {total_configs}")
    print()
    print("PRODUCTION CONFIG:")
    print(f"  BASE_SIZE = {BASE_SIZE} shares")
    print(f"  HIGH_ENTRY_THRESHOLD = ${HIGH_ENTRY_THRESHOLD:.2f}")
    print()

    # Load data
    btc_df, obs_df, hours, res_map = load_data(dataset=args.dataset)

    # Run grid search
    results = run_grid_search(btc_df, obs_df, res_map, hours, quick=args.quick)

    # Print results
    print_results(results, hours)

    # Save results
    save_results(results, args.output)

    print()
    print("=" * 100)


if __name__ == "__main__":
    main()
