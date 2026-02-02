#!/usr/bin/env python3
"""
Gabagool Regime-Filtered Grid Search

Based on findings from regime_detection_analysis.py:
- High conviction (>15%) → 84%+ accuracy in BOTH regimes
- Velocity alignment (21% vs 46%) → key regime indicator
- Velocity magnitude (0.046 vs 0.192) → 4x difference between regimes

This script tests whether filtering by:
1. Price conviction (|up_ask - 0.5|)
2. Velocity-price alignment
3. Velocity magnitude

...can make the "expensive side = winner" strategy profitable on IS+OOS2.

Usage:
    python research/backtests/gabagool_regime_grid_search.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, Tuple
from tqdm import tqdm
from itertools import product
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class RegimeConfig:
    """Configuration for regime-filtered Gabagool strategy."""
    # REGIME FILTERS (new)
    min_conviction: float = 0.15      # |up_ask - 0.5| > threshold
    require_vel_alignment: bool = True # Velocity must agree with expensive side
    min_velocity_mag: float = 0.05    # |velocity_bps| > threshold

    # Entry conditions (existing)
    max_pair_cost: float = 1.05
    max_entry_price: float = 0.70
    min_entry_price: float = 0.35     # Lower to allow high conviction cheap prices
    min_price_diff: float = 0.01      # Relaxed - conviction handles this
    min_time_remaining: int = 300

    # Trade sizing
    target_shares: int = 50
    min_trade_interval_ms: int = 10000

    # Fees
    taker_fee_rate: float = 0.02

    name: str = "default"


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

def precompute_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Precompute all signals needed for regime filtering."""
    df = df.copy()

    # Basic signals
    df['up_is_expensive'] = df['up_ask'] > df['down_ask']
    df['price_diff'] = (df['up_ask'] - df['down_ask']).abs()
    df['pair_cost'] = df['up_ask'] + df['down_ask']

    # CONVICTION: |up_ask - 0.5|
    df['conviction'] = (df['up_ask'] - 0.5).abs()

    # Winner prediction
    df['predicted_winner'] = np.where(df['up_is_expensive'], 'UP', 'DOWN')
    df['winner_price'] = np.where(df['up_is_expensive'], df['up_ask'], df['down_ask'])

    # VELOCITY ALIGNMENT
    # Velocity > 0 means BTC going up → should favor UP
    # Velocity < 0 means BTC going down → should favor DOWN
    df['velocity_agrees'] = (
        ((df['velocity_bps'] > 0) & df['up_is_expensive']) |
        ((df['velocity_bps'] < 0) & ~df['up_is_expensive'])
    )

    # VELOCITY MAGNITUDE
    df['velocity_mag'] = df['velocity_bps'].abs()

    return df


# =============================================================================
# SIMULATION
# =============================================================================

def simulate_market(
    mdf: pd.DataFrame,
    resolution: str,
    config: RegimeConfig
) -> Dict:
    """Simulate trading for a single market with regime filters."""
    if len(mdf) == 0:
        return None

    # Build filter mask
    mask = (
        # Basic filters
        (mdf['time_remaining_secs'] >= config.min_time_remaining) &
        (mdf['pair_cost'] < config.max_pair_cost) &
        (mdf['winner_price'] >= config.min_entry_price) &
        (mdf['winner_price'] <= config.max_entry_price) &
        (mdf['price_diff'] >= config.min_price_diff) &
        (mdf['predicted_winner'].notna()) &

        # REGIME FILTERS
        (mdf['conviction'] >= config.min_conviction) &
        (mdf['velocity_mag'] >= config.min_velocity_mag)
    )

    # Velocity alignment filter (optional)
    if config.require_vel_alignment:
        mask = mask & mdf['velocity_agrees']

    valid_rows = mdf[mask].copy()

    if len(valid_rows) == 0:
        return None

    # Subsample based on trade interval
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
        'avg_conviction': trades['conviction'].mean(),
        'avg_velocity_mag': trades['velocity_mag'].mean(),
        'avg_winner_price': trades['winner_price'].mean(),
        'vel_align_rate': trades['velocity_agrees'].mean() if 'velocity_agrees' in trades else 0,
    }


def run_backtest(
    obs_df: pd.DataFrame,
    res_map: Dict[str, str],
    config: RegimeConfig,
) -> Dict:
    """Run full backtest with given config."""
    slugs = obs_df['market_slug'].unique()
    results = []

    for slug in slugs:
        resolution = res_map.get(slug)
        if resolution not in ['UP', 'DOWN']:
            continue

        mdf = obs_df[obs_df['market_slug'] == slug]
        result = simulate_market(mdf, resolution, config)

        if result:
            results.append(result)

    if not results:
        return {
            'config': config.name,
            'n_trades': 0,
            'win_rate': 0,
            'pnl_net': 0,
        }

    total_trades = sum(r['n_trades'] for r in results)
    total_correct = sum(r['n_correct'] for r in results)
    total_pnl_net = sum(r['pnl_net'] for r in results)

    return {
        'config': config.name,
        'n_trades': total_trades,
        'n_correct': total_correct,
        'win_rate': total_correct / total_trades * 100 if total_trades > 0 else 0,
        'pnl_gross': sum(r['pnl_gross'] for r in results),
        'pnl_net': total_pnl_net,
        'fees': sum(r['fees'] for r in results),
        'avg_conviction': np.mean([r['avg_conviction'] for r in results]),
        'avg_velocity_mag': np.mean([r['avg_velocity_mag'] for r in results]),
        'n_markets': len(results),
    }


