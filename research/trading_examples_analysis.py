#!/usr/bin/env python3
"""
Trading Examples Analysis
Shows how the Grid MM strategy performs in:
1. Volatile (oscillating) markets - BEST case
2. Trending markets - WORST case

Also analyzes ways to improve pair cost.
"""

import pandas as pd
import numpy as np
from pathlib import Path

# Parameters
ORDER_SIZE = 15
MAX_POSITION = 200

# Formula: our_bid = best_bid - offset
# - Positive offset → bid BELOW best_bid (passive, cheaper fills)
# - Negative offset → bid ABOVE best_bid (aggressive, faster fills)
VELOCITY_ZONES = {
    'neutral':      {'vel_min': 0.00, 'vel_max': 0.10, 'winner_offset': 0.01, 'loser_offset': 0.01},
    'moderate':     {'vel_min': 0.10, 'vel_max': 0.30, 'winner_offset': 0.01, 'loser_offset': 0.01},
    'strong':       {'vel_min': 0.30, 'vel_max': 0.50, 'winner_offset': 0.00, 'loser_offset': 0.03},
    'very_strong':  {'vel_min': 0.50, 'vel_max': 99.0, 'winner_offset': -0.01, 'loser_offset': 0.05},
}

STATIC_OFFSET = 0.01  # best_bid - 0.01 (one tick below)


def get_velocity_zone(velocity):
    """Get velocity zone config based on current velocity."""
    abs_vel = abs(velocity)
    for zone_name, zone in VELOCITY_ZONES.items():
        if zone['vel_min'] <= abs_vel < zone['vel_max']:
            return zone
    return VELOCITY_ZONES['very_strong']


def get_offsets(velocity):
    """Get (up_offset, down_offset) based on velocity direction."""
    zone = get_velocity_zone(velocity)
    winner_offset = zone['winner_offset']
    loser_offset = zone['loser_offset']

    if velocity > 0:  # UP winning, DOWN losing
        return (winner_offset, loser_offset)
    elif velocity < 0:  # DOWN winning, UP losing
        return (loser_offset, winner_offset)
    else:
        return (winner_offset, winner_offset)


def load_data():
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    df = pd.read_csv(observer_dir / 'spread_capture_obs_20260115_aws_12hr.csv', on_bad_lines='skip')
    return df


