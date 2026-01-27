#!/usr/bin/env python3
"""
FAST Time-Stop Test - Top 20 Configs Only

Optimized version that pre-computes spikes to avoid redundant calculations.
"""

import sys
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
import pandas as pd
import numpy as np
from tqdm import tqdm
from concurrent.futures import ProcessPoolExecutor
import multiprocessing as mp

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.volatility_filter_analysis import (
    load_ou_params, load_btc_data, load_observer_data,
    compute_zscore_series, BacktestConfig,
    detect_spikes, simulate_trade_outcome, TradeWithZScore,
    calculate_maker_fee, calculate_taker_fee
)


def parse_z_zone(z_zone_lo, z_zone_hi):
    """Parse z-zone bounds from CSV values."""
    z_lo = None if z_zone_lo == -999 else z_zone_lo
    z_hi = None if z_zone_hi == 999 else z_zone_hi
    return z_lo, z_hi


def run_backtest_with_precomputed_spikes(
    config: BacktestConfig,
    spikes_df: pd.DataFrame,  # Pre-computed spikes
    obs_df: pd.DataFrame,
    zscore_df: pd.DataFrame,
    res_map: Dict[str, str],
) -> List[TradeWithZScore]:
    """Run backtest using pre-computed spikes."""
    trades = []

    obs_df = obs_df.copy()
    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    markets = obs_df['market_slug'].unique()
    active_positions = {}  # market -> entry_ts for cycling control

    for market in markets:
        market_obs = obs_df[obs_df['market_slug'] == market].sort_values('timestamp_ms')
        if market_obs.empty:
            continue

        resolution = market_obs['resolution'].iloc[0]
        market_start = market_obs['timestamp_ms'].min()
        market_end = market_obs['timestamp_ms'].max()

        # Filter spikes to this market's time window
        market_spikes = spikes_df[
            (spikes_df['timestamp_ms'] >= market_start) &
            (spikes_df['timestamp_ms'] <= market_end)
        ]

        for _, spike in market_spikes.iterrows():
            spike_ts = spike['timestamp_ms']

            # Cycling control
            if market in active_positions:
                if not config.use_cycling:
                    continue

            # Find nearest observer row
            obs_at_spike = market_obs[market_obs['timestamp_ms'] <= spike_ts]
            if obs_at_spike.empty:
                continue
            obs_row = obs_at_spike.iloc[-1]

            # Get z-score at entry
            zscore_at_entry = None
            zscore_rows = zscore_df[zscore_df['timestamp_ms'] <= spike_ts]
            if not zscore_rows.empty:
                zscore_at_entry = zscore_rows.iloc[-1]['zscore']

            # Determine winner/loser based on spike direction
            spike_direction = spike['direction']
            if spike_direction == 'up':
                winner_side = 'yes' if resolution == 'UP' else 'no'
            else:
                winner_side = 'no' if resolution == 'UP' else 'yes'

            correct_direction = (
                (spike_direction == 'up' and resolution == 'UP') or
                (spike_direction == 'down' and resolution == 'DOWN')
            )

            # Get prices
            if winner_side == 'yes':
                winner_ask = obs_row['yes_ask']
                loser_bid = obs_row['no_bid']
            else:
                winner_ask = obs_row['no_ask']
                loser_bid = obs_row['yes_bid']

            if pd.isna(winner_ask) or pd.isna(loser_bid):
                continue
            if winner_ask <= 0 or winner_ask >= 1:
                continue
            if loser_bid <= 0 or loser_bid >= 1:
                continue

            # Entry fees
            entry_fee = calculate_taker_fee(winner_ask, config.target_shares)

            # Simulate trade outcome
            result = simulate_trade_outcome(
                config,
                market_obs,
                spike_ts,
                winner_side,
                winner_ask,
                loser_bid,
                resolution,
            )

            if result is None:
                continue

            hedge_type, exit_ts, loser_fill, hedge_fee = result

            # Calculate PnL
            if hedge_type == "resolution":
                # Held to resolution
                pnl = (1.0 - winner_ask) * config.target_shares - entry_fee
            else:
                # Hedged (passive, stoploss, or timestop)
                spread_captured = (1.0 - winner_ask - loser_fill) * config.target_shares
                pnl = spread_captured - entry_fee - hedge_fee

            trade = TradeWithZScore(
                market_slug=market,
                entry_ts=spike_ts,
                exit_ts=exit_ts,
                winner_side=winner_side,
                winner_fill_price=winner_ask,
                loser_fill_price=loser_fill,
                hedge_type=hedge_type,
                pnl=pnl,
                spread_at_entry=1.0 - winner_ask - loser_bid,
                resolution=resolution,
                correct_direction=correct_direction,
                zscore_at_entry=zscore_at_entry,
                entry_time_remaining=(market_end - spike_ts) / 1000,
            )
            trades.append(trade)

            # Track position
            active_positions[market] = spike_ts

    return trades


