# Adaptive Spike Detection Thresholds - Mathematical Analysis

**Date:** January 17, 2026
**Purpose:** Comprehensive mathematical framework for volatility-adaptive spike detection
**Context:** 60Hz BTC data, fixed 0.02% threshold fails in low-volatility regimes

---

## Executive Summary

Fixed spike thresholds fail because market volatility varies by 10-50x across different periods:
- **High volatility:** BTC moves 2-5% per hour (0.02% threshold works well)
- **Low volatility:** BTC moves 0.1-0.5% per hour (0.02% threshold generates almost no signals)

This document provides **5 mathematical approaches** for adaptive thresholds, with formulas ready for Python implementation.

---

## 1. Rolling ATR (Average True Range)

### Mathematical Foundation

ATR was developed by J. Welles Wilder for measuring volatility in OHLC data. For tick data, we adapt it using synthetic "candles" from rolling windows.

### Formula for Tick Data

For 60Hz data, we create micro-candles from N-tick windows:

```
For each micro-candle of n ticks:
    High_i = max(prices[i:i+n])
    Low_i = min(prices[i:i+n])
    Close_i = prices[i+n-1]
    Close_{i-1} = previous close

True Range (TR_i) = max(
    High_i - Low_i,                    # Current range
    |High_i - Close_{i-1}|,           # Gap up
    |Low_i - Close_{i-1}|             # Gap down
)

ATR(n_periods) = EMA(TR, n_periods) or SMA(TR, n_periods)
```

### Adaptive Threshold Formula

```
threshold = k * ATR(n) / current_price * 100

Where:
    k = multiplier (sensitivity parameter)
    n = number of periods for ATR calculation
    Result is in percentage terms
```

### Recommended Parameters for 60Hz BTC Data

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Tick aggregation | 60 ticks (1 second) | Creates 1Hz micro-candles |
| ATR periods | 60-300 | 1-5 minutes of data |
| k (multiplier) | 1.5 - 3.0 | 1.5 = more signals, 3.0 = only large moves |

### Python Implementation

```python
import numpy as np
from collections import deque
from typing import Optional, Tuple

class ATRThreshold:
    """
    Adaptive spike threshold using Average True Range.

    For 60Hz tick data, aggregates into 1-second micro-candles.
    """

    def __init__(
        self,
        ticks_per_candle: int = 60,      # 60 ticks = 1 second at 60Hz
        atr_periods: int = 60,            # 60 candles = 60 seconds
        multiplier: float = 2.0,          # k factor
        use_ema: bool = True              # EMA vs SMA
    ):
        self.ticks_per_candle = ticks_per_candle
        self.atr_periods = atr_periods
        self.multiplier = multiplier
        self.use_ema = use_ema

        # State
        self.tick_buffer: deque = deque(maxlen=ticks_per_candle)
        self.tr_values: deque = deque(maxlen=atr_periods)
        self.prev_close: Optional[float] = None
        self.current_atr: float = 0.0
        self.ema_alpha = 2.0 / (atr_periods + 1)  # EMA smoothing factor

    def update(self, price: float) -> Optional[float]:
        """
        Update with new tick and return current threshold.

        Returns:
            Adaptive threshold as percentage, or None if insufficient data
        """
        self.tick_buffer.append(price)

        # Not enough ticks for a candle yet
        if len(self.tick_buffer) < self.ticks_per_candle:
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
        self.tick_buffer.clear()  # Reset for next candle

        # Need at least 2 TR values
        if len(self.tr_values) < 2:
            return None

        # Calculate ATR
        if self.use_ema:
            if self.current_atr == 0:
                self.current_atr = np.mean(self.tr_values)
            else:
                self.current_atr = (
                    self.ema_alpha * tr +
                    (1 - self.ema_alpha) * self.current_atr
                )
        else:
            self.current_atr = np.mean(self.tr_values)

        # Convert to percentage threshold
        threshold_pct = (self.multiplier * self.current_atr / price) * 100
        return threshold_pct

    def get_threshold(self, current_price: float) -> float:
        """Get current threshold without updating state."""
        if self.current_atr == 0:
            return 0.02  # Default fallback
        return (self.multiplier * self.current_atr / current_price) * 100
```

### Pros and Cons

