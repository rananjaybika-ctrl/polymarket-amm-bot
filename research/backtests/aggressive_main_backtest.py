#!/usr/bin/env python3
"""
Multi-Dataset AGGRESSIVE Backtest - QUICK SINGLE-CONFIG VALIDATION

=============================================================================
USE THIS FOR: Quick validation with winner config (no grid search)
FOR GRID SEARCH: Use research/optimizers/aggressive_grid_search.py
=============================================================================

CRITICAL: This file's simulation logic MUST match aggressive_grid_search.py exactly.
Any divergence will produce different results from validated benchmarks.

Runs validated backtest on multiple datasets with SINGLE winner config:
- IS+OOS2 (Jan 16-19): OBI auto
- OOS3+4 (Jan 22-24): OBI auto
- OOS5 (Jan 26): OBI auto
- OOS7 (Jan 29-30): OBI ON
- OOS8 (Jan 31): OBI ON

Uses 60Hz Binance HF data for spike detection (matching live strategy).

Usage:
    python research/backtests/aggressive_main_backtest.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict
from tqdm import tqdm
import math
import sys

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
    calculate_expensive_bid as calculate_expensive_bid_core,
    # Constants
    VELOCITY_CONFIRM_THRESHOLD,
    ENHANCED_SCORE_THRESHOLD,
)

# =============================================================================
# CONFIGURATION - FROM TRADING_CONFIGS.py (Single Source of Truth for PARAMS)
# =============================================================================
from research.reference.TRADING_CONFIGS import AGGRESSIVE as AGGRESSIVE_CONFIG

# -----------------------------------------------------------------------------
# PARAMS FROM TRADING_CONFIGS.py (winner config flows here)
# -----------------------------------------------------------------------------
SPIKE_LOOKBACK = AGGRESSIVE_CONFIG.lookback_ticks       # 72 ticks (1200ms at 60Hz)
TIME_STOP_SECONDS = AGGRESSIVE_CONFIG.time_stop_seconds # 30.0s (EWMA winner)
MIN_TIME = AGGRESSIVE_CONFIG.min_time_remaining         # 90.0s (time_stop + 60s buffer)
HIGH_ENTRY_THRESHOLD = AGGRESSIVE_CONFIG.high_entry_threshold  # 0.90 (from TRADING_CONFIGS.py)
SPIKE_METHOD = getattr(AGGRESSIVE_CONFIG, 'spike_method', 'FIXED')  # "EWMA_1000" (winner)

# -----------------------------------------------------------------------------
# BACKTEST-SPECIFIC PARAMS (not in TRADING_CONFIGS)
# -----------------------------------------------------------------------------
TARGET_SHARES = 50              # Backtest uses 50sh (live may use different for testing)
MIN_RUNTIME_SECS = 300          # 5 min market duration filter

# OU ADAPTIVE THRESHOLD params (match grid search exactly)
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10

# Expensive bid calculation (match grid search)
DROP_MULTIPLIER = 0.50
DROP_INTERCEPT = 0.08
TARGET_PAIR_COST = 0.99

# Cycling
MIN_CYCLE_GAP_MS = getattr(AGGRESSIVE_CONFIG, 'min_cycle_gap_ms', 50)  # 50ms (faster cycling)

# =============================================================================
# ENTRY FILL DELAY - Simulate realistic taker order execution
# =============================================================================
# In live trading, entry orders have a delay before filling:
# - 500ms Polymarket taker delay (exchange-enforced for crossing orders)
# - 42ms AWS Ireland → Polymarket network latency (from test_latency.py Feb 5, 2026)
# Total: 542ms from order submit to fill
#
# During this delay, the market moves. The backtest must simulate this by
# using the ask price AFTER the delay, not at spike detection time.
#
# Observer data is at ~5Hz (200ms intervals), so 542ms ≈ 2.7 rows ≈ 3 rows
ENTRY_FILL_DELAY_MS = 542  # Total taker delay (500ms exchange + 42ms network)
ENTRY_DELAY_ROWS = 3       # 542ms / 200ms = 2.7 → round to 3 rows (conservative)

# =============================================================================
# BREAKEVEN EXIT - Real-time profit threshold monitoring
# =============================================================================
# When enabled, checks every tick if spike_bid <= entry_price (breakeven/loss).
# If so, exits immediately at market (expensive_ask) instead of waiting for time-stop.
# This catches exits at ~$1.00 pair cost instead of $1.04 from 5s polling delay.
BREAKEVEN_EXIT_ENABLED = True  # Set False to compare with/without breakeven exit
BREAKEVEN_MIN_HOLD_MS = getattr(AGGRESSIVE_CONFIG, 'breakeven_min_hold_ms', 10000)  # From TRADING_CONFIGS (10s winner)
                               # FIXED Feb 5: Was hardcoded 2000ms, should be 10000ms from validated findings

# -----------------------------------------------------------------------------
# Z-SCORE VOLATILITY FILTER - CRITICAL DISCREPANCY!
# -----------------------------------------------------------------------------
# TRADING_CONFIGS has: z_lo=0.0, z_hi=1.5
# LIVE CODE (enhanced_spike.py) USES z-score filter to skip entries outside bounds
# GRID SEARCH (aggressive_grid_search.py) does NOT filter by z-score
#
# The Feb 1 validated results ($13.78/hr OOS7, $9.17/hr OOS8) were generated
# WITHOUT z-score filtering. To match those results, we also don't filter.
#
# TODO: If you want to TEST z-score filtering, uncomment the filter in
#       simulate_market_precomputed() and compare results.
# -----------------------------------------------------------------------------


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
# DATA CLASSES - Match grid search exactly
# =============================================================================

@dataclass
class TradeResult:
    market_slug: str
    cycle_num: int
    entry_time_remaining: float
    signal_score: float
    spike_side: str
    spike_fill_price: float
    expensive_fill_price: float
    hedge_type: str  # "passive", "time_stop", "resolution"
    pair_cost: float
    pnl_gross: float
    pnl_net: float
    entry_fee: float
    exit_fee: float
    correct_direction: bool
    spike_magnitude: float
    dataset: str
    offset_name: str = "CURRENT"
    cycle_mode: str = "SINGLE"
    shares: int = 50


# =============================================================================
# DATASET CONFIGURATION - Match grid search exactly
# =============================================================================

DATASETS = {
    "IS+OOS2": {
        "name": "IS+OOS2 (Jan 16-19)",
        "btc_file": "research/binance_hf/btc_prices_20260118_060340.csv",
        "obs_files": [
            "research/observer/grid_obs_20260116.csv",
            "research/observer/grid_obs_20260117.csv",
            "research/observer/grid_obs_20260118.csv",
            "research/observer/grid_obs_20260119.csv",
        ],
        "use_obi": True,  # Auto: uses OBI if columns exist
        "expected_hours": 23.0,
        "is_60hz": True,  # 87.1 Hz - include in spike testing
    },
    "OOS3+4": {
        "name": "OOS3+4 (Jan 22-24)",
        "btc_file": "research/observer/PROTECTED_btc_prices_oos3_oos4_combined.csv",
        "obs_files": [
            "research/observer/PROTECTED_grid_obs_oos3_oos4_combined.csv",
        ],
        "use_obi": True,
        "expected_hours": 47.0,
        "is_60hz": True,  # 84.9 Hz - include in spike testing
    },
    "OOS5": {
        "name": "OOS5 (Jan 26)",
        "btc_file": None,  # Will use observer binance_price at 5Hz
        "obs_files": [
            "research/observer/PROTECTED_grid_obs_oos5_recovered.csv",
        ],
        "use_obi": True,
        "expected_hours": 41.0,
        "is_60hz": False,  # Only 1.3 Hz - EXCLUDE from spike testing
    },
    "OOS7": {
        "name": "OOS7 (Jan 29-30)",
        "btc_file": "research/binance_hf/btc_prices_20260129_160523.csv",
        "obs_files": [
            "research/observer/grid_obs_20260129.csv",
            "research/observer/grid_obs_20260130.csv",
        ],
        "use_obi": True,
        "expected_hours": 19.0,
        "is_60hz": True,  # 185.9 Hz - include in spike testing
    },
    "OOS8": {
        "name": "OOS8 (Jan 31)",
        "btc_file": "research/binance_hf/btc_prices_20260131_055231.csv",
        "obs_files": [
            "research/observer/grid_obs_20260131.csv",
        ],
        "use_obi": True,
        "expected_hours": 24.0,
        "is_60hz": True,  # 197.3 Hz - include in spike testing
    },
    "OOS9.1": {
        "name": "OOS9.1 (Feb 1, 7.7h overlap - trending market)",
        "btc_file": "research/binance_hf/btc_prices_oos9_1.csv",
        "obs_files": [
            "research/observer/grid_obs_oos9_1.csv",
        ],
        "use_obi": True,
        "expected_hours": 7.7,
        "is_60hz": True,  # 229.5 Hz - include in spike testing
    },
    "OOS9.2": {
        "name": "OOS9.2 (Feb 2-3, 17.2h overlap)",
        "btc_file": "research/binance_hf/btc_prices_oos9_2.csv",
        "obs_files": [
            "research/observer/grid_obs_oos9_2.csv",
        ],
        "use_obi": True,
        "expected_hours": 17.2,
        "is_60hz": True,  # 60Hz+ - include in spike testing
    },
    "OOS9": {
        "name": "OOS9 (Feb 1-3, combined 9.1+9.2)",
        "btc_file": "research/binance_hf/btc_prices_oos9.csv",
        "obs_files": [
            "research/observer/grid_obs_oos9.csv",
        ],
        "use_obi": True,
        "expected_hours": 25.0,
        "is_60hz": True,  # 60Hz+ - include in spike testing
    },
    "OOS10.1": {
        "name": "OOS10.1 (Feb 5, paper trading validation)",
        "btc_file": "research/binance_hf/btc_prices_20260204_190733.csv",
        "obs_files": [
            "research/observer/grid_obs_20260205.csv",
        ],
        "resolutions_file": "research/observer/resolutions_20260205.csv",
        "use_obi": True,
        "expected_hours": 2.6,
        "is_60hz": True,
    },
    "OOS10.2": {
        "name": "OOS10.2 (Feb 5, 2hr session 04:00-06:00 UTC)",
        "btc_file": "research/binance_hf/btc_prices_oos10_2.csv",
        "obs_files": [
            "research/observer/grid_obs_oos10_2.csv",
        ],
        "resolutions_file": "research/observer/resolutions_oos10_2.csv",
        "use_obi": True,
        "expected_hours": 2.0,
        "is_60hz": True,
    },
}

# Filter to 60Hz-only datasets for EWMA spike testing
DATASETS_60HZ = {k: v for k, v in DATASETS.items() if v.get('is_60hz', True)}


# =============================================================================
# SPIKE DETECTION - VECTORIZED PRECOMPUTATION (matches grid search)
# =============================================================================

def precompute_spikes_fixed(btc_df: pd.DataFrame, lookback: int = SPIKE_LOOKBACK) -> pd.DataFrame:
    """Vectorized spike detection with FIXED lookback + OU ADAPTIVE threshold."""
    print(f"    Using FIXED lookback ({lookback} ticks) + OU ADAPTIVE threshold")
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
    print(f"    Found {spike_count:,} spikes (FIXED + OU adaptive)")

    return df


def precompute_spikes_ewma(btc_df: pd.DataFrame, halflife_ms: int) -> pd.DataFrame:
    """EWMA: Compare current price to exponentially weighted moving average.

    Key advantage: After a spike, the EWMA adapts, reducing redundant signals
    from the same price move. One price move → one spike (not 14 spikes).

    Gap handling: When there's a gap > 30 minutes, reset EWMA to current price
    to avoid false spikes from stale EWMA values.
    """
    halflife_ticks = halflife_ms / 16.67  # ~60Hz data
    alpha = 1 - 0.5 ** (1.0 / halflife_ticks)
    gap_threshold_ms = 30 * 60 * 1000  # 30 minutes

    print(f"    [EWMA_{halflife_ms}] Half-life={halflife_ms}ms, α={alpha:.4f}")

    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    # Deduplicate by timestamp to ensure consistent spike detection
    original_len = len(df)
    df = df.drop_duplicates(subset=['timestamp_ms'], keep='first').reset_index(drop=True)
    if len(df) < original_len:
        print(f"    [EWMA_{halflife_ms}] Deduplicated: {original_len:,} → {len(df):,} rows")

    # Compute EWMA of price with gap detection
    prices = df['price'].values
    timestamps = df['timestamp_ms'].values
    ewma_prices = np.zeros(len(prices))
    ewma_prices[0] = prices[0]

    gap_count = 0
    for i in range(1, len(prices)):
        time_diff = timestamps[i] - timestamps[i-1]
        if time_diff > gap_threshold_ms:
            # Gap detected - reset EWMA to current price
            ewma_prices[i] = prices[i]
            gap_count += 1
        else:
            ewma_prices[i] = alpha * prices[i] + (1 - alpha) * ewma_prices[i-1]

    if gap_count > 0:
        print(f"    [EWMA_{halflife_ms}] Reset EWMA at {gap_count} gap(s) > 30min")

    df['ewma_price'] = ewma_prices
    df['change_pct'] = (df['price'] - df['ewma_price']) / df['ewma_price'] * 100
    df['spike_magnitude'] = df['change_pct'].abs()

    # OU adaptive threshold (same as FIXED) - also reset on gaps
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
        # Reset variance on gaps
        if i > 0 and (timestamps[i] - timestamps[i-1]) > gap_threshold_ms:
            variance = 0.01  # Reset to default
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
    print(f"    [EWMA_{halflife_ms}] Found {spike_count:,} spikes")

    return df


def precompute_spikes(btc_df: pd.DataFrame, method: str) -> pd.DataFrame:
    """Dispatch to appropriate spike detection method."""
    if method == "FIXED":
        return precompute_spikes_fixed(btc_df)
    elif method.startswith("EWMA_"):
        halflife_ms = int(method.split("_")[1])
        return precompute_spikes_ewma(btc_df, halflife_ms)
    else:
        raise ValueError(f"Unknown spike method: {method}")


# Legacy alias for backward compatibility
def precompute_spikes_ou(btc_df: pd.DataFrame, lookback: int = SPIKE_LOOKBACK) -> pd.DataFrame:
    """Legacy: Alias for precompute_spikes_fixed."""
    return precompute_spikes_fixed(btc_df, lookback)


def calculate_expensive_bid(spike_entry: float, spike_magnitude: float) -> float:
    """Calculate expensive bid - matches grid search TestConfig.calculate_expensive_bid()."""
    expected_drop = DROP_MULTIPLIER * spike_magnitude + DROP_INTERCEPT
    max_expensive = TARGET_PAIR_COST - spike_entry
    expensive_bid = min((1.0 - spike_entry) - expected_drop, max_expensive)
    return max(0.01, min(0.95, expensive_bid))


# =============================================================================
# SIMULATION - Single cycle, matches grid search simulate_market_single() exactly
# =============================================================================

def simulate_market_precomputed(btc_spikes: pd.DataFrame, obs_df: pd.DataFrame,
                                 slug: str, resolution: str,
                                 use_obi_filter: bool, dataset_name: str) -> List[TradeResult]:
    """
    Single-cycle simulation - MATCHES aggressive_grid_search.py exactly.

    Key: NO z-score filtering (grid search doesn't filter by z-score).
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
    last_hedge_ts = 0
    in_position = False
    position_data = None
    time_stop_ms = TIME_STOP_SECONDS * 1000

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

                expensive_side = position_data['expensive_side']
                expensive_target = position_data['expensive_target']
                spike_entry = position_data['spike_entry']
                spike_mag = position_data['spike_magnitude']
                score = position_data['score']

                if expensive_side == "UP":
                    expensive_ask = obs_row['up_ask']
                else:
                    expensive_ask = obs_row['down_ask']

                # Check passive fill: when market ask drops to our bid, we get filled at OUR bid price
                if pd.notna(expensive_ask) and expensive_ask <= expensive_target:
                    pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                        spike_entry, expensive_target, TARGET_SHARES,
                        is_taker_entry=True,
                        is_taker_exit=False
                    )

                    trades.append(TradeResult(
                        market_slug=slug,
                        cycle_num=cycle_num,
                        entry_time_remaining=position_data['entry_time_rem'],
                        signal_score=score,
                        spike_side=position_data['spike_side'],
                        spike_fill_price=spike_entry,
                        expensive_fill_price=expensive_target,
                        hedge_type="passive",
                        pair_cost=spike_entry + expensive_target,
                        pnl_gross=pnl_gross,
                        pnl_net=pnl_net,
                        entry_fee=entry_fee,
                        exit_fee=exit_fee,
                        correct_direction=(resolution == position_data['spike_side']),
                        spike_magnitude=spike_mag,
                        dataset=dataset_name,
                        offset_name="CURRENT",
                        cycle_mode="SINGLE",
                        shares=TARGET_SHARES,
                    ))

                    in_position = False
                    position_data = None
                    last_hedge_ts = obs_ts
                    obs_idx += 1
                    break

                # =========================================================
                # BREAKEVEN EXIT: Check if spike_bid <= entry_price
                # This catches the exact moment we hit breakeven/loss
                # Exit immediately at market to get ~$1.00 pair cost
                # instead of waiting for time-stop at $1.04
                # =========================================================
                elapsed_ms = obs_ts - entry_ts
                if BREAKEVEN_EXIT_ENABLED and elapsed_ms >= BREAKEVEN_MIN_HOLD_MS:
                    spike_side_current = position_data['spike_side']
                    if spike_side_current == "UP":
                        spike_bid_current = obs_row['up_bid']
                    else:
                        spike_bid_current = obs_row['down_bid']

                    # Breakeven = spike_bid <= entry (not strictly less - includes equal)
                    if pd.notna(spike_bid_current) and spike_bid_current <= spike_entry:
                        # Exit at market (expensive_ask) - same as time-stop but triggered earlier
                        expensive_fill = expensive_ask if pd.notna(expensive_ask) else expensive_target * 1.05
                        pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                            spike_entry, expensive_fill, TARGET_SHARES,
                            is_taker_entry=True,
                            is_taker_exit=True  # Market order = taker
                        )

                        trades.append(TradeResult(
                            market_slug=slug,
                            cycle_num=cycle_num,
                            entry_time_remaining=position_data['entry_time_rem'],
                            signal_score=score,
                            spike_side=position_data['spike_side'],
                            spike_fill_price=spike_entry,
                            expensive_fill_price=expensive_fill,
                            hedge_type="breakeven",  # New hedge type
                            pair_cost=spike_entry + expensive_fill,
                            pnl_gross=pnl_gross,
                            pnl_net=pnl_net,
                            entry_fee=entry_fee,
                            exit_fee=exit_fee,
                            correct_direction=(resolution == position_data['spike_side']),
                            spike_magnitude=spike_mag,
                            dataset=dataset_name,
                            offset_name="CURRENT",
                            cycle_mode="SINGLE",
                            shares=TARGET_SHARES,
                        ))

                        in_position = False
                        position_data = None
                        last_hedge_ts = obs_ts
                        obs_idx += 1
                        break

                # Check time-stop (ONLY if NOT in profit) - matches grid search
                # elapsed_ms already calculated above for breakeven check
                if time_stop_ms > 0 and elapsed_ms >= time_stop_ms:
                    spike_side_current = position_data['spike_side']
                    if spike_side_current == "UP":
                        spike_bid_current = obs_row['up_bid']
                    else:
                        spike_bid_current = obs_row['down_bid']

                    in_profit = pd.notna(spike_bid_current) and spike_bid_current >= spike_entry

                    if not in_profit:
                        expensive_fill = expensive_ask if pd.notna(expensive_ask) else expensive_target * 1.05
                        pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                            spike_entry, expensive_fill, TARGET_SHARES,
                            is_taker_entry=True,
                            is_taker_exit=True
                        )

                        trades.append(TradeResult(
                            market_slug=slug,
                            cycle_num=cycle_num,
                            entry_time_remaining=position_data['entry_time_rem'],
                            signal_score=score,
                            spike_side=position_data['spike_side'],
                            spike_fill_price=spike_entry,
                            expensive_fill_price=expensive_fill,
                            hedge_type="time_stop",
                            pair_cost=spike_entry + expensive_fill,
                            pnl_gross=pnl_gross,
                            pnl_net=pnl_net,
                            entry_fee=entry_fee,
                            exit_fee=exit_fee,
                            correct_direction=(resolution == position_data['spike_side']),
                            spike_magnitude=spike_mag,
                            dataset=dataset_name,
                            offset_name="CURRENT",
                            cycle_mode="SINGLE",
                            shares=TARGET_SHARES,
                        ))

                        in_position = False
                        position_data = None
                        last_hedge_ts = obs_ts
                        obs_idx += 1
                        break

                obs_idx += 1

            # If we ran out of observer data while in position
            if in_position and obs_idx >= len(mdf):
                spike_side = position_data['spike_side']
                spike_entry = position_data['spike_entry']
                shares = TARGET_SHARES

                entry_fee = polymarket_taker_fee(spike_entry) * spike_entry * shares

                if resolution == spike_side:
                    pnl_gross = (1.0 - spike_entry) * shares
                    expensive_fill = 0.0
                else:
                    pnl_gross = (0.0 - spike_entry) * shares
                    expensive_fill = 1.0

                pnl_net = pnl_gross - entry_fee

                trades.append(TradeResult(
                    market_slug=slug,
                    cycle_num=cycle_num,
                    entry_time_remaining=position_data['entry_time_rem'],
                    signal_score=position_data['score'],
                    spike_side=spike_side,
                    spike_fill_price=spike_entry,
                    expensive_fill_price=expensive_fill,
                    hedge_type="resolution",
                    pair_cost=spike_entry + expensive_fill,
                    pnl_gross=pnl_gross,
                    pnl_net=pnl_net,
                    entry_fee=entry_fee,
                    exit_fee=0.0,
                    correct_direction=(resolution == spike_side),
                    spike_magnitude=position_data['spike_magnitude'],
                    dataset=dataset_name,
                    offset_name="CURRENT",
                    cycle_mode="SINGLE",
                    shares=shares,
                ))
                break

            continue

        # STATE 2: Not in position - check next spike
        if spike_idx >= len(market_spikes):
            break

        spike_row = market_spikes.iloc[spike_idx]
        spike_ts = spike_row['timestamp_ms']
        spike_dir = spike_row['spike_direction']
        spike_mag = spike_row['spike_magnitude']

        # NOTE: NO z-score filter here - grid search doesn't filter by z-score
        # z_score is computed but not used for entry filtering

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

        # Skip if too close to end
        if time_rem < MIN_TIME:
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
        spike_side = spike_dir
        if spike_side == "UP":
            spike_ask = obs_row['up_ask']
            expensive_bid = obs_row.get('down_bid', None)
            expensive_ask = obs_row.get('down_ask', None)
            obi_spike = obs_row.get('up_imbalance', None)
        else:
            spike_ask = obs_row['down_ask']
            expensive_bid = obs_row.get('up_bid', None)
            expensive_ask = obs_row.get('up_ask', None)
            obi_spike = obs_row.get('down_imbalance', None)

        if pd.isna(spike_ask) or spike_ask >= HIGH_ENTRY_THRESHOLD:
            spike_idx += 1
            continue

        # Enhanced OBI filter (uses expensive spread, time remaining, OBI magnitude)
        if use_obi_filter:
            if obi_spike is not None and not np.isnan(obi_spike):
                # Calculate expensive spread
                expensive_spread = 0.05  # Default
                if pd.notna(expensive_bid) and pd.notna(expensive_ask):
                    expensive_spread = expensive_ask - expensive_bid

                should_take, reject_reason = should_take_spike_enhanced(
                    spike_direction=spike_dir,
                    obi_spike=obi_spike,
                    expensive_spread=expensive_spread,
                    time_remaining=time_rem,
                    spike_ask_depth=None,  # Depth not available in observer data
                )
                if not should_take:
                    spike_idx += 1
                    continue

        # ENTRY - use local expensive bid calculation (matches grid search)
        # ENTRY DELAY FIX (Feb 5, 2026): Simulate 542ms taker delay
        # Look ahead in observer data to get the fill price AFTER delay
        delayed_obs_idx = min(obs_idx + ENTRY_DELAY_ROWS, len(mdf) - 1)
        delayed_obs_row = mdf.iloc[delayed_obs_idx]

        # Get the delayed fill price (what we actually pay after 542ms)
        if spike_side == "UP":
            spike_ask_delayed = delayed_obs_row['up_ask']
        else:
            spike_ask_delayed = delayed_obs_row['down_ask']

        # SKIP RULE RE-CHECK: If delayed price is too high, reject entry
        # This matches paper trading behavior where fills are rejected if
        # market moves against us during the delay
        if pd.isna(spike_ask_delayed) or spike_ask_delayed >= HIGH_ENTRY_THRESHOLD:
            spike_idx += 1
            continue

        cycle_num += 1
        expensive_side = "DOWN" if spike_side == "UP" else "UP"
        spike_entry = spike_ask_delayed  # USE DELAYED PRICE, not instant
        expensive_target = calculate_expensive_bid(spike_entry, spike_mag)

        in_position = True
        position_data = {
            'spike_side': spike_side,
            'expensive_side': expensive_side,
            'spike_entry': spike_entry,
            'expensive_target': expensive_target,
            'entry_ts': spike_ts,
            'entry_time_rem': time_rem,
            'spike_magnitude': spike_mag,
            'score': score,
        }

        spike_idx += 1

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

    # Load observer data
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

    # Load BTC data
    btc_df = None
    if config['btc_file']:
        btc_path = base_dir / config['btc_file']
        if btc_path.exists():
            btc_df = pd.read_csv(btc_path)
            print(f"  Binance HF: {len(btc_df):,} rows")
        else:
            print(f"  Binance HF: NOT FOUND - will use observer binance_price")

    # If no 60Hz data, create from observer binance_price (5Hz)
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

    # Load resolutions - use custom file if specified, else default
    if 'resolutions_file' in config:
        res_path = base_dir / config['resolutions_file']
        res_df = pd.read_csv(res_path)
        # OOS10+ format: market_slug, resolution
        if 'market_slug' in res_df.columns:
            res_map = dict(zip(res_df['market_slug'], res_df['resolution']))
        else:
            res_map = dict(zip(res_df['slug'], res_df['winner']))
        print(f"  Resolutions: {res_path.name} ({len(res_map)} markets)")
    else:
        res_path = base_dir / "research/observer/market_resolutions_verified.csv"
        res_df = pd.read_csv(res_path)
        res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Find overlap
    btc_start, btc_end = btc_df['timestamp_ms'].min(), btc_df['timestamp_ms'].max()
    obs_start, obs_end = obs_df['timestamp_ms'].min(), obs_df['timestamp_ms'].max()

    overlap_start = max(btc_start, obs_start)
    overlap_end = min(btc_end, obs_end)

    # Calculate ACTUAL trading hours (accounting for gaps)
    # Sort timestamps and find gaps > 30 minutes
    sorted_ts = np.sort(obs_df['timestamp_ms'].unique())
    if len(sorted_ts) > 1:
        diffs = np.diff(sorted_ts)
        gap_threshold_ms = 30 * 60 * 1000  # 30 minutes
        gaps = diffs[diffs > gap_threshold_ms]
        total_gap_ms = gaps.sum() if len(gaps) > 0 else 0

        # Actual hours = span - gaps
        span_ms = sorted_ts[-1] - sorted_ts[0]
        actual_ms = span_ms - total_gap_ms
        duration_hours = actual_ms / 3600000

        if len(gaps) > 0:
            print(f"  Span: {span_ms/3600000:.2f}h, Gaps: {total_gap_ms/3600000:.2f}h ({len(gaps)} gaps)")
            print(f"  Actual trading hours: {duration_hours:.2f}h")
        else:
            print(f"  Overlap: {duration_hours:.2f} hours (no gaps)")
    else:
        duration_hours = 0
        print(f"  Overlap: 0 hours (no data)")

    # Filter
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

    print(f"  Valid markets: {len(valid_slugs)}")

    return btc_df, obs_df, res_map, duration_hours


