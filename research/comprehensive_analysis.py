#!/usr/bin/env python3
"""
Comprehensive Strategy Analysis - Jan 16, 2026

Analyzes all observer data to determine:
1. Strategy profitability (passive hedge, stop-loss hedge, unhedged)
2. Comparison with backtest predictions
3. Separate analysis for the 12hr AWS run
"""

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime
from collections import defaultdict

# Strategy Parameters (must match live config)
SHARES = 15
WINNER_OFFSET = +0.01
LOSER_OFFSET = -0.12
MIN_TIME = 120
MIN_VELOCITY = 0.50
STOP_LOSS_PCT = 0.07
MIN_MARKET_RUNTIME = 420  # 7 minutes minimum for valid market

def load_all_observer_data():
    """Load and deduplicate all observer data."""
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')

    # Primary files to analyze
    files_to_load = [
        'spread_capture_obs_20260113.csv',
        'spread_capture_obs_20260114.csv',
        'spread_capture_obs_20260115_aws_12hr.csv',
        'spread_capture_obs_20260116.csv',
    ]

    all_markets = {}
    file_stats = []

    for filename in files_to_load:
        filepath = observer_dir / filename
        if not filepath.exists():
            continue

        try:
            df = pd.read_csv(filepath, on_bad_lines='skip')
            file_stats.append({
                'file': filename,
                'rows': len(df),
                'markets': df['market_slug'].nunique()
            })

            for slug in df['market_slug'].unique():
                mdf = df[df['market_slug'] == slug].copy()
                mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)

                if len(mdf) >= 2:
                    first = mdf.iloc[0]['time_remaining_secs']
                    last = mdf.iloc[-1]['time_remaining_secs']
                    runtime = first - last

                    # Keep market if runtime >= 7 min and good coverage
                    if runtime >= MIN_MARKET_RUNTIME and first >= 600 and last <= 120:
                        if slug not in all_markets or len(mdf) > len(all_markets[slug]['df']):
                            all_markets[slug] = {
                                'df': mdf,
                                'runtime': runtime,
                                'source': filename
                            }
        except Exception as e:
            print(f"Error loading {filename}: {e}")
            continue

    return all_markets, file_stats


