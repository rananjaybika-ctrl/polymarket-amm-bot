#!/usr/bin/env python3
"""
Stop-Out Analysis for Top 50 Configs

Analyzes which configs minimize "wrong" stop-losses (where direction was actually correct
but we got stopped out due to temporary price dip).

Key Metrics:
- Total stop-losses
- Stop-losses with correct direction (premature exits)
- Stop-losses with wrong direction (rightful exits)
- PnL lost from premature stop-outs

Author: Claude Code
Date: January 22, 2026
"""

import sys
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
import pandas as pd
import numpy as np
from tqdm import tqdm

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from research.volatility_filter_analysis import (
    load_ou_params, load_btc_data, load_observer_data,
    compute_zscore_series, BacktestConfig, run_backtest_with_zscore,
    TradeWithZScore
)


@dataclass
class StopOutAnalysis:
    """Results of stop-out analysis for a config."""
    config_rank: int
    method: str
    zscore_method: str
    lookback_ms: int
    stop_loss: float
    cycling: bool
    z_zone: str

    # Overall metrics
    total_trades: int
    total_pnl: float
    hourly_rate: float
    win_rate: float
    direction_acc: float

    # Hedge type breakdown
    passive_fills: int
    stop_losses: int
    resolutions: int

    # Stop-loss analysis
    sl_correct_dir: int  # Stop-losses where direction was correct
    sl_wrong_dir: int    # Stop-losses where direction was wrong
    sl_correct_dir_pct: float  # % of stop-losses that had correct direction
    sl_correct_pnl_lost: float  # PnL lost from premature stop-outs
    sl_wrong_pnl: float  # PnL from "correct" stop-outs

    # Passive fill analysis
    passive_fill_time_median: float
    passive_fill_time_p25: float
    passive_fill_time_p75: float


def analyze_stop_outs(
    trades: List[TradeWithZScore],
    z_lo: Optional[float],
    z_hi: Optional[float],
) -> Tuple[List[TradeWithZScore], Dict]:
    """
    Analyze stop-out trades for a given z-zone filter.

    Returns:
        filtered_trades: Trades within z-zone
        stats: Dict with stop-out statistics
    """
    # Filter trades by z-zone
    filtered = []
    for t in trades:
        z = t.zscore_at_entry
        if z_lo is not None and z <= z_lo:
            continue
        if z_hi is not None and z >= z_hi:
            continue
        filtered.append(t)

    if not filtered:
        return [], {}

    # Categorize by hedge type
    passive = [t for t in filtered if t.hedge_type == "passive"]
    stoploss = [t for t in filtered if t.hedge_type == "stoploss"]
    resolution = [t for t in filtered if t.hedge_type == "resolution"]

    # Analyze stop-losses
    sl_correct = [t for t in stoploss if t.correct_direction]
    sl_wrong = [t for t in stoploss if not t.correct_direction]

    sl_correct_pnl = sum(t.pnl for t in sl_correct)
    sl_wrong_pnl = sum(t.pnl for t in sl_wrong)

    # Passive fill times
    fill_times = []
    for t in passive:
        if t.exit_ts and t.entry_ts:
            fill_time_s = (t.exit_ts - t.entry_ts) / 1000.0
            fill_times.append(fill_time_s)

    fill_time_stats = {
        'median': np.median(fill_times) if fill_times else 0,
        'p25': np.percentile(fill_times, 25) if fill_times else 0,
        'p75': np.percentile(fill_times, 75) if fill_times else 0,
    }

    stats = {
        'total_trades': len(filtered),
        'passive_fills': len(passive),
        'stop_losses': len(stoploss),
        'resolutions': len(resolution),
        'sl_correct_dir': len(sl_correct),
        'sl_wrong_dir': len(sl_wrong),
        'sl_correct_dir_pct': len(sl_correct) / len(stoploss) * 100 if stoploss else 0,
        'sl_correct_pnl_lost': sl_correct_pnl,
        'sl_wrong_pnl': sl_wrong_pnl,
        'fill_time_median': fill_time_stats['median'],
        'fill_time_p25': fill_time_stats['p25'],
        'fill_time_p75': fill_time_stats['p75'],
        'total_pnl': sum(t.pnl for t in filtered),
        'wins': sum(1 for t in filtered if t.pnl > 0),
        'correct_dir': sum(1 for t in filtered if t.correct_direction),
    }

    return filtered, stats


