"""
Adaptive Spike Detection Thresholds for 60Hz BTC Data.

This module provides multiple approaches for calculating volatility-adaptive
thresholds for spike detection, replacing fixed thresholds that fail in
varying market conditions.

Usage:
    from research.adaptive_threshold import (
        EWMAThreshold,
        StdDevThreshold,
        PercentileThreshold,
        ATRThreshold,
        RegimeThreshold,
        HybridThreshold,
        OUThreshold,          # NEW: OU-based adaptive (recommended)
        AdaptiveSpikeDetector
    )

    # Simple usage
    detector = AdaptiveSpikeDetector(threshold_method="hybrid")
    for price in price_stream:
        direction, magnitude, threshold = detector.update(price)
        if direction:
            print(f"Spike {direction}: {magnitude:.4f}% (threshold: {threshold:.4f}%)")

Author: Research Team
Date: January 17, 2026
Context: Polymarket BTC 15-minute prediction markets, 60Hz Binance data
"""

import numpy as np
from collections import deque
from typing import Optional, Tuple, Dict, List, Callable
from dataclasses import dataclass
from enum import Enum


class VolatilityRegime(Enum):
    """Discrete volatility regimes for regime-based thresholding."""
    CALM = "CALM"
    NORMAL = "NORMAL"
    ACTIVE = "ACTIVE"
    SPIKE = "SPIKE"


@dataclass
class ThresholdState:
    """Current state of an adaptive threshold engine."""
    threshold: float
    volatility: float
    samples: int
    regime: Optional[str] = None


# =============================================================================
# 1. EWMA (Exponential Weighted Moving Average) Threshold
# =============================================================================

class EWMAThreshold:
    """
    Adaptive spike threshold using Exponential Weighted Moving Average volatility.

    EWMA reacts quickly to regime changes while smoothing noise.
    Most efficient: O(1) time and space per update.

    Formula:
        sigma^2_t = lambda * sigma^2_{t-1} + (1 - lambda) * r^2_t
        threshold = k * sqrt(sigma^2_t)

    Parameters:
        half_life_ticks: Number of ticks for volatility estimate to decay by half
            - 600 ticks (~10s at 60Hz): Very responsive
            - 1800 ticks (~30s at 60Hz): Balanced (recommended)
            - 3600 ticks (~60s at 60Hz): Smooth
        k_multiplier: Number of sigma for threshold (2.0-3.0 typical)
        min_threshold: Floor to prevent threshold from getting too tight
        max_threshold: Ceiling to prevent threshold from getting too loose
        initial_vol: Starting volatility estimate (%)
    """

    def __init__(
        self,
        half_life_ticks: int = 1800,
        k_multiplier: float = 2.5,
        min_threshold: float = 0.005,
        max_threshold: float = 0.10,
        initial_vol: float = 0.015
    ):
        # Calculate lambda from half-life: lambda = 0.5^(1/half_life)
        self.lambda_decay = 0.5 ** (1.0 / half_life_ticks)
        self.k = k_multiplier
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold
        self.half_life = half_life_ticks

        # State
        self.prev_price: Optional[float] = None
        self.variance: float = initial_vol ** 2
        self.tick_count: int = 0

    def update(self, price: float) -> Optional[float]:
        """
        Update with new tick and return current threshold.

        Args:
            price: Current BTC price

        Returns:
            Adaptive threshold as percentage, or None if first tick
        """
        if self.prev_price is None:
            self.prev_price = price
            return self.min_threshold

        # Calculate return (percentage)
        ret = (price - self.prev_price) / self.prev_price * 100
        self.prev_price = price
        self.tick_count += 1

        # EWMA variance update: sigma^2_t = lambda * sigma^2_{t-1} + (1-lambda) * r^2_t
        self.variance = (
            self.lambda_decay * self.variance +
            (1 - self.lambda_decay) * (ret ** 2)
        )

        # Threshold from volatility
        vol = np.sqrt(self.variance)
        threshold = self.k * vol

        # Apply bounds
        return max(self.min_threshold, min(threshold, self.max_threshold))

    def get_threshold(self, current_price: float = None) -> float:
        """Get current threshold without updating state."""
        if self.variance == 0:
            return self.min_threshold
        vol = np.sqrt(self.variance)
        threshold = self.k * vol
        return max(self.min_threshold, min(threshold, self.max_threshold))

    @property
    def current_volatility(self) -> float:
        """Current volatility estimate in percentage."""
        return np.sqrt(self.variance)

    def get_state(self) -> ThresholdState:
        """Get current state for monitoring."""
        return ThresholdState(
            threshold=self.get_threshold(),
            volatility=self.current_volatility,
            samples=self.tick_count
        )

    def reset(self, initial_vol: float = 0.015) -> None:
        """Reset state for new session."""
        self.prev_price = None
        self.variance = initial_vol ** 2
        self.tick_count = 0


