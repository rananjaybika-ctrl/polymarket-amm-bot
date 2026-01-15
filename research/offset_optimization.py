#!/usr/bin/env python3
"""
Offset Optimization: Find the profitable offset combination

Variables to test:
- Winner offset: -0.03 to +0.02
- Loser offset: -0.08 to 0.00

Goal: Maximize PnL by finding the right balance between:
1. Fill rate (higher bids = more fills)
2. Pair cost (lower bids = cheaper pairs)
"""

import pandas as pd
import numpy as np
from itertools import product

MIN_VELOCITY_BPS = 0.30
SHARES = 15


def simulate_with_offsets(df, complete_markets, winner_offset, loser_offset):
    """Run backtest with given offsets."""

    total_hedged_pnl = 0
    total_unhedged_pnl = 0
    both_filled = 0
    winner_only = 0
    pair_costs = []

    for slug in complete_markets:
        mdf = df[df['market_slug'] == slug].copy()

        # Find entry
        entry_row = None
        entry_idx = None
        for i, (idx, row) in enumerate(mdf.iterrows()):
            if abs(row['velocity_bps']) >= MIN_VELOCITY_BPS:
                entry_idx = i
                entry_row = row
                break

        if entry_row is None:
            continue

        vel = entry_row['velocity_bps']
        winner = "UP" if vel > 0 else "DOWN"
        loser = "DOWN" if vel > 0 else "UP"

        # Get prices
        if winner == "UP":
            winner_best_bid = entry_row['up_bid']
            loser_best_bid = entry_row['down_bid']
            winner_entry_ask = entry_row['up_ask']
            loser_entry_ask = entry_row['down_ask']
        else:
            winner_best_bid = entry_row['down_bid']
            loser_best_bid = entry_row['up_bid']
            winner_entry_ask = entry_row['down_ask']
            loser_entry_ask = entry_row['up_ask']

        # Calculate bid prices
        winner_bid = round(winner_best_bid + winner_offset, 2)
        loser_bid = round(loser_best_bid + loser_offset, 2)
        winner_bid = max(0.01, min(0.95, winner_bid))
        loser_bid = max(0.01, min(0.95, loser_bid))

        # Get post-entry asks
        post_entry = mdf.iloc[entry_idx:]
        if winner == "UP":
            winner_asks = post_entry['up_ask'].values
            loser_asks = post_entry['down_ask'].values
        else:
            winner_asks = post_entry['down_ask'].values
            loser_asks = post_entry['up_ask'].values

        winner_min_ask = np.min(winner_asks)
        loser_min_ask = np.min(loser_asks)

        # Fill logic
        winner_filled = False
        loser_filled = False
        winner_fill_price = 0
        loser_fill_price = 0

        # Winner fill
        if winner_bid >= winner_entry_ask:
            winner_filled = True
            winner_fill_price = winner_entry_ask
        elif winner_min_ask <= winner_bid:
            winner_filled = True
            winner_fill_price = winner_bid

        # Loser fill
        if loser_bid >= loser_entry_ask:
            loser_filled = True
            loser_fill_price = loser_entry_ask
        elif loser_min_ask <= loser_bid:
            loser_filled = True
            loser_fill_price = loser_bid

        # Resolution
        final = mdf.iloc[-1]
        if final['up_bid'] >= 0.90:
            resolution = 'UP'
        elif final['down_bid'] >= 0.90:
            resolution = 'DOWN'
        else:
            resolution = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'

        # PnL
        if winner_filled and loser_filled:
            pair_cost = winner_fill_price + loser_fill_price
            pair_costs.append(pair_cost)
            total_hedged_pnl += (1.0 - pair_cost) * SHARES
            both_filled += 1
        elif winner_filled:
            winner_only += 1
            if winner == resolution:
                total_unhedged_pnl += (1.0 - winner_fill_price) * SHARES
            else:
                total_unhedged_pnl += (0.0 - winner_fill_price) * SHARES
        elif loser_filled:
            if loser == resolution:
                total_unhedged_pnl += (1.0 - loser_fill_price) * SHARES
            else:
                total_unhedged_pnl += (0.0 - loser_fill_price) * SHARES

    total_pnl = total_hedged_pnl + total_unhedged_pnl
    avg_pair_cost = np.mean(pair_costs) if pair_costs else 0

    return {
        'winner_offset': winner_offset,
        'loser_offset': loser_offset,
        'both_filled': both_filled,
        'winner_only': winner_only,
        'hedge_rate': both_filled / len(complete_markets) if complete_markets else 0,
        'avg_pair_cost': avg_pair_cost,
        'hedged_pnl': total_hedged_pnl,
        'unhedged_pnl': total_unhedged_pnl,
        'total_pnl': total_pnl,
    }


