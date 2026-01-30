#!/usr/bin/env python3
"""
Trade Frequency Analysis

Shows how often we trade with the Zone 5-6 + 7% stop-loss strategy.

Metrics:
1. Trades per hour (actual vs theoretical)
2. Time spent in Zone 5-6 (tradeable zone) vs waiting
3. Average gap between Zone 5-6 signals
4. Trade density per market
5. Projected hourly/daily earnings at 15 shares and 30 shares
"""

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

# Strategy configuration (Zone 5-6 + 7% stop-loss)
SHARES_15 = 15   # Current target
SHARES_30 = 30   # After live validation
WINNER_OFFSET = +0.01
LOSER_OFFSET = -0.12
MIN_TIME = 120
MIN_VELOCITY = 0.50  # Zone 5-6 threshold
STOP_LOSS_PCT = 0.07


def analyze_zone_frequency(df):
    """Analyze how often velocity enters Zone 5-6."""

    total_samples = len(df)
    if total_samples == 0:
        return None

    # Count samples in each velocity zone
    zone_56_samples = len(df[abs(df['velocity_bps']) >= MIN_VELOCITY])
    zone_46_samples = len(df[abs(df['velocity_bps']) >= 0.30])

    # Time analysis (assuming ~200ms between samples)
    sample_interval_ms = 200
    total_time_hours = (total_samples * sample_interval_ms) / 1000 / 3600
    zone_56_time_hours = (zone_56_samples * sample_interval_ms) / 1000 / 3600

    return {
        'total_samples': total_samples,
        'total_time_hours': total_time_hours,
        'zone_56_samples': zone_56_samples,
        'zone_56_pct': zone_56_samples / total_samples * 100 if total_samples > 0 else 0,
        'zone_46_samples': zone_46_samples,
        'zone_46_pct': zone_46_samples / total_samples * 100 if total_samples > 0 else 0,
        'zone_56_time_hours': zone_56_time_hours,
    }


def simulate_market_with_stoploss(mdf, shares, stop_loss_pct=0.07):
    """Simulate single market with Zone 5-6 + stop-loss."""

    # Find entry meeting velocity threshold
    entry_row = None
    entry_idx = None
    entry_time_remaining = None

    for i, (idx, row) in enumerate(mdf.iterrows()):
        if row['time_remaining_secs'] >= MIN_TIME:
            if abs(row['velocity_bps']) >= MIN_VELOCITY:
                entry_idx = i
                entry_row = row
                entry_time_remaining = row['time_remaining_secs']
                break

    if entry_row is None:
        return None

    velocity = entry_row['velocity_bps']
    predicted_winner = "UP" if velocity > 0 else "DOWN"

    # Calculate bids
    if predicted_winner == "UP":
        winner_bid = round(entry_row['up_bid'] + WINNER_OFFSET, 2)
        loser_bid = round(entry_row['down_bid'] + LOSER_OFFSET, 2)
    else:
        winner_bid = round(entry_row['down_bid'] + WINNER_OFFSET, 2)
        loser_bid = round(entry_row['up_bid'] + LOSER_OFFSET, 2)

    winner_bid = max(0.01, min(0.95, winner_bid))
    loser_bid = max(0.01, min(0.95, loser_bid))

    # Simulate fills
    post_entry = mdf.iloc[entry_idx:]

    winner_filled = False
    winner_fill_price = 0
    loser_filled = False
    loser_fill_price = 0
    loser_fill_type = "none"

    for i, (idx, row) in enumerate(post_entry.iterrows()):
        if predicted_winner == "UP":
            winner_ask = row['up_ask']
            winner_bid_book = row['up_bid']
            loser_ask = row['down_ask']
        else:
            winner_ask = row['down_ask']
            winner_bid_book = row['down_bid']
            loser_ask = row['up_ask']

        # Winner fill
        if not winner_filled:
            if winner_bid >= winner_ask:
                winner_filled = True
                winner_fill_price = winner_ask
            elif winner_ask <= winner_bid:
                winner_filled = True
                winner_fill_price = winner_bid

        # Passive loser fill
        if not loser_filled and loser_ask <= loser_bid:
            loser_filled = True
            loser_fill_price = loser_bid
            loser_fill_type = "passive"

        # Stop-loss check
        if winner_filled and not loser_filled:
            if winner_fill_price > 0:
                drop_pct = (winner_fill_price - winner_bid_book) / winner_fill_price
                if drop_pct >= stop_loss_pct:
                    loser_filled = True
                    loser_fill_price = loser_ask
                    loser_fill_type = "stoploss"

    # Resolution
    final = mdf.iloc[-1]
    if final['up_bid'] >= 0.90:
        resolution = 'UP'
    elif final['down_bid'] >= 0.90:
        resolution = 'DOWN'
    else:
        resolution = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'

    velocity_correct = (predicted_winner == resolution)

    # PnL
    if winner_filled and loser_filled:
        pair_cost = winner_fill_price + loser_fill_price
        pnl = (1.0 - pair_cost) * shares
        return {
            'type': loser_fill_type,
            'pnl': pnl,
            'pair_cost': pair_cost,
            'velocity': velocity,
            'velocity_correct': velocity_correct,
            'winner_fill': winner_fill_price,
            'loser_fill': loser_fill_price,
            'entry_time_remaining': entry_time_remaining,
        }
    elif winner_filled:
        if predicted_winner == resolution:
            pnl = (1.0 - winner_fill_price) * shares
        else:
            pnl = (0.0 - winner_fill_price) * shares
        return {
            'type': 'unhedged',
            'pnl': pnl,
            'pair_cost': 0,
            'velocity': velocity,
            'velocity_correct': velocity_correct,
            'winner_fill': winner_fill_price,
            'loser_fill': 0,
            'entry_time_remaining': entry_time_remaining,
        }

    return None


