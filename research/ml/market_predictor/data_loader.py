"""
Data Loader for ML Market Predictor

Loads observer data for training and testing with proper 60/40 split:
- Training (60%): IS+OOS2 + OOS7 + OOS9 = 134 hours
- Testing (40%): OOS3+OOS4 + OOS6 + OOS8 = 89 hours

Author: Claude Code
Date: February 8, 2026
"""

import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, Tuple, Optional, List
from dataclasses import dataclass
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Base paths (use project root)
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent  # Up to polymarket-amm-bot
OBSERVER_DIR = PROJECT_ROOT / "research" / "observer"
RESOLUTIONS_FILE = OBSERVER_DIR / "market_resolutions_verified.csv"

# Dataset definitions
DATASETS = {
    # Training datasets (60%)
    "IS+OOS2": {
        "files": [OBSERVER_DIR / "PROTECTED_grid_obs_is_oos2_combined.csv"],
        "split": "train",
        "period": "Jan 16-19",
        "hours": 69,
    },
    "OOS7": {
        "files": [
            OBSERVER_DIR / "grid_obs_20260129.csv",
            OBSERVER_DIR / "grid_obs_20260130.csv",
        ],
        "split": "train",
        "period": "Jan 29-30",
        "hours": 19,
    },
    "OOS9": {
        "files": [OBSERVER_DIR / "grid_obs_oos9.csv"],
        "split": "train",
        "period": "Feb 1-3",
        "hours": 46,
    },
    # Testing datasets (40%)
    "OOS3+OOS4": {
        "files": [OBSERVER_DIR / "PROTECTED_grid_obs_oos3_oos4_combined.csv"],
        "split": "test",
        "period": "Jan 22-24",
        "hours": 47,
    },
    "OOS6": {
        "files": [OBSERVER_DIR / "grid_obs_20260128.csv"],
        "split": "test",
        "period": "Jan 28",
        "hours": 24,
    },
    "OOS8": {
        "files": [OBSERVER_DIR / "grid_obs_20260131.csv"],
        "split": "test",
        "period": "Jan 31",
        "hours": 18,
    },
}

# Core columns to load (reduces memory)
CORE_COLUMNS = [
    # Identifiers
    "timestamp_ms",
    "market_slug",
    "time_remaining_secs",
    # Prices
    "binance_price",
    "up_bid",
    "up_ask",
    "down_bid",
    "down_ask",
    "pair_cost",
    # Velocity
    "velocity_bps",
    "velocity_zone",
    "acceleration_bps2",
    "jerk_bps3",
    "momentum_5s",
    # Spike detection
    "spike_detected",
    "spike_direction",
    "spike_magnitude",
    # Signal quality
    "signal_quality",
    "spike_vs_velocity",
]

# Orderbook depth columns (L1-L5)
ORDERBOOK_COLUMNS = []
for side in ["up", "down"]:
    for level in range(1, 6):
        ORDERBOOK_COLUMNS.extend([
            f"{side}_bid_{level}",
            f"{side}_ask_{level}",
            f"{side}_bid_size_{level}",
            f"{side}_ask_size_{level}",
        ])


@dataclass
class DatasetInfo:
    """Information about a loaded dataset."""
    name: str
    split: str
    rows: int
    markets: int
    hours: float
    period: str


def load_resolutions() -> pd.DataFrame:
    """Load market resolutions (ground truth labels)."""
    logger.info(f"Loading resolutions from {RESOLUTIONS_FILE}")
    res_df = pd.read_csv(RESOLUTIONS_FILE)

    # Create binary winner column (1 = UP wins, 0 = DOWN wins)
    res_df["winner_binary"] = (res_df["winner"] == "UP").astype(int)

    logger.info(f"Loaded {len(res_df)} market resolutions")
    logger.info(f"  UP wins: {res_df['winner_binary'].sum()}")
    logger.info(f"  DOWN wins: {len(res_df) - res_df['winner_binary'].sum()}")

    return res_df


def load_single_dataset(
    name: str,
    include_orderbook: bool = False,
    sample_frac: Optional[float] = None,
) -> Tuple[pd.DataFrame, DatasetInfo]:
    """
    Load a single dataset by name.

    Args:
        name: Dataset name (e.g., "IS+OOS2", "OOS7", "OOS9")
        include_orderbook: Whether to include L1-L5 orderbook columns
        sample_frac: Optional fraction to sample (for testing)

    Returns:
        Tuple of (DataFrame, DatasetInfo)
    """
    if name not in DATASETS:
        raise ValueError(f"Unknown dataset: {name}. Available: {list(DATASETS.keys())}")

    dataset = DATASETS[name]
    files = dataset["files"]

    # Select columns
    columns = CORE_COLUMNS.copy()
    if include_orderbook:
        columns.extend(ORDERBOOK_COLUMNS)

    logger.info(f"Loading {name} from {len(files)} file(s)...")

    dfs = []
    for f in files:
        if not f.exists():
            logger.warning(f"File not found: {f}")
            continue

        # Read with selected columns (handle missing columns gracefully)
        try:
            df = pd.read_csv(f, usecols=lambda c: c in columns)
            if sample_frac:
                df = df.sample(frac=sample_frac, random_state=42)
            dfs.append(df)
            logger.info(f"  Loaded {len(df):,} rows from {f.name}")
        except Exception as e:
            logger.error(f"Error loading {f}: {e}")
            continue

    if not dfs:
        raise ValueError(f"No data loaded for {name}")

    # Combine
    df = pd.concat(dfs, ignore_index=True)

    # Add dataset label
    df["dataset"] = name

    # Calculate info
    n_markets = df["market_slug"].nunique()
    hours = df["timestamp_ms"].nunique() / (3600 * 1000) if len(df) > 0 else 0

    info = DatasetInfo(
        name=name,
        split=dataset["split"],
        rows=len(df),
        markets=n_markets,
        hours=round(hours, 1),
        period=dataset["period"],
    )

    logger.info(f"  Total: {len(df):,} rows, {n_markets} markets, ~{info.hours}h")

    return df, info


