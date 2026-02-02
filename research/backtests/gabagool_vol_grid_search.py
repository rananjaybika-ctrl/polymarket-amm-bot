#!/usr/bin/env python3
"""
Gabagool-Style Backtest with Volatility Filtering - Grid Search

Hypothesis: The "expensive side = winner" heuristic may work better in
certain volatility regimes. This script grid searches over:
1. Volatility filter thresholds (low/medium/high)
2. Entry price limits
3. Minimum price difference for conviction
4. Time remaining filters

Uses PRECOMPUTED volatility for speed.

Usage:
    python research/backtests/gabagool_vol_grid_search.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Tuple, Dict, Optional
from tqdm import tqdm
from itertools import product
import sys

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class GabagoolVolConfig:
    """Configuration for volatility-filtered Gabagool strategy."""
    # Volatility filter
    vol_window: int = 60  # Rolling window for volatility (seconds)
    min_vol: float = 0.0  # Minimum volatility to trade
    max_vol: float = 999.0  # Maximum volatility to trade

    # Entry conditions
    max_pair_cost: float = 1.05
    max_entry_price: float = 0.65
    min_entry_price: float = 0.40
    min_price_diff: float = 0.02
    min_time_remaining: int = 300

    # Trade sizing
    target_shares: int = 50
    min_trade_interval_ms: int = 10000

    # Fees
    taker_fee_rate: float = 0.02

    name: str = "default"


# Dataset
DATASET_CONFIG = {
    "name": "IS+OOS2 (Jan 16-19)",
    "obs_files": [
        "research/observer/grid_obs_20260116.csv",
        "research/observer/grid_obs_20260117.csv",
        "research/observer/grid_obs_20260118.csv",
        "research/observer/grid_obs_20260119.csv",
    ],
}


# =============================================================================
# PRECOMPUTATION
# =============================================================================

def precompute_volatility(df: pd.DataFrame, window_secs: int = 60) -> pd.DataFrame:
    """
    Precompute rolling volatility from BTC price.

    Volatility = std of returns over window.
    """
    df = df.copy()

    # Estimate sample rate (should be ~5Hz)
    if len(df) > 100:
        median_delta = df['timestamp_ms'].diff().median()
        samples_per_sec = 1000 / median_delta if median_delta > 0 else 5
    else:
        samples_per_sec = 5

    window_samples = int(window_secs * samples_per_sec)

    if 'binance_price' in df.columns:
        # Calculate returns
        df['btc_return'] = df['binance_price'].pct_change() * 100

        # Rolling volatility (std of returns)
        df['volatility'] = df['btc_return'].rolling(
            window=window_samples,
            min_periods=window_samples // 2
        ).std()

        # Fill NaN with median
        median_vol = df['volatility'].median()
        df['volatility'] = df['volatility'].fillna(median_vol)
    else:
        df['volatility'] = 0.05  # Default

    return df


def precompute_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Precompute expensive side signals.
    """
    df = df.copy()

    # Expensive side detection
    df['up_is_expensive'] = df['up_ask'] > df['down_ask']
    df['price_diff'] = (df['up_ask'] - df['down_ask']).abs()
    df['pair_cost'] = df['up_ask'] + df['down_ask']

    # Winner side based on expensive
    df['predicted_winner'] = np.where(df['up_is_expensive'], 'UP', 'DOWN')
    df['winner_price'] = np.where(df['up_is_expensive'], df['up_ask'], df['down_ask'])

    return df


# =============================================================================
# SIMULATION
# =============================================================================

