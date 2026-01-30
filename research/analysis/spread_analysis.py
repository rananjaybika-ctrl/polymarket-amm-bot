#!/usr/bin/env python3
"""
Spread Analysis: Is there any room for profit?

Check what pair costs are actually achievable in the data.
"""

import pandas as pd
import numpy as np

MIN_VELOCITY_BPS = 0.30

def main():
    print("=" * 80)
    print("SPREAD ANALYSIS: Where's the profit margin?")
    print("=" * 80)

    df = pd.read_csv('/Users/rananjaybika/polymarket-amm-bot/research/observer/spread_capture_obs_20260115.csv',
                     on_bad_lines='skip')

    markets = df['market_slug'].unique()
    complete = [s for s in markets
                if df[df['market_slug']==s].iloc[0]['time_remaining_secs'] >= 800
                and df[df['market_slug']==s].iloc[-1]['time_remaining_secs'] <= 60]

    print(f"\nAnalyzing {len(complete)} complete markets...")

    results = []

    for slug in complete:
        mdf = df[df['market_slug'] == slug].copy()

        # Find entry point
        entry_row = None
        entry_idx = None
        for i, (idx, row) in enumerate(mdf.iterrows()):
            if abs(row['velocity_bps']) >= MIN_VELOCITY_BPS:
                entry_idx = i
                entry_row = row
                break

        if entry_row is None:
            continue

        vel = entry_row['velocity_bps']
        winner = "UP" if vel > 0 else "DOWN"

        # AT ENTRY
        entry_up_bid = entry_row['up_bid']
        entry_up_ask = entry_row['up_ask']
        entry_down_bid = entry_row['down_bid']
        entry_down_ask = entry_row['down_ask']

        # Theoretical costs at entry
        bid_bid = entry_up_bid + entry_down_bid  # If we bid at both best_bids
        ask_ask = entry_up_ask + entry_down_ask  # If we hit both asks
        spread = ask_ask - bid_bid  # Total spread

        # POST ENTRY - what actually happened
        post_entry = mdf.iloc[entry_idx:]
        up_min_ask = post_entry['up_ask'].min()
        down_min_ask = post_entry['down_ask'].min()

        # Best possible pair cost (if we could fill at both min asks)
        best_possible = up_min_ask + down_min_ask

        results.append({
            'slug': slug,
            'winner': winner,
            'entry_up_bid': entry_up_bid,
            'entry_up_ask': entry_up_ask,
            'entry_down_bid': entry_down_bid,
            'entry_down_ask': entry_down_ask,
            'bid_bid': bid_bid,
            'ask_ask': ask_ask,
            'spread': spread,
            'up_min_ask': up_min_ask,
            'down_min_ask': down_min_ask,
            'best_possible': best_possible,
        })

    print("\n" + "=" * 80)
    print("ENTRY POINT ANALYSIS")
    print("=" * 80)
    print(f"\n{'Market':<35} {'UPbid':>6} {'UPask':>6} {'DNbid':>6} {'DNask':>6} {'Bid+Bid':>8} {'Ask+Ask':>8}")
    print("-" * 95)

    for r in results[:15]:
        print(f"{r['slug']:<35} ${r['entry_up_bid']:.2f}  ${r['entry_up_ask']:.2f}  "
              f"${r['entry_down_bid']:.2f}  ${r['entry_down_ask']:.2f}  "
              f"${r['bid_bid']:.2f}    ${r['ask_ask']:.2f}")

    print("\n" + "=" * 80)
    print("AGGREGATE STATISTICS")
    print("=" * 80)

    bid_bids = [r['bid_bid'] for r in results]
    ask_asks = [r['ask_ask'] for r in results]
    spreads = [r['spread'] for r in results]
    best_possibles = [r['best_possible'] for r in results]

    print(f"\nAt ENTRY (when velocity hits zone 4-6):")
    print(f"  Bid + Bid (best case if both fill at our bid):")
    print(f"    Average: ${np.mean(bid_bids):.4f}")
    print(f"    Min:     ${np.min(bid_bids):.4f}")
    print(f"    Max:     ${np.max(bid_bids):.4f}")

    print(f"\n  Ask + Ask (worst case if we hit both asks):")
    print(f"    Average: ${np.mean(ask_asks):.4f}")
    print(f"    Min:     ${np.min(ask_asks):.4f}")
    print(f"    Max:     ${np.max(ask_asks):.4f}")

    print(f"\n  Total Spread (ask+ask - bid+bid):")
    print(f"    Average: ${np.mean(spreads):.4f}")
    print(f"    Min:     ${np.min(spreads):.4f}")
    print(f"    Max:     ${np.max(spreads):.4f}")

    print(f"\n  BEST POSSIBLE (min_up_ask + min_down_ask over 15 min):")
    print(f"    Average: ${np.mean(best_possibles):.4f}")
    print(f"    Min:     ${np.min(best_possibles):.4f}")
    print(f"    Max:     ${np.max(best_possibles):.4f}")
    print(f"    Markets with best_possible < $0.50: {sum(1 for b in best_possibles if b < 0.50)}")
    print(f"    Markets with best_possible < $0.80: {sum(1 for b in best_possibles if b < 0.80)}")
    print(f"    Markets with best_possible < $0.90: {sum(1 for b in best_possibles if b < 0.90)}")

    print("\n" + "=" * 80)
    print("THE FUNDAMENTAL PROBLEM")
    print("=" * 80)

    avg_bid_bid = np.mean(bid_bids)
    avg_ask_ask = np.mean(ask_asks)

    print(f"""
At entry, the average prices are:
  UP bid/ask:   ~$0.50 / ~$0.52
  DOWN bid/ask: ~$0.48 / ~$0.50

This means:
  Bid + Bid = ~${avg_bid_bid:.2f}  (if BOTH sides fill at best_bid)
  Ask + Ask = ~${avg_ask_ask:.2f}  (if we hit both asks)

With spreads of ~$0.02 per side:
  Total spread = ~${np.mean(spreads):.2f}

THE MATH DOESN'T WORK:
  - Even if BOTH sides fill at best_bid, pair cost = ${avg_bid_bid:.2f}
  - Profit per pair = $1.00 - ${avg_bid_bid:.2f} = ${1.0 - avg_bid_bid:.2f}
  - Per 15 shares = ${(1.0 - avg_bid_bid) * 15:.2f}

  BUT: To fill at best_bid, the ask must DROP to that level.
  In practice, we're filling at our bid price which is:
    - Winner: best_bid + 0.01 = ~$0.52 (near ask)
    - Loser:  best_bid - 0.03 = ~$0.45

  Actual pair cost = ~$0.97
  Actual profit per pair = $0.03 × 15 = $0.45
  """)

    print("\n" + "=" * 80)
    print("WHERE THE PROFIT COULD COME FROM")
    print("=" * 80)

    print(f"""
Options to become profitable:

1. WIDER SPREADS
   Current: bid+bid = ${avg_bid_bid:.2f}, spread = ${np.mean(spreads):.2f}
   Need: bid+bid < $0.94 for meaningful profit

   This data shows tight spreads - not much room.

2. BETTER FILLS ON LOSER SIDE
   If loser dropped MORE, we could bid lower and still fill.
   Current: loser min_ask averages around ${np.mean([r['down_min_ask'] if r['winner']=='UP' else r['up_min_ask'] for r in results]):.2f}

3. MULTIPLE FILLS PER MARKET
   Instead of 1 pair per market, fill 10+ pairs at different price levels.
   Volume compensates for thin margins.

4. TRADE DIFFERENT MARKETS
   These are 15-min BTC up/down markets.
   Other markets may have wider spreads.

5. TAKE DIRECTIONAL RISK
   Instead of hedging, bet on velocity signal.
   41.7% resolution accuracy means this loses money too.
    """)

    # Check if there's ANY opportunity
    print("\n" + "=" * 80)
    print("OPPORTUNITY CHECK: Markets with bid+bid < $0.94")
    print("=" * 80)

    good_markets = [r for r in results if r['bid_bid'] < 0.94]
    print(f"\nMarkets with bid+bid < $0.94: {len(good_markets)}/{len(results)}")

    if good_markets:
        for r in good_markets:
            potential_profit = (1.0 - r['bid_bid']) * 15
            print(f"  {r['slug']}: bid+bid=${r['bid_bid']:.2f}, potential=${potential_profit:.2f}")


if __name__ == "__main__":
    main()