def parse_z_zone(z_zone_lo, z_zone_hi) -> Tuple[Optional[float], Optional[float]]:
    """Parse z-zone bounds from CSV values."""
    z_lo = None if z_zone_lo == -999 else z_zone_lo
    z_hi = None if z_zone_hi == 999 else z_zone_hi
    return z_lo, z_hi


def run_top_n_analysis(n: int = 50) -> pd.DataFrame:
    """Run stop-out analysis for top N configs by $/hr."""

    print("=" * 100)
    print(f"STOP-OUT ANALYSIS FOR TOP {n} CONFIGS")
    print("=" * 100)

    # Load grid search results
    results_path = Path("research/vol_filter_grid_results_all_combined.csv")
    if not results_path.exists():
        results_path = Path("/Users/rananjaybika/polymarket-amm-bot/research/vol_filter_grid_results_all_combined.csv")

    grid_df = pd.read_csv(results_path)
    grid_df = grid_df.sort_values('hourly_rate', ascending=False)
    top_n = grid_df.head(n)

    print(f"\nLoaded {len(grid_df)} total configs, analyzing top {n}")

    # Load data
    print("\nLoading data...")
    ou_params = load_ou_params()
    btc_df = load_btc_data()
    obs_df, res_map = load_observer_data()

    # Calculate total hours
    btc_start = btc_df['timestamp_ms'].min()
    btc_end = btc_df['timestamp_ms'].max()
    total_hours = (btc_end - btc_start) / 3600000
    print(f"Dataset: {total_hours:.2f} hours")

    # Cache for backtest results (key: method + zscore_method + lookback + sl + cycling)
    backtest_cache = {}

    # Pre-compute z-score dataframes for each method
    print("\nPre-computing z-score series for all methods...")
    zscore_cache = {}
    for method in tqdm(['ou', 'ewma', 'percentile', 'ewma_ratio'], desc="Z-score methods"):
        zscore_cache[method] = compute_zscore_series(btc_df, ou_params, zscore_method=method)

    results = []

    print(f"\nAnalyzing {n} configs...")
    pbar = tqdm(top_n.iterrows(), total=len(top_n), desc="Configs")

    for idx, row in pbar:
        rank = list(top_n.index).index(idx) + 1

        method = row['method']
        zscore_method = row['zscore_method']
        lookback = int(row['lookback_ticks'])
        sl = row['stop_loss']
        cycling = bool(row['cycling'])
        z_lo, z_hi = parse_z_zone(row['z_zone_lo'], row['z_zone_hi'])
        z_zone_label = row['z_zone_label']

        pbar.set_description(f"#{rank} {method}/{zscore_method}/{lookback}t")

        # Build cache key (without z-zone - we reuse backtest across z-zones)
        cache_key = (method, zscore_method, lookback, sl, cycling)

        if cache_key not in backtest_cache:
            # Run backtest
            config = BacktestConfig(
                target_shares=5,  # Same as grid search
                spike_lookback=lookback,
                stop_loss_pct=sl,
                use_cycling=cycling,
            )

            zscore_df = zscore_cache[zscore_method]

            trades = run_backtest_with_zscore(
                config, btc_df, obs_df, zscore_df, res_map,
                method=method,
                ou_params=ou_params,
                quiet=True
            )
            backtest_cache[cache_key] = trades
        else:
            trades = backtest_cache[cache_key]

        # Analyze stop-outs for this z-zone
        filtered_trades, stats = analyze_stop_outs(trades, z_lo, z_hi)

        if not stats:
            continue

        # Compute hourly rate
        from research.volatility_filter_analysis import estimate_active_hours_zone
        hours_active = estimate_active_hours_zone(total_hours, zscore_cache[zscore_method], z_lo, z_hi)
        hourly_rate = stats['total_pnl'] / hours_active if hours_active > 0 else 0

        result = StopOutAnalysis(
            config_rank=rank,
            method=method,
            zscore_method=zscore_method,
            lookback_ms=int(lookback * 1000 / 60),
            stop_loss=sl,
            cycling=cycling,
            z_zone=z_zone_label,
            total_trades=stats['total_trades'],
            total_pnl=stats['total_pnl'],
            hourly_rate=hourly_rate,
            win_rate=stats['wins'] / stats['total_trades'] * 100 if stats['total_trades'] > 0 else 0,
            direction_acc=stats['correct_dir'] / stats['total_trades'] * 100 if stats['total_trades'] > 0 else 0,
            passive_fills=stats['passive_fills'],
            stop_losses=stats['stop_losses'],
            resolutions=stats['resolutions'],
            sl_correct_dir=stats['sl_correct_dir'],
            sl_wrong_dir=stats['sl_wrong_dir'],
            sl_correct_dir_pct=stats['sl_correct_dir_pct'],
            sl_correct_pnl_lost=stats['sl_correct_pnl_lost'],
            sl_wrong_pnl=stats['sl_wrong_pnl'],
            passive_fill_time_median=stats['fill_time_median'],
            passive_fill_time_p25=stats['fill_time_p25'],
            passive_fill_time_p75=stats['fill_time_p75'],
        )
        results.append(result)

    # Convert to DataFrame
    df = pd.DataFrame([vars(r) for r in results])

    return df


