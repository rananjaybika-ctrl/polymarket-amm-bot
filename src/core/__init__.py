"""
Core Trading Utilities - Single Source of Truth

=============================================================================
BOTH live strategy (src/strategies/) AND backtests (research/) import from here.
This ensures identical logic across live trading and backtesting.
=============================================================================

Usage:
    from src.core import (
        # Fee model
        polymarket_taker_fee,
        calculate_pnl_with_fees,

        # Signal filters
        velocity_confirms_spike,
        obi_confirms_spike,
        should_take_spike_enhanced,
        compute_enhanced_score,

        # Calculations
        calculate_loser_bid,

        # Data classes
        TradeResult,
        BacktestCycle,

        # Constants
        VELOCITY_CONFIRM_THRESHOLD,
        ENHANCED_SCORE_THRESHOLD,
    )

For parameters (lookback, time_stop, z_scores, etc.), use:
    from research.reference.TRADING_CONFIGS import AGGRESSIVE
"""

from src.core.trading_utils import (
    # Fee model
    polymarket_taker_fee,
    calculate_pnl_with_fees,

    # Signal filters
    velocity_confirms_spike,
    obi_confirms_spike,
    should_take_spike_enhanced,
    compute_enhanced_score,

    # Calculations
    calculate_loser_bid,

    # Multi-cycle direction modes - DEPRECATED Jan 31, 2026
    # Only DIRECTION_MODE_SINGLE is live-ready. Multi-cycle destroyed profitability.
    DIRECTION_MODE_SINGLE,
    DIRECTION_MODE_BUILD,  # DEPRECATED
    DIRECTION_MODE_CLEAR,  # DEPRECATED
    can_enter_direction,   # DEPRECATED (only for backwards compatibility)

    # Data classes
    TradeResult,
    BacktestCycle,

    # Constants
    VELOCITY_CONFIRM_THRESHOLD,
    ENHANCED_SCORE_THRESHOLD,
)

__all__ = [
    # Fee model
    'polymarket_taker_fee',
    'calculate_pnl_with_fees',

    # Signal filters
    'velocity_confirms_spike',
    'obi_confirms_spike',
    'should_take_spike_enhanced',
    'compute_enhanced_score',

    # Calculations
    'calculate_loser_bid',

    # Multi-cycle direction modes - DEPRECATED Jan 31, 2026
    # Only DIRECTION_MODE_SINGLE is live-ready
    'DIRECTION_MODE_SINGLE',
    'DIRECTION_MODE_BUILD',  # DEPRECATED
    'DIRECTION_MODE_CLEAR',  # DEPRECATED
    'can_enter_direction',   # DEPRECATED

    # Data classes
    'TradeResult',
    'BacktestCycle',

    # Constants
    'VELOCITY_CONFIRM_THRESHOLD',
    'ENHANCED_SCORE_THRESHOLD',
]
