#!/usr/bin/env python3
"""
Test EWMA Fast vs Slow crossover as trend signal.

Signal: fast_ewma > slow_ewma → UP trend, expect UP +10c, DOWN -10c
        fast_ewma < slow_ewma → DOWN trend, expect DOWN +10c, UP -10c

Test various fast/slow combinations.
"""

import pandas as pd
import numpy as np
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# EWMA halflife options (in ms)
FAST_OPTIONS = [500, 1000, 2000]      # 0.5s, 1s, 2s
SLOW_OPTIONS = [5000, 10000, 20000]   # 5s, 10s, 20s

# Target move thresholds
TARGET_MOVE = 0.10  # 10 cents


def compute_ewma_crossover(btc_df: pd.DataFrame, fast_ms: int, slow_ms: int) -> pd.DataFrame:
    """Compute fast vs slow EWMA and crossover signal."""
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    # Convert ms halflife to ticks (assuming ~60Hz data = 16.67ms per tick)
    tick_ms = 16.67
    fast_ticks = fast_ms / tick_ms
    slow_ticks = slow_ms / tick_ms

    # Compute EWMAs
    df['ewma_fast'] = df['price'].ewm(halflife=fast_ticks).mean()
    df['ewma_slow'] = df['price'].ewm(halflife=slow_ticks).mean()

    # Crossover signal
    df['fast_above'] = df['ewma_fast'] > df['ewma_slow']
    df['signal'] = df['fast_above'].map({True: 'UP', False: 'DOWN'})

    # Crossover events (signal change)
    df['signal_prev'] = df['signal'].shift(1)
    df['crossover'] = df['signal'] != df['signal_prev']

    return df


def test_signal_accuracy(
    btc_df: pd.DataFrame,
    obs_df: pd.DataFrame,
    fast_ms: int,
    slow_ms: int,
) -> dict:
    """Test if crossover signal predicts ±10c moves."""

    # Compute crossover
    btc = compute_ewma_crossover(btc_df, fast_ms, slow_ms)

    # Merge with observer
    btc_for_merge = btc[['timestamp_ms', 'signal', 'crossover']].copy()
    obs = obs_df.sort_values('timestamp_ms').copy()
    obs = pd.merge_asof(
        obs,
        btc_for_merge,
        on='timestamp_ms',
        direction='nearest',
        tolerance=1000
    )

    # For each market, check if signal at entry predicts outcome
    results = []

    for slug in obs['market_slug'].unique():
        mdf = obs[obs['market_slug'] == slug].copy()
        mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

        if len(mdf) < 100:
            continue

        # Get expensive side and resolution
        first = mdf.iloc[0]
        last = mdf.iloc[-1]

        # Resolution from final prices
        if last['up_bid'] > 0.9:
            resolution = 'UP'
        elif last['down_bid'] > 0.9:
            resolution = 'DOWN'
        else:
            continue  # Skip unresolved

        # Find entry points where expensive_ask >= $0.80
        for idx in range(0, len(mdf), 60):  # Sample every 60 rows (~1s)
            row = mdf.iloc[idx]

            up_ask = row['up_ask']
            down_ask = row['down_ask']

            if pd.isna(up_ask) or pd.isna(down_ask):
                continue

            # Determine expensive side at this moment
            if up_ask > down_ask:
                expensive_side = 'UP'
                expensive_ask = up_ask
            else:
                expensive_side = 'DOWN'
                expensive_ask = down_ask

            if expensive_ask < 0.80:
                continue

            signal = row.get('signal')
            if pd.isna(signal):
                continue

            # Check if signal predicts resolution
            # UP signal → expect UP wins → expensive side should stay expensive if UP
            signal_correct = (signal == resolution)

            # Also check 10c move prediction
            # Look 10s ahead
            future_idx = min(idx + 600, len(mdf) - 1)  # ~10s at 60Hz
            future = mdf.iloc[future_idx]

            if expensive_side == 'UP':
                move_expensive = future['up_ask'] - up_ask if not pd.isna(future['up_ask']) else 0
                move_cheap = future['down_ask'] - down_ask if not pd.isna(future['down_ask']) else 0
            else:
                move_expensive = future['down_ask'] - down_ask if not pd.isna(future['down_ask']) else 0
                move_cheap = future['up_ask'] - up_ask if not pd.isna(future['up_ask']) else 0

            # Signal predicts: expensive +10c if signal matches, -10c if opposite
            if signal == expensive_side:
                # Signal says trend continues → expensive should go up
                move_prediction_correct = move_expensive > 0
            else:
                # Signal says trend reverses → expensive should go down
                move_prediction_correct = move_expensive < 0

            results.append({
                'slug': slug,
                'timestamp_ms': row['timestamp_ms'],
                'expensive_side': expensive_side,
                'expensive_ask': expensive_ask,
                'signal': signal,
                'resolution': resolution,
                'signal_matches_resolution': signal_correct,
                'move_expensive': move_expensive,
                'move_cheap': move_cheap,
                'move_prediction_correct': move_prediction_correct,
            })

    if not results:
        return {'fast_ms': fast_ms, 'slow_ms': slow_ms, 'n_samples': 0}

    df_results = pd.DataFrame(results)

    # Accuracy metrics
    resolution_acc = df_results['signal_matches_resolution'].mean() * 100
    move_acc = df_results['move_prediction_correct'].mean() * 100

    # When signal matches expensive side
    matches = df_results[df_results['signal'] == df_results['expensive_side']]
    opposite = df_results[df_results['signal'] != df_results['expensive_side']]

    return {
        'fast_ms': fast_ms,
        'slow_ms': slow_ms,
        'n_samples': len(df_results),
        'resolution_accuracy': resolution_acc,
        'move_accuracy': move_acc,
        'signal_matches_expensive': len(matches),
        'signal_opposite_expensive': len(opposite),
        'avg_move_when_match': matches['move_expensive'].mean() if len(matches) > 0 else 0,
        'avg_move_when_opposite': opposite['move_expensive'].mean() if len(opposite) > 0 else 0,
    }


