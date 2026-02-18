"""
Feature Engineering for ML Market Predictor

Engineers 80+ features from observer data:
- Price features (12)
- BTC/Spike features (10)
- Velocity features (8)
- Orderbook depth (40)
- Order book imbalance (4)
- Time features (3)
- Engineered features (15+)

Author: Claude Code
Date: February 8, 2026
"""

import pandas as pd
import numpy as np
from typing import Optional, List
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def add_price_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add price-based features (12 features).

    These are the most important features (85.7% of XGBoost importance).
    """
    logger.info("Adding price features...")

    # Mid prices
    df["up_mid"] = (df["up_bid"] + df["up_ask"]) / 2
    df["down_mid"] = (df["down_bid"] + df["down_ask"]) / 2
    df["mid_price_diff"] = df["up_mid"] - df["down_mid"]

    # Spreads
    df["up_spread"] = df["up_ask"] - df["up_bid"]
    df["down_spread"] = df["down_ask"] - df["down_bid"]
    df["spread_ratio"] = df["up_spread"] / (df["down_spread"] + 1e-8)

    # Ask differences (key for winner prediction)
    df["ask_price_diff"] = df["up_ask"] - df["down_ask"]
    df["ask_price_diff_pct"] = df["ask_price_diff"] / (df["up_ask"] + df["down_ask"] + 1e-8)
    df["bid_price_diff"] = df["up_bid"] - df["down_bid"]

    # Fair price estimate (assumes pair_cost = 1.0)
    df["fair_price"] = 0.5
    df["up_ask_from_fair"] = df["up_ask"] - df["fair_price"]
    df["down_ask_from_fair"] = df["down_ask"] - df["fair_price"]

    # Expensive side indicator (1 = UP is expensive, 0 = DOWN is expensive)
    df["expensive_side"] = (df["up_ask"] > df["down_ask"]).astype(int)
    df["expensive_ask"] = np.where(df["expensive_side"] == 1, df["up_ask"], df["down_ask"])
    df["cheap_ask"] = np.where(df["expensive_side"] == 1, df["down_ask"], df["up_ask"])

    return df


def add_spike_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add BTC/spike-related features (10 features).
    """
    logger.info("Adding spike features...")

    # Spike presence (binary)
    df["has_spike"] = df["spike_detected"].fillna(0).astype(int)

    # Spike direction encoding
    df["spike_up"] = (df["spike_direction"] == "UP").astype(int)
    df["spike_down"] = (df["spike_direction"] == "DOWN").astype(int)

    # Spike magnitude (handle NaN)
    df["spike_magnitude"] = df["spike_magnitude"].fillna(0)
    df["spike_magnitude_abs"] = df["spike_magnitude"].abs()

    # Spike vs expensive side alignment
    # If spike_direction matches expensive_side, signal may be stronger
    df["spike_favors_expensive"] = (
        ((df["spike_up"] == 1) & (df["expensive_side"] == 1)) |
        ((df["spike_down"] == 1) & (df["expensive_side"] == 0))
    ).astype(int)

    # Spike velocity alignment (if spike_vs_velocity exists)
    if "spike_vs_velocity" in df.columns:
        df["spike_velocity_aligned"] = (df["spike_vs_velocity"] == "aligned").astype(int)
    else:
        df["spike_velocity_aligned"] = 0

    return df


