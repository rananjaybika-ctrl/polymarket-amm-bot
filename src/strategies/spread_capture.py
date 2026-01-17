"""
Spread Capture Strategy - DEPRECATED, use enhanced_spike.py instead

This file maintains backward compatibility by re-exporting from enhanced_spike.py.
All new code should import from enhanced_spike directly or use the strategy
module's __init__.py exports.

Migration:
    # OLD (deprecated):
    from src.strategies.spread_capture import SpreadCaptureStrategy

    # NEW (preferred):
    from src.strategies.enhanced_spike import EnhancedSpikeStrategy
    # or
    from src.strategies import EnhancedSpikeStrategy

The SpreadCaptureStrategy class is now an alias for EnhancedSpikeStrategy.
"""

# Re-export everything from enhanced_spike for backward compatibility
from src.strategies.enhanced_spike import (
    # Main classes (new names)
    EnhancedSpikeStrategy,
    EnhancedSpikeState,
    EnhancedSpikePhase,

    # Backward compatibility aliases
    SpreadCaptureStrategy,
    SpreadCaptureState,
    SpreadCapturePhase,

    # Velocity zones (legacy)
    VelocityZone,
    VELOCITY_ZONES,

    # Constants
    VELOCITY_THRESHOLD,
    VELOCITY_STRONG,
    VELOCITY_PULL_THRESHOLD,
    BASE_OFFSET,
    TIGHT_OFFSET,
    WIDE_OFFSET,
    VERY_WIDE_OFFSET,
    DEFAULT_ENTRY_OFFSET,
    DEFAULT_HEDGE_OFFSET,
    DEFAULT_ENTRY_WAIT,
    DEFAULT_HEDGE_WAIT,
    MAX_WAIT_TIME,
    DEFAULT_GRID_LEVELS,
    GRID_SPACING,
    DEFAULT_MAX_IMBALANCE_PCT,
    DEFAULT_MAX_IMBALANCE_SHARES,
    FORCE_REBALANCE_OFFSET,
    MIN_SHARES,
    DEFAULT_BASE_SIZE,
    DEFAULT_TARGET_SHARES,
    DEFAULT_MIN_PROFIT,
    DEFAULT_MAX_SHARE_PRICE,
    DEFAULT_ENABLE_CYCLING,
    DEFAULT_MIN_VELOCITY_BPS,
    DEFAULT_STOP_LOSS_PCT,
    MIN_TIME_REMAINING,
    QUOTE_REFRESH_INTERVAL,

    # Spike detection constants
    DEFAULT_SPIKE_LOOKBACK,
    DEFAULT_SPIKE_THRESHOLD,
    SPIKE_HISTORY_SIZE,
    DROP_MULTIPLIER,
    DROP_INTERCEPT,
    DEFAULT_TARGET_PAIR_COST,

    # Helper functions
    calculate_velocity_edge,
    detect_binance_spike,
    calculate_magnitude_loser_bid,
    compute_enhanced_score,
    should_take_enhanced_signal,
)

# Backward compatibility: old spike_capture names
SpikeCaptureStrategy = EnhancedSpikeStrategy
SpikeCaptureState = EnhancedSpikeState
SpikeCapturePhase = EnhancedSpikePhase

# Ensure all legacy exports are available
__all__ = [
    # Primary exports (use these)
    "EnhancedSpikeStrategy",
    "EnhancedSpikeState",
    "EnhancedSpikePhase",

    # Backward compatibility (spike_capture names)
    "SpikeCaptureStrategy",
    "SpikeCaptureState",
    "SpikeCapturePhase",

    # Legacy aliases (spread_capture names)
    "SpreadCaptureStrategy",
    "SpreadCaptureState",
    "SpreadCapturePhase",

    # Other exports
    "VelocityZone",
    "VELOCITY_ZONES",
    "VELOCITY_THRESHOLD",
    "VELOCITY_STRONG",
    "VELOCITY_PULL_THRESHOLD",
    "calculate_velocity_edge",
    "detect_binance_spike",
    "calculate_magnitude_loser_bid",
    "compute_enhanced_score",
    "should_take_enhanced_signal",

    # Constants
    "DEFAULT_ENTRY_OFFSET",
    "DEFAULT_HEDGE_OFFSET",
    "DEFAULT_SPIKE_LOOKBACK",
    "DEFAULT_SPIKE_THRESHOLD",
    "DROP_MULTIPLIER",
    "DROP_INTERCEPT",
]
