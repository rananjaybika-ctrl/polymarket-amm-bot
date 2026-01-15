#!/usr/bin/env python3
"""
5% Stop-Loss Comparison: Zone 4 Included vs Excluded

Hypothesis: Earlier stop-loss (5%) might catch reversals cheaper.
But does including Zone 4 (lower accuracy) hurt or help?

Scenarios:
1. Zone 4-6 (vel >= 0.30) + 5% stop-loss
2. Zone 5-6 (vel >= 0.50) + 5% stop-loss
3. Compare with current config (Zone 5-6 + 7% stop-loss)
"""

import pandas as pd
import numpy as np
from pathlib import Path
from collections import defaultdict

SHARES = 15
WINNER_OFFSET = +0.01
LOSER_OFFSET = -0.12
MIN_TIME = 120


def simulate_market(mdf, min_velocity, stop_loss_pct):
    """Simulate single market with given velocity threshold and stop-loss."""

    # Find entry meeting velocity threshold
    entry_row = None
    entry_idx = None
    entry_velocity = None

    for i, (idx, row) in enumerate(mdf.iterrows()):
        if row['time_remaining_secs'] >= MIN_TIME:
            if abs(row['velocity_bps']) >= min_velocity:
                entry_idx = i
                entry_row = row
                entry_velocity = row['velocity_bps']
                break

    if entry_row is None:
        return None

    velocity = entry_row['velocity_bps']
    predicted_winner = "UP" if velocity > 0 else "DOWN"

    # Calculate bids
    if predicted_winner == "UP":
        winner_bid = round(entry_row['up_bid'] + WINNER_OFFSET, 2)
        loser_bid = round(entry_row['down_bid'] + LOSER_OFFSET, 2)
    else:
        winner_bid = round(entry_row['down_bid'] + WINNER_OFFSET, 2)
        loser_bid = round(entry_row['up_bid'] + LOSER_OFFSET, 2)

    winner_bid = max(0.01, min(0.95, winner_bid))
    loser_bid = max(0.01, min(0.95, loser_bid))

    # Simulate fills
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

        # Winner fill
        if not winner_filled:
            if winner_bid >= winner_ask:
                winner_filled = True
                winner_fill_price = winner_ask
            elif winner_ask <= winner_bid:
                winner_filled = True
                winner_fill_price = winner_bid

        # Passive loser fill
        if not loser_filled and loser_ask <= loser_bid:
            loser_filled = True
            loser_fill_price = loser_bid
            loser_fill_type = "passive"

        # Stop-loss check
        if winner_filled and not loser_filled:
            if winner_fill_price > 0:
                drop_pct = (winner_fill_price - winner_bid_book) / winner_fill_price
                if drop_pct >= stop_loss_pct:
                    loser_filled = True
                    loser_fill_price = loser_ask
                    loser_fill_type = "stoploss"

    # Resolution
    final = mdf.iloc[-1]
    if final['up_bid'] >= 0.90:
        resolution = 'UP'
    elif final['down_bid'] >= 0.90:
        resolution = 'DOWN'
    else:
        resolution = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'

    velocity_correct = (predicted_winner == resolution)

    # PnL
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
            'abs_velocity': abs(velocity),
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
            'abs_velocity': abs(velocity),
        }

    return None


def analyze_scenario(market_data, min_velocity, stop_loss_pct, name):
    """Analyze a single scenario."""

    results = []
    for mdf, slug in market_data:
        r = simulate_market(mdf, min_velocity, stop_loss_pct)
        if r:
            r['slug'] = slug
            results.append(r)

    if not results:
        return None

    # Breakdown
    passive = [r for r in results if r['type'] == 'passive']
    stoploss = [r for r in results if r['type'] == 'stoploss']
    unhedged = [r for r in results if r['type'] == 'unhedged']

    # PnL
    total_pnl = sum(r['pnl'] for r in results)
    passive_pnl = sum(r['pnl'] for r in passive)
    stoploss_pnl = sum(r['pnl'] for r in stoploss)
    unhedged_pnl = sum(r['pnl'] for r in unhedged)

    # Win rate
    winners = [r for r in results if r['pnl'] > 0]
    win_rate = len(winners) / len(results) * 100

    # Velocity accuracy
    vel_correct = sum(1 for r in results if r['velocity_correct'])
    vel_accuracy = vel_correct / len(results) * 100

    # Pair costs
    passive_costs = [r['pair_cost'] for r in passive] if passive else [0]
    stoploss_costs = [r['pair_cost'] for r in stoploss] if stoploss else [0]

    # Hours (each trade ~ 15 min market)
    hours = len(results) * 15 / 60
    hourly = total_pnl / hours if hours > 0 else 0

    return {
        'name': name,
        'min_velocity': min_velocity,
        'stop_loss_pct': stop_loss_pct,
        'trades': len(results),
        'passive': len(passive),
        'stoploss': len(stoploss),
        'unhedged': len(unhedged),
        'total_pnl': total_pnl,
        'passive_pnl': passive_pnl,
        'stoploss_pnl': stoploss_pnl,
        'unhedged_pnl': unhedged_pnl,
        'win_rate': win_rate,
        'vel_accuracy': vel_accuracy,
        'passive_avg_cost': np.mean(passive_costs),
        'stoploss_avg_cost': np.mean(stoploss_costs),
        'hourly': hourly,
        'avg_pnl_per_trade': total_pnl / len(results),
    }