def analyze_trades(trades: List[TradeWithZScore], z_lo, z_hi) -> Dict:
    """Analyze trade outcomes for a z-zone."""
    filtered = []
    for t in trades:
        z = t.zscore_at_entry
        if z is None:
            continue
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

    ts_correct = len([t for t in timestop if t.correct_direction])
    sl_correct = len([t for t in stoploss if t.correct_direction])

    return {
        'trades': len(filtered),
        'pnl': total_pnl,
        'win_rate': wins / len(filtered) * 100 if filtered else 0,
        'dir_acc': correct_dir / len(filtered) * 100 if filtered else 0,
        'passive': len(passive),
        'timestop': len(timestop),
        'stoploss': len(stoploss),
        'resolution': len(resolution),
        'ts_correct_pct': ts_correct / len(timestop) * 100 if timestop else 0,
        'sl_correct_pct': sl_correct / len(stoploss) * 100 if stoploss else 0,
    }


def main():
    print("="*100)
    print("FAST TIME-STOP TEST: 120s vs 180s on TOP 20 CONFIGS")
    print("="*100)

    # Load grid search results
    results_path = Path("/Users/rananjaybika/polymarket-amm-bot/research/vol_filter_grid_results_all_combined.csv")
    grid_df = pd.read_csv(results_path)
    grid_df = grid_df.sort_values('hourly_rate', ascending=False)
    top_n = 20  # Test top 20 for speed
    top_configs = grid_df.head(top_n)

    print(f"\nLoaded {len(grid_df)} total configs, testing top {top_n}")

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

    # Pre-compute z-score dataframes
    print("\nPre-computing z-score series...", flush=True)
    zscore_cache = {}
    for method in tqdm(['ou', 'ewma', 'percentile', 'ewma_ratio'], desc="Z-score methods"):
        zscore_cache[method] = compute_zscore_series(btc_df, ou_params, zscore_method=method)

    # Pre-compute spikes for each unique (method, lookback) combination
    print("\nPre-computing spikes for unique configs...", flush=True)
    unique_spike_configs = top_configs[['method', 'lookback_ticks']].drop_duplicates()
    spike_cache = {}

    for _, row in tqdm(unique_spike_configs.iterrows(), total=len(unique_spike_configs), desc="Spike detection"):
        method = row['method']
        lookback = int(row['lookback_ticks'])
        cache_key = (method, lookback)

        if cache_key not in spike_cache:
            spike_cache[cache_key] = detect_spikes(
                btc_df, lookback,
                method=method,
                ou_params=ou_params
            )

    results = []

    print(f"\nTesting {top_n} configs with 3 stop types...", flush=True)
    pbar = tqdm(top_configs.iterrows(), total=len(top_configs), desc="Configs")

    for idx, row in pbar:
        rank = list(top_configs.index).index(idx) + 1

        method = row['method']
        zscore_method = row['zscore_method']
        lookback = int(row['lookback_ticks'])
        cycling = bool(row['cycling'])
        z_lo, z_hi = parse_z_zone(row['z_zone_lo'], row['z_zone_hi'])
        z_zone_label = row['z_zone_label']

        pbar.set_description(f"#{rank} {method}/{zscore_method}/{lookback}t")

        zscore_df = zscore_cache[zscore_method]
        spikes_df = spike_cache[(method, lookback)]

        # Test 3 stop configurations
        stop_configs = [
            ('15% price', 0.15, None),
            ('120s time', None, 120),
            ('180s time', None, 180),
        ]

        for stop_label, sl_pct, time_stop in stop_configs:
            config = BacktestConfig(
                target_shares=5,
                spike_lookback=lookback,
                stop_loss_pct=sl_pct,
                use_cycling=cycling,
                time_stop_seconds=time_stop,
            )

            trades = run_backtest_with_precomputed_spikes(
                config, spikes_df, obs_df, zscore_df, res_map
            )

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
                'lookback_ticks': lookback,
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
            })

    # Convert to DataFrame
    df = pd.DataFrame(results)

    # Save results
    output_path = Path("/Users/rananjaybika/polymarket-amm-bot/research/time_stop_top20_results.csv")
    df.to_csv(output_path, index=False)
    print(f"\n\nSaved results to: {output_path}")

    # Print summary
    print("\n" + "="*120)
    print("SUMMARY: COMPARING STOP TYPES ACROSS TOP 20 CONFIGS")
    print("="*120)

    for stop_type in ['15% price', '120s time', '180s time']:
        subset = df[df['stop_type'] == stop_type]
        print(f"\n{stop_type.upper()}:")
        print(f"  Avg PnL: ${subset['pnl'].mean():.2f}")
        print(f"  Avg $/hr: ${subset['hourly_rate'].mean():.3f}")
        print(f"  Avg Win Rate: {subset['win_rate'].mean():.1f}%")
        print(f"  Avg Premature %: {subset['premature_pct'].mean():.1f}%")

    # Compare each config
    print("\n" + "="*120)
    print("PER-CONFIG COMPARISON: 15% Price vs 120s Time vs 180s Time")
    print("="*120)

    price_df = df[df['stop_type'] == '15% price'].set_index('rank')
    time120_df = df[df['stop_type'] == '120s time'].set_index('rank')
    time180_df = df[df['stop_type'] == '180s time'].set_index('rank')

    print(f"\n{'Rank':<5} {'Config':<30} {'15%':<10} {'120s':<10} {'180s':<10} {'Best':<12}")
    print("-"*90)

    for rank in sorted(price_df.index):
        if rank not in time120_df.index:
            continue

        config_str = f"{price_df.loc[rank, 'method']}/{price_df.loc[rank, 'zscore_method']}"
        price_pnl = price_df.loc[rank, 'pnl']
        time120_pnl = time120_df.loc[rank, 'pnl'] if rank in time120_df.index else 0
        time180_pnl = time180_df.loc[rank, 'pnl'] if rank in time180_df.index else 0

        best = "15% price"
        best_pnl = price_pnl
        if time120_pnl > best_pnl:
            best = "120s time"
            best_pnl = time120_pnl
        if time180_pnl > best_pnl:
            best = "180s time"

        print(f"#{rank:<4} {config_str:<30} ${price_pnl:<9.2f} ${time120_pnl:<9.2f} ${time180_pnl:<9.2f} {best:<12}")

    # Count winners
    print("\n" + "="*80)
    print("WINNER COUNT")
    print("="*80)

    price_wins = 0
    time120_wins = 0
    time180_wins = 0

    for rank in price_df.index:
        if rank not in time120_df.index:
            continue
        price_pnl = price_df.loc[rank, 'pnl']
        time120_pnl = time120_df.loc[rank, 'pnl']
        time180_pnl = time180_df.loc[rank, 'pnl'] if rank in time180_df.index else 0

        if price_pnl >= time120_pnl and price_pnl >= time180_pnl:
            price_wins += 1
        elif time120_pnl >= price_pnl and time120_pnl >= time180_pnl:
            time120_wins += 1
        else:
            time180_wins += 1

    print(f"\n15% Price-Stop wins: {price_wins}/{len(price_df)} configs")
    print(f"120s Time-Stop wins: {time120_wins}/{len(price_df)} configs")
    print(f"180s Time-Stop wins: {time180_wins}/{len(price_df)} configs")


if __name__ == "__main__":
    main()
