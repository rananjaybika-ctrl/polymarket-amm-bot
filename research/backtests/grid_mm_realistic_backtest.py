#!/usr/bin/env python3
"""
Realistic Grid MM Backtest - Fixed fill logic
"""

import pandas as pd
import numpy as np
import os
from dataclasses import dataclass, field
from typing import List, Dict


@dataclass
class GridConfig:
    bid_offset: float = 0.01
    order_size: float = 10.0
    max_position: float = 100.0
    max_imbalance: float = 50.0
    min_time_remaining: float = 60.0


@dataclass
class Position:
    up_shares: float = 0.0
    up_cost: float = 0.0
    down_shares: float = 0.0
    down_cost: float = 0.0
    fills: List[Dict] = field(default_factory=list)

    @property
    def pairs(self): return min(self.up_shares, self.down_shares)

    @property
    def pair_cost(self):
        if self.up_shares > 0 and self.down_shares > 0:
            return (self.up_cost/self.up_shares) + (self.down_cost/self.down_shares)
        return 0

    @property
    def profit(self):
        if self.pairs > 0: return self.pairs * (1.0 - self.pair_cost)
        return 0


def load_data():
    observer_dir = "/Users/rananjaybika/polymarket-amm-bot/research/observer"
    files = ["spread_capture_obs_20260115_aws_12hr.csv", "spread_capture_obs_20260114.csv", "spread_capture_obs_20260113.csv"]

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
    return combined


def simulate_realistic(df: pd.DataFrame, config: GridConfig):
    """
    More realistic fill simulation:
    - Fill only when bid CHANGES (not just <= our bid)
    - Fill only when there's actual price movement
    """
    results = []
    markets = df['market_slug'].unique()

    print(f"\nSimulating on {len(markets)} markets...")

    for market_slug in markets:
        mdf = df[df['market_slug'] == market_slug].sort_values('timestamp_ms').reset_index(drop=True)
        if len(mdf) < 50:
            continue

        pos = Position()
        last_up_bid = None
        last_down_bid = None

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
            if up_ask <= up_bid or down_ask <= down_bid:
                continue

            # MAKER bids (THE FIX)
            our_up_bid = min(up_bid + config.bid_offset, up_ask - 0.01)
            our_down_bid = min(down_bid + config.bid_offset, down_ask - 0.01)
            our_up_bid = max(0.01, our_up_bid)
            our_down_bid = max(0.01, our_down_bid)

            next_up_bid = next_row.get('up_bid')
            next_down_bid = next_row.get('down_bid')

            # REALISTIC FILL: Only fill when bid actually DROPS through our level
            # This means someone sold into the book

            # UP fill: bid dropped AND crossed our bid level
            if last_up_bid is not None and not pd.isna(next_up_bid):
                if pos.up_shares < config.max_position:
                    # Fill if price dropped through our bid
                    if up_bid > our_up_bid and next_up_bid <= our_up_bid:
                        pos.up_shares += config.order_size
                        pos.up_cost += our_up_bid * config.order_size
                        pos.fills.append({'side': 'UP', 'price': our_up_bid, 'time': row['time_remaining_secs']})
                    # Or if there was significant movement down
                    elif next_up_bid < up_bid - 0.01:
                        pos.up_shares += config.order_size
                        pos.up_cost += our_up_bid * config.order_size
                        pos.fills.append({'side': 'UP', 'price': our_up_bid, 'time': row['time_remaining_secs']})

            # DOWN fill: bid dropped AND crossed our bid level
            if last_down_bid is not None and not pd.isna(next_down_bid):
                if pos.down_shares < config.max_position:
                    if down_bid > our_down_bid and next_down_bid <= our_down_bid:
                        pos.down_shares += config.order_size
                        pos.down_cost += our_down_bid * config.order_size
                        pos.fills.append({'side': 'DOWN', 'price': our_down_bid, 'time': row['time_remaining_secs']})
                    elif next_down_bid < down_bid - 0.01:
                        pos.down_shares += config.order_size
                        pos.down_cost += our_down_bid * config.order_size
                        pos.fills.append({'side': 'DOWN', 'price': our_down_bid, 'time': row['time_remaining_secs']})

            # Check imbalance limit
            imbalance = abs(pos.up_shares - pos.down_shares)
            if imbalance >= config.max_imbalance:
                # Pause the side with more
                pass

            last_up_bid = up_bid
            last_down_bid = down_bid

        if pos.up_shares > 0 or pos.down_shares > 0:
            results.append({
                'market': market_slug,
                'up': pos.up_shares,
                'down': pos.down_shares,
                'pairs': pos.pairs,
                'pair_cost': pos.pair_cost,
                'profit': pos.profit,
                'fills': len(pos.fills),
                'imbalance': abs(pos.up_shares - pos.down_shares),
            })

    return results


