"""
Order Book Imbalance (OBI) Alpha Analysis

Analyzes whether orderbook imbalance predicts short-term price movement on Polymarket.

Research Questions:
1. Does OBI predict next-N-tick price direction?
2. What is the optimal lookback/depth for OBI calculation?
3. How does OBI combine with existing velocity signals?

Data Requirements:
- Observer data with depth columns: up_bid_1-5, up_bid_size_1-5, up_ask_1-5, up_ask_size_1-5
- Imbalance columns: up_imbalance, down_imbalance

Usage:
    python research/analyze_obi_alpha.py --input research/observer/grid_obs_20260127.csv

Author: Research Team
Date: January 28, 2026
Context: Polymarket BTC 15-minute prediction markets
Reference: https://hftbacktest.readthedocs.io/en/latest/tutorials/Market%20Making%20with%20Alpha%20-%20Order%20Book%20Imbalance.html
"""

import pandas as pd
import numpy as np
from pathlib import Path
import argparse
from typing import Tuple, Dict, List, Optional
from dataclasses import dataclass
import warnings
warnings.filterwarnings('ignore')


@dataclass
class OBIConfig:
    """Configuration for OBI analysis."""
    depth_levels: int = 5           # Number of orderbook levels to use
    prediction_horizons: List[int] = None  # Ticks ahead to predict
    imbalance_thresholds: List[float] = None  # Threshold values to test

    def __post_init__(self):
        if self.prediction_horizons is None:
            self.prediction_horizons = [1, 5, 10, 25, 50, 100]  # ~0.2s to 20s at 200ms sampling
        if self.imbalance_thresholds is None:
            self.imbalance_thresholds = [0.1, 0.2, 0.3, 0.4, 0.5]


