#!/usr/bin/env python3
"""
Pure Market Making Backtest - No Velocity Signal

Strategy:
- Post passive bids on BOTH sides (no winner/loser distinction)
- Wait for BOTH sides to fill before counting as a trade
- Only count hedged positions (no directional risk)
"""

import pandas as pd
import numpy as np
from pathlib import Path

# Configuration
OFFSET = -0.02  # Bid below best_bid
SHARES_PER_SIDE = 10
MIN_TIME_REMAINING = 120  # Don't enter too close to expiry


def backtest_file(filepath: str) -> dict:
    """Run pure MM backtest on a single CSV file."""
    print(f"\n{'='*80}")
    print(f"FILE: {Path(filepath).name}")
    print(f"{'='*80}")

    try:
        df = pd.read_csv(filepath, on_bad_lines='skip')
    except Exception as e:
        print(f"  ERROR: {e}")
        return {}

    if df.empty:
        print("  EMPTY")
        return {}

    markets = df['market_slug'].unique()
    complete = []
    for s in markets:
        mdf = df[df['market_slug'] == s]
        if len(mdf) < 2:
            continue
        if mdf.iloc[0]['time_remaining_secs'] >= 800 and mdf.iloc[-1]['time_remaining_secs'] <= 60:
            complete.append(s)

    print(f"  Complete markets: {len(complete)}")

    if not complete:
        return {}

    total_hedged_pnl = 0
    hedged_count = 0
    no_fill_count = 0
    partial_count = 0
    pair_costs = []

    for slug in complete:
        mdf = df[df['market_slug'] == slug].copy()

        # Entry at first sample with enough time
        entry_row = None
        entry_idx = None
        for i, (idx, row) in enumerate(mdf.iterrows()):
            if row['time_remaining_secs'] >= MIN_TIME_REMAINING:
                entry_idx = i
                entry_row = row
                break

        if entry_row is None:
            continue

        # Post passive bids on BOTH sides
        up_bid = round(entry_row['up_bid'] + OFFSET, 2)
        down_bid = round(entry_row['down_bid'] + OFFSET, 2)
        up_bid = max(0.01, min(0.95, up_bid))
        down_bid = max(0.01, min(0.95, down_bid))

        # Check if bids would fill
        post_entry = mdf.iloc[entry_idx:]
        up_min_ask = post_entry['up_ask'].min()
        down_min_ask = post_entry['down_ask'].min()

        up_filled = up_min_ask <= up_bid
        down_filled = down_min_ask <= down_bid

        if up_filled and down_filled:
            # Both filled - hedged position
            pair_cost = up_bid + down_bid
            pnl = (1.0 - pair_cost) * SHARES_PER_SIDE
            total_hedged_pnl += pnl
            hedged_count += 1
            pair_costs.append(pair_cost)
        elif up_filled or down_filled:
            partial_count += 1
        else:
            no_fill_count += 1

    hedge_rate = hedged_count / len(complete) if complete else 0
    avg_pair_cost = np.mean(pair_costs) if pair_costs else 0

    print(f"  Hedged: {hedged_count}/{len(complete)} ({100*hedge_rate:.1f}%)")
    print(f"  Partial fills: {partial_count}")
    print(f"  No fills: {no_fill_count}")
    print(f"  Avg pair cost: ${avg_pair_cost:.4f}")
    print(f"  Hedged PnL: ${total_hedged_pnl:.2f}")
    print(f"  PnL/market: ${total_hedged_pnl/len(complete):.2f}" if complete else "")

    return {
        'file': Path(filepath).name,
        'complete_markets': len(complete),
        'hedged': hedged_count,
        'hedge_rate': hedge_rate,
        'avg_pair_cost': avg_pair_cost,
        'hedged_pnl': total_hedged_pnl,
    }


def main():
    print("="*80)
    print("PURE MARKET MAKING BACKTEST")
    print("="*80)
    print(f"\nStrategy: Post passive bids at best_bid {OFFSET:+.2f} on BOTH sides")
    print("Only count HEDGED positions (both sides fill)")

    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    print(f"\nFound {len(csv_files)} files")

    all_results = []
    for f in csv_files:
        r = backtest_file(str(f))
        if r:
            all_results.append(r)

    if all_results:
        print("\n" + "="*80)
        print("AGGREGATE")
        print("="*80)

        total_markets = sum(r['complete_markets'] for r in all_results)
        total_hedged = sum(r['hedged'] for r in all_results)
        total_pnl = sum(r['hedged_pnl'] for r in all_results)
        avg_pair_cost = sum(r['avg_pair_cost'] * r['hedged'] for r in all_results) / total_hedged if total_hedged else 0

        print(f"  Total markets: {total_markets}")
        print(f"  Total hedged: {total_hedged} ({100*total_hedged/total_markets:.1f}%)")
        print(f"  Avg pair cost: ${avg_pair_cost:.4f}")
        print(f"  Total PnL: ${total_pnl:.2f}")
        print(f"  Per market: ${total_pnl/total_markets:.2f}")


if __name__ == "__main__":
    main()
