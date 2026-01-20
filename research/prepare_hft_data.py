#!/usr/bin/env python3
"""
HFT Data Preparation Script

Merges observer CSVs and aligns with high-frequency Binance data.
Calculates derived columns: acceleration, jerk, lag measurements.

Output: research/merged_hft_data.csv with all columns aligned

Usage:
    python research/prepare_hft_data.py
    python research/prepare_hft_data.py --output research/custom_output.csv
"""

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime
import argparse


# =============================================================================
# CONFIGURATION
# =============================================================================

OBSERVER_DIR = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
BINANCE_HF_DIR = Path('/Users/rananjaybika/polymarket-amm-bot/research/binance_hf')
DEFAULT_OUTPUT = Path('/Users/rananjaybika/polymarket-amm-bot/research/merged_hft_data.csv')

# Acceleration/jerk calculation windows
VELOCITY_WINDOW_SAMPLES = 50   # ~10 seconds at 5Hz
ACCEL_WINDOW_SAMPLES = 25      # ~5 seconds for acceleration
JERK_WINDOW_SAMPLES = 10       # ~2 seconds for jerk


# =============================================================================
# DATA LOADING
# =============================================================================

def load_observer_data() -> pd.DataFrame:
    """Load and merge all observer CSV files."""
    csv_files = sorted(OBSERVER_DIR.glob('grid_obs_*.csv'))

    if not csv_files:
        # Fallback to spread_capture files
        csv_files = sorted(OBSERVER_DIR.glob('spread_capture_obs_*.csv'))

    print(f"Found {len(csv_files)} observer files:")
    for f in csv_files:
        print(f"  - {f.name} ({f.stat().st_size / 1024 / 1024:.1f} MB)")

    dfs = []
    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath, on_bad_lines='skip', low_memory=False)
            df['source_file'] = filepath.name
            dfs.append(df)
            print(f"  Loaded {len(df):,} rows from {filepath.name}")
        except Exception as e:
            print(f"  Error loading {filepath.name}: {e}")

    if not dfs:
        raise ValueError("No observer data found!")

    merged = pd.concat(dfs, ignore_index=True)

    # Sort by timestamp
    if 'timestamp_ms' in merged.columns:
        merged = merged.sort_values('timestamp_ms').reset_index(drop=True)

    print(f"\nTotal observer rows: {len(merged):,}")
    return merged


def load_binance_hf_data() -> pd.DataFrame:
    """Load high-frequency Binance price data."""
    csv_files = sorted(BINANCE_HF_DIR.glob('btc_prices_*.csv'))

    if not csv_files:
        print("Warning: No Binance HF data found")
        return pd.DataFrame()

    print(f"\nFound {len(csv_files)} Binance HF files:")
    for f in csv_files:
        print(f"  - {f.name} ({f.stat().st_size / 1024 / 1024:.1f} MB)")

    dfs = []
    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath)
            dfs.append(df)
            print(f"  Loaded {len(df):,} rows from {filepath.name}")
        except Exception as e:
            print(f"  Error loading {filepath.name}: {e}")

    if not dfs:
        return pd.DataFrame()

    merged = pd.concat(dfs, ignore_index=True)
    merged = merged.sort_values('timestamp_ms').reset_index(drop=True)

    print(f"\nTotal Binance HF rows: {len(merged):,}")
    return merged


# =============================================================================
# DERIVED COLUMNS
# =============================================================================

def calculate_velocity(prices: pd.Series, window: int = 50) -> pd.Series:
    """
    Calculate velocity (first derivative) from price series.

    Returns: velocity in basis points per second
    """
    # Price change over window
    price_change = prices.diff(window)

    # Convert to percentage change (basis points)
    pct_change = (price_change / prices.shift(window)) * 10000  # basis points

    # Assuming 5Hz sampling, window of 50 = 10 seconds
    velocity_bps = pct_change / (window / 5)  # bps per second

    return velocity_bps


def calculate_acceleration(velocity: pd.Series, window: int = 25) -> pd.Series:
    """
    Calculate acceleration (second derivative) from velocity series.

    Returns: acceleration in basis points per second squared
    """
    velocity_change = velocity.diff(window)

    # Time in seconds (assuming 5Hz)
    time_seconds = window / 5

    acceleration = velocity_change / time_seconds

    return acceleration


def calculate_jerk(acceleration: pd.Series, window: int = 10) -> pd.Series:
    """
    Calculate jerk (third derivative) from acceleration series.

    Returns: jerk in basis points per second cubed
    """
    accel_change = acceleration.diff(window)

    # Time in seconds (assuming 5Hz)
    time_seconds = window / 5

    jerk = accel_change / time_seconds

    return jerk


