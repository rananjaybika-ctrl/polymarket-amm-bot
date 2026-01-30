"""
Feature Engineering for Gabagool NN

Transforms raw observer data into model-ready features.

Feature Categories:
1. Raw Features (~42 columns): prices, velocity, orderbook depth
2. Engineered Features (~38 columns): rolling stats, interactions, gabagool-specific
3. Total: ~80 features after encoding
"""

import pandas as pd
import numpy as np
from typing import List, Tuple, Optional
from dataclasses import dataclass
import warnings

warnings.filterwarnings('ignore', category=RuntimeWarning)


@dataclass
class FeatureConfig:
    """Configuration for feature engineering."""
    rolling_windows: List[int] = None  # In samples (at 5Hz)
    use_orderbook_depth: bool = True
    use_velocity_derivatives: bool = True
    use_interactions: bool = True

    def __post_init__(self):
        if self.rolling_windows is None:
            # Default: 1s, 5s, 30s at 5Hz = 5, 25, 150 samples
            self.rolling_windows = [5, 25, 150]


# Features to extract from raw data
RAW_PRICE_FEATURES = [
    'binance_price', 'up_bid', 'up_ask', 'down_bid', 'down_ask', 'pair_cost',
]

RAW_VELOCITY_FEATURES = [
    'velocity_bps', 'acceleration_bps2', 'jerk_bps3', 'momentum_5s',
]

RAW_SPIKE_FEATURES = [
    'spike_magnitude',
]

RAW_IMBALANCE_FEATURES = [
    'up_imbalance', 'down_imbalance',
]

RAW_TEMPORAL_FEATURES = [
    'time_remaining_secs',
]