def main():
    print("=" * 60)
    print("EWMA CROSSOVER SIGNAL TEST")
    print("=" * 60)
    print(f"Fast options: {FAST_OPTIONS} ms")
    print(f"Slow options: {SLOW_OPTIONS} ms")
    print(f"Target move: ±{TARGET_MOVE*100:.0f}c")

    # Load data
    obs_path = Path("research/observer/PROTECTED_grid_obs_is_oos2_combined.csv")
    btc_path = Path("research/binance_hf/btc_prices_is_oos2_5hz.csv")

    print(f"\nLoading...")
    obs_df = pd.read_csv(obs_path, low_memory=False)
    btc_df = pd.read_csv(btc_path)
    print(f"  Obs: {len(obs_df):,}, BTC: {len(btc_df):,}")

    # Test all combinations
    results = []
    for fast_ms in FAST_OPTIONS:
        for slow_ms in SLOW_OPTIONS:
            if fast_ms >= slow_ms:
                continue
            print(f"\nTesting fast={fast_ms}ms, slow={slow_ms}ms...")
            r = test_signal_accuracy(btc_df, obs_df, fast_ms, slow_ms)
            results.append(r)
            if r['n_samples'] > 0:
                print(f"  Samples: {r['n_samples']}")
                print(f"  Resolution accuracy: {r['resolution_accuracy']:.1f}%")
                print(f"  Move accuracy (10s): {r['move_accuracy']:.1f}%")

    # Summary table
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"\n{'Fast':<8} {'Slow':<8} {'N':<8} {'Res Acc':<10} {'Move Acc':<10} {'Avg Move (match)':<18}")
    print("-" * 70)

    for r in sorted(results, key=lambda x: x.get('resolution_accuracy', 0), reverse=True):
        if r['n_samples'] > 0:
            print(f"{r['fast_ms']:<8} {r['slow_ms']:<8} {r['n_samples']:<8} "
                  f"{r['resolution_accuracy']:>6.1f}%    {r['move_accuracy']:>6.1f}%    "
                  f"{r['avg_move_when_match']*100:>+6.2f}c")

    # Best config
    best = max([r for r in results if r['n_samples'] > 0],
               key=lambda x: x['resolution_accuracy'])
    print(f"\n✅ Best: fast={best['fast_ms']}ms, slow={best['slow_ms']}ms")
    print(f"   Resolution accuracy: {best['resolution_accuracy']:.1f}%")

    # Deep dive on best config
    print("\n" + "=" * 60)
    print("DEEP DIVE: FOLLOW vs FADE")
    print("=" * 60)
    deep_dive_signal(btc_df, obs_df, best['fast_ms'], best['slow_ms'])