# =============================================================================
# DATA LOADING
# =============================================================================

def load_data() -> Tuple[pd.DataFrame, Dict[str, str], float]:
    """Load and preprocess data."""
    base_dir = Path("/Users/rananjaybika/polymarket-amm-bot")

    print("Loading data...")

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

    # Filter to resolved markets
    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    # Duration
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

    # Precompute signals
    print("Precomputing signals...")
    obs_df = precompute_signals(obs_df)

    # Show distributions
    print(f"\n  Signal distributions:")
    print(f"    Conviction: mean={obs_df['conviction'].mean():.3f}, p50={obs_df['conviction'].median():.3f}")
    print(f"    Velocity mag: mean={obs_df['velocity_mag'].mean():.4f}, p50={obs_df['velocity_mag'].median():.4f}")
    print(f"    Velocity alignment: {obs_df['velocity_agrees'].mean():.1%}")

    return obs_df, res_map, duration_hours


# =============================================================================
# GRID SEARCH
# =============================================================================

def run_grid_search(obs_df: pd.DataFrame, res_map: Dict[str, str], duration_hours: float):
    """Run grid search over regime parameters."""

    # Parameter grid
    conviction_thresholds = [0.0, 0.10, 0.15, 0.20, 0.25]  # 0 = no filter
    velocity_alignment = [False, True]
    velocity_magnitudes = [0.0, 0.03, 0.05, 0.10, 0.15]  # 0 = no filter
    max_entry_prices = [0.65, 0.70, 0.75]
    min_times = [180, 300]

    results = []
    n_configs = (len(conviction_thresholds) * len(velocity_alignment) *
                 len(velocity_magnitudes) * len(max_entry_prices) * len(min_times))

    print(f"\nRunning grid search over {n_configs} configurations...")
    pbar = tqdm(total=n_configs, desc="Grid Search")

    for conv, vel_align, vel_mag, max_entry, min_time in product(
        conviction_thresholds, velocity_alignment, velocity_magnitudes,
        max_entry_prices, min_times
    ):
        # Build config name
        name_parts = []
        if conv > 0:
            name_parts.append(f"conv{int(conv*100)}")
        if vel_align:
            name_parts.append("align")
        if vel_mag > 0:
            name_parts.append(f"vmag{int(vel_mag*100)}")
        name_parts.append(f"e{int(max_entry*100)}")
        name_parts.append(f"t{min_time}")

        config = RegimeConfig(
            min_conviction=conv,
            require_vel_alignment=vel_align,
            min_velocity_mag=vel_mag,
            max_entry_price=max_entry,
            min_time_remaining=min_time,
            name="_".join(name_parts) if name_parts else "baseline",
        )

        result = run_backtest(obs_df, res_map, config)
        result['min_conviction'] = conv
        result['require_vel_alignment'] = vel_align
        result['min_velocity_mag'] = vel_mag
        result['max_entry_price'] = max_entry
        result['min_time_remaining'] = min_time
        result['hourly_rate'] = result['pnl_net'] / duration_hours if duration_hours > 0 else 0

        results.append(result)
        pbar.update(1)

    pbar.close()
    return pd.DataFrame(results)


