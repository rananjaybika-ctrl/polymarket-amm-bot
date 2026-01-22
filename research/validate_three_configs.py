#!/usr/bin/env python3
"""
Validate Three Trading Configs on Full Dataset

Runs AGGRESSIVE, BALANCED, and CONSERVATIVE configs with their
CORRECT stop settings (time-stop vs price-stop) on the full dataset.

This validates:
1. Stop-out mechanics work correctly
2. Time-stop vs price-stop behavior matches expectations
3. Cycling effect with different stop types
4. PnL matches expected values from grid search

Author: Claude Code
Date: January 22, 2026
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
    TradeWithZScore, estimate_active_hours_zone
)
from research.TRADING_CONFIGS import AGGRESSIVE, BALANCED, CONSERVATIVE, ALL_CONFIGS, TradingConfig


def analyze_trades_detailed(trades: List[TradeWithZScore], z_lo, z_hi) -> Optional[Dict]:
    """Detailed trade analysis for a z-zone."""
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
    timestop = [t for t in filtered if t.hedge_type == "timestop"]
    resolution = [t for t in filtered if t.hedge_type == "resolution"]

    # Stop analysis (combine stoploss and timestop)
    all_stops = stoploss + timestop
    stop_correct = [t for t in all_stops if t.correct_direction]
    stop_wrong = [t for t in all_stops if not t.correct_direction]
    stop_correct_pnl = sum(t.pnl for t in stop_correct)
    stop_wrong_pnl = sum(t.pnl for t in stop_wrong)

    # Overall metrics
    total_pnl = sum(t.pnl for t in filtered)
    wins = sum(1 for t in filtered if t.pnl > 0)
    correct_dir = sum(1 for t in filtered if t.correct_direction)

    # Fill time analysis
    fill_times = []
    for t in passive:
        if t.exit_ts and t.entry_ts:
            fill_times.append((t.exit_ts - t.entry_ts) / 1000.0)

    return {
        'trades': len(filtered),
        'pnl': total_pnl,
        'win_rate': wins / len(filtered) * 100 if filtered else 0,
        'dir_acc': correct_dir / len(filtered) * 100 if filtered else 0,
        'passive': len(passive),
        'stoploss': len(stoploss),
        'timestop': len(timestop),
        'resolution': len(resolution),
        'stop_correct': len(stop_correct),
        'stop_wrong': len(stop_wrong),
        'stop_correct_pct': len(stop_correct) / len(all_stops) * 100 if all_stops else 0,
        'stop_correct_pnl': stop_correct_pnl,
        'stop_wrong_pnl': stop_wrong_pnl,
        'fill_time_median': np.median(fill_times) if fill_times else 0,
        'fill_time_p25': np.percentile(fill_times, 25) if fill_times else 0,
        'fill_time_p75': np.percentile(fill_times, 75) if fill_times else 0,
    }


def run_config_validation(
    cfg: TradingConfig,
    btc_df: pd.DataFrame,
    obs_df: pd.DataFrame,
    zscore_df: pd.DataFrame,
    res_map: Dict[str, str],
    ou_params,
    total_hours: float,
) -> Dict:
    """Run validation for a single config."""

    print(f"\n{'='*80}")
    print(f"VALIDATING: {cfg.name}")
    print(f"{'='*80}")
    print(f"  Threshold: {cfg.threshold_method}, Z-Score: {cfg.zscore_method}")
    print(f"  Lookback: {cfg.lookback_ms}ms")
    print(f"  Stop: {'180s TIME' if cfg.time_stop_seconds else f'{int(cfg.stop_loss_pct*100)}% PRICE'}")
    print(f"  Cycling: {'ON' if cfg.use_cycling else 'OFF'}")
    print(f"  Z-Zone: {cfg.z_zone_label}")

    # Create backtest config
    backtest_cfg = BacktestConfig(
        target_shares=5,
        spike_lookback=cfg.lookback_ticks,
        stop_loss_pct=cfg.stop_loss_pct,
        time_stop_seconds=cfg.time_stop_seconds,
        use_cycling=cfg.use_cycling,
    )

    # Run backtest
    trades = run_backtest_with_zscore(
        backtest_cfg, btc_df, obs_df, zscore_df, res_map,
        method=cfg.threshold_method,
        ou_params=ou_params,
        quiet=True
    )

    # Analyze with z-zone filter
    stats = analyze_trades_detailed(trades, cfg.z_lo, cfg.z_hi)

    if not stats:
        print(f"  ERROR: No trades in z-zone!")
        return None

    # Calculate hourly rate
    hours_active = estimate_active_hours_zone(total_hours, zscore_df, cfg.z_lo, cfg.z_hi)
    hourly_rate = stats['pnl'] / hours_active if hours_active > 0 else 0

    # Print results
    print(f"\nRESULTS:")
    print(f"  Trades: {stats['trades']} (expected: {cfg.expected_trades})")
    print(f"  PnL: ${stats['pnl']:.2f} (expected: ${cfg.expected_pnl:.2f})")
    print(f"  $/hr: ${hourly_rate:.4f} (expected: ${cfg.expected_hourly_rate:.4f})")
    print(f"  Win Rate: {stats['win_rate']:.1f}% (expected: {cfg.expected_win_rate:.1f}%)")

    print(f"\nEXIT BREAKDOWN:")
    print(f"  Passive fills:  {stats['passive']} ({stats['passive']/stats['trades']*100:.1f}%)")
    if stats['stoploss'] > 0:
        print(f"  Price stops:    {stats['stoploss']} ({stats['stoploss']/stats['trades']*100:.1f}%)")
    if stats['timestop'] > 0:
        print(f"  Time stops:     {stats['timestop']} ({stats['timestop']/stats['trades']*100:.1f}%)")
    print(f"  Resolution:     {stats['resolution']} ({stats['resolution']/stats['trades']*100:.1f}%)")

    print(f"\nPREMATURE STOP ANALYSIS:")
    print(f"  Total stops: {stats['stoploss'] + stats['timestop']}")
    print(f"  Correct direction (premature): {stats['stop_correct']} ({stats['stop_correct_pct']:.1f}%)")
    print(f"    Expected: {cfg.premature_stop_pct:.1f}%")
    print(f"  PnL lost (premature): ${stats['stop_correct_pnl']:.2f}")
    print(f"    Expected: ${cfg.premature_pnl_lost:.2f}")

    # Compare to expected
    pnl_diff = stats['pnl'] - cfg.expected_pnl
    pnl_diff_pct = (pnl_diff / cfg.expected_pnl * 100) if cfg.expected_pnl != 0 else 0

    print(f"\nVALIDATION:")
    if abs(pnl_diff_pct) < 10:
        print(f"  PnL MATCH: ${stats['pnl']:.2f} vs ${cfg.expected_pnl:.2f} ({pnl_diff_pct:+.1f}%)")
    else:
        print(f"  PnL MISMATCH: ${stats['pnl']:.2f} vs ${cfg.expected_pnl:.2f} ({pnl_diff_pct:+.1f}%)")

    return {
        'config': cfg.name,
        'trades': stats['trades'],
        'pnl': stats['pnl'],
        'hourly_rate': hourly_rate,
        'win_rate': stats['win_rate'],
        'dir_acc': stats['dir_acc'],
        'passive': stats['passive'],
        'stoploss': stats['stoploss'],
        'timestop': stats['timestop'],
        'resolution': stats['resolution'],
        'premature_pct': stats['stop_correct_pct'],
        'premature_pnl': stats['stop_correct_pnl'],
        'expected_pnl': cfg.expected_pnl,
        'pnl_diff_pct': pnl_diff_pct,
    }


def main():
    print("=" * 100)
    print("THREE CONFIG VALIDATION - WITH CORRECT STOP SETTINGS")
    print("=" * 100)
    print("\nKey difference from grid search:")
    print("  - AGGRESSIVE uses 180s TIME-STOP (not 15% price-stop)")
    print("  - BALANCED/CONSERVATIVE use 15% PRICE-STOP")

    # Load data
    print("\n" + "=" * 80)
    print("LOADING DATA")
    print("=" * 80)

    ou_params = load_ou_params()
    print(f"  OU params loaded: mu={ou_params.mu:.4f}")

    btc_df = load_btc_data()
    obs_df, res_map = load_observer_data()

    # Calculate total hours
    btc_start = btc_df['timestamp_ms'].min()
    btc_end = btc_df['timestamp_ms'].max()
    total_hours = (btc_end - btc_start) / 3600000
    print(f"\nDataset: {total_hours:.2f} hours, {len(res_map)} markets")

    # Pre-compute z-scores for both methods needed
    print("\nComputing z-scores...")
    zscore_cache = {}
    for method in ['ewma', 'ou']:
        print(f"  {method}...")
        zscore_cache[method] = compute_zscore_series(btc_df, ou_params, zscore_method=method)

    # Run validation for each config
    results = []
    for cfg in ALL_CONFIGS:
        zscore_df = zscore_cache[cfg.zscore_method]
        result = run_config_validation(
            cfg, btc_df, obs_df, zscore_df, res_map, ou_params, total_hours
        )
        if result:
            results.append(result)

    # Summary table
    print("\n" + "=" * 100)
    print("SUMMARY: ALL THREE CONFIGS")
    print("=" * 100)

    print(f"\n{'Config':<15} {'Stop':<12} {'Trades':<8} {'PnL':<10} {'$/hr':<8} {'Win%':<8} {'Prem%':<8} {'vs Expected':<12}")
    print("-" * 95)

    for r in results:
        cfg = get_config(r['config'])
        stop_type = "180s TIME" if cfg.time_stop_seconds else "15% PRICE"
        print(f"{r['config']:<15} {stop_type:<12} {r['trades']:<8} ${r['pnl']:<9.2f} ${r['hourly_rate']:<7.3f} "
              f"{r['win_rate']:<7.1f}% {r['premature_pct']:<7.1f}% {r['pnl_diff_pct']:+.1f}%")

    # Scale to 50 shares
    print("\n" + "=" * 100)
    print("SCALED TO 50 SHARES")
    print("=" * 100)

    print(f"\n{'Config':<15} {'PnL @50sh':<12} {'$/hr @50sh':<12}")
    print("-" * 45)

    for r in results:
        print(f"{r['config']:<15} ${r['pnl']*10:<11.2f} ${r['hourly_rate']*10:<11.2f}")

    # Save results
    df = pd.DataFrame(results)
    output_path = Path("/Users/rananjaybika/polymarket-amm-bot/research/three_config_validation_results.csv")
    df.to_csv(output_path, index=False)
    print(f"\nResults saved to: {output_path}")


def get_config(name: str) -> TradingConfig:
    """Get config by name."""
    for cfg in ALL_CONFIGS:
        if cfg.name == name:
            return cfg
    raise ValueError(f"Unknown config: {name}")


if __name__ == "__main__":
    main()
