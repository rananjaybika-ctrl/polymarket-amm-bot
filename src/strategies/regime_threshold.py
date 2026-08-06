"""
Regime-based adaptive spike threshold for PHOENIX strategy.

Self-calibrating threshold using fast/slow volatility crossover.
No fitted parameters — adapts in real-time to current market conditions.

Interface matches OUAdaptiveThreshold (duck-typed):
    threshold = adaptive.update(price)  -> float
    adaptive.get_threshold()            -> float
    adaptive.get_state()                -> dict-like
    adaptive.get_debug_info()           -> dict
    adaptive.reset()                    -> None

Source: research/strategies/adaptive_threshold.py RegimeThreshold (line 542)
Backtest validation: research/backtests/phoenix_threshold_comparison.py
Winner config: REGIME_x1.0 — $3.72/hr, 95.2% WR, 552 trades across 6 datasets
"""

import logging
import math
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# REGIME DEFINITIONS
# =============================================================================

class VolatilityRegime(Enum):
    """Discrete volatility regimes based on fast/slow vol ratio."""
    CALM = "CALM"       # ratio < 0.5  — low vol, tight threshold
    NORMAL = "NORMAL"   # ratio 0.5-1.5 — baseline
    ACTIVE = "ACTIVE"   # ratio 1.5-3.0 — elevated vol
    SPIKE = "SPIKE"     # ratio > 3.0  — extreme vol, wide threshold


# Regime thresholds (percentage moves) — from backtest grid search
# These are the spike magnitude thresholds for each regime
REGIME_THRESHOLDS = {
    VolatilityRegime.CALM: 0.008,     # 0.008% — tight, catches small moves
    VolatilityRegime.NORMAL: 0.015,   # 0.015% — baseline
    VolatilityRegime.ACTIVE: 0.025,   # 0.025% — wider for noisy markets
    VolatilityRegime.SPIKE: 0.050,    # 0.050% — only big spikes in extreme vol
}


@dataclass
class RegimeState:
    """Current state for monitoring (compatible with OUState pattern)."""
    threshold: float
    volatility: float  # fast_vol
    samples: int
    regime: str
    fast_vol: float = 0.0
    slow_vol: float = 0.0
    ratio: float = 1.0


# =============================================================================
# REGIME ADAPTIVE THRESHOLD
# =============================================================================

