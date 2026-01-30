"""
Data Loader for Gabagool NN Training

Loads observer data from IS+OOS2+OOS5 for training
and OOS3+OOS4+OOS6 for validation.

Observer CSV columns (88 total):
- timestamp_ms, market_slug, time_remaining_secs
- binance_price, velocity_bps
- up_bid, up_ask, down_bid, down_ask, pair_cost
- data_source, velocity_zone, winner_offset, loser_offset
- grid_up_offset, grid_down_offset, grid_up_bid, grid_down_bid
- grid_up_filled, grid_down_filled, grid_pair_cost, grid_profit
- up_pos, down_pos, pairs, locked_profit
- cycles_this_market, cycles_total, cycles_pnl, cycle_just_completed
- spike_detected, spike_direction, spike_magnitude
- spike_loser_bid, expected_drop, velocity_signal, spike_vs_velocity
- acceleration_bps2, jerk_bps3, accel_aligned, signal_quality, momentum_5s
- Orderbook depth: up_bid_1-5, up_bid_size_1-5, up_ask_1-5, up_ask_size_1-5,
                   down_bid_1-5, down_bid_size_1-5, down_ask_1-5, down_ask_size_1-5
- up_imbalance, down_imbalance
"""

import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
import json
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Base paths
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
OBSERVER_DIR = PROJECT_ROOT / "research" / "observer"
FINDINGS_DIR = PROJECT_ROOT / "research" / "findings" / "data"


@dataclass
class DataSplit:
    """Container for train/validation data splits."""
    train_df: pd.DataFrame
    val_df: pd.DataFrame
    train_markets: List[str]
    val_markets: List[str]
    resolutions: Dict[str, str]
    whale_trades: Optional[Dict] = None


# Data split definitions (by date)
# Updated: Add OOS7 (Jan 29-30) to training for fresher data
TRAINING_DATES = [
    "20260116",  # IS
    "20260117",  # IS
    "20260118",  # OOS2
    "20260119",  # OOS2
    "20260126",  # OOS5
    "20260129",  # OOS7 (fresh data with full orderbook)
    "20260130",  # OOS7 (fresh data with full orderbook)
]

VALIDATION_DATES = [
    "20260120",  # OOS3
    "20260121",  # OOS3
    "20260122",  # OOS4
    "20260123",  # OOS4
    "20260124",  # OOS4
    "20260128",  # OOS6
]

# Observer CSV columns (from observer.py:651-686)
OBSERVER_COLUMNS = [
    'timestamp_ms', 'market_slug', 'time_remaining_secs',
    'binance_price', 'velocity_bps',
    'up_bid', 'up_ask', 'down_bid', 'down_ask', 'pair_cost',
    'data_source',
    'velocity_zone', 'winner_offset', 'loser_offset',
    'grid_up_offset', 'grid_down_offset',
    'grid_up_bid', 'grid_down_bid',
    'grid_up_filled', 'grid_down_filled',
    'grid_pair_cost', 'grid_profit',
    'up_pos', 'down_pos', 'pairs', 'locked_profit',
    'cycles_this_market', 'cycles_total', 'cycles_pnl',
    'cycle_just_completed',
    'spike_detected', 'spike_direction', 'spike_magnitude',
    'spike_loser_bid', 'expected_drop',
    'velocity_signal', 'spike_vs_velocity',
    'acceleration_bps2', 'jerk_bps3', 'accel_aligned',
    'signal_quality', 'momentum_5s',
    # Orderbook depth columns (5 levels per side, per token = 40 columns)
    'up_bid_1', 'up_bid_2', 'up_bid_3', 'up_bid_4', 'up_bid_5',
    'up_bid_size_1', 'up_bid_size_2', 'up_bid_size_3', 'up_bid_size_4', 'up_bid_size_5',
    'up_ask_1', 'up_ask_2', 'up_ask_3', 'up_ask_4', 'up_ask_5',
    'up_ask_size_1', 'up_ask_size_2', 'up_ask_size_3', 'up_ask_size_4', 'up_ask_size_5',
    'down_bid_1', 'down_bid_2', 'down_bid_3', 'down_bid_4', 'down_bid_5',
    'down_bid_size_1', 'down_bid_size_2', 'down_bid_size_3', 'down_bid_size_4', 'down_bid_size_5',
    'down_ask_1', 'down_ask_2', 'down_ask_3', 'down_ask_4', 'down_ask_5',
    'down_ask_size_1', 'down_ask_size_2', 'down_ask_size_3', 'down_ask_size_4', 'down_ask_size_5',
    'up_imbalance', 'down_imbalance',
]