| Pros | Cons |
|------|------|
| Captures gaps and true volatility | Requires candle aggregation (complexity) |
| Well-established method | Slower to adapt (needs candle formation) |
| Handles overnight gaps | More state to maintain |
| Works well for larger timeframes | May miss very fast micro-moves |

---

## 2. Rolling Standard Deviation

### Mathematical Foundation

Standard deviation of returns captures the typical "noise" in price movements. Spikes are moves significantly larger than this noise.

### Formula

```
returns[i] = (price[i] - price[i-1]) / price[i-1] * 100  # Percentage return

threshold = mean(|returns|, window) + k * std(returns, window)

Or simply:
threshold = k * std(returns, window)
```

### Alternative: Return-Based Threshold

```
For directional thresholds (which you need for spike direction):

threshold_up = mean(returns) + k * std(returns)
threshold_down = mean(returns) - k * std(returns)

Spike UP if: current_return > threshold_up
Spike DOWN if: current_return < threshold_down
```

### Window Size Calculation for 60Hz Data

```
At 60Hz: 60 ticks = 1 second

Desired lookback | Window size (ticks)
----------------|--------------------
10 seconds      | 600
30 seconds      | 1,800
1 minute        | 3,600
5 minutes       | 18,000
```

### Recommended Parameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Window (ticks) | 1800-3600 | 30-60 seconds at 60Hz |
| k (sigma) | 2.0-3.0 | 2σ = 95%, 3σ = 99.7% of normal moves |
| Use absolute returns | Yes | For threshold magnitude |

### Python Implementation

```python
import numpy as np
from collections import deque
from typing import Optional, Tuple

class StdDevThreshold:
    """
    Adaptive spike threshold using rolling standard deviation of returns.

    For 60Hz data, uses rolling window of tick-to-tick returns.
    """

    def __init__(
        self,
        window_ticks: int = 1800,    # 30 seconds at 60Hz
        k_sigma: float = 2.5,         # Number of standard deviations
        min_threshold: float = 0.005, # Floor (0.005%)
        max_threshold: float = 0.2    # Ceiling (0.2%)
    ):
        self.window_ticks = window_ticks
        self.k_sigma = k_sigma
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold

        # State
        self.returns: deque = deque(maxlen=window_ticks)
        self.prev_price: Optional[float] = None

        # Running statistics for efficiency
        self._sum: float = 0.0
        self._sum_sq: float = 0.0

    def update(self, price: float) -> Optional[float]:
        """
        Update with new tick and return current threshold.

        Returns:
            Adaptive threshold as percentage, or None if insufficient data
        """
        if self.prev_price is None:
            self.prev_price = price
            return None

        # Calculate return
        ret = (price - self.prev_price) / self.prev_price * 100
        self.prev_price = price

        # Update running sums (O(1) instead of O(n))
        if len(self.returns) == self.window_ticks:
            old_ret = self.returns[0]
            self._sum -= old_ret
            self._sum_sq -= old_ret ** 2

        self.returns.append(ret)
        self._sum += ret
        self._sum_sq += ret ** 2

        # Need minimum samples
        n = len(self.returns)
        if n < 100:  # Minimum ~1.7 seconds at 60Hz
            return None

        # Calculate statistics
        mean = self._sum / n
        variance = (self._sum_sq / n) - (mean ** 2)
        std = np.sqrt(max(variance, 0))  # Protect against numerical issues

        # Threshold formula
        threshold = abs(mean) + self.k_sigma * std

        # Apply bounds
        threshold = max(self.min_threshold, min(threshold, self.max_threshold))

        return threshold

    def get_stats(self) -> dict:
        """Get current statistics for debugging/monitoring."""
        n = len(self.returns)
        if n < 2:
            return {"mean": 0, "std": 0, "threshold": self.min_threshold}

        mean = self._sum / n
        variance = (self._sum_sq / n) - (mean ** 2)
        std = np.sqrt(max(variance, 0))

        return {
            "mean": mean,
            "std": std,
            "threshold": abs(mean) + self.k_sigma * std,
            "samples": n
        }
```

### Pros and Cons

| Pros | Cons |
|------|------|
| Simple mathematics | Assumes normal distribution (returns are fat-tailed) |
| Fast O(1) updates possible | Sensitive to window size choice |
| Directly measures tick noise | Outliers can skew the std |
| Easy to understand/debug | May miss regime changes quickly |

---

