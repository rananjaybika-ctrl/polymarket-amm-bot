#!/usr/bin/env python3
"""
OU Parameter Calibration Script.

Estimates Ornstein-Uhlenbeck process parameters from historical Binance data
for use in adaptive spike threshold calculation.

Usage:
    python ou_calibration.py                    # Full calibration
    python ou_calibration.py --validate         # Validate on OOS2 data
    python ou_calibration.py --synthetic-test   # Test parameter recovery

Output:
    - research/ou_params.json: Calibrated parameters
    - Validation plots (optional)
    - Comparison of z-scores between training and OOS2 periods

Author: Claude Code
Date: January 20, 2026
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.strategies.ou_volatility import (
    OUParameters,
    OUParameterEstimator,
    OUAdaptiveThreshold,
    compute_ou_z_score,
    compute_adaptive_threshold,
    classify_regime,
    test_parameter_recovery,
    DEFAULT_BASE_THRESHOLD,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)


# =============================================================================
# DATA LOADING
# =============================================================================

def load_binance_data(
    data_dir: str = "research/binance_hf",
    cutoff_ts: Optional[int] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Load Binance price data and split into training/OOS2.

    Args:
        data_dir: Directory containing Binance CSV files
        cutoff_ts: Timestamp cutoff for training/OOS2 split (milliseconds)
                   Default: 1768705387229 (Jan 18, 08:33:07 IST)

    Returns:
        (training_df, oos2_df) DataFrames with columns: timestamp_ms, price
    """
    data_path = Path(data_dir)

    # Load combined file
    combined_file = data_path / "btc_prices_combined.csv"
    if combined_file.exists():
        logger.info(f"Loading {combined_file}")
        df = pd.read_csv(combined_file)
    else:
        # Load individual files and combine
        csv_files = list(data_path.glob("btc_prices_*.csv"))
        if not csv_files:
            raise FileNotFoundError(f"No Binance CSV files found in {data_dir}")

        logger.info(f"Loading {len(csv_files)} Binance files...")
        dfs = [pd.read_csv(f) for f in csv_files]
        df = pd.concat(dfs, ignore_index=True)

    # Deduplicate and sort
    df = df.drop_duplicates(subset=['timestamp_ms'], keep='first')
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    logger.info(f"Total rows after dedup: {len(df):,}")

    # Default cutoff: Training data cutoff from HANDOVER
    if cutoff_ts is None:
        cutoff_ts = 1768705387229  # Jan 18, 08:33:07 IST

    # Split
    training_df = df[df['timestamp_ms'] < cutoff_ts].copy()
    oos2_df = df[df['timestamp_ms'] >= cutoff_ts].copy()

    # Log info
    def ts_to_str(ts_ms):
        return datetime.fromtimestamp(ts_ms / 1000).strftime('%Y-%m-%d %H:%M:%S')

    logger.info(f"Training: {len(training_df):,} rows")
    if len(training_df) > 0:
        logger.info(f"  Range: {ts_to_str(training_df['timestamp_ms'].min())} - {ts_to_str(training_df['timestamp_ms'].max())}")

    logger.info(f"OOS2: {len(oos2_df):,} rows")
    if len(oos2_df) > 0:
        logger.info(f"  Range: {ts_to_str(oos2_df['timestamp_ms'].min())} - {ts_to_str(oos2_df['timestamp_ms'].max())}")

    return training_df, oos2_df


def compute_returns(df: pd.DataFrame, price_col: str = 'price') -> np.ndarray:
    """
    Compute percentage returns from price series.

    Args:
        df: DataFrame with price column
        price_col: Name of price column

    Returns:
        Array of percentage returns
    """
    prices = df[price_col].values
    returns = np.diff(prices) / prices[:-1] * 100
    return returns


def resample_to_1s(df: pd.DataFrame) -> pd.DataFrame:
    """
    Resample high-frequency data to 1-second intervals.

    Takes the last price in each 1-second bucket.

    Args:
        df: DataFrame with timestamp_ms and price columns

    Returns:
        Resampled DataFrame
    """
    df = df.copy()
    df['timestamp_s'] = df['timestamp_ms'] // 1000
    resampled = df.groupby('timestamp_s').agg({
        'timestamp_ms': 'last',
        'price': 'last'
    }).reset_index(drop=True)
    logger.info(f"Resampled from {len(df):,} to {len(resampled):,} rows (1s intervals)")
    return resampled


