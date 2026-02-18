#!/usr/bin/env python3
"""
Calibrate OU params on combined low-vol + high-vol data for robust regime detection.

Combines:
- IS+OOS2 (Jan 17-19): Lower volatility, mean-reverting
- OOS9 (Feb 1-3): Higher volatility, trending

Both resampled to 1s for consistent calibration.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime
import json
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.strategies.ou_volatility import OUParameterEstimator, OUParameters


def load_and_resample(file_path: Path, label: str, interval_ms: int = 1000) -> pd.DataFrame:
    """Load BTC data and resample to specified interval (default 1000ms = 1s)."""
    print(f"\nLoading {label}: {file_path.name}")
    df = pd.read_csv(file_path)
    print(f"  Raw rows: {len(df):,}")

    # Deduplicate
    df = df.drop_duplicates(subset='timestamp_ms', keep='first')
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    # Check frequency
    ts_diff = np.diff(df['timestamp_ms'].values[:1000])
    median_dt_ms = np.median(ts_diff)
    print(f"  Sample rate: {1000/median_dt_ms:.1f}Hz ({median_dt_ms:.1f}ms)")

    # Resample to target interval (5Hz = 200ms)
    df['bucket'] = (df['timestamp_ms'] // interval_ms).astype(int)
    df_resampled = df.groupby('bucket').agg({
        'timestamp_ms': 'last',
        'price': 'last'
    }).reset_index(drop=True)

    target_hz = 1000 / interval_ms
    print(f"  Resampled: {len(df_resampled):,} rows ({target_hz:.0f}Hz)")

    # Time range
    ts_min = df_resampled['timestamp_ms'].min()
    ts_max = df_resampled['timestamp_ms'].max()
    dt_min = datetime.fromtimestamp(ts_min / 1000)
    dt_max = datetime.fromtimestamp(ts_max / 1000)
    duration_h = (ts_max - ts_min) / (1000 * 3600)
    print(f"  Range: {dt_min} to {dt_max} ({duration_h:.1f}h)")

    # Price range
    print(f"  Price: ${df_resampled['price'].min():,.2f} - ${df_resampled['price'].max():,.2f}")

    return df_resampled


def compute_volatility_stats(df: pd.DataFrame, label: str):
    """Compute and display volatility statistics."""
    returns = df['price'].pct_change().dropna() * 100

    # Rolling volatility
    vol_ewma = returns.ewm(halflife=300).std()

    print(f"\n{label} Volatility Stats:")
    print(f"  Mean |return|:  {returns.abs().mean():.4f}%")
    print(f"  Std return:     {returns.std():.4f}%")
    print(f"  P95 |return|:   {returns.abs().quantile(0.95):.4f}%")
    print(f"  Mean EWMA vol:  {vol_ewma.mean():.4f}%")
    print(f"  Max EWMA vol:   {vol_ewma.max():.4f}%")

    return vol_ewma


def main():
    btc_dir = Path("research/binance_hf")

    # Load datasets
    is_oos2 = load_and_resample(btc_dir / "btc_prices_is_oos2_5hz.csv", "IS+OOS2 (low vol)")
    oos9 = load_and_resample(btc_dir / "btc_prices_oos9.csv", "OOS9 (high vol)")

    # Compute volatility stats for each
    vol_is = compute_volatility_stats(is_oos2, "IS+OOS2")
    vol_oos9 = compute_volatility_stats(oos9, "OOS9")

    # Combine datasets
    print("\n" + "="*60)
    print("COMBINING DATASETS")
    print("="*60)

    combined = pd.concat([is_oos2, oos9], ignore_index=True)
    combined = combined.drop_duplicates(subset='timestamp_ms', keep='first')
    combined = combined.sort_values('timestamp_ms').reset_index(drop=True)

    print(f"Combined rows: {len(combined):,}")

    ts_min = combined['timestamp_ms'].min()
    ts_max = combined['timestamp_ms'].max()
    duration_h = (ts_max - ts_min) / (1000 * 3600)
    print(f"Total duration: {duration_h:.1f}h")

    # Compute combined volatility stats
    vol_combined = compute_volatility_stats(combined, "COMBINED")

    # Calibrate OU on combined data
    print("\n" + "="*60)
    print("CALIBRATING OU ON COMBINED DATA")
    print("="*60)

    # Compute returns
    returns = combined['price'].pct_change().dropna() * 100
    returns = returns[returns.abs() <= 5.0]  # Filter extreme

    print(f"Returns: {len(returns):,} samples")

    # Estimate dt
    ts_diff = np.diff(combined['timestamp_ms'].values)
    median_dt_ms = np.median(ts_diff)
    dt_seconds = median_dt_ms / 1000
    print(f"dt: {dt_seconds:.3f}s")

    # Fit OU - use 60 sample warmup (1 minute at 1s)
    estimator = OUParameterEstimator()
    params = estimator.fit_from_returns(
        returns_pct=returns.tolist(),
        dt_seconds=dt_seconds,
        ewma_window=60,
    )

    print("\nFITTED PARAMETERS:")
    print(f"  μ (mean log-vol):      {params.mu:.4f}")
    print(f"  θ (mean-reversion):    {params.theta:.6f}/s")
    print(f"  ξ (vol-of-vol):        {params.xi:.4f}")
    print(f"  σ_stat (stationary):   {params.sigma_stat:.4f}")
    print(f"  Half-life:             {params.half_life_sec:.1f}s")
    print(f"  n_samples:             {params.n_samples:,}")

    # Save
    output_file = "research/ou_params_combined_1s.json"
    params.save(output_file)
    print(f"\nSaved to {output_file}")

    # Test z-score distribution on each dataset
    print("\n" + "="*60)
    print("Z-SCORE DISTRIBUTION TEST")
    print("="*60)

    def test_zscore_distribution(df, label, params):
        returns = df['price'].pct_change().dropna() * 100
        returns = returns.fillna(0)

        # EWMA variance - 5 minute halflife at 1s = 300 samples
        var_ewma = (returns ** 2).ewm(halflife=300).mean()
        vol = np.sqrt(var_ewma.values)
        vol = np.maximum(vol, 1e-6)

        # Z-scores
        log_vol = np.log(vol)
        z_scores = (log_vol - params.mu) / params.sigma_stat
        z_scores = z_scores[60:]  # Skip warmup (1 minute at 1s)

        # Distribution
        low_pct = np.mean(z_scores < 0) * 100
        med_pct = np.mean((z_scores >= 0) & (z_scores < 1.5)) * 100
        high_pct = np.mean(z_scores >= 1.5) * 100

        print(f"\n{label}:")
        print(f"  Mean z: {np.mean(z_scores):.2f}, Std: {np.std(z_scores):.2f}")
        print(f"  LOW (z<0):     {low_pct:5.1f}%")
        print(f"  MEDIUM (0-1.5): {med_pct:5.1f}%")
        print(f"  HIGH (z>1.5):  {high_pct:5.1f}%")

        return z_scores

    z_is = test_zscore_distribution(is_oos2, "IS+OOS2 (low vol)", params)
    z_oos9 = test_zscore_distribution(oos9, "OOS9 (high vol)", params)
    z_combined = test_zscore_distribution(combined, "COMBINED", params)

    # Compare to old params
    print("\n" + "="*60)
    print("COMPARISON TO OLD PARAMS")
    print("="*60)

    old_params = {"mu": -3.9845, "sigma_stat": 0.3877}
    new_1s_is = {"mu": -6.9705, "sigma_stat": 1.6811}  # From earlier 1s IS-only calibration
    combined_params = {"mu": params.mu, "sigma_stat": params.sigma_stat}

    print(f"\n{'Param':<15} {'Old (60s)':<15} {'1s (IS only)':<15} {'Combined 1s':<15}")
    print("-" * 60)
    print(f"{'mu':<15} {old_params['mu']:<15.4f} {new_1s_is['mu']:<15.4f} {combined_params['mu']:<15.4f}")
    print(f"{'sigma_stat':<15} {old_params['sigma_stat']:<15.4f} {new_1s_is['sigma_stat']:<15.4f} {combined_params['sigma_stat']:<15.4f}")

    print("\n" + "="*60)
    print("RECOMMENDATION")
    print("="*60)

    # Check if distribution is reasonable for both datasets
    is_low_pct = np.mean(z_is < 0) * 100
    oos9_low_pct = np.mean(z_oos9 < 0) * 100

    if is_low_pct > 30 and is_low_pct < 70 and oos9_low_pct > 10 and oos9_low_pct < 50:
        print("\n✅ Combined calibration provides balanced regime distribution")
        print("   across both low-vol and high-vol periods.")
        print(f"\n   Use: {output_file}")
    else:
        print("\n⚠️ Distribution may still be skewed. Consider adding more diverse data.")


if __name__ == "__main__":
    main()
