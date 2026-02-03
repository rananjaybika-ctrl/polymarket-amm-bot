#!/usr/bin/env python3
"""
=============================================================================
BREAKEVEN EXIT SWEEP - Test optimal hold time before breakeven check
=============================================================================

Research question (Feb 3, 2026):
Breakeven exit catches when winner_bid <= entry_price, triggering immediate
market hedge at ~$1.00 pair cost instead of waiting for time-stop at $1.04.

But how long should we wait before checking breakeven?
- Too short (0s): Exit immediately due to bid/ask spread = worse than time-stop
- Too long (30s): Same as time-stop, no benefit
- Sweet spot: Allow passive fill opportunity, catch breakeven before $1.04

Grid dimensions:
1. Breakeven min hold: [None, 0, 1000, 2000, 5000, 10000, 15000, 20000, 30000] ms

Fixed params (winner config):
- Spike method: EWMA_1000 (validated winner)
- Time-stop: 30s
- Threshold: OU adaptive
- Hedge formula: OLD (0.50/0.08)
- OBI filter: ON

Datasets: 60Hz only (OOS5 excluded - only 1.3Hz)
~120h of data across OOS7, OOS8, OOS9.1, IS+OOS2, OOS3+4

Usage:
    python research/optimizers/test_breakeven_sweep.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict
from tqdm import tqdm
import math
import sys
import time
import gc

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# =============================================================================
# SHARED LOGIC - FROM src/core (Single Source of Truth for HOW to calculate)
# =============================================================================
from src.core import (
    polymarket_taker_fee,
    calculate_pnl_with_fees,
    velocity_confirms_spike,
    obi_confirms_spike,
    should_take_spike_enhanced,
    compute_enhanced_score,
    calculate_loser_bid as calculate_loser_bid_core,
    BacktestCycle,
    # Multi-cycle direction modes
    DIRECTION_MODE_SINGLE,
    DIRECTION_MODE_BUILD,
    DIRECTION_MODE_CLEAR,
    can_enter_direction,
)

# =============================================================================
# GRID SEARCH PARAMETERS - Define here, NOT from TRADING_CONFIGS
# =============================================================================
# Grid search tests MANY values to find the winner.
# Winner values then get saved to TRADING_CONFIGS.py for live trading.
#
# FIXED PARAMS (not part of grid - same for all tests):
TARGET_SHARES = 50
MIN_RUNTIME_SECS = 300      # 5 min market duration filter
LOW_ENTRY_THRESHOLD = None  # REMOVED per user request - matches main backtest
HIGH_ENTRY_THRESHOLD = 0.90 # Skip entries >= $0.90 (matches TRADING_CONFIGS.py)
SPIKE_LOOKBACK = 72         # 72 ticks (1200ms at 60Hz) - used by FIXED method

# =============================================================================
# SPIKE METHOD - EWMA_1000 (validated winner)
# =============================================================================
# Using EWMA_1000 for spike detection - already validated at +$13.80/hr
SPIKE_METHODS = ["EWMA_1000"]  # Fixed - only testing threshold method

# =============================================================================
# THRESHOLD METHOD - OU (baseline) vs Pure EWMA (test)
# =============================================================================

# OU params (baseline - current production)
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10

# =============================================================================
# BREAKEVEN HOLD TIMES TO TEST (key test dimension)
# =============================================================================
# None = disabled (time-stop only baseline)
# 0 = instant check (likely bad due to spread)
# Higher values = more time for passive fill before checking breakeven
BREAKEVEN_HOLD_MS_OPTIONS = [None, 0, 1000, 2000, 5000, 10000, 15000, 20000, 30000]

# Threshold method - FIXED at OU (winner config)
THRESHOLD_METHOD = "OU"  # Not testing this - use winner

# Signal filtering thresholds
VELOCITY_CONFIRM_THRESHOLD = 0.10
ENHANCED_SCORE_THRESHOLD = 0.40

# Loser bid defaults (CURRENT baseline - also tested in grid)
DEFAULT_DROP_MULTIPLIER = 0.50
DEFAULT_DROP_INTERCEPT = 0.08
TARGET_PAIR_COST = 0.99

# Cycling - FASTER for short-term
MIN_CYCLE_GAP_MS = 50  # Was 200 - faster cycling for volume play


# FEE MODEL: imported from src.core (polymarket_taker_fee, calculate_pnl_with_fees)

# =============================================================================
# CONFIG CLASS - Combines time-stop, loser offset, AND multi-cycle
# =============================================================================

@dataclass
class TestConfig:
    """Configuration for breakeven hold time test."""
    name: str
    time_stop_seconds: float
    drop_multiplier: float
    drop_intercept: float
    offset_name: str  # OLD (winner)
    spike_method: str = "EWMA_1000"  # Fixed - winner config
    # Threshold method - FIXED at OU (winner)
    threshold_method: str = "OU"
    # EWMA threshold params (only used if threshold_method="EWMA", kept for compatibility)
    ewma_vol_long_ms: int = 5000
    ewma_threshold_scale: float = 0.5
    # BREAKEVEN EXIT (key test dimension)
    breakeven_min_hold_ms: Optional[int] = None  # None = disabled, ms = min hold before check
    # Multi-cycle parameters
    max_cycles: int = 1      # 1 = single-cycle
    shares_per_cycle: int = 50  # Shares per cycle
    cycle_mode: str = "SINGLE"
    direction_mode: str = DIRECTION_MODE_SINGLE
    # Loss mechanism parameters
    stop_loss_pct: Optional[float] = None  # None = disabled
    max_market_losses: Optional[int] = None  # None = disabled

    @property
    def total_shares(self) -> int:
        """Total shares across all cycles."""
        return self.max_cycles * self.shares_per_cycle

    @property
    def min_time(self) -> float:
        """Minimum time remaining for entry = time_stop + 60s buffer."""
        return self.time_stop_seconds + 60.0

    def calculate_loser_bid(self, winner_entry: float, spike_magnitude: float) -> float:
        """Calculate loser bid with this config's offset parameters."""
        expected_drop = self.drop_multiplier * spike_magnitude + self.drop_intercept
        max_loser = TARGET_PAIR_COST - winner_entry
        loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
        return max(0.01, min(0.95, loser_bid))


# =============================================================================
# GRID PARAMETERS: OFFSET × TIME-STOP × CYCLE-MODE
# =============================================================================

# =============================================================================
# FIXED PARAMS - Using validated winner (not testing these)
# =============================================================================
# OLD hedge formula and TS30 validated at +$13.80/hr - only testing threshold
HEDGE_FORMULAS = {
    "OLD": (0.50, 0.08, "Deep hedge ~0.91 pair cost (winner)"),
}

# Time-stop: 30s only (winner config)
TIME_STOPS = [30.0]

# Stop-loss: Disabled for time-stop comparison
STOP_LOSS_PCTS = [None]

# Market loss limits: DISABLED for initial test
MAX_MARKET_LOSSES = [None]

# Cycle modes: (max_cycles, shares_per_cycle, direction_mode, description)
# SINGLE-CYCLE ONLY - Multi-cycle DEPRECATED (Jan 31, 2026)
#
# MULTI-CYCLE ABANDONED: Destroyed profitability even with direction fix.
#   - SINGLE: 54.3% win rate, +$1.37/hr (LIVE-READY)
#   - MULTI: 39.8% win rate, -$26.70/hr (10x trades, 15pp lower win rate)
# Root cause: Stacking same-direction trades catches weak follow-on spikes.
CYCLE_MODES = {
    "SINGLE": (1, 50, DIRECTION_MODE_SINGLE, "1 cycle × 50 shares (PRODUCTION)"),
    # DEPRECATED - kept for reference only:
    # "MULTI_BUILD": (2, 25, DIRECTION_MODE_BUILD, "DEPRECATED - destroyed profitability"),
    # "MULTI_CLEAR": (2, 25, DIRECTION_MODE_CLEAR, "DEPRECATED - destroyed profitability"),
}

# =============================================================================
# Generate configs: BREAKEVEN HOLD TIME SWEEP
# =============================================================================
# Test different breakeven hold times with winner config
CONFIGS = []

# Fixed params (winner config - EWMA_1000, TS30, OU threshold)
ts = TIME_STOPS[0]  # 30s
mult, intercept, _ = HEDGE_FORMULAS["OLD"]
max_cycles, shares_per, dir_mode, _ = CYCLE_MODES["SINGLE"]

# Generate config for each breakeven hold time
for be_hold_ms in BREAKEVEN_HOLD_MS_OPTIONS:
    if be_hold_ms is None:
        name = "BE_DISABLED"
    else:
        name = f"BE_{be_hold_ms}ms"

    CONFIGS.append(TestConfig(
        name=name,
        time_stop_seconds=ts,
        drop_multiplier=mult,
        drop_intercept=intercept,
        offset_name="OLD",
        spike_method="EWMA_1000",
        threshold_method="OU",
        breakeven_min_hold_ms=be_hold_ms,
        max_cycles=max_cycles,
        shares_per_cycle=shares_per,
        cycle_mode="SINGLE",
        direction_mode=dir_mode,
    ))

print(f"Generated {len(CONFIGS)} configs: breakeven hold times {BREAKEVEN_HOLD_MS_OPTIONS}")


# =============================================================================
# OU PARAMETERS (for adaptive threshold)
# =============================================================================

_ou_params = None


def load_ou_params():
    """Load OU parameters for adaptive threshold."""
    global _ou_params
    try:
        from src.strategies.ou_volatility import OUParameters
        _ou_params = OUParameters.load("research/ou_params.json")
        print(f"[OU] Loaded: mu={_ou_params.mu:.4f}, sigma_stat={_ou_params.sigma_stat:.4f}")
    except Exception as e:
        print(f"[OU] Warning: {e} - using fixed threshold 0.02")
        _ou_params = None


