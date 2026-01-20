#!/usr/bin/env python3
"""
Unhedged Trade Fill Likelihood Analysis

Analyzes the realistic fill probability for unhedged trades:
1. At the ASK price (taker - what backtest assumes)
2. At best_bid + 0.01 (maker - more realistic limit order)

Key questions:
- What is the typical spread at Zone 5-6 entries?
- Would a limit order at bid+0.01 get filled before market moves?
- What's the realistic fill assumption for unhedged trades?
"""

import pandas as pd
import numpy as np
from pathlib import Path
from typing import List, Dict, Tuple
from dataclasses import dataclass

# Match backtest parameters
SHARES = 15
MIN_TIME = 60
MIN_VELOCITY = 0.50
STOP_LOSS_PCT = 0.07  # Match original backtest

@dataclass
class UnhedgedEntry:
    """An unhedged trade entry point."""
    slug: str
    entry_time_remaining: float
    velocity: float
    winner_side: str

    # Price data at entry
    winner_bid: float
    winner_ask: float
    spread: float

    # What happens next
    resolution: str
    velocity_correct: bool

    # Fill scenarios
    fill_at_ask: float  # Taker fill
    fill_at_bid_plus_01: float  # Limit order at bid+0.01

    # Post-entry price movement (would limit order fill?)
    price_touched_bid_plus_01: bool
    samples_until_touch: int  # How many samples until bid+0.01 was hit


def load_market_data() -> Dict[str, pd.DataFrame]:
    """Load observer CSV data."""
    observer_dir = Path('research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    all_markets = {}
    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath, on_bad_lines='skip', low_memory=False)
            if df.empty or 'velocity_bps' not in df.columns:
                continue
            for slug in df['market_slug'].unique():
                mdf = df[df['market_slug'] == slug]
                if len(mdf) >= 2:
                    first = mdf.iloc[0]['time_remaining_secs']
                    last = mdf.iloc[-1]['time_remaining_secs']
                    if first >= 800 and last <= 60:
                        if slug not in all_markets or len(mdf) > len(all_markets[slug]):
                            all_markets[slug] = mdf.copy()
        except:
            continue

    return all_markets


def analyze_unhedged_fills(all_markets: Dict[str, pd.DataFrame]) -> List[UnhedgedEntry]:
    """Find all unhedged entries and analyze fill likelihood."""
    entries = []

    for slug, mdf in all_markets.items():
        mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)

        # Determine resolution
        final = mdf.iloc[-1]
        if final['up_bid'] >= 0.90:
            resolution = 'UP'
        elif final['down_bid'] >= 0.90:
            resolution = 'DOWN'
        else:
            resolution = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'

        i = 0
        in_trade = False

        while i < len(mdf):
            row = mdf.iloc[i]
            time_rem = row['time_remaining_secs']
            vel = row['velocity_bps']

            if time_rem < MIN_TIME:
                break

            if not in_trade and abs(vel) >= MIN_VELOCITY:
                # Entry signal - check if this becomes unhedged
                winner_side = "UP" if vel > 0 else "DOWN"

                if winner_side == "UP":
                    winner_bid = row['up_bid']
                    winner_ask = row['up_ask']
                    loser_ask = row['down_ask']
                else:
                    winner_bid = row['down_bid']
                    winner_ask = row['down_ask']
                    loser_ask = row['up_ask']

                spread = winner_ask - winner_bid
                loser_target = loser_ask - 0.12  # LOSER_OFFSET

                # Simulate to check if this would be hedged
                hedge_found = False
                for j in range(i + 1, len(mdf)):
                    check = mdf.iloc[j]
                    if check['time_remaining_secs'] < 10:
                        break

                    if winner_side == "UP":
                        check_loser_ask = check['down_ask']
                        check_winner_bid = check['up_bid']
                    else:
                        check_loser_ask = check['up_ask']
                        check_winner_bid = check['down_bid']

                    # Passive fill?
                    if check_loser_ask <= loser_target:
                        hedge_found = True
                        break

                    # Stop loss?
                    drop = (winner_ask - check_winner_bid) / winner_ask if winner_ask > 0 else 0
                    if drop >= STOP_LOSS_PCT:
                        hedge_found = True
                        break

                if not hedge_found:
                    # This is an UNHEDGED trade!
                    velocity_correct = (winner_side == resolution)

                    # Check if bid+0.01 would have been touched
                    limit_price = winner_bid + 0.01
                    touched = False
                    samples_until_touch = 0

                    for j in range(i + 1, len(mdf)):
                        check = mdf.iloc[j]
                        if winner_side == "UP":
                            check_ask = check['up_ask']
                        else:
                            check_ask = check['down_ask']

                        # Would our limit order have filled?
                        # Limit buy at bid+0.01 fills when ask drops to that level
                        if check_ask <= limit_price:
                            touched = True
                            samples_until_touch = j - i
                            break

                    entries.append(UnhedgedEntry(
                        slug=slug,
                        entry_time_remaining=time_rem,
                        velocity=vel,
                        winner_side=winner_side,
                        winner_bid=winner_bid,
                        winner_ask=winner_ask,
                        spread=spread,
                        resolution=resolution,
                        velocity_correct=velocity_correct,
                        fill_at_ask=winner_ask,
                        fill_at_bid_plus_01=limit_price,
                        price_touched_bid_plus_01=touched,
                        samples_until_touch=samples_until_touch,
                    ))

                # Skip forward (same as backtest)
                in_trade = True
                i += 5
            else:
                in_trade = False

            i += 1

    return entries


