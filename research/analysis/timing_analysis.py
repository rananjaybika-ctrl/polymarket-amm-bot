#!/usr/bin/env python3
"""
Timing Analysis: When are spreads widest?

Maybe the opportunity is NOT at zone 4-6 entry, but at other times.
"""

import pandas as pd
import numpy as np

def main():
    print("=" * 80)
    print("TIMING ANALYSIS: When do spreads widen?")
    print("=" * 80)

    df = pd.read_csv('/Users/rananjaybika/polymarket-amm-bot/research/observer/spread_capture_obs_20260115.csv',
                     on_bad_lines='skip')

    markets = df['market_slug'].unique()
    complete = [s for s in markets
                if df[df['market_slug']==s].iloc[0]['time_remaining_secs'] >= 800
                and df[df['market_slug']==s].iloc[-1]['time_remaining_secs'] <= 60]

    print(f"\nAnalyzing {len(complete)} complete markets...")

    # Collect spread data by time remaining
    time_buckets = {
        '900-800': [],
        '800-700': [],
        '700-600': [],
        '600-500': [],
        '500-400': [],
        '400-300': [],
        '300-200': [],
        '200-100': [],
        '100-0': [],
    }

    for slug in complete:
        mdf = df[df['market_slug'] == slug]

        for idx, row in mdf.iterrows():
            t = row['time_remaining_secs']
            bid_bid = row['up_bid'] + row['down_bid']
            ask_ask = row['up_ask'] + row['down_ask']

            if 800 < t <= 900:
                time_buckets['900-800'].append({'bid_bid': bid_bid, 'ask_ask': ask_ask, 'spread': ask_ask - bid_bid})
            elif 700 < t <= 800:
                time_buckets['800-700'].append({'bid_bid': bid_bid, 'ask_ask': ask_ask, 'spread': ask_ask - bid_bid})
            elif 600 < t <= 700:
                time_buckets['700-600'].append({'bid_bid': bid_bid, 'ask_ask': ask_ask, 'spread': ask_ask - bid_bid})
            elif 500 < t <= 600:
                time_buckets['600-500'].append({'bid_bid': bid_bid, 'ask_ask': ask_ask, 'spread': ask_ask - bid_bid})
            elif 400 < t <= 500:
                time_buckets['500-400'].append({'bid_bid': bid_bid, 'ask_ask': ask_ask, 'spread': ask_ask - bid_bid})
            elif 300 < t <= 400:
                time_buckets['400-300'].append({'bid_bid': bid_bid, 'ask_ask': ask_ask, 'spread': ask_ask - bid_bid})
            elif 200 < t <= 300:
                time_buckets['300-200'].append({'bid_bid': bid_bid, 'ask_ask': ask_ask, 'spread': ask_ask - bid_bid})
            elif 100 < t <= 200:
                time_buckets['200-100'].append({'bid_bid': bid_bid, 'ask_ask': ask_ask, 'spread': ask_ask - bid_bid})
            elif 0 <= t <= 100:
                time_buckets['100-0'].append({'bid_bid': bid_bid, 'ask_ask': ask_ask, 'spread': ask_ask - bid_bid})

    print("\n" + "=" * 80)
    print("SPREADS BY TIME REMAINING")
    print("=" * 80)
    print(f"\n{'Time Bucket':<12} {'Samples':>8} {'Avg Bid+Bid':>12} {'Avg Ask+Ask':>12} {'Avg Spread':>12} {'Min Bid+Bid':>12}")
    print("-" * 70)

    for bucket, data in time_buckets.items():
        if data:
            bid_bids = [d['bid_bid'] for d in data]
            ask_asks = [d['ask_ask'] for d in data]
            spreads = [d['spread'] for d in data]
            print(f"{bucket:<12} {len(data):>8} ${np.mean(bid_bids):>10.4f} ${np.mean(ask_asks):>10.4f} "
                  f"${np.mean(spreads):>10.4f} ${np.min(bid_bids):>10.4f}")

    # Check for opportunities: when is bid+bid < 0.94?
    print("\n" + "=" * 80)
    print("OPPORTUNITIES: Samples where bid+bid < $0.94")
    print("=" * 80)

    for bucket, data in time_buckets.items():
        if data:
            opportunities = [d for d in data if d['bid_bid'] < 0.94]
            pct = 100 * len(opportunities) / len(data) if data else 0
            avg_bid = np.mean([d['bid_bid'] for d in opportunities]) if opportunities else 0
            print(f"{bucket}: {len(opportunities):>5}/{len(data):<5} ({pct:5.1f}%) "
                  f"avg bid+bid when < 0.94: ${avg_bid:.4f}")

    # Analyze extreme price movements
    print("\n" + "=" * 80)
    print("PRICE EXTREMES (Where min_ask hit really low)")
    print("=" * 80)

    for slug in complete[:5]:
        mdf = df[df['market_slug'] == slug]
        up_min = mdf['up_ask'].min()
        down_min = mdf['down_ask'].min()
        up_min_time = mdf.loc[mdf['up_ask'].idxmin(), 'time_remaining_secs']
        down_min_time = mdf.loc[mdf['down_ask'].idxmin(), 'time_remaining_secs']

        print(f"\n{slug}:")
        print(f"  UP min_ask:   ${up_min:.2f} at t={up_min_time:.0f}s")
        print(f"  DOWN min_ask: ${down_min:.2f} at t={down_min_time:.0f}s")
        print(f"  Sum of mins:  ${up_min + down_min:.2f}")
        print(f"  (But these don't happen at same time!)")

    # Key insight: when do BOTH sides have low asks?
    print("\n" + "=" * 80)
    print("SIMULTANEOUS LOW ASKS (bid+bid at each sample)")
    print("=" * 80)

    all_bid_bids = []
    for slug in complete:
        mdf = df[df['market_slug'] == slug]
        for idx, row in mdf.iterrows():
            all_bid_bids.append(row['up_bid'] + row['down_bid'])

    print(f"\nAcross ALL {len(all_bid_bids):,} samples:")
    print(f"  Avg bid+bid: ${np.mean(all_bid_bids):.4f}")
    print(f"  Min bid+bid: ${np.min(all_bid_bids):.4f}")
    print(f"  Max bid+bid: ${np.max(all_bid_bids):.4f}")
    print(f"  Samples with bid+bid < $0.94: {sum(1 for b in all_bid_bids if b < 0.94):,} ({100*sum(1 for b in all_bid_bids if b < 0.94)/len(all_bid_bids):.1f}%)")
    print(f"  Samples with bid+bid < $0.90: {sum(1 for b in all_bid_bids if b < 0.90):,} ({100*sum(1 for b in all_bid_bids if b < 0.90)/len(all_bid_bids):.1f}%)")
    print(f"  Samples with bid+bid < $0.80: {sum(1 for b in all_bid_bids if b < 0.80):,} ({100*sum(1 for b in all_bid_bids if b < 0.80)/len(all_bid_bids):.1f}%)")

    print("\n" + "=" * 80)
    print("CONCLUSION")
    print("=" * 80)
    print(f"""
The data shows:
1. Bid+Bid averages ${np.mean(all_bid_bids):.2f} across all samples
2. Min bid+bid seen: ${np.min(all_bid_bids):.2f}
3. Only {100*sum(1 for b in all_bid_bids if b < 0.94)/len(all_bid_bids):.1f}% of samples have bid+bid < $0.94

PROBLEM: These markets have TIGHT spreads by design.
  - They're binary outcomes (UP vs DOWN)
  - Bid + Bid ≈ $0.99 (market efficiency)
  - Very little room for spread capture

TO BE PROFITABLE, you need either:
1. Much higher volume (100+ fills/market) to capture tiny margins
2. Different markets with wider spreads
3. Take directional risk (but velocity accuracy is only 42%)
    """)


if __name__ == "__main__":
    main()