def main():
    print("=" * 80)
    print("TRADE FREQUENCY ANALYSIS")
    print("Zone 5-6 + 7% Stop-Loss Strategy")
    print("=" * 80)

    # Load ALL observer data
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    print(f"\nLoading data from {len(csv_files)} files...")

    all_dfs = []
    market_data = []  # List of (market_df, slug)

    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath, on_bad_lines='skip')
            if df.empty:
                continue
            all_dfs.append(df)

            # Get complete markets
            markets = df['market_slug'].unique()
            for slug in markets:
                mdf = df[df['market_slug'] == slug]
                if len(mdf) >= 2:
                    first = mdf.iloc[0]['time_remaining_secs']
                    last = mdf.iloc[-1]['time_remaining_secs']
                    if first >= 800 and last <= 60:
                        market_data.append((mdf.copy(), slug))
        except Exception as e:
            print(f"Error loading {filepath}: {e}")
            continue

    if not all_dfs:
        print("No data found!")
        return

    # Combine all data for zone frequency analysis
    combined_df = pd.concat(all_dfs, ignore_index=True)

    print(f"\n{'='*80}")
    print("1. ZONE FREQUENCY ANALYSIS (How often is Zone 5-6 active?)")
    print("=" * 80)

    zone_stats = analyze_zone_frequency(combined_df)
    if zone_stats:
        print(f"\n  Total samples: {zone_stats['total_samples']:,}")
        print(f"  Total observation time: {zone_stats['total_time_hours']:.2f} hours")
        print(f"\n  Zone 5-6 (vel >= 0.50 bps):")
        print(f"    Samples: {zone_stats['zone_56_samples']:,} ({zone_stats['zone_56_pct']:.1f}%)")
        print(f"    Time: {zone_stats['zone_56_time_hours']:.2f} hours")
        print(f"\n  Zone 4-6 (vel >= 0.30 bps):")
        print(f"    Samples: {zone_stats['zone_46_samples']:,} ({zone_stats['zone_46_pct']:.1f}%)")

    print(f"\n{'='*80}")
    print("2. TRADE EXECUTION ANALYSIS (How many trades do we make?)")
    print("=" * 80)

    print(f"\n  Complete markets available: {len(market_data)}")

    # Simulate trades at 15 shares
    results_15 = []
    results_30 = []

    for mdf, slug in market_data:
        r15 = simulate_market_with_stoploss(mdf, SHARES_15, STOP_LOSS_PCT)
        r30 = simulate_market_with_stoploss(mdf, SHARES_30, STOP_LOSS_PCT)
        if r15:
            r15['slug'] = slug
            results_15.append(r15)
        if r30:
            r30['slug'] = slug
            results_30.append(r30)

    # Calculate frequency metrics
    total_markets = len(market_data)
    markets_with_entry = len(results_15)
    entry_rate = markets_with_entry / total_markets * 100 if total_markets > 0 else 0

    # Estimate observation time (each market is ~15 min)
    total_market_hours = total_markets * 15 / 60

    print(f"\n  Markets observed: {total_markets}")
    print(f"  Markets with Zone 5-6 entry: {markets_with_entry} ({entry_rate:.1f}%)")
    print(f"  Markets without entry: {total_markets - markets_with_entry}")

    if results_15:
        passive = [r for r in results_15 if r['type'] == 'passive']
        stoploss = [r for r in results_15 if r['type'] == 'stoploss']
        unhedged = [r for r in results_15 if r['type'] == 'unhedged']

        print(f"\n  Trade breakdown:")
        print(f"    Passive hedges: {len(passive)} ({len(passive)/len(results_15)*100:.0f}%)")
        print(f"    Stop-loss hedges: {len(stoploss)} ({len(stoploss)/len(results_15)*100:.0f}%)")
        print(f"    Unhedged: {len(unhedged)} ({len(unhedged)/len(results_15)*100:.0f}%)")

    print(f"\n{'='*80}")
    print("3. TRADING FREQUENCY (Trades per hour)")
    print("=" * 80)

    trades_per_hour = markets_with_entry / total_market_hours if total_market_hours > 0 else 0
    markets_per_hour = total_markets / total_market_hours if total_market_hours > 0 else 0

    print(f"\n  Observation time: {total_market_hours:.1f} hours")
    print(f"  Markets per hour: {markets_per_hour:.1f}")
    print(f"  Trades per hour: {trades_per_hour:.2f}")
    print(f"  Trades per day (24hr): {trades_per_hour * 24:.0f}")
    print(f"\n  Average wait between trades: {60/trades_per_hour:.0f} min" if trades_per_hour > 0 else "")

    print(f"\n{'='*80}")
    print("4. PROFIT PROJECTION")
    print("=" * 80)

    for shares, results in [(SHARES_15, results_15), (SHARES_30, results_30)]:
        if not results:
            continue

        total_pnl = sum(r['pnl'] for r in results)
        hourly = total_pnl / total_market_hours if total_market_hours > 0 else 0
        daily = hourly * 24
        monthly = daily * 30

        passive_pnl = sum(r['pnl'] for r in results if r['type'] == 'passive')
        stoploss_pnl = sum(r['pnl'] for r in results if r['type'] == 'stoploss')
        unhedged_pnl = sum(r['pnl'] for r in results if r['type'] == 'unhedged')

        print(f"\n  At {shares} shares/side:")
        print(f"    Total PnL: ${total_pnl:.2f}")
        print(f"      - Passive: ${passive_pnl:.2f}")
        print(f"      - Stop-loss: ${stoploss_pnl:.2f}")
        print(f"      - Unhedged: ${unhedged_pnl:.2f}")
        print(f"\n    Hourly: ${hourly:.2f}/hr")
        print(f"    Daily (24hr): ${daily:.0f}/day")
        print(f"    Monthly (30d): ${monthly:.0f}/month")

        if results:
            avg_pnl_per_trade = total_pnl / len(results)
            print(f"\n    Avg PnL per trade: ${avg_pnl_per_trade:.2f}")

            win_trades = [r for r in results if r['pnl'] > 0]
            lose_trades = [r for r in results if r['pnl'] <= 0]
            win_rate = len(win_trades) / len(results) * 100 if results else 0
            print(f"    Win rate: {win_rate:.0f}% ({len(win_trades)}/{len(results)})")

    print(f"\n{'='*80}")
    print("5. ENTRY TIMING ANALYSIS")
    print("=" * 80)

    if results_15:
        entry_times = [r['entry_time_remaining'] for r in results_15 if r.get('entry_time_remaining')]
        if entry_times:
            avg_entry_time = np.mean(entry_times)
            min_entry_time = min(entry_times)
            max_entry_time = max(entry_times)

            print(f"\n  Time remaining at entry:")
            print(f"    Average: {avg_entry_time:.0f}s ({avg_entry_time/60:.1f} min)")
            print(f"    Earliest: {max_entry_time:.0f}s ({max_entry_time/60:.1f} min into market)")
            print(f"    Latest: {min_entry_time:.0f}s ({min_entry_time/60:.1f} min left)")

    print(f"\n{'='*80}")
    print("SUMMARY")
    print("=" * 80)

    print(f"""
  Configuration:
    - Zone 5-6 (velocity >= {MIN_VELOCITY} bps)
    - 7% stop-loss trigger
    - Loser offset: {LOSER_OFFSET}
    - Winner offset: +{WINNER_OFFSET}

  Current (15 shares): ${sum(r['pnl'] for r in results_15):.2f} total, ${sum(r['pnl'] for r in results_15)/total_market_hours:.2f}/hr
  Target (30 shares):  ${sum(r['pnl'] for r in results_30):.2f} total, ${sum(r['pnl'] for r in results_30)/total_market_hours:.2f}/hr

  Trade frequency: {trades_per_hour:.2f} trades/hour
  Zone 5-6 active: {zone_stats['zone_56_pct']:.1f}% of time
""")


if __name__ == "__main__":
    main()
