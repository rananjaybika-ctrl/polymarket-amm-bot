#!/usr/bin/env python3
"""
AGGRESSIVE_M V2 - OBI Filter Study WITH PROPER DEDUPLICATION

Tests whether INVERTING the OBI filter improves FADE accuracy.

Current OBI logic (for FOLLOW):
- OBI > 0 on spike_side = market confirms spike = TAKE

For FADE, we should INVERT:
- OBI <= 0 on spike_side = market skeptical of spike = BETTER FADE signal

This script tests:
1. NO_OBI: No OBI filter at all
2. OBI_FOLLOW: OBI > 0 on spike_side (current, designed for FOLLOW)
3. OBI_FADE: OBI <= 0 on spike_side (INVERTED for FADE)

ALL results use 30s cooldown for proper deduplication.
"""

import pandas as pd
import numpy as np
import sys
from pathlib import Path
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.core import velocity_confirms_spike, compute_enhanced_score, ENHANCED_SCORE_THRESHOLD
from research.backtests.aggressive_main_backtest import (
    precompute_spikes_ewma, load_ou_params, MIN_TIME, HIGH_ENTRY_THRESHOLD
)

BASE_DIR = Path("/Users/rananjaybika/polymarket-amm-bot")

# Deduplication cooldown (seconds)
COOLDOWN_SECONDS = 10

# Datasets to test
DATASETS = {
    "IS+OOS2": {
        "btc_file": "research/binance_hf/btc_prices_20260118_060340.csv",
        "obs_files": [
            "research/observer/grid_obs_20260116.csv",
            "research/observer/grid_obs_20260117.csv",
            "research/observer/grid_obs_20260118.csv",
            "research/observer/grid_obs_20260119.csv",
        ],
    },
    "OOS7": {
        "btc_file": "research/binance_hf/btc_prices_20260129_160523.csv",
        "obs_files": [
            "research/observer/grid_obs_20260129.csv",
            "research/observer/grid_obs_20260130.csv",
        ],
    },
}


def load_data(dataset_key: str):
    """Load BTC and observer data for a dataset."""
    config = DATASETS[dataset_key]

    # Load BTC data
    btc_path = BASE_DIR / config['btc_file']
    if not btc_path.exists():
        print(f"  BTC file not found: {btc_path}")
        return None, None, None
    btc_df = pd.read_csv(btc_path)

    # Load observer data
    obs_dfs = []
    for fname in config['obs_files']:
        fpath = BASE_DIR / fname
        if fpath.exists():
            df = pd.read_csv(fpath, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)

    if not obs_dfs:
        return None, None, None

    obs_df = pd.concat(obs_dfs, ignore_index=True)

    # Load resolutions
    res_path = BASE_DIR / "research/observer/market_resolutions_verified.csv"
    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    return btc_df, obs_df, res_map


def analyze_obi_strategies(btc_df, obs_df, res_map, dataset_name):
    """
    Analyze FADE accuracy with different OBI strategies.
    Uses 30s cooldown for proper deduplication.
    """
    print(f"\n{'='*60}")
    print(f"Analyzing {dataset_name}")
    print(f"{'='*60}")

    # Compute EWMA spikes
    print("  Computing EWMA spikes...")
    btc_spikes = precompute_spikes_ewma(btc_df, 1000)

    # Results by OBI strategy
    results = {
        'no_obi': [],
        'obi_follow': [],  # OBI > 0 (current)
        'obi_fade': [],    # OBI <= 0 (inverted for FADE)
    }

    markets = obs_df['market_slug'].unique()
    print(f"  Markets: {len(markets)}")

    cooldown_ms = COOLDOWN_SECONDS * 1000

    for slug in tqdm(markets, desc=f"  {dataset_name}"):
        resolution = res_map.get(slug)
        if resolution not in ['UP', 'DOWN']:
            continue

        mdf = obs_df[obs_df['market_slug'] == slug].sort_values('timestamp_ms').reset_index(drop=True)
        if len(mdf) == 0:
            continue

        market_start = mdf['timestamp_ms'].min()
        market_end = mdf['timestamp_ms'].max()

        market_spikes = btc_spikes[
            (btc_spikes['timestamp_ms'] >= market_start) &
            (btc_spikes['timestamp_ms'] <= market_end) &
            (btc_spikes['spike_detected'] == True)
        ]

        if len(market_spikes) == 0:
            continue

        obs_idx = 0

        # Track last signal time per (direction, strategy) for deduplication
        last_signal_ts = {}
        for d in ['UP', 'DOWN']:
            for s in results.keys():
                last_signal_ts[(d, s)] = 0

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

            # Base filters (same as AGGRESSIVE)
            if time_rem < MIN_TIME:
                continue
            if not velocity_confirms_spike(spike_dir, velocity_bps):
                continue
            score = compute_enhanced_score(spike_mag, velocity_bps, spike_dir, time_rem)
            if score < ENHANCED_SCORE_THRESHOLD:
                continue

            # Get prices
            if spike_dir == "UP":
                spike_ask = obs_row['up_ask']
                expensive_ask = obs_row['down_ask']
                obi_spike = obs_row.get('up_imbalance', None)
            else:
                spike_ask = obs_row['down_ask']
                expensive_ask = obs_row['up_ask']
                obi_spike = obs_row.get('down_imbalance', None)

            # Price filters
            if pd.isna(spike_ask) or spike_ask >= HIGH_ENTRY_THRESHOLD:
                continue
            if pd.isna(expensive_ask) or expensive_ask < 0.65:
                continue

            # FADE outcome
            fade_correct = (spike_dir != resolution)

            # OBI conditions
            has_obi = obi_spike is not None and not np.isnan(obi_spike)
            obi_positive = has_obi and obi_spike > 0
            obi_negative = has_obi and obi_spike <= 0

            # Record signals with deduplication
            signal_data = {
                'market_slug': slug,
                'spike_ts': spike_ts,
                'spike_direction': spike_dir,
                'resolution': resolution,
                'expensive_ask': expensive_ask,
                'fade_correct': fade_correct,
                'obi_spike': obi_spike if has_obi else None,
            }

            # NO_OBI: Always record (with cooldown)
            key = (spike_dir, 'no_obi')
            if spike_ts - last_signal_ts[key] >= cooldown_ms:
                results['no_obi'].append(signal_data.copy())
                last_signal_ts[key] = spike_ts

            # OBI_FOLLOW: OBI > 0 (with cooldown)
            if obi_positive:
                key = (spike_dir, 'obi_follow')
                if spike_ts - last_signal_ts[key] >= cooldown_ms:
                    results['obi_follow'].append(signal_data.copy())
                    last_signal_ts[key] = spike_ts

            # OBI_FADE: OBI <= 0 (with cooldown)
            if obi_negative:
                key = (spike_dir, 'obi_fade')
                if spike_ts - last_signal_ts[key] >= cooldown_ms:
                    results['obi_fade'].append(signal_data.copy())
                    last_signal_ts[key] = spike_ts

    return results