def analyze_market_detailed(df, slug, show_fills=10):
    """Show detailed fill-by-fill analysis of a market."""
    mdf = df[df['market_slug'] == slug].sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)

    if len(mdf) < 50:
        return None

    print(f"\n{'='*80}")
    print(f"MARKET: {slug}")
    print(f"{'='*80}")

    # Market stats
    first = mdf.iloc[0]
    last = mdf.iloc[-1]
    print(f"\nDuration: {first['time_remaining_secs']:.0f}s → {last['time_remaining_secs']:.0f}s")
    print(f"Samples: {len(mdf)}")

    # Price range
    up_range = (mdf['up_bid'].min(), mdf['up_bid'].max())
    down_range = (mdf['down_bid'].min(), mdf['down_bid'].max())
    print(f"\nUP price range: ${up_range[0]:.2f} - ${up_range[1]:.2f} (swing: ${up_range[1]-up_range[0]:.2f})")
    print(f"DOWN price range: ${down_range[0]:.2f} - ${down_range[1]:.2f} (swing: ${down_range[1]-down_range[0]:.2f})")

    # Velocity stats
    vel_mean = mdf['velocity_bps'].mean()
    vel_std = mdf['velocity_bps'].std()
    vel_max = mdf['velocity_bps'].abs().max()
    print(f"\nVelocity: mean={vel_mean:.4f}, std={vel_std:.4f}, max|v|={vel_max:.4f}")

    # Classify market type
    up_trend = last['up_bid'] - first['up_bid']
    if abs(up_trend) < 0.05:
        market_type = "OSCILLATING (good for MM)"
    elif up_trend > 0.05:
        market_type = "TRENDING UP (challenging)"
    else:
        market_type = "TRENDING DOWN (challenging)"
    print(f"Market type: {market_type}")

    # Simulate fills with proper position tracking
    up_fills = []
    down_fills = []
    up_position = 0
    down_position = 0

    # Track last fill time to avoid counting multiple fills on same price move
    last_up_fill_idx = -10
    last_down_fill_idx = -10

    for i in range(len(mdf) - 1):
        row = mdf.iloc[i]
        next_row = mdf.iloc[i + 1]

        if row['time_remaining_secs'] < 60:
            break

        velocity = row['velocity_bps']
        up_bid = row['up_bid']
        up_ask = row['up_ask']
        down_bid = row['down_bid']
        down_ask = row['down_ask']

        if pd.isna(up_bid) or pd.isna(up_ask) or pd.isna(down_bid) or pd.isna(down_ask):
            continue
        if up_ask <= up_bid or down_ask <= down_bid:
            continue

        # Static bids: our_bid = best_bid - STATIC_OFFSET
        our_up_bid_static = up_bid - STATIC_OFFSET
        our_down_bid_static = down_bid - STATIC_OFFSET
        our_up_bid_static = max(0.01, min(our_up_bid_static, up_ask - 0.01))
        our_down_bid_static = max(0.01, min(our_down_bid_static, down_ask - 0.01))

        # Velocity-adjusted bids: our_bid = best_bid - offset
        up_offset, down_offset = get_offsets(velocity)
        our_up_bid_vel = up_bid - up_offset
        our_down_bid_vel = down_bid - down_offset
        our_up_bid_vel = max(0.01, min(our_up_bid_vel, up_ask - 0.01))
        our_down_bid_vel = max(0.01, min(our_down_bid_vel, down_ask - 0.01))

        next_up_bid = next_row['up_bid']
        next_down_bid = next_row['down_bid']

        # Check UP fills (with position limit and cooldown)
        if up_position < MAX_POSITION and (i - last_up_fill_idx) >= 3:
            if not pd.isna(next_up_bid) and next_up_bid <= our_up_bid_static:
                up_fills.append({
                    'time': row['time_remaining_secs'],
                    'velocity': velocity,
                    'book_bid': up_bid,
                    'our_bid_static': our_up_bid_static,
                    'our_bid_velocity': our_up_bid_vel,
                    'next_bid': next_up_bid,
                })
                up_position += ORDER_SIZE
                last_up_fill_idx = i

        # Check DOWN fills (with position limit and cooldown)
        if down_position < MAX_POSITION and (i - last_down_fill_idx) >= 3:
            if not pd.isna(next_down_bid) and next_down_bid <= our_down_bid_static:
                down_fills.append({
                    'time': row['time_remaining_secs'],
                    'velocity': velocity,
                    'book_bid': down_bid,
                    'our_bid_static': our_down_bid_static,
                    'our_bid_velocity': our_down_bid_vel,
                    'next_bid': next_down_bid,
                })
                down_position += ORDER_SIZE
                last_down_fill_idx = i

    print(f"\n--- FILLS (with position tracking) ---")
    print(f"UP fills: {len(up_fills)} ({up_position} shares)")
    print(f"DOWN fills: {len(down_fills)} ({down_position} shares)")
    print(f"Pairs possible: {min(len(up_fills), len(down_fills))}")

    # Show sample fills
    if up_fills and show_fills > 0:
        print(f"\n--- SAMPLE UP FILLS (first {min(show_fills, len(up_fills))}) ---")
        print(f"{'Time':>8} {'Velocity':>10} {'Book Bid':>10} {'Our Bid':>10} {'Next Bid':>10} {'Filled?':>8}")
        for f in up_fills[:show_fills]:
            print(f"{f['time']:>8.0f}s {f['velocity']:>10.4f} ${f['book_bid']:>9.2f} ${f['our_bid_static']:>9.2f} ${f['next_bid']:>9.2f} {'YES':>8}")

    if down_fills and show_fills > 0:
        print(f"\n--- SAMPLE DOWN FILLS (first {min(show_fills, len(down_fills))}) ---")
        print(f"{'Time':>8} {'Velocity':>10} {'Book Bid':>10} {'Our Bid':>10} {'Next Bid':>10} {'Filled?':>8}")
        for f in down_fills[:show_fills]:
            print(f"{f['time']:>8.0f}s {f['velocity']:>10.4f} ${f['book_bid']:>9.2f} ${f['our_bid_static']:>9.2f} ${f['next_bid']:>9.2f} {'YES':>8}")

    # Calculate pair costs
    num_pairs = min(len(up_fills), len(down_fills))
    if num_pairs > 0:
        static_costs = []
        velocity_costs = []
        for i in range(num_pairs):
            static_cost = up_fills[i]['our_bid_static'] + down_fills[i]['our_bid_static']
            velocity_cost = up_fills[i]['our_bid_velocity'] + down_fills[i]['our_bid_velocity']
            static_costs.append(static_cost)
            velocity_costs.append(velocity_cost)

        print(f"\n--- PAIR COST ANALYSIS ---")
        print(f"Static avg pair cost: ${np.mean(static_costs):.4f}")
        print(f"Velocity avg pair cost: ${np.mean(velocity_costs):.4f}")
        print(f"Improvement: ${np.mean(static_costs) - np.mean(velocity_costs):.4f}")

        static_profit = sum((1.0 - c) * ORDER_SIZE for c in static_costs)
        velocity_profit = sum((1.0 - c) * ORDER_SIZE for c in velocity_costs)
        print(f"\nStatic profit: ${static_profit:.2f}")
        print(f"Velocity profit: ${velocity_profit:.2f}")
        print(f"Extra profit: ${velocity_profit - static_profit:.2f}")

    return {
        'slug': slug,
        'market_type': market_type,
        'up_fills': len(up_fills),
        'down_fills': len(down_fills),
        'pairs': num_pairs,
    }


