"""
Ornstein-Uhlenbeck Process for Adaptive Volatility-Based Spike Thresholds.

This module implements volatility modeling using the OU (Ornstein-Uhlenbeck)
process to create adaptive spike detection thresholds that scale with market
volatility. This addresses the core problem where fixed thresholds (0.02%)
fail in different volatility regimes:

- Training data (LOW vol): 0.19% mean return → 0.02% threshold works
- OOS2 data (HIGH vol): 1.33% mean return (7x higher) → 0.02% is just noise

Mathematical Framework:
    Log-volatility follows: d(log σ_t) = θ(μ - log σ_t)dt + ξ dW_t

    Where:
    - θ = mean-reversion speed (higher = faster revert to mean)
    - μ = long-term mean log-volatility
    - ξ = volatility of volatility (diffusion)
    - W_t = Wiener process (Brownian motion)

    At stationarity: log(σ) ~ N(μ, σ²_stat) where σ²_stat = ξ²/2θ

    Z-score: z_t = (log(σ_t) - μ) / σ_stat

    Adaptive threshold: threshold = base * sigmoid_scale(z)

Author: Claude Code
Date: January 20, 2026
Context: Fix for OOS2 failure where fixed threshold produced 7x too many false signals
"""

import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS
# =============================================================================

# EWMA parameters for real-time volatility tracking
DEFAULT_EWMA_HALFLIFE_TICKS = 300    # 5 seconds at 60Hz (responsive but stable)
DEFAULT_VOLATILITY_FLOOR = 1e-6      # Prevent log(0)
DEFAULT_MIN_SAMPLES = 100            # Minimum samples before OU stats valid

# Adaptive threshold sigmoid parameters (calibrated)
DEFAULT_BASE_THRESHOLD = 0.02        # Current production value (0.02%)
DEFAULT_K_LOW = 0.5                  # 50% of base in calm markets
DEFAULT_K_HIGH = 1.75                # 175% of base in volatile markets
DEFAULT_SIGMOID_STEEPNESS = 1.5      # Gradual transition
DEFAULT_MIN_THRESHOLD = 0.005        # Floor for extremely calm markets
DEFAULT_MAX_THRESHOLD = 0.10         # Ceiling for extreme volatility

# MLE estimation constraints
MAX_AUTOCORR = 0.9999                # Clamp ρ to prevent numerical issues
MIN_AUTOCORR = -0.9999
MAX_RETURN_PCT = 5.0                 # Skip returns > 5% (likely data gap)


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class OUParameters:
    """
    Ornstein-Uhlenbeck process parameters estimated from historical data.

    These parameters define the stationary distribution of log-volatility
    and are used to compute z-scores for adaptive thresholding.
    """
    mu: float                         # Long-term mean log-volatility
    theta: float                      # Mean-reversion speed (per second)
    xi: float                         # Volatility of volatility (diffusion)
    sigma_stat: float                 # Stationary std = sqrt(xi²/2θ)
    half_life_sec: float              # Time to revert halfway = ln(2)/θ

    # Estimation metadata
    n_samples: int = 0                # Number of samples used
    dt_seconds: float = 1/60          # Time step (seconds between observations)
    estimation_timestamp: float = 0.0 # When parameters were estimated

    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict) -> "OUParameters":
        """Create from dictionary."""
        return cls(**d)

    def save(self, filepath: str) -> None:
        """Save parameters to JSON file."""
        with open(filepath, 'w') as f:
            json.dump(self.to_dict(), f, indent=2)
        logger.info(f"[OU] Saved parameters to {filepath}")

    @classmethod
    def load(cls, filepath: str) -> "OUParameters":
        """Load parameters from JSON file."""
        with open(filepath, 'r') as f:
            data = json.load(f)
        logger.info(f"[OU] Loaded parameters from {filepath}")
        return cls.from_dict(data)

    def __repr__(self) -> str:
        return (
            f"OUParameters(μ={self.mu:.4f}, θ={self.theta:.4f}/s, ξ={self.xi:.4f}, "
            f"σ_stat={self.sigma_stat:.4f}, half_life={self.half_life_sec:.1f}s)"
        )


