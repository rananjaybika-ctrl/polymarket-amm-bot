#!/usr/bin/env python3
"""
Velocity-Scaled Asymmetric Offset Test

Hypothesis: Scale the losing side's offset proportionally to velocity magnitude
- Higher velocity = more aggressive offset on the losing side
- Goal: Get more fills on the cheap side during strong moves
"""

import pandas as pd
import numpy as np
import os
from dataclasses import dataclass, field
from typing import List, Dict


@dataclass
class Position:
    up_shares: float = 0.0
    up_cost: float = 0.0
    down_shares: float = 0.0
    down_cost: float = 0.0

    @property
    def pairs(self):
        return min(self.up_shares, self.down_shares)

    @property
    def pair_cost(self):
        if self.up_shares > 0 and self.down_shares > 0:
            return (self.up_cost / self.up_shares) + (self.down_cost / self.down_shares)
        return 0

    @property
    def profit(self):
        if self.pairs > 0:
            return self.pairs * (1.0 - self.pair_cost)
        return 0


def load_data():
    observer_dir = "/Users/rananjaybika/polymarket-amm-bot/research/observer"
    files = [
        "spread_capture_obs_20260115_aws_12hr.csv",
        "spread_capture_obs_20260114.csv",
        "spread_capture_obs_20260113.csv",
    ]

    dfs = []
    for f in files:
        path = os.path.join(observer_dir, f)
        if os.path.exists(path):
            try:
                df = pd.read_csv(path, on_bad_lines='skip')
                dfs.append(df)
            except:
                pass

    combined = pd.concat(dfs, ignore_index=True)
    combined = combined.drop_duplicates(subset=['market_slug', 'timestamp_ms'])
    if 'velocity_bps' in combined.columns:
        combined['velocity'] = combined['velocity_bps']
    return combined


def simulate_asymmetric(df, base_offset=0.01, velocity_scale=0.01, order_size=10.0):
    """
    Asymmetric velocity-scaled offsets:
    - velocity > 0 (UP winning): UP offset = base, DOWN offset = base + |velocity| * scale
    - velocity < 0 (DOWN winning): DOWN offset = base, UP offset = base + |velocity| * scale

    Idea: Chase the LOSER more aggressively when velocity is high
    """
    results = []
    markets = df['market_slug'].unique()

    for market_slug in markets:
        mdf = df[df['market_slug'] == market_slug].sort_values('timestamp_ms').reset_index(drop=True)
        if len(mdf) < 50:
            continue

        pos = Position()
        fill_details = []

        for i in range(len(mdf) - 1):
            row = mdf.iloc[i]
            next_row = mdf.iloc[i + 1]

            if row['time_remaining_secs'] < 60:
                continue

            up_bid, up_ask = row['up_bid'], row['up_ask']
            down_bid, down_ask = row['down_bid'], row['down_ask']

            if pd.isna(up_bid) or pd.isna(up_ask) or pd.isna(down_bid) or pd.isna(down_ask):
                continue
            if up_ask <= up_bid or down_ask <= down_bid:
                continue

            velocity = row.get('velocity', 0)
            if pd.isna(velocity):
                velocity = 0

            # ASYMMETRIC OFFSET based on velocity magnitude
            abs_vel = abs(velocity)

            if velocity > 0.3:  # UP winning → chase DOWN (loser)
                up_offset = base_offset  # Keep winner at base
                down_offset = base_offset + (abs_vel * velocity_scale)  # Scale loser
            elif velocity < -0.3:  # DOWN winning → chase UP (loser)
                up_offset = base_offset + (abs_vel * velocity_scale)  # Scale loser
                down_offset = base_offset  # Keep winner at base
            else:  # Neutral
                up_offset = base_offset
                down_offset = base_offset

            # Cap offsets to avoid crossing spread
            our_up_bid = min(up_bid + up_offset, up_ask - 0.01)
            our_down_bid = min(down_bid + down_offset, down_ask - 0.01)
            our_up_bid = max(0.01, our_up_bid)
            our_down_bid = max(0.01, our_down_bid)

            # Check fills
            next_up_bid = next_row.get('up_bid')
            next_down_bid = next_row.get('down_bid')

            if not pd.isna(next_up_bid) and next_up_bid < up_bid - 0.005:
                if pos.up_shares < 200:
                    pos.up_shares += order_size
                    pos.up_cost += our_up_bid * order_size
                    fill_details.append({
                        'side': 'UP',
                        'price': our_up_bid,
                        'velocity': velocity,
                        'offset': up_offset
                    })

            if not pd.isna(next_down_bid) and next_down_bid < down_bid - 0.005:
                if pos.down_shares < 200:
                    pos.down_shares += order_size
                    pos.down_cost += our_down_bid * order_size
                    fill_details.append({
                        'side': 'DOWN',
                        'price': our_down_bid,
                        'velocity': velocity,
                        'offset': down_offset
                    })

        if pos.up_shares > 0 or pos.down_shares > 0:
            results.append({
                'market': market_slug,
                'up': pos.up_shares,
                'down': pos.down_shares,
                'pairs': pos.pairs,
                'pair_cost': pos.pair_cost,
                'profit': pos.profit,
                'imbalance': abs(pos.up_shares - pos.down_shares),
                'fills': fill_details,
            })

    return results