def find_extreme_markets(df):
    """Find most volatile and most trending markets."""
    markets = df['market_slug'].unique()

    market_stats = []
    for slug in markets:
        mdf = df[df['market_slug'] == slug]
        if len(mdf) < 100:
            continue

        first = mdf.iloc[0]
        last = mdf.iloc[-1]

        # Skip incomplete markets
        if first['time_remaining_secs'] < 800 or last['time_remaining_secs'] > 60:
            continue

        up_trend = last['up_bid'] - first['up_bid']
        volatility = mdf['up_bid'].std()
        vel_std = mdf['velocity_bps'].std()

        market_stats.append({
            'slug': slug,
            'trend': up_trend,
            'volatility': volatility,
            'vel_std': vel_std,
            'samples': len(mdf),
        })

    stats_df = pd.DataFrame(market_stats)

    # Most oscillating (low trend, high volatility)
    stats_df['oscillation_score'] = stats_df['volatility'] / (abs(stats_df['trend']) + 0.01)

    print("\n" + "="*80)
    print("MARKET CLASSIFICATION")
    print("="*80)

    print("\n--- TOP 5 OSCILLATING MARKETS (Best for MM) ---")
    oscillating = stats_df.nlargest(5, 'oscillation_score')
    for _, row in oscillating.iterrows():
        print(f"  {row['slug'][:50]}: volatility={row['volatility']:.3f}, trend={row['trend']:+.2f}")

    print("\n--- TOP 5 TRENDING MARKETS (Challenging) ---")
    trending = stats_df.nlargest(5, 'trend')
    for _, row in trending.iterrows():
        print(f"  {row['slug'][:50]}: trend={row['trend']:+.2f}, volatility={row['volatility']:.3f}")

    return oscillating.iloc[0]['slug'], trending.iloc[0]['slug']


