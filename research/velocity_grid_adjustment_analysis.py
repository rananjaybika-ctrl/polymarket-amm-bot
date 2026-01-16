#!/usr/bin/env python3
"""
Velocity Grid Adjustment Analysis
==================================
Analyze if velocity signals (Zone 5-6) can predict which side gets hit next,
enabling dynamic grid adjustment for better pair costs.

Key Question: Can we use velocity to know WHEN to post tighter bids on each side?
"""

import pandas as pd
import numpy as np
import os
from glob import glob

def load_all_observer_data():
    """Load all observer CSV files."""
    observer_dir = "/Users/rananjaybika/polymarket-amm-bot/research/observer"
    files = [
        "spread_capture_obs_20260115_aws_12hr.csv",
        "spread_capture_obs_20260115.csv",
        "spread_capture_obs_20260114.csv",
        "spread_capture_obs_20260113.csv",
    ]

    dfs = []
    for f in files:
        path = os.path.join(observer_dir, f)
        if os.path.exists(path):
            try:
                df = pd.read_csv(path, on_bad_lines='skip')
                df['source_file'] = f
                dfs.append(df)
                print(f"Loaded {f}: {len(df)} rows")
            except Exception as e:
                print(f"Error loading {f}: {e}")

    if not dfs:
        return None

    combined = pd.concat(dfs, ignore_index=True)

    # Standardize column names
    if 'velocity_bps' in combined.columns:
        combined['velocity'] = combined['velocity_bps']
    if 'market_slug' in combined.columns:
        combined['market_id'] = combined['market_slug']
    if 'timestamp_ms' in combined.columns:
        combined['timestamp'] = combined['timestamp_ms']

    print(f"\nTotal rows: {len(combined)}")
    return combined


def analyze_velocity_vs_price_movement(df):
    """
    Analyze if velocity direction predicts which side's price drops.

    Grid MM Logic:
    - When price DROPS on a side, our BID becomes more attractive to takers
    - Takers SELL to us (hit our BID) when their side's price is dropping

    Hypothesis:
    - Positive velocity (BTC up) → UP price rises, DOWN price drops
    - Negative velocity (BTC down) → DOWN price rises, UP price drops

    If true, we can:
    - Post tighter DOWN bids when velocity is positive (expect DOWN fills)
    - Post tighter UP bids when velocity is negative (expect UP fills)
    """
    print("\n" + "="*70)
    print("VELOCITY VS PRICE MOVEMENT ANALYSIS")
    print("="*70)

    # Need velocity and prices
    required_cols = ['velocity', 'up_bid', 'up_ask', 'down_bid', 'down_ask', 'market_id']
    if not all(col in df.columns for col in required_cols):
        print(f"Missing required columns. Available: {list(df.columns)}")
        return

    # Sort by market and time
    df = df.sort_values(['market_id', 'timestamp']).copy()

    # Calculate price changes (next observation vs current)
    df['up_mid'] = (df['up_bid'] + df['up_ask']) / 2
    df['down_mid'] = (df['down_bid'] + df['down_ask']) / 2

    # Calculate forward price change (what happens AFTER this velocity reading)
    df['up_mid_next'] = df.groupby('market_id')['up_mid'].shift(-1)
    df['down_mid_next'] = df.groupby('market_id')['down_mid'].shift(-1)

    df['up_change'] = df['up_mid_next'] - df['up_mid']
    df['down_change'] = df['down_mid_next'] - df['down_mid']

    # Filter to valid rows
    valid = df.dropna(subset=['velocity', 'up_change', 'down_change'])
    print(f"\nTotal valid observations: {len(valid)}")

    # Analyze Zone 5-6 (|velocity| > 0.5)
    zone56 = valid[abs(valid['velocity']) > 0.5].copy()
    print(f"Zone 5-6 observations (|v| > 0.5): {len(zone56)}")

    if len(zone56) < 100:
        print("Not enough Zone 5-6 data")
        return

    # Split by velocity direction
    positive_v = zone56[zone56['velocity'] > 0]
    negative_v = zone56[zone56['velocity'] < 0]

    print(f"\nPositive velocity (BTC up): {len(positive_v)} observations")
    print(f"Negative velocity (BTC down): {len(negative_v)} observations")

    # Analyze what happens to prices
    print("\n" + "-"*50)
    print("When VELOCITY > 0 (BTC trending UP):")
    print("-"*50)
    up_rises = (positive_v['up_change'] > 0).mean() * 100
    down_drops = (positive_v['down_change'] < 0).mean() * 100
    print(f"  UP price rises:  {up_rises:.1f}%")
    print(f"  DOWN price drops: {down_drops:.1f}%")
    print(f"  Avg UP change:   {positive_v['up_change'].mean()*100:.3f} cents")
    print(f"  Avg DOWN change: {positive_v['down_change'].mean()*100:.3f} cents")

    print("\n" + "-"*50)
    print("When VELOCITY < 0 (BTC trending DOWN):")
    print("-"*50)
    up_drops = (negative_v['up_change'] < 0).mean() * 100
    down_rises = (negative_v['down_change'] > 0).mean() * 100
    print(f"  UP price drops:   {up_drops:.1f}%")
    print(f"  DOWN price rises: {down_rises:.1f}%")
    print(f"  Avg UP change:   {negative_v['up_change'].mean()*100:.3f} cents")
    print(f"  Avg DOWN change: {negative_v['down_change'].mean()*100:.3f} cents")

    return zone56