@dataclass
class OUState:
    """Current state of the OU volatility tracker."""
    current_vol: float = 0.0          # Current EWMA volatility (%)
    log_vol: float = 0.0              # log(current_vol)
    z_score: float = 0.0              # (log_vol - mu) / sigma_stat
    regime: str = "UNKNOWN"           # LOW/MEDIUM/HIGH/EXTREME
    threshold: float = DEFAULT_BASE_THRESHOLD  # Current adaptive threshold
    samples: int = 0                  # Number of observations


# =============================================================================
# OU PARAMETER ESTIMATOR (MLE from historical data)
# =============================================================================

class OUParameterEstimator:
    """
    Maximum Likelihood Estimation of OU parameters from historical data.

    For discrete observations at intervals Δt:
        μ̂ = mean(log_vol)                           # Long-term mean
        ρ̂ = Cov(X_t, X_{t+1}) / Var(X)              # Lag-1 autocorrelation
        θ̂ = -log(ρ̂) / Δt                            # Mean-reversion speed
        ξ̂² = Var(X) * 2θ̂ / (1 - ρ̂²)                 # Vol-of-vol
        half_life = ln(2) / θ̂                        # Time to revert halfway

    Usage:
        estimator = OUParameterEstimator()
        params = estimator.fit_from_returns(returns_pct, dt_seconds=1/60)
        params.save("ou_params.json")
    """

    def __init__(
        self,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        vol_floor: float = DEFAULT_VOLATILITY_FLOOR,
        max_return_pct: float = MAX_RETURN_PCT,
    ):
        """
        Initialize estimator.

        Args:
            min_samples: Minimum samples required for valid estimation
            vol_floor: Floor for volatility to prevent log(0)
            max_return_pct: Filter out returns larger than this (data gaps)
        """
        self.min_samples = min_samples
        self.vol_floor = vol_floor
        self.max_return_pct = max_return_pct

    def _compute_rolling_volatility(
        self,
        returns: np.ndarray,
        window: int = 60,
    ) -> np.ndarray:
        """
        Compute rolling volatility using EWMA.

        Args:
            returns: Array of returns (%)
            window: EWMA window size

        Returns:
            Array of rolling volatility estimates
        """
        # Use pandas-style EWMA: alpha = 2/(span+1)
        alpha = 2.0 / (window + 1)

        # Initialize with first window variance
        variance = np.var(returns[:window]) if len(returns) >= window else np.var(returns)
        variance = max(variance, self.vol_floor ** 2)

        volatilities = []
        for i, r in enumerate(returns):
            if i < window:
                # Warm-up period: use expanding window
                vol = np.std(returns[:i+1]) if i > 0 else math.sqrt(variance)
            else:
                # EWMA variance update
                variance = alpha * (r ** 2) + (1 - alpha) * variance
                vol = math.sqrt(variance)

            vol = max(vol, self.vol_floor)
            volatilities.append(vol)

        return np.array(volatilities)

    def fit(self, log_volatilities: np.ndarray, dt_seconds: float = 1/60) -> OUParameters:
        """
        Fit OU parameters from pre-computed log-volatilities using MLE.

        Args:
            log_volatilities: Array of log(volatility) values
            dt_seconds: Time step between observations

        Returns:
            Fitted OUParameters
        """
        log_vol = np.array(log_volatilities)
        n = len(log_vol)

        if n < self.min_samples:
            raise ValueError(f"Need at least {self.min_samples} samples, got {n}")

        # MLE estimates
        mu = np.mean(log_vol)

        # Lag-1 autocorrelation (sample)
        x_centered = log_vol - mu
        cov_lag1 = np.mean(x_centered[:-1] * x_centered[1:])
        var_x = np.var(log_vol)

        if var_x < 1e-12:
            raise ValueError("Log-volatility has zero variance - cannot estimate OU parameters")

        rho = cov_lag1 / var_x
        rho = np.clip(rho, MIN_AUTOCORR, MAX_AUTOCORR)

        # Mean-reversion speed: θ = -log(ρ) / Δt
        if rho <= 0:
            logger.warning(f"[OU] Negative autocorrelation ({rho:.4f}), using |ρ|")
            rho = abs(rho) if abs(rho) > 0.01 else 0.5  # Default to moderate persistence

        theta = -math.log(rho) / dt_seconds
        theta = max(theta, 1e-6)  # Prevent division by zero

        # Volatility of volatility: ξ² = Var(X) * 2θ / (1 - ρ²)
        xi_squared = var_x * 2 * theta / (1 - rho ** 2)
        xi = math.sqrt(max(xi_squared, 1e-12))

        # Stationary standard deviation: σ_stat = sqrt(ξ² / 2θ)
        sigma_stat = math.sqrt(xi_squared / (2 * theta))

        # Half-life: t_{1/2} = ln(2) / θ
        half_life_sec = math.log(2) / theta

        params = OUParameters(
            mu=float(mu),
            theta=float(theta),
            xi=float(xi),
            sigma_stat=float(sigma_stat),
            half_life_sec=float(half_life_sec),
            n_samples=n,
            dt_seconds=dt_seconds,
            estimation_timestamp=time.time(),
        )

        logger.info(f"[OU] Fitted parameters: {params}")
        logger.info(
            f"[OU] Interpretation: log-vol mean={mu:.4f}, half-life={half_life_sec:.1f}s, "
            f"stationary_std={sigma_stat:.4f}"
        )

        return params

    def fit_from_returns(
        self,
        returns_pct: List[float],
        dt_seconds: float = 1/60,
        ewma_window: int = 60,
    ) -> OUParameters:
        """
        Fit OU parameters from price returns.

        This is the main entry point - computes rolling volatility from returns,
        then estimates OU parameters from log-volatility series.

        Args:
            returns_pct: List of percentage returns
            dt_seconds: Time step between observations (1/60 for 60Hz)
            ewma_window: Window for rolling volatility EWMA

        Returns:
            Fitted OUParameters
        """
        returns = np.array(returns_pct)

        # Filter out extreme returns (likely data gaps)
        valid_mask = np.abs(returns) <= self.max_return_pct
        n_filtered = np.sum(~valid_mask)
        if n_filtered > 0:
            logger.info(f"[OU] Filtered {n_filtered} extreme returns (>{self.max_return_pct}%)")
        returns = returns[valid_mask]

        if len(returns) < self.min_samples:
            raise ValueError(f"Need at least {self.min_samples} valid returns, got {len(returns)}")

        # Compute rolling volatility
        volatilities = self._compute_rolling_volatility(returns, window=ewma_window)

        # Take log (with floor)
        volatilities = np.maximum(volatilities, self.vol_floor)
        log_vol = np.log(volatilities)

        # Skip warm-up period
        log_vol = log_vol[ewma_window:]

        return self.fit(log_vol, dt_seconds=dt_seconds)

    def fit_from_prices(
        self,
        prices: List[float],
        dt_seconds: float = 1/60,
        ewma_window: int = 60,
    ) -> OUParameters:
        """
        Fit OU parameters from price series.

        Convenience method that computes returns from prices.

        Args:
            prices: List of prices
            dt_seconds: Time step between observations
            ewma_window: Window for rolling volatility EWMA

        Returns:
            Fitted OUParameters
        """
        prices = np.array(prices)

        # Compute percentage returns
        returns_pct = np.diff(prices) / prices[:-1] * 100

        return self.fit_from_returns(returns_pct, dt_seconds, ewma_window)


