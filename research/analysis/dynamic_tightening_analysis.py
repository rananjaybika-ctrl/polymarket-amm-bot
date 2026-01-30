#!/usr/bin/env python3
"""
Dynamic Tightening Analysis - Jan 15, 2026
Proper backtest on actual observer data with:
- Complete markets only (full 15 min cycle)
- Correct market resolution for unhedged impact
- Mathematical breakdown of dynamic tightening
"""

import pandas as pd
import numpy as np
from collections import defaultdict

# =============================================================================
# CONFIGURATION - Current offsets from observer
# =============================================================================
VELOCITY_ZONES = {
    'neutral':      {'vel_min': 0.00, 'vel_max': 0.05, 'pair_target': 0.97, 'winner_offset': -0.01, 'loser_offset': -0.01},
    'moderate':     {'vel_min': 0.05, 'vel_max': 0.10, 'pair_target': 0.97, 'winner_offset': -0.01, 'loser_offset': -0.02},
    'strong':       {'vel_min': 0.10, 'vel_max': 0.30, 'pair_target': 0.96, 'winner_offset':  0.00, 'loser_offset': -0.04},
    'very_strong':  {'vel_min': 0.30, 'vel_max': 0.50, 'pair_target': 0.95, 'winner_offset': +0.01, 'loser_offset': -0.03},
    'extreme':      {'vel_min': 0.50, 'vel_max': 1.00, 'pair_target': 0.94, 'winner_offset': +0.01, 'loser_offset': -0.04},
    'super_strong': {'vel_min': 1.00, 'vel_max': 99.0, 'pair_target': 0.93, 'winner_offset': +0.01, 'loser_offset': -0.05},
}

SHARES = 15
BALANCE = 170

def get_zone(velocity_bps):
    """Determine velocity zone from absolute velocity"""
    abs_vel = abs(velocity_bps)
    for name, z in VELOCITY_ZONES.items():
        if z['vel_min'] <= abs_vel < z['vel_max']:
            return name
    return 'super_strong'

def get_zone_config(zone_name):
    return VELOCITY_ZONES.get(zone_name, VELOCITY_ZONES['neutral'])

