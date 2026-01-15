#!/usr/bin/env python3
"""
Two-Sided Grid MM Backtest - Jan 15, 2026 Data

Strategy: When velocity >= 0.30 bps (zones 4-6), post bids on BOTH sides simultaneously.
Velocity adjusts offsets (widen unfavorable, tighten favorable).

Key differences from sequential:
- Post BOTH sides at same time (not entry then hedge)
- No timeout aborts - keep orders until filled or market ends
- Velocity biases offsets, not gates entry
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
from collections import defaultdict

# =============================================================================
# CONFIGURATION
# =============================================================================

# Velocity zones with TWO-SIDED offsets
# When velocity > 0 (UP favored): widen UP offset (bid lower), tighten DOWN (bid higher)
# When velocity < 0 (DOWN favored): widen DOWN offset, tighten UP offset
VELOCITY_ZONES = {
    'neutral':      {'vel_min': 0.00, 'vel_max': 0.05, 'up_offset': -0.01, 'down_offset': -0.01, 'pair_target': 0.97},
    'moderate':     {'vel_min': 0.05, 'vel_max': 0.10, 'up_offset': -0.01, 'down_offset': -0.02, 'pair_target': 0.97},
    'strong':       {'vel_min': 0.10, 'vel_max': 0.30, 'up_offset': -0.02, 'down_offset': -0.01, 'pair_target': 0.96},
    'very_strong':  {'vel_min': 0.30, 'vel_max': 0.50, 'up_offset': -0.03, 'down_offset': +0.00, 'pair_target': 0.95},
    'extreme':      {'vel_min': 0.50, 'vel_max': 1.00, 'up_offset': -0.04, 'down_offset': +0.01, 'pair_target': 0.94},
    'super_strong': {'vel_min': 1.00, 'vel_max': 99.0, 'up_offset': -0.05, 'down_offset': +0.02, 'pair_target': 0.93},
}

MIN_VELOCITY_BPS = 0.30  # Only trade in zones 4-6
SHARES_PER_SIDE = 15     # 15 shares on each side
GRID_LEVELS = 3          # 3 price levels per side
SHARES_PER_LEVEL = 5     # 5 shares per level


@dataclass
class GridOrder:
    """A single grid order."""
    side: str  # "UP" or "DOWN"
    price: float
    size: float
    filled: bool = False
    fill_price: float = 0.0


@dataclass
class TwoSidedPosition:
    """Track position from two-sided grid fills."""
    up_shares: float = 0.0
    up_cost: float = 0.0
    down_shares: float = 0.0
    down_cost: float = 0.0

    @property
    def up_avg_price(self) -> float:
        return self.up_cost / self.up_shares if self.up_shares > 0 else 0.0

    @property
    def down_avg_price(self) -> float:
        return self.down_cost / self.down_shares if self.down_shares > 0 else 0.0

    @property
    def pair_cost(self) -> float:
        if self.up_shares > 0 and self.down_shares > 0:
            return self.up_avg_price + self.down_avg_price
        return 0.0

    @property
    def pairs(self) -> float:
        return min(self.up_shares, self.down_shares)

    @property
    def imbalance(self) -> float:
        return abs(self.up_shares - self.down_shares)

    def add_fill(self, side: str, price: float, size: float):
        if side == "UP":
            self.up_cost += price * size
            self.up_shares += size
        else:
            self.down_cost += price * size
            self.down_shares += size


def get_zone(velocity_bps: float) -> str:
    """Get velocity zone name."""
    abs_vel = abs(velocity_bps)
    for name, z in VELOCITY_ZONES.items():
        if z['vel_min'] <= abs_vel < z['vel_max']:
            return name
    return 'super_strong'


def get_offsets(velocity_bps: float) -> Tuple[float, float, str]:
    """
    Get UP and DOWN offsets based on velocity.

    When velocity > 0 (BTC rising, UP favored):
        - Widen UP offset (bid lower - expect UP to get expensive)
        - Tighten DOWN offset (bid higher - expect DOWN to get cheap)

    When velocity < 0 (BTC falling, DOWN favored):
        - Swap the offsets

    Returns: (up_offset, down_offset, zone_name)
    """
    zone_name = get_zone(velocity_bps)
    zone = VELOCITY_ZONES[zone_name]

    if velocity_bps >= 0:
        # UP is favored - use config as-is
        return (zone['up_offset'], zone['down_offset'], zone_name)
    else:
        # DOWN is favored - swap offsets
        return (zone['down_offset'], zone['up_offset'], zone_name)


def would_fill(our_bid: float, best_ask: float) -> bool:
    """Check if our bid would fill (at or above ask)."""
    return our_bid >= best_ask


def analyze_market_two_sided(market_df: pd.DataFrame, market_slug: str) -> dict:
    """
    Analyze a single market using two-sided grid strategy.

    Strategy:
    1. When velocity >= 0.30 bps, post bids on BOTH sides
    2. Offsets adjusted based on velocity direction
    3. Track fills on both sides
    4. At market end, calculate PnL including resolution
    """
    result = {
        'market_slug': market_slug,
        'samples': len(market_df),
        'start_time_remaining': market_df.iloc[0]['time_remaining_secs'],
        'end_time_remaining': market_df.iloc[-1]['time_remaining_secs'],

        # Entry tracking
        'entry_triggered': False,
        'entry_time_remaining': None,
        'entry_velocity': None,
        'entry_zone': None,

        # Fill tracking
        'up_fills': [],
        'down_fills': [],
        'up_total_shares': 0.0,
        'down_total_shares': 0.0,
        'up_avg_price': 0.0,
        'down_avg_price': 0.0,

        # Final state
        'pair_cost': 0.0,
        'pairs': 0.0,
        'imbalance': 0.0,
        'market_resolution': None,

        # PnL
        'hedged_pnl': 0.0,
        'unhedged_pnl': 0.0,
        'total_pnl': 0.0,
    }

    pos = TwoSidedPosition()

    # Track if we've entered (posted orders)
    entry_triggered = False
    entry_velocity_dir = None

    # Pending orders (not yet filled)
    up_bid_price = 0.0
    down_bid_price = 0.0
    up_order_posted = False
    down_order_posted = False

    for idx, row in market_df.iterrows():
        velocity_bps = row['velocity_bps']
        up_bid = row['up_bid']
        up_ask = row['up_ask']
        down_bid = row['down_bid']
        down_ask = row['down_ask']
        time_remaining = row['time_remaining_secs']

        # Check if velocity is in zone 4-6
        in_tradeable_zone = abs(velocity_bps) >= MIN_VELOCITY_BPS

        # ENTRY: Post both sides when velocity hits zone 4-6
        if not entry_triggered and in_tradeable_zone:
            entry_triggered = True
            entry_velocity_dir = "UP" if velocity_bps > 0 else "DOWN"
            result['entry_triggered'] = True
            result['entry_time_remaining'] = time_remaining
            result['entry_velocity'] = velocity_bps
            result['entry_zone'] = get_zone(velocity_bps)

        # Update bid prices based on current velocity (continuous adjustment)
        if entry_triggered:
            up_offset, down_offset, zone = get_offsets(velocity_bps)

            # Calculate bid prices
            # UP bid = best_up_bid + up_offset
            # DOWN bid = best_down_bid + down_offset
            new_up_bid = up_bid + up_offset
            new_up_bid = max(0.01, min(up_ask - 0.001, new_up_bid))  # Stay below ask

            new_down_bid = down_bid + down_offset
            new_down_bid = max(0.01, min(down_ask - 0.001, new_down_bid))

            # Check for UP fill
            if not up_order_posted or new_up_bid != up_bid_price:
                up_bid_price = new_up_bid
                up_order_posted = True

            if up_order_posted and would_fill(up_bid_price, up_ask):
                # Fill at ask price
                fill_price = up_ask
                fill_size = SHARES_PER_SIDE - pos.up_shares
                if fill_size > 0:
                    pos.add_fill("UP", fill_price, fill_size)
                    result['up_fills'].append({
                        'time_remaining': time_remaining,
                        'price': fill_price,
                        'size': fill_size,
                        'velocity': velocity_bps,
                        'zone': zone
                    })

            # Check for DOWN fill
            if not down_order_posted or new_down_bid != down_bid_price:
                down_bid_price = new_down_bid
                down_order_posted = True

            if down_order_posted and would_fill(down_bid_price, down_ask):
                # Fill at ask price
                fill_price = down_ask
                fill_size = SHARES_PER_SIDE - pos.down_shares
                if fill_size > 0:
                    pos.add_fill("DOWN", fill_price, fill_size)
                    result['down_fills'].append({
                        'time_remaining': time_remaining,
                        'price': fill_price,
                        'size': fill_size,
                        'velocity': velocity_bps,
                        'zone': zone
                    })

    # Market resolution
    final_row = market_df.iloc[-1]
    if final_row['up_bid'] >= 0.90:
        result['market_resolution'] = 'UP'
    elif final_row['down_bid'] >= 0.90:
        result['market_resolution'] = 'DOWN'
    else:
        # Use higher bid
        result['market_resolution'] = 'UP' if final_row['up_bid'] > final_row['down_bid'] else 'DOWN'

    # Calculate final position stats
    result['up_total_shares'] = pos.up_shares
    result['down_total_shares'] = pos.down_shares
    result['up_avg_price'] = pos.up_avg_price
    result['down_avg_price'] = pos.down_avg_price
    result['pair_cost'] = pos.pair_cost
    result['pairs'] = pos.pairs
    result['imbalance'] = pos.imbalance

    # Calculate PnL
    # Hedged pairs: profit = (1.0 - pair_cost) * pairs
    if pos.pairs > 0:
        result['hedged_pnl'] = (1.0 - pos.pair_cost) * pos.pairs

    # Unhedged shares: depends on resolution
    unhedged_up = pos.up_shares - pos.pairs
    unhedged_down = pos.down_shares - pos.pairs

    if result['market_resolution'] == 'UP':
        # UP wins ($1), DOWN loses ($0)
        if unhedged_up > 0:
            result['unhedged_pnl'] += (1.0 - pos.up_avg_price) * unhedged_up
        if unhedged_down > 0:
            result['unhedged_pnl'] += (0.0 - pos.down_avg_price) * unhedged_down
    else:
        # DOWN wins ($1), UP loses ($0)
        if unhedged_down > 0:
            result['unhedged_pnl'] += (1.0 - pos.down_avg_price) * unhedged_down
        if unhedged_up > 0:
            result['unhedged_pnl'] += (0.0 - pos.up_avg_price) * unhedged_up

    result['total_pnl'] = result['hedged_pnl'] + result['unhedged_pnl']

    return result


def main():
    print("=" * 80)
    print("TWO-SIDED GRID MM BACKTEST - Jan 15, 2026 Data")
    print("=" * 80)
    print("\nStrategy: Post bids on BOTH sides when velocity >= 0.30 bps")
    print("Offsets: Velocity-adjusted (widen unfavorable, tighten favorable)")
    print(f"Shares per side: {SHARES_PER_SIDE}")

    # Load data
    df = pd.read_csv('/Users/rananjaybika/polymarket-amm-bot/research/observer/spread_capture_obs_20260115.csv',
                     on_bad_lines='skip')
    print(f"\nTotal samples: {len(df):,}")

    # Get unique markets
    markets = df['market_slug'].unique()
    print(f"Total markets: {len(markets)}")

    # Filter for complete markets
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
        result = analyze_market_two_sided(market_df, market_slug)
        results.append(result)

    # Filter for markets where we entered
    entries = [r for r in results if r['entry_triggered']]

    print(f"\n" + "=" * 80)
    print("MARKETS WITH ZONE 4-6 ENTRIES (TWO-SIDED GRID)")
    print("=" * 80)
    print(f"Markets with entries: {len(entries)}")

    if len(entries) == 0:
        print("\nNO ENTRIES - velocity never hit 0.30 bps in complete markets")
        return

    # Detailed breakdown
    print("\n" + "-" * 80)
    print("TRADE-BY-TRADE BREAKDOWN")
    print("-" * 80)

    total_hedged_pnl = 0
    total_unhedged_pnl = 0
    total_pnl = 0
    fully_hedged_count = 0
    partially_hedged_count = 0
    no_hedge_count = 0

    for i, r in enumerate(entries):
        print(f"\n[Market {i+1}] {r['market_slug']}")
        print(f"  Entry: velocity={r['entry_velocity']:.4f} bps (zone: {r['entry_zone']})")
        print(f"  Entry time: {r['entry_time_remaining']:.0f}s remaining")

        print(f"\n  UP FILLS: {len(r['up_fills'])}")
        for f in r['up_fills']:
            print(f"    - {f['size']:.0f} shares @ ${f['price']:.4f} (t={f['time_remaining']:.0f}s, zone={f['zone']})")
        print(f"  UP Total: {r['up_total_shares']:.0f} shares @ avg ${r['up_avg_price']:.4f}")

        print(f"\n  DOWN FILLS: {len(r['down_fills'])}")
        for f in r['down_fills']:
            print(f"    - {f['size']:.0f} shares @ ${f['price']:.4f} (t={f['time_remaining']:.0f}s, zone={f['zone']})")
        print(f"  DOWN Total: {r['down_total_shares']:.0f} shares @ avg ${r['down_avg_price']:.4f}")

        print(f"\n  POSITION:")
        print(f"    Pairs: {r['pairs']:.0f}")
        print(f"    Pair cost: ${r['pair_cost']:.4f}" if r['pair_cost'] > 0 else "    Pair cost: N/A")
        print(f"    Imbalance: {r['imbalance']:.0f} shares")
        print(f"    Resolution: {r['market_resolution']}")

        print(f"\n  PnL BREAKDOWN:")
        print(f"    Hedged PnL: ${r['hedged_pnl']:.2f}")
        print(f"    Unhedged PnL: ${r['unhedged_pnl']:.2f}")
        print(f"    TOTAL PnL: ${r['total_pnl']:.2f}")

        total_hedged_pnl += r['hedged_pnl']
        total_unhedged_pnl += r['unhedged_pnl']
        total_pnl += r['total_pnl']

        if r['pairs'] == SHARES_PER_SIDE:
            fully_hedged_count += 1
        elif r['pairs'] > 0:
            partially_hedged_count += 1
        else:
            no_hedge_count += 1

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY - TWO-SIDED GRID STRATEGY")
    print("=" * 80)

    print(f"\nTotal markets traded: {len(entries)}")
    print(f"Fully hedged (15/15 pairs): {fully_hedged_count}")
    print(f"Partially hedged: {partially_hedged_count}")
    print(f"No hedge: {no_hedge_count}")

    hedge_rate = sum(1 for r in entries if r['pairs'] > 0) / len(entries) * 100
    avg_pairs = sum(r['pairs'] for r in entries) / len(entries)
    avg_pair_cost = sum(r['pair_cost'] for r in entries if r['pair_cost'] > 0) / max(1, sum(1 for r in entries if r['pair_cost'] > 0))

    print(f"\nHedge rate: {hedge_rate:.1f}%")
    print(f"Avg pairs per market: {avg_pairs:.1f}")
    print(f"Avg pair cost: ${avg_pair_cost:.4f}")

    print(f"\n--- PnL ---")
    print(f"Total Hedged PnL: ${total_hedged_pnl:.2f}")
    print(f"Total Unhedged PnL: ${total_unhedged_pnl:.2f}")
    print(f"TOTAL PnL: ${total_pnl:.2f}")

    hours = len(entries) * 15 / 60  # 15 min per market
    print(f"\nEquivalent hours: {hours:.1f}")
    print(f"Hourly PnL: ${total_pnl / hours:.2f}/hr")

    # Comparison with sequential strategy
    print("\n" + "-" * 80)
    print("COMPARISON: TWO-SIDED vs SEQUENTIAL")
    print("-" * 80)

    # Load sequential results (from earlier analysis)
    sequential_pnl = -8.25  # From dynamic_tightening_analysis.py
    sequential_markets = 36

    print(f"\n| Metric | Sequential | Two-Sided |")
    print(f"|--------|------------|-----------|")
    print(f"| Markets traded | {sequential_markets} | {len(entries)} |")
    print(f"| Total PnL | ${sequential_pnl:.2f} | ${total_pnl:.2f} |")
    print(f"| Avg PnL/market | ${sequential_pnl/sequential_markets:.2f} | ${total_pnl/len(entries):.2f} |")
    print(f"| Hedge rate | 77.8% | {hedge_rate:.1f}% |")

    # Zone breakdown
    print("\n" + "-" * 80)
    print("BY ENTRY ZONE")
    print("-" * 80)

    for zone in ['very_strong', 'extreme', 'super_strong']:
        zone_entries = [r for r in entries if r['entry_zone'] == zone]
        if zone_entries:
            zone_pnl = sum(r['total_pnl'] for r in zone_entries)
            zone_pairs = sum(r['pairs'] for r in zone_entries)
            zone_hedge_rate = sum(1 for r in zone_entries if r['pairs'] > 0) / len(zone_entries) * 100
            print(f"\n{zone}:")
            print(f"  Markets: {len(zone_entries)}")
            print(f"  Total pairs: {zone_pairs:.0f}")
            print(f"  Hedge rate: {zone_hedge_rate:.1f}%")
            print(f"  PnL: ${zone_pnl:.2f}")


if __name__ == "__main__":
    main()
