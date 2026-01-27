#!/usr/bin/env python3
"""
Detailed OOS3 Analysis - Trade-level breakdown

Covers:
1. Unhedged (resolution) trades and their PnL contribution
2. Entry price distribution (% buying above 80c)
3. Stop-loss correctness analysis
4. Full PnL accounting including resolution outcomes
5. Comparison to in-sample characteristics
"""

import sys
from pathlib import Path
from typing import Dict, Optional, List
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.volatility_filter_analysis import (
    load_ou_params, compute_zscore_series, BacktestConfig, run_backtest_with_zscore,
    TradeWithZScore, estimate_active_hours_zone
)
from research.TRADING_CONFIGS import AGGRESSIVE, BALANCED, CONSERVATIVE, ALL_CONFIGS, TradingConfig

BASE_DIR = Path("/Users/rananjaybika/polymarket-amm-bot")
OOS3_BTC_FILE = BASE_DIR / "research/binance_hf/btc_prices_oos3_combined.csv"
OOS3_OBS_FILE = BASE_DIR / "research/observer/grid_obs_oos3_combined.csv"
OOS3_RES_FILE = BASE_DIR / "research/observer/market_resolutions_verified.csv"


def detailed_analysis(trades: List[TradeWithZScore], cfg: TradingConfig, total_hours: float, zscore_df: pd.DataFrame):
    """Full detailed analysis of trades for a config."""
    # Filter to z-zone
    filtered = []
    for t in trades:
        z = t.zscore_at_entry
        if cfg.z_lo is not None and z <= cfg.z_lo:
            continue
        if cfg.z_hi is not None and z >= cfg.z_hi:
            continue
        filtered.append(t)

    if not filtered:
        print(f"  NO TRADES in z-zone!")
        return

    n = len(filtered)
    stop_label = f"180s TIME" if cfg.time_stop_seconds else f"{int(cfg.stop_loss_pct*100)}% PRICE"

    print(f"\n{'#'*100}")
    print(f"# {cfg.name} ({stop_label}) - DETAILED OOS3 ANALYSIS")
    print(f"{'#'*100}")

    # =========================================================================
    # 1. OVERALL PnL ACCOUNTING
    # =========================================================================
    passive = [t for t in filtered if t.hedge_type == "passive"]
    stoploss = [t for t in filtered if t.hedge_type == "stoploss"]
    timestop = [t for t in filtered if t.hedge_type == "timestop"]
    resolution = [t for t in filtered if t.hedge_type == "resolution"]

    total_pnl = sum(t.pnl for t in filtered)
    total_pnl_gross = sum(t.pnl_gross for t in filtered)
    total_fees = sum(t.entry_fee + t.hedge_fee for t in filtered)

    print(f"\n{'='*80}")
    print(f"1. FULL PnL ACCOUNTING")
    print(f"{'='*80}")
    print(f"  Total Trades:      {n}")
    print(f"  Gross PnL:         ${total_pnl_gross:.4f}")
    print(f"  Total Fees:        ${total_fees:.4f}")
    print(f"  Net PnL (@5sh):    ${total_pnl:.4f}")
    print(f"  Net PnL (@50sh):   ${total_pnl*10:.2f}")

    # PnL by exit type
    print(f"\n  PnL BY EXIT TYPE:")
    for label, group in [("Passive", passive), ("Price Stop", stoploss), ("Time Stop", timestop), ("Resolution", resolution)]:
        if not group:
            continue
        g_pnl = sum(t.pnl for t in group)
        g_wins = sum(1 for t in group if t.pnl > 0)
        g_wr = g_wins / len(group) * 100
        print(f"    {label:<12}: {len(group):3} trades | PnL ${g_pnl:+.4f} | WR {g_wr:.1f}% | Avg ${g_pnl/len(group):+.4f}/trade")

    # =========================================================================
    # 2. UNHEDGED / RESOLUTION TRADES
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"2. UNHEDGED (RESOLUTION) TRADES")
    print(f"{'='*80}")

    if resolution:
        res_correct = [t for t in resolution if t.correct_direction]
        res_wrong = [t for t in resolution if not t.correct_direction]
        res_pnl = sum(t.pnl for t in resolution)

        print(f"  Resolution trades: {len(resolution)} ({len(resolution)/n*100:.1f}% of total)")
        print(f"  Correct direction: {len(res_correct)} ({len(res_correct)/len(resolution)*100:.1f}%)")
        print(f"  Wrong direction:   {len(res_wrong)} ({len(res_wrong)/len(resolution)*100:.1f}%)")
        print(f"  Resolution PnL:    ${res_pnl:.4f}")

        if res_correct:
            rc_pnl = sum(t.pnl for t in res_correct)
            print(f"\n  CORRECT direction resolutions (unhedged winners):")
            print(f"    Count: {len(res_correct)}")
            print(f"    PnL:   ${rc_pnl:.4f}")
            print(f"    Avg entry price: ${np.mean([t.winner_fill_price for t in res_correct]):.4f}")
            print(f"    These trades picked the right side and held to resolution = full payout")

        if res_wrong:
            rw_pnl = sum(t.pnl for t in res_wrong)
            print(f"\n  WRONG direction resolutions (unhedged losers):")
            print(f"    Count: {len(res_wrong)}")
            print(f"    PnL:   ${rw_pnl:.4f}")
            print(f"    Avg entry price: ${np.mean([t.winner_fill_price for t in res_wrong]):.4f}")
            print(f"    These trades picked the WRONG side and expired worthless = full loss")
            for t in res_wrong:
                print(f"      {t.market_slug} | entry=${t.winner_fill_price:.3f} | side={t.winner_side} | loss=${t.pnl:.4f}")
    else:
        print(f"  No resolution trades (all exited via passive/stop)")

    # =========================================================================
    # 3. ENTRY PRICE ANALYSIS
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"3. ENTRY PRICE ANALYSIS")
    print(f"{'='*80}")

    entry_prices = [t.winner_fill_price for t in filtered]
    above_80 = [p for p in entry_prices if p > 0.80]
    above_70 = [p for p in entry_prices if p > 0.70]
    above_60 = [p for p in entry_prices if p > 0.60]
    below_50 = [p for p in entry_prices if p <= 0.50]

    print(f"  Entry Price Distribution:")
    print(f"    Mean:   ${np.mean(entry_prices):.4f}")
    print(f"    Median: ${np.median(entry_prices):.4f}")
    print(f"    Min:    ${min(entry_prices):.4f}")
    print(f"    Max:    ${max(entry_prices):.4f}")
    print(f"    Std:    ${np.std(entry_prices):.4f}")

    print(f"\n  Price Buckets:")
    print(f"    > $0.80 (very expensive): {len(above_80):3} ({len(above_80)/n*100:.1f}%)")
    print(f"    > $0.70:                  {len(above_70):3} ({len(above_70)/n*100:.1f}%)")
    print(f"    > $0.60:                  {len(above_60):3} ({len(above_60)/n*100:.1f}%)")
    print(f"    <= $0.50 (cheap):         {len(below_50):3} ({len(below_50)/n*100:.1f}%)")

    # PnL by entry price bucket
    print(f"\n  PnL by Entry Price:")
    buckets = [(0, 0.50, "<=50c"), (0.50, 0.60, "50-60c"), (0.60, 0.70, "60-70c"),
               (0.70, 0.80, "70-80c"), (0.80, 1.0, ">80c")]
    for lo, hi, label in buckets:
        bucket_trades = [t for t in filtered if lo < t.winner_fill_price <= hi]
        if not bucket_trades:
            continue
        b_pnl = sum(t.pnl for t in bucket_trades)
        b_wr = sum(1 for t in bucket_trades if t.pnl > 0) / len(bucket_trades) * 100
        b_dir = sum(1 for t in bucket_trades if t.correct_direction) / len(bucket_trades) * 100
        print(f"    {label:>6}: {len(bucket_trades):3} trades | PnL ${b_pnl:+.4f} | WR {b_wr:.1f}% | Dir {b_dir:.1f}%")

    # =========================================================================
    # 4. STOP-LOSS DETAILED ANALYSIS
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"4. STOP-LOSS ANALYSIS")
    print(f"{'='*80}")

    all_stops = stoploss + timestop
    if all_stops:
        stop_correct = [t for t in all_stops if t.correct_direction]
        stop_wrong = [t for t in all_stops if not t.correct_direction]

        print(f"  Total stops: {len(all_stops)} ({len(all_stops)/n*100:.1f}% of trades)")
        if stoploss:
            print(f"    Price stops: {len(stoploss)}")
        if timestop:
            print(f"    Time stops:  {len(timestop)}")

        print(f"\n  CORRECT DIRECTION STOPS (premature - we were right but got stopped):")
        print(f"    Count:  {len(stop_correct)} ({len(stop_correct)/len(all_stops)*100:.1f}% of stops)")
        if stop_correct:
            sc_pnl = sum(t.pnl for t in stop_correct)
            print(f"    PnL:    ${sc_pnl:.4f} (this is PnL LOST to premature stops)")
            print(f"    Avg entry: ${np.mean([t.winner_fill_price for t in stop_correct]):.4f}")
            print(f"    Avg pair cost: ${np.mean([t.pair_cost for t in stop_correct]):.4f}")

        print(f"\n  WRONG DIRECTION STOPS (correct stops - we were wrong and got out):")
        print(f"    Count:  {len(stop_wrong)} ({len(stop_wrong)/len(all_stops)*100:.1f}% of stops)")
        if stop_wrong:
            sw_pnl = sum(t.pnl for t in stop_wrong)
            # What would PnL have been without stop? (held to resolution = full loss)
            counterfactual_pnl = sum(-t.winner_fill_price * t.shares_filled for t in stop_wrong)
            saved = counterfactual_pnl - sw_pnl
            print(f"    PnL:    ${sw_pnl:.4f}")
            print(f"    Without stop (resolution loss): ${counterfactual_pnl:.4f}")
            print(f"    PnL SAVED by stopping: ${-saved:.4f}")
            print(f"    Avg entry: ${np.mean([t.winner_fill_price for t in stop_wrong]):.4f}")

        # Stop timing analysis
        if all_stops and all_stops[0].exit_ts:
            stop_durations = [(t.exit_ts - t.entry_ts) / 1000.0 for t in all_stops if t.exit_ts]
            if stop_durations:
                print(f"\n  STOP TIMING:")
                print(f"    Mean time to stop: {np.mean(stop_durations):.1f}s")
                print(f"    Median:            {np.median(stop_durations):.1f}s")
                print(f"    Min:               {min(stop_durations):.1f}s")
                print(f"    Max:               {max(stop_durations):.1f}s")

        # Entry prices of stopped trades vs non-stopped
        stop_entries = [t.winner_fill_price for t in all_stops]
        non_stop_entries = [t.winner_fill_price for t in filtered if t.hedge_type not in ("stoploss", "timestop")]
        if non_stop_entries:
            print(f"\n  ENTRY PRICE: Stopped vs Non-Stopped:")
            print(f"    Stopped avg:     ${np.mean(stop_entries):.4f}")
            print(f"    Non-stopped avg: ${np.mean(non_stop_entries):.4f}")
            diff = np.mean(stop_entries) - np.mean(non_stop_entries)
            print(f"    Difference:      ${diff:+.4f} ({'stopped pay more' if diff > 0 else 'stopped pay less'})")
    else:
        print(f"  No stops triggered!")

    # =========================================================================
    # 5. PAIR COST ANALYSIS
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"5. PAIR COST & HEDGE ANALYSIS")
    print(f"{'='*80}")

    pair_costs = [t.pair_cost for t in filtered]
    print(f"  Pair Cost (winner_entry + loser_bid):")
    print(f"    Mean:   ${np.mean(pair_costs):.4f}")
    print(f"    Median: ${np.median(pair_costs):.4f}")
    print(f"    Min:    ${min(pair_costs):.4f}")
    print(f"    Max:    ${max(pair_costs):.4f}")
    print(f"    < $1.00 (profit zone): {sum(1 for p in pair_costs if p < 1.0)}/{n} ({sum(1 for p in pair_costs if p < 1.0)/n*100:.1f}%)")

    # Loser fill prices for hedged trades
    hedged = passive + stoploss + timestop
    if hedged:
        loser_fills = [t.loser_fill_price for t in hedged]
        print(f"\n  Loser Fill Price (hedged trades only):")
        print(f"    Mean:   ${np.mean(loser_fills):.4f}")
        print(f"    Median: ${np.median(loser_fills):.4f}")

    # =========================================================================
    # 6. DIRECTION ACCURACY
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"6. DIRECTION ACCURACY")
    print(f"{'='*80}")

    correct = [t for t in filtered if t.correct_direction]
    wrong = [t for t in filtered if not t.correct_direction]
    dir_acc = len(correct) / n * 100

    print(f"  Correct direction: {len(correct)}/{n} ({dir_acc:.1f}%)")
    print(f"  Wrong direction:   {len(wrong)}/{n} ({100-dir_acc:.1f}%)")

    # Direction accuracy by exit type
    for label, group in [("Passive", passive), ("Stops", all_stops), ("Resolution", resolution)]:
        if not group:
            continue
        g_correct = sum(1 for t in group if t.correct_direction)
        print(f"    {label}: {g_correct}/{len(group)} ({g_correct/len(group)*100:.1f}%) correct")

    # =========================================================================
    # 7. FILL TIME ANALYSIS
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"7. FILL TIME ANALYSIS (Passive fills)")
    print(f"{'='*80}")

    if passive:
        fill_times = [(t.exit_ts - t.entry_ts) / 1000.0 for t in passive if t.exit_ts]
        if fill_times:
            print(f"  Passive fills: {len(passive)}")
            print(f"  Fill time (seconds):")
            print(f"    Mean:   {np.mean(fill_times):.1f}s")
            print(f"    Median: {np.median(fill_times):.1f}s")
            print(f"    P25:    {np.percentile(fill_times, 25):.1f}s")
            print(f"    P75:    {np.percentile(fill_times, 75):.1f}s")
            print(f"    < 60s:  {sum(1 for f in fill_times if f < 60)}/{len(fill_times)} ({sum(1 for f in fill_times if f < 60)/len(fill_times)*100:.1f}%)")
            print(f"    < 180s: {sum(1 for f in fill_times if f < 180)}/{len(fill_times)} ({sum(1 for f in fill_times if f < 180)/len(fill_times)*100:.1f}%)")
    else:
        print(f"  No passive fills!")

    # =========================================================================
    # 8. TRADE FREQUENCY / CYCLING
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"8. TRADE FREQUENCY")
    print(f"{'='*80}")

    hours_active = estimate_active_hours_zone(total_hours, zscore_df, cfg.z_lo, cfg.z_hi)
    hourly_rate = total_pnl / hours_active if hours_active > 0 else 0

    print(f"  Total hours: {total_hours:.2f}")
    print(f"  Active hours (in z-zone): {hours_active:.2f} ({hours_active/total_hours*100:.1f}%)")
    print(f"  Trades/hr (active): {n/hours_active:.2f}")
    print(f"  $/hr (active, @5sh): ${hourly_rate:.4f}")
    print(f"  $/hr (active, @50sh): ${hourly_rate*10:.2f}")

    # =========================================================================
    # 9. COMPARISON TO IN-SAMPLE
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"9. OOS3 vs IN-SAMPLE COMPARISON")
    print(f"{'='*80}")

    print(f"  {'Metric':<25} {'OOS3':<15} {'In-Sample':<15} {'Delta':<12}")
    print(f"  {'-'*65}")

    oos_wr = sum(1 for t in filtered if t.pnl > 0) / n * 100
    print(f"  {'Win Rate':<25} {oos_wr:<14.1f}% {cfg.expected_win_rate:<14.1f}% {oos_wr - cfg.expected_win_rate:+.1f} ppts")

    oos_rate = hourly_rate
    print(f"  {'$/hr @5sh':<25} ${oos_rate:<13.4f} ${cfg.expected_hourly_rate:<13.4f} {(oos_rate/cfg.expected_hourly_rate - 1)*100:+.1f}%")

    trades_per_hr = n / hours_active
    is_trades_per_hr = cfg.expected_trades / (81.71 * hours_active / total_hours) if total_hours > 0 else 0
    # Approximate IS trades/active hr
    print(f"  {'Trades (total)':<25} {n:<15} {cfg.expected_trades:<15}")

    oos_dir = len(correct) / n * 100
    print(f"  {'Direction Accuracy':<25} {oos_dir:<14.1f}%")

    passive_pct = len(passive) / n * 100 if n > 0 else 0
    print(f"  {'Passive Fill %':<25} {passive_pct:<14.1f}%")

    stop_pct = len(all_stops) / n * 100 if n > 0 else 0
    print(f"  {'Stop %':<25} {stop_pct:<14.1f}%")

    res_pct = len(resolution) / n * 100 if n > 0 else 0
    print(f"  {'Resolution %':<25} {res_pct:<14.1f}%")

    return {
        'config': cfg.name,
        'trades': n,
        'pnl_5sh': total_pnl,
        'pnl_50sh': total_pnl * 10,
        'hourly_50sh': hourly_rate * 10,
        'win_rate': oos_wr,
        'dir_acc': oos_dir,
        'passive_pct': passive_pct,
        'stop_pct': stop_pct,
        'resolution_pct': res_pct,
        'above_80_pct': len(above_80) / n * 100,
        'mean_entry': np.mean(entry_prices),
        'premature_pct': len([t for t in all_stops if t.correct_direction]) / len(all_stops) * 100 if all_stops else 0,
    }


