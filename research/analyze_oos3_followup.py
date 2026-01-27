#!/usr/bin/env python3
"""
Follow-up OOS3 analysis:
1. Resolution trade details - entry/loser prices, why unhedged
2. Markets with >1 trade per strategy
"""

import sys
from pathlib import Path
from collections import Counter
from typing import List
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


def filter_to_zone(trades: List[TradeWithZScore], cfg: TradingConfig):
    filtered = []
    for t in trades:
        z = t.zscore_at_entry
        if cfg.z_lo is not None and z <= cfg.z_lo:
            continue
        if cfg.z_hi is not None and z >= cfg.z_hi:
            continue
        filtered.append(t)
    return filtered


def analyze_resolution_trades(trades: List[TradeWithZScore], cfg: TradingConfig):
    """Detailed breakdown of resolution (unhedged) trades."""
    filtered = filter_to_zone(trades, cfg)
    resolution = [t for t in filtered if t.hedge_type == "resolution"]

    print(f"\n{'='*80}")
    print(f"RESOLUTION TRADE DETAILS: {cfg.name}")
    print(f"{'='*80}")

    if not resolution:
        print(f"  No resolution trades for {cfg.name}")
        return

    print(f"\n  {'#':<3} {'Market':<35} {'Side':<5} {'Winner$':<8} {'Loser$':<8} {'PairCost':<9} {'Correct':<8} {'PnL':<8} {'EntryTime':<10}")
    print(f"  {'-'*105}")

    for i, t in enumerate(resolution):
        print(f"  {i+1:<3} {t.market_slug[-25:]:<35} {t.winner_side:<5} "
              f"${t.winner_fill_price:<7.4f} ${t.loser_fill_price:<7.4f} "
              f"${t.pair_cost:<8.4f} {'YES' if t.correct_direction else 'NO':<8} "
              f"${t.pnl:<7.4f} {t.entry_time_remaining:<8.0f}s")

    # Explain the PnL mechanics
    print(f"\n  PnL MECHANICS FOR RESOLUTION TRADES:")
    print(f"  The backtest calculates resolution PnL as:")
    print(f"    Correct: (1.0 - pair_cost) * shares = (1.0 - winner_entry - loser_bid) * shares")
    print(f"    Wrong:   -winner_entry * shares (total loss of entry)")
    print(f"")
    print(f"  NOTE: pair_cost includes loser_bid even though loser NEVER FILLED.")
    print(f"  Actual unhedged PnL if correct = (1.0 - winner_entry) * shares (no loser cost)")
    print(f"")

    # Show what actual PnL would be with correct accounting
    print(f"  CORRECT PnL ACCOUNTING (loser never purchased):")
    print(f"  {'#':<3} {'Winner$':<8} {'Backtest PnL':<13} {'Actual PnL':<11} {'Diff':<8}")
    print(f"  {'-'*50}")
    total_backtest = 0
    total_actual = 0
    for i, t in enumerate(resolution):
        if t.correct_direction:
            backtest_pnl = t.pnl
            # Actual: (1.0 - winner_entry) * shares - entry_fee (no loser cost, no hedge fee)
            actual_gross = (1.0 - t.winner_fill_price) * t.shares_filled
            actual_pnl = actual_gross - t.entry_fee
            total_backtest += backtest_pnl
            total_actual += actual_pnl
            print(f"  {i+1:<3} ${t.winner_fill_price:<7.4f} ${backtest_pnl:<12.4f} ${actual_pnl:<10.4f} ${actual_pnl - backtest_pnl:+.4f}")
        else:
            # Wrong direction: same either way (lose entry cost)
            print(f"  {i+1:<3} ${t.winner_fill_price:<7.4f} ${t.pnl:<12.4f} ${t.pnl:<10.4f} $0.0000")
            total_backtest += t.pnl
            total_actual += t.pnl

    print(f"  {'---'}")
    print(f"  {'TOT':<3} {'':8} ${total_backtest:<12.4f} ${total_actual:<10.4f} ${total_actual - total_backtest:+.4f}")
    print(f"\n  RISK EXPOSURE: If ANY of these 7 trades were WRONG direction:")
    for t in resolution:
        if t.correct_direction:
            potential_loss = -t.winner_fill_price * t.shares_filled - t.entry_fee
            print(f"    {t.market_slug[-30:]}: entry ${t.winner_fill_price:.3f} → potential loss ${potential_loss:.4f} (@5sh) / ${potential_loss*10:.2f} (@50sh)")


