#!/usr/bin/env python3
"""
AGGRESSIVE_M (V2) EWMA Study - FADE the Spike When Market Disagrees

Purpose: Test FOLLOW vs FADE accuracy using the EXACT same spike detection
and filtering as AGGRESSIVE taker mode (EWMA 1000ms + OU adaptive threshold).

Key Finding:
- AGGRESSIVE_M V1: FOLLOW spike as maker → 4-7pp adverse selection
- AGGRESSIVE_M V2: FADE spike when expensive_side >= $0.65 → 90% accuracy

This study uses `precompute_spikes_ewma()` from aggressive_main_backtest.py
which matches the exact spike detection used in AGGRESSIVE backtest/live.

Filter Chain (matches AGGRESSIVE exactly):
1. EWMA spike detection with 1000ms halflife
2. OU adaptive threshold (calibrated on IS+OOS2)
3. Time remaining >= MIN_TIME (90s)
4. Velocity confirmation
5. Enhanced score >= threshold
6. spike_ask < HIGH_ENTRY_THRESHOLD (0.90)
7. Enhanced OBI filter (if available)
8. 30s cooldown deduplication per (market, direction) - per CLAUDE_MISTAKES.md #50

For each filtered signal, we measure:
- FOLLOW accuracy: Did spike direction predict winner?
- FADE accuracy: Did OPPOSITE of spike direction predict winner?
- Segmented by expensive_side price (key insight: >= $0.65 → 90% FADE accuracy)
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
# IMPORT FROM EXISTING CODE (per CLAUDE_MISTAKES.md #33)
# =============================================================================
from src.core import (
    velocity_confirms_spike,
    should_take_spike_enhanced,
    compute_enhanced_score,
    ENHANCED_SCORE_THRESHOLD,
)

from research.reference.TRADING_CONFIGS import AGGRESSIVE as AGGRESSIVE_CONFIG

# Import EWMA spike detection from the validated backtest
from research.backtests.aggressive_main_backtest import (
    precompute_spikes_ewma,
    load_ou_params,
    compute_ou_threshold,
    MIN_TIME,
    HIGH_ENTRY_THRESHOLD,
    SPIKE_METHOD,
)

# =============================================================================
# CONFIGURATION
# =============================================================================
EWMA_HALFLIFE_MS = 1000  # Match AGGRESSIVE

# Deduplication cooldowns to test (per CLAUDE_MISTAKES.md #50)
# Signals cluster in bursts - 98.5% within 5s of each other
# Compare quality (accuracy) vs quantity (signal count)
COOLDOWN_VALUES = [10, 30]

# Expensive_side price thresholds to test
EXPENSIVE_THRESHOLDS = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75]

# =============================================================================
# DATASETS
# =============================================================================
DATASETS = {
    "IS+OOS2": {
        "name": "IS+OOS2 (Jan 16-19) - LOW volatility",
        "btc_file": "research/binance_hf/btc_prices_20260118_060340.csv",
        "obs_files": [
            "research/observer/grid_obs_20260116.csv",
            "research/observer/grid_obs_20260117.csv",
            "research/observer/grid_obs_20260118.csv",
            "research/observer/grid_obs_20260119.csv",
        ],
        "use_obi": True,
        "is_60hz": True,
    },
    "OOS3+4": {
        "name": "OOS3+4 (Jan 22-24) - MIXED volatility",
        "btc_file": "research/observer/PROTECTED_btc_prices_oos3_oos4_combined.csv",
        "obs_files": [
            "research/observer/PROTECTED_grid_obs_oos3_oos4_combined.csv",
        ],
        "use_obi": True,
        "is_60hz": True,
    },
    "OOS7": {
        "name": "OOS7 (Jan 29-30) - HIGH volatility",
        "btc_file": "research/binance_hf/btc_prices_20260129_160523.csv",
        "obs_files": [
            "research/observer/grid_obs_20260129.csv",
            "research/observer/grid_obs_20260130.csv",
        ],
        "use_obi": True,
        "is_60hz": True,
    },
}


def load_dataset(dataset_key: str) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame], Dict[str, str]]:
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
        return None, None, {}

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

    # If no 60Hz data, use observer binance_price (5Hz)
    if btc_df is None:
        if 'binance_price' in obs_df.columns:
            btc_df = obs_df[['timestamp_ms', 'binance_price']].copy()
            btc_df = btc_df.rename(columns={'binance_price': 'price'})
            btc_df = btc_df.dropna()
            btc_df = btc_df.drop_duplicates(subset='timestamp_ms')
            print(f"  Using observer binance_price: {len(btc_df):,} rows (5Hz)")
        else:
            return None, None, {}

    # Load resolutions
    res_path = base_dir / "research/observer/market_resolutions_verified.csv"
    if res_path.exists():
        res_df = pd.read_csv(res_path)
        res_map = dict(zip(res_df['slug'], res_df['winner']))
        print(f"  Resolutions: {len(res_map)} markets")
    else:
        res_map = {}

    return btc_df, obs_df, res_map


@dataclass
class SignalResult:
    """Result for a single filtered signal."""
    market_slug: str
    spike_ts: int
    spike_direction: str  # "UP" or "DOWN"
    resolution: str  # Market outcome
    spike_ask: float  # Entry price if FOLLOW
    expensive_ask: float  # Entry price if FADE
    time_remaining: float
    enhanced_score: float
    velocity_bps: float
    follow_correct: bool  # Did FOLLOW predict correctly?
    fade_correct: bool  # Did FADE predict correctly?


def analyze_aggressive_m_v2_signals(btc_df: pd.DataFrame, obs_df: pd.DataFrame,
                                res_map: Dict[str, str], dataset_name: str,
                                use_obi: bool = True,
                                cooldown_seconds: int = 30) -> List[SignalResult]:
    """
    Apply AGGRESSIVE filter chain and measure FOLLOW vs FADE accuracy.

    This exactly replicates the filtering from aggressive_main_backtest.py
    but instead of executing trades, we just record the signal and outcome.
    """
    # Step 1: Compute EWMA spikes (same as AGGRESSIVE backtest)
    print(f"\n  Detecting spikes with EWMA_{EWMA_HALFLIFE_MS}...")
    btc_spikes = precompute_spikes_ewma(btc_df, EWMA_HALFLIFE_MS)

    # Get unique markets
    markets = obs_df['market_slug'].unique()
    print(f"  Markets: {len(markets)}")

    results = []

    for slug in tqdm(markets, desc=f"Analyzing {dataset_name}"):
        resolution = res_map.get(slug)
        if resolution not in ['UP', 'DOWN']:
            continue

        mdf = obs_df[obs_df['market_slug'] == slug].copy()
        mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

        if len(mdf) == 0:
            continue

        market_start = mdf['timestamp_ms'].min()
        market_end = mdf['timestamp_ms'].max()

        # Get spikes within market time
        market_spikes = btc_spikes[
            (btc_spikes['timestamp_ms'] >= market_start) &
            (btc_spikes['timestamp_ms'] <= market_end) &
            (btc_spikes['spike_detected'] == True)
        ].copy()

        if len(market_spikes) == 0:
            continue

        obs_idx = 0
        cooldown_ms = cooldown_seconds * 1000

        # Track last signal time per direction for deduplication (per CLAUDE_MISTAKES.md #50)
        last_signal_ts = {'UP': 0, 'DOWN': 0}

        for _, spike_row in market_spikes.iterrows():
            spike_ts = spike_row['timestamp_ms']
            spike_dir = spike_row['spike_direction']
            spike_mag = spike_row['spike_magnitude']

            # Find nearest observer row
            while obs_idx < len(mdf) - 1 and mdf.iloc[obs_idx + 1]['timestamp_ms'] <= spike_ts:
                obs_idx += 1

            if obs_idx >= len(mdf):
                break

            obs_row = mdf.iloc[obs_idx]
            time_rem = obs_row['time_remaining_secs']
            velocity_bps = obs_row.get('velocity_bps', 0.0) or 0.0

            # ===== FILTER 1: Time remaining =====
            if time_rem < MIN_TIME:
                continue

            # ===== FILTER 2: Velocity confirmation =====
            if not velocity_confirms_spike(spike_dir, velocity_bps):
                continue

            # ===== FILTER 3: Enhanced score =====
            score = compute_enhanced_score(spike_mag, velocity_bps, spike_dir, time_rem)
            if score < ENHANCED_SCORE_THRESHOLD:
                continue

            # ===== Get prices =====
            if spike_dir == "UP":
                spike_ask = obs_row['up_ask']
                expensive_ask = obs_row['down_ask']
                expensive_bid = obs_row.get('down_bid', None)
                obi_spike = obs_row.get('up_imbalance', None)
            else:
                spike_ask = obs_row['down_ask']
                expensive_ask = obs_row['up_ask']
                expensive_bid = obs_row.get('up_bid', None)
                obi_spike = obs_row.get('down_imbalance', None)

            # ===== FILTER 4: High entry threshold =====
            if pd.isna(spike_ask) or spike_ask >= HIGH_ENTRY_THRESHOLD:
                continue

            if pd.isna(expensive_ask):
                continue

            # ===== FILTER 5: Enhanced OBI filter =====
            if use_obi and obi_spike is not None and not np.isnan(obi_spike):
                expensive_spread = 0.05
                if pd.notna(expensive_bid) and pd.notna(expensive_ask):
                    expensive_spread = expensive_ask - expensive_bid

                should_take, _ = should_take_spike_enhanced(
                    spike_direction=spike_dir,
                    obi_spike=obi_spike,
                    expensive_spread=expensive_spread,
                    time_remaining=time_rem,
                    spike_ask_depth=None,
                )
                if not should_take:
                    continue

            # ===== SIGNAL PASSED ALL FILTERS =====

            # ===== DEDUPLICATION (per CLAUDE_MISTAKES.md #50) =====
            # Signals cluster in bursts - apply 30s cooldown per direction
            if spike_ts - last_signal_ts[spike_dir] < cooldown_ms:
                continue  # Skip duplicate signal

            # Update last signal time for this direction
            last_signal_ts[spike_dir] = spike_ts

            # Now check FOLLOW vs FADE accuracy
            follow_correct = (spike_dir == resolution)  # FOLLOW: buy winner side
            fade_correct = (spike_dir != resolution)    # FADE: buy expensive_side (opposite of spike)

            results.append(SignalResult(
                market_slug=slug,
                spike_ts=spike_ts,
                spike_direction=spike_dir,
                resolution=resolution,
                spike_ask=spike_ask,
                expensive_ask=expensive_ask,
                time_remaining=time_rem,
                enhanced_score=score,
                velocity_bps=velocity_bps,
                follow_correct=follow_correct,
                fade_correct=fade_correct,
            ))

    return results


def print_results(results: List[SignalResult], dataset_name: str):
    """Print analysis of results."""
    if not results:
        print(f"\n{dataset_name}: No signals passed filters")
        return

    df = pd.DataFrame([vars(r) for r in results])

    print(f"\n{'='*70}")
    print(f"AGGRESSIVE_M V2 STUDY RESULTS: {dataset_name}")
    print(f"{'='*70}")

    n = len(df)
    follow_acc = df['follow_correct'].mean() * 100
    fade_acc = df['fade_correct'].mean() * 100

    print(f"\n--- WITH ALL AGGRESSIVE FILTERS (EWMA + OU) ---")
    print(f"Total signals: {n:,}")
    print(f"FOLLOW accuracy: {follow_acc:.1f}%")
    print(f"FADE accuracy: {fade_acc:.1f}%")
    print(f"Edge: {'FOLLOW' if follow_acc > fade_acc else 'FADE'} by {abs(follow_acc - fade_acc):.1f}pp")

    # Segment by expensive_side price (key insight from previous analysis)
    print(f"\n--- FADE BY EXPENSIVE_SIDE PRICE (filtered signals) ---")
    print(f"{'Expensive Ask':>12} | {'Signals':>8} | {'FADE Acc':>10} | {'FOLLOW Acc':>10}")
    print("-" * 50)

    for threshold in EXPENSIVE_THRESHOLDS:
        subset = df[df['expensive_ask'] >= threshold]
        if len(subset) > 0:
            fade_pct = subset['fade_correct'].mean() * 100
            follow_pct = subset['follow_correct'].mean() * 100
            print(f">= ${threshold:.2f}     | {len(subset):>8} | {fade_pct:>9.1f}% | {follow_pct:>9.1f}%")

    # Segment by enhanced score
    print(f"\n--- BY ENHANCED SCORE ---")
    score_25 = df['enhanced_score'].quantile(0.25)
    score_50 = df['enhanced_score'].quantile(0.50)
    score_75 = df['enhanced_score'].quantile(0.75)

    print(f"{'Score Range':>15} | {'Signals':>8} | {'FADE Acc':>10} | {'FOLLOW Acc':>10}")
    print("-" * 55)

    for label, lo, hi in [('Low (<P25)', 0, score_25),
                           ('Medium (P25-P75)', score_25, score_75),
                           ('High (>P75)', score_75, 999)]:
        subset = df[(df['enhanced_score'] >= lo) & (df['enhanced_score'] < hi)]
        if len(subset) > 0:
            fade_pct = subset['fade_correct'].mean() * 100
            follow_pct = subset['follow_correct'].mean() * 100
            print(f"{label:>15} | {len(subset):>8} | {fade_pct:>9.1f}% | {follow_pct:>9.1f}%")

    return df


def main():
    """Run the AGGRESSIVE_M V2 study on all datasets with multiple cooldowns."""
    # Load OU parameters for adaptive threshold
    load_ou_params()

    # Store results by cooldown
    results_by_cooldown = {}

    # Load all datasets once
    loaded_datasets = {}
    for dataset_key in DATASETS.keys():
        btc_df, obs_df, res_map = load_dataset(dataset_key)
        if btc_df is not None and obs_df is not None:
            loaded_datasets[dataset_key] = (btc_df, obs_df, res_map)

    # Test each cooldown value
    for cooldown in COOLDOWN_VALUES:
        print(f"\n{'#'*70}")
        print(f"# TESTING COOLDOWN: {cooldown}s")
        print(f"{'#'*70}")

        all_dfs = []

        for dataset_key, (btc_df, obs_df, res_map) in loaded_datasets.items():
            results = analyze_aggressive_m_v2_signals(
                btc_df, obs_df, res_map, dataset_key,
                use_obi=DATASETS[dataset_key]['use_obi'],
                cooldown_seconds=cooldown
            )

            df = print_results(results, f"{dataset_key} (cooldown={cooldown}s)")
            if df is not None:
                df['dataset'] = dataset_key
                df['cooldown'] = cooldown
                all_dfs.append(df)

        if all_dfs:
            combined = pd.concat(all_dfs, ignore_index=True)
            results_by_cooldown[cooldown] = combined

            # Save per-cooldown results
            output_file = f"research/findings/data/aggressive_m_v2_cooldown_{cooldown}s.csv"
            combined.to_csv(output_file, index=False)
            print(f"\nSaved: {output_file}")

    # =========================================================================
    # COMPARISON: Quality vs Quantity
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"COOLDOWN COMPARISON: QUALITY vs QUANTITY")
    print(f"{'='*80}")

    print(f"\n{'Cooldown':>10} | {'Signals':>10} | {'FADE Acc':>10} | {'FOLLOW Acc':>10} | {'FADE @ $0.65':>12}")
    print("-" * 65)

    for cooldown in COOLDOWN_VALUES:
        if cooldown in results_by_cooldown:
            df = results_by_cooldown[cooldown]
            n = len(df)
            fade_acc = df['fade_correct'].mean() * 100
            follow_acc = df['follow_correct'].mean() * 100

            # FADE accuracy at $0.65 threshold
            subset_65 = df[df['expensive_ask'] >= 0.65]
            fade_65 = subset_65['fade_correct'].mean() * 100 if len(subset_65) > 0 else 0

            print(f"{cooldown:>8}s | {n:>10,} | {fade_acc:>9.1f}% | {follow_acc:>9.1f}% | {fade_65:>11.1f}%")

    # Detailed comparison by threshold
    print(f"\n--- FADE ACCURACY BY THRESHOLD (side-by-side) ---")
    print(f"{'Threshold':>12} |", end="")
    for cooldown in COOLDOWN_VALUES:
        print(f" {cooldown}s Signals | {cooldown}s FADE |", end="")
    print()
    print("-" * (15 + 25 * len(COOLDOWN_VALUES)))

    for threshold in EXPENSIVE_THRESHOLDS:
        print(f">= ${threshold:.2f}     |", end="")
        for cooldown in COOLDOWN_VALUES:
            if cooldown in results_by_cooldown:
                df = results_by_cooldown[cooldown]
                subset = df[df['expensive_ask'] >= threshold]
                n = len(subset)
                fade_acc = subset['fade_correct'].mean() * 100 if n > 0 else 0
                print(f" {n:>10,} | {fade_acc:>7.1f}% |", end="")
        print()

    # Recommendation
    print(f"\n{'='*80}")
    print("RECOMMENDATION")
    print(f"{'='*80}")

    if len(results_by_cooldown) >= 2:
        c10 = results_by_cooldown.get(10)
        c30 = results_by_cooldown.get(30)

        if c10 is not None and c30 is not None:
            n10, n30 = len(c10), len(c30)
            acc10 = c10['fade_correct'].mean() * 100
            acc30 = c30['fade_correct'].mean() * 100

            print(f"\n10s cooldown: {n10:,} signals at {acc10:.1f}% accuracy")
            print(f"30s cooldown: {n30:,} signals at {acc30:.1f}% accuracy")
            print(f"Volume difference: {n10 - n30:+,} signals ({(n10/n30 - 1)*100:+.1f}%)")
            print(f"Accuracy difference: {acc10 - acc30:+.1f}pp")

            if acc30 > acc10 + 2:
                print(f"\n→ 30s WINS by QUALITY (+{acc30 - acc10:.1f}pp accuracy)")
            elif n10 > n30 * 1.5 and acc10 >= acc10 - 2:
                print(f"\n→ 10s WINS by QUANTITY ({n10/n30:.1f}x more signals, similar accuracy)")
            else:
                print(f"\n→ SIMILAR - choose based on execution capacity")


if __name__ == "__main__":
    main()