def calculate_lag_to_poly(
    observer_df: pd.DataFrame,
    binance_df: pd.DataFrame,
    spike_threshold: float = 0.02
) -> pd.DataFrame:
    """
    Measure lag between Binance spike and Polymarket orderbook reaction.

    For each spike in Binance data, find when Polymarket orderbook responds.
    """
    if binance_df.empty:
        observer_df['lag_to_poly_ms'] = np.nan
        return observer_df

    # Detect spikes in Binance HF data
    binance_df = binance_df.copy()
    binance_df['price_change_3tick'] = binance_df['price'].pct_change(3) * 100
    binance_df['spike_magnitude'] = binance_df['price_change_3tick'].abs()
    binance_df['is_spike'] = binance_df['spike_magnitude'] >= spike_threshold

    spikes = binance_df[binance_df['is_spike']].copy()
    print(f"\nDetected {len(spikes):,} spikes in Binance HF data (threshold={spike_threshold}%)")

    # For each spike, find corresponding observer row and measure lag
    lags = []

    for _, spike in spikes.iterrows():
        spike_ts = spike['timestamp_ms']
        spike_dir = "UP" if spike['price_change_3tick'] > 0 else "DOWN"

        # Find observer rows within 5 seconds after spike
        mask = (observer_df['timestamp_ms'] >= spike_ts) & \
               (observer_df['timestamp_ms'] <= spike_ts + 5000)

        nearby = observer_df[mask]

        if len(nearby) == 0:
            continue

        # Look for orderbook movement in spike direction
        if spike_dir == "UP":
            # UP spike should cause up_bid to increase
            first_row = nearby.iloc[0]
            for _, row in nearby.iterrows():
                if row['up_bid'] > first_row['up_bid'] + 0.005:
                    lag_ms = row['timestamp_ms'] - spike_ts
                    lags.append({
                        'spike_ts': spike_ts,
                        'spike_dir': spike_dir,
                        'spike_mag': spike['spike_magnitude'],
                        'lag_ms': lag_ms,
                    })
                    break
        else:
            # DOWN spike should cause down_bid to increase
            first_row = nearby.iloc[0]
            for _, row in nearby.iterrows():
                if row['down_bid'] > first_row['down_bid'] + 0.005:
                    lag_ms = row['timestamp_ms'] - spike_ts
                    lags.append({
                        'spike_ts': spike_ts,
                        'spike_dir': spike_dir,
                        'spike_mag': spike['spike_magnitude'],
                        'lag_ms': lag_ms,
                    })
                    break

    if lags:
        lag_df = pd.DataFrame(lags)
        print(f"Measured {len(lag_df):,} spike-to-poly lags")
        print(f"  Mean lag: {lag_df['lag_ms'].mean():.0f}ms")
        print(f"  Median lag: {lag_df['lag_ms'].median():.0f}ms")
        print(f"  Min/Max: {lag_df['lag_ms'].min():.0f}ms / {lag_df['lag_ms'].max():.0f}ms")

        # Save lag analysis
        lag_output = OBSERVER_DIR / 'lag_analysis.csv'
        lag_df.to_csv(lag_output, index=False)
        print(f"  Saved lag analysis to {lag_output}")

    return observer_df


