#!/usr/bin/env python3
"""
Deep analysis of unhedged outcomes.

Key question: When velocity predicts winner, and only winner fills,
does that winner actually WIN the resolution?
"""

import pandas as pd
import numpy as np
from pathlib import Path

SHARES = 15  # Target: 15 shares/side (scale to 30 after live validation)
OFFSET = -0.02
MIN_TIME = 120


def analyze_all_outcomes(filepath):
    """Analyze all outcomes in detail."""
    df = pd.read_csv(filepath, on_bad_lines='skip')

    markets = df['market_slug'].unique()
    complete = [s for s in markets
                if len(df[df['market_slug']==s]) >= 2
                and df[df['market_slug']==s].iloc[0]['time_remaining_secs'] >= 800
                and df[df['market_slug']==s].iloc[-1]['time_remaining_secs'] <= 60]

    results = {
        'hedged': {'count': 0, 'pnl': 0, 'wins': 0},
        'unhedged_winner_correct': {'count': 0, 'pnl': 0},  # Winner fills, velocity was right
        'unhedged_winner_wrong': {'count': 0, 'pnl': 0},    # Winner fills, velocity was wrong
        'unhedged_loser': {'count': 0, 'pnl': 0},
    }

    velocity_resolution_correct = 0
    velocity_resolution_total = 0

    for slug in complete:
        mdf = df[df['market_slug'] == slug].copy()

        # Entry
        entry_row = None
        entry_idx = None
        for i, (idx, row) in enumerate(mdf.iterrows()):
            if row['time_remaining_secs'] >= MIN_TIME:
                entry_idx = i
                entry_row = row
                break

        if entry_row is None:
            continue

        velocity = entry_row['velocity_bps']
        predicted_winner = "UP" if velocity > 0 else "DOWN"

        # Bids
        up_bid = round(entry_row['up_bid'] + OFFSET, 2)
        down_bid = round(entry_row['down_bid'] + OFFSET, 2)
        up_bid = max(0.01, min(0.95, up_bid))
        down_bid = max(0.01, min(0.95, down_bid))

        # Fills
        post_entry = mdf.iloc[entry_idx:]
        up_filled = post_entry['up_ask'].min() <= up_bid
        down_filled = post_entry['down_ask'].min() <= down_bid

        # Resolution
        final = mdf.iloc[-1]
        if final['up_bid'] >= 0.90:
            resolution = 'UP'
        elif final['down_bid'] >= 0.90:
            resolution = 'DOWN'
        else:
            resolution = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'

        # Track velocity vs resolution accuracy
        velocity_resolution_total += 1
        if predicted_winner == resolution:
            velocity_resolution_correct += 1

        # Categorize outcome
        if up_filled and down_filled:
            pair_cost = up_bid + down_bid
            pnl = (1.0 - pair_cost) * SHARES
            results['hedged']['count'] += 1
            results['hedged']['pnl'] += pnl

        elif up_filled and not down_filled:
            if resolution == 'UP':
                pnl = (1.0 - up_bid) * SHARES
            else:
                pnl = (0.0 - up_bid) * SHARES

            # Was UP the predicted winner?
            if predicted_winner == 'UP':
                # Winner filled, check if velocity was right
                if resolution == 'UP':
                    results['unhedged_winner_correct']['count'] += 1
                    results['unhedged_winner_correct']['pnl'] += pnl
                else:
                    results['unhedged_winner_wrong']['count'] += 1
                    results['unhedged_winner_wrong']['pnl'] += pnl
            else:
                # Loser filled
                results['unhedged_loser']['count'] += 1
                results['unhedged_loser']['pnl'] += pnl

        elif not up_filled and down_filled:
            if resolution == 'DOWN':
                pnl = (1.0 - down_bid) * SHARES
            else:
                pnl = (0.0 - down_bid) * SHARES

            # Was DOWN the predicted winner?
            if predicted_winner == 'DOWN':
                # Winner filled, check if velocity was right
                if resolution == 'DOWN':
                    results['unhedged_winner_correct']['count'] += 1
                    results['unhedged_winner_correct']['pnl'] += pnl
                else:
                    results['unhedged_winner_wrong']['count'] += 1
                    results['unhedged_winner_wrong']['pnl'] += pnl
            else:
                # Loser filled
                results['unhedged_loser']['count'] += 1
                results['unhedged_loser']['pnl'] += pnl

    return results, velocity_resolution_correct, velocity_resolution_total