# =============================================================================
# 2. Rolling Standard Deviation Threshold
# =============================================================================

class StdDevThreshold:
    """
    Adaptive spike threshold using rolling standard deviation of returns.

    Simple and interpretable approach based on normal distribution assumptions.
    Uses efficient O(1) running statistics updates.

    Formula:
        threshold = |mean(returns)| + k * std(returns)

    Parameters:
        window_ticks: Rolling window size in ticks
            - 1800 ticks (~30s at 60Hz): Short-term
            - 3600 ticks (~60s at 60Hz): Medium-term (recommended)
            - 7200 ticks (~120s at 60Hz): Longer-term
        k_sigma: Number of standard deviations (2.0-3.0 typical)
        min_threshold: Floor threshold
        max_threshold: Ceiling threshold
    """

    def __init__(
        self,
        window_ticks: int = 1800,
        k_sigma: float = 2.5,
        min_threshold: float = 0.005,
        max_threshold: float = 0.2
    ):
        self.window_ticks = window_ticks
        self.k_sigma = k_sigma
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold

        # State
        self.returns: deque = deque(maxlen=window_ticks)
        self.prev_price: Optional[float] = None

        # Running statistics for O(1) updates (Welford's algorithm)
        self._n: int = 0
        self._mean: float = 0.0
        self._M2: float = 0.0  # Sum of squared deviations

    def _update_stats(self, new_value: float, old_value: Optional[float] = None) -> None:
        """Update running mean and variance using Welford's online algorithm."""
        if old_value is not None:
            # Remove old value (reverse Welford step)
            self._n -= 1
            if self._n > 0:
                delta = old_value - self._mean
                self._mean -= delta / self._n
                delta2 = old_value - self._mean
                self._M2 -= delta * delta2
            else:
                self._mean = 0.0
                self._M2 = 0.0

        # Add new value
        self._n += 1
        delta = new_value - self._mean
        self._mean += delta / self._n
        delta2 = new_value - self._mean
        self._M2 += delta * delta2

    def update(self, price: float) -> Optional[float]:
        """
        Update with new tick and return current threshold.

        Args:
            price: Current BTC price

        Returns:
            Adaptive threshold as percentage, or None if insufficient data
        """
        if self.prev_price is None:
            self.prev_price = price
            return None

        # Calculate return
        ret = (price - self.prev_price) / self.prev_price * 100
        self.prev_price = price

        # Handle window overflow
        old_value = None
        if len(self.returns) == self.window_ticks:
            old_value = self.returns[0]

        self.returns.append(ret)
        self._update_stats(ret, old_value)

        # Need minimum samples
        if self._n < 100:
            return None

        # Calculate statistics
        variance = self._M2 / self._n if self._n > 0 else 0
        std = np.sqrt(max(variance, 0))

        # Threshold formula: |mean| + k * std
        threshold = abs(self._mean) + self.k_sigma * std

        # Apply bounds
        return max(self.min_threshold, min(threshold, self.max_threshold))

    def get_stats(self) -> Dict:
        """Get current statistics for debugging/monitoring."""
        variance = self._M2 / self._n if self._n > 0 else 0
        std = np.sqrt(max(variance, 0))

        return {
            "mean": self._mean,
            "std": std,
            "threshold": abs(self._mean) + self.k_sigma * std,
            "samples": self._n
        }

    def get_state(self) -> ThresholdState:
        """Get current state for monitoring."""
        stats = self.get_stats()
        return ThresholdState(
            threshold=max(self.min_threshold, min(stats["threshold"], self.max_threshold)),
            volatility=stats["std"],
            samples=stats["samples"]
        )

    def reset(self) -> None:
        """Reset state for new session."""
        self.returns.clear()
        self.prev_price = None
        self._n = 0
        self._mean = 0.0
        self._M2 = 0.0


# =============================================================================
# 3. Percentile-Based Threshold
# =============================================================================