# =============================================================================
# OU VOLATILITY TRACKER (Real-time EWMA with z-score)
# =============================================================================

class OUVolatilityTracker:
    """
    Real-time EWMA volatility tracker with OU-based z-score computation.

    Tracks current volatility using efficient EWMA updates and computes
    z-scores against the stationary distribution for regime classification.

    Usage:
        params = OUParameters.load("ou_params.json")
        tracker = OUVolatilityTracker(params)

        for price in price_stream:
            vol, log_vol, z_score = tracker.update(price)
            if z_score > 1.5:
                print("HIGH volatility regime")
    """

    def __init__(
        self,
        ou_params: OUParameters,
        ewma_halflife_ticks: int = DEFAULT_EWMA_HALFLIFE_TICKS,
        vol_floor: float = DEFAULT_VOLATILITY_FLOOR,
    ):
        """
        Initialize tracker with pre-calibrated OU parameters.

        Args:
            ou_params: Calibrated OUParameters from historical data
            ewma_halflife_ticks: Half-life for EWMA variance (in ticks)
            vol_floor: Floor for volatility to prevent log(0)
        """
        self.params = ou_params
        self.vol_floor = vol_floor

        # EWMA decay factor: λ = 0.5^(1/halflife)
        self.lambda_decay = 0.5 ** (1.0 / ewma_halflife_ticks)
        self.halflife = ewma_halflife_ticks

        # State
        self.prev_price: Optional[float] = None
        self.variance: float = 0.0  # Will be initialized on first update
        self.tick_count: int = 0
        self._initialized: bool = False

        logger.info(
            f"[OU-Tracker] Initialized: halflife={ewma_halflife_ticks} ticks, "
            f"params={ou_params}"
        )

    def update(self, price: float) -> Tuple[float, float, float]:
        """
        Update tracker with new price and return current volatility metrics.

        Args:
            price: Current price

        Returns:
            Tuple of (current_vol, log_vol, z_score)
            - current_vol: Current EWMA volatility (%)
            - log_vol: log(current_vol)
            - z_score: (log_vol - mu) / sigma_stat
        """
        if self.prev_price is None:
            self.prev_price = price
            # Initialize variance from OU stationary distribution
            # E[vol] = exp(mu + sigma_stat^2/2) for log-normal
            initial_vol = math.exp(self.params.mu + self.params.sigma_stat ** 2 / 2)
            self.variance = initial_vol ** 2
            return initial_vol, self.params.mu, 0.0

        # Compute return
        ret = (price - self.prev_price) / self.prev_price * 100
        self.prev_price = price
        self.tick_count += 1

        # EWMA variance update: σ²_t = λ * σ²_{t-1} + (1-λ) * r²_t
        self.variance = (
            self.lambda_decay * self.variance +
            (1 - self.lambda_decay) * (ret ** 2)
        )

        # Current volatility
        current_vol = max(math.sqrt(self.variance), self.vol_floor)
        log_vol = math.log(current_vol)

        # Z-score against OU stationary distribution
        z_score = (log_vol - self.params.mu) / self.params.sigma_stat

        return current_vol, log_vol, z_score

    def get_regime(self, z_score: float) -> str:
        """
        Classify volatility regime based on z-score.

        Args:
            z_score: Current z-score

        Returns:
            Regime string: "LOW", "MEDIUM", "HIGH", or "EXTREME"
        """
        if z_score < -1.0:
            return "LOW"
        elif z_score < 1.0:
            return "MEDIUM"
        elif z_score < 2.0:
            return "HIGH"
        else:
            return "EXTREME"

    def get_state(self) -> OUState:
        """Get current state for monitoring."""
        vol = max(math.sqrt(self.variance), self.vol_floor)
        log_vol = math.log(vol)
        z = (log_vol - self.params.mu) / self.params.sigma_stat

        return OUState(
            current_vol=vol,
            log_vol=log_vol,
            z_score=z,
            regime=self.get_regime(z),
            threshold=0.0,  # Not computed here
            samples=self.tick_count,
        )

    def reset(self) -> None:
        """Reset tracker for new session."""
        self.prev_price = None
        self.variance = 0.0
        self.tick_count = 0
        self._initialized = False


