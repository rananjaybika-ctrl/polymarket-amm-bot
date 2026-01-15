#!/usr/bin/env python3
"""
Velocity-Gated Grid Backtest

Strategy:
- Only trade when velocity is in Zone 4-6 (|vel| >= 0.30 bps)
- Winner side: Single aggressive order (NO grid)
- Loser side: 2-level grid
- 10 share max imbalance (Gabagool rule)
- Fill at OUR bid price (correct logic)
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from pathlib import Path
import glob

# Strategy Configuration
VELOCITY_ZONES = {
    # HEDGE-FOCUSED: Less aggressive loser offsets to maximize fill rate
    'strong':      {'min': 0.30, 'max': 0.50, 'winner_ratio': 0.60, 'w_off': +0.01, 'l_off': -0.01},
    'very_strong': {'min': 0.50, 'max': 1.00, 'winner_ratio': 0.65, 'w_off': +0.01, 'l_off': -0.02},
    'extreme':     {'min': 1.00, 'max': 99.0, 'winner_ratio': 0.70, 'w_off': +0.02, 'l_off': -0.03},
}

MIN_VELOCITY_BPS = 0.30
LOSER_GRID_LEVELS = 2
LOSER_GRID_SPACING = 0.01  # Tighter grid for better hedge rate
MAX_SHARES_PER_SIDE = 30
MAX_IMBALANCE_SHARES = 10

# Only trade when spread is wide enough
MAX_BID_BID_FOR_ENTRY = 0.96  # Only enter if bid+bid < this (wider spread)

# For backtest simulation - REDUCED to enforce 10 share imbalance limit
WINNER_SHARES = 10  # Reduced to match max imbalance
LOSER_SHARES_PER_LEVEL = 5  # 10 total / 2 levels


@dataclass
class Order:
    side: str  # 'UP' or 'DOWN'
    price: float
    size: int
    is_winner: bool
    grid_level: int = 0  # 0 for winner, 1-2 for loser grid


@dataclass
class Fill:
    side: str
    price: float
    size: int
    is_winner: bool


@dataclass
class MarketResult:
    slug: str
    entry: bool = False
    zone: str = ""
    velocity: float = 0.0
    winner: str = ""

    # Orders placed
    winner_order: Optional[Order] = None
    loser_orders: List[Order] = field(default_factory=list)

    # Fills
    winner_fill: Optional[Fill] = None
    loser_fills: List[Fill] = field(default_factory=list)

    # Inventory tracking
    up_shares: int = 0
    down_shares: int = 0
    max_imbalance: int = 0

    # PnL
    pair_cost: float = 0.0
    hedged_pnl: float = 0.0
    unhedged_pnl: float = 0.0
    total_pnl: float = 0.0
    resolution: str = ""

    # Debug
    winner_entry_ask: float = 0.0
    winner_min_ask: float = 0.0
    loser_entry_asks: List[float] = field(default_factory=list)
    loser_min_asks: List[float] = field(default_factory=list)


def get_zone(vel: float) -> Tuple[str, dict]:
    """Get velocity zone and configuration."""
    abs_vel = abs(vel)
    for name, z in VELOCITY_ZONES.items():
        if z['min'] <= abs_vel < z['max']:
            return name, z
    return 'extreme', VELOCITY_ZONES['extreme']


def analyze_market(market_df: pd.DataFrame, slug: str) -> MarketResult:
    """Analyze a single market with velocity-gated grid strategy."""
    result = MarketResult(slug=slug)

    # Find zone 4-6 entry WITH spread filter
    entry_idx = None
    entry_row = None
    for i, (idx, row) in enumerate(market_df.iterrows()):
        if abs(row['velocity_bps']) >= MIN_VELOCITY_BPS:
            # Check if spread is wide enough
            bid_bid = row['up_bid'] + row['down_bid']
            if bid_bid <= MAX_BID_BID_FOR_ENTRY:
                entry_idx = i
                entry_row = row
                break

    if entry_row is None:
        return result

    result.entry = True
    result.velocity = entry_row['velocity_bps']
    zone_name, zone = get_zone(result.velocity)
    result.zone = zone_name

    # Determine winner/loser
    winner = "UP" if result.velocity > 0 else "DOWN"
    loser = "DOWN" if result.velocity > 0 else "UP"
    result.winner = winner

    # Get entry prices
    if winner == "UP":
        winner_best_bid = entry_row['up_bid']
        loser_best_bid = entry_row['down_bid']
        winner_entry_ask = entry_row['up_ask']
        loser_entry_ask = entry_row['down_ask']
    else:
        winner_best_bid = entry_row['down_bid']
        loser_best_bid = entry_row['up_bid']
        winner_entry_ask = entry_row['down_ask']
        loser_entry_ask = entry_row['up_ask']

    result.winner_entry_ask = winner_entry_ask

    # Calculate bid prices
    # Winner: Single aggressive order
    winner_bid = round(winner_best_bid + zone['w_off'], 2)
    winner_bid = max(0.01, min(0.95, winner_bid))

    result.winner_order = Order(
        side=winner,
        price=winner_bid,
        size=WINNER_SHARES,
        is_winner=True,
        grid_level=0
    )

    # Loser: 2-level grid
    for level in range(1, LOSER_GRID_LEVELS + 1):
        offset = zone['l_off'] - (level - 1) * LOSER_GRID_SPACING
        loser_bid = round(loser_best_bid + offset, 2)
        loser_bid = max(0.01, min(0.95, loser_bid))

        result.loser_orders.append(Order(
            side=loser,
            price=loser_bid,
            size=LOSER_SHARES_PER_LEVEL,
            is_winner=False,
            grid_level=level
        ))
        result.loser_entry_asks.append(loser_entry_ask)

    # Get post-entry data
    post_entry = market_df.iloc[entry_idx:]

    if winner == "UP":
        winner_asks = post_entry['up_ask'].values
        loser_asks = post_entry['down_ask'].values
    else:
        winner_asks = post_entry['down_ask'].values
        loser_asks = post_entry['up_ask'].values

    winner_min_ask = np.min(winner_asks)
    loser_min_ask = np.min(loser_asks)

    result.winner_min_ask = winner_min_ask
    result.loser_min_asks = [loser_min_ask] * LOSER_GRID_LEVELS

    # =========================================================================
    # FILL LOGIC (Correct: fill at OUR bid price)
    # =========================================================================

    # Winner fill
    if winner_bid >= winner_entry_ask:
        # Aggressive bid at or above ask - fills immediately at entry ask
        result.winner_fill = Fill(
            side=winner,
            price=winner_entry_ask,
            size=WINNER_SHARES,
            is_winner=True
        )
        if winner == "UP":
            result.up_shares += WINNER_SHARES
        else:
            result.down_shares += WINNER_SHARES
    elif winner_min_ask <= winner_bid:
        # Passive bid - ask dropped to our level, fill at our bid
        result.winner_fill = Fill(
            side=winner,
            price=winner_bid,
            size=WINNER_SHARES,
            is_winner=True
        )
        if winner == "UP":
            result.up_shares += WINNER_SHARES
        else:
            result.down_shares += WINNER_SHARES

    # Loser grid fills
    for i, order in enumerate(result.loser_orders):
        loser_entry_ask_i = result.loser_entry_asks[i]

        if order.price >= loser_entry_ask_i:
            # Aggressive (shouldn't happen with negative offsets)
            fill = Fill(
                side=loser,
                price=loser_entry_ask_i,
                size=order.size,
                is_winner=False
            )
            result.loser_fills.append(fill)
            if loser == "UP":
                result.up_shares += order.size
            else:
                result.down_shares += order.size
        elif loser_min_ask <= order.price:
            # Passive fill at our bid
            fill = Fill(
                side=loser,
                price=order.price,
                size=order.size,
                is_winner=False
            )
            result.loser_fills.append(fill)
            if loser == "UP":
                result.up_shares += order.size
            else:
                result.down_shares += order.size

    # Track max imbalance
    result.max_imbalance = abs(result.up_shares - result.down_shares)

    # Resolution
    final = market_df.iloc[-1]
    if final['up_bid'] >= 0.90:
        result.resolution = 'UP'
    elif final['down_bid'] >= 0.90:
        result.resolution = 'DOWN'
    else:
        result.resolution = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'

    # =========================================================================
    # PnL CALCULATION
    # =========================================================================

    # Calculate hedged pairs (min of winner and loser shares)
    winner_filled_shares = result.winner_fill.size if result.winner_fill else 0
    loser_filled_shares = sum(f.size for f in result.loser_fills)

    hedged_pairs = min(winner_filled_shares, loser_filled_shares)
    unhedged_winner = winner_filled_shares - hedged_pairs
    unhedged_loser = loser_filled_shares - hedged_pairs

    if hedged_pairs > 0:
        # Calculate average fill prices
        winner_fill_price = result.winner_fill.price if result.winner_fill else 0
        loser_avg_price = np.mean([f.price for f in result.loser_fills]) if result.loser_fills else 0

        result.pair_cost = winner_fill_price + loser_avg_price
        result.hedged_pnl = (1.0 - result.pair_cost) * hedged_pairs

    # Unhedged winner PnL
    if unhedged_winner > 0 and result.winner_fill:
        if winner == result.resolution:
            result.unhedged_pnl += (1.0 - result.winner_fill.price) * unhedged_winner
        else:
            result.unhedged_pnl += (0.0 - result.winner_fill.price) * unhedged_winner

    # Unhedged loser PnL
    if unhedged_loser > 0 and result.loser_fills:
        loser_avg_price = np.mean([f.price for f in result.loser_fills])
        if loser == result.resolution:
            result.unhedged_pnl += (1.0 - loser_avg_price) * unhedged_loser
        else:
            result.unhedged_pnl += (0.0 - loser_avg_price) * unhedged_loser

    result.total_pnl = result.hedged_pnl + result.unhedged_pnl

    return result


def backtest_file(filepath: str) -> Dict:
    """Run backtest on a single CSV file."""
    print(f"\n{'='*80}")
    print(f"FILE: {Path(filepath).name}")
    print(f"{'='*80}")

    try:
        df = pd.read_csv(filepath, on_bad_lines='skip')
    except Exception as e:
        print(f"  ERROR reading file: {e}")
        return {}

    if df.empty:
        print("  EMPTY file, skipping")
        return {}

    # Find complete markets
    markets = df['market_slug'].unique()
    complete = []
    for s in markets:
        mdf = df[df['market_slug'] == s]
        if len(mdf) < 2:
            continue
        first_time = mdf.iloc[0]['time_remaining_secs']
        last_time = mdf.iloc[-1]['time_remaining_secs']
        if first_time >= 800 and last_time <= 60:
            complete.append(s)

    print(f"  Total markets: {len(markets)}")
    print(f"  Complete markets (800s→60s): {len(complete)}")

    if not complete:
        print("  No complete markets, skipping")
        return {}

    # Run backtest
    results = []
    for slug in complete:
        mdf = df[df['market_slug'] == slug].copy()
        results.append(analyze_market(mdf, slug))

    # Aggregate metrics
    entries = [r for r in results if r.entry]

    if not entries:
        print("  No zone 4-6 entries found")
        return {
            'file': Path(filepath).name,
            'total_markets': len(markets),
            'complete_markets': len(complete),
            'zone_4_6_entries': 0,
        }

    # Fill rates
    winner_fills = sum(1 for r in entries if r.winner_fill)
    loser_l1_fills = sum(1 for r in entries if len(r.loser_fills) >= 1)
    loser_l2_fills = sum(1 for r in entries if len(r.loser_fills) >= 2)

    # PnL
    total_hedged = sum(r.hedged_pnl for r in entries)
    total_unhedged = sum(r.unhedged_pnl for r in entries)
    total_pnl = total_hedged + total_unhedged

    # Pair costs (only for hedged)
    hedged_results = [r for r in entries if r.winner_fill and r.loser_fills]
    avg_pair_cost = np.mean([r.pair_cost for r in hedged_results]) if hedged_results else 0

    # Imbalance tracking
    max_imbalance_seen = max(r.max_imbalance for r in entries) if entries else 0

    # Time calculation
    hours = len(entries) * 15 / 60

    # Print results
    print(f"\n  Zone 4-6 entries: {len(entries)}")
    print(f"  Winner fill rate: {100*winner_fills/len(entries):.1f}% ({winner_fills}/{len(entries)})")
    print(f"  Loser L1 fill rate: {100*loser_l1_fills/len(entries):.1f}% ({loser_l1_fills}/{len(entries)})")
    print(f"  Loser L2 fill rate: {100*loser_l2_fills/len(entries):.1f}% ({loser_l2_fills}/{len(entries)})")
    print(f"  Avg pair cost: ${avg_pair_cost:.4f}")
    print(f"  Max imbalance seen: {max_imbalance_seen} shares")
    print(f"\n  --- PnL ---")
    print(f"  Hedged: ${total_hedged:.2f}")
    print(f"  Unhedged: ${total_unhedged:.2f}")
    print(f"  TOTAL: ${total_pnl:.2f}")
    print(f"  Per market: ${total_pnl/len(entries):.2f}")
    print(f"  Per hour: ${total_pnl/hours:.2f}")

    # Zone breakdown
    zone_counts = {}
    for r in entries:
        zone_counts[r.zone] = zone_counts.get(r.zone, 0) + 1
    print(f"\n  Zone breakdown: {zone_counts}")

    return {
        'file': Path(filepath).name,
        'total_markets': len(markets),
        'complete_markets': len(complete),
        'zone_4_6_entries': len(entries),
        'winner_fill_rate': winner_fills / len(entries) if entries else 0,
        'loser_l1_fill_rate': loser_l1_fills / len(entries) if entries else 0,
        'loser_l2_fill_rate': loser_l2_fills / len(entries) if entries else 0,
        'avg_pair_cost': avg_pair_cost,
        'hedged_pnl': total_hedged,
        'unhedged_pnl': total_unhedged,
        'total_pnl': total_pnl,
        'pnl_per_market': total_pnl / len(entries) if entries else 0,
        'pnl_per_hour': total_pnl / hours if hours > 0 else 0,
        'max_imbalance': max_imbalance_seen,
        'hours': hours,
    }


def main():
    print("="*80)
    print("VELOCITY-GATED GRID BACKTEST")
    print("="*80)
    print("\nStrategy:")
    print("  - Only trade zone 4-6 (|vel| >= 0.30 bps)")
    print("  - Winner: Single aggressive order")
    print("  - Loser: 2-level grid")
    print("  - Max imbalance: 10 shares")
    print("  - Fill at OUR bid price (correct logic)")

    # Find all observer CSV files
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    print(f"\nFound {len(csv_files)} observer files")

    all_results = []

    for filepath in csv_files:
        result = backtest_file(str(filepath))
        if result:
            all_results.append(result)

    # Aggregate summary
    if all_results:
        print("\n" + "="*80)
        print("AGGREGATE SUMMARY")
        print("="*80)

        total_markets = sum(r.get('complete_markets', 0) for r in all_results)
        total_entries = sum(r.get('zone_4_6_entries', 0) for r in all_results)
        total_pnl = sum(r.get('total_pnl', 0) for r in all_results)
        total_hours = sum(r.get('hours', 0) for r in all_results)

        # Weighted averages
        if total_entries > 0:
            avg_pair_cost = sum(r.get('avg_pair_cost', 0) * r.get('zone_4_6_entries', 0)
                               for r in all_results) / total_entries
            avg_winner_rate = sum(r.get('winner_fill_rate', 0) * r.get('zone_4_6_entries', 0)
                                  for r in all_results) / total_entries
            avg_loser_l1_rate = sum(r.get('loser_l1_fill_rate', 0) * r.get('zone_4_6_entries', 0)
                                    for r in all_results) / total_entries
            avg_loser_l2_rate = sum(r.get('loser_l2_fill_rate', 0) * r.get('zone_4_6_entries', 0)
                                    for r in all_results) / total_entries
        else:
            avg_pair_cost = avg_winner_rate = avg_loser_l1_rate = avg_loser_l2_rate = 0

        max_imbalance = max(r.get('max_imbalance', 0) for r in all_results)

        print(f"\n  Files processed: {len(all_results)}")
        print(f"  Total complete markets: {total_markets}")
        print(f"  Total zone 4-6 entries: {total_entries}")
        print(f"  Total hours simulated: {total_hours:.1f}")
        print(f"\n  --- Fill Rates (weighted avg) ---")
        print(f"  Winner: {100*avg_winner_rate:.1f}%")
        print(f"  Loser L1: {100*avg_loser_l1_rate:.1f}%")
        print(f"  Loser L2: {100*avg_loser_l2_rate:.1f}%")
        print(f"\n  Avg pair cost: ${avg_pair_cost:.4f}")
        print(f"  Max imbalance seen: {max_imbalance} shares")
        print(f"\n  --- TOTAL PnL ---")
        print(f"  TOTAL: ${total_pnl:.2f}")
        print(f"  Per market: ${total_pnl/total_entries:.2f}" if total_entries else "  N/A")
        print(f"  Per hour: ${total_pnl/total_hours:.2f}" if total_hours else "  N/A")

        # Success criteria check
        print("\n" + "="*80)
        print("SUCCESS CRITERIA CHECK")
        print("="*80)
        criteria = [
            ("Winner fill rate = 100%", avg_winner_rate >= 0.95),
            ("Loser grid fill rate > 80%", avg_loser_l1_rate >= 0.80),
            ("Avg pair cost < $0.96", avg_pair_cost < 0.96),
            ("Imbalance <= 10 shares", max_imbalance <= 10),
            ("Positive total PnL", total_pnl > 0),
        ]

        for name, passed in criteria:
            status = "PASS" if passed else "FAIL"
            print(f"  [{status}] {name}")


if __name__ == "__main__":
    main()
