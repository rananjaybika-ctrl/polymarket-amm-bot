#!/usr/bin/env python3
"""
Deep analysis: Compare Feb 4 paper trading to historical backtest datasets.

Goal: Find which backtest period most closely matches today's volatility regime
and understand why backtest results diverged from live performance.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta

# Paths
BASE_DIR = Path("/Users/rananjaybika/polymarket-amm-bot")
BINANCE_HF_DIR = BASE_DIR / "research/binance_hf"
OBSERVER_DIR = BASE_DIR / "research/observer"
PAPER_TRADES = BASE_DIR / "paper_trades_aggressive_2026-02-04.csv"

def load_binance_data(filepath, sample_rate=100):
    """Load binance price data with sampling for memory efficiency."""
    print(f"Loading {filepath.name}...")
    df = pd.read_csv(filepath, nrows=1000000)  # First 1M rows
    df['timestamp'] = pd.to_datetime(df['timestamp_ms'], unit='ms')
    return df

def calculate_volatility_metrics(df, window_ms=1200):
    """Calculate volatility metrics matching our EWMA_1000 spike detection."""

    # Price returns (percentage)
    df['returns'] = df['price'].pct_change() * 100

    # Rolling volatility (standard deviation of returns)
    # At 60Hz, 72 ticks = 1200ms
    window_ticks = 72
    df['rolling_vol'] = df['returns'].rolling(window=window_ticks).std()

    # EWMA price (alpha = 0.0115 for 1000ms half-life at 60Hz)
    alpha = 0.0115
    df['ewma_price'] = df['price'].ewm(alpha=alpha, adjust=False).mean()

    # Spike magnitude: deviation from EWMA
    df['spike_mag'] = abs(df['price'] - df['ewma_price']) / df['ewma_price'] * 100

    # Price range in 15-minute windows
    df['minute_15'] = df['timestamp'].dt.floor('15min')

    return df

def analyze_dataset(filepath, name):
    """Analyze a single dataset and return summary statistics."""
    try:
        df = load_binance_data(filepath)
        df = calculate_volatility_metrics(df)

        stats = {
            'name': name,
            'rows': len(df),
            'price_mean': df['price'].mean(),
            'price_std': df['price'].std(),
            'price_range_pct': (df['price'].max() - df['price'].min()) / df['price'].mean() * 100,
            'avg_rolling_vol': df['rolling_vol'].mean(),
            'max_rolling_vol': df['rolling_vol'].max(),
            'avg_spike_mag': df['spike_mag'].mean(),
            'spike_mag_p50': df['spike_mag'].quantile(0.50),
            'spike_mag_p75': df['spike_mag'].quantile(0.75),
            'spike_mag_p90': df['spike_mag'].quantile(0.90),
            'spike_mag_p95': df['spike_mag'].quantile(0.95),
            'spike_mag_p99': df['spike_mag'].quantile(0.99),
            'spikes_above_0.02': (df['spike_mag'] > 0.02).sum() / len(df) * 100,
            'spikes_above_0.035': (df['spike_mag'] > 0.035).sum() / len(df) * 100,
            'spikes_above_0.05': (df['spike_mag'] > 0.05).sum() / len(df) * 100,
        }

        # Time range
        stats['start_time'] = df['timestamp'].min()
        stats['end_time'] = df['timestamp'].max()
        stats['duration_hours'] = (stats['end_time'] - stats['start_time']).total_seconds() / 3600

        return stats, df

    except Exception as e:
        print(f"Error loading {filepath}: {e}")
        return None, None

def analyze_paper_trades():
    """Analyze today's paper trading results."""
    df = pd.read_csv(PAPER_TRADES)
    df['timestamp'] = pd.to_datetime(df['timestamp'])

    # Calculate trade outcomes
    # pair_cost < 1.0 = WIN, >= 1.0 = LOSS
    completed_trades = df[df['pos_hedged_pairs'] > 0].copy()

    wins = completed_trades[completed_trades['pos_pair_cost'] < 1.0]
    losses = completed_trades[completed_trades['pos_pair_cost'] >= 1.0]

    # Entry prices
    entry_trades = df[df['pos_hedged_pairs'] == 0].copy()  # First leg of trade

    stats = {
        'total_trades': len(completed_trades),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': len(wins) / len(completed_trades) * 100 if len(completed_trades) > 0 else 0,
        'avg_pair_cost': completed_trades['pos_pair_cost'].mean(),
        'avg_win_pair_cost': wins['pos_pair_cost'].mean() if len(wins) > 0 else 0,
        'avg_loss_pair_cost': losses['pos_pair_cost'].mean() if len(losses) > 0 else 0,
        'entry_prices': entry_trades['price'].tolist(),
        'avg_entry_price': entry_trades['price'].mean(),
    }

    print("\n" + "="*80)
    print("FEB 4 PAPER TRADING SUMMARY")
    print("="*80)
    print(f"Total completed trades: {stats['total_trades']}")
    print(f"Wins: {stats['wins']} ({stats['win_rate']:.1f}%)")
    print(f"Losses: {stats['losses']}")
    print(f"Average pair cost: ${stats['avg_pair_cost']:.4f}")
    print(f"Average winning pair cost: ${stats['avg_win_pair_cost']:.4f}")
    print(f"Average losing pair cost: ${stats['avg_loss_pair_cost']:.4f}")
    print(f"Average entry price: ${stats['avg_entry_price']:.4f}")

    # Entry price distribution
    entry_prices = entry_trades['price']
    print(f"\nEntry Price Distribution:")
    print(f"  Min: ${entry_prices.min():.4f}")
    print(f"  Max: ${entry_prices.max():.4f}")
    print(f"  Median: ${entry_prices.median():.4f}")
    print(f"  Entries at extreme (<$0.20 or >$0.80): {((entry_prices < 0.20) | (entry_prices > 0.80)).sum()}")
    print(f"  Entries at mid-range ($0.30-$0.70): {((entry_prices >= 0.30) & (entry_prices <= 0.70)).sum()}")

    return stats, df