def print_results(all_results):
    """Print combined results."""
    print(f"\n{'='*70}")
    print(f"COMBINED RESULTS (all datasets, {COOLDOWN_SECONDS}s dedup)")
    print(f"{'='*70}")

    for strategy in ['no_obi', 'obi_follow', 'obi_fade']:
        data = all_results[strategy]
        if len(data) == 0:
            print(f"\n{strategy.upper()}: No signals")
            continue

        df = pd.DataFrame(data)
        acc = df['fade_correct'].mean() * 100

        print(f"\n{strategy.upper()}:")
        print(f"  Total signals: {len(df):,}")
        print(f"  FADE accuracy: {acc:.1f}%")

        # By expensive_ask threshold
        print(f"  By threshold:")
        for thresh in [0.65, 0.70, 0.75, 0.80]:
            subset = df[df['expensive_ask'] >= thresh]
            if len(subset) > 0:
                print(f"    >= ${thresh:.2f}: {len(subset):,} signals, {subset['fade_correct'].mean()*100:.1f}%")

    # Recommendation
    print(f"\n{'='*70}")
    print("RECOMMENDATION")
    print(f"{'='*70}")

    best_strategy = None
    best_acc = 0
    for strategy in ['no_obi', 'obi_follow', 'obi_fade']:
        data = all_results[strategy]
        if len(data) > 0:
            df = pd.DataFrame(data)
            acc = df['fade_correct'].mean() * 100
            if acc > best_acc:
                best_acc = acc
                best_strategy = strategy

    print(f"\nBest strategy: {best_strategy.upper()} ({best_acc:.1f}% accuracy)")

    if best_strategy == 'obi_fade':
        print("→ INVERT OBI filter for FADE: use OBI <= 0 on spike_side")
    elif best_strategy == 'no_obi':
        print("→ REMOVE OBI filter entirely for FADE")
    else:
        print("→ Keep current OBI filter (OBI > 0)")


def main():
    """Run OBI study with proper deduplication."""
    print("=" * 70)
    print("AGGRESSIVE_M V2 - OBI FILTER STUDY (WITH DEDUPLICATION)")
    print(f"Cooldown: {COOLDOWN_SECONDS}s")
    print("=" * 70)

    # Load OU params
    load_ou_params()

    # Combined results
    all_results = {
        'no_obi': [],
        'obi_follow': [],
        'obi_fade': [],
    }

    for dataset_key in DATASETS.keys():
        btc_df, obs_df, res_map = load_data(dataset_key)

        if btc_df is None:
            print(f"Skipping {dataset_key} - data not available")
            continue

        results = analyze_obi_strategies(btc_df, obs_df, res_map, dataset_key)

        # Combine
        for strategy in all_results.keys():
            all_results[strategy].extend(results[strategy])

    # Print combined results
    print_results(all_results)

    # Save detailed results
    for strategy in all_results.keys():
        if len(all_results[strategy]) > 0:
            df = pd.DataFrame(all_results[strategy])
            output_path = BASE_DIR / f"research/findings/data/aggressive_m_v2_{strategy}_dedup.csv"
            df.to_csv(output_path, index=False)
            print(f"\nSaved: {output_path}")


if __name__ == "__main__":
    main()
