#!/usr/bin/env python3
"""
Debug script to understand z-score distribution in FOLLOW signals.
"""

import pandas as pd
import numpy as np
from pathlib import Path
import math

# Use NEW OU params (1s calibration)
OU_MU = -6.9705
OU_SIGMA = 1.6811

# OU threshold params
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5


def compute_ou_threshold(volatility, mu, sigma):
    """Returns (threshold, z_score)."""
    log_vol = math.log(max(volatility, 1e-6))
    z_score = (log_vol - mu) / sigma
    z_clamped = max(-10, min(10, z_score * OU_SIGMOID_STEEPNESS))
    sigmoid = 1.0 / (1.0 + math.exp(-z_clamped))
    multiplier = OU_K_LOW + (OU_K_HIGH - OU_K_LOW) * sigmoid
    threshold = OU_BASE_THRESHOLD * multiplier
    return max(0.015, min(0.10, threshold)), z_score


def main():
    # Load BTC data
    btc_file = Path("research/binance_hf/btc_prices_is_oos2_5hz.csv")
    if not btc_file.exists():
        print(f"BTC file not found: {btc_file}")
        return

    print("Loading BTC data...")
    btc_df = pd.read_csv(btc_file)
    print(f"Rows: {len(btc_df):,}")

    # Resample to 1s
    btc_df['bucket'] = (btc_df['timestamp_ms'] // 1000).astype(int)
    btc_1s = btc_df.groupby('bucket').agg({
        'timestamp_ms': 'last',
        'price': 'last'
    }).reset_index(drop=True)
    print(f"Resampled to {len(btc_1s):,} rows (1s)")

    # Compute returns
    prices = btc_1s['price'].values
    returns = np.diff(prices) / prices[:-1] * 100

    # Compute rolling volatility with EWMA
    ewma_halflife = 300  # Same as in follow_signal_analysis.py
    var_alpha = 1 - 0.5 ** (1.0 / ewma_halflife)

    variance = returns[:60].var() if len(returns) > 60 else 0.01
    z_scores = []
    volatilities = []

    for i, r in enumerate(returns):
        if np.isnan(r):
            z_scores.append(np.nan)
            volatilities.append(np.nan)
            continue
        variance = var_alpha * (r ** 2) + (1 - var_alpha) * variance
        vol = max(np.sqrt(variance), 1e-6)
        volatilities.append(vol)
        _, z_score = compute_ou_threshold(vol, OU_MU, OU_SIGMA)
        z_scores.append(z_score)

    z_scores = np.array(z_scores)
    volatilities = np.array(volatilities)

    # Skip warm-up
    z_scores = z_scores[60:]
    volatilities = volatilities[60:]

    # Remove NaN
    valid_mask = ~np.isnan(z_scores)
    z_scores = z_scores[valid_mask]
    volatilities = volatilities[valid_mask]

    print(f"\n{'='*60}")
    print("Z-SCORE DISTRIBUTION (with NEW OU params)")
    print(f"{'='*60}")
    print(f"Mean:   {np.mean(z_scores):.2f}")
    print(f"Std:    {np.std(z_scores):.2f}")
    print(f"Min:    {np.min(z_scores):.2f}")
    print(f"Max:    {np.max(z_scores):.2f}")

    print(f"\n{'='*60}")
    print("REGIME DISTRIBUTION")
    print(f"{'='*60}")

    regimes = [
        ('z < -1 (LOW)', lambda z: z < -1),
        ('-1 < z < 0 (MED-)', lambda z: -1 <= z < 0),
        ('0 < z < 1 (MED)', lambda z: 0 <= z < 1),
        ('1 < z < 1.5 (MED+)', lambda z: 1 <= z < 1.5),
        ('1.5 < z < 2 (HIGH)', lambda z: 1.5 <= z < 2),
        ('z > 2 (EXTREME)', lambda z: z >= 2),
    ]

    for name, condition in regimes:
        mask = [condition(z) for z in z_scores]
        pct = np.mean(mask) * 100
        count = np.sum(mask)
        print(f"  {name:25s}: {pct:5.1f}% ({count:,} samples)")

    print(f"\n{'='*60}")
    print("VOLATILITY STATS")
    print(f"{'='*60}")
    print(f"Mean volatility: {np.mean(volatilities):.4f}%")
    print(f"Median volatility: {np.median(volatilities):.4f}%")
    print(f"P95 volatility: {np.percentile(volatilities, 95):.4f}%")
    print(f"Max volatility: {np.max(volatilities):.4f}%")

    # What volatility corresponds to z > 1.5?
    z_threshold = 1.5
    vol_at_threshold = np.exp(OU_MU + z_threshold * OU_SIGMA)
    print(f"\nTo reach z > 1.5, volatility must be > {vol_at_threshold:.4f}%")

    above_threshold = volatilities > vol_at_threshold
    print(f"Samples with vol > {vol_at_threshold:.4f}%: {np.sum(above_threshold):,} ({np.mean(above_threshold)*100:.1f}%)")


if __name__ == "__main__":
    main()