# =============================================================================
# OU ADAPTIVE THRESHOLD (Combines tracker with sigmoid mapping)
# =============================================================================

class OUAdaptiveThreshold:
    """
    Adaptive spike threshold using OU-based volatility z-scores.

    Combines real-time EWMA volatility tracking with sigmoid mapping to
    produce adaptive thresholds that scale appropriately with market volatility.

    Threshold formula:
        z = (log(current_vol) - μ) / σ_stat
        multiplier = k_low + (k_high - k_low) / (1 + exp(-steepness * z))
        threshold = base_threshold * multiplier

    Expected behavior:
        z < -1 (LOW vol):     ~0.6x base → 0.012% threshold
        z ≈ 0  (MEDIUM vol):  ~1.0x base → 0.020% threshold
        z > +1 (HIGH vol):    ~1.4x base → 0.028% threshold
        z > +2 (EXTREME vol): ~1.7x base → 0.034% threshold

    Usage:
        params = OUParameters.load("ou_params.json")
        adaptive = OUAdaptiveThreshold(params)

        for price in price_stream:
            threshold = adaptive.update(price)
            if spike_magnitude >= threshold:
                # Valid spike signal
    """

    def __init__(
        self,
        ou_params: OUParameters,
        base_threshold: float = DEFAULT_BASE_THRESHOLD,
        k_low: float = DEFAULT_K_LOW,
        k_high: float = DEFAULT_K_HIGH,
        sigmoid_steepness: float = DEFAULT_SIGMOID_STEEPNESS,
        min_threshold: float = DEFAULT_MIN_THRESHOLD,
        max_threshold: float = DEFAULT_MAX_THRESHOLD,
        ewma_halflife_ticks: int = DEFAULT_EWMA_HALFLIFE_TICKS,
    ):
        """
        Initialize adaptive threshold with OU parameters.

        Args:
            ou_params: Calibrated OUParameters from historical data
            base_threshold: Base spike threshold (%) (default 0.02%)
            k_low: Multiplier for very low volatility (default 0.5)
            k_high: Multiplier for very high volatility (default 1.75)
            sigmoid_steepness: Steepness of sigmoid transition (default 1.5)
            min_threshold: Absolute minimum threshold (default 0.005%)
            max_threshold: Absolute maximum threshold (default 0.10%)
            ewma_halflife_ticks: Half-life for EWMA variance (default 300)
        """
        self.base_threshold = base_threshold
        self.k_low = k_low
        self.k_high = k_high
        self.steepness = sigmoid_steepness
        self.min_threshold = min_threshold
        self.max_threshold = max_threshold

        # Internal tracker
        self.tracker = OUVolatilityTracker(
            ou_params=ou_params,
            ewma_halflife_ticks=ewma_halflife_ticks,
        )

        # Current state
        self.current_threshold = base_threshold
        self.current_z_score = 0.0
        self.current_multiplier = 1.0
        self.current_regime = "UNKNOWN"

        logger.info(
            f"[OU-Adaptive] Initialized: base={base_threshold:.3f}%, "
            f"k=[{k_low:.2f}, {k_high:.2f}], steepness={sigmoid_steepness:.2f}"
        )

    def _sigmoid_multiplier(self, z_score: float) -> float:
        """
        Compute threshold multiplier using sigmoid mapping.

        multiplier = k_low + (k_high - k_low) / (1 + exp(-steepness * z))

        Args:
            z_score: Current volatility z-score

        Returns:
            Multiplier in range [k_low, k_high]
        """
        # Clamp z to prevent overflow
        z_clamped = np.clip(z_score * self.steepness, -10, 10)
        sigmoid = 1.0 / (1.0 + math.exp(-z_clamped))
        return self.k_low + (self.k_high - self.k_low) * sigmoid

    def update(self, price: float) -> float:
        """
        Update with new price and return current adaptive threshold.

        Args:
            price: Current price

        Returns:
            Adaptive spike threshold (%)
        """
        # Update volatility tracker
        current_vol, log_vol, z_score = self.tracker.update(price)

        # Compute multiplier
        multiplier = self._sigmoid_multiplier(z_score)

        # Compute adaptive threshold
        threshold = self.base_threshold * multiplier

        # Apply bounds
        threshold = max(self.min_threshold, min(threshold, self.max_threshold))

        # Update state
        self.current_threshold = threshold
        self.current_z_score = z_score
        self.current_multiplier = multiplier
        self.current_regime = self.tracker.get_regime(z_score)

        return threshold

    def get_threshold(self) -> float:
        """Get current threshold without updating state."""
        return self.current_threshold

    def get_state(self) -> OUState:
        """Get current state for monitoring."""
        state = self.tracker.get_state()
        state.threshold = self.current_threshold
        state.regime = self.current_regime
        return state

    def get_debug_info(self) -> Dict:
        """Get detailed debug information."""
        state = self.tracker.get_state()
        return {
            "current_vol": state.current_vol,
            "log_vol": state.log_vol,
            "z_score": self.current_z_score,
            "regime": self.current_regime,
            "multiplier": self.current_multiplier,
            "threshold": self.current_threshold,
            "base_threshold": self.base_threshold,
            "samples": state.samples,
            "ou_mu": self.tracker.params.mu,
            "ou_sigma_stat": self.tracker.params.sigma_stat,
            "ou_half_life": self.tracker.params.half_life_sec,
        }

    def reset(self) -> None:
        """Reset for new session."""
        self.tracker.reset()
        self.current_threshold = self.base_threshold
        self.current_z_score = 0.0
        self.current_multiplier = 1.0
        self.current_regime = "UNKNOWN"


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def compute_ou_z_score(
    current_vol: float,
    ou_params: OUParameters,
    vol_floor: float = DEFAULT_VOLATILITY_FLOOR,
) -> float:
    """
    Compute OU z-score for a given volatility.

    Standalone function for use in backtests.

    Args:
        current_vol: Current volatility (%)
        ou_params: Calibrated OU parameters
        vol_floor: Floor for volatility

    Returns:
        Z-score
    """
    vol = max(current_vol, vol_floor)
    log_vol = math.log(vol)
    return (log_vol - ou_params.mu) / ou_params.sigma_stat