## 3. Percentile-Based Threshold

### Mathematical Foundation

Instead of assuming normal distribution, use empirical percentiles to capture "unusual" moves relative to recent history.

### Formula

```
threshold = percentile(|returns|, p)

Where:
    |returns| = absolute values of recent returns
    p = percentile (e.g., 95 or 99)

A spike is detected when:
    |current_return| > threshold
```

### Percentile Selection

| Percentile | Meaning | Spike Frequency |
|------------|---------|-----------------|
| 90th | Top 10% of moves | ~6 spikes/minute at 60Hz |
| 95th | Top 5% of moves | ~3 spikes/minute at 60Hz |
| 99th | Top 1% of moves | ~36 spikes/hour at 60Hz |
| 99.5th | Top 0.5% of moves | ~18 spikes/hour at 60Hz |

### Recommended Parameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Window (ticks) | 3600-7200 | 1-2 minutes history |
| Percentile | 95-99 | Balance signals vs noise |
| Lookback for spike | 3-5 ticks | ~50-80ms detection |

### Python Implementation

```python
import numpy as np
from collections import deque
from typing import Optional

class PercentileThreshold:
    """
    Adaptive spike threshold using rolling percentile of absolute returns.

    Non-parametric: makes no distributional assumptions.
    """

    def __init__(
        self,
        window_ticks: int = 3600,     # 1 minute at 60Hz
        percentile: float = 95.0,      # 95th percentile
        min_threshold: float = 0.005,  # Floor
        max_threshold: float = 0.2,    # Ceiling
        update_frequency: int = 60     # Recalculate every N ticks
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

    def get_percentile_distribution(self) -> dict:
        """Get distribution statistics for analysis."""
        if len(self.abs_returns) < 10:
            return {}

        arr = np.array(self.abs_returns)
        return {
            "p50": np.percentile(arr, 50),
            "p75": np.percentile(arr, 75),
            "p90": np.percentile(arr, 90),
            "p95": np.percentile(arr, 95),
            "p99": np.percentile(arr, 99),
            "max": np.max(arr),
            "current_threshold": self.current_threshold
        }
```

### Efficient Approximation: T-Digest

For very high-frequency updates, consider the t-digest algorithm:

```python
# Using tdigest library for O(1) approximate percentiles
from tdigest import TDigest

class TDigestThreshold:
    def __init__(self, percentile: float = 95.0, compression: int = 100):
        self.percentile = percentile
        self.digest = TDigest(compression)
        self.prev_price = None

    def update(self, price: float) -> float:
        if self.prev_price is not None:
            ret = abs((price - self.prev_price) / self.prev_price * 100)
            self.digest.update(ret)
        self.prev_price = price
        return self.digest.percentile(self.percentile)
```

### Pros and Cons

| Pros | Cons |
|------|------|
| No distributional assumptions | O(n log n) to calculate exactly |
| Handles fat tails naturally | Requires approximation for speed |
| Intuitive interpretation | Window size critically important |
| Robust to outliers | Memory for full window |

---

## 4. EWMA (Exponential Weighted Moving Average) Volatility

### Mathematical Foundation

EWMA gives more weight to recent observations, allowing faster adaptation to changing volatility regimes.

### Core Formulas

```
EWMA Volatility (σ):
σ²_t = λ * σ²_{t-1} + (1 - λ) * r²_t

Where:
    r_t = return at time t
    λ = decay factor (0 < λ < 1), typically 0.94-0.99

Half-life relationship:
    λ = 0.5^(1/half_life)
    half_life = log(0.5) / log(λ)
```

### Threshold Formula

```
threshold = k * σ_t

Where:
    k = multiplier (typically 2-3)
    σ_t = sqrt(σ²_t) = current EWMA volatility estimate
```

### Parameter Selection for 60Hz Data

| Parameter | Formula | 60Hz Value |
|-----------|---------|------------|
| λ for 10s half-life | 0.5^(1/600) | 0.99885 |
| λ for 30s half-life | 0.5^(1/1800) | 0.99962 |
| λ for 1min half-life | 0.5^(1/3600) | 0.99981 |
| k (multiplier) | - | 2.0-3.0 |

### Python Implementation

