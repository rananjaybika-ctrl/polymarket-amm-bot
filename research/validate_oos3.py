#!/usr/bin/env python3
"""
OOS3 Validation: Run Three Configs on Out-of-Sample Data (Jan 22-23, 2026)

Tests AGGRESSIVE, BALANCED, and CONSERVATIVE configs on fresh OOS3 data
that was NOT used in the grid search optimization.

Usage:
    python research/validate_oos3.py
"""

import sys
from pathlib import Path
from typing import Dict, Optional, List
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.volatility_filter_analysis import (
    load_ou_params, compute_zscore_series, BacktestConfig, run_backtest_with_zscore,
    TradeWithZScore, estimate_active_hours_zone
)
from research.TRADING_CONFIGS import AGGRESSIVE, BALANCED, CONSERVATIVE, ALL_CONFIGS, TradingConfig

# OOS3 data paths
BASE_DIR = Path("/Users/rananjaybika/polymarket-amm-bot")
OOS3_BTC_FILE = BASE_DIR / "research/binance_hf/btc_prices_oos3_combined.csv"
OOS3_OBS_FILE = BASE_DIR / "research/observer/grid_obs_oos3_combined.csv"
OOS3_RES_FILE = BASE_DIR / "research/observer/market_resolutions_verified.csv"


def load_oos3_btc() -> pd.DataFrame:
    """Load OOS3 BTC price data."""
    print(f"  Loading OOS3 BTC: {OOS3_BTC_FILE.name}")
    btc_df = pd.read_csv(OOS3_BTC_FILE)
    btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
    print(f"  BTC rows: {len(btc_df):,}")
    return btc_df


def load_oos3_observer():
    """Load OOS3 observer data and resolutions."""
    print(f"  Loading OOS3 observer: {OOS3_OBS_FILE.name}")
    obs_df = pd.read_csv(OOS3_OBS_FILE, on_bad_lines='skip', low_memory=False)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    print(f"  Observer rows: {len(obs_df):,}")

    # Load resolutions
    print(f"  Loading resolutions: {OOS3_RES_FILE.name}")
    res_df = pd.read_csv(OOS3_RES_FILE)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Filter to only markets in OOS3 observer data
    oos3_slugs = set(obs_df['market_slug'].unique())
    res_map_filtered = {k: v for k, v in res_map.items() if k in oos3_slugs}
    resolved = {k: v for k, v in res_map_filtered.items() if v in ('UP', 'DOWN')}
    pending = {k: v for k, v in res_map_filtered.items() if v not in ('UP', 'DOWN')}

    print(f"  OOS3 markets: {len(oos3_slugs)}")
    print(f"  Resolved: {len(resolved)}, Pending: {len(pending)}")

    return obs_df, res_map

def analyze_trades(trades: List[TradeWithZScore], z_lo, z_hi) -> Optional[Dict]:
    """Analyze trades filtered to z-zone."""
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

    all_stops = stoploss + timestop
    stop_correct = [t for t in all_stops if t.correct_direction]

    total_pnl = sum(t.pnl for t in filtered)
    wins = sum(1 for t in filtered if t.pnl > 0)

    return {
        'trades': len(filtered),
        'pnl': total_pnl,
        'win_rate': wins / len(filtered) * 100 if filtered else 0,
        'passive': len(passive),
        'stoploss': len(stoploss),
        'timestop': len(timestop),
        'resolution': len(resolution),
        'premature_stops': len(stop_correct),
        'premature_pnl': sum(t.pnl for t in stop_correct),
    }


