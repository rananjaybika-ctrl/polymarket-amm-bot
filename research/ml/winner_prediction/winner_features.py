"""
Winner-Specific Features for Prediction Model

Features specifically designed to predict market winners (UP/DOWN).
These complement the existing features in gabagool_nn/feature_engineer.py.

Key feature categories:
1. Orderbook Imbalance Ratio (OBI) - asymmetry in bid/ask depth
2. BTC Momentum - price movement in multiple windows
3. Velocity Persistence - directional consistency
4. Expensive Side - which token costs more (usually predicted winner)
5. Time-Weighted Features - signals that become more important near resolution
"""

import pandas as pd
import numpy as np
from typing import Optional, List, Tuple
from dataclasses import dataclass
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class WinnerFeatureConfig:
    """Configuration for winner-specific feature engineering."""
    # Momentum windows in samples (at 5Hz)
    momentum_windows: List[int] = None  # [5, 25, 75] = 1s, 5s, 15s

    # Rolling window for velocity persistence
    velocity_persistence_window: int = 25  # 5 seconds

    # Time remaining thresholds for phase features
    time_phases: List[int] = None  # [900, 600, 300, 120, 60] seconds

    # Orderbook depth levels to use
    orderbook_levels: int = 5

    # Smoothing for noisy signals
    ewm_span: int = 5

    def __post_init__(self):
        if self.momentum_windows is None:
            self.momentum_windows = [5, 25, 75]  # 1s, 5s, 15s at 5Hz
        if self.time_phases is None:
            self.time_phases = [900, 600, 300, 120, 60]


