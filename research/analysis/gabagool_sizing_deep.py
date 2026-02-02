#!/usr/bin/env python3
"""
Deep dive into Gabagool's sizing - correlate with orderbook conditions.
"""

import json
import pandas as pd
import numpy as np
from pathlib import Path
from scipy import stats

BASE_DIR = Path("/Users/rananjaybika/polymarket-amm-bot")


def load_gabagool_trades():
    """Load main OOS7 trades."""
    path = BASE_DIR / "research/findings/data/gabagool_trades_oos7.json"
    with open(path) as f:
        data = json.load(f)
    return data if isinstance(data, list) else data.get('trades', [])


def load_observer_data():
    """Load observer data for OOS7 period."""
    files = [
        BASE_DIR / "research/observer/grid_obs_20260129.csv",
        BASE_DIR / "research/observer/grid_obs_20260130.csv",
    ]

    dfs = []
    for f in files:
        if f.exists():
            df = pd.read_csv(f, low_memory=False)
            dfs.append(df)
            print(f"Loaded: {f.name} ({len(df):,} rows)")

    if dfs:
        return pd.concat(dfs, ignore_index=True)
    return None


def analyze_sizing_by_price_detail(trades_df):
    """Detailed analysis of sizing by price."""
    print("\n" + "="*60)
    print("SIZING BY PRICE - DETAILED")
    print("="*60)

    # More granular price buckets
    trades_df['price_5c'] = (trades_df['price'] * 20).round() / 20  # 5-cent buckets

    price_stats = trades_df.groupby('price_5c').agg({
        'size': ['mean', 'std', 'count', 'sum'],
    }).round(2)
    price_stats.columns = ['mean_size', 'std_size', 'count', 'total_shares']
    price_stats = price_stats[price_stats['count'] >= 100]  # Min 100 trades

    print("\nSize by 5-cent price buckets:")
    print(price_stats.to_string())

    # Correlation: price vs size
    corr, pval = stats.pearsonr(trades_df['price'], trades_df['size'])
    print(f"\nCorrelation (price vs size): r={corr:.4f}, p={pval:.2e}")

    # Regression
    slope, intercept, r_value, p_value, std_err = stats.linregress(
        trades_df['price'], trades_df['size']
    )
    print(f"Regression: size = {slope:.2f} * price + {intercept:.2f}")
    print(f"  R²={r_value**2:.4f}, p={p_value:.2e}")

    return price_stats


def analyze_sizing_by_pair_cost(trades_df):
    """Check if sizing varies with pair cost."""
    print("\n" + "="*60)
    print("SIZING BY PAIR COST")
    print("="*60)

    # Calculate pair cost per market
    market_prices = trades_df.groupby(['market_slug', 'outcome'])['price'].mean().unstack()
    if 'Up' in market_prices.columns and 'Down' in market_prices.columns:
        market_prices['pair_cost'] = market_prices['Up'] + market_prices['Down']
    elif 'UP' in market_prices.columns and 'DOWN' in market_prices.columns:
        market_prices['pair_cost'] = market_prices['UP'] + market_prices['DOWN']
    else:
        print("Could not calculate pair cost - checking column names:")
        print(market_prices.columns.tolist())
        return

    # Get average size per market
    market_size = trades_df.groupby('market_slug')['size'].mean()

    # Merge
    combined = market_prices.join(market_size)
    combined = combined.dropna()

    if len(combined) < 10:
        print("Not enough markets with pair cost data")
        return

    print(f"\nMarkets analyzed: {len(combined)}")

    # Correlation
    corr, pval = stats.pearsonr(combined['pair_cost'], combined['size'])
    print(f"Correlation (pair_cost vs avg_size): r={corr:.4f}, p={pval:.2e}")

    # Buckets
    combined['pc_bucket'] = pd.cut(combined['pair_cost'],
                                   bins=[0.85, 0.98, 1.00, 1.02, 1.05, 1.25],
                                   labels=['<0.98', '0.98-1.00', '1.00-1.02', '1.02-1.05', '>1.05'])

    print("\nSize by pair cost bucket:")
    pc_stats = combined.groupby('pc_bucket')['size'].agg(['mean', 'std', 'count'])
    print(pc_stats.round(2).to_string())


def analyze_sizing_by_time_remaining(trades_df, obs_df):
    """Check if sizing varies with time remaining in market."""
    print("\n" + "="*60)
    print("SIZING BY TIME REMAINING")
    print("="*60)

    if obs_df is None:
        print("No observer data available")
        return

    # Get time_remaining per market from observer data
    market_time = obs_df.groupby('market_slug')['time_remaining_secs'].max().to_dict()

    trades_df['time_remaining'] = trades_df['market_slug'].map(market_time)
    trades_df = trades_df.dropna(subset=['time_remaining'])

    if len(trades_df) == 0:
        print("No matching time data")
        return

    # Buckets
    trades_df['time_bucket'] = pd.cut(trades_df['time_remaining'],
                                      bins=[0, 180, 360, 540, 720, 900, 1800],
                                      labels=['0-3m', '3-6m', '6-9m', '9-12m', '12-15m', '15-30m'])

    time_stats = trades_df.groupby('time_bucket')['size'].agg(['mean', 'std', 'count'])
    print("\nSize by time remaining:")
    print(time_stats.round(2).to_string())