def print_results(df: pd.DataFrame):
    """Print formatted results."""

    print("\n" + "=" * 140)
    print("TOP 50 CONFIGS - STOP-OUT ANALYSIS")
    print("=" * 140)

    print(f"\n{'Rank':<5} {'Method':<12} {'Z-Method':<10} {'Lookback':<9} {'SL':<5} {'Cyc':<4} "
          f"{'Z-Zone':<15} {'$/hr':<8} {'Trades':<7} {'SL':<5} {'SL-Corr':<8} {'SL-Corr%':<9} {'Lost$':<9}")
    print("-" * 140)

    for _, row in df.iterrows():
        print(f"{row['config_rank']:<5} {row['method']:<12} {row['zscore_method']:<10} "
              f"{row['lookback_ms']}ms     {row['stop_loss']:.0%}   {'Y' if row['cycling'] else 'N':<4} "
              f"{row['z_zone']:<15} ${row['hourly_rate']:<7.2f} {row['total_trades']:<7} "
              f"{row['stop_losses']:<5} {row['sl_correct_dir']:<8} {row['sl_correct_dir_pct']:<8.1f}% "
              f"${row['sl_correct_pnl_lost']:<8.2f}")

    # Summary statistics
    print("\n" + "=" * 100)
    print("SUMMARY STATISTICS")
    print("=" * 100)

    print(f"\nStop-Loss with Correct Direction (Premature Exit):")
    print(f"  Mean: {df['sl_correct_dir_pct'].mean():.1f}%")
    print(f"  Min:  {df['sl_correct_dir_pct'].min():.1f}%")
    print(f"  Max:  {df['sl_correct_dir_pct'].max():.1f}%")
    print(f"  Std:  {df['sl_correct_dir_pct'].std():.1f}%")

    print(f"\nTotal PnL Lost from Premature Stop-Outs:")
    print(f"  Mean: ${df['sl_correct_pnl_lost'].mean():.2f}")
    print(f"  Min:  ${df['sl_correct_pnl_lost'].min():.2f}")
    print(f"  Max:  ${df['sl_correct_pnl_lost'].max():.2f}")

    # Best configs by lowest wrong stop-out rate
    print("\n" + "=" * 100)
    print("BEST CONFIGS BY LOWEST PREMATURE STOP-OUT RATE")
    print("=" * 100)

    # Filter to configs with at least 5 stop-losses for statistical significance
    significant = df[df['stop_losses'] >= 5].copy()
    if len(significant) > 0:
        best_by_sl_correct = significant.nsmallest(10, 'sl_correct_dir_pct')

        print(f"\n{'Rank':<5} {'Config':<50} {'SL-Corr%':<10} {'$/hr':<10} {'Win%':<8}")
        print("-" * 90)

        for _, row in best_by_sl_correct.iterrows():
            config_str = f"{row['method']}/{row['zscore_method']}/{row['lookback_ms']}ms/{row['z_zone']}"
            print(f"{row['config_rank']:<5} {config_str:<50} {row['sl_correct_dir_pct']:<9.1f}% "
                  f"${row['hourly_rate']:<9.2f} {row['win_rate']:<7.1f}%")

    # Configs with NO premature stop-outs (0%)
    zero_premature = df[df['sl_correct_dir_pct'] == 0]
    if len(zero_premature) > 0:
        print(f"\n" + "-" * 100)
        print(f"CONFIGS WITH 0% PREMATURE STOP-OUTS: {len(zero_premature)} configs")
        print("-" * 100)

        for _, row in zero_premature.head(10).iterrows():
            config_str = f"{row['method']}/{row['zscore_method']}/{row['lookback_ms']}ms/{row['z_zone']}"
            print(f"  Rank #{row['config_rank']}: {config_str} - ${row['hourly_rate']:.2f}/hr, {row['stop_losses']} stop-losses")

    # Correlation analysis
    print("\n" + "=" * 100)
    print("CORRELATION ANALYSIS")
    print("=" * 100)

    # Correlation between premature stop-out rate and various metrics
    correlations = {
        'hourly_rate': df['sl_correct_dir_pct'].corr(df['hourly_rate']),
        'win_rate': df['sl_correct_dir_pct'].corr(df['win_rate']),
        'direction_acc': df['sl_correct_dir_pct'].corr(df['direction_acc']),
        'stop_losses': df['sl_correct_dir_pct'].corr(df['stop_losses']),
    }

    print("\nCorrelation with Premature Stop-Out Rate:")
    for metric, corr in correlations.items():
        direction = "↑" if corr > 0 else "↓"
        print(f"  {metric:<20}: {corr:+.3f} {direction}")

    # By cycling
    print("\n" + "-" * 100)
    print("ANALYSIS BY CYCLING")
    print("-" * 100)

    for cycling in [True, False]:
        subset = df[df['cycling'] == cycling]
        if len(subset) > 0:
            label = "ON" if cycling else "OFF"
            print(f"\nCycling {label}:")
            print(f"  Configs: {len(subset)}")
            print(f"  Avg Premature Stop-Out Rate: {subset['sl_correct_dir_pct'].mean():.1f}%")
            print(f"  Avg $/hr: ${subset['hourly_rate'].mean():.2f}")

    # By stop-loss percentage
    print("\n" + "-" * 100)
    print("ANALYSIS BY STOP-LOSS PERCENTAGE")
    print("-" * 100)

    for sl in sorted(df['stop_loss'].unique()):
        subset = df[df['stop_loss'] == sl]
        if len(subset) > 0:
            print(f"\nStop-Loss {sl:.0%}:")
            print(f"  Configs: {len(subset)}")
            print(f"  Avg Premature Stop-Out Rate: {subset['sl_correct_dir_pct'].mean():.1f}%")
            print(f"  Avg $/hr: ${subset['hourly_rate'].mean():.2f}")

    # By lookback
    print("\n" + "-" * 100)
    print("ANALYSIS BY LOOKBACK")
    print("-" * 100)

    for lookback in sorted(df['lookback_ms'].unique()):
        subset = df[df['lookback_ms'] == lookback]
        if len(subset) > 0:
            print(f"\nLookback {lookback}ms:")
            print(f"  Configs: {len(subset)}")
            print(f"  Avg Premature Stop-Out Rate: {subset['sl_correct_dir_pct'].mean():.1f}%")
            print(f"  Avg $/hr: ${subset['hourly_rate'].mean():.2f}")


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Stop-out analysis for top configs")
    parser.add_argument("--top", type=int, default=50, help="Number of top configs to analyze")
    parser.add_argument("--output", type=str, default="research/stop_out_analysis_results.csv",
                        help="Output CSV file")
    args = parser.parse_args()

    # Run analysis
    df = run_top_n_analysis(args.top)

    # Print results
    print_results(df)

    # Save to CSV
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = Path("/Users/rananjaybika/polymarket-amm-bot") / args.output

    df.to_csv(output_path, index=False)
    print(f"\n\nResults saved to: {output_path}")

    # Final recommendation
    print("\n" + "=" * 100)
    print("RECOMMENDATION")
    print("=" * 100)

    # Find best config balancing $/hr and low premature stop-out rate
    significant = df[df['stop_losses'] >= 5].copy()
    if len(significant) > 0:
        # Score = hourly_rate * (1 - premature_rate/100)
        significant['score'] = significant['hourly_rate'] * (1 - significant['sl_correct_dir_pct'] / 100)
        best = significant.nlargest(1, 'score').iloc[0]

        print(f"\nBest balanced config (high $/hr + low premature stop-out rate):")
        print(f"  Rank: #{best['config_rank']}")
        print(f"  Method: {best['method']}")
        print(f"  Z-Score Method: {best['zscore_method']}")
        print(f"  Lookback: {best['lookback_ms']}ms")
        print(f"  Stop-Loss: {best['stop_loss']:.0%}")
        print(f"  Cycling: {'ON' if best['cycling'] else 'OFF'}")
        print(f"  Z-Zone: {best['z_zone']}")
        print(f"  $/hr: ${best['hourly_rate']:.2f}")
        print(f"  Premature Stop-Out Rate: {best['sl_correct_dir_pct']:.1f}%")
        print(f"  Win Rate: {best['win_rate']:.1f}%")


if __name__ == "__main__":
    main()
