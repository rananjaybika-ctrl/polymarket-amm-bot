#!/usr/bin/env python3
"""
Two-Sided Grid Backtest V4 - Fixed Fill Logic

Based on deep analysis showing:
- 97% of time, loser drops by $0.28+ average
- Winner fillable 100% of time at entry price
- Loser offset -0.03 to -0.05 should fill 94%+

Fix: Track MIN ask seen after entry, fill when min_ask <= our_bid
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Optional

VELOCITY_ZONES = {
    'neutral':      {'vel_min': 0.00, 'vel_max': 0.05, 'winner_offset': -0.01, 'loser_offset': -0.01},
    'moderate':     {'vel_min': 0.05, 'vel_max': 0.10, 'winner_offset': -0.01, 'loser_offset': -0.02},
    'strong':       {'vel_min': 0.10, 'vel_max': 0.30, 'winner_offset':  0.00, 'loser_offset': -0.04},
    'very_strong':  {'vel_min': 0.30, 'vel_max': 0.50, 'winner_offset': +0.01, 'loser_offset': -0.03},
    'extreme':      {'vel_min': 0.50, 'vel_max': 1.00, 'winner_offset': +0.01, 'loser_offset': -0.04},
    'super_strong': {'vel_min': 1.00, 'vel_max': 99.0, 'winner_offset': +0.01, 'loser_offset': -0.05},
}

MIN_VELOCITY_BPS = 0.30
SHARES = 15


def get_zone(vel):
    abs_vel = abs(vel)
    for name, z in VELOCITY_ZONES.items():
        if z['vel_min'] <= abs_vel < z['vel_max']:
            return name, z
    return 'super_strong', VELOCITY_ZONES['super_strong']


def analyze_market(market_df, slug):
    result = {
        'slug': slug,
        'entry': False,
        'winner': None,
        'winner_bid': None,
        'loser_bid': None,
        'winner_fill_price': None,
        'loser_fill_price': None,
        'winner_filled': False,
        'loser_filled': False,
        'pair_cost': 0,
        'resolution': None,
        'hedged_pnl': 0,
        'unhedged_pnl': 0,
        'total_pnl': 0,
        'debug': {}
    }

    # Find entry
    entry_idx = None
    entry_row = None
    for i, (idx, row) in enumerate(market_df.iterrows()):
        if abs(row['velocity_bps']) >= MIN_VELOCITY_BPS:
            entry_idx = i
            entry_row = row
            break

    if entry_row is None:
        return result

    result['entry'] = True
    vel = entry_row['velocity_bps']
    zone_name, zone = get_zone(vel)

    winner = "UP" if vel > 0 else "DOWN"
    loser = "DOWN" if vel > 0 else "UP"
    result['winner'] = winner

    # Calculate bid prices
    if winner == "UP":
        winner_best_bid = entry_row['up_bid']
        loser_best_bid = entry_row['down_bid']
    else:
        winner_best_bid = entry_row['down_bid']
        loser_best_bid = entry_row['up_bid']

    winner_bid = round(winner_best_bid + zone['winner_offset'], 2)
    loser_bid = round(loser_best_bid + zone['loser_offset'], 2)

    winner_bid = max(0.01, min(0.95, winner_bid))
    loser_bid = max(0.01, min(0.95, loser_bid))

    result['winner_bid'] = winner_bid
    result['loser_bid'] = loser_bid

    # Get post-entry data
    post_entry = market_df.iloc[entry_idx:]

    if winner == "UP":
        winner_asks = post_entry['up_ask'].values
        loser_asks = post_entry['down_ask'].values
    else:
        winner_asks = post_entry['down_ask'].values
        loser_asks = post_entry['up_ask'].values

    # Track MIN ask after entry for each side
    winner_min_ask = np.min(winner_asks)
    loser_min_ask = np.min(loser_asks)

    result['debug']['winner_min_ask'] = winner_min_ask
    result['debug']['loser_min_ask'] = loser_min_ask
    result['debug']['winner_entry_ask'] = winner_asks[0]
    result['debug']['loser_entry_ask'] = loser_asks[0]

    # FILL LOGIC: Fill when min_ask <= our_bid
    # (passive bid gets hit when ask drops to our level)
    if winner_min_ask <= winner_bid:
        result['winner_filled'] = True
        result['winner_fill_price'] = winner_min_ask  # Fill at best available
    else:
        # Winner bid was below min ask, check if we can fill at entry ask
        # (aggressive offset +0.01 means we're above best_bid, should fill)
        if zone['winner_offset'] >= 0:
            # We bid aggressively, should fill at entry ask
            result['winner_filled'] = True
            result['winner_fill_price'] = winner_asks[0]

    if loser_min_ask <= loser_bid:
        result['loser_filled'] = True
        result['loser_fill_price'] = loser_min_ask

    # Resolution
    final = market_df.iloc[-1]
    if final['up_bid'] >= 0.90:
        result['resolution'] = 'UP'
    elif final['down_bid'] >= 0.90:
        result['resolution'] = 'DOWN'
    else:
        result['resolution'] = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'

    # Calculate PnL
    if result['winner_filled'] and result['loser_filled']:
        result['pair_cost'] = result['winner_fill_price'] + result['loser_fill_price']
        result['hedged_pnl'] = (1.0 - result['pair_cost']) * SHARES
    elif result['winner_filled']:
        # Only winner filled
        if winner == result['resolution']:
            result['unhedged_pnl'] = (1.0 - result['winner_fill_price']) * SHARES
        else:
            result['unhedged_pnl'] = (0.0 - result['winner_fill_price']) * SHARES
    elif result['loser_filled']:
        # Only loser filled
        if loser == result['resolution']:
            result['unhedged_pnl'] = (1.0 - result['loser_fill_price']) * SHARES
        else:
            result['unhedged_pnl'] = (0.0 - result['loser_fill_price']) * SHARES

    result['total_pnl'] = result['hedged_pnl'] + result['unhedged_pnl']

    return result


def main():
    print("=" * 80)
    print("TWO-SIDED GRID BACKTEST V4 - Correct Fill Logic")
    print("=" * 80)
    print("\nFill rule: Fill when MIN ask after entry <= our bid")
    print("Winner offset: +0.01 (aggressive)")
    print("Loser offset: -0.03 to -0.05 (passive)")

    df = pd.read_csv('/Users/rananjaybika/polymarket-amm-bot/research/observer/spread_capture_obs_20260115.csv',
                     on_bad_lines='skip')

    markets = df['market_slug'].unique()
    complete = [s for s in markets
                if df[df['market_slug']==s].iloc[0]['time_remaining_secs'] >= 800
                and df[df['market_slug']==s].iloc[-1]['time_remaining_secs'] <= 60]

    print(f"\nComplete markets: {len(complete)}")

    results = []
    for slug in complete:
        mdf = df[df['market_slug'] == slug].copy()
        results.append(analyze_market(mdf, slug))

    entries = [r for r in results if r['entry']]
    print(f"Markets with zone 4-6 entry: {len(entries)}")

    # Output
    print("\n" + "=" * 80)
    print("TRADE DETAILS")
    print("=" * 80)

    total_hedged = 0
    total_unhedged = 0
    both_filled = 0
    winner_only = 0
    loser_only = 0

    for i, r in enumerate(entries):
        print(f"\n[{i+1}] {r['slug']}")
        print(f"    Winner: {r['winner']}, bid=${r['winner_bid']:.2f}")
        print(f"    Loser bid: ${r['loser_bid']:.2f}")
        print(f"    Winner min_ask: ${r['debug']['winner_min_ask']:.2f}, entry_ask: ${r['debug']['winner_entry_ask']:.2f}")
        print(f"    Loser min_ask: ${r['debug']['loser_min_ask']:.2f}, entry_ask: ${r['debug']['loser_entry_ask']:.2f}")

        w_status = f"FILLED @ ${r['winner_fill_price']:.2f}" if r['winner_filled'] else "NO FILL"
        l_status = f"FILLED @ ${r['loser_fill_price']:.2f}" if r['loser_filled'] else "NO FILL"
        print(f"    Winner: {w_status}")
        print(f"    Loser: {l_status}")

        if r['winner_filled'] and r['loser_filled']:
            print(f"    Pair cost: ${r['pair_cost']:.4f}")
            both_filled += 1
        elif r['winner_filled']:
            winner_only += 1
        elif r['loser_filled']:
            loser_only += 1

        print(f"    Resolution: {r['resolution']}")
        print(f"    PnL: ${r['total_pnl']:.2f}")

        total_hedged += r['hedged_pnl']
        total_unhedged += r['unhedged_pnl']

    total_pnl = total_hedged + total_unhedged

    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    print(f"\nFill rates:")
    print(f"  Both filled: {both_filled}/{len(entries)} ({100*both_filled/len(entries):.1f}%)")
    print(f"  Winner only: {winner_only}/{len(entries)} ({100*winner_only/len(entries):.1f}%)")
    print(f"  Loser only: {loser_only}/{len(entries)} ({100*loser_only/len(entries):.1f}%)")

    hedged = [r for r in entries if r['winner_filled'] and r['loser_filled']]
    if hedged:
        avg_pc = sum(r['pair_cost'] for r in hedged) / len(hedged)
        print(f"\nAvg pair cost: ${avg_pc:.4f}")

    print(f"\n--- PnL ---")
    print(f"Hedged: ${total_hedged:.2f}")
    print(f"Unhedged: ${total_unhedged:.2f}")
    print(f"TOTAL: ${total_pnl:.2f}")

    hours = len(entries) * 15 / 60
    print(f"Hourly: ${total_pnl/hours:.2f}/hr")


if __name__ == "__main__":
    main()
