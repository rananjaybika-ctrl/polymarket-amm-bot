#!/usr/bin/env python3
"""
Extract BTC prices from observer file for OU recalibration.

The IS+OOS2 observer data contains 5Hz BTC prices in the binance_price column.
This script extracts them into a format suitable for ou_calibration.py.

Usage:
    python research/scripts/extract_btc_for_ou.py

Output:
    research/binance_hf/btc_prices_is_oos2_5hz.csv
"""

import pandas as pd
from pathlib import Path
from datetime import datetime

def main():
    # Input file
    input_file = Path("research/observer/PROTECTED_grid_obs_is_oos2_combined.csv")
    output_file = Path("research/binance_hf/btc_prices_is_oos2_5hz.csv")

    print(f"Loading {input_file}...")

    # Load only necessary columns
    obs = pd.read_csv(
        input_file,
        usecols=['timestamp_ms', 'binance_price']
    )

    print(f"Loaded {len(obs):,} rows")

    # Rename to match ou_calibration.py expectations
    obs = obs.rename(columns={'binance_price': 'price'})

    # Drop duplicates and sort
    obs = obs.drop_duplicates(subset='timestamp_ms', keep='first')
    obs = obs.sort_values('timestamp_ms').reset_index(drop=True)

    print(f"After dedup: {len(obs):,} rows")

    # Check data range
    ts_min = obs['timestamp_ms'].min()
    ts_max = obs['timestamp_ms'].max()
    duration_hours = (ts_max - ts_min) / (1000 * 3600)

    dt_min = datetime.fromtimestamp(ts_min / 1000)
    dt_max = datetime.fromtimestamp(ts_max / 1000)

    print(f"Time range: {dt_min} to {dt_max}")
    print(f"Duration: {duration_hours:.1f} hours")

    # Check price range
    price_min = obs['price'].min()
    price_max = obs['price'].max()
    print(f"Price range: ${price_min:,.2f} - ${price_max:,.2f}")

    # Check for nulls
    null_count = obs['price'].isnull().sum()
    if null_count > 0:
        print(f"WARNING: {null_count} null prices, dropping...")
        obs = obs.dropna(subset=['price'])

    # Estimate sample rate
    ts_diffs = obs['timestamp_ms'].diff().dropna()
    median_interval_ms = ts_diffs.median()
    sample_rate_hz = 1000 / median_interval_ms

    print(f"Median interval: {median_interval_ms:.1f}ms ({sample_rate_hz:.1f}Hz)")

    # Save
    output_file.parent.mkdir(parents=True, exist_ok=True)
    obs.to_csv(output_file, index=False)

    print(f"\nSaved to {output_file}")
    print(f"Total rows: {len(obs):,}")

    return len(obs)

if __name__ == "__main__":
    main()