def simulate_market_vectorized(
    mdf: pd.DataFrame,
    resolution: str,
    config: GabagoolVolConfig
) -> Dict:
    """
    Vectorized simulation for a single market.
    Returns summary stats instead of individual trades for speed.
    """
    if len(mdf) == 0:
        return None

    # Apply filters
    mask = (
        (mdf['time_remaining_secs'] >= config.min_time_remaining) &
        (mdf['pair_cost'] < config.max_pair_cost) &
        (mdf['winner_price'] >= config.min_entry_price) &
        (mdf['winner_price'] <= config.max_entry_price) &
        (mdf['price_diff'] >= config.min_price_diff) &
        (mdf['volatility'] >= config.min_vol) &
        (mdf['volatility'] <= config.max_vol) &
        (mdf['predicted_winner'].notna())
    )

    valid_rows = mdf[mask].copy()

    if len(valid_rows) == 0:
        return None

    # Subsample based on trade interval
    # Group by time buckets
    valid_rows['time_bucket'] = (valid_rows['timestamp_ms'] // config.min_trade_interval_ms)
    trades = valid_rows.groupby('time_bucket').first().reset_index(drop=True)

    if len(trades) == 0:
        return None

    # Calculate outcomes
    trades['correct'] = trades['predicted_winner'] == resolution
    trades['pnl_gross'] = np.where(
        trades['correct'],
        (1.0 - trades['winner_price']) * config.target_shares,
        (0.0 - trades['winner_price']) * config.target_shares
    )
    trades['fee'] = config.taker_fee_rate * trades['winner_price'] * config.target_shares
    trades['pnl_net'] = trades['pnl_gross'] - trades['fee']

    return {
        'n_trades': len(trades),
        'n_correct': trades['correct'].sum(),
        'pnl_gross': trades['pnl_gross'].sum(),
        'pnl_net': trades['pnl_net'].sum(),
        'fees': trades['fee'].sum(),
        'avg_vol': trades['volatility'].mean(),
        'avg_price_diff': trades['price_diff'].mean(),
        'avg_winner_price': trades['winner_price'].mean(),
    }


def run_backtest(
    obs_df: pd.DataFrame,
    res_map: Dict[str, str],
    config: GabagoolVolConfig,
    show_progress: bool = False
) -> Dict:
    """
    Run full backtest with given config.
    """
    slugs = obs_df['market_slug'].unique()

    results = []
    iterator = tqdm(slugs, desc=config.name) if show_progress else slugs

    for slug in iterator:
        resolution = res_map.get(slug)
        if resolution not in ['UP', 'DOWN']:
            continue

        mdf = obs_df[obs_df['market_slug'] == slug]
        result = simulate_market_vectorized(mdf, resolution, config)

        if result:
            results.append(result)

    if not results:
        return {
            'config': config.name,
            'n_trades': 0,
            'win_rate': 0,
            'pnl_net': 0,
            'hourly_rate': 0,
        }

    total_trades = sum(r['n_trades'] for r in results)
    total_correct = sum(r['n_correct'] for r in results)
    total_pnl_net = sum(r['pnl_net'] for r in results)
    total_fees = sum(r['fees'] for r in results)

    return {
        'config': config.name,
        'n_trades': total_trades,
        'n_correct': total_correct,
        'win_rate': total_correct / total_trades * 100 if total_trades > 0 else 0,
        'pnl_gross': sum(r['pnl_gross'] for r in results),
        'pnl_net': total_pnl_net,
        'fees': total_fees,
        'avg_vol': np.mean([r['avg_vol'] for r in results]),
        'avg_price_diff': np.mean([r['avg_price_diff'] for r in results]),
        'n_markets': len(results),
    }


# =============================================================================
# DATA LOADING
# =============================================================================

def load_data() -> Tuple[pd.DataFrame, Dict[str, str], float]:
    """Load and preprocess data."""
    base_dir = Path("/Users/rananjaybika/polymarket-amm-bot")

    print("Loading data...")

    # Load observer files
    obs_dfs = []
    for fname in DATASET_CONFIG['obs_files']:
        fpath = base_dir / fname
        if fpath.exists():
            df = pd.read_csv(fpath, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)
            print(f"  {fpath.name}: {len(df):,} rows")

    if not obs_dfs:
        raise FileNotFoundError("No observer files found")

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    print(f"  Combined: {len(obs_df):,} rows")

    # Load resolutions
    res_path = base_dir / "research/observer/market_resolutions_verified.csv"
    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Add resolutions
    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    # Calculate duration
    duration_hours = (obs_df['timestamp_ms'].max() - obs_df['timestamp_ms'].min()) / 3600000

    # Filter valid markets
    valid_slugs = []
    for slug, mdf in obs_df.groupby('market_slug'):
        max_time = mdf['time_remaining_secs'].max()
        min_time = mdf['time_remaining_secs'].min()
        duration = max_time - min_time
        if duration >= 300 and max_time >= 840:
            valid_slugs.append(slug)

    obs_df = obs_df[obs_df['market_slug'].isin(valid_slugs)]
    print(f"  Valid markets: {len(valid_slugs)}")

    # Precompute
    print("Precomputing volatility and signals...")
    obs_df = precompute_volatility(obs_df)
    obs_df = precompute_signals(obs_df)

    # Show volatility distribution
    vol_pcts = obs_df['volatility'].quantile([0.1, 0.25, 0.5, 0.75, 0.9])
    print(f"  Volatility percentiles: p10={vol_pcts.iloc[0]:.4f}, p50={vol_pcts.iloc[2]:.4f}, p90={vol_pcts.iloc[4]:.4f}")

    return obs_df, res_map, duration_hours


# =============================================================================
# GRID SEARCH
# =============================================================================

def run_grid_search(obs_df: pd.DataFrame, res_map: Dict[str, str], duration_hours: float):
    """Run grid search over volatility and entry parameters."""

    # Define parameter grid
    vol_ranges = [
        (0.0, 0.02, "low_vol"),      # Low volatility only
        (0.02, 0.05, "med_vol"),     # Medium volatility
        (0.05, 999.0, "high_vol"),   # High volatility
        (0.0, 0.03, "low_med_vol"),  # Low to medium
        (0.03, 999.0, "med_high_vol"), # Medium to high
        (0.0, 999.0, "all_vol"),     # All volatility
    ]

    max_entry_prices = [0.55, 0.60, 0.65, 0.70]
    min_price_diffs = [0.01, 0.02, 0.03, 0.05]
    min_times = [180, 300, 420]

    results = []

    # Total configs
    n_configs = len(vol_ranges) * len(max_entry_prices) * len(min_price_diffs) * len(min_times)
    print(f"\nRunning grid search over {n_configs} configurations...")

    pbar = tqdm(total=n_configs, desc="Grid Search")

    for (min_vol, max_vol, vol_name), max_entry, min_diff, min_time in product(
        vol_ranges, max_entry_prices, min_price_diffs, min_times
    ):
        config = GabagoolVolConfig(
            min_vol=min_vol,
            max_vol=max_vol,
            max_entry_price=max_entry,
            min_price_diff=min_diff,
            min_time_remaining=min_time,
            name=f"{vol_name}_e{int(max_entry*100)}_d{int(min_diff*100)}_t{min_time}",
        )

        result = run_backtest(obs_df, res_map, config, show_progress=False)
        result['min_vol'] = min_vol
        result['max_vol'] = max_vol
        result['vol_regime'] = vol_name
        result['max_entry_price'] = max_entry
        result['min_price_diff'] = min_diff
        result['min_time_remaining'] = min_time
        result['hourly_rate'] = result['pnl_net'] / duration_hours if duration_hours > 0 else 0

        results.append(result)
        pbar.update(1)

    pbar.close()

    return pd.DataFrame(results)


def analyze_results(results_df: pd.DataFrame):
    """Analyze and print grid search results."""

    print("\n" + "=" * 80)
    print("GRID SEARCH RESULTS")
    print("=" * 80)

    # Filter to configs with trades
    active = results_df[results_df['n_trades'] > 0].copy()

    if len(active) == 0:
        print("No configs had trades!")
        return

    print(f"\nConfigs with trades: {len(active)}/{len(results_df)}")

    # Best by PnL
    print("\n--- TOP 10 BY PNL NET ---")
    top_pnl = active.nlargest(10, 'pnl_net')
    print(f"{'Config':<45} {'Trades':>8} {'Win%':>8} {'PnL Net':>12} {'$/hr':>10}")
    print("-" * 90)
    for _, row in top_pnl.iterrows():
        print(f"{row['config']:<45} {row['n_trades']:>8} {row['win_rate']:>7.1f}% ${row['pnl_net']:>10.2f} ${row['hourly_rate']:>9.2f}")

    # Best by win rate (with min trades)
    print("\n--- TOP 10 BY WIN RATE (min 500 trades) ---")
    enough_trades = active[active['n_trades'] >= 500]
    if len(enough_trades) > 0:
        top_win = enough_trades.nlargest(10, 'win_rate')
        print(f"{'Config':<45} {'Trades':>8} {'Win%':>8} {'PnL Net':>12} {'$/hr':>10}")
        print("-" * 90)
        for _, row in top_win.iterrows():
            print(f"{row['config']:<45} {row['n_trades']:>8} {row['win_rate']:>7.1f}% ${row['pnl_net']:>10.2f} ${row['hourly_rate']:>9.2f}")

    # Analysis by volatility regime
    print("\n--- RESULTS BY VOLATILITY REGIME ---")
    vol_summary = active.groupby('vol_regime').agg({
        'n_trades': 'sum',
        'n_correct': 'sum',
        'pnl_net': 'sum',
        'win_rate': 'mean',
    }).reset_index()
    vol_summary['overall_win_rate'] = vol_summary['n_correct'] / vol_summary['n_trades'] * 100

    print(f"{'Vol Regime':<15} {'Trades':>10} {'Win%':>10} {'Avg Win%':>10} {'PnL Net':>12}")
    print("-" * 60)
    for _, row in vol_summary.iterrows():
        print(f"{row['vol_regime']:<15} {row['n_trades']:>10} {row['overall_win_rate']:>9.1f}% {row['win_rate']:>9.1f}% ${row['pnl_net']:>10.2f}")

    # Analysis by max entry price
    print("\n--- RESULTS BY MAX ENTRY PRICE ---")
    entry_summary = active.groupby('max_entry_price').agg({
        'n_trades': 'sum',
        'n_correct': 'sum',
        'pnl_net': 'sum',
    }).reset_index()
    entry_summary['win_rate'] = entry_summary['n_correct'] / entry_summary['n_trades'] * 100

    print(f"{'Max Entry':>10} {'Trades':>10} {'Win%':>10} {'PnL Net':>12}")
    print("-" * 45)
    for _, row in entry_summary.iterrows():
        print(f"${row['max_entry_price']:>9.2f} {row['n_trades']:>10} {row['win_rate']:>9.1f}% ${row['pnl_net']:>10.2f}")

    # Any profitable configs?
    profitable = active[active['pnl_net'] > 0]
    print(f"\n--- PROFITABLE CONFIGS: {len(profitable)} ---")
    if len(profitable) > 0:
        print(f"{'Config':<45} {'Trades':>8} {'Win%':>8} {'PnL Net':>12} {'$/hr':>10}")
        print("-" * 90)
        for _, row in profitable.nlargest(20, 'pnl_net').iterrows():
            print(f"{row['config']:<45} {row['n_trades']:>8} {row['win_rate']:>7.1f}% ${row['pnl_net']:>10.2f} ${row['hourly_rate']:>9.2f}")
    else:
        print("No profitable configurations found.")
        print("\nThis confirms: The 'expensive side = winner' heuristic")
        print("does NOT work on IS+OOS2 data regardless of volatility regime.")


def main():
    print("=" * 80)
    print("GABAGOOL VOL-FILTERED GRID SEARCH")
    print("Testing: Does volatility filtering make 'expensive side' profitable?")
    print("=" * 80)

    # Load data
    obs_df, res_map, duration_hours = load_data()

    # Run grid search
    results_df = run_grid_search(obs_df, res_map, duration_hours)

    # Analyze
    analyze_results(results_df)

    # Save results
    out_path = Path("research/findings/data/gabagool_vol_grid_search_results.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    results_df.to_csv(out_path, index=False)
    print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()
