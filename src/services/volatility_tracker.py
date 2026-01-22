"""
Live Volatility Z-Score Tracker

Computes z-scores incrementally on each BTC price tick for live trading.
Implements the volatility filter that was validated in grid search analysis.

Key Findings from Grid Search (Jan 22, 2026):
- Best z-zone: 0 < z < 1.5 (52% improvement over no filter)
- Best z-score method: EWMA (adaptive)
- Skip both low volatility (z < 0) AND high volatility (z > 1.5)

Usage:
    tracker = LiveZScoreTracker(method="ewma")

    # On each Binance price tick
    zscore = tracker.update(btc_price)

    if not tracker.should_trade(z_lo=0.0, z_hi=1.5):
        return  # Skip trade - outside optimal volatility zone

Author: Claude Code
Date: January 22, 2026
Based on: research/volatility_filter_analysis.py findings
"""

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS (from grid search findings)
# =============================================================================

# Default z-score bounds (best from grid search)
DEFAULT_Z_LO = 0.0
DEFAULT_Z_HI = 1.5

# OU parameters path
OU_PARAMS_PATH = "research/ou_params.json"

# EWMA parameters (matched to backtest)
EWMA_VOL_WINDOW = 60      # Window for volatility EWMA (samples)
EWMA_ZSCORE_WINDOW = 300  # Window for z-score normalization (samples)

# EWMA Ratio parameters
EWMA_RATIO_FAST = 60      # Fast EWMA window (samples)
EWMA_RATIO_SLOW = 300     # Slow EWMA window (samples)


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class OUParams:
    """OU parameters for z-score computation (OU method)."""
    mu: float = -3.9845
    theta: float = 0.000125
    xi: float = 0.0502
    sigma_stat: float = 0.3877
    half_life_sec: float = 5527.0
    n_samples: int = 0
    dt_seconds: float = 60.0
    estimation_timestamp: float = 0.0

    @classmethod
    def load(cls, filepath: str = OU_PARAMS_PATH) -> "OUParams":
        """Load OU parameters from JSON file."""
        path = Path(filepath)
        if not path.exists():
            # Try absolute path
            path = Path("/Users/rananjaybika/polymarket-amm-bot") / filepath

        if not path.exists():
            logger.warning(f"OU params file not found at {filepath}, using defaults")
            return cls()

        with open(path, 'r') as f:
            data = json.load(f)
        return cls(**data)


# =============================================================================
# LIVE Z-SCORE TRACKER
# =============================================================================

