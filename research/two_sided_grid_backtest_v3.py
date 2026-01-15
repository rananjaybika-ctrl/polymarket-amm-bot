#!/usr/bin/env python3
"""
Two-Sided Grid MM Backtest V3 - CORRECT Offset Logic

AGGRESSIVE on WINNER: +0.01 (bid above best_bid, fill quickly)
PASSIVE on LOSER: -0.03 to -0.05 (bid below best_bid, wait for drop)
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Optional, Tuple
from collections import defaultdict

# =============================================================================
# CORRECT OFFSETS - From observer code
# =============================================================================
VELOCITY_ZONES = {
    'neutral':      {'vel_min': 0.00, 'vel_max': 0.05, 'pair_target': 0.97, 'winner_offset': -0.01, 'loser_offset': -0.01},
    'moderate':     {'vel_min': 0.05, 'vel_max': 0.10, 'pair_target': 0.97, 'winner_offset': -0.01, 'loser_offset': -0.02},
    'strong':       {'vel_min': 0.10, 'vel_max': 0.30, 'pair_target': 0.96, 'winner_offset':  0.00, 'loser_offset': -0.04},
    'very_strong':  {'vel_min': 0.30, 'vel_max': 0.50, 'pair_target': 0.95, 'winner_offset': +0.01, 'loser_offset': -0.03},
    'extreme':      {'vel_min': 0.50, 'vel_max': 1.00, 'pair_target': 0.94, 'winner_offset': +0.01, 'loser_offset': -0.04},
    'super_strong': {'vel_min': 1.00, 'vel_max': 99.0, 'pair_target': 0.93, 'winner_offset': +0.01, 'loser_offset': -0.05},
}

MIN_VELOCITY_BPS = 0.30
SHARES_PER_SIDE = 15


def get_zone(velocity_bps: float) -> str:
    abs_vel = abs(velocity_bps)
    for name, z in VELOCITY_ZONES.items():
        if z['vel_min'] <= abs_vel < z['vel_max']:
            return name
    return 'super_strong'


def get_offsets(velocity_bps: float) -> Tuple[float, float, str, float]:
    """
    Get offsets based on velocity direction.

    Winner = side velocity points to (UP if vel > 0, DOWN if vel < 0)
    - Winner gets POSITIVE offset (+0.01) → aggressive, fill quickly
    - Loser gets NEGATIVE offset (-0.03 to -0.05) → passive, wait for drop

    Returns: (up_offset, down_offset, zone_name, pair_target)
    """
    zone_name = get_zone(velocity_bps)
    zone = VELOCITY_ZONES[zone_name]

    if velocity_bps >= 0:
        # UP is winner, DOWN is loser
        up_offset = zone['winner_offset']   # +0.01 (aggressive)
        down_offset = zone['loser_offset']  # -0.03 to -0.05 (passive)
    else:
        # DOWN is winner, UP is loser
        up_offset = zone['loser_offset']    # passive
        down_offset = zone['winner_offset'] # aggressive

    return (up_offset, down_offset, zone_name, zone['pair_target'])


@dataclass
class Position:
    up_shares: float = 0.0
    up_cost: float = 0.0
    down_shares: float = 0.0
    down_cost: float = 0.0

    @property
    def up_avg(self) -> float:
        return self.up_cost / self.up_shares if self.up_shares > 0 else 0

    @property
    def down_avg(self) -> float:
        return self.down_cost / self.down_shares if self.down_shares > 0 else 0

    @property
    def pair_cost(self) -> float:
        if self.up_shares > 0 and self.down_shares > 0:
            return self.up_avg + self.down_avg
        return 0

    @property
    def pairs(self) -> float:
        return min(self.up_shares, self.down_shares)

    def fill(self, side: str, price: float, size: float):
        if side == "UP":
            self.up_cost += price * size
            self.up_shares += size
        else:
            self.down_cost += price * size
            self.down_shares += size


def analyze_market(market_df: pd.DataFrame, market_slug: str) -> dict:
    """
    Two-sided grid with CORRECT offsets:
    - WINNER: +0.01 offset (aggressive, bid above best_bid)
    - LOSER: -0.03 to -0.05 offset (passive, bid below best_bid)
    """
    result = {
        'market_slug': market_slug,
        'entry_triggered': False,
        'entry_time': None,
        'entry_velocity': None,
        'entry_zone': None,
        'winner_side': None,
        'up_bid_price': None,
        'down_bid_price': None,
        'up_filled': False,
        'up_fill_price': 0,
        'up_fill_time': None,
        'down_filled': False,
        'down_fill_price': 0,
        'down_fill_time': None,
        'pair_cost': 0,
        'pairs': 0,
        'market_resolution': None,
        'hedged_pnl': 0,
        'unhedged_pnl': 0,
        'total_pnl': 0,
    }

    pos = Position()
    entry_triggered = False
    up_bid_price = 0
    down_bid_price = 0

    for idx, row in market_df.iterrows():
        vel = row['velocity_bps']
        time_rem = row['time_remaining_secs']
        up_bid = row['up_bid']
        up_ask = row['up_ask']
        down_bid = row['down_bid']
        down_ask = row['down_ask']

        in_zone = abs(vel) >= MIN_VELOCITY_BPS

        # POST ORDERS when hitting zone 4-6
        if not entry_triggered and in_zone:
            entry_triggered = True
            result['entry_triggered'] = True
            result['entry_time'] = time_rem
            result['entry_velocity'] = vel
            result['entry_zone'] = get_zone(vel)
            result['winner_side'] = "UP" if vel > 0 else "DOWN"

            up_off, down_off, zone, pair_target = get_offsets(vel)

            # Calculate bid prices
            # Winner: best_bid + 0.01 (aggressive, above best bid)
            # Loser: best_bid - 0.03 (passive, below best bid)
            up_bid_price = round(up_bid + up_off, 2)
            up_bid_price = max(0.01, min(up_ask - 0.001, up_bid_price))

            down_bid_price = round(down_bid + down_off, 2)
            down_bid_price = max(0.01, min(down_ask - 0.001, down_bid_price))

            result['up_bid_price'] = up_bid_price
            result['down_bid_price'] = down_bid_price

        # CHECK FILLS - fill when ask drops to or below our bid
        if entry_triggered:
            if not result['up_filled'] and up_ask <= up_bid_price:
                result['up_filled'] = True
                result['up_fill_price'] = up_ask
                result['up_fill_time'] = time_rem
                pos.fill("UP", up_ask, SHARES_PER_SIDE)

            if not result['down_filled'] and down_ask <= down_bid_price:
                result['down_filled'] = True
                result['down_fill_price'] = down_ask
                result['down_fill_time'] = time_rem
                pos.fill("DOWN", down_ask, SHARES_PER_SIDE)

    # Market resolution
    final = market_df.iloc[-1]
    if final['up_bid'] >= 0.90:
        result['market_resolution'] = 'UP'
    elif final['down_bid'] >= 0.90:
        result['market_resolution'] = 'DOWN'
    else:
        result['market_resolution'] = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'

    # Calculate PnL
    result['pair_cost'] = pos.pair_cost
    result['pairs'] = pos.pairs

    # Hedged PnL: (1.0 - pair_cost) * pairs
    if pos.pairs > 0:
        result['hedged_pnl'] = (1.0 - pos.pair_cost) * pos.pairs

    # Unhedged PnL
    unhedged_up = pos.up_shares - pos.pairs
    unhedged_down = pos.down_shares - pos.pairs

    if result['market_resolution'] == 'UP':
        if unhedged_up > 0:
            result['unhedged_pnl'] += (1.0 - pos.up_avg) * unhedged_up
        if unhedged_down > 0:
            result['unhedged_pnl'] += (0.0 - pos.down_avg) * unhedged_down
    else:
        if unhedged_down > 0:
            result['unhedged_pnl'] += (1.0 - pos.down_avg) * unhedged_down
        if unhedged_up > 0:
            result['unhedged_pnl'] += (0.0 - pos.up_avg) * unhedged_up

    result['total_pnl'] = result['hedged_pnl'] + result['unhedged_pnl']

    return result


def main():
    print("=" * 80)
    print("TWO-SIDED GRID BACKTEST V3 - CORRECT Offsets")
    print("=" * 80)
    print("\nOFFSET LOGIC (zones 4-6):")
    print("  WINNER (favored):  +0.01 (bid ABOVE best_bid → aggressive)")
    print("  LOSER (unfavored): -0.03 to -0.05 (bid BELOW best_bid → passive)")
    print(f"\nShares per side: {SHARES_PER_SIDE}")

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

    # Analyze
    results = []
    for slug in complete:
        mdf = df[df['market_slug'] == slug].copy()
        results.append(analyze_market(mdf, slug))

    entries = [r for r in results if r['entry_triggered']]
    print(f"Markets with zone 4-6 entry: {len(entries)}")

    # Detailed output
    print("\n" + "=" * 80)
    print("TRADE DETAILS")
    print("=" * 80)

    total_hedged = 0
    total_unhedged = 0
    both_filled = 0
    winner_only = 0
    loser_only = 0
    none_filled = 0

    correct_predictions = 0

    for i, r in enumerate(entries):
        winner = r['winner_side']
        loser = "DOWN" if winner == "UP" else "UP"

        winner_filled = r['up_filled'] if winner == "UP" else r['down_filled']
        loser_filled = r['down_filled'] if winner == "UP" else r['up_filled']
        winner_price = r['up_fill_price'] if winner == "UP" else r['down_fill_price']
        loser_price = r['down_fill_price'] if winner == "UP" else r['up_fill_price']
        winner_bid = r['up_bid_price'] if winner == "UP" else r['down_bid_price']
        loser_bid = r['down_bid_price'] if winner == "UP" else r['up_bid_price']

        prediction_correct = (winner == r['market_resolution'])
        if prediction_correct:
            correct_predictions += 1

        print(f"\n[{i+1}] {r['market_slug']}")
        print(f"    Velocity: {r['entry_velocity']:.4f} bps → Winner={winner}")
        print(f"    {winner} bid: ${winner_bid:.2f} (aggressive) → {'FILLED @ $'+f'{winner_price:.2f}' if winner_filled else 'NO FILL'}")
        print(f"    {loser} bid: ${loser_bid:.2f} (passive) → {'FILLED @ $'+f'{loser_price:.2f}' if loser_filled else 'NO FILL'}")

        if r['up_filled'] and r['down_filled']:
            print(f"    Pair cost: ${r['pair_cost']:.4f}")
            both_filled += 1
        elif winner_filled and not loser_filled:
            winner_only += 1
        elif loser_filled and not winner_filled:
            loser_only += 1
        else:
            none_filled += 1

        print(f"    Resolution: {r['market_resolution']} (prediction {'CORRECT' if prediction_correct else 'WRONG'})")
        print(f"    PnL: ${r['total_pnl']:.2f}")

        total_hedged += r['hedged_pnl']
        total_unhedged += r['unhedged_pnl']

    total_pnl = total_hedged + total_unhedged

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    print(f"\nPrediction accuracy: {correct_predictions}/{len(entries)} ({100*correct_predictions/len(entries):.1f}%)")

    print(f"\nFill breakdown:")
    print(f"  Both filled (hedged): {both_filled} ({100*both_filled/len(entries):.1f}%)")
    print(f"  Winner only: {winner_only} ({100*winner_only/len(entries):.1f}%)")
    print(f"  Loser only: {loser_only} ({100*loser_only/len(entries):.1f}%)")
    print(f"  Neither: {none_filled} ({100*none_filled/len(entries):.1f}%)")

    hedged_markets = [r for r in entries if r['pairs'] > 0]
    if hedged_markets:
        avg_pair_cost = sum(r['pair_cost'] for r in hedged_markets) / len(hedged_markets)
        print(f"\nAvg pair cost: ${avg_pair_cost:.4f}")

    print(f"\n--- PnL ---")
    print(f"Hedged PnL: ${total_hedged:.2f}")
    print(f"Unhedged PnL: ${total_unhedged:.2f}")
    print(f"TOTAL PnL: ${total_pnl:.2f}")

    hours = len(entries) * 15 / 60
    print(f"Hourly: ${total_pnl/hours:.2f}/hr")

    # Compare
    print("\n" + "-" * 80)
    print("COMPARISON")
    print("-" * 80)
    print(f"\n| Strategy | Hedge Rate | PnL |")
    print(f"|----------|------------|-----|")
    print(f"| Sequential (entry→hedge) | 77.8% | -$8.25 |")
    print(f"| Two-Sided Grid (correct) | {100*both_filled/len(entries):.1f}% | ${total_pnl:.2f} |")


if __name__ == "__main__":
    main()
