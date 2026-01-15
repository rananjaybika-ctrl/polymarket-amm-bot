#!/usr/bin/env python3
"""
Pure MM Offset Optimization - Find optimal passive bid offset
"""

import pandas as pd
import numpy as np
from pathlib import Path

SHARES_PER_SIDE = 10
MIN_TIME_REMAINING = 120


def test_offset(df, complete, offset):
    """Test a specific offset."""
    total_pnl = 0
    hedged_count = 0
    pair_costs = []

    for slug in complete:
        mdf = df[df['market_slug'] == slug].copy()

        entry_row = None
        entry_idx = None
        for i, (idx, row) in enumerate(mdf.iterrows()):
            if row['time_remaining_secs'] >= MIN_TIME_REMAINING:
                entry_idx = i
                entry_row = row
                break

        if entry_row is None:
            continue

        up_bid = round(entry_row['up_bid'] + offset, 2)
        down_bid = round(entry_row['down_bid'] + offset, 2)
        up_bid = max(0.01, min(0.95, up_bid))
        down_bid = max(0.01, min(0.95, down_bid))

        post_entry = mdf.iloc[entry_idx:]
        up_min_ask = post_entry['up_ask'].min()
        down_min_ask = post_entry['down_ask'].min()

        up_filled = up_min_ask <= up_bid
        down_filled = down_min_ask <= down_bid

        if up_filled and down_filled:
            pair_cost = up_bid + down_bid
            pnl = (1.0 - pair_cost) * SHARES_PER_SIDE
            total_pnl += pnl
            hedged_count += 1
            pair_costs.append(pair_cost)

    hedge_rate = hedged_count / len(complete) if complete else 0
    avg_pair_cost = np.mean(pair_costs) if pair_costs else 0

    return {
        'offset': offset,
        'hedged': hedged_count,
        'hedge_rate': hedge_rate,
        'avg_pair_cost': avg_pair_cost,
        'total_pnl': total_pnl,
        'pnl_per_market': total_pnl / len(complete) if complete else 0,
    }


def main():
    print("="*80)
    print("PURE MM OFFSET OPTIMIZATION")
    print("="*80)

    # Load all data
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    # Use largest file for optimization
    main_file = '/Users/rananjaybika/polymarket-amm-bot/research/observer/spread_capture_obs_20260115.csv'

    print(f"\nUsing: {Path(main_file).name}")

    df = pd.read_csv(main_file, on_bad_lines='skip')
    markets = df['market_slug'].unique()
    complete = [s for s in markets
                if df[df['market_slug']==s].iloc[0]['time_remaining_secs'] >= 800
                and df[df['market_slug']==s].iloc[-1]['time_remaining_secs'] <= 60]

    print(f"Complete markets: {len(complete)}")

    # Test different offsets
    offsets = [-0.05, -0.04, -0.03, -0.02, -0.01, 0.00, +0.01]

    print(f"\n{'Offset':>8} {'Hedged':>8} {'Rate':>8} {'AvgCost':>10} {'TotalPnL':>10} {'PnL/Mkt':>10}")
    print("-" * 60)

    results = []
    for offset in offsets:
        r = test_offset(df, complete, offset)
        results.append(r)
        print(f"{r['offset']:+8.2f} {r['hedged']:>8} {100*r['hedge_rate']:>7.1f}% "
              f"${r['avg_pair_cost']:>8.4f} ${r['total_pnl']:>9.2f} ${r['pnl_per_market']:>9.2f}")

    # Best result
    best = max(results, key=lambda x: x['total_pnl'])
    print(f"\n{'='*60}")
    print(f"BEST OFFSET: {best['offset']:+.2f}")
    print(f"  Hedge rate: {100*best['hedge_rate']:.1f}%")
    print(f"  Avg pair cost: ${best['avg_pair_cost']:.4f}")
    print(f"  Total PnL: ${best['total_pnl']:.2f}")
    print(f"  PnL per market: ${best['pnl_per_market']:.2f}")

    # Hourly estimate
    hours = len(complete) * 15 / 60
    print(f"\n  Estimated hourly: ${best['total_pnl']/hours:.2f}/hr")


if __name__ == "__main__":
    main()