def compute_ou_threshold(volatility: float) -> float:
    """Compute OU adaptive threshold from current volatility."""
    global _ou_params
    if _ou_params is None:
        return OU_BASE_THRESHOLD
    vol = max(volatility, 1e-6)
    log_vol = math.log(vol)
    z_score = (log_vol - _ou_params.mu) / _ou_params.sigma_stat
    z_clamped = max(-10, min(10, z_score * OU_SIGMOID_STEEPNESS))
    sigmoid = 1.0 / (1.0 + math.exp(-z_clamped))
    multiplier = OU_K_LOW + (OU_K_HIGH - OU_K_LOW) * sigmoid
    threshold = OU_BASE_THRESHOLD * multiplier
    return max(OU_MIN_THRESHOLD, min(OU_MAX_THRESHOLD, threshold))


def compute_ou_zscore(volatility: float) -> float:
    """Compute OU z-score for volatility."""
    global _ou_params
    if _ou_params is None:
        return 0.0
    vol = max(volatility, 1e-6)
    log_vol = math.log(vol)
    z_score = (log_vol - _ou_params.mu) / _ou_params.sigma_stat
    return z_score


# =============================================================================
# PURE EWMA THRESHOLD (test) - No OU params, everything EWMA
# =============================================================================

def compute_ewma_thresholds(prices: np.ndarray, vol_short_ms: int, vol_long_ms: int,
                            scale: float, base_threshold: float = 0.02) -> np.ndarray:
    """
    Compute pure EWMA-based adaptive thresholds.

    Instead of OU params from calibration, uses ratio of short-term to long-term
    volatility EWMA. When vol_short > vol_long, threshold increases (high vol regime).

    threshold = base * (1 + scale * (vol_short/vol_long - 1))

    Clamped to [0.015, 0.10] to match OU range.
    """
    # Calculate returns
    returns = np.diff(prices) / prices[:-1] * 100  # pct change
    returns = np.concatenate([[0], returns])  # pad first value
    abs_returns = np.abs(returns)

    # EWMA half-lives in ticks (~60Hz)
    halflife_short = vol_short_ms / 16.67
    halflife_long = vol_long_ms / 16.67

    alpha_short = 1 - 0.5 ** (1.0 / halflife_short)
    alpha_long = 1 - 0.5 ** (1.0 / halflife_long)

    # Initialize EWMAs
    n = len(abs_returns)
    vol_short = np.zeros(n)
    vol_long = np.zeros(n)
    thresholds = np.zeros(n)

    # Bootstrap with first 60 values
    init_vol = np.mean(abs_returns[:60]) if len(abs_returns) > 60 else 0.1
    vol_short[0] = init_vol
    vol_long[0] = init_vol

    for i in range(1, n):
        vol_short[i] = alpha_short * abs_returns[i] + (1 - alpha_short) * vol_short[i-1]
        vol_long[i] = alpha_long * abs_returns[i] + (1 - alpha_long) * vol_long[i-1]

        # Ratio-based threshold scaling
        vol_ratio = vol_short[i] / max(vol_long[i], 1e-6)
        multiplier = 1 + scale * (vol_ratio - 1)
        multiplier = max(0.5, min(2.5, multiplier))  # Clamp multiplier

        threshold = base_threshold * multiplier
        thresholds[i] = max(OU_MIN_THRESHOLD, min(OU_MAX_THRESHOLD, threshold))

    thresholds[0] = base_threshold
    return thresholds


# =============================================================================
# DATASET CONFIGURATION
# =============================================================================

DATASETS = {
    # ==========================================================================
    # ALL DATASETS - For comprehensive grid search (Jan 31, 2026)
    # ==========================================================================
    "IS+OOS2": {
        "name": "IS+OOS2 (Jan 16-19)",
        "btc_file": "research/binance_hf/btc_prices_20260118_060340.csv",
        "obs_files": [
            "research/observer/grid_obs_20260116.csv",
            "research/observer/grid_obs_20260117.csv",
            "research/observer/grid_obs_20260118.csv",
            "research/observer/grid_obs_20260119.csv",
        ],
        "use_obi": True,  # Auto: uses OBI if columns exist, skips if not
        "expected_hours": 23.0,  # Reduced from 69h - partial data available
    },
    "OOS3+4": {
        "name": "OOS3+4 (Jan 22-24)",
        "btc_file": "research/observer/PROTECTED_btc_prices_oos3_oos4_combined.csv",
        "obs_files": [
            "research/observer/PROTECTED_grid_obs_oos3_oos4_combined.csv",
        ],
        "use_obi": True,  # Auto: uses OBI if columns exist, skips if not
        "expected_hours": 47.0,
    },
    "OOS5": {
        "name": "OOS5 (Jan 26)",
        "btc_file": None,  # Will use observer binance_price at 5Hz
        "obs_files": [
            "research/observer/PROTECTED_grid_obs_oos5_recovered.csv",
        ],
        "use_obi": True,  # Auto: uses OBI if columns exist, skips if not
        "expected_hours": 41.0,
    },
    "OOS7": {
        "name": "OOS7 (Jan 29-30)",
        "btc_file": "research/binance_hf/btc_prices_20260129_160523.csv",
        "obs_files": [
            "research/observer/grid_obs_20260129.csv",
            "research/observer/grid_obs_20260130.csv",
        ],
        "use_obi": True,  # OBI ON for OOS7
        "expected_hours": 19.0,
    },
    "OOS8": {
        "name": "OOS8 (Jan 31)",
        "btc_file": "research/binance_hf/btc_prices_20260131_055231.csv",
        "obs_files": [
            "research/observer/grid_obs_20260131.csv",
        ],
        "use_obi": True,  # OBI ON for OOS8
        "expected_hours": 24.0,  # Estimate
    },
    "OOS9.1": {
        "name": "OOS9.1 (Feb 1, 7.7h overlap - trending market)",
        "btc_file": "research/binance_hf/btc_prices_oos9_1.csv",
        "obs_files": [
            "research/observer/grid_obs_oos9_1.csv",
        ],
        "use_obi": True,  # OBI ON
        "expected_hours": 7.7,
    },
}

# =============================================================================
# DATASETS TO RUN - 60Hz ONLY
# =============================================================================
# EWMA threshold relies on high-frequency price updates
# OOS5 excluded: only 1.3Hz (observer binance_price, not 60Hz HF stream)
#
# 60Hz datasets:
#   - OOS7: 185.9 Hz, OOS8: 197.3 Hz, OOS9.1: 229.5 Hz
#   - IS+OOS2: 87.1 Hz, OOS3+4: 84.9 Hz
# Low-frequency (excluded):
#   - OOS5: 1.3 Hz ❌

DATASETS_TO_RUN = ["OOS7", "OOS8", "OOS9.1", "IS+OOS2", "OOS3+4"]  # 60Hz only, OOS5 excluded

MIN_COVERAGE_PCT = 75.0  # Lowered for OOS8 (76% coverage)


def validate_dataset_coverage() -> bool:
    """Validate that all datasets have sufficient data coverage."""
    print("=" * 80)
    print("DATASET COVERAGE VALIDATION")
    print("=" * 80)
    print(f"Minimum required coverage: {MIN_COVERAGE_PCT}%\n")

    base_dir = Path("/Users/rananjaybika/polymarket-amm-bot")
    all_passed = True

    for ds_key, ds_config in DATASETS.items():
        expected_hours = ds_config.get('expected_hours', 0)

        obs_dfs = []
        for fname in ds_config['obs_files']:
            fpath = base_dir / fname
            if fpath.exists():
                df = pd.read_csv(fpath, usecols=['timestamp_ms'], nrows=None)
                obs_dfs.append(df)

        if not obs_dfs:
            print(f"  {ds_key}: FAIL - No observer files found!")
            all_passed = False
            continue

        obs_combined = pd.concat(obs_dfs, ignore_index=True)
        obs_start = obs_combined['timestamp_ms'].min()
        obs_end = obs_combined['timestamp_ms'].max()
        obs_hours = (obs_end - obs_start) / 3600000

        if ds_config['btc_file']:
            btc_path = base_dir / ds_config['btc_file']
            if btc_path.exists():
                btc_df = pd.read_csv(btc_path, usecols=['timestamp_ms'])
                btc_start = btc_df['timestamp_ms'].min()
                btc_end = btc_df['timestamp_ms'].max()
                overlap_start = max(obs_start, btc_start)
                overlap_end = min(obs_end, btc_end)
                overlap_hours = max(0, (overlap_end - overlap_start) / 3600000)
            else:
                overlap_hours = 0
        else:
            overlap_hours = obs_hours

        coverage_pct = (overlap_hours / expected_hours * 100) if expected_hours > 0 else 100
        passed = coverage_pct >= MIN_COVERAGE_PCT
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False

        print(f"  {ds_key}: Expected: {expected_hours:.1f}h | Actual: {overlap_hours:.1f}h | {coverage_pct:.0f}% {status}")

    print("=" * 80)
    return all_passed


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class TradeResult:
    market_slug: str
    cycle_num: int
    entry_time_remaining: float
    signal_score: float
    winner_side: str
    winner_fill_price: float
    loser_fill_price: float
    hedge_type: str  # "passive", "breakeven", "time_stop", "stop_loss", "resolution"
    pair_cost: float
    pnl_gross: float
    pnl_net: float
    entry_fee: float
    exit_fee: float
    correct_direction: bool
    spike_magnitude: float
    dataset: str
    offset_name: str  # Track which offset was used
    cycle_mode: str = "SINGLE"  # SINGLE or MULTI
    shares: int = 50  # Shares for this trade
    # Loss mechanism tracking (Feb 1, 2026)
    stop_loss_pct: Optional[float] = None  # Config stop-loss % used
    max_market_losses: Optional[int] = None  # Config market loss limit used
    skipped_by_mml: bool = False  # True if this trade was skipped due to market loss limit


@dataclass
class BacktestCycle:
    """Track a single cycle in multi-cycle mode."""
    cycle_id: int
    entry_ts: int
    winner_side: str
    loser_side: str
    winner_entry: float
    loser_target: float
    entry_time_rem: float
    spike_magnitude: float
    score: float
    shares: int


# =============================================================================
# SPIKE DETECTION - VECTORIZED PRECOMPUTATION
# =============================================================================

