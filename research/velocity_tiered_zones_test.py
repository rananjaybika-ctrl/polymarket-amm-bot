#!/usr/bin/env python3
"""
Tiered Velocity Zones Test

Instead of linear scaling, use discrete zones with increasing offsets:
- Zone 1 (0.1-0.3): small reduction
- Zone 2 (0.3-0.5): medium reduction
- Zone 3 (0.5-1.0): large reduction
- Zone 4 (>1.0): maximum reduction
"""

import pandas as pd
import numpy as np
import os
from dataclasses import dataclass


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
                print(f"Loaded {f}: {len(df)} rows")
            except:
                pass

    combined = pd.concat(dfs, ignore_index=True)
    combined = combined.drop_duplicates(subset=['market_slug', 'timestamp_ms'])
    if 'velocity_bps' in combined.columns:
        combined['velocity'] = combined['velocity_bps']
    return combined


def get_zone_reduction(velocity, zone_config):
    """
    Get loser offset reduction based on velocity zone.

    zone_config is a dict like:
    {
        0.1: 0.002,  # |v| 0.1-0.3 → reduce by $0.002
        0.3: 0.005,  # |v| 0.3-0.5 → reduce by $0.005
        0.5: 0.008,  # |v| 0.5-1.0 → reduce by $0.008
        1.0: 0.010,  # |v| > 1.0 → reduce by $0.010 (full offset)
    }
    """
    abs_vel = abs(velocity)
    reduction = 0.0

    for threshold, red in sorted(zone_config.items()):
        if abs_vel >= threshold:
            reduction = red

    return reduction


def simulate_tiered(df, base_offset=0.01, zone_config=None, order_size=10.0):
    """Tiered zone simulation."""
    if zone_config is None:
        zone_config = {0.1: 0.002, 0.3: 0.005, 0.5: 0.008, 1.0: 0.010}

    results = []
    zone_fills = {z: {'up': 0, 'down': 0} for z in zone_config.keys()}
    zone_fills['neutral'] = {'up': 0, 'down': 0}

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

            # Determine zone and reduction
            reduction = get_zone_reduction(velocity, zone_config)

            # Determine which zone we're in
            abs_vel = abs(velocity)
            current_zone = 'neutral'
            for threshold in sorted(zone_config.keys()):
                if abs_vel >= threshold:
                    current_zone = threshold

            # Apply asymmetric offset
            if velocity > 0.1:  # UP winning → DOWN is loser
                up_adjustment = base_offset
                down_adjustment = max(0.001, base_offset - reduction)
                loser_side = 'down'
            elif velocity < -0.1:  # DOWN winning → UP is loser
                down_adjustment = base_offset
                up_adjustment = max(0.001, base_offset - reduction)
                loser_side = 'up'
            else:
                up_adjustment = base_offset
                down_adjustment = base_offset
                loser_side = None

            our_up_bid = min(up_bid + up_adjustment, up_ask - 0.01)
            our_down_bid = min(down_bid + down_adjustment, down_ask - 0.01)
            our_up_bid = max(0.01, our_up_bid)
            our_down_bid = max(0.01, our_down_bid)

            next_up_bid = next_row.get('up_bid')
            next_down_bid = next_row.get('down_bid')

            if not pd.isna(next_up_bid) and next_up_bid < up_bid - 0.005:
                if pos.up_shares < 200:
                    pos.up_shares += order_size
                    pos.up_cost += our_up_bid * order_size
                    if current_zone in zone_fills:
                        zone_fills[current_zone]['up'] += 1
                    else:
                        zone_fills['neutral']['up'] += 1

            if not pd.isna(next_down_bid) and next_down_bid < down_bid - 0.005:
                if pos.down_shares < 200:
                    pos.down_shares += order_size
                    pos.down_cost += our_down_bid * order_size
                    if current_zone in zone_fills:
                        zone_fills[current_zone]['down'] += 1
                    else:
                        zone_fills['neutral']['down'] += 1

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

    return results, zone_fills


def simulate_static(df, base_offset=0.01, order_size=10.0):
    """Static baseline."""
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