def analyze_sizing_by_orderbook(trades_df, obs_df):
    """Check if sizing varies with orderbook conditions."""
    print("\n" + "="*60)
    print("SIZING BY ORDERBOOK CONDITIONS")
    print("="*60)

    if obs_df is None:
        print("No observer data available")
        return

    # Calculate OBI per market (average)
    if 'up_total_bid' in obs_df.columns and 'up_total_ask' in obs_df.columns:
        obs_df['up_obi'] = (obs_df['up_total_bid'] - obs_df['up_total_ask']) / \
                          (obs_df['up_total_bid'] + obs_df['up_total_ask'] + 0.001)

        market_obi = obs_df.groupby('market_slug')['up_obi'].mean().to_dict()
        trades_df['market_obi'] = trades_df['market_slug'].map(market_obi)

        valid = trades_df.dropna(subset=['market_obi'])
        if len(valid) > 100:
            corr, pval = stats.pearsonr(valid['market_obi'], valid['size'])
            print(f"\nCorrelation (OBI vs size): r={corr:.4f}, p={pval:.2e}")

            # OBI buckets
            valid['obi_bucket'] = pd.cut(valid['market_obi'],
                                         bins=[-1, -0.3, -0.1, 0.1, 0.3, 1],
                                         labels=['Strong DOWN', 'Weak DOWN', 'Neutral', 'Weak UP', 'Strong UP'])

            obi_stats = valid.groupby('obi_bucket')['size'].agg(['mean', 'std', 'count'])
            print("\nSize by OBI:")
            print(obi_stats.round(2).to_string())

    # Calculate volatility per market (using bid-ask spread)
    if 'up_ask' in obs_df.columns and 'up_bid' in obs_df.columns:
        obs_df['spread'] = obs_df['up_ask'] - obs_df['up_bid']
        market_spread = obs_df.groupby('market_slug')['spread'].mean().to_dict()
        trades_df['market_spread'] = trades_df['market_slug'].map(market_spread)

        valid = trades_df.dropna(subset=['market_spread'])
        if len(valid) > 100:
            corr, pval = stats.pearsonr(valid['market_spread'], valid['size'])
            print(f"\nCorrelation (Spread vs size): r={corr:.4f}, p={pval:.2e}")


def analyze_sizing_by_expensive_side(trades_df, obs_df):
    """Key question: Does Gabagool size UP when buying the expensive side?"""
    print("\n" + "="*60)
    print("SIZING: EXPENSIVE SIDE vs CHEAP SIDE")
    print("="*60)

    if obs_df is None:
        print("No observer data available")
        return

    # Get average up_ask and down_ask per market
    market_prices = obs_df.groupby('market_slug').agg({
        'up_ask': 'mean',
        'down_ask': 'mean'
    }).to_dict()

    trades_df['mkt_up_ask'] = trades_df['market_slug'].map(market_prices.get('up_ask', {}))
    trades_df['mkt_down_ask'] = trades_df['market_slug'].map(market_prices.get('down_ask', {}))

    trades_df = trades_df.dropna(subset=['mkt_up_ask', 'mkt_down_ask'])

    # Determine expensive side
    trades_df['expensive_side'] = np.where(
        trades_df['mkt_up_ask'] > trades_df['mkt_down_ask'], 'Up', 'Down'
    )

    # Normalize outcome column
    trades_df['outcome_norm'] = trades_df['outcome'].str.title()

    # Check if trade is on expensive side
    trades_df['is_expensive_side'] = trades_df['outcome_norm'] == trades_df['expensive_side']

    exp_stats = trades_df.groupby('is_expensive_side')['size'].agg(['mean', 'std', 'count', 'sum'])
    exp_stats.index = ['Cheap Side', 'Expensive Side']

    print("\nSize by side type:")
    print(exp_stats.round(2).to_string())

    # Statistical test
    expensive = trades_df[trades_df['is_expensive_side']]['size']
    cheap = trades_df[~trades_df['is_expensive_side']]['size']

    t_stat, p_val = stats.ttest_ind(expensive, cheap)
    print(f"\nT-test (expensive vs cheap side):")
    print(f"  t={t_stat:.3f}, p={p_val:.4f}")

    if p_val < 0.05:
        if expensive.mean() > cheap.mean():
            print("  SIGNIFICANT: Gabagool sizes UP on expensive side!")
        else:
            print("  SIGNIFICANT: Gabagool sizes DOWN on expensive side!")
    else:
        print("  NOT SIGNIFICANT: No size difference by side type")


def main():
    print("="*60)
    print("GABAGOOL SIZING DEEP DIVE")
    print("="*60)

    # Load data
    trades = load_gabagool_trades()
    trades_df = pd.DataFrame(trades)
    print(f"\nLoaded {len(trades_df):,} trades")

    obs_df = load_observer_data()

    # Analyses
    analyze_sizing_by_price_detail(trades_df)
    analyze_sizing_by_pair_cost(trades_df)
    analyze_sizing_by_time_remaining(trades_df.copy(), obs_df)
    analyze_sizing_by_orderbook(trades_df.copy(), obs_df)
    analyze_sizing_by_expensive_side(trades_df.copy(), obs_df)

    print("\n" + "="*60)
    print("CONCLUSIONS")
    print("="*60)


if __name__ == "__main__":
    main()
