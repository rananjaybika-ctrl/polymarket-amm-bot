#!/usr/bin/env python3
"""
Direct comparison: EWMA crossover vs just trusting the market.

Question: Does BTC signal add predictive value, or should we just bet expensive wins?
"""

import pandas as pd
import numpy as np
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def main():
    print("=" * 60)
    print("SIGNAL vs MARKET TRUST")
    print("=" * 60)

    # Load data
    obs_path = Path("research/observer/PROTECTED_grid_obs_is_oos2_combined.csv")
    btc_path = Path("research/binance_hf/btc_prices_is_oos2_5hz.csv")

    print("\nLoading...")
    obs_df = pd.read_csv(obs_path, low_memory=False)
    btc_df = pd.read_csv(btc_path)

    # Compute EWMA crossover (best config: 100ms/1000ms)
    print("Computing EWMA crossover...")
    tick_ms = 16.67
    btc = btc_df.copy().sort_values('timestamp_ms').reset_index(drop=True)
    btc['ewma_fast'] = btc['price'].ewm(halflife=100/tick_ms).mean()
    btc['ewma_slow'] = btc['price'].ewm(halflife=1000/tick_ms).mean()
    btc['signal'] = np.where(btc['ewma_fast'] > btc['ewma_slow'], 'UP', 'DOWN')

    # Merge
    btc_for_merge = btc[['timestamp_ms', 'signal']].copy()
    obs = obs_df.sort_values('timestamp_ms').copy()
    obs = pd.merge_asof(obs, btc_for_merge, on='timestamp_ms', direction='nearest', tolerance=500)

    # Collect predictions
    results = []

    for slug in obs['market_slug'].unique():
        mdf = obs[obs['market_slug'] == slug].copy()
        mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

        if len(mdf) < 100:
            continue

        # Resolution
        last = mdf.iloc[-1]
        if last['up_bid'] > 0.9:
            resolution = 'UP'
        elif last['down_bid'] > 0.9:
            resolution = 'DOWN'
        else:
            continue

        # Sample at different expensive_ask thresholds
        for idx in range(0, len(mdf), 60):
            row = mdf.iloc[idx]
            up_ask, down_ask = row['up_ask'], row['down_ask']

            if pd.isna(up_ask) or pd.isna(down_ask):
                continue

            expensive_side = 'UP' if up_ask > down_ask else 'DOWN'
            expensive_ask = max(up_ask, down_ask)
            signal = row.get('signal')

            if pd.isna(signal):
                continue

            results.append({
                'expensive_ask': expensive_ask,
                'expensive_side': expensive_side,
                'signal': signal,
                'resolution': resolution,
                'market_correct': expensive_side == resolution,
                'signal_correct': signal == resolution,
                'signal_agrees': signal == expensive_side,
            })

    df = pd.DataFrame(results)
    print(f"\nTotal samples: {len(df):,}")

    # Compare by threshold
    print("\n" + "=" * 60)
    print("ACCURACY COMPARISON BY THRESHOLD")
    print("=" * 60)

    print(f"\n{'Threshold':<12} {'N':<8} {'Market':<12} {'Signal':<12} {'Difference':<12}")
    print("-" * 56)

    for thresh in [0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.95]:
        subset = df[df['expensive_ask'] >= thresh]
        if len(subset) < 50:
            continue

        market_acc = subset['market_correct'].mean() * 100
        signal_acc = subset['signal_correct'].mean() * 100
        diff = signal_acc - market_acc

        print(f">=${thresh:<10} {len(subset):<8} {market_acc:>6.1f}%     {signal_acc:>6.1f}%     {diff:>+6.1f}%")

    # When signal disagrees with market
    print("\n" + "=" * 60)
    print("WHEN SIGNAL DISAGREES WITH MARKET")
    print("=" * 60)

    disagree = df[df['signal_agrees'] == False]
    print(f"\nDisagreement cases: {len(disagree)} ({len(disagree)/len(df)*100:.1f}% of samples)")

    if len(disagree) > 0:
        market_right = disagree['market_correct'].mean() * 100
        signal_right = disagree['signal_correct'].mean() * 100

        print(f"\nWhen signal ≠ market:")
        print(f"  Market still right: {market_right:.1f}%")
        print(f"  Signal right:       {signal_right:.1f}%")

        print(f"\n{'Threshold':<12} {'N':<8} {'Market':<12} {'Signal':<12}")
        print("-" * 44)
        for thresh in [0.70, 0.80, 0.85, 0.90]:
            subset = disagree[disagree['expensive_ask'] >= thresh]
            if len(subset) < 10:
                continue
            m = subset['market_correct'].mean() * 100
            s = subset['signal_correct'].mean() * 100
            print(f">=${thresh:<10} {len(subset):<8} {m:>6.1f}%     {s:>6.1f}%")

    # Conclusion
    print("\n" + "=" * 60)
    print("CONCLUSION")
    print("=" * 60)

    high_thresh = df[df['expensive_ask'] >= 0.80]
    market_acc = high_thresh['market_correct'].mean() * 100
    signal_acc = high_thresh['signal_correct'].mean() * 100

    if abs(signal_acc - market_acc) < 2:
        print(f"""
At expensive_ask >= $0.80:
  Market accuracy: {market_acc:.1f}%
  Signal accuracy: {signal_acc:.1f}%

→ EWMA crossover adds NO predictive value.
→ Just trust the market: bet expensive side wins.
""")
    elif signal_acc > market_acc:
        print(f"""
At expensive_ask >= $0.80:
  Market accuracy: {market_acc:.1f}%
  Signal accuracy: {signal_acc:.1f}%

→ EWMA crossover IMPROVES prediction by {signal_acc - market_acc:.1f}%
""")
    else:
        print(f"""
At expensive_ask >= $0.80:
  Market accuracy: {market_acc:.1f}%
  Signal accuracy: {signal_acc:.1f}%

→ Market is MORE accurate than signal.
→ Trust the market.
""")


if __name__ == "__main__":
    main()