class PercentileThreshold:
    """
    Adaptive spike threshold using rolling percentile of absolute returns.

    Non-parametric approach: makes no distributional assumptions.
    Handles fat tails naturally (common in financial returns).

    Formula:
        threshold = percentile(|returns|, p)

    Parameters:
        window_ticks: Rolling window size
        percentile: Which percentile to use (90-99 typical)
        update_frequency: Recalculate every N ticks (O(n log n) operation)
        min_threshold: Floor threshold
        max_threshold: Ceiling threshold
    """

    def __init__(
        self,
        window_ticks: int = 3600,
        percentile: float = 95.0,
        min_threshold: float = 0.005,
        max_threshold: float = 0.2,
        update_frequency: int = 60
    ):
        self.window_ticks = window_ticks
        self.percentile = percentile
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold
        self.update_frequency = update_frequency

        # State
        self.abs_returns: deque = deque(maxlen=window_ticks)
        self.prev_price: Optional[float] = None
        self.current_threshold: float = min_threshold
        self.tick_count: int = 0

    def update(self, price: float) -> Optional[float]:
        """
        Update with new tick and return current threshold.

        Note: Percentile is recalculated every update_frequency ticks
        for efficiency (O(n log n) operation).
        """
        if self.prev_price is None:
            self.prev_price = price
            return None

        # Calculate absolute return
        ret = abs((price - self.prev_price) / self.prev_price * 100)
        self.prev_price = price
        self.abs_returns.append(ret)
        self.tick_count += 1

        # Minimum samples
        if len(self.abs_returns) < 100:
            return None

        # Recalculate percentile periodically
        if self.tick_count % self.update_frequency == 0:
            self.current_threshold = np.percentile(
                list(self.abs_returns),
                self.percentile
            )
            # Apply bounds
            self.current_threshold = max(
                self.min_threshold,
                min(self.current_threshold, self.max_threshold)
            )

        return self.current_threshold

    def get_percentile_distribution(self) -> Dict:
        """Get distribution statistics for analysis."""
        if len(self.abs_returns) < 10:
            return {}

        arr = np.array(self.abs_returns)
        return {
            "p50": float(np.percentile(arr, 50)),
            "p75": float(np.percentile(arr, 75)),
            "p90": float(np.percentile(arr, 90)),
            "p95": float(np.percentile(arr, 95)),
            "p99": float(np.percentile(arr, 99)),
            "max": float(np.max(arr)),
            "current_threshold": self.current_threshold
        }

    def get_state(self) -> ThresholdState:
        """Get current state for monitoring."""
        return ThresholdState(
            threshold=self.current_threshold,
            volatility=float(np.std(self.abs_returns)) if len(self.abs_returns) > 1 else 0,
            samples=len(self.abs_returns)
        )

    def reset(self) -> None:
        """Reset state for new session."""
        self.abs_returns.clear()
        self.prev_price = None
        self.current_threshold = self.min_threshold
        self.tick_count = 0


# =============================================================================
# 4. ATR (Average True Range) Threshold
# =============================================================================

class ATRThreshold:
    """
    Adaptive spike threshold using Average True Range.

    Originally designed for OHLC data, adapted for tick data by creating
    micro-candles from rolling windows.

    Formula:
        TR = max(High - Low, |High - PrevClose|, |Low - PrevClose|)
        ATR = EMA(TR, n_periods) or SMA(TR, n_periods)
        threshold = k * ATR / price * 100

    Parameters:
        ticks_per_candle: Aggregate this many ticks into one micro-candle
        atr_periods: Number of candles for ATR calculation
        multiplier: k factor
        use_ema: Use EMA (True) or SMA (False) for ATR
    """

    def __init__(
        self,
        ticks_per_candle: int = 60,
        atr_periods: int = 60,
        multiplier: float = 2.0,
        use_ema: bool = True,
        min_threshold: float = 0.005,
        max_threshold: float = 0.2
    ):
        self.ticks_per_candle = ticks_per_candle
        self.atr_periods = atr_periods
        self.multiplier = multiplier
        self.use_ema = use_ema
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold

        # State
        self.tick_buffer: deque = deque(maxlen=ticks_per_candle)
        self.tr_values: deque = deque(maxlen=atr_periods)
        self.prev_close: Optional[float] = None
        self.current_atr: float = 0.0
        self.ema_alpha = 2.0 / (atr_periods + 1)
        self.last_price: float = 0.0

    def update(self, price: float) -> Optional[float]:
        """
        Update with new tick and return current threshold.

        Returns threshold only when a new candle completes.
        """
        self.tick_buffer.append(price)
        self.last_price = price

        # Not enough ticks for a candle yet
        if len(self.tick_buffer) < self.ticks_per_candle:
            # Return current threshold if available
            if self.current_atr > 0:
                return self._calculate_threshold(price)
            return None

        # Form micro-candle
        candle_high = max(self.tick_buffer)
        candle_low = min(self.tick_buffer)
        candle_close = self.tick_buffer[-1]

        # Calculate True Range
        if self.prev_close is None:
            tr = candle_high - candle_low
        else:
            tr = max(
                candle_high - candle_low,
                abs(candle_high - self.prev_close),
                abs(candle_low - self.prev_close)
            )

        self.prev_close = candle_close
        self.tr_values.append(tr)
        self.tick_buffer.clear()

        # Need at least 2 TR values
        if len(self.tr_values) < 2:
            return None

        # Calculate ATR
        if self.use_ema:
            if self.current_atr == 0:
                self.current_atr = float(np.mean(self.tr_values))
            else:
                self.current_atr = (
                    self.ema_alpha * tr +
                    (1 - self.ema_alpha) * self.current_atr
                )
        else:
            self.current_atr = float(np.mean(self.tr_values))

        return self._calculate_threshold(price)

    def _calculate_threshold(self, price: float) -> float:
        """Convert ATR to percentage threshold."""
        if price <= 0:
            return self.min_threshold
        threshold_pct = (self.multiplier * self.current_atr / price) * 100
        return max(self.min_threshold, min(threshold_pct, self.max_threshold))

    def get_threshold(self, current_price: float = None) -> float:
        """Get current threshold without updating state."""
        price = current_price or self.last_price
        if self.current_atr == 0 or price <= 0:
            return self.min_threshold
        return self._calculate_threshold(price)

    def get_state(self) -> ThresholdState:
        """Get current state for monitoring."""
        return ThresholdState(
            threshold=self.get_threshold(self.last_price),
            volatility=self.current_atr,
            samples=len(self.tr_values)
        )

    def reset(self) -> None:
        """Reset state for new session."""
        self.tick_buffer.clear()
        self.tr_values.clear()
        self.prev_close = None
        self.current_atr = 0.0
        self.last_price = 0.0