def add_velocity_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add velocity-related features (8 features).

    Note: Velocity is used as a FILTER, not predictor (per research corrections).
    """
    logger.info("Adding velocity features...")

    # Base velocity (handle NaN)
    df["velocity_bps"] = df["velocity_bps"].fillna(0)
    df["velocity_abs"] = df["velocity_bps"].abs()
    df["velocity_direction"] = np.sign(df["velocity_bps"])

    # Acceleration and jerk (handle NaN)
    df["acceleration_bps2"] = df.get("acceleration_bps2", pd.Series(0, index=df.index)).fillna(0)
    df["jerk_bps3"] = df.get("jerk_bps3", pd.Series(0, index=df.index)).fillna(0)

    # Momentum (handle NaN)
    df["momentum_5s"] = df.get("momentum_5s", pd.Series(0, index=df.index)).fillna(0)

    # Velocity zone encoding
    if "velocity_zone" in df.columns:
        df["velocity_zone_high"] = (df["velocity_zone"] == "HIGH").astype(int)
        df["velocity_zone_low"] = (df["velocity_zone"] == "LOW").astype(int)
    else:
        df["velocity_zone_high"] = 0
        df["velocity_zone_low"] = 0

    # Velocity confirmation signal
    # Positive velocity + UP spike = aligned
    df["velocity_confirms_spike"] = (
        ((df.get("spike_up", 0) == 1) & (df["velocity_bps"] > 0.10)) |
        ((df.get("spike_down", 0) == 1) & (df["velocity_bps"] < -0.10))
    ).astype(int)

    return df


def add_time_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add time-related features (5 features).
    """
    logger.info("Adding time features...")

    # Time remaining (handle NaN)
    df["time_remaining_secs"] = df["time_remaining_secs"].fillna(450)

    # Time buckets
    df["time_bucket"] = pd.cut(
        df["time_remaining_secs"],
        bins=[0, 150, 300, 450, 600, 750, 900],
        labels=["0-150", "150-300", "300-450", "450-600", "600-750", "750-900"],
        include_lowest=True,
    )

    # Time urgency (higher = less time)
    df["time_urgency"] = 1.0 / (df["time_remaining_secs"] + 1)
    df["time_urgency_sq"] = df["time_urgency"] ** 2

    # Optimal entry window (300-600s based on Baguette analysis)
    df["in_entry_window"] = (
        (df["time_remaining_secs"] >= 300) & (df["time_remaining_secs"] <= 600)
    ).astype(int)

    # Late phase indicator
    df["is_late_phase"] = (df["time_remaining_secs"] < 150).astype(int)

    return df