# =============================================================================
# CALIBRATION
# =============================================================================

def calibrate_ou_params(
    training_df: pd.DataFrame,
    ewma_window: int = 60,
    output_file: str = "research/ou_params.json",
) -> OUParameters:
    """
    Calibrate OU parameters from training data.

    Args:
        training_df: Training data DataFrame
        ewma_window: Window for rolling volatility EWMA
        output_file: Path to save parameters

    Returns:
        Calibrated OUParameters
    """
    logger.info("=" * 60)
    logger.info("CALIBRATING OU PARAMETERS")
    logger.info("=" * 60)

    # Compute returns
    returns = compute_returns(training_df)
    logger.info(f"Computed {len(returns):,} returns")

    # Filter extreme returns (data gaps)
    max_return = 5.0
    valid_mask = np.abs(returns) <= max_return
    n_filtered = np.sum(~valid_mask)
    logger.info(f"Filtered {n_filtered} extreme returns (>{max_return}%)")
    returns_filtered = returns[valid_mask]

    # Estimate sample rate
    ts_diff = np.diff(training_df['timestamp_ms'].values)
    median_dt_ms = np.median(ts_diff)
    sample_rate_hz = 1000 / median_dt_ms
    dt_seconds = median_dt_ms / 1000

    logger.info(f"Estimated sample rate: {sample_rate_hz:.1f} Hz (dt={dt_seconds*1000:.1f}ms)")

    # Fit OU parameters
    estimator = OUParameterEstimator()
    params = estimator.fit_from_returns(
        returns_pct=returns_filtered.tolist(),
        dt_seconds=dt_seconds,
        ewma_window=ewma_window,
    )

    # Display results
    logger.info("-" * 40)
    logger.info("FITTED PARAMETERS:")
    logger.info(f"  μ (mean log-vol):      {params.mu:.4f}")
    logger.info(f"  θ (mean-reversion):    {params.theta:.6f}/s")
    logger.info(f"  ξ (vol-of-vol):        {params.xi:.4f}")
    logger.info(f"  σ_stat (stationary):   {params.sigma_stat:.4f}")
    logger.info(f"  Half-life:             {params.half_life_sec:.1f}s")
    logger.info("-" * 40)

    # Interpretation
    expected_vol = np.exp(params.mu + params.sigma_stat**2 / 2)
    logger.info(f"Expected volatility: {expected_vol:.4f}%")

    # Z-score thresholds
    logger.info("\nZ-SCORE INTERPRETATION:")
    for z in [-2, -1, 0, 1, 2]:
        vol_at_z = np.exp(params.mu + z * params.sigma_stat)
        regime = classify_regime(z)
        threshold = compute_adaptive_threshold(z)
        logger.info(f"  z={z:+d} ({regime:>8}): vol={vol_at_z:.4f}%, threshold={threshold:.4f}%")

    # Save
    params.save(output_file)

    return params


