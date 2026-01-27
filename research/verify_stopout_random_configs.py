#!/usr/bin/env python3
"""
Stop-Out Verification on 5 Random Configs

Verifies stop-loss trigger mechanics and premature stop % on configs
OUTSIDE the top 10, to confirm findings hold across the full 1440 grid search.

Selection criteria for variety:
- Different methods (ou, ewma)
- Different z-score methods (ou, ewma, percentile, ewma_ratio)
- Different stop-loss % (7%, 12%, 15%)
- Mix of cycling ON/OFF

Author: Claude Code
Date: January 22, 2026
"""

import sys
import random
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.volatility_filter_analysis import (
    load_ou_params, load_btc_data, load_observer_data,
    compute_zscore_series, BacktestConfig, run_backtest_with_zscore,
    TradeWithZScore
)


def parse_z_zone(z_zone_lo, z_zone_hi):
    """Parse z-zone bounds from CSV values."""
    z_lo = None if z_zone_lo == -999 else z_zone_lo
    z_hi = None if z_zone_hi == 999 else z_zone_hi
    return z_lo, z_hi


def analyze_stopout_detailed(trades: List[TradeWithZScore], z_lo, z_hi) -> Optional[Dict]:
    """Analyze stop-out breakdown for trades in a z-zone."""
    # Filter to z-zone
    filtered = []
    for t in trades:
        z = t.zscore_at_entry
        if z_lo is not None and z <= z_lo:
            continue
        if z_hi is not None and z >= z_hi:
            continue
        filtered.append(t)

    if not filtered:
        return None

    # Exit type breakdown
    passive = [t for t in filtered if t.hedge_type == "passive"]
    stoploss = [t for t in filtered if t.hedge_type == "stoploss"]
    resolution = [t for t in filtered if t.hedge_type == "resolution"]

    # Stop-loss analysis
    sl_correct = [t for t in stoploss if t.correct_direction]
    sl_wrong = [t for t in stoploss if not t.correct_direction]

    sl_correct_pnl = sum(t.pnl for t in sl_correct)
    sl_wrong_pnl = sum(t.pnl for t in sl_wrong)

    # Overall metrics
    total_pnl = sum(t.pnl for t in filtered)
    wins = sum(1 for t in filtered if t.pnl > 0)
    correct_dir = sum(1 for t in filtered if t.correct_direction)

    # Passive fill time analysis
    passive_fill_times = []
    for t in passive:
        if t.exit_ts and t.entry_ts:
            fill_time = (t.exit_ts - t.entry_ts) / 1000.0
            passive_fill_times.append(fill_time)

    return {
        'trades': len(filtered),
        'pnl': total_pnl,
        'win_rate': wins / len(filtered) * 100 if filtered else 0,
        'dir_acc': correct_dir / len(filtered) * 100 if filtered else 0,
        'passive': len(passive),
        'stoploss': len(stoploss),
        'resolution': len(resolution),
        'sl_correct': len(sl_correct),
        'sl_wrong': len(sl_wrong),
        'sl_correct_pct': len(sl_correct) / len(stoploss) * 100 if stoploss else 0,
        'sl_correct_pnl': sl_correct_pnl,
        'sl_wrong_pnl': sl_wrong_pnl,
        'passive_fill_median': np.median(passive_fill_times) if passive_fill_times else 0,
        'passive_fill_p25': np.percentile(passive_fill_times, 25) if passive_fill_times else 0,
        'passive_fill_p75': np.percentile(passive_fill_times, 75) if passive_fill_times else 0,
    }


def select_random_configs(grid_df: pd.DataFrame, n: int = 5) -> pd.DataFrame:
    """
    Select n random configs with variety, avoiding top 10.

    Ensures diversity in:
    - methods (ou, ewma)
    - zscore methods (ou, ewma, percentile, ewma_ratio)
    - stop_loss (0.07, 0.12, 0.15)
    - cycling (True, False)
    """
    # Skip top 10 (already analyzed)
    grid_df = grid_df.sort_values('hourly_rate', ascending=False)
    remaining = grid_df.iloc[10:].copy()

    # Also skip configs with very few trades (< 30)
    remaining = remaining[remaining['trades'] >= 30]

    selected = []
    used_combos = set()

    # Try to get variety
    attempts = 0
    max_attempts = 1000

    while len(selected) < n and attempts < max_attempts:
        attempts += 1
        row = remaining.sample(1).iloc[0]

        # Create a diversity key
        diversity_key = (row['method'], row['zscore_method'], row['cycling'])

        # Accept if we haven't seen this combo, or if we've tried many times
        if diversity_key not in used_combos or attempts > 500:
            selected.append(row)
            used_combos.add(diversity_key)

    return pd.DataFrame(selected)