def main():
    print("=" * 100)
    print("DETAILED OOS3 ANALYSIS - FULL TRADE BREAKDOWN")
    print("=" * 100)

    # Load data
    print("\nLoading OOS3 data...")
    ou_params = load_ou_params()

    print(f"  Loading BTC: {OOS3_BTC_FILE.name}")
    btc_df = pd.read_csv(OOS3_BTC_FILE)
    btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
    print(f"  BTC rows: {len(btc_df):,}")

    print(f"  Loading observer: {OOS3_OBS_FILE.name}")
    obs_df = pd.read_csv(OOS3_OBS_FILE, on_bad_lines='skip', low_memory=False)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    print(f"  Observer rows: {len(obs_df):,}")

    res_df = pd.read_csv(OOS3_RES_FILE)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    total_hours = (btc_df['timestamp_ms'].max() - btc_df['timestamp_ms'].min()) / 3600000
    print(f"  Dataset: {total_hours:.2f} hours")

    # Compute z-scores
    print("\nComputing z-scores...")
    zscore_cache = {}
    for method in ['ewma', 'ou']:
        zscore_cache[method] = compute_zscore_series(btc_df, ou_params, zscore_method=method)

    # Run each config and analyze
    all_results = []
    for cfg in ALL_CONFIGS:
        zscore_df = zscore_cache[cfg.zscore_method]

        backtest_cfg = BacktestConfig(
            target_shares=5,
            spike_lookback=cfg.lookback_ticks,
            stop_loss_pct=cfg.stop_loss_pct,
            time_stop_seconds=cfg.time_stop_seconds,
            use_cycling=cfg.use_cycling,
        )

        trades = run_backtest_with_zscore(
            backtest_cfg, btc_df, obs_df, zscore_df, res_map,
            method=cfg.threshold_method,
            ou_params=ou_params,
            quiet=True
        )

        result = detailed_analysis(trades, cfg, total_hours, zscore_df)
        if result:
            all_results.append(result)

    # Final comparison table
    print(f"\n\n{'='*100}")
    print(f"FINAL COMPARISON TABLE")
    print(f"{'='*100}")
    print(f"\n{'Config':<14} {'Trades':<7} {'PnL@50':<10} {'$/hr@50':<9} {'WR%':<7} {'Dir%':<7} {'Pass%':<7} {'Stop%':<7} {'Res%':<7} {'>80c%':<7} {'Prem%':<7}")
    print("-" * 100)
    for r in all_results:
        print(f"{r['config']:<14} {r['trades']:<7} ${r['pnl_50sh']:<9.2f} ${r['hourly_50sh']:<8.2f} "
              f"{r['win_rate']:<6.1f}% {r['dir_acc']:<6.1f}% {r['passive_pct']:<6.1f}% "
              f"{r['stop_pct']:<6.1f}% {r['resolution_pct']:<6.1f}% {r['above_80_pct']:<6.1f}% "
              f"{r['premature_pct']:<6.1f}%")


if __name__ == "__main__":
    main()
