#!/usr/bin/env python3
"""
Fixed Grid Market Making Backtest
==================================
Simulates Gabagool-style grid MM with MAKER-only execution.

KEY FIX: Always post bids BELOW best_ask to ensure MAKER fills.

The old grid_maker.py bug:
- Posted at fixed prices without checking orderbook
- Sometimes became TAKER when price moved

The fix:
- bid_price = min(best_bid + offset, best_ask - 0.01)
- This GUARANTEES maker execution
"""

import pandas as pd
import numpy as np
import os
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional


@dataclass
class GridConfig:
    """Grid MM configuration."""
    bid_offset: float = 0.01      # How much above best_bid to post
    order_size: float = 10.0      # Shares per fill
    max_position: float = 100.0   # Max shares per side
    max_imbalance: float = 50.0   # Max imbalance before pausing
    min_time_remaining: float = 60.0  # Don't trade in last 60s


@dataclass
class GridPosition:
    """Track position for a market."""
    up_shares: float = 0.0
    up_cost: float = 0.0
    down_shares: float = 0.0
    down_cost: float = 0.0
    fills: List[Dict] = field(default_factory=list)

    @property
    def pair_count(self) -> float:
        return min(self.up_shares, self.down_shares)

    @property
    def pair_cost(self) -> float:
        if self.up_shares > 0 and self.down_shares > 0:
            avg_up = self.up_cost / self.up_shares
            avg_down = self.down_cost / self.down_shares
            return avg_up + avg_down
        return 0.0

    @property
    def imbalance(self) -> float:
        return abs(self.up_shares - self.down_shares)

    @property
    def locked_profit(self) -> float:
        pairs = self.pair_count
        if pairs > 0 and self.pair_cost > 0:
            return pairs * (1.0 - self.pair_cost)
        return 0.0


def load_observer_data() -> pd.DataFrame:
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

    # Deduplicate by market_slug and timestamp
    combined = combined.drop_duplicates(subset=['market_slug', 'timestamp_ms'])

    print(f"\nTotal rows after dedup: {len(combined)}")
    print(f"Unique markets: {combined['market_slug'].nunique()}")

    return combined


def simulate_grid_mm(df: pd.DataFrame, config: GridConfig) -> Dict:
    """
    Simulate grid MM strategy on observer data.

    MAKER-only logic:
    1. Calculate our bid: min(best_bid + offset, best_ask - 0.01)
    2. Fill occurs when next tick's bid <= our bid (someone sold into us)
    3. Track both sides independently
    """
    results = []

    markets = df['market_slug'].unique()
    print(f"\nSimulating on {len(markets)} markets...")

    for market_slug in markets:
        mdf = df[df['market_slug'] == market_slug].sort_values('timestamp_ms').reset_index(drop=True)

        if len(mdf) < 10:
            continue

        pos = GridPosition()

        for i in range(len(mdf) - 1):
            row = mdf.iloc[i]
            next_row = mdf.iloc[i + 1]

            # Skip if not enough time remaining
            if row['time_remaining_secs'] < config.min_time_remaining:
                continue

            # Get current orderbook
            up_bid = row['up_bid']
            up_ask = row['up_ask']
            down_bid = row['down_bid']
            down_ask = row['down_ask']

            # Skip invalid data
            if pd.isna(up_bid) or pd.isna(up_ask) or pd.isna(down_bid) or pd.isna(down_ask):
                continue
            if up_ask <= up_bid or down_ask <= down_bid:
                continue

            # Check position limits
            if pos.up_shares >= config.max_position or pos.down_shares >= config.max_position:
                continue
            if pos.imbalance >= config.max_imbalance:
                continue

            # === MAKER BID CALCULATION (THE FIX) ===
            # Post slightly above best_bid, but NEVER at or above best_ask
            our_up_bid = min(up_bid + config.bid_offset, up_ask - 0.01)
            our_down_bid = min(down_bid + config.bid_offset, down_ask - 0.01)

            # Ensure valid prices
            our_up_bid = max(0.01, our_up_bid)
            our_down_bid = max(0.01, our_down_bid)

            # === FILL SIMULATION ===
            # We get filled when next tick's best_bid drops to or below our bid
            # This means someone market-sold into the book, hitting our resting bid

            next_up_bid = next_row['up_bid']
            next_down_bid = next_row['down_bid']

            # Check for UP fill
            if not pd.isna(next_up_bid) and next_up_bid <= our_up_bid:
                # We got filled at our bid price (MAKER)
                fill_price = our_up_bid
                pos.up_shares += config.order_size
                pos.up_cost += fill_price * config.order_size
                pos.fills.append({
                    'side': 'UP',
                    'price': fill_price,
                    'size': config.order_size,
                    'timestamp': row['timestamp_ms'],
                    'time_remaining': row['time_remaining_secs'],
                })

            # Check for DOWN fill
            if not pd.isna(next_down_bid) and next_down_bid <= our_down_bid:
                fill_price = our_down_bid
                pos.down_shares += config.order_size
                pos.down_cost += fill_price * config.order_size
                pos.fills.append({
                    'side': 'DOWN',
                    'price': fill_price,
                    'size': config.order_size,
                    'timestamp': row['timestamp_ms'],
                    'time_remaining': row['time_remaining_secs'],
                })

        # Record market results
        if pos.up_shares > 0 or pos.down_shares > 0:
            results.append({
                'market_slug': market_slug,
                'up_shares': pos.up_shares,
                'down_shares': pos.down_shares,
                'pair_count': pos.pair_count,
                'pair_cost': pos.pair_cost,
                'locked_profit': pos.locked_profit,
                'imbalance': pos.imbalance,
                'total_fills': len(pos.fills),
                'up_fills': len([f for f in pos.fills if f['side'] == 'UP']),
                'down_fills': len([f for f in pos.fills if f['side'] == 'DOWN']),
            })

    return results