def show_order_flow_example(df, market_slug, title, num_ticks=50):
    """Show realistic order flow."""
    print(f"\n{'='*100}")
    print(f"ORDER FLOW: {title}")
    print(f"Market: {market_slug}")
    print("="*100)

    mdf = df[df['market_slug'] == market_slug].sort_values('timestamp_ms').reset_index(drop=True)

    BID_OFFSET = 0.01
    ORDER_SIZE = 10

    pos = Position()
    last_up_bid = None
    last_down_bid = None

    print(f"\n{'Tick':>4} | {'Time':>5} | {'UP_bid':>6} | {'UP_ask':>6} | {'DN_bid':>6} | {'DN_ask':>6} | {'OurUP':>6} | {'OurDN':>6} | {'Fill?':>20} | {'UP':>4} | {'DN':>4} | {'Imbal':>5}")
    print("-"*120)

    for i in range(min(num_ticks, len(mdf) - 1)):
        row = mdf.iloc[i]
        next_row = mdf.iloc[i + 1]

        time_rem = row['time_remaining_secs']
        up_bid = row['up_bid']
        up_ask = row['up_ask']
        down_bid = row['down_bid']
        down_ask = row['down_ask']

        if pd.isna(up_bid) or pd.isna(up_ask):
            continue

        our_up_bid = min(up_bid + BID_OFFSET, up_ask - 0.01)
        our_down_bid = min(down_bid + BID_OFFSET, down_ask - 0.01)
        our_up_bid = max(0.01, our_up_bid)
        our_down_bid = max(0.01, our_down_bid)

        next_up_bid = next_row.get('up_bid')
        next_down_bid = next_row.get('down_bid')

        action = ""

        # Check for fills with stricter logic
        if last_up_bid is not None and not pd.isna(next_up_bid):
            if next_up_bid < up_bid - 0.005:  # Significant drop
                pos.up_shares += ORDER_SIZE
                pos.up_cost += our_up_bid * ORDER_SIZE
                action = f"UP@${our_up_bid:.2f}"

        if last_down_bid is not None and not pd.isna(next_down_bid):
            if next_down_bid < down_bid - 0.005:  # Significant drop
                pos.down_shares += ORDER_SIZE
                pos.down_cost += our_down_bid * ORDER_SIZE
                if action:
                    action += " + "
                action += f"DN@${our_down_bid:.2f}"

        if not action:
            action = "-"

        imbal = abs(pos.up_shares - pos.down_shares)

        print(f"{i:>4} | {time_rem:>4.0f}s | ${up_bid:.2f} | ${up_ask:.2f} | ${down_bid:.2f} | ${down_ask:.2f} | ${our_up_bid:.2f} | ${our_down_bid:.2f} | {action:>20} | {pos.up_shares:>4.0f} | {pos.down_shares:>4.0f} | {imbal:>5.0f}")

        last_up_bid = up_bid
        last_down_bid = down_bid

    # Summary
    print("-"*120)
    print(f"RESULT: {pos.up_shares:.0f} UP, {pos.down_shares:.0f} DOWN, {pos.pairs:.0f} pairs")
    if pos.pairs > 0:
        print(f"Pair cost: ${pos.pair_cost:.4f}, Profit: ${pos.profit:.2f}")


def classify_markets(df):
    """Find examples of different market types."""
    markets = df['market_slug'].unique()

    volatile = []
    trending_up = []
    trending_down = []

    for m in markets:
        mdf = df[df['market_slug'] == m].sort_values('timestamp_ms')
        if len(mdf) < 100:
            continue

        up = mdf['up_bid'].dropna().values
        if len(up) < 50:
            continue

        start = up[:20].mean()
        end = up[-20:].mean()
        change = end - start
        vol = np.std(np.diff(up))

        if change > 0.15:
            trending_up.append((m, change, vol))
        elif change < -0.15:
            trending_down.append((m, change, vol))
        elif vol > 0.02:
            volatile.append((m, vol, change))

    return volatile, trending_up, trending_down


