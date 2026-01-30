#!/usr/bin/env python3
"""
Conservative Offset PnL Analysis
Analyzes observer data with NEW conservative offsets:
- Zone 4 (very_strong): winner=+0.01, loser=-0.03
- Zone 5 (extreme): winner=+0.01, loser=-0.04
- Zone 6 (super_strong): winner=+0.01, loser=-0.05

Compares against OLD super aggressive offsets:
- Zone 4: winner=+0.01, loser=-0.12
- Zone 5: winner=+0.01, loser=-0.15
- Zone 6: winner=+0.02, loser=-0.18
"""

import pandas as pd
import numpy as np
from collections import defaultdict

# Configuration
SHARES = 15
MIN_VELOCITY_BPS = 0.30

# OLD super aggressive offsets
OLD_OFFSETS = {
    'very_strong': {'pair_target': 0.95, 'winner_offset': 0.01, 'loser_offset': -0.12},
    'extreme': {'pair_target': 0.94, 'winner_offset': 0.01, 'loser_offset': -0.15},
    'super_strong': {'pair_target': 0.93, 'winner_offset': 0.02, 'loser_offset': -0.18},
}

# NEW conservative offsets
NEW_OFFSETS = {
    'very_strong': {'pair_target': 0.95, 'winner_offset': 0.01, 'loser_offset': -0.03},
    'extreme': {'pair_target': 0.94, 'winner_offset': 0.01, 'loser_offset': -0.04},
    'super_strong': {'pair_target': 0.93, 'winner_offset': 0.01, 'loser_offset': -0.05},
}

def get_zone(velocity_bps):
    """Get velocity zone name."""
    abs_vel = abs(velocity_bps)
    if abs_vel >= 1.00:
        return 'super_strong'
    elif abs_vel >= 0.50:
        return 'extreme'
    elif abs_vel >= 0.30:
        return 'very_strong'
    elif abs_vel >= 0.10:
        return 'strong'
    elif abs_vel >= 0.05:
        return 'moderate'
    return 'neutral'

def analyze_market(df, offsets):
    """Analyze a single market with given offsets.

    Returns dict with entry info, hedge info, resolution, and PnL.
    """
    market_slug = df['market_slug'].iloc[0]

    # Check completeness: FULL 15-minute market
    # Started with >900s (within first ~60s of market) AND ended with <60s (near resolution)
    start_time = df['time_remaining_secs'].iloc[0]
    end_time = df['time_remaining_secs'].iloc[-1]

    if start_time < 900 or end_time > 60:
        return None  # Incomplete market - didn't run full 15 mins

    # Find first zone 4-6 entry signal
    entry_row = None
    entry_idx = None
    for idx, row in df.iterrows():
        vel = row['velocity_bps']
        zone = get_zone(vel)
        if zone in ['very_strong', 'extreme', 'super_strong']:
            entry_row = row
            entry_idx = idx
            break

    if entry_row is None:
        return None  # No zone 4-6 signal in this market

    # Entry details
    velocity = entry_row['velocity_bps']
    zone = get_zone(velocity)
    entry_side = "UP" if velocity > 0 else "DOWN"

    # Get prices at entry
    if entry_side == "UP":
        best_bid = entry_row['up_bid']
        best_ask = entry_row['up_ask']
        loser_bid = entry_row['down_bid']
        loser_ask = entry_row['down_ask']
    else:
        best_bid = entry_row['down_bid']
        best_ask = entry_row['down_ask']
        loser_bid = entry_row['up_bid']
        loser_ask = entry_row['up_ask']

    # Calculate entry bid with winner offset
    config = offsets[zone]
    entry_bid = best_bid + config['winner_offset']
    entry_bid = min(entry_bid, best_ask - 0.001)  # Don't cross spread
    entry_bid = max(0.01, min(0.95, entry_bid))

    # Check if entry would fill (bid >= best_bid + 0.005 or bid >= best_ask)
    entry_filled = entry_bid >= best_ask or entry_bid >= best_bid + 0.005
    if not entry_filled:
        return None  # Entry didn't fill

    # Entry fills at ASK price (we're buyer, fill at best offer)
    entry_price = best_ask

    # Calculate hedge target with loser offset
    pair_target = config['pair_target']
    loser_offset = config['loser_offset']
    hedge_target = pair_target - entry_price + loser_offset
    hedge_target = max(0.01, min(0.95, hedge_target))

    # Check if hedge would fill: scan all rows after entry
    hedge_filled = False
    hedge_price = None

    df_after_entry = df.loc[entry_idx:]
    for _, row in df_after_entry.iterrows():
        if entry_side == "UP":
            # Hedge is DOWN side
            loser_ask_now = row['down_ask']
        else:
            # Hedge is UP side
            loser_ask_now = row['up_ask']

        if loser_ask_now <= hedge_target:
            hedge_filled = True
            hedge_price = loser_ask_now
            break

    # Determine market resolution from final prices (last 5 samples for stability)
    final_rows = df.tail(5)
    final_up_bid = final_rows['up_bid'].mean()
    final_down_bid = final_rows['down_bid'].mean()

    # Resolution: which side went to ~1.0 (winner) vs ~0.0 (loser)
    if final_up_bid >= 0.85:
        resolution = "UP"
    elif final_down_bid >= 0.85:
        resolution = "DOWN"
    else:
        resolution = "UNCLEAR"

    # Calculate PnL
    if hedge_filled:
        # Hedged: profit = 1.0 - entry_price - hedge_price
        pnl = (1.0 - entry_price - hedge_price) * SHARES
        pnl_type = "HEDGED"
    else:
        # Unhedged: depends on resolution
        if resolution == entry_side:
            # Correct prediction
            pnl = (1.0 - entry_price) * SHARES
            pnl_type = "UNHEDGED_CORRECT"
        elif resolution == "UNCLEAR":
            # Can't determine
            pnl = 0
            pnl_type = "UNCLEAR"
        else:
            # Wrong prediction
            pnl = -entry_price * SHARES
            pnl_type = "UNHEDGED_WRONG"

    return {
        'market': market_slug,
        'entry_side': entry_side,
        'zone': zone,
        'velocity': velocity,
        'entry_price': entry_price,
        'hedge_target': hedge_target,
        'hedge_filled': hedge_filled,
        'hedge_price': hedge_price,
        'resolution': resolution,
        'pnl_type': pnl_type,
        'pnl': pnl,
        'final_up_bid': final_up_bid,
        'final_down_bid': final_down_bid,
    }