# =============================================================================
# 5. Regime-Based Threshold
# =============================================================================

class RegimeThreshold:
    """
    Regime-based adaptive threshold using volatility state detection.

    Uses fast/slow volatility crossover to detect regime changes.
    Each regime has a fixed, optimized threshold.

    Parameters:
        fast_window: Fast volatility window (ticks)
        slow_window: Slow volatility window (ticks)
        regime_bounds: (low_ratio, high_ratio) for regime classification
    """

    # Regime thresholds (percentage moves) - tune these based on backtest
    THRESHOLDS = {
        VolatilityRegime.CALM: 0.008,
        VolatilityRegime.NORMAL: 0.015,
        VolatilityRegime.ACTIVE: 0.025,
        VolatilityRegime.SPIKE: 0.05
    }

    def __init__(
        self,
        fast_window: int = 300,
        slow_window: int = 3600,
        regime_bounds: Tuple[float, float] = (0.5, 1.5),
        min_threshold: float = 0.005,
        max_threshold: float = 0.2
    ):
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.low_ratio, self.high_ratio = regime_bounds
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold

        # State
        self.returns: deque = deque(maxlen=slow_window)
        self.prev_price: Optional[float] = None
        self.current_regime: VolatilityRegime = VolatilityRegime.NORMAL

    def _calculate_vol(self, window: int) -> float:
        """Calculate rolling volatility for a given window."""
        if len(self.returns) < window:
            return 0.0
        recent = list(self.returns)[-window:]
        return float(np.std(recent)) if len(recent) > 1 else 0.0

    def update(self, price: float) -> Tuple[float, str]:
        """
        Update and return (threshold, regime_name).
        """
        if self.prev_price is None:
            self.prev_price = price
            return self.THRESHOLDS[VolatilityRegime.NORMAL], "NORMAL"

        ret = (price - self.prev_price) / self.prev_price * 100
        self.prev_price = price
        self.returns.append(ret)

        # Need enough data
        if len(self.returns) < self.fast_window:
            return self.THRESHOLDS[VolatilityRegime.NORMAL], "NORMAL"

        # Calculate fast and slow volatility
        fast_vol = self._calculate_vol(self.fast_window)
        slow_vol = self._calculate_vol(self.slow_window)

        # Determine regime from ratio
        if slow_vol == 0:
            ratio = 1.0
        else:
            ratio = fast_vol / slow_vol

        # Classify regime with hysteresis
        if ratio < self.low_ratio:
            self.current_regime = VolatilityRegime.CALM
        elif ratio > self.high_ratio * 2:
            self.current_regime = VolatilityRegime.SPIKE
        elif ratio > self.high_ratio:
            self.current_regime = VolatilityRegime.ACTIVE
        else:
            self.current_regime = VolatilityRegime.NORMAL

        threshold = self.THRESHOLDS[self.current_regime]
        return max(self.min_threshold, min(threshold, self.max_threshold)), self.current_regime.value

    def get_state(self) -> ThresholdState:
        """Get current state for monitoring."""
        fast_vol = self._calculate_vol(self.fast_window)
        return ThresholdState(
            threshold=self.THRESHOLDS[self.current_regime],
            volatility=fast_vol,
            samples=len(self.returns),
            regime=self.current_regime.value
        )

    def reset(self) -> None:
        """Reset state for new session."""
        self.returns.clear()
        self.prev_price = None
        self.current_regime = VolatilityRegime.NORMAL