def analyze_results(results_df: pd.DataFrame):
    """Analyze and print grid search results."""

    print("\n" + "=" * 80)
    print("REGIME-FILTERED GRID SEARCH RESULTS")
    print("=" * 80)

    active = results_df[results_df['n_trades'] > 0].copy()
    print(f"\nConfigs with trades: {len(active)}/{len(results_df)}")

    if len(active) == 0:
        print("No configs had trades!")
        return

    # Best by PnL
    print("\n--- TOP 15 BY PNL NET ---")
    top_pnl = active.nlargest(15, 'pnl_net')
    print(f"{'Config':<40} {'Trades':>7} {'Win%':>7} {'PnL Net':>10} {'$/hr':>8}")
    print("-" * 80)
    for _, row in top_pnl.iterrows():
        print(f"{row['config']:<40} {row['n_trades']:>7} {row['win_rate']:>6.1f}% ${row['pnl_net']:>8.0f} ${row['hourly_rate']:>7.0f}")

    # Best by win rate
    print("\n--- TOP 15 BY WIN RATE (min 100 trades) ---")
    enough_trades = active[active['n_trades'] >= 100]
    if len(enough_trades) > 0:
        top_win = enough_trades.nlargest(15, 'win_rate')
        print(f"{'Config':<40} {'Trades':>7} {'Win%':>7} {'PnL Net':>10} {'$/hr':>8}")
        print("-" * 80)
        for _, row in top_win.iterrows():
            print(f"{row['config']:<40} {row['n_trades']:>7} {row['win_rate']:>6.1f}% ${row['pnl_net']:>8.0f} ${row['hourly_rate']:>7.0f}")

    # Analysis by conviction
    print("\n--- RESULTS BY CONVICTION THRESHOLD ---")
    conv_summary = active.groupby('min_conviction').agg({
        'n_trades': 'sum',
        'n_correct': 'sum',
        'pnl_net': 'sum',
    }).reset_index()
    conv_summary['win_rate'] = conv_summary['n_correct'] / conv_summary['n_trades'] * 100

    print(f"{'Conviction':>10} {'Trades':>10} {'Win%':>8} {'PnL Net':>12}")
    print("-" * 45)
    for _, row in conv_summary.iterrows():
        print(f"{row['min_conviction']:>10.2f} {row['n_trades']:>10} {row['win_rate']:>7.1f}% ${row['pnl_net']:>10.0f}")

    # Analysis by velocity alignment
    print("\n--- RESULTS BY VELOCITY ALIGNMENT ---")
    align_summary = active.groupby('require_vel_alignment').agg({
        'n_trades': 'sum',
        'n_correct': 'sum',
        'pnl_net': 'sum',
    }).reset_index()
    align_summary['win_rate'] = align_summary['n_correct'] / align_summary['n_trades'] * 100

    print(f"{'Vel Align':>10} {'Trades':>10} {'Win%':>8} {'PnL Net':>12}")
    print("-" * 45)
    for _, row in align_summary.iterrows():
        align_str = "Required" if row['require_vel_alignment'] else "Not Req"
        print(f"{align_str:>10} {row['n_trades']:>10} {row['win_rate']:>7.1f}% ${row['pnl_net']:>10.0f}")

    # Analysis by velocity magnitude
    print("\n--- RESULTS BY VELOCITY MAGNITUDE ---")
    vmag_summary = active.groupby('min_velocity_mag').agg({
        'n_trades': 'sum',
        'n_correct': 'sum',
        'pnl_net': 'sum',
    }).reset_index()
    vmag_summary['win_rate'] = vmag_summary['n_correct'] / vmag_summary['n_trades'] * 100

    print(f"{'Vel Mag':>10} {'Trades':>10} {'Win%':>8} {'PnL Net':>12}")
    print("-" * 45)
    for _, row in vmag_summary.iterrows():
        print(f"{row['min_velocity_mag']:>10.2f} {row['n_trades']:>10} {row['win_rate']:>7.1f}% ${row['pnl_net']:>10.0f}")

    # Profitable configs
    profitable = active[active['pnl_net'] > 0]
    print(f"\n--- PROFITABLE CONFIGS: {len(profitable)} ---")
    if len(profitable) > 0:
        print(f"{'Config':<40} {'Trades':>7} {'Win%':>7} {'PnL Net':>10} {'$/hr':>8}")
        print("-" * 80)
        for _, row in profitable.nlargest(20, 'pnl_net').iterrows():
            print(f"{row['config']:<40} {row['n_trades']:>7} {row['win_rate']:>6.1f}% ${row['pnl_net']:>8.0f} ${row['hourly_rate']:>7.0f}")

        # Analyze what makes profitable configs work
        print("\n--- PROFITABLE CONFIG CHARACTERISTICS ---")
        print(f"  Avg conviction threshold: {profitable['min_conviction'].mean():.2f}")
        print(f"  Require vel alignment: {profitable['require_vel_alignment'].mean():.1%}")
        print(f"  Avg vel magnitude threshold: {profitable['min_velocity_mag'].mean():.3f}")
        print(f"  Avg win rate: {profitable['win_rate'].mean():.1f}%")
    else:
        print("No profitable configurations found.")
        print("\nEven with regime filters, the strategy may not be profitable on IS+OOS2.")
        print("This suggests the regime difference is more fundamental than these signals can capture.")


def main():
    print("=" * 80)
    print("GABAGOOL REGIME-FILTERED GRID SEARCH")
    print("Testing: Conviction + Velocity Alignment + Velocity Magnitude filters")
    print("=" * 80)

    # Load data
    obs_df, res_map, duration_hours = load_data()

    # Run grid search
    results_df = run_grid_search(obs_df, res_map, duration_hours)

    # Analyze
    analyze_results(results_df)

    # Save results
    out_path = Path("research/findings/data/gabagool_regime_grid_search_results.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    results_df.to_csv(out_path, index=False)
    print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()
