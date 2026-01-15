#!/usr/bin/env python3
"""
Analyze what happens when velocity IS correct about short-term movement.

If velocity says UP will rise:
- Correct: UP ask rises, DOWN ask drops → loser fills, winner doesn't
- Wrong: UP ask drops, DOWN ask rises → winner fills, loser doesn't

This explains the 0% unhedged winner win rate!
"""

import pandas as pd
import numpy as np
from pathlib import Path

OFFSET = -0.02
MIN_TIME = 120
SHARES = 15  # Target: 15 shares/side (scale to 30 after live validation)


def analyze_file(filepath):
    """Analyze velocity correctness vs fill patterns."""
    df = pd.read_csv(filepath, on_bad_lines='skip')

    markets = df['market_slug'].unique()
    complete = [s for s in markets
                if len(df[df['market_slug']==s]) >= 2
                and df[df['market_slug']==s].iloc[0]['time_remaining_secs'] >= 800
                and df[df['market_slug']==s].iloc[-1]['time_remaining_secs'] <= 60]

    results = []

    for slug in complete:
        mdf = df[df['market_slug'] == slug].copy()

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

        # Entry prices
        up_ask_entry = entry_row['up_ask']
        down_ask_entry = entry_row['down_ask']

        # Bids
        up_bid = round(entry_row['up_bid'] + OFFSET, 2)
        down_bid = round(entry_row['down_bid'] + OFFSET, 2)

        # Post-entry
        post_entry = mdf.iloc[entry_idx:]
        up_min_ask = post_entry['up_ask'].min()
        up_max_ask = post_entry['up_ask'].max()
        down_min_ask = post_entry['down_ask'].min()
        down_max_ask = post_entry['down_ask'].max()

        # Did prices move as velocity predicted?
        # If vel > 0 (UP winner): UP should rise, DOWN should drop
        if velocity > 0:
            up_rose = up_max_ask > up_ask_entry + 0.02  # UP rose significantly
            down_dropped = down_min_ask < down_ask_entry - 0.02  # DOWN dropped
            velocity_correct_short_term = down_dropped  # Key: loser should drop
        else:
            down_rose = down_max_ask > down_ask_entry + 0.02
            up_dropped = up_min_ask < up_ask_entry - 0.02
            velocity_correct_short_term = up_dropped

        # Fills
        up_filled = up_min_ask <= up_bid
        down_filled = down_min_ask <= down_bid

        # Resolution
        final = mdf.iloc[-1]
        if final['up_bid'] >= 0.90:
            resolution = 'UP'
        elif final['down_bid'] >= 0.90:
            resolution = 'DOWN'
        else:
            resolution = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'

        results.append({
            'slug': slug,
            'velocity': velocity,
            'predicted_winner': predicted_winner,
            'velocity_correct_short_term': velocity_correct_short_term,
            'up_filled': up_filled,
            'down_filled': down_filled,
            'resolution': resolution,
            'up_drop': up_ask_entry - up_min_ask,
            'down_drop': down_ask_entry - down_min_ask,
        })

    return results


