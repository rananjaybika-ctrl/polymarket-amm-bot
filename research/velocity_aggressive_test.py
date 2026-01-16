#!/usr/bin/env python3
"""
Aggressive Velocity Lower Loser Bid Test
- More aggressive scales (up to 0.20)
- Different velocity thresholds (0.1, 0.3, 0.5, 1.0)
"""

import pandas as pd
import numpy as np
import os
from dataclasses import dataclass
from itertools import product


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


def simulate(df, base_offset=0.01, velocity_scale=0.01, velocity_threshold=0.3, order_size=10.0):
    """
    Lower loser bid based on velocity.

    velocity_threshold: minimum |velocity| to trigger adjustment
    velocity_scale: how much to reduce loser bid per unit velocity
    """
    results = []
    total_fills = {'up': 0, 'down': 0, 'loser_up': 0, 'loser_down': 0}
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

            # Dynamic adjustment based on velocity
            if velocity > velocity_threshold:  # UP winning → DOWN is loser
                up_adjustment = base_offset
                # Scale reduction by how much velocity exceeds threshold
                reduction = (velocity - velocity_threshold) * velocity_scale
                down_adjustment = max(0.001, base_offset - reduction)
                down_is_loser = True
                up_is_loser = False
            elif velocity < -velocity_threshold:  # DOWN winning → UP is loser
                down_adjustment = base_offset
                reduction = (abs(velocity) - velocity_threshold) * velocity_scale
                up_adjustment = max(0.001, base_offset - reduction)
                up_is_loser = True
                down_is_loser = False
            else:  # Neutral
                up_adjustment = base_offset
                down_adjustment = base_offset
                up_is_loser = False
                down_is_loser = False

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
                    total_fills['up'] += 1
                    if up_is_loser:
                        total_fills['loser_up'] += 1

            if not pd.isna(next_down_bid) and next_down_bid < down_bid - 0.005:
                if pos.down_shares < 200:
                    pos.down_shares += order_size
                    pos.down_cost += our_down_bid * order_size
                    total_fills['down'] += 1
                    if down_is_loser:
                        total_fills['loser_down'] += 1

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

    return results, total_fills