def compute_orderbook_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute orderbook-derived features from 5-level depth data.

    Features:
    - Total bid/ask depth (UP and DOWN)
    - Bid-ask spread (UP and DOWN)
    - Depth imbalance at each level
    - Weighted mid-price
    """
    features = pd.DataFrame(index=df.index)

    for side in ['up', 'down']:
        # Total depth
        bid_sizes = [f'{side}_bid_size_{i}' for i in range(1, 6)]
        ask_sizes = [f'{side}_ask_size_{i}' for i in range(1, 6)]

        bid_cols = [c for c in bid_sizes if c in df.columns]
        ask_cols = [c for c in ask_sizes if c in df.columns]

        if bid_cols:
            features[f'{side}_total_bid_depth'] = df[bid_cols].sum(axis=1)
        else:
            features[f'{side}_total_bid_depth'] = 0

        if ask_cols:
            features[f'{side}_total_ask_depth'] = df[ask_cols].sum(axis=1)
        else:
            features[f'{side}_total_ask_depth'] = 0

        # Spread
        bid_col = f'{side}_bid'
        ask_col = f'{side}_ask'
        if bid_col in df.columns and ask_col in df.columns:
            features[f'{side}_spread'] = df[ask_col] - df[bid_col]
            features[f'{side}_spread_pct'] = features[f'{side}_spread'] / (df[bid_col] + 0.001)
            features[f'{side}_mid'] = (df[bid_col] + df[ask_col]) / 2

        # Depth ratio per level
        for level in range(1, 4):  # Top 3 levels
            bid_size = f'{side}_bid_size_{level}'
            ask_size = f'{side}_ask_size_{level}'
            if bid_size in df.columns and ask_size in df.columns:
                total = df[bid_size] + df[ask_size] + 0.001
                features[f'{side}_depth_imbalance_L{level}'] = (df[bid_size] - df[ask_size]) / total

    # Cross-side features
    features['total_bid_depth'] = features.get('up_total_bid_depth', 0) + features.get('down_total_bid_depth', 0)
    features['total_ask_depth'] = features.get('up_total_ask_depth', 0) + features.get('down_total_ask_depth', 0)
    features['global_depth_imbalance'] = (features['total_bid_depth'] - features['total_ask_depth']) / \
                                          (features['total_bid_depth'] + features['total_ask_depth'] + 0.001)

    return features


def compute_rolling_features(df: pd.DataFrame, windows: List[int]) -> pd.DataFrame:
    """
    Compute rolling window statistics.

    Windows are in samples (5Hz = 200ms per sample).
    Example: window=5 = 1 second, window=25 = 5 seconds
    """
    features = pd.DataFrame(index=df.index)

    # Columns to compute rolling stats for
    roll_cols = [
        ('velocity_bps', ['mean', 'std', 'min', 'max']),
        ('binance_price', ['mean', 'std']),
        ('pair_cost', ['mean', 'min']),
        ('up_imbalance', ['mean', 'std']),
        ('down_imbalance', ['mean', 'std']),
        ('spike_magnitude', ['max', 'sum']),
        ('acceleration_bps2', ['mean', 'std']),
    ]

    for col, stats in roll_cols:
        if col not in df.columns:
            continue

        for window in windows:
            window_name = f'{window}s' if window == 5 else f'{window//5}s' if window >= 5 else f'{window}t'
            roll = df[col].rolling(window=window, min_periods=1)

            for stat in stats:
                feat_name = f'{col}_{stat}_{window_name}'
                if stat == 'mean':
                    features[feat_name] = roll.mean()
                elif stat == 'std':
                    features[feat_name] = roll.std().fillna(0)
                elif stat == 'min':
                    features[feat_name] = roll.min()
                elif stat == 'max':
                    features[feat_name] = roll.max()
                elif stat == 'sum':
                    features[feat_name] = roll.sum()

    # Price change features
    if 'binance_price' in df.columns:
        for window in windows:
            window_name = f'{window}s' if window == 5 else f'{window//5}s' if window >= 5 else f'{window}t'
            price_change = df['binance_price'].diff(window)
            features[f'price_change_{window_name}'] = price_change.fillna(0)
            pct_change = price_change / (df['binance_price'].shift(window) + 0.001)
            features[f'price_change_pct_{window_name}'] = pct_change.fillna(0)

    # Fill any remaining NaN values with 0
    for col in features.columns:
        if features[col].isna().any():
            features[col] = features[col].fillna(0)

    return features


def compute_interaction_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute interaction features between variables.

    Key interactions:
    - velocity × orderbook imbalance
    - spike × velocity alignment
    - time × velocity (urgency)
    """
    features = pd.DataFrame(index=df.index)

    # Velocity × Imbalance
    if 'velocity_bps' in df.columns:
        if 'up_imbalance' in df.columns:
            features['velocity_x_up_imbalance'] = df['velocity_bps'] * df['up_imbalance']
        if 'down_imbalance' in df.columns:
            features['velocity_x_down_imbalance'] = df['velocity_bps'] * df['down_imbalance']

    # Spike × Velocity alignment (1 if same direction, -1 if opposite, 0 if no spike)
    if 'spike_magnitude' in df.columns and 'velocity_bps' in df.columns:
        spike_dir_sign = np.sign(df.get('spike_direction_encoded', 0))  # Need to encode first
        vel_sign = np.sign(df['velocity_bps'])
        features['spike_velocity_alignment'] = spike_dir_sign * vel_sign * df['spike_magnitude']

    # Time-based urgency features
    if 'time_remaining_secs' in df.columns:
        time_rem = df['time_remaining_secs']
        features['time_urgency'] = 1.0 - np.clip(time_rem / 900.0, 0, 1)  # Max at t=0
        features['time_phase'] = np.clip(time_rem / 900.0, 0, 1)  # 1 at start, 0 at end

        # Time × velocity (urgency-weighted velocity)
        if 'velocity_bps' in df.columns:
            features['velocity_urgency'] = df['velocity_bps'] * features['time_urgency']

    # Pair cost opportunity
    if 'pair_cost' in df.columns:
        features['pair_cost_opportunity'] = 1.0 - df['pair_cost']  # Positive when pair_cost < 1

    return features