# =============================================================================
# 6. Hybrid Threshold (RECOMMENDED)
# =============================================================================

class HybridThreshold:
    """
    Hybrid adaptive threshold combining EWMA (fast) and Percentile (robust).

    Takes the maximum of EWMA threshold and scaled percentile threshold.
    This prevents threshold from dropping too low during quiet periods
    while still adapting quickly to volatility spikes.

    Formula:
        threshold = max(EWMA_threshold, Percentile_threshold * scale)

    This is the RECOMMENDED approach for 60Hz BTC spike detection.
    """

    def __init__(
        self,
        ewma_half_life: int = 1800,
        ewma_k: float = 2.5,
        percentile_window: int = 7200,
        percentile_value: float = 90.0,
        percentile_scale: float = 0.8,
        min_threshold: float = 0.005,
        max_threshold: float = 0.10
    ):
        self.ewma = EWMAThreshold(
            half_life_ticks=ewma_half_life,
            k_multiplier=ewma_k,
            min_threshold=min_threshold,
            max_threshold=max_threshold
        )
        self.percentile = PercentileThreshold(
            window_ticks=percentile_window,
            percentile=percentile_value,
            min_threshold=min_threshold,
            max_threshold=max_threshold
        )
        self.percentile_scale = percentile_scale
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold
        self.prev_price: Optional[float] = None

    def update(self, price: float) -> float:
        """
        Update both engines and return the combined threshold.
        """
        self.prev_price = price

        ewma_thresh = self.ewma.update(price)
        pctl_thresh = self.percentile.update(price)

        # Use defaults if None
        ewma_thresh = ewma_thresh or self.min_threshold
        pctl_thresh = pctl_thresh or self.min_threshold

        # Take max of EWMA and scaled percentile
        threshold = max(ewma_thresh, pctl_thresh * self.percentile_scale)

        # Apply bounds
        return max(self.min_threshold, min(threshold, self.max_threshold))

    def get_components(self) -> Dict:
        """Get individual component thresholds for monitoring."""
        ewma_thresh = self.ewma.get_threshold() if self.prev_price else self.min_threshold
        pctl_thresh = self.percentile.current_threshold

        return {
            "ewma": ewma_thresh,
            "percentile": pctl_thresh,
            "percentile_scaled": pctl_thresh * self.percentile_scale,
            "final": max(ewma_thresh, pctl_thresh * self.percentile_scale)
        }

    def get_state(self) -> ThresholdState:
        """Get current state for monitoring."""
        components = self.get_components()
        return ThresholdState(
            threshold=components["final"],
            volatility=self.ewma.current_volatility,
            samples=self.ewma.tick_count
        )

    def reset(self) -> None:
        """Reset state for new session."""
        self.ewma.reset()
        self.percentile.reset()
        self.prev_price = None


# =============================================================================
# 7. OU (Ornstein-Uhlenbeck) Threshold
# =============================================================================