def analyze_fill_prediction(df):
    """
    For grid MM, we care about which side's BID gets hit.

    Our BID gets hit when:
    1. Price drops on that side (sellers want out)
    2. Our bid is competitive (close to best bid)

    Prediction: If we can predict price drops, we know where to post tight bids.
    """
    print("\n" + "="*70)
    print("FILL PREDICTION ANALYSIS")
    print("="*70)

    if 'velocity' not in df.columns:
        print("No velocity column")
        return

    # Zone 5-6 data
    zone56 = df[abs(df['velocity']) > 0.5].copy()

    # Calculate bid-ask spread on each side
    zone56['up_spread'] = zone56['up_ask'] - zone56['up_bid']
    zone56['down_spread'] = zone56['down_ask'] - zone56['down_bid']

    # Calculate which side has tighter spread (more activity)
    zone56['tighter_up'] = zone56['up_spread'] < zone56['down_spread']

    # Analyze correlation with velocity
    positive_v = zone56[zone56['velocity'] > 0]
    negative_v = zone56[zone56['velocity'] < 0]

    print(f"\nPositive velocity → UP spread tighter: {positive_v['tighter_up'].mean()*100:.1f}%")
    print(f"Negative velocity → UP spread tighter: {negative_v['tighter_up'].mean()*100:.1f}%")

    # Look at which side would fill first (lower spread = more likely to fill)
    print("\n" + "-"*50)
    print("SPREAD ANALYSIS BY VELOCITY DIRECTION")
    print("-"*50)

    print(f"\nVelocity > 0 (BTC up):")
    print(f"  Avg UP spread:   ${positive_v['up_spread'].mean():.4f}")
    print(f"  Avg DOWN spread: ${positive_v['down_spread'].mean():.4f}")

    print(f"\nVelocity < 0 (BTC down):")
    print(f"  Avg UP spread:   ${negative_v['up_spread'].mean():.4f}")
    print(f"  Avg DOWN spread: ${negative_v['down_spread'].mean():.4f}")


