#!/usr/bin/env python3
"""
Verify 5% vs 7% Stop-Loss - NO CYCLING

Clean comparison with:
- Cycling OFF (1 entry per market max)
- Merging OFF (just track if hedge fills)
- Zone 5-6 only (vel >= 0.50 bps)
- Same parameters as observer

This isolates the stop-loss impact without cycling complexity.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional

# Match observer parameters exactly
SHARES = 15
WINNER_OFFSET = +0.01
LOSER_OFFSET = -0.12
MIN_TIME = 120
MIN_VELOCITY = 0.50


@dataclass
class TradeResult:
    """Single trade result (no cycling)."""
    market_slug: str
    entry_time_remaining: float
    entry_velocity: float
    winner_side: str
    winner_fill_price: float  # Always at ASK (taker)
    hedge_type: str  # "passive", "stoploss", or "none"
    hedge_fill_price: float
    pair_cost: float
    pnl: float
    samples_to_hedge: int


def simulate_single_entry(mdf, slug: str, stop_loss_pct: float) -> Optional[TradeResult]:
    """
    Simulate ONE entry per market (no cycling).

    Entry: When Zone 5-6 signal, fill at ASK (taker/aggressive)
    Hedge: Wait for passive fill OR stop-loss trigger
    """
    # Sort by time remaining descending (oldest first)
    mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)

    # Scan for first Zone 5-6 entry opportunity
    for i in range(len(mdf)):
        row = mdf.iloc[i]
        time_rem = row['time_remaining_secs']
        vel = row['velocity_bps']

        if time_rem < MIN_TIME:
            return None  # No entry opportunity found

        if abs(vel) >= MIN_VELOCITY:
            # Entry signal found!
            entry_velocity = vel
            entry_time_remaining = time_rem
            winner_side = "UP" if vel > 0 else "DOWN"

            # Winner fills at ASK (aggressive/taker)
            if winner_side == "UP":
                winner_fill_price = row['up_ask']
                loser_bid_target = row['down_bid'] + LOSER_OFFSET
            else:
                winner_fill_price = row['down_ask']
                loser_bid_target = row['up_bid'] + LOSER_OFFSET

            loser_bid_target = max(0.01, min(0.95, loser_bid_target))

            # Scan forward for hedge fill
            for j in range(i + 1, len(mdf)):
                check_row = mdf.iloc[j]
                check_time = check_row['time_remaining_secs']

                if check_time < 60:  # Buffer before market end
                    break

                if winner_side == "UP":
                    loser_ask = check_row['down_ask']
                    winner_bid_now = check_row['up_bid']
                else:
                    loser_ask = check_row['up_ask']
                    winner_bid_now = check_row['down_bid']

                # Check passive fill first
                if loser_ask <= loser_bid_target:
                    pair_cost = winner_fill_price + loser_bid_target
                    pnl = (1.0 - pair_cost) * SHARES
                    return TradeResult(
                        market_slug=slug,
                        entry_time_remaining=entry_time_remaining,
                        entry_velocity=entry_velocity,
                        winner_side=winner_side,
                        winner_fill_price=winner_fill_price,
                        hedge_type="passive",
                        hedge_fill_price=loser_bid_target,
                        pair_cost=pair_cost,
                        pnl=pnl,
                        samples_to_hedge=j - i,
                    )

                # Check stop-loss
                drop_pct = (winner_fill_price - winner_bid_now) / winner_fill_price
                if drop_pct >= stop_loss_pct:
                    pair_cost = winner_fill_price + loser_ask
                    pnl = (1.0 - pair_cost) * SHARES
                    return TradeResult(
                        market_slug=slug,
                        entry_time_remaining=entry_time_remaining,
                        entry_velocity=entry_velocity,
                        winner_side=winner_side,
                        winner_fill_price=winner_fill_price,
                        hedge_type="stoploss",
                        hedge_fill_price=loser_ask,
                        pair_cost=pair_cost,
                        pnl=pnl,
                        samples_to_hedge=j - i,
                    )

            # No hedge fill - unhedged position
            # For this analysis, we'll skip unhedged (they resolve at market end)
            return None

    return None


def main():
    print("=" * 80)
    print("VERIFY 5% vs 7% STOP-LOSS - NO CYCLING")
    print("=" * 80)
    print("\nThis is a CLEAN comparison with cycling OFF.")
    print("Only 1 entry per market, isolates stop-loss impact.")

    # Load data
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    print(f"\nLoading data from {len(csv_files)} files...")

    # Deduplicate markets across files
    all_markets = {}
    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath, on_bad_lines='skip')
            if df.empty:
                continue

            for slug in df['market_slug'].unique():
                mdf = df[df['market_slug'] == slug]
                if len(mdf) >= 2:
                    first = mdf.iloc[0]['time_remaining_secs']
                    last = mdf.iloc[-1]['time_remaining_secs']
                    # Only complete markets
                    if first >= 800 and last <= 60:
                        # Use the most complete version of each market
                        if slug not in all_markets or len(mdf) > len(all_markets[slug]):
                            all_markets[slug] = mdf.copy()
        except Exception:
            continue

    market_data = list(all_markets.items())
    print(f"Unique complete markets: {len(market_data)}")
    total_hours = len(market_data) * 15 / 60

    # Test 5% vs 7% stop-loss
    stop_loss_options = [0.05, 0.07]

    results = {}

    for sl_pct in stop_loss_options:
        trades = []
        for slug, mdf in market_data:
            result = simulate_single_entry(mdf, slug, sl_pct)
            if result:
                trades.append(result)

        passive = [t for t in trades if t.hedge_type == 'passive']
        stoploss = [t for t in trades if t.hedge_type == 'stoploss']

        total_pnl = sum(t.pnl for t in trades)
        passive_pnl = sum(t.pnl for t in passive)
        stoploss_pnl = sum(t.pnl for t in stoploss)

        results[sl_pct] = {
            'total_trades': len(trades),
            'passive_count': len(passive),
            'stoploss_count': len(stoploss),
            'passive_pct': len(passive) / len(trades) * 100 if trades else 0,
            'stoploss_pct': len(stoploss) / len(trades) * 100 if trades else 0,
            'total_pnl': total_pnl,
            'passive_pnl': passive_pnl,
            'stoploss_pnl': stoploss_pnl,
            'hourly': total_pnl / total_hours if total_hours > 0 else 0,
            'avg_passive_cost': np.mean([t.pair_cost for t in passive]) if passive else 0,
            'avg_stoploss_cost': np.mean([t.pair_cost for t in stoploss]) if stoploss else 0,
        }

    # Print comparison
    print(f"\n{'='*80}")
    print("5% vs 7% STOP-LOSS COMPARISON (NO CYCLING)")
    print("=" * 80)

    print(f"\n{'Metric':<30} {'5% SL':>15} {'7% SL':>15} {'Diff':>15}")
    print("─" * 75)

    r5 = results[0.05]
    r7 = results[0.07]

    print(f"{'Total trades':<30} {r5['total_trades']:>15} {r7['total_trades']:>15} {r5['total_trades']-r7['total_trades']:>+15}")
    print(f"{'Passive hedges':<30} {r5['passive_count']:>15} {r7['passive_count']:>15} {r5['passive_count']-r7['passive_count']:>+15}")
    print(f"{'Stop-loss hedges':<30} {r5['stoploss_count']:>15} {r7['stoploss_count']:>15} {r5['stoploss_count']-r7['stoploss_count']:>+15}")
    print(f"{'Passive %':<30} {r5['passive_pct']:>14.0f}% {r7['passive_pct']:>14.0f}% {r5['passive_pct']-r7['passive_pct']:>+14.0f}%")
    print(f"{'Stop-loss %':<30} {r5['stoploss_pct']:>14.0f}% {r7['stoploss_pct']:>14.0f}% {r5['stoploss_pct']-r7['stoploss_pct']:>+14.0f}%")
    print()
    print(f"{'Passive PnL':<30} ${r5['passive_pnl']:>13.2f} ${r7['passive_pnl']:>13.2f} ${r5['passive_pnl']-r7['passive_pnl']:>+13.2f}")
    print(f"{'Stop-loss PnL':<30} ${r5['stoploss_pnl']:>13.2f} ${r7['stoploss_pnl']:>13.2f} ${r5['stoploss_pnl']-r7['stoploss_pnl']:>+13.2f}")
    print(f"{'TOTAL PnL':<30} ${r5['total_pnl']:>13.2f} ${r7['total_pnl']:>13.2f} ${r5['total_pnl']-r7['total_pnl']:>+13.2f}")
    print()
    print(f"{'Hourly rate':<30} ${r5['hourly']:>13.2f} ${r7['hourly']:>13.2f} ${r5['hourly']-r7['hourly']:>+13.2f}")
    print(f"{'Avg passive pair cost':<30} ${r5['avg_passive_cost']:>13.4f} ${r7['avg_passive_cost']:>13.4f} ${r5['avg_passive_cost']-r7['avg_passive_cost']:>+13.4f}")
    print(f"{'Avg stop-loss pair cost':<30} ${r5['avg_stoploss_cost']:>13.4f} ${r7['avg_stoploss_cost']:>13.4f} ${r5['avg_stoploss_cost']-r7['avg_stoploss_cost']:>+13.4f}")

    # Verdict
    print(f"\n{'='*80}")
    print("VERDICT")
    print("=" * 80)

    if r5['total_pnl'] > r7['total_pnl']:
        winner = "5%"
        improvement = r5['total_pnl'] - r7['total_pnl']
        hourly_diff = r5['hourly'] - r7['hourly']
    else:
        winner = "7%"
        improvement = r7['total_pnl'] - r5['total_pnl']
        hourly_diff = r7['hourly'] - r5['hourly']

    print(f"\n  Winner: {winner} STOP-LOSS")
    print(f"  PnL advantage: ${improvement:.2f}")
    print(f"  Hourly advantage: ${abs(hourly_diff):.2f}/hr")

    if r7['total_pnl'] > r5['total_pnl']:
        print(f"\n  ✅ 7% STOP-LOSS CONFIRMED AS OPTIMAL")
        print(f"     Keep current configuration.")
    else:
        print(f"\n  ⚠️ 5% STOP-LOSS APPEARS BETTER")
        print(f"     Consider updating configuration.")

    # Detailed breakdown
    print(f"\n{'='*80}")
    print("WHY THIS RESULT?")
    print("=" * 80)

    print(f"""
  5% Stop-Loss:
    - Triggers earlier (more often)
    - Lower stop-loss pair cost: ${r5['avg_stoploss_cost']:.4f}
    - But more trades end in stop-loss: {r5['stoploss_pct']:.0f}%
    - Fewer passive hedges: {r5['passive_count']}

  7% Stop-Loss:
    - Triggers later (less often)
    - Higher stop-loss pair cost: ${r7['avg_stoploss_cost']:.4f}
    - Fewer trades end in stop-loss: {r7['stoploss_pct']:.0f}%
    - More passive hedges: {r7['passive_count']}

  The trade-off:
    - 5% SL: More hedges complete, but more are expensive stop-losses
    - 7% SL: Fewer stop-losses trigger, more get cheap passive fills
""")


if __name__ == "__main__":
    main()