def analyze_market_cycling(trades: List[TradeWithZScore], cfg: TradingConfig):
    """How many markets had >1 trade."""
    filtered = filter_to_zone(trades, cfg)

    print(f"\n{'='*80}")
    print(f"MARKET CYCLING ANALYSIS: {cfg.name}")
    print(f"{'='*80}")

    # Count trades per market
    market_counts = Counter(t.market_slug for t in filtered)
    total_markets = len(market_counts)

    # Distribution
    count_dist = Counter(market_counts.values())

    print(f"\n  Total markets traded: {total_markets}")
    print(f"  Total trades: {len(filtered)}")
    print(f"  Cycling: {'ON' if cfg.use_cycling else 'OFF'}")
    print(f"\n  Trades per market distribution:")
    for num_trades in sorted(count_dist.keys()):
        num_markets = count_dist[num_trades]
        pct = num_markets / total_markets * 100
        print(f"    {num_trades} trade(s):  {num_markets} markets ({pct:.1f}%)")

    # Markets with >1 trade
    multi_trade_markets = {k: v for k, v in market_counts.items() if v > 1}
    print(f"\n  Markets with >1 trade: {len(multi_trade_markets)} ({len(multi_trade_markets)/total_markets*100:.1f}%)")

    if multi_trade_markets:
        # Show details for multi-trade markets
        print(f"\n  {'Market':<40} {'Trades':<7} {'PnL':<10} {'Winners':<8}")
        print(f"  {'-'*70}")
        for market, count in sorted(multi_trade_markets.items(), key=lambda x: -x[1])[:15]:
            market_trades = [t for t in filtered if t.market_slug == market]
            m_pnl = sum(t.pnl for t in market_trades)
            m_wins = sum(1 for t in market_trades if t.pnl > 0)
            print(f"  {market[-38:]:<40} {count:<7} ${m_pnl:<9.4f} {m_wins}/{count}")

        # PnL from 1-trade vs multi-trade markets
        single_trades = [t for t in filtered if market_counts[t.market_slug] == 1]
        multi_trades = [t for t in filtered if market_counts[t.market_slug] > 1]

        print(f"\n  PnL BREAKDOWN:")
        print(f"    Single-trade markets: {len(single_trades)} trades, PnL ${sum(t.pnl for t in single_trades):.4f}")
        print(f"    Multi-trade markets:  {len(multi_trades)} trades, PnL ${sum(t.pnl for t in multi_trades):.4f}")


def main():
    print("=" * 100)
    print("OOS3 FOLLOW-UP: RESOLUTION TRADES & MARKET CYCLING")
    print("=" * 100)

    # Load data
    ou_params = load_ou_params()
    btc_df = pd.read_csv(OOS3_BTC_FILE)
    btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
    obs_df = pd.read_csv(OOS3_OBS_FILE, on_bad_lines='skip', low_memory=False)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    res_df = pd.read_csv(OOS3_RES_FILE)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    print(f"\nComputing z-scores...")
    zscore_cache = {}
    for method in ['ewma', 'ou']:
        zscore_cache[method] = compute_zscore_series(btc_df, ou_params, zscore_method=method)

    # Run each config
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

        analyze_resolution_trades(trades, cfg)
        analyze_market_cycling(trades, cfg)


if __name__ == "__main__":
    main()
