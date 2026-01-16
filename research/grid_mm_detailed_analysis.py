#!/usr/bin/env python3
"""
Detailed Grid MM Analysis - Show order flow in different market conditions
"""

import pandas as pd
import numpy as np
import os


def load_data():
    """Load observer data."""
    observer_dir = "/Users/rananjaybika/polymarket-amm-bot/research/observer"
    files = [
        "spread_capture_obs_20260115_aws_12hr.csv",
        "spread_capture_obs_20260114.csv",
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
    return combined


def show_backtest_methodology():
    """Explain exactly how the backtest works."""
    print("="*80)
    print("BACKTEST METHODOLOGY")
    print("="*80)

    print("""
HOW THE SIMULATION WORKS:
=========================

1. PARAMETERS USED:
   - bid_offset = $0.01 (how much above best_bid to post)
   - order_size = 10 shares per fill
   - max_position = 200 shares per side
   - max_imbalance = 100 shares (UP - DOWN)
   - min_time_remaining = 60 seconds

2. AT EACH TICK:
   a) Read orderbook: up_bid, up_ask, down_bid, down_ask

   b) Calculate our MAKER bids:
      our_up_bid = min(up_bid + 0.01, up_ask - 0.01)
      our_down_bid = min(down_bid + 0.01, down_ask - 0.01)

      This GUARANTEES we never cross the spread (always MAKER)

   c) Check for fills:
      - UP fill if: next_tick's up_bid <= our_up_bid
        (Someone sold, bid dropped to or below our level)
      - DOWN fill if: next_tick's down_bid <= our_down_bid

   d) Record fill at OUR bid price (MAKER price)

3. FILL LOGIC EXPLANATION:

   Tick 1: up_bid=$0.50, up_ask=$0.52
           Our bid = min($0.51, $0.51) = $0.51

   Tick 2: up_bid=$0.49 (dropped!)
           This means someone SOLD into the book
           Our resting bid at $0.51 would have been HIT
           We record a fill at $0.51

4. WHY THIS IS CONSERVATIVE:
   - We only fill when bid DROPS (confirmed sell pressure)
   - Real market makers fill on ANY matching order
   - So actual fill rates should be HIGHER than simulation
""")


def analyze_market_conditions(df):
    """Classify markets by condition and show examples."""
    print("\n" + "="*80)
    print("MARKET CONDITION ANALYSIS")
    print("="*80)

    markets = df['market_slug'].unique()

    volatile_markets = []
    trending_up_markets = []
    trending_down_markets = []
    neutral_markets = []

    for market_slug in markets:
        mdf = df[df['market_slug'] == market_slug].sort_values('timestamp_ms')

        if len(mdf) < 100:
            continue

        # Get price series
        up_prices = mdf['up_bid'].dropna().values
        down_prices = mdf['down_bid'].dropna().values

        if len(up_prices) < 50:
            continue

        # Calculate metrics
        start_up = up_prices[:10].mean()
        end_up = up_prices[-10:].mean()
        up_change = end_up - start_up

        # Volatility = std of price changes
        up_volatility = np.std(np.diff(up_prices))

        # Classify
        if abs(up_change) < 0.05 and up_volatility > 0.01:
            volatile_markets.append((market_slug, up_volatility, up_change))
        elif up_change > 0.10:
            trending_up_markets.append((market_slug, up_change, up_volatility))
        elif up_change < -0.10:
            trending_down_markets.append((market_slug, up_change, up_volatility))
        else:
            neutral_markets.append((market_slug, up_change, up_volatility))

    print(f"\nMarket Classification:")
    print(f"  Volatile (sideways, high vol): {len(volatile_markets)}")
    print(f"  Trending UP (>10% move): {len(trending_up_markets)}")
    print(f"  Trending DOWN (>10% move): {len(trending_down_markets)}")
    print(f"  Neutral: {len(neutral_markets)}")

    return volatile_markets, trending_up_markets, trending_down_markets


def show_order_flow(df, market_slug, market_type, num_ticks=30):
    """Show detailed order flow for a specific market."""
    print(f"\n" + "-"*80)
    print(f"ORDER FLOW: {market_type.upper()} MARKET")
    print(f"Market: {market_slug}")
    print("-"*80)

    mdf = df[df['market_slug'] == market_slug].sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) < num_ticks:
        print("Not enough data")
        return

    # Parameters
    BID_OFFSET = 0.01
    ORDER_SIZE = 10

    # Track position
    up_shares = 0
    down_shares = 0
    up_cost = 0
    down_cost = 0
    fills = []

    print(f"\n{'Tick':>4} | {'Time':>6} | {'UP_bid':>7} | {'UP_ask':>7} | {'DN_bid':>7} | {'DN_ask':>7} | {'Our UP':>7} | {'Our DN':>7} | {'Action':>20} | {'Pos UP':>6} | {'Pos DN':>6}")
    print("-"*140)

    # Show order flow for first N ticks
    for i in range(min(num_ticks, len(mdf) - 1)):
        row = mdf.iloc[i]
        next_row = mdf.iloc[i + 1]

        time_remaining = row['time_remaining_secs']
        up_bid = row['up_bid']
        up_ask = row['up_ask']
        down_bid = row['down_bid']
        down_ask = row['down_ask']

        if pd.isna(up_bid) or pd.isna(up_ask) or pd.isna(down_bid) or pd.isna(down_ask):
            continue

        # Calculate our MAKER bids
        our_up_bid = min(up_bid + BID_OFFSET, up_ask - 0.01)
        our_down_bid = min(down_bid + BID_OFFSET, down_ask - 0.01)
        our_up_bid = max(0.01, our_up_bid)
        our_down_bid = max(0.01, our_down_bid)

        # Check for fills
        next_up_bid = next_row['up_bid']
        next_down_bid = next_row['down_bid']

        action = ""

        if not pd.isna(next_up_bid) and next_up_bid <= our_up_bid:
            up_shares += ORDER_SIZE
            up_cost += our_up_bid * ORDER_SIZE
            action = f"FILL UP @${our_up_bid:.2f}"
            fills.append(('UP', our_up_bid))

        if not pd.isna(next_down_bid) and next_down_bid <= our_down_bid:
            down_shares += ORDER_SIZE
            down_cost += our_down_bid * ORDER_SIZE
            if action:
                action += " + "
            action += f"FILL DN @${our_down_bid:.2f}"
            fills.append(('DOWN', our_down_bid))

        if not action:
            action = "posting..."

        print(f"{i:>4} | {time_remaining:>5.0f}s | ${up_bid:>5.2f} | ${up_ask:>5.2f} | ${down_bid:>5.2f} | ${down_ask:>5.2f} | ${our_up_bid:>5.2f} | ${our_down_bid:>5.2f} | {action:<20} | {up_shares:>6} | {down_shares:>6}")

    # Summary
    print("\n" + "-"*40)
    print(f"SUMMARY for {num_ticks} ticks:")
    print(f"  UP fills: {len([f for f in fills if f[0]=='UP'])}")
    print(f"  DOWN fills: {len([f for f in fills if f[0]=='DOWN'])}")
    print(f"  Position: {up_shares} UP, {down_shares} DOWN")

    if up_shares > 0 and down_shares > 0:
        avg_up = up_cost / up_shares
        avg_down = down_cost / down_shares
        pair_cost = avg_up + avg_down
        pairs = min(up_shares, down_shares)
        profit = pairs * (1.0 - pair_cost)
        print(f"  Avg UP price: ${avg_up:.4f}")
        print(f"  Avg DOWN price: ${avg_down:.4f}")
        print(f"  Pair cost: ${pair_cost:.4f}")
        print(f"  Pairs: {pairs}")
        print(f"  Locked profit: ${profit:.2f}")


