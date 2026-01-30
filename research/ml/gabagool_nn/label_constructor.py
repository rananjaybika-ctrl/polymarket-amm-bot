"""
Label Constructor for Gabagool NN

Constructs multi-task learning labels from observer and whale trade data.

Prediction Targets:
1. Fill Prediction: Binary [p_up_fill, p_down_fill] - will UP/DOWN get filled in next N seconds?
2. Imbalance Direction: Regression [-1, 1] - which side accumulates more?
3. Profitability: Regression - expected PnL given current conditions
4. Grid Level: Ordinal [0.01-0.05] - optimal passive offset from best bid
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
import json
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

# Path to gabagool trades
GABAGOOL_TRADES_FILE = Path(__file__).parent.parent.parent / "findings" / "data" / "gabagool_trades_oos7.json"


def load_gabagool_trades() -> Dict:
    """Load gabagool's actual trades for OOS7."""
    if GABAGOOL_TRADES_FILE.exists():
        with open(GABAGOOL_TRADES_FILE) as f:
            return json.load(f)
    return {}


@dataclass
class LabelConfig:
    """Configuration for label construction."""
    fill_horizon_secs: float = 30.0  # Look-ahead for fill prediction
    fill_threshold: float = 0.01  # Minimum price movement to count as "fill"
    grid_levels: List[float] = None  # Possible grid offsets
    sample_rate_hz: float = 5.0  # Observer sample rate

    def __post_init__(self):
        if self.grid_levels is None:
            self.grid_levels = [0.01, 0.02, 0.03, 0.04, 0.05]


@dataclass
class Labels:
    """Container for all label types."""
    fill_up: np.ndarray  # Binary: UP side filled in horizon
    fill_down: np.ndarray  # Binary: DOWN side filled in horizon
    imbalance_direction: np.ndarray  # [-1, 1]: net accumulation direction
    profitability: np.ndarray  # Expected PnL
    grid_level: np.ndarray  # Ordinal: optimal grid offset index
    resolution: np.ndarray  # Market resolution (UP=1, DOWN=0)
    valid_mask: np.ndarray  # Which samples have valid labels


def construct_fill_labels(df: pd.DataFrame, config: LabelConfig) -> Tuple[np.ndarray, np.ndarray]:
    """
    Construct fill prediction labels.

    A "fill" occurs when the ask price drops below our bid (passive order executes).
    We predict if UP or DOWN side will get filled in the next N seconds.

    For passive market making:
    - UP fill: up_ask drops (someone sells INTO our bid)
    - DOWN fill: down_ask drops (someone sells INTO our bid)
    """
    n_samples = len(df)
    horizon_samples = int(config.fill_horizon_secs * config.sample_rate_hz)

    fill_up = np.zeros(n_samples)
    fill_down = np.zeros(n_samples)

    # Get price arrays
    up_ask = df['up_ask'].values if 'up_ask' in df.columns else np.zeros(n_samples)
    down_ask = df['down_ask'].values if 'down_ask' in df.columns else np.zeros(n_samples)

    for i in range(n_samples - horizon_samples):
        # Look ahead in the horizon window
        future_up_ask = up_ask[i+1:i+horizon_samples+1]
        future_down_ask = down_ask[i+1:i+horizon_samples+1]

        current_up_ask = up_ask[i]
        current_down_ask = down_ask[i]

        # Fill occurs if ask drops (price moves against the ask side)
        # For UP: if up_ask drops, UP makers get filled
        # For DOWN: if down_ask drops, DOWN makers get filled
        if len(future_up_ask) > 0:
            min_future_up = np.min(future_up_ask)
            fill_up[i] = 1.0 if (current_up_ask - min_future_up) >= config.fill_threshold else 0.0

        if len(future_down_ask) > 0:
            min_future_down = np.min(future_down_ask)
            fill_down[i] = 1.0 if (current_down_ask - min_future_down) >= config.fill_threshold else 0.0

    return fill_up, fill_down


