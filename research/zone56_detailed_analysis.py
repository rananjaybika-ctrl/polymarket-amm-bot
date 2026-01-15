#!/usr/bin/env python3
"""
Detailed Zone 5-6 Analysis

Best config: Zone 5-6 (vel >= 0.50) + 10% stop-loss = +$41.30

Let's analyze:
1. What's happening in the 3 unhedged cases?
2. Can we do even better with different loser offsets?
3. Full breakdown of profitable vs losing trades
"""

import pandas as pd
import numpy as np
from pathlib import Path
from itertools import product

SHARES = 15  # Target: 15 shares/side (scale to 30 after live validation)
WINNER_OFFSET = +0.01
MIN_TIME = 120
MIN_VELOCITY = 0.50  # Zone 5-6


def simulate_market_detailed(mdf, loser_offset, stop_loss_pct):
    """Detailed simulation with full output."""

    entry_row = None
    entry_idx = None
    for i, (idx, row) in enumerate(mdf.iterrows()):
        if row['time_remaining_secs'] >= MIN_TIME:
            if abs(row['velocity_bps']) >= MIN_VELOCITY:
                entry_idx = i
                entry_row = row
                break

    if entry_row is None:
        return None

    velocity = entry_row['velocity_bps']
    predicted_winner = "UP" if velocity > 0 else "DOWN"

    if predicted_winner == "UP":
        winner_bid = round(entry_row['up_bid'] + WINNER_OFFSET, 2)
        loser_bid = round(entry_row['down_bid'] + loser_offset, 2)
    else:
        winner_bid = round(entry_row['down_bid'] + WINNER_OFFSET, 2)
        loser_bid = round(entry_row['up_bid'] + loser_offset, 2)

    winner_bid = max(0.01, min(0.95, winner_bid))
    loser_bid = max(0.01, min(0.95, loser_bid))

    post_entry = mdf.iloc[entry_idx:]

    winner_filled = False
    winner_fill_price = 0
    loser_filled = False
    loser_fill_price = 0
    loser_fill_type = "none"

    for i, (idx, row) in enumerate(post_entry.iterrows()):
        if predicted_winner == "UP":
            winner_ask = row['up_ask']
            winner_bid_book = row['up_bid']
            loser_ask = row['down_ask']
        else:
            winner_ask = row['down_ask']
            winner_bid_book = row['down_bid']
            loser_ask = row['up_ask']

        if not winner_filled:
            if winner_bid >= winner_ask:
                winner_filled = True
                winner_fill_price = winner_ask
            elif winner_ask <= winner_bid:
                winner_filled = True
                winner_fill_price = winner_bid

        if not loser_filled and loser_ask <= loser_bid:
            loser_filled = True
            loser_fill_price = loser_bid
            loser_fill_type = "passive"

        if winner_filled and not loser_filled:
            if winner_fill_price > 0:
                drop_pct = (winner_fill_price - winner_bid_book) / winner_fill_price
                if drop_pct >= stop_loss_pct:
                    loser_filled = True
                    loser_fill_price = loser_ask
                    loser_fill_type = "stoploss"

    final = mdf.iloc[-1]
    if final['up_bid'] >= 0.90:
        resolution = 'UP'
    elif final['down_bid'] >= 0.90:
        resolution = 'DOWN'
    else:
        resolution = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'

    velocity_correct = (predicted_winner == resolution)

    if winner_filled and loser_filled:
        pair_cost = winner_fill_price + loser_fill_price
        pnl = (1.0 - pair_cost) * SHARES
        return {
            'type': loser_fill_type,
            'pnl': pnl,
            'pair_cost': pair_cost,
            'velocity': velocity,
            'velocity_correct': velocity_correct,
            'winner_fill': winner_fill_price,
            'loser_fill': loser_fill_price,
        }
    elif winner_filled:
        if predicted_winner == resolution:
            pnl = (1.0 - winner_fill_price) * SHARES
        else:
            pnl = (0.0 - winner_fill_price) * SHARES
        return {
            'type': 'unhedged',
            'pnl': pnl,
            'pair_cost': 0,
            'velocity': velocity,
            'velocity_correct': velocity_correct,
            'winner_fill': winner_fill_price,
            'loser_fill': 0,
        }

    return None