class LiveZScoreTracker:
    """
    Compute z-score incrementally on each BTC price tick.

    Supports multiple z-score methods matching the backtest analysis:
    - "ewma": Fully adaptive EWMA z-score (recommended - best $/hr)
    - "ou": Static OU parameters (best win rate)
    - "ewma_ratio": Fast/slow volatility ratio z-score

    All methods run in O(1) time per tick with no historical data needed.
    """

    def __init__(
        self,
        method: str = "ewma",
        ou_params: Optional[OUParams] = None,
        vol_window: int = EWMA_VOL_WINDOW,
        zscore_window: int = EWMA_ZSCORE_WINDOW,
        z_lo: float = DEFAULT_Z_LO,
        z_hi: float = DEFAULT_Z_HI,
    ):
        """
        Initialize the z-score tracker.

        Args:
            method: Z-score computation method ("ewma", "ou", "ewma_ratio")
            ou_params: Pre-loaded OU parameters (loads from file if None)
            vol_window: EWMA window for volatility calculation (samples)
            zscore_window: EWMA window for z-score normalization (samples)
            z_lo: Lower z-score bound for trading (default 0.0)
            z_hi: Upper z-score bound for trading (default 1.5)
        """
        self.method = method.lower()
        self.vol_window = vol_window
        self.zscore_window = zscore_window
        self.z_lo = z_lo
        self.z_hi = z_hi

        # OU parameters (for "ou" method)
        self.ou_params = ou_params if ou_params else OUParams.load()

        # EWMA alpha values (for exponential moving average)
        self.vol_alpha = 2.0 / (vol_window + 1)
        self.zscore_alpha = 2.0 / (zscore_window + 1)

        # State variables
        self._reset_state()

        logger.info(
            f"[ZSCORE] Initialized: method={method}, z_bounds=[{z_lo}, {z_hi}], "
            f"vol_window={vol_window}, zscore_window={zscore_window}"
        )

    def _reset_state(self) -> None:
        """Reset all internal state variables."""
        # Price tracking
        self.last_price: Optional[float] = None
        self.tick_count: int = 0

        # Volatility EWMA state
        self.ewma_variance: Optional[float] = None

        # Z-score normalization EWMA state (for "ewma" and "ewma_ratio" methods)
        self.zscore_ewma_mean: float = 0.0
        self.zscore_ewma_var: float = 0.1  # Initial variance estimate

        # EWMA Ratio state
        self.fast_vol: Optional[float] = None
        self.slow_vol: Optional[float] = None

        # Current values
        self.current_zscore: float = 0.0
        self.current_volatility: float = 0.0
        self.current_log_vol: float = -4.0  # log(~0.02) as initial guess
        self.current_regime: str = "UNKNOWN"

    def update(self, price: float) -> float:
        """
        Update with new price tick and return current z-score.

        This method runs in O(1) time per tick.

        Args:
            price: Current BTC price

        Returns:
            Current z-score value
        """
        self.tick_count += 1

        # First tick - initialize and return neutral z-score
        if self.last_price is None:
            self.last_price = price
            return 0.0

        # Calculate percentage return
        ret_pct = (price - self.last_price) / self.last_price * 100
        self.last_price = price

        # Filter extreme returns (likely data gaps)
        if abs(ret_pct) > 5.0:
            ret_pct = 0.0

        ret_sq = ret_pct ** 2

        # Update volatility EWMA
        if self.ewma_variance is None:
            self.ewma_variance = ret_sq
        else:
            self.ewma_variance = self.vol_alpha * ret_sq + (1 - self.vol_alpha) * self.ewma_variance

        # Current volatility (std of returns)
        self.current_volatility = math.sqrt(max(self.ewma_variance, 1e-10))
        self.current_log_vol = math.log(max(self.current_volatility, 1e-10))

        # Compute z-score based on method
        if self.method == "ou":
            self.current_zscore = self._compute_ou_zscore()
        elif self.method == "ewma":
            self.current_zscore = self._compute_ewma_zscore()
        elif self.method == "ewma_ratio":
            self.current_zscore = self._compute_ewma_ratio_zscore(ret_sq)
        else:
            raise ValueError(f"Unknown z-score method: {self.method}")

        # Update regime classification
        self.current_regime = self._classify_regime()

        return self.current_zscore

    def _compute_ou_zscore(self) -> float:
        """Compute z-score using static OU parameters."""
        return (self.current_log_vol - self.ou_params.mu) / self.ou_params.sigma_stat

    def _compute_ewma_zscore(self) -> float:
        """Compute z-score using adaptive EWMA normalization."""
        lv = self.current_log_vol

        # Warm-up period - use simple difference
        if self.tick_count < self.zscore_window:
            # During warm-up, center around typical log_vol for BTC (~-4)
            return (lv - (-4.0)) / 0.4

        # Update EWMA mean and variance of log_vol
        old_mean = self.zscore_ewma_mean
        self.zscore_ewma_mean = self.zscore_alpha * lv + (1 - self.zscore_alpha) * self.zscore_ewma_mean

        # Update EWMA variance using Welford-like approach
        diff_sq = (lv - self.zscore_ewma_mean) ** 2
        self.zscore_ewma_var = self.zscore_alpha * diff_sq + (1 - self.zscore_alpha) * self.zscore_ewma_var

        # Compute z-score
        ewma_std = math.sqrt(max(self.zscore_ewma_var, 0.0001))
        return (lv - self.zscore_ewma_mean) / ewma_std

    def _compute_ewma_ratio_zscore(self, ret_sq: float) -> float:
        """Compute z-score of fast/slow volatility ratio."""
        # Fast and slow alpha values
        alpha_fast = 2.0 / (EWMA_RATIO_FAST + 1)
        alpha_slow = 2.0 / (EWMA_RATIO_SLOW + 1)

        # Initialize fast/slow vol
        if self.fast_vol is None:
            self.fast_vol = self.current_volatility
            self.slow_vol = self.current_volatility
            return 0.0

        # Update fast and slow volatilities
        self.fast_vol = alpha_fast * self.current_volatility + (1 - alpha_fast) * self.fast_vol
        self.slow_vol = alpha_slow * self.current_volatility + (1 - alpha_slow) * self.slow_vol

        # Compute log ratio
        ratio = self.fast_vol / max(self.slow_vol, 1e-10)
        ratio = max(0.1, min(10.0, ratio))  # Clamp to reasonable range
        log_ratio = math.log(ratio)

        # Warm-up period
        if self.tick_count < self.zscore_window:
            # During warm-up, just return scaled log_ratio
            # log(1) = 0 is neutral, typical range is [-1, 1]
            return log_ratio / 0.3

        # Update EWMA mean and variance of log_ratio
        self.zscore_ewma_mean = self.zscore_alpha * log_ratio + (1 - self.zscore_alpha) * self.zscore_ewma_mean
        diff_sq = (log_ratio - self.zscore_ewma_mean) ** 2
        self.zscore_ewma_var = self.zscore_alpha * diff_sq + (1 - self.zscore_alpha) * self.zscore_ewma_var

        # Compute z-score
        ewma_std = math.sqrt(max(self.zscore_ewma_var, 0.0001))
        return (log_ratio - self.zscore_ewma_mean) / ewma_std

    def _classify_regime(self) -> str:
        """Classify current volatility regime based on z-score."""
        z = self.current_zscore
        if z < 0:
            return "LOW"
        elif z < 1.5:
            return "MEDIUM"
        elif z < 2.5:
            return "HIGH"
        else:
            return "EXTREME"

    def should_trade(self, z_lo: Optional[float] = None, z_hi: Optional[float] = None) -> bool:
        """
        Check if current z-score is within tradeable zone.

        Grid search finding: Best zone is 0 < z < 1.5.
        - Skip low vol (z < 0): Not enough movement for profitable trades
        - Skip high vol (z > 1.5): Direction accuracy drops significantly

        Args:
            z_lo: Lower bound (default: self.z_lo)
            z_hi: Upper bound (default: self.z_hi)

        Returns:
            True if z-score is in tradeable zone
        """
        lo = z_lo if z_lo is not None else self.z_lo
        hi = z_hi if z_hi is not None else self.z_hi
        return lo < self.current_zscore < hi

    def get_regime(self) -> str:
        """Get current volatility regime classification."""
        return self.current_regime

    def get_zscore(self) -> float:
        """Get current z-score."""
        return self.current_zscore

    def get_volatility(self) -> float:
        """Get current volatility (std of returns)."""
        return self.current_volatility

    def get_state(self) -> dict:
        """Get current tracker state for logging/debugging."""
        return {
            "method": self.method,
            "tick_count": self.tick_count,
            "zscore": round(self.current_zscore, 3),
            "volatility": round(self.current_volatility, 6),
            "log_vol": round(self.current_log_vol, 4),
            "regime": self.current_regime,
            "z_bounds": [self.z_lo, self.z_hi],
            "tradeable": self.should_trade(),
        }

    def reset(self) -> None:
        """Reset tracker state (call on new market or session)."""
        self._reset_state()
        logger.info("[ZSCORE] Tracker reset")

    def __repr__(self) -> str:
        return (
            f"LiveZScoreTracker(method={self.method}, z=[{self.z_lo}, {self.z_hi}], "
            f"zscore={self.current_zscore:.3f}, regime={self.current_regime})"
        )