def analyze_pair_cost_by_velocity(df):
    """
    If velocity predicts which side drops, can we get better pair costs
    by being aggressive on the predicted-to-drop side?

    Strategy:
    - Velocity > 0: Post tight DOWN bid, loose UP bid
    - Velocity < 0: Post tight UP bid, loose DOWN bid
    """
    print("\n" + "="*70)
    print("PAIR COST OPTIMIZATION BY VELOCITY")
    print("="*70)

    if 'velocity' not in df.columns:
        return

    zone56 = df[abs(df['velocity']) > 0.5].copy()

    # Current pair costs
    zone56['maker_cost'] = zone56['up_bid'] + zone56['down_bid']
    zone56['taker_cost'] = zone56['up_ask'] + zone56['down_ask']

    # Asymmetric pair cost:
    # When velocity > 0: UP ask + DOWN bid (aggressive on DOWN)
    # When velocity < 0: UP bid + DOWN ask (aggressive on UP)
    zone56['asymmetric_cost'] = np.where(
        zone56['velocity'] > 0,
        zone56['up_ask'] + zone56['down_bid'],  # Hit UP ask, post DOWN bid
        zone56['up_bid'] + zone56['down_ask']   # Post UP bid, hit DOWN ask
    )

    # Alternative: Both sides aggressive on predicted-to-drop
    zone56['velocity_adjusted_cost'] = np.where(
        zone56['velocity'] > 0,
        zone56['up_ask'] + zone56['down_bid'] - 0.01,  # Can be even more aggressive on DOWN
        zone56['up_bid'] - 0.01 + zone56['down_ask']   # Can be even more aggressive on UP
    )

    print(f"\nObservations: {len(zone56)}")
    print(f"\nPair Cost Comparison:")
    print(f"  Pure MAKER (bid+bid):    ${zone56['maker_cost'].mean():.4f}")
    print(f"  Pure TAKER (ask+ask):    ${zone56['taker_cost'].mean():.4f}")
    print(f"  Asymmetric (velocity):   ${zone56['asymmetric_cost'].mean():.4f}")

    # Profitability analysis
    print(f"\nProfitability (< $1.00):")
    print(f"  Pure MAKER: {(zone56['maker_cost'] < 1.0).mean()*100:.1f}%")
    print(f"  Pure TAKER: {(zone56['taker_cost'] < 1.0).mean()*100:.1f}%")
    print(f"  Asymmetric: {(zone56['asymmetric_cost'] < 1.0).mean()*100:.1f}%")

    # What if we're smarter about it?
    print("\n" + "-"*50)
    print("SMART VELOCITY-ADJUSTED GRID")
    print("-"*50)

    # When velocity is strong, be MORE aggressive on the predicted side
    strong_positive = zone56[zone56['velocity'] > 1.0]
    strong_negative = zone56[zone56['velocity'] < -1.0]

    if len(strong_positive) > 0:
        print(f"\nStrong positive velocity (v > 1.0): {len(strong_positive)} obs")
        print(f"  DOWN bid available at: ${strong_positive['down_bid'].mean():.3f}")
        print(f"  DOWN ask at: ${strong_positive['down_ask'].mean():.3f}")
        print(f"  If we front-run by $0.01: ${(strong_positive['down_bid'] + 0.01).mean():.3f}")

    if len(strong_negative) > 0:
        print(f"\nStrong negative velocity (v < -1.0): {len(strong_negative)} obs")
        print(f"  UP bid available at: ${strong_negative['up_bid'].mean():.3f}")
        print(f"  UP ask at: ${strong_negative['up_ask'].mean():.3f}")
        print(f"  If we front-run by $0.01: ${(strong_negative['up_bid'] + 0.01).mean():.3f}")


def simulate_velocity_adjusted_grid(df):
    """
    Simulate a grid MM strategy that adjusts based on velocity.

    Base strategy: Post bids at best_bid + 0.01 on both sides
    Velocity adjustment:
    - When |velocity| > 0.5: Post tighter on predicted-to-drop side
    """
    print("\n" + "="*70)
    print("VELOCITY-ADJUSTED GRID SIMULATION")
    print("="*70)

    if 'velocity' not in df.columns:
        return

    # Simulate fills over time
    df = df.sort_values(['market_id', 'timestamp']).copy()

    # Calculate forward price for fill simulation
    df['up_bid_next'] = df.groupby('market_id')['up_bid'].shift(-1)
    df['down_bid_next'] = df.groupby('market_id')['down_bid'].shift(-1)

    # Our bid prices
    BASE_OFFSET = 0.01
    AGGRESSIVE_OFFSET = 0.02

    results = []

    for market_id, mdf in df.groupby('market_id'):
        if len(mdf) < 100:
            continue

        up_fills = []
        down_fills = []

        for idx, row in mdf.iterrows():
            velocity = row.get('velocity', 0)

            if pd.isna(velocity):
                continue

            # Determine our bid prices based on velocity
            if abs(velocity) > 0.5:
                if velocity > 0:  # BTC up, DOWN should drop
                    up_bid_offset = BASE_OFFSET
                    down_bid_offset = AGGRESSIVE_OFFSET  # More aggressive on DOWN
                else:  # BTC down, UP should drop
                    up_bid_offset = AGGRESSIVE_OFFSET  # More aggressive on UP
                    down_bid_offset = BASE_OFFSET
            else:
                up_bid_offset = BASE_OFFSET
                down_bid_offset = BASE_OFFSET

            # Our bid prices
            our_up_bid = row['up_bid'] + up_bid_offset
            our_down_bid = row['down_bid'] + down_bid_offset

            # Check if we would fill (next tick's ask <= our bid means taker hits us)
            # Actually, we fill when someone market sells to us
            # Simplified: we fill if next bid < current bid (someone sold into the book)
            next_up_bid = row.get('up_bid_next')
            next_down_bid = row.get('down_bid_next')

            if pd.notna(next_up_bid) and next_up_bid <= our_up_bid:
                up_fills.append(our_up_bid)
            if pd.notna(next_down_bid) and next_down_bid <= our_down_bid:
                down_fills.append(our_down_bid)

        if up_fills and down_fills:
            # Calculate pair costs
            min_fills = min(len(up_fills), len(down_fills))
            pair_costs = []
            for i in range(min_fills):
                pair_costs.append(up_fills[i] + down_fills[i])

            results.append({
                'market_id': market_id,
                'up_fills': len(up_fills),
                'down_fills': len(down_fills),
                'pairs': min_fills,
                'avg_pair_cost': np.mean(pair_costs) if pair_costs else None
            })

    if results:
        results_df = pd.DataFrame(results)
        print(f"\nMarkets with fills: {len(results_df)}")
        print(f"Average pairs per market: {results_df['pairs'].mean():.1f}")
        print(f"Average pair cost: ${results_df['avg_pair_cost'].mean():.4f}")
        print(f"Profitable markets: {(results_df['avg_pair_cost'] < 1.0).sum()} / {len(results_df)}")
    else:
        print("No simulated fills found")