def analyze_market(market_df, market_slug):
    """
    Analyze a single complete market.
    Returns dict with all trade details and PnL calculations.
    """
    result = {
        'market_slug': market_slug,
        'samples': len(market_df),
        'start_time_remaining': market_df.iloc[0]['time_remaining_secs'],
        'end_time_remaining': market_df.iloc[-1]['time_remaining_secs'],
        'entry_found': False,
        'entry_zone': None,
        'entry_side': None,
        'entry_price': None,
        'entry_time_remaining': None,
        'entry_velocity': None,
        'initial_hedge_target': None,
        'tightened_hedge_targets': [],
        'final_hedge_target': None,
        'hedge_filled': False,
        'hedge_fill_price': None,
        'hedge_fill_time_remaining': None,
        'market_resolution': None,  # 'UP' or 'DOWN'
        'prediction_correct': None,
        'pnl_no_tightening': 0.0,
        'pnl_with_tightening': 0.0,
        'tightening_benefit': 0.0,
    }

    # Filter for zones 4-6 entries only
    entry_zones = ['very_strong', 'extreme', 'super_strong']

    # Find first entry signal in zones 4-6
    entry_row = None
    for idx, row in market_df.iterrows():
        if row['entry_signal'] == True and row['velocity_zone'] in entry_zones:
            entry_row = row
            break

    if entry_row is None:
        return result

    result['entry_found'] = True
    result['entry_zone'] = entry_row['velocity_zone']
    result['entry_side'] = entry_row['entry_side']  # UP or DOWN
    result['entry_velocity'] = entry_row['velocity_bps']
    result['entry_time_remaining'] = entry_row['time_remaining_secs']

    # Entry price is the ASK on the entry side (we're buying)
    if result['entry_side'] == 'UP':
        result['entry_price'] = entry_row['up_ask']
        loser_side = 'DOWN'
    else:
        result['entry_price'] = entry_row['down_ask']
        loser_side = 'UP'

    # Calculate initial hedge target
    zone_config = get_zone_config(result['entry_zone'])
    pair_target = zone_config['pair_target']
    loser_offset = zone_config['loser_offset']

    # Hedge target = pair_target - entry_price + loser_offset
    # This is the price we're willing to pay for the loser side
    result['initial_hedge_target'] = pair_target - result['entry_price'] + loser_offset
    result['final_hedge_target'] = result['initial_hedge_target']

    # Track hedge target tightening as velocity changes
    current_target = result['initial_hedge_target']
    tighten_history = [(result['entry_time_remaining'], result['entry_zone'], current_target)]

    # Get rows after entry
    entry_idx = market_df.index.get_loc(entry_row.name)
    post_entry_df = market_df.iloc[entry_idx:]

    hedge_filled_no_tightening = False
    hedge_fill_price_no_tightening = None
    hedge_filled_with_tightening = False
    hedge_fill_price_with_tightening = None

    for idx, row in post_entry_df.iterrows():
        # Get loser side ask price
        if loser_side == 'UP':
            loser_ask = row['up_ask']
        else:
            loser_ask = row['down_ask']

        # Check if hedge would fill at INITIAL target (no tightening scenario)
        if not hedge_filled_no_tightening and loser_ask <= result['initial_hedge_target']:
            hedge_filled_no_tightening = True
            hedge_fill_price_no_tightening = min(loser_ask, result['initial_hedge_target'])

        # Dynamic tightening: check if velocity strengthened in SAME direction
        current_vel = row['velocity_bps']
        entry_vel_dir = 1 if result['entry_velocity'] > 0 else -1
        current_vel_dir = 1 if current_vel > 0 else -1

        if current_vel_dir == entry_vel_dir:  # Same direction
            new_zone = get_zone(current_vel)
            new_config = get_zone_config(new_zone)
            new_pair_target = new_config['pair_target']
            new_loser_offset = new_config['loser_offset']
            new_target = new_pair_target - result['entry_price'] + new_loser_offset

            # ONLY TIGHTEN (lower target), never loosen
            if new_target < current_target:
                tighten_history.append((row['time_remaining_secs'], new_zone, new_target))
                current_target = new_target
                result['final_hedge_target'] = current_target

        # Check if hedge would fill at CURRENT (tightened) target
        if not hedge_filled_with_tightening and loser_ask <= current_target:
            hedge_filled_with_tightening = True
            hedge_fill_price_with_tightening = min(loser_ask, current_target)
            result['hedge_fill_time_remaining'] = row['time_remaining_secs']

    result['tightened_hedge_targets'] = tighten_history

    # Determine market resolution from final prices
    final_row = market_df.iloc[-1]
    if final_row['up_bid'] >= 0.90:
        result['market_resolution'] = 'UP'
    elif final_row['down_bid'] >= 0.90:
        result['market_resolution'] = 'DOWN'
    else:
        # Unclear resolution - use higher bid
        if final_row['up_bid'] > final_row['down_bid']:
            result['market_resolution'] = 'UP'
        else:
            result['market_resolution'] = 'DOWN'

    result['prediction_correct'] = (result['entry_side'] == result['market_resolution'])

    # Calculate PnL for both scenarios
    entry_cost = result['entry_price'] * SHARES

    # === NO TIGHTENING SCENARIO ===
    if hedge_filled_no_tightening:
        hedge_cost = hedge_fill_price_no_tightening * SHARES
        pair_cost = result['entry_price'] + hedge_fill_price_no_tightening
        # Hedged: we own both sides, one pays $1
        result['pnl_no_tightening'] = (1.0 - pair_cost) * SHARES
        result['hedge_status_no_tight'] = 'HEDGED'
    else:
        # Unhedged: depends on market resolution
        if result['prediction_correct']:
            # Entry side wins, pays $1
            result['pnl_no_tightening'] = (1.0 - result['entry_price']) * SHARES
        else:
            # Entry side loses, pays $0
            result['pnl_no_tightening'] = (0.0 - result['entry_price']) * SHARES
        result['hedge_status_no_tight'] = 'UNHEDGED'

    # === WITH TIGHTENING SCENARIO ===
    if hedge_filled_with_tightening:
        hedge_cost = hedge_fill_price_with_tightening * SHARES
        pair_cost = result['entry_price'] + hedge_fill_price_with_tightening
        result['pnl_with_tightening'] = (1.0 - pair_cost) * SHARES
        result['hedge_fill_price'] = hedge_fill_price_with_tightening
        result['hedge_filled'] = True
        result['hedge_status_tight'] = 'HEDGED'
    else:
        if result['prediction_correct']:
            result['pnl_with_tightening'] = (1.0 - result['entry_price']) * SHARES
        else:
            result['pnl_with_tightening'] = (0.0 - result['entry_price']) * SHARES
        result['hedge_status_tight'] = 'UNHEDGED'

    result['tightening_benefit'] = result['pnl_with_tightening'] - result['pnl_no_tightening']

    return result