class OUThreshold:
    """
    Adaptive spike threshold using Ornstein-Uhlenbeck process model.

    Uses pre-calibrated OU parameters to compute z-scores of current volatility
    against the stationary distribution, then applies sigmoid mapping.

    This is the RECOMMENDED approach for handling regime changes (e.g., OOS2).

    Formula:
        z = (log(vol) - mu) / sigma_stat
        multiplier = k_low + (k_high - k_low) / (1 + exp(-steepness * z))
        threshold = base * multiplier

    Expected behavior:
        z < -1 (LOW vol):     ~0.6x base → 0.012% threshold
        z ≈ 0  (MEDIUM vol):  ~1.0x base → 0.020% threshold
        z > +1 (HIGH vol):    ~1.4x base → 0.028% threshold
        z > +2 (EXTREME vol): ~1.7x base → 0.034% threshold

    Parameters:
        ou_params_file: Path to JSON file with calibrated OU parameters
        base_threshold: Base spike threshold (default 0.02%)
        k_low: Multiplier for low volatility (default 0.5)
        k_high: Multiplier for high volatility (default 1.75)
        sigmoid_steepness: Steepness of sigmoid transition (default 1.5)
        ewma_halflife: Half-life for EWMA volatility (default 300 ticks)
        min_threshold: Minimum threshold (default 0.005%)
        max_threshold: Maximum threshold (default 0.10%)
    """

    def __init__(
        self,
        ou_params_file: Optional[str] = None,
        base_threshold: float = 0.02,
        k_low: float = 0.5,
        k_high: float = 1.75,
        sigmoid_steepness: float = 1.5,
        ewma_halflife: int = 300,
        min_threshold: float = 0.005,
        max_threshold: float = 0.10
    ):
        import math

        self.base_threshold = base_threshold
        self.k_low = k_low
        self.k_high = k_high
        self.steepness = sigmoid_steepness
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold

        # EWMA for volatility
        self.lambda_decay = 0.5 ** (1.0 / ewma_halflife)
        self.prev_price: Optional[float] = None
        self.variance: float = 0.01  # Initial estimate
        self.tick_count: int = 0

        # OU parameters (load from file or use defaults)
        self.ou_mu = -4.0  # Default mean log-volatility
        self.ou_sigma_stat = 1.0  # Default stationary std

        if ou_params_file:
            self._load_params(ou_params_file)

        # Current state
        self.current_vol: float = 0.0
        self.current_z: float = 0.0
        self.current_threshold: float = base_threshold

    def _load_params(self, filepath: str) -> None:
        """Load OU parameters from JSON file."""
        import json
        try:
            with open(filepath, 'r') as f:
                params = json.load(f)
            self.ou_mu = params.get('mu', self.ou_mu)
            self.ou_sigma_stat = params.get('sigma_stat', self.ou_sigma_stat)
            print(f"[OUThreshold] Loaded: μ={self.ou_mu:.4f}, σ_stat={self.ou_sigma_stat:.4f}")
        except Exception as e:
            print(f"[OUThreshold] Warning: Could not load {filepath}: {e}")

    def update(self, price: float) -> float:
        """Update with new price and return current threshold."""
        import math

        self.tick_count += 1

        if self.prev_price is None:
            self.prev_price = price
            return self.base_threshold

        # Compute return
        ret = (price - self.prev_price) / self.prev_price * 100
        self.prev_price = price

        # EWMA variance update
        self.variance = self.lambda_decay * self.variance + (1 - self.lambda_decay) * (ret ** 2)
        self.current_vol = max(math.sqrt(self.variance), 1e-6)

        # Compute z-score
        log_vol = math.log(self.current_vol)
        self.current_z = (log_vol - self.ou_mu) / self.ou_sigma_stat

        # Sigmoid mapping
        z_clamped = max(-10, min(10, self.current_z * self.steepness))
        sigmoid = 1.0 / (1.0 + math.exp(-z_clamped))
        multiplier = self.k_low + (self.k_high - self.k_low) * sigmoid

        # Compute threshold with bounds
        self.current_threshold = self.base_threshold * multiplier
        self.current_threshold = max(self.min_threshold, min(self.current_threshold, self.max_threshold))

        return self.current_threshold

    def get_threshold(self) -> float:
        """Get current threshold without updating."""
        return self.current_threshold

    def get_z_score(self) -> float:
        """Get current z-score."""
        return self.current_z

    def get_volatility(self) -> float:
        """Get current EWMA volatility."""
        return self.current_vol

    def get_state(self) -> ThresholdState:
        """Get current state for monitoring."""
        import math
        regime = self.classify_regime(self.current_z)
        return ThresholdState(
            threshold=self.current_threshold,
            volatility=self.current_vol,
            samples=self.tick_count,
            regime=regime
        )

    def classify_regime(self, z_score: float) -> str:
        """Classify regime from z-score."""
        if z_score < -1.0:
            return "LOW"
        elif z_score < 1.0:
            return "MEDIUM"
        elif z_score < 2.0:
            return "HIGH"
        else:
            return "EXTREME"

    def reset(self) -> None:
        """Reset state for new session."""
        self.prev_price = None
        self.variance = 0.01
        self.tick_count = 0
        self.current_vol = 0.0
        self.current_z = 0.0
        self.current_threshold = self.base_threshold


# =============================================================================
# 8. Adaptive Spike Detector (Main Interface)
# =============================================================================

