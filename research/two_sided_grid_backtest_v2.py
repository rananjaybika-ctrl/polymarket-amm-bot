#!/usr/bin/env python3
"""
Two-Sided Grid MM Backtest V2 - Proper Fill Simulation

Key fix: Track resting orders and fill when ask drops to our bid level.
Passive MM = post bid below market, wait for price to come to us.
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
from collections import defaultdict

# =============================================================================
# CONFIGURATION - From plan clever-mixing-lollipop.md
# =============================================================================

# When velocity > 0 (UP favored): widen UP offset (bid lower), tighten DOWN (bid higher)
# When velocity < 0 (DOWN favored): swap offsets
VELOCITY_ZONES = {
    'neutral':      {'vel_min': 0.00, 'vel_max': 0.05, 'favored_offset': -0.01, 'unfavored_offset': -0.01},
    'moderate':     {'vel_min': 0.05, 'vel_max': 0.10, 'favored_offset': -0.01, 'unfavored_offset': -0.02},
    'strong':       {'vel_min': 0.10, 'vel_max': 0.30, 'favored_offset': -0.02, 'unfavored_offset': -0.01},
    'very_strong':  {'vel_min': 0.30, 'vel_max': 0.50, 'favored_offset': -0.03, 'unfavored_offset': +0.00},
    'extreme':      {'vel_min': 0.50, 'vel_max': 1.00, 'favored_offset': -0.04, 'unfavored_offset': +0.01},
    'super_strong': {'vel_min': 1.00, 'vel_max': 99.0, 'favored_offset': -0.05, 'unfavored_offset': +0.02},
}

MIN_VELOCITY_BPS = 0.30
SHARES_PER_SIDE = 15


def get_zone(velocity_bps: float) -> str:
    abs_vel = abs(velocity_bps)
    for name, z in VELOCITY_ZONES.items():
        if z['vel_min'] <= abs_vel < z['vel_max']:
            return name
    return 'super_strong'


def get_offsets(velocity_bps: float) -> Tuple[float, float, str]:
    """
    Get offsets based on velocity.

    Favored side = side velocity points to (UP if vel > 0, DOWN if vel < 0)
    - Favored side: WIDEN offset (bid lower) - expect price to rise
    - Unfavored side: TIGHTEN offset (bid higher) - expect price to fall (cheap fill)
    """
    zone_name = get_zone(velocity_bps)
    zone = VELOCITY_ZONES[zone_name]

    if velocity_bps >= 0:
        # UP is favored
        up_offset = zone['favored_offset']    # WIDEN (lower bid)
        down_offset = zone['unfavored_offset']  # TIGHTEN (higher bid)
    else:
        # DOWN is favored
        up_offset = zone['unfavored_offset']  # TIGHTEN
        down_offset = zone['favored_offset']  # WIDEN

    return (up_offset, down_offset, zone_name)


@dataclass
class RestingOrder:
    """A resting limit order."""
    side: str
    bid_price: float
    size: float
    posted_time: float  # time_remaining when posted
    filled: bool = False
    fill_price: float = 0.0
    fill_time: float = 0.0


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
    Simulate two-sided grid strategy for one market.

    Flow:
    1. Wait for velocity >= 0.30 bps (zone 4-6)
    2. Post bids on BOTH sides using offset formula
    3. Track resting orders - fill when ask drops to bid level
    4. At end, calculate PnL including resolution for unhedged
    """
    result = {
        'market_slug': market_slug,
        'samples': len(market_df),
        'entry_triggered': False,
        'entry_time': None,
        'entry_velocity': None,
        'entry_zone': None,
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

    # Order tracking
    up_order: Optional[RestingOrder] = None
    down_order: Optional[RestingOrder] = None
    entry_triggered = False

    for idx, row in market_df.iterrows():
        vel = row['velocity_bps']
        time_rem = row['time_remaining_secs']
        up_bid = row['up_bid']
        up_ask = row['up_ask']
        down_bid = row['down_bid']
        down_ask = row['down_ask']

        # Check for zone 4-6 entry
        in_zone = abs(vel) >= MIN_VELOCITY_BPS

        # POST ORDERS when first hitting zone 4-6
        if not entry_triggered and in_zone:
            entry_triggered = True
            result['entry_triggered'] = True
            result['entry_time'] = time_rem
            result['entry_velocity'] = vel
            result['entry_zone'] = get_zone(vel)

            up_off, down_off, zone = get_offsets(vel)

            # Calculate bid prices
            up_bid_price = round(up_bid + up_off, 2)
            up_bid_price = max(0.01, min(0.95, up_bid_price))

            down_bid_price = round(down_bid + down_off, 2)
            down_bid_price = max(0.01, min(0.95, down_bid_price))

            result['up_bid_price'] = up_bid_price
            result['down_bid_price'] = down_bid_price

            up_order = RestingOrder("UP", up_bid_price, SHARES_PER_SIDE, time_rem)
            down_order = RestingOrder("DOWN", down_bid_price, SHARES_PER_SIDE, time_rem)

        # CHECK FOR FILLS on resting orders
        # Fill when ask drops to or below our bid
        if up_order and not up_order.filled:
            if up_ask <= up_order.bid_price:
                up_order.filled = True
                up_order.fill_price = up_ask  # Fill at the ask
                up_order.fill_time = time_rem
                pos.fill("UP", up_ask, up_order.size)
                result['up_filled'] = True
                result['up_fill_price'] = up_ask
                result['up_fill_time'] = time_rem

        if down_order and not down_order.filled:
            if down_ask <= down_order.bid_price:
                down_order.filled = True
                down_order.fill_price = down_ask
                down_order.fill_time = time_rem
                pos.fill("DOWN", down_ask, down_order.size)
                result['down_filled'] = True
                result['down_fill_price'] = down_ask
                result['down_fill_time'] = time_rem

        # OPTIONAL: Reprice orders based on current velocity
        # (Keep same price for now - pure passive strategy)

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

    # Hedged PnL
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
    print("TWO-SIDED GRID MM BACKTEST V2 - Passive Fill Simulation")
    print("=" * 80)
    print("\nStrategy: Post bids on BOTH sides when velocity >= 0.30 bps")
    print("Fill logic: When ask drops to our bid level, we fill at ask")
    print(f"Shares per side: {SHARES_PER_SIDE}")

    # Show offset table
    print("\n--- OFFSET CONFIGURATION ---")
    print("When velocity > 0 (UP favored):")
    for zone, cfg in VELOCITY_ZONES.items():
        if cfg['vel_min'] >= 0.30:
            print(f"  {zone}: UP_offset={cfg['favored_offset']:+.2f} (wide), DOWN_offset={cfg['unfavored_offset']:+.2f} (tight)")

    # Load data
    df = pd.read_csv('/Users/rananjaybika/polymarket-amm-bot/research/observer/spread_capture_obs_20260115.csv',
                     on_bad_lines='skip')
    print(f"\nTotal samples: {len(df):,}")

    markets = df['market_slug'].unique()
    print(f"Total markets: {len(markets)}")

    # Filter complete markets
    complete = []
    for slug in markets:
        mdf = df[df['market_slug'] == slug]
        if mdf.iloc[0]['time_remaining_secs'] >= 800 and mdf.iloc[-1]['time_remaining_secs'] <= 60:
            complete.append(slug)

    print(f"Complete markets (full 15 min): {len(complete)}")

    # Analyze
    results = []
    for slug in complete:
        mdf = df[df['market_slug'] == slug].copy()
        results.append(analyze_market(mdf, slug))

    entries = [r for r in results if r['entry_triggered']]
    print(f"\nMarkets with zone 4-6 entry: {len(entries)}")

    if not entries:
        print("NO ENTRIES!")
        return

    # Detailed output
    print("\n" + "=" * 80)
    print("TRADE-BY-TRADE DETAILS")
    print("=" * 80)

    total_hedged = 0
    total_unhedged = 0
    total_pnl = 0
    both_filled = 0
    one_filled = 0
    none_filled = 0

    for i, r in enumerate(entries):
        print(f"\n[{i+1}] {r['market_slug']}")
        print(f"    Entry: vel={r['entry_velocity']:.4f} bps, zone={r['entry_zone']}, t={r['entry_time']:.0f}s")
        up_status = f"FILLED @ ${r['up_fill_price']:.2f}" if r['up_filled'] else "NO FILL"
        down_status = f"FILLED @ ${r['down_fill_price']:.2f}" if r['down_filled'] else "NO FILL"
        print(f"    UP bid: ${r['up_bid_price']:.2f} -> {up_status}")
        print(f"    DOWN bid: ${r['down_bid_price']:.2f} -> {down_status}")

        if r['up_filled'] and r['down_filled']:
            print(f"    Pair cost: ${r['pair_cost']:.4f}")
            both_filled += 1
        elif r['up_filled'] or r['down_filled']:
            one_filled += 1
        else:
            none_filled += 1

        print(f"    Resolution: {r['market_resolution']}")
        print(f"    PnL: Hedged=${r['hedged_pnl']:.2f}, Unhedged=${r['unhedged_pnl']:.2f}, Total=${r['total_pnl']:.2f}")

        total_hedged += r['hedged_pnl']
        total_unhedged += r['unhedged_pnl']
        total_pnl += r['total_pnl']

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    print(f"\nFill rates:")
    print(f"  Both sides filled: {both_filled}/{len(entries)} ({100*both_filled/len(entries):.1f}%)")
    print(f"  One side filled: {one_filled}/{len(entries)} ({100*one_filled/len(entries):.1f}%)")
    print(f"  No fills: {none_filled}/{len(entries)} ({100*none_filled/len(entries):.1f}%)")

    hedged_markets = [r for r in entries if r['pairs'] > 0]
    if hedged_markets:
        avg_pair_cost = sum(r['pair_cost'] for r in hedged_markets) / len(hedged_markets)
        print(f"\nAvg pair cost (hedged): ${avg_pair_cost:.4f}")

    print(f"\n--- PnL ---")
    print(f"Hedged PnL: ${total_hedged:.2f}")
    print(f"Unhedged PnL: ${total_unhedged:.2f}")
    print(f"TOTAL PnL: ${total_pnl:.2f}")

    hours = len(entries) * 15 / 60
    print(f"\nHourly PnL: ${total_pnl/hours:.2f}/hr")

    # Compare with sequential
    print("\n" + "-" * 80)
    print("COMPARISON WITH SEQUENTIAL STRATEGY")
    print("-" * 80)
    print(f"\n| Metric | Sequential | Two-Sided Grid |")
    print(f"|--------|------------|----------------|")
    print(f"| Total PnL | $-8.25 | ${total_pnl:.2f} |")
    print(f"| Hedge rate | 77.8% | {100*both_filled/len(entries):.1f}% |")
    print(f"| Avg PnL/market | $-0.23 | ${total_pnl/len(entries):.2f} |")


if __name__ == "__main__":
    main()