```python
import numpy as np
from typing import Optional

class EWMAThreshold:
    """
    Adaptive spike threshold using Exponential Weighted Moving Average volatility.

    EWMA reacts quickly to regime changes while smoothing noise.
    """

    def __init__(
        self,
        half_life_ticks: int = 1800,  # 30 seconds at 60Hz
        k_multiplier: float = 2.5,
        min_threshold: float = 0.005,
        max_threshold: float = 0.2,
        initial_vol: float = 0.01     # Initial volatility estimate (%)
    ):
        # Calculate lambda from half-life
        self.lambda_decay = 0.5 ** (1.0 / half_life_ticks)
        self.k = k_multiplier
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold

        # State
        self.prev_price: Optional[float] = None
        self.variance: float = initial_vol ** 2
        self.tick_count: int = 0

    def update(self, price: float) -> Optional[float]:
        """
        Update with new tick and return current threshold.

        O(1) time and space complexity.
        """
        if self.prev_price is None:
            self.prev_price = price
            return self.min_threshold

        # Calculate return
        ret = (price - self.prev_price) / self.prev_price * 100
        self.prev_price = price
        self.tick_count += 1

        # EWMA variance update
        # σ²_t = λ * σ²_{t-1} + (1 - λ) * r²_t
        self.variance = (
            self.lambda_decay * self.variance +
            (1 - self.lambda_decay) * (ret ** 2)
        )

        # Threshold from volatility
        vol = np.sqrt(self.variance)
        threshold = self.k * vol

        # Apply bounds
        threshold = max(self.min_threshold, min(threshold, self.max_threshold))

        return threshold

    @property
    def current_volatility(self) -> float:
        """Current volatility estimate in percentage."""
        return np.sqrt(self.variance)

    def get_effective_window(self) -> int:
        """Get effective window size (99% of weight)."""
        # Weight of observation t periods ago: λ^t
        # For 99% of weight: 1 - λ^t = 0.99
        # t = log(0.01) / log(λ)
        return int(np.log(0.01) / np.log(self.lambda_decay))
```

### GARCH Extension (Optional)

For even better volatility modeling, consider GARCH(1,1):

```python
class GARCHThreshold:
    """
    GARCH(1,1) volatility model for adaptive thresholds.

    σ²_t = ω + α * r²_{t-1} + β * σ²_{t-1}

    More flexible than EWMA (which is GARCH with ω=0, α+β=1).
    """

    def __init__(
        self,
        omega: float = 0.00001,  # Long-run variance weight
        alpha: float = 0.05,      # Return impact
        beta: float = 0.94,       # Persistence
        k_multiplier: float = 2.5
    ):
        self.omega = omega
        self.alpha = alpha
        self.beta = beta
        self.k = k_multiplier

        self.prev_price = None
        self.variance = omega / (1 - alpha - beta)  # Unconditional variance

    def update(self, price: float) -> float:
        if self.prev_price is None:
            self.prev_price = price
            return 0.02  # Default

        ret = (price - self.prev_price) / self.prev_price * 100
        self.prev_price = price

        # GARCH update
        self.variance = (
            self.omega +
            self.alpha * (ret ** 2) +
            self.beta * self.variance
        )

        return self.k * np.sqrt(self.variance)
```

### Pros and Cons

| Pros | Cons |
|------|------|
| O(1) time and space | Single decay parameter may not fit all regimes |
| Fast adaptation to regime changes | Initial volatility estimate matters |
| Well-established (RiskMetrics) | May overreact to single large move |
| Smooth threshold evolution | Requires tuning of half-life |

---

## 5. Regime Detection with Fixed Thresholds

### Mathematical Foundation

Instead of continuous adaptation, detect discrete volatility regimes and apply fixed thresholds per regime.

### Regime Classification

```
Regime = classify(recent_volatility)

Where:
    CALM:    vol < vol_threshold_low
    NORMAL:  vol_threshold_low <= vol < vol_threshold_high
    SPIKE:   vol >= vol_threshold_high
```

### Hidden Markov Model Approach

```
States: S = {LOW_VOL, MED_VOL, HIGH_VOL}

Transition Matrix P:
         LOW    MED    HIGH
LOW   [  0.95   0.04   0.01  ]
MED   [  0.10   0.85   0.05  ]
HIGH  [  0.05   0.15   0.80  ]

Emission: P(return | state) ~ N(0, σ_state)
    σ_LOW = 0.005%
    σ_MED = 0.015%
    σ_HIGH = 0.05%
```