def simulate_aggressive_grid_mm(df: pd.DataFrame, config: GridConfig) -> Dict:
    """
    More aggressive simulation that assumes fills on price movement.

    Key insight from wallet 0x640a...:
    - They get ~164 trades per 15-min market
    - That's ~11 trades per minute
    - They're capturing almost every price oscillation

    This simulation:
    1. Detects price movements (bid changes)
    2. Assumes we capture fills on those movements if we're posting
    3. Fill price = best_bid at time of fill (MAKER)
    """
    results = []

    markets = df['market_slug'].unique()
    print(f"\nSimulating AGGRESSIVE grid on {len(markets)} markets...")

    for market_slug in markets:
        mdf = df[df['market_slug'] == market_slug].sort_values('timestamp_ms').reset_index(drop=True)

        if len(mdf) < 10:
            continue

        pos = GridPosition()
        last_up_bid = None
        last_down_bid = None

        for i in range(len(mdf)):
            row = mdf.iloc[i]

            # Skip if not enough time remaining
            if row['time_remaining_secs'] < config.min_time_remaining:
                continue

            up_bid = row['up_bid']
            up_ask = row['up_ask']
            down_bid = row['down_bid']
            down_ask = row['down_ask']

            if pd.isna(up_bid) or pd.isna(down_bid):
                continue

            # Check position limits
            if pos.up_shares >= config.max_position and pos.down_shares >= config.max_position:
                continue

            # Detect price movements and simulate fills
            # When bid DROPS, someone sold → we get filled at bid
            if last_up_bid is not None and pos.up_shares < config.max_position:
                if up_bid < last_up_bid - 0.005:  # Significant drop
                    # We got filled at our bid (which was last_up_bid + offset, capped)
                    if not pd.isna(up_ask):
                        our_bid = min(last_up_bid + config.bid_offset, up_ask - 0.01)
                        our_bid = max(0.01, our_bid)
                        pos.up_shares += config.order_size
                        pos.up_cost += our_bid * config.order_size
                        pos.fills.append({'side': 'UP', 'price': our_bid})

            if last_down_bid is not None and pos.down_shares < config.max_position:
                if down_bid < last_down_bid - 0.005:
                    if not pd.isna(down_ask):
                        our_bid = min(last_down_bid + config.bid_offset, down_ask - 0.01)
                        our_bid = max(0.01, our_bid)
                        pos.down_shares += config.order_size
                        pos.down_cost += our_bid * config.order_size
                        pos.fills.append({'side': 'DOWN', 'price': our_bid})

            last_up_bid = up_bid
            last_down_bid = down_bid

        if pos.up_shares > 0 or pos.down_shares > 0:
            results.append({
                'market_slug': market_slug,
                'up_shares': pos.up_shares,
                'down_shares': pos.down_shares,
                'pair_count': pos.pair_count,
                'pair_cost': pos.pair_cost,
                'locked_profit': pos.locked_profit,
                'imbalance': pos.imbalance,
                'total_fills': len(pos.fills),
                'up_fills': len([f for f in pos.fills if f['side'] == 'UP']),
                'down_fills': len([f for f in pos.fills if f['side'] == 'DOWN']),
            })

    return results


