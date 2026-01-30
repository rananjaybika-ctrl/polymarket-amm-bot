#!/usr/bin/env python3
"""
Aggressive Winner + Stop-Loss Strategy

Combine:
1. AGGRESSIVE winner bid (+0.01 above best_bid) - ensures winner fills
2. PASSIVE loser bid (-0.05 below best_bid) - waits for drop
3. Stop-loss if winner drops 15% - hedge by hitting loser ask

This should:
- Eliminate unhedged loser (winner always fills first)
- Reduce unhedged winner losses via stop-loss
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

SHARES = 15  # Target: 15 shares/side (scale to 30 after live validation)
WINNER_OFFSET = +0.01  # AGGRESSIVE - fill immediately
LOSER_OFFSET = -0.12   # VERY PASSIVE - wait for bigger drop
MIN_TIME = 120
STOP_LOSS_PCT = 0.15


@dataclass
class TradeResult:
    slug: str
    velocity: float
    predicted_winner: str
    resolution: str
    winner_fill_price: float
    winner_filled: bool
    loser_fill_price: float
    loser_filled: bool
    loser_fill_type: str
    pair_cost: float
    pnl: float
    outcome_type: str


def simulate_market(mdf: pd.DataFrame, slug: str) -> Optional[TradeResult]:
    """Simulate with aggressive winner + stop-loss."""

    entry_row = None
    entry_idx = None
    for i, (idx, row) in enumerate(mdf.iterrows()):
        if row['time_remaining_secs'] >= MIN_TIME:
            entry_idx = i
            entry_row = row
            break

    if entry_row is None:
        return None

    velocity = entry_row['velocity_bps']
    predicted_winner = "UP" if velocity > 0 else "DOWN"
    predicted_loser = "DOWN" if velocity > 0 else "UP"

    # Calculate bid prices - AGGRESSIVE on winner, PASSIVE on loser
    if predicted_winner == "UP":
        winner_bid = round(entry_row['up_bid'] + WINNER_OFFSET, 2)
        loser_bid = round(entry_row['down_bid'] + LOSER_OFFSET, 2)
        winner_entry_ask = entry_row['up_ask']
        loser_entry_ask = entry_row['down_ask']
    else:
        winner_bid = round(entry_row['down_bid'] + WINNER_OFFSET, 2)
        loser_bid = round(entry_row['up_bid'] + LOSER_OFFSET, 2)
        winner_entry_ask = entry_row['down_ask']
        loser_entry_ask = entry_row['up_ask']

    winner_bid = max(0.01, min(0.95, winner_bid))
    loser_bid = max(0.01, min(0.95, loser_bid))

    # Simulate tick-by-tick
    post_entry = mdf.iloc[entry_idx:]

    winner_filled = False
    winner_fill_price = 0

    loser_filled = False
    loser_fill_price = 0
    loser_fill_type = "none"

    for i, (idx, row) in enumerate(post_entry.iterrows()):
        if predicted_winner == "UP":
            winner_ask = row['up_ask']
            winner_bid_book = row['up_bid']
            loser_ask = row['down_ask']
        else:
            winner_ask = row['down_ask']
            winner_bid_book = row['down_bid']
            loser_ask = row['up_ask']

        # Check winner fill - AGGRESSIVE bid, likely fills immediately
        if not winner_filled:
            if winner_bid >= winner_ask:
                # Bid at or above ask - fill at ask
                winner_filled = True
                winner_fill_price = winner_ask
            elif winner_ask <= winner_bid:
                # Ask dropped to our bid
                winner_filled = True
                winner_fill_price = winner_bid

        # Check passive loser fill
        if not loser_filled and loser_ask <= loser_bid:
            loser_filled = True
            loser_fill_price = loser_bid
            loser_fill_type = "passive"

        # Stop-loss: if winner filled but loser hasn't, check for drop
        if winner_filled and not loser_filled:
            drop_pct = (winner_fill_price - winner_bid_book) / winner_fill_price
            if drop_pct >= STOP_LOSS_PCT:
                loser_filled = True
                loser_fill_price = loser_ask  # Hit the ask
                loser_fill_type = "stoploss"

    # Resolution
    final = mdf.iloc[-1]
    if final['up_bid'] >= 0.90:
        resolution = 'UP'
    elif final['down_bid'] >= 0.90:
        resolution = 'DOWN'
    else:
        resolution = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'

    # Calculate PnL
    pnl = 0.0
    pair_cost = 0.0
    outcome_type = "no_fill"

    if winner_filled and loser_filled:
        pair_cost = winner_fill_price + loser_fill_price
        pnl = (1.0 - pair_cost) * SHARES
        outcome_type = f"hedged_{loser_fill_type}"

    elif winner_filled and not loser_filled:
        if predicted_winner == resolution:
            pnl = (1.0 - winner_fill_price) * SHARES
        else:
            pnl = (0.0 - winner_fill_price) * SHARES
        outcome_type = "unhedged_winner"

    elif not winner_filled and loser_filled:
        if predicted_loser == resolution:
            pnl = (1.0 - loser_fill_price) * SHARES
        else:
            pnl = (0.0 - loser_fill_price) * SHARES
        outcome_type = "unhedged_loser"

    return TradeResult(
        slug=slug,
        velocity=velocity,
        predicted_winner=predicted_winner,
        resolution=resolution,
        winner_fill_price=winner_fill_price,
        winner_filled=winner_filled,
        loser_fill_price=loser_fill_price,
        loser_filled=loser_filled,
        loser_fill_type=loser_fill_type,
        pair_cost=pair_cost,
        pnl=pnl,
        outcome_type=outcome_type,
    )


def main():
    print("="*80)
    print("AGGRESSIVE WINNER + STOP-LOSS STRATEGY")
    print("="*80)
    print(f"\nWinner offset: {WINNER_OFFSET:+.2f} (AGGRESSIVE - fill first)")
    print(f"Loser offset: {LOSER_OFFSET:+.2f} (PASSIVE - wait for drop)")
    print(f"Stop-loss: {STOP_LOSS_PCT*100:.0f}% drop triggers hedge")

    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    all_results = []

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

            for slug in complete:
                mdf = df[df['market_slug'] == slug].copy()
                result = simulate_market(mdf, slug)
                if result:
                    all_results.append(result)
        except:
            continue

    # Categorize results
    hedged_passive = [r for r in all_results if r.outcome_type == "hedged_passive"]
    hedged_stoploss = [r for r in all_results if r.outcome_type == "hedged_stoploss"]
    unhedged_winner = [r for r in all_results if r.outcome_type == "unhedged_winner"]
    unhedged_loser = [r for r in all_results if r.outcome_type == "unhedged_loser"]
    no_fill = [r for r in all_results if r.outcome_type == "no_fill"]

    total = len(all_results)
    hours = total * 15 / 60

    print(f"\n{'='*80}")
    print("RESULTS")
    print("="*80)

    print(f"\n  Total markets: {total}")
    print(f"\n  Hedged (passive loser fill): {len(hedged_passive)} ({100*len(hedged_passive)/total:.1f}%)")
    print(f"  Hedged (stop-loss):          {len(hedged_stoploss)} ({100*len(hedged_stoploss)/total:.1f}%)")
    print(f"  Unhedged Winner:             {len(unhedged_winner)} ({100*len(unhedged_winner)/total:.1f}%)")
    print(f"  Unhedged Loser:              {len(unhedged_loser)} ({100*len(unhedged_loser)/total:.1f}%)")
    print(f"  No Fill:                     {len(no_fill)} ({100*len(no_fill)/total:.1f}%)")

    # PnL breakdown
    hp_pnl = sum(r.pnl for r in hedged_passive)
    hs_pnl = sum(r.pnl for r in hedged_stoploss)
    uw_pnl = sum(r.pnl for r in unhedged_winner)
    ul_pnl = sum(r.pnl for r in unhedged_loser)
    total_pnl = hp_pnl + hs_pnl + uw_pnl + ul_pnl

    print(f"\n  --- PnL Breakdown ---")
    print(f"  Hedged (passive): ${hp_pnl:>8.2f} (avg ${hp_pnl/len(hedged_passive):.2f})" if hedged_passive else "  Hedged (passive): $    0.00")
    print(f"  Hedged (stop-loss): ${hs_pnl:>6.2f} (avg ${hs_pnl/len(hedged_stoploss):.2f})" if hedged_stoploss else "  Hedged (stop-loss): $    0.00")
    print(f"  Unhedged Winner: ${uw_pnl:>8.2f} (avg ${uw_pnl/len(unhedged_winner):.2f})" if unhedged_winner else "  Unhedged Winner: $    0.00")
    print(f"  Unhedged Loser: ${ul_pnl:>9.2f} (avg ${ul_pnl/len(unhedged_loser):.2f})" if unhedged_loser else "  Unhedged Loser: $    0.00")
    print(f"\n  TOTAL: ${total_pnl:.2f}")
    print(f"  Hourly: ${total_pnl/hours:.2f}/hr")

    # Pair cost analysis
    if hedged_passive:
        passive_costs = [r.pair_cost for r in hedged_passive]
        print(f"\n  Passive hedge pair costs: avg ${np.mean(passive_costs):.4f}")

    if hedged_stoploss:
        sl_costs = [r.pair_cost for r in hedged_stoploss]
        print(f"  Stop-loss hedge pair costs: avg ${np.mean(sl_costs):.4f}")

    # Key insight
    print(f"\n{'='*80}")
    print("KEY INSIGHT")
    print("="*80)
    print(f"""
With AGGRESSIVE winner ({WINNER_OFFSET:+.2f}) + PASSIVE loser ({LOSER_OFFSET:+.2f}) + {STOP_LOSS_PCT*100:.0f}% stop-loss:

  Unhedged Loser cases: {len(unhedged_loser)} (target: 0)
  Unhedged Winner cases: {len(unhedged_winner)} (should be caught by stop-loss)

The aggressive winner bid SHOULD fill first in most cases.
If winner fills but drops {STOP_LOSS_PCT*100:.0f}%, we hedge via stop-loss.
    """)


if __name__ == "__main__":
    main()