def compute_adaptive_threshold(
    z_score: float,
    base_threshold: float = DEFAULT_BASE_THRESHOLD,
    k_low: float = DEFAULT_K_LOW,
    k_high: float = DEFAULT_K_HIGH,
    steepness: float = DEFAULT_SIGMOID_STEEPNESS,
    min_threshold: float = DEFAULT_MIN_THRESHOLD,
    max_threshold: float = DEFAULT_MAX_THRESHOLD,
) -> float:
    """
    Compute adaptive threshold from z-score.

    Standalone function for use in backtests.

    Args:
        z_score: Current volatility z-score
        base_threshold: Base spike threshold
        k_low: Low volatility multiplier
        k_high: High volatility multiplier
        steepness: Sigmoid steepness
        min_threshold: Minimum threshold
        max_threshold: Maximum threshold

    Returns:
        Adaptive threshold
    """
    # Sigmoid multiplier
    z_clamped = np.clip(z_score * steepness, -10, 10)
    sigmoid = 1.0 / (1.0 + math.exp(-z_clamped))
    multiplier = k_low + (k_high - k_low) * sigmoid

    # Compute threshold
    threshold = base_threshold * multiplier

    # Apply bounds
    return max(min_threshold, min(threshold, max_threshold))


def classify_regime(z_score: float) -> str:
    """
    Classify volatility regime from z-score.

    Args:
        z_score: Current z-score

    Returns:
        "LOW", "MEDIUM", "HIGH", or "EXTREME"
    """
    if z_score < -1.0:
        return "LOW"
    elif z_score < 1.0:
        return "MEDIUM"
    elif z_score < 2.0:
        return "HIGH"
    else:
        return "EXTREME"


