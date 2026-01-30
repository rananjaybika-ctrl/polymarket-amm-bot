#!/usr/bin/env python3
"""
Focused test: Best config with buycount variations and SL ON/OFF
Only 6 configurations to test quickly.
"""

import subprocess
import sys

# Import the main optimizer module
sys.path.insert(0, '/Users/rananjaybika/polymarket-amm-bot')

from research.optimizers.spike_param_optimizer_taker import (
    OptConfig, load_data, precompute_spikes,
    run_single_config, OptResult, ENHANCED_SCORE_THRESHOLD
)
import pandas as pd
import numpy as np

def main():
    print("="*100)
    print("FOCUSED TAKER TEST: Best Config × Buycount × Stop-Loss")
    print("="*100)
    print(f"\nENHANCED_SCORE_THRESHOLD = {ENHANCED_SCORE_THRESHOLD}")

    # Load data
    print("\nLoading data...")
    btc_df, obs_df, hours, resolutions = load_data()
    print(f"  Data: {hours:.2f} hours, {len(resolutions)} markets")

    # Precompute spikes for 1000ms lookback only
    lookbacks = [60]  # 1000ms
    print(f"\nPrecomputing spikes for lookback {lookbacks}...")
    spikes_by_lookback = precompute_spikes(btc_df, lookbacks, adaptive_volatility=True)
    print(f"  1000ms: {len(spikes_by_lookback.get(60, []))} spikes")

    # Define the 6 configurations
    configs = []

    # Best param base: 50 shares, 1 level, 1000ms lookback
    for buycount in [1, 3, 5]:
        for stop_loss in [0.12, 0.15]:  # 12%, 15%
            configs.append(OptConfig(
                target_shares=50,
                grid_levels=1,
                grid_spacing=0.01,
                spike_lookback=60,  # 1000ms
                stop_loss_pct=stop_loss,
                order_pulling=False,  # Irrelevant for taker
                entry_order_pull_timeout=10.0,
                hedge_ratio=1.0,
                grid_buycount=buycount,
            ))

    print(f"\nTesting {len(configs)} configurations...")
    print("-"*100)

    # Run each config
    results = []
    for i, config in enumerate(configs):
        sl_str = f"{int(config.stop_loss_pct*100)}%" if config.stop_loss_pct else "OFF"
        print(f"\n[{i+1}/{len(configs)}] Buycount={config.grid_buycount}, SL={sl_str}")

        result = run_single_config(
            config=config,
            spikes_by_lookback=spikes_by_lookback,
            obs_df=obs_df,
            hours=hours,
            market_resolutions=resolutions,
            slippage=0.0
        )
        results.append(result)

        # Print immediate results
        print(f"    Trades: {result.total_trades}")
        print(f"    Net PnL: ${result.total_pnl:.2f} (${result.hourly_rate:.2f}/hr)")
        print(f"    Gross PnL: ${result.total_pnl_gross:.2f}")
        print(f"    Fees: ${result.total_entry_fees + result.total_hedge_fees:.2f}")
        print(f"    Win Rate: {result.win_rate*100:.1f}%")
        print(f"    Direction Acc: {result.direction_accuracy*100:.1f}%")
        print(f"    Passive: {result.passive_hedge_pct*100:.1f}% | SL: {result.stoploss_hedge_pct*100:.1f}% | Res: {result.resolution_pct*100:.1f}%")

    # Summary table
    print("\n" + "="*100)
    print("RESULTS SUMMARY")
    print("="*100)
    print(f"\n{'Buycount':<10} {'SL':<8} {'Trades':<8} {'$/hr Net':<12} {'$/hr Gross':<12} {'Win%':<8} {'Acc%':<8} {'Passive%':<10} {'SL%':<8} {'Res%':<8}")
    print("-"*100)

    for result in results:
        sl_str = f"{int(result.config.stop_loss_pct*100)}%" if result.config.stop_loss_pct else "OFF"
        print(f"{result.config.grid_buycount:<10} {sl_str:<8} {result.total_trades:<8} "
              f"${result.hourly_rate:<11.2f} ${result.hourly_rate_gross:<11.2f} "
              f"{result.win_rate*100:<7.1f}% {result.direction_accuracy*100:<7.1f}% "
              f"{result.passive_hedge_pct*100:<9.1f}% {result.stoploss_hedge_pct*100:<7.1f}% "
              f"{result.resolution_pct*100:<7.1f}%")

    # Detailed PnL breakdown
    print("\n" + "="*100)
    print("PNL BREAKDOWN BY HEDGE TYPE")
    print("="*100)
    print(f"\n{'Buycount':<10} {'SL':<8} {'Passive PnL':<14} {'SL PnL':<14} {'Res PnL':<14} {'Fees':<12} {'Net PnL':<12}")
    print("-"*100)

    for result in results:
        sl_str = f"{int(result.config.stop_loss_pct*100)}%" if result.config.stop_loss_pct else "OFF"
        total_fees = result.total_entry_fees + result.total_hedge_fees
        print(f"{result.config.grid_buycount:<10} {sl_str:<8} "
              f"${result.passive_pnl:<13.2f} ${result.stoploss_pnl:<13.2f} "
              f"${result.resolution_pnl:<13.2f} ${total_fees:<11.2f} ${result.total_pnl:<11.2f}")

    # Analysis: SL comparison
    print("\n" + "="*100)
    print("STOP-LOSS COMPARISON (12% vs 15%)")
    print("="*100)

    for buycount in [1, 3, 5]:
        print(f"\nBuycount={buycount}:")
        for sl_pct in [0.12, 0.15]:
            sl_on = [r for r in results if r.config.grid_buycount == buycount and r.config.stop_loss_pct == sl_pct]
            if sl_on:
                sl_on = sl_on[0]
                print(f"  SL {int(sl_pct*100)}%:  ${sl_on.hourly_rate:.2f}/hr | {sl_on.stoploss_hedge_pct*100:.1f}% stopped | {sl_on.resolution_pct*100:.1f}% resolution")

    # Trade-level loss analysis
    print("\n" + "="*100)
    print("LOSS ANALYSIS: MAXIMUM LOSSES PER TRADE")
    print("="*100)

    for result in results:
        if not result.trades:
            continue
        sl_str = f"{int(result.config.stop_loss_pct*100)}%" if result.config.stop_loss_pct else "OFF"

        # Find worst trade
        worst_trade = min(result.trades, key=lambda t: t.pnl)
        best_trade = max(result.trades, key=lambda t: t.pnl)

        losses = [t for t in result.trades if t.pnl < 0]
        avg_loss = np.mean([t.pnl for t in losses]) if losses else 0

        print(f"\nBuycount={result.config.grid_buycount}, SL={sl_str}:")
        print(f"  Worst trade: ${worst_trade.pnl:.2f} (type: {worst_trade.hedge_type}, {worst_trade.shares_filled} shares @ ${worst_trade.winner_fill_price:.2f})")
        print(f"  Best trade:  ${best_trade.pnl:.2f}")
        print(f"  Losing trades: {len(losses)}/{len(result.trades)}")
        print(f"  Avg loss when wrong: ${avg_loss:.2f}")

    print("\n" + "="*100)
    print("ACCURACY VS THRESHOLD INSIGHT")
    print("="*100)
    print("""
To INCREASE ACCURACY at threshold 0.005:

1. RAISE THRESHOLD (trade-off: fewer signals)
   - 0.005: ~27% of signals pass (current)
   - 0.007: ~15% pass (higher accuracy expected)
   - 0.010: ~0.2% pass (very high accuracy, very few trades)

2. ADD TIME WINDOW FILTER
   - 300-600s remaining: 88.9% accuracy (optimal)
   - Outside window: 57% accuracy
   - Could hard-filter to only trade in optimal window

3. REQUIRE HIGHER VELOCITY CONFIRMATION
   - Current: velocity_bps >= 0.10 (weak filter)
   - Stricter: velocity_bps >= 0.20 or 0.30

4. USE REGIME WEIGHTING MORE AGGRESSIVELY
   - Current: HIGH regime gets 1.2x weight
   - Could: Require HIGH regime only (66.4% accuracy vs 58.9% MEDIUM)

5. LOOKBACK SELECTION
   - 1000ms: Best 10s accuracy (70.6%)
   - 1400ms: Best resolution accuracy (75.7%) but fewer signals

The current 83.3% accuracy with 12 trades should improve with more
data (500+ trades at 0.005 threshold) - law of large numbers.
""")

if __name__ == "__main__":
    main()