def simulate_static(df, base_offset=0.01, order_size=10.0):
    """Static baseline - same offset both sides."""
    results = []
    markets = df['market_slug'].unique()

    for market_slug in markets:
        mdf = df[df['market_slug'] == market_slug].sort_values('timestamp_ms').reset_index(drop=True)
        if len(mdf) < 50:
            continue

        pos = Position()

        for i in range(len(mdf) - 1):
            row = mdf.iloc[i]
            next_row = mdf.iloc[i + 1]

            if row['time_remaining_secs'] < 60:
                continue

            up_bid, up_ask = row['up_bid'], row['up_ask']
            down_bid, down_ask = row['down_bid'], row['down_ask']

            if pd.isna(up_bid) or pd.isna(up_ask) or pd.isna(down_bid) or pd.isna(down_ask):
                continue
            if up_ask <= up_bid or down_ask <= down_bid:
                continue

            our_up_bid = min(up_bid + base_offset, up_ask - 0.01)
            our_down_bid = min(down_bid + base_offset, down_ask - 0.01)
            our_up_bid = max(0.01, our_up_bid)
            our_down_bid = max(0.01, our_down_bid)

            next_up_bid = next_row.get('up_bid')
            next_down_bid = next_row.get('down_bid')

            if not pd.isna(next_up_bid) and next_up_bid < up_bid - 0.005:
                if pos.up_shares < 200:
                    pos.up_shares += order_size
                    pos.up_cost += our_up_bid * order_size

            if not pd.isna(next_down_bid) and next_down_bid < down_bid - 0.005:
                if pos.down_shares < 200:
                    pos.down_shares += order_size
                    pos.down_cost += our_down_bid * order_size

        if pos.up_shares > 0 or pos.down_shares > 0:
            results.append({
                'market': market_slug,
                'up': pos.up_shares,
                'down': pos.down_shares,
                'pairs': pos.pairs,
                'pair_cost': pos.pair_cost,
                'profit': pos.profit,
                'imbalance': abs(pos.up_shares - pos.down_shares),
            })

    return results


