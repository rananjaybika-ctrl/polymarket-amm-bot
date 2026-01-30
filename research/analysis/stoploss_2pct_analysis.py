#!/usr/bin/env python3
"""
Stop-Loss Comparison: 2% vs 5% vs 7% in Zone 5-6

Analyzes how different stop-loss thresholds affect:
1. Hedge frequency (passive vs stop-loss)
2. Pair costs
3. PnL per cycle
4. Total profitability
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional

SHARES = 15
WINNER_OFFSET = +0.01
LOSER_OFFSET = -0.12
MIN_TIME = 120
MIN_VELOCITY = 0.50
MIN_CYCLE_GAP_SAMPLES = 5


@dataclass
class CycleResult:
    cycle_num: int
    entry_time_remaining: float
    entry_velocity: float
    winner_side: str
    winner_fill_price: float
    loser_fill_price: float
    hedge_type: str
    pair_cost: float
    pnl: float
    samples_to_hedge: int


def simulate_with_stoploss(mdf, slug, stop_loss_pct: float) -> List[CycleResult]:
    """Simulate market with specific stop-loss percentage."""
    mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)

    cycles = []
    cycle_num = 0
    i = 0
    in_trade = False

    while i < len(mdf):
        row = mdf.iloc[i]
        time_rem = row['time_remaining_secs']
        vel = row['velocity_bps']

        if time_rem < MIN_TIME:
            break

        if not in_trade:
            if abs(vel) >= MIN_VELOCITY:
                in_trade = True
                cycle_num += 1
                entry_velocity = vel
                entry_time_remaining = time_rem
                winner_side = "UP" if vel > 0 else "DOWN"

                if winner_side == "UP":
                    winner_bid = round(row['up_bid'] + WINNER_OFFSET, 2)
                    loser_bid = round(row['down_bid'] + LOSER_OFFSET, 2)
                    winner_ask = row['up_ask']
                else:
                    winner_bid = round(row['down_bid'] + WINNER_OFFSET, 2)
                    loser_bid = round(row['up_bid'] + LOSER_OFFSET, 2)
                    winner_ask = row['down_ask']

                winner_bid = max(0.01, min(0.95, winner_bid))
                loser_bid = max(0.01, min(0.95, loser_bid))

                if winner_bid >= winner_ask:
                    winner_fill_price = winner_ask
                else:
                    winner_fill_price = winner_bid

                # Scan for hedge
                loser_filled = False
                loser_fill_price = 0.0
                hedge_type = "none"

                for j in range(i + 1, len(mdf)):
                    check_row = mdf.iloc[j]
                    check_time = check_row['time_remaining_secs']

                    if check_time < MIN_TIME - 60:
                        break

                    if winner_side == "UP":
                        loser_ask = check_row['down_ask']
                        winner_bid_book = check_row['up_bid']
                    else:
                        loser_ask = check_row['up_ask']
                        winner_bid_book = check_row['down_bid']

                    # Check passive fill first
                    if loser_ask <= loser_bid:
                        loser_filled = True
                        loser_fill_price = loser_bid
                        hedge_type = "passive"
                        samples_to_hedge = j - i
                        i = j + MIN_CYCLE_GAP_SAMPLES
                        break

                    # Check stop-loss
                    if winner_fill_price > 0:
                        drop_pct = (winner_fill_price - winner_bid_book) / winner_fill_price
                        if drop_pct >= stop_loss_pct:
                            loser_filled = True
                            loser_fill_price = loser_ask
                            hedge_type = "stoploss"
                            samples_to_hedge = j - i
                            i = j + MIN_CYCLE_GAP_SAMPLES
                            break

                if loser_filled:
                    pair_cost = winner_fill_price + loser_fill_price
                    pnl = (1.0 - pair_cost) * SHARES

                    cycles.append(CycleResult(
                        cycle_num=cycle_num,
                        entry_time_remaining=entry_time_remaining,
                        entry_velocity=entry_velocity,
                        winner_side=winner_side,
                        winner_fill_price=winner_fill_price,
                        loser_fill_price=loser_fill_price,
                        hedge_type=hedge_type,
                        pair_cost=pair_cost,
                        pnl=pnl,
                        samples_to_hedge=samples_to_hedge,
                    ))
                    in_trade = False
                else:
                    in_trade = False
                    i += 1
                    continue
        else:
            i += 1
            continue

        i += 1

    return cycles


def main():
    print("=" * 80)
    print("STOP-LOSS COMPARISON: 2% vs 5% vs 7% in Zone 5-6")
    print("=" * 80)

    # Load data
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    print(f"\nLoading data from {len(csv_files)} files...")

    market_data = []
    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath, on_bad_lines='skip')
            if df.empty:
                continue

            markets = df['market_slug'].unique()
            for slug in markets:
                mdf = df[df['market_slug'] == slug]
                if len(mdf) >= 2:
                    first = mdf.iloc[0]['time_remaining_secs']
                    last = mdf.iloc[-1]['time_remaining_secs']
                    if first >= 800 and last <= 60:
                        market_data.append((mdf.copy(), slug))
        except Exception:
            continue

    print(f"Complete markets: {len(market_data)}")
    total_hours = len(market_data) * 15 / 60

    # Test different stop-loss percentages
    stop_loss_options = [0.02, 0.03, 0.05, 0.07, 0.10, 0.15]

    results = {}

    for sl_pct in stop_loss_options:
        all_cycles = []
        for mdf, slug in market_data:
            cycles = simulate_with_stoploss(mdf, slug, sl_pct)
            all_cycles.extend(cycles)

        passive = [c for c in all_cycles if c.hedge_type == 'passive']
        stoploss = [c for c in all_cycles if c.hedge_type == 'stoploss']

        total_pnl = sum(c.pnl for c in all_cycles)
        passive_pnl = sum(c.pnl for c in passive)
        stoploss_pnl = sum(c.pnl for c in stoploss)

        results[sl_pct] = {
            'total_cycles': len(all_cycles),
            'passive_count': len(passive),
            'stoploss_count': len(stoploss),
            'passive_pct': len(passive) / len(all_cycles) * 100 if all_cycles else 0,
            'stoploss_pct': len(stoploss) / len(all_cycles) * 100 if all_cycles else 0,
            'total_pnl': total_pnl,
            'passive_pnl': passive_pnl,
            'stoploss_pnl': stoploss_pnl,
            'hourly': total_pnl / total_hours if total_hours > 0 else 0,
            'avg_passive_cost': np.mean([c.pair_cost for c in passive]) if passive else 0,
            'avg_stoploss_cost': np.mean([c.pair_cost for c in stoploss]) if stoploss else 0,
            'avg_passive_time': np.mean([c.samples_to_hedge for c in passive]) * 0.2 if passive else 0,
            'avg_stoploss_time': np.mean([c.samples_to_hedge for c in stoploss]) * 0.2 if stoploss else 0,
        }

    # Print comparison table
    print(f"\n{'='*80}")
    print("COMPARISON TABLE")
    print("=" * 80)

    print(f"\n{'SL%':>6} │ {'Cycles':>7} │ {'Pass%':>6} │ {'SL%':>6} │ {'Total PnL':>10} │ {'$/hr':>8} │ {'Pass Cost':>9} │ {'SL Cost':>9}")
    print("─" * 85)

    for sl_pct in stop_loss_options:
        r = results[sl_pct]
        print(f"{sl_pct*100:5.0f}% │ {r['total_cycles']:7} │ {r['passive_pct']:5.0f}% │ {r['stoploss_pct']:5.0f}% │ ${r['total_pnl']:8.2f} │ ${r['hourly']:6.2f} │ ${r['avg_passive_cost']:.4f} │ ${r['avg_stoploss_cost']:.4f}")

    # Detailed breakdown for key options
    print(f"\n{'='*80}")
    print("DETAILED BREAKDOWN")
    print("=" * 80)

    for sl_pct in [0.02, 0.05, 0.07]:
        r = results[sl_pct]
        print(f"\n  {sl_pct*100:.0f}% STOP-LOSS:")
        print(f"    Total cycles: {r['total_cycles']}")
        print(f"    Passive hedges: {r['passive_count']} ({r['passive_pct']:.0f}%)")
        print(f"    Stop-loss hedges: {r['stoploss_count']} ({r['stoploss_pct']:.0f}%)")
        print(f"    ")
        print(f"    Passive PnL: ${r['passive_pnl']:.2f} (avg cost: ${r['avg_passive_cost']:.4f})")
        print(f"    Stop-loss PnL: ${r['stoploss_pnl']:.2f} (avg cost: ${r['avg_stoploss_cost']:.4f})")
        print(f"    TOTAL PnL: ${r['total_pnl']:.2f}")
        print(f"    Hourly rate: ${r['hourly']:.2f}/hr")
        print(f"    ")
        print(f"    Time to hedge:")
        print(f"      Passive: {r['avg_passive_time']:.1f}s")
        print(f"      Stop-loss: {r['avg_stoploss_time']:.1f}s")

    # Find optimal
    print(f"\n{'='*80}")
    print("OPTIMAL STOP-LOSS")
    print("=" * 80)

    best_sl = max(results.keys(), key=lambda k: results[k]['total_pnl'])
    best = results[best_sl]

    print(f"\n  Best stop-loss: {best_sl*100:.0f}%")
    print(f"  PnL: ${best['total_pnl']:.2f}")
    print(f"  Hourly: ${best['hourly']:.2f}/hr")

    # Compare 2% vs 7%
    print(f"\n{'='*80}")
    print("2% vs 7% COMPARISON")
    print("=" * 80)

    r2 = results[0.02]
    r7 = results[0.07]

    print(f"\n  {'Metric':<25} {'2% SL':>12} {'7% SL':>12} {'Diff':>12}")
    print("  " + "─" * 55)
    print(f"  {'Total cycles':<25} {r2['total_cycles']:>12} {r7['total_cycles']:>12} {r2['total_cycles']-r7['total_cycles']:>+12}")
    print(f"  {'Passive hedges':<25} {r2['passive_count']:>12} {r7['passive_count']:>12} {r2['passive_count']-r7['passive_count']:>+12}")
    print(f"  {'Stop-loss hedges':<25} {r2['stoploss_count']:>12} {r7['stoploss_count']:>12} {r2['stoploss_count']-r7['stoploss_count']:>+12}")
    print(f"  {'Passive PnL':<25} ${r2['passive_pnl']:>10.2f} ${r7['passive_pnl']:>10.2f} ${r2['passive_pnl']-r7['passive_pnl']:>+10.2f}")
    print(f"  {'Stop-loss PnL':<25} ${r2['stoploss_pnl']:>10.2f} ${r7['stoploss_pnl']:>10.2f} ${r2['stoploss_pnl']-r7['stoploss_pnl']:>+10.2f}")
    print(f"  {'TOTAL PnL':<25} ${r2['total_pnl']:>10.2f} ${r7['total_pnl']:>10.2f} ${r2['total_pnl']-r7['total_pnl']:>+10.2f}")
    print(f"  {'Hourly rate':<25} ${r2['hourly']:>10.2f} ${r7['hourly']:>10.2f} ${r2['hourly']-r7['hourly']:>+10.2f}")
    print(f"  {'Avg SL pair cost':<25} ${r2['avg_stoploss_cost']:>10.4f} ${r7['avg_stoploss_cost']:>10.4f} ${r2['avg_stoploss_cost']-r7['avg_stoploss_cost']:>+10.4f}")

    # Recommendation
    print(f"\n{'='*80}")
    print("RECOMMENDATION")
    print("=" * 80)

    if r2['total_pnl'] > r7['total_pnl']:
        improvement = (r2['total_pnl'] - r7['total_pnl']) / r7['total_pnl'] * 100
        print(f"\n  ✅ 2% STOP-LOSS IS BETTER!")
        print(f"     PnL improvement: +${r2['total_pnl'] - r7['total_pnl']:.2f} (+{improvement:.0f}%)")
        print(f"     Hourly: ${r2['hourly']:.2f}/hr vs ${r7['hourly']:.2f}/hr")
        print(f"\n  Why 2% works better:")
        print(f"     - Triggers stop-loss earlier = lower pair cost")
        print(f"     - More cycles complete (faster hedge)")
        print(f"     - Stop-loss cost: ${r2['avg_stoploss_cost']:.4f} vs ${r7['avg_stoploss_cost']:.4f}")
    else:
        print(f"\n  ✅ 7% STOP-LOSS REMAINS OPTIMAL")
        print(f"     Hourly: ${r7['hourly']:.2f}/hr")


if __name__ == "__main__":
    main()
