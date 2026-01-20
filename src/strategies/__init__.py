"""
Trading Strategies Module

Contains modular trading strategies for BTC 15-minute markets.

Strategies:
- calculus_maker: Dynamic mispricing detection with exponential decay pricing
- simple_hedger: Minimal hedging strategy with flip logic
- enhanced_spike: Raw Binance spike detection with velocity confirmation ($7.54/hr)
- enhanced_momentum: Velocity strategy with partial hedging (T1/T2 tranches)
- latency_arb: Exploits Binance->Polymarket lag (~800ms window)
- opportunistic_mm: Two-sided quoting with inventory balance
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

from .enhanced_spike import (
    EnhancedSpikeStrategy,
    EnhancedSpikeState,
    EnhancedSpikePhase,
    # Backward compatibility aliases
    SpreadCaptureStrategy,
    SpreadCaptureState,
    SpreadCapturePhase,
    # Helper functions
    detect_binance_spike,
    calculate_magnitude_loser_bid,
    compute_enhanced_score,
    should_take_enhanced_signal,
)

# Backward compatibility: old names still work
SpikeCaptureStrategy = EnhancedSpikeStrategy
SpikeCaptureState = EnhancedSpikeState
SpikeCapturePhase = EnhancedSpikePhase

from .enhanced_momentum import (
    EnhancedMomentumStrategy,
    EnhancedMomentumState,
    EnhancedMomentumPhase,
)

from .latency_arb import (
    LatencyArbStrategy,
    LatencyArbState,
    LatencyArbPhase,
)

from .opportunistic_mm import (
    OpportunisticMMStrategy,
    OpportunisticMMState,
    MMPhase,
)

from .volatility_regime import (
    VolatilityRegimeDetector,
    VolatilityRegime,
    RegimeState,
    RegimeAwareEnhancedSpike,
    SimpleMACrossoverDetector,
    # Standalone functions
    calculate_rolling_atr,
    classify_regime_simple,
    get_regime_adjusted_threshold,
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
    # Enhanced Spike (primary)
    "EnhancedSpikeStrategy",
    "EnhancedSpikeState",
    "EnhancedSpikePhase",
    # Backward compatibility aliases (old spike_capture names)
    "SpikeCaptureStrategy",
    "SpikeCaptureState",
    "SpikeCapturePhase",
    # Backward compatibility aliases (old spread_capture names)
    "SpreadCaptureStrategy",
    "SpreadCaptureState",
    "SpreadCapturePhase",
    # Helper functions
    "detect_binance_spike",
    "calculate_magnitude_loser_bid",
    "compute_enhanced_score",
    "should_take_enhanced_signal",
    # Enhanced Momentum
    "EnhancedMomentumStrategy",
    "EnhancedMomentumState",
    "EnhancedMomentumPhase",
    # Latency Arbitrage
    "LatencyArbStrategy",
    "LatencyArbState",
    "LatencyArbPhase",
    # Opportunistic MM
    "OpportunisticMMStrategy",
    "OpportunisticMMState",
    "MMPhase",
    # Volatility Regime Detection
    "VolatilityRegimeDetector",
    "VolatilityRegime",
    "RegimeState",
    "RegimeAwareEnhancedSpike",
    "SimpleMACrossoverDetector",
    "calculate_rolling_atr",
    "classify_regime_simple",
    "get_regime_adjusted_threshold",
]