def construct_fill_labels_from_trades(
    df: pd.DataFrame,
    market_slug: str,
    gabagool_trades: Dict,
    config: LabelConfig
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Construct fill labels from actual gabagool trades.

    This uses ground truth - actual trade timestamps from gabagool's wallet
    to determine when fills occurred, rather than inferring from price movement.

    A fill is labeled as 1 if gabagool executed a BUY trade within the horizon.
    BUY trades indicate a fill of a passive order (maker getting filled).
    """
    n_samples = len(df)
    horizon_ms = int(config.fill_horizon_secs * 1000)

    fill_up = np.zeros(n_samples)
    fill_down = np.zeros(n_samples)

    # Get trades for this market
    trades = gabagool_trades.get('trades', [])
    market_trades = [t for t in trades if t.get('market_slug') == market_slug]

    if not market_trades:
        # Fall back to price-based labels
        logger.debug(f"No trades for {market_slug}, using price-based labels")
        return construct_fill_labels(df, config)

    # Convert trades to dataframe for efficient lookup
    trade_df = pd.DataFrame(market_trades)
    trade_df['timestamp_ms'] = trade_df['timestamp'] * 1000  # Convert to ms

    # Separate UP and DOWN trades
    up_buys = trade_df[(trade_df['outcome'] == 'Up') & (trade_df['side'] == 'BUY')]
    down_buys = trade_df[(trade_df['outcome'] == 'Down') & (trade_df['side'] == 'BUY')]

    # Get observer timestamps
    if 'timestamp_ms' not in df.columns:
        logger.warning(f"No timestamp_ms in df for {market_slug}")
        return construct_fill_labels(df, config)

    obs_timestamps = df['timestamp_ms'].values

    # For each observer timestamp, check if there's a fill in the horizon
    for i in range(n_samples):
        t = obs_timestamps[i]
        t_end = t + horizon_ms

        # Check UP fills (gabagool BUY of UP = fill of passive UP bid)
        up_fills_in_horizon = up_buys[
            (up_buys['timestamp_ms'] >= t) &
            (up_buys['timestamp_ms'] < t_end)
        ]
        if len(up_fills_in_horizon) > 0:
            fill_up[i] = 1.0

        # Check DOWN fills
        down_fills_in_horizon = down_buys[
            (down_buys['timestamp_ms'] >= t) &
            (down_buys['timestamp_ms'] < t_end)
        ]
        if len(down_fills_in_horizon) > 0:
            fill_down[i] = 1.0

    # Log fill rates
    up_rate = fill_up.mean() * 100
    down_rate = fill_down.mean() * 100
    logger.debug(f"{market_slug}: UP fill rate {up_rate:.1f}%, DOWN fill rate {down_rate:.1f}%")

    return fill_up, fill_down


def construct_imbalance_labels(df: pd.DataFrame, config: LabelConfig) -> np.ndarray:
    """
    Construct imbalance direction labels.

    Gabagool's 71.2% edge comes from predicting which side will accumulate more.
    Label: [-1, 1] where:
    - -1 = DOWN side accumulates more
    - +1 = UP side accumulates more
    - 0 = balanced

    Based on future price movement and orderbook changes.
    """
    n_samples = len(df)
    horizon_samples = int(config.fill_horizon_secs * config.sample_rate_hz)

    imbalance_dir = np.zeros(n_samples)

    # Use multiple signals to determine imbalance direction
    if 'binance_price' in df.columns:
        price = df['binance_price'].values

        for i in range(n_samples - horizon_samples):
            future_price = price[i+horizon_samples]
            current_price = price[i]

            if current_price > 0:
                price_change_pct = (future_price - current_price) / current_price

                # Map to [-1, 1] with some scaling
                # Price up = UP side more valuable = +1
                # Price down = DOWN side more valuable = -1
                imbalance_dir[i] = np.clip(price_change_pct * 100, -1, 1)

    # Alternative: use orderbook imbalance changes
    if 'up_imbalance' in df.columns and 'down_imbalance' in df.columns:
        up_imb = df['up_imbalance'].values
        down_imb = df['down_imbalance'].values

        for i in range(n_samples - horizon_samples):
            # Combine price direction with imbalance
            future_up_imb = up_imb[i+horizon_samples] if i + horizon_samples < n_samples else up_imb[i]
            future_down_imb = down_imb[i+horizon_samples] if i + horizon_samples < n_samples else down_imb[i]

            imb_change = (future_up_imb - up_imb[i]) - (future_down_imb - down_imb[i])

            # Blend with price-based signal
            imbalance_dir[i] = np.clip(imbalance_dir[i] + imb_change * 0.5, -1, 1)

    return imbalance_dir


def construct_profitability_labels(df: pd.DataFrame, resolution: str) -> np.ndarray:
    """
    Construct profitability labels.

    Expected PnL = (1 - pair_cost) if hedged, or depends on resolution if unhedged.
    This is the "ground truth" profit from gabagool's perspective.
    """
    n_samples = len(df)
    profitability = np.zeros(n_samples)

    if 'pair_cost' in df.columns:
        pair_cost = df['pair_cost'].values

        for i in range(n_samples):
            # Hedged profit per share (assuming both sides fill at pair_cost)
            hedged_profit = 1.0 - pair_cost[i]

            # Adjust based on resolution risk
            # If resolution matches our "expensive" side, unhedged is better
            # This is simplified - real profit depends on actual fill prices
            profitability[i] = hedged_profit

            # Cap extreme values
            profitability[i] = np.clip(profitability[i], -0.20, 0.10)

    return profitability


def construct_grid_level_labels(df: pd.DataFrame, config: LabelConfig) -> np.ndarray:
    """
    Construct grid level labels (ordinal classification).

    Predicts optimal offset from best bid: [0.01, 0.02, 0.03, 0.04, 0.05]
    Class index: 0=0.01, 1=0.02, 2=0.03, 3=0.04, 4=0.05

    The "optimal" level is the one that would have gotten filled with best profit.
    """
    n_samples = len(df)
    n_levels = len(config.grid_levels)
    horizon_samples = int(config.fill_horizon_secs * config.sample_rate_hz)

    grid_level = np.zeros(n_samples, dtype=int)

    # Heuristic: use volatility to determine grid level
    # Higher volatility = wider grid (higher offset index)
    if 'velocity_bps' in df.columns:
        velocity = np.abs(df['velocity_bps'].values)

        # Also consider spike magnitude
        spike_mag = df['spike_magnitude'].values if 'spike_magnitude' in df.columns else np.zeros(n_samples)

        for i in range(n_samples):
            # Combined volatility measure
            vol_measure = velocity[i] + spike_mag[i] * 10

            # Map to grid level
            # Low vol (< 0.1) = tight grid (0.01)
            # High vol (> 0.5) = wide grid (0.05)
            if vol_measure < 0.1:
                grid_level[i] = 0  # 0.01
            elif vol_measure < 0.2:
                grid_level[i] = 1  # 0.02
            elif vol_measure < 0.3:
                grid_level[i] = 2  # 0.03
            elif vol_measure < 0.4:
                grid_level[i] = 3  # 0.04
            else:
                grid_level[i] = 4  # 0.05

    return grid_level


def construct_labels_from_whale_trades(df: pd.DataFrame,
                                       whale_trades: Dict,
                                       market_slug: str) -> Optional[Labels]:
    """
    Construct labels using actual gabagool trade data.

    Uses real trade timestamps and prices to construct ground truth labels.
    """
    if whale_trades is None or 'trades' not in whale_trades:
        return None

    trades = whale_trades['trades']
    market_trades = [t for t in trades if t.get('market_slug') == market_slug or
                     market_slug in t.get('asset', '')]

    if not market_trades:
        return None

    # TODO: Implement trade-based label construction
    # This would align trade timestamps with observer data
    # and construct labels based on actual fill events

    return None


def construct_labels(df: pd.DataFrame, resolution: str,
                    config: Optional[LabelConfig] = None,
                    whale_trades: Optional[Dict] = None,
                    market_slug: Optional[str] = None,
                    use_trade_labels: bool = True) -> Labels:
    """
    Main label construction pipeline.

    Args:
        df: Feature DataFrame for a single market
        resolution: Market resolution ('UP' or 'DOWN')
        config: Label configuration
        whale_trades: Optional gabagool trade data for ground truth
        market_slug: Market identifier for trade-based labels
        use_trade_labels: Whether to use actual trade data for fill labels

    Returns:
        Labels object with all prediction targets
    """
    if config is None:
        config = LabelConfig()

    n_samples = len(df)
    horizon_samples = int(config.fill_horizon_secs * config.sample_rate_hz)

    # Construct fill labels - prefer actual trades when available
    if use_trade_labels and whale_trades and market_slug:
        fill_up, fill_down = construct_fill_labels_from_trades(
            df, market_slug, whale_trades, config
        )
    else:
        fill_up, fill_down = construct_fill_labels(df, config)
    imbalance_direction = construct_imbalance_labels(df, config)
    profitability = construct_profitability_labels(df, resolution)
    grid_level = construct_grid_level_labels(df, config)

    # Resolution encoding
    resolution_encoded = np.ones(n_samples) if resolution == 'UP' else np.zeros(n_samples)

    # Valid mask: exclude samples too close to market end
    valid_mask = np.ones(n_samples, dtype=bool)
    valid_mask[-horizon_samples:] = False  # Can't compute look-ahead labels for last samples

    # Also mask samples with invalid data
    if 'time_remaining_secs' in df.columns:
        time_rem = df['time_remaining_secs'].values
        valid_mask &= (time_rem >= 60)  # Exclude last minute

    return Labels(
        fill_up=fill_up,
        fill_down=fill_down,
        imbalance_direction=imbalance_direction,
        profitability=profitability,
        grid_level=grid_level,
        resolution=resolution_encoded,
        valid_mask=valid_mask,
    )


def labels_to_tensors(labels: Labels) -> Dict[str, np.ndarray]:
    """Convert Labels object to dictionary of numpy arrays."""
    # Ensure fill labels are binary [0, 1]
    fill_up = np.clip(labels.fill_up, 0, 1).astype(np.float32)
    fill_down = np.clip(labels.fill_down, 0, 1).astype(np.float32)

    # Handle NaN values
    fill_up = np.nan_to_num(fill_up, nan=0.0)
    fill_down = np.nan_to_num(fill_down, nan=0.0)

    return {
        'fill': np.stack([fill_up, fill_down], axis=1),  # [N, 2]
        'imbalance': np.nan_to_num(labels.imbalance_direction, nan=0.0).astype(np.float32),  # [N]
        'pnl': np.nan_to_num(labels.profitability, nan=0.0).astype(np.float32),  # [N]
        'grid_level': labels.grid_level.astype(np.int64),  # [N]
        'resolution': labels.resolution.astype(np.float32),  # [N]
        'valid_mask': labels.valid_mask,  # [N]
    }


def compute_label_statistics(labels: Labels) -> Dict:
    """Compute statistics on labels for analysis."""
    valid = labels.valid_mask

    stats = {
        'n_samples': len(valid),
        'n_valid': valid.sum(),
        'fill_up_rate': labels.fill_up[valid].mean() if valid.sum() > 0 else 0,
        'fill_down_rate': labels.fill_down[valid].mean() if valid.sum() > 0 else 0,
        'imbalance_mean': labels.imbalance_direction[valid].mean() if valid.sum() > 0 else 0,
        'imbalance_std': labels.imbalance_direction[valid].std() if valid.sum() > 0 else 0,
        'pnl_mean': labels.profitability[valid].mean() if valid.sum() > 0 else 0,
        'pnl_std': labels.profitability[valid].std() if valid.sum() > 0 else 0,
        'grid_level_dist': np.bincount(labels.grid_level[valid], minlength=5).tolist() if valid.sum() > 0 else [0]*5,
        'resolution_up_rate': labels.resolution[valid].mean() if valid.sum() > 0 else 0,
    }

    return stats


if __name__ == "__main__":
    # Test label construction
    from data_loader import load_training_data, get_market_data
    from feature_engineer import engineer_features

    print("Loading data...")
    data = load_training_data()

    if data.train_markets:
        slug = data.train_markets[0]
        print(f"\nTesting label construction on {slug}")

        mdf = get_market_data(data.train_df, slug)
        resolution = data.resolutions.get(slug, 'UP')
        print(f"Raw data: {len(mdf)} rows, resolution: {resolution}")

        # Engineer features first
        features = engineer_features(mdf)

        # Construct labels
        config = LabelConfig()
        labels = construct_labels(features, resolution, config)

        print(f"\nLabels constructed:")
        print(f"  Fill UP shape: {labels.fill_up.shape}")
        print(f"  Fill DOWN shape: {labels.fill_down.shape}")
        print(f"  Imbalance shape: {labels.imbalance_direction.shape}")
        print(f"  Profitability shape: {labels.profitability.shape}")
        print(f"  Grid level shape: {labels.grid_level.shape}")
        print(f"  Valid samples: {labels.valid_mask.sum()} / {len(labels.valid_mask)}")

        # Statistics
        stats = compute_label_statistics(labels)
        print("\nLabel statistics:")
        for key, value in stats.items():
            print(f"  {key}: {value}")
