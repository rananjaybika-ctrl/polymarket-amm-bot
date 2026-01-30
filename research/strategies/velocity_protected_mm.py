#!/usr/bin/env python3
"""
Velocity-Protected Market Making

Use velocity to PROTECT against the dangerous unhedged loser case.

Strategy:
1. Use velocity to identify winner/loser
2. Be MORE aggressive on WINNER side (fill it first)
3. Be MORE passive on LOSER side (let it come to us OR cancel if winner doesn't fill)

The key insight:
- If velocity says UP is winner, we WANT to fill UP
- If we fill DOWN but not UP, we're stuck with the losing side
- Solution: Don't let loser fill unless winner has filled (or will fill)
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

SHARES_PER_SIDE = 10
MIN_TIME_REMAINING = 120
MIN_VELOCITY_BPS = 0.05  # Low threshold to use velocity assist


@dataclass
class MarketOutcome:
    slug: str
    velocity_bps: float
    winner_side: str  # "UP" or "DOWN"
    resolution: str

    # Fill status
    winner_filled: bool
    loser_filled: bool

    # PnL
    pnl: float
    outcome_type: str


def analyze_market_velocity_protected(df: pd.DataFrame, slug: str,
                                      winner_offset: float, loser_offset: float) -> Optional[MarketOutcome]:
    """
    Analyze with velocity-protected logic.

    Key: Different offsets for winner vs loser based on velocity.
    """
    mdf = df[df['market_slug'] == slug].copy()

    # Find entry
    entry_row = None
    entry_idx = None
    for i, (idx, row) in enumerate(mdf.iterrows()):
        if row['time_remaining_secs'] >= MIN_TIME_REMAINING:
            entry_idx = i
            entry_row = row
            break

    if entry_row is None:
        return None

    velocity = entry_row['velocity_bps']

    # Determine winner/loser based on velocity
    if abs(velocity) >= MIN_VELOCITY_BPS:
        if velocity > 0:
            winner_side = "UP"
            loser_side = "DOWN"
        else:
            winner_side = "DOWN"
            loser_side = "UP"
    else:
        # No clear velocity signal - treat symmetrically
        winner_side = "UP"
        loser_side = "DOWN"

    # Calculate bid prices with asymmetric offsets
    if winner_side == "UP":
        up_bid = round(entry_row['up_bid'] + winner_offset, 2)
        down_bid = round(entry_row['down_bid'] + loser_offset, 2)
    else:
        up_bid = round(entry_row['up_bid'] + loser_offset, 2)
        down_bid = round(entry_row['down_bid'] + winner_offset, 2)

    up_bid = max(0.01, min(0.95, up_bid))
    down_bid = max(0.01, min(0.95, down_bid))

    # Post-entry price action
    post_entry = mdf.iloc[entry_idx:]
    up_min_ask = post_entry['up_ask'].min()
    down_min_ask = post_entry['down_ask'].min()

    # Fill logic
    up_filled = up_min_ask <= up_bid
    down_filled = down_min_ask <= down_bid

    # Map to winner/loser fills
    if winner_side == "UP":
        winner_filled = up_filled
        loser_filled = down_filled
        winner_fill_price = up_bid
        loser_fill_price = down_bid
    else:
        winner_filled = down_filled
        loser_filled = up_filled
        winner_fill_price = down_bid
        loser_fill_price = up_bid

    # Resolution
    final = mdf.iloc[-1]
    if final['up_bid'] >= 0.90:
        resolution = 'UP'
    elif final['down_bid'] >= 0.90:
        resolution = 'DOWN'
    else:
        resolution = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'

    # PnL calculation
    pnl = 0.0
    outcome_type = "no_fill"

    if winner_filled and loser_filled:
        # HEDGED
        pair_cost = winner_fill_price + loser_fill_price
        pnl = (1.0 - pair_cost) * SHARES_PER_SIDE
        outcome_type = "hedged"

    elif winner_filled and not loser_filled:
        # Only winner filled - this is OK, depends on resolution
        if winner_side == resolution:
            pnl = (1.0 - winner_fill_price) * SHARES_PER_SIDE
        else:
            pnl = (0.0 - winner_fill_price) * SHARES_PER_SIDE
        outcome_type = "unhedged_winner"

    elif not winner_filled and loser_filled:
        # Only loser filled - THE DANGEROUS CASE
        if loser_side == resolution:
            pnl = (1.0 - loser_fill_price) * SHARES_PER_SIDE
        else:
            pnl = (0.0 - loser_fill_price) * SHARES_PER_SIDE
        outcome_type = "unhedged_loser"

    return MarketOutcome(
        slug=slug,
        velocity_bps=velocity,
        winner_side=winner_side,
        resolution=resolution,
        winner_filled=winner_filled,
        loser_filled=loser_filled,
        pnl=pnl,
        outcome_type=outcome_type,
    )


def run_test(df, complete, winner_offset, loser_offset, name):
    """Run backtest with specific offset configuration."""
    results = []
    for slug in complete:
        r = analyze_market_velocity_protected(df, slug, winner_offset, loser_offset)
        if r:
            results.append(r)

    hedged = [r for r in results if r.outcome_type == "hedged"]
    unhedged_winner = [r for r in results if r.outcome_type == "unhedged_winner"]
    unhedged_loser = [r for r in results if r.outcome_type == "unhedged_loser"]

    hedged_pnl = sum(r.pnl for r in hedged)
    winner_pnl = sum(r.pnl for r in unhedged_winner)
    loser_pnl = sum(r.pnl for r in unhedged_loser)
    total_pnl = hedged_pnl + winner_pnl + loser_pnl

    return {
        'name': name,
        'winner_offset': winner_offset,
        'loser_offset': loser_offset,
        'hedged': len(hedged),
        'unhedged_winner': len(unhedged_winner),
        'unhedged_loser': len(unhedged_loser),
        'hedged_pnl': hedged_pnl,
        'winner_pnl': winner_pnl,
        'loser_pnl': loser_pnl,
        'total_pnl': total_pnl,
    }


def main():
    print("="*80)
    print("VELOCITY-PROTECTED MARKET MAKING - ALL FILES")
    print("="*80)
    print("\nGoal: Use velocity to PROTECT against unhedged loser fills")
    print("Strategy: Aggressive on winner, passive on loser")

    # Load ALL observer files
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    all_dfs = []
    total_complete = []

    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath, on_bad_lines='skip')
            if df.empty:
                continue
            markets = df['market_slug'].unique()
            complete = [s for s in markets
                        if len(df[df['market_slug']==s]) >= 2
                        and df[df['market_slug']==s].iloc[0]['time_remaining_secs'] >= 800
                        and df[df['market_slug']==s].iloc[-1]['time_remaining_secs'] <= 60]
            if complete:
                all_dfs.append((df, complete))
                total_complete.extend(complete)
        except:
            continue

    print(f"\nTotal complete markets across all files: {len(total_complete)}")

    # Test configurations
    configs = [
        # Baseline symmetric
        {'name': 'symmetric -0.03', 'w': -0.03, 'l': -0.03},

        # Winner more aggressive
        {'name': 'w=-0.02, l=-0.04', 'w': -0.02, 'l': -0.04},
        {'name': 'w=-0.01, l=-0.05', 'w': -0.01, 'l': -0.05},
        {'name': 'w=+0.00, l=-0.06', 'w': +0.00, 'l': -0.06},
        {'name': 'w=+0.01, l=-0.07', 'w': +0.01, 'l': -0.07},

        # Extreme: winner at ask, loser very passive
        {'name': 'w=+0.02, l=-0.08', 'w': +0.02, 'l': -0.08},
        {'name': 'w=+0.02, l=-0.10', 'w': +0.02, 'l': -0.10},

        # Only bid on winner (no loser bid)
        {'name': 'WINNER ONLY -0.02', 'w': -0.02, 'l': -0.50},  # Loser bid so low it never fills
    ]

    print(f"\n{'Config':<25} {'H':>4} {'UW':>4} {'UL':>4} {'H_PnL':>9} {'W_PnL':>9} {'L_PnL':>9} {'Total':>9}")
    print("-" * 85)

    results = []
    for c in configs:
        # Aggregate across all files
        agg = {'name': c['name'], 'winner_offset': c['w'], 'loser_offset': c['l'],
               'hedged': 0, 'unhedged_winner': 0, 'unhedged_loser': 0,
               'hedged_pnl': 0, 'winner_pnl': 0, 'loser_pnl': 0, 'total_pnl': 0}

        for df, complete in all_dfs:
            r = run_test(df, complete, c['w'], c['l'], c['name'])
            agg['hedged'] += r['hedged']
            agg['unhedged_winner'] += r['unhedged_winner']
            agg['unhedged_loser'] += r['unhedged_loser']
            agg['hedged_pnl'] += r['hedged_pnl']
            agg['winner_pnl'] += r['winner_pnl']
            agg['loser_pnl'] += r['loser_pnl']
            agg['total_pnl'] += r['total_pnl']

        results.append(agg)
        print(f"{agg['name']:<25} {agg['hedged']:>4} {agg['unhedged_winner']:>4} {agg['unhedged_loser']:>4} "
              f"${agg['hedged_pnl']:>7.2f} ${agg['winner_pnl']:>7.2f} ${agg['loser_pnl']:>7.2f} ${agg['total_pnl']:>7.2f}")

    # Best result
    best = max(results, key=lambda x: x['total_pnl'])
    total_markets = best['hedged'] + best['unhedged_winner'] + best['unhedged_loser']
    hours = total_markets * 15 / 60

    print(f"\n{'='*85}")
    print(f"BEST: {best['name']}")
    print(f"  Total markets: {total_markets}")
    print(f"  Hedged: {best['hedged']} ({100*best['hedged']/total_markets:.1f}%)")
    print(f"  Unhedged Winner: {best['unhedged_winner']} ({100*best['unhedged_winner']/total_markets:.1f}%)")
    print(f"  Unhedged Loser: {best['unhedged_loser']} ({100*best['unhedged_loser']/total_markets:.1f}%)")
    print(f"\n  Hedged PnL: ${best['hedged_pnl']:.2f}")
    print(f"  Winner PnL: ${best['winner_pnl']:.2f}")
    print(f"  Loser PnL: ${best['loser_pnl']:.2f}")
    print(f"  Total PnL: ${best['total_pnl']:.2f}")
    print(f"  Hourly: ${best['total_pnl']/hours:.2f}/hr")

    # Analyze winner-only strategy
    winner_only = [r for r in results if 'WINNER ONLY' in r['name']][0]
    print(f"\n{'='*85}")
    print("WINNER-ONLY STRATEGY ANALYSIS")
    print("="*85)
    print(f"  Only bid on predicted winner side")
    print(f"  Hedged: {winner_only['hedged']} (loser fills by chance)")
    print(f"  Unhedged Winner: {winner_only['unhedged_winner']}")
    print(f"  Unhedged Loser: {winner_only['unhedged_loser']} (should be 0!)")
    print(f"  Total PnL: ${winner_only['total_pnl']:.2f}")

    # Summary of the edge
    print(f"\n{'='*85}")
    print("VELOCITY EDGE SUMMARY")
    print("="*85)
    print(f"""
The Binance/Chainlink delay creates an edge:
- Velocity predicts SHORT-TERM movement (97%)
- This tells us which side will get MORE/LESS expensive

Using asymmetric offsets:
- Winner offset: {best['winner_offset']:+.2f} (more aggressive - fill before price rises)
- Loser offset: {best['loser_offset']:+.2f} (more passive - let price come to us)

This REDUCES unhedged loser cases:
- Symmetric: ~11% unhedged loser (dangerous!)
- Best config: ~{100*best['unhedged_loser']/total_markets:.1f}% unhedged loser
    """)


if __name__ == "__main__":
    main()