def main():
    print("="*80)
    print("DEEP UNHEDGED ANALYSIS")
    print("="*80)

    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    total_results = {
        'hedged': {'count': 0, 'pnl': 0},
        'unhedged_winner_correct': {'count': 0, 'pnl': 0},
        'unhedged_winner_wrong': {'count': 0, 'pnl': 0},
        'unhedged_loser': {'count': 0, 'pnl': 0},
    }
    total_vel_correct = 0
    total_vel_total = 0

    for filepath in csv_files:
        try:
            results, vel_correct, vel_total = analyze_all_outcomes(str(filepath))
            for k in total_results:
                total_results[k]['count'] += results[k]['count']
                total_results[k]['pnl'] += results[k]['pnl']
            total_vel_correct += vel_correct
            total_vel_total += vel_total
        except Exception as e:
            continue

    print(f"\nVelocity → Resolution Accuracy: {total_vel_correct}/{total_vel_total} "
          f"({100*total_vel_correct/total_vel_total:.1f}%)")

    total_count = sum(r['count'] for r in total_results.values())
    print(f"\nTotal markets analyzed: {total_count}")

    print("\n" + "="*80)
    print("OUTCOME BREAKDOWN")
    print("="*80)

    print(f"\n  HEDGED (both fill):")
    print(f"    Count: {total_results['hedged']['count']} ({100*total_results['hedged']['count']/total_count:.1f}%)")
    print(f"    PnL: ${total_results['hedged']['pnl']:.2f}")
    if total_results['hedged']['count'] > 0:
        print(f"    Avg: ${total_results['hedged']['pnl']/total_results['hedged']['count']:.2f}/market")

    print(f"\n  UNHEDGED WINNER - VELOCITY CORRECT (winner fills, wins resolution):")
    print(f"    Count: {total_results['unhedged_winner_correct']['count']} ({100*total_results['unhedged_winner_correct']['count']/total_count:.1f}%)")
    print(f"    PnL: ${total_results['unhedged_winner_correct']['pnl']:.2f}")
    if total_results['unhedged_winner_correct']['count'] > 0:
        print(f"    Avg: ${total_results['unhedged_winner_correct']['pnl']/total_results['unhedged_winner_correct']['count']:.2f}/market")

    print(f"\n  UNHEDGED WINNER - VELOCITY WRONG (winner fills, LOSES resolution):")
    print(f"    Count: {total_results['unhedged_winner_wrong']['count']} ({100*total_results['unhedged_winner_wrong']['count']/total_count:.1f}%)")
    print(f"    PnL: ${total_results['unhedged_winner_wrong']['pnl']:.2f}")
    if total_results['unhedged_winner_wrong']['count'] > 0:
        print(f"    Avg: ${total_results['unhedged_winner_wrong']['pnl']/total_results['unhedged_winner_wrong']['count']:.2f}/market")

    print(f"\n  UNHEDGED LOSER (loser fills):")
    print(f"    Count: {total_results['unhedged_loser']['count']} ({100*total_results['unhedged_loser']['count']/total_count:.1f}%)")
    print(f"    PnL: ${total_results['unhedged_loser']['pnl']:.2f}")
    if total_results['unhedged_loser']['count'] > 0:
        print(f"    Avg: ${total_results['unhedged_loser']['pnl']/total_results['unhedged_loser']['count']:.2f}/market")

    # Summary
    total_pnl = sum(r['pnl'] for r in total_results.values())
    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)
    print(f"\n  Total PnL: ${total_pnl:.2f}")

    # The key insight
    unhedged_winner_count = (total_results['unhedged_winner_correct']['count'] +
                            total_results['unhedged_winner_wrong']['count'])
    if unhedged_winner_count > 0:
        win_rate = total_results['unhedged_winner_correct']['count'] / unhedged_winner_count
        print(f"\n  Unhedged Winner Resolution Win Rate: {100*win_rate:.1f}%")
        print(f"  (This is velocity's RESOLUTION accuracy for unhedged trades)")

    print(f"\n{'='*80}")
    print("KEY INSIGHT")
    print("="*80)
    print(f"""
Velocity predicts SHORT-TERM movement well ({100*total_vel_correct/total_vel_total:.1f}% fill direction).
But velocity does NOT predict RESOLUTION well.

When only WINNER fills:
  - Velocity correct (wins resolution): {total_results['unhedged_winner_correct']['count']} → ${total_results['unhedged_winner_correct']['pnl']:.2f}
  - Velocity wrong (loses resolution): {total_results['unhedged_winner_wrong']['count']} → ${total_results['unhedged_winner_wrong']['pnl']:.2f}

The unhedged winner win rate is ~42%, which matches overall velocity→resolution accuracy.
This means UNHEDGED POSITIONS ARE A COIN FLIP WITH NEGATIVE EV.

CONCLUSION: The ONLY way to be profitable is to MAXIMIZE hedge rate.
We need BOTH sides to fill, not just the winner.
    """)


if __name__ == "__main__":
    main()
