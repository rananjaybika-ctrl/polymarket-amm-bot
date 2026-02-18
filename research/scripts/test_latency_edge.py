#!/usr/bin/env python3
"""
Test latency edge: BTC signal → Polymarket price movement.

Hypothesis: BTC EWMA crossover predicts Polymarket price movement
with 1-10 second latency. We can place maker orders during this window.

Test: When BTC fast > slow (UP signal), does Polymarket UP_ask increase
in the next N seconds? Can we get maker fills before adjustment?
"""

import pandas as pd
import numpy as np
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# FAST EWMA for latency edge (detect moves happening NOW)
FAST_OPTIONS = [100, 200, 500]      # 100-500ms
SLOW_OPTIONS = [1000, 2000, 5000]   # 1-5s

# Latency windows to test (seconds)
LATENCY_WINDOWS = [1, 2, 3, 5]

# Minimum move to be actionable (cents)
MIN_MOVE_CENTS = 0.01


def compute_ewma_signals(btc_df: pd.DataFrame, fast_ms: int, slow_ms: int) -> pd.DataFrame:
    """Compute EWMA crossover and detect NEW crossovers."""
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    tick_ms = 16.67
    fast_ticks = fast_ms / tick_ms
    slow_ticks = slow_ms / tick_ms

    df['ewma_fast'] = df['price'].ewm(halflife=fast_ticks).mean()
    df['ewma_slow'] = df['price'].ewm(halflife=slow_ticks).mean()

    # Signal direction
    df['signal'] = np.where(df['ewma_fast'] > df['ewma_slow'], 'UP', 'DOWN')

    # Detect NEW crossovers (signal change)
    df['prev_signal'] = df['signal'].shift(1)
    df['is_crossover'] = df['signal'] != df['prev_signal']

    # Crossover strength: how far fast is from slow (%)
    df['crossover_strength'] = (df['ewma_fast'] - df['ewma_slow']) / df['ewma_slow'] * 100

    return df


def test_latency_edge(btc_df: pd.DataFrame, obs_df: pd.DataFrame, fast_ms: int, slow_ms: int) -> dict:
    """Test if BTC crossovers predict Polymarket moves within latency windows."""

    # Compute BTC signals
    btc = compute_ewma_signals(btc_df, fast_ms, slow_ms)

    # Get crossover events
    crossovers = btc[btc['is_crossover'] == True].copy()
    print(f"  Crossover events: {len(crossovers):,}")

    # Merge observer with BTC signals (nearest match)
    print("Merging observer with BTC...")
    btc_for_merge = btc[['timestamp_ms', 'signal', 'is_crossover', 'crossover_strength']].copy()
    obs = obs_df.sort_values('timestamp_ms').copy()
    obs = pd.merge_asof(
        obs,
        btc_for_merge,
        on='timestamp_ms',
        direction='nearest',
        tolerance=500  # 500ms tolerance
    )

    # For each latency window, test prediction accuracy
    results = {w: {'correct': 0, 'total': 0, 'avg_move': [], 'fills': 0} for w in LATENCY_WINDOWS}

    # Group by market for efficiency
    print("Testing latency edge per market...")
    markets = obs['market_slug'].unique()

    for i, slug in enumerate(markets):
        if i > 0 and i % 50 == 0:
            print(f"  {i}/{len(markets)} markets...")

        mdf = obs[obs['market_slug'] == slug].copy()
        mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

        if len(mdf) < 100:
            continue

        # Find crossover moments in this market
        cross_rows = mdf[mdf['is_crossover'] == True]

        for _, row in cross_rows.iterrows():
            ts = row['timestamp_ms']
            signal = row['signal']

            up_ask = row['up_ask']
            down_ask = row['down_ask']

            if pd.isna(up_ask) or pd.isna(down_ask):
                continue

            # Signal predicts which side will move UP
            # UP signal → UP_ask should increase, DOWN_ask should decrease
            # DOWN signal → DOWN_ask should increase, UP_ask should decrease

            for window in LATENCY_WINDOWS:
                # Find row at ts + window seconds
                future_ts = ts + window * 1000
                future_rows = mdf[(mdf['timestamp_ms'] >= future_ts - 100) &
                                   (mdf['timestamp_ms'] <= future_ts + 100)]

                if len(future_rows) == 0:
                    continue

                future = future_rows.iloc[0]
                future_up_ask = future['up_ask']
                future_down_ask = future['down_ask']

                if pd.isna(future_up_ask) or pd.isna(future_down_ask):
                    continue

                # Calculate moves
                up_move = future_up_ask - up_ask
                down_move = future_down_ask - down_ask

                # Check if signal predicted correctly
                if signal == 'UP':
                    # Expect UP to increase, DOWN to decrease
                    predicted_move = up_move
                    correct = up_move > 0 or down_move < 0
                else:
                    # Expect DOWN to increase, UP to decrease
                    predicted_move = down_move
                    correct = down_move > 0 or up_move < 0

                results[window]['total'] += 1
                if correct:
                    results[window]['correct'] += 1
                results[window]['avg_move'].append(predicted_move)

                # Check if maker fill possible
                # Can we bid at current ask - 1c and get filled?
                if signal == 'UP':
                    bid = up_ask - 0.01
                    filled = future_up_ask <= bid  # Ask dropped to our bid
                else:
                    bid = down_ask - 0.01
                    filled = future_down_ask <= bid

                if filled:
                    results[window]['fills'] += 1

    return results