def validate_on_oos2(
    params: OUParameters,
    oos2_df: pd.DataFrame,
    ewma_window: int = 60,
) -> Dict:
    """
    Validate OU parameters on OOS2 data.

    Expected: OOS2 should have significantly positive z-scores (z > 1.5)
    indicating higher volatility than training period.

    Args:
        params: Calibrated OU parameters
        oos2_df: OOS2 DataFrame
        ewma_window: EWMA window

    Returns:
        Validation statistics
    """
    logger.info("=" * 60)
    logger.info("VALIDATING ON OOS2 DATA")
    logger.info("=" * 60)

    if len(oos2_df) < 1000:
        logger.warning(f"OOS2 data too small ({len(oos2_df)} rows), skipping validation")
        return {}

    # Compute rolling volatility
    returns = compute_returns(oos2_df)

    # Filter extreme returns
    valid_mask = np.abs(returns) <= 5.0
    returns = returns[valid_mask]

    # Compute rolling EWMA volatility
    alpha = 2.0 / (ewma_window + 1)
    variance = np.var(returns[:ewma_window])
    volatilities = []

    for i, r in enumerate(returns):
        if i < ewma_window:
            vol = np.std(returns[:i+1]) if i > 0 else np.sqrt(variance)
        else:
            variance = alpha * (r ** 2) + (1 - alpha) * variance
            vol = np.sqrt(variance)
        vol = max(vol, 1e-6)
        volatilities.append(vol)

    volatilities = np.array(volatilities)

    # Compute z-scores
    log_vol = np.log(volatilities)
    z_scores = (log_vol - params.mu) / params.sigma_stat

    # Skip warm-up
    z_scores = z_scores[ewma_window:]
    volatilities = volatilities[ewma_window:]

    # Statistics
    stats = {
        "n_samples": len(z_scores),
        "mean_z": float(np.mean(z_scores)),
        "std_z": float(np.std(z_scores)),
        "min_z": float(np.min(z_scores)),
        "max_z": float(np.max(z_scores)),
        "median_z": float(np.median(z_scores)),
        "pct_above_1": float(np.mean(z_scores > 1) * 100),
        "pct_above_1_5": float(np.mean(z_scores > 1.5) * 100),
        "pct_above_2": float(np.mean(z_scores > 2) * 100),
        "mean_vol": float(np.mean(volatilities)),
        "median_vol": float(np.median(volatilities)),
    }

    # Log results
    logger.info("-" * 40)
    logger.info("OOS2 Z-SCORE STATISTICS:")
    logger.info(f"  Mean z-score:     {stats['mean_z']:.2f}")
    logger.info(f"  Std z-score:      {stats['std_z']:.2f}")
    logger.info(f"  Median z-score:   {stats['median_z']:.2f}")
    logger.info(f"  Range:            [{stats['min_z']:.2f}, {stats['max_z']:.2f}]")
    logger.info("-" * 40)
    logger.info("REGIME DISTRIBUTION:")
    logger.info(f"  % in HIGH (z>1):      {stats['pct_above_1']:.1f}%")
    logger.info(f"  % in HIGH+ (z>1.5):   {stats['pct_above_1_5']:.1f}%")
    logger.info(f"  % in EXTREME (z>2):   {stats['pct_above_2']:.1f}%")
    logger.info("-" * 40)
    logger.info("VOLATILITY:")
    logger.info(f"  Mean OOS2 vol:    {stats['mean_vol']:.4f}%")
    logger.info(f"  Median OOS2 vol:  {stats['median_vol']:.4f}%")

    # Expected result check
    if stats['mean_z'] > 1.0:
        logger.info("\n✓ OOS2 correctly identified as HIGH volatility (mean z > 1)")
    else:
        logger.warning("\n✗ OOS2 not identified as high volatility (mean z <= 1)")

    # Adaptive threshold analysis
    logger.info("\n" + "=" * 60)
    logger.info("ADAPTIVE THRESHOLD IMPACT")
    logger.info("=" * 60)

    fixed_threshold = DEFAULT_BASE_THRESHOLD
    adaptive_thresholds = [compute_adaptive_threshold(z) for z in z_scores]

    logger.info(f"Fixed threshold:     {fixed_threshold:.4f}%")
    logger.info(f"Adaptive mean:       {np.mean(adaptive_thresholds):.4f}%")
    logger.info(f"Adaptive median:     {np.median(adaptive_thresholds):.4f}%")
    logger.info(f"Adaptive range:      [{np.min(adaptive_thresholds):.4f}%, {np.max(adaptive_thresholds):.4f}%]")

    # How much would thresholds increase?
    avg_multiplier = np.mean(adaptive_thresholds) / fixed_threshold
    logger.info(f"\nAverage threshold multiplier: {avg_multiplier:.2f}x")
    logger.info(f"This means in OOS2, thresholds would be ~{avg_multiplier:.2f}x higher")

    return stats


