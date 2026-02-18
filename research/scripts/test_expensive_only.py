#!/usr/bin/env python3
"""
Test: Only accumulate expensive side as makers. No hedge.

Since expensive wins 98%+ at $0.80, why hedge?
Just bet expensive wins and collect maker fills.
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
    shares: int
    cost: float
    avg_price: float
    resolution: str
    expensive_side: str
    pnl: float
    won: bool


def simulate_expensive_only(obs_df: pd.DataFrame, offset: float, min_exp_ask: float) -> List[Trade]:
    """Only accumulate expensive side as maker."""

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

        # Determine expensive side (use mode over market lifetime)
        mdf['expensive_side'] = np.where(mdf['up_ask'] > mdf['down_ask'], 'UP', 'DOWN')
        expensive_side = mdf['expensive_side'].mode().iloc[0] if len(mdf['expensive_side'].mode()) > 0 else 'UP'

        # State
        shares = 0
        cost = 0.0
        pending = None
        last_order_ts = 0
        cooldown_ms = 5000

        for _, row in mdf.iterrows():
            ts = row['timestamp_ms']
            up_ask = row['up_ask']
            down_ask = row['down_ask']
            time_rem = row.get('time_remaining_secs', 450)

            if pd.isna(up_ask) or pd.isna(down_ask):
                continue

            # Current expensive side and ask
            if up_ask > down_ask:
                curr_exp_side = 'UP'
                curr_exp_ask = up_ask
            else:
                curr_exp_side = 'DOWN'
                curr_exp_ask = down_ask

            # Only trade if expensive_ask >= threshold
            if curr_exp_ask < min_exp_ask:
                continue

            # Only trade expensive side
            if curr_exp_side != expensive_side:
                continue

            # Don't trade last 60s
            if time_rem < 60:
                continue

            # Check for fill
            if expensive_side == 'UP':
                ask = up_ask
            else:
                ask = down_ask

            if pending and ask <= pending[0]:
                shares += 1
                cost += pending[0]
                pending = None

            # Place new order
            if pending is None and (ts - last_order_ts) >= cooldown_ms:
                pending = (ask - offset, ts)
                last_order_ts = ts

        # Results
        if shares > 0:
            avg_price = cost / shares
            won = (expensive_side == resolution)
            if won:
                pnl = shares * 1.0 - cost
            else:
                pnl = -cost  # Lose everything

            trades.append(Trade(
                slug=slug,
                shares=shares,
                cost=cost,
                avg_price=avg_price,
                resolution=resolution,
                expensive_side=expensive_side,
                pnl=pnl,
                won=won,
            ))

    return trades


def main():
    print("=" * 60)
    print("EXPENSIVE SIDE ONLY - NO HEDGE")
    print("=" * 60)

    obs_path = Path("research/observer/PROTECTED_grid_obs_is_oos2_combined.csv")
    print("\nLoading...")
    obs_df = pd.read_csv(obs_path, low_memory=False)
    print(f"  Obs: {len(obs_df):,}")

    # Test configs
    configs = [
        (0.01, 0.80),
        (0.01, 0.85),
        (0.01, 0.90),
        (0.02, 0.80),
        (0.02, 0.85),
        (0.02, 0.90),
        (0.03, 0.80),
        (0.03, 0.85),
        (0.03, 0.90),
    ]

    results = []
    for offset, min_exp in configs:
        trades = simulate_expensive_only(obs_df, offset, min_exp)

        if not trades:
            continue

        total_pnl = sum(t.pnl for t in trades)
        total_shares = sum(t.shares for t in trades)
        win_rate = sum(1 for t in trades if t.won) / len(trades) * 100
        avg_price = sum(t.cost for t in trades) / total_shares if total_shares > 0 else 0

        results.append({
            'offset': offset,
            'min_exp': min_exp,
            'markets': len(trades),
            'shares': total_shares,
            'avg_price': avg_price,
            'win_rate': win_rate,
            'pnl': total_pnl,
            'pnl_hr': total_pnl / 69,
        })

    # Summary table
    print(f"\n{'Offset':<8} {'Min Exp':<10} {'Markets':<10} {'Shares':<10} {'Avg Price':<12} {'Win Rate':<10} {'PnL':<10} {'PnL/hr':<10}")
    print("-" * 90)

    for r in sorted(results, key=lambda x: x['pnl'], reverse=True):
        print(f"{r['offset']*100:.0f}c{'':<5} ${r['min_exp']:<8} {r['markets']:<10} {r['shares']:<10} "
              f"${r['avg_price']:<11.3f} {r['win_rate']:>5.1f}%    ${r['pnl']:<10.2f} ${r['pnl_hr']:<10.2f}")

    # Best config
    best = max(results, key=lambda x: x['pnl'])
    print(f"\n✅ Best: {best['offset']*100:.0f}c offset, min_exp=${best['min_exp']}")
    print(f"   Shares: {best['shares']}, Avg price: ${best['avg_price']:.3f}")
    print(f"   Win rate: {best['win_rate']:.1f}%")
    print(f"   PnL: ${best['pnl']:.2f} (${best['pnl_hr']:.2f}/hr)")

    # Expected value analysis
    print("\n" + "=" * 60)
    print("EXPECTED VALUE ANALYSIS")
    print("=" * 60)

    for r in results[:3]:  # Top 3
        # EV = P(win) * profit_if_win + P(lose) * loss_if_lose
        # profit_if_win = 1 - avg_price
        # loss_if_lose = avg_price
        p_win = r['win_rate'] / 100
        profit = 1 - r['avg_price']
        loss = r['avg_price']
        ev_per_share = p_win * profit - (1 - p_win) * loss

        print(f"\n{r['offset']*100:.0f}c offset, min=${r['min_exp']}:")
        print(f"  P(win) = {p_win:.1%}")
        print(f"  Profit if win = ${profit:.3f}")
        print(f"  Loss if lose = ${loss:.3f}")
        print(f"  EV per share = ${ev_per_share:.4f}")
        print(f"  EV per share (expected) = {p_win:.1%} × ${profit:.3f} - {1-p_win:.1%} × ${loss:.3f} = ${ev_per_share:.4f}")


if __name__ == "__main__":
    main()