def load_observer_data(filepath: str) -> pd.DataFrame:
    """Load observer CSV with depth columns."""
    df = pd.read_csv(filepath)

    # Check required columns (timestamp can be timestamp or timestamp_ms)
    required_base = ['up_bid', 'up_ask', 'down_bid', 'down_ask']
    depth_cols = [f'up_bid_{i}' for i in range(1, 6)] + \
                 [f'up_bid_size_{i}' for i in range(1, 6)] + \
                 [f'up_ask_{i}' for i in range(1, 6)] + \
                 [f'up_ask_size_{i}' for i in range(1, 6)]

    missing = [c for c in required_base if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    depth_missing = [c for c in depth_cols if c not in df.columns]
    if depth_missing:
        print(f"Warning: Missing depth columns: {depth_missing[:5]}... ({len(depth_missing)} total)")
        print("Will use pre-computed imbalance if available")

    # Parse timestamps
    if 'timestamp_ms' in df.columns:
        df['timestamp'] = pd.to_datetime(df['timestamp_ms'], unit='ms')
        df = df.sort_values('timestamp').reset_index(drop=True)
    elif 'timestamp' in df.columns:
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('timestamp').reset_index(drop=True)

    return df


def compute_imbalance(df: pd.DataFrame, levels: int = 5, side: str = 'up') -> pd.Series:
    """
    Compute orderbook imbalance from depth data.

    Imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
    Range: -1 (all asks) to +1 (all bids)

    Positive imbalance -> buying pressure -> price likely to rise
    """
    bid_cols = [f'{side}_bid_size_{i}' for i in range(1, levels + 1)]
    ask_cols = [f'{side}_ask_size_{i}' for i in range(1, levels + 1)]

    # Check if columns exist
    bid_exists = all(c in df.columns for c in bid_cols)
    ask_exists = all(c in df.columns for c in ask_cols)

    if not (bid_exists and ask_exists):
        # Try pre-computed column
        imb_col = f'{side}_imbalance'
        if imb_col in df.columns:
            return df[imb_col]
        else:
            raise ValueError(f"Cannot compute imbalance: missing columns for {side}")

    bid_depth = df[bid_cols].sum(axis=1)
    ask_depth = df[ask_cols].sum(axis=1)

    total = bid_depth + ask_depth
    imbalance = (bid_depth - ask_depth) / total.replace(0, np.nan)

    return imbalance.fillna(0)


def compute_mid_price(df: pd.DataFrame, side: str = 'up') -> pd.Series:
    """Compute mid price from best bid/ask."""
    bid_col = f'{side}_bid' if f'{side}_bid' in df.columns else f'{side}_bid_1'
    ask_col = f'{side}_ask' if f'{side}_ask' in df.columns else f'{side}_ask_1'

    return (df[bid_col] + df[ask_col]) / 2


def compute_price_change(mid: pd.Series, horizon: int) -> pd.Series:
    """Compute price change over horizon ticks."""
    return mid.shift(-horizon) - mid


def compute_price_direction(mid: pd.Series, horizon: int) -> pd.Series:
    """Compute price direction: +1 (up), -1 (down), 0 (unchanged)."""
    change = compute_price_change(mid, horizon)
    return np.sign(change)


def analyze_imbalance_predictive_power(
    imbalance: pd.Series,
    mid: pd.Series,
    horizons: List[int],
    thresholds: List[float]
) -> pd.DataFrame:
    """
    Analyze how well imbalance predicts future price direction.

    Returns DataFrame with:
    - horizon: prediction horizon in ticks
    - threshold: imbalance threshold used
    - n_signals: number of times threshold exceeded
    - accuracy: % of correct direction predictions
    - avg_return: average return per signal (in price units)
    """
    results = []

    for horizon in horizons:
        direction = compute_price_direction(mid, horizon)
        price_change = compute_price_change(mid, horizon)

        for threshold in thresholds:
            # Long signal: imbalance > threshold
            long_mask = imbalance > threshold
            long_correct = (direction[long_mask] > 0).sum()
            long_total = long_mask.sum()
            long_returns = price_change[long_mask].mean() if long_total > 0 else 0

            # Short signal: imbalance < -threshold
            short_mask = imbalance < -threshold
            short_correct = (direction[short_mask] < 0).sum()
            short_total = short_mask.sum()
            short_returns = -price_change[short_mask].mean() if short_total > 0 else 0

            # Combined
            total_signals = long_total + short_total
            total_correct = long_correct + short_correct
            accuracy = total_correct / total_signals if total_signals > 0 else 0
            avg_return = (long_returns * long_total + short_returns * short_total) / total_signals if total_signals > 0 else 0

            results.append({
                'horizon': horizon,
                'threshold': threshold,
                'n_signals': total_signals,
                'long_signals': long_total,
                'short_signals': short_total,
                'accuracy': accuracy,
                'avg_return': avg_return,
                'long_accuracy': long_correct / long_total if long_total > 0 else 0,
                'short_accuracy': short_correct / short_total if short_total > 0 else 0,
            })

    return pd.DataFrame(results)


def analyze_imbalance_correlation(
    imbalance: pd.Series,
    mid: pd.Series,
    horizons: List[int]
) -> pd.DataFrame:
    """
    Compute correlation between imbalance and future price change.
    """
    results = []

    for horizon in horizons:
        price_change = compute_price_change(mid, horizon)

        # Remove NaN
        valid = ~(imbalance.isna() | price_change.isna())

        if valid.sum() > 100:
            corr = imbalance[valid].corr(price_change[valid])

            results.append({
                'horizon': horizon,
                'correlation': corr,
                'n_samples': valid.sum()
            })

    return pd.DataFrame(results)


def analyze_by_market(
    df: pd.DataFrame,
    config: OBIConfig
) -> Dict[str, pd.DataFrame]:
    """
    Analyze OBI predictive power grouped by market.
    """
    if 'market_id' not in df.columns and 'condition_id' not in df.columns:
        print("No market grouping column found, analyzing all data together")
        return {'all': analyze_single_market(df, config)}

    group_col = 'market_id' if 'market_id' in df.columns else 'condition_id'
    results = {}

    for market_id, group in df.groupby(group_col):
        if len(group) < 100:
            continue
        try:
            results[market_id] = analyze_single_market(group, config)
        except Exception as e:
            print(f"Error analyzing market {market_id}: {e}")

    return results


def analyze_single_market(df: pd.DataFrame, config: OBIConfig) -> Dict:
    """Analyze OBI for a single market/dataset."""
    # Compute imbalance
    try:
        up_imbalance = compute_imbalance(df, levels=config.depth_levels, side='up')
    except ValueError as e:
        print(f"Cannot compute imbalance: {e}")
        return {}

    # Compute mid price
    try:
        up_mid = compute_mid_price(df, side='up')
    except Exception as e:
        print(f"Cannot compute mid price: {e}")
        return {}

    # Predictive power analysis
    predictive = analyze_imbalance_predictive_power(
        up_imbalance, up_mid,
        config.prediction_horizons,
        config.imbalance_thresholds
    )

    # Correlation analysis
    correlation = analyze_imbalance_correlation(
        up_imbalance, up_mid,
        config.prediction_horizons
    )

    # Summary stats
    stats = {
        'n_rows': len(df),
        'imbalance_mean': up_imbalance.mean(),
        'imbalance_std': up_imbalance.std(),
        'imbalance_skew': up_imbalance.skew(),
        'pct_positive': (up_imbalance > 0).mean(),
        'pct_strong_positive': (up_imbalance > 0.3).mean(),
        'pct_strong_negative': (up_imbalance < -0.3).mean(),
    }

    return {
        'predictive': predictive,
        'correlation': correlation,
        'stats': stats
    }


def analyze_obi_with_spike(df: pd.DataFrame, config: OBIConfig) -> Dict:
    """
    Analyze OBI combined with spike detection.

    Key question: Does OBI improve AGGRESSIVE's spike accuracy?
    """
    results = {}

    if 'spike_detected' not in df.columns:
        print("No spike_detected column - skipping spike analysis")
        return results

    # Get spikes only
    spikes = df[df['spike_detected'] == True].copy()
    n_spikes = len(spikes)

    if n_spikes < 20:
        print(f"Insufficient spikes for analysis: {n_spikes}")
        return results

    print(f"\n--- OBI + SPIKE ANALYSIS ({n_spikes} spikes) ---")

    # Check if imbalance columns exist
    if 'up_imbalance' not in df.columns:
        print("No imbalance column - skipping")
        return results

    # Compute OBI confirmation
    # UP spike + positive UP imbalance = OBI confirms
    # DOWN spike + positive DOWN imbalance = OBI confirms
    def obi_confirms(row):
        if row['spike_direction'] == 'UP':
            return row.get('up_imbalance', 0) > 0
        elif row['spike_direction'] == 'DOWN':
            return row.get('down_imbalance', 0) > 0
        return False

    spikes['obi_confirms'] = spikes.apply(obi_confirms, axis=1)

    obi_agrees = spikes[spikes['obi_confirms'] == True]
    obi_disagrees = spikes[spikes['obi_confirms'] == False]

    print(f"  Spikes where OBI confirms: {len(obi_agrees)} ({len(obi_agrees)/n_spikes:.1%})")
    print(f"  Spikes where OBI disagrees: {len(obi_disagrees)} ({len(obi_disagrees)/n_spikes:.1%})")

    results['n_spikes'] = n_spikes
    results['obi_confirms_rate'] = len(obi_agrees) / n_spikes if n_spikes > 0 else 0

    # Compute mid prices for direction check
    spikes['up_mid'] = (spikes['up_bid'] + spikes['up_ask']) / 2
    spikes['down_mid'] = (spikes['down_bid'] + spikes['down_ask']) / 2

    # Future direction (need to compute from original df)
    for horizon in [10, 30, 60]:
        # Get future prices from original df
        up_future = df['up_bid'].shift(-horizon) + df['up_ask'].shift(-horizon)
        up_future = up_future / 2
        down_future = df['down_bid'].shift(-horizon) + df['down_ask'].shift(-horizon)
        down_future = down_future / 2

        df[f'up_direction_{horizon}'] = np.sign(up_future - (df['up_bid'] + df['up_ask'])/2)
        df[f'down_direction_{horizon}'] = np.sign(down_future - (df['down_bid'] + df['down_ask'])/2)

    # Re-get spikes with direction columns
    spikes = df[df['spike_detected'] == True].copy()
    spikes['obi_confirms'] = spikes.apply(obi_confirms, axis=1)
    obi_agrees = spikes[spikes['obi_confirms'] == True]
    obi_disagrees = spikes[spikes['obi_confirms'] == False]

    print(f"\n{'Filter':<25} {'10-tick':<12} {'30-tick':<12} {'60-tick':<12} {'Count':<8}")
    print("-" * 70)

    # All spikes accuracy
    for label, subset in [('All spikes', spikes), ('OBI confirms', obi_agrees), ('OBI disagrees', obi_disagrees)]:
        accs = []
        for horizon in [10, 30, 60]:
            # For UP spikes, check if UP price went up
            up_spikes = subset[subset['spike_direction'] == 'UP']
            down_spikes = subset[subset['spike_direction'] == 'DOWN']

            up_col = f'up_direction_{horizon}'
            down_col = f'down_direction_{horizon}'

            up_correct = (up_spikes[up_col] > 0).sum() if up_col in up_spikes.columns else 0
            down_correct = (down_spikes[down_col] > 0).sum() if down_col in down_spikes.columns else 0

            total = len(subset)
            acc = (up_correct + down_correct) / total if total > 0 else 0
            accs.append(f"{acc:.1%}")

            results[f'{label.lower().replace(" ", "_")}_acc_{horizon}'] = acc

        print(f"{label:<25} {accs[0]:<12} {accs[1]:<12} {accs[2]:<12} {len(subset):<8}")

    # Recommendation
    spike_acc = results.get('all_spikes_acc_30', 0)
    obi_acc = results.get('obi_confirms_acc_30', 0)

    print(f"\n  Improvement from OBI filter: {(obi_acc - spike_acc)*100:+.1f}pp at 30-tick horizon")

    if obi_acc > spike_acc + 0.03:
        print("  RECOMMENDATION: Use OBI as confirmation filter (+3%+ improvement)")
    elif obi_acc > spike_acc:
        print("  RECOMMENDATION: OBI provides marginal improvement")
    else:
        print("  RECOMMENDATION: OBI does not help - stick with spike-only")

    return results


def print_summary(results: Dict) -> None:
    """Print analysis summary."""
    print("\n" + "="*70)
    print("OBI ALPHA ANALYSIS SUMMARY")
    print("="*70)

    if 'stats' in results:
        stats = results['stats']
        print(f"\nDataset: {stats['n_rows']:,} rows")
        print(f"Imbalance mean: {stats['imbalance_mean']:.4f}")
        print(f"Imbalance std:  {stats['imbalance_std']:.4f}")
        print(f"% Strong positive (>0.3): {stats['pct_strong_positive']*100:.1f}%")
        print(f"% Strong negative (<-0.3): {stats['pct_strong_negative']*100:.1f}%")

    if 'correlation' in results and len(results['correlation']) > 0:
        print("\n--- Imbalance -> Price Change Correlation ---")
        corr_df = results['correlation']
        for _, row in corr_df.iterrows():
            print(f"  Horizon {int(row['horizon']):3d} ticks: r = {row['correlation']:+.4f} (n={int(row['n_samples']):,})")

    if 'predictive' in results and len(results['predictive']) > 0:
        print("\n--- Predictive Power (Direction Accuracy) ---")
        pred_df = results['predictive']

        # Best config per horizon
        for horizon in sorted(pred_df['horizon'].unique()):
            horizon_df = pred_df[pred_df['horizon'] == horizon]
            best = horizon_df.loc[horizon_df['accuracy'].idxmax()]
            if best['n_signals'] >= 50:
                edge = (best['accuracy'] - 0.5) * 100
                print(f"  Horizon {int(horizon):3d}: {best['accuracy']*100:.1f}% acc ({edge:+.1f}pp edge) "
                      f"@ threshold {best['threshold']:.1f} (n={int(best['n_signals']):,})")

    # Find overall best configuration
    if 'predictive' in results and len(results['predictive']) > 0:
        pred_df = results['predictive']
        # Filter for sufficient signals
        valid = pred_df[pred_df['n_signals'] >= 100]
        if len(valid) > 0:
            best_overall = valid.loc[valid['accuracy'].idxmax()]
            print("\n--- BEST CONFIGURATION ---")
            print(f"Horizon: {int(best_overall['horizon'])} ticks")
            print(f"Threshold: {best_overall['threshold']}")
            print(f"Accuracy: {best_overall['accuracy']*100:.1f}%")
            print(f"Edge over random: {(best_overall['accuracy']-0.5)*100:+.1f}pp")
            print(f"Signals: {int(best_overall['n_signals']):,}")
            print(f"Avg return per signal: ${best_overall['avg_return']:.4f}")


def main():
    parser = argparse.ArgumentParser(description='Analyze OBI Alpha')
    parser.add_argument('--input', type=str,
                       default='research/observer/grid_obs_20260127.csv',
                       help='Input CSV file with depth data')
    parser.add_argument('--levels', type=int, default=5,
                       help='Number of orderbook levels to use (1-5)')
    parser.add_argument('--output', type=str, default=None,
                       help='Output CSV for detailed results')
    args = parser.parse_args()

    # Find input file
    input_path = Path(args.input)
    if not input_path.exists():
        # Try relative to script
        alt_path = Path(__file__).parent.parent / args.input
        if alt_path.exists():
            input_path = alt_path
        else:
            print(f"Error: Cannot find input file: {args.input}")
            print("Make sure observer data collection is complete.")
            return

    print(f"Loading data from: {input_path}")
    df = load_observer_data(str(input_path))
    print(f"Loaded {len(df):,} rows")

    # Configure analysis
    config = OBIConfig(depth_levels=args.levels)

    # Run analysis
    print("\nAnalyzing OBI predictive power...")
    results = analyze_single_market(df, config)

    # Run spike + OBI combination analysis
    print("\nAnalyzing OBI + Spike combination...")
    spike_results = analyze_obi_with_spike(df, config)
    results['spike_analysis'] = spike_results

    # Print summary
    print_summary(results)

    # Save detailed results
    if args.output and 'predictive' in results:
        output_path = Path(args.output)
        results['predictive'].to_csv(output_path, index=False)
        print(f"\nDetailed results saved to: {output_path}")

    print("\n" + "="*70)
    print("NEXT STEPS:")
    print("="*70)
    print("1. If correlation is strong (|r| > 0.1), OBI has predictive power")
    print("2. If accuracy > 55% with 100+ signals, consider implementing")
    print("3. Test combining OBI with velocity signal for enhanced alpha")
    print("4. Check if predictive power varies by time-to-resolution")


if __name__ == '__main__':
    main()
