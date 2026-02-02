#!/usr/bin/env python3
"""
Gabagool Sizing Analysis

Analyze how Gabagool varies his position sizing across:
- Different markets
- Different price levels
- Different times
- Different market conditions

Question: Does he use the same size everywhere, or is there intelligent sizing?
"""

import json
import pandas as pd
import numpy as np
from pathlib import Path
from collections import defaultdict

BASE_DIR = Path("/Users/rananjaybika/polymarket-amm-bot")

def load_all_gabagool_data():
    """Load all available Gabagool trade data from multiple sources."""
    all_trades = []
    sources = {}

    # 1. Main OOS7 trades (63,293 trades)
    oos7_path = BASE_DIR / "research/findings/data/gabagool_trades_oos7.json"
    if oos7_path.exists():
        with open(oos7_path) as f:
            data = json.load(f)
        trades = data if isinstance(data, list) else data.get('trades', [])
        for t in trades:
            t['source'] = 'oos7'
        all_trades.extend(trades)
        sources['oos7'] = len(trades)

    # 2. Earlier trade samples
    early_path = BASE_DIR / "research/gabagool_earliest_trades_20260110_155142.csv"
    if early_path.exists():
        df = pd.read_csv(early_path)
        for _, row in df.iterrows():
            all_trades.append({
                'size': row.get('size', row.get('amount', 0)),
                'price': row.get('price', 0),
                'outcome': row.get('outcome', row.get('side', '')),
                'market_slug': row.get('market_slug', row.get('slug', '')),
                'timestamp': row.get('timestamp', 0),
                'source': 'early_jan10'
            })
        sources['early_jan10'] = len(df)

    # 3. Live capture fills
    fills_path = BASE_DIR / "research/live_capture/gabagool_btc_fills_20260111_121501.csv"
    if fills_path.exists():
        df = pd.read_csv(fills_path)
        for _, row in df.iterrows():
            all_trades.append({
                'size': row.get('size', row.get('amount', 0)),
                'price': row.get('price', 0),
                'outcome': row.get('outcome', row.get('side', '')),
                'market_slug': row.get('market_slug', row.get('slug', '')),
                'timestamp': row.get('timestamp', 0),
                'source': 'live_jan11'
            })
        sources['live_jan11'] = len(df)

    # 4. Deep dive data
    deep_path = BASE_DIR / "research/findings/data/deep_dive_gabagool.json"
    if deep_path.exists():
        with open(deep_path) as f:
            deep_data = json.load(f)
        # Extract any trade-level data if available
        if 'trades' in deep_data:
            for t in deep_data['trades']:
                t['source'] = 'deep_dive'
            all_trades.extend(deep_data['trades'])
            sources['deep_dive'] = len(deep_data['trades'])

    # 5. Scripts folder data
    for script_file in ['gabagool_trades.json', 'gabagool_btc_930pm.json',
                        'gabagool_10pm.json', 'gabagool_945pm.json']:
        script_path = BASE_DIR / f"scripts/{script_file}"
        if script_path.exists():
            try:
                with open(script_path) as f:
                    data = json.load(f)
                trades = data if isinstance(data, list) else data.get('trades', [])
                for t in trades:
                    t['source'] = script_file
                all_trades.extend(trades)
                sources[script_file] = len(trades)
            except:
                pass

    print(f"\n{'='*60}")
    print("GABAGOOL DATA SOURCES")
    print(f"{'='*60}")
    for src, count in sources.items():
        print(f"  {src}: {count:,} trades")
    print(f"  TOTAL: {len(all_trades):,} trades")

    return all_trades, sources