def simulate_strategy(mdf, slug):
    """
    Simulate the velocity-gated strategy on a single market.
    Returns list of trade results.
    """
    trades = []
    mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)

    in_trade = False
    entry_price = 0
    entry_side = None
    entry_time = 0
    loser_bid_target = 0

    i = 0
    while i < len(mdf):
        row = mdf.iloc[i]
        time_rem = row['time_remaining_secs']
        vel = row['velocity_bps']

        # Don't enter with < MIN_TIME remaining
        if time_rem < MIN_TIME:
            i += 1
            continue

        # ENTRY: If not in trade and velocity signal
        if not in_trade and abs(vel) >= MIN_VELOCITY:
            entry_side = "UP" if vel > 0 else "DOWN"

            if entry_side == "UP":
                winner_ask = row['up_ask']
                loser_bid = row['down_bid']
            else:
                winner_ask = row['down_ask']
                loser_bid = row['up_bid']

            # Entry at ASK (taker assumption - conservative)
            entry_price = winner_ask
            loser_bid_target = loser_bid + LOSER_OFFSET
            loser_bid_target = max(0.01, min(0.95, loser_bid_target))
            entry_time = time_rem
            in_trade = True
            i += 1
            continue

        # HEDGE CHECK: Look for passive fill or stop-loss
        if in_trade:
            if entry_side == "UP":
                loser_ask = row['down_ask']
                winner_bid = row['up_bid']
            else:
                loser_ask = row['up_ask']
                winner_bid = row['down_bid']

            # Passive hedge fill?
            if loser_ask <= loser_bid_target:
                loser_fill = loser_ask
                pair_cost = entry_price + loser_fill
                pnl = (1.0 - pair_cost) * SHARES

                trades.append({
                    'slug': slug,
                    'type': 'passive',
                    'entry_side': entry_side,
                    'entry_price': entry_price,
                    'hedge_price': loser_fill,
                    'pair_cost': pair_cost,
                    'pnl': pnl,
                    'entry_time': entry_time,
                    'exit_time': time_rem,
                    'velocity_correct': None  # Will determine at resolution
                })
                in_trade = False
                i += 1
                continue

            # Stop-loss trigger?
            drop_pct = (entry_price - winner_bid) / entry_price
            if drop_pct >= STOP_LOSS_PCT:
                loser_fill = loser_ask  # Hit the ask
                pair_cost = entry_price + loser_fill
                pnl = (1.0 - pair_cost) * SHARES

                trades.append({
                    'slug': slug,
                    'type': 'stoploss',
                    'entry_side': entry_side,
                    'entry_price': entry_price,
                    'hedge_price': loser_fill,
                    'pair_cost': pair_cost,
                    'pnl': pnl,
                    'entry_time': entry_time,
                    'exit_time': time_rem,
                    'velocity_correct': None
                })
                in_trade = False
                i += 1
                continue

            # Check if market ending
            if time_rem < 10:
                # Unhedged - determine resolution
                final = mdf.iloc[-1]
                resolution = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'
                velocity_correct = (entry_side == resolution)

                if velocity_correct:
                    pnl = (1.0 - entry_price) * SHARES  # Win full amount
                else:
                    pnl = (0.0 - entry_price) * SHARES  # Lose entry cost

                trades.append({
                    'slug': slug,
                    'type': 'unhedged',
                    'entry_side': entry_side,
                    'entry_price': entry_price,
                    'hedge_price': 0,
                    'pair_cost': entry_price,
                    'pnl': pnl,
                    'entry_time': entry_time,
                    'exit_time': time_rem,
                    'velocity_correct': velocity_correct
                })
                in_trade = False

        i += 1

    # Handle any remaining open trade at end of data
    if in_trade:
        final = mdf.iloc[-1]
        resolution = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'
        velocity_correct = (entry_side == resolution)

        if velocity_correct:
            pnl = (1.0 - entry_price) * SHARES
        else:
            pnl = (0.0 - entry_price) * SHARES

        trades.append({
            'slug': slug,
            'type': 'unhedged',
            'entry_side': entry_side,
            'entry_price': entry_price,
            'hedge_price': 0,
            'pair_cost': entry_price,
            'pnl': pnl,
            'entry_time': entry_time,
            'exit_time': final['time_remaining_secs'],
            'velocity_correct': velocity_correct
        })

    return trades


