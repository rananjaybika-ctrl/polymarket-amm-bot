#!/usr/bin/env python3
"""
Cycling Backtest - Multiple Entries Per Market

Simulates realistic cycling:
1. Entry when Zone 5-6 signal
2. Wait for hedge (passive or stop-loss)
3. Merge pair → lock profit
4. Re-enter if new Zone 5-6 signal available
5. Repeat until market ends

Key constraints:
- MIN_TIME = 120s (no entry with <2min left)
- Zone 5-6 (vel >= 0.50 bps) required
- After merge, need Zone 5-6 signal to re-enter
- Tracks each cycle separately
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from collections import defaultdict

# Strategy config (Zone 5-6 + 7% stop-loss)
SHARES = 15
WINNER_OFFSET = +0.01
LOSER_OFFSET = -0.12
MIN_TIME = 120
MIN_VELOCITY = 0.50
STOP_LOSS_PCT = 0.07

# Cycling constraints
MIN_CYCLE_GAP_SAMPLES = 5  # ~1 second between cycles (at 200ms sample rate)


@dataclass
class CycleResult:
    """Result of a single entry→hedge→merge cycle."""
    cycle_num: int
    entry_time_remaining: float
    entry_velocity: float
    winner_side: str
    winner_fill_price: float
    loser_fill_price: float
    hedge_type: str  # "passive" or "stoploss"
    pair_cost: float
    pnl: float
    velocity_correct: bool
    samples_to_hedge: int  # How many samples until hedge filled


@dataclass
class MarketResult:
    """Result from cycling through one market."""
    slug: str
    total_samples: int
    total_cycles: int
    cycles: List[CycleResult]
    total_pnl: float
    resolution: str


def simulate_market_with_cycling(mdf, slug) -> Optional[MarketResult]:
    """
    Simulate market with cycling enabled.

    Process:
    1. Scan for Zone 5-6 entry
    2. Simulate fill (winner fills quickly, loser via passive or stop-loss)
    3. On both filled → merge pair → record profit
    4. Continue scanning for next Zone 5-6 entry
    5. Repeat until market ends
    """

    # Sort by time remaining (descending - oldest first)
    mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)

    total_samples = len(mdf)
    cycles = []
    cycle_num = 0

    # State machine
    i = 0  # Current sample index
    in_trade = False
    winner_side = None
    winner_fill_price = 0.0
    winner_fill_idx = 0
    loser_bid = 0.0
    entry_velocity = 0.0
    entry_time_remaining = 0.0

    while i < len(mdf):
        row = mdf.iloc[i]
        time_rem = row['time_remaining_secs']
        vel = row['velocity_bps']

        # Skip if too close to end
        if time_rem < MIN_TIME:
            break

        if not in_trade:
            # Looking for Zone 5-6 entry
            if abs(vel) >= MIN_VELOCITY:
                # Entry signal!
                in_trade = True
                cycle_num += 1
                entry_velocity = vel
                entry_time_remaining = time_rem
                winner_side = "UP" if vel > 0 else "DOWN"
                loser_side = "DOWN" if winner_side == "UP" else "UP"

                # Calculate bids
                if winner_side == "UP":
                    winner_bid = round(row['up_bid'] + WINNER_OFFSET, 2)
                    loser_bid = round(row['down_bid'] + LOSER_OFFSET, 2)
                else:
                    winner_bid = round(row['down_bid'] + WINNER_OFFSET, 2)
                    loser_bid = round(row['up_bid'] + LOSER_OFFSET, 2)

                winner_bid = max(0.01, min(0.95, winner_bid))
                loser_bid = max(0.01, min(0.95, loser_bid))

                # Winner fills immediately (aggressive offset)
                if winner_side == "UP":
                    winner_ask = row['up_ask']
                else:
                    winner_ask = row['down_ask']

                if winner_bid >= winner_ask:
                    winner_fill_price = winner_ask
                else:
                    winner_fill_price = winner_bid  # Assume fills at our bid

                winner_fill_idx = i

                # Now scan forward for loser fill
                loser_filled = False
                loser_fill_price = 0.0
                hedge_type = "none"

                for j in range(i + 1, len(mdf)):
                    check_row = mdf.iloc[j]
                    check_time = check_row['time_remaining_secs']

                    if check_time < MIN_TIME - 60:  # Give some buffer
                        break

                    if winner_side == "UP":
                        loser_ask = check_row['down_ask']
                        winner_bid_book = check_row['up_bid']
                    else:
                        loser_ask = check_row['up_ask']
                        winner_bid_book = check_row['down_bid']

                    # Check passive fill
                    if loser_ask <= loser_bid:
                        loser_filled = True
                        loser_fill_price = loser_bid
                        hedge_type = "passive"
                        samples_to_hedge = j - i
                        i = j + MIN_CYCLE_GAP_SAMPLES  # Move past this cycle
                        break

                    # Check stop-loss
                    if winner_fill_price > 0:
                        drop_pct = (winner_fill_price - winner_bid_book) / winner_fill_price
                        if drop_pct >= STOP_LOSS_PCT:
                            loser_filled = True
                            loser_fill_price = loser_ask
                            hedge_type = "stoploss"
                            samples_to_hedge = j - i
                            i = j + MIN_CYCLE_GAP_SAMPLES
                            break

                if loser_filled:
                    # Complete cycle - calculate PnL
                    pair_cost = winner_fill_price + loser_fill_price
                    pnl = (1.0 - pair_cost) * SHARES

                    # Check resolution for velocity correctness
                    final = mdf.iloc[-1]
                    if final['up_bid'] >= 0.90:
                        resolution = 'UP'
                    elif final['down_bid'] >= 0.90:
                        resolution = 'DOWN'
                    else:
                        resolution = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'

                    velocity_correct = (winner_side == resolution)

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
                        velocity_correct=velocity_correct,
                        samples_to_hedge=samples_to_hedge,
                    ))

                    in_trade = False
                else:
                    # No hedge fill - unhedged position (skip for now, could add later)
                    in_trade = False
                    i += 1
                    continue
        else:
            i += 1
            continue

        i += 1

    if not cycles:
        return None

    # Determine resolution
    final = mdf.iloc[-1]
    if final['up_bid'] >= 0.90:
        resolution = 'UP'
    elif final['down_bid'] >= 0.90:
        resolution = 'DOWN'
    else:
        resolution = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'

    total_pnl = sum(c.pnl for c in cycles)

    return MarketResult(
        slug=slug,
        total_samples=total_samples,
        total_cycles=len(cycles),
        cycles=cycles,
        total_pnl=total_pnl,
        resolution=resolution,
    )


def main():
    print("=" * 80)
    print("CYCLING BACKTEST - Multiple Entries Per Market")
    print("=" * 80)
    print(f"\nConfig: Zone 5-6 (vel >= {MIN_VELOCITY}) + {STOP_LOSS_PCT*100:.0f}% stop-loss")
    print(f"Shares: {SHARES} per side")

    # Load ALL observer data
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
        except Exception as e:
            continue

    print(f"Complete markets: {len(market_data)}")

    # Run cycling simulation
    print(f"\n{'='*80}")
    print("CYCLING SIMULATION RESULTS")
    print("=" * 80)

    all_results = []
    for mdf, slug in market_data:
        result = simulate_market_with_cycling(mdf, slug)
        if result:
            all_results.append(result)

    # Aggregate statistics
    total_cycles = sum(r.total_cycles for r in all_results)
    total_pnl = sum(r.total_pnl for r in all_results)
    markets_with_trades = len(all_results)

    all_cycles = []
    for r in all_results:
        all_cycles.extend(r.cycles)

    print(f"\n  Markets analyzed: {len(market_data)}")
    print(f"  Markets with trades: {markets_with_trades}")
    print(f"  Total cycles: {total_cycles}")
    print(f"  Avg cycles/market: {total_cycles/markets_with_trades:.2f}" if markets_with_trades > 0 else "")

    # Breakdown by type
    passive_cycles = [c for c in all_cycles if c.hedge_type == 'passive']
    stoploss_cycles = [c for c in all_cycles if c.hedge_type == 'stoploss']

    print(f"\n  Cycle breakdown:")
    print(f"    Passive hedges: {len(passive_cycles)} ({len(passive_cycles)/total_cycles*100:.0f}%)")
    print(f"    Stop-loss hedges: {len(stoploss_cycles)} ({len(stoploss_cycles)/total_cycles*100:.0f}%)")

    # PnL breakdown
    passive_pnl = sum(c.pnl for c in passive_cycles)
    stoploss_pnl = sum(c.pnl for c in stoploss_cycles)

    print(f"\n  PnL breakdown:")
    print(f"    Passive: ${passive_pnl:.2f} (avg ${passive_pnl/len(passive_cycles):.2f}/trade)" if passive_cycles else "")
    print(f"    Stop-loss: ${stoploss_pnl:.2f} (avg ${stoploss_pnl/len(stoploss_cycles):.2f}/trade)" if stoploss_cycles else "")
    print(f"    TOTAL: ${total_pnl:.2f}")

    # Hourly rate (each market is ~15 min)
    total_hours = len(market_data) * 15 / 60
    hourly = total_pnl / total_hours if total_hours > 0 else 0

    print(f"\n  Performance:")
    print(f"    Total hours: {total_hours:.1f}")
    print(f"    Hourly rate: ${hourly:.2f}/hr")
    print(f"    Daily (24hr): ${hourly*24:.0f}/day")
    print(f"    Monthly (30d): ${hourly*24*30:.0f}/month")

    # Win rate
    winners = [c for c in all_cycles if c.pnl > 0]
    win_rate = len(winners) / len(all_cycles) * 100 if all_cycles else 0
    print(f"\n    Win rate: {win_rate:.0f}% ({len(winners)}/{len(all_cycles)})")

    # Pair cost analysis
    passive_costs = [c.pair_cost for c in passive_cycles]
    stoploss_costs = [c.pair_cost for c in stoploss_cycles]

    print(f"\n  Pair cost analysis:")
    print(f"    Passive avg: ${np.mean(passive_costs):.4f}" if passive_costs else "")
    print(f"    Stop-loss avg: ${np.mean(stoploss_costs):.4f}" if stoploss_costs else "")

    # Samples to hedge (timing analysis)
    passive_samples = [c.samples_to_hedge for c in passive_cycles]
    stoploss_samples = [c.samples_to_hedge for c in stoploss_cycles]

    print(f"\n  Time to hedge (samples, ~200ms each):")
    print(f"    Passive avg: {np.mean(passive_samples):.0f} samples ({np.mean(passive_samples)*0.2:.1f}s)" if passive_samples else "")
    print(f"    Stop-loss avg: {np.mean(stoploss_samples):.0f} samples ({np.mean(stoploss_samples)*0.2:.1f}s)" if stoploss_samples else "")

    # Compare with no-cycling baseline
    print(f"\n{'='*80}")
    print("COMPARISON: CYCLING vs NO-CYCLING")
    print("=" * 80)

    # No cycling = max 1 trade per market
    nocycle_cycles = []
    for r in all_results:
        if r.cycles:
            nocycle_cycles.append(r.cycles[0])  # Only first cycle

    nocycle_pnl = sum(c.pnl for c in nocycle_cycles)
    nocycle_hourly = nocycle_pnl / total_hours if total_hours > 0 else 0

    print(f"\n  No Cycling (1 entry/market):")
    print(f"    Cycles: {len(nocycle_cycles)}")
    print(f"    PnL: ${nocycle_pnl:.2f}")
    print(f"    Hourly: ${nocycle_hourly:.2f}/hr")

    print(f"\n  With Cycling (multiple entries/market):")
    print(f"    Cycles: {total_cycles}")
    print(f"    PnL: ${total_pnl:.2f}")
    print(f"    Hourly: ${hourly:.2f}/hr")

    improvement = (total_pnl - nocycle_pnl) / nocycle_pnl * 100 if nocycle_pnl > 0 else 0
    cycle_multiplier = total_cycles / len(nocycle_cycles) if nocycle_cycles else 0

    print(f"\n  Improvement:")
    print(f"    Cycle multiplier: {cycle_multiplier:.2f}x")
    print(f"    PnL improvement: {improvement:.0f}%")
    print(f"    Extra PnL: ${total_pnl - nocycle_pnl:.2f}")

    # Distribution of cycles per market
    print(f"\n{'='*80}")
    print("CYCLES PER MARKET DISTRIBUTION")
    print("=" * 80)

    cycle_counts = [r.total_cycles for r in all_results]
    for n in range(1, min(max(cycle_counts) + 1, 11)):
        count = sum(1 for c in cycle_counts if c == n)
        pct = count / len(cycle_counts) * 100 if cycle_counts else 0
        bar = "█" * int(pct / 2)
        print(f"    {n:2} cycles: {count:3} markets ({pct:5.1f}%) {bar}")

    if max(cycle_counts) > 10:
        count_10plus = sum(1 for c in cycle_counts if c > 10)
        pct = count_10plus / len(cycle_counts) * 100
        print(f"    10+ cycles: {count_10plus:3} markets ({pct:5.1f}%)")

    print(f"\n    Avg cycles/market: {np.mean(cycle_counts):.2f}")
    print(f"    Max cycles/market: {max(cycle_counts)}")

    # Top performers
    print(f"\n{'='*80}")
    print("TOP 10 MARKETS BY CYCLES")
    print("=" * 80)

    top_markets = sorted(all_results, key=lambda r: r.total_cycles, reverse=True)[:10]
    for r in top_markets:
        print(f"    {r.slug[:45]:45} {r.total_cycles:2} cycles  ${r.total_pnl:6.2f}")


if __name__ == "__main__":
    main()