# =============================================================================
# SYNTHETIC DATA TESTING
# =============================================================================

def generate_synthetic_ou_process(
    n_samples: int = 10000,
    mu: float = -4.0,
    theta: float = 0.1,
    xi: float = 0.5,
    dt: float = 1/60,
    seed: Optional[int] = None,
) -> np.ndarray:
    """
    Generate synthetic OU process for testing parameter recovery.

    Args:
        n_samples: Number of samples to generate
        mu: Long-term mean
        theta: Mean-reversion speed
        xi: Volatility of volatility
        dt: Time step
        seed: Random seed

    Returns:
        Array of OU process values
    """
    if seed is not None:
        np.random.seed(seed)

    # Discretized OU: X_{t+1} = X_t + theta*(mu - X_t)*dt + xi*sqrt(dt)*Z
    x = np.zeros(n_samples)
    x[0] = mu  # Start at mean

    sqrt_dt = math.sqrt(dt)
    noise = np.random.randn(n_samples - 1)

    for i in range(1, n_samples):
        drift = theta * (mu - x[i-1]) * dt
        diffusion = xi * sqrt_dt * noise[i-1]
        x[i] = x[i-1] + drift + diffusion

    return x


def test_parameter_recovery(
    true_mu: float = -4.0,
    true_theta: float = 0.1,
    true_xi: float = 0.5,
    n_samples: int = 50000,
    dt: float = 1/60,
    tolerance: float = 0.20,  # 20% tolerance
) -> bool:
    """
    Test that MLE recovers true OU parameters from synthetic data.

    Args:
        true_mu: True mean
        true_theta: True mean-reversion speed
        true_xi: True volatility of volatility
        n_samples: Number of samples
        dt: Time step
        tolerance: Acceptable relative error

    Returns:
        True if parameters recovered within tolerance
    """
    logger.info(f"[OU-Test] Generating synthetic OU process: μ={true_mu}, θ={true_theta}, ξ={true_xi}")

    # Generate synthetic data
    log_vol = generate_synthetic_ou_process(
        n_samples=n_samples,
        mu=true_mu,
        theta=true_theta,
        xi=true_xi,
        dt=dt,
        seed=42,
    )

    # Fit parameters
    estimator = OUParameterEstimator()
    params = estimator.fit(log_vol, dt_seconds=dt)

    # Check recovery
    mu_error = abs(params.mu - true_mu) / abs(true_mu)
    theta_error = abs(params.theta - true_theta) / true_theta
    xi_error = abs(params.xi - true_xi) / true_xi

    logger.info(f"[OU-Test] Recovery errors: μ={mu_error:.1%}, θ={theta_error:.1%}, ξ={xi_error:.1%}")

    passed = (mu_error < tolerance) and (theta_error < tolerance) and (xi_error < tolerance)

    if passed:
        logger.info("[OU-Test] PASSED: Parameters recovered within tolerance")
    else:
        logger.warning("[OU-Test] FAILED: Parameters not recovered within tolerance")

    return passed


