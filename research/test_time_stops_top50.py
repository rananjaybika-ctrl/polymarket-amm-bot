#!/usr/bin/env python3
"""
Test 120s and 180s Time-Stops on Top 50 Configs

Tests each of the top 50 configs from grid search with:
- 120s time-stop (only exits if NOT in profit)
- 180s time-stop (only exits if NOT in profit)

Compares against original price-stop results (pulled from CSV, not re-run).
"""

import sys
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional
import pandas as pd
import numpy as np
from tqdm import tqdm

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


def analyze_trades(trades: List[TradeWithZScore], z_lo, z_hi) -> Dict:
    """Analyze trade outcomes for a z-zone."""
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

    passive = [t for t in filtered if t.hedge_type == "passive"]
    stoploss = [t for t in filtered if t.hedge_type == "stoploss"]
    timestop = [t for t in filtered if t.hedge_type == "timestop"]
    resolution = [t for t in filtered if t.hedge_type == "resolution"]

    total_pnl = sum(t.pnl for t in filtered)
    wins = sum(1 for t in filtered if t.pnl > 0)
    correct_dir = sum(1 for t in filtered if t.correct_direction)

    # Time-stop analysis
    ts_correct = len([t for t in timestop if t.correct_direction])
    ts_wrong = len([t for t in timestop if not t.correct_direction])
    ts_correct_pnl = sum(t.pnl for t in timestop if t.correct_direction)

    # Stop-loss analysis (for comparison with original)
    sl_correct = len([t for t in stoploss if t.correct_direction])
    sl_wrong = len([t for t in stoploss if not t.correct_direction])
    sl_correct_pnl = sum(t.pnl for t in stoploss if t.correct_direction)

    return {
        'trades': len(filtered),
        'pnl': total_pnl,
        'win_rate': wins / len(filtered) * 100 if filtered else 0,
        'dir_acc': correct_dir / len(filtered) * 100 if filtered else 0,
        'passive': len(passive),
        'timestop': len(timestop),
        'stoploss': len(stoploss),
        'resolution': len(resolution),
        'ts_correct': ts_correct,
        'ts_wrong': ts_wrong,
        'ts_correct_pct': ts_correct / len(timestop) * 100 if timestop else 0,
        'ts_correct_pnl': ts_correct_pnl,
        'sl_correct': sl_correct,
        'sl_wrong': sl_wrong,
        'sl_correct_pct': sl_correct / len(stoploss) * 100 if stoploss else 0,
        'sl_correct_pnl': sl_correct_pnl,
    }