def main():
    print("=" * 80)
    print("5% STOP-LOSS COMPARISON: Zone 4 Included vs Excluded")
    print("=" * 80)

    # Load ALL observer data
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    print(f"\nLoading data from {len(csv_files)} files...")

    market_data = []

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
                        market_data.append((mdf.copy(), slug))
        except Exception as e:
            continue

    print(f"Complete markets: {len(market_data)}")

    # Define scenarios to test
    scenarios = [
        # (min_velocity, stop_loss_pct, name)
        (0.30, 0.05, "Zone 4-6 + 5% SL"),
        (0.50, 0.05, "Zone 5-6 + 5% SL"),
        (0.30, 0.07, "Zone 4-6 + 7% SL"),
        (0.50, 0.07, "Zone 5-6 + 7% SL (CURRENT)"),
        (0.30, 0.10, "Zone 4-6 + 10% SL"),
        (0.50, 0.10, "Zone 5-6 + 10% SL"),
    ]

    results = []
    for min_vel, sl_pct, name in scenarios:
        r = analyze_scenario(market_data, min_vel, sl_pct, name)
        if r:
            results.append(r)

    # Print comparison table
    print(f"\n{'='*80}")
    print("SCENARIO COMPARISON")
    print("=" * 80)

    print(f"\n{'Scenario':<30} {'Trades':>7} {'Win%':>6} {'VelAcc':>7} {'PnL':>10} {'$/hr':>8}")
    print("-" * 80)

    for r in results:
        print(f"{r['name']:<30} {r['trades']:>7} {r['win_rate']:>5.0f}% {r['vel_accuracy']:>6.1f}% ${r['total_pnl']:>8.2f} ${r['hourly']:>6.2f}")

    # Detailed breakdown for key scenarios
    print(f"\n{'='*80}")
    print("DETAILED BREAKDOWN")
    print("=" * 80)

    for r in results:
        if r['stop_loss_pct'] == 0.05:  # Focus on 5% scenarios
            print(f"\n  {r['name']}:")
            print(f"    Trades: {r['trades']}")
            print(f"    Velocity Accuracy: {r['vel_accuracy']:.1f}%")
            print(f"    Win Rate: {r['win_rate']:.0f}%")
            print(f"\n    Passive hedges: {r['passive']} → ${r['passive_pnl']:.2f} (avg cost: ${r['passive_avg_cost']:.4f})")
            print(f"    Stop-loss hedges: {r['stoploss']} → ${r['stoploss_pnl']:.2f} (avg cost: ${r['stoploss_avg_cost']:.4f})")
            print(f"    Unhedged: {r['unhedged']} → ${r['unhedged_pnl']:.2f}")
            print(f"\n    TOTAL: ${r['total_pnl']:.2f} (${r['hourly']:.2f}/hr)")
            print(f"    Avg per trade: ${r['avg_pnl_per_trade']:.2f}")

    # Analysis: Why win rate is low
    print(f"\n{'='*80}")
    print("WHY WIN RATE IS ~44%")
    print("=" * 80)

    # Get detailed results for current config
    current = None
    for r in results:
        if r['name'] == "Zone 5-6 + 7% SL (CURRENT)":
            current = r
            break

    if current:
        print(f"""
  Win rate = {current['win_rate']:.0f}% means {100-current['win_rate']:.0f}% of trades have negative PnL.

  BUT the strategy is still profitable because:

  1. Winners are BIGGER than losers:
     - Passive hedge profit: avg ${current['passive_pnl']/current['passive']:.2f} per trade
     - Stop-loss hedge loss: avg ${current['stoploss_pnl']/current['stoploss']:.2f} per trade

  2. Passive hedges (41%) make more than stop-loss (56%) loses:
     - Passive total: ${current['passive_pnl']:.2f}
     - Stop-loss total: ${current['stoploss_pnl']:.2f}
     - Net: ${current['passive_pnl'] + current['stoploss_pnl']:.2f}

  3. Low unhedged count (3%) = limited downside risk
""")

    # Recommendation
    print(f"\n{'='*80}")
    print("RECOMMENDATION")
    print("=" * 80)

    # Find best 5% scenario
    best_5pct = max([r for r in results if r['stop_loss_pct'] == 0.05], key=lambda x: x['total_pnl'])
    best_overall = max(results, key=lambda x: x['total_pnl'])

    print(f"""
  Best 5% stop-loss config:
    {best_5pct['name']}: ${best_5pct['total_pnl']:.2f} (${best_5pct['hourly']:.2f}/hr)

  Best overall config:
    {best_overall['name']}: ${best_overall['total_pnl']:.2f} (${best_overall['hourly']:.2f}/hr)
""")

    # Zone 4 analysis
    z46_5 = next((r for r in results if r['name'] == "Zone 4-6 + 5% SL"), None)
    z56_5 = next((r for r in results if r['name'] == "Zone 5-6 + 5% SL"), None)

    if z46_5 and z56_5:
        print(f"""
  Zone 4 Impact (5% SL):
    Including Zone 4: {z46_5['trades']} trades, ${z46_5['total_pnl']:.2f}, {z46_5['vel_accuracy']:.1f}% accuracy
    Excluding Zone 4: {z56_5['trades']} trades, ${z56_5['total_pnl']:.2f}, {z56_5['vel_accuracy']:.1f}% accuracy

    Zone 4 adds {z46_5['trades'] - z56_5['trades']} trades but:
    - Lower accuracy ({z46_5['vel_accuracy']:.1f}% vs {z56_5['vel_accuracy']:.1f}%)
    - PnL difference: ${z46_5['total_pnl'] - z56_5['total_pnl']:.2f}
""")


if __name__ == "__main__":
    main()