def precompute_spikes_fixed(btc_df: pd.DataFrame, lookback: int = SPIKE_LOOKBACK) -> pd.DataFrame:
    """FIXED: Compare current price to price N ticks ago (sliding window)."""
    print(f"    [FIXED] Using {lookback}-tick lookback")
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    df['price_prev'] = df['price'].shift(lookback)
    df['change_pct'] = (df['price'] - df['price_prev']) / df['price_prev'] * 100
    df['spike_magnitude'] = df['change_pct'].abs()

    returns = df['price'].pct_change() * 100
    ewma_halflife = 300
    alpha = 1 - 0.5 ** (1.0 / ewma_halflife)

    variance = returns.iloc[:60].var() if len(returns) > 60 else 0.01
    thresholds = []
    zscores = []

    for i, r in enumerate(returns):
        if pd.isna(r):
            thresholds.append(OU_BASE_THRESHOLD)
            zscores.append(0.0)
            continue
        variance = alpha * (r ** 2) + (1 - alpha) * variance
        vol = max(np.sqrt(variance), 1e-6)
        threshold = compute_ou_threshold(vol)
        z_score = compute_ou_zscore(vol)
        thresholds.append(threshold)
        zscores.append(z_score)

    df['threshold'] = thresholds
    df['z_score'] = zscores
    df['spike_detected'] = df['spike_magnitude'] >= df['threshold']
    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['change_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['change_pct'] < 0), 'spike_direction'] = 'DOWN'

    spike_count = df['spike_detected'].sum()
    print(f"    [FIXED] Found {spike_count:,} spikes")

    return df


def precompute_spikes_ewma(btc_df: pd.DataFrame, halflife_ms: int,
                           threshold_method: str = "OU",
                           ewma_vol_long_ms: int = 5000,
                           ewma_threshold_scale: float = 0.5) -> pd.DataFrame:
    """EWMA: Compare current price to exponentially weighted moving average.

    Key advantage: After a spike, the EWMA adapts, reducing redundant signals
    from the same price move. One price move → one spike (not 14 spikes).

    Args:
        btc_df: BTC price DataFrame
        halflife_ms: Half-life for spike EWMA (1000ms for EWMA_1000)
        threshold_method: "OU" (baseline) or "EWMA" (pure EWMA test)
        ewma_vol_long_ms: Long-term vol EWMA half-life (if threshold_method="EWMA")
        ewma_threshold_scale: Threshold scaling factor (if threshold_method="EWMA")
    """
    halflife_ticks = halflife_ms / 16.67  # ~60Hz data
    alpha = 1 - 0.5 ** (1.0 / halflife_ticks)

    method_str = f"[EWMA_{halflife_ms}+{threshold_method}]"
    if threshold_method == "EWMA":
        method_str = f"[EWMA_{halflife_ms}+EWMA_L{ewma_vol_long_ms//1000}k_S{int(ewma_threshold_scale*10)}]"
    print(f"    {method_str} Half-life={halflife_ms}ms, α={alpha:.4f}")

    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    # Compute EWMA of price (for spike magnitude)
    prices = df['price'].values
    ewma_prices = np.zeros(len(prices))
    ewma_prices[0] = prices[0]

    for i in range(1, len(prices)):
        ewma_prices[i] = alpha * prices[i] + (1 - alpha) * ewma_prices[i-1]

    df['ewma_price'] = ewma_prices
    df['change_pct'] = (df['price'] - df['ewma_price']) / df['ewma_price'] * 100
    df['spike_magnitude'] = df['change_pct'].abs()

    # Compute thresholds based on method
    if threshold_method == "EWMA":
        # Pure EWMA threshold (test)
        thresholds = compute_ewma_thresholds(
            prices, EWMA_VOL_SHORT_MS, ewma_vol_long_ms, ewma_threshold_scale
        )
        df['threshold'] = thresholds
        df['z_score'] = 0.0  # Not used for EWMA threshold
    else:
        # OU adaptive threshold (baseline)
        returns = df['price'].pct_change() * 100
        vol_halflife = 300
        vol_alpha = 1 - 0.5 ** (1.0 / vol_halflife)

        variance = returns.iloc[:60].var() if len(returns) > 60 else 0.01
        thresholds = []
        zscores = []

        for i, r in enumerate(returns):
            if pd.isna(r):
                thresholds.append(OU_BASE_THRESHOLD)
                zscores.append(0.0)
                continue
            variance = vol_alpha * (r ** 2) + (1 - vol_alpha) * variance
            vol = max(np.sqrt(variance), 1e-6)
            threshold = compute_ou_threshold(vol)
            z_score = compute_ou_zscore(vol)
            thresholds.append(threshold)
            zscores.append(z_score)

        df['threshold'] = thresholds
        df['z_score'] = zscores

    df['spike_detected'] = df['spike_magnitude'] >= df['threshold']
    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['change_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['change_pct'] < 0), 'spike_direction'] = 'DOWN'

    spike_count = df['spike_detected'].sum()
    print(f"    {method_str} Found {spike_count:,} spikes")

    return df


def precompute_spikes(btc_df: pd.DataFrame, method: str, config: Optional[TestConfig] = None) -> pd.DataFrame:
    """Dispatch to appropriate spike detection method.

    Args:
        btc_df: BTC price DataFrame
        method: Spike method ("FIXED", "EWMA_500", "EWMA_1000", etc.)
        config: Optional TestConfig with threshold_method params
    """
    if method == "FIXED":
        return precompute_spikes_fixed(btc_df)
    elif method.startswith("EWMA_"):
        halflife_ms = int(method.split("_")[1])
        if config is not None:
            return precompute_spikes_ewma(
                btc_df, halflife_ms,
                threshold_method=config.threshold_method,
                ewma_vol_long_ms=config.ewma_vol_long_ms,
                ewma_threshold_scale=config.ewma_threshold_scale
            )
        else:
            return precompute_spikes_ewma(btc_df, halflife_ms)
    else:
        raise ValueError(f"Unknown spike method: {method}")


# Legacy alias for backward compatibility
def precompute_spikes_ou(btc_df: pd.DataFrame, lookback: int = SPIKE_LOOKBACK) -> pd.DataFrame:
    """Legacy: Alias for precompute_spikes_fixed."""
    return precompute_spikes_fixed(btc_df, lookback)


# HELPER FUNCTIONS: imported from src.core
# (velocity_confirms_spike, obi_confirms_spike, should_take_spike_enhanced, compute_enhanced_score)

# =============================================================================
# SIMULATION - Uses TestConfig for both time-stop AND loser offset
# =============================================================================

def simulate_market(btc_spikes: pd.DataFrame, obs_df: pd.DataFrame,
                    slug: str, resolution: str,
                    use_obi_filter: bool, dataset_name: str,
                    config: TestConfig) -> List[TradeResult]:
    """
    Simulate trading with configurable time-stop, loser offset, and cycle mode.
    Dispatches to single-cycle or multi-cycle based on config.max_cycles.
    """
    if config.max_cycles > 1:
        return simulate_market_multicycle(
            btc_spikes, obs_df, slug, resolution, use_obi_filter, dataset_name, config
        )
    return simulate_market_single(
        btc_spikes, obs_df, slug, resolution, use_obi_filter, dataset_name, config
    )


def simulate_market_single(btc_spikes: pd.DataFrame, obs_df: pd.DataFrame,
                           slug: str, resolution: str,
                           use_obi_filter: bool, dataset_name: str,
                           config: TestConfig) -> List[TradeResult]:
    """Single-cycle simulation (original behavior)."""
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    market_spikes = btc_spikes[
        (btc_spikes['timestamp_ms'] >= market_start) &
        (btc_spikes['timestamp_ms'] <= market_end) &
        (btc_spikes['spike_detected'] == True)
    ].copy()

    if len(market_spikes) == 0:
        return []

    trades = []
    cycle_num = 0
    last_hedge_ts = 0
    in_position = False
    position_data = None
    time_stop_ms = config.time_stop_seconds * 1000

    # Market loss counter (Feb 1, 2026) - tracks losses in THIS market
    market_loss_count = 0
    market_blocked = False  # Set True when max_market_losses reached

    spike_idx = 0
    obs_idx = 0

    while spike_idx < len(market_spikes) or in_position:

        # STATE 1: In position - check for hedge
        if in_position and position_data is not None:
            entry_ts = position_data['entry_ts']

            while obs_idx < len(mdf):
                obs_row = mdf.iloc[obs_idx]
                obs_ts = obs_row['timestamp_ms']

                if obs_ts < entry_ts:
                    obs_idx += 1
                    continue

                loser_side = position_data['loser_side']
                loser_target = position_data['loser_target']
                winner_entry = position_data['winner_entry']
                spike_mag = position_data['spike_magnitude']
                score = position_data['score']

                if loser_side == "UP":
                    loser_ask = obs_row['up_ask']
                else:
                    loser_ask = obs_row['down_ask']

                # Check passive fill
                if pd.notna(loser_ask) and loser_ask <= loser_target:
                    pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                        winner_entry, loser_target, config.shares_per_cycle,
                        is_taker_entry=True,
                        is_taker_exit=False
                    )

                    trades.append(TradeResult(
                        market_slug=slug,
                        cycle_num=cycle_num,
                        entry_time_remaining=position_data['entry_time_rem'],
                        signal_score=score,
                        winner_side=position_data['winner_side'],
                        winner_fill_price=winner_entry,
                        loser_fill_price=loser_target,
                        hedge_type="passive",
                        pair_cost=winner_entry + loser_target,
                        pnl_gross=pnl_gross,
                        pnl_net=pnl_net,
                        entry_fee=entry_fee,
                        exit_fee=exit_fee,
                        correct_direction=(resolution == position_data['winner_side']),
                        spike_magnitude=spike_mag,
                        dataset=dataset_name,
                        offset_name=config.offset_name,
                        cycle_mode=config.cycle_mode,
                        shares=config.shares_per_cycle,
                        stop_loss_pct=config.stop_loss_pct,
                        max_market_losses=config.max_market_losses,
                    ))

                    # Increment loss counter if negative PnL
                    if pnl_net < 0:
                        market_loss_count += 1

                    in_position = False
                    position_data = None
                    last_hedge_ts = obs_ts
                    obs_idx += 1
                    break

                # Check STOP-LOSS FIRST (before time-stop) - Feb 1, 2026
                if config.stop_loss_pct is not None:
                    winner_side_current = position_data['winner_side']
                    if winner_side_current == "UP":
                        winner_bid_current = obs_row['up_bid']
                    else:
                        winner_bid_current = obs_row['down_bid']

                    if pd.notna(winner_bid_current) and winner_entry > 0:
                        drop_pct = (winner_entry - winner_bid_current) / winner_entry
                        if drop_pct >= config.stop_loss_pct:
                            # Stop-loss triggered - exit immediately
                            loser_fill = loser_ask if pd.notna(loser_ask) else loser_target * 1.05
                            pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                                winner_entry, loser_fill, config.shares_per_cycle,
                                is_taker_entry=True, is_taker_exit=True
                            )
                            trades.append(TradeResult(
                                market_slug=slug,
                                cycle_num=cycle_num,
                                entry_time_remaining=position_data['entry_time_rem'],
                                signal_score=score,
                                winner_side=position_data['winner_side'],
                                winner_fill_price=winner_entry,
                                loser_fill_price=loser_fill,
                                hedge_type="stop_loss",
                                pair_cost=winner_entry + loser_fill,
                                pnl_gross=pnl_gross,
                                pnl_net=pnl_net,
                                entry_fee=entry_fee,
                                exit_fee=exit_fee,
                                correct_direction=(resolution == position_data['winner_side']),
                                spike_magnitude=spike_mag,
                                dataset=dataset_name,
                                offset_name=config.offset_name,
                                cycle_mode=config.cycle_mode,
                                shares=config.shares_per_cycle,
                                stop_loss_pct=config.stop_loss_pct,
                                max_market_losses=config.max_market_losses,
                            ))

                            # Increment loss counter (stop-loss always a loss)
                            if pnl_net < 0:
                                market_loss_count += 1

                            in_position = False
                            position_data = None
                            last_hedge_ts = obs_ts
                            obs_idx += 1
                            break

                # Check BREAKEVEN EXIT (Feb 3, 2026) - exit when winner_bid <= entry
                elapsed_ms = obs_ts - entry_ts
                if config.breakeven_min_hold_ms is not None and elapsed_ms >= config.breakeven_min_hold_ms:
                    winner_side_current = position_data['winner_side']
                    if winner_side_current == "UP":
                        winner_bid_current = obs_row['up_bid']
                    else:
                        winner_bid_current = obs_row['down_bid']

                    # Breakeven = winner_bid <= entry (exit before loss grows)
                    if pd.notna(winner_bid_current) and winner_bid_current <= winner_entry:
                        loser_fill = loser_ask if pd.notna(loser_ask) else loser_target * 1.05
                        pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                            winner_entry, loser_fill, config.shares_per_cycle,
                            is_taker_entry=True, is_taker_exit=True
                        )
                        trades.append(TradeResult(
                            market_slug=slug,
                            cycle_num=cycle_num,
                            entry_time_remaining=position_data['entry_time_rem'],
                            signal_score=score,
                            winner_side=position_data['winner_side'],
                            winner_fill_price=winner_entry,
                            loser_fill_price=loser_fill,
                            hedge_type="breakeven",
                            pair_cost=winner_entry + loser_fill,
                            pnl_gross=pnl_gross,
                            pnl_net=pnl_net,
                            entry_fee=entry_fee,
                            exit_fee=exit_fee,
                            correct_direction=(resolution == position_data['winner_side']),
                            spike_magnitude=spike_mag,
                            dataset=dataset_name,
                            offset_name=config.offset_name,
                            cycle_mode=config.cycle_mode,
                            shares=config.shares_per_cycle,
                            stop_loss_pct=config.stop_loss_pct,
                            max_market_losses=config.max_market_losses,
                        ))

                        if pnl_net < 0:
                            market_loss_count += 1

                        in_position = False
                        position_data = None
                        last_hedge_ts = obs_ts
                        obs_idx += 1
                        break

                # Check time-stop (ONLY if NOT in profit)
                # elapsed_ms already calculated above for breakeven check
                if time_stop_ms > 0 and elapsed_ms >= time_stop_ms:
                    winner_side_current = position_data['winner_side']
                    if winner_side_current == "UP":
                        winner_bid_current = obs_row['up_bid']
                    else:
                        winner_bid_current = obs_row['down_bid']

                    in_profit = pd.notna(winner_bid_current) and winner_bid_current >= winner_entry

                    if not in_profit:
                        loser_fill = loser_ask if pd.notna(loser_ask) else loser_target * 1.05
                        pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                            winner_entry, loser_fill, config.shares_per_cycle,
                            is_taker_entry=True,
                            is_taker_exit=True
                        )

                        trades.append(TradeResult(
                            market_slug=slug,
                            cycle_num=cycle_num,
                            entry_time_remaining=position_data['entry_time_rem'],
                            signal_score=score,
                            winner_side=position_data['winner_side'],
                            winner_fill_price=winner_entry,
                            loser_fill_price=loser_fill,
                            hedge_type="time_stop",
                            pair_cost=winner_entry + loser_fill,
                            pnl_gross=pnl_gross,
                            pnl_net=pnl_net,
                            entry_fee=entry_fee,
                            exit_fee=exit_fee,
                            correct_direction=(resolution == position_data['winner_side']),
                            spike_magnitude=spike_mag,
                            dataset=dataset_name,
                            offset_name=config.offset_name,
                            cycle_mode=config.cycle_mode,
                            shares=config.shares_per_cycle,
                            stop_loss_pct=config.stop_loss_pct,
                            max_market_losses=config.max_market_losses,
                        ))

                        # Increment loss counter if negative PnL
                        if pnl_net < 0:
                            market_loss_count += 1

                        in_position = False
                        position_data = None
                        last_hedge_ts = obs_ts
                        obs_idx += 1
                        break

                obs_idx += 1

            # If we ran out of observer data while in position
            if in_position and obs_idx >= len(mdf):
                winner_side = position_data['winner_side']
                winner_entry = position_data['winner_entry']
                shares = config.shares_per_cycle

                entry_fee = polymarket_taker_fee(winner_entry) * winner_entry * shares

                if resolution == winner_side:
                    pnl_gross = (1.0 - winner_entry) * shares
                    loser_fill = 0.0
                else:
                    pnl_gross = (0.0 - winner_entry) * shares
                    loser_fill = 1.0

                pnl_net = pnl_gross - entry_fee

                trades.append(TradeResult(
                    market_slug=slug,
                    cycle_num=cycle_num,
                    entry_time_remaining=position_data['entry_time_rem'],
                    signal_score=position_data['score'],
                    winner_side=winner_side,
                    winner_fill_price=winner_entry,
                    loser_fill_price=loser_fill,
                    hedge_type="resolution",
                    pair_cost=winner_entry + loser_fill,
                    pnl_gross=pnl_gross,
                    pnl_net=pnl_net,
                    entry_fee=entry_fee,
                    exit_fee=0.0,
                    correct_direction=(resolution == winner_side),
                    spike_magnitude=position_data['spike_magnitude'],
                    dataset=dataset_name,
                    offset_name=config.offset_name,
                    cycle_mode=config.cycle_mode,
                    shares=shares,
                    stop_loss_pct=config.stop_loss_pct,
                    max_market_losses=config.max_market_losses,
                ))
                # Note: No need to increment market_loss_count for resolution - market is ending
                break

            continue

        # STATE 2: Not in position - check next spike
        if spike_idx >= len(market_spikes):
            break

        spike_row = market_spikes.iloc[spike_idx]
        spike_ts = spike_row['timestamp_ms']
        spike_dir = spike_row['spike_direction']
        spike_mag = spike_row['spike_magnitude']

        # Enforce cycle gap
        if (spike_ts - last_hedge_ts) < MIN_CYCLE_GAP_MS:
            spike_idx += 1
            continue

        # Find nearest observer row for this spike
        while obs_idx < len(mdf) - 1 and mdf.iloc[obs_idx + 1]['timestamp_ms'] <= spike_ts:
            obs_idx += 1

        if obs_idx >= len(mdf):
            break

        obs_row = mdf.iloc[obs_idx]
        time_rem = obs_row['time_remaining_secs']
        velocity_bps = obs_row.get('velocity_bps', 0.0) or 0.0

        # Skip if too close to end (dynamic based on config.time_stop)
        if time_rem < config.min_time:
            spike_idx += 1
            continue

        # Velocity confirmation
        if not velocity_confirms_spike(spike_dir, velocity_bps):
            spike_idx += 1
            continue

        # Enhanced score
        score = compute_enhanced_score(spike_mag, velocity_bps, spike_dir, time_rem)
        if score < ENHANCED_SCORE_THRESHOLD:
            spike_idx += 1
            continue

        # Get prices first (needed for enhanced OBI filter)
        winner_side = spike_dir
        if winner_side == "UP":
            winner_ask = obs_row['up_ask']
            loser_bid = obs_row.get('down_bid', None)
            loser_ask = obs_row.get('down_ask', None)
            obi_winner = obs_row.get('up_imbalance', None)
        else:
            winner_ask = obs_row['down_ask']
            loser_bid = obs_row.get('up_bid', None)
            loser_ask = obs_row.get('up_ask', None)
            obi_winner = obs_row.get('down_imbalance', None)

        if pd.isna(winner_ask) or winner_ask >= HIGH_ENTRY_THRESHOLD:
            spike_idx += 1
            continue

        # Enhanced OBI filter (uses loser spread, time remaining, OBI magnitude)
        if use_obi_filter:
            if obi_winner is not None and not np.isnan(obi_winner):
                # Calculate loser spread
                loser_spread = 0.05  # Default
                if pd.notna(loser_bid) and pd.notna(loser_ask):
                    loser_spread = loser_ask - loser_bid

                should_take, reject_reason = should_take_spike_enhanced(
                    spike_direction=spike_dir,
                    obi_winner=obi_winner,
                    loser_spread=loser_spread,
                    time_remaining=time_rem,
                    winner_ask_depth=None,  # Depth not available in observer data
                )
                if not should_take:
                    spike_idx += 1
                    continue

        # MARKET LOSS LIMIT check (Feb 1, 2026) - stop trading if too many losses
        if config.max_market_losses is not None:
            if market_loss_count >= config.max_market_losses:
                # Market is blocked - skip all remaining spikes
                if not market_blocked:
                    market_blocked = True
                spike_idx += 1
                continue

        # ENTRY - use config's loser bid calculation
        cycle_num += 1
        loser_side = "DOWN" if winner_side == "UP" else "UP"
        winner_entry = winner_ask
        loser_target = config.calculate_loser_bid(winner_entry, spike_mag)

        in_position = True
        position_data = {
            'winner_side': winner_side,
            'loser_side': loser_side,
            'winner_entry': winner_entry,
            'loser_target': loser_target,
            'entry_ts': spike_ts,
            'entry_time_rem': time_rem,
            'spike_magnitude': spike_mag,
            'score': score,
        }

        spike_idx += 1

    return trades