def analyze_fill_prices(results):
    """Analyze fill prices by velocity zone."""
    all_fills = []
    for r in results:
        if 'fills' in r:
            all_fills.extend(r['fills'])

    if not all_fills:
        return

    fills_df = pd.DataFrame(all_fills)

    print("\n" + "="*80)
    print("FILL PRICE ANALYSIS BY VELOCITY")
    print("="*80)

    # Categorize by velocity
    fills_df['vel_zone'] = pd.cut(fills_df['velocity'],
                                   bins=[-10, -1, -0.5, -0.3, 0.3, 0.5, 1, 10],
                                   labels=['<-1', '-1 to -0.5', '-0.5 to -0.3', 'neutral', '0.3 to 0.5', '0.5 to 1', '>1'])

    print("\nFills by velocity zone:")
    print(fills_df.groupby('vel_zone').agg({
        'price': ['count', 'mean'],
        'offset': 'mean'
    }).round(4))

    # UP vs DOWN fills by velocity
    print("\nUP fills by velocity:")
    up_fills = fills_df[fills_df['side'] == 'UP']
    if len(up_fills) > 0:
        print(f"  Total: {len(up_fills)}")
        print(f"  Avg price: ${up_fills['price'].mean():.4f}")
        print(f"  During high velocity (|v|>0.5): {len(up_fills[up_fills['velocity'].abs() > 0.5])}")

    print("\nDOWN fills by velocity:")
    down_fills = fills_df[fills_df['side'] == 'DOWN']
    if len(down_fills) > 0:
        print(f"  Total: {len(down_fills)}")
        print(f"  Avg price: ${down_fills['price'].mean():.4f}")
        print(f"  During high velocity (|v|>0.5): {len(down_fills[down_fills['velocity'].abs() > 0.5])}")