def compute_gabagool_specific_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute features specific to gabagool's grid market making.

    Features:
    - Distance from grid center (0.50)
    - Side imbalance direction
    - Optimal offset estimation
    """
    features = pd.DataFrame(index=df.index)

    # Price distance from fair value (0.50)
    if 'up_bid' in df.columns and 'down_bid' in df.columns:
        # Implied fair UP price from pair cost
        if 'pair_cost' in df.columns:
            fair_up = (1.0 - df['pair_cost']) / 2 + 0.5  # Symmetric around 0.50
            features['up_distance_from_fair'] = df['up_bid'] - fair_up
            features['down_distance_from_fair'] = df['down_bid'] - (1.0 - fair_up)

        # Mid-price deviation from 0.50
        up_mid = (df['up_bid'] + df.get('up_ask', df['up_bid'] + 0.02)) / 2
        features['up_mid_deviation'] = up_mid - 0.50
        features['down_mid_deviation'] = 0.50 - (df['down_bid'] + df.get('down_ask', df['down_bid'] + 0.02)) / 2

    # Which side is "expensive" (winner candidate)
    if 'up_ask' in df.columns and 'down_ask' in df.columns:
        features['expensive_side'] = (df['up_ask'] > df['down_ask']).astype(float)  # 1 if UP is expensive

    # Grid level estimation (based on best bid)
    if 'up_bid' in df.columns:
        # Common grid offsets: 0.01, 0.02, 0.03, 0.04, 0.05
        # Estimate grid level from best bid
        features['estimated_up_grid_level'] = np.round((df['up_bid'] - 0.50) * 100) / 100
        features['estimated_down_grid_level'] = np.round((df['down_bid'] - 0.50) * 100) / 100

    # Fill probability proxies
    # Higher imbalance on one side suggests fills are more likely there
    if 'up_imbalance' in df.columns and 'down_imbalance' in df.columns:
        # Positive imbalance = more bids than asks = harder to get filled as maker
        features['up_fill_difficulty'] = df['up_imbalance']
        features['down_fill_difficulty'] = df['down_imbalance']
        features['imbalance_direction'] = df['up_imbalance'] - df['down_imbalance']

    return features


def encode_categorical_features(df: pd.DataFrame) -> Tuple[pd.DataFrame, dict]:
    """
    Encode categorical features.

    Returns encoded DataFrame and encoding mappings.
    """
    encodings = {}
    encoded_df = pd.DataFrame(index=df.index)

    # Velocity zone
    if 'velocity_zone' in df.columns:
        zone_map = {'bearish': -1, 'neutral': 0, 'bullish': 1}
        encoded_df['velocity_zone_encoded'] = df['velocity_zone'].map(zone_map).fillna(0).astype(float)
        encodings['velocity_zone'] = zone_map

    # Spike direction
    if 'spike_direction' in df.columns:
        dir_map = {'DOWN': -1, '': 0, 'UP': 1}
        encoded_df['spike_direction_encoded'] = df['spike_direction'].map(dir_map).fillna(0).astype(float)
        encodings['spike_direction'] = dir_map

    # Data source (one-hot or binary)
    if 'data_source' in df.columns:
        encoded_df['is_websocket'] = (df['data_source'] == 'websocket').astype(float)
        encodings['data_source'] = {'websocket': 1, 'rest': 0}

    # Boolean features - handle missing/NaN values
    bool_cols = ['spike_detected', 'accel_aligned', 'cycle_just_completed']
    for col in bool_cols:
        if col in df.columns:
            # Convert to float, filling any non-boolean values with 0
            try:
                encoded_df[f'{col}_encoded'] = df[col].fillna(False).astype(float)
            except (ValueError, TypeError):
                encoded_df[f'{col}_encoded'] = 0.0

    return encoded_df, encodings


def engineer_features(df: pd.DataFrame, config: Optional[FeatureConfig] = None) -> pd.DataFrame:
    """
    Main feature engineering pipeline.

    Transforms raw observer data into model-ready features.

    Args:
        df: Raw observer DataFrame
        config: Feature engineering configuration

    Returns:
        DataFrame with engineered features
    """
    if config is None:
        config = FeatureConfig()

    # Start with copy
    features = pd.DataFrame(index=df.index)

    # 1. Raw features (normalized)
    for col in RAW_PRICE_FEATURES:
        if col in df.columns:
            features[col] = df[col]

    for col in RAW_VELOCITY_FEATURES:
        if col in df.columns:
            features[col] = df[col]

    for col in RAW_SPIKE_FEATURES:
        if col in df.columns:
            features[col] = df[col]

    for col in RAW_IMBALANCE_FEATURES:
        if col in df.columns:
            features[col] = df[col]

    for col in RAW_TEMPORAL_FEATURES:
        if col in df.columns:
            features[col] = df[col]

    # 2. Categorical encodings
    encoded_df, _ = encode_categorical_features(df)
    for col in encoded_df.columns:
        features[col] = encoded_df[col]

    # 3. Orderbook depth features
    if config.use_orderbook_depth:
        ob_features = compute_orderbook_features(df)
        for col in ob_features.columns:
            features[col] = ob_features[col]

    # 4. Rolling window features
    rolling_features = compute_rolling_features(df, config.rolling_windows)
    for col in rolling_features.columns:
        features[col] = rolling_features[col]

    # 5. Interaction features
    if config.use_interactions:
        # Need spike_direction_encoded for interactions
        if 'spike_direction_encoded' in features.columns:
            df['spike_direction_encoded'] = features['spike_direction_encoded']
        interaction_features = compute_interaction_features(df)
        for col in interaction_features.columns:
            features[col] = interaction_features[col]

    # 6. Gabagool-specific features
    gabagool_features = compute_gabagool_specific_features(df)
    for col in gabagool_features.columns:
        features[col] = gabagool_features[col]

    # Keep essential metadata
    features['timestamp_ms'] = df['timestamp_ms']
    features['market_slug'] = df['market_slug']
    if 'resolution' in df.columns:
        features['resolution'] = df['resolution']

    return features


def normalize_features(df: pd.DataFrame, fit_stats: Optional[dict] = None) -> Tuple[pd.DataFrame, dict]:
    """
    Normalize features to zero mean and unit variance.

    Args:
        df: Feature DataFrame
        fit_stats: Pre-computed mean/std (for validation data)

    Returns:
        Normalized DataFrame and normalization stats
    """
    # Columns to normalize (exclude metadata and binary)
    skip_cols = ['timestamp_ms', 'market_slug', 'resolution',
                 'is_websocket', 'spike_detected_encoded', 'accel_aligned_encoded',
                 'cycle_just_completed_encoded', 'expensive_side']

    numeric_cols = [c for c in df.columns if c not in skip_cols and df[c].dtype in ['float64', 'float32', 'int64']]

    if fit_stats is None:
        fit_stats = {}
        for col in numeric_cols:
            col_data = df[col].dropna()
            if len(col_data) > 0:
                fit_stats[col] = {
                    'mean': float(col_data.mean()),
                    'std': float(col_data.std()) + 1e-8,
                }
            else:
                fit_stats[col] = {'mean': 0.0, 'std': 1.0}

    normalized = df.copy()

    # Fill NaN with 0 BEFORE normalization
    for col in numeric_cols:
        normalized[col] = normalized[col].fillna(0)

    # Now normalize
    for col in numeric_cols:
        if col in fit_stats:
            normalized[col] = (normalized[col] - fit_stats[col]['mean']) / fit_stats[col]['std']
        else:
            # New column not in fit_stats - normalize with own stats
            mean = normalized[col].mean()
            std = normalized[col].std() + 1e-8
            normalized[col] = (normalized[col] - mean) / std
            fit_stats[col] = {'mean': float(mean), 'std': float(std)}

    # Final NaN check - replace any remaining NaN with 0
    for col in normalized.columns:
        if normalized[col].dtype in ['float64', 'float32', 'int64']:
            normalized[col] = normalized[col].fillna(0)

    return normalized, fit_stats


def get_feature_names(exclude_metadata: bool = True) -> List[str]:
    """Get list of feature names (without metadata columns)."""
    # This is a placeholder - actual names depend on data
    # Will be populated after feature engineering
    pass


if __name__ == "__main__":
    # Test feature engineering
    from data_loader import load_training_data, get_market_data

    print("Loading data...")
    data = load_training_data()

    # Test on one market
    if data.train_markets:
        slug = data.train_markets[0]
        print(f"\nTesting feature engineering on {slug}")

        mdf = get_market_data(data.train_df, slug)
        print(f"Raw data: {len(mdf)} rows, {len(mdf.columns)} columns")

        features = engineer_features(mdf)
        print(f"Engineered features: {len(features)} rows, {len(features.columns)} columns")

        print("\nFeature columns:")
        for i, col in enumerate(features.columns):
            print(f"  {i+1:2}. {col}")

        # Normalize
        normalized, stats = normalize_features(features)
        print(f"\nNormalized: {len(normalized)} rows")
        print(f"Normalization stats for {len(stats)} features")