def main():
    # Load data
    df = pd.read_csv('/Users/rananjaybika/polymarket-amm-bot/research/observer/spread_capture_obs_20260115_latest.csv')
    print(f"Loaded {len(df)} rows")
    print(f"Time range: {df['time_remaining_secs'].min():.1f}s to {df['time_remaining_secs'].max():.1f}s")

    # Get unique markets
    markets = df['market_slug'].unique()
    print(f"Total markets: {len(markets)}")

    # Analyze each market with both offset configurations
    old_results = []
    new_results = []

    for market in markets:
        market_df = df[df['market_slug'] == market].copy()

        old_result = analyze_market(market_df, OLD_OFFSETS)
        new_result = analyze_market(market_df, NEW_OFFSETS)

        if old_result:
            old_results.append(old_result)
        if new_result:
            new_results.append(new_result)

    print(f"\n{'='*80}")
    print("COMPLETE MARKETS ANALYZED")
    print(f"{'='*80}")
    print(f"OLD (super aggressive): {len(old_results)} markets")
    print(f"NEW (conservative): {len(new_results)} markets")

    # Detailed analysis for each configuration
    for name, results in [("OLD SUPER AGGRESSIVE", old_results), ("NEW CONSERVATIVE", new_results)]:
        print(f"\n{'='*80}")
        print(f"{name} OFFSETS")
        print(f"{'='*80}")

        if not results:
            print("No results")
            continue

        # Summary stats
        hedged = [r for r in results if r['hedge_filled']]
        unhedged_correct = [r for r in results if r['pnl_type'] == 'UNHEDGED_CORRECT']
        unhedged_wrong = [r for r in results if r['pnl_type'] == 'UNHEDGED_WRONG']
        unclear = [r for r in results if r['pnl_type'] == 'UNCLEAR']

        total_pnl = sum(r['pnl'] for r in results)
        hedged_pnl = sum(r['pnl'] for r in hedged)
        unhedged_correct_pnl = sum(r['pnl'] for r in unhedged_correct)
        unhedged_wrong_pnl = sum(r['pnl'] for r in unhedged_wrong)

        print(f"\nSUMMARY:")
        print(f"  Total markets: {len(results)}")
        print(f"  Hedged: {len(hedged)} ({100*len(hedged)/len(results):.1f}%)")
        print(f"  Unhedged Correct: {len(unhedged_correct)}")
        print(f"  Unhedged Wrong: {len(unhedged_wrong)}")
        print(f"  Unclear: {len(unclear)}")

        print(f"\nPnL BREAKDOWN:")
        print(f"  Hedged PnL: ${hedged_pnl:+.2f}")
        print(f"  Unhedged Correct PnL: ${unhedged_correct_pnl:+.2f}")
        print(f"  Unhedged Wrong PnL: ${unhedged_wrong_pnl:+.2f}")
        print(f"  TOTAL PnL: ${total_pnl:+.2f}")

        # Signal accuracy (correct predictions = entry_side matches resolution)
        correct_predictions = [r for r in results if r['entry_side'] == r['resolution']]
        wrong_predictions = [r for r in results if r['entry_side'] != r['resolution'] and r['resolution'] != 'UNCLEAR']
        unclear_predictions = [r for r in results if r['resolution'] == 'UNCLEAR']

        total_resolved = len(correct_predictions) + len(wrong_predictions)
        if total_resolved > 0:
            accuracy = 100 * len(correct_predictions) / total_resolved
            print(f"\nSIGNAL ACCURACY: {len(correct_predictions)}/{total_resolved} = {accuracy:.1f}%")
            print(f"  (Correct: {len(correct_predictions)}, Wrong: {len(wrong_predictions)}, Unclear: {len(unclear_predictions)})")

        # Per-zone breakdown
        print(f"\nPER-ZONE BREAKDOWN:")
        for zone in ['very_strong', 'extreme', 'super_strong']:
            zone_results = [r for r in results if r['zone'] == zone]
            if zone_results:
                zone_hedged = len([r for r in zone_results if r['hedge_filled']])
                zone_pnl = sum(r['pnl'] for r in zone_results)
                print(f"  {zone}: {len(zone_results)} markets, {zone_hedged} hedged ({100*zone_hedged/len(zone_results):.0f}%), PnL: ${zone_pnl:+.2f}")

        # Market-by-market detail
        print(f"\nMARKET-BY-MARKET DETAIL:")
        print(f"{'Market':<35} {'Side':<5} {'Zone':<13} {'Entry':<6} {'Hedge':<6} {'Fill':<5} {'Res':<8} {'PnL':>8}")
        print("-" * 95)

        for r in results:
            hedge_str = f"${r['hedge_price']:.2f}" if r['hedge_price'] else "---"
            fill_str = "YES" if r['hedge_filled'] else "NO"
            print(f"{r['market'][-34:]:<35} {r['entry_side']:<5} {r['zone']:<13} ${r['entry_price']:.2f}  {hedge_str:<6} {fill_str:<5} {r['pnl_type']:<8} ${r['pnl']:>+7.2f}")

        print("-" * 95)
        print(f"{'TOTAL':<75} ${total_pnl:>+7.2f}")

    # Comparison
    if old_results and new_results:
        print(f"\n{'='*80}")
        print("COMPARISON: OLD vs NEW")
        print(f"{'='*80}")

        old_total = sum(r['pnl'] for r in old_results)
        new_total = sum(r['pnl'] for r in new_results)

        old_hedge_rate = 100 * len([r for r in old_results if r['hedge_filled']]) / len(old_results)
        new_hedge_rate = 100 * len([r for r in new_results if r['hedge_filled']]) / len(new_results)

        old_hedged_pnl = sum(r['pnl'] for r in old_results if r['hedge_filled'])
        new_hedged_pnl = sum(r['pnl'] for r in new_results if r['hedge_filled'])

        old_unhedged_pnl = sum(r['pnl'] for r in old_results if not r['hedge_filled'])
        new_unhedged_pnl = sum(r['pnl'] for r in new_results if not r['hedge_filled'])

        print(f"\n{'Metric':<25} {'Old (Super Agg)':<18} {'New (Conservative)':<18} {'Diff':<10}")
        print("-" * 70)
        print(f"{'Hedge Rate':<25} {old_hedge_rate:>15.1f}% {new_hedge_rate:>15.1f}% {new_hedge_rate-old_hedge_rate:>+8.1f}pp")
        print(f"{'Hedged PnL':<25} ${old_hedged_pnl:>14.2f} ${new_hedged_pnl:>14.2f} ${new_hedged_pnl-old_hedged_pnl:>+8.2f}")
        print(f"{'Unhedged PnL':<25} ${old_unhedged_pnl:>14.2f} ${new_unhedged_pnl:>14.2f} ${new_unhedged_pnl-old_unhedged_pnl:>+8.2f}")
        print(f"{'TOTAL PnL':<25} ${old_total:>14.2f} ${new_total:>14.2f} ${new_total-old_total:>+8.2f}")

if __name__ == "__main__":
    main()
