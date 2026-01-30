#!/usr/bin/env python3
"""
Full Strategy Optimization

Find optimal loser offset and stop-loss threshold for:
- Aggressive winner (+0.01)
- Variable loser offset
- Variable stop-loss threshold
"""

import pandas as pd
import numpy as np
from pathlib import Path
from itertools import product

SHARES = 15  # Target: 15 shares/side (scale to 30 after live validation)
WINNER_OFFSET = +0.01
MIN_TIME = 120


def simulate_market(mdf, winner_offset, loser_offset, stop_loss_pct):
    """Simulate single market."""
    entry_row = None
    entry_idx = None
    for i, (idx, row) in enumerate(mdf.iterrows()):
        if row['time_remaining_secs'] >= MIN_TIME:
            entry_idx = i
            entry_row = row
            break

    if entry_row is None:
        return None

    velocity = entry_row['velocity_bps']
    predicted_winner = "UP" if velocity > 0 else "DOWN"

    if predicted_winner == "UP":
        winner_bid = round(entry_row['up_bid'] + winner_offset, 2)
        loser_bid = round(entry_row['down_bid'] + loser_offset, 2)
        winner_entry_ask = entry_row['up_ask']
    else:
        winner_bid = round(entry_row['down_bid'] + winner_offset, 2)
        loser_bid = round(entry_row['up_bid'] + loser_offset, 2)
        winner_entry_ask = entry_row['down_ask']

    winner_bid = max(0.01, min(0.95, winner_bid))
    loser_bid = max(0.01, min(0.95, loser_bid))

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

        if not winner_filled:
            if winner_bid >= winner_ask:
                winner_filled = True
                winner_fill_price = winner_ask
            elif winner_ask <= winner_bid:
                winner_filled = True
                winner_fill_price = winner_bid

        if not loser_filled and loser_ask <= loser_bid:
            loser_filled = True
            loser_fill_price = loser_bid
            loser_fill_type = "passive"

        if winner_filled and not loser_filled:
            drop_pct = (winner_fill_price - winner_bid_book) / winner_fill_price if winner_fill_price > 0 else 0
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

    # PnL
    if winner_filled and loser_filled:
        pair_cost = winner_fill_price + loser_fill_price
        pnl = (1.0 - pair_cost) * SHARES
        return {'type': loser_fill_type, 'pnl': pnl, 'pair_cost': pair_cost}
    elif winner_filled:
        if predicted_winner == resolution:
            pnl = (1.0 - winner_fill_price) * SHARES
        else:
            pnl = (0.0 - winner_fill_price) * SHARES
        return {'type': 'unhedged', 'pnl': pnl, 'pair_cost': 0}
    return None


def run_optimization():
    """Run full optimization."""
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    # Load all data
    all_data = []
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
                        all_data.append(mdf.copy())
        except:
            continue

    print(f"Total complete markets: {len(all_data)}")

    # Test parameter combinations
    loser_offsets = [-0.04, -0.06, -0.08, -0.10, -0.12]
    stop_loss_pcts = [0.10, 0.15, 0.20, 0.25, 0.30]

    print(f"\n{'L_Off':>6} {'SL%':>5} {'Pass':>6} {'SL':>6} {'Unh':>5} {'P_PnL':>8} {'SL_PnL':>8} {'Total':>8}")
    print("-" * 70)

    best_pnl = float('-inf')
    best_config = None

    for l_off, sl_pct in product(loser_offsets, stop_loss_pcts):
        passive_pnl = 0
        stoploss_pnl = 0
        unhedged_pnl = 0
        passive_count = 0
        stoploss_count = 0
        unhedged_count = 0

        for mdf in all_data:
            result = simulate_market(mdf, WINNER_OFFSET, l_off, sl_pct)
            if result:
                if result['type'] == 'passive':
                    passive_pnl += result['pnl']
                    passive_count += 1
                elif result['type'] == 'stoploss':
                    stoploss_pnl += result['pnl']
                    stoploss_count += 1
                elif result['type'] == 'unhedged':
                    unhedged_pnl += result['pnl']
                    unhedged_count += 1

        total_pnl = passive_pnl + stoploss_pnl + unhedged_pnl

        print(f"{l_off:>+6.2f} {sl_pct*100:>4.0f}% {passive_count:>6} {stoploss_count:>6} {unhedged_count:>5} "
              f"${passive_pnl:>6.2f} ${stoploss_pnl:>6.2f} ${total_pnl:>6.2f}")

        if total_pnl > best_pnl:
            best_pnl = total_pnl
            best_config = {
                'loser_offset': l_off,
                'stop_loss_pct': sl_pct,
                'passive_count': passive_count,
                'stoploss_count': stoploss_count,
                'unhedged_count': unhedged_count,
                'passive_pnl': passive_pnl,
                'stoploss_pnl': stoploss_pnl,
                'total_pnl': total_pnl,
            }

    print(f"\n{'='*70}")
    print(f"BEST CONFIG: Loser {best_config['loser_offset']:+.2f}, Stop-Loss {best_config['stop_loss_pct']*100:.0f}%")
    print(f"  Passive: {best_config['passive_count']} trades, ${best_config['passive_pnl']:.2f}")
    print(f"  Stop-loss: {best_config['stoploss_count']} trades, ${best_config['stoploss_pnl']:.2f}")
    print(f"  Unhedged: {best_config['unhedged_count']} trades")
    print(f"  TOTAL: ${best_config['total_pnl']:.2f}")

    hours = len(all_data) * 15 / 60
    print(f"  Hourly: ${best_config['total_pnl']/hours:.2f}/hr")


if __name__ == "__main__":
    run_optimization()