def add_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add acceleration, jerk, and other derived columns."""
    df = df.copy()

    # Ensure we have binance_price column
    if 'binance_price' not in df.columns:
        print("Warning: binance_price column not found")
        return df

    print("\nCalculating derived columns...")

    # Calculate velocity if not present
    if 'velocity_bps' not in df.columns:
        df['velocity_bps'] = calculate_velocity(df['binance_price'], VELOCITY_WINDOW_SAMPLES)
        print("  Added velocity_bps")

    # Calculate acceleration (2nd derivative)
    df['acceleration_bps2'] = calculate_acceleration(df['velocity_bps'], ACCEL_WINDOW_SAMPLES)
    print("  Added acceleration_bps2")

    # Calculate jerk (3rd derivative)
    df['jerk_bps3'] = calculate_jerk(df['acceleration_bps2'], JERK_WINDOW_SAMPLES)
    print("  Added jerk_bps3")

    # Acceleration alignment score
    # Aligned = velocity and acceleration have same sign (momentum building)
    df['accel_aligned'] = ((df['velocity_bps'] > 0) & (df['acceleration_bps2'] > 0)) | \
                          ((df['velocity_bps'] < 0) & (df['acceleration_bps2'] < 0))
    print("  Added accel_aligned")

    # Signal quality score (for enhanced momentum)
    # Higher when velocity is strong, acceleration aligned, and consistent
    vel_component = (df['velocity_bps'].abs() / 1.0).clip(upper=1.0) * 0.30
    accel_component = df['accel_aligned'].astype(float) * 0.25

    # Spike confirmation (if spike columns exist)
    if 'spike_detected' in df.columns:
        spike_component = df['spike_detected'].astype(float) * 0.25
    else:
        spike_component = 0.0

    # Duration component (samples in same velocity direction)
    df['vel_direction'] = np.sign(df['velocity_bps'])
    df['vel_direction_streak'] = df.groupby(
        (df['vel_direction'] != df['vel_direction'].shift()).cumsum()
    ).cumcount() + 1
    duration_component = (df['vel_direction_streak'] / 20).clip(upper=1.0) * 0.20

    df['signal_quality'] = vel_component + accel_component + spike_component + duration_component
    print("  Added signal_quality")

    # Price momentum (rolling mean of velocity)
    df['momentum_5s'] = df['velocity_bps'].rolling(window=25, min_periods=1).mean()
    df['momentum_10s'] = df['velocity_bps'].rolling(window=50, min_periods=1).mean()
    print("  Added momentum_5s, momentum_10s")

    # Volatility (rolling std of velocity)
    df['volatility_5s'] = df['velocity_bps'].rolling(window=25, min_periods=1).std()
    print("  Added volatility_5s")

    return df


def add_spike_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add spike detection columns if not present."""
    if 'spike_detected' in df.columns:
        return df

    df = df.copy()

    if 'binance_price' not in df.columns:
        return df

    print("\nAdding spike detection columns...")

    SPIKE_LOOKBACK = 3
    SPIKE_THRESHOLD = 0.02

    df['price_change_3tick'] = df['binance_price'].pct_change(periods=SPIKE_LOOKBACK) * 100
    df['spike_magnitude'] = df['price_change_3tick'].abs()
    df['spike_detected'] = df['spike_magnitude'] >= SPIKE_THRESHOLD
    df['spike_direction'] = df['price_change_3tick'].apply(
        lambda x: 'UP' if x >= SPIKE_THRESHOLD else ('DOWN' if x <= -SPIKE_THRESHOLD else None)
    )

    print(f"  Detected {df['spike_detected'].sum():,} spikes")

    return df


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="HFT Data Preparation")
    parser.add_argument('--output', type=str, default=str(DEFAULT_OUTPUT),
                        help='Output file path')
    parser.add_argument('--skip-binance', action='store_true',
                        help='Skip Binance HF data loading')
    args = parser.parse_args()

    print("=" * 70)
    print("HFT DATA PREPARATION")
    print("=" * 70)

    # Load observer data
    observer_df = load_observer_data()

    # Load Binance HF data
    binance_df = pd.DataFrame()
    if not args.skip_binance:
        binance_df = load_binance_hf_data()

    # Add spike columns if not present
    observer_df = add_spike_columns(observer_df)

    # Add derived columns
    observer_df = add_derived_columns(observer_df)

    # Calculate lag measurements
    if not binance_df.empty:
        observer_df = calculate_lag_to_poly(observer_df, binance_df)

    # Save merged data
    output_path = Path(args.output)
    observer_df.to_csv(output_path, index=False)

    print(f"\n{'=' * 70}")
    print("DATA PREPARATION COMPLETE")
    print(f"{'=' * 70}")
    print(f"Output: {output_path}")
    print(f"Rows: {len(observer_df):,}")
    print(f"Columns: {len(observer_df.columns)}")
    print(f"Size: {output_path.stat().st_size / 1024 / 1024:.1f} MB")

    # Print column summary
    print("\nColumns:")
    for col in observer_df.columns:
        print(f"  - {col}")

    # Print data summary
    print("\nData Summary:")
    if 'market_slug' in observer_df.columns:
        print(f"  Markets: {observer_df['market_slug'].nunique()}")
    if 'timestamp_ms' in observer_df.columns:
        start_ts = observer_df['timestamp_ms'].min()
        end_ts = observer_df['timestamp_ms'].max()
        duration_hrs = (end_ts - start_ts) / 1000 / 3600
        print(f"  Duration: {duration_hrs:.2f} hours")
    if 'spike_detected' in observer_df.columns:
        print(f"  Spikes detected: {observer_df['spike_detected'].sum():,}")
    if 'accel_aligned' in observer_df.columns:
        print(f"  Accel aligned samples: {observer_df['accel_aligned'].sum():,}")


if __name__ == "__main__":
    main()