def main():
    print("=" * 80)
    print("UNHEDGED TRADE FILL LIKELIHOOD ANALYSIS")
    print("=" * 80)

    # Load data
    all_markets = load_market_data()
    print(f"\nLoaded {len(all_markets)} complete markets")

    # Analyze unhedged entries
    print("\nAnalyzing unhedged entries...")
    entries = analyze_unhedged_fills(all_markets)
    print(f"Found {len(entries)} unhedged trades")

    if not entries:
        print("No unhedged trades found!")
        return

    # Basic stats
    print(f"\n{'='*80}")
    print("UNHEDGED TRADE STATISTICS")
    print("=" * 80)

    correct = [e for e in entries if e.velocity_correct]
    wrong = [e for e in entries if not e.velocity_correct]

    print(f"\n  Velocity accuracy:")
    print(f"    Correct: {len(correct)} ({len(correct)/len(entries)*100:.1f}%)")
    print(f"    Wrong: {len(wrong)} ({len(wrong)/len(entries)*100:.1f}%)")

    # Debug: show some wrong trades
    if wrong:
        print(f"\n  Sample WRONG trades:")
        for e in wrong[:5]:
            print(f"    {e.slug[:40]:40} vel={e.velocity:+.2f} side={e.winner_side} res={e.resolution}")
    else:
        print(f"\n  DEBUG: No wrong trades found!")
        print(f"  Sample correct trades:")
        for e in entries[:5]:
            print(f"    {e.slug[:40]:40} vel={e.velocity:+.2f} side={e.winner_side} res={e.resolution}")

    # Spread analysis at entry
    spreads = [e.spread for e in entries]
    print(f"\n  Spread at entry (ask - bid):")
    print(f"    Min: ${min(spreads):.4f}")
    print(f"    Median: ${np.median(spreads):.4f}")
    print(f"    Mean: ${np.mean(spreads):.4f}")
    print(f"    Max: ${max(spreads):.4f}")

    # Entry prices
    asks = [e.winner_ask for e in entries]
    bids = [e.winner_bid for e in entries]

    print(f"\n  Entry prices:")
    print(f"    Avg winner_ask (taker): ${np.mean(asks):.4f}")
    print(f"    Avg winner_bid: ${np.mean(bids):.4f}")
    print(f"    Avg bid+0.01 (limit): ${np.mean([e.fill_at_bid_plus_01 for e in entries]):.4f}")

    # FILL LIKELIHOOD ANALYSIS
    print(f"\n{'='*80}")
    print("FILL LIKELIHOOD ANALYSIS")
    print("=" * 80)

    # Scenario 1: Fill at ASK (taker) - what backtest assumes
    print(f"\n  SCENARIO 1: Fill at ASK (taker)")
    print(f"    Assumption: 100% fill rate (market order)")
    print(f"    Avg fill price: ${np.mean(asks):.4f}")

    pnl_at_ask_correct = sum((1.0 - e.winner_ask) * SHARES for e in correct)
    pnl_at_ask_wrong = sum((0.0 - e.winner_ask) * SHARES for e in wrong)
    total_pnl_at_ask = pnl_at_ask_correct + pnl_at_ask_wrong

    print(f"    PnL if correct: ${pnl_at_ask_correct:.2f}")
    print(f"    PnL if wrong: ${pnl_at_ask_wrong:.2f}")
    print(f"    Total PnL: ${total_pnl_at_ask:.2f}")

    # Scenario 2: Fill at bid+0.01 (limit order)
    print(f"\n  SCENARIO 2: Fill at bid+0.01 (limit order)")

    touched = [e for e in entries if e.price_touched_bid_plus_01]
    not_touched = [e for e in entries if not e.price_touched_bid_plus_01]

    fill_rate = len(touched) / len(entries) * 100
    print(f"    Fill rate: {fill_rate:.1f}% ({len(touched)}/{len(entries)} would have filled)")

    if touched:
        samples_to_fill = [e.samples_until_touch for e in touched if e.samples_until_touch > 0]
        if samples_to_fill:
            print(f"    Avg time to fill: {np.mean(samples_to_fill):.0f} samples ({np.mean(samples_to_fill)*0.2:.1f}s)")

        touched_correct = [e for e in touched if e.velocity_correct]
        touched_wrong = [e for e in touched if not e.velocity_correct]

        pnl_limit_correct = sum((1.0 - e.fill_at_bid_plus_01) * SHARES for e in touched_correct)
        pnl_limit_wrong = sum((0.0 - e.fill_at_bid_plus_01) * SHARES for e in touched_wrong)
        total_pnl_limit = pnl_limit_correct + pnl_limit_wrong

        print(f"    Filled trades accuracy: {len(touched_correct)/len(touched)*100:.1f}%")
        print(f"    Avg fill price: ${np.mean([e.fill_at_bid_plus_01 for e in touched]):.4f}")
        print(f"    Price improvement: ${np.mean([e.spread - 0.01 for e in touched]):.4f}")
        print(f"    PnL if correct: ${pnl_limit_correct:.2f}")
        print(f"    PnL if wrong: ${pnl_limit_wrong:.2f}")
        print(f"    Total PnL (filled only): ${total_pnl_limit:.2f}")

    # What about unfilled orders?
    print(f"\n  Unfilled limit orders: {len(not_touched)}")
    if not_touched:
        not_touched_correct = [e for e in not_touched if e.velocity_correct]
        not_touched_wrong = [e for e in not_touched if not e.velocity_correct]
        print(f"    Of unfilled: {len(not_touched_correct)} would have been correct ({len(not_touched_correct)/len(not_touched)*100:.1f}%)")
        print(f"    Of unfilled: {len(not_touched_wrong)} would have been wrong ({len(not_touched_wrong)/len(not_touched)*100:.1f}%)")

        # These represent MISSED opportunities
        missed_profit = sum((1.0 - e.fill_at_bid_plus_01) * SHARES for e in not_touched_correct)
        avoided_loss = sum((0.0 - e.fill_at_bid_plus_01) * SHARES for e in not_touched_wrong)
        print(f"    Missed profit (correct): ${missed_profit:.2f}")
        print(f"    Avoided loss (wrong): ${-avoided_loss:.2f}")

    # Comparison
    print(f"\n{'='*80}")
    print("COMPARISON: TAKER vs LIMIT FILLS")
    print("=" * 80)

    print(f"\n  TAKER (fill at ask):")
    print(f"    Fill rate: 100%")
    print(f"    Total PnL: ${total_pnl_at_ask:.2f}")
    print(f"    PnL per trade: ${total_pnl_at_ask/len(entries):.2f}")

    if touched:
        total_pnl_limit_only = total_pnl_limit
        print(f"\n  LIMIT (fill at bid+0.01):")
        print(f"    Fill rate: {fill_rate:.1f}%")
        print(f"    Total PnL (filled): ${total_pnl_limit_only:.2f}")
        print(f"    PnL per filled trade: ${total_pnl_limit_only/len(touched):.2f}")

        # Effective PnL including opportunity cost
        print(f"\n  NET COMPARISON:")
        if not_touched:
            # Limit orders that didn't fill: no position taken
            # This means missed profits on correct calls, but also avoided losses
            net_from_unfilled = missed_profit + avoided_loss  # avoided_loss is negative
            print(f"    Limit order unfilled impact: ${net_from_unfilled:.2f}")
            effective_limit_pnl = total_pnl_limit + 0  # Unfilled = $0 (no position)
        else:
            effective_limit_pnl = total_pnl_limit

        print(f"\n    Taker strategy PnL: ${total_pnl_at_ask:.2f}")
        print(f"    Limit strategy PnL: ${effective_limit_pnl:.2f}")
        diff = total_pnl_at_ask - effective_limit_pnl
        print(f"    Difference: ${diff:.2f} ({'taker better' if diff > 0 else 'limit better'})")

    # Spread distribution analysis
    print(f"\n{'='*80}")
    print("SPREAD DISTRIBUTION")
    print("=" * 80)

    spread_buckets = [0.01, 0.02, 0.03, 0.04, 0.05, 0.10]
    for bucket in spread_buckets:
        count = sum(1 for e in entries if e.spread <= bucket)
        pct = count / len(entries) * 100
        bar = "█" * int(pct / 2)
        print(f"    Spread <= ${bucket:.2f}: {count:3} ({pct:5.1f}%) {bar}")

    # Bid+0.01 fill likelihood by spread
    print(f"\n{'='*80}")
    print("FILL RATE BY SPREAD SIZE")
    print("=" * 80)

    for i, bucket in enumerate(spread_buckets):
        prev = spread_buckets[i-1] if i > 0 else 0
        bucket_entries = [e for e in entries if prev < e.spread <= bucket]
        if bucket_entries:
            filled_in_bucket = [e for e in bucket_entries if e.price_touched_bid_plus_01]
            rate = len(filled_in_bucket) / len(bucket_entries) * 100
            print(f"    Spread ${prev:.2f}-${bucket:.2f}: {rate:5.1f}% fill rate ({len(filled_in_bucket)}/{len(bucket_entries)})")

    # Final recommendation
    print(f"\n{'='*80}")
    print("CONCLUSIONS")
    print("=" * 80)

    avg_spread = np.mean(spreads)
    print(f"""
  1. Average spread at unhedged entry: ${avg_spread:.4f}
     - This is the "cost" of taker execution

  2. Limit order (bid+0.01) fill rate: {fill_rate:.1f}%
     - {100-fill_rate:.1f}% of limit orders would NOT fill

  3. Backtest validity:
     - Taker fills are realistic (100% fill at ask)
     - PnL difference: ${abs(total_pnl_at_ask - effective_limit_pnl):.2f}

  4. Recommendation:
""")

    if total_pnl_at_ask > effective_limit_pnl:
        print(f"     TAKER strategy is better by ${total_pnl_at_ask - effective_limit_pnl:.2f}")
        print(f"     - Captures all opportunities (100% fill)")
        print(f"     - Higher fill certainty compensates for worse price")
    else:
        print(f"     LIMIT strategy is better by ${effective_limit_pnl - total_pnl_at_ask:.2f}")
        print(f"     - Better fill price when it works")
        print(f"     - Missing {100-fill_rate:.1f}% of trades is acceptable")

    print(f"\n{'='*80}")


if __name__ == "__main__":
    main()