def main():
    print("="*100)
    print("TIME-STOP TEST: 120s vs 180s on TOP 50 CONFIGS")
    print("="*100)

    # Load grid search results
    results_path = Path("/Users/rananjaybika/polymarket-amm-bot/research/vol_filter_grid_results_all_combined.csv")
    grid_df = pd.read_csv(results_path)
    grid_df = grid_df.sort_values('hourly_rate', ascending=False)
    top50 = grid_df.head(50)

    print(f"\nLoaded {len(grid_df)} total configs, testing top 50")

    # Load data
    print("\nLoading data...", flush=True)
    ou_params = load_ou_params()
    btc_df = load_btc_data()
    obs_df, res_map = load_observer_data()

    # Calculate total hours
    btc_start = btc_df['timestamp_ms'].min()
    btc_end = btc_df['timestamp_ms'].max()
    total_hours = (btc_end - btc_start) / 3600000
    print(f"Dataset: {total_hours:.2f} hours")

    # Pre-compute z-score dataframes for each method
    print("\nPre-computing z-score series...", flush=True)
    zscore_cache = {}
    for method in tqdm(['ou', 'ewma', 'percentile', 'ewma_ratio'], desc="Z-score methods"):
        zscore_cache[method] = compute_zscore_series(btc_df, ou_params, zscore_method=method)

    # Cache for backtests
    backtest_cache = {}

    results = []

    print(f"\nTesting top 50 configs with 120s and 180s time-stops...", flush=True)
    pbar = tqdm(top50.iterrows(), total=len(top50), desc="Configs")

    for idx, row in pbar:
        rank = list(top50.index).index(idx) + 1

        method = row['method']
        zscore_method = row['zscore_method']
        lookback = int(row['lookback_ticks'])
        original_sl = row['stop_loss']
        cycling = bool(row['cycling'])
        z_lo, z_hi = parse_z_zone(row['z_zone_lo'], row['z_zone_hi'])
        z_zone_label = row['z_zone_label']
        original_hourly = row['hourly_rate']

        pbar.set_description(f"#{rank} {method}/{zscore_method}/{lookback}t")

        zscore_df = zscore_cache[zscore_method]

        # Add baseline results from CSV (no need to re-run)
        results.append({
            'rank': rank,
            'method': method,
            'zscore_method': zscore_method,
            'lookback_ms': int(lookback * 1000 / 60),
            'cycling': cycling,
            'z_zone': z_zone_label,
            'stop_type': f"{int(row['stop_loss']*100)}% price (baseline)",
            'trades': int(row['trades']),
            'pnl': row['total_pnl'],
            'hourly_rate': row['hourly_rate'],
            'win_rate': row['win_rate'],
            'dir_acc': row['direction_acc'],
            'passive': None,  # Not available from CSV
            'timestop': None,
            'stoploss': None,
            'resolution': None,
            'premature_pct': None,
            'premature_pnl': None,
        })

        # Test 2 time-stop configurations (skip price-stop - already have from CSV)
        stop_configs = [
            ('120s time', None, 120),
            ('180s time', None, 180),
        ]

        for stop_label, sl_pct, time_stop in stop_configs:
            cache_key = (method, zscore_method, lookback, sl_pct, time_stop, cycling)

            if cache_key not in backtest_cache:
                config = BacktestConfig(
                    target_shares=5,
                    spike_lookback=lookback,
                    stop_loss_pct=sl_pct,
                    use_cycling=cycling,
                    time_stop_seconds=time_stop,
                )

                trades = run_backtest_with_zscore(
                    config, btc_df, obs_df, zscore_df, res_map,
                    method=method,
                    ou_params=ou_params,
                    quiet=True
                )
                backtest_cache[cache_key] = trades
            else:
                trades = backtest_cache[cache_key]

            stats = analyze_trades(trades, z_lo, z_hi)
            if not stats:
                continue

            # Compute hourly rate
            from research.volatility_filter_analysis import estimate_active_hours_zone
            hours_active = estimate_active_hours_zone(total_hours, zscore_df, z_lo, z_hi)
            hourly_rate = stats['pnl'] / hours_active if hours_active > 0 else 0

            results.append({
                'rank': rank,
                'method': method,
                'zscore_method': zscore_method,
                'lookback_ms': int(lookback * 1000 / 60),
                'cycling': cycling,
                'z_zone': z_zone_label,
                'stop_type': stop_label,
                'trades': stats['trades'],
                'pnl': stats['pnl'],
                'hourly_rate': hourly_rate,
                'win_rate': stats['win_rate'],
                'dir_acc': stats['dir_acc'],
                'passive': stats['passive'],
                'timestop': stats['timestop'],
                'stoploss': stats['stoploss'],
                'resolution': stats['resolution'],
                'premature_pct': stats['ts_correct_pct'] if time_stop else stats['sl_correct_pct'],
                'premature_pnl': stats['ts_correct_pnl'] if time_stop else stats['sl_correct_pnl'],
            })

    # Convert to DataFrame
    df = pd.DataFrame(results)

    # Save results
    output_path = Path("/Users/rananjaybika/polymarket-amm-bot/research/time_stop_top50_results.csv")
    df.to_csv(output_path, index=False)
    print(f"\n\nSaved results to: {output_path}")

    # Print summary
    print("\n" + "="*120)
    print("SUMMARY: COMPARING STOP TYPES ACROSS TOP 50 CONFIGS")
    print("="*120)

    # Group by stop type
    baseline_df = df[df['stop_type'].str.contains('baseline')]
    time120_df = df[df['stop_type'] == '120s time']
    time180_df = df[df['stop_type'] == '180s time']

    print(f"\nPRICE-STOP BASELINE (from grid search CSV):")
    print(f"  Avg PnL: ${baseline_df['pnl'].mean():.2f}")
    print(f"  Avg $/hr: ${baseline_df['hourly_rate'].mean():.3f}")
    print(f"  Avg Win Rate: {baseline_df['win_rate'].mean():.1f}%")

    for stop_type, subset in [('120s time', time120_df), ('180s time', time180_df)]:
        print(f"\n{stop_type.upper()}:")
        print(f"  Avg PnL: ${subset['pnl'].mean():.2f}")
        print(f"  Avg $/hr: ${subset['hourly_rate'].mean():.3f}")
        print(f"  Avg Win Rate: {subset['win_rate'].mean():.1f}%")
        print(f"  Avg Premature Stop %: {subset['premature_pct'].mean():.1f}%")
        print(f"  Avg Premature PnL Lost: ${subset['premature_pnl'].mean():.2f}")

    # Find configs where time-stop beats price-stop
    print("\n" + "="*120)
    print("CONFIGS WHERE 120s TIME-STOP BEATS PRICE-STOP BASELINE")
    print("="*120)

    baseline_indexed = df[df['stop_type'].str.contains('baseline')].set_index('rank')
    time120_indexed = df[df['stop_type'] == '120s time'].set_index('rank')

    winners = []
    for rank in baseline_indexed.index:
        if rank in time120_indexed.index:
            price_pnl = baseline_indexed.loc[rank, 'pnl']
            time_pnl = time120_indexed.loc[rank, 'pnl']
            if time_pnl > price_pnl:
                winners.append({
                    'rank': rank,
                    'config': f"{baseline_indexed.loc[rank, 'method']}/{baseline_indexed.loc[rank, 'zscore_method']}/{baseline_indexed.loc[rank, 'lookback_ms']}ms",
                    'cycling': baseline_indexed.loc[rank, 'cycling'],
                    'baseline_sl': baseline_indexed.loc[rank, 'stop_type'],
                    'price_pnl': price_pnl,
                    'time_pnl': time_pnl,
                    'improvement': time_pnl - price_pnl,
                })

    if winners:
        print(f"\nFound {len(winners)} configs where 120s time-stop has higher PnL:")
        print(f"\n{'Rank':<6} {'Config':<40} {'Cyc':<5} {'Baseline PnL':<14} {'Time PnL':<12} {'Diff':<10}")
        print("-"*95)
        for w in sorted(winners, key=lambda x: -x['improvement'])[:20]:
            print(f"{w['rank']:<6} {w['config']:<40} {'Y' if w['cycling'] else 'N':<5} "
                  f"${w['price_pnl']:<13.2f} ${w['time_pnl']:<11.2f} ${w['improvement']:+.2f}")
    else:
        print("\nNo configs found where 120s time-stop beats price-stop baseline.")

    # Find BEST overall config
    print("\n" + "="*120)
    print("BEST CONFIGS BY PnL (ALL STOP TYPES)")
    print("="*120)

    top_by_pnl = df.nlargest(15, 'pnl')
    print(f"\n{'Rank':<6} {'Config':<35} {'Stop':<12} {'PnL':<10} {'Win%':<8} {'Prem%':<8}")
    print("-"*90)
    for _, r in top_by_pnl.iterrows():
        config_str = f"{r['method']}/{r['zscore_method']}/{r['lookback_ms']}ms"
        print(f"{r['rank']:<6} {config_str:<35} {r['stop_type']:<12} ${r['pnl']:<9.2f} "
              f"{r['win_rate']:<7.1f}% {r['premature_pct']:<7.1f}%")


if __name__ == "__main__":
    main()