### Simple Implementation: Moving Average Crossover

```python
class RegimeThreshold:
    """
    Regime-based adaptive threshold using volatility state detection.

    Uses fast/slow volatility crossover to detect regime changes.
    """

    # Regime thresholds (percentage moves)
    THRESHOLDS = {
        "CALM": 0.005,    # Very tight - only large moves
        "NORMAL": 0.015,  # Standard threshold
        "ACTIVE": 0.025,  # Elevated volatility
        "SPIKE": 0.05     # High volatility regime
    }

    def __init__(
        self,
        fast_window: int = 300,    # 5 seconds at 60Hz
        slow_window: int = 3600,   # 1 minute at 60Hz
        regime_bounds: tuple = (0.5, 1.5)  # Fast/slow ratio bounds
    ):
        from collections import deque

        self.fast_window = fast_window
        self.slow_window = slow_window
        self.low_ratio, self.high_ratio = regime_bounds

        # State
        self.returns: deque = deque(maxlen=slow_window)
        self.prev_price: Optional[float] = None
        self.current_regime: str = "NORMAL"

    def _calculate_vol(self, returns, window: int) -> float:
        """Calculate rolling volatility."""
        if len(returns) < window:
            return 0.0
        recent = list(returns)[-window:]
        return np.std(recent) if len(recent) > 1 else 0.0

    def update(self, price: float) -> tuple:
        """
        Update and return (threshold, regime).
        """
        if self.prev_price is None:
            self.prev_price = price
            return self.THRESHOLDS["NORMAL"], "NORMAL"

        ret = (price - self.prev_price) / self.prev_price * 100
        self.prev_price = price
        self.returns.append(ret)

        # Need enough data
        if len(self.returns) < self.fast_window:
            return self.THRESHOLDS["NORMAL"], "NORMAL"

        # Calculate fast and slow volatility
        fast_vol = self._calculate_vol(self.returns, self.fast_window)
        slow_vol = self._calculate_vol(self.returns, self.slow_window)

        # Determine regime from ratio
        if slow_vol == 0:
            ratio = 1.0
        else:
            ratio = fast_vol / slow_vol

        # Classify regime
        if ratio < self.low_ratio:
            self.current_regime = "CALM"
        elif ratio > self.high_ratio * 2:
            self.current_regime = "SPIKE"
        elif ratio > self.high_ratio:
            self.current_regime = "ACTIVE"
        else:
            self.current_regime = "NORMAL"

        return self.THRESHOLDS[self.current_regime], self.current_regime
```

### Advanced: Markov Switching Model

```python
class MarkovRegimeThreshold:
    """
    Hidden Markov Model for regime detection with fixed thresholds per state.

    Uses Baum-Welch for online learning (optional) or fixed parameters.
    """

    def __init__(
        self,
        n_states: int = 3,
        state_thresholds: tuple = (0.008, 0.02, 0.05),
        state_volatilities: tuple = (0.003, 0.01, 0.03),
        transition_persistence: float = 0.95
    ):
        self.n_states = n_states
        self.thresholds = np.array(state_thresholds)
        self.state_vols = np.array(state_volatilities)

        # Transition matrix (high persistence)
        p = transition_persistence
        self.trans_matrix = np.array([
            [p, (1-p)/2, (1-p)/2],
            [(1-p)/2, p, (1-p)/2],
            [(1-p)/2, (1-p)/2, p]
        ])

        # State probabilities
        self.state_probs = np.array([0.5, 0.4, 0.1])  # Prior: mostly calm
        self.prev_price = None

    def update(self, price: float) -> tuple:
        """
        Update state probabilities and return (threshold, most_likely_state).
        """
        if self.prev_price is None:
            self.prev_price = price
            return self.thresholds[0], 0

        ret = abs((price - self.prev_price) / self.prev_price * 100)
        self.prev_price = price

        # Emission probabilities: P(return | state)
        # Using Gaussian likelihood
        emissions = np.exp(-0.5 * (ret / self.state_vols) ** 2) / self.state_vols
        emissions /= emissions.sum()  # Normalize

        # Forward step: P(state_t | observations)
        # state_probs = P(state_{t-1}) @ trans_matrix * P(obs | state_t)
        self.state_probs = self.state_probs @ self.trans_matrix
        self.state_probs *= emissions
        self.state_probs /= self.state_probs.sum()  # Normalize

        # Most likely state
        current_state = np.argmax(self.state_probs)

        # Weighted threshold (soft assignment)
        # threshold = sum(P(state) * threshold(state))
        threshold = np.dot(self.state_probs, self.thresholds)

        return threshold, current_state
```

