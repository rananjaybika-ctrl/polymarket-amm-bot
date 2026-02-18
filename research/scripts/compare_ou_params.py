#!/usr/bin/env python3
"""
Compare OU parameters at different calibration frequencies.

Checks if regime distribution improves from old (60s) to new (1s, 5Hz) calibrations.
"""

import json
import numpy as np
import pandas as pd
from pathlib import Path


def load_params(file_path):
    """Load OU params from JSON."""
    with open(file_path) as f:
        return json.load(f)


def classify_regime(z_score):
    """Classify z-score into volatility regime."""
    if z_score < 0:
        return "LOW"
    elif z_score < 1.5:
        return "MEDIUM"
    elif z_score < 2.5:
        return "HIGH"
    else:
        return "EXTREME"


def compute_z_scores_for_data(btc_df, params, ewma_window=60):
    """
    Compute rolling z-scores using OU params.

    Args:
        btc_df: DataFrame with price column
        params: dict with mu, sigma_stat
        ewma_window: window for rolling volatility

    Returns:
        Array of z-scores
    """
    prices = btc_df['price'].values

    # Compute returns
    returns = np.diff(prices) / prices[:-1] * 100

    # Filter extreme returns
    returns = np.where(np.abs(returns) > 5.0, np.nan, returns)

    # Compute rolling EWMA volatility
    alpha = 2.0 / (ewma_window + 1)
    variance = np.nanvar(returns[:ewma_window])
    volatilities = []

    for i, r in enumerate(returns):
        if i < ewma_window:
            vol = np.nanstd(returns[:i+1]) if i > 0 else np.sqrt(variance)
        else:
            if not np.isnan(r):
                variance = alpha * (r ** 2) + (1 - alpha) * variance
            vol = np.sqrt(variance)
        vol = max(vol, 1e-8)
        volatilities.append(vol)

    volatilities = np.array(volatilities)

    # Compute z-scores
    log_vol = np.log(volatilities)
    z_scores = (log_vol - params['mu']) / params['sigma_stat']

    # Skip warm-up
    z_scores = z_scores[ewma_window:]

    return z_scores


def analyze_regime_distribution(z_scores, label):
    """Print regime distribution stats."""
    regimes = [classify_regime(z) for z in z_scores]

    low_pct = np.mean([r == "LOW" for r in regimes]) * 100
    med_pct = np.mean([r == "MEDIUM" for r in regimes]) * 100
    high_pct = np.mean([r == "HIGH" for r in regimes]) * 100
    ext_pct = np.mean([r == "EXTREME" for r in regimes]) * 100

    print(f"\n{'='*60}")
    print(f"{label}")
    print(f"{'='*60}")
    print(f"  Mean z-score:   {np.mean(z_scores):.2f}")
    print(f"  Std z-score:    {np.std(z_scores):.2f}")
    print(f"  Min/Max:        [{np.min(z_scores):.2f}, {np.max(z_scores):.2f}]")
    print(f"\n  Regime Distribution:")
    print(f"    LOW (z<0):       {low_pct:5.1f}%")
    print(f"    MEDIUM (0<z<1.5): {med_pct:5.1f}%")
    print(f"    HIGH (1.5<z<2.5): {high_pct:5.1f}%")
    print(f"    EXTREME (z>2.5):  {ext_pct:5.1f}%")

    return {
        'label': label,
        'mean_z': np.mean(z_scores),
        'std_z': np.std(z_scores),
        'low_pct': low_pct,
        'med_pct': med_pct,
        'high_pct': high_pct,
        'ext_pct': ext_pct
    }


def main():
    # Load all OU params
    old_params = load_params("research/ou_params.json")  # 60s calibration
    params_1s = load_params("research/ou_params_1s.json")
    params_5hz = load_params("research/ou_params_5hz.json")

    print("="*60)
    print("OU PARAMETER COMPARISON")
    print("="*60)

    print("\n{:<20} {:>15} {:>15} {:>15}".format(
        "Parameter", "Old (60s)", "1s", "5Hz"
    ))
    print("-"*65)

    for key in ['mu', 'theta', 'sigma_stat', 'half_life_sec', 'n_samples', 'dt_seconds']:
        old_val = old_params.get(key, 'N/A')
        s1_val = params_1s.get(key, 'N/A')
        hz5_val = params_5hz.get(key, 'N/A')

        if key == 'n_samples':
            print(f"{key:<20} {old_val:>15,} {s1_val:>15,} {hz5_val:>15,}")
        elif key == 'dt_seconds':
            print(f"{key:<20} {old_val:>15.3f} {s1_val:>15.3f} {hz5_val:>15.3f}")
        else:
            print(f"{key:<20} {old_val:>15.4f} {s1_val:>15.4f} {hz5_val:>15.4f}")

    # Load BTC data
    print("\n\nLoading BTC data for z-score distribution test...")
    btc_df = pd.read_csv("research/binance_hf/btc_prices_is_oos2_5hz.csv")

    # Resample to 1s for fair comparison
    btc_df['bucket'] = (btc_df['timestamp_ms'] // 1000).astype(int)
    btc_1s = btc_df.groupby('bucket').agg({
        'timestamp_ms': 'last',
        'price': 'last'
    }).reset_index(drop=True)

    print(f"BTC data: {len(btc_1s):,} rows at 1s intervals")

    # Compute z-scores with each param set
    results = []

    z_old = compute_z_scores_for_data(btc_1s, old_params)
    results.append(analyze_regime_distribution(z_old, "Old (60s calibration)"))

    z_1s = compute_z_scores_for_data(btc_1s, params_1s)
    results.append(analyze_regime_distribution(z_1s, "New (1s calibration)"))

    z_5hz = compute_z_scores_for_data(btc_1s, params_5hz)
    results.append(analyze_regime_distribution(z_5hz, "New (5Hz calibration)"))

    # Summary comparison
    print("\n\n" + "="*70)
    print("SUMMARY: REGIME DISTRIBUTION COMPARISON")
    print("="*70)
    print("\n{:<25} {:>10} {:>10} {:>10} {:>10}".format(
        "Calibration", "LOW%", "MEDIUM%", "HIGH%", "EXTREME%"
    ))
    print("-"*70)
    for r in results:
        print(f"{r['label']:<25} {r['low_pct']:>10.1f} {r['med_pct']:>10.1f} {r['high_pct']:>10.1f} {r['ext_pct']:>10.1f}")

    print("\n" + "="*70)
    print("DIAGNOSIS")
    print("="*70)

    # Check if old params have all-LOW problem
    if results[0]['low_pct'] > 90:
        print("\n[PROBLEM] Old (60s) calibration: >90% in LOW regime - BROKEN!")

    # Check if new params fix it
    for r in results[1:]:
        if r['med_pct'] + r['high_pct'] + r['ext_pct'] > 30:
            print(f"\n[FIXED] {r['label']}: Good distribution across regimes")
        else:
            print(f"\n[STILL BROKEN] {r['label']}: Still mostly LOW regime")

    return results


if __name__ == "__main__":
    main()