def load_observer_csv(date: str) -> Optional[pd.DataFrame]:
    """Load observer CSV for a specific date."""
    # Try different filename patterns
    patterns = [
        f"grid_obs_{date}.csv",
        f"grid_obs_{date}_recovered.csv",
        f"grid_obs_oos5_recovered.csv" if date == "20260126" else None,
    ]

    for pattern in patterns:
        if pattern is None:
            continue
        filepath = OBSERVER_DIR / pattern
        if filepath.exists():
            logger.info(f"Loading {filepath.name}...")
            df = pd.read_csv(filepath, on_bad_lines='skip', low_memory=False)
            logger.info(f"  Loaded {len(df):,} rows")
            return df

    # Check findings/data directory as fallback
    for pattern in patterns:
        if pattern is None:
            continue
        filepath = FINDINGS_DIR / pattern
        if filepath.exists():
            logger.info(f"Loading from findings: {filepath.name}...")
            df = pd.read_csv(filepath, on_bad_lines='skip', low_memory=False)
            logger.info(f"  Loaded {len(df):,} rows")
            return df

    logger.warning(f"No observer data found for date {date}")
    return None


def load_resolutions() -> Dict[str, str]:
    """Load market resolution data (UP or DOWN winner)."""
    resolutions = {}

    # Try multiple resolution files
    resolution_files = [
        OBSERVER_DIR / "market_resolutions_verified.csv",
        OBSERVER_DIR / "market_resolutions.csv",
        FINDINGS_DIR / "oos6_resolutions.csv",
    ]

    for filepath in resolution_files:
        if filepath.exists():
            logger.info(f"Loading resolutions from {filepath.name}")
            df = pd.read_csv(filepath)

            # Handle different column names
            slug_col = 'slug' if 'slug' in df.columns else 'market_slug'
            winner_col = 'winner' if 'winner' in df.columns else 'resolution'

            if slug_col in df.columns and winner_col in df.columns:
                for _, row in df.iterrows():
                    slug = row[slug_col]
                    winner = row[winner_col]
                    if pd.notna(winner) and winner in ['UP', 'DOWN']:
                        resolutions[slug] = winner

    logger.info(f"Loaded {len(resolutions)} market resolutions")
    return resolutions


def load_whale_trades() -> Optional[Dict]:
    """Load gabagool's actual trades for label construction."""
    whale_file = FINDINGS_DIR / "whale_trades_oos6.json"

    if whale_file.exists():
        logger.info("Loading whale trades...")
        with open(whale_file, 'r') as f:
            data = json.load(f)

        # Extract gabagool's trades
        if 'gabagool' in data:
            gabagool = data['gabagool']
            logger.info(f"  Gabagool: {gabagool.get('oos6_btc15m_trades', 0)} trades in {gabagool.get('markets_traded', 0)} markets")
            return gabagool

    logger.warning("No whale trades file found")
    return None


def load_data_by_dates(dates: List[str]) -> pd.DataFrame:
    """Load and concatenate observer data for specified dates."""
    dfs = []

    for date in dates:
        df = load_observer_csv(date)
        if df is not None:
            dfs.append(df)

    if not dfs:
        raise ValueError(f"No data loaded for dates: {dates}")

    combined = pd.concat(dfs, ignore_index=True)

    # Remove duplicates
    combined = combined.drop_duplicates(subset=['timestamp_ms', 'market_slug'])

    # Sort by time
    combined = combined.sort_values(['market_slug', 'timestamp_ms']).reset_index(drop=True)

    logger.info(f"Combined data: {len(combined):,} rows from {dates}")
    return combined


def filter_valid_markets(df: pd.DataFrame, min_duration_secs: float = 300,
                        min_start_time: float = 840) -> pd.DataFrame:
    """Filter to markets with sufficient data coverage."""
    valid_slugs = []

    for slug, mdf in df.groupby('market_slug'):
        max_time = mdf['time_remaining_secs'].max()
        min_time = mdf['time_remaining_secs'].min()
        duration = max_time - min_time

        if duration >= min_duration_secs and max_time >= min_start_time:
            valid_slugs.append(slug)

    df_filtered = df[df['market_slug'].isin(valid_slugs)].copy()
    logger.info(f"Filtered to {len(valid_slugs)} valid markets ({len(df_filtered):,} rows)")
    return df_filtered


