#!/usr/bin/env python3
"""
Multi-Dataset AGGRESSIVE Backtest - COPIED FROM aggressive_main_backtest.py

Runs validated backtest on multiple datasets:
- IS+OOS2 (Jan 16-19): OBI OFF
- OOS3+4 (Jan 22-24): OBI OFF
- OOS5 (Jan 26): OBI OFF
- OOS7 (Jan 29-30): OBI ON

Uses 60Hz Binance HF data for spike detection (matching live strategy).
Simulation logic COPIED from aggressive_main_backtest.py.

Usage:
    python research/backtests/multi_dataset_validated_backtest.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
from tqdm import tqdm

# =============================================================================
# CONFIGURATION - SOURCED FROM TRADING_CONFIGS.py (Jan 31, 2026)
# =============================================================================

import math
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Import from TRADING_CONFIGS.py - SINGLE SOURCE OF TRUTH
from research.reference.TRADING_CONFIGS import AGGRESSIVE as AGGRESSIVE_CONFIG

TARGET_SHARES = 50  # PRODUCTION: 50 shares
MIN_TIME = int(AGGRESSIVE_CONFIG.min_time_remaining)  # 180 from config
MIN_RUNTIME_SECS = 300  # 5 minutes minimum market duration
HIGH_ENTRY_THRESHOLD = AGGRESSIVE_CONFIG.high_entry_threshold  # 0.90 from config

# Spike detection at 60Hz - CANONICAL from TRADING_CONFIGS.py
SPIKE_LOOKBACK = AGGRESSIVE_CONFIG.lookback_ticks  # 72 ticks (1200ms)

# OU ADAPTIVE THRESHOLD - NOT fixed 0.02! (per TRADING_CONFIGS.py threshold_method="ou")
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10

# Enhanced signal filtering
VELOCITY_CONFIRM_THRESHOLD = 0.10
ENHANCED_SCORE_THRESHOLD = 0.40

# Time-stop from config
TIME_STOP_SECONDS = AGGRESSIVE_CONFIG.time_stop_seconds  # 120.0 from config

# Loser bid calculation (FIXED - no /100 bug)
DROP_MULTIPLIER = 0.50
DROP_INTERCEPT = 0.08
TARGET_PAIR_COST = 0.99

# Cycling
MIN_CYCLE_GAP_MS = 200

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
        "use_obi": False,
    },
    "OOS3+4": {
        "name": "OOS3+4 (Jan 22-24)",
        "btc_file": "research/observer/PROTECTED_btc_prices_oos3_oos4_combined.csv",
        "obs_files": [
            "research/observer/PROTECTED_grid_obs_oos3_oos4_combined.csv",
        ],
        "use_obi": False,
    },
    "OOS5": {
        "name": "OOS5 (Jan 26)",
        "btc_file": None,  # Will use observer binance_price at 5Hz
        "obs_files": [
            "research/observer/PROTECTED_grid_obs_oos5_recovered.csv",
        ],
        "use_obi": False,
    },
    "OOS7": {
        "name": "OOS7 (Jan 29-30)",
        "btc_file": "research/binance_hf/btc_prices_20260129_160523.csv",
        "obs_files": [
            "research/observer/grid_obs_20260129.csv",
            "research/observer/grid_obs_20260130.csv",
        ],
        "use_obi": True,  # OBI ON for OOS7
    },
}

# =============================================================================
# DATA CLASSES (COPIED from aggressive_main_backtest.py)
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
    pnl: float
    correct_direction: bool
    spike_magnitude: float
    dataset: str


# =============================================================================
# SPIKE DETECTION - VECTORIZED PRECOMPUTATION FOR SPEED
# =============================================================================

def precompute_spikes_ou(btc_df: pd.DataFrame, lookback: int = SPIKE_LOOKBACK) -> pd.DataFrame:
    """
    Vectorized spike detection with OU ADAPTIVE threshold.
    Uses EWMA volatility to compute adaptive threshold per tick.
    """
    print("    Using OU ADAPTIVE threshold (per TRADING_CONFIGS.py)")
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    # Calculate price change over lookback
    df['price_prev'] = df['price'].shift(lookback)
    df['change_pct'] = (df['price'] - df['price_prev']) / df['price_prev'] * 100
    df['spike_magnitude'] = df['change_pct'].abs()

    # Compute EWMA volatility for OU adaptive threshold
    returns = df['price'].pct_change() * 100
    ewma_halflife = 300
    alpha = 1 - 0.5 ** (1.0 / ewma_halflife)

    variance = returns.iloc[:60].var() if len(returns) > 60 else 0.01
    thresholds = []

    for i, r in enumerate(returns):
        if pd.isna(r):
            thresholds.append(OU_BASE_THRESHOLD)
            continue
        variance = alpha * (r ** 2) + (1 - alpha) * variance
        vol = max(np.sqrt(variance), 1e-6)
        threshold = compute_ou_threshold(vol)
        thresholds.append(threshold)

    df['threshold'] = thresholds
    df['spike_detected'] = df['spike_magnitude'] >= df['threshold']
    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['change_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['change_pct'] < 0), 'spike_direction'] = 'DOWN'

    spike_count = df['spike_detected'].sum()
    print(f"    Found {spike_count:,} spikes (OU adaptive)")

    return df


# SpikeDetector class removed - using vectorized precompute_spikes_ou() instead


# =============================================================================
# HELPER FUNCTIONS (COPIED from aggressive_main_backtest.py)
# =============================================================================

def velocity_confirms_spike(spike_dir: str, velocity_bps: float) -> bool:
    """Check if velocity confirms spike direction."""
    if spike_dir == "UP":
        return velocity_bps > -VELOCITY_CONFIRM_THRESHOLD
    elif spike_dir == "DOWN":
        return velocity_bps < VELOCITY_CONFIRM_THRESHOLD
    return True


def obi_confirms_spike(spike_dir: str, up_imbalance: Optional[float],
                       down_imbalance: Optional[float]) -> Tuple[bool, bool]:
    """
    Check if Order Book Imbalance confirms spike direction.
    Returns: (obi_available, obi_confirms)
    """
    if spike_dir == "UP":
        if up_imbalance is not None and not np.isnan(up_imbalance):
            return True, up_imbalance > 0
    elif spike_dir == "DOWN":
        if down_imbalance is not None and not np.isnan(down_imbalance):
            return True, down_imbalance > 0
    return False, True  # Not available = don't filter


def compute_enhanced_score(spike_mag: float, velocity_bps: float,
                           spike_dir: str, time_remaining: float) -> float:
    """Compute composite score (matching live strategy)."""
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


def calculate_loser_bid(winner_entry: float, spike_magnitude: float) -> float:
    """Calculate loser bid. FIXED: No /100 division."""
    expected_drop = DROP_MULTIPLIER * spike_magnitude + DROP_INTERCEPT
    max_loser = TARGET_PAIR_COST - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid))


# =============================================================================
# SIMULATION - OPTIMIZED WITH PRECOMPUTED SPIKES
# =============================================================================

def simulate_market_precomputed(btc_spikes: pd.DataFrame, obs_df: pd.DataFrame,
                                 slug: str, resolution: str,
                                 use_obi_filter: bool, dataset_name: str) -> List[TradeResult]:
    """
    Simulate trading using PRECOMPUTED spikes.
    Only processes spike events + observer rows for hedge checking.
    100-1000x faster than tick-by-tick.
    """
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    # Get only spikes in this market's time range
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

    # Process each spike as potential entry
    spike_idx = 0
    obs_idx = 0

    while spike_idx < len(market_spikes) or in_position:
        # If in position, check for hedge using observer data
        if in_position and position_data is not None:
            entry_ts = position_data['entry_ts']

            # Find observer rows after entry to check hedge
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
                    pair_cost = winner_entry + loser_target
                    pnl = (1.0 - pair_cost) * TARGET_SHARES

                    trades.append(TradeResult(
                        market_slug=slug,
                        cycle_num=cycle_num,
                        entry_time_remaining=position_data['entry_time_rem'],
                        signal_score=score,
                        winner_side=position_data['winner_side'],
                        winner_fill_price=winner_entry,
                        loser_fill_price=loser_target,
                        hedge_type="passive",
                        pair_cost=pair_cost,
                        pnl=pnl,
                        correct_direction=(resolution == position_data['winner_side']),
                        spike_magnitude=spike_mag,
                        dataset=dataset_name,
                    ))

                    in_position = False
                    position_data = None
                    last_hedge_ts = obs_ts
                    obs_idx += 1
                    break

                # Check time-stop (ONLY if NOT in profit - matches live enhanced_spike.py:1177-1195)
                elapsed_ms = obs_ts - entry_ts
                if elapsed_ms >= time_stop_ms:
                    # Get current winner bid to check if in profit
                    winner_side_current = position_data['winner_side']
                    if winner_side_current == "UP":
                        winner_bid_current = obs_row['up_bid']
                    else:
                        winner_bid_current = obs_row['down_bid']

                    # Check if in profit: winner_bid >= entry price
                    in_profit = pd.notna(winner_bid_current) and winner_bid_current >= winner_entry

                    if not in_profit:
                        # NOT in profit - execute time-stop
                        loser_fill = loser_ask if pd.notna(loser_ask) else loser_target * 1.05
                        pair_cost = winner_entry + loser_fill
                        pnl = (1.0 - pair_cost) * TARGET_SHARES

                        trades.append(TradeResult(
                            market_slug=slug,
                            cycle_num=cycle_num,
                            entry_time_remaining=position_data['entry_time_rem'],
                            signal_score=score,
                            winner_side=position_data['winner_side'],
                            winner_fill_price=winner_entry,
                            loser_fill_price=loser_fill,
                            hedge_type="time_stop",
                            pair_cost=pair_cost,
                            pnl=pnl,
                            correct_direction=(resolution == position_data['winner_side']),
                            spike_magnitude=spike_mag,
                            dataset=dataset_name,
                        ))

                        in_position = False
                        position_data = None
                        last_hedge_ts = obs_ts
                        obs_idx += 1
                        break
                    # else: in profit, keep waiting for passive fill (don't time-stop)

                obs_idx += 1

            # If we ran out of observer data while in position
            if in_position and obs_idx >= len(mdf):
                # Resolution fill
                winner_side = position_data['winner_side']
                winner_entry = position_data['winner_entry']
                if resolution == winner_side:
                    pnl = (1.0 - winner_entry) * TARGET_SHARES
                    loser_fill = 0.0
                else:
                    pnl = (0.0 - winner_entry) * TARGET_SHARES
                    loser_fill = 1.0

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
                    pnl=pnl,
                    correct_direction=(resolution == winner_side),
                    spike_magnitude=position_data['spike_magnitude'],
                    dataset=dataset_name,
                ))
                break

            continue

        # Not in position - check next spike
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

        # OBI filter
        if use_obi_filter:
            up_imbalance = obs_row.get('up_imbalance', None)
            down_imbalance = obs_row.get('down_imbalance', None)
            obi_available, obi_confirmed = obi_confirms_spike(spike_dir, up_imbalance, down_imbalance)
            if obi_available and not obi_confirmed:
                spike_idx += 1
                continue

        # High entry check
        winner_side = spike_dir
        if winner_side == "UP":
            winner_ask = obs_row['up_ask']
        else:
            winner_ask = obs_row['down_ask']

        if pd.isna(winner_ask) or winner_ask >= HIGH_ENTRY_THRESHOLD:
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

    # Load resolutions
    res_path = base_dir / "research/observer/market_resolutions_verified.csv"
    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Find overlap
    btc_start, btc_end = btc_df['timestamp_ms'].min(), btc_df['timestamp_ms'].max()
    obs_start, obs_end = obs_df['timestamp_ms'].min(), obs_df['timestamp_ms'].max()

    overlap_start = max(btc_start, obs_start)
    overlap_end = min(btc_end, obs_end)

    duration_hours = (overlap_end - overlap_start) / 3600000
    print(f"  Overlap: {duration_hours:.2f} hours")

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

    # PRECOMPUTE SPIKES with OU ADAPTIVE threshold (per TRADING_CONFIGS.py)
    print(f"  Precomputing spikes with OU adaptive threshold...")
    btc_spikes = precompute_spikes_ou(btc_df)

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


def print_results(trades: List[TradeResult], dataset_key: str, hours: float):
    """Print results for a dataset."""
    if not trades:
        print(f"\n{dataset_key}: No trades")
        return None

    config = DATASETS[dataset_key]
    df = pd.DataFrame([t.__dict__ for t in trades])

    total_pnl = df['pnl'].sum()
    total_trades = len(df)
    win_rate = df['correct_direction'].mean() * 100
    avg_pair_cost = df['pair_cost'].mean()
    hourly_rate = total_pnl / hours if hours > 0 else 0

    passive = (df['hedge_type'] == 'passive').sum()
    time_stop = (df['hedge_type'] == 'time_stop').sum()
    resolution = (df['hedge_type'] == 'resolution').sum()

    print(f"\n{'='*60}")
    print(f"RESULTS: {config['name']} (OBI {'ON' if config['use_obi'] else 'OFF'})")
    print(f"{'='*60}")
    print(f"Total trades: {total_trades}")
    print(f"Direction accuracy: {win_rate:.1f}%")
    print(f"Total PnL: ${total_pnl:.2f}")
    print(f"Hourly rate: ${hourly_rate:.2f}/hr")
    print(f"Avg pair cost: ${avg_pair_cost:.4f}")
    print(f"\nHedge breakdown:")
    print(f"  Passive: {passive} ({100*passive/total_trades:.1f}%)")
    print(f"  Time-stop: {time_stop} ({100*time_stop/total_trades:.1f}%)")
    print(f"  Resolution: {resolution} ({100*resolution/total_trades:.1f}%)")

    return {
        'dataset': dataset_key,
        'name': config['name'],
        'obi': 'ON' if config['use_obi'] else 'OFF',
        'trades': total_trades,
        'pnl': total_pnl,
        'hourly_rate': hourly_rate,
        'win_rate': win_rate,
        'avg_pair_cost': avg_pair_cost,
        'passive_pct': 100*passive/total_trades,
        'time_stop_pct': 100*time_stop/total_trades,
        'hours': hours,
    }


def main():
    print("=" * 60)
    print("MULTI-DATASET AGGRESSIVE BACKTEST")
    print("Config from TRADING_CONFIGS.py - OU ADAPTIVE threshold")
    print("=" * 60)

    # Load OU parameters for adaptive threshold
    load_ou_params()

    all_results = []
    all_trades = []

    for dataset_key in DATASETS.keys():
        trades, hours = run_backtest_dataset(dataset_key)
        all_trades.extend(trades)

        if trades:
            result = print_results(trades, dataset_key, hours)
            if result:
                all_results.append(result)

    # Combined summary
    if all_results:
        print("\n" + "=" * 60)
        print("COMBINED SUMMARY")
        print("=" * 60)

        total_hours = sum(r['hours'] for r in all_results)
        total_pnl = sum(r['pnl'] for r in all_results)
        total_trades = sum(r['trades'] for r in all_results)

        print(f"\nTotal hours: {total_hours:.1f}")
        print(f"Total trades: {total_trades}")
        print(f"Total PnL: ${total_pnl:.2f}")
        print(f"Combined hourly rate: ${total_pnl/total_hours:.2f}/hr")

        print("\n" + "-" * 80)
        print(f"{'Dataset':<20} {'OBI':<5} {'Trades':>8} {'PnL':>12} {'$/hr':>10} {'Win%':>8}")
        print("-" * 80)
        for r in all_results:
            print(f"{r['dataset']:<20} {r['obi']:<5} {r['trades']:>8} ${r['pnl']:>10.2f} ${r['hourly_rate']:>9.2f} {r['win_rate']:>7.1f}%")

    # Save results
    if all_trades:
        out_path = Path("research/findings/data/multi_dataset_validated_results.csv")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame([t.__dict__ for t in all_trades])
        df.to_csv(out_path, index=False)
        print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()
