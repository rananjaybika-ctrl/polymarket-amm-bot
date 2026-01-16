#!/usr/bin/env python3
"""
Velocity-Based LOWER Loser Bid Test

CORRECTED LOGIC:
- velocity > 0 (UP winning, DOWN losing):
  - UP (winner): normal offset → try to fill at market
  - DOWN (loser): LOWER bid (negative adjustment) → wait for cheaper price

- velocity < 0 (DOWN winning, UP losing):
  - DOWN (winner): normal offset → try to fill at market
  - UP (loser): LOWER bid → wait for cheaper price

Goal: Get BETTER fill prices on the loser by waiting for it to drop more
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


def simulate_lower_loser(df, base_offset=0.01, velocity_scale=0.01, order_size=10.0):
    """
    LOWER the loser's bid based on velocity magnitude:

    - velocity > 0 (UP winning, DOWN losing):
      - UP bid = best_bid + base_offset (normal)
      - DOWN bid = best_bid + base_offset - (velocity * scale)  ← LOWER!

    - velocity < 0 (DOWN winning, UP losing):
      - DOWN bid = best_bid + base_offset (normal)
      - UP bid = best_bid + base_offset - (|velocity| * scale)  ← LOWER!

    Idea: When a side is losing, wait for even better prices
    """
    results = []
    fill_details = []
    markets = df['market_slug'].unique()

    for market_slug in markets:
        mdf = df[df['market_slug'] == market_slug].sort_values('timestamp_ms').reset_index(drop=True)
        if len(mdf) < 50:
            continue

        pos = Position()
        market_fills = []

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

            # CORRECTED LOGIC: LOWER the loser's bid
            if velocity > 0.3:  # UP winning → DOWN is loser
                # UP: normal
                up_adjustment = base_offset
                # DOWN: LOWER bid (subtract velocity-scaled amount)
                down_adjustment = base_offset - (velocity * velocity_scale)
                down_adjustment = max(0.001, down_adjustment)  # Don't go negative
            elif velocity < -0.3:  # DOWN winning → UP is loser
                # DOWN: normal
                down_adjustment = base_offset
                # UP: LOWER bid
                up_adjustment = base_offset - (abs(velocity) * velocity_scale)
                up_adjustment = max(0.001, up_adjustment)
            else:  # Neutral
                up_adjustment = base_offset
                down_adjustment = base_offset

            # Calculate our bids
            our_up_bid = min(up_bid + up_adjustment, up_ask - 0.01)
            our_down_bid = min(down_bid + down_adjustment, down_ask - 0.01)
            our_up_bid = max(0.01, our_up_bid)
            our_down_bid = max(0.01, our_down_bid)

            # Check fills
            next_up_bid = next_row.get('up_bid')
            next_down_bid = next_row.get('down_bid')

            if not pd.isna(next_up_bid) and next_up_bid < up_bid - 0.005:
                if pos.up_shares < 200:
                    pos.up_shares += order_size
                    pos.up_cost += our_up_bid * order_size
                    market_fills.append({
                        'side': 'UP',
                        'price': our_up_bid,
                        'best_bid': up_bid,
                        'adjustment': up_adjustment,
                        'velocity': velocity,
                        'is_loser': velocity < -0.3,
                    })

            if not pd.isna(next_down_bid) and next_down_bid < down_bid - 0.005:
                if pos.down_shares < 200:
                    pos.down_shares += order_size
                    pos.down_cost += our_down_bid * order_size
                    market_fills.append({
                        'side': 'DOWN',
                        'price': our_down_bid,
                        'best_bid': down_bid,
                        'adjustment': down_adjustment,
                        'velocity': velocity,
                        'is_loser': velocity > 0.3,
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
            })
            fill_details.extend(market_fills)

    return results, fill_details


def simulate_static(df, base_offset=0.01, order_size=10.0):
    """Static baseline."""
    results = []
    fill_details = []
    markets = df['market_slug'].unique()

    for market_slug in markets:
        mdf = df[df['market_slug'] == market_slug].sort_values('timestamp_ms').reset_index(drop=True)
        if len(mdf) < 50:
            continue

        pos = Position()
        market_fills = []

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

            velocity = row.get('velocity', 0)
            if pd.isna(velocity):
                velocity = 0

            if not pd.isna(next_up_bid) and next_up_bid < up_bid - 0.005:
                if pos.up_shares < 200:
                    pos.up_shares += order_size
                    pos.up_cost += our_up_bid * order_size
                    market_fills.append({
                        'side': 'UP',
                        'price': our_up_bid,
                        'best_bid': up_bid,
                        'adjustment': base_offset,
                        'velocity': velocity,
                    })

            if not pd.isna(next_down_bid) and next_down_bid < down_bid - 0.005:
                if pos.down_shares < 200:
                    pos.down_shares += order_size
                    pos.down_cost += our_down_bid * order_size
                    market_fills.append({
                        'side': 'DOWN',
                        'price': our_down_bid,
                        'best_bid': down_bid,
                        'adjustment': base_offset,
                        'velocity': velocity,
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
            })
            fill_details.extend(market_fills)

    return results, fill_details


def show_example_market(df, market_slug, base_offset=0.01, velocity_scale=0.02):
    """Show tick-by-tick comparison."""
    print(f"\n{'='*140}")
    print(f"EXAMPLE: {market_slug}")
    print("="*140)

    mdf = df[df['market_slug'] == market_slug].sort_values('timestamp_ms').reset_index(drop=True)

    # Find a period with high velocity
    high_vel_idx = mdf[mdf['velocity'].abs() > 0.5].index
    if len(high_vel_idx) > 0:
        start = max(0, high_vel_idx[0] - 5)
    else:
        start = 0

    print(f"\n{'Tick':>4} | {'Vel':>6} | {'UP_bid':>7} | {'DN_bid':>7} | {'Static UP':>10} | {'Static DN':>10} | {'Dyn UP':>10} | {'Dyn DN':>10} | Notes")
    print("-"*120)

    for i in range(start, min(start + 30, len(mdf))):
        row = mdf.iloc[i]

        velocity = row.get('velocity', 0)
        if pd.isna(velocity):
            velocity = 0

        up_bid, up_ask = row['up_bid'], row['up_ask']
        down_bid, down_ask = row['down_bid'], row['down_ask']

        if pd.isna(up_bid) or pd.isna(up_ask):
            continue

        # Static
        static_up = min(up_bid + base_offset, up_ask - 0.01)
        static_down = min(down_bid + base_offset, down_ask - 0.01)

        # Dynamic (lower loser)
        if velocity > 0.3:
            dyn_up_adj = base_offset
            dyn_down_adj = max(0.001, base_offset - velocity * velocity_scale)
            note = f"UP winning → DN bid LOWER by ${velocity * velocity_scale:.3f}"
        elif velocity < -0.3:
            dyn_up_adj = max(0.001, base_offset - abs(velocity) * velocity_scale)
            dyn_down_adj = base_offset
            note = f"DN winning → UP bid LOWER by ${abs(velocity) * velocity_scale:.3f}"
        else:
            dyn_up_adj = base_offset
            dyn_down_adj = base_offset
            note = "neutral"

        dyn_up = min(up_bid + dyn_up_adj, up_ask - 0.01)
        dyn_down = min(down_bid + dyn_down_adj, down_ask - 0.01)

        print(f"{i:>4} | {velocity:>+5.2f} | ${up_bid:>5.2f} | ${down_bid:>5.2f} | ${static_up:>8.3f} | ${static_down:>8.3f} | ${dyn_up:>8.3f} | ${dyn_down:>8.3f} | {note}")


def main():
    print("="*80)
    print("VELOCITY-BASED LOWER LOSER BID TEST")
    print("="*80)

    print("""
