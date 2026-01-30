#!/usr/bin/env python3
"""
Fixed Cycling Grid Backtest - Unified Script

Re-runs ALL winning configs with FIXED cycling logic (tracks position state, not just time gap).

Configs to run (~146 total):
- Group 1: 60 parameter configs (EWMA z-score, 3 lookbacks, 5 stop types, 2 z-zones, 2 cycling)
- Group 2: 16 strategy configs (Multi, Regime, Velocity, Acceleration)
- Group 3: 70 Kalman configs (7 strategies x 5 zones x 2 cycling)

FIXED CYCLING LOGIC:
- Tracks `in_position` state (bool)
- Blocks new entries until hedge fills completely
- Enforces MIN_CYCLE_GAP_MS after hedge fill timestamp
- Uses actual hedge fill timestamp, not just sample count

Usage:
    python research/fixed_cycling_grid_backtest.py --data is_oos2
    python research/fixed_cycling_grid_backtest.py --data oos34
    python research/fixed_cycling_grid_backtest.py --data all
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Callable
from collections import deque
import sys
import math
import argparse
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

# =============================================================================
# CONSTANTS
# =============================================================================

TARGET_SHARES = 50
MIN_TIME = 60
MIN_RUNTIME_SECS = 300

# Cycling constraint - time gap after hedge fill
MIN_CYCLE_GAP_MS = 1000

# Spike detection (OU method)
SPIKE_LOOKBACK = 72  # Default lookback (can be overridden)
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10

# Hedge pricing
DROP_MULTIPLIER = 0.50
DROP_INTERCEPT = 0.08
TARGET_PAIR_COST = 0.99

# Velocity thresholds
RAW_VELOCITY_THRESHOLD = 0.10
KALMAN_VELOCITY_THRESHOLD = 0.08
KALMAN_ACCEL_THRESHOLD = 0.005

# Kalman filter parameters
KALMAN_PROCESS_VAR = 0.001
KALMAN_MEASURE_VAR = 0.01

# Multi-signal thresholds
ACCEL_THRESHOLD = 0.01
MOMENTUM_THRESHOLD = 0.05

# Regime detection parameters
VELOCITY_VAR_WINDOW = 30
VELOCITY_VAR_THRESHOLD = 0.15
ACCEL_MAGNITUDE_WINDOW = 20
ACCEL_MAGNITUDE_THRESHOLD = 0.02


# =============================================================================
# VELOCITY ZONE CONFIGURATIONS
# =============================================================================

ZONE_CONFIGS = {
    "ALL":     {"min_vel": 0.00, "max_vel": 99.0, "desc": "No velocity filter"},
    "Z2_6":    {"min_vel": 0.05, "max_vel": 99.0, "desc": "Exclude neutral zone"},
    "Z3_6":    {"min_vel": 0.10, "max_vel": 99.0, "desc": "Moderate+ only"},
    "Z4_6":    {"min_vel": 0.30, "max_vel": 99.0, "desc": "Strong+ only"},
    "Z5_6":    {"min_vel": 0.50, "max_vel": 99.0, "desc": "Extreme+ only"},
}


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class TradeResult:
    """Result of a single trade."""
    config_name: str
    market_slug: str
    entry_time_remaining: float
    winner_side: str
    winner_fill_price: float
    loser_fill_price: float
    hedge_type: str  # passive, stoploss, timestop, resolution
    pair_cost: float
    pnl: float
    correct_direction: bool
    velocity_bps: float
    zscore: float
    hedge_fill_ts: int  # Timestamp when hedge filled


@dataclass
class ConfigResult:
    """Result for a single config."""
    config_name: str
    group: str
    trades: int
    total_pnl: float
    hourly_rate: float
    direction_accuracy: float
    trades_per_hour: float
    hedge_breakdown: Dict[str, int] = field(default_factory=dict)


@dataclass
class BacktestConfig:
    """Configuration for a backtest run."""
    name: str
    group: str  # "PARAM", "STRATEGY", "KALMAN"

    # Core params (Group 1)
    lookback_ticks: int = 72
    zscore_method: str = "ewma"  # Always EWMA per plan
    z_lo: float = 0.0
    z_hi: float = 1.5

    # Stop settings
    stop_loss_pct: Optional[float] = None  # e.g., 0.07, 0.12, 0.15
    time_stop_seconds: Optional[float] = None  # e.g., 120, 180

    # Cycling
    enable_cycling: bool = True

    # Zone filter
    zone_config: str = "ALL"

    # Strategy (Group 2/3)
    strategy: str = "BASELINE"  # BASELINE, STACK_1of3, KALMAN_VEL, etc.


# =============================================================================
# OU PARAMETERS
# =============================================================================

_ou_params = None


def load_ou_params():
    """Load OU volatility parameters."""
    global _ou_params
    try:
        from src.strategies.ou_volatility import OUParameters
        _ou_params = OUParameters.load("research/ou_params.json")
        print(f"[OU] Loaded: mu={_ou_params.mu:.4f}, sigma_stat={_ou_params.sigma_stat:.4f}")
    except Exception as e:
        print(f"[OU] Warning: {e} - using defaults")
        _ou_params = None


def compute_ou_threshold(volatility: float) -> float:
    """Compute OU adaptive threshold."""
    global _ou_params
    if _ou_params is None:
        return OU_BASE_THRESHOLD
    vol = max(volatility, 1e-6)
    log_vol = math.log(vol)
    z_score = (log_vol - _ou_params.mu) / _ou_params.sigma_stat
    z_clamped = max(-10, min(10, z_score * OU_SIGMOID_STEEPNESS))
    sigmoid = 1.0 / (1.0 + math.exp(-z_clamped))
    multiplier = OU_K_LOW + (OU_K_HIGH - OU_K_LOW) * sigmoid
    threshold = OU_BASE_THRESHOLD * multiplier
    return max(OU_MIN_THRESHOLD, min(OU_MAX_THRESHOLD, threshold))


# =============================================================================
# KALMAN FILTER
# =============================================================================

class PriceKalmanFilter:
    """Kalman filter for price tracking with velocity and acceleration estimation."""

    def __init__(self, process_var: float = 0.001, measurement_var: float = 0.01):
        self.process_var = process_var
        self.measurement_var = measurement_var
        self.x = np.zeros(3)  # [price, velocity, acceleration]
        self.P = np.eye(3) * 1.0
        self.Q = np.eye(3) * process_var
        self.R = measurement_var
        self.H = np.array([[1.0, 0.0, 0.0]])
        self.initialized = False
        self.last_timestamp = None

    def reset(self, initial_price: float):
        self.x = np.array([initial_price, 0.0, 0.0])
        self.P = np.eye(3) * 1.0
        self.initialized = True
        self.last_timestamp = None

    def predict(self, dt: float):
        F = np.array([
            [1, dt, 0.5 * dt**2],
            [0, 1, dt],
            [0, 0, 1]
        ])
        self.x = F @ self.x
        self.P = F @ self.P @ F.T + self.Q

    def update(self, price: float, timestamp_ms: float = None) -> Tuple[float, float, float, float]:
        if not self.initialized:
            self.reset(price)
            self.last_timestamp = timestamp_ms
            return self.x[0], self.x[1], self.x[2], self.P[1, 1]

        if timestamp_ms is not None and self.last_timestamp is not None:
            dt = (timestamp_ms - self.last_timestamp) / 1000.0
            dt = max(0.001, min(1.0, dt))
        else:
            dt = 0.0167

        self.last_timestamp = timestamp_ms
        self.predict(dt)

        y = price - self.H @ self.x
        S = self.H @ self.P @ self.H.T + self.R
        K = self.P @ self.H.T / S
        self.x = self.x + K.flatten() * y

        I = np.eye(3)
        self.P = (I - K @ self.H) @ self.P

        velocity_bps = (self.x[1] / self.x[0]) * 10000 if self.x[0] > 0 else 0
        accel_bps = (self.x[2] / self.x[0]) * 10000 if self.x[0] > 0 else 0

        return self.x[0], velocity_bps, accel_bps, self.P[1, 1]


# =============================================================================
# REGIME TRACKER
# =============================================================================

class RegimeTracker:
    """Tracks market regime based on various signals."""

    def __init__(self):
        self.velocity_history = deque(maxlen=VELOCITY_VAR_WINDOW)
        self.accel_history = deque(maxlen=ACCEL_MAGNITUDE_WINDOW)

    def update(self, velocity: float, acceleration: float):
        self.velocity_history.append(velocity)
        self.accel_history.append(abs(acceleration))

    def get_zscore_regime(self, zscore: float) -> str:
        if zscore < 0.5:
            return "LOW"
        elif zscore < 1.0:
            return "MEDIUM"
        else:
            return "HIGH"

    def get_velocity_var_regime(self) -> str:
        if len(self.velocity_history) < 10:
            return "UNKNOWN"
        var = np.var(list(self.velocity_history))
        return "VOLATILE" if var > VELOCITY_VAR_THRESHOLD else "CALM"

    def get_accel_regime(self) -> str:
        if len(self.accel_history) < 5:
            return "UNKNOWN"
        avg_accel = np.mean(list(self.accel_history))
        return "SHIFTING" if avg_accel > ACCEL_MAGNITUDE_THRESHOLD else "STABLE"


# =============================================================================
# SPIKE DETECTION
# =============================================================================

def detect_spikes_ou(btc_df: pd.DataFrame, lookback: int = 72) -> pd.DataFrame:
    """Detect spikes using OU adaptive threshold."""
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    df['price_prev'] = df['price'].shift(lookback)
    df['change_pct'] = (df['price'] - df['price_prev']) / df['price_prev'] * 100
    df['magnitude'] = df['change_pct'].abs()

    returns = df['price'].pct_change() * 100
    ewma_halflife = 300
    alpha = 1 - 0.5 ** (1.0 / ewma_halflife)

    variance = returns.iloc[:60].var() if len(returns) > 60 else 0.01
    volatilities = []
    zscores = []

    for i, r in enumerate(returns):
        if pd.isna(r):
            volatilities.append(0.01)
            zscores.append(0.5)
            continue
        variance = alpha * (r ** 2) + (1 - alpha) * variance
        vol = max(np.sqrt(variance), 1e-6)
        volatilities.append(vol)

        if _ou_params:
            log_vol = math.log(vol)
            z = (log_vol - _ou_params.mu) / _ou_params.sigma_stat
            zscores.append(max(0, min(3, z)))
        else:
            zscores.append(0.5)

    df['volatility'] = volatilities
    df['zscore'] = zscores
    df['threshold'] = df['volatility'].apply(compute_ou_threshold)
    df['spike_detected'] = df['magnitude'] >= df['threshold']

    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['change_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['change_pct'] < 0), 'spike_direction'] = 'DOWN'
    df['spike_magnitude'] = df['magnitude'].where(df['spike_detected'], 0)

    return df[['timestamp_ms', 'price', 'spike_detected', 'spike_direction',
               'spike_magnitude', 'threshold', 'zscore']]


# =============================================================================
# CONFIRMATION STRATEGIES
# =============================================================================

def confirm_baseline(spike_dir: str, velocity: float, accel: float = 0,
                     kalman_vel: float = 0, kalman_accel: float = 0,
                     regime_tracker: RegimeTracker = None, zscore: float = 0,
                     estimation_var: float = 0.01, **kwargs) -> bool:
    """BASELINE: Current velocity-only confirmation (accepts neutral zone)."""
    if spike_dir == "UP":
        return velocity > -RAW_VELOCITY_THRESHOLD
    else:
        return velocity < RAW_VELOCITY_THRESHOLD


def confirm_conservative(spike_dir: str, velocity: float, estimation_var: float = 0.01, **kwargs) -> bool:
    """CONSERVATIVE: Require velocity to confirm (reject neutral)."""
    if spike_dir == "UP":
        return velocity >= RAW_VELOCITY_THRESHOLD
    else:
        return velocity <= -RAW_VELOCITY_THRESHOLD


# --- Multi-signal strategies ---

def confirm_stack_1of3(spike_dir: str, velocity: float, accel: float = 0, **kwargs) -> bool:
    """STACK_1of3: Accept if at least 1 of 3 signals confirms."""
    momentum = velocity * 0.8  # Approximation
    confirmations = 0
    if spike_dir == "UP":
        if velocity > 0: confirmations += 1
        if accel > 0: confirmations += 1
        if momentum > 0: confirmations += 1
    else:
        if velocity < 0: confirmations += 1
        if accel < 0: confirmations += 1
        if momentum < 0: confirmations += 1
    return confirmations >= 1


def confirm_stack_2of3(spike_dir: str, velocity: float, accel: float = 0, **kwargs) -> bool:
    """STACK_2of3: Accept if at least 2 of 3 signals confirm."""
    momentum = velocity * 0.8
    confirmations = 0
    if spike_dir == "UP":
        if velocity > 0: confirmations += 1
        if accel > 0: confirmations += 1
        if momentum > 0: confirmations += 1
    else:
        if velocity < 0: confirmations += 1
        if accel < 0: confirmations += 1
        if momentum < 0: confirmations += 1
    return confirmations >= 2


def confirm_stack_3of3(spike_dir: str, velocity: float, accel: float = 0, **kwargs) -> bool:
    """STACK_3of3: Accept if all 3 signals confirm."""
    momentum = velocity * 0.8
    confirmations = 0
    if spike_dir == "UP":
        if velocity > 0: confirmations += 1
        if accel > 0: confirmations += 1
        if momentum > 0: confirmations += 1
    else:
        if velocity < 0: confirmations += 1
        if accel < 0: confirmations += 1
        if momentum < 0: confirmations += 1
    return confirmations >= 3


def confirm_tiered(spike_dir: str, velocity: float, accel: float = 0, **kwargs) -> bool:
    """TIERED: Progressive filtering based on signal strength."""
    abs_vel = abs(velocity)
    if abs_vel >= 0.2:
        # Strong velocity - accept if direction matches
        return (spike_dir == "UP" and velocity > 0) or (spike_dir == "DOWN" and velocity < 0)
    elif abs_vel >= 0.1:
        # Medium velocity - need acceleration
        if spike_dir == "UP":
            return velocity > 0 and accel > 0
        else:
            return velocity < 0 and accel < 0
    else:
        # Weak velocity - need all signals
        return confirm_stack_3of3(spike_dir, velocity, accel)


# --- Regime-adaptive strategies ---

def confirm_adapt_zscore(spike_dir: str, velocity: float, accel: float = 0,
                         regime_tracker: RegimeTracker = None, zscore: float = 0, **kwargs) -> bool:
    """ADAPT_ZSCORE: Different thresholds per z-score regime."""
    if regime_tracker is None:
        regime_tracker = RegimeTracker()
    regime = regime_tracker.get_zscore_regime(zscore)

    thresholds = {"LOW": 0.05, "MEDIUM": 0.10, "HIGH": 0.15}
    threshold = thresholds.get(regime, 0.10)

    if spike_dir == "UP":
        return velocity > -threshold
    else:
        return velocity < threshold


def confirm_adapt_velvar(spike_dir: str, velocity: float, accel: float = 0,
                         regime_tracker: RegimeTracker = None, **kwargs) -> bool:
    """ADAPT_VELVAR: Switch method based on velocity variance regime."""
    if regime_tracker is None:
        return confirm_baseline(spike_dir, velocity)

    regime = regime_tracker.get_velocity_var_regime()
    if regime == "CALM":
        return confirm_baseline(spike_dir, velocity)
    else:
        return confirm_conservative(spike_dir, velocity)


def confirm_adapt_accel(spike_dir: str, velocity: float, accel: float = 0,
                        regime_tracker: RegimeTracker = None, **kwargs) -> bool:
    """ADAPT_ACCEL: Switch method based on acceleration state."""
    if regime_tracker is None:
        return confirm_baseline(spike_dir, velocity)

    regime = regime_tracker.get_accel_regime()
    if regime == "STABLE":
        return confirm_baseline(spike_dir, velocity)
    else:
        # Require acceleration alignment
        if spike_dir == "UP":
            return velocity > -RAW_VELOCITY_THRESHOLD and accel > 0
        else:
            return velocity < RAW_VELOCITY_THRESHOLD and accel < 0


def confirm_adapt_combined(spike_dir: str, velocity: float, accel: float = 0,
                           regime_tracker: RegimeTracker = None, zscore: float = 0, **kwargs) -> bool:
    """ADAPT_COMBINED: Use all regime signals for maximum adaptation."""
    if regime_tracker is None:
        return confirm_baseline(spike_dir, velocity)

    z_regime = regime_tracker.get_zscore_regime(zscore)
    vv_regime = regime_tracker.get_velocity_var_regime()
    ac_regime = regime_tracker.get_accel_regime()

    risk_score = 0
    if z_regime == "HIGH": risk_score += 2
    elif z_regime == "LOW": risk_score -= 1
    if vv_regime == "VOLATILE": risk_score += 1
    elif vv_regime == "CALM": risk_score -= 1
    if ac_regime == "SHIFTING": risk_score += 1

    if risk_score <= -1:
        threshold = 0.05
        require_accel = False
    elif risk_score <= 1:
        threshold = 0.10
        require_accel = False
    else:
        threshold = 0.15
        require_accel = True

    if spike_dir == "UP":
        vel_ok = velocity > -threshold
        accel_ok = accel > 0 if require_accel else True
    else:
        vel_ok = velocity < threshold
        accel_ok = accel < 0 if require_accel else True

    return vel_ok and accel_ok


# --- Acceleration strategies ---

def confirm_vel_or_accel(spike_dir: str, velocity: float, accel: float = 0, **kwargs) -> bool:
    """VEL_OR_ACCEL: Accept if EITHER velocity OR acceleration confirms."""
    if spike_dir == "UP":
        return velocity > RAW_VELOCITY_THRESHOLD or accel > ACCEL_THRESHOLD
    else:
        return velocity < -RAW_VELOCITY_THRESHOLD or accel < -ACCEL_THRESHOLD


def confirm_accel_aligned(spike_dir: str, velocity: float, accel: float = 0, **kwargs) -> bool:
    """ACCEL_ALIGNED: Require velocity AND acceleration same direction."""
    if spike_dir == "UP":
        return velocity > -RAW_VELOCITY_THRESHOLD and accel > 0
    else:
        return velocity < RAW_VELOCITY_THRESHOLD and accel < 0


# --- Kalman strategies ---

def confirm_kalman_vel(spike_dir: str, velocity: float = 0, kalman_vel: float = 0, **kwargs) -> bool:
    """KALMAN_VEL: Use Kalman-filtered velocity."""
    if spike_dir == "UP":
        return kalman_vel > -KALMAN_VELOCITY_THRESHOLD
    else:
        return kalman_vel < KALMAN_VELOCITY_THRESHOLD


def confirm_kalman_accel(spike_dir: str, velocity: float = 0, accel: float = 0,
                         kalman_accel: float = 0, **kwargs) -> bool:
    """KALMAN_ACCEL: Require Kalman acceleration to confirm."""
    if spike_dir == "UP":
        vel_ok = velocity > -RAW_VELOCITY_THRESHOLD
        accel_ok = kalman_accel > KALMAN_ACCEL_THRESHOLD
        return vel_ok and accel_ok
    else:
        vel_ok = velocity < RAW_VELOCITY_THRESHOLD
        accel_ok = kalman_accel < -KALMAN_ACCEL_THRESHOLD
        return vel_ok and accel_ok


def confirm_kalman_both(spike_dir: str, kalman_vel: float = 0, kalman_accel: float = 0, **kwargs) -> bool:
    """KALMAN_BOTH: Use both Kalman velocity and acceleration."""
    if spike_dir == "UP":
        return kalman_vel > -KALMAN_VELOCITY_THRESHOLD and kalman_accel > 0
    else:
        return kalman_vel < KALMAN_VELOCITY_THRESHOLD and kalman_accel < 0


def confirm_kalman_adaptive(spike_dir: str, kalman_vel: float = 0,
                            estimation_var: float = 0.01, **kwargs) -> bool:
    """KALMAN_ADAPTIVE: Adaptive threshold based on estimation variance."""
    var_scale = min(max(estimation_var * 100, 0.5), 2.0)
    adaptive_threshold = KALMAN_VELOCITY_THRESHOLD * var_scale

    if spike_dir == "UP":
        return kalman_vel > -adaptive_threshold
    else:
        return kalman_vel < adaptive_threshold


def confirm_kalman_strict(spike_dir: str, kalman_vel: float = 0, **kwargs) -> bool:
    """KALMAN_STRICT: Require Kalman velocity to STRONGLY confirm."""
    if spike_dir == "UP":
        return kalman_vel >= KALMAN_VELOCITY_THRESHOLD
    else:
        return kalman_vel <= -KALMAN_VELOCITY_THRESHOLD


def confirm_hybrid_raw_kf(spike_dir: str, velocity: float = 0, kalman_vel: float = 0, **kwargs) -> bool:
    """HYBRID_RAW_KF: Accept if EITHER raw OR Kalman velocity confirms."""
    if spike_dir == "UP":
        return velocity > RAW_VELOCITY_THRESHOLD or kalman_vel > KALMAN_VELOCITY_THRESHOLD
    else:
        return velocity < -RAW_VELOCITY_THRESHOLD or kalman_vel < -KALMAN_VELOCITY_THRESHOLD


# Strategy lookup
STRATEGY_FUNCS = {
    "BASELINE": confirm_baseline,
    "CONSERVATIVE": confirm_conservative,
    "STACK_1of3": confirm_stack_1of3,
    "STACK_2of3": confirm_stack_2of3,
    "STACK_3of3": confirm_stack_3of3,
    "TIERED": confirm_tiered,
    "ADAPT_ZSCORE": confirm_adapt_zscore,
    "ADAPT_VELVAR": confirm_adapt_velvar,
    "ADAPT_ACCEL": confirm_adapt_accel,
    "ADAPT_COMBINED": confirm_adapt_combined,
    "VEL_OR_ACCEL": confirm_vel_or_accel,
    "ACCEL_ALIGNED": confirm_accel_aligned,
    "KALMAN_VEL": confirm_kalman_vel,
    "KALMAN_ACCEL": confirm_kalman_accel,
    "KALMAN_BOTH": confirm_kalman_both,
    "KALMAN_ADAPTIVE": confirm_kalman_adaptive,
    "KALMAN_STRICT": confirm_kalman_strict,
    "HYBRID_RAW_KF": confirm_hybrid_raw_kf,
}


# =============================================================================
# SIMULATION WITH FIXED CYCLING
# =============================================================================

def calc_loser_bid(winner_entry: float, spike_mag: float) -> float:
    """Calculate loser bid price."""
    # FIX: Do NOT divide by 100 - spike_mag is already percentage (0.05 = 0.05%)
    expected_drop = DROP_MULTIPLIER * spike_mag + DROP_INTERCEPT
    expected_drop = max(0.02, min(0.20, expected_drop))
    max_loser = TARGET_PAIR_COST - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid))


def simulate_market(
    spikes_df: pd.DataFrame,
    obs_df: pd.DataFrame,
    btc_df: pd.DataFrame,
    slug: str,
    resolution: str,
    config: BacktestConfig,
    kalman_states: Dict[int, Tuple[float, float, float]] = None,
) -> List[TradeResult]:
    """
    Simulate trading in a single market with FIXED cycling logic.

    FIXED CYCLING:
    - Tracks `in_position` state
    - Blocks new entries until hedge fills completely
    - Uses actual hedge fill timestamp for cycle gap
    """
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    market_spikes = spikes_df[
        (spikes_df['timestamp_ms'] >= market_start) &
        (spikes_df['timestamp_ms'] <= market_end)
    ].copy()

    trades = []

    # FIXED CYCLING STATE
    in_position = False
    last_hedge_ts = 0

    # Regime tracker for adaptive strategies
    regime_tracker = RegimeTracker()

    # Get confirmation function
    confirm_func = STRATEGY_FUNCS.get(config.strategy, confirm_baseline)

    for _, spike_row in market_spikes.iterrows():
        spike_ts = spike_row['timestamp_ms']
        spike_dir = spike_row['spike_direction']
        spike_mag = spike_row['spike_magnitude']
        zscore = spike_row['zscore']

        # Z-score volatility filter
        if zscore < config.z_lo or zscore > config.z_hi:
            continue

        # FIXED CYCLING: Block if still in position
        if in_position:
            continue

        # Enforce gap after hedge fill
        if (spike_ts - last_hedge_ts) < MIN_CYCLE_GAP_MS:
            continue

        # Find nearest observer row
        obs_idx = mdf['timestamp_ms'].searchsorted(spike_ts)
        if obs_idx >= len(mdf):
            obs_idx = len(mdf) - 1

        obs_row = mdf.iloc[obs_idx]
        time_rem = obs_row['time_remaining_secs']

        if time_rem < MIN_TIME:
            continue

        # Get signals
        velocity_bps = obs_row.get('velocity_bps', 0) or 0
        acceleration_bps2 = obs_row.get('acceleration_bps2', 0) or 0

        # Apply velocity zone filter
        zone = ZONE_CONFIGS.get(config.zone_config, ZONE_CONFIGS["ALL"])
        abs_velocity = abs(velocity_bps)
        if abs_velocity < zone["min_vel"] or abs_velocity > zone["max_vel"]:
            continue

        # Update regime tracker
        regime_tracker.update(velocity_bps, acceleration_bps2)

        # Get Kalman-filtered values if available
        kalman_vel, kalman_accel, estimation_var = 0.0, 0.0, 0.01
        if kalman_states:
            closest_ts = min(kalman_states.keys(), key=lambda x: abs(x - spike_ts), default=None)
            if closest_ts and abs(closest_ts - spike_ts) < 1000:
                kalman_vel, kalman_accel, estimation_var = kalman_states[closest_ts]

        # Apply confirmation function
        confirms = confirm_func(
            spike_dir=spike_dir,
            velocity=velocity_bps,
            accel=acceleration_bps2,
            kalman_vel=kalman_vel,
            kalman_accel=kalman_accel,
            estimation_var=estimation_var,
            regime_tracker=regime_tracker,
            zscore=zscore,
        )

        if not confirms:
            continue

        # Entry
        winner_side = spike_dir
        loser_side = "DOWN" if winner_side == "UP" else "UP"

        if winner_side == "UP":
            winner_entry = obs_row['up_ask']
        else:
            winner_entry = obs_row['down_ask']

        loser_target = calc_loser_bid(winner_entry, spike_mag)

        # FIXED CYCLING: Enter position
        in_position = True
        entry_ts = spike_ts

        # Scan forward for hedge
        hedge_type = "resolution"
        loser_fill = 0.0
        hedge_fill_ts = market_end

        for j in range(obs_idx + 1, len(mdf)):
            scan_row = mdf.iloc[j]
            scan_ts = scan_row['timestamp_ms']

            # Time-based stop check
            if config.time_stop_seconds is not None:
                elapsed_secs = (scan_ts - entry_ts) / 1000.0
                if elapsed_secs >= config.time_stop_seconds:
                    # Time stop triggered - exit at current price
                    if loser_side == "UP":
                        loser_fill = scan_row['up_ask']
                    else:
                        loser_fill = scan_row['down_ask']
                    hedge_type = "timestop"
                    hedge_fill_ts = scan_ts
                    break

            # Price-based stop check
            if config.stop_loss_pct is not None:
                if winner_side == "UP":
                    winner_bid_now = scan_row['up_bid']
                else:
                    winner_bid_now = scan_row['down_bid']

                drop_pct = (winner_entry - winner_bid_now) / winner_entry if winner_entry > 0 else 0
                if drop_pct >= config.stop_loss_pct:
                    # Stop loss triggered
                    if loser_side == "UP":
                        loser_fill = scan_row['up_ask']
                    else:
                        loser_fill = scan_row['down_ask']
                    hedge_type = "stoploss"
                    hedge_fill_ts = scan_ts
                    break

            # Passive fill check
            if loser_side == "UP":
                curr_loser_ask = scan_row['up_ask']
            else:
                curr_loser_ask = scan_row['down_ask']

            if curr_loser_ask <= loser_target:
                loser_fill = loser_target
                hedge_type = "passive"
                hedge_fill_ts = scan_ts
                break

        # If no hedge, resolve at market end
        if hedge_type == "resolution":
            if resolution == winner_side:
                loser_fill = loser_target
            else:
                loser_fill = 1.0

        # Calculate PnL
        pair_cost = winner_entry + loser_fill
        if hedge_type == "resolution" and resolution != winner_side:
            pnl = -winner_entry * TARGET_SHARES
        else:
            pnl = (1.0 - pair_cost) * TARGET_SHARES

        trades.append(TradeResult(
            config_name=config.name,
            market_slug=slug,
            entry_time_remaining=time_rem,
            winner_side=winner_side,
            winner_fill_price=winner_entry,
            loser_fill_price=loser_fill,
            hedge_type=hedge_type,
            pair_cost=pair_cost,
            pnl=pnl,
            correct_direction=(resolution == winner_side),
            velocity_bps=velocity_bps,
            zscore=zscore,
            hedge_fill_ts=hedge_fill_ts,
        ))

        # FIXED CYCLING: Exit position after hedge simulation
        in_position = False
        last_hedge_ts = hedge_fill_ts

        # If cycling disabled, stop after first trade
        if not config.enable_cycling:
            break

    return trades


def run_config(
    spikes_df: pd.DataFrame,
    obs_df: pd.DataFrame,
    btc_df: pd.DataFrame,
    config: BacktestConfig,
    hours: float,
) -> ConfigResult:
    """Run backtest for a single config."""
    all_trades = []

    # Pre-compute Kalman states if needed
    needs_kalman = config.strategy.startswith("KALMAN") or config.strategy == "HYBRID_RAW_KF"
    kalman_states = {}

    if needs_kalman and btc_df is not None:
        kf = PriceKalmanFilter(KALMAN_PROCESS_VAR, KALMAN_MEASURE_VAR)
        for _, row in btc_df.iterrows():
            ts = row['timestamp_ms']
            price = row['price']
            _, vel, accel, var = kf.update(price, ts)
            kalman_states[ts] = (vel, accel, var)

    for slug in obs_df['market_slug'].unique():
        mdf = obs_df[obs_df['market_slug'] == slug]
        resolution = mdf['resolution'].iloc[0]
        trades = simulate_market(
            spikes_df, obs_df, btc_df, slug, resolution, config, kalman_states
        )
        all_trades.extend(trades)

    if not all_trades:
        return ConfigResult(
            config_name=config.name,
            group=config.group,
            trades=0,
            total_pnl=0,
            hourly_rate=0,
            direction_accuracy=0,
            trades_per_hour=0,
        )

    total_pnl = sum(t.pnl for t in all_trades)
    total_trades = len(all_trades)
    correct = sum(1 for t in all_trades if t.correct_direction)
    hourly_rate = total_pnl / hours if hours > 0 else 0

    # Hedge breakdown
    hedge_breakdown = {}
    for t in all_trades:
        hedge_breakdown[t.hedge_type] = hedge_breakdown.get(t.hedge_type, 0) + 1

    return ConfigResult(
        config_name=config.name,
        group=config.group,
        trades=total_trades,
        total_pnl=total_pnl,
        hourly_rate=hourly_rate,
        direction_accuracy=correct / total_trades,
        trades_per_hour=total_trades / hours if hours > 0 else 0,
        hedge_breakdown=hedge_breakdown,
    )


# =============================================================================
# CONFIG GENERATION
# =============================================================================

def generate_param_configs() -> List[BacktestConfig]:
    """
    Generate Group 1: Parameter configs.

    30 base configs:
    - 1 zscore_method (EWMA only)
    - 3 lookbacks (60, 72, 84)
    - 3 price_stops (7%, 12%, 15%) for 2 z_zones = 18
    - 2 time_stops (120s, 180s) for 2 z_zones = 12

    x 2 cycling modes = 60 configs
    """
    configs = []

    lookbacks = [60, 72, 84]
    price_stops = [0.07, 0.12, 0.15]
    time_stops = [120, 180]
    z_zones = [
        (0.0, 1.5, "0<z<1.5"),
        (-0.5, 1.5, "-0.5<z<1.5"),
    ]
    cycling_modes = [True, False]

    for lookback in lookbacks:
        for z_lo, z_hi, z_label in z_zones:
            # Price-based stops
            for stop_pct in price_stops:
                for cycling in cycling_modes:
                    cyc_label = "CYC" if cycling else "NOCYC"
                    name = f"EWMA_LB{lookback}_PRICE{int(stop_pct*100)}%_{z_label}_{cyc_label}"
                    configs.append(BacktestConfig(
                        name=name,
                        group="PARAM",
                        lookback_ticks=lookback,
                        zscore_method="ewma",
                        z_lo=z_lo,
                        z_hi=z_hi,
                        stop_loss_pct=stop_pct,
                        time_stop_seconds=None,
                        enable_cycling=cycling,
                        zone_config="ALL",
                        strategy="BASELINE",
                    ))

            # Time-based stops
            for time_stop in time_stops:
                for cycling in cycling_modes:
                    cyc_label = "CYC" if cycling else "NOCYC"
                    name = f"EWMA_LB{lookback}_TIME{time_stop}s_{z_label}_{cyc_label}"
                    configs.append(BacktestConfig(
                        name=name,
                        group="PARAM",
                        lookback_ticks=lookback,
                        zscore_method="ewma",
                        z_lo=z_lo,
                        z_hi=z_hi,
                        stop_loss_pct=None,
                        time_stop_seconds=time_stop,
                        enable_cycling=cycling,
                        zone_config="ALL",
                        strategy="BASELINE",
                    ))

    return configs


def generate_strategy_configs() -> List[BacktestConfig]:
    """
    Generate Group 2: Strategy configs.

    From plan:
    - Multi: STACK_1of3 (ALL, Z2_6, Z3_6, Z4_6), STACK_2of3/STACK_3of3/TIERED (ALL only)
    - Regime: ADAPT_COMBINED, ADAPT_ZSCORE, ADAPT_VELVAR, ADAPT_ACCEL (ALL only)
    - Velocity: BASELINE (ALL, Z2_6), CONSERVATIVE (ALL only)
    - Accel: VEL_OR_ACCEL, ACCEL_ALIGNED (ALL only)
    """
    configs = []

    # Multi-signal
    for zone in ["ALL", "Z2_6", "Z3_6", "Z4_6"]:
        configs.append(BacktestConfig(
            name=f"STACK_1of3_{zone}",
            group="STRATEGY",
            strategy="STACK_1of3",
            zone_config=zone,
            enable_cycling=True,
        ))

    for strategy in ["STACK_2of3", "STACK_3of3", "TIERED"]:
        configs.append(BacktestConfig(
            name=f"{strategy}_ALL",
            group="STRATEGY",
            strategy=strategy,
            zone_config="ALL",
            enable_cycling=True,
        ))

    # Regime-adaptive
    for strategy in ["ADAPT_COMBINED", "ADAPT_ZSCORE", "ADAPT_VELVAR", "ADAPT_ACCEL"]:
        configs.append(BacktestConfig(
            name=f"{strategy}_ALL",
            group="STRATEGY",
            strategy=strategy,
            zone_config="ALL",
            enable_cycling=True,
        ))

    # Velocity
    for zone in ["ALL", "Z2_6"]:
        configs.append(BacktestConfig(
            name=f"BASELINE_{zone}",
            group="STRATEGY",
            strategy="BASELINE",
            zone_config=zone,
            enable_cycling=True,
        ))

    configs.append(BacktestConfig(
        name="CONSERVATIVE_ALL",
        group="STRATEGY",
        strategy="CONSERVATIVE",
        zone_config="ALL",
        enable_cycling=True,
    ))

    # Acceleration
    for strategy in ["VEL_OR_ACCEL", "ACCEL_ALIGNED"]:
        configs.append(BacktestConfig(
            name=f"{strategy}_ALL",
            group="STRATEGY",
            strategy=strategy,
            zone_config="ALL",
            enable_cycling=True,
        ))

    return configs


def generate_kalman_configs() -> List[BacktestConfig]:
    """
    Generate Group 3: Kalman configs.

    7 strategies x 5 zones x 2 cycling = 70 configs
    """
    configs = []

    strategies = [
        "KALMAN_VEL",
        "KALMAN_ACCEL",
        "KALMAN_BOTH",
        "KALMAN_ADAPTIVE",
        "KALMAN_STRICT",
        "HYBRID_RAW_KF",
        "BASELINE",  # Include baseline for comparison
    ]
    zones = ["ALL", "Z2_6", "Z3_6", "Z4_6", "Z5_6"]
    cycling_modes = [True, False]

    for strategy in strategies:
        for zone in zones:
            for cycling in cycling_modes:
                cyc_label = "CYC" if cycling else "NOCYC"
                name = f"{strategy}_{zone}_{cyc_label}"
                configs.append(BacktestConfig(
                    name=name,
                    group="KALMAN",
                    strategy=strategy,
                    zone_config=zone,
                    enable_cycling=cycling,
                ))

    return configs


# =============================================================================
# DATA LOADING
# =============================================================================

def load_data(period: str = "is_oos2") -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, float]:
    """Load data for specified period."""
    print(f"Loading data for period: {period}...")

    if period == "oos5":
        btc_df = pd.read_csv("research/binance_hf/btc_prices_20260124_recovered.csv")
        obs_df = pd.read_csv("research/observer/grid_obs_oos5.csv",
                             on_bad_lines='skip', low_memory=False)
    elif period == "oos34":
        btc_df = pd.read_csv("research/observer/btc_prices_oos3_oos4_combined.csv")
        obs_df = pd.read_csv("research/observer/grid_obs_oos3_oos4_combined.csv",
                             on_bad_lines='skip', low_memory=False)
    else:  # is_oos2
        btc_dir = Path("research/binance_hf")
        btc_dfs = []
        for f in sorted(btc_dir.glob("btc_prices_*.csv")):
            if "recovered" not in f.name:
                df = pd.read_csv(f)
                btc_dfs.append(df)
        btc_df = pd.concat(btc_dfs, ignore_index=True)

        obs_dir = Path("research/observer")
        obs_dfs = []
        for f in sorted(obs_dir.glob("grid_obs_*.csv")):
            if "combined" not in f.name and "oos5" not in f.name and "recovered" not in f.name:
                df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
                obs_dfs.append(df)
        obs_df = pd.concat(obs_dfs, ignore_index=True)

    btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])

    print(f"  BTC prices: {len(btc_df):,} rows")
    print(f"  Observer: {len(obs_df):,} rows")

    # Detect spikes
    spikes_df = detect_spikes_ou(btc_df)

    # Load resolutions
    res_df = pd.read_csv("research/observer/market_resolutions_verified.csv")
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Find overlap
    btc_start, btc_end = spikes_df['timestamp_ms'].min(), spikes_df['timestamp_ms'].max()
    obs_start, obs_end = obs_df['timestamp_ms'].min(), obs_df['timestamp_ms'].max()
    overlap_start = max(btc_start, obs_start)
    overlap_end = min(btc_end, obs_end)

    hours = (overlap_end - overlap_start) / 3600000
    print(f"  Overlap: {hours:.2f} hours")

    # Filter to overlap
    spikes_df = spikes_df[
        (spikes_df['timestamp_ms'] >= overlap_start) &
        (spikes_df['timestamp_ms'] <= overlap_end)
    ]
    obs_df = obs_df[
        (obs_df['timestamp_ms'] >= overlap_start) &
        (obs_df['timestamp_ms'] <= overlap_end)
    ].copy()

    # Add resolutions
    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    # Filter valid markets
    valid_slugs = []
    for slug, mdf in obs_df.groupby('market_slug'):
        max_time = mdf['time_remaining_secs'].max()
        min_time = mdf['time_remaining_secs'].min()
        duration = max_time - min_time
        if duration >= MIN_RUNTIME_SECS and max_time >= 840:
            valid_slugs.append(slug)

    obs_df = obs_df[obs_df['market_slug'].isin(valid_slugs)]
    print(f"  Valid markets: {len(valid_slugs)}")

    # Keep spike events
    spikes_only = spikes_df[spikes_df['spike_detected'] == True].copy()
    print(f"  Spike events: {len(spikes_only):,}")

    return spikes_only, obs_df, btc_df, hours


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Fixed Cycling Grid Backtest")
    parser.add_argument("--data", choices=["is_oos2", "oos34", "oos5", "all"],
                        default="is_oos2", help="Data period to use")
    parser.add_argument("--group", choices=["param", "strategy", "kalman", "all"],
                        default="all", help="Config group to run")
    parser.add_argument("--output", type=str, default="research/fixed_cycling_results.csv",
                        help="Output CSV path")
    args = parser.parse_args()

    print("=" * 80)
    print("FIXED CYCLING GRID BACKTEST")
    print("=" * 80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # Load OU parameters
    load_ou_params()

    # Generate configs
    configs = []
    if args.group in ["param", "all"]:
        configs.extend(generate_param_configs())
    if args.group in ["strategy", "all"]:
        configs.extend(generate_strategy_configs())
    if args.group in ["kalman", "all"]:
        configs.extend(generate_kalman_configs())

    print(f"Total configs to run: {len(configs)}")
    print()

    # Determine periods
    if args.data == "all":
        periods = ["is_oos2", "oos34"]
    else:
        periods = [args.data]

    all_results = []

    for period in periods:
        print()
        print("=" * 80)
        print(f"PERIOD: {period}")
        print("=" * 80)

        spikes_df, obs_df, btc_df, hours = load_data(period)
        print(f"Backtest hours: {hours:.2f}")
        print()

        print(f"Running {len(configs)} configs...")
        print("-" * 80)

        for i, config in enumerate(configs):
            result = run_config(spikes_df, obs_df, btc_df, config, hours)
            result_dict = {
                'period': period,
                'config_name': result.config_name,
                'group': result.group,
                'trades': result.trades,
                'total_pnl': result.total_pnl,
                'hourly_rate': result.hourly_rate,
                'direction_accuracy': result.direction_accuracy,
                'trades_per_hour': result.trades_per_hour,
                'hedge_passive': result.hedge_breakdown.get('passive', 0),
                'hedge_stoploss': result.hedge_breakdown.get('stoploss', 0),
                'hedge_timestop': result.hedge_breakdown.get('timestop', 0),
                'hedge_resolution': result.hedge_breakdown.get('resolution', 0),
            }
            all_results.append(result_dict)

            if result.trades > 0:
                print(f"  [{i+1:3}/{len(configs)}] {config.name[:45]:45} | "
                      f"Trades={result.trades:4} | $/hr=${result.hourly_rate:8.2f} | "
                      f"Acc={result.direction_accuracy:.1%}")

    # Save results
    print()
    print("=" * 80)
    print("SAVING RESULTS")
    print("=" * 80)

    results_df = pd.DataFrame(all_results)
    results_df = results_df.sort_values(['period', 'hourly_rate'], ascending=[True, False])
    results_df.to_csv(args.output, index=False)
    print(f"Results saved to: {args.output}")

    # Summary
    print()
    print("=" * 80)
    print("SUMMARY - TOP 20 BY HOURLY RATE")
    print("=" * 80)
    print()

    for period in periods:
        print(f"\n{period}:")
        print("-" * 70)
        period_df = results_df[results_df['period'] == period]
        top20 = period_df.nlargest(20, 'hourly_rate')

        print(f"{'Config':<45} {'Trades':>7} {'$/hr':>10} {'Acc':>8}")
        print("-" * 70)
        for _, row in top20.iterrows():
            print(f"{row['config_name'][:45]:<45} {row['trades']:>7} "
                  f"${row['hourly_rate']:>8.2f} {row['direction_accuracy']:>7.1%}")

    # Compare cycling=True vs cycling=False for Kalman
    print()
    print("=" * 80)
    print("KALMAN CYCLING COMPARISON")
    print("=" * 80)

    kalman_results = results_df[results_df['group'] == 'KALMAN']
    if len(kalman_results) > 0:
        # Extract cycling mode from name
        kalman_results = kalman_results.copy()
        kalman_results['cycling'] = kalman_results['config_name'].apply(
            lambda x: 'CYC' if '_CYC' in x and '_NOCYC' not in x else 'NOCYC'
        )
        kalman_results['base_config'] = kalman_results['config_name'].apply(
            lambda x: x.replace('_CYC', '').replace('_NOCYC', '')
        )

        print("\nAverage hourly rate by cycling mode:")
        for period in periods:
            print(f"\n  {period}:")
            period_kalman = kalman_results[kalman_results['period'] == period]
            cyc_avg = period_kalman[period_kalman['cycling'] == 'CYC']['hourly_rate'].mean()
            nocyc_avg = period_kalman[period_kalman['cycling'] == 'NOCYC']['hourly_rate'].mean()
            print(f"    CYCLING=True:  ${cyc_avg:.2f}/hr")
            print(f"    CYCLING=False: ${nocyc_avg:.2f}/hr")
            print(f"    Difference:    ${cyc_avg - nocyc_avg:+.2f}/hr")

    print()
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
