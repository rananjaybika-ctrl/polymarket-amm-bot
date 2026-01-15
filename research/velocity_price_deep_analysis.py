#!/usr/bin/env python3
"""
Deep Analysis: Velocity Signal vs Price Movements

ULTRATHINK: Trace exactly how velocity and prices move through each market.
- When does velocity hit zone 4-6?
- How do UP and DOWN prices change AFTER the signal?
- Does the winner side go UP (as expected)?
- Does the loser side go DOWN (creating fill opportunity)?
"""

import pandas as pd
import numpy as np
from collections import defaultdict

MIN_VELOCITY_BPS = 0.30

def analyze_market_deeply(market_df: pd.DataFrame, market_slug: str) -> dict:
    """
    Trace velocity and price movements through entire market.
    """
    result = {
        'market_slug': market_slug,
        'samples': len(market_df),

        # Entry point
        'entry_idx': None,
        'entry_time': None,
        'entry_velocity': None,
        'entry_zone': None,
        'predicted_winner': None,

        # Prices at entry
        'entry_up_bid': None,
        'entry_up_ask': None,
        'entry_down_bid': None,
        'entry_down_ask': None,

        # Price movements AFTER entry
        'winner_price_max': None,
        'winner_price_min': None,
        'loser_price_max': None,
        'loser_price_min': None,

        # Did prices move as predicted?
        'winner_went_up': None,  # Winner price increased (as expected)
        'loser_went_down': None,  # Loser price decreased (fill opportunity)

        # Fill opportunities
        'winner_fill_opportunity': None,  # Could fill winner at entry price or better
        'loser_fill_opportunity': None,   # Could fill loser below entry price

        # Resolution
        'resolution': None,
        'prediction_correct': None,

        # Detailed trace
        'price_trace': [],
    }

    # Find entry (first time velocity hits zone 4-6)
    entry_row = None
    entry_idx = None
    for i, (idx, row) in enumerate(market_df.iterrows()):
        if abs(row['velocity_bps']) >= MIN_VELOCITY_BPS:
            entry_row = row
            entry_idx = i
            break

    if entry_row is None:
        return result

    # Entry details
    result['entry_idx'] = entry_idx
    result['entry_time'] = entry_row['time_remaining_secs']
    result['entry_velocity'] = entry_row['velocity_bps']
    result['predicted_winner'] = "UP" if entry_row['velocity_bps'] > 0 else "DOWN"

    result['entry_up_bid'] = entry_row['up_bid']
    result['entry_up_ask'] = entry_row['up_ask']
    result['entry_down_bid'] = entry_row['down_bid']
    result['entry_down_ask'] = entry_row['down_ask']

    # Track prices AFTER entry
    post_entry_df = market_df.iloc[entry_idx:]

    if result['predicted_winner'] == "UP":
        winner_asks = post_entry_df['up_ask'].values
        loser_asks = post_entry_df['down_ask'].values
        winner_bids = post_entry_df['up_bid'].values
        loser_bids = post_entry_df['down_bid'].values
        entry_winner_ask = result['entry_up_ask']
        entry_loser_ask = result['entry_down_ask']
    else:
        winner_asks = post_entry_df['down_ask'].values
        loser_asks = post_entry_df['up_ask'].values
        winner_bids = post_entry_df['down_bid'].values
        loser_bids = post_entry_df['up_bid'].values
        entry_winner_ask = result['entry_down_ask']
        entry_loser_ask = result['entry_up_ask']

    result['winner_price_max'] = float(np.max(winner_asks))
    result['winner_price_min'] = float(np.min(winner_asks))
    result['loser_price_max'] = float(np.max(loser_asks))
    result['loser_price_min'] = float(np.min(loser_asks))

    # Did prices move as expected?
    # Winner should go UP (ask increases) - we want to buy before it gets expensive
    result['winner_went_up'] = result['winner_price_max'] > entry_winner_ask + 0.01

    # Loser should go DOWN (ask decreases) - we want to buy when cheap
    result['loser_went_down'] = result['loser_price_min'] < entry_loser_ask - 0.01

    # Fill opportunities
    # Winner: Could we fill at entry_ask or better (ask stayed same or dropped)?
    result['winner_fill_opportunity'] = float(np.min(winner_asks)) <= entry_winner_ask

    # Loser: Did ask drop below entry level? By how much?
    loser_drop = entry_loser_ask - result['loser_price_min']
    result['loser_fill_opportunity'] = loser_drop

    # Resolution
    final_row = market_df.iloc[-1]
    if final_row['up_bid'] >= 0.90:
        result['resolution'] = 'UP'
    elif final_row['down_bid'] >= 0.90:
        result['resolution'] = 'DOWN'
    else:
        result['resolution'] = 'UP' if final_row['up_bid'] > final_row['down_bid'] else 'DOWN'

    result['prediction_correct'] = (result['predicted_winner'] == result['resolution'])

    # Detailed price trace at key points
    trace_points = [0, 100, 200, 300, 400, 500]  # samples after entry
    for offset in trace_points:
        if entry_idx + offset < len(market_df):
            row = market_df.iloc[entry_idx + offset]
            result['price_trace'].append({
                'offset': offset,
                'time_remaining': row['time_remaining_secs'],
                'velocity': row['velocity_bps'],
                'up_ask': row['up_ask'],
                'down_ask': row['down_ask'],
            })

    return result


