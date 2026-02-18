#!/usr/bin/env python3
"""
Test pair accumulation strategy as makers.

Goal: Buy expensive + cheap sides for < $1 total → guaranteed profit.

Questions:
1. How often is pair_cost < $1?
2. What's the distribution of pair costs?
3. Can we get maker fills on both sides?
4. What's realistic profit per pair?
"""

import pandas as pd
import numpy as np
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def analyze_pair_costs(obs_df: pd.DataFrame):
    """Analyze pair cost distribution and fill opportunities."""

    print("=" * 60)
    print("PAIR COST ANALYSIS")
    print("=" * 60)

    # Calculate pair costs
    obs = obs_df.copy()
    obs['pair_cost'] = obs['up_ask'] + obs['down_ask']
    obs['spread'] = 1.0 - obs['pair_cost']  # Profit if we buy both at ask

    # Filter valid rows
    obs = obs.dropna(subset=['pair_cost'])
    print(f"\nTotal observations: {len(obs):,}")

    # Distribution
    print("\n--- PAIR COST DISTRIBUTION ---")
    for thresh in [0.95, 0.96, 0.97, 0.98, 0.99, 1.00]:
        pct = (obs['pair_cost'] < thresh).mean() * 100
        print(f"  < ${thresh:.2f}: {pct:5.1f}%")

    print(f"\n  Mean pair cost: ${obs['pair_cost'].mean():.3f}")
    print(f"  Min pair cost:  ${obs['pair_cost'].min():.3f}")
    print(f"  Max pair cost:  ${obs['pair_cost'].max():.3f}")

    # When expensive_ask >= $0.80
    print("\n--- WHEN EXPENSIVE_ASK >= $0.80 ---")
    obs['expensive_ask'] = obs[['up_ask', 'down_ask']].max(axis=1)
    high_conf = obs[obs['expensive_ask'] >= 0.80]
    print(f"  Samples: {len(high_conf):,}")

    for thresh in [0.95, 0.96, 0.97, 0.98, 0.99, 1.00]:
        pct = (high_conf['pair_cost'] < thresh).mean() * 100
        print(f"  < ${thresh:.2f}: {pct:5.1f}%")

    print(f"\n  Mean pair cost: ${high_conf['pair_cost'].mean():.3f}")
    print(f"  Mean spread:    ${high_conf['spread'].mean():.3f}")


def analyze_maker_fills(obs_df: pd.DataFrame):
    """Simulate maker fill opportunities."""

    print("\n" + "=" * 60)
    print("MAKER FILL SIMULATION")
    print("=" * 60)

    # For each market, simulate placing maker orders
    results = []

    for slug in obs_df['market_slug'].unique():
        mdf = obs_df[obs_df['market_slug'] == slug].copy()
        mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

        if len(mdf) < 100:
            continue

        # Track best prices seen
        min_up_ask = mdf['up_ask'].min()
        min_down_ask = mdf['down_ask'].min()
        min_pair_cost = min_up_ask + min_down_ask

        # Resolution
        last = mdf.iloc[-1]
        if last['up_bid'] > 0.9:
            resolution = 'UP'
        elif last['down_bid'] > 0.9:
            resolution = 'DOWN'
        else:
            continue

        # Simulate maker orders at various offsets
        for offset in [0.01, 0.02, 0.03]:
            # Place orders 1-3c below ask
            up_fills = 0
            down_fills = 0
            up_fill_prices = []
            down_fill_prices = []

            pending_up = None
            pending_down = None

            for idx, row in mdf.iterrows():
                up_ask = row['up_ask']
                down_ask = row['down_ask']

                if pd.isna(up_ask) or pd.isna(down_ask):
                    continue

                # Check for fills
                if pending_up and up_ask <= pending_up:
                    up_fills += 1
                    up_fill_prices.append(pending_up)
                    pending_up = None

                if pending_down and down_ask <= pending_down:
                    down_fills += 1
                    down_fill_prices.append(pending_down)
                    pending_down = None

                # Place new orders (only if not already pending)
                if pending_up is None and idx % 60 == 0:  # Every ~1s
                    pending_up = up_ask - offset

                if pending_down is None and idx % 60 == 0:
                    pending_down = down_ask - offset

            # Calculate results
            if up_fills > 0 and down_fills > 0:
                avg_up = np.mean(up_fill_prices)
                avg_down = np.mean(down_fill_prices)
                pair_cost = avg_up + avg_down
                pairs = min(up_fills, down_fills)

                # PnL: we pay pair_cost, get $1 for winner
                pnl_per_pair = 1.0 - pair_cost

                results.append({
                    'slug': slug,
                    'offset': offset,
                    'up_fills': up_fills,
                    'down_fills': down_fills,
                    'pairs': pairs,
                    'avg_up_price': avg_up,
                    'avg_down_price': avg_down,
                    'pair_cost': pair_cost,
                    'pnl_per_pair': pnl_per_pair,
                    'total_pnl': pnl_per_pair * pairs,
                    'resolution': resolution,
                })

    df = pd.DataFrame(results)

    # Summarize by offset
    print(f"\n{'Offset':<8} {'Markets':<10} {'Avg Pairs':<12} {'Avg Pair Cost':<15} {'Avg PnL/Pair':<15} {'Total PnL':<12}")
    print("-" * 72)

    for offset in [0.01, 0.02, 0.03]:
        subset = df[df['offset'] == offset]
        if len(subset) == 0:
            continue

        avg_pairs = subset['pairs'].mean()
        avg_cost = subset['pair_cost'].mean()
        avg_pnl = subset['pnl_per_pair'].mean()
        total_pnl = subset['total_pnl'].sum()

        print(f"{offset*100:.0f}c{'':<5} {len(subset):<10} {avg_pairs:<12.1f} ${avg_cost:<14.3f} ${avg_pnl:<14.3f} ${total_pnl:<12.2f}")

    return df


