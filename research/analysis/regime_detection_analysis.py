#!/usr/bin/env python3
"""
Regime Detection Analysis

Question: What differentiates OOS7 (77% accuracy) from IS+OOS2 (51-56% accuracy)?

Find observable metrics that could serve as "regime indicators" to know
WHEN the "expensive side = winner" heuristic will work.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from scipy import stats
import warnings
warnings.filterwarnings('ignore')

BASE_DIR = Path("/Users/rananjaybika/polymarket-amm-bot")


def load_data(period):
    """Load observer data for a period."""
    if period == "IS_OOS2":
        files = [
            "research/observer/grid_obs_20260116.csv",
            "research/observer/grid_obs_20260117.csv",
            "research/observer/grid_obs_20260118.csv",
            "research/observer/grid_obs_20260119.csv",
        ]
    else:  # OOS7
        files = [
            "research/observer/grid_obs_20260129.csv",
            "research/observer/grid_obs_20260130.csv",
        ]

    dfs = []
    for f in files:
        path = BASE_DIR / f
        if path.exists():
            df = pd.read_csv(path, low_memory=False)
            dfs.append(df)

    return pd.concat(dfs, ignore_index=True) if dfs else None


def load_resolutions():
    """Load market resolutions."""
    path = BASE_DIR / "research/observer/market_resolutions_verified.csv"
    if path.exists():
        df = pd.read_csv(path)
        return dict(zip(df['slug'], df['winner']))
    return {}


def compute_regime_metrics(df, resolutions, period_name):
    """Compute metrics that might indicate regime quality."""
    print(f"\n{'='*60}")
    print(f"REGIME METRICS: {period_name}")
    print(f"{'='*60}")

    # Add resolution
    df['winner'] = df['market_slug'].map(resolutions)
    df = df[df['winner'].isin(['UP', 'DOWN'])].copy()

    # Expensive side prediction
    df['expensive_side'] = np.where(df['up_ask'] > df['down_ask'], 'UP', 'DOWN')
    df['prediction_correct'] = (df['expensive_side'] == df['winner']).astype(int)

    # Overall accuracy
    accuracy = df['prediction_correct'].mean()
    print(f"\n  Overall 'expensive=winner' accuracy: {accuracy:.1%}")

    metrics = {'period': period_name, 'accuracy': accuracy}

    # ===== 1. PRICE CONVICTION =====
    # How far is expensive side from 50%?
    df['price_conviction'] = (df['up_ask'] - 0.5).abs()
    metrics['price_conviction_mean'] = df['price_conviction'].mean()
    metrics['price_conviction_std'] = df['price_conviction'].std()
    print(f"\n  Price Conviction:")
    print(f"    Mean: {df['price_conviction'].mean():.4f}")
    print(f"    Std: {df['price_conviction'].std():.4f}")

    # Accuracy by conviction level
    df['conviction_bucket'] = pd.cut(df['price_conviction'],
                                      bins=[0, 0.05, 0.10, 0.15, 0.20, 0.50],
                                      labels=['0-5%', '5-10%', '10-15%', '15-20%', '20%+'])
    conv_acc = df.groupby('conviction_bucket')['prediction_correct'].agg(['mean', 'count'])
    print(f"    Accuracy by conviction:")
    for bucket, row in conv_acc.iterrows():
        print(f"      {bucket}: {row['mean']:.1%} (n={row['count']:,})")

    # ===== 2. PAIR COST =====
    df['pair_cost'] = df['up_ask'] + df['down_ask']
    metrics['pair_cost_mean'] = df['pair_cost'].mean()
    metrics['pair_cost_std'] = df['pair_cost'].std()
    print(f"\n  Pair Cost:")
    print(f"    Mean: ${df['pair_cost'].mean():.4f}")
    print(f"    Std: ${df['pair_cost'].std():.4f}")

    # ===== 3. SPREAD =====
    df['up_spread'] = df['up_ask'] - df['up_bid']
    df['down_spread'] = df['down_ask'] - df['down_bid']
    df['total_spread'] = df['up_spread'] + df['down_spread']
    metrics['spread_mean'] = df['total_spread'].mean()
    print(f"\n  Spread:")
    print(f"    Mean total spread: ${df['total_spread'].mean():.4f}")

    # ===== 4. VELOCITY =====
    metrics['velocity_mean'] = df['velocity_bps'].mean()
    metrics['velocity_std'] = df['velocity_bps'].std()
    metrics['velocity_abs_mean'] = df['velocity_bps'].abs().mean()
    print(f"\n  Velocity:")
    print(f"    Mean: {df['velocity_bps'].mean():.4f} bps")
    print(f"    Std: {df['velocity_bps'].std():.4f} bps")
    print(f"    Abs Mean: {df['velocity_bps'].abs().mean():.4f} bps")

    # Velocity-price alignment
    df['vel_agrees'] = (
        ((df['velocity_bps'] > 0) & (df['expensive_side'] == 'UP')) |
        ((df['velocity_bps'] < 0) & (df['expensive_side'] == 'DOWN'))
    ).astype(int)
    metrics['velocity_alignment'] = df['vel_agrees'].mean()
    print(f"    Velocity-price alignment: {df['vel_agrees'].mean():.1%}")

    # ===== 5. TIME REMAINING =====
    metrics['time_remaining_mean'] = df['time_remaining_secs'].mean()
    print(f"\n  Time Remaining:")
    print(f"    Mean: {df['time_remaining_secs'].mean():.0f}s")

    # ===== 6. MARKET-LEVEL METRICS =====
    market_stats = df.groupby('market_slug').agg({
        'prediction_correct': 'mean',
        'price_conviction': 'mean',
        'pair_cost': 'mean',
        'velocity_bps': ['mean', 'std'],
    })
    market_stats.columns = ['accuracy', 'conviction', 'pair_cost', 'vel_mean', 'vel_std']

    metrics['markets_above_70pct'] = (market_stats['accuracy'] > 0.70).mean()
    metrics['markets_above_60pct'] = (market_stats['accuracy'] > 0.60).mean()
    print(f"\n  Market-Level:")
    print(f"    Markets with >70% accuracy: {(market_stats['accuracy'] > 0.70).mean():.1%}")
    print(f"    Markets with >60% accuracy: {(market_stats['accuracy'] > 0.60).mean():.1%}")

    # ===== 7. SPIKE DETECTION =====
    if 'spike_detected' in df.columns:
        spike_rate = df['spike_detected'].fillna(0).mean()
        metrics['spike_rate'] = spike_rate
        print(f"\n  Spike Rate: {spike_rate:.1%}")

    # ===== 8. CORRELATION: Conviction vs Accuracy =====
    # At market level
    corr = market_stats['conviction'].corr(market_stats['accuracy'])
    metrics['conviction_accuracy_corr'] = corr
    print(f"\n  Correlation (conviction vs accuracy): r={corr:.3f}")

    return metrics, df


def compare_regimes(metrics_good, metrics_bad):
    """Compare metrics between good and bad regimes."""
    print(f"\n{'='*60}")
    print("REGIME COMPARISON")
    print(f"{'='*60}")

    print(f"\n{'Metric':<30} {'IS+OOS2':>12} {'OOS7':>12} {'Diff':>12}")
    print("-"*70)

    key_metrics = [
        ('accuracy', 'Accuracy', '{:.1%}'),
        ('price_conviction_mean', 'Price Conviction', '{:.4f}'),
        ('pair_cost_mean', 'Pair Cost', '${:.4f}'),
        ('spread_mean', 'Total Spread', '${:.4f}'),
        ('velocity_abs_mean', 'Velocity (abs)', '{:.4f}'),
        ('velocity_alignment', 'Velocity Alignment', '{:.1%}'),
        ('markets_above_70pct', 'Markets >70%', '{:.1%}'),
        ('spike_rate', 'Spike Rate', '{:.1%}'),
        ('conviction_accuracy_corr', 'Conv-Acc Corr', '{:.3f}'),
    ]

    significant_diffs = []

    for key, name, fmt in key_metrics:
        if key in metrics_bad and key in metrics_good:
            bad_val = metrics_bad[key]
            good_val = metrics_good[key]
            diff = good_val - bad_val

            bad_str = fmt.format(bad_val)
            good_str = fmt.format(good_val)

            # Highlight significant differences
            if abs(diff) > 0.05 * abs(bad_val + 0.001):
                diff_str = f"{diff:+.4f} ***"
                significant_diffs.append((name, bad_val, good_val, diff))
            else:
                diff_str = f"{diff:+.4f}"

            print(f"{name:<30} {bad_str:>12} {good_str:>12} {diff_str:>12}")

    return significant_diffs


def analyze_accuracy_predictors(df_bad, df_good):
    """Find what predicts accuracy within each regime."""
    print(f"\n{'='*60}")
    print("ACCURACY PREDICTORS")
    print(f"{'='*60}")

    for name, df in [("IS+OOS2", df_bad), ("OOS7", df_good)]:
        print(f"\n--- {name} ---")

        # Market-level analysis
        market_stats = df.groupby('market_slug').agg({
            'prediction_correct': 'mean',
            'price_conviction': 'mean',
            'pair_cost': 'mean',
            'total_spread': 'mean',
            'velocity_bps': lambda x: x.abs().mean(),
            'vel_agrees': 'mean',
        })
        market_stats.columns = ['accuracy', 'conviction', 'pair_cost', 'spread', 'velocity', 'vel_align']

        print("\n  Correlation with market accuracy:")
        for col in ['conviction', 'pair_cost', 'spread', 'velocity', 'vel_align']:
            corr = market_stats[col].corr(market_stats['accuracy'])
            print(f"    {col:<15}: r={corr:+.3f}")


def find_regime_indicators(df_bad, df_good):
    """Find metrics that could serve as real-time regime indicators."""
    print(f"\n{'='*60}")
    print("POTENTIAL REGIME INDICATORS")
    print(f"{'='*60}")

    # We need metrics that:
    # 1. Are observable in real-time (before resolution)
    # 2. Differ significantly between regimes
    # 3. Correlate with accuracy

    indicators = []

    # 1. Price conviction distribution
    bad_conv = df_bad['price_conviction'].mean()
    good_conv = df_good['price_conviction'].mean()
    if good_conv > bad_conv * 1.1:
        indicators.append(f"Higher price conviction: {bad_conv:.3f} → {good_conv:.3f}")

    # 2. Pair cost tightness
    bad_pc = df_bad['pair_cost'].std()
    good_pc = df_good['pair_cost'].std()
    if good_pc < bad_pc * 0.9:
        indicators.append(f"Tighter pair cost variance: {bad_pc:.4f} → {good_pc:.4f}")

    # 3. Velocity patterns
    bad_vel = df_bad['velocity_bps'].abs().mean()
    good_vel = df_good['velocity_bps'].abs().mean()
    if abs(good_vel - bad_vel) > 0.01:
        indicators.append(f"Velocity magnitude: {bad_vel:.4f} → {good_vel:.4f}")

    # 4. Spread
    bad_spread = df_bad['total_spread'].mean()
    good_spread = df_good['total_spread'].mean()
    if good_spread < bad_spread * 0.9:
        indicators.append(f"Tighter spreads: {bad_spread:.4f} → {good_spread:.4f}")

    print("\n  Candidate Indicators:")
    for ind in indicators:
        print(f"    • {ind}")

    # Statistical tests
    print("\n  Statistical Tests (Mann-Whitney U):")
    test_cols = ['price_conviction', 'pair_cost', 'total_spread', 'velocity_bps']
    for col in test_cols:
        if col in df_bad.columns and col in df_good.columns:
            stat, pval = stats.mannwhitneyu(
                df_bad[col].dropna().sample(min(10000, len(df_bad))),
                df_good[col].dropna().sample(min(10000, len(df_good))),
                alternative='two-sided'
            )
            sig = "***" if pval < 0.001 else "**" if pval < 0.01 else "*" if pval < 0.05 else ""
            print(f"    {col:<20}: p={pval:.2e} {sig}")


def main():
    print("="*60)
    print("REGIME DETECTION ANALYSIS")
    print("What differentiates good regimes from bad regimes?")
    print("="*60)

    # Load data
    print("\nLoading data...")
    df_bad = load_data("IS_OOS2")
    df_good = load_data("OOS7")
    resolutions = load_resolutions()

    print(f"  IS+OOS2: {len(df_bad):,} observations")
    print(f"  OOS7: {len(df_good):,} observations")
    print(f"  Resolutions: {len(resolutions)}")

    # Compute metrics for each regime
    metrics_bad, df_bad = compute_regime_metrics(df_bad, resolutions, "IS+OOS2 (Bad)")
    metrics_good, df_good = compute_regime_metrics(df_good, resolutions, "OOS7 (Good)")

    # Add computed columns to both
    df_bad['total_spread'] = df_bad['up_spread'] + df_bad['down_spread']
    df_good['total_spread'] = df_good['up_spread'] + df_good['down_spread']

    # Compare regimes
    significant_diffs = compare_regimes(metrics_good, metrics_bad)

    # Analyze accuracy predictors
    analyze_accuracy_predictors(df_bad, df_good)

    # Find regime indicators
    find_regime_indicators(df_bad, df_good)

    # Summary
    print(f"\n{'='*60}")
    print("CONCLUSIONS")
    print("="*60)
    print("""
To detect "good" regimes, look for:

1. HIGHER PRICE CONVICTION
   - When expensive side is clearly expensive (not 51/49)
   - Track: |up_ask - 0.5| > threshold

2. VELOCITY-PRICE ALIGNMENT
   - When BTC momentum agrees with expensive side
   - Track: sign(velocity) matches expensive side

3. TIGHTER SPREADS
   - More liquid markets are more predictable
   - Track: (up_spread + down_spread) < threshold

4. MARKET MATURITY
   - Later in market lifecycle might be more predictable
   - Track: time_remaining < threshold

NEXT STEPS:
1. Build a "regime quality score" from these indicators
2. Backtest: only trade when score > threshold
3. Paper trade to validate in real-time
""")


if __name__ == "__main__":
    main()