def analyze_last_60_seconds(df):
    """Show what happens in last 60 seconds."""
    print("\n" + "="*80)
    print("LAST 60 SECONDS ANALYSIS")
    print("="*80)

    print("""
CURRENT RULE: min_time_remaining = 60 seconds
- We STOP posting new orders when time_remaining < 60s
- Existing positions are held to resolution

WHY?
1. Spread widens near resolution (less liquidity)
2. Price becomes more predictable (less oscillation profit)
3. Risk of being caught one-sided increases
""")

    # Analyze what pair costs look like in last 60s
    last_60 = df[df['time_remaining_secs'] <= 60].copy()
    last_60['maker_cost'] = last_60['up_bid'] + last_60['down_bid']
    last_60['taker_cost'] = last_60['up_ask'] + last_60['down_ask']
    last_60['spread'] = last_60['taker_cost'] - last_60['maker_cost']

    before_60 = df[df['time_remaining_secs'] > 60].copy()
    before_60['maker_cost'] = before_60['up_bid'] + before_60['down_bid']
    before_60['spread'] = (before_60['up_ask'] + before_60['down_ask']) - before_60['maker_cost']

    print(f"\nSpread Comparison:")
    print(f"  Before last 60s: ${before_60['spread'].mean():.4f} avg spread")
    print(f"  Last 60s:        ${last_60['spread'].mean():.4f} avg spread")
    print(f"  Spread WIDENS by: ${last_60['spread'].mean() - before_60['spread'].mean():.4f}")

    print(f"\nMaker Cost Comparison:")
    print(f"  Before last 60s: ${before_60['maker_cost'].mean():.4f}")
    print(f"  Last 60s:        ${last_60['maker_cost'].mean():.4f}")

    # Should we trade in last 60s?
    last_60_profitable = (last_60['maker_cost'] < 1.0).mean() * 100
    before_60_profitable = (before_60['maker_cost'] < 1.0).mean() * 100

    print(f"\nProfitability:")
    print(f"  Before last 60s: {before_60_profitable:.1f}% of ticks profitable")
    print(f"  Last 60s:        {last_60_profitable:.1f}% of ticks profitable")

    if last_60_profitable > 90:
        print(f"\n>>> RECOMMENDATION: Could consider trading until 30s or even 10s")
    else:
        print(f"\n>>> RECOMMENDATION: Keep 60s cutoff (spread too wide)")