def analyze_velocity_persistence(df):
    """
    How long does a velocity signal last?
    If velocity persists, we have time to adjust grid.
    """
    print("\n" + "="*70)
    print("VELOCITY SIGNAL PERSISTENCE")
    print("="*70)

    if 'velocity' not in df.columns or 'timestamp' not in df.columns:
        return

    df = df.sort_values(['market_id', 'timestamp']).copy()

    # For each Zone 5-6 entry, how long does velocity stay above threshold?
    results = []

    for market_id, mdf in df.groupby('market_id'):
        mdf = mdf.reset_index(drop=True)

        in_zone = False
        zone_start = None
        zone_start_time = None

        for idx, row in mdf.iterrows():
            v = row.get('velocity', 0)
            t = row.get('timestamp')

            if pd.isna(v) or pd.isna(t):
                continue

            if abs(v) > 0.5 and not in_zone:
                # Entering zone
                in_zone = True
                zone_start = idx
                zone_start_time = t
            elif abs(v) <= 0.5 and in_zone:
                # Exiting zone
                in_zone = False
                duration = idx - zone_start
                if zone_start_time:
                    time_duration = t - zone_start_time
                    results.append({
                        'market_id': market_id,
                        'ticks': duration,
                        'duration_ms': time_duration
                    })

    if results:
        results_df = pd.DataFrame(results)
        print(f"\nZone 5-6 Events: {len(results_df)}")
        print(f"Average duration: {results_df['ticks'].mean():.1f} ticks")
        print(f"Median duration: {results_df['ticks'].median():.0f} ticks")

        # Convert to seconds if possible
        avg_ms = results_df['duration_ms'].mean()
        print(f"Average duration: {avg_ms/1000:.1f} seconds")

        print(f"\nDuration distribution:")
        print(f"  < 1 sec: {(results_df['duration_ms'] < 1000).mean()*100:.1f}%")
        print(f"  1-5 sec: {((results_df['duration_ms'] >= 1000) & (results_df['duration_ms'] < 5000)).mean()*100:.1f}%")
        print(f"  5-10 sec: {((results_df['duration_ms'] >= 5000) & (results_df['duration_ms'] < 10000)).mean()*100:.1f}%")
        print(f"  > 10 sec: {(results_df['duration_ms'] >= 10000).mean()*100:.1f}%")


def main():
    print("="*70)
    print("VELOCITY SIGNALS FOR GRID MM ADJUSTMENT")
    print("="*70)
    print("\nObjective: Can velocity signals improve grid market making?")
    print("Hypothesis: Velocity predicts which side will drop, allowing")
    print("           dynamic grid adjustment for better pair costs.")

    # Load data
    df = load_all_observer_data()
    if df is None:
        print("No data loaded")
        return

    # Run analyses
    analyze_velocity_vs_price_movement(df)
    analyze_fill_prediction(df)
    analyze_pair_cost_by_velocity(df)
    analyze_velocity_persistence(df)
    simulate_velocity_adjusted_grid(df)

    # Summary
    print("\n" + "="*70)
    print("CONCLUSIONS")
    print("="*70)
    print("""
If velocity DOES predict price drops:
  → Use velocity to adjust grid dynamically
  → Post tighter bids on predicted-to-drop side
  → Expected improvement: Better pair costs, more fills

If velocity DOESN'T predict price drops:
  → Velocity is noise for grid MM
  → Stick to symmetric grid (like wallet 0x640a...)
  → Focus on volume over prediction

Key metrics to check:
  1. Does velocity direction correlate with price movement?
  2. Can we achieve better pair costs with velocity adjustment?
  3. How long do velocity signals persist (time to adjust)?
""")


if __name__ == "__main__":
    main()