# =============================================================================
# MULTI-CYCLE SIMULATION
# =============================================================================

def simulate_market_multicycle(btc_spikes: pd.DataFrame, obs_df: pd.DataFrame,
                                slug: str, resolution: str,
                                use_obi_filter: bool, dataset_name: str,
                                config: TestConfig) -> List[TradeResult]:
    """
    Multi-cycle simulation: allows multiple concurrent entry/hedge cycles.

    Key difference from single-cycle:
    - Tracks multiple active cycles (up to config.max_cycles)
    - New entries allowed while existing cycles pending hedge
    - Each cycle uses config.shares_per_cycle shares
    """
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    market_spikes = btc_spikes[
        (btc_spikes['timestamp_ms'] >= market_start) &
        (btc_spikes['timestamp_ms'] <= market_end) &
        (btc_spikes['spike_detected'] == True)
    ].copy()

    if len(market_spikes) == 0:
        return []

    trades = []
    cycle_num = 0
    active_cycles: List[BacktestCycle] = []
    time_stop_ms = config.time_stop_seconds * 1000
    shares = config.shares_per_cycle

    spike_idx = 0
    obs_idx = 0

    while spike_idx < len(market_spikes) or active_cycles:
        # Get current timestamp
        current_ts = None
        if spike_idx < len(market_spikes):
            current_ts = market_spikes.iloc[spike_idx]['timestamp_ms']

        # Find observer row for current time
        if obs_idx < len(mdf) and current_ts:
            while obs_idx < len(mdf) - 1 and mdf.iloc[obs_idx + 1]['timestamp_ms'] <= current_ts:
                obs_idx += 1

        if obs_idx >= len(mdf):
            break

        obs_row = mdf.iloc[obs_idx]
        obs_ts = obs_row['timestamp_ms']

        # Process active cycles: check for hedge fills and time-stops
        completed_cycles = []
        for cycle in active_cycles:
            loser_side = cycle.loser_side
            if loser_side == "UP":
                loser_ask = obs_row['up_ask']
            else:
                loser_ask = obs_row['down_ask']

            # Check passive fill
            if pd.notna(loser_ask) and loser_ask <= cycle.loser_target:
                pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                    cycle.winner_entry, cycle.loser_target, shares,
                    is_taker_entry=True, is_taker_exit=False
                )
                trades.append(TradeResult(
                    market_slug=slug,
                    cycle_num=cycle.cycle_id,
                    entry_time_remaining=cycle.entry_time_rem,
                    signal_score=cycle.score,
                    winner_side=cycle.winner_side,
                    winner_fill_price=cycle.winner_entry,
                    loser_fill_price=cycle.loser_target,
                    hedge_type="passive",
                    pair_cost=cycle.winner_entry + cycle.loser_target,
                    pnl_gross=pnl_gross,
                    pnl_net=pnl_net,
                    entry_fee=entry_fee,
                    exit_fee=exit_fee,
                    correct_direction=(resolution == cycle.winner_side),
                    spike_magnitude=cycle.spike_magnitude,
                    dataset=dataset_name,
                    offset_name=config.offset_name,
                    cycle_mode=config.cycle_mode,
                    shares=shares,
                    stop_loss_pct=config.stop_loss_pct,
                    max_market_losses=config.max_market_losses,
                ))
                completed_cycles.append(cycle)
                continue

            # Check BREAKEVEN EXIT (Feb 3, 2026)
            elapsed_ms = obs_ts - cycle.entry_ts
            if config.breakeven_min_hold_ms is not None and elapsed_ms >= config.breakeven_min_hold_ms:
                winner_side = cycle.winner_side
                if winner_side == "UP":
                    winner_bid = obs_row['up_bid']
                else:
                    winner_bid = obs_row['down_bid']

                if pd.notna(winner_bid) and winner_bid <= cycle.winner_entry:
                    loser_fill = loser_ask if pd.notna(loser_ask) else cycle.loser_target * 1.05
                    pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                        cycle.winner_entry, loser_fill, shares,
                        is_taker_entry=True, is_taker_exit=True
                    )
                    trades.append(TradeResult(
                        market_slug=slug,
                        cycle_num=cycle.cycle_id,
                        entry_time_remaining=cycle.entry_time_rem,
                        signal_score=cycle.score,
                        winner_side=cycle.winner_side,
                        winner_fill_price=cycle.winner_entry,
                        loser_fill_price=loser_fill,
                        hedge_type="breakeven",
                        pair_cost=cycle.winner_entry + loser_fill,
                        pnl_gross=pnl_gross,
                        pnl_net=pnl_net,
                        entry_fee=entry_fee,
                        exit_fee=exit_fee,
                        correct_direction=(resolution == cycle.winner_side),
                        spike_magnitude=cycle.spike_magnitude,
                        dataset=dataset_name,
                        offset_name=config.offset_name,
                        cycle_mode=config.cycle_mode,
                        shares=shares,
                        stop_loss_pct=config.stop_loss_pct,
                        max_market_losses=config.max_market_losses,
                    ))
                    completed_cycles.append(cycle)
                    continue

            # Check time-stop
            # elapsed_ms already calculated above
            if time_stop_ms > 0 and elapsed_ms >= time_stop_ms:
                winner_side = cycle.winner_side
                if winner_side == "UP":
                    winner_bid = obs_row['up_bid']
                else:
                    winner_bid = obs_row['down_bid']

                in_profit = pd.notna(winner_bid) and winner_bid >= cycle.winner_entry

                if not in_profit:
                    loser_fill = loser_ask if pd.notna(loser_ask) else cycle.loser_target * 1.05
                    pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                        cycle.winner_entry, loser_fill, shares,
                        is_taker_entry=True, is_taker_exit=True
                    )
                    trades.append(TradeResult(
                        market_slug=slug,
                        cycle_num=cycle.cycle_id,
                        entry_time_remaining=cycle.entry_time_rem,
                        signal_score=cycle.score,
                        winner_side=cycle.winner_side,
                        winner_fill_price=cycle.winner_entry,
                        loser_fill_price=loser_fill,
                        hedge_type="time_stop",
                        pair_cost=cycle.winner_entry + loser_fill,
                        pnl_gross=pnl_gross,
                        pnl_net=pnl_net,
                        entry_fee=entry_fee,
                        exit_fee=exit_fee,
                        correct_direction=(resolution == cycle.winner_side),
                        spike_magnitude=cycle.spike_magnitude,
                        dataset=dataset_name,
                        offset_name=config.offset_name,
                        cycle_mode=config.cycle_mode,
                        shares=shares,
                        stop_loss_pct=config.stop_loss_pct,
                        max_market_losses=config.max_market_losses,
                    ))
                    completed_cycles.append(cycle)

        # Remove completed cycles
        for c in completed_cycles:
            active_cycles.remove(c)

        # Check for new entry if capacity available
        if len(active_cycles) < config.max_cycles and spike_idx < len(market_spikes):
            spike_row = market_spikes.iloc[spike_idx]
            spike_ts = spike_row['timestamp_ms']
            spike_dir = spike_row['spike_direction']
            spike_mag = spike_row['spike_magnitude']

            time_rem = obs_row['time_remaining_secs']
            velocity_bps = obs_row.get('velocity_bps', 0.0) or 0.0

            # Get prices first (needed for enhanced OBI)
            winner_side = spike_dir
            if winner_side == "UP":
                winner_ask = obs_row['up_ask']
                loser_bid = obs_row.get('down_bid', None)
                loser_ask = obs_row.get('down_ask', None)
                obi_winner = obs_row.get('up_imbalance', None)
            else:
                winner_ask = obs_row['down_ask']
                loser_bid = obs_row.get('up_bid', None)
                loser_ask = obs_row.get('up_ask', None)
                obi_winner = obs_row.get('down_imbalance', None)

            # Entry filters
            can_enter = True
            score = 0.0
            if time_rem < config.min_time:
                can_enter = False
            elif not velocity_confirms_spike(spike_dir, velocity_bps):
                can_enter = False
            elif pd.isna(winner_ask) or winner_ask >= HIGH_ENTRY_THRESHOLD:
                can_enter = False
            else:
                score = compute_enhanced_score(spike_mag, velocity_bps, spike_dir, time_rem)
                if score < ENHANCED_SCORE_THRESHOLD:
                    can_enter = False
                elif use_obi_filter and obi_winner is not None and not np.isnan(obi_winner):
                    # Binary OBI filter (simple check - does orderbook confirm spike?)
                    loser_spread = 0.05
                    if pd.notna(loser_bid) and pd.notna(loser_ask):
                        loser_spread = loser_ask - loser_bid
                    should_take, _ = should_take_spike_enhanced(
                        spike_direction=spike_dir,
                        obi_winner=obi_winner,
                        loser_spread=loser_spread,
                        time_remaining=time_rem,
                        winner_ask_depth=None,
                    )
                    if not should_take:
                        can_enter = False

            # Direction consistency check (CRITICAL FIX - Jan 31, 2026)
            # Prevents conflicting positions (long UP while also long DOWN)
            if can_enter:
                can_enter_dir, dir_reason = can_enter_direction(
                    spike_direction=spike_dir,
                    active_cycles=active_cycles,
                    direction_mode=config.direction_mode,
                )
                if not can_enter_dir:
                    can_enter = False

            if can_enter:
                cycle_num += 1
                loser_side = "DOWN" if winner_side == "UP" else "UP"
                loser_target = config.calculate_loser_bid(winner_ask, spike_mag)

                active_cycles.append(BacktestCycle(
                    cycle_id=cycle_num,
                    entry_ts=spike_ts,
                    winner_side=winner_side,
                    loser_side=loser_side,
                    winner_entry=winner_ask,
                    loser_target=loser_target,
                    entry_time_rem=time_rem,
                    spike_magnitude=spike_mag,
                    score=score,
                    shares=shares,
                ))

            spike_idx += 1
        elif spike_idx < len(market_spikes):
            spike_idx += 1  # Skip spike if at capacity

        obs_idx += 1

    # Handle resolution for any remaining active cycles
    for cycle in active_cycles:
        entry_fee = polymarket_taker_fee(cycle.winner_entry) * cycle.winner_entry * shares
        if resolution == cycle.winner_side:
            pnl_gross = (1.0 - cycle.winner_entry) * shares
            loser_fill = 0.0
        else:
            pnl_gross = (0.0 - cycle.winner_entry) * shares
            loser_fill = 1.0
        pnl_net = pnl_gross - entry_fee

        trades.append(TradeResult(
            market_slug=slug,
            cycle_num=cycle.cycle_id,
            entry_time_remaining=cycle.entry_time_rem,
            signal_score=cycle.score,
            winner_side=cycle.winner_side,
            winner_fill_price=cycle.winner_entry,
            loser_fill_price=loser_fill,
            hedge_type="resolution",
            pair_cost=cycle.winner_entry + loser_fill,
            pnl_gross=pnl_gross,
            pnl_net=pnl_net,
            entry_fee=entry_fee,
            exit_fee=0.0,
            correct_direction=(resolution == cycle.winner_side),
            spike_magnitude=cycle.spike_magnitude,
            dataset=dataset_name,
            offset_name=config.offset_name,
            cycle_mode=config.cycle_mode,
            shares=shares,
            stop_loss_pct=config.stop_loss_pct,
            max_market_losses=config.max_market_losses,
        ))

    return trades


