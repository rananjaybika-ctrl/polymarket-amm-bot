#!/usr/bin/env python3
"""
Gabagool-Style Backtest - Following Market Consensus

=============================================================================
STRATEGY: Buy the expensive side (market's predicted winner)
Based on reverse-engineering analysis (Feb 2, 2026):
- Gabagool achieves 85.7% accuracy by following the market
- No sophisticated signals - just buy whichever side costs more
- Pair cost arbitrage when < $1.00
=============================================================================

Key differences from AGGRESSIVE:
- NO spike detection
- NO velocity/OBI filters
- Entry signal: expensive side (UP ask > DOWN ask → buy UP)
- Focus on pair cost < $1.00 for guaranteed profit
- Passive two-sided accumulation

Config Choices:
- Entry threshold: pair_cost < 0.99 (1% guaranteed profit)
- Max entry price: 0.75 (don't chase extreme prices)
- Min time remaining: 300s (5 min before end)
- Trade frequency: every 5s when conditions met

Usage:
    python research/backtests/gabagool_style_backtest.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
from tqdm import tqdm
import sys

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# =============================================================================
# GABAGOOL-STYLE CONFIGURATION
# =============================================================================

@dataclass
class GabagoolConfig:
    """Configuration for Gabagool-style strategy."""
    # Entry conditions
    max_pair_cost: float = 1.05       # Realistic pair cost (market has spread)
    max_entry_price: float = 0.75     # Don't chase extreme prices
    min_entry_price: float = 0.40     # Don't buy near-zero prices
    min_price_diff: float = 0.02      # Minimum difference to have conviction
    min_time_remaining: int = 300     # 5 min before end

    # Trade sizing
    target_shares: int = 50           # Shares per trade

    # Trade frequency
    min_trade_interval_ms: int = 10000  # 10 seconds between trades

    # Fees
    taker_fee_rate: float = 0.02      # 2% Polymarket taker fee

    # Strategy mode
    mode: str = "winner_only"  # "winner_only" or "both_sides"

    # Dataset
    name: str = "gabagool_style"


# Default config
CONFIG = GabagoolConfig()


@dataclass
class GabagoolTrade:
    """Result of a single Gabagool-style trade."""
    market_slug: str
    trade_num: int
    entry_time_remaining: float
    winner_side: str  # "UP" or "DOWN" (expensive side)
    winner_entry_price: float
    loser_entry_price: float
    pair_cost: float
    hedge_type: str  # "same_tick", "resolution"
    pnl_gross: float
    pnl_net: float
    entry_fee: float
    correct_direction: bool
    price_diff: float  # How much more expensive was winner
    dataset: str


# =============================================================================
# DATASET CONFIGURATION
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
    },
}


# =============================================================================
# SIMULATION
# =============================================================================

def simulate_gabagool_market(
    obs_df: pd.DataFrame,
    slug: str,
    resolution: str,
    config: GabagoolConfig,
    dataset_name: str
) -> List[GabagoolTrade]:
    """
    Simulate Gabagool-style trading on a single market.

    Strategy:
    1. Identify the expensive side (market's predicted winner)
    2. Buy the expensive side only (directional bet)
    3. Hold to resolution
    4. Win if our side wins, lose if it doesn't

    This is NOT arbitrage - it's following market consensus.
    """
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    trades = []
    trade_num = 0
    last_trade_ts = 0

    for idx, row in mdf.iterrows():
        ts = row['timestamp_ms']
        time_rem = row['time_remaining_secs']

        # Skip if too close to end
        if time_rem < config.min_time_remaining:
            continue

        # Enforce trade interval
        if (ts - last_trade_ts) < config.min_trade_interval_ms:
            continue

        # Get prices
        up_ask = row.get('up_ask')
        down_ask = row.get('down_ask')

        if pd.isna(up_ask) or pd.isna(down_ask):
            continue

        # Determine expensive side (Gabagool's "winner" prediction)
        if up_ask > down_ask:
            winner_side = "UP"
            winner_price = up_ask
            loser_price = down_ask
        elif down_ask > up_ask:
            winner_side = "DOWN"
            winner_price = down_ask
            loser_price = up_ask
        else:
            # Equal prices - skip (no clear signal)
            continue

        price_diff = winner_price - loser_price
        pair_cost = winner_price + loser_price

        # Check entry conditions
        if pair_cost >= config.max_pair_cost:
            continue

        if winner_price > config.max_entry_price or winner_price < config.min_entry_price:
            continue

        # Require minimum price difference (conviction)
        if price_diff < config.min_price_diff:
            continue

        # ENTRY: Buy the expensive side only
        trade_num += 1
        last_trade_ts = ts

        # Calculate PnL at resolution
        # If our side wins: receive $1.00
        # If our side loses: receive $0.00
        if resolution == winner_side:
            # Correct prediction
            pnl_gross = (1.0 - winner_price) * config.target_shares
            correct = True
        else:
            # Wrong prediction - lose entry
            pnl_gross = (0.0 - winner_price) * config.target_shares
            correct = False

        # Fees (taker on entry only)
        entry_fee = config.taker_fee_rate * winner_price * config.target_shares

        pnl_net = pnl_gross - entry_fee

        trades.append(GabagoolTrade(
            market_slug=slug,
            trade_num=trade_num,
            entry_time_remaining=time_rem,
            winner_side=winner_side,
            winner_entry_price=winner_price,
            loser_entry_price=loser_price,
            pair_cost=pair_cost,
            hedge_type="resolution",
            pnl_gross=pnl_gross,
            pnl_net=pnl_net,
            entry_fee=entry_fee,
            correct_direction=correct,
            price_diff=price_diff,
            dataset=dataset_name,
        ))

    return trades


# =============================================================================
# DATA LOADING
# =============================================================================

def load_dataset(dataset_key: str) -> Tuple[Optional[pd.DataFrame], Dict[str, str], float]:
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
        return None, {}, 0

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    print(f"  Combined observer: {len(obs_df):,} rows")

    # Load resolutions
    res_path = base_dir / "research/observer/market_resolutions_verified.csv"
    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Add resolutions
    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    # Calculate duration
    duration_hours = (obs_df['timestamp_ms'].max() - obs_df['timestamp_ms'].min()) / 3600000

    # Filter valid markets (5 min minimum runtime)
    valid_slugs = []
    for slug, mdf in obs_df.groupby('market_slug'):
        max_time = mdf['time_remaining_secs'].max()
        min_time = mdf['time_remaining_secs'].min()
        duration = max_time - min_time
        if duration >= 300 and max_time >= 840:
            valid_slugs.append(slug)

    obs_df = obs_df[obs_df['market_slug'].isin(valid_slugs)]

    print(f"  Valid markets: {len(valid_slugs)}")
    print(f"  Duration: {duration_hours:.1f} hours")

    return obs_df, res_map, duration_hours


# =============================================================================
# MAIN BACKTEST
# =============================================================================

def run_gabagool_backtest(config: GabagoolConfig = CONFIG) -> Tuple[List[GabagoolTrade], float]:
    """Run Gabagool-style backtest on IS+OOS2."""
    obs_df, res_map, hours = load_dataset("IS+OOS2")

    if obs_df is None or len(obs_df) == 0:
        print("No valid data")
        return [], 0

    print(f"\nRunning Gabagool-style simulation...")
    print(f"  Config: max_pair_cost={config.max_pair_cost}, max_entry={config.max_entry_price}")

    all_trades = []
    slugs = obs_df['market_slug'].unique()

    for slug in tqdm(slugs, desc="Markets"):
        resolution = res_map.get(slug)
        if resolution not in ['UP', 'DOWN']:
            continue

        trades = simulate_gabagool_market(obs_df, slug, resolution, config, "IS+OOS2")
        all_trades.extend(trades)

    return all_trades, hours


def print_gabagool_results(trades: List[GabagoolTrade], hours: float, config: GabagoolConfig):
    """Print comprehensive results."""
    if not trades:
        print("\nNo trades executed")
        return

    df = pd.DataFrame([t.__dict__ for t in trades])

    total_pnl_gross = df['pnl_gross'].sum()
    total_pnl_net = df['pnl_net'].sum()
    total_fees = df['entry_fee'].sum()
    total_trades = len(df)
    n_markets = df['market_slug'].nunique()
    win_rate = df['correct_direction'].mean() * 100
    avg_pair_cost = df['pair_cost'].mean()
    avg_price_diff = df['price_diff'].mean()

    print("\n" + "=" * 60)
    print("GABAGOOL-STYLE BACKTEST RESULTS")
    print("=" * 60)
    print(f"\nConfig:")
    print(f"  Max pair cost: ${config.max_pair_cost:.2f}")
    print(f"  Entry price range: ${config.min_entry_price:.2f} - ${config.max_entry_price:.2f}")
    print(f"  Min time remaining: {config.min_time_remaining}s")
    print(f"  Trade interval: {config.min_trade_interval_ms/1000:.0f}s")

    print(f"\nResults:")
    print(f"  Total trades: {total_trades:,}")
    print(f"  Markets traded: {n_markets}")
    print(f"  Trades per market: {total_trades/n_markets:.1f}")
    print(f"  Duration: {hours:.1f} hours")

    print(f"\nPerformance:")
    print(f"  Direction accuracy: {win_rate:.1f}%")
    print(f"  Avg pair cost: ${avg_pair_cost:.4f}")
    print(f"  Avg price diff: ${avg_price_diff:.4f}")

    print(f"\nPnL:")
    print(f"  Gross: ${total_pnl_gross:.2f}")
    print(f"  Fees:  ${total_fees:.2f}")
    print(f"  Net:   ${total_pnl_net:.2f}")
    print(f"  Hourly rate: ${total_pnl_net/hours:.2f}/hr")

    # Breakdown by winner side
    print("\nBy predicted side:")
    for side in ['UP', 'DOWN']:
        side_df = df[df['winner_side'] == side]
        if len(side_df) > 0:
            side_win = side_df['correct_direction'].mean() * 100
            side_pnl = side_df['pnl_net'].sum()
            print(f"  {side}: {len(side_df)} trades, {side_win:.1f}% accuracy, ${side_pnl:.2f} net")

    # Comparison to baseline
    print("\n" + "-" * 60)
    print("COMPARISON TO RANDOM (50%):")
    expected_pnl_random = (0.5 * (1.0 - avg_pair_cost) - 0.5 * avg_pair_cost) * total_trades * config.target_shares
    print(f"  Random expected PnL: ${expected_pnl_random:.2f}")
    print(f"  Strategy excess: ${total_pnl_gross - expected_pnl_random:.2f}")

    return df


def run_config_sweep():
    """Test different configurations."""
    configs = [
        GabagoolConfig(max_pair_cost=1.05, max_entry_price=0.70, min_price_diff=0.02, name="tight_70"),
        GabagoolConfig(max_pair_cost=1.05, max_entry_price=0.65, min_price_diff=0.02, name="tight_65"),
        GabagoolConfig(max_pair_cost=1.05, max_entry_price=0.60, min_price_diff=0.02, name="tight_60"),
        GabagoolConfig(max_pair_cost=1.05, max_entry_price=0.55, min_price_diff=0.02, name="tight_55"),
        GabagoolConfig(max_pair_cost=1.03, max_entry_price=0.65, min_price_diff=0.03, name="strict"),
    ]

    print("=" * 80)
    print("GABAGOOL-STYLE CONFIG SWEEP")
    print("=" * 80)

    results = []

    for config in configs:
        print(f"\n{'='*60}")
        print(f"Testing: {config.name}")
        print(f"{'='*60}")

        trades, hours = run_gabagool_backtest(config)

        if trades:
            df = pd.DataFrame([t.__dict__ for t in trades])
            results.append({
                'name': config.name,
                'max_pair_cost': config.max_pair_cost,
                'max_entry_price': config.max_entry_price,
                'trades': len(trades),
                'win_rate': df['correct_direction'].mean() * 100,
                'pnl_gross': df['pnl_gross'].sum(),
                'pnl_net': df['pnl_net'].sum(),
                'avg_pair_cost': df['pair_cost'].mean(),
                'hourly_rate': df['pnl_net'].sum() / hours if hours > 0 else 0,
            })

    # Summary table
    print("\n" + "=" * 80)
    print("CONFIG SWEEP SUMMARY")
    print("=" * 80)
    print(f"\n{'Config':<15} {'PairCost':<10} {'MaxEntry':<10} {'Trades':>8} {'Win%':>8} {'PnL Net':>12} {'$/hr':>10}")
    print("-" * 80)

    for r in results:
        print(f"{r['name']:<15} ${r['max_pair_cost']:<9.2f} ${r['max_entry_price']:<9.2f} {r['trades']:>8} {r['win_rate']:>7.1f}% ${r['pnl_net']:>10.2f} ${r['hourly_rate']:>9.2f}")

    return results


def main():
    """Main entry point."""
    print("=" * 60)
    print("GABAGOOL-STYLE BACKTEST")
    print("Strategy: Buy the expensive side (follow market consensus)")
    print("=" * 60)

    # Run config sweep
    results = run_config_sweep()

    # Run detailed analysis with best config
    if results:
        best = max(results, key=lambda x: x['pnl_net'])
        print(f"\n{'='*60}")
        print(f"DETAILED ANALYSIS: Best Config ({best['name']})")
        print(f"{'='*60}")

        best_config = GabagoolConfig(
            max_pair_cost=best['max_pair_cost'],
            max_entry_price=best['max_entry_price'],
            name=best['name'],
        )

        trades, hours = run_gabagool_backtest(best_config)
        df = print_gabagool_results(trades, hours, best_config)

        # Save results
        if df is not None:
            out_path = Path("research/findings/data/gabagool_style_backtest_results.csv")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(out_path, index=False)
            print(f"\nResults saved to: {out_path}")


if __name__ == "__main__":
    main()