# =============================================================================
# FACTORY FUNCTIONS
# =============================================================================

def create_zscore_tracker(
    method: str = "ewma",
    z_lo: float = DEFAULT_Z_LO,
    z_hi: float = DEFAULT_Z_HI,
) -> LiveZScoreTracker:
    """
    Factory function to create a z-score tracker with recommended settings.

    Grid Search Recommendations (Jan 22, 2026):
    - method="ewma": Best $/hr ($7.14/hr scaled to 50 shares)
    - method="ou": Best win rate (70.7% with OU threshold + OU z-score)
    - z_bounds=[0, 1.5]: Optimal zone (52% improvement over no filter)

    Args:
        method: Z-score method ("ewma", "ou", "ewma_ratio")
        z_lo: Lower z-score bound for trading
        z_hi: Upper z-score bound for trading

    Returns:
        Configured LiveZScoreTracker instance
    """
    return LiveZScoreTracker(
        method=method,
        z_lo=z_lo,
        z_hi=z_hi,
    )


def create_aggressive_tracker() -> LiveZScoreTracker:
    """Create tracker with aggressive settings (max $/hr)."""
    return create_zscore_tracker(method="ewma", z_lo=0.0, z_hi=1.5)


def create_balanced_tracker() -> LiveZScoreTracker:
    """Create tracker with balanced settings (high win rate + good $/hr)."""
    return create_zscore_tracker(method="ou", z_lo=-0.5, z_hi=1.5)


def create_conservative_tracker() -> LiveZScoreTracker:
    """Create tracker with conservative settings (highest win rate)."""
    return create_zscore_tracker(method="ou", z_lo=0.0, z_hi=1.5)


# =============================================================================
# TESTING / VALIDATION
# =============================================================================

def validate_against_backtest(
    btc_prices: list,
    expected_zscores: list,
    method: str = "ewma",
    tolerance: float = 0.1,
) -> Tuple[bool, float]:
    """
    Validate live tracker against precomputed backtest z-scores.

    Args:
        btc_prices: List of BTC prices from backtest
        expected_zscores: Corresponding z-scores from backtest
        method: Z-score method to test
        tolerance: Maximum allowed mean absolute error

    Returns:
        (passed, mae) - Whether validation passed and mean absolute error
    """
    tracker = LiveZScoreTracker(method=method)

    errors = []
    for i, (price, expected) in enumerate(zip(btc_prices, expected_zscores)):
        computed = tracker.update(price)

        # Skip warm-up period
        if i < tracker.zscore_window:
            continue

        error = abs(computed - expected)
        errors.append(error)

    mae = sum(errors) / len(errors) if errors else 0.0
    passed = mae < tolerance

    if not passed:
        logger.warning(f"[ZSCORE] Validation FAILED: MAE={mae:.4f} > tolerance={tolerance:.4f}")
    else:
        logger.info(f"[ZSCORE] Validation PASSED: MAE={mae:.4f}")

    return passed, mae


# =============================================================================
# EXPORTS
# =============================================================================

__all__ = [
    'LiveZScoreTracker',
    'OUParams',
    'create_zscore_tracker',
    'create_aggressive_tracker',
    'create_balanced_tracker',
    'create_conservative_tracker',
    'validate_against_backtest',
    'DEFAULT_Z_LO',
    'DEFAULT_Z_HI',
]