# =============================================================================
# DATA LOADING
# =============================================================================

def load_dataset(dataset_key: str) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame], Dict[str, str], float]:
    """Load a dataset."""
    config = DATASETS[dataset_key]
    base_dir = Path("/Users/rananjaybika/polymarket-amm-bot")

    print(f"\n{'='*60}")
    print(f"Loading {config['name']}")
    print(f"{'='*60}")

    obs_dfs = []
    for fname in config['obs_files']:
        fpath = base_dir / fname
        if fpath.exists():
            df = pd.read_csv(fpath, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)
            print(f"  {fpath.name}: {len(df):,} rows")
        else:
            print(f"  {fpath.name}: NOT FOUND")

    if not obs_dfs:
        return None, None, {}, 0

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    print(f"  Combined observer: {len(obs_df):,} rows")

    btc_df = None
    if config['btc_file']:
        btc_path = base_dir / config['btc_file']
        if btc_path.exists():
            btc_df = pd.read_csv(btc_path)
            print(f"  Binance HF: {len(btc_df):,} rows")
        else:
            print(f"  Binance HF: NOT FOUND - will use observer binance_price")

    if btc_df is None:
        if 'binance_price' in obs_df.columns:
            btc_df = obs_df[['timestamp_ms', 'binance_price']].copy()
            btc_df = btc_df.rename(columns={'binance_price': 'price'})
            btc_df = btc_df.dropna()
            btc_df = btc_df.drop_duplicates(subset='timestamp_ms')
            print(f"  Using observer binance_price: {len(btc_df):,} rows (5Hz)")
        else:
            print(f"  ERROR: No price data available")
            return None, None, {}, 0

    res_path = base_dir / "research/observer/market_resolutions_verified.csv"
    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    btc_start, btc_end = btc_df['timestamp_ms'].min(), btc_df['timestamp_ms'].max()
    obs_start, obs_end = obs_df['timestamp_ms'].min(), obs_df['timestamp_ms'].max()

    overlap_start = max(btc_start, obs_start)
    overlap_end = min(btc_end, obs_end)

    duration_hours = (overlap_end - overlap_start) / 3600000
    print(f"  Overlap: {duration_hours:.2f} hours")

    btc_df = btc_df[(btc_df['timestamp_ms'] >= overlap_start) &
                     (btc_df['timestamp_ms'] <= overlap_end)].copy()
    obs_df = obs_df[(obs_df['timestamp_ms'] >= overlap_start) &
                     (obs_df['timestamp_ms'] <= overlap_end)].copy()

    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    valid_slugs = []
    for slug, mdf in obs_df.groupby('market_slug'):
        max_time = mdf['time_remaining_secs'].max()
        min_time = mdf['time_remaining_secs'].min()
        duration = max_time - min_time
        if duration >= MIN_RUNTIME_SECS and max_time >= 840:
            valid_slugs.append(slug)

    obs_df = obs_df[obs_df['market_slug'].isin(valid_slugs)]

    print(f"  Valid markets: {len(valid_slugs)}")

    return btc_df, obs_df, res_map, duration_hours


