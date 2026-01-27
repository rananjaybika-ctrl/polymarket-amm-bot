#!/usr/bin/env python3
"""
Quick comparison: 120s time-stop vs 15% price-stop

Tests the AGGRESSIVE config with both stop types to see which performs better.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from research.volatility_filter_analysis import (
    load_ou_params, load_btc_data, load_observer_data,
    compute_zscore_series, BacktestConfig, run_backtest_with_zscore,
)


def analyze_trades(trades, label):
    """Analyze trade outcomes."""
    if not trades:
        print(f"\n{label}: No trades")
        return

    # Filter to z-zone 0 < z < 1.5
    filtered = [t for t in trades if 0 < t.zscore_at_entry < 1.5]

    passive = [t for t in filtered if t.hedge_type == "passive"]
    stoploss = [t for t in filtered if t.hedge_type == "stoploss"]
    timestop = [t for t in filtered if t.hedge_type == "timestop"]
    resolution = [t for t in filtered if t.hedge_type == "resolution"]

    total_pnl = sum(t.pnl for t in filtered)
    wins = sum(1 for t in filtered if t.pnl > 0)
    correct_dir = sum(1 for t in filtered if t.correct_direction)

    # Stop-loss analysis
    sl_correct = [t for t in stoploss if t.correct_direction]
    sl_wrong = [t for t in stoploss if not t.correct_direction]

    # Time-stop analysis
    ts_correct = [t for t in timestop if t.correct_direction]
    ts_wrong = [t for t in timestop if not t.correct_direction]

    print(f"\n{'='*80}")
    print(f"{label}")
    print(f"{'='*80}")
    print(f"Total trades (0<z<1.5): {len(filtered)}")
    print(f"Total PnL: ${total_pnl:.2f}")
    print(f"Win rate: {wins/len(filtered)*100:.1f}%" if filtered else "N/A")
    print(f"Direction accuracy: {correct_dir/len(filtered)*100:.1f}%" if filtered else "N/A")

    print(f"\nHedge Type Breakdown:")
    print(f"  Passive fills: {len(passive)} ({len(passive)/len(filtered)*100:.1f}%)" if filtered else "")
    print(f"  Stop-losses:   {len(stoploss)} ({len(stoploss)/len(filtered)*100:.1f}%)" if filtered else "")
    print(f"  Time-stops:    {len(timestop)} ({len(timestop)/len(filtered)*100:.1f}%)" if filtered else "")
    print(f"  Resolutions:   {len(resolution)} ({len(resolution)/len(filtered)*100:.1f}%)" if filtered else "")

    if stoploss:
        print(f"\nStop-Loss Analysis:")
        print(f"  Correct direction (premature): {len(sl_correct)} ({len(sl_correct)/len(stoploss)*100:.1f}%)")
        print(f"  Wrong direction (rightful):    {len(sl_wrong)} ({len(sl_wrong)/len(stoploss)*100:.1f}%)")
        print(f"  PnL from correct-dir stops:    ${sum(t.pnl for t in sl_correct):.2f}")

    if timestop:
        print(f"\nTime-Stop Analysis:")
        print(f"  Correct direction (premature): {len(ts_correct)} ({len(ts_correct)/len(timestop)*100:.1f}%)")
        print(f"  Wrong direction (rightful):    {len(ts_wrong)} ({len(ts_wrong)/len(timestop)*100:.1f}%)")
        print(f"  PnL from correct-dir stops:    ${sum(t.pnl for t in ts_correct):.2f}")


def main():
    print("Loading data...")
    ou_params = load_ou_params()
    btc_df = load_btc_data()
    obs_df, res_map = load_observer_data()

    print("Computing z-scores (EWMA method)...")
    zscore_df = compute_zscore_series(btc_df, ou_params, zscore_method='ewma')

    # Test configs
    configs = [
        ("15% PRICE-STOP (Current)", BacktestConfig(
            target_shares=5,
            spike_lookback=72,  # 1200ms
            stop_loss_pct=0.15,
            use_cycling=True,
            time_stop_seconds=None,  # No time stop
        )),
        ("120s TIME-STOP (New)", BacktestConfig(
            target_shares=5,
            spike_lookback=72,  # 1200ms
            stop_loss_pct=None,  # No price stop
            use_cycling=True,
            time_stop_seconds=120,  # 120 second timeout
        )),
        ("HYBRID: 15% OR 120s", BacktestConfig(
            target_shares=5,
            spike_lookback=72,  # 1200ms
            stop_loss_pct=0.15,  # 15% price stop
            use_cycling=True,
            time_stop_seconds=120,  # AND 120s time stop
        )),
        ("90s TIME-STOP", BacktestConfig(
            target_shares=5,
            spike_lookback=72,  # 1200ms
            stop_loss_pct=None,
            use_cycling=True,
            time_stop_seconds=90,
        )),
        ("180s TIME-STOP", BacktestConfig(
            target_shares=5,
            spike_lookback=72,  # 1200ms
            stop_loss_pct=None,
            use_cycling=True,
            time_stop_seconds=180,
        )),
    ]

    for label, config in configs:
        print(f"\nRunning: {label}...")
        trades = run_backtest_with_zscore(
            config, btc_df, obs_df, zscore_df, res_map,
            method='ou',
            ou_params=ou_params,
            quiet=True
        )
        analyze_trades(trades, label)

    # Summary comparison
    print("\n" + "="*80)
    print("SUMMARY COMPARISON")
    print("="*80)


if __name__ == "__main__":
    main()
