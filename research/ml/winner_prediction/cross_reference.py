"""
Cross-Reference Gabagool Trades to Observer Data

Matches Gabagool's trade timestamps to the nearest observer row within tolerance.
This allows us to see what orderbook state he observed when making trading decisions.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, Tuple, Optional
import json
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Base paths
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
FINDINGS_DIR = PROJECT_ROOT / "research" / "findings" / "data"
OBSERVER_DIR = PROJECT_ROOT / "research" / "observer"


def load_gabagool_trades(filepath: Optional[Path] = None) -> pd.DataFrame:
    """
    Load Gabagool's trade data from JSON file.

    Args:
        filepath: Path to trades JSON. Defaults to gabagool_trades_oos7.json

    Returns:
        DataFrame with columns: side, outcome, price, size, timestamp, market_slug
    """
    if filepath is None:
        filepath = FINDINGS_DIR / "gabagool_trades_oos7.json"

    if not filepath.exists():
        raise FileNotFoundError(f"Trades file not found: {filepath}")

    logger.info(f"Loading trades from {filepath.name}")

    with open(filepath, 'r') as f:
        data = json.load(f)

    trades = data.get('trades', [])
    logger.info(f"  Loaded {len(trades):,} trades from {data.get('markets_traded', 0)} markets")

    # Convert to DataFrame with essential columns
    df = pd.DataFrame(trades)

    # Standardize column names
    column_map = {
        'slug': 'market_slug',
        'market_slug': 'market_slug',
    }

    for old_col, new_col in column_map.items():
        if old_col in df.columns and new_col not in df.columns:
            df[new_col] = df[old_col]

    # Ensure required columns exist
    required_cols = ['side', 'outcome', 'price', 'size', 'timestamp', 'market_slug']
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        logger.warning(f"Missing columns in trades: {missing}")

    # Convert timestamp to ms
    df['timestamp_ms'] = df['timestamp'] * 1000

    return df


def load_observer_data(dates: list = None) -> pd.DataFrame:
    """
    Load observer data for specified dates.

    Args:
        dates: List of date strings (e.g., ['20260129', '20260130'])

    Returns:
        Combined DataFrame of observer data
    """
    if dates is None:
        # Default to OOS7 dates
        dates = ['20260129', '20260130']

    dfs = []

    for date in dates:
        # Try different filename patterns
        patterns = [
            f"grid_obs_{date}.csv",
            f"grid_obs_{date}_recovered.csv",
        ]

        for pattern in patterns:
            # Check observer directory
            filepath = OBSERVER_DIR / pattern
            if filepath.exists():
                logger.info(f"Loading {filepath.name}")
                df = pd.read_csv(filepath, on_bad_lines='skip', low_memory=False)
                df['source_file'] = pattern
                dfs.append(df)
                break

            # Check findings/data directory
            filepath = FINDINGS_DIR / pattern
            if filepath.exists():
                logger.info(f"Loading from findings: {filepath.name}")
                df = pd.read_csv(filepath, on_bad_lines='skip', low_memory=False)
                df['source_file'] = pattern
                dfs.append(df)
                break

    if not dfs:
        raise FileNotFoundError(f"No observer data found for dates: {dates}")

    combined = pd.concat(dfs, ignore_index=True)

    # Sort by timestamp
    combined = combined.sort_values('timestamp_ms').reset_index(drop=True)

    logger.info(f"Combined observer data: {len(combined):,} rows")
    return combined


def cross_reference_trades_to_observer(
    trades_df: pd.DataFrame,
    observer_df: pd.DataFrame,
    tolerance_ms: int = 500
) -> pd.DataFrame:
    """
    Match Gabagool trade timestamps to nearest observer row.

    Uses pd.merge_asof for efficient nearest-neighbor matching.

    Args:
        trades_df: DataFrame with Gabagool trades (must have timestamp_ms, market_slug)
        observer_df: DataFrame with observer data (must have timestamp_ms, market_slug)
        tolerance_ms: Maximum time difference for matching (default 500ms)

    Returns:
        DataFrame with trade data joined to observer state at time of trade
    """
    logger.info(f"Cross-referencing {len(trades_df):,} trades with tolerance={tolerance_ms}ms")

    # Ensure timestamp_ms columns exist
    if 'timestamp_ms' not in trades_df.columns:
        trades_df = trades_df.copy()
        trades_df['timestamp_ms'] = trades_df['timestamp'] * 1000

    # Sort both DataFrames
    trades_sorted = trades_df.sort_values('timestamp_ms').copy()
    observer_sorted = observer_df.sort_values('timestamp_ms').copy()

    # Use merge_asof for nearest-neighbor matching per market
    matched_dfs = []

    markets = trades_sorted['market_slug'].unique()
    logger.info(f"  Processing {len(markets)} markets...")

    for slug in markets:
        trade_market = trades_sorted[trades_sorted['market_slug'] == slug].copy()
        obs_market = observer_sorted[observer_sorted['market_slug'] == slug].copy()

        if len(obs_market) == 0:
            logger.warning(f"  No observer data for market: {slug}")
            continue

        # Merge on timestamp within market
        matched = pd.merge_asof(
            trade_market,
            obs_market,
            on='timestamp_ms',
            direction='nearest',
            tolerance=tolerance_ms,
            suffixes=('_trade', '_obs')
        )

        matched_dfs.append(matched)

    if not matched_dfs:
        raise ValueError("No trades matched to observer data")

    result = pd.concat(matched_dfs, ignore_index=True)

    # Calculate match statistics
    total_trades = len(trades_df)
    matched_trades = result['binance_price'].notna().sum()
    match_rate = matched_trades / total_trades * 100

    logger.info(f"  Matched {matched_trades:,}/{total_trades:,} trades ({match_rate:.1f}%)")

    # Add time delta column
    if 'timestamp_ms_trade' in result.columns and 'timestamp_ms_obs' in result.columns:
        result['time_delta_ms'] = result['timestamp_ms_trade'] - result['timestamp_ms_obs']

    return result


def validate_cross_reference(
    matched_df: pd.DataFrame,
    min_match_rate: float = 0.95
) -> Dict[str, any]:
    """
    Validate the quality of trade-to-observer cross-reference.

    Args:
        matched_df: Output from cross_reference_trades_to_observer
        min_match_rate: Minimum acceptable match rate (default 95%)

    Returns:
        Dict with validation metrics and pass/fail status
    """
    total = len(matched_df)

    # Check for matched rows (have observer data)
    has_observer = matched_df['binance_price'].notna().sum()
    match_rate = has_observer / total

    # Time delta statistics
    if 'time_delta_ms' in matched_df.columns:
        time_deltas = matched_df['time_delta_ms'].dropna()
        delta_stats = {
            'mean_delta_ms': float(time_deltas.abs().mean()),
            'median_delta_ms': float(time_deltas.abs().median()),
            'max_delta_ms': float(time_deltas.abs().max()),
            'p95_delta_ms': float(time_deltas.abs().quantile(0.95)),
        }
    else:
        delta_stats = {}

    # Market coverage - handle different column names after merge
    slug_col = 'market_slug'
    if slug_col not in matched_df.columns:
        # Try alternative names from merge
        for col in ['market_slug_trade', 'market_slug_obs', 'slug']:
            if col in matched_df.columns:
                slug_col = col
                break

    total_markets = matched_df[slug_col].nunique() if slug_col in matched_df.columns else 0
    matched_markets = matched_df[matched_df['binance_price'].notna()][slug_col].nunique() if slug_col in matched_df.columns else 0

    validation = {
        'total_trades': total,
        'matched_trades': has_observer,
        'match_rate': match_rate,
        'total_markets': total_markets,
        'matched_markets': matched_markets,
        **delta_stats,
        'passed': match_rate >= min_match_rate,
    }

    logger.info("\nValidation Results:")
    logger.info(f"  Match rate: {match_rate*100:.1f}% (target: {min_match_rate*100:.0f}%)")
    logger.info(f"  Markets: {matched_markets}/{total_markets}")
    if 'mean_delta_ms' in validation:
        logger.info(f"  Mean time delta: {validation['mean_delta_ms']:.1f}ms")
        logger.info(f"  Median time delta: {validation['median_delta_ms']:.1f}ms")
    logger.info(f"  PASSED: {validation['passed']}")

    return validation


if __name__ == "__main__":
    # Test cross-reference functionality
    print("=" * 60)
    print("Testing Cross-Reference Module")
    print("=" * 60)

    # Load data
    try:
        trades_df = load_gabagool_trades()
        print(f"\nTrades loaded: {len(trades_df):,}")
        print(f"  Columns: {trades_df.columns.tolist()[:10]}")

        observer_df = load_observer_data()
        print(f"\nObserver loaded: {len(observer_df):,}")
        print(f"  Columns: {observer_df.columns.tolist()[:10]}")

        # Cross-reference
        matched = cross_reference_trades_to_observer(trades_df, observer_df)
        print(f"\nMatched trades: {len(matched):,}")

        # Validate
        validation = validate_cross_reference(matched)

        # Sample output
        print("\nSample matched trade:")
        sample = matched[matched['binance_price'].notna()].iloc[0]
        print(f"  Market: {sample['market_slug']}")
        print(f"  Trade price: ${sample['price']:.3f}")
        print(f"  Outcome: {sample['outcome']}")
        print(f"  BTC price at trade: ${sample['binance_price']:,.2f}")
        print(f"  Velocity: {sample.get('velocity_bps', 'N/A')} bps")

    except FileNotFoundError as e:
        print(f"Error: {e}")
        print("Run this from the polymarket-amm-bot directory")