def main():
    print("=" * 80)
    print("OFFSET OPTIMIZATION")
    print("=" * 80)

    df = pd.read_csv('/Users/rananjaybika/polymarket-amm-bot/research/observer/spread_capture_obs_20260115.csv',
                     on_bad_lines='skip')

    markets = df['market_slug'].unique()
    complete = [s for s in markets
                if df[df['market_slug']==s].iloc[0]['time_remaining_secs'] >= 800
                and df[df['market_slug']==s].iloc[-1]['time_remaining_secs'] <= 60]

    print(f"Complete markets: {len(complete)}")

    # Test different offset combinations
    winner_offsets = [-0.03, -0.02, -0.01, 0.00, +0.01, +0.02]
    loser_offsets = [-0.08, -0.07, -0.06, -0.05, -0.04, -0.03, -0.02, -0.01, 0.00]

    results = []

    print("\nTesting offset combinations...")
    for w_off, l_off in product(winner_offsets, loser_offsets):
        r = simulate_with_offsets(df, complete, w_off, l_off)
        results.append(r)

    # Sort by PnL
    results.sort(key=lambda x: x['total_pnl'], reverse=True)

    print("\n" + "=" * 80)
    print("TOP 20 OFFSET COMBINATIONS BY PnL")
    print("=" * 80)
    print(f"\n{'Win_Off':>8} {'Los_Off':>8} {'Hedge%':>8} {'AvgCost':>8} {'Hedged$':>9} {'Unhedg$':>9} {'Total$':>9}")
    print("-" * 70)

    for r in results[:20]:
        print(f"{r['winner_offset']:+8.2f} {r['loser_offset']:+8.2f} "
              f"{100*r['hedge_rate']:7.1f}% ${r['avg_pair_cost']:7.4f} "
              f"${r['hedged_pnl']:8.2f} ${r['unhedged_pnl']:8.2f} ${r['total_pnl']:8.2f}")

    print("\n" + "=" * 80)
    print("BOTTOM 10 (WORST)")
    print("=" * 80)
    for r in results[-10:]:
        print(f"{r['winner_offset']:+8.2f} {r['loser_offset']:+8.2f} "
              f"{100*r['hedge_rate']:7.1f}% ${r['avg_pair_cost']:7.4f} "
              f"${r['hedged_pnl']:8.2f} ${r['unhedged_pnl']:8.2f} ${r['total_pnl']:8.2f}")

    # Analysis
    print("\n" + "=" * 80)
    print("KEY INSIGHTS")
    print("=" * 80)

    best = results[0]
    print(f"\nBest combination:")
    print(f"  Winner offset: {best['winner_offset']:+.2f}")
    print(f"  Loser offset:  {best['loser_offset']:+.2f}")
    print(f"  Hedge rate:    {100*best['hedge_rate']:.1f}%")
    print(f"  Avg pair cost: ${best['avg_pair_cost']:.4f}")
    print(f"  Total PnL:     ${best['total_pnl']:.2f}")

    # Find breakeven point
    print("\n--- BREAKEVEN ANALYSIS ---")
    profitable = [r for r in results if r['total_pnl'] > 0]
    breakeven = [r for r in results if -5 < r['total_pnl'] < 5]

    print(f"Profitable combinations: {len(profitable)}/{len(results)}")
    if profitable:
        print(f"Min pair cost for profit: ${min(r['avg_pair_cost'] for r in profitable):.4f}")
        print(f"Max pair cost for profit: ${max(r['avg_pair_cost'] for r in profitable):.4f}")

    # Trade-off analysis
    print("\n--- HEDGE RATE vs PAIR COST TRADE-OFF ---")
    print("\nAt different loser offsets (with winner_offset = +0.01):")
    for l_off in loser_offsets:
        r = next((x for x in results if x['winner_offset'] == 0.01 and x['loser_offset'] == l_off), None)
        if r:
            print(f"  Loser {l_off:+.2f}: {100*r['hedge_rate']:5.1f}% hedge, "
                  f"${r['avg_pair_cost']:.3f} cost, ${r['total_pnl']:+7.2f} PnL")


if __name__ == "__main__":
    main()