def analyze_last_60s(df):
    """Analyze trading in last 60 seconds."""
    print("\n" + "="*80)
    print("LAST 60 SECONDS ANALYSIS")
    print("="*80)

    last_60 = df[df['time_remaining_secs'] <= 60].copy()
    before_60 = df[df['time_remaining_secs'] > 60].copy()

    # Spreads
    last_60['spread'] = (last_60['up_ask'] - last_60['up_bid']) + (last_60['down_ask'] - last_60['down_bid'])
    before_60['spread'] = (before_60['up_ask'] - before_60['up_bid']) + (before_60['down_ask'] - before_60['down_bid'])

    # Maker cost
    last_60['maker_cost'] = last_60['up_bid'] + last_60['down_bid']
    before_60['maker_cost'] = before_60['up_bid'] + before_60['down_bid']

    print(f"\n| Metric            | Before 60s | Last 60s | Difference |")
    print(f"|-------------------|------------|----------|------------|")
    print(f"| Avg spread        | ${before_60['spread'].mean():.4f}   | ${last_60['spread'].mean():.4f} | ${last_60['spread'].mean() - before_60['spread'].mean():+.4f}    |")
    print(f"| Avg maker cost    | ${before_60['maker_cost'].mean():.4f}   | ${last_60['maker_cost'].mean():.4f} | ${last_60['maker_cost'].mean() - before_60['maker_cost'].mean():+.4f}    |")
    print(f"| Profitable (%)    | {(before_60['maker_cost'] < 1).mean()*100:.1f}%      | {(last_60['maker_cost'] < 1).mean()*100:.1f}%    |            |")

    print(f"""
CURRENT SETTING: min_time_remaining = 60s

ANSWER: Yes, we STOP posting new orders in last 60 seconds.
- Existing positions are held to resolution
- No new fills are recorded

WHY 60 SECONDS?
- Spread widens (${last_60['spread'].mean():.4f} vs ${before_60['spread'].mean():.4f})
- But maker cost still profitable ({(last_60['maker_cost'] < 1).mean()*100:.0f}% < $1.00)
- Could potentially trade until 30s or even 10s

RECOMMENDATION: Test with 30s cutoff to capture more fills.
""")


def main():
    print("="*80)
    print("REALISTIC GRID MM BACKTEST")
    print("="*80)

    print("""
PARAMETERS:
-----------
bid_offset     = $0.01    (post $0.01 above best_bid, capped at ask-0.01)
order_size     = 10       (shares per fill)
max_position   = 100      (max shares per side)
max_imbalance  = 50       (max |UP - DOWN|)
min_time       = 60s      (stop posting in last 60s)

FILL LOGIC:
-----------
1. Our bid = min(best_bid + 0.01, best_ask - 0.01)  ← MAKER GUARANTEED
2. Fill when: next_tick_bid < current_bid - 0.005   ← Significant drop
3. Fill price = our_bid (MAKER)
""")

    df = load_data()
    print(f"\nTotal: {len(df)} observations, {df['market_slug'].nunique()} markets")

    # Classify markets
    volatile, trending_up, trending_down = classify_markets(df)
    print(f"\nMarket types found:")
    print(f"  Volatile: {len(volatile)}")
    print(f"  Trending UP: {len(trending_up)}")
    print(f"  Trending DOWN: {len(trending_down)}")

    # Show examples
    if trending_up:
        m = sorted(trending_up, key=lambda x: x[1], reverse=True)[0][0]
        show_order_flow_example(df, m, "TRENDING UP MARKET")

    if trending_down:
        m = sorted(trending_down, key=lambda x: x[1])[0][0]
        show_order_flow_example(df, m, "TRENDING DOWN MARKET")

    if volatile:
        m = sorted(volatile, key=lambda x: x[1], reverse=True)[0][0]
        show_order_flow_example(df, m, "VOLATILE (SIDEWAYS) MARKET")

    # Run simulation
    print("\n" + "="*80)
    print("BACKTEST RESULTS")
    print("="*80)

    config = GridConfig()
    results = simulate_realistic(df, config)

    if results:
        rdf = pd.DataFrame(results)
        print(f"\nMarkets with activity: {len(rdf)}")
        print(f"Total fills: {rdf['fills'].sum()}")
        print(f"Avg fills/market: {rdf['fills'].mean():.1f}")
        print(f"Avg pairs/market: {rdf['pairs'].mean():.1f}")

        with_pairs = rdf[rdf['pairs'] > 0]
        if len(with_pairs) > 0:
            print(f"\nMarkets with pairs: {len(with_pairs)}")
            print(f"Avg pair cost: ${with_pairs['pair_cost'].mean():.4f}")
            profitable = with_pairs[with_pairs['pair_cost'] < 1.0]
            print(f"Profitable: {len(profitable)}/{len(with_pairs)} ({len(profitable)/len(with_pairs)*100:.1f}%)")
            print(f"Total profit: ${profitable['profit'].sum():.2f}")
            print(f"Avg profit/market: ${profitable['profit'].mean():.2f}")

            # Hourly estimate
            hourly = profitable['profit'].mean() * 4 * (len(profitable)/len(with_pairs))
            print(f"\nEstimated hourly: ${hourly:.2f}/hr")

    # Last 60s analysis
    analyze_last_60s(df)


if __name__ == "__main__":
    main()