# =============================================================================
# DEEP METRICS COMPUTATION - Per-trade analysis for autonomous trading decisions
# =============================================================================

def compute_deep_metrics(trades: List[TradeResult], hours: float, config_name: str, dataset_name: str) -> dict:
    """
    Compute comprehensive risk metrics for autonomous trading decisions.

    Returns metrics required by CLAUDE_MISTAKES.md MANDATORY ANALYSIS METRICS:
    - Sharpe ratio (> 1.0 minimum, > 1.5 strong)
    - Profitable market % (> 50% minimum)
    - Worst single trade (> -$10)
    - Worst single market
    - Taker exit % (formerly "unhedged %") - lower is better
    - Max drawdown % (target < 20%)
    """
    if not trades:
        return None

    trades_df = pd.DataFrame([t.__dict__ for t in trades])
    returns = trades_df['pnl_net']

    # Sharpe ratio (annualized to hourly - sqrt of trades per hour)
    trades_per_hour = len(trades_df) / hours if hours > 0 else 0
    if returns.std() > 0 and trades_per_hour > 0:
        sharpe = (returns.mean() / returns.std()) * np.sqrt(trades_per_hour)
    else:
        sharpe = 0.0

    # Max drawdown calculation
    cumulative_pnl = returns.cumsum()
    rolling_max = cumulative_pnl.cummax()
    drawdown = rolling_max - cumulative_pnl
    max_drawdown = drawdown.max()
    total_pnl = returns.sum()
    max_drawdown_pct = (max_drawdown / abs(total_pnl) * 100) if total_pnl != 0 else 0

    # Worst single trade
    worst_trade_pnl = returns.min()
    worst_trade_idx = returns.idxmin()
    worst_trade_market = trades_df.loc[worst_trade_idx, 'market_slug']

    # Per-market statistics
    market_pnl = trades_df.groupby('market_slug')['pnl_net'].sum()
    worst_market_pnl = market_pnl.min()
    worst_market_slug = market_pnl.idxmin()
    profitable_markets = (market_pnl > 0).sum()
    total_markets = len(market_pnl)
    profitable_market_pct = (profitable_markets / total_markets * 100) if total_markets > 0 else 0

    # Taker exit % (breakeven, time_stop, stop_loss, resolution = taker exit)
    # These trades ARE hedged, just via taker instead of passive maker
    taker_exit_count = trades_df['hedge_type'].isin(['breakeven', 'time_stop', 'stop_loss', 'resolution']).sum()
    taker_exit_pct = (taker_exit_count / len(trades_df) * 100) if len(trades_df) > 0 else 0

    # Exit type stats
    passive_pct = (trades_df['hedge_type'] == 'passive').sum() / len(trades_df) * 100
    breakeven_pct = (trades_df['hedge_type'] == 'breakeven').sum() / len(trades_df) * 100
    time_stop_pct = (trades_df['hedge_type'] == 'time_stop').sum() / len(trades_df) * 100
    stop_loss_pct = (trades_df['hedge_type'] == 'stop_loss').sum() / len(trades_df) * 100

    return {
        'sharpe': sharpe,
        'max_drawdown': max_drawdown,
        'max_drawdown_pct': max_drawdown_pct,
        'profitable_market_pct': profitable_market_pct,
        'profitable_markets': profitable_markets,
        'total_markets': total_markets,
        'worst_trade_pnl': worst_trade_pnl,
        'worst_trade_market': worst_trade_market,
        'worst_market_pnl': worst_market_pnl,
        'worst_market_slug': worst_market_slug,
        'taker_exit_pct': taker_exit_pct,
        'passive_pct': passive_pct,
        'breakeven_pct': breakeven_pct,
        'time_stop_pct': time_stop_pct,
        'stop_loss_pct': stop_loss_pct,
    }


def save_per_trade_csv(trades: List[TradeResult], config_name: str, dataset_name: str) -> str:
    """Save per-trade data to CSV for detailed analysis."""
    if not trades:
        return None

    trades_df = pd.DataFrame([t.__dict__ for t in trades])

    # Create clean filename
    clean_config = config_name.replace('/', '_').replace(' ', '_')
    clean_dataset = dataset_name.replace('+', '_').replace(' ', '_')

    trades_path = Path(f"research/findings/data/pure_ewma_trades_{clean_config}_{clean_dataset}.csv")
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    trades_df.to_csv(trades_path, index=False)

    return str(trades_path)


# =============================================================================
# MAIN GRID SEARCH - With deep metrics and per-trade logging
# =============================================================================