def simulate_continuous_posting(df: pd.DataFrame, config: GridConfig) -> Dict:
    """
    Simulate continuous two-sided posting like wallet 0x640a...

    Key insight: They post on BOTH sides simultaneously.
    When price oscillates, BOTH sides fill.

    Simulation:
    1. At each tick, calculate our maker bids on both sides
    2. If either bid would have filled (price moved through it), record fill
    3. Track balance and pair cost
    """
    results = []

    markets = df['market_slug'].unique()
    print(f"\nSimulating CONTINUOUS posting on {len(markets)} markets...")

    for market_slug in markets:
        mdf = df[df['market_slug'] == market_slug].sort_values('timestamp_ms').reset_index(drop=True)

        if len(mdf) < 10:
            continue

        pos = GridPosition()

        # Track our resting bids
        our_up_bid = None
        our_down_bid = None

        for i in range(len(mdf) - 1):
            row = mdf.iloc[i]
            next_row = mdf.iloc[i + 1]

            if row['time_remaining_secs'] < config.min_time_remaining:
                continue

            up_bid = row['up_bid']
            up_ask = row['up_ask']
            down_bid = row['down_bid']
            down_ask = row['down_ask']

            if pd.isna(up_bid) or pd.isna(up_ask) or pd.isna(down_bid) or pd.isna(down_ask):
                continue

            # Calculate our maker bids (ALWAYS below ask)
            our_up_bid = min(up_bid + config.bid_offset, up_ask - 0.01)
            our_down_bid = min(down_bid + config.bid_offset, down_ask - 0.01)
            our_up_bid = max(0.01, our_up_bid)
            our_down_bid = max(0.01, our_down_bid)

            next_up_bid = next_row.get('up_bid')
            next_down_bid = next_row.get('down_bid')

            # Check for fills
            # UP fill: next bid dropped below our bid
            if pos.up_shares < config.max_position:
                if not pd.isna(next_up_bid) and next_up_bid <= our_up_bid:
                    pos.up_shares += config.order_size
                    pos.up_cost += our_up_bid * config.order_size
                    pos.fills.append({'side': 'UP', 'price': our_up_bid})

            # DOWN fill: next bid dropped below our bid
            if pos.down_shares < config.max_position:
                if not pd.isna(next_down_bid) and next_down_bid <= our_down_bid:
                    pos.down_shares += config.order_size
                    pos.down_cost += our_down_bid * config.order_size
                    pos.fills.append({'side': 'DOWN', 'price': our_down_bid})

            # Check imbalance limit
            if pos.imbalance >= config.max_imbalance:
                # Pause the side with more shares
                pass

        if pos.up_shares > 0 or pos.down_shares > 0:
            results.append({
                'market_slug': market_slug,
                'up_shares': pos.up_shares,
                'down_shares': pos.down_shares,
                'pair_count': pos.pair_count,
                'pair_cost': pos.pair_cost,
                'locked_profit': pos.locked_profit,
                'imbalance': pos.imbalance,
                'total_fills': len(pos.fills),
                'up_fills': len([f for f in pos.fills if f['side'] == 'UP']),
                'down_fills': len([f for f in pos.fills if f['side'] == 'DOWN']),
            })

    return results