class AdaptiveSpikeDetector:
    """
    Spike detector with adaptive threshold based on market volatility.

    Replaces fixed 0.02% threshold with volatility-responsive threshold.

    Usage:
        detector = AdaptiveSpikeDetector(threshold_method="hybrid")

        for price in price_stream:
            direction, magnitude, threshold = detector.update(price)
            if direction:
                print(f"Spike {direction}: {magnitude:.4f}%")
    """

    THRESHOLD_METHODS = {
        "ewma": EWMAThreshold,
        "stddev": StdDevThreshold,
        "percentile": PercentileThreshold,
        "atr": ATRThreshold,
        "regime": RegimeThreshold,
        "hybrid": HybridThreshold,
        "ou": OUThreshold,  # OU-based adaptive (recommended for regime changes)
    }

    def __init__(
        self,
        lookback_ticks: int = 3,
        threshold_method: str = "hybrid",
        min_threshold: float = 0.005,
        max_threshold: float = 0.10,
        **method_kwargs
    ):
        """
        Initialize adaptive spike detector.

        Args:
            lookback_ticks: Ticks to look back for spike detection (~50ms at 60Hz for 3 ticks)
            threshold_method: One of "ewma", "stddev", "percentile", "atr", "regime", "hybrid"
            min_threshold: Minimum threshold (percentage)
            max_threshold: Maximum threshold (percentage)
            **method_kwargs: Additional kwargs passed to threshold engine
        """
        self.lookback = lookback_ticks
        self.method_name = threshold_method
        self.price_history: List[float] = []
        self.history_size = 50

        # Initialize threshold engine
        if threshold_method not in self.THRESHOLD_METHODS:
            raise ValueError(f"Unknown method: {threshold_method}. "
                           f"Choose from: {list(self.THRESHOLD_METHODS.keys())}")

        ThresholdClass = self.THRESHOLD_METHODS[threshold_method]

        # Build kwargs with defaults
        kwargs = {
            "min_threshold": min_threshold,
            "max_threshold": max_threshold,
            **method_kwargs
        }

        # Filter kwargs to only those accepted by the class
        import inspect
        sig = inspect.signature(ThresholdClass.__init__)
        valid_params = set(sig.parameters.keys()) - {"self"}
        filtered_kwargs = {k: v for k, v in kwargs.items() if k in valid_params}

        self.threshold_engine = ThresholdClass(**filtered_kwargs)
        self.current_threshold = min_threshold
        self.min_threshold = min_threshold

    def update(self, price: float) -> Tuple[Optional[str], float, float]:
        """
        Check for spike and return (direction, magnitude, threshold).

        Args:
            price: Current BTC price

        Returns:
            Tuple of:
                - direction: "UP", "DOWN", or None if no spike
                - magnitude_pct: Absolute percentage change
                - threshold_pct: Current adaptive threshold
        """
        # Update adaptive threshold
        if isinstance(self.threshold_engine, RegimeThreshold):
            thresh, _ = self.threshold_engine.update(price)
            self.current_threshold = thresh
        else:
            thresh = self.threshold_engine.update(price)
            self.current_threshold = thresh if thresh is not None else self.current_threshold

        # Add to history
        self.price_history.append(price)
        if len(self.price_history) > self.history_size:
            self.price_history = self.price_history[-self.history_size:]

        # Check for spike
        if len(self.price_history) < self.lookback + 1:
            return None, 0, self.current_threshold

        current = self.price_history[-1]
        previous = self.price_history[-self.lookback - 1]

        if previous <= 0:
            return None, 0, self.current_threshold

        change_pct = (current - previous) / previous * 100
        magnitude = abs(change_pct)

        # Compare against adaptive threshold
        if magnitude >= self.current_threshold:
            direction = "UP" if change_pct > 0 else "DOWN"
            return direction, magnitude, self.current_threshold

        return None, 0, self.current_threshold

    def get_state(self) -> Dict:
        """Get current detector state for monitoring."""
        engine_state = self.threshold_engine.get_state()
        return {
            "method": self.method_name,
            "threshold": self.current_threshold,
            "volatility": engine_state.volatility,
            "samples": engine_state.samples,
            "regime": engine_state.regime,
            "lookback": self.lookback
        }

    def reset(self) -> None:
        """Reset state for new session."""
        self.threshold_engine.reset()
        self.price_history = []
        self.current_threshold = self.min_threshold


# =============================================================================
# 8. Threshold Monitor (for production monitoring)
# =============================================================================

