#!/usr/bin/env python3
"""
Multi-Dataset AGGRESSIVE Backtest (OBI OFF)

Runs AGGRESSIVE strategy backtest on multiple datasets:
- IS+OOS2 (Jan 16-19)
- OOS5 (Jan 26)

Uses 5Hz observer binance_price data with vectorized spike detection.
Lookback adjusted: 6 ticks at 5Hz ≈ 1200ms (matches 72 ticks at 60Hz)

Pre-computes spikes for speed while maintaining live realism.

Usage:
    python research/backtests/multi_dataset_backtest.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple
from tqdm import tqdm
import warnings
warnings.filterwarnings('ignore')

from research.reference.TRADING_CONFIGS import AGGRESSIVE

# =============================================================================
# CONFIGURATION - IMPORTED FROM TRADING_CONFIGS.py (SOURCE OF TRUTH)
# =============================================================================

# From TRADING_CONFIGS.py
TARGET_SHARES = 10  # Match live config
MIN_TIME = AGGRESSIVE.min_time_remaining  # 180.0 seconds
MIN_RUNTIME_SECS = 300  # 5 min minimum market duration
TIME_STOP_SECONDS = AGGRESSIVE.time_stop_seconds  # 120.0 seconds
SKIP_HIGH_ENTRY = AGGRESSIVE.skip_high_entry  # True
HIGH_ENTRY_THRESHOLD = AGGRESSIVE.high_entry_threshold  # 0.90

# Spike detection - adjusted for 5Hz data
# CANONICAL: lookback_ms from TRADING_CONFIGS
# At 5Hz (200ms per tick): lookback_ms / 200ms = ticks
LOOKBACK_MS = AGGRESSIVE.lookback_ms  # 1200ms
SPIKE_LOOKBACK_5HZ = LOOKBACK_MS // 200  # 6 ticks at 5Hz ≈ 1200ms
SPIKE_THRESHOLD = 0.02  # 0.02% minimum spike (fixed threshold mode)

# Velocity confirmation
VELOCITY_CONFIRM_THRESHOLD = 0.05  # |velocity| threshold for confirmation

# Loser bid calculation (FIXED - no /100 bug)
DROP_MULTIPLIER = 0.50
DROP_INTERCEPT = 0.08
TARGET_PAIR_COST = 0.99

# Cycling
MIN_CYCLE_GAP_MS = 1000  # 1 second between cycles

# =============================================================================
# DATASET DEFINITIONS
# =============================================================================

DATASETS = {
    "IS+OOS2": {
        "name": "IS+OOS2 (Jan 16-19)",
        "files": [
            "grid_obs_20260116.csv",
            "grid_obs_20260117.csv",
            "grid_obs_20260118.csv",
            "grid_obs_20260119.csv",
        ],
    },
    "OOS3+4": {
        "name": "OOS3+4 (Jan 22-24)",
        "files": [
            "PROTECTED_grid_obs_oos3_oos4_combined.csv",
        ],
    },
    "OOS5": {
        "name": "OOS5 (Jan 26)",
        "files": [
            "PROTECTED_grid_obs_20260126_recovered.csv",
        ],
    },
}

# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class TradeResult:
    market_slug: str
    cycle_num: int
    entry_time_remaining: float
    winner_side: str
    winner_fill_price: float
    loser_fill_price: float
    hedge_type: str  # passive, time_stop, resolution
    pair_cost: float
    pnl: float
    correct_direction: bool
    spike_magnitude: float
    dataset: str


# =============================================================================
# VECTORIZED SPIKE DETECTION (Pre-compute for speed)
# =============================================================================

def detect_spikes_vectorized(df: pd.DataFrame, lookback: int = SPIKE_LOOKBACK_5HZ) -> pd.DataFrame:
    """
    Vectorized spike detection on observer data using binance_price.

    Pre-computes all spikes at once for efficiency.
    Uses fixed threshold (no OU adaptive) to match simpler live behavior.
    """
    df = df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    # Calculate price change over lookback
    df['price_prev'] = df['binance_price'].shift(lookback)
    df['change_pct'] = (df['binance_price'] - df['price_prev']) / df['price_prev'] * 100
    df['magnitude'] = df['change_pct'].abs()

    # Fixed threshold detection
    df['spike_detected'] = df['magnitude'] >= SPIKE_THRESHOLD
    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['change_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['change_pct'] < 0), 'spike_direction'] = 'DOWN'
    df['spike_magnitude'] = df['magnitude'].where(df['spike_detected'], 0)

    return df


def velocity_confirms_spike(spike_dir: str, velocity_bps: float) -> bool:
    """Check if velocity confirms spike direction."""
    if spike_dir == "UP":
        return velocity_bps > -VELOCITY_CONFIRM_THRESHOLD
    else:  # DOWN
        return velocity_bps < VELOCITY_CONFIRM_THRESHOLD


def calculate_loser_bid(winner_entry: float, spike_magnitude: float) -> float:
    """Calculate loser bid price. FIXED: No /100 division."""
    expected_drop = DROP_MULTIPLIER * spike_magnitude + DROP_INTERCEPT
    expected_drop = max(0.02, min(0.20, expected_drop))
    max_loser = TARGET_PAIR_COST - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid))


# =============================================================================
# SIMULATION
# =============================================================================

def simulate_market(
    mdf: pd.DataFrame,
    slug: str,
    resolution: str,
    dataset_name: str,
) -> List[TradeResult]:
    """
    Simulate trading on a single market using pre-computed spikes.

    Maintains live realism:
    - Processes rows in time order
    - Respects cycling (one position at a time)
    - Uses time-stop logic
    - Checks passive fills against loser ask
    """
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    market_end = mdf['timestamp_ms'].max()
    trades = []

    in_position = False
    position_data = None
    last_hedge_ts = 0
    cycle_num = 0

    for idx, row in mdf.iterrows():
        ts = row['timestamp_ms']
        time_rem = row['time_remaining_secs']

        # Skip if too close to market end
        if time_rem < MIN_TIME:
            continue

        # If in position, check for hedge/time-stop
        if in_position and position_data is not None:
            winner_side = position_data['winner_side']
            loser_side = position_data['loser_side']
            winner_entry = position_data['winner_entry']
            loser_target = position_data['loser_target']
            entry_ts = position_data['entry_ts']
            spike_mag = position_data['spike_magnitude']

            # Get current loser ask
            if loser_side == "UP":
                loser_ask = row['up_ask']
            else:
                loser_ask = row['down_ask']

            # Check passive fill
            if pd.notna(loser_ask) and loser_ask <= loser_target:
                loser_fill = loser_target
                pair_cost = winner_entry + loser_fill
                pnl = (1.0 - pair_cost) * TARGET_SHARES

                trades.append(TradeResult(
                    market_slug=slug,
                    cycle_num=cycle_num,
                    entry_time_remaining=position_data['entry_time_rem'],
                    winner_side=winner_side,
                    winner_fill_price=winner_entry,
                    loser_fill_price=loser_fill,
                    hedge_type="passive",
                    pair_cost=pair_cost,
                    pnl=pnl,
                    correct_direction=(resolution == winner_side),
                    spike_magnitude=spike_mag,
                    dataset=dataset_name,
                ))

                in_position = False
                position_data = None
                last_hedge_ts = ts
                continue

            # Check time-stop
            elapsed_ms = ts - entry_ts
            if elapsed_ms >= TIME_STOP_SECONDS * 1000:
                loser_fill = loser_ask if pd.notna(loser_ask) else loser_target * 1.05
                pair_cost = winner_entry + loser_fill
                pnl = (1.0 - pair_cost) * TARGET_SHARES

                trades.append(TradeResult(
                    market_slug=slug,
                    cycle_num=cycle_num,
                    entry_time_remaining=position_data['entry_time_rem'],
                    winner_side=winner_side,
                    winner_fill_price=winner_entry,
                    loser_fill_price=loser_fill,
                    hedge_type="time_stop",
                    pair_cost=pair_cost,
                    pnl=pnl,
                    correct_direction=(resolution == winner_side),
                    spike_magnitude=spike_mag,
                    dataset=dataset_name,
                ))

                in_position = False
                position_data = None
                last_hedge_ts = ts
                continue

            # Still in position, continue
            continue

        # Not in position - look for entry signal

        # Enforce cycle gap
        if (ts - last_hedge_ts) < MIN_CYCLE_GAP_MS:
            continue

        # Check for spike
        if not row.get('spike_detected', False):
            continue

        spike_dir = row.get('spike_direction')
        spike_mag = row.get('spike_magnitude', 0)

        if spike_dir is None or spike_mag == 0:
            continue

        # Velocity confirmation (OBI OFF)
        velocity_bps = row.get('velocity_bps', 0) or 0
        if not velocity_confirms_spike(spike_dir, velocity_bps):
            continue

        # Get entry price
        winner_side = spike_dir
        loser_side = "DOWN" if winner_side == "UP" else "UP"

        if winner_side == "UP":
            winner_entry = row['up_ask']
        else:
            winner_entry = row['down_ask']

        if pd.isna(winner_entry) or winner_entry <= 0:
            continue

        # Skip high entry (turkey problem)
        if SKIP_HIGH_ENTRY and winner_entry >= HIGH_ENTRY_THRESHOLD:
            continue

        # Calculate loser target
        loser_target = calculate_loser_bid(winner_entry, spike_mag)

        # Enter position
        cycle_num += 1
        in_position = True
        position_data = {
            'winner_side': winner_side,
            'loser_side': loser_side,
            'winner_entry': winner_entry,
            'loser_target': loser_target,
            'entry_ts': ts,
            'entry_time_rem': time_rem,
            'spike_magnitude': spike_mag,
        }

    # Handle position held to resolution
    if in_position and position_data is not None:
        winner_side = position_data['winner_side']
        loser_side = position_data['loser_side']
        winner_entry = position_data['winner_entry']
        spike_mag = position_data['spike_magnitude']

        # Resolution fill
        if resolution == winner_side:
            # Winner resolves to $1, loser to $0
            loser_fill = 0.0
            pair_cost = winner_entry + loser_fill
        else:
            # Wrong direction - winner goes to $0, loser to $1
            loser_fill = 1.0
            pair_cost = winner_entry + loser_fill

        pnl = (1.0 - pair_cost) * TARGET_SHARES

        trades.append(TradeResult(
            market_slug=slug,
            cycle_num=cycle_num,
            entry_time_remaining=position_data['entry_time_rem'],
            winner_side=winner_side,
            winner_fill_price=winner_entry,
            loser_fill_price=loser_fill,
            hedge_type="resolution",
            pair_cost=pair_cost,
            pnl=pnl,
            correct_direction=(resolution == winner_side),
            spike_magnitude=spike_mag,
            dataset=dataset_name,
        ))

    return trades


# =============================================================================
# MAIN
# =============================================================================

def load_dataset(dataset_key: str) -> Tuple[pd.DataFrame, Dict[str, str], float]:
    """Load and prepare a dataset."""
    config = DATASETS[dataset_key]
    obs_dir = Path("research/observer")

    print(f"\n{'='*60}")
    print(f"Loading {config['name']}")
    print(f"{'='*60}")

    # Load observer files
    dfs = []
    for fname in config['files']:
        fpath = obs_dir / fname
        if fpath.exists():
            df = pd.read_csv(fpath, on_bad_lines='skip', low_memory=False)
            dfs.append(df)
            print(f"  {fname}: {len(df):,} rows")
        else:
            print(f"  {fname}: NOT FOUND")

    if not dfs:
        return None, {}, 0

    obs_df = pd.concat(dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    print(f"  Combined: {len(obs_df):,} rows")

    # Load resolutions
    res_path = obs_dir / "market_resolutions_verified.csv"
    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Add resolution
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

    # Calculate duration
    duration_hours = (obs_df['timestamp_ms'].max() - obs_df['timestamp_ms'].min()) / 3600000

    print(f"  Valid markets: {len(valid_slugs)}")
    print(f"  Duration: {duration_hours:.2f} hours")

    return obs_df, res_map, duration_hours


def run_backtest(dataset_key: str) -> List[TradeResult]:
    """Run backtest on a single dataset."""
    obs_df, res_map, hours = load_dataset(dataset_key)

    if obs_df is None or len(obs_df) == 0:
        return []

    config = DATASETS[dataset_key]

    # Pre-compute spikes (vectorized for speed)
    print(f"\nPre-computing spikes...")
    obs_df = detect_spikes_vectorized(obs_df, lookback=SPIKE_LOOKBACK_5HZ)
    spike_count = obs_df['spike_detected'].sum()
    print(f"  Spikes detected: {spike_count:,}")

    # Run simulation
    print(f"\nSimulating trades...")
    all_trades = []
    slugs = obs_df['market_slug'].unique()

    for slug in tqdm(slugs, desc=config['name']):
        mdf = obs_df[obs_df['market_slug'] == slug]
        resolution = res_map.get(slug)
        if resolution:
            trades = simulate_market(mdf, slug, resolution, dataset_key)
            all_trades.extend(trades)

    return all_trades


def print_results(trades: List[TradeResult], dataset_key: str, hours: float):
    """Print results summary."""
    if not trades:
        print(f"\n{dataset_key}: No trades")
        return

    df = pd.DataFrame([t.__dict__ for t in trades])

    total_pnl = df['pnl'].sum()
    total_trades = len(df)
    win_rate = df['correct_direction'].mean() * 100
    avg_pair_cost = df['pair_cost'].mean()
    hourly_rate = total_pnl / hours if hours > 0 else 0

    # Hedge breakdown
    passive = (df['hedge_type'] == 'passive').sum()
    time_stop = (df['hedge_type'] == 'time_stop').sum()
    resolution = (df['hedge_type'] == 'resolution').sum()

    print(f"\n{'='*60}")
    print(f"RESULTS: {DATASETS[dataset_key]['name']}")
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
        'trades': total_trades,
        'pnl': total_pnl,
        'hourly_rate': hourly_rate,
        'win_rate': win_rate,
        'avg_pair_cost': avg_pair_cost,
        'passive_pct': 100*passive/total_trades,
        'hours': hours,
    }


def main():
    print("="*60)
    print("MULTI-DATASET AGGRESSIVE BACKTEST (OBI OFF)")
    print("Using 5Hz observer binance_price data")
    print("Lookback: 6 ticks at 5Hz ≈ 1200ms")
    print("="*60)

    all_results = []
    all_trades = []

    for dataset_key in DATASETS.keys():
        trades = run_backtest(dataset_key)
        all_trades.extend(trades)

        # Get hours for this dataset
        _, _, hours = load_dataset(dataset_key)

        if trades:
            result = print_results(trades, dataset_key, hours)
            if result:
                all_results.append(result)

    # Combined summary
    if all_trades:
        print("\n" + "="*60)
        print("COMBINED SUMMARY (All Datasets)")
        print("="*60)

        df = pd.DataFrame([t.__dict__ for t in all_trades])
        total_hours = sum(r['hours'] for r in all_results)

        print(f"Total trades: {len(df)}")
        print(f"Total hours: {total_hours:.1f}")
        print(f"Total PnL: ${df['pnl'].sum():.2f}")
        print(f"Combined hourly rate: ${df['pnl'].sum() / total_hours:.2f}/hr")
        print(f"Win rate: {df['correct_direction'].mean()*100:.1f}%")
        print(f"Avg pair cost: ${df['pair_cost'].mean():.4f}")

        # Per-dataset comparison
        print("\n" + "-"*60)
        print(f"{'Dataset':<20} {'Trades':>8} {'PnL':>10} {'$/hr':>10} {'Win%':>8}")
        print("-"*60)
        for r in all_results:
            print(f"{r['dataset']:<20} {r['trades']:>8} ${r['pnl']:>9.2f} ${r['hourly_rate']:>9.2f} {r['win_rate']:>7.1f}%")

    # Save results
    if all_trades:
        out_path = Path("research/findings/data/multi_dataset_backtest_results.csv")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame([t.__dict__ for t in all_trades])
        df.to_csv(out_path, index=False)
        print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()
