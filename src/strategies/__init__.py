"""
Trading Strategies Module

Contains modular trading strategies for BTC 15-minute markets.

Strategies:
- calculus_maker: Dynamic mispricing detection with exponential decay pricing
- simple_hedger: Minimal hedging strategy with flip logic
- spread_capture: Z-score based spread capture with tiered entry/hedge offsets
"""

from .calculus_maker import (
    CalculusMakerStrategy,
    get_mispricing_threshold,
    get_dynamic_size,
    get_calculus_price,
    check_prospective_pair_cost,
)

from .simple_hedger import (
    SimpleHedgerStrategy,
    SimpleHedgerState,
    Phase,
)

from .spread_capture import (
    SpreadCaptureStrategy,
    SpreadCaptureState,
    SpreadCapturePhase,
)

__all__ = [
    "CalculusMakerStrategy",
    "get_mispricing_threshold",
    "get_dynamic_size",
    "get_calculus_price",
    "check_prospective_pair_cost",
    "SimpleHedgerStrategy",
    "SimpleHedgerState",
    "Phase",
    "SpreadCaptureStrategy",
    "SpreadCaptureState",
    "SpreadCapturePhase",
]