class RegimeAdaptiveThreshold:
    """
    Adaptive spike threshold using fast/slow volatility regime detection.

    Self-calibrating: no fitted parameters, no external calibration files.
    Uses the ratio of fast (recent) to slow (historical) volatility to
    classify the current market regime, then returns a fixed threshold
    for each regime.

    Interface is duck-type compatible with OUAdaptiveThreshold:
        threshold = adaptive.update(price)   # Returns float threshold
        adaptive.get_threshold()             # Current threshold without update
        adaptive.get_state()                 # Current state for monitoring
        adaptive.get_debug_info()            # Detailed debug dict
        adaptive.reset()                     # Reset for new session

    Parameters:
        fast_window: Ticks for fast volatility (default 300 = ~5s at 60Hz)
        slow_window: Ticks for slow volatility (default 3600 = ~60s at 60Hz)
        regime_bounds: (calm_ratio, active_ratio) for regime classification
        min_threshold: Absolute minimum threshold (safety floor)
        max_threshold: Absolute maximum threshold (safety cap)
        scale_factor: Multiplier applied to all regime thresholds (1.0 = no scaling)
    """

    def __init__(
        self,
        fast_window: int = 300,
        slow_window: int = 3600,
        regime_bounds: Tuple[float, float] = (0.5, 1.5),
        min_threshold: float = 0.005,
        max_threshold: float = 0.200,
        scale_factor: float = 1.0,
    ):
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.low_ratio, self.high_ratio = regime_bounds
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold
        self.scale_factor = scale_factor

        # State: deque of percentage returns (maxlen = slow_window)
        self.returns: deque = deque(maxlen=slow_window)
        self.prev_price: Optional[float] = None

        # Current outputs
        self.current_regime: VolatilityRegime = VolatilityRegime.NORMAL
        self.current_threshold: float = REGIME_THRESHOLDS[VolatilityRegime.NORMAL] * scale_factor
        self._fast_vol: float = 0.0
        self._slow_vol: float = 0.0
        self._ratio: float = 1.0
        self._tick_count: int = 0

        logger.info(
            f"[REGIME] Initialized: fast={fast_window}, slow={slow_window}, "
            f"bounds=({self.low_ratio}, {self.high_ratio}), scale={scale_factor:.2f}"
        )

    def _calculate_vol(self, window: int) -> float:
        """Calculate rolling std dev of returns for a given window."""
        if len(self.returns) < window:
            return 0.0
        recent = list(self.returns)[-window:]
        if len(recent) < 2:
            return 0.0
        mean = sum(recent) / len(recent)
        variance = sum((r - mean) ** 2 for r in recent) / (len(recent) - 1)
        return math.sqrt(variance)

    def update(self, price: float) -> float:
        """
        Update with new price and return current adaptive threshold.

        Args:
            price: Current BTC price from Binance

        Returns:
            Adaptive spike threshold (%) — float, compatible with OUAdaptiveThreshold
        """
        self._tick_count += 1

        # First tick: store price, return default
        if self.prev_price is None or self.prev_price <= 0:
            self.prev_price = price
            return self.current_threshold

        # Compute percentage return
        ret = (price - self.prev_price) / self.prev_price * 100
        self.prev_price = price
        self.returns.append(ret)

        # Need enough data for fast window
        if len(self.returns) < self.fast_window:
            return self.current_threshold

        # Calculate fast and slow volatility
        self._fast_vol = self._calculate_vol(self.fast_window)
        self._slow_vol = self._calculate_vol(self.slow_window)

        # Compute ratio (fast / slow)
        if self._slow_vol <= 0:
            self._ratio = 1.0
        else:
            self._ratio = self._fast_vol / self._slow_vol

        # Classify regime from ratio
        if self._ratio < self.low_ratio:
            self.current_regime = VolatilityRegime.CALM
        elif self._ratio > self.high_ratio * 2:  # >3.0 with default bounds
            self.current_regime = VolatilityRegime.SPIKE
        elif self._ratio > self.high_ratio:  # >1.5
            self.current_regime = VolatilityRegime.ACTIVE
        else:
            self.current_regime = VolatilityRegime.NORMAL

        # Get threshold for current regime, apply scale factor
        raw_threshold = REGIME_THRESHOLDS[self.current_regime] * self.scale_factor

        # Apply bounds
        self.current_threshold = max(self.min_threshold, min(raw_threshold, self.max_threshold))

        return self.current_threshold

    def get_threshold(self) -> float:
        """Get current threshold without updating state."""
        return self.current_threshold

    def get_state(self) -> RegimeState:
        """Get current state for monitoring."""
        return RegimeState(
            threshold=self.current_threshold,
            volatility=self._fast_vol,
            samples=len(self.returns),
            regime=self.current_regime.value,
            fast_vol=self._fast_vol,
            slow_vol=self._slow_vol,
            ratio=self._ratio,
        )

    def get_debug_info(self) -> Dict:
        """Get detailed debug information (compatible with OUAdaptiveThreshold)."""
        return {
            "current_vol": self._fast_vol,
            "log_vol": 0.0,  # Not applicable for regime (OU compat field)
            "z_score": 0.0,  # Not applicable for regime (OU compat field)
            "regime": self.current_regime.value,
            "multiplier": self.scale_factor,
            "threshold": self.current_threshold,
            "base_threshold": REGIME_THRESHOLDS[VolatilityRegime.NORMAL],
            "samples": len(self.returns),
            "fast_vol": self._fast_vol,
            "slow_vol": self._slow_vol,
            "ratio": self._ratio,
            "fast_window": self.fast_window,
            "slow_window": self.slow_window,
            "tick_count": self._tick_count,
        }

    def reset(self) -> None:
        """Reset for new session."""
        self.returns.clear()
        self.prev_price = None
        self.current_regime = VolatilityRegime.NORMAL
        self.current_threshold = REGIME_THRESHOLDS[VolatilityRegime.NORMAL] * self.scale_factor
        self._fast_vol = 0.0
        self._slow_vol = 0.0
        self._ratio = 1.0
        self._tick_count = 0
        logger.info("[REGIME] State reset")