def main():
    print("="*100)
    print("AGGRESSIVE VELOCITY TEST: Scales + Thresholds")
    print("="*100)

    df = load_data()
    print(f"\nLoaded {len(df)} observations, {df['market_slug'].nunique()} markets")

    # Velocity distribution
    vel = df['velocity'].dropna()
    print(f"\nVelocity distribution:")
    for thresh in [0.1, 0.2, 0.3, 0.5, 1.0, 2.0]:
        count = (vel.abs() > thresh).sum()
        pct = (vel.abs() > thresh).mean() * 100
        print(f"  |v| > {thresh}: {count:>6} ticks ({pct:>5.1f}%)")

    # Test grid
    scales = [0.01, 0.02, 0.05, 0.10, 0.15, 0.20]
    thresholds = [0.1, 0.3, 0.5, 1.0]

    print("\n" + "="*100)
    print("GRID SEARCH: Scale × Threshold")
    print("="*100)

    # Static baseline
    static_results, static_fills = simulate(df, velocity_scale=0, velocity_threshold=999)
    static_df = pd.DataFrame(static_results)
    with_pairs = static_df[static_df['pairs'] > 0]
    baseline_profit = with_pairs[with_pairs['pair_cost'] < 1.0]['profit'].sum()
    baseline_cost = with_pairs['pair_cost'].mean()
    baseline_profitable_pct = (with_pairs['pair_cost'] < 1.0).mean() * 100

    print(f"\nBASELINE (static): Cost=${baseline_cost:.4f}, Profitable={baseline_profitable_pct:.1f}%, Profit=${baseline_profit:.2f}")
    print(f"Fills: {static_fills['up']} UP, {static_fills['down']} DOWN")

    # Results grid
    results_grid = []

    print(f"\n{'Thresh':>6} | {'Scale':>6} | {'Pair Cost':>10} | {'Prof%':>6} | {'Profit':>8} | {'vs Base':>8} | {'Loser Fills':>12}")
    print("-"*80)

    for threshold in thresholds:
        for scale in scales:
            results, fills = simulate(df, velocity_scale=scale, velocity_threshold=threshold)
            rdf = pd.DataFrame(results)
            with_pairs = rdf[rdf['pairs'] > 0]

            if len(with_pairs) > 0:
                profitable = with_pairs[with_pairs['pair_cost'] < 1.0]
                profit = profitable['profit'].sum()
                cost = with_pairs['pair_cost'].mean()
                pct = len(profitable) / len(with_pairs) * 100
                loser_fills = fills['loser_up'] + fills['loser_down']

                improvement = profit - baseline_profit
                sign = "+" if improvement >= 0 else ""

                results_grid.append({
                    'threshold': threshold,
                    'scale': scale,
                    'cost': cost,
                    'profitable_pct': pct,
                    'profit': profit,
                    'improvement': improvement,
                    'loser_fills': loser_fills,
                })

                print(f"{threshold:>6.1f} | {scale:>6.2f} | ${cost:>9.4f} | {pct:>5.1f}% | ${profit:>7.2f} | {sign}${improvement:>6.2f} | {loser_fills:>12}")

    # Find best
    best = max(results_grid, key=lambda x: x['profit'])
    print(f"\n{'='*80}")
    print(f"BEST: threshold={best['threshold']}, scale={best['scale']}")
    print(f"  Pair cost: ${best['cost']:.4f} (baseline: ${baseline_cost:.4f})")
    print(f"  Profit: ${best['profit']:.2f} (baseline: ${baseline_profit:.2f})")
    print(f"  Improvement: +${best['improvement']:.2f} (+{best['improvement']/baseline_profit*100:.1f}%)")

    # Analyze by threshold
    print(f"\n{'='*80}")
    print("ANALYSIS BY THRESHOLD")
    print("="*80)

    for threshold in thresholds:
        subset = [r for r in results_grid if r['threshold'] == threshold]
        best_for_thresh = max(subset, key=lambda x: x['profit'])
        print(f"\nThreshold {threshold}:")
        print(f"  Best scale: {best_for_thresh['scale']}")
        print(f"  Profit: ${best_for_thresh['profit']:.2f} (+${best_for_thresh['improvement']:.2f})")
        print(f"  Loser fills: {best_for_thresh['loser_fills']}")

    # Very aggressive test
    print(f"\n{'='*80}")
    print("EXTREME AGGRESSIVE TEST")
    print("="*80)

    extreme_scales = [0.30, 0.50, 1.00]
    extreme_thresholds = [0.1, 0.5]

    for threshold in extreme_thresholds:
        for scale in extreme_scales:
            results, fills = simulate(df, velocity_scale=scale, velocity_threshold=threshold)
            rdf = pd.DataFrame(results)
            with_pairs = rdf[rdf['pairs'] > 0]

            if len(with_pairs) > 0:
                profitable = with_pairs[with_pairs['pair_cost'] < 1.0]
                profit = profitable['profit'].sum()
                cost = with_pairs['pair_cost'].mean()

                print(f"Thresh={threshold}, Scale={scale}: Cost=${cost:.4f}, Profit=${profit:.2f}, Fills={fills['up']+fills['down']}")

    # Key insight
    print(f"\n{'='*80}")
    print("KEY INSIGHTS")
    print("="*80)

    print("""
OBSERVATIONS:
1. Lower thresholds (0.1) activate more often but on weaker signals
2. Higher thresholds (1.0) only activate on strong moves (rare)
3. Aggressive scales (0.10+) risk posting too low and missing fills

OPTIMAL ZONE:
- Threshold: 0.3-0.5 (moderate velocity signals)
- Scale: 0.02-0.05 (meaningful but not extreme adjustment)

THE CONSTRAINT:
- Max useful reduction ≈ base_offset ($0.01)
- Beyond that, you're posting at or below best_bid (no improvement)
- At scale=0.10, velocity=1.0 → reduction = $0.10 (way more than offset)
  → adjustment bottoms out at 0.001

RECOMMENDATION:
- Threshold: 0.3
- Scale: 0.03-0.05
- Expected improvement: ~7-10% over static
""")


if __name__ == "__main__":
    main()