def analyze_sizing_patterns(trades):
    """Analyze Gabagool's sizing patterns."""

    if not trades:
        print("No trades to analyze")
        return

    # Convert to DataFrame
    df = pd.DataFrame(trades)

    # Clean up columns
    if 'size' not in df.columns and 'amount' in df.columns:
        df['size'] = df['amount']

    # Filter to valid sizes
    df = df[df['size'].notna() & (df['size'] > 0)]

    print(f"\n{'='*60}")
    print("SIZING ANALYSIS")
    print(f"{'='*60}")

    # 1. Overall size distribution
    print(f"\n--- Overall Size Distribution ---")
    print(f"  Total trades: {len(df):,}")
    print(f"  Mean size: {df['size'].mean():.2f}")
    print(f"  Median size: {df['size'].median():.2f}")
    print(f"  Std size: {df['size'].std():.2f}")
    print(f"  Min: {df['size'].min():.2f}")
    print(f"  Max: {df['size'].max():.2f}")

    # Size percentiles
    percentiles = [10, 25, 50, 75, 90, 95, 99]
    print(f"\n  Percentiles:")
    for p in percentiles:
        val = df['size'].quantile(p/100)
        print(f"    p{p}: {val:.2f}")

    # 2. Size by market
    if 'market_slug' in df.columns:
        print(f"\n--- Size By Market ---")
        market_stats = df.groupby('market_slug')['size'].agg(['mean', 'std', 'count', 'sum'])
        market_stats = market_stats.sort_values('count', ascending=False)

        print(f"  Total markets: {len(market_stats)}")
        print(f"\n  Top 10 markets by trade count:")
        for slug, row in market_stats.head(10).iterrows():
            print(f"    {slug[:40]:<40} | n={row['count']:>5.0f} | mean={row['mean']:>6.1f} | std={row['std']:>6.1f}")

        # Check if size varies by market
        market_means = market_stats['mean']
        print(f"\n  Size variance across markets:")
        print(f"    Mean of market means: {market_means.mean():.2f}")
        print(f"    Std of market means: {market_means.std():.2f}")
        print(f"    CV of market means: {market_means.std()/market_means.mean():.3f}")

        # Markets with notably different sizing
        low_size_markets = market_stats[market_stats['mean'] < market_means.mean() - market_means.std()]
        high_size_markets = market_stats[market_stats['mean'] > market_means.mean() + market_means.std()]
        print(f"    Markets with LOW sizing: {len(low_size_markets)}")
        print(f"    Markets with HIGH sizing: {len(high_size_markets)}")

    # 3. Size by price level
    if 'price' in df.columns:
        print(f"\n--- Size By Price Level ---")
        df['price_bucket'] = pd.cut(df['price'], bins=[0, 0.2, 0.35, 0.5, 0.65, 0.8, 1.0],
                                    labels=['0-20¢', '20-35¢', '35-50¢', '50-65¢', '65-80¢', '80-100¢'])

        price_stats = df.groupby('price_bucket')['size'].agg(['mean', 'std', 'count'])
        print(f"\n  Size by price bucket:")
        for bucket, row in price_stats.iterrows():
            print(f"    {bucket:<10} | n={row['count']:>6.0f} | mean={row['mean']:>6.1f} | std={row['std']:>5.1f}")

    # 4. Size by outcome (UP vs DOWN)
    if 'outcome' in df.columns:
        print(f"\n--- Size By Outcome (UP vs DOWN) ---")
        outcome_stats = df.groupby('outcome')['size'].agg(['mean', 'std', 'count', 'sum'])
        for outcome, row in outcome_stats.iterrows():
            print(f"    {outcome:<10} | n={row['count']:>6.0f} | mean={row['mean']:>6.1f} | total={row['sum']:>10.0f}")

    # 5. Size clustering - are there preferred sizes?
    print(f"\n--- Size Clustering ---")
    size_counts = df['size'].value_counts().head(20)
    print(f"  Top 20 most common sizes:")
    for size, count in size_counts.items():
        pct = count / len(df) * 100
        print(f"    {size:>8.2f}: {count:>6} ({pct:>5.1f}%)")

    # Check for round number preference
    df['is_round'] = df['size'].apply(lambda x: x == int(x) or x % 5 == 0 or x % 10 == 0)
    round_pct = df['is_round'].mean() * 100
    print(f"\n  Round number trades: {round_pct:.1f}%")

    # 6. Size over time (if timestamp available)
    if 'timestamp' in df.columns and df['timestamp'].notna().any():
        print(f"\n--- Size Over Time ---")
        df_time = df[df['timestamp'].notna()].copy()
        if len(df_time) > 0:
            df_time['timestamp'] = pd.to_numeric(df_time['timestamp'], errors='coerce')
            df_time = df_time.sort_values('timestamp')

            # Split into quintiles by time
            df_time['time_quintile'] = pd.qcut(df_time['timestamp'], 5, labels=['Early', 'Q2', 'Mid', 'Q4', 'Late'])
            time_stats = df_time.groupby('time_quintile')['size'].agg(['mean', 'std', 'count'])
            print(f"  Size by time period:")
            for period, row in time_stats.iterrows():
                print(f"    {period:<8} | n={row['count']:>6.0f} | mean={row['mean']:>6.1f}")

    # 7. Size within market - does he scale up/down during market?
    if 'market_slug' in df.columns and 'timestamp' in df.columns:
        print(f"\n--- Size Progression Within Markets ---")

        # For each market, compare first half vs second half sizing
        progression_results = []
        for slug, mdf in df.groupby('market_slug'):
            if len(mdf) < 10:
                continue
            mdf = mdf.sort_values('timestamp')
            mid = len(mdf) // 2
            first_half_mean = mdf.iloc[:mid]['size'].mean()
            second_half_mean = mdf.iloc[mid:]['size'].mean()
            change = (second_half_mean - first_half_mean) / first_half_mean if first_half_mean > 0 else 0
            progression_results.append({
                'slug': slug,
                'first_half': first_half_mean,
                'second_half': second_half_mean,
                'change_pct': change * 100
            })

        if progression_results:
            prog_df = pd.DataFrame(progression_results)
            print(f"  Markets analyzed: {len(prog_df)}")
            print(f"  Mean change (2nd half vs 1st half): {prog_df['change_pct'].mean():.1f}%")
            print(f"  Markets where size INCREASED: {(prog_df['change_pct'] > 10).sum()}")
            print(f"  Markets where size DECREASED: {(prog_df['change_pct'] < -10).sum()}")
            print(f"  Markets with stable sizing: {((prog_df['change_pct'] >= -10) & (prog_df['change_pct'] <= 10)).sum()}")

    return df