def analyze_pair_cost_improvements(df):
    """Analyze different ways to improve pair cost."""
    print("\n" + "="*80)
    print("PAIR COST IMPROVEMENT ANALYSIS")
    print("="*80)

    # Calculate theoretical pair costs at different offsets
    # Formula: our_bid = best_bid - offset (positive = below best_bid)
    offsets = [0.01, 0.02, 0.03, 0.04, 0.05]

    print("\n--- OFFSET IMPACT ON PAIR COST ---")
    print("Formula: our_bid = best_bid - offset (larger offset = deeper/cheaper bid)")
    print(f"{'Offset':>10} {'Avg Pair Cost':>15} {'Profit/Pair':>15} {'Note':>25}")

    for offset in offsets:
        # Calculate what pair cost would be
        pair_costs = []
        for _, row in df.sample(min(10000, len(df))).iterrows():
            up_bid = row['up_bid']
            up_ask = row['up_ask']
            down_bid = row['down_bid']
            down_ask = row['down_ask']

            if pd.isna(up_bid) or pd.isna(up_ask) or pd.isna(down_bid) or pd.isna(down_ask):
                continue
            if up_ask <= up_bid or down_ask <= down_bid:
                continue

            # Formula: our_bid = best_bid - offset
            our_up = up_bid - offset
            our_down = down_bid - offset
            our_up = max(0.01, min(our_up, up_ask - 0.01))
            our_down = max(0.01, min(our_down, down_ask - 0.01))
            pair_costs.append(our_up + our_down)

        avg_cost = np.mean(pair_costs)
        profit = 1.0 - avg_cost

        note = ""
        if offset == 0.01:
            note = "← Current (1 tick below)"
        elif offset == 0.03:
            note = "← Strong zone loser"
        elif offset == 0.05:
            note = "← Very strong zone loser"

        print(f"${offset:>9.3f} ${avg_cost:>14.4f} ${profit:>14.4f} {note:>25}")

    print("\n--- OTHER IMPROVEMENT STRATEGIES ---")
    print("""
1. LOWER BASE OFFSET
   - $0.005 would be ideal but NOT ALLOWED (min tick = $0.01)
   - Current $0.01 is already the minimum practical offset
   - No room for improvement here

2. AGGRESSIVE VELOCITY REDUCTION
   - Current: reduce by $0.008-0.009
   - More aggressive: reduce by $0.009 (floor at $0.001)
   - Catches cheaper fills on losing side

3. SPREAD FILTERING
   - Only trade when spread > $0.02
   - Avoids tight-spread periods with less edge
   - May reduce volume but improve quality

4. TIME-BASED ADJUSTMENT
   - Early in market (>600s): use smaller offset (more conservative)
   - Mid-market (300-600s): use normal offset
   - Late market (<300s): widen offset (capture remaining fills)

5. IMBALANCE-BASED ADJUSTMENT
   - When UP_pos > DOWN_pos: lower UP offset (less aggressive)
   - When DOWN_pos > UP_pos: lower DOWN offset
   - Helps rebalance naturally

6. VELOCITY DIRECTION FILTER
   - Don't post on loser side when |v| > 0.5
   - Reduces fills on losing side during strong moves
   - May hurt if velocity reverses
""")


def main():
    print("="*80)
    print("TRADING EXAMPLES ANALYSIS")
    print("="*80)

    df = load_data()
    print(f"Loaded {len(df)} observations")

    # Find extreme markets
    oscillating_slug, trending_slug = find_extreme_markets(df)

    # Analyze oscillating market (BEST case)
    print("\n" + "="*80)
    print("EXAMPLE 1: OSCILLATING MARKET (Best Case)")
    print("="*80)
    analyze_market_detailed(df, oscillating_slug, show_fills=5)

    # Analyze trending market (WORST case)
    print("\n" + "="*80)
    print("EXAMPLE 2: TRENDING MARKET (Challenging Case)")
    print("="*80)
    analyze_market_detailed(df, trending_slug, show_fills=5)

    # Improvement analysis
    analyze_pair_cost_improvements(df)


if __name__ == "__main__":
    main()