def main():
    print("="*80)
    print("VELOCITY SHORT-TERM CORRECTNESS ANALYSIS")
    print("="*80)

    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    all_results = []
    for filepath in csv_files:
        try:
            results = analyze_file(str(filepath))
            all_results.extend(results)
        except:
            continue

    print(f"\nTotal markets: {len(all_results)}")

    # Categorize by velocity correctness
    vel_correct = [r for r in all_results if r['velocity_correct_short_term']]
    vel_wrong = [r for r in all_results if not r['velocity_correct_short_term']]

    print(f"\nVelocity correct about short-term: {len(vel_correct)} ({100*len(vel_correct)/len(all_results):.1f}%)")
    print(f"Velocity wrong about short-term: {len(vel_wrong)} ({100*len(vel_wrong)/len(all_results):.1f}%)")

    # When velocity is CORRECT
    print("\n" + "="*80)
    print("WHEN VELOCITY IS CORRECT (loser drops as predicted)")
    print("="*80)

    if vel_correct:
        both_fill = [r for r in vel_correct if r['up_filled'] and r['down_filled']]
        winner_only = [r for r in vel_correct if
                       (r['predicted_winner']=='UP' and r['up_filled'] and not r['down_filled']) or
                       (r['predicted_winner']=='DOWN' and r['down_filled'] and not r['up_filled'])]
        loser_only = [r for r in vel_correct if
                      (r['predicted_winner']=='UP' and r['down_filled'] and not r['up_filled']) or
                      (r['predicted_winner']=='DOWN' and r['up_filled'] and not r['down_filled'])]

        print(f"\n  Both fill (hedged): {len(both_fill)}")
        print(f"  Winner only: {len(winner_only)}")
        print(f"  Loser only: {len(loser_only)}")

        # Resolution accuracy when velocity is correct
        vel_correct_res = sum(1 for r in vel_correct if r['predicted_winner'] == r['resolution'])
        print(f"\n  Resolution accuracy when velocity correct: {vel_correct_res}/{len(vel_correct)} "
              f"({100*vel_correct_res/len(vel_correct):.1f}%)")

    # When velocity is WRONG
    print("\n" + "="*80)
    print("WHEN VELOCITY IS WRONG (loser doesn't drop, winner drops)")
    print("="*80)

    if vel_wrong:
        both_fill = [r for r in vel_wrong if r['up_filled'] and r['down_filled']]
        winner_only = [r for r in vel_wrong if
                       (r['predicted_winner']=='UP' and r['up_filled'] and not r['down_filled']) or
                       (r['predicted_winner']=='DOWN' and r['down_filled'] and not r['up_filled'])]
        loser_only = [r for r in vel_wrong if
                      (r['predicted_winner']=='UP' and r['down_filled'] and not r['up_filled']) or
                      (r['predicted_winner']=='DOWN' and r['up_filled'] and not r['down_filled'])]

        print(f"\n  Both fill (hedged): {len(both_fill)}")
        print(f"  Winner only (velocity wrong, we filled wrong side!): {len(winner_only)}")
        print(f"  Loser only: {len(loser_only)}")

        # Resolution accuracy when velocity is wrong
        vel_wrong_res = sum(1 for r in vel_wrong if r['predicted_winner'] == r['resolution'])
        print(f"\n  Resolution accuracy when velocity wrong: {vel_wrong_res}/{len(vel_wrong)} "
              f"({100*vel_wrong_res/len(vel_wrong):.1f}%)")

    # Average price drops
    print("\n" + "="*80)
    print("PRICE MOVEMENT ANALYSIS")
    print("="*80)

    up_drops = [r['up_drop'] for r in all_results]
    down_drops = [r['down_drop'] for r in all_results]

    print(f"\n  Avg UP ask drop: ${np.mean(up_drops):.2f} (min ${np.min(up_drops):.2f}, max ${np.max(up_drops):.2f})")
    print(f"  Avg DOWN ask drop: ${np.mean(down_drops):.2f} (min ${np.min(down_drops):.2f}, max ${np.max(down_drops):.2f})")

    # Key insight
    print("\n" + "="*80)
    print("KEY INSIGHT")
    print("="*80)
    print(f"""
When velocity predicts UP winner:
  - Expected: UP ask rises, DOWN ask drops → we fill DOWN (loser)
  - Reality: Sometimes UP drops instead → we fill UP (winner) but velocity was WRONG

The "unhedged winner" case is when velocity was WRONG about short-term.
Since velocity was wrong, resolution also goes against us → 0% win rate.

To profit, we need:
1. Velocity to be CORRECT about short-term (loser drops)
2. BOTH sides to fill (hedged position)

If velocity is wrong → we fill the wrong side → loss
If only loser fills → we have wrong side → loss
    """)


if __name__ == "__main__":
    main()