def simulate_strategy_cycling(mdf, slug):
    """
    Simulate strategy WITH CYCLING (multiple entries per market).
    Re-enters after each hedge completes.
    """
    trades = []
    mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)

    in_trade = False
    entry_price = 0
    entry_side = None
    entry_time = 0
    loser_bid_target = 0

    i = 0
    while i < len(mdf):
        row = mdf.iloc[i]
        time_rem = row['time_remaining_secs']
        vel = row['velocity_bps']

        if time_rem < MIN_TIME:
            i += 1
            continue

        # ENTRY
        if not in_trade and abs(vel) >= MIN_VELOCITY:
            entry_side = "UP" if vel > 0 else "DOWN"

            if entry_side == "UP":
                winner_ask = row['up_ask']
                loser_bid = row['down_bid']
            else:
                winner_ask = row['down_ask']
                loser_bid = row['up_bid']

            entry_price = winner_ask
            loser_bid_target = loser_bid + LOSER_OFFSET
            loser_bid_target = max(0.01, min(0.95, loser_bid_target))
            entry_time = time_rem
            in_trade = True
            i += 1
            continue

        # HEDGE CHECK
        if in_trade:
            if entry_side == "UP":
                loser_ask = row['down_ask']
                winner_bid = row['up_bid']
            else:
                loser_ask = row['up_ask']
                winner_bid = row['down_bid']

            # Passive fill
            if loser_ask <= loser_bid_target:
                loser_fill = loser_ask
                pair_cost = entry_price + loser_fill
                pnl = (1.0 - pair_cost) * SHARES

                trades.append({
                    'slug': slug,
                    'type': 'passive',
                    'entry_side': entry_side,
                    'entry_price': entry_price,
                    'hedge_price': loser_fill,
                    'pair_cost': pair_cost,
                    'pnl': pnl,
                    'entry_time': entry_time,
                    'exit_time': time_rem,
                    'velocity_correct': None
                })
                in_trade = False  # Ready for next cycle
                i += 1
                continue

            # Stop-loss
            drop_pct = (entry_price - winner_bid) / entry_price
            if drop_pct >= STOP_LOSS_PCT:
                loser_fill = loser_ask
                pair_cost = entry_price + loser_fill
                pnl = (1.0 - pair_cost) * SHARES

                trades.append({
                    'slug': slug,
                    'type': 'stoploss',
                    'entry_side': entry_side,
                    'entry_price': entry_price,
                    'hedge_price': loser_fill,
                    'pair_cost': pair_cost,
                    'pnl': pnl,
                    'entry_time': entry_time,
                    'exit_time': time_rem,
                    'velocity_correct': None
                })
                in_trade = False  # Ready for next cycle
                i += 1
                continue

            # Market ending - unhedged
            if time_rem < 10:
                final = mdf.iloc[-1]
                resolution = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'
                velocity_correct = (entry_side == resolution)

                if velocity_correct:
                    pnl = (1.0 - entry_price) * SHARES
                else:
                    pnl = (0.0 - entry_price) * SHARES

                trades.append({
                    'slug': slug,
                    'type': 'unhedged',
                    'entry_side': entry_side,
                    'entry_price': entry_price,
                    'hedge_price': 0,
                    'pair_cost': entry_price,
                    'pnl': pnl,
                    'entry_time': entry_time,
                    'exit_time': time_rem,
                    'velocity_correct': velocity_correct
                })
                in_trade = False

        i += 1

    # Handle remaining open trade
    if in_trade and len(mdf) > 0:
        final = mdf.iloc[-1]
        resolution = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'
        velocity_correct = (entry_side == resolution)

        if velocity_correct:
            pnl = (1.0 - entry_price) * SHARES
        else:
            pnl = (0.0 - entry_price) * SHARES

        trades.append({
            'slug': slug,
            'type': 'unhedged',
            'entry_side': entry_side,
            'entry_price': entry_price,
            'hedge_price': 0,
            'pair_cost': entry_price,
            'pnl': pnl,
            'entry_time': entry_time,
            'exit_time': final['time_remaining_secs'],
            'velocity_correct': velocity_correct
        })

    return trades


def analyze_trades(trades, label=""):
    """Analyze trade results and print summary."""
    if not trades:
        print(f"\n{label}: No trades found")
        return {}

    df = pd.DataFrame(trades)

    passive = df[df['type'] == 'passive']
    stoploss = df[df['type'] == 'stoploss']
    unhedged = df[df['type'] == 'unhedged']

    results = {
        'total_trades': len(df),
        'total_pnl': df['pnl'].sum(),
        'passive_count': len(passive),
        'passive_pnl': passive['pnl'].sum() if len(passive) > 0 else 0,
        'passive_avg': passive['pnl'].mean() if len(passive) > 0 else 0,
        'stoploss_count': len(stoploss),
        'stoploss_pnl': stoploss['pnl'].sum() if len(stoploss) > 0 else 0,
        'stoploss_avg': stoploss['pnl'].mean() if len(stoploss) > 0 else 0,
        'unhedged_count': len(unhedged),
        'unhedged_pnl': unhedged['pnl'].sum() if len(unhedged) > 0 else 0,
        'unhedged_correct': unhedged['velocity_correct'].sum() if len(unhedged) > 0 else 0,
    }

    print(f"\n{'='*70}")
    print(f"{label}")
    print(f"{'='*70}")

    print(f"\nTrade Breakdown:")
    print(f"{'Type':<15} {'Count':>8} {'%':>8} {'Total PnL':>12} {'Avg PnL':>10}")
    print("-" * 55)

    total = len(df)
    for ttype, tdf in [('Passive', passive), ('Stop-loss', stoploss), ('Unhedged', unhedged)]:
        cnt = len(tdf)
        pct = (cnt / total * 100) if total > 0 else 0
        total_pnl = tdf['pnl'].sum() if cnt > 0 else 0
        avg_pnl = tdf['pnl'].mean() if cnt > 0 else 0
        print(f"{ttype:<15} {cnt:>8} {pct:>7.1f}% ${total_pnl:>10.2f} ${avg_pnl:>8.2f}")

    print("-" * 55)
    print(f"{'TOTAL':<15} {total:>8} {'100.0%':>8} ${df['pnl'].sum():>10.2f} ${df['pnl'].mean():>8.2f}")

    if len(unhedged) > 0:
        correct = unhedged['velocity_correct'].sum()
        pct_correct = (correct / len(unhedged) * 100) if len(unhedged) > 0 else 0
        print(f"\nUnhedged Velocity Accuracy: {correct}/{len(unhedged)} ({pct_correct:.1f}%)")

    # Pair cost analysis
    hedged = df[df['type'].isin(['passive', 'stoploss'])]
    if len(hedged) > 0:
        print(f"\nPair Cost Analysis (hedged trades):")
        print(f"  Passive avg pair cost: ${passive['pair_cost'].mean():.3f}" if len(passive) > 0 else "")
        print(f"  Stop-loss avg pair cost: ${stoploss['pair_cost'].mean():.3f}" if len(stoploss) > 0 else "")

    return results