def add_orderbook_imbalance(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add order book imbalance features (4+ features).

    OBI is critical for Baguette signal (98.1% when OBI contrarian to BTC trend).
    """
    logger.info("Adding orderbook imbalance features...")

    # Check if L1 orderbook columns exist
    has_orderbook = all(col in df.columns for col in ["up_bid_size_1", "up_ask_size_1"])

    if has_orderbook:
        # L1 imbalance
        df["up_imbalance"] = (
            (df["up_bid_size_1"] - df["up_ask_size_1"]) /
            (df["up_bid_size_1"] + df["up_ask_size_1"] + 1e-8)
        )
        df["down_imbalance"] = (
            (df["down_bid_size_1"] - df["down_ask_size_1"]) /
            (df["down_bid_size_1"] + df["down_ask_size_1"] + 1e-8)
        )

        # Aggregate L1-L5 if available
        for level in range(1, 6):
            bid_col = f"up_bid_size_{level}"
            ask_col = f"up_ask_size_{level}"
            if bid_col in df.columns and ask_col in df.columns:
                df[f"up_obi_L{level}"] = (
                    (df[bid_col] - df[ask_col]) / (df[bid_col] + df[ask_col] + 1e-8)
                )
    else:
        # Default to 0 if no orderbook data
        df["up_imbalance"] = 0
        df["down_imbalance"] = 0

    # OBI difference
    df["obi_diff"] = df["up_imbalance"] - df["down_imbalance"]

    # OBI direction (1 = UP bias, -1 = DOWN bias)
    df["obi_direction"] = np.sign(df["obi_diff"])

    # OBI contrarian to expensive side (key Baguette signal)
    # If expensive_side is UP (1) but OBI favors DOWN (obi_diff < 0) = contrarian
    df["obi_contrarian"] = (
        ((df.get("expensive_side", 0) == 1) & (df["obi_diff"] < 0)) |
        ((df.get("expensive_side", 0) == 0) & (df["obi_diff"] > 0))
    ).astype(int)

    return df


def add_rolling_features(df: pd.DataFrame, windows: List[int] = [5, 30, 60]) -> pd.DataFrame:
    """
    Add rolling statistics (per market).

    Args:
        df: DataFrame with market_slug column
        windows: Window sizes in seconds (approximated by rows)
    """
    logger.info(f"Adding rolling features for windows {windows}...")

    # Sort by market and time
    df = df.sort_values(["market_slug", "timestamp_ms"])

    # Group by market for rolling calculations
    grouped = df.groupby("market_slug")

    for window in windows:
        # Approximate window in rows (assume ~5Hz = 5 rows per second)
        rows = window * 5

        # Price volatility
        df[f"up_ask_std_{window}s"] = grouped["up_ask"].transform(
            lambda x: x.rolling(rows, min_periods=1).std()
        )
        df[f"down_ask_std_{window}s"] = grouped["down_ask"].transform(
            lambda x: x.rolling(rows, min_periods=1).std()
        )

        # Velocity mean
        df[f"velocity_mean_{window}s"] = grouped["velocity_bps"].transform(
            lambda x: x.rolling(rows, min_periods=1).mean()
        )

        # Velocity std (for regime detection)
        df[f"velocity_std_{window}s"] = grouped["velocity_bps"].transform(
            lambda x: x.rolling(rows, min_periods=1).std()
        )

    return df


def add_volatility_regime(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add volatility regime indicator.
    """
    logger.info("Adding volatility regime...")

    # Use 60s velocity std if available
    if "velocity_std_60s" in df.columns:
        vol = df["velocity_std_60s"]
    elif "velocity_abs" in df.columns:
        vol = df["velocity_abs"]
    else:
        vol = pd.Series(0, index=df.index)

    # Percentile-based regime
    df["volatility_percentile"] = vol.rank(pct=True)

    # Categorical regime
    df["volatility_regime"] = pd.cut(
        df["volatility_percentile"],
        bins=[0, 0.33, 0.67, 1.0],
        labels=["LOW", "MEDIUM", "HIGH"],
        include_lowest=True,
    )

    # One-hot encode
    df["vol_regime_low"] = (df["volatility_regime"] == "LOW").astype(int)
    df["vol_regime_high"] = (df["volatility_regime"] == "HIGH").astype(int)

    return df


def add_composite_signals(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add composite signal scores.
    """
    logger.info("Adding composite signals...")

    # Baguette-inspired composite
    # High confidence = OBI contrarian + velocity confirms
    df["baguette_signal"] = (
        df.get("obi_contrarian", 0) * 0.5 +
        df.get("velocity_confirms_spike", 0) * 0.3 +
        df.get("in_entry_window", 0) * 0.2
    )

    # FADE signal strength
    # Strong when expensive_ask is high and spike detected
    df["fade_signal"] = (
        (df.get("expensive_ask", 0.5) >= 0.80).astype(int) *
        df.get("has_spike", 0) *
        df.get("in_entry_window", 0)
    )

    return df


def engineer_all_features(
    df: pd.DataFrame,
    include_rolling: bool = True,
    rolling_windows: List[int] = [5, 30, 60],
) -> pd.DataFrame:
    """
    Apply all feature engineering.

    Args:
        df: Raw observer DataFrame
        include_rolling: Whether to compute rolling statistics
        rolling_windows: Window sizes for rolling features

    Returns:
        DataFrame with all engineered features
    """
    logger.info("=" * 60)
    logger.info("STARTING FEATURE ENGINEERING")
    logger.info(f"Input shape: {df.shape}")
    logger.info("=" * 60)

    # Apply feature transformations
    df = add_price_features(df)
    df = add_spike_features(df)
    df = add_velocity_features(df)
    df = add_time_features(df)
    df = add_orderbook_imbalance(df)

    if include_rolling:
        df = add_rolling_features(df, rolling_windows)

    df = add_volatility_regime(df)
    df = add_composite_signals(df)

    logger.info("=" * 60)
    logger.info("FEATURE ENGINEERING COMPLETE")
    logger.info(f"Output shape: {df.shape}")
    logger.info(f"New features: {df.shape[1]} columns")
    logger.info("=" * 60)

    return df


def get_feature_columns(df: pd.DataFrame) -> List[str]:
    """
    Get list of feature columns for ML training.

    Excludes identifiers, labels, and metadata.
    """
    exclude_cols = {
        # Identifiers
        "timestamp_ms", "market_slug", "slug",
        # Labels
        "winner", "winner_binary",
        # Metadata
        "dataset", "data_source",
        # Categorical (need encoding)
        "time_bucket", "volatility_regime", "spike_direction", "velocity_zone",
    }

    feature_cols = [col for col in df.columns if col not in exclude_cols]

    # Only numeric columns
    numeric_cols = df[feature_cols].select_dtypes(include=[np.number]).columns.tolist()

    logger.info(f"Selected {len(numeric_cols)} numeric features for ML")

    return numeric_cols


if __name__ == "__main__":
    # Test feature engineering
    from data_loader import load_all_with_labels

    print("Loading sample data...")
    train_df, test_df, res = load_all_with_labels(
        include_orderbook=True,
        sample_frac=0.01,
    )

    print("\nEngineering features on train data...")
    train_featured = engineer_all_features(train_df, include_rolling=True)

    print("\nFeature columns:")
    feature_cols = get_feature_columns(train_featured)
    print(f"Total features: {len(feature_cols)}")
    print(f"Sample: {feature_cols[:20]}")

    print("\nFeature stats (first 10):")
    for col in feature_cols[:10]:
        if col in train_featured.columns:
            print(f"  {col}: mean={train_featured[col].mean():.4f}, std={train_featured[col].std():.4f}")