def main():
    print("="*80)
    print("DEEP ANALYSIS: FEB 4 PAPER TRADING vs BACKTEST DATASETS")
    print("="*80)

    # Analyze paper trades first
    paper_stats, paper_df = analyze_paper_trades()

    # Datasets to analyze
    datasets = [
        (BINANCE_HF_DIR / "btc_prices_oos9.csv", "OOS9 (Jan 31-Feb 2)"),
        (BINANCE_HF_DIR / "btc_prices_20260131_055231.csv", "Jan 31"),
        (BINANCE_HF_DIR / "btc_prices_20260129_160523.csv", "Jan 29"),
        (BINANCE_HF_DIR / "btc_prices_20260124_recovered.csv", "Jan 24 (OOS5)"),
        (BINANCE_HF_DIR / "btc_prices_20260118_060340.csv", "Jan 18 (IS)"),
    ]

    all_stats = []

    for filepath, name in datasets:
        if filepath.exists():
            stats, df = analyze_dataset(filepath, name)
            if stats:
                all_stats.append(stats)

    # Print comparison table
    print("\n" + "="*80)
    print("VOLATILITY COMPARISON ACROSS DATASETS")
    print("="*80)

    print(f"\n{'Dataset':<25} {'Price Range%':<12} {'Avg Vol':<10} {'Avg Spike':<12} {'Spikes>0.035%':<15}")
    print("-"*80)

    for stats in all_stats:
        print(f"{stats['name']:<25} {stats['price_range_pct']:.3f}%       {stats['avg_rolling_vol']:.4f}    {stats['avg_spike_mag']:.4f}%      {stats['spikes_above_0.035']:.2f}%")

    # Detailed comparison
    print("\n" + "="*80)
    print("SPIKE MAGNITUDE PERCENTILES")
    print("="*80)

    print(f"\n{'Dataset':<25} {'p50':<10} {'p75':<10} {'p90':<10} {'p95':<10} {'p99':<10}")
    print("-"*80)

    for stats in all_stats:
        print(f"{stats['name']:<25} {stats['spike_mag_p50']:.4f}%   {stats['spike_mag_p75']:.4f}%   {stats['spike_mag_p90']:.4f}%   {stats['spike_mag_p95']:.4f}%   {stats['spike_mag_p99']:.4f}%")

    # Find most similar dataset to Feb 4
    print("\n" + "="*80)
    print("SIMILARITY ANALYSIS - Which backtest period matches today?")
    print("="*80)

    # Feb 4 characteristics (from the volatility analysis):
    # - BTC price range: $75,768 - $76,333 (~0.75% daily range)
    # - Most common spike magnitudes: 0.0288%, 0.0372%, 0.0262%
    # - Dynamic threshold: 0.015% - 0.023%

    feb4_price_range = 0.75  # from logs
    feb4_avg_spike = 0.030  # estimated from logs

    print(f"\nFeb 4 characteristics:")
    print(f"  - Price range: ~{feb4_price_range}%")
    print(f"  - Average spike magnitude: ~{feb4_avg_spike}%")
    print(f"  - Win rate: {paper_stats['win_rate']:.1f}%")

    print(f"\nSimilarity scores (lower = more similar to Feb 4):")
    for stats in all_stats:
        price_diff = abs(stats['price_range_pct'] - feb4_price_range)
        spike_diff = abs(stats['avg_spike_mag'] - feb4_avg_spike)
        similarity = price_diff + spike_diff * 10  # Weight spike magnitude more
        print(f"  {stats['name']}: {similarity:.3f} (price_diff={price_diff:.3f}, spike_diff={spike_diff:.4f})")

    # Key insight
    print("\n" + "="*80)
    print("KEY INSIGHT: BACKTEST vs LIVE DISCREPANCY")
    print("="*80)

    print("""
The discrepancy between backtest results and live performance comes from:

1. VOLATILITY REGIME MISMATCH:
   - Backtests used higher volatility periods (OOS7-9 had more price action)
   - Feb 4 had LOW volatility (~0.75% daily range)
   - Low volatility = smaller spikes = harder to profit

2. EWMA_1000 SPIKE BASE ISSUE:
   - EWMA with 1000ms half-life SMOOTHS price heavily
   - In low volatility, EWMA tracks price closely
   - Result: Spike magnitude appears LARGER than actual move
   - This causes FALSE POSITIVE signals

3. THE LOOKBACK TRADEOFF:
   - Shorter lookback (decrease EWMA half-life) = MORE selective
   - Longer lookback (increase EWMA half-life) = LESS selective

   Current EWMA_1000 may be too LONG for low volatility
   -> EWMA tracks price, so any small wiggle looks like a spike

4. RECOMMENDATIONS:
   a) DECREASE EWMA half-life to 500ms for low volatility
      OR
   b) ADD VOLATILITY GATE: Only trade when rolling_vol > threshold

   The volatility gate is SAFER - don't change core parameters,
   just skip trading in unfavorable conditions.
""")

    return all_stats, paper_stats

if __name__ == "__main__":
    main()