def main():
    print("=" * 80)
    print("VELOCITY vs PRICE MOVEMENT DEEP ANALYSIS")
    print("=" * 80)

    # Load data
    df = pd.read_csv('/Users/rananjaybika/polymarket-amm-bot/research/observer/spread_capture_obs_20260115.csv',
                     on_bad_lines='skip')
    print(f"\nTotal samples: {len(df):,}")

    markets = df['market_slug'].unique()

    # Filter complete markets
    complete = []
    for slug in markets:
        mdf = df[df['market_slug'] == slug]
        if mdf.iloc[0]['time_remaining_secs'] >= 800 and mdf.iloc[-1]['time_remaining_secs'] <= 60:
            complete.append(slug)

    print(f"Complete markets: {len(complete)}")

    # Analyze each market
    results = []
    for slug in complete:
        mdf = df[df['market_slug'] == slug].copy()
        results.append(analyze_market_deeply(mdf, slug))

    entries = [r for r in results if r['entry_idx'] is not None]
    print(f"Markets with zone 4-6 entry: {len(entries)}")

    # Detailed output
    print("\n" + "=" * 80)
    print("MARKET-BY-MARKET ANALYSIS")
    print("=" * 80)

    winner_went_up_count = 0
    loser_went_down_count = 0
    winner_fill_count = 0
    correct_predictions = 0

    loser_drops = []

    for i, r in enumerate(entries):
        print(f"\n{'='*60}")
        print(f"[{i+1}] {r['market_slug']}")
        print(f"{'='*60}")

        print(f"\n  ENTRY SIGNAL:")
        print(f"    Time: {r['entry_time']:.0f}s remaining")
        print(f"    Velocity: {r['entry_velocity']:.4f} bps")
        print(f"    Predicted Winner: {r['predicted_winner']}")

        print(f"\n  PRICES AT ENTRY:")
        print(f"    UP:   bid=${r['entry_up_bid']:.2f}, ask=${r['entry_up_ask']:.2f}")
        print(f"    DOWN: bid=${r['entry_down_bid']:.2f}, ask=${r['entry_down_ask']:.2f}")

        winner = r['predicted_winner']
        loser = "DOWN" if winner == "UP" else "UP"

        entry_winner_ask = r['entry_up_ask'] if winner == "UP" else r['entry_down_ask']
        entry_loser_ask = r['entry_down_ask'] if winner == "UP" else r['entry_up_ask']

        print(f"\n  PRICE MOVEMENTS AFTER ENTRY:")
        print(f"    {winner} (winner) ask: ${entry_winner_ask:.2f} → max ${r['winner_price_max']:.2f}, min ${r['winner_price_min']:.2f}")
        print(f"    {loser} (loser) ask:  ${entry_loser_ask:.2f} → max ${r['loser_price_max']:.2f}, min ${r['loser_price_min']:.2f}")

        # Did winner go up?
        winner_delta = r['winner_price_max'] - entry_winner_ask
        if r['winner_went_up']:
            winner_went_up_count += 1
            print(f"    ✓ Winner went UP by ${winner_delta:.2f} (as predicted)")
        else:
            print(f"    ✗ Winner did NOT go up significantly (delta: ${winner_delta:.2f})")

        # Did loser go down?
        loser_delta = entry_loser_ask - r['loser_price_min']
        loser_drops.append(loser_delta)
        if r['loser_went_down']:
            loser_went_down_count += 1
            print(f"    ✓ Loser went DOWN by ${loser_delta:.2f} (fill opportunity!)")
        else:
            print(f"    ✗ Loser did NOT go down significantly (delta: ${loser_delta:.2f})")

        # Fill opportunity
        if r['winner_fill_opportunity']:
            winner_fill_count += 1
            print(f"    ✓ Winner fillable at entry price or better")
        else:
            print(f"    ✗ Winner price rose before we could fill")

        print(f"\n  PRICE TRACE:")
        for t in r['price_trace'][:4]:
            print(f"    +{t['offset']:3d} samples (t={t['time_remaining']:.0f}s): vel={t['velocity']:.4f}, UP=${t['up_ask']:.2f}, DOWN=${t['down_ask']:.2f}")

        print(f"\n  RESOLUTION: {r['resolution']}")
        if r['prediction_correct']:
            correct_predictions += 1
            print(f"    ✓ Prediction CORRECT")
        else:
            print(f"    ✗ Prediction WRONG")

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY: VELOCITY SIGNAL ANALYSIS")
    print("=" * 80)

    print(f"\n  PRICE MOVEMENT ACCURACY (short-term, not resolution):")
    print(f"    Winner went UP (as predicted): {winner_went_up_count}/{len(entries)} ({100*winner_went_up_count/len(entries):.1f}%)")
    print(f"    Loser went DOWN (fill opportunity): {loser_went_down_count}/{len(entries)} ({100*loser_went_down_count/len(entries):.1f}%)")
    print(f"    Winner fillable at entry: {winner_fill_count}/{len(entries)} ({100*winner_fill_count/len(entries):.1f}%)")

    print(f"\n  LOSER DROP ANALYSIS:")
    print(f"    Average loser drop: ${np.mean(loser_drops):.4f}")
    print(f"    Median loser drop: ${np.median(loser_drops):.4f}")
    print(f"    Max loser drop: ${np.max(loser_drops):.4f}")
    print(f"    Min loser drop: ${np.min(loser_drops):.4f}")
    print(f"    Drops >= $0.03: {sum(1 for d in loser_drops if d >= 0.03)}/{len(loser_drops)}")
    print(f"    Drops >= $0.05: {sum(1 for d in loser_drops if d >= 0.05)}/{len(loser_drops)}")

    print(f"\n  FINAL RESOLUTION ACCURACY:")
    print(f"    Prediction matched resolution: {correct_predictions}/{len(entries)} ({100*correct_predictions/len(entries):.1f}%)")

    print(f"\n  KEY INSIGHT:")
    print(f"    The velocity signal may predict SHORT-TERM price movement")
    print(f"    even if final resolution differs.")
    print(f"    If loser consistently drops by $0.03+, we can fill passive orders.")


if __name__ == "__main__":
    main()