class ThresholdMonitor:
    """
    Monitor adaptive threshold behavior for anomaly detection and logging.
    """

    def __init__(
        self,
        alert_callback: Optional[Callable[[str, float, float], None]] = None,
        history_seconds: int = 3600
    ):
        self.alert_callback = alert_callback
        self.history_seconds = history_seconds
        self.threshold_history: List[Dict] = []

    def record(self, threshold: float, price: float, timestamp: float) -> None:
        """Record a threshold observation."""
        self.threshold_history.append({
            "threshold": threshold,
            "price": price,
            "timestamp": timestamp
        })

        # Keep last history_seconds
        cutoff = timestamp - self.history_seconds
        self.threshold_history = [
            h for h in self.threshold_history
            if h["timestamp"] > cutoff
        ]

        # Check for anomalies
        self._check_alerts(threshold)

    def _check_alerts(self, current_threshold: float) -> None:
        """Check for threshold anomalies and fire alerts."""
        if len(self.threshold_history) < 100:
            return

        thresholds = [h["threshold"] for h in self.threshold_history]
        mean_thresh = np.mean(thresholds)

        # Alert if threshold is 3x higher or lower than average
        if current_threshold > mean_thresh * 3:
            if self.alert_callback:
                self.alert_callback("HIGH_THRESHOLD", current_threshold, mean_thresh)
        elif current_threshold < mean_thresh / 3:
            if self.alert_callback:
                self.alert_callback("LOW_THRESHOLD", current_threshold, mean_thresh)

    def get_statistics(self) -> Dict:
        """Get threshold statistics over the monitoring period."""
        if not self.threshold_history:
            return {}

        thresholds = [h["threshold"] for h in self.threshold_history]
        return {
            "mean": float(np.mean(thresholds)),
            "std": float(np.std(thresholds)),
            "min": float(np.min(thresholds)),
            "max": float(np.max(thresholds)),
            "current": thresholds[-1] if thresholds else 0,
            "samples": len(thresholds)
        }


# =============================================================================
# Convenience factory function
# =============================================================================

def create_adaptive_threshold(
    method: str = "hybrid",
    min_threshold: float = 0.005,
    max_threshold: float = 0.10,
    **kwargs
) -> "AdaptiveSpikeDetector":
    """
    Factory function to create an adaptive spike detector.

    Args:
        method: Threshold method ("ewma", "stddev", "percentile", "atr", "regime", "hybrid")
        min_threshold: Minimum threshold percentage
        max_threshold: Maximum threshold percentage
        **kwargs: Additional method-specific parameters

    Returns:
        Configured AdaptiveSpikeDetector instance

    Example:
        detector = create_adaptive_threshold(
            method="hybrid",
            min_threshold=0.005,
            max_threshold=0.10,
            ewma_half_life=1800,
            ewma_k=2.5
        )
    """
    return AdaptiveSpikeDetector(
        threshold_method=method,
        min_threshold=min_threshold,
        max_threshold=max_threshold,
        **kwargs
    )


# =============================================================================
# Example usage and testing
# =============================================================================

if __name__ == "__main__":
    import time

    print("=" * 60)
    print("Adaptive Spike Threshold - Demo")
    print("=" * 60)

    # Simulate 60Hz price data with varying volatility
    np.random.seed(42)

    # Generate synthetic price data
    n_ticks = 10000  # ~167 seconds at 60Hz
    base_price = 95000.0

    # Create volatility regimes
    prices = [base_price]
    for i in range(1, n_ticks):
        # Vary volatility over time
        if i < 3000:
            vol = 0.0005  # Low volatility: 0.0005% per tick
        elif i < 6000:
            vol = 0.002   # Normal volatility
        else:
            vol = 0.005   # High volatility

        # Generate return
        ret = np.random.normal(0, vol)
        new_price = prices[-1] * (1 + ret)
        prices.append(new_price)

    print(f"\nGenerated {n_ticks} synthetic price ticks")
    print(f"Price range: ${min(prices):,.2f} - ${max(prices):,.2f}")

    # Test each method
    methods = ["ewma", "stddev", "percentile", "hybrid"]

    for method in methods:
        print(f"\n{'=' * 40}")
        print(f"Testing {method.upper()} threshold")
        print("=" * 40)

        detector = AdaptiveSpikeDetector(
            threshold_method=method,
            lookback_ticks=3,
            min_threshold=0.005,
            max_threshold=0.10
        )

        spikes_detected = 0
        thresholds = []

        for price in prices:
            direction, magnitude, threshold = detector.update(price)
            thresholds.append(threshold)
            if direction:
                spikes_detected += 1

        state = detector.get_state()
        print(f"Spikes detected: {spikes_detected}")
        print(f"Final threshold: {state['threshold']:.4f}%")
        print(f"Final volatility: {state['volatility']:.4f}")
        print(f"Threshold range: {min(thresholds):.4f}% - {max(thresholds):.4f}%")

        # Show threshold evolution
        print(f"\nThreshold at regime changes:")
        print(f"  Low vol (t=1000):  {thresholds[1000]:.4f}%")
        print(f"  Med vol (t=4000):  {thresholds[4000]:.4f}%")
        print(f"  High vol (t=8000): {thresholds[8000]:.4f}%")