def run_config(cfg: TradingConfig, btc_df, obs_df, zscore_df, res_map, ou_params, total_hours) -> Optional[Dict]:
    """Run a single config on OOS3 data."""
    print(f"\n{'='*80}")
    print(f"  CONFIG: {cfg.name}")
    print(f"  Threshold: {cfg.threshold_method} | Z-Score: {cfg.zscore_method}")
    print(f"  Lookback: {cfg.lookback_ms}ms ({cfg.lookback_ticks} ticks)")
    stop_label = f"180s TIME" if cfg.time_stop_seconds else f"{int(cfg.stop_loss_pct*100)}% PRICE"
    print(f"  Stop: {stop_label} | Cycling: {'ON' if cfg.use_cycling else 'OFF'}")
    print(f"  Z-Zone: {cfg.z_zone_label}")
    print(f"{'='*80}")

    backtest_cfg = BacktestConfig(
        target_shares=5,
        spike_lookback=cfg.lookback_ticks,
        stop_loss_pct=cfg.stop_loss_pct,
        time_stop_seconds=cfg.time_stop_seconds,
        use_cycling=cfg.use_cycling,
    )

    trades = run_backtest_with_zscore(
        backtest_cfg, btc_df, obs_df, zscore_df, res_map,
        method=cfg.threshold_method,
        ou_params=ou_params,
        quiet=True
    )

    stats = analyze_trades(trades, cfg.z_lo, cfg.z_hi)
    if not stats:
        print(f"  NO TRADES in z-zone!")
        return None

    hours_active = estimate_active_hours_zone(total_hours, zscore_df, cfg.z_lo, cfg.z_hi)
    hourly_rate = stats['pnl'] / hours_active if hours_active > 0 else 0

    # Print results
    print(f"\n  RESULTS (OOS3):")
    print(f"    Trades:    {stats['trades']}")
    print(f"    PnL @5sh:  ${stats['pnl']:.2f}")
    print(f"    PnL @50sh: ${stats['pnl']*10:.2f}")
    print(f"    $/hr @5sh: ${hourly_rate:.4f}")
    print(f"    $/hr @50sh:${hourly_rate*10:.2f}")
    print(f"    Win Rate:  {stats['win_rate']:.1f}%")
    print(f"    Hours Active: {hours_active:.2f}")

    print(f"\n  EXIT BREAKDOWN:")
    print(f"    Passive:    {stats['passive']} ({stats['passive']/stats['trades']*100:.1f}%)")
    if stats['stoploss']:
        print(f"    Price Stop: {stats['stoploss']} ({stats['stoploss']/stats['trades']*100:.1f}%)")
    if stats['timestop']:
        print(f"    Time Stop:  {stats['timestop']} ({stats['timestop']/stats['trades']*100:.1f}%)")
    print(f"    Resolution: {stats['resolution']} ({stats['resolution']/stats['trades']*100:.1f}%)")

    premature_pct = stats['premature_stops'] / (stats['stoploss'] + stats['timestop']) * 100 if (stats['stoploss'] + stats['timestop']) > 0 else 0
    print(f"\n  PREMATURE STOPS:")
    print(f"    Premature: {stats['premature_stops']} ({premature_pct:.1f}%)")
    print(f"    PnL lost:  ${stats['premature_pnl']:.2f}")

    # Compare to in-sample expectations
    pnl_vs_expected = ((stats['pnl'] / cfg.expected_pnl) - 1) * 100 if cfg.expected_pnl else 0
    rate_vs_expected = ((hourly_rate / cfg.expected_hourly_rate) - 1) * 100 if cfg.expected_hourly_rate else 0

    print(f"\n  vs IN-SAMPLE (per hour normalization):")
    print(f"    $/hr OOS3: ${hourly_rate:.4f} vs IS: ${cfg.expected_hourly_rate:.4f} ({rate_vs_expected:+.1f}%)")
    print(f"    WR OOS3:   {stats['win_rate']:.1f}% vs IS: {cfg.expected_win_rate:.1f}%")

    return {
        'config': cfg.name,
        'stop_type': stop_label,
        'trades': stats['trades'],
        'pnl_5sh': stats['pnl'],
        'pnl_50sh': stats['pnl'] * 10,
        'hourly_5sh': hourly_rate,
        'hourly_50sh': hourly_rate * 10,
        'win_rate': stats['win_rate'],
        'hours_active': hours_active,
        'passive': stats['passive'],
        'stoploss': stats['stoploss'],
        'timestop': stats['timestop'],
        'resolution': stats['resolution'],
        'premature_pct': premature_pct,
        'premature_pnl': stats['premature_pnl'],
        'is_hourly': cfg.expected_hourly_rate,
        'is_win_rate': cfg.expected_win_rate,
        'rate_vs_is_pct': rate_vs_expected,
    }


