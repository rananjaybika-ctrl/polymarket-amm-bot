#!/usr/bin/env python3
"""
Balanced pair accumulation - always hedge to stay balanced.

Strategy:
1. Quote both sides as maker (bid = ask - offset)
2. When one side fills, immediately try to fill the other
3. Track imbalance and manage exposure

Key question: Can we accumulate balanced pairs profitably?
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@dataclass
class Trade:
    slug: str
    up_shares: int
    down_shares: int
    up_cost: float
    down_cost: float
    pair_cost: float
    resolution: str
    pnl: float


def simulate_balanced_mm(obs_df: pd.DataFrame, offset: float = 0.01) -> List[Trade]:
    """Simulate balanced market making."""

    trades = []

    for slug in obs_df['market_slug'].unique():
        mdf = obs_df[obs_df['market_slug'] == slug].copy()
        mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

        if len(mdf) < 100:
            continue

        # Resolution
        last = mdf.iloc[-1]
        if last['up_bid'] > 0.9:
            resolution = 'UP'
        elif last['down_bid'] > 0.9:
            resolution = 'DOWN'
        else:
            continue

        # State
        up_shares = 0
        down_shares = 0
        up_cost = 0.0
        down_cost = 0.0

        pending_up = None  # (bid_price, timestamp)
        pending_down = None
        last_order_ts = 0
        cooldown_ms = 5000  # 5s between new orders

        for _, row in mdf.iterrows():
            ts = row['timestamp_ms']
            up_ask = row['up_ask']
            down_ask = row['down_ask']
            time_rem = row.get('time_remaining_secs', 450)

            if pd.isna(up_ask) or pd.isna(down_ask):
                continue

            # Check for fills
            if pending_up and up_ask <= pending_up[0]:
                up_shares += 1
                up_cost += pending_up[0]
                pending_up = None

            if pending_down and down_ask <= pending_down[0]:
                down_shares += 1
                down_cost += pending_down[0]
                pending_down = None

            # Don't trade in last 60s (manipulation risk)
            if time_rem < 60:
                continue

            # Calculate imbalance
            total = up_shares + down_shares
            if total > 0:
                imbalance = (up_shares - down_shares) / total
            else:
                imbalance = 0

            # Strategy: Stay balanced
            # If imbalanced, prioritize the lagging side
            # If balanced, quote both sides

            if ts - last_order_ts < cooldown_ms:
                continue

            if abs(imbalance) > 0.3:
                # Imbalanced - only quote lagging side
                if up_shares < down_shares and pending_up is None:
                    pending_up = (up_ask - offset, ts)
                    last_order_ts = ts
                elif down_shares < up_shares and pending_down is None:
                    pending_down = (down_ask - offset, ts)
                    last_order_ts = ts
            else:
                # Balanced - quote both sides
                if pending_up is None:
                    pending_up = (up_ask - offset, ts)
                if pending_down is None:
                    pending_down = (down_ask - offset, ts)
                last_order_ts = ts

        # Calculate results
        if up_shares > 0 or down_shares > 0:
            pairs = min(up_shares, down_shares)
            pair_cost = (up_cost + down_cost) / max(pairs, 1) if pairs > 0 else 0

            # PnL: pairs are hedged, exposed shares depend on resolution
            if resolution == 'UP':
                pnl = up_shares * 1.0 - up_cost - down_cost
            else:
                pnl = down_shares * 1.0 - up_cost - down_cost

            trades.append(Trade(
                slug=slug,
                up_shares=up_shares,
                down_shares=down_shares,
                up_cost=up_cost,
                down_cost=down_cost,
                pair_cost=pair_cost,
                resolution=resolution,
                pnl=pnl,
            ))

    return trades


def main():
    print("=" * 60)
    print("BALANCED PAIR ACCUMULATION")
    print("=" * 60)

    # Load data
    obs_path = Path("research/observer/PROTECTED_grid_obs_is_oos2_combined.csv")
    print("\nLoading...")
    obs_df = pd.read_csv(obs_path, low_memory=False)
    print(f"  Obs: {len(obs_df):,}")

    # Test different offsets
    for offset in [0.01, 0.02, 0.03]:
        print(f"\n{'='*60}")
        print(f"OFFSET: {offset*100:.0f}c below ask")
        print("=" * 60)

        trades = simulate_balanced_mm(obs_df, offset)

        if not trades:
            print("No trades")
            continue

        # Stats
        total_pnl = sum(t.pnl for t in trades)
        total_up = sum(t.up_shares for t in trades)
        total_down = sum(t.down_shares for t in trades)
        total_pairs = sum(min(t.up_shares, t.down_shares) for t in trades)
        total_cost = sum(t.up_cost + t.down_cost for t in trades)

        winners = [t for t in trades if t.pnl > 0]
        losers = [t for t in trades if t.pnl <= 0]

        print(f"\nMarkets traded: {len(trades)}")
        print(f"Total UP shares: {total_up}")
        print(f"Total DOWN shares: {total_down}")
        print(f"Total pairs: {total_pairs}")
        print(f"Total cost: ${total_cost:.2f}")

        print(f"\nPnL: ${total_pnl:.2f}")
        print(f"PnL/hour: ${total_pnl/69:.2f}")
        print(f"Win rate: {len(winners)}/{len(trades)} ({len(winners)/len(trades)*100:.1f}%)")

        # Imbalance analysis
        imbalances = [(t.up_shares - t.down_shares) / max(t.up_shares + t.down_shares, 1) for t in trades]
        print(f"\nAvg imbalance at resolution: {np.mean(imbalances):.2f}")
        print(f"Max imbalance: {max(imbalances):.2f}")
        print(f"Fully balanced (|imb|<0.1): {sum(1 for i in imbalances if abs(i) < 0.1)}/{len(trades)}")

        # Best and worst trades
        print(f"\nTop 3 winners:")
        for t in sorted(trades, key=lambda x: x.pnl, reverse=True)[:3]:
            print(f"  {t.slug}: ${t.pnl:.2f} (UP={t.up_shares}, DOWN={t.down_shares})")

        print(f"\nTop 3 losers:")
        for t in sorted(trades, key=lambda x: x.pnl)[:3]:
            print(f"  {t.slug}: ${t.pnl:.2f} (UP={t.up_shares}, DOWN={t.down_shares})")

    # Summary
    print("\n" + "=" * 60)
    print("INSIGHT")
    print("=" * 60)
    print("""
The challenge: staying balanced as a maker.

- Maker fills depend on price dips
- One side may dip more than the other
- Imbalance = unhedged exposure

Solutions:
1. Accept some imbalance (directional exposure)
2. Use taker to hedge imbalance (costs 2% fee)
3. Only trade when both sides are liquid
""")


if __name__ == "__main__":
    main()