def compare_training_vs_oos2(
    params: OUParameters,
    training_df: pd.DataFrame,
    oos2_df: pd.DataFrame,
    ewma_window: int = 60,
) -> None:
    """
    Compare volatility characteristics between training and OOS2.

    Args:
        params: Calibrated OU parameters
        training_df: Training DataFrame
        oos2_df: OOS2 DataFrame
        ewma_window: EWMA window
    """
    logger.info("=" * 60)
    logger.info("TRAINING vs OOS2 COMPARISON")
    logger.info("=" * 60)

    def compute_stats(df: pd.DataFrame, label: str) -> Dict:
        """Compute volatility stats for a dataset."""
        returns = compute_returns(df)
        valid_mask = np.abs(returns) <= 5.0
        returns = returns[valid_mask]

        return {
            "label": label,
            "n_returns": len(returns),
            "mean_abs_return": float(np.mean(np.abs(returns))),
            "std_return": float(np.std(returns)),
            "p95_return": float(np.percentile(np.abs(returns), 95)),
            "p99_return": float(np.percentile(np.abs(returns), 99)),
        }

    train_stats = compute_stats(training_df, "Training")
    oos2_stats = compute_stats(oos2_df, "OOS2")

    # Print comparison
    logger.info(f"{'Metric':<25} {'Training':>15} {'OOS2':>15} {'Ratio':>10}")
    logger.info("-" * 65)

    for key in ["n_returns", "mean_abs_return", "std_return", "p95_return", "p99_return"]:
        train_val = train_stats[key]
        oos2_val = oos2_stats[key]
        ratio = oos2_val / train_val if train_val > 0 else 0

        if key == "n_returns":
            logger.info(f"{key:<25} {train_val:>15,} {oos2_val:>15,} {ratio:>10.2f}x")
        else:
            logger.info(f"{key:<25} {train_val:>15.4f}% {oos2_val:>15.4f}% {ratio:>10.2f}x")

    logger.info("-" * 65)

    # Volatility ratio
    vol_ratio = oos2_stats["mean_abs_return"] / train_stats["mean_abs_return"]
    logger.info(f"\nOOS2 volatility is {vol_ratio:.1f}x higher than training")

    # What threshold would be needed?
    suggested_threshold = DEFAULT_BASE_THRESHOLD * vol_ratio
    logger.info(f"Suggested OOS2 threshold: {DEFAULT_BASE_THRESHOLD:.3f}% × {vol_ratio:.1f} = {suggested_threshold:.3f}%")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="OU Parameter Calibration")
    parser.add_argument("--data-dir", default="research/binance_hf",
                       help="Directory containing Binance data")
    parser.add_argument("--output", default="research/ou_params.json",
                       help="Output file for parameters")
    parser.add_argument("--ewma-window", type=int, default=60,
                       help="EWMA window for volatility (default: 60)")
    parser.add_argument("--validate", action="store_true",
                       help="Validate on OOS2 data")
    parser.add_argument("--compare", action="store_true",
                       help="Compare training vs OOS2 volatility")
    parser.add_argument("--synthetic-test", action="store_true",
                       help="Run synthetic parameter recovery test")
    parser.add_argument("--all", action="store_true",
                       help="Run all: calibrate, validate, and compare")

    args = parser.parse_args()

    # Run synthetic test if requested
    if args.synthetic_test:
        logger.info("Running synthetic parameter recovery test...")
        success = test_parameter_recovery()
        sys.exit(0 if success else 1)

    # Load data
    training_df, oos2_df = load_binance_data(args.data_dir)

    # Calibrate
    params = calibrate_ou_params(
        training_df=training_df,
        ewma_window=args.ewma_window,
        output_file=args.output,
    )

    # Validate if requested
    if args.validate or args.all:
        validate_on_oos2(params, oos2_df, args.ewma_window)

    # Compare if requested
    if args.compare or args.all:
        compare_training_vs_oos2(params, training_df, oos2_df, args.ewma_window)

    logger.info("\n" + "=" * 60)
    logger.info("CALIBRATION COMPLETE")
    logger.info(f"Parameters saved to: {args.output}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