def run_grid_search():
    """Run grid search across all configurations with deep metrics."""

    if not validate_dataset_coverage():
        print("\n  ABORTING: Fix dataset coverage issues before running backtest!")
        return None

    print("=" * 80)
    print("BREAKEVEN SWEEP - Winner config, varying BE hold time only")
    print("=" * 80)

    print(f"Breakeven hold times: {len(CONFIGS)} configs")
    print(f"  {BREAKEVEN_HOLD_MS_OPTIONS}")
    print(f"Datasets: {len(DATASETS_TO_RUN)} ({', '.join(DATASETS_TO_RUN)})")
    print(f"Total runs: {len(CONFIGS) * len(DATASETS_TO_RUN)}")
    print()

    load_ou_params()

    # Cache dataset loading only (not spikes - each config has different thresholds)
    dataset_cache = {}  # key: dataset_key, value: (btc_df, obs_df, res_map, hours, use_obi)
    all_results = []
    all_deep_metrics = []
    checkpoint_path = Path("research/findings/data/breakeven_sweep_checkpoint.csv")
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    total_iterations = len(CONFIGS) * len(DATASETS_TO_RUN)
    iteration = 0
    start_time = time.time()

    for config in CONFIGS:
        for dataset_key in DATASETS_TO_RUN:
            iteration += 1
            ds_config = DATASETS[dataset_key]
            use_obi = ds_config['use_obi']

            print(f"\n[{iteration}/{total_iterations}] {config.name} on {dataset_key}")

            # Load dataset (cached by dataset_key)
            if dataset_key not in dataset_cache:
                btc_df, obs_df, res_map, hours = load_dataset(dataset_key)
                if btc_df is not None:
                    dataset_cache[dataset_key] = (btc_df, obs_df, res_map, hours, use_obi)
                else:
                    continue
            else:
                btc_df, obs_df, res_map, hours, use_obi = dataset_cache[dataset_key]

            # Compute spikes - NO CACHING (each config has different threshold params)
            # This prevents memory bloat from storing 7 different spike DataFrames per dataset
            btc_spikes = precompute_spikes(btc_df, config.spike_method, config)
            gc.collect()  # Force garbage collection to free memory

            all_trades = []
            slugs = obs_df['market_slug'].unique()

            for slug in tqdm(slugs, desc=f"  Markets", leave=False):
                resolution = res_map.get(slug)
                if resolution not in ['UP', 'DOWN']:
                    continue

                trades = simulate_market(
                    btc_spikes, obs_df, slug, resolution, use_obi, dataset_key, config
                )
                all_trades.extend(trades)

            if all_trades:
                total_pnl_net = sum(t.pnl_net for t in all_trades)
                total_trades = len(all_trades)
                win_rate = sum(1 for t in all_trades if t.correct_direction) / total_trades * 100
                avg_pair_cost = np.mean([t.pair_cost for t in all_trades])
                passive_pct = sum(1 for t in all_trades if t.hedge_type == "passive") / total_trades * 100
                hourly_rate = total_pnl_net / hours if hours > 0 else 0

                # Compute deep metrics (MANDATORY per CLAUDE_MISTAKES.md)
                deep_metrics = compute_deep_metrics(all_trades, hours, config.name, dataset_key)

                # Save per-trade CSV for detailed analysis
                trades_csv_path = save_per_trade_csv(all_trades, config.name, dataset_key)

                result = {
                    'config': config.name,
                    'spike_method': config.spike_method,
                    'time_stop': config.time_stop_seconds,
                    'drop_multiplier': config.drop_multiplier,
                    'drop_intercept': config.drop_intercept,
                    'offset_name': config.offset_name,
                    'cycle_mode': config.cycle_mode,
                    'max_cycles': config.max_cycles,
                    'shares_per_cycle': config.shares_per_cycle,
                    'dataset': dataset_key,
                    'use_obi': use_obi,
                    'hours': hours,
                    'trades': total_trades,
                    'pnl_net': total_pnl_net,
                    'hourly_rate': hourly_rate,
                    'win_rate': win_rate,
                    'avg_pair_cost': avg_pair_cost,
                    'passive_pct': passive_pct,
                    # Deep metrics
                    'sharpe': deep_metrics['sharpe'] if deep_metrics else 0,
                    'max_drawdown': deep_metrics['max_drawdown'] if deep_metrics else 0,
                    'max_drawdown_pct': deep_metrics['max_drawdown_pct'] if deep_metrics else 0,
                    'profitable_market_pct': deep_metrics['profitable_market_pct'] if deep_metrics else 0,
                    'worst_trade_pnl': deep_metrics['worst_trade_pnl'] if deep_metrics else 0,
                    'worst_market_pnl': deep_metrics['worst_market_pnl'] if deep_metrics else 0,
                    'worst_market_slug': deep_metrics['worst_market_slug'] if deep_metrics else '',
                    'taker_exit_pct': deep_metrics['taker_exit_pct'] if deep_metrics else 0,
                    'trades_csv': trades_csv_path,
                }
                all_results.append(result)

                # Store deep metrics separately for detailed report
                if deep_metrics:
                    deep_metrics['config'] = config.name
                    deep_metrics['dataset'] = dataset_key
                    deep_metrics['use_obi'] = use_obi
                    deep_metrics['hourly_rate'] = hourly_rate
                    deep_metrics['hours'] = hours
                    deep_metrics['trades'] = total_trades
                    all_deep_metrics.append(deep_metrics)

                # Print summary with deep metrics
                print(f"    Trades: {total_trades}, $/hr: ${hourly_rate:.2f}, Win%: {win_rate:.1f}%")
                print(f"    Sharpe: {deep_metrics['sharpe']:.2f}, Drawdown: ${deep_metrics['max_drawdown']:.2f}, ProfMkts: {deep_metrics['profitable_market_pct']:.1f}%, Taker: {deep_metrics['taker_exit_pct']:.1f}%")
                print(f"    Worst trade: ${deep_metrics['worst_trade_pnl']:.2f}, Worst mkt: ${deep_metrics['worst_market_pnl']:.2f} ({deep_metrics['worst_market_slug'][:30]}...)")

        # Checkpoint save after each config
        if all_results:
            pd.DataFrame(all_results).to_csv(checkpoint_path, index=False)

    elapsed = time.time() - start_time
    print(f"\n\nCompleted in {elapsed / 60:.1f} minutes")

    # Save deep metrics summary
    if all_deep_metrics:
        deep_metrics_path = Path("research/findings/data/pure_ewma_deep_metrics.csv")
        pd.DataFrame(all_deep_metrics).to_csv(deep_metrics_path, index=False)
        print(f"\nDeep metrics saved to: {deep_metrics_path}")

    return all_results, all_deep_metrics