def clean_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Clean and convert numeric columns."""
    numeric_cols = [
        'timestamp_ms', 'time_remaining_secs', 'binance_price', 'velocity_bps',
        'up_bid', 'up_ask', 'down_bid', 'down_ask', 'pair_cost',
        'spike_magnitude', 'acceleration_bps2', 'jerk_bps3', 'momentum_5s',
        'up_imbalance', 'down_imbalance',
    ]

    # Add orderbook columns
    for side in ['up', 'down']:
        for book_type in ['bid', 'ask']:
            for level in range(1, 6):
                numeric_cols.append(f'{side}_{book_type}_{level}')
                numeric_cols.append(f'{side}_{book_type}_size_{level}')

    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    # Fill NaN with defaults
    default_values = {
        'velocity_bps': 0.0,
        'spike_magnitude': 0.0,
        'acceleration_bps2': 0.0,
        'jerk_bps3': 0.0,
        'momentum_5s': 0.0,
        'up_imbalance': 0.0,
        'down_imbalance': 0.0,
    }

    for col, default in default_values.items():
        if col in df.columns:
            df[col] = df[col].fillna(default)

    # Fill orderbook columns with 0
    for col in df.columns:
        if 'bid_size' in col or 'ask_size' in col:
            df[col] = df[col].fillna(0)
        elif any(x in col for x in ['_bid_', '_ask_']):
            # Price columns - forward fill then 0
            df[col] = df[col].ffill().fillna(0)

    return df


def load_training_data() -> DataSplit:
    """
    Load all data with proper train/validation splits.

    Training: IS+OOS2 (Jan 16-19) + OOS5 (Jan 26)
    Validation: OOS3+OOS4 (Jan 20-24) + OOS6 (Jan 28-29)
    """
    logger.info("=" * 60)
    logger.info("Loading Training Data")
    logger.info("=" * 60)

    # Load resolutions first
    resolutions = load_resolutions()

    # Load whale trades
    whale_trades = load_whale_trades()

    # Load training data
    logger.info("\nLoading TRAINING data (IS+OOS2+OOS5)...")
    train_df = load_data_by_dates(TRAINING_DATES)
    train_df = clean_numeric_columns(train_df)
    train_df = filter_valid_markets(train_df)

    # Add resolutions
    train_df['resolution'] = train_df['market_slug'].map(resolutions)
    train_df = train_df[train_df['resolution'].isin(['UP', 'DOWN'])]
    train_markets = train_df['market_slug'].unique().tolist()

    logger.info(f"Training: {len(train_df):,} rows, {len(train_markets)} markets")

    # Load validation data
    logger.info("\nLoading VALIDATION data (OOS3+OOS4+OOS6)...")
    val_df = load_data_by_dates(VALIDATION_DATES)
    val_df = clean_numeric_columns(val_df)
    val_df = filter_valid_markets(val_df)

    # Add resolutions
    val_df['resolution'] = val_df['market_slug'].map(resolutions)
    val_df = val_df[val_df['resolution'].isin(['UP', 'DOWN'])]
    val_markets = val_df['market_slug'].unique().tolist()

    logger.info(f"Validation: {len(val_df):,} rows, {len(val_markets)} markets")

    return DataSplit(
        train_df=train_df,
        val_df=val_df,
        train_markets=train_markets,
        val_markets=val_markets,
        resolutions=resolutions,
        whale_trades=whale_trades,
    )


def get_market_data(df: pd.DataFrame, slug: str) -> pd.DataFrame:
    """Extract and sort data for a single market."""
    mdf = df[df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)
    return mdf


def get_sample_rate(df: pd.DataFrame) -> float:
    """Estimate sample rate in Hz from timestamp deltas."""
    if len(df) < 10:
        return 5.0  # Default 5Hz

    # Use median delta
    deltas = df['timestamp_ms'].diff().dropna()
    median_delta_ms = deltas.median()

    if median_delta_ms > 0:
        return 1000.0 / median_delta_ms
    return 5.0


if __name__ == "__main__":
    # Test data loading
    data = load_training_data()

    print("\n" + "=" * 60)
    print("DATA SUMMARY")
    print("=" * 60)
    print(f"Training rows:     {len(data.train_df):,}")
    print(f"Training markets:  {len(data.train_markets)}")
    print(f"Validation rows:   {len(data.val_df):,}")
    print(f"Validation markets: {len(data.val_markets)}")
    print(f"Resolutions:       {len(data.resolutions)}")

    if data.whale_trades:
        print(f"Whale trades:      {len(data.whale_trades.get('trades', []))}")

    # Show sample
    print("\nSample columns:")
    print(data.train_df.columns.tolist()[:20])
