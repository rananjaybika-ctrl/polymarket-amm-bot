#!/usr/bin/env python3
"""
Test #9 Config with 120s Time-Stop: Cycling ON vs OFF

#9 Config: ou/ou/1400ms/0<z<1.5
Testing with 120s time-stop (only exits if NOT in profit)
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from research.analysis.volatility_filter_analysis import (
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

    if not filtered:
        print(f"\n{label}: No trades in z-zone 0<z<1.5")
        return

    passive = [t for t in filtered if t.hedge_type == "passive"]
    stoploss = [t for t in filtered if t.hedge_type == "stoploss"]
    timestop = [t for t in filtered if t.hedge_type == "timestop"]
    resolution = [t for t in filtered if t.hedge_type == "resolution"]

    total_pnl = sum(t.pnl for t in filtered)
    wins = sum(1 for t in filtered if t.pnl > 0)
    correct_dir = sum(1 for t in filtered if t.correct_direction)

    # Stop-loss analysis (price-based)
    sl_correct = [t for t in stoploss if t.correct_direction]
    sl_wrong = [t for t in stoploss if not t.correct_direction]

    # Time-stop analysis
    ts_correct = [t for t in timestop if t.correct_direction]
    ts_wrong = [t for t in timestop if not t.correct_direction]

    # Resolution analysis
    res_correct = [t for t in resolution if t.correct_direction]
    res_wrong = [t for t in resolution if not t.correct_direction]

    print(f"\n{'='*80}")
    print(f"{label}")
    print(f"{'='*80}")
    print(f"Total trades (0<z<1.5): {len(filtered)}")
    print(f"Total PnL: ${total_pnl:.2f}")
    print(f"Win rate: {wins/len(filtered)*100:.1f}%")
    print(f"Direction accuracy: {correct_dir/len(filtered)*100:.1f}%")

    print(f"\nHedge Type Breakdown:")
    print(f"  Passive fills: {len(passive)} ({len(passive)/len(filtered)*100:.1f}%)")
    if stoploss:
        print(f"  Price stop-losses: {len(stoploss)} ({len(stoploss)/len(filtered)*100:.1f}%)")
    if timestop:
        print(f"  Time-stops: {len(timestop)} ({len(timestop)/len(filtered)*100:.1f}%)")
    print(f"  Resolutions: {len(resolution)} ({len(resolution)/len(filtered)*100:.1f}%)")

    if stoploss:
        print(f"\nPrice Stop-Loss Analysis:")
        print(f"  Correct direction (premature): {len(sl_correct)} ({len(sl_correct)/len(stoploss)*100:.1f}%)")
        print(f"  Wrong direction (rightful):    {len(sl_wrong)} ({len(sl_wrong)/len(stoploss)*100:.1f}%)")
        print(f"  PnL from premature stops:      ${sum(t.pnl for t in sl_correct):.2f}")

    if timestop:
        print(f"\nTime-Stop Analysis:")
        print(f"  Correct direction (premature): {len(ts_correct)} ({len(ts_correct)/len(timestop)*100:.1f}%)")
        print(f"  Wrong direction (rightful):    {len(ts_wrong)} ({len(ts_wrong)/len(timestop)*100:.1f}%)")
        print(f"  PnL from premature stops:      ${sum(t.pnl for t in ts_correct):.2f}")

    if resolution:
        print(f"\nResolution Analysis:")
        print(f"  Correct direction: {len(res_correct)} ({len(res_correct)/len(resolution)*100:.1f}%)")
        print(f"  Wrong direction:   {len(res_wrong)} ({len(res_wrong)/len(resolution)*100:.1f}%)")
        print(f"  PnL from resolutions: ${sum(t.pnl for t in resolution):.2f}")

    return {
        'trades': len(filtered),
        'pnl': total_pnl,
        'win_rate': wins/len(filtered)*100,
        'dir_acc': correct_dir/len(filtered)*100,
        'passive': len(passive),
        'timestop': len(timestop),
        'resolution': len(resolution),
    }


def main():
    print("="*80)
    print("#9 CONFIG TEST: 1400ms OU/OU with 120s TIME-STOP")
    print("Cycling ON vs OFF Comparison")
    print("="*80)

    print("\nLoading data...", flush=True)
    ou_params = load_ou_params()
    btc_df = load_btc_data()
    obs_df, res_map = load_observer_data()

    print("Computing z-scores (OU method)...", flush=True)
    zscore_df = compute_zscore_series(btc_df, ou_params, zscore_method='ou')

    # Test configs - #9 config with 120s time-stop, cycling ON vs OFF
    configs = [
        ("120s TIME-STOP + CYCLING OFF", BacktestConfig(
            target_shares=5,
            spike_lookback=84,  # 1400ms (#9 config)
            stop_loss_pct=None,  # No price stop
            use_cycling=False,  # OFF
            time_stop_seconds=120,
        )),
        ("120s TIME-STOP + CYCLING ON", BacktestConfig(
            target_shares=5,
            spike_lookback=84,  # 1400ms (#9 config)
            stop_loss_pct=None,  # No price stop
            use_cycling=True,  # ON
            time_stop_seconds=120,
        )),
        ("15% PRICE-STOP + CYCLING OFF (Original #9)", BacktestConfig(
            target_shares=5,
            spike_lookback=84,  # 1400ms
            stop_loss_pct=0.15,  # 15% price stop
            use_cycling=False,
            time_stop_seconds=None,
        )),
        ("15% PRICE-STOP + CYCLING ON", BacktestConfig(
            target_shares=5,
            spike_lookback=84,  # 1400ms
            stop_loss_pct=0.15,
            use_cycling=True,
            time_stop_seconds=None,
        )),
    ]

    results = []
    for label, config in configs:
        print(f"\nRunning: {label}...", flush=True)
        trades = run_backtest_with_zscore(
            config, btc_df, obs_df, zscore_df, res_map,
            method='ou',  # OU threshold method (#9 config)
            ou_params=ou_params,
            quiet=True
        )
        result = analyze_trades(trades, label)
        if result:
            result['label'] = label
            results.append(result)

    # Summary comparison
    print("\n" + "="*80)
    print("SUMMARY COMPARISON")
    print("="*80)

    print(f"\n{'Config':<45} {'Trades':<8} {'PnL':<10} {'Win%':<8} {'Dir%':<8}")
    print("-"*80)
    for r in results:
        print(f"{r['label']:<45} {r['trades']:<8} ${r['pnl']:<9.2f} {r['win_rate']:<7.1f}% {r['dir_acc']:<7.1f}%")

    print("\n" + "="*80)
    print("KEY INSIGHT")
    print("="*80)

    # Find best by PnL
    best_pnl = max(results, key=lambda x: x['pnl'])
    best_win = max(results, key=lambda x: x['win_rate'])

    print(f"\nBest by PnL: {best_pnl['label']}")
    print(f"  ${best_pnl['pnl']:.2f} total, {best_pnl['win_rate']:.1f}% win rate")

    print(f"\nBest by Win Rate: {best_win['label']}")
    print(f"  {best_win['win_rate']:.1f}% win rate, ${best_win['pnl']:.2f} total")


if __name__ == "__main__":
    main()