def show_parameters_used():
    """Show all parameters used in backtest."""
    print("\n" + "="*80)
    print("BACKTEST PARAMETERS")
    print("="*80)

    print("""
PARAMETERS USED IN BACKTEST:
============================

@dataclass
class GridConfig:
    bid_offset: float = 0.01      # Post $0.01 above best_bid
    order_size: float = 10.0      # 10 shares per fill
    max_position: float = 200.0   # Max 200 shares per side
    max_imbalance: float = 100.0  # Max 100 share imbalance (UP - DOWN)
    min_time_remaining: float = 60.0  # Stop trading at 60s

FILL LOGIC:
===========
1. Our UP bid = min(up_bid + 0.01, up_ask - 0.01)
   - Front-run by $0.01, but NEVER cross spread

2. Our DOWN bid = min(down_bid + 0.01, down_ask - 0.01)
   - Same logic

3. Fill occurs when:
   - next_tick's bid <= our_bid
   - This means someone sold into the book

4. Fill price = our_bid (MAKER price, not taker)

POSITION LIMITS:
================
- Stop posting UP if up_shares >= 200
- Stop posting DOWN if down_shares >= 200
- Stop posting if |up_shares - down_shares| >= 100

TIME LIMIT:
===========
- Stop ALL posting if time_remaining < 60 seconds
- Hold existing position to resolution
""")


def main():
    print("="*80)
    print("DETAILED GRID MM BACKTEST ANALYSIS")
    print("="*80)

    # Show methodology
    show_backtest_methodology()

    # Show parameters
    show_parameters_used()

    # Load data
    df = load_data()
    print(f"\nLoaded {len(df)} observations from {df['market_slug'].nunique()} markets")

    # Classify markets
    volatile, trending_up, trending_down = analyze_market_conditions(df)

    # Show order flow examples
    if volatile:
        market = volatile[0][0]
        show_order_flow(df, market, "VOLATILE (sideways)")

    if trending_up:
        market = trending_up[0][0]
        show_order_flow(df, market, "TRENDING UP")

    if trending_down:
        market = trending_down[0][0]
        show_order_flow(df, market, "TRENDING DOWN")

    # Last 60s analysis
    analyze_last_60_seconds(df)


if __name__ == "__main__":
    main()
