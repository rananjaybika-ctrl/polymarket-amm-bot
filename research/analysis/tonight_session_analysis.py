#!/usr/bin/env python3
"""
Tonight's Session Analysis (6:30pm - 10:00pm IST)

Analyzes ONLY the data from tonight's observer session to validate
the Zone 5-6 + 7% stop-loss strategy in real-time conditions.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timezone

# Tonight's session start: 6:30pm IST = 1:00pm UTC = 1768480200000 ms (approx)
# 6:30pm IST on Jan 15, 2026
SESSION_START_UTC_HOUR = 13  # 1pm UTC = 6:30pm IST
SESSION_START_MS = 1768480200000  # Approximate - will filter by this

SHARES = 15  # Target: 15 shares/side (scale to 30 after live validation)
WINNER_OFFSET = +0.01
LOSER_OFFSET = -0.12  # Profitable config
MIN_TIME = 120
MIN_VELOCITY = 0.50  # Zone 5-6


def simulate_market_with_stoploss(mdf, stop_loss_pct=0.07):
    """Simulate single market with Zone 5-6 + stop-loss."""

    # Find entry meeting velocity threshold
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
    print("=" * 80)
    print("TONIGHT'S SESSION ANALYSIS (6:30pm - 10:00pm IST)")
    print("=" * 80)
    print(f"\nConfiguration: Zone 5-6 (vel >= {MIN_VELOCITY}) + 7% stop-loss + -0.12 loser")

    # Load data
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    # Filter to only today's file
    today_files = [f for f in csv_files if '20260115' in f.name]

    print(f"\nLoading data from {len(today_files)} files...")

    all_data = []
    tonight_data = []

    for filepath in today_files:
        try:
            df = pd.read_csv(filepath, on_bad_lines='skip')
            if df.empty:
                continue

            # Filter to tonight's session (after 6:30pm IST / 1pm UTC)
            # timestamp_ms > SESSION_START_MS
            tonight_df = df[df['timestamp_ms'] >= SESSION_START_MS]

            markets = df['market_slug'].unique()
            tonight_markets = tonight_df['market_slug'].unique() if len(tonight_df) > 0 else []

            for slug in markets:
                mdf = df[df['market_slug'] == slug]
                if len(mdf) >= 2:
                    first = mdf.iloc[0]['time_remaining_secs']
                    last = mdf.iloc[-1]['time_remaining_secs']
                    if first >= 800 and last <= 60:
                        all_data.append(mdf.copy())

                        # Check if this market is from tonight
                        first_ts = mdf.iloc[0]['timestamp_ms']
                        if first_ts >= SESSION_START_MS:
                            tonight_data.append(mdf.copy())
        except Exception as e:
            print(f"Error loading {filepath}: {e}")
            continue

    print(f"\nTotal complete markets (all time): {len(all_data)}")
    print(f"Tonight's complete markets (since 6:30pm IST): {len(tonight_data)}")

    # Analyze tonight's data
    if tonight_data:
        print(f"\n{'='*80}")
        print("TONIGHT'S SESSION RESULTS")
        print("=" * 80)

        for sl_pct in [0.05, 0.07, 0.10]:
            results = []
            for mdf in tonight_data:
                r = simulate_market_with_stoploss(mdf, sl_pct)
                if r:
                    results.append(r)

            if not results:
                print(f"\n  {sl_pct*100:.0f}% Stop-Loss: No Zone 5-6 entries found")
                continue

            passive = [r for r in results if r['type'] == 'passive']
            stoploss = [r for r in results if r['type'] == 'stoploss']
            unhedged = [r for r in results if r['type'] == 'unhedged']

            p_pnl = sum(r['pnl'] for r in passive)
            s_pnl = sum(r['pnl'] for r in stoploss)
            u_pnl = sum(r['pnl'] for r in unhedged)
            total = p_pnl + s_pnl + u_pnl

            vel_correct = sum(1 for r in results if r['velocity_correct'])
            vel_accuracy = vel_correct / len(results) * 100 if results else 0

            hours = len(results) * 15 / 60
            hourly = total / hours if hours > 0 else 0

            print(f"\n  {sl_pct*100:.0f}% Stop-Loss:")
            print(f"    Markets: {len(results)} (Zone 5-6 entries)")
            print(f"    Velocity Accuracy: {vel_accuracy:.1f}%")
            print(f"    Passive hedges: {len(passive)} → ${p_pnl:.2f}")
            print(f"    Stop-loss hedges: {len(stoploss)} → ${s_pnl:.2f}")
            print(f"    Unhedged: {len(unhedged)} → ${u_pnl:.2f}")
            print(f"    TOTAL PnL: ${total:.2f}")
            print(f"    Hourly: ${hourly:.2f}/hr")

            if passive:
                passive_costs = [r['pair_cost'] for r in passive]
                print(f"    Passive avg pair cost: ${np.mean(passive_costs):.4f}")
            if stoploss:
                sl_costs = [r['pair_cost'] for r in stoploss]
                print(f"    Stop-loss avg pair cost: ${np.mean(sl_costs):.4f}")
    else:
        print("\nNo complete markets from tonight's session yet.")
        print("Markets are still in progress or not enough data collected.")

    # Compare with all-time data
    print(f"\n{'='*80}")
    print("COMPARISON: ALL-TIME DATA (Historical)")
    print("=" * 80)

    for sl_pct in [0.07]:  # Just show 7% for comparison
        results = []
        for mdf in all_data:
            r = simulate_market_with_stoploss(mdf, sl_pct)
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

        vel_correct = sum(1 for r in results if r['velocity_correct'])
        vel_accuracy = vel_correct / len(results) * 100 if results else 0

        hours = len(results) * 15 / 60
        hourly = total / hours if hours > 0 else 0

        print(f"\n  7% Stop-Loss (All-Time Benchmark):")
        print(f"    Markets: {len(results)}")
        print(f"    Velocity Accuracy: {vel_accuracy:.1f}%")
        print(f"    Passive: {len(passive)} → ${p_pnl:.2f}")
        print(f"    Stop-loss: {len(stoploss)} → ${s_pnl:.2f}")
        print(f"    Unhedged: {len(unhedged)} → ${u_pnl:.2f}")
        print(f"    TOTAL: ${total:.2f} (${hourly:.2f}/hr)")


if __name__ == "__main__":
    main()