def analyze_results(results: List[Dict]) -> None:
    """Analyze and print backtest results."""
    if not results:
        print("\nNo results to analyze!")
        return

    df = pd.DataFrame(results)

    print("\n" + "="*70)
    print("GRID MM BACKTEST RESULTS (MAKER-ONLY)")
    print("="*70)

    print(f"\nMarkets with fills: {len(df)}")
    print(f"Total fills: {df['total_fills'].sum()}")
    print(f"Total UP fills: {df['up_fills'].sum()}")
    print(f"Total DOWN fills: {df['down_fills'].sum()}")

    print(f"\n--- Position Stats ---")
    print(f"Avg UP shares/market: {df['up_shares'].mean():.1f}")
    print(f"Avg DOWN shares/market: {df['down_shares'].mean():.1f}")
    print(f"Avg pairs/market: {df['pair_count'].mean():.1f}")
    print(f"Avg imbalance/market: {df['imbalance'].mean():.1f}")

    # Filter to markets with pairs
    with_pairs = df[df['pair_count'] > 0]
    print(f"\n--- Markets with Pairs: {len(with_pairs)} ---")

    if len(with_pairs) > 0:
        print(f"Avg pair cost: ${with_pairs['pair_cost'].mean():.4f}")
        print(f"Min pair cost: ${with_pairs['pair_cost'].min():.4f}")
        print(f"Max pair cost: ${with_pairs['pair_cost'].max():.4f}")

        profitable = with_pairs[with_pairs['pair_cost'] < 1.0]
        print(f"\nProfitable markets (pair_cost < $1.00): {len(profitable)} / {len(with_pairs)} ({len(profitable)/len(with_pairs)*100:.1f}%)")

        if len(profitable) > 0:
            print(f"Avg profitable pair cost: ${profitable['pair_cost'].mean():.4f}")
            print(f"Total locked profit: ${profitable['locked_profit'].sum():.2f}")
            print(f"Avg profit/market: ${profitable['locked_profit'].mean():.2f}")

            # Calculate hourly rate (assuming 15-min markets, 4/hour)
            markets_per_hour = 4
            hourly_profit = profitable['locked_profit'].mean() * markets_per_hour * (len(profitable) / len(with_pairs))
            print(f"\nEstimated hourly rate: ${hourly_profit:.2f}/hr")

    # Pair cost distribution
    print(f"\n--- Pair Cost Distribution ---")
    if len(with_pairs) > 0:
        bins = [0.90, 0.95, 0.98, 0.99, 1.00, 1.01, 1.02, 1.05, 1.10]
        for i in range(len(bins) - 1):
            count = len(with_pairs[(with_pairs['pair_cost'] >= bins[i]) & (with_pairs['pair_cost'] < bins[i+1])])
            pct = count / len(with_pairs) * 100
            print(f"  ${bins[i]:.2f} - ${bins[i+1]:.2f}: {count} markets ({pct:.1f}%)")


def run_parameter_sweep(df: pd.DataFrame) -> None:
    """Test different bid offsets to find optimal."""
    print("\n" + "="*70)
    print("PARAMETER SWEEP: BID OFFSET")
    print("="*70)

    offsets = [0.005, 0.01, 0.015, 0.02, 0.025, 0.03]

    sweep_results = []

    for offset in offsets:
        config = GridConfig(bid_offset=offset)
        results = simulate_grid_mm(df, config)

        if results:
            results_df = pd.DataFrame(results)
            with_pairs = results_df[results_df['pair_count'] > 0]

            if len(with_pairs) > 0:
                profitable = with_pairs[with_pairs['pair_cost'] < 1.0]
                avg_pair_cost = with_pairs['pair_cost'].mean()
                total_profit = profitable['locked_profit'].sum() if len(profitable) > 0 else 0
                profitable_pct = len(profitable) / len(with_pairs) * 100

                sweep_results.append({
                    'offset': offset,
                    'markets_with_pairs': len(with_pairs),
                    'avg_pair_cost': avg_pair_cost,
                    'profitable_pct': profitable_pct,
                    'total_profit': total_profit,
                    'avg_profit': total_profit / len(with_pairs) if len(with_pairs) > 0 else 0,
                })

    print("\n| Offset | Markets | Avg Cost | Profitable | Total Profit | Avg/Market |")
    print("|--------|---------|----------|------------|--------------|------------|")
    for r in sweep_results:
        print(f"| ${r['offset']:.3f} | {r['markets_with_pairs']:>7} | ${r['avg_pair_cost']:.4f} | {r['profitable_pct']:>9.1f}% | ${r['total_profit']:>11.2f} | ${r['avg_profit']:>9.2f} |")

    # Find best
    if sweep_results:
        best = max(sweep_results, key=lambda x: x['total_profit'])
        print(f"\nBest offset: ${best['offset']:.3f} (${best['total_profit']:.2f} total profit)")


def compare_maker_vs_taker(df: pd.DataFrame) -> None:
    """Compare MAKER fills vs what TAKER would cost."""
    print("\n" + "="*70)
    print("MAKER vs TAKER COMPARISON")
    print("="*70)

    # Calculate what MAKER vs TAKER pair costs would be at each tick
    df = df.copy()
    df['maker_pair_cost'] = df['up_bid'] + df['down_bid']
    df['taker_pair_cost'] = df['up_ask'] + df['down_ask']
    df['spread'] = df['taker_pair_cost'] - df['maker_pair_cost']

    valid = df.dropna(subset=['maker_pair_cost', 'taker_pair_cost'])

    print(f"\nObservations: {len(valid)}")
    print(f"\nMAKER (bid+bid):")
    print(f"  Mean: ${valid['maker_pair_cost'].mean():.4f}")
    print(f"  Profitable (<$1.00): {(valid['maker_pair_cost'] < 1.0).mean()*100:.1f}%")

    print(f"\nTAKER (ask+ask):")
    print(f"  Mean: ${valid['taker_pair_cost'].mean():.4f}")
    print(f"  Profitable (<$1.00): {(valid['taker_pair_cost'] < 1.0).mean()*100:.1f}%")

    print(f"\nSpread (TAKER - MAKER):")
    print(f"  Mean: ${valid['spread'].mean():.4f}")
    print(f"  This is the MAKER edge per pair")