def compute_orderbook_imbalance_ratio(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute Orderbook Imbalance Ratio (OBI) for both sides.

    OBI = (total_bid - total_ask) / (total_bid + total_ask)
    Range: [-1, 1], where positive means more bid pressure

    The theory: The side with more bid pressure (buying interest) is more
    likely to be the predicted winner.
    """
    features = pd.DataFrame(index=df.index)

    for side in ['up', 'down']:
        # Check for total depth columns first
        total_bid_col = f'{side}_total_bid_depth'
        total_ask_col = f'{side}_total_ask_depth'

        if total_bid_col in df.columns and total_ask_col in df.columns:
            total_bid = df[total_bid_col]
            total_ask = df[total_ask_col]
        else:
            # Compute from level-by-level data
            bid_cols = [f'{side}_bid_size_{i}' for i in range(1, 6) if f'{side}_bid_size_{i}' in df.columns]
            ask_cols = [f'{side}_ask_size_{i}' for i in range(1, 6) if f'{side}_ask_size_{i}' in df.columns]

            if bid_cols:
                total_bid = df[bid_cols].sum(axis=1)
            else:
                total_bid = pd.Series(0, index=df.index)

            if ask_cols:
                total_ask = df[ask_cols].sum(axis=1)
            else:
                total_ask = pd.Series(0, index=df.index)

        # Compute OBI
        denominator = total_bid + total_ask + 1e-8  # Avoid division by zero
        features[f'{side}_obi'] = (total_bid - total_ask) / denominator

    # OBI difference (UP - DOWN) - positive means UP has more buying pressure
    if 'up_obi' in features.columns and 'down_obi' in features.columns:
        features['obi_diff'] = features['up_obi'] - features['down_obi']

    return features


def compute_btc_momentum_features(
    df: pd.DataFrame,
    config: WinnerFeatureConfig
) -> pd.DataFrame:
    """
    Compute BTC momentum features at multiple time windows.

    Momentum is the price change over a window. Direction predicts winner:
    - Positive momentum -> price going up -> UP likely to win
    - Negative momentum -> price going down -> DOWN likely to win
    """
    features = pd.DataFrame(index=df.index)

    if 'binance_price' not in df.columns:
        logger.warning("binance_price not found, skipping momentum features")
        return features

    price = df['binance_price']

    for window in config.momentum_windows:
        # Absolute momentum (price change in $)
        features[f'btc_momentum_{window}'] = price.diff(window).fillna(0)

        # Percentage change
        pct_change = price.pct_change(window) * 100  # In percentage
        features[f'btc_pct_change_{window}'] = pct_change.fillna(0)

        # Momentum direction sign
        features[f'btc_momentum_sign_{window}'] = np.sign(features[f'btc_momentum_{window}'])

        # EMA-smoothed momentum (less noisy)
        features[f'btc_momentum_ema_{window}'] = (
            features[f'btc_momentum_{window}']
            .ewm(span=config.ewm_span, adjust=False)
            .mean()
        )

    # Momentum consistency (do all windows agree on direction?)
    momentum_cols = [f'btc_momentum_sign_{w}' for w in config.momentum_windows]
    if len(momentum_cols) > 1:
        features['momentum_consistency'] = (
            features[momentum_cols].sum(axis=1).abs() / len(momentum_cols)
        )

    return features


def compute_velocity_persistence(
    df: pd.DataFrame,
    config: WinnerFeatureConfig
) -> pd.DataFrame:
    """
    Compute velocity persistence features.

    Velocity persistence measures how consistently velocity points in one direction.
    High persistence suggests a clear trend, making winner prediction easier.
    """
    features = pd.DataFrame(index=df.index)

    if 'velocity_bps' not in df.columns:
        logger.warning("velocity_bps not found, skipping velocity persistence")
        return features

    velocity = df['velocity_bps']
    window = config.velocity_persistence_window

    # Rolling mean of velocity sign (persistence)
    vel_sign = np.sign(velocity)
    features['velocity_sign_mean'] = vel_sign.rolling(window, min_periods=1).mean()

    # Percentage of positive velocities in window
    features['velocity_positive_ratio'] = (velocity > 0).rolling(window, min_periods=1).mean()

    # Velocity direction encoded as winner prediction
    # Positive velocity -> UP winning, Negative -> DOWN winning
    features['velocity_predicts_up'] = (features['velocity_sign_mean'] > 0).astype(float)

    # Velocity magnitude persistence (is magnitude consistent?)
    vel_abs = velocity.abs()
    features['velocity_magnitude_mean'] = vel_abs.rolling(window, min_periods=1).mean()
    features['velocity_magnitude_std'] = vel_abs.rolling(window, min_periods=1).std().fillna(0)

    # Coefficient of variation (lower = more consistent)
    features['velocity_cv'] = (
        features['velocity_magnitude_std'] /
        (features['velocity_magnitude_mean'] + 1e-8)
    )

    return features


def compute_expensive_side_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute features based on which side is more expensive.

    The more expensive side is typically the predicted winner due to
    adverse selection - informed traders pay more for the side they think will win.
    """
    features = pd.DataFrame(index=df.index)

    # Check for ask prices
    up_ask = df.get('up_ask')
    down_ask = df.get('down_ask')

    if up_ask is not None and down_ask is not None:
        # Binary: is UP the expensive side?
        features['up_is_expensive'] = (up_ask > down_ask).astype(float)

        # Price difference (positive = UP more expensive)
        features['ask_price_diff'] = up_ask - down_ask

        # Normalized price difference
        avg_ask = (up_ask + down_ask) / 2 + 1e-8
        features['ask_price_diff_pct'] = (up_ask - down_ask) / avg_ask

        # Distance from fair (0.50)
        features['up_ask_from_fair'] = up_ask - 0.50
        features['down_ask_from_fair'] = down_ask - 0.50

    # Check for bid prices
    up_bid = df.get('up_bid')
    down_bid = df.get('down_bid')

    if up_bid is not None and down_bid is not None:
        features['bid_price_diff'] = up_bid - down_bid

        # Mid price features
        up_mid = (up_bid + up_ask) / 2 if up_ask is not None else up_bid
        down_mid = (down_bid + down_ask) / 2 if down_ask is not None else down_bid

        features['mid_price_diff'] = up_mid - down_mid
        features['up_mid'] = up_mid
        features['down_mid'] = down_mid

    return features


def compute_time_phase_features(
    df: pd.DataFrame,
    config: WinnerFeatureConfig
) -> pd.DataFrame:
    """
    Compute time-based phase features.

    Different phases of the market have different signal reliability.
    Near the end, prices converge to 0/1, making prediction easier.
    """
    features = pd.DataFrame(index=df.index)

    if 'time_remaining_secs' not in df.columns:
        logger.warning("time_remaining_secs not found, skipping time phase features")
        return features

    time_rem = df['time_remaining_secs']

    # Normalized time (0 at end, 1 at start)
    max_time = 900.0  # 15 minute markets
    features['time_normalized'] = np.clip(time_rem / max_time, 0, 1)

    # Urgency (inverse of normalized time)
    features['time_urgency'] = 1.0 - features['time_normalized']

    # Time urgency squared (accelerating urgency near end)
    features['time_urgency_sq'] = features['time_urgency'] ** 2

    # Phase indicators
    for phase_time in config.time_phases:
        features[f'phase_lt_{phase_time}s'] = (time_rem < phase_time).astype(float)

    # Log time (compresses early time, expands late time)
    features['log_time_remaining'] = np.log1p(time_rem)

    return features


def compute_winner_signal_interactions(
    df: pd.DataFrame,
    momentum_features: pd.DataFrame,
    velocity_features: pd.DataFrame,
    expensive_features: pd.DataFrame,
    time_features: pd.DataFrame
) -> pd.DataFrame:
    """
    Compute interaction features between winner signals.

    Key interactions:
    - Momentum × Expensive side agreement
    - Velocity × Time urgency (velocity matters more near end)
    - Signal consensus (do all signals agree?)
    """
    features = pd.DataFrame(index=df.index)

    # Momentum × Expensive side agreement
    if 'btc_momentum_sign_25' in momentum_features.columns and 'up_is_expensive' in expensive_features.columns:
        # Positive momentum + UP expensive = strong UP signal
        momentum_predicts_up = (momentum_features['btc_momentum_sign_25'] > 0).astype(float)
        expensive_up = expensive_features['up_is_expensive']
        features['momentum_expensive_agreement'] = (
            momentum_predicts_up * expensive_up +
            (1 - momentum_predicts_up) * (1 - expensive_up)
        )

    # Velocity × Time urgency
    if 'velocity_magnitude_mean' in velocity_features.columns and 'time_urgency' in time_features.columns:
        features['velocity_x_urgency'] = (
            velocity_features['velocity_magnitude_mean'] *
            time_features['time_urgency']
        )

    # Signal consensus
    signals = []

    if 'btc_momentum_sign_25' in momentum_features.columns:
        signals.append((momentum_features['btc_momentum_sign_25'] > 0).astype(float))

    if 'velocity_predicts_up' in velocity_features.columns:
        signals.append(velocity_features['velocity_predicts_up'])

    if 'up_is_expensive' in expensive_features.columns:
        signals.append(expensive_features['up_is_expensive'])

    if len(signals) >= 2:
        signal_matrix = pd.concat(signals, axis=1)
        features['signal_consensus'] = signal_matrix.mean(axis=1)
        features['signal_agreement'] = (features['signal_consensus'] - 0.5).abs() * 2

    return features


def compute_winner_features(
    df: pd.DataFrame,
    config: Optional[WinnerFeatureConfig] = None
) -> pd.DataFrame:
    """
    Main function to compute all winner-prediction features.

    Args:
        df: Raw observer DataFrame with orderbook, price, velocity data
        config: Feature configuration

    Returns:
        DataFrame with winner-specific features
    """
    if config is None:
        config = WinnerFeatureConfig()

    logger.info(f"Computing winner features for {len(df):,} rows")

    # Compute each feature category
    obi_features = compute_orderbook_imbalance_ratio(df)
    momentum_features = compute_btc_momentum_features(df, config)
    velocity_features = compute_velocity_persistence(df, config)
    expensive_features = compute_expensive_side_features(df)
    time_features = compute_time_phase_features(df, config)

    # Interaction features
    interaction_features = compute_winner_signal_interactions(
        df, momentum_features, velocity_features, expensive_features, time_features
    )

    # Combine all features
    all_features = pd.concat([
        obi_features,
        momentum_features,
        velocity_features,
        expensive_features,
        time_features,
        interaction_features,
    ], axis=1)

    # Fill NaN with 0
    all_features = all_features.fillna(0)

    # Replace inf with large values
    all_features = all_features.replace([np.inf, -np.inf], 0)

    logger.info(f"Generated {len(all_features.columns)} winner features")

    return all_features


def get_winner_feature_names() -> List[str]:
    """Get list of all winner feature names."""
    # Create dummy data to get feature names
    dummy_df = pd.DataFrame({
        'binance_price': [100000] * 10,
        'velocity_bps': [0.0] * 10,
        'up_bid': [0.5] * 10,
        'up_ask': [0.52] * 10,
        'down_bid': [0.5] * 10,
        'down_ask': [0.52] * 10,
        'time_remaining_secs': [500] * 10,
        'up_bid_size_1': [100] * 10,
        'up_ask_size_1': [100] * 10,
        'down_bid_size_1': [100] * 10,
        'down_ask_size_1': [100] * 10,
    })

    features = compute_winner_features(dummy_df)
    return features.columns.tolist()


if __name__ == "__main__":
    print("=" * 60)
    print("Testing Winner Feature Engineering")
    print("=" * 60)

    # Create test data
    np.random.seed(42)
    n_rows = 1000

    test_df = pd.DataFrame({
        'binance_price': 100000 + np.cumsum(np.random.randn(n_rows) * 10),
        'velocity_bps': np.random.randn(n_rows) * 5,
        'up_bid': 0.50 + np.random.randn(n_rows) * 0.05,
        'up_ask': 0.52 + np.random.randn(n_rows) * 0.05,
        'down_bid': 0.50 + np.random.randn(n_rows) * 0.05,
        'down_ask': 0.52 + np.random.randn(n_rows) * 0.05,
        'time_remaining_secs': np.linspace(900, 0, n_rows),
        'up_bid_size_1': np.random.randint(50, 200, n_rows),
        'up_ask_size_1': np.random.randint(50, 200, n_rows),
        'down_bid_size_1': np.random.randint(50, 200, n_rows),
        'down_ask_size_1': np.random.randint(50, 200, n_rows),
    })

    print(f"\nTest data: {len(test_df)} rows")

    # Compute features
    features = compute_winner_features(test_df)

    print(f"\nGenerated {len(features.columns)} features:")
    for i, col in enumerate(features.columns):
        sample_val = features[col].iloc[500]
        print(f"  {i+1:2}. {col}: {sample_val:.4f}")

    # Feature statistics
    print("\nFeature statistics:")
    print(features.describe().T[['mean', 'std', 'min', 'max']])