def print_results(results: List[dict], deep_metrics: List[dict] = None):
    """Print comprehensive results summary with deep metrics."""
    df = pd.DataFrame(results)

    print("\n" + "=" * 140)
    print("COMPREHENSIVE GRID SEARCH RESULTS - Spike Method × Time-Stop × Hedge Formula")
    print("=" * 140)

    # ==========================================================================
    # DEEP METRICS TABLE (MANDATORY per CLAUDE_MISTAKES.md)
    # ==========================================================================
    print("\n" + "=" * 140)
    print("DEEP METRICS - Required for Autonomous Trading Decision")
    print("Thresholds: Sharpe > 1.0, Drawdown < 20%, Profitable Mkts > 50%, Worst Trade > -$10")
    print("=" * 140)

    if 'sharpe' in df.columns:
        print()
        print(f"{'Config':<22} {'Dataset':<10} {'$/hr':>8} {'Sharpe':>7} {'MaxDD':>8} {'ProfMkt%':>9} {'WorstTrd':>10} {'Taker%':>8}")
        print("-" * 140)

        for _, row in df.sort_values(['config', 'dataset']).iterrows():
            # Color-code pass/fail (using markers)
            sharpe_mark = "✓" if row['sharpe'] > 1.0 else "✗"
            drawdown_mark = "✓" if row.get('max_drawdown', 0) < row.get('pnl_net', 1) * 0.2 else "✗"
            profmkt_mark = "✓" if row['profitable_market_pct'] > 50 else "✗"
            worst_trade_mark = "✓" if row['worst_trade_pnl'] > -10 else "✗"

            taker_pct = row.get('taker_exit_pct', row.get('unhedged_pct', 0))

            print(f"{row['config']:<22} {row['dataset']:<10} "
                  f"${row['hourly_rate']:>7.2f} {row['sharpe']:>6.2f}{sharpe_mark} "
                  f"${row.get('max_drawdown', 0):>6.2f}{drawdown_mark} "
                  f"{row['profitable_market_pct']:>8.1f}%{profmkt_mark} "
                  f"${row['worst_trade_pnl']:>8.2f}{worst_trade_mark} "
                  f"{taker_pct:>7.1f}%")

    # ==========================================================================
    # CONSERVATIVE VALIDATION SUMMARY (older datasets without OBI)
    # ==========================================================================
    conservative_df = df[df['use_obi'] == False] if 'use_obi' in df.columns else pd.DataFrame()
    if len(conservative_df) > 0:
        print("\n" + "=" * 120)
        print("CONSERVATIVE VALIDATION (Older Data WITHOUT OBI)")
        print("If profitable here, strategy is robust (OBI improves results ~4pp)")
        print("=" * 120)
        print()

        for config_name in conservative_df['config'].unique():
            config_df = conservative_df[conservative_df['config'] == config_name]
            total_pnl = config_df['pnl_net'].sum()
            total_hours = config_df['hours'].sum()
            avg_rate = total_pnl / total_hours if total_hours > 0 else 0
            avg_sharpe = config_df['sharpe'].mean()
            all_profitable = (config_df['hourly_rate'] > 0).all()

            status = "PASS ✓" if all_profitable and avg_rate > 0 else "FAIL ✗"
            print(f"  {config_name:<25}: ${avg_rate:.2f}/hr, Sharpe={avg_sharpe:.2f} → {status}")

            # Per-dataset breakdown
            for _, row in config_df.iterrows():
                ds_status = "+" if row['hourly_rate'] > 0 else "-"
                print(f"      {row['dataset']:<12}: ${row['hourly_rate']:.2f}/hr {ds_status}")

    # ==========================================================================
    # OOS7/8/9.1 RESULTS (with OBI - primary validation)
    # ==========================================================================
    obi_on_df = df[df['use_obi'] == True] if 'use_obi' in df.columns else df
    if len(obi_on_df) > 0:
        print("\n" + "=" * 120)
        print("PRIMARY VALIDATION (OOS7/8/9.1 WITH OBI)")
        print("=" * 120)

        # Per-dataset breakdown
        datasets_shown = set()
        for dataset in obi_on_df['dataset'].unique():
            ds_df = obi_on_df[obi_on_df['dataset'] == dataset]
            if len(ds_df) == 0 or dataset in datasets_shown:
                continue
            datasets_shown.add(dataset)

            hours = ds_df['hours'].iloc[0]
            print(f"\n{'='*80}")
            print(f"Dataset: {dataset} ({hours:.1f}h, OBI ON)")
            print(f"{'='*80}")
            print()
            print(f"{'Config':<22} {'Trades':>8} {'$/hr':>10} {'Sharpe':>8} {'Drawdown':>10} {'ProfMkt%':>10} {'Taker%':>8}")
            print("-" * 90)

            ds_df_sorted = ds_df.sort_values('hourly_rate', ascending=False)
            for _, row in ds_df_sorted.iterrows():
                taker_pct = row.get('taker_exit_pct', row.get('unhedged_pct', 0))
                print(f"{row['config']:<22} {row['trades']:>8} ${row['hourly_rate']:>9.2f} "
                      f"{row['sharpe']:>7.2f} ${row.get('max_drawdown', 0):>8.2f} {row['profitable_market_pct']:>9.1f}% {taker_pct:>7.1f}%")

    # Combined results
    print("\n" + "=" * 100)
    print("COMBINED RESULTS (All Datasets)")
    print("=" * 100)

    combined = df.groupby('config').agg({
        'trades': 'sum',
        'pnl_net': 'sum',
        'hours': 'sum',
        'win_rate': 'mean',
        'passive_pct': 'mean',
        'avg_pair_cost': 'mean',
    }).reset_index()

    combined['hourly_rate'] = combined['pnl_net'] / combined['hours']
    combined = combined.sort_values('hourly_rate', ascending=False)

    print()
    print(f"{'Config':<20} {'Trades':>8} {'$/hr':>10} {'Win%':>8} {'Passive%':>10} {'Winner':>8}")
    print("-" * 80)

    best_rate = combined['hourly_rate'].max()
    for _, row in combined.iterrows():
        winner = "  *" if row['hourly_rate'] == best_rate else ""
        print(f"{row['config']:<20} {row['trades']:>8} ${row['hourly_rate']:>9.2f} "
              f"{row['win_rate']:>7.1f}% {row['passive_pct']:>9.1f}% {winner:>8}")

    # Hedge formula sensitivity
    print("\n" + "=" * 100)
    print("HEDGE FORMULA SENSITIVITY ANALYSIS")
    print("=" * 100)

    print("\nBy Hedge Formula:")
    for hedge_name in HEDGE_FORMULAS.keys():
        hedge_df = df[df['offset_name'] == hedge_name]
        if len(hedge_df) > 0:
            total_pnl = hedge_df['pnl_net'].sum()
            total_hours = hedge_df['hours'].sum()
            avg_rate = total_pnl / total_hours
            avg_passive = hedge_df['passive_pct'].mean()
            mult, intercept, desc = HEDGE_FORMULAS[hedge_name]
            print(f"  {hedge_name:<8}: ${avg_rate:.2f}/hr, {avg_passive:.1f}% passive (mult={mult}, int={intercept})")

    # Time-stop sensitivity
    print("\nBy Time-Stop:")
    for ts in TIME_STOPS:
        ts_df = df[df['time_stop'] == ts]
        if len(ts_df) > 0:
            total_pnl = ts_df['pnl_net'].sum()
            total_hours = ts_df['hours'].sum()
            avg_rate = total_pnl / total_hours
            avg_passive = ts_df['passive_pct'].mean()
            print(f"  {int(ts):>4}s: ${avg_rate:.2f}/hr, {avg_passive:.1f}% passive")

    # Cycle-mode sensitivity
    print("\nBy Cycle Mode:")
    for mode_name, (max_cycles, shares_per, dir_mode, desc) in CYCLE_MODES.items():
        mode_df = df[df['cycle_mode'] == mode_name]
        if len(mode_df) > 0:
            total_pnl = mode_df['pnl_net'].sum()
            total_hours = mode_df['hours'].sum()
            total_trades = mode_df['trades'].sum()
            avg_rate = total_pnl / total_hours
            avg_win = mode_df['win_rate'].mean()
            print(f"  {mode_name:<12}: ${avg_rate:.2f}/hr, {total_trades} trades, {avg_win:.1f}% win ({desc})")

    # Spike method sensitivity
    print("\n" + "=" * 120)
    print("SPIKE METHOD SENSITIVITY ANALYSIS")
    print("=" * 120)
    print("\nBy Spike Method:")
    for method in SPIKE_METHODS:
        method_df = df[df['spike_method'] == method] if 'spike_method' in df.columns else pd.DataFrame()
        if len(method_df) > 0:
            total_pnl = method_df['pnl_net'].sum()
            total_hours = method_df['hours'].sum()
            total_trades = method_df['trades'].sum()
            avg_rate = total_pnl / total_hours if total_hours > 0 else 0
            avg_sharpe = method_df['sharpe'].mean() if 'sharpe' in method_df.columns else 0
            avg_drawdown = method_df['max_drawdown'].mean() if 'max_drawdown' in method_df.columns else 0
            print(f"  {method:<12}: ${avg_rate:.2f}/hr, {total_trades} trades, Sharpe={avg_sharpe:.2f}, Drawdown=${avg_drawdown:.2f}")

    # Time-stop sensitivity with deep metrics
    print("\n" + "=" * 120)
    print("TIME-STOP SENSITIVITY WITH DEEP METRICS")
    print("=" * 120)
    print("\nBy Time-Stop:")
    for ts in TIME_STOPS:
        ts_df = df[df['time_stop'] == ts]
        if len(ts_df) > 0:
            total_pnl = ts_df['pnl_net'].sum()
            total_hours = ts_df['hours'].sum()
            avg_rate = total_pnl / total_hours if total_hours > 0 else 0
            avg_sharpe = ts_df['sharpe'].mean() if 'sharpe' in ts_df.columns else 0
            avg_taker = ts_df.get('taker_exit_pct', ts_df.get('unhedged_pct', pd.Series([0]))).mean()
            avg_profmkt = ts_df['profitable_market_pct'].mean() if 'profitable_market_pct' in ts_df.columns else 0
            avg_drawdown = ts_df['max_drawdown'].mean() if 'max_drawdown' in ts_df.columns else 0
            print(f"  {int(ts):>4}s: ${avg_rate:.2f}/hr, Sharpe={avg_sharpe:.2f}, Drawdown=${avg_drawdown:.2f}, ProfMkt={avg_profmkt:.1f}%, Taker={avg_taker:.1f}%")

    # ==========================================================================
    # DECISION CRITERIA FOR AUTONOMOUS TRADING
    # ==========================================================================
    print("\n" + "=" * 120)
    print("DECISION CRITERIA FOR AUTONOMOUS TRADING")
    print("=" * 120)
    print()
    print("Minimum thresholds:")
    print("  - Sharpe > 1.0 on OOS7/8/9.1 (with OBI)")
    print("  - Profitable market % > 50% across all datasets")
    print("  - Worst single trade > -$10 (no catastrophic losses)")
    print("  - Max drawdown < 20% of total PnL")
    print("  - Profitable on older data (no OBI) - conservative validation passes")
    print()
    print("Strong signal if:")
    print("  - Sharpe > 1.5")
    print("  - Profitable on ALL datasets including older ones")
    print("  - Worst market loss < -$20")
    print()

    # Evaluate each config against criteria
    if 'sharpe' in df.columns:
        print("Config Evaluation:")
        for config_name in df['config'].unique():
            config_df = df[df['config'] == config_name]

            # OBI ON results (primary)
            obi_on = config_df[config_df['use_obi'] == True] if 'use_obi' in config_df.columns else config_df
            # OBI OFF results (conservative)
            obi_off = config_df[config_df['use_obi'] == False] if 'use_obi' in config_df.columns else pd.DataFrame()

            passes_sharpe = (obi_on['sharpe'] > 1.0).all() if len(obi_on) > 0 else False
            passes_profmkt = (config_df['profitable_market_pct'] > 50).all()
            passes_worst_trade = (config_df['worst_trade_pnl'] > -10).all()
            passes_drawdown = (config_df['max_drawdown_pct'] < 20).all() if 'max_drawdown_pct' in config_df.columns else True
            passes_conservative = (obi_off['hourly_rate'] > 0).all() if len(obi_off) > 0 else True

            all_pass = passes_sharpe and passes_profmkt and passes_worst_trade and passes_drawdown and passes_conservative

            status = "READY FOR AUTONOMOUS TRADING ✓" if all_pass else "NEEDS REVIEW"
            print(f"\n  {config_name}:")
            print(f"    Sharpe > 1.0 (OBI ON):     {'✓' if passes_sharpe else '✗'}")
            print(f"    Profitable Mkts > 50%:     {'✓' if passes_profmkt else '✗'}")
            print(f"    Worst Trade > -$10:        {'✓' if passes_worst_trade else '✗'}")
            print(f"    Drawdown < 20% PnL:        {'✓' if passes_drawdown else '✗'}")
            print(f"    Conservative Valid (no OBI): {'✓' if passes_conservative else '✗'}")
            print(f"    → {status}")

    # Save final results
    output_path = Path("research/findings/data/pure_ewma_test_results.csv")
    df.to_csv(output_path, index=False)
    print(f"\nResults saved to: {output_path}")


def main():
    print("=" * 100)
    print("BREAKEVEN EXIT SWEEP - Testing optimal hold time before breakeven check")
    print("=" * 100)
    print()
    print("WINNER CONFIG (FIXED):")
    print("  - Spike: EWMA_1000 (1000ms half-life)")
    print("  - Threshold: OU adaptive")
    print("  - Time-stop: 30s")
    print("  - Hedge: OLD (0.50/0.08)")
    print()
    print("TESTING ONLY: Breakeven min hold times")
    print(f"  {BREAKEVEN_HOLD_MS_OPTIONS}")
    print()
    print(f"Datasets: {DATASETS_TO_RUN} (60Hz only)")
    print(f"Total configs: {len(CONFIGS)}")
    print()

    result = run_grid_search()

    if result:
        results, deep_metrics = result
        print_results(results, deep_metrics)


if __name__ == "__main__":
    main()
