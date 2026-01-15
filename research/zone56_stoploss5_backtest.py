#!/usr/bin/env python3
"""
Zone 5-6 Only + 5% Stop-Loss Backtest

Hypothesis: Higher velocity = more accurate signal = fewer stop-loss triggers

Zone Thresholds:
- Zone 4: 0.30-0.50 bps (currently included)
- Zone 5: 0.50-1.00 bps (TRADE)
- Zone 6: 1.00+ bps (TRADE)

Testing:
1. Zone 4-6 (current): vel >= 0.30 bps
2. Zone 5-6 only: vel >= 0.50 bps
3. Zone 6 only: vel >= 1.00 bps

Combined with stop-loss thresholds: 5%, 10%, 15%
"""

import pandas as pd
import numpy as np
from pathlib import Path
from itertools import product

SHARES = 15  # Target: 15 shares/side (scale to 30 after live validation)
WINNER_OFFSET = +0.01
LOSER_OFFSET = -0.08
MIN_TIME = 120


def simulate_market(mdf, min_velocity, stop_loss_pct):
    """Simulate single market with velocity threshold and stop-loss."""

    # Find entry meeting velocity threshold
    entry_row = None
    entry_idx = None
    for i, (idx, row) in enumerate(mdf.iterrows()):
        if row['time_remaining_secs'] >= MIN_TIME:
            if abs(row['velocity_bps']) >= min_velocity:
                entry_idx = i
                entry_row = row
                break

    if entry_row is None:
        return None  # No entry meeting threshold

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

        # Winner fill (aggressive)
        if not winner_filled:
            if winner_bid >= winner_ask:
                winner_filled = True
                winner_fill_price = winner_ask
            elif winner_ask <= winner_bid:
                winner_filled = True
                winner_fill_price = winner_bid

        # Loser fill (passive)
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

    # Velocity correctness (did predicted winner actually win?)
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
        }

    return None


def run_test(all_data, min_velocity, stop_loss_pct):
    """Run test with specific parameters."""
    results = []

    for mdf in all_data:
        result = simulate_market(mdf, min_velocity, stop_loss_pct)
        if result:
            results.append(result)

    if not results:
        return None

    passive = [r for r in results if r['type'] == 'passive']
    stoploss = [r for r in results if r['type'] == 'stoploss']
    unhedged = [r for r in results if r['type'] == 'unhedged']

    # Velocity accuracy for this subset
    vel_correct = sum(1 for r in results if r['velocity_correct'])
    vel_accuracy = vel_correct / len(results) if results else 0

    return {
        'total': len(results),
        'passive': len(passive),
        'stoploss': len(stoploss),
        'unhedged': len(unhedged),
        'passive_pnl': sum(r['pnl'] for r in passive),
        'stoploss_pnl': sum(r['pnl'] for r in stoploss),
        'unhedged_pnl': sum(r['pnl'] for r in unhedged),
        'total_pnl': sum(r['pnl'] for r in results),
        'velocity_accuracy': vel_accuracy,
        'avg_velocity': np.mean([abs(r['velocity']) for r in results]),
    }


def main():
    print("="*90)
    print("ZONE 5-6 ONLY + 5% STOP-LOSS BACKTEST")
    print("="*90)

    print("""
VELOCITY FORMULA:
  velocity_bps = (sum of % price changes over 10s window) / 10 * 100

  Example: BTC moves +$50 (0.05%) in 10 seconds
           velocity = 0.05 / 10 * 100 = 0.50 bps

ZONE THRESHOLDS:
  Zone 4 (very_strong):  0.30-0.50 bps = $30-50 BTC move in 10s
  Zone 5 (extreme):      0.50-1.00 bps = $50-100 BTC move in 10s
  Zone 6 (super_strong): 1.00+ bps     = $100+ BTC move in 10s
""")

    # Load all data
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

    print(f"Total complete markets available: {len(all_data)}")

    # Test configurations
    velocity_thresholds = [
        (0.30, "Zone 4-6 (vel >= 0.30)"),
        (0.50, "Zone 5-6 (vel >= 0.50)"),
        (1.00, "Zone 6 only (vel >= 1.00)"),
    ]

    stop_loss_pcts = [0.05, 0.10, 0.15]

    print(f"\n{'Config':<25} {'SL%':>5} {'Tot':>5} {'Pass':>5} {'SL':>5} {'Unh':>5} "
          f"{'P_PnL':>8} {'SL_PnL':>8} {'Total':>8} {'VelAcc':>7} {'AvgVel':>7}")
    print("-" * 105)

    best_result = None
    best_pnl = float('-inf')

    for (min_vel, zone_name), sl_pct in product(velocity_thresholds, stop_loss_pcts):
        result = run_test(all_data, min_vel, sl_pct)

        if result is None:
            print(f"{zone_name:<25} {sl_pct*100:>4.0f}% {'N/A':>5}")
            continue

        total_pnl = result['total_pnl']

        print(f"{zone_name:<25} {sl_pct*100:>4.0f}% {result['total']:>5} {result['passive']:>5} "
              f"{result['stoploss']:>5} {result['unhedged']:>5} "
              f"${result['passive_pnl']:>6.2f} ${result['stoploss_pnl']:>6.2f} ${total_pnl:>6.2f} "
              f"{result['velocity_accuracy']*100:>6.1f}% {result['avg_velocity']:>6.2f}")

        if total_pnl > best_pnl:
            best_pnl = total_pnl
            best_result = {
                'zone_name': zone_name,
                'min_vel': min_vel,
                'sl_pct': sl_pct,
                **result
            }

    # Best result analysis
    print(f"\n{'='*105}")
    print(f"BEST CONFIGURATION")
    print(f"{'='*105}")

    if best_result:
        hours = best_result['total'] * 15 / 60
        print(f"\n  Zone: {best_result['zone_name']}")
        print(f"  Stop-Loss: {best_result['sl_pct']*100:.0f}%")
        print(f"  Trades: {best_result['total']}")
        print(f"  Velocity Accuracy: {best_result['velocity_accuracy']*100:.1f}%")
        print(f"  Avg Velocity: {best_result['avg_velocity']:.2f} bps")
        print(f"\n  Passive hedges: {best_result['passive']} → ${best_result['passive_pnl']:.2f}")
        print(f"  Stop-loss hedges: {best_result['stoploss']} → ${best_result['stoploss_pnl']:.2f}")
        print(f"  Unhedged: {best_result['unhedged']} → ${best_result['unhedged_pnl']:.2f}")
        print(f"\n  TOTAL PnL: ${best_result['total_pnl']:.2f}")
        print(f"  Hourly: ${best_result['total_pnl']/hours:.2f}/hr" if hours > 0 else "")

    # Compare velocity accuracy across zones
    print(f"\n{'='*105}")
    print(f"VELOCITY ACCURACY BY ZONE (at 5% stop-loss)")
    print(f"{'='*105}")

    for min_vel, zone_name in velocity_thresholds:
        result = run_test(all_data, min_vel, 0.05)
        if result:
            print(f"\n  {zone_name}:")
            print(f"    Trades: {result['total']}")
            print(f"    Velocity → Resolution Accuracy: {result['velocity_accuracy']*100:.1f}%")
            print(f"    Avg velocity: {result['avg_velocity']:.2f} bps")

            # Calculate how many stop-loss triggers as % of total
            sl_pct_of_total = result['stoploss'] / result['total'] * 100 if result['total'] > 0 else 0
            print(f"    Stop-loss triggers: {result['stoploss']}/{result['total']} ({sl_pct_of_total:.1f}%)")


if __name__ == "__main__":
    main()