def analyze_timing(obs_df: pd.DataFrame):
    """When is pair cost lowest? Near expiry? After spikes?"""

    print("\n" + "=" * 60)
    print("TIMING ANALYSIS: When is pair cost lowest?")
    print("=" * 60)

    obs = obs_df.copy()
    obs['pair_cost'] = obs['up_ask'] + obs['down_ask']
    obs = obs.dropna(subset=['pair_cost', 'time_remaining_secs'])

    # By time remaining
    print("\n--- BY TIME REMAINING ---")
    bins = [(0, 60), (60, 120), (120, 180), (180, 300), (300, 450)]
    for lo, hi in bins:
        subset = obs[(obs['time_remaining_secs'] >= lo) & (obs['time_remaining_secs'] < hi)]
        if len(subset) > 0:
            mean_cost = subset['pair_cost'].mean()
            under_1 = (subset['pair_cost'] < 1.0).mean() * 100
            under_98 = (subset['pair_cost'] < 0.98).mean() * 100
            print(f"  {lo:>3}-{hi:<3}s: mean=${mean_cost:.3f}, <$1={under_1:5.1f}%, <$0.98={under_98:5.1f}%")


def main():
    # Load data
    obs_path = Path("research/observer/PROTECTED_grid_obs_is_oos2_combined.csv")
    print("Loading...")
    obs_df = pd.read_csv(obs_path, low_memory=False)
    print(f"  Obs: {len(obs_df):,}")

    # Analyze
    analyze_pair_costs(obs_df)
    results = analyze_maker_fills(obs_df)
    analyze_timing(obs_df)

    # Summary
    print("\n" + "=" * 60)
    print("STRATEGY SUMMARY")
    print("=" * 60)

    best_offset = results.groupby('offset')['total_pnl'].sum().idxmax()
    best_results = results[results['offset'] == best_offset]
    total_pnl = best_results['total_pnl'].sum()
    total_pairs = best_results['pairs'].sum()
    avg_cost = best_results['pair_cost'].mean()

    print(f"""
Best offset: {best_offset*100:.0f}c below ask
Total pairs filled: {total_pairs}
Average pair cost: ${avg_cost:.3f}
Total PnL: ${total_pnl:.2f}
PnL/hour: ${total_pnl/69:.2f} (over 69h)

Strategy:
1. Place maker bids {best_offset*100:.0f}c below ask on BOTH sides
2. Accumulate pairs when prices dip
3. Each pair costs ~${avg_cost:.2f}, resolves to $1
4. Profit per pair: ~${1-avg_cost:.3f}
""")


if __name__ == "__main__":
    main()
