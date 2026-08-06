#!/usr/bin/env python3
"""
PHOENIX V2 — Comprehensive Signal Research Study
==================================================

COPIED FROM: phoenix_main_backtest.py (validated data loading)
PURPOSE: Run all Phase 1-4 signal tests for cheap-side / position-building strategies

Tests implemented:
  Phase 1: Market-level analysis (1.1-1.7)
  Phase 2: Signal-level analysis (2.1-2.10)
  Phase 3: Combination analysis (3.1-3.4)
  Phase 4: Strategy family tests (4.1-4.6)

Usage:
    python research/signal_research/v2_comprehensive_signal_study.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Any
import sys
import json
import math
import warnings
from datetime import datetime
from scipy import stats
from tqdm import tqdm

warnings.filterwarnings('ignore')

BASE_DIR = Path("/Users/rananjaybika/polymarket-amm-bot")
OUTPUT_DIR = BASE_DIR / "research" / "signal_research" / "results"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# =============================================================================
# DATASETS (copied from phoenix_main_backtest.py — validated)
# =============================================================================
DATASETS = {
    "IS+OOS2": {
        "name": "IS+OOS2 (Jan 16-19)",
        "obs_files": [
            "research/observer/grid_obs_20260116.csv",
            "research/observer/grid_obs_20260117.csv",
            "research/observer/grid_obs_20260118.csv",
            "research/observer/grid_obs_20260119.csv",
        ],
        "res_files": ["research/observer/market_resolutions.csv"],
        "schema": "gen1",  # no accel/jerk/OBI
    },
    "OOS3+4": {
        "name": "OOS3+4 (Jan 22-24)",
        "obs_files": [
            "research/observer/PROTECTED_grid_obs_oos3_oos4_combined.csv",
        ],
        "res_files": ["research/observer/market_resolutions_verified.csv"],
        "schema": "gen2",  # has accel/jerk, no depth/OBI
    },
    "OOS7": {
        "name": "OOS7 (Jan 29-30)",
        "obs_files": [
            "research/observer/grid_obs_20260129.csv",
            "research/observer/grid_obs_20260130.csv",
        ],
        "res_files": [
            "research/observer/resolutions_20260129.csv",
            "research/observer/resolutions_20260130.csv",
        ],
        "schema": "gen3",  # full depth + OBI
    },
    "OOS8": {
        "name": "OOS8 (Jan 31)",
        "obs_files": [
            "research/observer/grid_obs_20260131.csv",
        ],
        "res_files": ["research/observer/resolutions_20260131.csv"],
        "schema": "gen3",
    },
    "OOS9": {
        "name": "OOS9 (Feb 1-3)",
        "obs_files": [
            "research/observer/grid_obs_oos9.csv",
        ],
        "res_files": [
            "research/observer/resolutions_oos9_1.csv",
            "research/observer/resolutions_oos9_2.csv",
        ],
        "schema": "gen3",
    },
}

# =============================================================================
# DATA LOADING (from phoenix_main_backtest.py — validated)
# =============================================================================
def load_dataset(dataset_key: str) -> Tuple[Optional[pd.DataFrame], Dict[str, str]]:
    """Load observer data + resolutions for a dataset."""
    config = DATASETS[dataset_key]
    print(f"\n  Loading {config['name']}...")

    obs_dfs = []
    for fname in config['obs_files']:
        fpath = BASE_DIR / fname
        if fpath.exists():
            df = pd.read_csv(fpath, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)
            print(f"    {fpath.name}: {len(df):,} rows")
        else:
            print(f"    {fpath.name}: NOT FOUND")

    if not obs_dfs:
        return None, {}

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])

    # Ensure numeric types
    for col in ['up_ask', 'down_ask', 'up_bid', 'down_bid', 'binance_price',
                'velocity_bps', 'time_remaining_secs', 'pair_cost', 'spike_magnitude']:
        if col in obs_df.columns:
            obs_df[col] = pd.to_numeric(obs_df[col], errors='coerce')

    # Optional columns
    for col in ['acceleration_bps2', 'jerk_bps3', 'momentum_5s',
                'up_imbalance', 'down_imbalance']:
        if col in obs_df.columns:
            obs_df[col] = pd.to_numeric(obs_df[col], errors='coerce')

    # Load resolutions
    resolutions = {}
    for res_fname in config.get('res_files', []):
        res_path = BASE_DIR / res_fname
        if res_path.exists():
            res_df = pd.read_csv(res_path)
            if 'slug' in res_df.columns and 'winner' in res_df.columns:
                for _, row in res_df.iterrows():
                    resolutions[row['slug']] = row['winner']
            elif 'market_slug' in res_df.columns and 'resolution' in res_df.columns:
                for _, row in res_df.iterrows():
                    resolutions[row['market_slug']] = row['resolution']

    n_markets = obs_df['market_slug'].nunique()
    n_resolved = sum(1 for s in obs_df['market_slug'].unique() if s in resolutions)
    print(f"    Combined: {len(obs_df):,} rows, {n_markets} markets, {n_resolved} resolved")

    return obs_df, resolutions


def load_strikes() -> Dict[str, Dict]:
    """Load Chainlink strike price data."""
    path = BASE_DIR / "research" / "chainlink_strikes_historical.json"
    if not path.exists():
        print("  WARNING: chainlink_strikes_historical.json not found")
        return {}
    with open(path) as f:
        data = json.load(f)
    return data.get('markets', {})


# =============================================================================
# HELPER: Identify cheap/expensive side per market
# =============================================================================
def classify_sides(obs_df: pd.DataFrame, resolutions: Dict[str, str]) -> pd.DataFrame:
    """
    For each market, determine which side is cheap and whether cheap side won.
    Uses the FIRST observation where time_remaining < 600 to determine bias.
    """
    results = []
    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions:
            continue

        resolution = resolutions[slug]
        mdf = group.sort_values('timestamp_ms')

        # Use observation at T~600s to determine sides (early enough to be meaningful)
        t600 = mdf[mdf['time_remaining_secs'].between(590, 610)]
        if len(t600) == 0:
            # Fallback: use earliest available
            t600 = mdf.head(5)
        if len(t600) == 0:
            continue

        row = t600.iloc[len(t600)//2]  # Middle observation
        up_ask = row.get('up_ask', np.nan)
        down_ask = row.get('down_ask', np.nan)

        if pd.isna(up_ask) or pd.isna(down_ask) or up_ask <= 0 or down_ask <= 0:
            continue

        if up_ask >= down_ask:
            expensive_side = "UP"
            cheap_side = "DOWN"
            cheap_ask_at_600 = down_ask
            expensive_ask_at_600 = up_ask
        else:
            expensive_side = "DOWN"
            cheap_side = "UP"
            cheap_ask_at_600 = up_ask
            expensive_ask_at_600 = down_ask

        cheap_won = (resolution == cheap_side)
        spread_at_600 = abs(up_ask - down_ask)

        results.append({
            'slug': slug,
            'resolution': resolution,
            'expensive_side': expensive_side,
            'cheap_side': cheap_side,
            'cheap_won': cheap_won,
            'cheap_ask_at_600': cheap_ask_at_600,
            'expensive_ask_at_600': expensive_ask_at_600,
            'spread_at_600': spread_at_600,
            'pair_cost_at_600': up_ask + down_ask,
            'n_obs': len(mdf),
        })

    return pd.DataFrame(results)


# =============================================================================
# PHASE 1: Market-Level Analysis
# =============================================================================

def test_1_1_regime_spread(obs_df, resolutions, dataset_name):
    """Test 1.1: Cheap-side win rate by spread regime at various time points."""
    print(f"\n  [1.1] Regime (Spread) — {dataset_name}")
    results = []
    time_points = [900, 800, 700, 600, 500, 400, 300]
    spread_buckets = [(0, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.40), (0.40, 1.0)]

    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions:
            continue
        resolution = resolutions[slug]
        mdf = group.sort_values('timestamp_ms')

        for tp in time_points:
            nearby = mdf[(mdf['time_remaining_secs'] >= tp - 10) & (mdf['time_remaining_secs'] <= tp + 10)]
            if len(nearby) == 0:
                continue
            row = nearby.iloc[len(nearby)//2]
            ua, da = row['up_ask'], row['down_ask']
            if pd.isna(ua) or pd.isna(da) or ua <= 0 or da <= 0:
                continue

            spread = abs(ua - da)
            cheap_side = "DOWN" if ua >= da else "UP"
            cheap_won = (resolution == cheap_side)

            results.append({
                'dataset': dataset_name, 'slug': slug, 'time_point': tp,
                'spread': spread, 'cheap_won': cheap_won,
                'cheap_ask': min(ua, da), 'expensive_ask': max(ua, da),
            })

    df = pd.DataFrame(results)
    if len(df) == 0:
        return pd.DataFrame()

    # Bucket and aggregate
    summary = []
    for tp in time_points:
        tdf = df[df['time_point'] == tp]
        for lo, hi in spread_buckets:
            bucket = tdf[(tdf['spread'] >= lo) & (tdf['spread'] < hi)]
            if len(bucket) < 3:
                continue
            wr = bucket['cheap_won'].mean()
            summary.append({
                'dataset': dataset_name, 'time_point': tp,
                'spread_bucket': f"{lo:.2f}-{hi:.2f}",
                'n_markets': len(bucket), 'cheap_win_rate': wr,
                'avg_cheap_ask': bucket['cheap_ask'].mean(),
                'avg_spread': bucket['spread'].mean(),
            })

    return pd.DataFrame(summary)


def test_1_2_strike_proximity(obs_df, resolutions, strikes, dataset_name):
    """Test 1.2: Cheap-side win rate by strike proximity."""
    print(f"\n  [1.2] Strike Proximity — {dataset_name}")
    results = []
    proximity_buckets = [(0, 50), (50, 100), (100, 200), (200, 500), (500, 10000)]

    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions or slug not in strikes:
            continue
        resolution = resolutions[slug]
        strike_info = strikes[slug]
        strike_price = strike_info.get('binance_strike') or strike_info.get('inferred_chainlink_strike')
        if not strike_price or strike_price <= 0:
            continue

        mdf = group.sort_values('timestamp_ms')

        # At T=600s
        t600 = mdf[mdf['time_remaining_secs'].between(590, 610)]
        if len(t600) == 0:
            continue
        row = t600.iloc[len(t600)//2]
        btc = row.get('binance_price', np.nan)
        ua, da = row['up_ask'], row['down_ask']
        if pd.isna(btc) or pd.isna(ua) or pd.isna(da) or btc <= 0:
            continue

        proximity_bps = abs(btc - strike_price) / strike_price * 10000
        cheap_side = "DOWN" if ua >= da else "UP"
        cheap_won = (resolution == cheap_side)

        results.append({
            'dataset': dataset_name, 'slug': slug,
            'proximity_bps': proximity_bps,
            'cheap_won': cheap_won,
            'cheap_ask': min(ua, da),
            'btc_price': btc, 'strike': strike_price,
        })

    df = pd.DataFrame(results)
    if len(df) == 0:
        return pd.DataFrame()

    summary = []
    for lo, hi in proximity_buckets:
        bucket = df[(df['proximity_bps'] >= lo) & (df['proximity_bps'] < hi)]
        if len(bucket) < 3:
            continue
        wr = bucket['cheap_won'].mean()
        summary.append({
            'dataset': dataset_name,
            'proximity_bucket_bps': f"{lo}-{hi}",
            'n_markets': len(bucket), 'cheap_win_rate': wr,
            'avg_cheap_ask': bucket['cheap_ask'].mean(),
            'avg_proximity_bps': bucket['proximity_bps'].mean(),
        })

    return pd.DataFrame(summary)


def test_1_3_trajectory(obs_df, resolutions, dataset_name):
    """Test 1.3: Cheap-side win trajectory — do winning/losing cheap sides diverge early?"""
    print(f"\n  [1.3] Trajectory Analysis — {dataset_name}")
    time_points = list(range(900, 110, -30))  # Every 30s from 900 to 120
    win_trajectories = {tp: [] for tp in time_points}
    lose_trajectories = {tp: [] for tp in time_points}

    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions:
            continue
        resolution = resolutions[slug]
        mdf = group.sort_values('timestamp_ms')

        # Determine cheap side at T=600
        t600 = mdf[mdf['time_remaining_secs'].between(590, 610)]
        if len(t600) == 0:
            continue
        row = t600.iloc[len(t600)//2]
        ua, da = row['up_ask'], row['down_ask']
        if pd.isna(ua) or pd.isna(da) or ua <= 0 or da <= 0:
            continue

        cheap_side = "DOWN" if ua >= da else "UP"
        cheap_col = 'down_ask' if cheap_side == "DOWN" else 'up_ask'
        cheap_won = (resolution == cheap_side)

        for tp in time_points:
            nearby = mdf[mdf['time_remaining_secs'].between(tp - 5, tp + 5)]
            if len(nearby) == 0:
                continue
            val = nearby[cheap_col].median()
            if pd.isna(val):
                continue
            if cheap_won:
                win_trajectories[tp].append(val)
            else:
                lose_trajectories[tp].append(val)

    results = []
    for tp in time_points:
        wins = win_trajectories[tp]
        loses = lose_trajectories[tp]
        if len(wins) < 3 or len(loses) < 3:
            continue
        win_mean = np.mean(wins)
        lose_mean = np.mean(loses)
        # T-test for difference
        t_stat, p_val = stats.ttest_ind(wins, loses, equal_var=False)
        results.append({
            'dataset': dataset_name, 'time_remaining': tp,
            'n_wins': len(wins), 'n_loses': len(loses),
            'win_avg_cheap_ask': win_mean, 'lose_avg_cheap_ask': lose_mean,
            'difference': win_mean - lose_mean,
            't_stat': t_stat, 'p_value': p_val,
        })

    return pd.DataFrame(results)


def test_1_4_feature_importance(obs_df, resolutions, strikes, dataset_name):
    """Test 1.4: Feature importance for cheap-side wins at T=600s and T=300s."""
    print(f"\n  [1.4] Feature Importance — {dataset_name}")
    schema = DATASETS[dataset_name].get('schema', 'gen1')
    results_all = []

    for eval_time in [600, 300]:
        features_list = []
        labels = []

        for slug, group in obs_df.groupby('market_slug'):
            if slug not in resolutions:
                continue
            resolution = resolutions[slug]
            mdf = group.sort_values('timestamp_ms')

            nearby = mdf[mdf['time_remaining_secs'].between(eval_time - 10, eval_time + 10)]
            if len(nearby) == 0:
                continue
            row = nearby.iloc[len(nearby)//2]
            ua, da = row['up_ask'], row['down_ask']
            if pd.isna(ua) or pd.isna(da) or ua <= 0 or da <= 0:
                continue

            cheap_side = "DOWN" if ua >= da else "UP"
            cheap_ask = min(ua, da)
            expensive_ask = max(ua, da)
            cheap_won = int(resolution == cheap_side)
            spread = abs(ua - da)

            feats = {
                'expensive_ask': expensive_ask,
                'cheap_ask': cheap_ask,
                'spread': spread,
                'pair_cost': ua + da,
                'velocity_bps': row.get('velocity_bps', np.nan),
                'spike_magnitude': row.get('spike_magnitude', 0),
            }

            # Strike proximity
            if slug in strikes:
                strike_info = strikes[slug]
                sp = strike_info.get('binance_strike', 0)
                btc = row.get('binance_price', np.nan)
                if sp > 0 and not pd.isna(btc) and btc > 0:
                    feats['strike_proximity_bps'] = abs(btc - sp) / sp * 10000
                    feats['velocity_toward_strike'] = row.get('velocity_bps', 0) * np.sign(sp - btc)

            # Gen2+ signals
            if schema in ('gen2', 'gen3'):
                feats['acceleration_bps2'] = row.get('acceleration_bps2', np.nan)
                feats['jerk_bps3'] = row.get('jerk_bps3', np.nan)
                feats['momentum_5s'] = row.get('momentum_5s', np.nan)
                feats['accel_aligned'] = int(row.get('accel_aligned', False)) if not pd.isna(row.get('accel_aligned', np.nan)) else np.nan

                # Deceleration flag
                vel = row.get('velocity_bps', 0)
                accel = row.get('acceleration_bps2', 0)
                if not pd.isna(vel) and not pd.isna(accel):
                    feats['deceleration'] = int(vel * accel < 0)

                # Kinematic state octant
                jerk = row.get('jerk_bps3', 0)
                if not any(pd.isna(x) for x in [vel, accel, jerk]):
                    octant = (int(vel > 0) << 2) | (int(accel > 0) << 1) | int(jerk > 0)
                    feats['kinematic_octant'] = octant

            # Gen3 signals
            if schema == 'gen3':
                feats['up_imbalance'] = row.get('up_imbalance', np.nan)
                feats['down_imbalance'] = row.get('down_imbalance', np.nan)
                # Cheap-side imbalance
                if cheap_side == "DOWN":
                    feats['cheap_side_imbalance'] = row.get('down_imbalance', np.nan)
                else:
                    feats['cheap_side_imbalance'] = row.get('up_imbalance', np.nan)

            # Derived: cheap price change over last 60s
            earlier = mdf[mdf['time_remaining_secs'].between(eval_time + 50, eval_time + 70)]
            if len(earlier) > 0:
                cheap_col = 'down_ask' if cheap_side == "DOWN" else 'up_ask'
                earlier_price = earlier[cheap_col].median()
                if not pd.isna(earlier_price) and earlier_price > 0:
                    feats['cheap_ask_change_60s'] = cheap_ask - earlier_price

            # Derived: cheap price stability (stdev over last 120s)
            window = mdf[mdf['time_remaining_secs'].between(eval_time, eval_time + 120)]
            if len(window) >= 5:
                cheap_col = 'down_ask' if cheap_side == "DOWN" else 'up_ask'
                feats['cheap_ask_stdev_120s'] = window[cheap_col].std()

            features_list.append(feats)
            labels.append(cheap_won)

        if len(features_list) < 10:
            continue

        feat_df = pd.DataFrame(features_list)
        labels = np.array(labels)

        # Per-feature analysis
        for col in feat_df.columns:
            vals = feat_df[col].values
            valid = ~np.isnan(vals)
            if valid.sum() < 10:
                continue

            x = vals[valid]
            y = labels[valid]

            # Correlation
            if len(np.unique(x)) > 1:
                corr, p_corr = stats.pointbiserialr(y, x)
            else:
                corr, p_corr = 0, 1.0

            # Univariate AUC (Mann-Whitney)
            pos = x[y == 1]
            neg = x[y == 0]
            if len(pos) >= 3 and len(neg) >= 3:
                u_stat, p_mw = stats.mannwhitneyu(pos, neg, alternative='two-sided')
                auc = u_stat / (len(pos) * len(neg))
            else:
                auc, p_mw = 0.5, 1.0

            results_all.append({
                'dataset': dataset_name,
                'eval_time': eval_time,
                'feature': col,
                'n_valid': int(valid.sum()),
                'n_cheap_wins': int(y.sum()),
                'correlation': corr,
                'p_correlation': p_corr,
                'auc': auc,
                'p_mannwhitney': p_mw,
                'mean_when_cheap_wins': float(np.mean(pos)) if len(pos) > 0 else np.nan,
                'mean_when_cheap_loses': float(np.mean(neg)) if len(neg) > 0 else np.nan,
            })

    return pd.DataFrame(results_all)


def test_1_5_correlation_decay(obs_df, resolutions, dataset_name):
    """Test 1.5: Adverse selection correlation decay between cheap and expensive asks."""
    print(f"\n  [1.5] Correlation Decay — {dataset_name}")
    deltas_seconds = [0, 1, 5, 10, 30, 60, 120, 300]
    results = []

    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions:
            continue
        mdf = group.sort_values('timestamp_ms')
        if len(mdf) < 50:
            continue

        ua = mdf['up_ask'].values.astype(float)
        da = mdf['down_ask'].values.astype(float)
        ts = mdf['timestamp_ms'].values.astype(float)

        valid = ~(np.isnan(ua) | np.isnan(da))
        ua, da, ts = ua[valid], da[valid], ts[valid]
        if len(ua) < 50:
            continue

        # Determine cheap/expensive
        mid_idx = len(ua) // 2
        if ua[mid_idx] >= da[mid_idx]:
            cheap = da
            expensive = ua
        else:
            cheap = ua
            expensive = da

        # Changes (tick-level)
        d_cheap = np.diff(cheap)
        d_expensive = np.diff(expensive)
        d_ts = np.diff(ts)

        # Δ=0: correlation of simultaneous changes
        if len(d_cheap) >= 20:
            corr0 = np.corrcoef(d_cheap, d_expensive)[0, 1]
            results.append({
                'dataset': dataset_name, 'slug': slug,
                'delta_seconds': 0, 'correlation': corr0, 'n_pairs': len(d_cheap),
            })

        # For Δ > 0: find pairs where time gap ≈ Δ
        for delta_s in deltas_seconds[1:]:
            delta_ms = delta_s * 1000
            corr_pairs_cheap = []
            corr_pairs_exp = []

            # Sample: take every 5th observation to avoid overlap
            step = max(1, len(ts) // 200)
            for i in range(0, len(ts) - 1, step):
                target_ts = ts[i] + delta_ms
                j = np.searchsorted(ts, target_ts)
                if j >= len(ts):
                    continue
                if abs(ts[j] - target_ts) > delta_ms * 0.2:  # 20% tolerance
                    continue
                corr_pairs_cheap.append(cheap[i])
                corr_pairs_exp.append(expensive[j])

            if len(corr_pairs_cheap) >= 10:
                c = np.corrcoef(corr_pairs_cheap, corr_pairs_exp)[0, 1]
                results.append({
                    'dataset': dataset_name, 'slug': slug,
                    'delta_seconds': delta_s, 'correlation': c,
                    'n_pairs': len(corr_pairs_cheap),
                })

    df = pd.DataFrame(results)
    if len(df) == 0:
        return pd.DataFrame()

    # Aggregate across markets
    summary = df.groupby(['dataset', 'delta_seconds']).agg(
        mean_correlation=('correlation', 'mean'),
        median_correlation=('correlation', 'median'),
        std_correlation=('correlation', 'std'),
        n_markets=('slug', 'nunique'),
        total_pairs=('n_pairs', 'sum'),
    ).reset_index()

    return summary


def test_1_6_strike_crossings(obs_df, resolutions, strikes, dataset_name):
    """Test 1.6: Strike crossing events — do they create exploitable cheap entries?"""
    print(f"\n  [1.6] Strike Crossings — {dataset_name}")
    results = []

    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions or slug not in strikes:
            continue
        resolution = resolutions[slug]
        strike_info = strikes[slug]
        strike_price = strike_info.get('binance_strike', 0)
        if strike_price <= 0:
            continue

        mdf = group.sort_values('timestamp_ms')
        btc = mdf['binance_price'].values.astype(float)
        ts = mdf['timestamp_ms'].values.astype(float)
        ua = mdf['up_ask'].values.astype(float)
        da = mdf['down_ask'].values.astype(float)
        tr = mdf['time_remaining_secs'].values.astype(float)

        valid = ~(np.isnan(btc) | np.isnan(ua) | np.isnan(da))
        btc, ts, ua, da, tr = btc[valid], ts[valid], ua[valid], da[valid], tr[valid]
        if len(btc) < 20:
            continue

        # Find crossings: sign change of (BTC - strike)
        side = np.sign(btc - strike_price)
        crossings = np.where(np.diff(side) != 0)[0]

        for ci in crossings:
            if ci + 1 >= len(btc):
                continue
            crossing_time = ts[ci + 1]
            crossing_tr = tr[ci + 1]

            # Before crossing: which side was cheap?
            if ua[ci] >= da[ci]:
                pre_cheap_side = "DOWN"
                pre_cheap_ask = da[ci]
            else:
                pre_cheap_side = "UP"
                pre_cheap_ask = ua[ci]

            # After crossing (30s later)
            post_mask = (ts > crossing_time) & (ts <= crossing_time + 30000)
            if post_mask.sum() == 0:
                continue
            post_idx = np.where(post_mask)[0][-1]
            post_ua, post_da = ua[post_idx], da[post_idx]

            # Did cheap side change?
            if post_ua >= post_da:
                post_cheap_side = "DOWN"
                post_cheap_ask = post_da
            else:
                post_cheap_side = "UP"
                post_cheap_ask = post_ua

            sides_flipped = (pre_cheap_side != post_cheap_side)

            results.append({
                'dataset': dataset_name, 'slug': slug,
                'crossing_time_remaining': crossing_tr,
                'sides_flipped': sides_flipped,
                'pre_cheap_ask': pre_cheap_ask,
                'post_cheap_ask': post_cheap_ask,
                'pre_cheap_side': pre_cheap_side,
                'resolution': resolution,
                'pre_cheap_won': (resolution == pre_cheap_side),
            })

    return pd.DataFrame(results)


def test_1_7_btc_volatility(obs_df, resolutions, dataset_name):
    """Test 1.7: BTC volatility as regime filter."""
    print(f"\n  [1.7] BTC Volatility — {dataset_name}")
    results = []
    vol_buckets = [(0, 5), (5, 15), (15, 30), (30, 100)]

    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions:
            continue
        resolution = resolutions[slug]
        mdf = group.sort_values('timestamp_ms')

        # BTC range in first 300s (T=900 to T=600)
        early = mdf[mdf['time_remaining_secs'].between(600, 900)]
        if len(early) < 10:
            continue
        btc = early['binance_price'].dropna().values
        if len(btc) < 10:
            continue

        btc_range_bps = (btc.max() - btc.min()) / btc.mean() * 10000

        # Cheap side
        ua = mdf['up_ask'].median()
        da = mdf['down_ask'].median()
        if pd.isna(ua) or pd.isna(da):
            continue
        cheap_side = "DOWN" if ua >= da else "UP"
        cheap_won = (resolution == cheap_side)

        # Spike count
        spike_count = (mdf['spike_detected'] == True).sum() if 'spike_detected' in mdf.columns else 0

        results.append({
            'dataset': dataset_name, 'slug': slug,
            'btc_range_bps': btc_range_bps,
            'cheap_won': cheap_won,
            'spike_count': spike_count,
        })

    df = pd.DataFrame(results)
    if len(df) == 0:
        return pd.DataFrame()

    summary = []
    for lo, hi in vol_buckets:
        bucket = df[(df['btc_range_bps'] >= lo) & (df['btc_range_bps'] < hi)]
        if len(bucket) < 3:
            continue
        summary.append({
            'dataset': dataset_name,
            'vol_bucket_bps': f"{lo}-{hi}",
            'n_markets': len(bucket),
            'cheap_win_rate': bucket['cheap_won'].mean(),
            'avg_spike_count': bucket['spike_count'].mean(),
            'avg_btc_range': bucket['btc_range_bps'].mean(),
        })

    return pd.DataFrame(summary)


# =============================================================================
# PHASE 2: Signal-Level Analysis
# =============================================================================

def test_2_1_velocity_toward_strike(obs_df, resolutions, strikes, dataset_name):
    """Test 2.1: Velocity toward strike as cheap entry timer."""
    print(f"\n  [2.1] Velocity Toward Strike — {dataset_name}")
    results = []

    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions or slug not in strikes:
            continue
        resolution = resolutions[slug]
        strike_info = strikes[slug]
        strike_price = strike_info.get('binance_strike', 0)
        if strike_price <= 0:
            continue

        mdf = group.sort_values('timestamp_ms').reset_index(drop=True)
        if len(mdf) < 30:
            continue

        vel = mdf['velocity_bps'].values.astype(float)
        btc = mdf['binance_price'].values.astype(float)
        tr = mdf['time_remaining_secs'].values.astype(float)
        ts = mdf['timestamp_ms'].values.astype(float)

        # Determine cheap side
        mid = len(mdf) // 2
        ua_mid = mdf['up_ask'].iloc[mid]
        da_mid = mdf['down_ask'].iloc[mid]
        if pd.isna(ua_mid) or pd.isna(da_mid):
            continue
        cheap_side = "DOWN" if ua_mid >= da_mid else "UP"
        cheap_col = 'down_ask' if cheap_side == "DOWN" else 'up_ask'
        cheap = mdf[cheap_col].values.astype(float)
        cheap_won = (resolution == cheap_side)

        # Velocity toward strike
        vts = vel * np.sign(strike_price - btc)

        # Sample every 5th tick in entry-relevant window (T=700 to T=200)
        for i in range(0, len(mdf) - 1, 5):
            if pd.isna(vts[i]) or pd.isna(tr[i]):
                continue
            if tr[i] > 700 or tr[i] < 200:
                continue

            # Cheap price change in next 30s, 60s
            for horizon_s, horizon_label in [(30, '30s'), (60, '60s')]:
                target_ts = ts[i] + horizon_s * 1000
                j = np.searchsorted(ts, target_ts)
                if j >= len(cheap):
                    continue
                if abs(ts[j] - target_ts) > horizon_s * 200:  # tolerance
                    continue
                if pd.isna(cheap[i]) or pd.isna(cheap[j]):
                    continue
                d_cheap = cheap[j] - cheap[i]

                results.append({
                    'dataset': dataset_name, 'slug': slug,
                    'time_remaining': tr[i],
                    'vel_toward_strike': vts[i],
                    'horizon': horizon_label,
                    'cheap_ask_change': d_cheap,
                    'cheap_won': cheap_won,
                })

    df = pd.DataFrame(results)
    if len(df) == 0:
        return pd.DataFrame()

    # Aggregate: bucket vel_toward_strike and measure average cheap_ask_change
    summary = []
    for horizon in ['30s', '60s']:
        hdf = df[df['horizon'] == horizon]
        for lo, hi, label in [(-10, -0.3, 'strong_away'), (-0.3, -0.05, 'weak_away'),
                               (-0.05, 0.05, 'neutral'), (0.05, 0.3, 'weak_toward'),
                               (0.3, 10, 'strong_toward')]:
            bucket = hdf[(hdf['vel_toward_strike'] >= lo) & (hdf['vel_toward_strike'] < hi)]
            if len(bucket) < 20:
                continue
            summary.append({
                'dataset': dataset_name, 'horizon': horizon,
                'vel_bucket': label,
                'n_obs': len(bucket),
                'avg_cheap_change': bucket['cheap_ask_change'].mean(),
                'std_cheap_change': bucket['cheap_ask_change'].std(),
                'pct_positive': (bucket['cheap_ask_change'] > 0).mean(),
                'cheap_win_rate': bucket['cheap_won'].mean(),
            })

    return pd.DataFrame(summary)


def test_2_2_kinematic_state(obs_df, resolutions, dataset_name):
    """Test 2.2: Kinematic state octant as entry timer (Gen2+ only)."""
    print(f"\n  [2.2] Kinematic State — {dataset_name}")
    schema = DATASETS[dataset_name].get('schema', 'gen1')
    if schema == 'gen1':
        print("    Skipping (Gen1 — no accel/jerk)")
        return pd.DataFrame()

    results = []

    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions:
            continue
        resolution = resolutions[slug]
        mdf = group.sort_values('timestamp_ms').reset_index(drop=True)

        vel = mdf['velocity_bps'].values.astype(float)
        accel = mdf['acceleration_bps2'].values.astype(float) if 'acceleration_bps2' in mdf.columns else np.full(len(mdf), np.nan)
        jerk = mdf['jerk_bps3'].values.astype(float) if 'jerk_bps3' in mdf.columns else np.full(len(mdf), np.nan)
        tr = mdf['time_remaining_secs'].values.astype(float)
        ts = mdf['timestamp_ms'].values.astype(float)

        mid = len(mdf) // 2
        ua_mid = mdf['up_ask'].iloc[mid]
        da_mid = mdf['down_ask'].iloc[mid]
        if pd.isna(ua_mid) or pd.isna(da_mid):
            continue
        cheap_side = "DOWN" if ua_mid >= da_mid else "UP"
        cheap_col = 'down_ask' if cheap_side == "DOWN" else 'up_ask'
        cheap = mdf[cheap_col].values.astype(float)
        cheap_won = (resolution == cheap_side)

        for i in range(0, len(mdf) - 1, 5):
            if any(pd.isna(x) for x in [vel[i], accel[i], jerk[i], tr[i]]):
                continue
            if tr[i] > 700 or tr[i] < 150:
                continue

            octant = (int(vel[i] > 0) << 2) | (int(accel[i] > 0) << 1) | int(jerk[i] > 0)
            decel = int(vel[i] * accel[i] < 0)

            # Cheap price 30s later
            target_ts = ts[i] + 30000
            j = np.searchsorted(ts, target_ts)
            if j >= len(cheap) or abs(ts[j] - target_ts) > 6000:
                continue
            if pd.isna(cheap[i]) or pd.isna(cheap[j]):
                continue

            results.append({
                'dataset': dataset_name, 'slug': slug,
                'octant': octant,
                'deceleration': decel,
                'vel': vel[i], 'accel': accel[i], 'jerk': jerk[i],
                'time_remaining': tr[i],
                'cheap_ask_change_30s': cheap[j] - cheap[i],
                'cheap_won': cheap_won,
            })

    df = pd.DataFrame(results)
    if len(df) == 0:
        return pd.DataFrame()

    # Octant labels
    octant_labels = {
        0: '[-v,-a,-j]', 1: '[-v,-a,+j]', 2: '[-v,+a,-j]', 3: '[-v,+a,+j]',
        4: '[+v,-a,-j]', 5: '[+v,-a,+j]', 6: '[+v,+a,-j]', 7: '[+v,+a,+j]',
    }

    summary = []
    for oct_val in range(8):
        odf = df[df['octant'] == oct_val]
        if len(odf) < 20:
            continue
        summary.append({
            'dataset': dataset_name,
            'octant': oct_val,
            'octant_label': octant_labels[oct_val],
            'n_obs': len(odf),
            'avg_cheap_change_30s': odf['cheap_ask_change_30s'].mean(),
            'std_cheap_change_30s': odf['cheap_ask_change_30s'].std(),
            'pct_cheap_rises': (odf['cheap_ask_change_30s'] > 0).mean(),
            'cheap_win_rate': odf['cheap_won'].mean(),
        })

    # Also: deceleration vs not
    for decel_val in [0, 1]:
        ddf = df[df['deceleration'] == decel_val]
        if len(ddf) < 20:
            continue
        summary.append({
            'dataset': dataset_name,
            'octant': f'decel={decel_val}',
            'octant_label': 'DECEL' if decel_val else 'NO_DECEL',
            'n_obs': len(ddf),
            'avg_cheap_change_30s': ddf['cheap_ask_change_30s'].mean(),
            'std_cheap_change_30s': ddf['cheap_ask_change_30s'].std(),
            'pct_cheap_rises': (ddf['cheap_ask_change_30s'] > 0).mean(),
            'cheap_win_rate': ddf['cheap_won'].mean(),
        })

    return pd.DataFrame(summary)


def test_2_5_price_support(obs_df, resolutions, dataset_name):
    """Test 2.5: Cheap-side price support detection."""
    print(f"\n  [2.5] Price Support — {dataset_name}")
    results = []

    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions:
            continue
        resolution = resolutions[slug]
        mdf = group.sort_values('timestamp_ms').reset_index(drop=True)

        ua = mdf['up_ask'].values.astype(float)
        da = mdf['down_ask'].values.astype(float)
        tr = mdf['time_remaining_secs'].values.astype(float)
        ts = mdf['timestamp_ms'].values.astype(float)

        mid = len(mdf) // 2
        if pd.isna(ua[mid]) or pd.isna(da[mid]):
            continue
        cheap_side = "DOWN" if ua[mid] >= da[mid] else "UP"
        cheap = da if cheap_side == "DOWN" else ua
        cheap_won = (resolution == cheap_side)

        # Compute support metrics at T=600, T=400, T=300
        for eval_t in [600, 400, 300]:
            # Window: last 120s before eval_t
            window_mask = (tr >= eval_t) & (tr <= eval_t + 120)
            w_cheap = cheap[window_mask]
            w_cheap = w_cheap[~np.isnan(w_cheap)]
            if len(w_cheap) < 10:
                continue

            # Support score: how stable is cheap price?
            stdev = np.std(w_cheap)
            price_change = w_cheap[-1] - w_cheap[0] if len(w_cheap) > 1 else 0

            # Trajectory slope (linear regression)
            x = np.arange(len(w_cheap))
            if len(x) >= 3:
                slope, _, r_value, p_value, _ = stats.linregress(x, w_cheap)
            else:
                slope, r_value, p_value = 0, 0, 1

            # Curvature (2nd derivative of price trajectory)
            if len(w_cheap) >= 5:
                d2 = np.diff(np.diff(w_cheap))
                curvature = np.mean(d2)
            else:
                curvature = 0

            results.append({
                'dataset': dataset_name, 'slug': slug,
                'eval_time': eval_t,
                'cheap_stdev': stdev,
                'cheap_price_change': price_change,
                'trajectory_slope': slope,
                'trajectory_r2': r_value**2,
                'curvature': curvature,
                'cheap_level': np.mean(w_cheap),
                'cheap_won': cheap_won,
            })

    df = pd.DataFrame(results)
    if len(df) == 0:
        return pd.DataFrame()

    # Analyze: bucket by stability/support metrics
    summary = []
    for eval_t in [600, 400, 300]:
        edf = df[df['eval_time'] == eval_t]
        if len(edf) < 10:
            continue

        # Stability buckets
        for metric, label in [('cheap_stdev', 'stability'), ('trajectory_slope', 'slope'), ('curvature', 'curvature')]:
            vals = edf[metric].dropna()
            if len(vals) < 10:
                continue
            q25, q50, q75 = vals.quantile([0.25, 0.5, 0.75])
            for lo, hi, qlabel in [(vals.min()-1, q25, 'Q1'), (q25, q50, 'Q2'), (q50, q75, 'Q3'), (q75, vals.max()+1, 'Q4')]:
                bucket = edf[(edf[metric] >= lo) & (edf[metric] < hi)]
                if len(bucket) < 3:
                    continue
                summary.append({
                    'dataset': dataset_name, 'eval_time': eval_t,
                    'metric': label, 'quartile': qlabel,
                    'n_markets': len(bucket),
                    'cheap_win_rate': bucket['cheap_won'].mean(),
                    'metric_range': f"{lo:.4f} to {hi:.4f}",
                })

    return pd.DataFrame(summary)


def test_2_6_crossside_flow(obs_df, resolutions, dataset_name):
    """Test 2.6: Cross-side flow detection (unified orderbook signal)."""
    print(f"\n  [2.6] Cross-Side Flow — {dataset_name}")
    results = []

    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions:
            continue
        resolution = resolutions[slug]
        mdf = group.sort_values('timestamp_ms').reset_index(drop=True)
        if len(mdf) < 20:
            continue

        ua = mdf['up_ask'].values.astype(float)
        da = mdf['down_ask'].values.astype(float)
        tr = mdf['time_remaining_secs'].values.astype(float)

        # Changes
        d_ua = np.diff(ua)
        d_da = np.diff(da)
        mid = len(ua) // 2
        if pd.isna(ua[mid]) or pd.isna(da[mid]):
            continue
        cheap_side = "DOWN" if ua[mid] >= da[mid] else "UP"
        cheap_won = (resolution == cheap_side)

        # Classify flow at each tick
        # both_drop: new liquidity, both_rise: liquidity leaving
        # ua_down_da_up: flow toward DOWN, ua_up_da_down: flow toward UP
        for i in range(len(d_ua)):
            if np.isnan(d_ua[i]) or np.isnan(d_da[i]):
                continue
            if tr[i] > 700 or tr[i] < 150:
                continue

            if d_ua[i] < -0.005 and d_da[i] > 0.005:
                flow = "toward_DOWN"
            elif d_ua[i] > 0.005 and d_da[i] < -0.005:
                flow = "toward_UP"
            elif d_ua[i] < -0.005 and d_da[i] < -0.005:
                flow = "both_drop"
            elif d_ua[i] > 0.005 and d_da[i] > 0.005:
                flow = "both_rise"
            else:
                flow = "neutral"

            # Is flow toward cheap side?
            toward_cheap = (flow == f"toward_{cheap_side}")

            results.append({
                'dataset': dataset_name, 'slug': slug,
                'flow': flow, 'toward_cheap': toward_cheap,
                'time_remaining': tr[i],
                'cheap_won': cheap_won,
            })

    df = pd.DataFrame(results)
    if len(df) == 0:
        return pd.DataFrame()

    # Per-market: aggregate flow toward cheap in 600-300s window
    market_flow = []
    for slug in df['slug'].unique():
        sdf = df[(df['slug'] == slug) & (df['time_remaining'].between(300, 600))]
        if len(sdf) < 10:
            continue
        toward_pct = sdf['toward_cheap'].mean()
        cheap_won = sdf['cheap_won'].iloc[0]
        market_flow.append({
            'dataset': dataset_name, 'slug': slug,
            'toward_cheap_pct': toward_pct,
            'cheap_won': cheap_won,
            'n_ticks': len(sdf),
        })

    mf = pd.DataFrame(market_flow)
    if len(mf) < 10:
        return mf

    # Correlation
    corr, p = stats.pointbiserialr(mf['cheap_won'].astype(int), mf['toward_cheap_pct'])
    print(f"    Cross-side flow → cheap win: r={corr:.3f}, p={p:.3f}")

    return mf


def test_2_7_fade_footprint(obs_df, resolutions, dataset_name):
    """Test 2.7: FADE bot footprint detection at T=300s."""
    print(f"\n  [2.7] FADE Footprint — {dataset_name}")
    results = []

    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions:
            continue
        resolution = resolutions[slug]
        mdf = group.sort_values('timestamp_ms').reset_index(drop=True)

        tr = mdf['time_remaining_secs'].values.astype(float)
        ua = mdf['up_ask'].values.astype(float)
        da = mdf['down_ask'].values.astype(float)

        # Find observations near T=300 and T=270 (30s after FADE entry window opens)
        t310 = np.where((tr >= 305) & (tr <= 315))[0]
        t270 = np.where((tr >= 265) & (tr <= 275))[0]
        t240 = np.where((tr >= 235) & (tr <= 245))[0]

        if len(t310) == 0 or len(t270) == 0:
            continue

        # Prices at T=310 (before FADE) and T=270 (30s after FADE window opens)
        i310 = t310[len(t310)//2]
        i270 = t270[len(t270)//2]

        ua_310, da_310 = ua[i310], da[i310]
        ua_270, da_270 = ua[i270], da[i270]

        if any(pd.isna(x) for x in [ua_310, da_310, ua_270, da_270]):
            continue

        # Determine expensive side
        if ua_310 >= da_310:
            exp_side = "UP"
            cheap_side = "DOWN"
            exp_change = ua_270 - ua_310  # Expensive ask change (FADE buys → ask should rise)
            cheap_change = da_270 - da_310  # Cheap ask change (FADE sells → ask should drop)
        else:
            exp_side = "DOWN"
            cheap_side = "UP"
            exp_change = da_270 - da_310
            cheap_change = ua_270 - ua_310

        cheap_won = (resolution == cheap_side)

        # Recovery: cheap at T=240 vs T=270
        if len(t240) > 0:
            i240 = t240[len(t240)//2]
            if cheap_side == "DOWN":
                cheap_240 = da[i240]
            else:
                cheap_240 = ua[i240]
            if not pd.isna(cheap_240):
                recovery = cheap_240 - (da_270 if cheap_side == "DOWN" else ua_270)
            else:
                recovery = np.nan
        else:
            recovery = np.nan

        results.append({
            'dataset': dataset_name, 'slug': slug,
            'exp_change_310_to_270': exp_change,
            'cheap_change_310_to_270': cheap_change,
            'cheap_recovery_270_to_240': recovery,
            'cheap_won': cheap_won,
            'exp_side': exp_side,
        })

    df = pd.DataFrame(results)
    if len(df) == 0:
        return pd.DataFrame()

    print(f"    FADE pulse: avg exp_change={df['exp_change_310_to_270'].mean():.4f}, "
          f"avg cheap_change={df['cheap_change_310_to_270'].mean():.4f}")
    if not df['cheap_recovery_270_to_240'].isna().all():
        print(f"    Recovery 270→240: avg={df['cheap_recovery_270_to_240'].dropna().mean():.4f}")

    return df


def test_2_9_post_spike_recovery(obs_df, resolutions, dataset_name):
    """Test 2.9: Post-spike recovery analysis."""
    print(f"\n  [2.9] Post-Spike Recovery — {dataset_name}")
    results = []

    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions:
            continue
        resolution = resolutions[slug]
        mdf = group.sort_values('timestamp_ms').reset_index(drop=True)

        # Find spikes
        spike_mask = mdf['spike_detected'] == True
        if 'spike_detected' not in mdf.columns or spike_mask.sum() == 0:
            continue

        ts = mdf['timestamp_ms'].values.astype(float)
        ua = mdf['up_ask'].values.astype(float)
        da = mdf['down_ask'].values.astype(float)
        tr = mdf['time_remaining_secs'].values.astype(float)

        mid = len(mdf) // 2
        if pd.isna(ua[mid]) or pd.isna(da[mid]):
            continue
        cheap_side = "DOWN" if ua[mid] >= da[mid] else "UP"
        cheap = da if cheap_side == "DOWN" else ua
        cheap_won = (resolution == cheap_side)

        spike_indices = np.where(spike_mask.values)[0]

        for si in spike_indices:
            if tr[si] > 700 or tr[si] < 150:
                continue
            if pd.isna(cheap[si]):
                continue

            cheap_at_spike = cheap[si]
            spike_mag = mdf['spike_magnitude'].iloc[si] if 'spike_magnitude' in mdf.columns else 0

            # Track recovery at +5s, +10s, +30s, +60s
            for horizon_s in [5, 10, 30, 60]:
                target = ts[si] + horizon_s * 1000
                j = np.searchsorted(ts, target)
                if j >= len(cheap) or abs(ts[j] - target) > horizon_s * 500:
                    continue
                if pd.isna(cheap[j]):
                    continue

                recovery = cheap[j] - cheap_at_spike

                results.append({
                    'dataset': dataset_name, 'slug': slug,
                    'horizon_s': horizon_s,
                    'cheap_at_spike': cheap_at_spike,
                    'recovery': recovery,
                    'spike_magnitude': spike_mag,
                    'time_remaining': tr[si],
                    'cheap_won': cheap_won,
                })

    df = pd.DataFrame(results)
    if len(df) == 0:
        return pd.DataFrame()

    summary = []
    for horizon in [5, 10, 30, 60]:
        hdf = df[df['horizon_s'] == horizon]
        if len(hdf) < 20:
            continue

        # By cheap price level at spike
        for lo, hi, label in [(0.05, 0.20, '$0.05-0.20'), (0.20, 0.35, '$0.20-0.35'), (0.35, 0.50, '$0.35-0.50')]:
            bucket = hdf[(hdf['cheap_at_spike'] >= lo) & (hdf['cheap_at_spike'] < hi)]
            if len(bucket) < 10:
                continue
            summary.append({
                'dataset': dataset_name,
                'horizon_s': horizon,
                'cheap_level': label,
                'n_spikes': len(bucket),
                'avg_recovery': bucket['recovery'].mean(),
                'pct_positive_recovery': (bucket['recovery'] > 0).mean(),
                'avg_spike_magnitude': bucket['spike_magnitude'].mean(),
                'cheap_win_rate': bucket['cheap_won'].mean(),
            })

    return pd.DataFrame(summary)


def test_2_8_spread_dynamics(obs_df, resolutions, dataset_name):
    """Test 2.8: Spread dynamics as regime indicator."""
    print(f"\n  [2.8] Spread Dynamics — {dataset_name}")
    results = []

    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions:
            continue
        resolution = resolutions[slug]
        mdf = group.sort_values('timestamp_ms').reset_index(drop=True)

        ua = mdf['up_ask'].values.astype(float)
        da = mdf['down_ask'].values.astype(float)
        tr = mdf['time_remaining_secs'].values.astype(float)

        pair_cost = ua + da
        valid = ~(np.isnan(pair_cost))

        # Pair cost in T=600-400 window
        window = (tr >= 400) & (tr <= 600) & valid
        if window.sum() < 10:
            continue

        pc_window = pair_cost[window]
        spread_vol = np.std(pc_window)
        spread_mean = np.mean(pc_window)
        spread_slope = np.polyfit(np.arange(window.sum()), pc_window, 1)[0] if window.sum() >= 3 else 0

        mid = len(mdf) // 2
        if pd.isna(ua[mid]) or pd.isna(da[mid]):
            continue
        cheap_side = "DOWN" if ua[mid] >= da[mid] else "UP"
        cheap_won = (resolution == cheap_side)

        results.append({
            'dataset': dataset_name, 'slug': slug,
            'spread_volatility': spread_vol,
            'spread_mean': spread_mean,
            'spread_slope': spread_slope,
            'cheap_won': cheap_won,
        })

    df = pd.DataFrame(results)
    if len(df) == 0:
        return pd.DataFrame()

    # Analyze
    for metric in ['spread_volatility', 'spread_mean', 'spread_slope']:
        vals = df[metric].dropna()
        if len(vals) < 10:
            continue
        q50 = vals.median()
        above = df[df[metric] >= q50]
        below = df[df[metric] < q50]
        if len(above) >= 5 and len(below) >= 5:
            print(f"    {metric}: above_median CWR={above['cheap_won'].mean():.3f} (n={len(above)}), "
                  f"below_median CWR={below['cheap_won'].mean():.3f} (n={len(below)})")

    return df


# =============================================================================
# PHASE 4: Strategy Family Tests
# =============================================================================

def test_4_1_overreaction(obs_df, resolutions, dataset_name):
    """Test 4.1: Overreaction detection — do Polymarket prices overreact to BTC moves?"""
    print(f"\n  [4.1] Overreaction Detection — {dataset_name}")
    results = []

    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions:
            continue
        resolution = resolutions[slug]
        mdf = group.sort_values('timestamp_ms').reset_index(drop=True)
        if len(mdf) < 30:
            continue

        btc = mdf['binance_price'].values.astype(float)
        ua = mdf['up_ask'].values.astype(float)
        da = mdf['down_ask'].values.astype(float)
        tr = mdf['time_remaining_secs'].values.astype(float)
        ts = mdf['timestamp_ms'].values.astype(float)

        mid = len(mdf) // 2
        if pd.isna(ua[mid]) or pd.isna(da[mid]):
            continue
        cheap_side = "DOWN" if ua[mid] >= da[mid] else "UP"
        cheap = da if cheap_side == "DOWN" else ua
        cheap_won = (resolution == cheap_side)

        # Every 10 ticks: compare BTC change to Polymarket change
        for i in range(10, len(mdf) - 30, 10):
            if any(pd.isna(x) for x in [btc[i], btc[i-10], cheap[i], cheap[i-10]]):
                continue
            if tr[i] > 700 or tr[i] < 150:
                continue
            if btc[i-10] <= 0 or cheap[i-10] <= 0:
                continue

            btc_change_bps = (btc[i] - btc[i-10]) / btc[i-10] * 10000
            cheap_change = cheap[i] - cheap[i-10]

            # "Expected" cheap change = proportional to BTC change
            # When BTC goes UP, DOWN cheap should drop (negative change expected)
            # When BTC goes DOWN, UP cheap should drop (negative change expected)
            if cheap_side == "DOWN":
                # BTC up → DOWN drops → expected negative
                expected_dir = -1
            else:
                # BTC down → UP drops → expected positive for UP when BTC down
                expected_dir = 1

            expected_cheap_change = expected_dir * btc_change_bps * 0.001  # Crude scaling

            if abs(expected_cheap_change) < 0.001:
                continue  # Too small to classify

            # Overreaction: actual change >> expected
            if abs(cheap_change) > abs(expected_cheap_change) * 2:
                reaction = "overreaction"
            elif abs(cheap_change) < abs(expected_cheap_change) * 0.3:
                reaction = "underreaction"
            else:
                reaction = "proportional"

            # Does it revert in next 30s?
            target = ts[i] + 30000
            j = np.searchsorted(ts, target)
            if j >= len(cheap) or abs(ts[j] - target) > 6000:
                continue
            if pd.isna(cheap[j]):
                continue
            reversion = cheap[j] - cheap[i]

            results.append({
                'dataset': dataset_name, 'slug': slug,
                'reaction': reaction,
                'btc_change_bps': btc_change_bps,
                'cheap_change': cheap_change,
                'reversion_30s': reversion,
                'time_remaining': tr[i],
                'cheap_won': cheap_won,
            })

    df = pd.DataFrame(results)
    if len(df) == 0:
        return pd.DataFrame()

    summary = []
    for reaction in ['overreaction', 'underreaction', 'proportional']:
        rdf = df[df['reaction'] == reaction]
        if len(rdf) < 20:
            continue
        summary.append({
            'dataset': dataset_name,
            'reaction': reaction,
            'n_events': len(rdf),
            'avg_reversion_30s': rdf['reversion_30s'].mean(),
            'pct_reverts': (rdf['reversion_30s'] * np.sign(-rdf['cheap_change']) > 0).mean(),
            'cheap_win_rate': rdf['cheap_won'].mean(),
        })

    return pd.DataFrame(summary)


def test_4_3_bothside_dca(obs_df, resolutions, dataset_name):
    """Test 4.3: Both-side DCA pair cost achievement (Gabagool-inspired)."""
    print(f"\n  [4.3] Both-Side DCA — {dataset_name}")
    results = []

    # Maker bid levels for each side
    up_bid_levels = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80]
    down_bid_levels = [0.45, 0.40, 0.35, 0.30, 0.25, 0.20]

    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions:
            continue
        resolution = resolutions[slug]
        mdf = group.sort_values('timestamp_ms').reset_index(drop=True)
        if len(mdf) < 50:
            continue

        ua = mdf['up_ask'].values.astype(float)
        da = mdf['down_ask'].values.astype(float)
        tr = mdf['time_remaining_secs'].values.astype(float)

        # Simulate maker fills on both sides (price-touch: fill when ask <= our bid)
        up_fills = []
        down_fills = []

        for bid in up_bid_levels:
            # Find first time up_ask <= bid
            fill_mask = ua <= bid
            fill_idx = np.where(fill_mask & (tr <= 800) & (tr >= 120))[0]
            if len(fill_idx) > 0:
                up_fills.append(bid)  # Fill at our bid price (maker)

        for bid in down_bid_levels:
            fill_mask = da <= bid
            fill_idx = np.where(fill_mask & (tr <= 800) & (tr >= 120))[0]
            if len(fill_idx) > 0:
                down_fills.append(bid)

        if len(up_fills) == 0 or len(down_fills) == 0:
            continue

        avg_up = np.mean(up_fills)
        avg_down = np.mean(down_fills)
        pair_cost = avg_up + avg_down

        results.append({
            'dataset': dataset_name, 'slug': slug,
            'n_up_fills': len(up_fills),
            'n_down_fills': len(down_fills),
            'avg_up_fill': avg_up,
            'avg_down_fill': avg_down,
            'pair_cost': pair_cost,
            'pair_cost_sub_1': pair_cost < 1.0,
            'resolution': resolution,
        })

    df = pd.DataFrame(results)
    if len(df) == 0:
        return pd.DataFrame()

    print(f"    Markets with both-side fills: {len(df)}")
    print(f"    Avg pair cost: ${df['pair_cost'].mean():.4f}")
    print(f"    Pair cost < $1.00: {df['pair_cost_sub_1'].mean():.1%}")
    print(f"    Avg up fills: {df['n_up_fills'].mean():.1f}, Avg down fills: {df['n_down_fills'].mean():.1f}")

    return df


def test_4_5_cheap_first_probe(obs_df, resolutions, dataset_name):
    """Test 4.5: Cheap-first probe → conditional expensive entry (Baguette-style)."""
    print(f"\n  [4.5] Cheap-First Probe — {dataset_name}")
    results = []

    # Probe: buy cheap side at T=400-300 via maker bid
    probe_prices = [0.15, 0.20, 0.25, 0.30]

    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions:
            continue
        resolution = resolutions[slug]
        mdf = group.sort_values('timestamp_ms').reset_index(drop=True)
        if len(mdf) < 50:
            continue

        ua = mdf['up_ask'].values.astype(float)
        da = mdf['down_ask'].values.astype(float)
        tr = mdf['time_remaining_secs'].values.astype(float)
        ts = mdf['timestamp_ms'].values.astype(float)

        # Determine cheap side at T~500 (before probe)
        t500 = np.where((tr >= 490) & (tr <= 510))[0]
        if len(t500) == 0:
            continue
        i500 = t500[len(t500)//2]
        if pd.isna(ua[i500]) or pd.isna(da[i500]):
            continue

        cheap_side = "DOWN" if ua[i500] >= da[i500] else "UP"
        cheap = da if cheap_side == "DOWN" else ua
        expensive = ua if cheap_side == "DOWN" else da
        cheap_won = (resolution == cheap_side)

        for probe_bid in probe_prices:
            # Find fill in T=400-200 window
            probe_window = (tr <= 400) & (tr >= 200)
            fill_mask = probe_window & (cheap <= probe_bid)
            fill_idx = np.where(fill_mask)[0]

            if len(fill_idx) == 0:
                continue  # No fill at this probe price

            probe_fill_idx = fill_idx[0]
            probe_fill_price = probe_bid  # Maker fill at our bid

            # After probe fills, check if expensive side dips (for pair entry)
            # Look for expensive ask dip in next 120s
            post_probe = (np.arange(len(ts)) > probe_fill_idx) & (tr >= 120)
            if post_probe.sum() == 0:
                continue

            exp_after = expensive[post_probe]
            min_exp = np.nanmin(exp_after)

            # Pair cost if we buy expensive at its minimum
            pair_cost_best = probe_fill_price + min_exp

            # Average expensive in post-probe window
            avg_exp = np.nanmean(exp_after)
            pair_cost_avg = probe_fill_price + avg_exp

            # PnL scenarios
            if cheap_won:
                # Cheap wins → cheap goes to $1, expensive goes to $0
                probe_pnl = (1.0 - probe_fill_price) * 25  # Huge win
            else:
                # Cheap loses → cheap goes to $0
                probe_pnl = -probe_fill_price * 25  # Small loss

            results.append({
                'dataset': dataset_name, 'slug': slug,
                'probe_bid': probe_bid,
                'probe_fill_price': probe_fill_price,
                'min_expensive_after': min_exp,
                'avg_expensive_after': avg_exp,
                'pair_cost_best': pair_cost_best,
                'pair_cost_avg': pair_cost_avg,
                'pair_viable': pair_cost_best < 1.0,
                'cheap_won': cheap_won,
                'naked_probe_pnl': probe_pnl,
            })

    df = pd.DataFrame(results)
    if len(df) == 0:
        return pd.DataFrame()

    # Summarize by probe price
    summary = []
    for pb in probe_prices:
        pdf = df[df['probe_bid'] == pb]
        if len(pdf) < 5:
            continue
        summary.append({
            'dataset': dataset_name,
            'probe_bid': pb,
            'n_fills': len(pdf),
            'fill_rate': len(pdf) / obs_df['market_slug'].nunique(),
            'cheap_win_rate': pdf['cheap_won'].mean(),
            'avg_pair_cost_best': pdf['pair_cost_best'].mean(),
            'pct_pair_viable': pdf['pair_viable'].mean(),
            'avg_naked_pnl': pdf['naked_probe_pnl'].mean(),
            'avg_naked_pnl_if_wins': pdf[pdf['cheap_won']]['naked_probe_pnl'].mean() if pdf['cheap_won'].any() else 0,
            'avg_naked_pnl_if_loses': pdf[~pdf['cheap_won']]['naked_probe_pnl'].mean() if (~pdf['cheap_won']).any() else 0,
        })

    return pd.DataFrame(summary)


# =============================================================================
# MAIN: Run all tests
# =============================================================================
def main():
    print("=" * 80)
    print("PHOENIX V2 — Comprehensive Signal Research Study")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    # Load strike data
    strikes = load_strikes()
    print(f"\nLoaded {len(strikes)} market strikes")

    # Result containers
    all_results = {}

    # Load all datasets
    datasets = {}
    for key in tqdm(DATASETS.keys(), desc="Loading datasets"):
        obs_df, resolutions = load_dataset(key)
        if obs_df is not None:
            datasets[key] = (obs_df, resolutions)

    print(f"\nLoaded {len(datasets)} datasets")

    # =========================================================================
    # PHASE 1: Market-Level Analysis
    # =========================================================================
    print("\n" + "=" * 80)
    print("PHASE 1: MARKET-LEVEL ANALYSIS")
    print("=" * 80)

    # Test 1.1: Regime (Spread)
    t1_1_results = []
    for key, (obs_df, resolutions) in tqdm(datasets.items(), desc="Test 1.1"):
        r = test_1_1_regime_spread(obs_df, resolutions, key)
        if len(r) > 0:
            t1_1_results.append(r)
    if t1_1_results:
        all_results['1.1_regime_spread'] = pd.concat(t1_1_results, ignore_index=True)
        all_results['1.1_regime_spread'].to_csv(OUTPUT_DIR / "test_1_1_regime_spread.csv", index=False)
        print(f"\n  Test 1.1 saved: {len(all_results['1.1_regime_spread'])} rows")

    # Test 1.2: Strike Proximity
    t1_2_results = []
    for key, (obs_df, resolutions) in tqdm(datasets.items(), desc="Test 1.2"):
        r = test_1_2_strike_proximity(obs_df, resolutions, strikes, key)
        if len(r) > 0:
            t1_2_results.append(r)
    if t1_2_results:
        all_results['1.2_strike_proximity'] = pd.concat(t1_2_results, ignore_index=True)
        all_results['1.2_strike_proximity'].to_csv(OUTPUT_DIR / "test_1_2_strike_proximity.csv", index=False)
        print(f"\n  Test 1.2 saved: {len(all_results['1.2_strike_proximity'])} rows")

    # Test 1.3: Trajectory
    t1_3_results = []
    for key, (obs_df, resolutions) in tqdm(datasets.items(), desc="Test 1.3"):
        r = test_1_3_trajectory(obs_df, resolutions, key)
        if len(r) > 0:
            t1_3_results.append(r)
    if t1_3_results:
        all_results['1.3_trajectory'] = pd.concat(t1_3_results, ignore_index=True)
        all_results['1.3_trajectory'].to_csv(OUTPUT_DIR / "test_1_3_trajectory.csv", index=False)
        print(f"\n  Test 1.3 saved: {len(all_results['1.3_trajectory'])} rows")

    # Test 1.4: Feature Importance
    t1_4_results = []
    for key, (obs_df, resolutions) in tqdm(datasets.items(), desc="Test 1.4"):
        r = test_1_4_feature_importance(obs_df, resolutions, strikes, key)
        if len(r) > 0:
            t1_4_results.append(r)
    if t1_4_results:
        all_results['1.4_features'] = pd.concat(t1_4_results, ignore_index=True)
        all_results['1.4_features'].to_csv(OUTPUT_DIR / "test_1_4_features.csv", index=False)
        print(f"\n  Test 1.4 saved: {len(all_results['1.4_features'])} rows")

    # Test 1.5: Correlation Decay
    t1_5_results = []
    for key, (obs_df, resolutions) in tqdm(datasets.items(), desc="Test 1.5"):
        r = test_1_5_correlation_decay(obs_df, resolutions, key)
        if len(r) > 0:
            t1_5_results.append(r)
    if t1_5_results:
        all_results['1.5_corr_decay'] = pd.concat(t1_5_results, ignore_index=True)
        all_results['1.5_corr_decay'].to_csv(OUTPUT_DIR / "test_1_5_correlation_decay.csv", index=False)
        print(f"\n  Test 1.5 saved: {len(all_results['1.5_corr_decay'])} rows")

    # Test 1.6: Strike Crossings
    t1_6_results = []
    for key, (obs_df, resolutions) in tqdm(datasets.items(), desc="Test 1.6"):
        r = test_1_6_strike_crossings(obs_df, resolutions, strikes, key)
        if len(r) > 0:
            t1_6_results.append(r)
    if t1_6_results:
        all_results['1.6_crossings'] = pd.concat(t1_6_results, ignore_index=True)
        all_results['1.6_crossings'].to_csv(OUTPUT_DIR / "test_1_6_strike_crossings.csv", index=False)
        print(f"\n  Test 1.6 saved: {len(all_results['1.6_crossings'])} rows")

    # Test 1.7: BTC Volatility
    t1_7_results = []
    for key, (obs_df, resolutions) in tqdm(datasets.items(), desc="Test 1.7"):
        r = test_1_7_btc_volatility(obs_df, resolutions, key)
        if len(r) > 0:
            t1_7_results.append(r)
    if t1_7_results:
        all_results['1.7_btc_vol'] = pd.concat(t1_7_results, ignore_index=True)
        all_results['1.7_btc_vol'].to_csv(OUTPUT_DIR / "test_1_7_btc_volatility.csv", index=False)
        print(f"\n  Test 1.7 saved: {len(all_results['1.7_btc_vol'])} rows")

    # =========================================================================
    # PHASE 2: Signal-Level Analysis
    # =========================================================================
    print("\n" + "=" * 80)
    print("PHASE 2: SIGNAL-LEVEL ANALYSIS")
    print("=" * 80)

    # Test 2.1: Velocity Toward Strike
    t2_1_results = []
    for key, (obs_df, resolutions) in tqdm(datasets.items(), desc="Test 2.1"):
        r = test_2_1_velocity_toward_strike(obs_df, resolutions, strikes, key)
        if len(r) > 0:
            t2_1_results.append(r)
    if t2_1_results:
        all_results['2.1_vel_strike'] = pd.concat(t2_1_results, ignore_index=True)
        all_results['2.1_vel_strike'].to_csv(OUTPUT_DIR / "test_2_1_velocity_toward_strike.csv", index=False)
        print(f"\n  Test 2.1 saved: {len(all_results['2.1_vel_strike'])} rows")

    # Test 2.2: Kinematic State
    t2_2_results = []
    for key, (obs_df, resolutions) in tqdm(datasets.items(), desc="Test 2.2"):
        r = test_2_2_kinematic_state(obs_df, resolutions, key)
        if len(r) > 0:
            t2_2_results.append(r)
    if t2_2_results:
        all_results['2.2_kinematic'] = pd.concat(t2_2_results, ignore_index=True)
        all_results['2.2_kinematic'].to_csv(OUTPUT_DIR / "test_2_2_kinematic_state.csv", index=False)
        print(f"\n  Test 2.2 saved: {len(all_results['2.2_kinematic'])} rows")

    # Test 2.5: Price Support
    t2_5_results = []
    for key, (obs_df, resolutions) in tqdm(datasets.items(), desc="Test 2.5"):
        r = test_2_5_price_support(obs_df, resolutions, key)
        if len(r) > 0:
            t2_5_results.append(r)
    if t2_5_results:
        all_results['2.5_support'] = pd.concat(t2_5_results, ignore_index=True)
        all_results['2.5_support'].to_csv(OUTPUT_DIR / "test_2_5_price_support.csv", index=False)
        print(f"\n  Test 2.5 saved: {len(all_results['2.5_support'])} rows")

    # Test 2.6: Cross-Side Flow
    t2_6_results = []
    for key, (obs_df, resolutions) in tqdm(datasets.items(), desc="Test 2.6"):
        r = test_2_6_crossside_flow(obs_df, resolutions, key)
        if len(r) > 0:
            t2_6_results.append(r)
    if t2_6_results:
        all_results['2.6_flow'] = pd.concat(t2_6_results, ignore_index=True)
        all_results['2.6_flow'].to_csv(OUTPUT_DIR / "test_2_6_crossside_flow.csv", index=False)
        print(f"\n  Test 2.6 saved: {len(all_results['2.6_flow'])} rows")

    # Test 2.7: FADE Footprint
    t2_7_results = []
    for key, (obs_df, resolutions) in tqdm(datasets.items(), desc="Test 2.7"):
        r = test_2_7_fade_footprint(obs_df, resolutions, key)
        if len(r) > 0:
            t2_7_results.append(r)
    if t2_7_results:
        all_results['2.7_fade'] = pd.concat(t2_7_results, ignore_index=True)
        all_results['2.7_fade'].to_csv(OUTPUT_DIR / "test_2_7_fade_footprint.csv", index=False)
        print(f"\n  Test 2.7 saved: {len(all_results['2.7_fade'])} rows")

    # Test 2.8: Spread Dynamics
    t2_8_results = []
    for key, (obs_df, resolutions) in tqdm(datasets.items(), desc="Test 2.8"):
        r = test_2_8_spread_dynamics(obs_df, resolutions, key)
        if len(r) > 0:
            t2_8_results.append(r)
    if t2_8_results:
        all_results['2.8_spread'] = pd.concat(t2_8_results, ignore_index=True)
        all_results['2.8_spread'].to_csv(OUTPUT_DIR / "test_2_8_spread_dynamics.csv", index=False)
        print(f"\n  Test 2.8 saved: {len(all_results['2.8_spread'])} rows")

    # Test 2.9: Post-Spike Recovery
    t2_9_results = []
    for key, (obs_df, resolutions) in tqdm(datasets.items(), desc="Test 2.9"):
        r = test_2_9_post_spike_recovery(obs_df, resolutions, key)
        if len(r) > 0:
            t2_9_results.append(r)
    if t2_9_results:
        all_results['2.9_spike_recovery'] = pd.concat(t2_9_results, ignore_index=True)
        all_results['2.9_spike_recovery'].to_csv(OUTPUT_DIR / "test_2_9_post_spike_recovery.csv", index=False)
        print(f"\n  Test 2.9 saved: {len(all_results['2.9_spike_recovery'])} rows")

    # =========================================================================
    # PHASE 4: Strategy Family Tests
    # =========================================================================
    print("\n" + "=" * 80)
    print("PHASE 4: STRATEGY FAMILY TESTS")
    print("=" * 80)

    # Test 4.1: Overreaction Detection
    t4_1_results = []
    for key, (obs_df, resolutions) in tqdm(datasets.items(), desc="Test 4.1"):
        r = test_4_1_overreaction(obs_df, resolutions, key)
        if len(r) > 0:
            t4_1_results.append(r)
    if t4_1_results:
        all_results['4.1_overreaction'] = pd.concat(t4_1_results, ignore_index=True)
        all_results['4.1_overreaction'].to_csv(OUTPUT_DIR / "test_4_1_overreaction.csv", index=False)
        print(f"\n  Test 4.1 saved: {len(all_results['4.1_overreaction'])} rows")

    # Test 4.3: Both-Side DCA
    t4_3_results = []
    for key, (obs_df, resolutions) in tqdm(datasets.items(), desc="Test 4.3"):
        r = test_4_3_bothside_dca(obs_df, resolutions, key)
        if len(r) > 0:
            t4_3_results.append(r)
    if t4_3_results:
        all_results['4.3_dca'] = pd.concat(t4_3_results, ignore_index=True)
        all_results['4.3_dca'].to_csv(OUTPUT_DIR / "test_4_3_bothside_dca.csv", index=False)
        print(f"\n  Test 4.3 saved: {len(all_results['4.3_dca'])} rows")

    # Test 4.5: Cheap-First Probe
    t4_5_results = []
    for key, (obs_df, resolutions) in tqdm(datasets.items(), desc="Test 4.5"):
        r = test_4_5_cheap_first_probe(obs_df, resolutions, key)
        if len(r) > 0:
            t4_5_results.append(r)
    if t4_5_results:
        all_results['4.5_probe'] = pd.concat(t4_5_results, ignore_index=True)
        all_results['4.5_probe'].to_csv(OUTPUT_DIR / "test_4_5_cheap_first_probe.csv", index=False)
        print(f"\n  Test 4.5 saved: {len(all_results['4.5_probe'])} rows")

    # =========================================================================
    # SUMMARY REPORT
    # =========================================================================
    print("\n" + "=" * 80)
    print("GENERATING SUMMARY REPORT")
    print("=" * 80)

    report = []
    report.append("# PHOENIX V2 — Signal Research Results")
    report.append(f"\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report.append(f"Datasets: {', '.join(datasets.keys())}")
    report.append(f"Total tests run: {len(all_results)}")

    # Phase 1 Summary
    report.append("\n## PHASE 1: Market-Level Analysis\n")

    if '1.1_regime_spread' in all_results:
        df = all_results['1.1_regime_spread']
        report.append("### Test 1.1: Cheap Win Rate by Spread Regime")
        agg = df.groupby('spread_bucket').agg(
            avg_cheap_wr=('cheap_win_rate', 'mean'),
            total_markets=('n_markets', 'sum'),
        ).reset_index()
        for _, row in agg.iterrows():
            report.append(f"  Spread {row['spread_bucket']}: CWR={row['avg_cheap_wr']:.1%} (n={row['total_markets']:.0f})")

    if '1.2_strike_proximity' in all_results:
        df = all_results['1.2_strike_proximity']
        report.append("\n### Test 1.2: Cheap Win Rate by Strike Proximity")
        agg = df.groupby('proximity_bucket_bps').agg(
            avg_cheap_wr=('cheap_win_rate', 'mean'),
            total_markets=('n_markets', 'sum'),
        ).reset_index()
        for _, row in agg.iterrows():
            report.append(f"  {row['proximity_bucket_bps']} bps: CWR={row['avg_cheap_wr']:.1%} (n={row['total_markets']:.0f})")

    if '1.3_trajectory' in all_results:
        df = all_results['1.3_trajectory']
        report.append("\n### Test 1.3: Trajectory Divergence")
        # Find earliest significant divergence
        sig = df[df['p_value'] < 0.05].sort_values('time_remaining', ascending=False)
        if len(sig) > 0:
            earliest = sig.iloc[0]
            report.append(f"  Earliest significant divergence: T={earliest['time_remaining']:.0f}s (p={earliest['p_value']:.4f})")
            report.append(f"  Win avg cheap: ${earliest['win_avg_cheap_ask']:.3f} vs Lose avg: ${earliest['lose_avg_cheap_ask']:.3f}")
        else:
            report.append("  No significant divergence found")

    if '1.4_features' in all_results:
        df = all_results['1.4_features']
        report.append("\n### Test 1.4: Feature Importance (sorted by AUC)")
        # Top features at T=600
        t600 = df[df['eval_time'] == 600].copy()
        if len(t600) > 0:
            # Average AUC across datasets
            feat_avg = t600.groupby('feature').agg(
                avg_auc=('auc', 'mean'),
                avg_p=('p_mannwhitney', 'mean'),
                avg_corr=('correlation', 'mean'),
                n_datasets=('dataset', 'nunique'),
            ).sort_values('avg_auc', key=lambda x: abs(x - 0.5), ascending=False).head(15)
            report.append("  **At T=600s:**")
            for feat, row in feat_avg.iterrows():
                report.append(f"    {feat}: AUC={row['avg_auc']:.3f}, r={row['avg_corr']:.3f}, p={row['avg_p']:.3f} ({row['n_datasets']:.0f} datasets)")

        t300 = df[df['eval_time'] == 300].copy()
        if len(t300) > 0:
            feat_avg = t300.groupby('feature').agg(
                avg_auc=('auc', 'mean'),
                avg_p=('p_mannwhitney', 'mean'),
                avg_corr=('correlation', 'mean'),
                n_datasets=('dataset', 'nunique'),
            ).sort_values('avg_auc', key=lambda x: abs(x - 0.5), ascending=False).head(15)
            report.append("  **At T=300s:**")
            for feat, row in feat_avg.iterrows():
                report.append(f"    {feat}: AUC={row['avg_auc']:.3f}, r={row['avg_corr']:.3f}, p={row['avg_p']:.3f} ({row['n_datasets']:.0f} datasets)")

    if '1.5_corr_decay' in all_results:
        df = all_results['1.5_corr_decay']
        report.append("\n### Test 1.5: Correlation Decay")
        agg = df.groupby('delta_seconds').agg(
            avg_corr=('mean_correlation', 'mean'),
            n=('n_markets', 'sum'),
        ).reset_index()
        for _, row in agg.iterrows():
            report.append(f"  Δ={row['delta_seconds']:.0f}s: r={row['avg_corr']:.3f} (n={row['n']:.0f})")

    if '1.7_btc_vol' in all_results:
        df = all_results['1.7_btc_vol']
        report.append("\n### Test 1.7: BTC Volatility Regime")
        agg = df.groupby('vol_bucket_bps').agg(
            avg_cwr=('cheap_win_rate', 'mean'),
            total=('n_markets', 'sum'),
            avg_spikes=('avg_spike_count', 'mean'),
        ).reset_index()
        for _, row in agg.iterrows():
            report.append(f"  {row['vol_bucket_bps']} bps: CWR={row['avg_cwr']:.1%}, spikes={row['avg_spikes']:.1f} (n={row['total']:.0f})")

    # Phase 2 Summary
    report.append("\n## PHASE 2: Signal-Level Analysis\n")

    if '2.1_vel_strike' in all_results:
        df = all_results['2.1_vel_strike']
        report.append("### Test 2.1: Velocity Toward Strike")
        for horizon in ['30s', '60s']:
            hdf = df[df['horizon'] == horizon]
            if len(hdf) == 0:
                continue
            report.append(f"  **Horizon {horizon}:**")
            for _, row in hdf.groupby('vel_bucket').agg(
                avg_change=('avg_cheap_change', 'mean'),
                avg_pct_pos=('pct_positive', 'mean'),
                total=('n_obs', 'sum'),
            ).reset_index().iterrows():
                report.append(f"    {row['vel_bucket']}: avg_change={row['avg_change']:.4f}, pct_positive={row['avg_pct_pos']:.1%} (n={row['total']:.0f})")

    if '2.2_kinematic' in all_results:
        df = all_results['2.2_kinematic']
        report.append("\n### Test 2.2: Kinematic State Octants")
        # Average across datasets
        octant_df = df[df['octant'].apply(lambda x: isinstance(x, (int, np.integer)))]
        if len(octant_df) > 0:
            agg = octant_df.groupby(['octant', 'octant_label']).agg(
                avg_cheap_change=('avg_cheap_change_30s', 'mean'),
                avg_pct_rises=('pct_cheap_rises', 'mean'),
                total=('n_obs', 'sum'),
                avg_cwr=('cheap_win_rate', 'mean'),
            ).reset_index().sort_values('avg_cheap_change', ascending=False)
            for _, row in agg.iterrows():
                report.append(f"  {row['octant_label']}: Δcheap={row['avg_cheap_change']:.5f}, "
                              f"rises={row['avg_pct_rises']:.1%}, CWR={row['avg_cwr']:.1%} (n={row['total']:.0f})")

        # Deceleration
        decel_df = df[df['octant'].apply(lambda x: isinstance(x, str))]
        if len(decel_df) > 0:
            report.append("  **Deceleration:**")
            for _, row in decel_df.groupby('octant_label').agg(
                avg_change=('avg_cheap_change_30s', 'mean'),
                avg_rises=('pct_cheap_rises', 'mean'),
                total=('n_obs', 'sum'),
            ).reset_index().iterrows():
                report.append(f"    {row['octant_label']}: Δcheap={row['avg_change']:.5f}, rises={row['avg_rises']:.1%} (n={row['total']:.0f})")

    if '2.9_spike_recovery' in all_results:
        df = all_results['2.9_spike_recovery']
        report.append("\n### Test 2.9: Post-Spike Recovery")
        agg = df.groupby(['horizon_s', 'cheap_level']).agg(
            avg_recovery=('avg_recovery', 'mean'),
            avg_pct_pos=('pct_positive_recovery', 'mean'),
            total=('n_spikes', 'sum'),
        ).reset_index()
        for _, row in agg.iterrows():
            report.append(f"  {row['cheap_level']} @ {row['horizon_s']}s: "
                          f"avg_recovery={row['avg_recovery']:.4f}, pct_positive={row['avg_pct_pos']:.1%} (n={row['total']:.0f})")

    # Phase 4 Summary
    report.append("\n## PHASE 4: Strategy Family Tests\n")

    if '4.1_overreaction' in all_results:
        df = all_results['4.1_overreaction']
        report.append("### Test 4.1: Overreaction Detection")
        agg = df.groupby('reaction').agg(
            avg_reversion=('avg_reversion_30s', 'mean'),
            avg_revert_pct=('pct_reverts', 'mean'),
            total=('n_events', 'sum'),
        ).reset_index()
        for _, row in agg.iterrows():
            report.append(f"  {row['reaction']}: avg_reversion={row['avg_reversion']:.4f}, "
                          f"revert_pct={row['avg_revert_pct']:.1%} (n={row['total']:.0f})")

    if '4.3_dca' in all_results:
        df = all_results['4.3_dca']
        report.append("\n### Test 4.3: Both-Side DCA")
        report.append(f"  Total markets with both-side fills: {len(df)}")
        report.append(f"  Avg pair cost: ${df['pair_cost'].mean():.4f}")
        report.append(f"  Pair cost < $1.00: {df['pair_cost_sub_1'].mean():.1%}")
        # By dataset
        for ds, gdf in df.groupby('dataset'):
            report.append(f"    {ds}: avg_pc=${gdf['pair_cost'].mean():.4f}, sub_$1={gdf['pair_cost_sub_1'].mean():.1%} (n={len(gdf)})")

    if '4.5_probe' in all_results:
        df = all_results['4.5_probe']
        report.append("\n### Test 4.5: Cheap-First Probe (Baguette-Style)")
        agg = df.groupby('probe_bid').agg(
            total_fills=('n_fills', 'sum'),
            avg_fill_rate=('fill_rate', 'mean'),
            avg_cwr=('cheap_win_rate', 'mean'),
            avg_pair_best=('avg_pair_cost_best', 'mean'),
            avg_pair_viable=('pct_pair_viable', 'mean'),
            avg_naked_pnl=('avg_naked_pnl', 'mean'),
        ).reset_index()
        for _, row in agg.iterrows():
            report.append(f"  Probe ${row['probe_bid']:.2f}: fills={row['total_fills']:.0f}, "
                          f"CWR={row['avg_cwr']:.1%}, pair_viable={row['avg_pair_viable']:.1%}, "
                          f"naked_EV=${row['avg_naked_pnl']:.2f}")

    # Write report
    report_text = "\n".join(report)
    report_path = OUTPUT_DIR / "COMPREHENSIVE_SIGNAL_REPORT.md"
    with open(report_path, 'w') as f:
        f.write(report_text)

    print(f"\n{'=' * 80}")
    print(f"COMPLETE! Report saved to: {report_path}")
    print(f"Individual test CSVs saved to: {OUTPUT_DIR}")
    print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 80}")

    # Print report to stdout
    print("\n" + report_text)


if __name__ == "__main__":
    main()