def main():
    print("=" * 100)
    print("STOP-OUT VERIFICATION: 5 RANDOM CONFIGS (Outside Top 10)")
    print("=" * 100)

    # Set random seed for reproducibility
    random.seed(42)
    np.random.seed(42)

    # Load grid search results
    results_path = Path("/Users/rananjaybika/polymarket-amm-bot/research/vol_filter_grid_results_all_combined.csv")
    grid_df = pd.read_csv(results_path)
    print(f"\nLoaded {len(grid_df)} total configs from grid search")

    # Select 5 random configs (avoiding top 10)
    selected = select_random_configs(grid_df, n=5)

    print(f"\n{'='*100}")
    print("SELECTED CONFIGS FOR VERIFICATION")
    print("="*100)
    print(f"\n{'Rank':<6} {'Method':<6} {'ZScore':<12} {'Lookback':<10} {'SL':<6} {'Cyc':<5} {'Zone':<15} {'$/hr':<8}")
    print("-"*80)

    for idx, row in selected.iterrows():
        rank = list(grid_df.sort_values('hourly_rate', ascending=False).index).index(idx) + 1
        print(f"{rank:<6} {row['method']:<6} {row['zscore_method']:<12} {row['lookback_ms']}ms    "
              f"{row['stop_loss']:.0%}   {'Y' if row['cycling'] else 'N':<5} "
              f"{row['z_zone_label']:<15} ${row['hourly_rate']:.3f}")

    # Load data
    print("\n" + "="*100)
    print("LOADING DATA")
    print("="*100)

    ou_params = load_ou_params()
    btc_df = load_btc_data()
    obs_df, res_map = load_observer_data()

    # Calculate total hours
    btc_start = btc_df['timestamp_ms'].min()
    btc_end = btc_df['timestamp_ms'].max()
    total_hours = (btc_end - btc_start) / 3600000
    print(f"Dataset: {total_hours:.2f} hours")

    # Pre-compute z-score dataframes for each method needed
    print("\nPre-computing z-score series...")
    zscore_cache = {}
    needed_methods = selected['zscore_method'].unique()
    for method in needed_methods:
        print(f"  Computing {method}...")
        zscore_cache[method] = compute_zscore_series(btc_df, ou_params, zscore_method=method)

    # Run verification on each config
    print("\n" + "="*100)
    print("VERIFICATION RESULTS")
    print("="*100)

    results = []

    for idx, row in selected.iterrows():
        rank = list(grid_df.sort_values('hourly_rate', ascending=False).index).index(idx) + 1

        method = row['method']
        zscore_method = row['zscore_method']
        lookback = int(row['lookback_ticks'])
        sl = row['stop_loss']
        cycling = bool(row['cycling'])
        z_lo, z_hi = parse_z_zone(row['z_zone_lo'], row['z_zone_hi'])
        z_zone_label = row['z_zone_label']
        csv_hourly = row['hourly_rate']
        csv_trades = row['trades']

        print(f"\n{'='*80}")
        print(f"Config #{rank}: {method}/{zscore_method}/{row['lookback_ms']}ms, SL={sl:.0%}, Cycling={'ON' if cycling else 'OFF'}, Zone={z_zone_label}")
        print("="*80)

        # Run backtest
        zscore_df = zscore_cache[zscore_method]

        config = BacktestConfig(
            target_shares=5,
            spike_lookback=lookback,
            stop_loss_pct=sl,
            use_cycling=cycling,
        )

        trades = run_backtest_with_zscore(
            config, btc_df, obs_df, zscore_df, res_map,
            method=method,
            ou_params=ou_params,
            quiet=True
        )

        # Analyze stop-out breakdown
        stats = analyze_stopout_detailed(trades, z_lo, z_hi)

        if not stats:
            print("  No trades in z-zone!")
            continue

        # Print detailed results
        print(f"\nCSV Baseline:")
        print(f"  Trades: {csv_trades}, $/hr: ${csv_hourly:.4f}")

        print(f"\nVerified Results:")
        print(f"  Total Trades: {stats['trades']}")
        print(f"  Total PnL: ${stats['pnl']:.2f}")
        print(f"  Win Rate: {stats['win_rate']:.1f}%")
        print(f"  Direction Acc: {stats['dir_acc']:.1f}%")

        print(f"\nExit Type Breakdown:")
        print(f"  Passive fills:  {stats['passive']} ({stats['passive']/stats['trades']*100:.1f}%)")
        print(f"  Stop-losses:    {stats['stoploss']} ({stats['stoploss']/stats['trades']*100:.1f}%)")
        print(f"  Resolution:     {stats['resolution']} ({stats['resolution']/stats['trades']*100:.1f}%)")

        print(f"\nSTOP-LOSS ANALYSIS (Key Metric):")
        print(f"  Total stop-losses:     {stats['stoploss']}")
        print(f"  Correct direction:     {stats['sl_correct']} ({stats['sl_correct_pct']:.1f}%) <- PREMATURE STOPS")
        print(f"  Wrong direction:       {stats['sl_wrong']} ({100-stats['sl_correct_pct']:.1f}%) <- RIGHTFUL STOPS")
        print(f"  PnL lost (premature):  ${stats['sl_correct_pnl']:.2f}")
        print(f"  PnL lost (rightful):   ${stats['sl_wrong_pnl']:.2f}")

        if stats['passive_fill_median'] > 0:
            print(f"\nPassive Fill Time:")
            print(f"  Median: {stats['passive_fill_median']:.1f}s")
            print(f"  P25-P75: {stats['passive_fill_p25']:.1f}s - {stats['passive_fill_p75']:.1f}s")

        results.append({
            'rank': rank,
            'config': f"{method}/{zscore_method}/{row['lookback_ms']}ms",
            'cycling': cycling,
            'z_zone': z_zone_label,
            'sl_pct': sl,
            'trades': stats['trades'],
            'win_rate': stats['win_rate'],
            'sl_correct_pct': stats['sl_correct_pct'],
            'sl_correct_pnl': stats['sl_correct_pnl'],
        })

    # Summary table
    print("\n" + "="*100)
    print("SUMMARY: PREMATURE STOP-OUT RATES ACROSS 5 RANDOM CONFIGS")
    print("="*100)

    print(f"\n{'Rank':<6} {'Config':<35} {'Cyc':<5} {'Zone':<15} {'SL':<6} {'Trades':<8} {'WinRate':<10} {'Prem%':<10} {'PremPnL':<12}")
    print("-"*115)

    for r in results:
        print(f"{r['rank']:<6} {r['config']:<35} {'Y' if r['cycling'] else 'N':<5} {r['z_zone']:<15} "
              f"{r['sl_pct']:.0%}   {r['trades']:<8} {r['win_rate']:<9.1f}% "
              f"{r['sl_correct_pct']:<9.1f}% ${r['sl_correct_pnl']:<11.2f}")

    # Compute average
    if results:
        avg_prem_pct = np.mean([r['sl_correct_pct'] for r in results])
        avg_prem_pnl = np.mean([r['sl_correct_pnl'] for r in results])

        print("-"*115)
        print(f"{'AVERAGE':<62} {'':<14} {'':<8} {'':<10} {avg_prem_pct:<9.1f}% ${avg_prem_pnl:<11.2f}")

    # Comparison with top 10
    print("\n" + "="*100)
    print("COMPARISON: Random 5 vs Top 10 Findings")
    print("="*100)

    print(f"\nTop 10 Average Premature Stop %: ~40% (from stop_out_analysis_results.csv)")
    print(f"Random 5 Average Premature Stop %: {avg_prem_pct:.1f}%")

    if avg_prem_pct > 35:
        print("\nFINDING CONFIRMED: Premature stop-out rate remains high (>35%) outside top 10.")
        print("This validates the concern about price-based stops triggering on correct-direction trades.")
    else:
        print("\nFINDING DIFFERS: Random configs show lower premature stop rate.")
        print("Top 10 configs may be outliers due to higher trade frequency/more aggressive settings.")


if __name__ == "__main__":
    main()