def main():
    print("=" * 100)
    print("OOS3 VALIDATION - THREE CONFIGS ON FRESH DATA (Jan 22-23, 2026)")
    print("=" * 100)
    print("\nThis data was NOT used in the grid search. True out-of-sample test.")

    # Load OOS3 data
    print("\n" + "-" * 60)
    print("LOADING OOS3 DATA")
    print("-" * 60)

    ou_params = load_ou_params()
    print(f"  OU params: mu={ou_params.mu:.4f}, theta={ou_params.theta:.4f}")

    btc_df = load_oos3_btc()
    obs_df, res_map = load_oos3_observer()

    # Dataset stats
    btc_start = btc_df['timestamp_ms'].min()
    btc_end = btc_df['timestamp_ms'].max()
    total_hours = (btc_end - btc_start) / 3600000
    print(f"\n  OOS3 Dataset: {total_hours:.2f} hours")
    print(f"  Time range: {pd.Timestamp(btc_start, unit='ms')} to {pd.Timestamp(btc_end, unit='ms')}")

    # Compute z-scores for both methods
    print("\n" + "-" * 60)
    print("COMPUTING Z-SCORES")
    print("-" * 60)
    zscore_cache = {}
    for method in ['ewma', 'ou']:
        print(f"  Computing {method} z-scores...")
        zscore_cache[method] = compute_zscore_series(btc_df, ou_params, zscore_method=method)

    # Run all three configs
    print("\n" + "-" * 60)
    print("RUNNING BACKTESTS")
    print("-" * 60)

    results = []
    for cfg in ALL_CONFIGS:
        zscore_df = zscore_cache[cfg.zscore_method]
        result = run_config(cfg, btc_df, obs_df, zscore_df, res_map, ou_params, total_hours)
        if result:
            results.append(result)

    # Final summary
    print("\n" + "=" * 100)
    print("OOS3 VALIDATION SUMMARY")
    print("=" * 100)

    if not results:
        print("  NO RESULTS - all configs had zero trades!")
        return

    print(f"\n{'Config':<14} {'Stop':<10} {'Trades':<7} {'PnL@50':<10} {'$/hr@50':<10} {'WR%':<7} {'vs IS $/hr':<12}")
    print("-" * 80)
    for r in results:
        print(f"{r['config']:<14} {r['stop_type']:<10} {r['trades']:<7} "
              f"${r['pnl_50sh']:<9.2f} ${r['hourly_50sh']:<9.2f} "
              f"{r['win_rate']:<6.1f}% {r['rate_vs_is_pct']:+.1f}%")

    # Verdict
    print("\n" + "=" * 100)
    print("VERDICT")
    print("=" * 100)
    for r in results:
        degradation = r['rate_vs_is_pct']
        if degradation >= -10:
            verdict = "PASS - minimal degradation"
        elif degradation >= -25:
            verdict = "MARGINAL - some degradation"
        elif degradation >= -50:
            verdict = "CAUTION - significant degradation"
        else:
            verdict = "FAIL - severe degradation"
        print(f"  {r['config']:<14}: {verdict} ({r['rate_vs_is_pct']:+.1f}% $/hr)")

    # Save results
    df = pd.DataFrame(results)
    output_path = BASE_DIR / "research/oos3_validation_results.csv"
    df.to_csv(output_path, index=False)
    print(f"\nResults saved to: {output_path}")


if __name__ == "__main__":
    main()