CORRECTED LOGIC:
----------------
When velocity > 0 (UP winning, DOWN losing):
  - UP (winner): bid = best_bid + 0.01 (normal)
  - DOWN (loser): bid = best_bid + 0.01 - (velocity * scale)  ← LOWER!

When velocity < -0.3 (DOWN winning, UP losing):
  - DOWN (winner): bid = best_bid + 0.01 (normal)
  - UP (loser): bid = best_bid + 0.01 - (|velocity| * scale)  ← LOWER!

GOAL: Get BETTER (cheaper) fill prices on the loser by waiting
""")

    df = load_data()
    print(f"\nLoaded {len(df)} observations, {df['market_slug'].nunique()} markets")

    # Test different scales
    print("\n" + "="*80)
    print("TESTING DIFFERENT VELOCITY SCALES")
    print("="*80)

    static_results, static_fills = simulate_static(df)

    scales = [0.005, 0.01, 0.02, 0.03, 0.05]

    print(f"\n| Strategy            | UP Fills | DN Fills | Avg UP$ | Avg DN$ | Pair Cost | Profitable | Profit |")
    print(f"|---------------------|----------|----------|---------|---------|-----------|------------|--------|")

    # Static baseline
    static_df = pd.DataFrame(static_results)
    fills_df = pd.DataFrame(static_fills)
    with_pairs = static_df[static_df['pairs'] > 0]

    up_fills = fills_df[fills_df['side'] == 'UP']
    down_fills = fills_df[fills_df['side'] == 'DOWN']

    if len(with_pairs) > 0:
        profitable = with_pairs[with_pairs['pair_cost'] < 1.0]
        print(f"| Static (baseline)   | {len(up_fills):>8} | {len(down_fills):>8} | ${up_fills['price'].mean():.3f} | ${down_fills['price'].mean():.3f} | ${with_pairs['pair_cost'].mean():.4f}  | {len(profitable)/len(with_pairs)*100:>9.1f}% | ${profitable['profit'].sum():>5.2f} |")

    best_scale = None
    best_profit = -999
    best_cost = 999

    for scale in scales:
        results, fills = simulate_lower_loser(df, velocity_scale=scale)
        rdf = pd.DataFrame(results)
        fills_df = pd.DataFrame(fills)

        up_fills = fills_df[fills_df['side'] == 'UP']
        down_fills = fills_df[fills_df['side'] == 'DOWN']

        with_pairs = rdf[rdf['pairs'] > 0]

        if len(with_pairs) > 0:
            profitable = with_pairs[with_pairs['pair_cost'] < 1.0]
            profit = profitable['profit'].sum()
            avg_cost = with_pairs['pair_cost'].mean()

            print(f"| Lower loser s={scale:.3f} | {len(up_fills):>8} | {len(down_fills):>8} | ${up_fills['price'].mean():.3f} | ${down_fills['price'].mean():.3f} | ${avg_cost:.4f}  | {len(profitable)/len(with_pairs)*100:>9.1f}% | ${profit:>5.2f} |")

            if avg_cost < best_cost:
                best_cost = avg_cost
                best_scale = scale
                best_profit = profit

    print(f"\nBest scale: {best_scale} with pair cost ${best_cost:.4f}")

    # Show example
    markets_with_vel = df[df['velocity'].abs() > 0.5]['market_slug'].unique()
    if len(markets_with_vel) > 0:
        show_example_market(df, markets_with_vel[0], velocity_scale=0.02)

    # Analyze fill prices
    print("\n" + "="*80)
    print("FILL PRICE ANALYSIS")
    print("="*80)

    _, static_fills = simulate_static(df)
    _, dynamic_fills = simulate_lower_loser(df, velocity_scale=best_scale or 0.02)

    static_df = pd.DataFrame(static_fills)
    dynamic_df = pd.DataFrame(dynamic_fills)

    print("\n| Metric                  | Static    | Dynamic (lower loser) |")
    print("|-------------------------|-----------|----------------------|")

    # During high velocity periods
    static_high_vel = static_df[static_df['velocity'].abs() > 0.5]
    dynamic_high_vel = dynamic_df[dynamic_df['velocity'].abs() > 0.5]

    if len(static_high_vel) > 0 and len(dynamic_high_vel) > 0:
        print(f"| High-vel fills          | {len(static_high_vel):>9} | {len(dynamic_high_vel):>20} |")
        print(f"| High-vel avg price      | ${static_high_vel['price'].mean():.4f}  | ${dynamic_high_vel['price'].mean():.4f}              |")

    # Loser fills specifically
    if 'is_loser' in dynamic_df.columns:
        loser_fills = dynamic_df[dynamic_df['is_loser'] == True]
        if len(loser_fills) > 0:
            print(f"| Loser-side fills        |     N/A   | {len(loser_fills):>20} |")
            print(f"| Loser avg price         |     N/A   | ${loser_fills['price'].mean():.4f}              |")
            print(f"| Loser avg adjustment    |     N/A   | ${loser_fills['adjustment'].mean():.4f}              |")

    # KEY INSIGHT
    print("\n" + "="*80)
    print("KEY INSIGHT")
    print("="*80)

    print("""
WHAT LOWERING LOSER BID DOES:
-----------------------------
When velocity > 0.5 (strong UP move):
  Static:  DOWN bid = best_bid + $0.01 = e.g., $0.60 + $0.01 = $0.61
  Dynamic: DOWN bid = best_bid + $0.01 - (0.5 * 0.02) = $0.60 + $0.00 = $0.60

We're posting LOWER on the loser → we fill at a CHEAPER price IF it drops to us.

THE TRADEOFF:
1. BETTER price when we DO fill (cheaper loser)
2. FEWER fills on loser (bid is further from market)

Does the cheaper price compensate for fewer fills?
Check the pair cost above to see if this helps.
""")


if __name__ == "__main__":
    main()