### Pros and Cons

| Pros | Cons |
|------|------|
| Discrete regimes are interpretable | Hard transitions may cause flapping |
| Can use optimized thresholds per regime | More parameters to tune |
| Captures regime persistence | Regime classification itself can fail |
| Easy to backtest per regime | May lag actual regime changes |

---

## 6. Comparison and Recommendation

### Summary Table

| Method | Complexity | Adaptation Speed | Memory | Best For |
|--------|------------|------------------|--------|----------|
| ATR | Medium | Slow (1+ second) | O(n) | Larger moves, gaps |
| StdDev | Simple | Medium | O(n) or O(1) | General purpose |
| Percentile | Simple | Medium | O(n) | Fat-tailed data |
| EWMA | Simple | Fast | O(1) | High-frequency |
| Regime | Complex | Slow | O(n) | Multi-modal volatility |

### Recommended Approach for 60Hz BTC Spike Detection

**Primary: EWMA Threshold**

```python
# Recommended configuration for 60Hz BTC data
threshold_engine = EWMAThreshold(
    half_life_ticks=1800,    # 30-second half-life
    k_multiplier=2.5,        # 2.5 sigma equivalent
    min_threshold=0.005,     # Floor: 0.005% (~$5 on $100k BTC)
    max_threshold=0.10,      # Ceiling: 0.1% (~$100 on $100k BTC)
    initial_vol=0.015        # Start with moderate volatility assumption
)
```

**Hybrid Approach (Recommended)**

Combine EWMA for fast adaptation with percentile for robustness:

```python
class HybridThreshold:
    """
    Hybrid adaptive threshold combining EWMA (fast) and Percentile (robust).

    Final threshold = max(EWMA_threshold, Percentile_threshold * scale)

    This prevents threshold from dropping too low during quiet periods.
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
            k_multiplier=ewma_k
        )
        self.percentile = PercentileThreshold(
            window_ticks=percentile_window,
            percentile=percentile_value
        )
        self.percentile_scale = percentile_scale
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold

    def update(self, price: float) -> float:
        ewma_thresh = self.ewma.update(price) or self.min_threshold
        pctl_thresh = self.percentile.update(price) or self.min_threshold

        # Take max of EWMA and scaled percentile
        threshold = max(ewma_thresh, pctl_thresh * self.percentile_scale)

        # Apply bounds
        return max(self.min_threshold, min(threshold, self.max_threshold))

    def get_components(self) -> dict:
        """Get individual component thresholds for monitoring."""
        return {
            "ewma": self.ewma.get_threshold(self.ewma.prev_price or 100000),
            "percentile": self.percentile.current_threshold,
            "final": max(
                self.ewma.get_threshold(self.ewma.prev_price or 100000),
                self.percentile.current_threshold * self.percentile_scale
            )
        }
```

---

## 7. Integration with Spike Detection

### Modified Spike Detector with Adaptive Threshold

```python
class AdaptiveSpikeDetector:
    """
    Spike detector with adaptive threshold based on market volatility.

    Replaces fixed 0.02% threshold with volatility-responsive threshold.
    """

    def __init__(
        self,
        lookback_ticks: int = 3,          # ~50ms at 60Hz
        threshold_method: str = "hybrid",  # "ewma", "percentile", "hybrid"
        min_threshold: float = 0.005,
        max_threshold: float = 0.10
    ):
        self.lookback = lookback_ticks
        self.price_history: list = []
        self.history_size = 50

        # Initialize threshold engine
        if threshold_method == "ewma":
            self.threshold_engine = EWMAThreshold(
                half_life_ticks=1800,
                k_multiplier=2.5,
                min_threshold=min_threshold,
                max_threshold=max_threshold
            )
        elif threshold_method == "percentile":
            self.threshold_engine = PercentileThreshold(
                window_ticks=3600,
                percentile=95.0,
                min_threshold=min_threshold,
                max_threshold=max_threshold
            )
        else:  # hybrid
            self.threshold_engine = HybridThreshold(
                min_threshold=min_threshold,
                max_threshold=max_threshold
            )

        self.current_threshold = min_threshold

    def update(self, price: float) -> tuple:
        """
        Check for spike and return (direction, magnitude, threshold).

        Returns:
            (direction, magnitude_pct, threshold_pct) if spike detected
            (None, 0, threshold_pct) if no spike
        """
        # Update adaptive threshold
        self.current_threshold = self.threshold_engine.update(price) or self.current_threshold

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
```