def main():
    print("=" * 60)
    print("LATENCY EDGE TEST - FAST EWMA")
    print("=" * 60)
    print(f"Fast options: {FAST_OPTIONS} ms")
    print(f"Slow options: {SLOW_OPTIONS} ms")
    print(f"Windows: {LATENCY_WINDOWS} seconds")

    # Load data
    obs_path = Path("research/observer/PROTECTED_grid_obs_is_oos2_combined.csv")
    btc_path = Path("research/binance_hf/btc_prices_is_oos2_5hz.csv")

    print(f"\nLoading...")
    obs_df = pd.read_csv(obs_path, low_memory=False)
    btc_df = pd.read_csv(btc_path)
    print(f"  Obs: {len(obs_df):,}, BTC: {len(btc_df):,}")

    # Test all combinations
    all_results = []

    for fast_ms in FAST_OPTIONS:
        for slow_ms in SLOW_OPTIONS:
            if fast_ms >= slow_ms:
                continue

            print(f"\nTesting fast={fast_ms}ms, slow={slow_ms}ms...")
            results = test_latency_edge(btc_df, obs_df, fast_ms, slow_ms)

            # Get best window (highest accuracy)
            best_window = None
            best_acc = 0
            for w in LATENCY_WINDOWS:
                r = results[w]
                if r['total'] > 0:
                    acc = r['correct'] / r['total'] * 100
                    if acc > best_acc:
                        best_acc = acc
                        best_window = w

            if best_window:
                r = results[best_window]
                avg_move = np.mean(r['avg_move']) * 100 if r['avg_move'] else 0
                fill_rate = r['fills'] / r['total'] * 100

                all_results.append({
                    'fast_ms': fast_ms,
                    'slow_ms': slow_ms,
                    'best_window': best_window,
                    'samples': r['total'],
                    'accuracy': best_acc,
                    'avg_move': avg_move,
                    'fill_rate': fill_rate,
                })
                print(f"  Best: {best_window}s window, {best_acc:.1f}% acc, {avg_move:+.2f}c move")

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY: Best config per accuracy")
    print("=" * 60)

    print(f"\n{'Fast':<8} {'Slow':<8} {'Window':<8} {'Samples':<10} {'Accuracy':<10} {'Move':<10} {'Fills':<10}")
    print("-" * 74)

    for r in sorted(all_results, key=lambda x: x['accuracy'], reverse=True):
        print(f"{r['fast_ms']:<8} {r['slow_ms']:<8} {r['best_window']}s{'':<5} "
              f"{r['samples']:<10} {r['accuracy']:>6.1f}%   {r['avg_move']:>+6.2f}c   {r['fill_rate']:>6.1f}%")

    # Best result
    if all_results:
        best = max(all_results, key=lambda x: x['accuracy'])
        print(f"\n✅ Best: fast={best['fast_ms']}ms, slow={best['slow_ms']}ms, window={best['best_window']}s")
        print(f"   Accuracy: {best['accuracy']:.1f}%, Avg move: {best['avg_move']:+.2f}c")

        if best['accuracy'] > 55:
            print("\n🎯 LATENCY EDGE EXISTS!")
        else:
            print("\n❌ No significant latency edge found.")

        # Detailed breakdown for best config
        print("\n" + "=" * 60)
        print(f"DETAILED: fast={best['fast_ms']}ms, slow={best['slow_ms']}ms")
        print("=" * 60)
        results = test_latency_edge(btc_df, obs_df, best['fast_ms'], best['slow_ms'])
        print(f"\n{'Window':<8} {'Samples':<10} {'Accuracy':<10} {'Avg Move':<12} {'Fill Rate':<10}")
        print("-" * 50)
        for w in LATENCY_WINDOWS:
            r = results[w]
            if r['total'] > 0:
                acc = r['correct'] / r['total'] * 100
                avg_move = np.mean(r['avg_move']) * 100 if r['avg_move'] else 0
                fill_rate = r['fills'] / r['total'] * 100
                print(f"{w}s{'':<6} {r['total']:<10} {acc:>6.1f}%   {avg_move:>+6.2f}c     {fill_rate:>6.1f}%")


if __name__ == "__main__":
    main()