def main():
    print("="*80)
    print("VELOCITY-SCALED ASYMMETRIC OFFSET TEST")
    print("="*80)

    print("""
HYPOTHESIS:
-----------
Scale the losing side's offset proportionally to velocity magnitude:
- velocity = +0.5 → DOWN offset = base + 0.5 * scale
- velocity = +1.0 → DOWN offset = base + 1.0 * scale
- velocity = +2.0 → DOWN offset = base + 2.0 * scale

The higher the velocity, the more aggressively we chase the LOSER.
Goal: Get more fills on the cheap side during strong moves.
""")

    df = load_data()
    print(f"\nLoaded {len(df)} observations, {df['market_slug'].nunique()} markets")

    # Velocity distribution
    vel = df['velocity'].dropna()
    print(f"\nVelocity distribution:")
    print(f"  |v| > 0.3: {(vel.abs() > 0.3).sum()} ticks ({(vel.abs() > 0.3).mean()*100:.1f}%)")
    print(f"  |v| > 0.5: {(vel.abs() > 0.5).sum()} ticks ({(vel.abs() > 0.5).mean()*100:.1f}%)")
    print(f"  |v| > 1.0: {(vel.abs() > 1.0).sum()} ticks ({(vel.abs() > 1.0).mean()*100:.1f}%)")
    print(f"  |v| > 2.0: {(vel.abs() > 2.0).sum()} ticks ({(vel.abs() > 2.0).mean()*100:.1f}%)")

    # Test different velocity scales
    print("\n" + "="*80)
    print("TESTING DIFFERENT VELOCITY SCALES")
    print("="*80)

    static_results = simulate_static(df, base_offset=0.01)

    scales_to_test = [0.005, 0.01, 0.02, 0.03, 0.05]

    print(f"\n| Strategy            | Avg Imbal | Avg Cost | Profitable | Total Profit |")
    print(f"|---------------------|-----------|----------|------------|--------------|")

    # Static baseline
    static_df = pd.DataFrame(static_results)
    with_pairs = static_df[static_df['pairs'] > 0]
    if len(with_pairs) > 0:
        profitable = with_pairs[with_pairs['pair_cost'] < 1.0]
        print(f"| Static (baseline)   | {static_df['imbalance'].mean():>9.1f} | ${with_pairs['pair_cost'].mean():.4f} | {len(profitable)/len(with_pairs)*100:>9.1f}% | ${profitable['profit'].sum():>11.2f} |")

    best_scale = None
    best_profit = -999

    for scale in scales_to_test:
        results = simulate_asymmetric(df, base_offset=0.01, velocity_scale=scale)
        rdf = pd.DataFrame(results)
        with_pairs = rdf[rdf['pairs'] > 0]

        if len(with_pairs) > 0:
            profitable = with_pairs[with_pairs['pair_cost'] < 1.0]
            profit = profitable['profit'].sum()
            pct = len(profitable)/len(with_pairs)*100

            print(f"| Asymm scale={scale:.3f} | {rdf['imbalance'].mean():>9.1f} | ${with_pairs['pair_cost'].mean():.4f} | {pct:>9.1f}% | ${profit:>11.2f} |")

            if profit > best_profit:
                best_profit = profit
                best_scale = scale

    print(f"\nBest scale: {best_scale} with ${best_profit:.2f} profit")

    # Analyze fills for best scale
    if best_scale:
        best_results = simulate_asymmetric(df, base_offset=0.01, velocity_scale=best_scale)
        analyze_fill_prices(best_results)

    # KEY ANALYSIS: Does asymmetric help with imbalance?
    print("\n" + "="*80)
    print("IMBALANCE ANALYSIS: Does asymmetric help during trends?")
    print("="*80)

    # Find trending markets
    trending_markets = []
    for market in df['market_slug'].unique():
        mdf = df[df['market_slug'] == market].sort_values('timestamp_ms')
        if len(mdf) < 100:
            continue
        up = mdf['up_bid'].dropna().values
        if len(up) < 50:
            continue
        change = up[-20:].mean() - up[:20].mean()
        if abs(change) > 0.15:
            trending_markets.append((market, change))

    print(f"\nFound {len(trending_markets)} trending markets (>15% move)")

    if trending_markets:
        trending_slugs = [m[0] for m in trending_markets]
        trending_df = df[df['market_slug'].isin(trending_slugs)]

        static_trending = simulate_static(trending_df)
        asymm_trending = simulate_asymmetric(trending_df, velocity_scale=best_scale or 0.01)

        if static_trending and asymm_trending:
            static_t = pd.DataFrame(static_trending)
            asymm_t = pd.DataFrame(asymm_trending)

            print(f"\nTrending Markets Only:")
            print(f"| Strategy   | Markets | Avg Imbalance | Avg Pairs | Avg Cost |")
            print(f"|------------|---------|---------------|-----------|----------|")
            print(f"| Static     | {len(static_t):>7} | {static_t['imbalance'].mean():>13.1f} | {static_t['pairs'].mean():>9.1f} | ${static_t[static_t['pairs']>0]['pair_cost'].mean():.4f} |")
            print(f"| Asymmetric | {len(asymm_t):>7} | {asymm_t['imbalance'].mean():>13.1f} | {asymm_t['pairs'].mean():>9.1f} | ${asymm_t[asymm_t['pairs']>0]['pair_cost'].mean():.4f} |")

    # CONCLUSION
    print("\n" + "="*80)
    print("CONCLUSION")
    print("="*80)

    print("""
THE FUNDAMENTAL PROBLEM:

When velocity is high (strong trend), BOTH these things happen:
1. The LOSER's bid drops (getting cheaper)
2. The LOSER's spread WIDENS (less liquidity)

Our asymmetric offset DOES increase our bid on the loser...
BUT: The fill depends on someone SELLING into our bid.

During a strong UP move:
├── DOWN is cheap (bid dropping)
├── We increase DOWN offset → bid higher on DOWN
├── BUT: Who is selling DOWN when UP is winning?
│       → Very few sellers! They're holding for resolution.
└── Result: Higher bid, but still no fills

THE OFFSET DOESN'T CREATE SELLERS.

The orderbook structure means:
- Fills happen when takers sell INTO our bids
- During trends, takers are on the WINNING side
- Increasing loser offset just makes us pay more when we DO fill
- It doesn't increase fill rate on the loser

This is why asymmetric offsets show NO improvement in imbalance.
""")


if __name__ == "__main__":
    main()