def analyze_sizing_vs_market_conditions(trades):
    """Check if sizing correlates with market conditions (if we have observer data)."""

    # Try to load observer data for cross-reference
    obs_files = [
        BASE_DIR / "research/findings/data/grid_obs_20260129.csv",
        BASE_DIR / "research/observer/grid_obs_20260129.csv",
        BASE_DIR / "research/observer/grid_obs_20260130.csv",
    ]

    obs_dfs = []
    for f in obs_files:
        if f.exists():
            try:
                df = pd.read_csv(f, low_memory=False)
                obs_dfs.append(df)
                print(f"  Loaded observer: {f.name} ({len(df):,} rows)")
            except:
                pass

    if not obs_dfs:
        print("\n  No observer data found for cross-reference")
        return

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    print(f"  Combined observer: {len(obs_df):,} rows")

    # Convert trades to DataFrame
    trades_df = pd.DataFrame(trades)
    if 'timestamp' not in trades_df.columns:
        print("  No timestamps in trade data for cross-reference")
        return

    # Try to match trades to observer data
    # This would require careful timestamp alignment
    print("\n  (Cross-reference analysis requires timestamp alignment - TODO)")


def main():
    print("="*60)
    print("GABAGOOL SIZING PATTERN ANALYSIS")
    print("="*60)

    # Load all data
    trades, sources = load_all_gabagool_data()

    if not trades:
        print("No trades loaded!")
        return

    # Analyze sizing patterns
    df = analyze_sizing_patterns(trades)

    # Check vs market conditions
    analyze_sizing_vs_market_conditions(trades)

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")

    if df is not None and len(df) > 0:
        cv = df['size'].std() / df['size'].mean()
        if cv < 0.3:
            print("  SIZING: UNIFORM (low variance)")
            print(f"  Gabagool uses roughly the same size across all trades")
        elif cv < 0.6:
            print("  SIZING: MODERATELY VARIED")
            print(f"  Some variation, but not strongly condition-dependent")
        else:
            print("  SIZING: HIGHLY VARIED")
            print(f"  Sizing appears to depend on market conditions")

        print(f"\n  Coefficient of Variation: {cv:.3f}")
        print(f"  Typical size: {df['size'].median():.1f} shares")


if __name__ == "__main__":
    main()
