"""
Volatility Regime Detection System for Spike Capture Strategy.

Provides real-time detection of volatility regimes (LOW, MEDIUM, HIGH) and
maps each regime to appropriate spike thresholds for optimal signal capture.

Design Principles:
1. REAL-TIME ONLY: Uses only past data (no lookahead bias)
2. SIMPLE AND FAST: Threshold-based approach with adaptive scaling
3. INTEGRATED: Works seamlessly with EnhancedSpikeStrategy

Recommended Approach: Adaptive Threshold with Rolling ATR
- Rolling ATR provides stable volatility measure
- Percentile-based regime classification
- Regime-specific spike thresholds for better signal quality

Author: Claude Code
Date: January 17, 2026
"""

import logging
import math
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from src.strategies.ou_volatility import OUParameters

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS
# =============================================================================

# Rolling window sizes
DEFAULT_SHORT_WINDOW = 30      # 30 ticks (~6 seconds at 5 ticks/sec)
DEFAULT_LONG_WINDOW = 300      # 300 ticks (~60 seconds)
DEFAULT_ATR_PERIOD = 14        # Standard ATR period
DEFAULT_HISTORY_SIZE = 1000    # Maximum price history size

# Regime classification percentiles (from ATR distribution)
DEFAULT_LOW_PERCENTILE = 25    # Below 25th percentile = LOW
DEFAULT_HIGH_PERCENTILE = 75   # Above 75th percentile = HIGH

# Spike thresholds per regime (% change)
DEFAULT_REGIME_THRESHOLDS = {
    "LOW": 0.010,      # 0.01% - More sensitive in calm markets
    "MEDIUM": 0.020,   # 0.02% - Standard threshold (current default)
    "HIGH": 0.035,     # 0.035% - Higher threshold in volatile markets
}

# Minimum score thresholds per regime
DEFAULT_REGIME_MIN_SCORES = {
    "LOW": 0.35,       # Lower bar in calm markets (signals are reliable)
    "MEDIUM": 0.40,    # Standard threshold
    "HIGH": 0.50,      # Higher bar in volatile markets (avoid noise)
}

# Regime transition hysteresis (prevent rapid switching)
DEFAULT_HYSTERESIS_FACTOR = 0.10  # 10% buffer around thresholds

# OU-based adaptive threshold parameters (see PLAN_OU_ADAPTIVE_THRESHOLD.md)
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.005
OU_MAX_THRESHOLD = 0.10
OU_EWMA_HALFLIFE = 300  # 5 seconds at 60Hz


# =============================================================================
# ENUMS
# =============================================================================

