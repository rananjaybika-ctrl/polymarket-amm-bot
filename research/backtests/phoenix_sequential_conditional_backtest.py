#!/usr/bin/env python3
"""
Sequential Conditional Backtest — Cheap-First Probe → Conditional Expensive Entry
==================================================================================

COPIED FROM: phoenix_main_backtest.py (validated data loading + spike detection)
PURPOSE: Test time-separated position building:
  1. Buy cheap side via maker at T=cheap_time (insurance/probe)
  2. Wait for market to develop
  3. At T=expensive_time: IF market confirms direction → buy expensive side via PHOENIX mechanism
  4. Hold all positions to resolution

This tests whether cheap-first insurance reduces risk enough to improve
risk-adjusted returns, even if absolute EV decreases.

Grid search over:
  - cheap_entry_time: when to place cheap bid (T=800 to T=400)
  - cheap_offset: how far below current ask to bid
  - expensive_threshold: minimum ask to confirm direction
  - imbalance_ratio: shares_expensive / shares_cheap

Usage:
    python research/backtests/phoenix_sequential_conditional_backtest.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Dict
import sys
import math
from datetime import datetime
from itertools import product
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.core import polymarket_taker_fee

# =============================================================================
# CONSTANTS
# =============================================================================
STARTING_CAPITAL = 170.0
MAX_CAPITAL_FRACTION = 0.50
EWMA_HALFLIFE_MS = 1000
SKIP_UTC_HOURS = frozenset()
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10
DECEL_WINDOWS = [(600, 180), (600, 120), (300, 180), (300, 120)]


# =============================================================================
# DATA CLASSES
# =============================================================================
@dataclass
class SeqConfig:
    name: str
    # Cheap side entry
    cheap_entry_start_secs: float = 700.0  # Start looking for cheap entry
    cheap_entry_end_secs: float = 400.0    # Stop looking for cheap entry
    cheap_offset: float = 0.03             # Bid below current cheap_ask
    cheap_shares: int = 10                 # Shares on cheap side (insurance)
    # Expensive side entry (PHOENIX-style, spike-based)
    exp_entry_start_secs: float = 300.0
    exp_entry_end_secs: float = 120.0
    exp_offset: float = 0.02
    exp_threshold: float = 0.80            # Min expensive_ask to confirm direction
    exp_shares: int = 25                   # Shares on expensive side (main bet)
    # Cycling
    max_entries: int = 3
    cooldown_secs: int = 10


@dataclass
class SeqTradeResult:
    market_slug: str
    dataset: str
    config_name: str
    # Cheap side
    cheap_filled: bool
    cheap_fill_price: float
    cheap_shares: int
    cheap_time_remaining: float
    cheap_side: str  # "UP" or "DOWN"
    cheap_won: bool
    cheap_pnl: float
    # Expensive side
    exp_filled: bool
    exp_fill_price: float
    exp_shares: int
    exp_time_remaining: float
    exp_side: str
    exp_won: bool
    exp_pnl: float
    # Combined
    total_pnl: float
    total_cost: float
    pair_cost: Optional[float]  # cheap_fill + exp_fill if both filled


# =============================================================================
# SPIKE DETECTION (identical to phoenix_main_backtest.py)
# =============================================================================
@dataclass
class OUParams:
    mu: float = -3.9845
    sigma_stat: float = 0.3877


def compute_ou_threshold(volatility, ou_params):
    log_vol = math.log(max(volatility, 1e-6))
    z_score = (log_vol - ou_params.mu) / ou_params.sigma_stat
    z_clamped = max(-10, min(10, z_score * OU_SIGMOID_STEEPNESS))
    sigmoid = 1.0 / (1.0 + math.exp(-z_clamped))
    multiplier = OU_K_LOW + (OU_K_HIGH - OU_K_LOW) * sigmoid
    threshold = OU_BASE_THRESHOLD * multiplier
    return max(OU_MIN_THRESHOLD, min(OU_MAX_THRESHOLD, threshold)), z_score


def precompute_spikes_ewma(btc_df, halflife_ms=EWMA_HALFLIFE_MS):
    halflife_ticks = halflife_ms / 16.67
    alpha = 1 - 0.5 ** (1.0 / halflife_ticks)
    gap_threshold_ms = 30 * 60 * 1000

    df = btc_df.copy().sort_values('timestamp_ms').reset_index(drop=True)
    df = df.drop_duplicates(subset=['timestamp_ms'], keep='first').reset_index(drop=True)

    prices = df['price'].values
    timestamps = df['timestamp_ms'].values
    ewma_prices = np.zeros(len(prices))
    ewma_prices[0] = prices[0]

    for i in range(1, len(prices)):
        time_diff = timestamps[i] - timestamps[i-1]
        if time_diff > gap_threshold_ms:
            ewma_prices[i] = prices[i]
        else:
            ewma_prices[i] = alpha * prices[i] + (1 - alpha) * ewma_prices[i-1]

    df['ewma_price'] = ewma_prices
    df['deviation_pct'] = (df['price'] - df['ewma_price']) / df['ewma_price'] * 100
    df['spike_magnitude'] = df['deviation_pct'].abs()

    ou_params = OUParams()
    returns = df['price'].pct_change() * 100
    var_alpha = 1 - 0.5 ** (1.0 / 300)
    variance = returns.iloc[:60].var() if len(returns) > 60 else 0.01
    thresholds = []

    for i, r in enumerate(returns):
        if pd.isna(r):
            thresholds.append(OU_BASE_THRESHOLD)
            continue
        variance = var_alpha * (r ** 2) + (1 - var_alpha) * variance
        vol = max(np.sqrt(variance), 1e-6)
        threshold, _ = compute_ou_threshold(vol, ou_params)
        thresholds.append(threshold)

    df['threshold'] = thresholds
    df['spike_detected'] = df['spike_magnitude'] >= df['threshold']
    return df


# =============================================================================
# MARKET PRECOMPUTATION
# =============================================================================
def precompute_markets(obs_df, spike_ts_all, spike_mag_all, resolutions):
    market_data = {}

    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions:
            continue

        mdf = group.sort_values('timestamp_ms')
        n = len(mdf)
        if n < 10:
            continue

        ts = mdf['timestamp_ms'].values.copy()
        up_ask = mdf['up_ask'].values.astype(float)
        down_ask = mdf['down_ask'].values.astype(float)
        time_rem = mdf['time_remaining_secs'].values.astype(float)
        hours = pd.to_datetime(ts, unit='ms', utc=True).hour.values

        start_idx = np.searchsorted(spike_ts_all, ts[0])
        end_idx = np.searchsorted(spike_ts_all, ts[-1], side='right')
        m_spike_ts = spike_ts_all[start_idx:end_idx]
        m_spike_mag = spike_mag_all[start_idx:end_idx]
        m_spike_obs_idx = np.searchsorted(ts, m_spike_ts, side='right') - 1
        m_spike_obs_idx = np.clip(m_spike_obs_idx, 0, n - 1)

        market_data[slug] = {
            'resolution': resolutions[slug],
            'n': n, 'ts': ts,
            'up_ask': up_ask, 'down_ask': down_ask,
            'time_rem': time_rem, 'hours': hours,
            'spike_ts': m_spike_ts, 'spike_mag': m_spike_mag,
            'spike_obs_idx': m_spike_obs_idx,
        }

    return market_data


# =============================================================================
# SEQUENTIAL CONDITIONAL SIMULATION
# =============================================================================
def simulate_market_sequential(slug, md, config):
    """
    Two-phase entry:
    Phase 1 (cheap_entry_start to cheap_entry_end):
      - Identify cheap side (lower ask)
      - Place maker bid at cheap_ask - cheap_offset
      - Price-touch fill: if cheap_ask drops to our bid → filled

    Phase 2 (exp_entry_start to exp_entry_end):
      - IF market confirms direction (expensive_ask >= threshold) → PHOENIX-style entry
      - Uses spike-based entry like PHOENIX V1
      - Entry on expensive side via maker bid at expensive_ask - exp_offset

    Resolution:
      - All positions held to resolution
    """
    resolution = md['resolution']
    ts = md['ts']
    up_ask = md['up_ask']
    down_ask = md['down_ask']
    time_rem = md['time_rem']
    spike_ts = md['spike_ts']
    spike_obs_idx = md['spike_obs_idx']
    spike_mag = md['spike_mag']

    # =================================================================
    # PHASE 1: Cheap-side maker entry
    # =================================================================
    cheap_filled = False
    cheap_fill_price = 0.0
    cheap_fill_time_rem = 0.0
    cheap_side = ""

    # Find the observation at cheap_entry_start
    cheap_window = (time_rem <= config.cheap_entry_start_secs) & (time_rem >= config.cheap_entry_end_secs)
    cheap_indices = np.where(cheap_window)[0]

    if len(cheap_indices) > 0:
        first_cheap_idx = cheap_indices[0]
        ua_at_entry = up_ask[first_cheap_idx]
        da_at_entry = down_ask[first_cheap_idx]

        if not (np.isnan(ua_at_entry) or np.isnan(da_at_entry)):
            # Identify cheap side
            if ua_at_entry <= da_at_entry:
                cheap_side = "UP"
                cheap_asks = up_ask
                cheap_ask_at_entry = ua_at_entry
            else:
                cheap_side = "DOWN"
                cheap_asks = down_ask
                cheap_ask_at_entry = da_at_entry

            # Place maker bid
            cheap_bid = max(0.01, cheap_ask_at_entry - config.cheap_offset)

            # Check for price-touch fill in the cheap window
            for ci in cheap_indices[1:]:
                if not np.isnan(cheap_asks[ci]) and cheap_asks[ci] <= cheap_bid:
                    cheap_filled = True
                    cheap_fill_price = cheap_bid
                    cheap_fill_time_rem = time_rem[ci]
                    break

    # =================================================================
    # PHASE 2: Expensive-side PHOENIX-style entry (spike-based)
    # =================================================================
    exp_filled = False
    exp_fill_price = 0.0
    exp_fill_time_rem = 0.0
    exp_side = ""

    cooldown_ms = config.cooldown_secs * 1000
    last_spike_ts = 0

    for si in range(len(spike_ts)):
        oi = spike_obs_idx[si]
        tr = time_rem[oi]

        if tr > config.exp_entry_start_secs or tr < config.exp_entry_end_secs:
            continue
        if spike_ts[si] - last_spike_ts < cooldown_ms:
            continue

        ua, da = up_ask[oi], down_ask[oi]
        if np.isnan(ua) or np.isnan(da) or ua <= 0 or da <= 0:
            continue

        if ua >= da:
            exp_ask = ua
            exp_side_candidate = "UP"
            entry_asks = up_ask
        else:
            exp_ask = da
            exp_side_candidate = "DOWN"
            entry_asks = down_ask

        if exp_ask < config.exp_threshold:
            continue

        last_spike_ts = spike_ts[si]
        entry_bid = max(0.01, exp_ask - config.exp_offset)

        # Maker fill check
        if oi + 1 >= len(entry_asks):
            continue
        entry_slice = entry_asks[oi + 1:]
        fill_indices = np.where(entry_slice <= entry_bid)[0]
        if len(fill_indices) > 0:
            exp_filled = True
            exp_fill_price = entry_bid
            exp_side = exp_side_candidate
            fill_global = oi + 1 + fill_indices[0]
            exp_fill_time_rem = time_rem[fill_global]
            break  # Take first fill

    # =================================================================
    # PnL CALCULATION
    # =================================================================
    cheap_won = (cheap_side == resolution) if cheap_side else False
    exp_won = (exp_side == resolution) if exp_side else False

    # Cheap PnL
    cheap_pnl = 0.0
    cheap_shares_used = 0
    if cheap_filled:
        cheap_shares_used = config.cheap_shares
        if cheap_won:
            cheap_pnl = (1.0 - cheap_fill_price) * cheap_shares_used
        else:
            cheap_pnl = -cheap_fill_price * cheap_shares_used

    # Expensive PnL
    exp_pnl = 0.0
    exp_shares_used = 0
    if exp_filled:
        exp_shares_used = config.exp_shares
        if exp_won:
            exp_pnl = (1.0 - exp_fill_price) * exp_shares_used
        else:
            exp_pnl = -exp_fill_price * exp_shares_used

    total_pnl = cheap_pnl + exp_pnl
    total_cost = (cheap_fill_price * cheap_shares_used if cheap_filled else 0) + \
                 (exp_fill_price * exp_shares_used if exp_filled else 0)

    pair_cost = None
    if cheap_filled and exp_filled:
        pair_cost = cheap_fill_price + exp_fill_price

    # Only return result if at least one side filled
    if not cheap_filled and not exp_filled:
        return None

    return SeqTradeResult(
        market_slug=slug,
        dataset="",  # filled by caller
        config_name=config.name,
        cheap_filled=cheap_filled,
        cheap_fill_price=cheap_fill_price,
        cheap_shares=cheap_shares_used,
        cheap_time_remaining=cheap_fill_time_rem,
        cheap_side=cheap_side,
        cheap_won=cheap_won,
        cheap_pnl=cheap_pnl,
        exp_filled=exp_filled,
        exp_fill_price=exp_fill_price,
        exp_shares=exp_shares_used,
        exp_time_remaining=exp_fill_time_rem,
        exp_side=exp_side,
        exp_won=exp_won,
        exp_pnl=exp_pnl,
        total_pnl=total_pnl,
        total_cost=total_cost,
        pair_cost=pair_cost,
    )


# =============================================================================
# DATASETS (identical to phoenix_main_backtest.py)
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
        "res_files": ["research/observer/market_resolutions.csv"],
    },
    "OOS3+4": {
        "name": "OOS3+4 (Jan 22-24)",
        "btc_file": "research/observer/PROTECTED_btc_prices_oos3_oos4_combined.csv",
        "obs_files": ["research/observer/PROTECTED_grid_obs_oos3_oos4_combined.csv"],
        "res_files": ["research/observer/market_resolutions_verified.csv"],
    },
    "OOS7": {
        "name": "OOS7 (Jan 29-30)",
        "btc_file": "research/binance_hf/btc_prices_20260129_160523.csv",
        "obs_files": [
            "research/observer/grid_obs_20260129.csv",
            "research/observer/grid_obs_20260130.csv",
        ],
        "res_files": [
            "research/observer/resolutions_20260129.csv",
            "research/observer/resolutions_20260130.csv",
        ],
    },
    "OOS8": {
        "name": "OOS8 (Jan 31)",
        "btc_file": "research/binance_hf/btc_prices_20260131_055231.csv",
        "obs_files": ["research/observer/grid_obs_20260131.csv"],
        "res_files": ["research/observer/resolutions_20260131.csv"],
    },
    "OOS9": {
        "name": "OOS9 (Feb 1-3)",
        "btc_file": "research/binance_hf/btc_prices_oos9.csv",
        "obs_files": ["research/observer/grid_obs_oos9.csv"],
        "res_files": [
            "research/observer/resolutions_oos9_1.csv",
            "research/observer/resolutions_oos9_2.csv",
        ],
    },
}


def load_dataset(dataset_key):
    config = DATASETS[dataset_key]
    base_dir = Path("/Users/rananjaybika/polymarket-amm-bot")

    obs_dfs = []
    for fname in config['obs_files']:
        fpath = base_dir / fname
        if fpath.exists():
            df = pd.read_csv(fpath, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)

    if not obs_dfs:
        return None, None, {}, 0

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])

    btc_path = base_dir / config['btc_file']
    if btc_path.exists():
        btc_df = pd.read_csv(btc_path)
    else:
        return None, None, {}, 0

    resolutions = {}
    for res_fname in config.get('res_files', []):
        res_path = base_dir / res_fname
        if res_path.exists():
            res_df = pd.read_csv(res_path)
            if 'slug' in res_df.columns and 'winner' in res_df.columns:
                for _, row in res_df.iterrows():
                    resolutions[row['slug']] = row['winner']
            elif 'market_slug' in res_df.columns and 'resolution' in res_df.columns:
                for _, row in res_df.iterrows():
                    resolutions[row['market_slug']] = row['resolution']

    duration_hours = (obs_df['timestamp_ms'].max() - obs_df['timestamp_ms'].min()) / (1000 * 3600)
    return obs_df, btc_df, resolutions, duration_hours


# =============================================================================
# GRID CONFIGS
# =============================================================================
def generate_configs():
    configs = []

    # Baseline: PHOENIX V1 only (no cheap probe)
    configs.append(SeqConfig(
        name="BASELINE_PHOENIX_ONLY",
        cheap_entry_start_secs=0, cheap_entry_end_secs=0,
        cheap_shares=0,
    ))

    # Cheap-only (no expensive entry) — measures naked probe EV
    for cheap_time, cheap_off in product([700, 600, 500], [0.02, 0.04]):
        configs.append(SeqConfig(
            name=f"CHEAP_ONLY_T{cheap_time}_O{int(cheap_off*100)}",
            cheap_entry_start_secs=cheap_time,
            cheap_entry_end_secs=cheap_time - 200,
            cheap_offset=cheap_off,
            cheap_shares=10,
            exp_threshold=99.0,  # Never triggers expensive entry
            exp_shares=0,
        ))

    # Sequential: cheap first + conditional expensive
    cheap_times = [800, 700, 600, 500]
    cheap_offsets = [0.02, 0.04]
    cheap_share_levels = [5, 10, 15]
    exp_thresholds = [0.80, 0.85]

    for ct, co, cs, et in product(cheap_times, cheap_offsets, cheap_share_levels, exp_thresholds):
        name = f"SEQ_CT{ct}_CO{int(co*100)}_CS{cs}_ET{int(et*100)}"
        configs.append(SeqConfig(
            name=name,
            cheap_entry_start_secs=ct,
            cheap_entry_end_secs=max(ct - 200, 300),
            cheap_offset=co,
            cheap_shares=cs,
            exp_threshold=et,
            exp_shares=25,
        ))

    return configs


# =============================================================================
# METRICS
# =============================================================================
def calc_metrics(results, duration_hours, config_name, dataset_name):
    if not results:
        return None

    n = len(results)
    pnls = [r.total_pnl for r in results]
    total_pnl = sum(pnls)

    # Cheap side stats
    cheap_fills = sum(1 for r in results if r.cheap_filled)
    cheap_wins = sum(1 for r in results if r.cheap_filled and r.cheap_won)
    cheap_total_pnl = sum(r.cheap_pnl for r in results)

    # Expensive side stats
    exp_fills = sum(1 for r in results if r.exp_filled)
    exp_wins = sum(1 for r in results if r.exp_filled and r.exp_won)
    exp_total_pnl = sum(r.exp_pnl for r in results)

    # Both sides
    both_filled = sum(1 for r in results if r.cheap_filled and r.exp_filled)
    cheap_only = sum(1 for r in results if r.cheap_filled and not r.exp_filled)
    exp_only = sum(1 for r in results if not r.cheap_filled and r.exp_filled)

    # Pair cost when both filled
    pair_costs = [r.pair_cost for r in results if r.pair_cost is not None]
    avg_pair_cost = np.mean(pair_costs) if pair_costs else 0
    sub_dollar_pairs = sum(1 for pc in pair_costs if pc < 1.0)

    # Risk metrics
    worst_loss = min(pnls) if pnls else 0
    wrong_exp_pnls = [r.exp_pnl for r in results if r.exp_filled and not r.exp_won]
    avg_wrong_exp = np.mean(wrong_exp_pnls) if wrong_exp_pnls else 0

    # When wrong AND have cheap insurance
    wrong_with_cheap = [r for r in results if r.exp_filled and not r.exp_won and r.cheap_filled]
    avg_loss_with_insurance = np.mean([r.total_pnl for r in wrong_with_cheap]) if wrong_with_cheap else 0
    wrong_without_cheap = [r for r in results if r.exp_filled and not r.exp_won and not r.cheap_filled]
    avg_loss_without_insurance = np.mean([r.total_pnl for r in wrong_without_cheap]) if wrong_without_cheap else 0

    return {
        'config': config_name,
        'dataset': dataset_name,
        'markets': n,
        'total_pnl': round(total_pnl, 2),
        'pnl_per_hr': round(total_pnl / duration_hours, 2) if duration_hours > 0 else 0,
        'cheap_fills': cheap_fills,
        'cheap_wr': round(cheap_wins / max(cheap_fills, 1) * 100, 1),
        'cheap_pnl': round(cheap_total_pnl, 2),
        'exp_fills': exp_fills,
        'exp_wr': round(exp_wins / max(exp_fills, 1) * 100, 1),
        'exp_pnl': round(exp_total_pnl, 2),
        'both_filled': both_filled,
        'cheap_only': cheap_only,
        'exp_only': exp_only,
        'avg_pair_cost': round(avg_pair_cost, 4),
        'sub_dollar_pairs': sub_dollar_pairs,
        'worst_loss': round(worst_loss, 2),
        'avg_wrong_exp_pnl': round(avg_wrong_exp, 2),
        'avg_loss_with_insurance': round(avg_loss_with_insurance, 2),
        'avg_loss_without_insurance': round(avg_loss_without_insurance, 2),
        'insurance_savings': round(avg_loss_without_insurance - avg_loss_with_insurance, 2),
    }


# =============================================================================
# MAIN
# =============================================================================
def main():
    print("=" * 80)
    print("SEQUENTIAL CONDITIONAL BACKTEST — Cheap-First → Conditional Expensive")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    configs = generate_configs()
    print(f"Configs: {len(configs)}")
    dataset_keys = ["IS+OOS2", "OOS3+4", "OOS7", "OOS8", "OOS9"]

    output_dir = Path("/Users/rananjaybika/polymarket-amm-bot/research/findings/data")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "sequential_conditional_results.csv"
    checkpoint_file = output_dir / "sequential_conditional_checkpoint.csv"

    all_results = []

    for dataset_key in dataset_keys:
        obs_df, btc_df, resolutions, duration_hours = load_dataset(dataset_key)
        if obs_df is None:
            continue

        print(f"\n{'='*60}")
        print(f"Dataset: {dataset_key} ({duration_hours:.1f}h)")

        print(f"  Precomputing spikes...")
        btc_spikes_df = precompute_spikes_ewma(btc_df)
        spike_mask = btc_spikes_df['spike_detected'].values
        spike_ts_all = btc_spikes_df.loc[spike_mask, 'timestamp_ms'].values
        spike_mag_all = btc_spikes_df.loc[spike_mask, 'spike_magnitude'].values
        sort_idx = np.argsort(spike_ts_all)
        spike_ts_all = spike_ts_all[sort_idx]
        spike_mag_all = spike_mag_all[sort_idx]

        print(f"  Precomputing markets...")
        market_data = precompute_markets(obs_df, spike_ts_all, spike_mag_all, resolutions)
        print(f"  {len(market_data)} markets, {len(spike_ts_all):,} spikes")

        for config in tqdm(configs, desc=f"  {dataset_key}"):
            market_results = []
            for slug, md in market_data.items():
                result = simulate_market_sequential(slug, md, config)
                if result:
                    result.dataset = dataset_key
                    market_results.append(result)

            metrics = calc_metrics(market_results, duration_hours, config.name, dataset_key)
            if metrics:
                metrics['cheap_entry_start'] = config.cheap_entry_start_secs
                metrics['cheap_offset'] = config.cheap_offset
                metrics['cheap_share_size'] = config.cheap_shares
                metrics['exp_threshold'] = config.exp_threshold
                metrics['exp_shares'] = config.exp_shares
                all_results.append(metrics)

        pd.DataFrame(all_results).to_csv(checkpoint_file, index=False)
        print(f"  Checkpoint: {len(all_results)} results")

    results_df = pd.DataFrame(all_results)
    results_df.to_csv(output_file, index=False)

    # =========================================================================
    # SUMMARY
    # =========================================================================
    print("\n" + "=" * 80)
    print("RESULTS SUMMARY")
    print("=" * 80)

    agg = results_df.groupby('config').agg({
        'total_pnl': 'sum',
        'pnl_per_hr': 'mean',
        'markets': 'sum',
        'cheap_fills': 'sum',
        'exp_fills': 'sum',
        'both_filled': 'sum',
        'worst_loss': 'min',
        'avg_loss_with_insurance': 'mean',
        'avg_loss_without_insurance': 'mean',
        'insurance_savings': 'mean',
        'avg_pair_cost': 'mean',
    }).sort_values('pnl_per_hr', ascending=False)

    print(f"\n{'Config':<40} {'$/hr':>7} {'PnL':>8} {'Mkts':>5} {'Cheap':>5} "
          f"{'Exp':>5} {'Both':>5} {'Worst':>7} {'InsurSave':>9} {'PairC':>7}")
    print("-" * 120)
    for name, row in agg.head(25).iterrows():
        print(f"{name:<40} ${row['pnl_per_hr']:>6.2f} ${row['total_pnl']:>7.2f} "
              f"{int(row['markets']):>5} {int(row['cheap_fills']):>5} "
              f"{int(row['exp_fills']):>5} {int(row['both_filled']):>5} "
              f"${row['worst_loss']:>6.2f} ${row['insurance_savings']:>8.2f} "
              f"${row['avg_pair_cost']:>.4f}")

    # Highlight insurance value
    print(f"\n{'='*80}")
    print("INSURANCE VALUE ANALYSIS")
    print("=" * 80)

    seq_configs = agg[agg.index.str.startswith('SEQ_')]
    if len(seq_configs) > 0:
        baseline_row = agg.loc['BASELINE_PHOENIX_ONLY'] if 'BASELINE_PHOENIX_ONLY' in agg.index else None

        print(f"\nBaseline (PHOENIX only): ${baseline_row['pnl_per_hr']:.2f}/hr, worst=${baseline_row['worst_loss']:.2f}" if baseline_row is not None else "No baseline")

        # Best by insurance savings
        best_insurance = seq_configs.sort_values('insurance_savings', ascending=False).head(5)
        print(f"\nTop 5 by insurance savings (avg loss reduction when wrong):")
        for name, row in best_insurance.iterrows():
            print(f"  {name}: saves ${row['insurance_savings']:.2f}/wrong trade, "
                  f"${row['pnl_per_hr']:.2f}/hr, worst=${row['worst_loss']:.2f}")

        # Best by pnl_per_hr
        best_pnl = seq_configs.sort_values('pnl_per_hr', ascending=False).head(5)
        print(f"\nTop 5 by $/hr:")
        for name, row in best_pnl.iterrows():
            print(f"  {name}: ${row['pnl_per_hr']:.2f}/hr, "
                  f"insur_saves=${row['insurance_savings']:.2f}, worst=${row['worst_loss']:.2f}")

    print(f"\nResults saved to: {output_file}")
    print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