def load_train_test_data(
    include_orderbook: bool = False,
    sample_frac: Optional[float] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, List[DatasetInfo]]:
    """
    Load all training and testing data with 60/40 split.

    Args:
        include_orderbook: Whether to include L1-L5 orderbook columns
        sample_frac: Optional fraction to sample (for testing)

    Returns:
        Tuple of (train_df, test_df, list of DatasetInfo)
    """
    train_dfs = []
    test_dfs = []
    infos = []

    for name, dataset in DATASETS.items():
        try:
            df, info = load_single_dataset(name, include_orderbook, sample_frac)
            infos.append(info)

            if dataset["split"] == "train":
                train_dfs.append(df)
            else:
                test_dfs.append(df)
        except Exception as e:
            logger.error(f"Failed to load {name}: {e}")
            continue

    train_df = pd.concat(train_dfs, ignore_index=True) if train_dfs else pd.DataFrame()
    test_df = pd.concat(test_dfs, ignore_index=True) if test_dfs else pd.DataFrame()

    logger.info("=" * 60)
    logger.info("DATA LOADING COMPLETE")
    logger.info(f"Training: {len(train_df):,} rows")
    logger.info(f"Testing:  {len(test_df):,} rows")
    logger.info(f"Split ratio: {len(train_df)/(len(train_df)+len(test_df))*100:.1f}% / {len(test_df)/(len(train_df)+len(test_df))*100:.1f}%")
    logger.info("=" * 60)

    return train_df, test_df, infos


def merge_with_resolutions(
    df: pd.DataFrame,
    resolutions: pd.DataFrame,
) -> pd.DataFrame:
    """
    Merge observer data with market resolutions.

    Args:
        df: Observer DataFrame with 'market_slug' column
        resolutions: Resolutions DataFrame with 'slug' and 'winner' columns

    Returns:
        Merged DataFrame with winner labels
    """
    logger.info("Merging with resolutions...")

    # Merge on slug
    merged = df.merge(
        resolutions[["slug", "winner", "winner_binary"]],
        left_on="market_slug",
        right_on="slug",
        how="left",
    )

    # Drop duplicate slug column
    merged = merged.drop(columns=["slug"], errors="ignore")

    # Check for unresolved markets
    missing = merged["winner"].isna().sum()
    if missing > 0:
        pct = missing / len(merged) * 100
        logger.warning(f"Missing resolutions for {missing:,} rows ({pct:.1f}%)")

    logger.info(f"Merged: {len(merged):,} rows with {merged['winner'].notna().sum():,} resolved")

    return merged


def load_all_with_labels(
    include_orderbook: bool = False,
    sample_frac: Optional[float] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Load all data with resolution labels.

    Returns:
        Tuple of (train_df, test_df, resolutions_df)
    """
    # Load resolutions first
    resolutions = load_resolutions()

    # Load train/test split
    train_df, test_df, infos = load_train_test_data(include_orderbook, sample_frac)

    # Merge with labels
    train_df = merge_with_resolutions(train_df, resolutions)
    test_df = merge_with_resolutions(test_df, resolutions)

    # Summary
    print("\n" + "=" * 60)
    print("DATASET SUMMARY")
    print("=" * 60)
    print(f"{'Dataset':<12} {'Split':<6} {'Rows':>12} {'Markets':>8} {'Hours':>6}")
    print("-" * 60)
    for info in infos:
        print(f"{info.name:<12} {info.split:<6} {info.rows:>12,} {info.markets:>8} {info.hours:>6}")
    print("-" * 60)
    train_markets = train_df["market_slug"].nunique() if len(train_df) > 0 else 0
    test_markets = test_df["market_slug"].nunique() if len(test_df) > 0 else 0
    print(f"{'TRAIN TOTAL':<12} {'train':<6} {len(train_df):>12,} {train_markets:>8}")
    print(f"{'TEST TOTAL':<12} {'test':<6} {len(test_df):>12,} {test_markets:>8}")
    print("=" * 60)

    return train_df, test_df, resolutions


def get_market_level_data(df: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate to market-level for classification.

    Takes one row per market (e.g., final observation) with label.

    Args:
        df: Row-level observer data with labels

    Returns:
        Market-level DataFrame with one row per market
    """
    logger.info("Aggregating to market level...")

    # Get last observation per market (closest to resolution)
    market_df = df.sort_values("time_remaining_secs").groupby("market_slug").first().reset_index()

    logger.info(f"Aggregated to {len(market_df)} markets")

    return market_df


if __name__ == "__main__":
    # Test loading
    print("Testing data loader...")

    # Quick test with sampling
    train_df, test_df, res = load_all_with_labels(
        include_orderbook=False,
        sample_frac=0.01,  # 1% sample for testing
    )

    print(f"\nTrain columns ({len(train_df.columns)}): {list(train_df.columns[:15])}...")
    print(f"Test columns ({len(test_df.columns)}): {list(test_df.columns[:15])}...")

    # Check label distribution
    if "winner_binary" in train_df.columns:
        print(f"\nTrain label distribution:")
        print(train_df["winner_binary"].value_counts(normalize=True))