class VolatilityRegime(Enum):
    """Volatility regime states."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"  # Initial state before enough data


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class RegimeState:
    """Current volatility regime state."""
    regime: VolatilityRegime = VolatilityRegime.UNKNOWN
    current_atr: float = 0.0
    baseline_atr: float = 0.0  # Long-term average ATR
    atr_percentile: float = 50.0  # Current ATR percentile
    spike_threshold: float = 0.02  # Current spike threshold
    min_score: float = 0.40  # Current minimum signal score
    last_transition_time: float = 0.0
    transition_count: int = 0

    @property
    def volatility_ratio(self) -> float:
        """Current volatility vs baseline (1.0 = normal)."""
        if self.baseline_atr <= 0:
            return 1.0
        return self.current_atr / self.baseline_atr


@dataclass
class ATRPoint:
    """Single ATR observation."""
    timestamp: float
    value: float
    true_range: float


# =============================================================================
# MAIN CLASS: Volatility Regime Detector
# =============================================================================

class VolatilityRegimeDetector:
    """
    Real-time volatility regime detection for spike capture strategy.

    Approach: Rolling ATR with percentile-based regime classification.

    Why This Approach (vs alternatives):

    1. HMM (Hidden Markov Model):
       - Pro: Elegant state transitions
       - Con: Requires offline training, complex, slow inference
       - Verdict: Overkill for this use case

    2. Simple MA Crossover:
       - Pro: Very simple
       - Con: Lagging, no magnitude information
       - Verdict: Too simple, loses important information

    3. Threshold-Based (CHOSEN):
       - Pro: Fast, interpretable, no training needed
       - Con: Fixed thresholds may need tuning
       - Verdict: Best balance of simplicity and effectiveness

    4. Adaptive Threshold (CHOSEN - ENHANCEMENT):
       - Pro: Automatically adjusts to market conditions
       - Con: Slightly more complex
       - Verdict: Recommended upgrade to threshold-based

    Usage:
        detector = VolatilityRegimeDetector()

        # On each price update
        regime = detector.update(current_price, high, low)
        threshold = detector.get_spike_threshold()
        min_score = detector.get_min_score()

        # Use in strategy
        if spike_magnitude >= threshold:
            if enhanced_score >= min_score:
                # Take signal
    """

    def __init__(
        self,
        short_window: int = DEFAULT_SHORT_WINDOW,
        long_window: int = DEFAULT_LONG_WINDOW,
        atr_period: int = DEFAULT_ATR_PERIOD,
        low_percentile: float = DEFAULT_LOW_PERCENTILE,
        high_percentile: float = DEFAULT_HIGH_PERCENTILE,
        regime_thresholds: Optional[Dict[str, float]] = None,
        regime_min_scores: Optional[Dict[str, float]] = None,
        hysteresis_factor: float = DEFAULT_HYSTERESIS_FACTOR,
        history_size: int = DEFAULT_HISTORY_SIZE,
        ou_params: Optional["OUParameters"] = None,
    ):
        """
        Initialize volatility regime detector.

        Args:
            short_window: Short-term volatility window (ticks)
            long_window: Long-term baseline window (ticks)
            atr_period: ATR calculation period
            low_percentile: Percentile threshold for LOW regime
            high_percentile: Percentile threshold for HIGH regime
            regime_thresholds: Spike thresholds per regime
            regime_min_scores: Minimum signal scores per regime
            hysteresis_factor: Buffer around thresholds to prevent rapid switching
            history_size: Maximum history size
            ou_params: Optional OUParameters for OU-based adaptive thresholds
        """
        # Configuration
        self.short_window = short_window
        self.long_window = long_window
        self.atr_period = atr_period
        self.low_percentile = low_percentile
        self.high_percentile = high_percentile
        self.hysteresis_factor = hysteresis_factor

        # Thresholds per regime
        self.regime_thresholds = regime_thresholds or DEFAULT_REGIME_THRESHOLDS.copy()
        self.regime_min_scores = regime_min_scores or DEFAULT_REGIME_MIN_SCORES.copy()

        # Price history for ATR calculation
        self._price_history: Deque[float] = deque(maxlen=history_size)
        self._high_history: Deque[float] = deque(maxlen=history_size)
        self._low_history: Deque[float] = deque(maxlen=history_size)

        # ATR history
        self._atr_history: Deque[ATRPoint] = deque(maxlen=history_size)
        self._true_range_history: Deque[float] = deque(maxlen=history_size)

        # Current state
        self.state = RegimeState()

        # Percentile tracking (for regime classification)
        self._atr_percentile_window: Deque[float] = deque(maxlen=long_window)

        # OU-based adaptive threshold support
        self.ou_params = ou_params
        self._ou_prev_price: Optional[float] = None
        self._ou_variance: float = 0.01  # Initial variance estimate
        self._ou_current_vol: float = 0.0
        self._ou_current_z: float = 0.0
        self._ou_alpha: float = 1 - 0.5 ** (1.0 / OU_EWMA_HALFLIFE)

        logger.info(
            f"[REGIME] Initialized: short={short_window}, long={long_window}, "
            f"atr_period={atr_period}, thresholds={self.regime_thresholds}"
        )
        if ou_params:
            logger.info(
                f"[REGIME] OU params: μ={ou_params.mu:.4f}, σ_stat={ou_params.sigma_stat:.4f}"
            )

    # =========================================================================
    # CORE UPDATE METHODS
    # =========================================================================

    def update(
        self,
        price: float,
        high: Optional[float] = None,
        low: Optional[float] = None,
    ) -> VolatilityRegime:
        """
        Update volatility state with new price data.

        For trade stream data (where high/low not available), we estimate
        true range from price changes.

        Args:
            price: Current price
            high: Period high (optional, will estimate if None)
            low: Period low (optional, will estimate if None)

        Returns:
            Current volatility regime
        """
        now = time.time()

        # Add to history
        self._price_history.append(price)

        # If high/low not provided, estimate from recent prices
        if high is None or low is None:
            high, low = self._estimate_high_low(price)

        self._high_history.append(high)
        self._low_history.append(low)

        # Need at least 2 prices for true range
        if len(self._price_history) < 2:
            return self.state.regime

        # Calculate True Range
        tr = self._calculate_true_range()
        self._true_range_history.append(tr)

        # Calculate ATR when we have enough data
        if len(self._true_range_history) >= self.atr_period:
            atr = self._calculate_atr()
            self._atr_percentile_window.append(atr)

            self.state.current_atr = atr

            # Record ATR point
            self._atr_history.append(ATRPoint(
                timestamp=now,
                value=atr,
                true_range=tr,
            ))

            # Update baseline (long-term average)
            if len(self._atr_percentile_window) >= self.short_window:
                self.state.baseline_atr = statistics.mean(self._atr_percentile_window)

            # Classify regime
            if len(self._atr_percentile_window) >= self.short_window:
                self._classify_regime(now)

        return self.state.regime

    def update_from_binance(self, price: float) -> VolatilityRegime:
        """
        Convenience method for Binance trade stream updates.

        Estimates high/low from recent price range.

        Args:
            price: Current Binance BTCUSDT price

        Returns:
            Current volatility regime
        """
        return self.update(price)

    # =========================================================================
    # REGIME CLASSIFICATION
    # =========================================================================

    def _classify_regime(self, now: float) -> None:
        """
        Classify current volatility regime based on ATR percentile.

        Uses hysteresis to prevent rapid regime switching.
        """
        old_regime = self.state.regime

        # Calculate current ATR percentile
        sorted_atr = sorted(self._atr_percentile_window)
        n = len(sorted_atr)
        current_atr = self.state.current_atr

        # Find percentile rank
        rank = sum(1 for x in sorted_atr if x < current_atr)
        percentile = (rank / n) * 100
        self.state.atr_percentile = percentile

        # Apply hysteresis for regime transitions
        hysteresis = self.hysteresis_factor * 100

        # Determine new regime with hysteresis
        if old_regime == VolatilityRegime.LOW:
            # Need to exceed threshold + hysteresis to transition out
            if percentile > self.high_percentile + hysteresis:
                new_regime = VolatilityRegime.HIGH
            elif percentile > self.low_percentile + hysteresis:
                new_regime = VolatilityRegime.MEDIUM
            else:
                new_regime = VolatilityRegime.LOW

        elif old_regime == VolatilityRegime.HIGH:
            # Need to drop below threshold - hysteresis to transition out
            if percentile < self.low_percentile - hysteresis:
                new_regime = VolatilityRegime.LOW
            elif percentile < self.high_percentile - hysteresis:
                new_regime = VolatilityRegime.MEDIUM
            else:
                new_regime = VolatilityRegime.HIGH

        elif old_regime == VolatilityRegime.MEDIUM:
            # Apply hysteresis in both directions
            if percentile < self.low_percentile - hysteresis:
                new_regime = VolatilityRegime.LOW
            elif percentile > self.high_percentile + hysteresis:
                new_regime = VolatilityRegime.HIGH
            else:
                new_regime = VolatilityRegime.MEDIUM

        else:
            # UNKNOWN state - use raw thresholds
            if percentile < self.low_percentile:
                new_regime = VolatilityRegime.LOW
            elif percentile > self.high_percentile:
                new_regime = VolatilityRegime.HIGH
            else:
                new_regime = VolatilityRegime.MEDIUM

        # Update state
        if new_regime != old_regime:
            self.state.regime = new_regime
            self.state.last_transition_time = now
            self.state.transition_count += 1

            logger.info(
                f"[REGIME] Transition: {old_regime.value} -> {new_regime.value} | "
                f"ATR={self.state.current_atr:.6f}, percentile={percentile:.1f}%"
            )

        # Update thresholds based on regime
        regime_key = new_regime.value.upper()
        self.state.spike_threshold = self.regime_thresholds.get(
            regime_key, DEFAULT_REGIME_THRESHOLDS["MEDIUM"]
        )
        self.state.min_score = self.regime_min_scores.get(
            regime_key, DEFAULT_REGIME_MIN_SCORES["MEDIUM"]
        )

    # =========================================================================
    # ATR CALCULATION
    # =========================================================================

    def _calculate_true_range(self) -> float:
        """
        Calculate True Range (TR) for current period.

        TR = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )
        """
        if len(self._price_history) < 2:
            return 0.0

        current_high = self._high_history[-1]
        current_low = self._low_history[-1]
        previous_close = self._price_history[-2]

        # True Range components
        range1 = current_high - current_low
        range2 = abs(current_high - previous_close)
        range3 = abs(current_low - previous_close)

        return max(range1, range2, range3)

    def _calculate_atr(self) -> float:
        """
        Calculate Average True Range (ATR) using simple moving average.

        Uses SMA for simplicity and stability. EMA could be used for
        more responsiveness but adds complexity.
        """
        if len(self._true_range_history) < self.atr_period:
            return 0.0

        recent_tr = list(self._true_range_history)[-self.atr_period:]
        return statistics.mean(recent_tr)

    def _estimate_high_low(self, price: float) -> Tuple[float, float]:
        """
        Estimate high/low from recent price movements.

        Used when only trade prices available (no candlestick data).
        """
        if len(self._price_history) < 2:
            # First price - use small spread estimate
            spread = price * 0.0001  # 0.01% spread
            return price + spread / 2, price - spread / 2

        # Use range from last few prices
        recent = list(self._price_history)[-min(5, len(self._price_history)):]
        recent_high = max(recent)
        recent_low = min(recent)

        # Extend slightly beyond observed range
        high = max(price, recent_high)
        low = min(price, recent_low)

        return high, low

    # =========================================================================
    # THRESHOLD GETTERS
    # =========================================================================

    def get_spike_threshold(self) -> float:
        """
        Get current spike threshold based on volatility regime.

        Returns:
            Spike threshold in percentage (e.g., 0.02 for 0.02%)
        """
        return self.state.spike_threshold

    def get_min_score(self) -> float:
        """
        Get minimum signal score threshold based on volatility regime.

        Returns:
            Minimum composite score to accept signals
        """
        return self.state.min_score

    def get_adaptive_threshold(self, base_threshold: float = 0.02) -> float:
        """
        Get adaptive spike threshold scaled by volatility ratio.

        Formula: threshold = base_threshold * (current_vol / baseline_vol)

        This scales the threshold proportionally to current volatility.
        In high vol: higher threshold (avoid noise)
        In low vol: lower threshold (catch small moves)

        Args:
            base_threshold: Base spike threshold (default 0.02%)

        Returns:
            Adapted spike threshold
        """
        ratio = self.state.volatility_ratio

        # Clamp ratio to reasonable bounds
        ratio = max(0.5, min(ratio, 2.5))

        return base_threshold * ratio

    def get_ou_adaptive_threshold(
        self,
        price: Optional[float] = None,
        base_threshold: float = OU_BASE_THRESHOLD,
    ) -> float:
        """
        Get OU-based adaptive spike threshold using z-score sigmoid mapping.

        Uses the Ornstein-Uhlenbeck process model for volatility:
        - Computes EWMA volatility from returns
        - Maps log-volatility to z-score using OU stationary distribution
        - Applies sigmoid mapping: threshold = base * multiplier

        Multiplier formula:
            z = (log(vol) - μ) / σ_stat
            multiplier = k_low + (k_high - k_low) / (1 + exp(-steepness * z))

        Args:
            price: Current price (updates EWMA volatility if provided)
            base_threshold: Base spike threshold (default 0.02%)

        Returns:
            Adaptive spike threshold, or base_threshold if OU params not set
        """
        if self.ou_params is None:
            return base_threshold

        # Update EWMA volatility if price provided
        if price is not None and self._ou_prev_price is not None:
            ret = (price - self._ou_prev_price) / self._ou_prev_price * 100
            self._ou_variance = (
                self._ou_alpha * (ret ** 2) +
                (1 - self._ou_alpha) * self._ou_variance
            )
            self._ou_current_vol = max(math.sqrt(self._ou_variance), 1e-6)

        if price is not None:
            self._ou_prev_price = price

        # Compute z-score
        if self._ou_current_vol <= 0:
            return base_threshold

        log_vol = math.log(self._ou_current_vol)
        self._ou_current_z = (log_vol - self.ou_params.mu) / self.ou_params.sigma_stat

        # Sigmoid mapping
        z_clamped = max(-10, min(10, self._ou_current_z * OU_SIGMOID_STEEPNESS))
        sigmoid = 1.0 / (1.0 + math.exp(-z_clamped))
        multiplier = OU_K_LOW + (OU_K_HIGH - OU_K_LOW) * sigmoid

        # Compute threshold with bounds
        threshold = base_threshold * multiplier
        return max(OU_MIN_THRESHOLD, min(OU_MAX_THRESHOLD, threshold))

    def get_ou_z_score(self) -> float:
        """Get current OU z-score (for monitoring)."""
        return self._ou_current_z

    def get_ou_volatility(self) -> float:
        """Get current EWMA volatility (for monitoring)."""
        return self._ou_current_vol

    # =========================================================================
    # REGIME QUERIES
    # =========================================================================

    def get_regime(self) -> VolatilityRegime:
        """Get current volatility regime."""
        return self.state.regime

    def get_regime_name(self) -> str:
        """Get current regime as string."""
        return self.state.regime.value.upper()

    def is_high_volatility(self) -> bool:
        """Check if currently in HIGH volatility regime."""
        return self.state.regime == VolatilityRegime.HIGH

    def is_low_volatility(self) -> bool:
        """Check if currently in LOW volatility regime."""
        return self.state.regime == VolatilityRegime.LOW

    def time_in_regime(self) -> float:
        """Get seconds since last regime transition."""
        if self.state.last_transition_time <= 0:
            return 0.0
        return time.time() - self.state.last_transition_time

    # =========================================================================
    # STATUS AND DIAGNOSTICS
    # =========================================================================

    def get_status(self) -> Dict:
        """Get current detector status."""
        return {
            "regime": self.state.regime.value,
            "current_atr": self.state.current_atr,
            "baseline_atr": self.state.baseline_atr,
            "volatility_ratio": self.state.volatility_ratio,
            "atr_percentile": self.state.atr_percentile,
            "spike_threshold": self.state.spike_threshold,
            "min_score": self.state.min_score,
            "transition_count": self.state.transition_count,
            "time_in_regime_sec": self.time_in_regime(),
            "history_size": len(self._price_history),
        }

    def reset(self) -> None:
        """Reset detector for new market."""
        self._price_history.clear()
        self._high_history.clear()
        self._low_history.clear()
        self._atr_history.clear()
        self._true_range_history.clear()
        self._atr_percentile_window.clear()

        self.state = RegimeState()

        logger.info("[REGIME] Reset for new market")

    def __repr__(self) -> str:
        return (
            f"VolatilityRegimeDetector("
            f"regime={self.state.regime.value}, "
            f"atr={self.state.current_atr:.6f}, "
            f"threshold={self.state.spike_threshold:.4f}%)"
        )


# =============================================================================
# SIMPLE MOVING AVERAGE CROSSOVER DETECTOR (ALTERNATIVE)
# =============================================================================

class SimpleMACrossoverDetector:
    """
    Simple volatility regime detector using MA crossover.

    Simpler than ATR-based but less accurate.
    Use when:
    - You want minimal computation
    - You don't have high/low data
    - You prefer simplicity over accuracy

    Approach:
        regime = HIGH when short_vol > long_vol * k
        regime = LOW when short_vol < long_vol / k
        regime = MEDIUM otherwise
    """

    def __init__(
        self,
        short_window: int = 20,
        long_window: int = 100,
        threshold_multiplier: float = 1.5,
    ):
        """
        Initialize MA crossover detector.

        Args:
            short_window: Short-term window for volatility
            long_window: Long-term window for volatility
            threshold_multiplier: k value for regime thresholds
        """
        self.short_window = short_window
        self.long_window = long_window
        self.threshold_multiplier = threshold_multiplier

        self._returns: Deque[float] = deque(maxlen=long_window)
        self._last_price: Optional[float] = None
        self.regime = VolatilityRegime.UNKNOWN

    def update(self, price: float) -> VolatilityRegime:
        """Update with new price and return current regime."""
        if self._last_price is not None:
            ret = (price - self._last_price) / self._last_price * 100
            self._returns.append(abs(ret))  # Use absolute returns for vol

        self._last_price = price

        if len(self._returns) < self.long_window:
            return self.regime

        # Calculate short and long term volatility
        short_vol = statistics.mean(list(self._returns)[-self.short_window:])
        long_vol = statistics.mean(self._returns)

        if long_vol <= 0:
            return self.regime

        ratio = short_vol / long_vol

        if ratio > self.threshold_multiplier:
            self.regime = VolatilityRegime.HIGH
        elif ratio < 1 / self.threshold_multiplier:
            self.regime = VolatilityRegime.LOW
        else:
            self.regime = VolatilityRegime.MEDIUM

        return self.regime

    def reset(self) -> None:
        """Reset for new market."""
        self._returns.clear()
        self._last_price = None
        self.regime = VolatilityRegime.UNKNOWN


# =============================================================================
# INTEGRATION HELPER: Enhanced Spike Strategy Wrapper
# =============================================================================

class RegimeAwareEnhancedSpike:
    """
    Wrapper that adds volatility regime awareness to EnhancedSpikeStrategy.

    Usage:
        from src.strategies.enhanced_spike import EnhancedSpikeStrategy
        from src.strategies.volatility_regime import RegimeAwareEnhancedSpike

        # Create base strategy
        base_strategy = EnhancedSpikeStrategy(base_size=15)

        # Wrap with regime awareness
        strategy = RegimeAwareEnhancedSpike(base_strategy)

        # On each price update
        strategy.update_regime(binance_price)

        # Get quotes (uses regime-adjusted thresholds automatically)
        quotes = strategy.get_quotes(...)
    """

    def __init__(
        self,
        strategy: "EnhancedSpikeStrategy",
        detector: Optional[VolatilityRegimeDetector] = None,
    ):
        """
        Initialize regime-aware wrapper.

        Args:
            strategy: EnhancedSpikeStrategy instance to wrap
            detector: VolatilityRegimeDetector (creates default if None)
        """
        self.strategy = strategy
        self.detector = detector or VolatilityRegimeDetector()

        # Store original thresholds for reference
        self._base_spike_threshold = strategy.spike_threshold

        logger.info(
            f"[REGIME] Wrapped EnhancedSpikeStrategy with regime detection"
        )

    def update_regime(self, price: float) -> VolatilityRegime:
        """
        Update volatility regime with new price.

        This should be called on every Binance price update.

        Args:
            price: Current Binance BTCUSDT price

        Returns:
            Current volatility regime
        """
        regime = self.detector.update_from_binance(price)

        # Update strategy's spike threshold based on regime
        self.strategy.spike_threshold = self.detector.get_spike_threshold()

        return regime

    def get_quotes(self, *args, **kwargs) -> List[Dict]:
        """
        Get quotes with regime-aware thresholds.

        Passes through to underlying strategy after adjusting parameters.
        """
        # Get minimum score based on regime
        min_score = self.detector.get_min_score()

        # Call underlying strategy
        # Note: The strategy already has updated spike_threshold from update_regime
        return self.strategy.get_quotes(*args, **kwargs)

    def should_take_enhanced_signal(
        self,
        spike_dir: Optional[str],
        spike_magnitude: float,
        velocity_bps: float,
        time_remaining: float,
    ) -> Tuple[bool, float, Optional[str]]:
        """
        Regime-aware signal filtering.

        Uses regime-specific minimum score threshold.
        """
        min_score = self.detector.get_min_score()

        return self.strategy.should_take_enhanced_signal(
            spike_dir=spike_dir,
            spike_magnitude=spike_magnitude,
            velocity_bps=velocity_bps,
            time_remaining=time_remaining,
            min_score=min_score,
        )

    def get_regime_status(self) -> Dict:
        """Get current regime status."""
        return self.detector.get_status()

    def reset(self) -> None:
        """Reset both strategy and detector for new market."""
        self.strategy.reset()
        self.detector.reset()
        # Restore base threshold
        self.strategy.spike_threshold = self._base_spike_threshold

    def __repr__(self) -> str:
        return (
            f"RegimeAwareEnhancedSpike("
            f"regime={self.detector.get_regime_name()}, "
            f"threshold={self.strategy.spike_threshold:.4f}%)"
        )


# =============================================================================
# STANDALONE FUNCTIONS
# =============================================================================

def calculate_rolling_atr(
    prices: List[float],
    highs: Optional[List[float]] = None,
    lows: Optional[List[float]] = None,
    period: int = 14,
) -> float:
    """
    Calculate ATR from price history.

    Args:
        prices: Price history (close prices)
        highs: High prices (optional, will estimate if None)
        lows: Low prices (optional, will estimate if None)
        period: ATR period

    Returns:
        Current ATR value
    """
    if len(prices) < period + 1:
        return 0.0

    # Estimate highs/lows if not provided
    if highs is None or lows is None:
        highs = prices
        lows = prices

    true_ranges = []
    for i in range(1, len(prices)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - prices[i-1]),
            abs(lows[i] - prices[i-1]),
        )
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return 0.0

    return statistics.mean(true_ranges[-period:])


def classify_regime_simple(
    current_vol: float,
    baseline_vol: float,
    low_ratio: float = 0.7,
    high_ratio: float = 1.5,
) -> VolatilityRegime:
    """
    Simple volatility regime classification.

    Args:
        current_vol: Current volatility measure
        baseline_vol: Baseline volatility measure
        low_ratio: Ratio below which is LOW
        high_ratio: Ratio above which is HIGH

    Returns:
        VolatilityRegime
    """
    if baseline_vol <= 0:
        return VolatilityRegime.UNKNOWN

    ratio = current_vol / baseline_vol

    if ratio < low_ratio:
        return VolatilityRegime.LOW
    elif ratio > high_ratio:
        return VolatilityRegime.HIGH
    else:
        return VolatilityRegime.MEDIUM


def get_regime_adjusted_threshold(
    base_threshold: float,
    regime: VolatilityRegime,
    thresholds: Optional[Dict[str, float]] = None,
) -> float:
    """
    Get spike threshold adjusted for volatility regime.

    Args:
        base_threshold: Base spike threshold
        regime: Current volatility regime
        thresholds: Optional custom thresholds per regime

    Returns:
        Adjusted spike threshold
    """
    if thresholds is None:
        thresholds = DEFAULT_REGIME_THRESHOLDS

    regime_key = regime.value.upper()
    if regime_key in thresholds:
        return thresholds[regime_key]

    return base_threshold