def run_backtest_dataset(dataset_key: str) -> Tuple[List[TradeResult], float]:
    """Run backtest on a single dataset with PRECOMPUTED spikes."""
    config = DATASETS[dataset_key]
    btc_df, obs_df, res_map, hours = load_dataset(dataset_key)

    if btc_df is None or obs_df is None or len(obs_df) == 0:
        print(f"  Skipping {dataset_key} - no valid data")
        return [], 0

    # PRECOMPUTE SPIKES with configured method (EWMA_1000 winner)
    print(f"  Precomputing spikes with method={SPIKE_METHOD}...")
    btc_spikes = precompute_spikes(btc_df, SPIKE_METHOD)

    use_obi = config['use_obi']
    print(f"  Running simulation (OBI {'ON' if use_obi else 'OFF'})...")

    all_trades = []
    slugs = obs_df['market_slug'].unique()

    for slug in tqdm(slugs, desc=config['name']):
        resolution = res_map.get(slug)
        if resolution not in ['UP', 'DOWN']:
            continue

        trades = simulate_market_precomputed(btc_spikes, obs_df, slug, resolution, use_obi, dataset_key)
        all_trades.extend(trades)

    return all_trades, hours


def compute_deep_metrics(trades: List[TradeResult], hours: float, dataset_name: str) -> dict:
    """
    Compute comprehensive risk metrics for autonomous trading decisions.

    Returns metrics required by CLAUDE_MISTAKES.md MANDATORY ANALYSIS METRICS:
    - Sharpe ratio (> 1.0 minimum, > 1.5 strong)
    - Profitable market % (> 50% minimum)
    - Worst single trade (> -$10)
    - Worst single market
    - Taker exit % - lower is better
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

    # Taker exit % (time_stop, breakeven, stop_loss, resolution = taker exit)
    taker_exit_count = trades_df['hedge_type'].isin(['time_stop', 'breakeven', 'stop_loss', 'resolution']).sum()
    taker_exit_pct = (taker_exit_count / len(trades_df) * 100) if len(trades_df) > 0 else 0

    # Passive fill stats
    passive_pct = (trades_df['hedge_type'] == 'passive').sum() / len(trades_df) * 100

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
    }


def print_results(trades: List[TradeResult], dataset_key: str, hours: float):
    """Print results for a dataset."""
    if not trades:
        print(f"\n{dataset_key}: No trades")
        return None

    config = DATASETS[dataset_key]
    df = pd.DataFrame([t.__dict__ for t in trades])

    total_pnl_gross = df['pnl_gross'].sum()
    total_pnl_net = df['pnl_net'].sum()
    total_fees = df['entry_fee'].sum() + df['exit_fee'].sum()
    total_trades = len(df)
    win_rate = df['correct_direction'].mean() * 100
    avg_pair_cost = df['pair_cost'].mean()
    hourly_rate = total_pnl_net / hours if hours > 0 else 0

    passive = (df['hedge_type'] == 'passive').sum()
    time_stop = (df['hedge_type'] == 'time_stop').sum()
    breakeven = (df['hedge_type'] == 'breakeven').sum()
    resolution = (df['hedge_type'] == 'resolution').sum()

    # Compute deep metrics
    deep_metrics = compute_deep_metrics(trades, hours, dataset_key)

    print(f"\n{'='*60}")
    print(f"RESULTS: {config['name']} (OBI {'ON' if config['use_obi'] else 'OFF'})")
    print(f"{'='*60}")
    print(f"Total trades: {total_trades}")
    print(f"Direction accuracy: {win_rate:.1f}%")
    print(f"PnL Gross: ${total_pnl_gross:.2f}")
    print(f"PnL Net:   ${total_pnl_net:.2f} (after ${total_fees:.2f} fees)")
    print(f"Hourly rate: ${hourly_rate:.2f}/hr")
    print(f"Avg pair cost: {avg_pair_cost:.4f}")
    print(f"Exit types: {passive} passive, {breakeven} breakeven, {time_stop} time-stop, {resolution} resolution")
    if deep_metrics:
        print(f"Sharpe: {deep_metrics['sharpe']:.2f}, ProfMkt: {deep_metrics['profitable_market_pct']:.1f}%")
        print(f"Max drawdown: ${deep_metrics['max_drawdown']:.2f} ({deep_metrics['max_drawdown_pct']:.1f}%)")
        print(f"Worst trade: ${deep_metrics['worst_trade_pnl']:.2f} in {deep_metrics['worst_trade_market']}")

    return {
        'dataset': dataset_key,
        'hours': hours,
        'trades': total_trades,
        'pnl_gross': total_pnl_gross,
        'pnl_net': total_pnl_net,
        'fees': total_fees,
        'hourly_rate': hourly_rate,
        'win_rate': win_rate,
        'avg_pair_cost': avg_pair_cost,
        'passive_pct': passive / total_trades * 100 if total_trades > 0 else 0,
        'sharpe': deep_metrics['sharpe'] if deep_metrics else 0,
        'max_drawdown': deep_metrics['max_drawdown'] if deep_metrics else 0,
        'max_drawdown_pct': deep_metrics['max_drawdown_pct'] if deep_metrics else 0,
        'profitable_market_pct': deep_metrics['profitable_market_pct'] if deep_metrics else 0,
        'worst_trade_pnl': deep_metrics['worst_trade_pnl'] if deep_metrics else 0,
    }


def main():
    """Run full backtest on 60Hz-only datasets."""
    print("=" * 80)
    print("AGGRESSIVE BACKTEST - EWMA Winner Config Validation")
    print("=" * 80)
    print(f"Config: SPIKE_METHOD={SPIKE_METHOD}, TIME_STOP={TIME_STOP_SECONDS}s, MIN_TIME={MIN_TIME}s")
    print(f"        LOOKBACK={SPIKE_LOOKBACK}, HIGH_ENTRY_THRESHOLD={HIGH_ENTRY_THRESHOLD}")
    print(f"        DROP_MULT={DROP_MULTIPLIER}, DROP_INT={DROP_INTERCEPT}")
    print(f"        MIN_CYCLE_GAP={MIN_CYCLE_GAP_MS}ms, TARGET_SHARES={TARGET_SHARES}")
    breakeven_status = f"ON (min_hold={BREAKEVEN_MIN_HOLD_MS}ms)" if BREAKEVEN_EXIT_ENABLED else "OFF"
    print(f"        BREAKEVEN_EXIT={breakeven_status}")
    print("=" * 80)
    print(f"Running on 60Hz-only datasets: {list(DATASETS_60HZ.keys())}")
    print(f"Excluding: {[k for k in DATASETS.keys() if k not in DATASETS_60HZ]}")
    print("=" * 80)

    # Load OU parameters for adaptive threshold
    load_ou_params()

    all_trades = []
    all_results = []
    total_hours = 0

    # Use 60Hz-only datasets for EWMA spike validation
    for dataset_key in DATASETS_60HZ.keys():
        trades, hours = run_backtest_dataset(dataset_key)
        if trades:
            result = print_results(trades, dataset_key, hours)
            if result:
                all_results.append(result)
            all_trades.extend(trades)
            total_hours += hours

    # Overall summary
    if all_trades:
        df = pd.DataFrame([t.__dict__ for t in all_trades])

        print(f"\n{'='*80}")
        print("OVERALL SUMMARY (All Datasets)")
        print(f"{'='*80}")
        print(f"Total hours: {total_hours:.1f}")
        print(f"Total trades: {len(df)}")
        print(f"Total PnL Net: ${df['pnl_net'].sum():.2f}")
        print(f"Overall hourly rate: ${df['pnl_net'].sum() / total_hours:.2f}/hr")
        print(f"Overall win rate: {df['correct_direction'].mean() * 100:.1f}%")

        # Save results
        output_path = Path("research/findings/data/aggressive_main_backtest_results.csv")
        df.to_csv(output_path, index=False)
        print(f"\nResults saved to: {output_path}")

        # Save summary
        summary_df = pd.DataFrame(all_results)
        summary_path = Path("research/findings/data/aggressive_main_backtest_summary.csv")
        summary_df.to_csv(summary_path, index=False)
        print(f"Summary saved to: {summary_path}")


if __name__ == "__main__":
    main()