def estimate_theoretical_profit(df: pd.DataFrame) -> None:
    """
    Estimate theoretical profit if we captured all price oscillations.

    Based on wallet 0x640a...:
    - ~164 trades per 15-min market (82 UP + 82 DOWN)
    - $0.9961 pair cost achieved
    - ~$0.0039 profit per pair × 82 pairs = $0.32/market
    """
    print("\n" + "="*70)
    print("THEORETICAL PROFIT ESTIMATE")
    print("="*70)

    # Count price oscillations per market
    markets = df['market_slug'].unique()
    oscillation_counts = []

    for market_slug in markets:
        mdf = df[df['market_slug'] == market_slug].sort_values('timestamp_ms')

        if len(mdf) < 10:
            continue

        up_bids = mdf['up_bid'].dropna().values
        down_bids = mdf['down_bid'].dropna().values

        # Count direction changes (oscillations)
        up_changes = sum(1 for i in range(1, len(up_bids)) if up_bids[i] != up_bids[i-1])
        down_changes = sum(1 for i in range(1, len(down_bids)) if down_bids[i] != down_bids[i-1])

        oscillation_counts.append({
            'market_slug': market_slug,
            'up_changes': up_changes,
            'down_changes': down_changes,
            'total_changes': up_changes + down_changes,
            'ticks': len(mdf),
        })

    osc_df = pd.DataFrame(oscillation_counts)

    print(f"\nMarkets analyzed: {len(osc_df)}")
    print(f"Avg ticks per market: {osc_df['ticks'].mean():.0f}")
    print(f"Avg UP price changes: {osc_df['up_changes'].mean():.0f}")
    print(f"Avg DOWN price changes: {osc_df['down_changes'].mean():.0f}")

    # Estimate fills if we captured 50% of oscillations
    avg_maker_cost = df['up_bid'].mean() + df['down_bid'].mean()
    avg_profit_per_pair = 1.0 - avg_maker_cost

    fill_rate = 0.5  # Assume we capture 50% of price movements
    avg_pairs = min(osc_df['up_changes'].mean(), osc_df['down_changes'].mean()) * fill_rate

    print(f"\nAt {fill_rate*100:.0f}% fill rate:")
    print(f"  Estimated pairs/market: {avg_pairs:.0f}")
    print(f"  Avg maker pair cost: ${avg_maker_cost:.4f}")
    print(f"  Profit/pair: ${avg_profit_per_pair:.4f}")
    print(f"  Profit/market: ${avg_pairs * avg_profit_per_pair:.2f}")
    print(f"  Hourly (4 markets): ${avg_pairs * avg_profit_per_pair * 4:.2f}/hr")


def main():
    print("="*70)
    print("FIXED GRID MM BACKTEST")
    print("="*70)
    print("\nKey fix: bid_price = min(best_bid + offset, best_ask - 0.01)")
    print("This ensures MAKER execution (never cross the spread)")

    # Load data
    df = load_observer_data()
    if df is None:
        print("No data loaded")
        return

    # Compare MAKER vs TAKER theoretical
    compare_maker_vs_taker(df)

    # Theoretical estimate
    estimate_theoretical_profit(df)

    # Run default simulation
    print("\n" + "="*70)
    print("SIMULATION 1: CONSERVATIVE (wait for bid drop)")
    print("="*70)

    config = GridConfig(
        bid_offset=0.01,
        order_size=10.0,
        max_position=200.0,
        max_imbalance=100.0,
    )
    print(f"\nConfig: offset=${config.bid_offset}, size={config.order_size}, max_pos={config.max_position}")

    results = simulate_grid_mm(df, config)
    analyze_results(results)

    # Try continuous posting simulation
    print("\n" + "="*70)
    print("SIMULATION 2: CONTINUOUS POSTING (more aggressive)")
    print("="*70)

    results2 = simulate_continuous_posting(df, config)
    analyze_results(results2)

    # Parameter sweep
    run_parameter_sweep(df)


if __name__ == "__main__":
    main()
