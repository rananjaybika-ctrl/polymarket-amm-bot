#!/usr/bin/env python3
"""
Z-Score Filter Impact Test

=============================================================================
Purpose: Test whether the z-score filter [0.0, 1.5] used in live trading
         improves or hurts performance.

Background:
- Grid search v2 (canonical) does NOT use z-score filtering
- Live code (enhanced_spike.py) DOES use z-score filter [0.0, 1.5]
- This test compares performance WITH and WITHOUT the filter
=============================================================================

Test Setup:
- Config: CURRENT_TS180_NOSL_NOMML (winner from grid search)
- Run 1: WITHOUT z-score filter (baseline, matches grid search v2)
- Run 2: WITH z-score filter [0.0, 1.5] (matches live code)
- Datasets: IS+OOS2, OOS3+4, OOS5, OOS7, OOS8

Decision Rule:
- If WITH_FILTER $/hr > NO_FILTER $/hr overall → keep z-score filter in live
- If NO_FILTER $/hr > WITH_FILTER $/hr overall → remove z-score filter from live

Usage:
    python research/optimizers/zscore_filter_test.py
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
# SHARED LOGIC - FROM src/core (Single Source of Truth)
# =============================================================================
from src.core import (
    polymarket_taker_fee,
    calculate_pnl_with_fees,
    velocity_confirms_spike,
    obi_confirms_spike,
    should_take_spike_enhanced,
    compute_enhanced_score,
    VELOCITY_CONFIRM_THRESHOLD,
    ENHANCED_SCORE_THRESHOLD,
)

# =============================================================================
# TEST CONFIGURATION - CURRENT_TS180_NOSL_NOMML (winner from grid search)
# =============================================================================
TARGET_SHARES = 50
TIME_STOP_SECONDS = 180.0
MIN_TIME = 240.0              # Entry cutoff (time_stop + 60s buffer)
MIN_RUNTIME_SECS = 300        # 5 min market duration filter
HIGH_ENTRY_THRESHOLD = 0.90   # Skip entries >= $0.90
SPIKE_LOOKBACK = 72           # 72 ticks (1200ms at 60Hz)

# Loser bid calculation (CURRENT offset)
DROP_MULTIPLIER = 0.50
DROP_INTERCEPT = 0.08
TARGET_PAIR_COST = 0.99

# OU ADAPTIVE THRESHOLD params
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10

# Cycling
MIN_CYCLE_GAP_MS = 200

# Z-Score filter bounds (to test)
ZSCORE_LO = 0.0
ZSCORE_HI = 1.5


# =============================================================================
# OU PARAMETERS
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
    hedge_type: str
    pair_cost: float
    pnl_gross: float
    pnl_net: float
    entry_fee: float
    exit_fee: float
    correct_direction: bool
    spike_magnitude: float
    dataset: str
    zscore_at_entry: float = 0.0
    filter_mode: str = "NO_FILTER"


# =============================================================================
# DATASET CONFIGURATION
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
        "use_obi": True,
    },
    "OOS3+4": {
        "name": "OOS3+4 (Jan 22-24)",
        "btc_file": "research/observer/PROTECTED_btc_prices_oos3_oos4_combined.csv",
        "obs_files": [
            "research/observer/PROTECTED_grid_obs_oos3_oos4_combined.csv",
        ],
        "use_obi": True,
    },
    "OOS5": {
        "name": "OOS5 (Jan 26)",
        "btc_file": None,
        "obs_files": [
            "research/observer/PROTECTED_grid_obs_oos5_recovered.csv",
        ],
        "use_obi": True,
    },
    "OOS7": {
        "name": "OOS7 (Jan 29-30)",
        "btc_file": "research/binance_hf/btc_prices_20260129_160523.csv",
        "obs_files": [
            "research/observer/grid_obs_20260129.csv",
            "research/observer/grid_obs_20260130.csv",
        ],
        "use_obi": True,
    },
    "OOS8": {
        "name": "OOS8 (Jan 31)",
        "btc_file": "research/binance_hf/btc_prices_20260131_055231.csv",
        "obs_files": [
            "research/observer/grid_obs_20260131.csv",
        ],
        "use_obi": True,
    },
}


# =============================================================================
# SPIKE DETECTION WITH Z-SCORE TRACKING
# =============================================================================

def precompute_spikes_ou(btc_df: pd.DataFrame, lookback: int = SPIKE_LOOKBACK) -> pd.DataFrame:
    """Vectorized spike detection with OU ADAPTIVE threshold and z-score tracking."""
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
    print(f"    Found {spike_count:,} spikes (OU adaptive)")

    return df


def calculate_loser_bid(winner_entry: float, spike_magnitude: float) -> float:
    """Calculate loser bid - matches grid search."""
    expected_drop = DROP_MULTIPLIER * spike_magnitude + DROP_INTERCEPT
    max_loser = TARGET_PAIR_COST - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid))


# =============================================================================
# SIMULATION - With optional z-score filter
# =============================================================================

def simulate_market(btc_spikes: pd.DataFrame, obs_df: pd.DataFrame,
                    slug: str, resolution: str,
                    use_obi_filter: bool, dataset_name: str,
                    use_zscore_filter: bool = False) -> List[TradeResult]:
    """
    Simulate trading with optional z-score filter.

    Args:
        use_zscore_filter: If True, skip entries when z_score < ZSCORE_LO or z_score > ZSCORE_HI
    """
    filter_mode = "WITH_FILTER" if use_zscore_filter else "NO_FILTER"

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

                loser_side = position_data['loser_side']
                loser_target = position_data['loser_target']
                winner_entry = position_data['winner_entry']
                spike_mag = position_data['spike_magnitude']
                score = position_data['score']
                zscore_at_entry = position_data['zscore_at_entry']

                if loser_side == "UP":
                    loser_ask = obs_row['up_ask']
                else:
                    loser_ask = obs_row['down_ask']

                # Check passive fill
                if pd.notna(loser_ask) and loser_ask <= loser_target:
                    pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                        winner_entry, loser_target, TARGET_SHARES,
                        is_taker_entry=True, is_taker_exit=False
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
                        zscore_at_entry=zscore_at_entry,
                        filter_mode=filter_mode,
                    ))

                    in_position = False
                    position_data = None
                    last_hedge_ts = obs_ts
                    obs_idx += 1
                    break

                # Check time-stop (ONLY if NOT in profit)
                elapsed_ms = obs_ts - entry_ts
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
                            winner_entry, loser_fill, TARGET_SHARES,
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
                            hedge_type="time_stop",
                            pair_cost=winner_entry + loser_fill,
                            pnl_gross=pnl_gross,
                            pnl_net=pnl_net,
                            entry_fee=entry_fee,
                            exit_fee=exit_fee,
                            correct_direction=(resolution == position_data['winner_side']),
                            spike_magnitude=spike_mag,
                            dataset=dataset_name,
                            zscore_at_entry=zscore_at_entry,
                            filter_mode=filter_mode,
                        ))

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

                entry_fee = polymarket_taker_fee(winner_entry) * winner_entry * TARGET_SHARES

                if resolution == winner_side:
                    pnl_gross = (1.0 - winner_entry) * TARGET_SHARES
                    loser_fill = 0.0
                else:
                    pnl_gross = (0.0 - winner_entry) * TARGET_SHARES
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
                    zscore_at_entry=position_data['zscore_at_entry'],
                    filter_mode=filter_mode,
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
        z_score = spike_row['z_score']

        # Z-SCORE FILTER (only when enabled)
        if use_zscore_filter:
            if z_score < ZSCORE_LO or z_score > ZSCORE_HI:
                spike_idx += 1
                continue

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

        # Get prices
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

        # OBI filter
        if use_obi_filter:
            if obi_winner is not None and not np.isnan(obi_winner):
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
                    spike_idx += 1
                    continue

        # ENTRY
        cycle_num += 1
        loser_side = "DOWN" if winner_side == "UP" else "UP"
        winner_entry = winner_ask
        loser_target = calculate_loser_bid(winner_entry, spike_mag)

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
            'zscore_at_entry': z_score,
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
# MAIN TEST
# =============================================================================

def run_zscore_test():
    """Run z-score filter comparison test."""
    print("=" * 80)
    print("Z-SCORE FILTER IMPACT TEST")
    print("=" * 80)
    print(f"\nConfig: CURRENT_TS180_NOSL_NOMML (TIME_STOP={TIME_STOP_SECONDS}s)")
    print(f"Z-Score Filter Bounds: [{ZSCORE_LO}, {ZSCORE_HI}]")
    print(f"\nTest runs:")
    print(f"  1. NO_FILTER - Baseline (matches grid search v2)")
    print(f"  2. WITH_FILTER - Z-score filter [{ZSCORE_LO}, {ZSCORE_HI}]")
    print()

    load_ou_params()

    results = []
    dataset_cache = {}

    for dataset_key in DATASETS.keys():
        print(f"\n{'='*60}")
        print(f"Testing {dataset_key}")
        print(f"{'='*60}")

        # Load dataset
        if dataset_key not in dataset_cache:
            btc_df, obs_df, res_map, hours = load_dataset(dataset_key)
            if btc_df is not None:
                btc_spikes = precompute_spikes_ou(btc_df)
                dataset_cache[dataset_key] = (btc_spikes, obs_df, res_map, hours)
            else:
                continue
        else:
            btc_spikes, obs_df, res_map, hours = dataset_cache[dataset_key]

        use_obi = DATASETS[dataset_key]['use_obi']
        slugs = obs_df['market_slug'].unique()

        for use_zscore_filter in [False, True]:
            filter_mode = "WITH_FILTER" if use_zscore_filter else "NO_FILTER"
            print(f"\n  Running {filter_mode}...")

            all_trades = []
            for slug in tqdm(slugs, desc=f"  {filter_mode}", leave=False):
                resolution = res_map.get(slug)
                if resolution not in ['UP', 'DOWN']:
                    continue

                trades = simulate_market(
                    btc_spikes, obs_df, slug, resolution, use_obi, dataset_key,
                    use_zscore_filter=use_zscore_filter
                )
                all_trades.extend(trades)

            if all_trades:
                total_pnl_net = sum(t.pnl_net for t in all_trades)
                total_trades = len(all_trades)
                win_rate = sum(1 for t in all_trades if t.correct_direction) / total_trades * 100
                hourly_rate = total_pnl_net / hours if hours > 0 else 0

                result = {
                    'dataset': dataset_key,
                    'filter_mode': filter_mode,
                    'hours': hours,
                    'trades': total_trades,
                    'pnl_net': total_pnl_net,
                    'hourly_rate': hourly_rate,
                    'win_rate': win_rate,
                }
                results.append(result)

                print(f"    Trades: {total_trades}, $/hr: ${hourly_rate:.2f}, Win%: {win_rate:.1f}%")

    return results


def print_results(results: List[dict]):
    """Print comparison results."""
    df = pd.DataFrame(results)

    print("\n" + "=" * 80)
    print("Z-SCORE FILTER COMPARISON RESULTS")
    print("=" * 80)

    # Per-dataset comparison
    print("\n" + "-" * 80)
    print(f"{'Dataset':<12} {'Filter':<14} {'Trades':>8} {'PnL':>10} {'$/hr':>10} {'Win%':>8}")
    print("-" * 80)

    for dataset in DATASETS.keys():
        ds_df = df[df['dataset'] == dataset]
        if len(ds_df) == 0:
            continue

        for _, row in ds_df.sort_values('filter_mode').iterrows():
            print(f"{row['dataset']:<12} {row['filter_mode']:<14} {row['trades']:>8} "
                  f"${row['pnl_net']:>9.2f} ${row['hourly_rate']:>9.2f} {row['win_rate']:>7.1f}%")
        print()

    # Combined totals
    print("\n" + "=" * 80)
    print("COMBINED RESULTS (All Datasets)")
    print("=" * 80)

    combined = df.groupby('filter_mode').agg({
        'trades': 'sum',
        'pnl_net': 'sum',
        'hours': 'sum',
        'win_rate': 'mean',
    }).reset_index()

    combined['hourly_rate'] = combined['pnl_net'] / combined['hours']

    print()
    print(f"{'Filter Mode':<14} {'Total Trades':>14} {'Total PnL':>12} {'$/hr':>10} {'Win%':>8}")
    print("-" * 60)

    for _, row in combined.sort_values('hourly_rate', ascending=False).iterrows():
        print(f"{row['filter_mode']:<14} {row['trades']:>14} ${row['pnl_net']:>11.2f} "
              f"${row['hourly_rate']:>9.2f} {row['win_rate']:>7.1f}%")

    # Decision
    no_filter = combined[combined['filter_mode'] == 'NO_FILTER'].iloc[0]
    with_filter = combined[combined['filter_mode'] == 'WITH_FILTER'].iloc[0]

    print("\n" + "=" * 80)
    print("DECISION")
    print("=" * 80)

    diff = with_filter['hourly_rate'] - no_filter['hourly_rate']
    diff_pct = (diff / abs(no_filter['hourly_rate'])) * 100 if no_filter['hourly_rate'] != 0 else 0
    trade_reduction = (1 - with_filter['trades'] / no_filter['trades']) * 100

    print(f"\nNO_FILTER:   ${no_filter['hourly_rate']:.2f}/hr ({int(no_filter['trades'])} trades)")
    print(f"WITH_FILTER: ${with_filter['hourly_rate']:.2f}/hr ({int(with_filter['trades'])} trades)")
    print(f"\nDifference:  ${diff:+.2f}/hr ({diff_pct:+.1f}%)")
    print(f"Trade reduction: {trade_reduction:.1f}%")
    print()

    if with_filter['hourly_rate'] > no_filter['hourly_rate']:
        print("RECOMMENDATION: KEEP z-score filter in live code")
        print(f"  Filter improves $/hr by ${diff:.2f} ({diff_pct:+.1f}%)")
    else:
        print("RECOMMENDATION: REMOVE z-score filter from live code")
        print(f"  Filter reduces $/hr by ${abs(diff):.2f} ({diff_pct:.1f}%)")

    # Save results
    output_path = Path("research/findings/data/zscore_filter_test_results.csv")
    df.to_csv(output_path, index=False)
    print(f"\nResults saved to: {output_path}")


def main():
    results = run_zscore_test()
    if results:
        print_results(results)


if __name__ == "__main__":
    main()