---

## 8. Practical Calibration Guide

### Step 1: Collect Baseline Data

```python
# Collect 1 hour of 60Hz data
# Calculate distribution of returns
returns = []
for i in range(1, len(prices)):
    ret = (prices[i] - prices[i-1]) / prices[i-1] * 100
    returns.append(ret)

# Analyze distribution
abs_returns = np.abs(returns)
print(f"Mean |return|: {np.mean(abs_returns):.6f}%")
print(f"Std |return|:  {np.std(abs_returns):.6f}%")
print(f"P50: {np.percentile(abs_returns, 50):.6f}%")
print(f"P95: {np.percentile(abs_returns, 95):.6f}%")
print(f"P99: {np.percentile(abs_returns, 99):.6f}%")
```

### Step 2: Backtest Different Thresholds

```python
def backtest_threshold(prices, poly_outcomes, threshold_engine):
    """
    Backtest an adaptive threshold method.

    Returns accuracy and signal count.
    """
    correct = 0
    total = 0

    detector = AdaptiveSpikeDetector(threshold_method=threshold_engine)

    for i, price in enumerate(prices):
        direction, magnitude, threshold = detector.update(price)

        if direction is not None:
            total += 1
            actual_outcome = poly_outcomes[i]
            predicted = 1 if direction == "UP" else 0
            if predicted == actual_outcome:
                correct += 1

    accuracy = correct / total if total > 0 else 0
    signals_per_hour = total / (len(prices) / 60 / 3600)

    return {
        "accuracy": accuracy,
        "total_signals": total,
        "signals_per_hour": signals_per_hour
    }
```

### Step 3: Parameter Grid Search

```python
# Grid search for optimal parameters
from itertools import product

results = []
for half_life in [600, 1200, 1800, 3600]:
    for k_mult in [2.0, 2.5, 3.0, 3.5]:
        for min_thresh in [0.003, 0.005, 0.008]:
            engine = EWMAThreshold(
                half_life_ticks=half_life,
                k_multiplier=k_mult,
                min_threshold=min_thresh
            )
            result = backtest_threshold(prices, outcomes, engine)
            result.update({
                "half_life": half_life,
                "k_mult": k_mult,
                "min_thresh": min_thresh
            })
            results.append(result)

# Find optimal
best = max(results, key=lambda x: x["accuracy"])
print(f"Best: {best}")
```

---

## 9. Monitoring and Alerting

### Key Metrics to Track

```python
class ThresholdMonitor:
    """
    Monitor adaptive threshold behavior for anomaly detection.
    """

    def __init__(self, alert_callback=None):
        self.threshold_history = []
        self.alert_callback = alert_callback

    def record(self, threshold: float, price: float, timestamp: float):
        self.threshold_history.append({
            "threshold": threshold,
            "price": price,
            "timestamp": timestamp
        })

        # Keep last hour
        cutoff = timestamp - 3600
        self.threshold_history = [
            h for h in self.threshold_history
            if h["timestamp"] > cutoff
        ]

        # Check for anomalies
        self._check_alerts(threshold)

    def _check_alerts(self, current_threshold: float):
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
```

---

## 10. References

1. **ATR**: Wilder, J.W. (1978). New Concepts in Technical Trading Systems.
2. **EWMA/RiskMetrics**: J.P. Morgan (1996). RiskMetrics Technical Document.
3. **GARCH**: Bollerslev, T. (1986). Generalized Autoregressive Conditional Heteroskedasticity.
4. **Regime Switching**: Hamilton, J.D. (1989). A New Approach to the Economic Analysis of Nonstationary Time Series.
5. **T-Digest**: Dunning, T. (2019). Computing Extremely Accurate Quantiles Using t-Digests.

---

*Document generated: January 17, 2026*
*For use with Polymarket BTC 15-minute prediction markets*
*60Hz Binance tick data context*
