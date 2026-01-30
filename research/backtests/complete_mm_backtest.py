#!/usr/bin/env python3
"""
COMPLETE Market Making Backtest - Proper Analysis

This backtest properly accounts for:
1. HEDGED positions (both sides fill) - guaranteed profit
2. UNHEDGED WINNER (winner fills, loser doesn't) - depends on resolution
3. UNHEDGED LOSER (loser fills, winner doesn't) - THE DANGEROUS CASE

Key insight from user:
"If markets aggressively trend we lose"
- If BTC trends UP: UP ask rises (no fill), DOWN ask drops (we fill)
- We end up holding DOWN shares which resolve to $0 if UP wins

Also tracks:
- Velocity at entry
- Resolution accuracy
- PnL breakdown by fill type
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional

SHARES_PER_SIDE = 10
OFFSET = -0.03  # Bid below best_bid
MIN_TIME_REMAINING = 120


@dataclass
class MarketOutcome:
    slug: str
    # Entry data
    entry_time: float
    up_bid_entry: float
    down_bid_entry: float
    up_ask_entry: float
    down_ask_entry: float
    velocity_bps: float

    # Our bids
    our_up_bid: float
    our_down_bid: float

    # Post-entry price action
    up_min_ask: float
    down_min_ask: float
    up_max_ask: float
    down_max_ask: float

    # Fill status
    up_filled: bool
    down_filled: bool

    # Resolution
    resolution: str  # "UP" or "DOWN"

    # PnL
    pnl: float
    outcome_type: str  # "hedged", "unhedged_winner", "unhedged_loser", "no_fill"


def analyze_market_properly(df: pd.DataFrame, slug: str, offset: float) -> Optional[MarketOutcome]:
    """Analyze market with proper fill and resolution logic."""
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

    # Entry data
    up_bid = entry_row['up_bid']
    down_bid = entry_row['down_bid']
    up_ask = entry_row['up_ask']
    down_ask = entry_row['down_ask']
    velocity = entry_row['velocity_bps']

    # Our bids (same offset on both sides)
    our_up_bid = round(up_bid + offset, 2)
    our_down_bid = round(down_bid + offset, 2)
    our_up_bid = max(0.01, min(0.95, our_up_bid))
    our_down_bid = max(0.01, min(0.95, our_down_bid))

    # Post-entry price action
    post_entry = mdf.iloc[entry_idx:]
    up_min_ask = post_entry['up_ask'].min()
    up_max_ask = post_entry['up_ask'].max()
    down_min_ask = post_entry['down_ask'].min()
    down_max_ask = post_entry['down_ask'].max()

    # Fill logic - fill at our bid if ask drops to our level
    up_filled = up_min_ask <= our_up_bid
    down_filled = down_min_ask <= our_down_bid

    # Resolution (from final prices)
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

    if up_filled and down_filled:
        # HEDGED - guaranteed profit
        pair_cost = our_up_bid + our_down_bid
        pnl = (1.0 - pair_cost) * SHARES_PER_SIDE
        outcome_type = "hedged"

    elif up_filled and not down_filled:
        # Only UP filled
        if resolution == 'UP':
            # UP wins, we profit
            pnl = (1.0 - our_up_bid) * SHARES_PER_SIDE
        else:
            # DOWN wins, we lose
            pnl = (0.0 - our_up_bid) * SHARES_PER_SIDE

        # Determine if UP was winner or loser based on velocity at entry
        if velocity > 0:  # UP was predicted winner
            outcome_type = "unhedged_winner"
        else:
            outcome_type = "unhedged_loser"

    elif not up_filled and down_filled:
        # Only DOWN filled - THE DANGEROUS CASE
        if resolution == 'DOWN':
            # DOWN wins, we profit
            pnl = (1.0 - our_down_bid) * SHARES_PER_SIDE
        else:
            # UP wins, we lose all
            pnl = (0.0 - our_down_bid) * SHARES_PER_SIDE

        # Determine if DOWN was winner or loser based on velocity at entry
        if velocity < 0:  # DOWN was predicted winner
            outcome_type = "unhedged_winner"
        else:
            outcome_type = "unhedged_loser"

    return MarketOutcome(
        slug=slug,
        entry_time=entry_row['time_remaining_secs'],
        up_bid_entry=up_bid,
        down_bid_entry=down_bid,
        up_ask_entry=up_ask,
        down_ask_entry=down_ask,
        velocity_bps=velocity,
        our_up_bid=our_up_bid,
        our_down_bid=our_down_bid,
        up_min_ask=up_min_ask,
        down_min_ask=down_min_ask,
        up_max_ask=up_max_ask,
        down_max_ask=down_max_ask,
        up_filled=up_filled,
        down_filled=down_filled,
        resolution=resolution,
        pnl=pnl,
        outcome_type=outcome_type,
    )


def main():
    print("="*80)
    print("COMPLETE MARKET MAKING BACKTEST")
    print("="*80)
    print(f"\nStrategy: Passive bids at best_bid {OFFSET:+.2f} on BOTH sides")
    print(f"Shares per side: {SHARES_PER_SIDE}")
    print("\nThis backtest PROPERLY accounts for partial fills and resolution!")

    # Load data
    main_file = '/Users/rananjaybika/polymarket-amm-bot/research/observer/spread_capture_obs_20260115.csv'
    print(f"\nUsing: {Path(main_file).name}")

    df = pd.read_csv(main_file, on_bad_lines='skip')
    markets = df['market_slug'].unique()
    complete = [s for s in markets
                if df[df['market_slug']==s].iloc[0]['time_remaining_secs'] >= 800
                and df[df['market_slug']==s].iloc[-1]['time_remaining_secs'] <= 60]

    print(f"Complete markets: {len(complete)}")

    # Analyze all markets
    results = []
    for slug in complete:
        r = analyze_market_properly(df, slug, OFFSET)
        if r:
            results.append(r)

    # Categorize outcomes
    hedged = [r for r in results if r.outcome_type == "hedged"]
    unhedged_winner = [r for r in results if r.outcome_type == "unhedged_winner"]
    unhedged_loser = [r for r in results if r.outcome_type == "unhedged_loser"]
    no_fill = [r for r in results if r.outcome_type == "no_fill"]

    print("\n" + "="*80)
    print("OUTCOME BREAKDOWN")
    print("="*80)
    print(f"\n  HEDGED (both fill):        {len(hedged):>3} ({100*len(hedged)/len(results):.1f}%)")
    print(f"  UNHEDGED WINNER only:      {len(unhedged_winner):>3} ({100*len(unhedged_winner)/len(results):.1f}%)")
    print(f"  UNHEDGED LOSER only:       {len(unhedged_loser):>3} ({100*len(unhedged_loser)/len(results):.1f}%)")
    print(f"  NO FILL:                   {len(no_fill):>3} ({100*len(no_fill)/len(results):.1f}%)")

    # PnL by category
    print("\n" + "="*80)
    print("PNL BY CATEGORY")
    print("="*80)

    hedged_pnl = sum(r.pnl for r in hedged)
    winner_pnl = sum(r.pnl for r in unhedged_winner)
    loser_pnl = sum(r.pnl for r in unhedged_loser)
    total_pnl = hedged_pnl + winner_pnl + loser_pnl

    print(f"\n  HEDGED PnL:          ${hedged_pnl:>8.2f} (avg ${hedged_pnl/len(hedged):.2f}/market)" if hedged else "  HEDGED PnL:          $    0.00")
    print(f"  UNHEDGED WINNER PnL: ${winner_pnl:>8.2f} (avg ${winner_pnl/len(unhedged_winner):.2f}/market)" if unhedged_winner else "  UNHEDGED WINNER PnL: $    0.00")
    print(f"  UNHEDGED LOSER PnL:  ${loser_pnl:>8.2f} (avg ${loser_pnl/len(unhedged_loser):.2f}/market)" if unhedged_loser else "  UNHEDGED LOSER PnL:  $    0.00")
    print(f"\n  TOTAL PnL:           ${total_pnl:>8.2f}")

    # Analyze the DANGEROUS case: unhedged loser
    if unhedged_loser:
        print("\n" + "="*80)
        print("DANGEROUS CASE ANALYSIS: Unhedged Loser Fills")
        print("="*80)
        print("\nThese are markets where we ONLY filled the LOSER side (price trended away)")

        loser_wins = [r for r in unhedged_loser if
                      (r.down_filled and not r.up_filled and r.resolution == 'DOWN') or
                      (r.up_filled and not r.down_filled and r.resolution == 'UP')]
        loser_loses = [r for r in unhedged_loser if r not in loser_wins]

        print(f"\n  Loser position WINS (lucky): {len(loser_wins)} → PnL ${sum(r.pnl for r in loser_wins):.2f}")
        print(f"  Loser position LOSES:        {len(loser_loses)} → PnL ${sum(r.pnl for r in loser_loses):.2f}")

        # Show examples
        print("\n  Example losing trades:")
        for r in loser_loses[:5]:
            filled_side = "DOWN" if r.down_filled else "UP"
            print(f"    {r.slug[:30]}: filled {filled_side} @ ${r.our_down_bid if r.down_filled else r.our_up_bid:.2f}, "
                  f"resolution={r.resolution}, pnl=${r.pnl:.2f}")

    # Analyze what velocity tells us
    print("\n" + "="*80)
    print("VELOCITY ANALYSIS")
    print("="*80)

    # Check if velocity predicts which side FILLS (not resolution)
    positive_vel = [r for r in results if r.velocity_bps > 0.1]
    negative_vel = [r for r in results if r.velocity_bps < -0.1]

    print(f"\n  Positive velocity (UP predicted winner): {len(positive_vel)} markets")
    if positive_vel:
        up_fills = sum(1 for r in positive_vel if r.up_filled)
        down_fills = sum(1 for r in positive_vel if r.down_filled)
        print(f"    UP fills:   {up_fills}/{len(positive_vel)} ({100*up_fills/len(positive_vel):.1f}%)")
        print(f"    DOWN fills: {down_fills}/{len(positive_vel)} ({100*down_fills/len(positive_vel):.1f}%)")

    print(f"\n  Negative velocity (DOWN predicted winner): {len(negative_vel)} markets")
    if negative_vel:
        up_fills = sum(1 for r in negative_vel if r.up_filled)
        down_fills = sum(1 for r in negative_vel if r.down_filled)
        print(f"    UP fills:   {up_fills}/{len(negative_vel)} ({100*up_fills/len(negative_vel):.1f}%)")
        print(f"    DOWN fills: {down_fills}/{len(negative_vel)} ({100*down_fills/len(negative_vel):.1f}%)")

    # Ask+Ask at entry (should be ~$1.00)
    print("\n" + "="*80)
    print("ORDERBOOK ANALYSIS (ask+ask should be ~$1.00)")
    print("="*80)
    ask_asks = [r.up_ask_entry + r.down_ask_entry for r in results]
    bid_bids = [r.up_bid_entry + r.down_bid_entry for r in results]
    print(f"\n  Avg ask+ask at entry: ${np.mean(ask_asks):.4f}")
    print(f"  Avg bid+bid at entry: ${np.mean(bid_bids):.4f}")
    print(f"  Avg spread: ${np.mean(ask_asks) - np.mean(bid_bids):.4f}")

    # Pair costs for hedged trades
    if hedged:
        pair_costs = [r.our_up_bid + r.our_down_bid for r in hedged]
        print(f"\n  Hedged pair costs:")
        print(f"    Avg: ${np.mean(pair_costs):.4f}")
        print(f"    Min: ${np.min(pair_costs):.4f}")
        print(f"    Max: ${np.max(pair_costs):.4f}")

    # Summary
    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)
    hours = len(results) * 15 / 60
    print(f"\n  Total markets: {len(results)}")
    print(f"  Hours: {hours:.1f}")
    print(f"  Total PnL: ${total_pnl:.2f}")
    print(f"  Hourly rate: ${total_pnl/hours:.2f}/hr")

    # Key insight
    print("\n" + "="*80)
    print("KEY INSIGHT")
    print("="*80)
    print(f"""
The UNHEDGED LOSER case is critical:
  - {len(unhedged_loser)} markets where only LOSER side filled
  - PnL from these: ${loser_pnl:.2f}

This happens when market TRENDS:
  - Winner price rises → our bid doesn't fill
  - Loser price drops → our bid fills
  - We're stuck holding the losing side

With symmetric passive MM, we have NO CONTROL over this.
We need velocity to AVOID filling the loser side.
    """)


if __name__ == "__main__":
    main()
