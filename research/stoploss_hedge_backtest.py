#!/usr/bin/env python3
"""
Stop-Loss Hedge Backtest

Strategy:
1. Post passive bids on both sides
2. When winner fills, monitor price
3. If winner price drops X% from fill → IMMEDIATELY hedge by hitting loser ASK
4. This limits loss when velocity is wrong

The idea:
- Without stop-loss: velocity wrong → unhedged winner → lose ~$5
- With stop-loss: velocity wrong → detect drop → hedge at higher cost → lose ~$0.50-1.00
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List

SHARES = 15  # Target: 15 shares/side (scale to 30 after live validation)
WINNER_OFFSET = -0.02  # Passive bid on winner
LOSER_OFFSET = -0.04   # More passive on loser
MIN_TIME = 120

# Stop-loss configuration
STOP_LOSS_PCT = 0.25  # Trigger hedge if winner drops 25% from fill


@dataclass
class TradeResult:
    slug: str
    velocity: float
    predicted_winner: str
    resolution: str

    winner_fill_price: float
    winner_filled: bool

    loser_fill_price: float
    loser_filled: bool
    loser_fill_type: str  # "passive", "stoploss", or "none"

    pair_cost: float
    pnl: float
    outcome_type: str  # "hedged_passive", "hedged_stoploss", "unhedged_winner", "unhedged_loser", "no_fill"


def simulate_market_with_stoploss(mdf: pd.DataFrame, slug: str, stop_loss_pct: float) -> Optional[TradeResult]:
    """Simulate market with stop-loss hedge logic."""

    # Find entry
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
    predicted_loser = "DOWN" if velocity > 0 else "UP"

    # Calculate bid prices
    if predicted_winner == "UP":
        winner_bid = round(entry_row['up_bid'] + WINNER_OFFSET, 2)
        loser_bid = round(entry_row['down_bid'] + LOSER_OFFSET, 2)
        winner_entry_ask = entry_row['up_ask']
        loser_entry_ask = entry_row['down_ask']
    else:
        winner_bid = round(entry_row['down_bid'] + WINNER_OFFSET, 2)
        loser_bid = round(entry_row['up_bid'] + LOSER_OFFSET, 2)
        winner_entry_ask = entry_row['down_ask']
        loser_entry_ask = entry_row['up_ask']

    winner_bid = max(0.01, min(0.95, winner_bid))
    loser_bid = max(0.01, min(0.95, loser_bid))

    # Simulate tick-by-tick
    post_entry = mdf.iloc[entry_idx:]

    winner_filled = False
    winner_fill_price = 0
    winner_fill_idx = None

    loser_filled = False
    loser_fill_price = 0
    loser_fill_type = "none"

    stoploss_triggered = False

    for i, (idx, row) in enumerate(post_entry.iterrows()):
        if predicted_winner == "UP":
            winner_ask = row['up_ask']
            loser_ask = row['down_ask']
        else:
            winner_ask = row['down_ask']
            loser_ask = row['up_ask']

        # Check winner fill (passive)
        if not winner_filled and winner_ask <= winner_bid:
            winner_filled = True
            winner_fill_price = winner_bid  # Fill at our bid
            winner_fill_idx = i

        # Check loser fill (passive)
        if not loser_filled and loser_ask <= loser_bid:
            loser_filled = True
            loser_fill_price = loser_bid
            loser_fill_type = "passive"

        # Stop-loss check: if winner filled but loser hasn't
        if winner_filled and not loser_filled and not stoploss_triggered:
            # Check if winner price dropped X% from fill
            current_winner_value = winner_ask  # Current ask is approximate value

            # Actually, we should look at bid (what we could sell for)
            if predicted_winner == "UP":
                current_winner_bid = row['up_bid']
            else:
                current_winner_bid = row['down_bid']

            drop_pct = (winner_fill_price - current_winner_bid) / winner_fill_price

            if drop_pct >= stop_loss_pct:
                # STOP-LOSS TRIGGERED - hedge immediately by hitting loser ask
                stoploss_triggered = True
                loser_filled = True
                loser_fill_price = loser_ask  # Hit the ask (market order)
                loser_fill_type = "stoploss"

    # Resolution
    final = mdf.iloc[-1]
    if final['up_bid'] >= 0.90:
        resolution = 'UP'
    elif final['down_bid'] >= 0.90:
        resolution = 'DOWN'
    else:
        resolution = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'

    # Calculate PnL
    pnl = 0.0
    outcome_type = "no_fill"
    pair_cost = 0.0

    if winner_filled and loser_filled:
        pair_cost = winner_fill_price + loser_fill_price
        pnl = (1.0 - pair_cost) * SHARES
        outcome_type = f"hedged_{loser_fill_type}"

    elif winner_filled and not loser_filled:
        if predicted_winner == resolution:
            pnl = (1.0 - winner_fill_price) * SHARES
        else:
            pnl = (0.0 - winner_fill_price) * SHARES
        outcome_type = "unhedged_winner"

    elif not winner_filled and loser_filled:
        if predicted_loser == resolution:
            pnl = (1.0 - loser_fill_price) * SHARES
        else:
            pnl = (0.0 - loser_fill_price) * SHARES
        outcome_type = "unhedged_loser"

    return TradeResult(
        slug=slug,
        velocity=velocity,
        predicted_winner=predicted_winner,
        resolution=resolution,
        winner_fill_price=winner_fill_price,
        winner_filled=winner_filled,
        loser_fill_price=loser_fill_price,
        loser_filled=loser_filled,
        loser_fill_type=loser_fill_type,
        pair_cost=pair_cost,
        pnl=pnl,
        outcome_type=outcome_type,
    )


def run_backtest(stop_loss_pct: float):
    """Run backtest with specific stop-loss percentage."""
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    all_results = []

    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath, on_bad_lines='skip')
            if df.empty:
                continue

            markets = df['market_slug'].unique()
            complete = [s for s in markets
                        if len(df[df['market_slug']==s]) >= 2
                        and df[df['market_slug']==s].iloc[0]['time_remaining_secs'] >= 800
                        and df[df['market_slug']==s].iloc[-1]['time_remaining_secs'] <= 60]

            for slug in complete:
                mdf = df[df['market_slug'] == slug].copy()
                result = simulate_market_with_stoploss(mdf, slug, stop_loss_pct)
                if result:
                    all_results.append(result)
        except:
            continue

    return all_results


def main():
    print("="*80)
    print("STOP-LOSS HEDGE BACKTEST")
    print("="*80)
    print(f"\nStrategy: When winner drops {STOP_LOSS_PCT*100:.0f}% from fill, immediately hedge")

    # Test different stop-loss levels
    stop_loss_levels = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 1.00]  # 1.00 = no stop-loss

    print(f"\n{'SL%':>6} {'Hedged':>8} {'SL_Hdg':>8} {'UW':>6} {'UL':>6} {'H_PnL':>9} {'SL_PnL':>9} {'UW_PnL':>9} {'Total':>9}")
    print("-" * 85)

    best_result = None
    best_pnl = float('-inf')

    for sl_pct in stop_loss_levels:
        results = run_backtest(sl_pct)

        hedged_passive = [r for r in results if r.outcome_type == "hedged_passive"]
        hedged_stoploss = [r for r in results if r.outcome_type == "hedged_stoploss"]
        unhedged_winner = [r for r in results if r.outcome_type == "unhedged_winner"]
        unhedged_loser = [r for r in results if r.outcome_type == "unhedged_loser"]

        h_pnl = sum(r.pnl for r in hedged_passive)
        sl_pnl = sum(r.pnl for r in hedged_stoploss)
        uw_pnl = sum(r.pnl for r in unhedged_winner)
        ul_pnl = sum(r.pnl for r in unhedged_loser)
        total_pnl = h_pnl + sl_pnl + uw_pnl + ul_pnl

        sl_label = f"{sl_pct*100:.0f}%" if sl_pct < 1.0 else "OFF"
        print(f"{sl_label:>6} {len(hedged_passive):>8} {len(hedged_stoploss):>8} "
              f"{len(unhedged_winner):>6} {len(unhedged_loser):>6} "
              f"${h_pnl:>7.2f} ${sl_pnl:>7.2f} ${uw_pnl:>7.2f} ${total_pnl:>7.2f}")

        if total_pnl > best_pnl:
            best_pnl = total_pnl
            best_result = {
                'sl_pct': sl_pct,
                'hedged_passive': len(hedged_passive),
                'hedged_stoploss': len(hedged_stoploss),
                'unhedged_winner': len(unhedged_winner),
                'unhedged_loser': len(unhedged_loser),
                'total_pnl': total_pnl,
                'results': results,
            }

    # Detailed analysis of best result
    print(f"\n{'='*85}")
    print(f"BEST: Stop-Loss at {best_result['sl_pct']*100:.0f}%" if best_result['sl_pct'] < 1.0 else "BEST: No Stop-Loss")
    print("="*85)

    results = best_result['results']
    total_markets = len(results)
    hours = total_markets * 15 / 60

    print(f"\n  Total markets: {total_markets}")
    print(f"  Hedged (passive): {best_result['hedged_passive']}")
    print(f"  Hedged (stop-loss): {best_result['hedged_stoploss']}")
    print(f"  Unhedged Winner: {best_result['unhedged_winner']}")
    print(f"  Unhedged Loser: {best_result['unhedged_loser']}")
    print(f"\n  Total PnL: ${best_result['total_pnl']:.2f}")
    print(f"  Hourly: ${best_result['total_pnl']/hours:.2f}/hr")

    # Analyze stop-loss hedge trades
    if best_result['sl_pct'] < 1.0:
        sl_hedged = [r for r in results if r.outcome_type == "hedged_stoploss"]
        if sl_hedged:
            print(f"\n{'='*85}")
            print("STOP-LOSS HEDGE ANALYSIS")
            print("="*85)
            sl_pair_costs = [r.pair_cost for r in sl_hedged]
            sl_pnls = [r.pnl for r in sl_hedged]
            print(f"\n  Stop-loss hedged trades: {len(sl_hedged)}")
            print(f"  Avg pair cost: ${np.mean(sl_pair_costs):.4f}")
            print(f"  Avg PnL per trade: ${np.mean(sl_pnls):.2f}")
            print(f"  Total PnL from stop-loss hedges: ${sum(sl_pnls):.2f}")

            # Show some examples
            print("\n  Examples:")
            for r in sl_hedged[:5]:
                print(f"    {r.slug[:30]}: W@${r.winner_fill_price:.2f} + L@${r.loser_fill_price:.2f} "
                      f"= ${r.pair_cost:.2f} → PnL ${r.pnl:.2f}")

    # Compare with baseline (no stop-loss)
    print(f"\n{'='*85}")
    print("COMPARISON: WITH vs WITHOUT STOP-LOSS")
    print("="*85)

    baseline = run_backtest(1.0)  # No stop-loss
    baseline_unhedged = [r for r in baseline if r.outcome_type == "unhedged_winner"]
    baseline_unhedged_pnl = sum(r.pnl for r in baseline_unhedged)

    best_unhedged = [r for r in results if r.outcome_type == "unhedged_winner"]
    best_unhedged_pnl = sum(r.pnl for r in best_unhedged)
    best_sl_pnl = sum(r.pnl for r in results if r.outcome_type == "hedged_stoploss")

    print(f"\n  WITHOUT stop-loss:")
    print(f"    Unhedged winner trades: {len(baseline_unhedged)}")
    print(f"    Unhedged winner PnL: ${baseline_unhedged_pnl:.2f}")

    print(f"\n  WITH stop-loss at {best_result['sl_pct']*100:.0f}%:")
    print(f"    Remaining unhedged winner: {len(best_unhedged)}")
    print(f"    Remaining unhedged PnL: ${best_unhedged_pnl:.2f}")
    print(f"    Stop-loss hedge PnL: ${best_sl_pnl:.2f}")

    saved = baseline_unhedged_pnl - best_unhedged_pnl - best_sl_pnl
    print(f"\n  Loss reduction: ${-saved:.2f} saved")


if __name__ == "__main__":
    main()