def main():
    print("="*90)
    print("DETAILED ZONE 5-6 ANALYSIS")
    print("="*90)

    # Load data
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    all_data = []
    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath, on_bad_lines='skip')
            if df.empty:
                continue
            markets = df['market_slug'].unique()
            for slug in markets:
                mdf = df[df['market_slug'] == slug]
                if len(mdf) >= 2:
                    first = mdf.iloc[0]['time_remaining_secs']
                    last = mdf.iloc[-1]['time_remaining_secs']
                    if first >= 800 and last <= 60:
                        all_data.append(mdf.copy())
        except:
            continue

    print(f"\nTotal complete markets: {len(all_data)}")

    # Optimize loser offset for Zone 5-6
    print("\n" + "="*90)
    print("LOSER OFFSET OPTIMIZATION (Zone 5-6, 10% stop-loss)")
    print("="*90)

    loser_offsets = [-0.04, -0.06, -0.08, -0.10, -0.12, -0.15]

    print(f"\n{'L_Off':>7} {'Tot':>5} {'Pass':>5} {'SL':>5} {'Unh':>5} "
          f"{'P_PnL':>8} {'SL_PnL':>8} {'U_PnL':>8} {'Total':>8} {'$/hr':>7}")
    print("-" * 85)

    best_config = None
    best_pnl = float('-inf')

    for l_off in loser_offsets:
        results = []
        for mdf in all_data:
            r = simulate_market_detailed(mdf, l_off, 0.10)
            if r:
                results.append(r)

        if not results:
            continue

        passive = [r for r in results if r['type'] == 'passive']
        stoploss = [r for r in results if r['type'] == 'stoploss']
        unhedged = [r for r in results if r['type'] == 'unhedged']

        p_pnl = sum(r['pnl'] for r in passive)
        s_pnl = sum(r['pnl'] for r in stoploss)
        u_pnl = sum(r['pnl'] for r in unhedged)
        total = p_pnl + s_pnl + u_pnl

        hours = len(results) * 15 / 60
        hourly = total / hours if hours > 0 else 0

        print(f"{l_off:>+7.2f} {len(results):>5} {len(passive):>5} {len(stoploss):>5} {len(unhedged):>5} "
              f"${p_pnl:>6.2f} ${s_pnl:>6.2f} ${u_pnl:>6.2f} ${total:>6.2f} ${hourly:>5.2f}")

        if total > best_pnl:
            best_pnl = total
            best_config = {'l_off': l_off, 'total': total, 'hourly': hourly,
                          'passive': len(passive), 'stoploss': len(stoploss), 'unhedged': len(unhedged),
                          'results': results}

    # Best config details
    print(f"\n{'='*85}")
    print(f"BEST: Loser offset {best_config['l_off']:+.2f}")
    print(f"  Total PnL: ${best_config['total']:.2f}")
    print(f"  Hourly: ${best_config['hourly']:.2f}/hr")

    # Analyze pair costs
    results = best_config['results']
    passive = [r for r in results if r['type'] == 'passive']
    stoploss = [r for r in results if r['type'] == 'stoploss']

    print(f"\n{'='*85}")
    print("PAIR COST ANALYSIS")
    print("="*85)

    if passive:
        passive_costs = [r['pair_cost'] for r in passive]
        print(f"\n  Passive hedges ({len(passive)}):")
        print(f"    Avg pair cost: ${np.mean(passive_costs):.4f}")
        print(f"    Min: ${np.min(passive_costs):.4f}")
        print(f"    Max: ${np.max(passive_costs):.4f}")
        print(f"    Profit per trade: ${np.mean([r['pnl'] for r in passive]):.2f}")

    if stoploss:
        sl_costs = [r['pair_cost'] for r in stoploss]
        print(f"\n  Stop-loss hedges ({len(stoploss)}):")
        print(f"    Avg pair cost: ${np.mean(sl_costs):.4f}")
        print(f"    Min: ${np.min(sl_costs):.4f}")
        print(f"    Max: ${np.max(sl_costs):.4f}")
        print(f"    Loss per trade: ${np.mean([r['pnl'] for r in stoploss]):.2f}")

    # Now test combined with 5% stop-loss
    print(f"\n{'='*85}")
    print("5% STOP-LOSS TEST (Zone 5-6, best loser offset)")
    print("="*85)

    for sl_pct in [0.05, 0.07, 0.10, 0.12, 0.15]:
        results = []
        for mdf in all_data:
            r = simulate_market_detailed(mdf, best_config['l_off'], sl_pct)
            if r:
                results.append(r)

        if not results:
            continue

        passive = [r for r in results if r['type'] == 'passive']
        stoploss = [r for r in results if r['type'] == 'stoploss']
        unhedged = [r for r in results if r['type'] == 'unhedged']

        total = sum(r['pnl'] for r in results)
        hours = len(results) * 15 / 60

        sl_cost_avg = np.mean([r['pair_cost'] for r in stoploss]) if stoploss else 0

        print(f"\n  Stop-Loss {sl_pct*100:.0f}%:")
        print(f"    Passive: {len(passive)}, SL: {len(stoploss)}, Unh: {len(unhedged)}")
        print(f"    SL avg pair cost: ${sl_cost_avg:.4f}")
        print(f"    Total PnL: ${total:.2f}")
        print(f"    Hourly: ${total/hours:.2f}/hr")


if __name__ == "__main__":
    main()