def main():
    print("=" * 80)
    print("DYNAMIC TIGHTENING ANALYSIS - Jan 15, 2026 Observer Data")
    print("=" * 80)

    # Load data (handle malformed lines)
    df = pd.read_csv('/Users/rananjaybika/polymarket-amm-bot/research/observer/spread_capture_obs_20260115.csv',
                     on_bad_lines='skip')
    print(f"\nTotal samples: {len(df):,}")

    # Get unique markets
    markets = df['market_slug'].unique()
    print(f"Total markets: {len(markets)}")

    # Filter for COMPLETE markets only
    # Complete = started with >800s remaining AND ended with <60s remaining
    complete_markets = []
    incomplete_reasons = defaultdict(int)

    for market_slug in markets:
        market_df = df[df['market_slug'] == market_slug].copy()
        start_time = market_df.iloc[0]['time_remaining_secs']
        end_time = market_df.iloc[-1]['time_remaining_secs']

        if start_time < 800:
            incomplete_reasons['started_late'] += 1
        elif end_time > 60:
            incomplete_reasons['ended_early'] += 1
        else:
            complete_markets.append(market_slug)

    print(f"\nComplete markets (full 15 min): {len(complete_markets)}")
    print(f"Incomplete markets:")
    for reason, count in incomplete_reasons.items():
        print(f"  - {reason}: {count}")

    # Analyze each complete market
    results = []
    for market_slug in complete_markets:
        market_df = df[df['market_slug'] == market_slug].copy()
        result = analyze_market(market_df, market_slug)
        results.append(result)

    # Filter for markets with zone 4-6 entries
    entries = [r for r in results if r['entry_found']]

    print(f"\n" + "=" * 80)
    print("MARKETS WITH ZONE 4-6 ENTRIES")
    print("=" * 80)
    print(f"Markets with entries in zones 4-6: {len(entries)}")

    if len(entries) == 0:
        print("\nNO ENTRIES FOUND IN ZONES 4-6!")
        print("This means velocity never reached 0.30+ bps during complete markets.")

        # Show velocity distribution
        print("\nVelocity distribution in complete markets:")
        for market_slug in complete_markets[:10]:
            market_df = df[df['market_slug'] == market_slug]
            max_vel = market_df['velocity_bps'].abs().max()
            print(f"  {market_slug}: max |velocity| = {max_vel:.4f} bps")
        return

    # Detailed breakdown
    print("\n" + "-" * 80)
    print("TRADE-BY-TRADE BREAKDOWN")
    print("-" * 80)

    total_pnl_no_tight = 0
    total_pnl_with_tight = 0
    hedged_no_tight = 0
    hedged_with_tight = 0
    correct_predictions = 0

    for i, r in enumerate(entries):
        print(f"\n[Trade {i+1}] {r['market_slug']}")
        print(f"  Entry: {r['entry_side']} @ ${r['entry_price']:.4f} (zone: {r['entry_zone']}, vel: {r['entry_velocity']:.4f} bps)")
        print(f"  Initial hedge target: ${r['initial_hedge_target']:.4f}")

        if len(r['tightened_hedge_targets']) > 1:
            print(f"  Tightening events:")
            for t_time, t_zone, t_target in r['tightened_hedge_targets']:
                print(f"    - {t_time:.0f}s remaining: {t_zone} → target ${t_target:.4f}")
            print(f"  Final hedge target: ${r['final_hedge_target']:.4f}")
        else:
            print(f"  No tightening occurred")

        print(f"  Market resolved: {r['market_resolution']}")
        print(f"  Prediction correct: {r['prediction_correct']}")

        print(f"\n  === PnL COMPARISON ===")
        print(f"  NO TIGHTENING:")
        print(f"    Status: {r['hedge_status_no_tight']}")
        if r['hedge_status_no_tight'] == 'HEDGED':
            print(f"    Pair cost: ${r['entry_price']:.4f} + ${r['initial_hedge_target']:.4f} = ${r['entry_price'] + r['initial_hedge_target']:.4f}")
            print(f"    PnL = (1.0 - {r['entry_price'] + r['initial_hedge_target']:.4f}) × {SHARES} = ${r['pnl_no_tightening']:.2f}")
        else:
            if r['prediction_correct']:
                print(f"    Entry wins → PnL = (1.0 - {r['entry_price']:.4f}) × {SHARES} = ${r['pnl_no_tightening']:.2f}")
            else:
                print(f"    Entry loses → PnL = (0.0 - {r['entry_price']:.4f}) × {SHARES} = ${r['pnl_no_tightening']:.2f}")

        print(f"  WITH TIGHTENING:")
        print(f"    Status: {r['hedge_status_tight']}")
        if r['hedge_status_tight'] == 'HEDGED':
            print(f"    Pair cost: ${r['entry_price']:.4f} + ${r['hedge_fill_price']:.4f} = ${r['entry_price'] + r['hedge_fill_price']:.4f}")
            print(f"    PnL = (1.0 - {r['entry_price'] + r['hedge_fill_price']:.4f}) × {SHARES} = ${r['pnl_with_tightening']:.2f}")
        else:
            if r['prediction_correct']:
                print(f"    Entry wins → PnL = (1.0 - {r['entry_price']:.4f}) × {SHARES} = ${r['pnl_with_tightening']:.2f}")
            else:
                print(f"    Entry loses → PnL = (0.0 - {r['entry_price']:.4f}) × {SHARES} = ${r['pnl_with_tightening']:.2f}")

        print(f"  TIGHTENING BENEFIT: ${r['tightening_benefit']:.2f}")

        total_pnl_no_tight += r['pnl_no_tightening']
        total_pnl_with_tight += r['pnl_with_tightening']
        if r['hedge_status_no_tight'] == 'HEDGED':
            hedged_no_tight += 1
        if r['hedge_status_tight'] == 'HEDGED':
            hedged_with_tight += 1
        if r['prediction_correct']:
            correct_predictions += 1

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    print(f"\nTotal trades (zone 4-6 entries): {len(entries)}")
    print(f"Prediction accuracy: {correct_predictions}/{len(entries)} ({100*correct_predictions/len(entries):.1f}%)")

    print(f"\n--- NO TIGHTENING ---")
    print(f"Hedge rate: {hedged_no_tight}/{len(entries)} ({100*hedged_no_tight/len(entries):.1f}%)")
    print(f"Total PnL: ${total_pnl_no_tight:.2f}")

    print(f"\n--- WITH DYNAMIC TIGHTENING ---")
    print(f"Hedge rate: {hedged_with_tight}/{len(entries)} ({100*hedged_with_tight/len(entries):.1f}%)")
    print(f"Total PnL: ${total_pnl_with_tight:.2f}")

    print(f"\n--- TIGHTENING IMPACT ---")
    print(f"PnL Difference: ${total_pnl_with_tight - total_pnl_no_tight:.2f}")

    # Zone breakdown
    print("\n" + "-" * 80)
    print("BY ENTRY ZONE")
    print("-" * 80)

    for zone in ['very_strong', 'extreme', 'super_strong']:
        zone_entries = [r for r in entries if r['entry_zone'] == zone]
        if zone_entries:
            zone_pnl_no = sum(r['pnl_no_tightening'] for r in zone_entries)
            zone_pnl_yes = sum(r['pnl_with_tightening'] for r in zone_entries)
            zone_correct = sum(1 for r in zone_entries if r['prediction_correct'])
            print(f"\n{zone}:")
            print(f"  Trades: {len(zone_entries)}")
            print(f"  Accuracy: {zone_correct}/{len(zone_entries)} ({100*zone_correct/len(zone_entries):.1f}%)")
            print(f"  PnL (no tight): ${zone_pnl_no:.2f}")
            print(f"  PnL (with tight): ${zone_pnl_yes:.2f}")
            print(f"  Benefit: ${zone_pnl_yes - zone_pnl_no:.2f}")


if __name__ == "__main__":
    main()