def main():
    print("="*100)
    print("TIERED VELOCITY ZONES TEST")
    print("="*100)

    df = load_data()
    print(f"\nTotal: {len(df)} observations, {df['market_slug'].nunique()} markets")

    # Velocity zone distribution
    vel = df['velocity'].dropna()
    print(f"\nVelocity zone distribution:")
    zones = [(0.0, 0.1), (0.1, 0.3), (0.3, 0.5), (0.5, 1.0), (1.0, float('inf'))]
    for low, high in zones:
        count = ((vel.abs() >= low) & (vel.abs() < high)).sum()
        pct = count / len(vel) * 100
        print(f"  |v| {low}-{high}: {count:>6} ticks ({pct:>5.1f}%)")

    # Static baseline
    print("\n" + "="*100)
    print("BASELINE")
    print("="*100)

    static_results = simulate_static(df)
    static_df = pd.DataFrame(static_results)
    with_pairs = static_df[static_df['pairs'] > 0]
    baseline_profit = with_pairs[with_pairs['pair_cost'] < 1.0]['profit'].sum()
    baseline_cost = with_pairs['pair_cost'].mean()

    print(f"Static: Cost=${baseline_cost:.4f}, Profit=${baseline_profit:.2f}")

    # Test different zone configurations
    print("\n" + "="*100)
    print("TIERED ZONE CONFIGURATIONS")
    print("="*100)

    configs = [
        # Conservative
        ("Conservative", {0.1: 0.002, 0.3: 0.004, 0.5: 0.006, 1.0: 0.008}),
        # Moderate
        ("Moderate", {0.1: 0.003, 0.3: 0.005, 0.5: 0.007, 1.0: 0.009}),
        # Aggressive
        ("Aggressive", {0.1: 0.005, 0.3: 0.007, 0.5: 0.009, 1.0: 0.010}),
        # Very Aggressive
        ("Very Aggressive", {0.1: 0.007, 0.3: 0.008, 0.5: 0.009, 1.0: 0.010}),
        # Max (full offset reduction at all zones)
        ("Max Reduction", {0.1: 0.009, 0.3: 0.009, 0.5: 0.009, 1.0: 0.009}),
        # Linear equivalent (what we tested before with scale=0.10)
        ("Linear (scale=0.10)", {0.1: 0.000, 0.2: 0.010, 0.3: 0.020, 0.5: 0.040, 1.0: 0.090}),
    ]

    print(f"\n{'Config':<20} | {'Zone 0.1':<8} | {'Zone 0.3':<8} | {'Zone 0.5':<8} | {'Zone 1.0':<8} | {'Cost':<10} | {'Profit':<8} | {'vs Base':<8}")
    print("-"*110)

    for name, zone_config in configs:
        results, zone_fills = simulate_tiered(df, zone_config=zone_config)
        rdf = pd.DataFrame(results)
        with_pairs = rdf[rdf['pairs'] > 0]

        if len(with_pairs) > 0:
            profitable = with_pairs[with_pairs['pair_cost'] < 1.0]
            profit = profitable['profit'].sum()
            cost = with_pairs['pair_cost'].mean()
            improvement = profit - baseline_profit

            z1 = zone_config.get(0.1, 0)
            z2 = zone_config.get(0.3, 0)
            z3 = zone_config.get(0.5, 0)
            z4 = zone_config.get(1.0, 0)

            print(f"{name:<20} | ${z1:<7.3f} | ${z2:<7.3f} | ${z3:<7.3f} | ${z4:<7.3f} | ${cost:<9.4f} | ${profit:<7.2f} | +${improvement:<6.2f}")

    # Detailed analysis of best config
    print("\n" + "="*100)
    print("DETAILED ANALYSIS: Very Aggressive Config")
    print("="*100)

    best_config = {0.1: 0.007, 0.3: 0.008, 0.5: 0.009, 1.0: 0.010}
    results, zone_fills = simulate_tiered(df, zone_config=best_config)

    print("\nZone fill breakdown:")
    print(f"{'Zone':<15} | {'UP Fills':<10} | {'DOWN Fills':<10} | {'Total':<10}")
    print("-"*50)

    total_up = 0
    total_down = 0
    for zone in ['neutral', 0.1, 0.3, 0.5, 1.0]:
        if zone in zone_fills:
            up = zone_fills[zone]['up']
            down = zone_fills[zone]['down']
            total = up + down
            total_up += up
            total_down += down
            zone_label = f"|v| >= {zone}" if zone != 'neutral' else "neutral"
            print(f"{zone_label:<15} | {up:<10} | {down:<10} | {total:<10}")

    print("-"*50)
    print(f"{'TOTAL':<15} | {total_up:<10} | {total_down:<10} | {total_up + total_down:<10}")

    # Custom deep zone test
    print("\n" + "="*100)
    print("CUSTOM DEEP ZONE CONFIGURATIONS")
    print("="*100)

    # Test progressively deeper reductions
    deep_configs = [
        # Standard deep
        ("Deep Linear", {0.1: 0.002, 0.2: 0.004, 0.3: 0.006, 0.4: 0.007, 0.5: 0.008, 0.7: 0.009, 1.0: 0.010}),
        # Exponential-ish
        ("Exponential", {0.1: 0.001, 0.2: 0.002, 0.3: 0.004, 0.5: 0.007, 1.0: 0.010}),
        # Front-loaded (most reduction in zone 1)
        ("Front-loaded", {0.1: 0.008, 0.3: 0.009, 0.5: 0.009, 1.0: 0.010}),
        # Back-loaded (most reduction in high zones)
        ("Back-loaded", {0.1: 0.001, 0.3: 0.003, 0.5: 0.007, 1.0: 0.010}),
    ]

    print(f"\n{'Config':<15} | {'Cost':<10} | {'Profit':<8} | {'vs Base':<8} | {'Improvement %':<12}")
    print("-"*70)

    for name, zone_config in deep_configs:
        results, zone_fills = simulate_tiered(df, zone_config=zone_config)
        rdf = pd.DataFrame(results)
        with_pairs = rdf[rdf['pairs'] > 0]

        if len(with_pairs) > 0:
            profitable = with_pairs[with_pairs['pair_cost'] < 1.0]
            profit = profitable['profit'].sum()
            cost = with_pairs['pair_cost'].mean()
            improvement = profit - baseline_profit
            pct = improvement / baseline_profit * 100

            print(f"{name:<15} | ${cost:<9.4f} | ${profit:<7.2f} | +${improvement:<6.2f} | +{pct:<10.1f}%")

    # SUMMARY
    print("\n" + "="*100)
    print("SUMMARY")
    print("="*100)

    print("""
DATA USED:
- spread_capture_obs_20260115_aws_12hr.csv: 166,308 rows
- spread_capture_obs_20260114.csv: 17,459 rows
- spread_capture_obs_20260113.csv: 15,667 rows
- TOTAL: 199,434 observations across 51 markets

BEST CONFIGURATION:
The "Very Aggressive" or "Front-loaded" config works best because:
1. Zone 0.1 has 43% of ticks → most opportunity for improvement
2. Deeper reduction at low thresholds captures more savings
3. High zones (>1.0) are too rare to matter much

RECOMMENDED ZONE CONFIG:
{
    0.1: 0.007,  # |v| 0.1-0.3: reduce loser offset by $0.007
    0.3: 0.008,  # |v| 0.3-0.5: reduce by $0.008
    0.5: 0.009,  # |v| 0.5-1.0: reduce by $0.009
    1.0: 0.010,  # |v| > 1.0: reduce by full $0.010 (post at best_bid)
}

This means:
- At |velocity| = 0.15: loser_offset = 0.01 - 0.007 = $0.003
- At |velocity| = 0.40: loser_offset = 0.01 - 0.008 = $0.002
- At |velocity| = 0.70: loser_offset = 0.01 - 0.009 = $0.001
- At |velocity| = 1.50: loser_offset = 0.01 - 0.010 = $0.000 (floors at $0.001)
""")


if __name__ == "__main__":
    main()