def main():
    print("="*70)
    print("COMPREHENSIVE STRATEGY ANALYSIS")
    print("Jan 16, 2026")
    print("="*70)

    print("\nStrategy Parameters:")
    print(f"  SHARES: {SHARES}")
    print(f"  MIN_VELOCITY: {MIN_VELOCITY} bps")
    print(f"  WINNER_OFFSET: +${WINNER_OFFSET}")
    print(f"  LOSER_OFFSET: ${LOSER_OFFSET}")
    print(f"  STOP_LOSS: {STOP_LOSS_PCT*100:.0f}%")
    print(f"  MIN_TIME: {MIN_TIME}s")
    print(f"  MIN_MARKET_RUNTIME: {MIN_MARKET_RUNTIME}s (7 min)")

    # Load all data
    print("\nLoading observer data...")
    all_markets, file_stats = load_all_observer_data()

    print(f"\nFiles loaded:")
    for fs in file_stats:
        print(f"  {fs['file']}: {fs['rows']:,} rows, {fs['markets']} markets")

    print(f"\nTotal unique markets (deduplicated): {len(all_markets)}")

    # Calculate total observation time
    total_runtime = sum(m['runtime'] for m in all_markets.values())
    total_hours = total_runtime / 3600
    print(f"Total observation time: {total_hours:.2f} hours")

    # =========================================================================
    # ANALYSIS 1: ALL DATA - NO CYCLING
    # =========================================================================
    all_trades_no_cycling = []
    for slug, data in all_markets.items():
        trades = simulate_strategy(data['df'], slug)
        all_trades_no_cycling.extend(trades)

    results_no_cycling = analyze_trades(all_trades_no_cycling, "ALL DATA - NO CYCLING (1 entry per market)")
    if results_no_cycling:
        hourly = results_no_cycling['total_pnl'] / total_hours
        print(f"\nHourly Rate: ${hourly:.2f}/hr")

    # =========================================================================
    # ANALYSIS 2: ALL DATA - WITH CYCLING
    # =========================================================================
    all_trades_cycling = []
    for slug, data in all_markets.items():
        trades = simulate_strategy_cycling(data['df'], slug)
        all_trades_cycling.extend(trades)

    results_cycling = analyze_trades(all_trades_cycling, "ALL DATA - WITH CYCLING (multiple entries)")
    if results_cycling:
        hourly = results_cycling['total_pnl'] / total_hours
        print(f"\nHourly Rate: ${hourly:.2f}/hr")
        cycles_per_market = results_cycling['total_trades'] / len(all_markets)
        print(f"Average cycles per market: {cycles_per_market:.2f}")

    # =========================================================================
    # ANALYSIS 3: 12HR AWS RUN ONLY (Jan 15)
    # =========================================================================
    print("\n" + "="*70)
    print("12HR AWS RUN ANALYSIS (Jan 15)")
    print("="*70)

    aws_markets = {k: v for k, v in all_markets.items()
                   if '20260115_aws' in v['source'] or '20260115.csv' in v['source']}

    if aws_markets:
        aws_runtime = sum(m['runtime'] for m in aws_markets.values())
        aws_hours = aws_runtime / 3600
        print(f"\nAWS 12hr Run Markets: {len(aws_markets)}")
        print(f"Total observation time: {aws_hours:.2f} hours")

        # No cycling
        aws_trades_no_cycling = []
        for slug, data in aws_markets.items():
            trades = simulate_strategy(data['df'], slug)
            aws_trades_no_cycling.extend(trades)

        aws_results_no = analyze_trades(aws_trades_no_cycling, "AWS 12HR - NO CYCLING")
        if aws_results_no:
            hourly = aws_results_no['total_pnl'] / aws_hours
            print(f"\nHourly Rate: ${hourly:.2f}/hr")

        # With cycling
        aws_trades_cycling = []
        for slug, data in aws_markets.items():
            trades = simulate_strategy_cycling(data['df'], slug)
            aws_trades_cycling.extend(trades)

        aws_results_cycling = analyze_trades(aws_trades_cycling, "AWS 12HR - WITH CYCLING")
        if aws_results_cycling:
            hourly = aws_results_cycling['total_pnl'] / aws_hours
            print(f"\nHourly Rate: ${hourly:.2f}/hr")
    else:
        print("No AWS 12hr data found")

    # =========================================================================
    # COMPARISON WITH BACKTEST PREDICTIONS
    # =========================================================================
    print("\n" + "="*70)
    print("COMPARISON WITH BACKTEST PREDICTIONS")
    print("="*70)

    print("""
Backtest Predictions (from plan file):
┌─────────────────┬─────────┬─────────┬───────────┐
│ Type            │ Count   │    %    │ Avg PnL   │
├─────────────────┼─────────┼─────────┼───────────┤
│ Passive hedge   │ 202     │   28%   │ +$1.36    │
│ Stop-loss hedge │ 327     │   46%   │ -$0.80    │
│ Unhedged        │ 188     │   26%   │ +$1.18    │
├─────────────────┼─────────┼─────────┼───────────┤
│ TOTAL           │ 717     │  100%   │ +$0.33    │
└─────────────────┴─────────┴─────────┴───────────┘
Predicted hourly: $12.35/hr
""")

    if results_cycling:
        print("Actual Results (Cycling):")
        total = results_cycling['total_trades']
        print(f"┌─────────────────┬─────────┬─────────┬───────────┐")
        print(f"│ Type            │ Count   │    %    │ Avg PnL   │")
        print(f"├─────────────────┼─────────┼─────────┼───────────┤")

        p_cnt = results_cycling['passive_count']
        p_pct = (p_cnt/total*100) if total else 0
        p_avg = results_cycling['passive_avg']
        print(f"│ Passive hedge   │ {p_cnt:<7} │ {p_pct:>5.1f}%  │ ${p_avg:>+7.2f}  │")

        s_cnt = results_cycling['stoploss_count']
        s_pct = (s_cnt/total*100) if total else 0
        s_avg = results_cycling['stoploss_avg']
        print(f"│ Stop-loss hedge │ {s_cnt:<7} │ {s_pct:>5.1f}%  │ ${s_avg:>+7.2f}  │")

        u_cnt = results_cycling['unhedged_count']
        u_pct = (u_cnt/total*100) if total else 0
        u_avg = results_cycling['unhedged_pnl'] / u_cnt if u_cnt else 0
        print(f"│ Unhedged        │ {u_cnt:<7} │ {u_pct:>5.1f}%  │ ${u_avg:>+7.2f}  │")

        print(f"├─────────────────┼─────────┼─────────┼───────────┤")
        t_avg = results_cycling['total_pnl'] / total if total else 0
        print(f"│ TOTAL           │ {total:<7} │ 100.0%  │ ${t_avg:>+7.2f}  │")
        print(f"└─────────────────┴─────────┴─────────┴───────────┘")

        actual_hourly = results_cycling['total_pnl'] / total_hours
        print(f"\nActual hourly: ${actual_hourly:.2f}/hr")
        print(f"Predicted hourly: $12.35/hr")
        diff = actual_hourly - 12.35
        print(f"Difference: ${diff:+.2f}/hr ({diff/12.35*100:+.1f}%)")


if __name__ == "__main__":
    main()
