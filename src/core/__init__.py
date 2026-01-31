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

    # Data classes
    'TradeResult',
    'BacktestCycle',

    # Constants
    'VELOCITY_CONFIRM_THRESHOLD',
    'ENHANCED_SCORE_THRESHOLD',
]