def deep_dive_signal(btc_df, obs_df, fast_ms, slow_ms):
    """Analyze FOLLOW vs FADE cases separately."""
    btc = compute_ewma_crossover(btc_df, fast_ms, slow_ms)

    btc_for_merge = btc[['timestamp_ms', 'signal', 'crossover']].copy()
    obs = obs_df.sort_values('timestamp_ms').copy()
    obs = pd.merge_asof(
        obs,
        btc_for_merge,
        on='timestamp_ms',
        direction='nearest',
        tolerance=1000
    )

    follow_results = []  # signal matches expensive
    fade_results = []    # signal opposite expensive

    for slug in obs['market_slug'].unique():
        mdf = obs[obs['market_slug'] == slug].copy()
        mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

        if len(mdf) < 100:
            continue

        last = mdf.iloc[-1]
        if last['up_bid'] > 0.9:
            resolution = 'UP'
        elif last['down_bid'] > 0.9:
            resolution = 'DOWN'
        else:
            continue

        for idx in range(0, len(mdf), 60):
            row = mdf.iloc[idx]
            up_ask = row['up_ask']
            down_ask = row['down_ask']

            if pd.isna(up_ask) or pd.isna(down_ask):
                continue

            if up_ask > down_ask:
                expensive_side = 'UP'
                expensive_ask = up_ask
            else:
                expensive_side = 'DOWN'
                expensive_ask = down_ask

            if expensive_ask < 0.80:
                continue

            signal = row.get('signal')
            if pd.isna(signal):
                continue

            # FOLLOW: signal matches expensive → bet expensive wins
            # FADE: signal opposite expensive → bet expensive loses
            is_follow = (signal == expensive_side)
            bet_side = expensive_side if is_follow else ('DOWN' if expensive_side == 'UP' else 'UP')
            bet_correct = (bet_side == resolution)

            entry = {
                'expensive_ask': expensive_ask,
                'bet_side': bet_side,
                'resolution': resolution,
                'bet_correct': bet_correct,
            }

            if is_follow:
                follow_results.append(entry)
            else:
                fade_results.append(entry)

    # Analyze
    print(f"\nFOLLOW (signal = expensive side): {len(follow_results)} samples")
    if follow_results:
        df = pd.DataFrame(follow_results)
        acc = df['bet_correct'].mean() * 100
        print(f"  Accuracy: {acc:.1f}%")
        print(f"  → Bet expensive wins, correct {acc:.1f}% of time")

    print(f"\nFADE (signal ≠ expensive side): {len(fade_results)} samples")
    if fade_results:
        df = pd.DataFrame(fade_results)
        acc = df['bet_correct'].mean() * 100
        print(f"  Accuracy: {acc:.1f}%")
        print(f"  → Bet cheap wins, correct {acc:.1f}% of time")

    # By expensive_ask threshold
    print(f"\nFOLLOW by expensive_ask threshold:")
    if follow_results:
        df = pd.DataFrame(follow_results)
        for thresh in [0.80, 0.85, 0.90]:
            subset = df[df['expensive_ask'] >= thresh]
            if len(subset) > 10:
                acc = subset['bet_correct'].mean() * 100
                print(f"  >=${thresh}: {acc:.1f}% ({len(subset)} samples)")

    print(f"\nFADE by expensive_ask threshold:")
    if fade_results:
        df = pd.DataFrame(fade_results)
        for thresh in [0.80, 0.85, 0.90]:
            subset = df[df['expensive_ask'] >= thresh]
            if len(subset) > 10:
                acc = subset['bet_correct'].mean() * 100
                print(f"  >=${thresh}: {acc:.1f}% ({len(subset)} samples)")


if __name__ == "__main__":
    main()
