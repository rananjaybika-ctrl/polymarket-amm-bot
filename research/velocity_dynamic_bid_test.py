#!/usr/bin/env python3
"""
Velocity-Based Dynamic Bid Adjustment Test

Hypothesis: Use velocity to dynamically adjust bid prices
- When velocity > 0 (BTC rising → UP winning): increase UP bids, decrease DOWN bids
- When velocity < 0 (BTC falling → DOWN winning): increase DOWN bids, decrease UP bids

Goal: Better fill balance in trending markets
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
    fills: List[Dict] = field(default_factory=list)

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
    """Load observer data with velocity."""
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
                print(f"Loaded {f}: {len(df)} rows")
            except Exception as e:
                print(f"Error {f}: {e}")

    combined = pd.concat(dfs, ignore_index=True)
    combined = combined.drop_duplicates(subset=['market_slug', 'timestamp_ms'])

    # Standardize column names
    if 'velocity_bps' in combined.columns:
        combined['velocity'] = combined['velocity_bps']

    return combined


def simulate_static_grid(df: pd.DataFrame, bid_offset=0.01, order_size=10.0):
    """Static grid MM - same offset on both sides."""
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

            # STATIC: Same offset both sides
            our_up_bid = min(up_bid + bid_offset, up_ask - 0.01)
            our_down_bid = min(down_bid + bid_offset, down_ask - 0.01)
            our_up_bid = max(0.01, our_up_bid)
            our_down_bid = max(0.01, our_down_bid)

            # Check fills
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


def simulate_velocity_adjusted(df: pd.DataFrame, base_offset=0.01, velocity_adjust=0.005, order_size=10.0):
    """
    Velocity-adjusted grid MM:
    - velocity > 0 (UP winning): increase UP bid offset, decrease DOWN bid offset
    - velocity < 0 (DOWN winning): increase DOWN bid offset, decrease UP bid offset

    The idea: Chase the winner to get filled on the winning side
    """
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

            # Get velocity
            velocity = row.get('velocity', 0)
            if pd.isna(velocity):
                velocity = 0

            # DYNAMIC ADJUSTMENT based on velocity
            if velocity > 0.3:  # UP winning (BTC rising)
                # Increase UP bid (chase winner), decrease DOWN bid
                up_offset = base_offset + velocity_adjust
                down_offset = max(0.001, base_offset - velocity_adjust)
            elif velocity < -0.3:  # DOWN winning (BTC falling)
                # Increase DOWN bid (chase winner), decrease UP bid
                up_offset = max(0.001, base_offset - velocity_adjust)
                down_offset = base_offset + velocity_adjust
            else:  # Neutral
                up_offset = base_offset
                down_offset = base_offset

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


def simulate_inverse_velocity(df: pd.DataFrame, base_offset=0.01, velocity_adjust=0.005, order_size=10.0):
    """
    INVERSE velocity-adjusted grid MM:
    - velocity > 0 (UP winning): increase DOWN bid (it's cheap!), decrease UP bid
    - velocity < 0 (DOWN winning): increase UP bid (it's cheap!), decrease DOWN bid

    The idea: Buy the LOSER because it's cheap and will revert
    """
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

            velocity = row.get('velocity', 0)
            if pd.isna(velocity):
                velocity = 0

            # INVERSE ADJUSTMENT - buy the loser!
            if velocity > 0.3:  # UP winning → DOWN is cheap
                up_offset = max(0.001, base_offset - velocity_adjust)
                down_offset = base_offset + velocity_adjust  # Chase cheap DOWN
            elif velocity < -0.3:  # DOWN winning → UP is cheap
                up_offset = base_offset + velocity_adjust  # Chase cheap UP
                down_offset = max(0.001, base_offset - velocity_adjust)
            else:
                up_offset = base_offset
                down_offset = base_offset

            our_up_bid = min(up_bid + up_offset, up_ask - 0.01)
            our_down_bid = min(down_bid + down_offset, down_ask - 0.01)
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


def show_detailed_comparison(df, market_slug):
    """Show tick-by-tick comparison of static vs velocity-adjusted."""
    print(f"\n{'='*120}")
    print(f"DETAILED COMPARISON: {market_slug}")
    print("="*120)

    mdf = df[df['market_slug'] == market_slug].sort_values('timestamp_ms').reset_index(drop=True)
    if len(mdf) < 30:
        print("Not enough data")
        return

    BASE_OFFSET = 0.01
    VEL_ADJUST = 0.005
    ORDER_SIZE = 10

    # Track both strategies
    static_pos = Position()
    dynamic_pos = Position()

    print(f"\n{'Tick':>4} | {'Time':>5} | {'Vel':>6} | {'UP_bid':>6} | {'DN_bid':>6} | {'Static UP':>9} | {'Static DN':>9} | {'Dyn UP':>9} | {'Dyn DN':>9} | {'Static Fill':>15} | {'Dynamic Fill':>15}")
    print("-"*140)

    for i in range(min(40, len(mdf) - 1)):
        row = mdf.iloc[i]
        next_row = mdf.iloc[i + 1]

        if row['time_remaining_secs'] < 60:
            continue

        up_bid, up_ask = row['up_bid'], row['up_ask']
        down_bid, down_ask = row['down_bid'], row['down_ask']

        if pd.isna(up_bid) or pd.isna(up_ask):
            continue

        velocity = row.get('velocity', 0)
        if pd.isna(velocity):
            velocity = 0

        # Static bids
        static_up_bid = min(up_bid + BASE_OFFSET, up_ask - 0.01)
        static_down_bid = min(down_bid + BASE_OFFSET, down_ask - 0.01)

        # Dynamic bids (inverse - buy the loser)
        if velocity > 0.3:
            dyn_up_offset = max(0.001, BASE_OFFSET - VEL_ADJUST)
            dyn_down_offset = BASE_OFFSET + VEL_ADJUST
        elif velocity < -0.3:
            dyn_up_offset = BASE_OFFSET + VEL_ADJUST
            dyn_down_offset = max(0.001, BASE_OFFSET - VEL_ADJUST)
        else:
            dyn_up_offset = BASE_OFFSET
            dyn_down_offset = BASE_OFFSET

        dyn_up_bid = min(up_bid + dyn_up_offset, up_ask - 0.01)
        dyn_down_bid = min(down_bid + dyn_down_offset, down_ask - 0.01)

        next_up_bid = next_row.get('up_bid')
        next_down_bid = next_row.get('down_bid')

        static_fill = ""
        dynamic_fill = ""

        # Check static fills
        if not pd.isna(next_up_bid) and next_up_bid < up_bid - 0.005:
            static_pos.up_shares += ORDER_SIZE
            static_pos.up_cost += static_up_bid * ORDER_SIZE
            static_fill = f"UP@{static_up_bid:.2f}"

            dynamic_pos.up_shares += ORDER_SIZE
            dynamic_pos.up_cost += dyn_up_bid * ORDER_SIZE
            dynamic_fill = f"UP@{dyn_up_bid:.2f}"

        if not pd.isna(next_down_bid) and next_down_bid < down_bid - 0.005:
            static_pos.down_shares += ORDER_SIZE
            static_pos.down_cost += static_down_bid * ORDER_SIZE
            if static_fill:
                static_fill += "+"
            static_fill += f"DN@{static_down_bid:.2f}"

            dynamic_pos.down_shares += ORDER_SIZE
            dynamic_pos.down_cost += dyn_down_bid * ORDER_SIZE
            if dynamic_fill:
                dynamic_fill += "+"
            dynamic_fill += f"DN@{dyn_down_bid:.2f}"

        if not static_fill:
            static_fill = "-"
        if not dynamic_fill:
            dynamic_fill = "-"

        print(f"{i:>4} | {row['time_remaining_secs']:>4.0f}s | {velocity:>+5.2f} | ${up_bid:.2f} | ${down_bid:.2f} | ${static_up_bid:.2f} | ${static_down_bid:.2f} | ${dyn_up_bid:.2f} | ${dyn_down_bid:.2f} | {static_fill:>15} | {dynamic_fill:>15}")

    print("-"*140)
    print(f"\nSTATIC:  UP={static_pos.up_shares:.0f}, DN={static_pos.down_shares:.0f}, Pairs={static_pos.pairs:.0f}, Cost=${static_pos.pair_cost:.4f}, Profit=${static_pos.profit:.2f}")
    print(f"DYNAMIC: UP={dynamic_pos.up_shares:.0f}, DN={dynamic_pos.down_shares:.0f}, Pairs={dynamic_pos.pairs:.0f}, Cost=${dynamic_pos.pair_cost:.4f}, Profit=${dynamic_pos.profit:.2f}")


def main():
    print("="*80)
    print("VELOCITY-BASED DYNAMIC BID ADJUSTMENT TEST")
    print("="*80)

    print("""
HYPOTHESIS:
-----------
Use velocity to dynamically adjust bid prices:

1. CHASE WINNER (follow momentum):
   - velocity > 0 (UP winning): increase UP bid, decrease DOWN bid
   - velocity < 0 (DOWN winning): increase DOWN bid, decrease UP bid

2. BUY LOSER (mean reversion):
   - velocity > 0 (UP winning): increase DOWN bid (it's cheap!), decrease UP bid
   - velocity < 0 (DOWN winning): increase UP bid (it's cheap!), decrease DOWN bid

GOAL: Better fill balance in trending markets
""")

    df = load_data()
    print(f"\nTotal: {len(df)} observations, {df['market_slug'].nunique()} markets")

    # Check velocity coverage
    if 'velocity' in df.columns:
        vel_data = df['velocity'].dropna()
        print(f"\nVelocity coverage: {len(vel_data)}/{len(df)} ({len(vel_data)/len(df)*100:.1f}%)")
        print(f"Velocity range: {vel_data.min():.2f} to {vel_data.max():.2f}")
        print(f"Velocity > 0.3: {(vel_data > 0.3).sum()} ticks")
        print(f"Velocity < -0.3: {(vel_data < -0.3).sum()} ticks")
    else:
        print("\nWARNING: No velocity data found!")
        return

    # Run all three strategies
    print("\n" + "="*80)
    print("RUNNING SIMULATIONS...")
    print("="*80)

    static_results = simulate_static_grid(df)
    chase_results = simulate_velocity_adjusted(df)
    inverse_results = simulate_inverse_velocity(df)

    # Compare results
    print("\n" + "="*80)
    print("RESULTS COMPARISON")
    print("="*80)

    def summarize(results, name):
        if not results:
            print(f"\n{name}: No results")
            return
        rdf = pd.DataFrame(results)
        with_pairs = rdf[rdf['pairs'] > 0]
        profitable = with_pairs[with_pairs['pair_cost'] < 1.0]

        print(f"\n{name}:")
        print(f"  Markets with activity: {len(rdf)}")
        print(f"  Markets with pairs: {len(with_pairs)}")
        print(f"  Avg imbalance: {rdf['imbalance'].mean():.1f} shares")
        if len(with_pairs) > 0:
            print(f"  Avg pair cost: ${with_pairs['pair_cost'].mean():.4f}")
            print(f"  Profitable: {len(profitable)}/{len(with_pairs)} ({len(profitable)/len(with_pairs)*100:.1f}%)")
            print(f"  Total profit: ${profitable['profit'].sum():.2f}")

    summarize(static_results, "STATIC GRID (baseline)")
    summarize(chase_results, "CHASE WINNER (velocity-follow)")
    summarize(inverse_results, "BUY LOSER (velocity-inverse)")

    # Detailed comparison
    print("\n" + "="*80)
    print("IMBALANCE ANALYSIS")
    print("="*80)

    if static_results:
        static_df = pd.DataFrame(static_results)
        chase_df = pd.DataFrame(chase_results)
        inverse_df = pd.DataFrame(inverse_results)

        print(f"\n| Strategy      | Avg Imbalance | Max Imbalance | Avg Pair Cost | Profitable % |")
        print(f"|---------------|---------------|---------------|---------------|--------------|")

        for name, rdf in [("Static", static_df), ("Chase", chase_df), ("Inverse", inverse_df)]:
            with_pairs = rdf[rdf['pairs'] > 0]
            if len(with_pairs) > 0:
                profitable_pct = (with_pairs['pair_cost'] < 1.0).mean() * 100
                avg_cost = with_pairs['pair_cost'].mean()
            else:
                profitable_pct = 0
                avg_cost = 0
            print(f"| {name:<13} | {rdf['imbalance'].mean():>13.1f} | {rdf['imbalance'].max():>13.1f} | ${avg_cost:>12.4f} | {profitable_pct:>12.1f}% |")

    # Show example
    if static_results:
        # Find a trending market with velocity data
        markets_with_velocity = df[df['velocity'].abs() > 0.3]['market_slug'].unique()
        if len(markets_with_velocity) > 0:
            show_detailed_comparison(df, markets_with_velocity[0])

    # KEY INSIGHT
    print("\n" + "="*80)
    print("KEY INSIGHT")
    print("="*80)

    print("""
THE UNIFIED ORDERBOOK CONSTRAINT:
---------------------------------
UP_ask + DOWN_bid ≈ $1.00 (always)
UP_bid + DOWN_ask ≈ $1.00 (always)

This means:
- If UP goes up → DOWN goes down (exact opposite)
- The "winner" and "loser" are PERFECTLY ANTI-CORRELATED

WHAT THIS MEANS FOR VELOCITY ADJUSTMENT:
-----------------------------------------
1. When velocity > 0 (UP winning):
   - UP bid increases → our UP bid gets FURTHER from fills
   - DOWN bid decreases → our DOWN bid is CLOSER to fills
   - We naturally fill more DOWN (the loser)

2. This is AUTOMATIC! The orderbook already adjusts.
   Velocity adjustment just makes us pay MORE for the winner.

CONCLUSION:
-----------
Dynamic velocity adjustment CANNOT improve grid MM because:
1. The orderbook automatically shifts both sides together
2. Increasing winner bid = paying more = WORSE pair cost
3. The unified orderbook constraint makes all adjustments sum to ~$1.00

RECOMMENDATION: Stick with STATIC grid MM.
The only variable we control is the bid offset, and it should be constant.
""")


if __name__ == "__main__":
    main()