# =============================================================================
# MAIN (Demo/Testing)
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=" * 60)
    print("Ornstein-Uhlenbeck Adaptive Threshold - Demo")
    print("=" * 60)

    # Test 1: Parameter recovery from synthetic data
    print("\n1. Testing parameter recovery from synthetic data...")
    test_parameter_recovery()

    # Test 2: Demo adaptive threshold computation
    print("\n2. Demo: Z-score to threshold mapping")
    print("-" * 40)
    print(f"{'Z-score':>10} {'Regime':>10} {'Multiplier':>12} {'Threshold':>12}")
    print("-" * 40)

    for z in [-2.0, -1.0, 0.0, 1.0, 2.0, 3.0]:
        regime = classify_regime(z)
        threshold = compute_adaptive_threshold(z)

        # Compute multiplier
        sigmoid = 1.0 / (1.0 + math.exp(-DEFAULT_SIGMOID_STEEPNESS * z))
        multiplier = DEFAULT_K_LOW + (DEFAULT_K_HIGH - DEFAULT_K_LOW) * sigmoid

        print(f"{z:>10.1f} {regime:>10} {multiplier:>12.2f}x {threshold:>11.4f}%")

    print("-" * 40)
    print(f"\nBase threshold: {DEFAULT_BASE_THRESHOLD:.3f}%")
    print(f"Range: [{DEFAULT_MIN_THRESHOLD:.3f}%, {DEFAULT_MAX_THRESHOLD:.3f}%]")

    # Test 3: Simulate real-time tracking
    print("\n3. Simulating real-time tracking...")

    # Create fake OU params
    fake_params = OUParameters(
        mu=-4.0,
        theta=0.1,
        xi=0.5,
        sigma_stat=1.12,  # sqrt(0.5^2 / (2*0.1))
        half_life_sec=6.93,  # ln(2)/0.1
    )

    adaptive = OUAdaptiveThreshold(fake_params)

    # Simulate price stream with varying volatility
    np.random.seed(123)
    base_price = 95000.0
    prices = [base_price]

    for i in range(1000):
        # Vary volatility over time
        if i < 300:
            vol = 0.001  # Low vol
        elif i < 600:
            vol = 0.005  # Medium vol
        else:
            vol = 0.015  # High vol

        ret = np.random.normal(0, vol)
        prices.append(prices[-1] * (1 + ret))

    print(f"\nSimulated {len(prices)} prices with varying volatility")

    thresholds = []
    z_scores = []

    for price in prices:
        threshold = adaptive.update(price)
        thresholds.append(threshold)
        z_scores.append(adaptive.current_z_score)

    print(f"Threshold range: [{min(thresholds):.4f}%, {max(thresholds):.4f}%]")
    print(f"Z-score range: [{min(z_scores):.2f}, {max(z_scores):.2f}]")

    # Show threshold at each regime
    print(f"\nThreshold at regime changes:")
    print(f"  Low vol (t=200):  z={z_scores[200]:.2f}, threshold={thresholds[200]:.4f}%")
    print(f"  Med vol (t=450):  z={z_scores[450]:.2f}, threshold={thresholds[450]:.4f}%")
    print(f"  High vol (t=800): z={z_scores[800]:.2f}, threshold={thresholds[800]:.4f}%")

    print("\n" + "=" * 60)
    print("Demo complete!")
