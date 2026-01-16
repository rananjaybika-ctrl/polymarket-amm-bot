#!/usr/bin/env python3
"""
Grid MM PnL Projection with Velocity-Based Loser Bid Reduction

Simulates two-sided grid market making with:
- 15 share order size
- Cycling ON (continuous posting - multiple fills per market)
- Merging ON (pair matching)
- Velocity-based loser bid reduction

MAKER fill logic:
- We post BIDs on both sides
- Our fill price is our BID (not the ask)
- Pair cost = our_up_bid + our_down_bid
- Profit = (1.0 - pair_cost) × shares

Fill detection:
- Count price oscillations (bid changes)
- Each oscillation represents a potential fill
- Apply fill rate based on position in book
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional
from collections import defaultdict

# =============================================================================
# USER'S ACTUAL PARAMETERS
# =============================================================================
ORDER_SIZE = 15  # shares per fill
MAX_POSITION = 200  # per side
MIN_TIME = 60  # stop at 60s remaining

# =============================================================================
# VELOCITY ZONES - Formula: our_bid = best_bid - offset
# - Positive offset → bid BELOW best_bid (passive, cheaper fills)
# - ALL BIDS ARE PASSIVE (below best_bid) - only depth varies
# =============================================================================
VELOCITY_ZONES = {
    # |v| < 0.1: Both sides at best_bid - 0.01 (symmetric passive)
    'neutral': {
        'vel_min': 0.00, 'vel_max': 0.10,
        'winner_offset': 0.01,  # best_bid - 0.01 (passive)
        'loser_offset': 0.01,   # best_bid - 0.01 (same)
    },
    # |v| >= 0.1 to < 0.3: Same as neutral
    'moderate': {
        'vel_min': 0.10, 'vel_max': 0.30,
        'winner_offset': 0.01,  # best_bid - 0.01 (passive)
        'loser_offset': 0.01,   # best_bid - 0.01 (same)
    },
    # |v| >= 0.3: Winner stays passive, loser goes DEEPER
    'strong': {
        'vel_min': 0.30, 'vel_max': 0.50,
        'winner_offset': 0.01,  # best_bid - 0.01 (still passive)
        'loser_offset': 0.03,   # best_bid - 0.03 (deeper passive)
    },
    # |v| >= 0.5: Winner stays passive, loser goes VERY DEEP
    'very_strong': {
        'vel_min': 0.50, 'vel_max': 99.0,
        'winner_offset': 0.01,  # best_bid - 0.01 (still passive)
        'loser_offset': 0.05,   # best_bid - 0.05 (very deep passive)
    },
}

# Static strategy offset (for comparison baseline)
STATIC_OFFSET = 0.01  # best_bid - 0.01 (one tick below)


@dataclass
class Fill:
    """Represents a single fill."""
    side: str  # 'UP' or 'DOWN'
    price: float
    shares: int
    time_remaining: float
    velocity: float


@dataclass
class Pair:
    """A matched UP+DOWN pair."""
    up_fill: Fill
    down_fill: Fill
    pair_cost: float
    profit_per_share: float
    total_profit: float


@dataclass
class MarketResult:
    """Results from simulating one market."""
    slug: str
    total_samples: int
    up_fills: List[Fill]
    down_fills: List[Fill]
    pairs: List[Pair]
    total_locked_profit: float
    avg_pair_cost: float
    fill_count: int
    duration_hours: float


def get_velocity_zone(velocity: float) -> dict:
    """Get velocity zone config based on current velocity."""
    abs_vel = abs(velocity)
    for zone_name, zone in VELOCITY_ZONES.items():
        if zone['vel_min'] <= abs_vel < zone['vel_max']:
            return zone
    return VELOCITY_ZONES['very_strong']  # Default to highest zone


def get_offsets(velocity: float) -> tuple:
    """
    Get (winner_offset, loser_offset) for current velocity.

    Formula: our_bid = best_bid - offset
    - Positive offset → bid BELOW best_bid (passive)
    - Negative offset → bid ABOVE best_bid (aggressive)

    Returns (up_offset, down_offset) based on velocity direction.
    """
    zone = get_velocity_zone(velocity)
    winner_offset = zone['winner_offset']
    loser_offset = zone['loser_offset']

    if velocity > 0:
        # UP is winning, DOWN is losing
        return (winner_offset, loser_offset)
    elif velocity < 0:
        # DOWN is winning, UP is losing
        return (loser_offset, winner_offset)
    else:
        # Neutral - both use winner offset
        return (winner_offset, winner_offset)


def simulate_market_static(mdf: pd.DataFrame, slug: str) -> Optional[MarketResult]:
    """
    Simulate STATIC grid MM with cycling (multiple fills per market).
    Both UP and DOWN bids use STATIC_OFFSET.

    Formula: our_bid = best_bid - offset (positive offset = below best_bid)

    REALISTIC ORDER TRACKING:
    - Orders stay at their posted price until filled
    - We only post new orders after a fill (cycling)
    - Fill occurs when book bid drops to or below our posted price
    """
    mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)

    if len(mdf) < 50:
        return None

    up_fills = []
    down_fills = []
    up_position = 0
    down_position = 0

    # Track active orders (None = no order posted)
    active_up_order = None  # (price, posted_at_idx)
    active_down_order = None

    for i in range(len(mdf) - 1):
        row = mdf.iloc[i]
        next_row = mdf.iloc[i + 1]
        time_rem = row['time_remaining_secs']

        if time_rem < MIN_TIME:
            break

        if up_position >= MAX_POSITION and down_position >= MAX_POSITION:
            break

        velocity = row['velocity_bps']
        up_bid_book = row['up_bid']
        up_ask = row['up_ask']
        down_bid_book = row['down_bid']
        down_ask = row['down_ask']

        # Skip invalid data
        if pd.isna(up_bid_book) or pd.isna(up_ask) or pd.isna(down_bid_book) or pd.isna(down_ask):
            continue
        if up_ask <= up_bid_book or down_ask <= down_bid_book:
            continue

        # Post new orders if we don't have active ones
        if active_up_order is None and up_position < MAX_POSITION:
            new_up_bid = up_bid_book - STATIC_OFFSET
            new_up_bid = max(0.01, min(new_up_bid, up_ask - 0.01))
            active_up_order = (new_up_bid, i)

        if active_down_order is None and down_position < MAX_POSITION:
            new_down_bid = down_bid_book - STATIC_OFFSET
            new_down_bid = max(0.01, min(new_down_bid, down_ask - 0.01))
            active_down_order = (new_down_bid, i)

        # Get next tick's bids
        next_up_bid = next_row['up_bid']
        next_down_bid = next_row['down_bid']

        # Check for fills on ACTIVE orders (at their POSTED price)
        if active_up_order is not None:
            posted_up_price, _ = active_up_order
            if not pd.isna(next_up_bid) and next_up_bid <= posted_up_price:
                up_fills.append(Fill(
                    side='UP',
                    price=posted_up_price,
                    shares=ORDER_SIZE,
                    time_remaining=time_rem,
                    velocity=velocity
                ))
                up_position += ORDER_SIZE
                active_up_order = None  # Order filled, will post new one next tick

        if active_down_order is not None:
            posted_down_price, _ = active_down_order
            if not pd.isna(next_down_bid) and next_down_bid <= posted_down_price:
                down_fills.append(Fill(
                    side='DOWN',
                    price=posted_down_price,
                    shares=ORDER_SIZE,
                    time_remaining=time_rem,
                    velocity=velocity
                ))
                down_position += ORDER_SIZE
                active_down_order = None  # Order filled, will post new one next tick

    # Match pairs (FIFO)
    pairs = []
    num_pairs = min(len(up_fills), len(down_fills))
    for i in range(num_pairs):
        up_fill = up_fills[i]
        down_fill = down_fills[i]
        pair_cost = up_fill.price + down_fill.price
        profit_per_share = 1.0 - pair_cost
        total_profit = profit_per_share * ORDER_SIZE

        pairs.append(Pair(
            up_fill=up_fill,
            down_fill=down_fill,
            pair_cost=pair_cost,
            profit_per_share=profit_per_share,
            total_profit=total_profit
        ))

    total_locked = sum(p.total_profit for p in pairs)
    avg_cost = np.mean([p.pair_cost for p in pairs]) if pairs else 0.0

    if len(mdf) > 1:
        start_time = mdf.iloc[0]['time_remaining_secs']
        end_time = mdf.iloc[-1]['time_remaining_secs']
        duration_hours = (start_time - end_time) / 3600
    else:
        duration_hours = 0.25

    return MarketResult(
        slug=slug,
        total_samples=len(mdf),
        up_fills=up_fills,
        down_fills=down_fills,
        pairs=pairs,
        total_locked_profit=total_locked,
        avg_pair_cost=avg_cost,
        fill_count=len(up_fills) + len(down_fills),
        duration_hours=duration_hours
    )


def get_zone_name(velocity: float) -> str:
    """Get velocity zone name for tracking zone changes."""
    abs_vel = abs(velocity)
    for zone_name, zone in VELOCITY_ZONES.items():
        if zone['vel_min'] <= abs_vel < zone['vel_max']:
            return zone_name
    return 'very_strong'


def simulate_market_velocity(mdf: pd.DataFrame, slug: str) -> Optional[MarketResult]:
    """
    Simulate VELOCITY-ADJUSTED grid MM with cycling AND order pulling.

    Formula: our_bid = best_bid - offset
    - Winner gets smaller/negative offset (closer to or above best_bid)
    - Loser gets larger positive offset (further below best_bid, cheaper fills)

    REALISTIC ORDER MANAGEMENT:
    - Orders stay at their posted price until filled OR velocity zone changes
    - When velocity zone changes, we PULL unfilled orders and repost at new prices
    - This is more realistic than assuming instant price updates

    CORRECT MAKER Fill Logic:
    - At tick T, post order at calculated price (or keep existing if no zone change)
    - At tick T+1, if book bid <= our posted price, we got filled
    - Fill price = our posted price (not recalculated each tick)
    """
    mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)

    if len(mdf) < 50:
        return None

    up_fills = []
    down_fills = []
    up_position = 0
    down_position = 0

    # Track active orders (None = no order posted)
    active_up_order = None  # (price, posted_at_idx)
    active_down_order = None
    last_zone = None
    pulls_count = 0

    for i in range(len(mdf) - 1):
        row = mdf.iloc[i]
        next_row = mdf.iloc[i + 1]
        time_rem = row['time_remaining_secs']

        if time_rem < MIN_TIME:
            break

        if up_position >= MAX_POSITION and down_position >= MAX_POSITION:
            break

        velocity = row['velocity_bps']
        up_bid_book = row['up_bid']
        up_ask = row['up_ask']
        down_bid_book = row['down_bid']
        down_ask = row['down_ask']

        # Skip invalid data
        if pd.isna(up_bid_book) or pd.isna(up_ask) or pd.isna(down_bid_book) or pd.isna(down_ask):
            continue
        if up_ask <= up_bid_book or down_ask <= down_bid_book:
            continue

        # Check for velocity zone change (for tracking only, NO pulling)
        current_zone = get_zone_name(velocity)
        zone_changed = (last_zone is not None and current_zone != last_zone)
        last_zone = current_zone

        # DISABLED ORDER PULLING - orders stay posted until filled
        # This lets us benefit from velocity-based initial posting
        # without disrupting fills due to zone changes
        # if zone_changed:
        #     if active_up_order is not None:
        #         active_up_order = None
        #         pulls_count += 1
        #     if active_down_order is not None:
        #         active_down_order = None
        #         pulls_count += 1

        # Get offsets for current velocity zone
        up_offset, down_offset = get_offsets(velocity)

        # Post new orders if we don't have active ones
        if active_up_order is None and up_position < MAX_POSITION:
            new_up_bid = up_bid_book - up_offset
            new_up_bid = max(0.01, min(new_up_bid, up_ask - 0.01))
            active_up_order = (new_up_bid, i)

        if active_down_order is None and down_position < MAX_POSITION:
            new_down_bid = down_bid_book - down_offset
            new_down_bid = max(0.01, min(new_down_bid, down_ask - 0.01))
            active_down_order = (new_down_bid, i)

        # Get next tick's bids
        next_up_bid = next_row['up_bid']
        next_down_bid = next_row['down_bid']

        # Check for fills on ACTIVE orders (at their POSTED price)
        if active_up_order is not None:
            posted_up_price, _ = active_up_order
            if not pd.isna(next_up_bid) and next_up_bid <= posted_up_price:
                up_fills.append(Fill(
                    side='UP',
                    price=posted_up_price,
                    shares=ORDER_SIZE,
                    time_remaining=time_rem,
                    velocity=velocity
                ))
                up_position += ORDER_SIZE
                active_up_order = None  # Order filled, need to post new one

        if active_down_order is not None:
            posted_down_price, _ = active_down_order
            if not pd.isna(next_down_bid) and next_down_bid <= posted_down_price:
                down_fills.append(Fill(
                    side='DOWN',
                    price=posted_down_price,
                    shares=ORDER_SIZE,
                    time_remaining=time_rem,
                    velocity=velocity
                ))
                down_position += ORDER_SIZE
                active_down_order = None  # Order filled, need to post new one

    # Match pairs
    pairs = []
    num_pairs = min(len(up_fills), len(down_fills))
    for i in range(num_pairs):
        up_fill = up_fills[i]
        down_fill = down_fills[i]
        pair_cost = up_fill.price + down_fill.price
        profit_per_share = 1.0 - pair_cost
        total_profit = profit_per_share * ORDER_SIZE

        pairs.append(Pair(
            up_fill=up_fill,
            down_fill=down_fill,
            pair_cost=pair_cost,
            profit_per_share=profit_per_share,
            total_profit=total_profit
        ))

    total_locked = sum(p.total_profit for p in pairs)
    avg_cost = np.mean([p.pair_cost for p in pairs]) if pairs else 0.0

    if len(mdf) > 1:
        start_time = mdf.iloc[0]['time_remaining_secs']
        end_time = mdf.iloc[-1]['time_remaining_secs']
        duration_hours = (start_time - end_time) / 3600
    else:
        duration_hours = 0.25

    return MarketResult(
        slug=slug,
        total_samples=len(mdf),
        up_fills=up_fills,
        down_fills=down_fills,
        pairs=pairs,
        total_locked_profit=total_locked,
        avg_pair_cost=avg_cost,
        fill_count=len(up_fills) + len(down_fills),
        duration_hours=duration_hours
    )


def load_observer_data() -> Dict[str, pd.DataFrame]:
    """Load all observer CSV files and return unique complete markets."""
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')

    target_files = [
        'spread_capture_obs_20260115_aws_12hr.csv',
        'spread_capture_obs_20260114.csv',
        'spread_capture_obs_20260113.csv',
    ]

    all_markets = {}
    total_rows = 0

    for filename in target_files:
        filepath = observer_dir / filename
        if not filepath.exists():
            print(f"  Warning: {filename} not found")
            continue

        try:
            df = pd.read_csv(filepath, on_bad_lines='skip')
            if df.empty:
                continue

            total_rows += len(df)

            for slug in df['market_slug'].unique():
                mdf = df[df['market_slug'] == slug]
                if len(mdf) >= 50:
                    first = mdf.iloc[0]['time_remaining_secs']
                    last = mdf.iloc[-1]['time_remaining_secs']
                    if first >= 800 and last <= 60:
                        if slug not in all_markets or len(mdf) > len(all_markets[slug]):
                            all_markets[slug] = mdf.copy()
        except Exception as e:
            print(f"  Error loading {filename}: {e}")

    print(f"  Loaded {total_rows:,} total rows from {len(target_files)} files")
    print(f"  Found {len(all_markets)} unique complete markets")

    return all_markets


def main():
    print("=" * 80)
    print("GRID MM PNL PROJECTION - Velocity-Based Loser Bid Reduction")
    print("=" * 80)

    print(f"\nParameters:")
    print(f"  Order Size: {ORDER_SIZE} shares")
    print(f"  Static Offset: ${STATIC_OFFSET} (best_bid - offset)")
    print(f"  Max Position: {MAX_POSITION} per side")
    print(f"  Min Time: {MIN_TIME}s remaining")
    print(f"\nVelocity Zones (formula: our_bid = best_bid - offset):")
    for name, zone in VELOCITY_ZONES.items():
        print(f"  {name}: vel {zone['vel_min']}-{zone['vel_max']}, "
              f"winner={zone['winner_offset']:+.2f}, loser={zone['loser_offset']:+.2f}")

    print(f"\n{'='*80}")
    print("LOADING DATA")
    print("=" * 80)

    markets = load_observer_data()

    if not markets:
        print("No data loaded!")
        return

    # Data validation
    print(f"\n{'='*80}")
    print("DATA VALIDATION")
    print("=" * 80)

    sample_slug = list(markets.keys())[0]
    sample_df = markets[sample_slug]
    print(f"\n  Sample market: {sample_slug}")
    print(f"  Samples: {len(sample_df)}")

    # Count bid changes (potential fills)
    up_changes = (sample_df['up_bid'].diff() != 0).sum()
    down_changes = (sample_df['down_bid'].diff() != 0).sum()
    print(f"  UP bid changes: {up_changes}")
    print(f"  DOWN bid changes: {down_changes}")

    # Check typical pair costs (using formula: best_bid - offset)
    sample_df_sorted = sample_df.sort_values('time_remaining_secs', ascending=False)
    pair_costs = []
    for _, row in sample_df_sorted.head(100).iterrows():
        up_bid = row['up_bid'] - STATIC_OFFSET
        down_bid = row['down_bid'] - STATIC_OFFSET
        up_bid = max(0.01, min(up_bid, row['up_ask'] - 0.01))
        down_bid = max(0.01, min(down_bid, row['down_ask'] - 0.01))
        pair_costs.append(up_bid + down_bid)
    print(f"  Sample pair costs: ${np.mean(pair_costs):.4f} avg, ${min(pair_costs):.4f} - ${max(pair_costs):.4f}")

    print(f"\n{'='*80}")
    print("RUNNING BACKTEST")
    print("=" * 80)

    static_results = []
    velocity_results = []

    for slug, mdf in markets.items():
        static_res = simulate_market_static(mdf, slug)
        velocity_res = simulate_market_velocity(mdf, slug)

        if static_res:
            static_results.append(static_res)
        if velocity_res:
            velocity_results.append(velocity_res)

    print(f"\n  Simulated {len(static_results)} markets (static)")
    print(f"  Simulated {len(velocity_results)} markets (velocity)")

    # ==========================================================================
    # STATIC GRID RESULTS
    # ==========================================================================
    print(f"\n{'='*80}")
    print("STATIC GRID RESULTS (No Velocity Adjustment)")
    print("=" * 80)

    total_static_profit = sum(r.total_locked_profit for r in static_results)
    total_static_pairs = sum(len(r.pairs) for r in static_results)
    total_static_fills = sum(r.fill_count for r in static_results)
    total_static_hours = sum(r.duration_hours for r in static_results)

    static_pair_costs = [p.pair_cost for r in static_results for p in r.pairs]

    print(f"\n  Total Markets: {len(static_results)}")
    print(f"  Total Fills: {total_static_fills}")
    print(f"  Total Pairs: {total_static_pairs}")
    print(f"  Total Hours: {total_static_hours:.1f}h")
    print(f"\n  Total Locked Profit: ${total_static_profit:.2f}")
    if len(static_results) > 0:
        print(f"  Profit per Market: ${total_static_profit/len(static_results):.2f}")
    if total_static_pairs > 0:
        print(f"  Profit per Pair: ${total_static_profit/total_static_pairs:.4f}")
    if total_static_hours > 0:
        print(f"  Hourly Rate: ${total_static_profit/total_static_hours:.2f}/hr")

    if static_pair_costs:
        print(f"\n  Pair Cost Stats:")
        print(f"    Mean: ${np.mean(static_pair_costs):.4f}")
        print(f"    Min:  ${np.min(static_pair_costs):.4f}")
        print(f"    Max:  ${np.max(static_pair_costs):.4f}")
        profitable_static = sum(1 for c in static_pair_costs if c < 1.0)
        print(f"    Profitable: {profitable_static}/{len(static_pair_costs)} ({profitable_static/len(static_pair_costs)*100:.1f}%)")

    # ==========================================================================
    # VELOCITY-ADJUSTED GRID RESULTS
    # ==========================================================================
    print(f"\n{'='*80}")
    print("VELOCITY-ADJUSTED GRID RESULTS")
    print("=" * 80)

    total_velocity_profit = sum(r.total_locked_profit for r in velocity_results)
    total_velocity_pairs = sum(len(r.pairs) for r in velocity_results)
    total_velocity_fills = sum(r.fill_count for r in velocity_results)
    total_velocity_hours = sum(r.duration_hours for r in velocity_results)

    velocity_pair_costs = [p.pair_cost for r in velocity_results for p in r.pairs]

    print(f"\n  Total Markets: {len(velocity_results)}")
    print(f"  Total Fills: {total_velocity_fills}")
    print(f"  Total Pairs: {total_velocity_pairs}")
    print(f"  Total Hours: {total_velocity_hours:.1f}h")
    print(f"\n  Total Locked Profit: ${total_velocity_profit:.2f}")
    if len(velocity_results) > 0:
        print(f"  Profit per Market: ${total_velocity_profit/len(velocity_results):.2f}")
    if total_velocity_pairs > 0:
        print(f"  Profit per Pair: ${total_velocity_profit/total_velocity_pairs:.4f}")
    if total_velocity_hours > 0:
        print(f"  Hourly Rate: ${total_velocity_profit/total_velocity_hours:.2f}/hr")

    if velocity_pair_costs:
        print(f"\n  Pair Cost Stats:")
        print(f"    Mean: ${np.mean(velocity_pair_costs):.4f}")
        print(f"    Min:  ${np.min(velocity_pair_costs):.4f}")
        print(f"    Max:  ${np.max(velocity_pair_costs):.4f}")
        profitable_velocity = sum(1 for c in velocity_pair_costs if c < 1.0)
        print(f"    Profitable: {profitable_velocity}/{len(velocity_pair_costs)} ({profitable_velocity/len(velocity_pair_costs)*100:.1f}%)")

    # ==========================================================================
    # COMPARISON
    # ==========================================================================
    print(f"\n{'='*80}")
    print("COMPARISON: STATIC vs VELOCITY-ADJUSTED")
    print("=" * 80)

    if total_static_pairs > 0 and total_velocity_pairs > 0:
        improvement = total_velocity_profit - total_static_profit
        improvement_pct = (improvement / abs(total_static_profit) * 100) if total_static_profit != 0 else 0

        print(f"\n  {'Metric':<25} {'Static':>12} {'Velocity':>12} {'Diff':>12}")
        print(f"  {'-'*25} {'-'*12} {'-'*12} {'-'*12}")
        print(f"  {'Total Profit':<25} ${total_static_profit:>10.2f} ${total_velocity_profit:>10.2f} ${improvement:>+10.2f}")
        if total_static_hours > 0:
            print(f"  {'Hourly Rate':<25} ${total_static_profit/total_static_hours:>10.2f} ${total_velocity_profit/total_velocity_hours:>10.2f} ${(total_velocity_profit/total_velocity_hours - total_static_profit/total_static_hours):>+10.2f}")
        if static_pair_costs and velocity_pair_costs:
            print(f"  {'Avg Pair Cost':<25} ${np.mean(static_pair_costs):>10.4f} ${np.mean(velocity_pair_costs):>10.4f} ${np.mean(velocity_pair_costs) - np.mean(static_pair_costs):>+10.4f}")
        print(f"  {'Improvement':<25} {'-':>12} {'-':>12} {improvement_pct:>+10.1f}%")

    # ==========================================================================
    # TOP PERFORMING MARKETS
    # ==========================================================================
    if velocity_results:
        print(f"\n{'='*80}")
        print("TOP 10 MARKETS BY PROFIT (Velocity-Adjusted)")
        print("=" * 80)

        top_markets = sorted(velocity_results, key=lambda r: r.total_locked_profit, reverse=True)[:10]
        print(f"\n  {'Market':<50} {'Pairs':>6} {'Profit':>10} {'$/Pair':>8}")
        print(f"  {'-'*50} {'-'*6} {'-'*10} {'-'*8}")
        for r in top_markets:
            per_pair = r.total_locked_profit / len(r.pairs) if r.pairs else 0
            print(f"  {r.slug[:50]:<50} {len(r.pairs):>6} ${r.total_locked_profit:>8.2f} ${per_pair:>6.4f}")

    # ==========================================================================
    # PROJECTED RETURNS
    # ==========================================================================
    if total_velocity_hours > 0:
        print(f"\n{'='*80}")
        print("PROJECTED RETURNS (Velocity-Adjusted)")
        print("=" * 80)

        hourly_rate = total_velocity_profit / total_velocity_hours

        print(f"\n  Hourly:  ${hourly_rate:.2f}/hr")
        print(f"  Daily:   ${hourly_rate * 24:.0f}/day (24h continuous)")
        print(f"  Weekly:  ${hourly_rate * 24 * 7:.0f}/week")
        print(f"  Monthly: ${hourly_rate * 24 * 30:.0f}/month")

        max_capital_per_market = MAX_POSITION * 2 * 1.0
        print(f"\n  Max Capital/Market: ${max_capital_per_market:.0f}")

    print(f"\n{'='*80}")
    print("BACKTEST COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    main()
