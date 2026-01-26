#!/usr/bin/env python3
"""
Kalman Filter Signal Backtest

Tests Kalman-filtered signals for AGGRESSIVE strategy confirmation.

Hypothesis: Kalman filter provides cleaner velocity/acceleration estimates by
filtering noise, leading to better confirmation signals. The filter separates
"true" price trend from noise and adapts dynamically without fixed window parameters.

Strategies:
1. BASELINE: Current raw velocity confirmation
2. KALMAN_VEL: Use Kalman-filtered velocity
3. KALMAN_ACCEL: Use Kalman-filtered acceleration
4. KALMAN_BOTH: Use both Kalman-filtered velocity and acceleration
5. KALMAN_ADAPTIVE: Adaptive threshold based on Kalman estimation variance

References:
- Academic research shows Kalman filter achieves Sharpe ~0.64 with excellent drawdown control
- Built-in velocity AND acceleration estimation
- Adapts dynamically without fixed window parameters

Data periods:
  --is-oos2: IS+OOS2 (Jan 16-19, ~82 hours)
  --oos34:   OOS3+OOS4 (Jan 22-24, ~47 hours)
  --oos5:    OOS5 (Jan 26, ~42 hours)
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Tuple
import sys
import math

sys.path.insert(0, str(Path(__file__).parent.parent))

# =============================================================================
# CONFIGURATION (Match AGGRESSIVE spec)
# =============================================================================

TARGET_SHARES = 50
MIN_TIME = 60
MIN_RUNTIME_SECS = 300

# =============================================================================
# VELOCITY ZONE CONFIGURATIONS
# =============================================================================
# Zone filtering: LOSING_PATTERNS.md shows "strong" zone = 54.5% WR vs "neutral" = 36.9%
# Kalman filter might enable trading in lower zones due to better noise rejection

ZONE_CONFIGS = {
    "ALL":     {"min_vel": 0.00, "max_vel": 99.0, "desc": "No velocity filter (current)"},
    "Z2_6":    {"min_vel": 0.05, "max_vel": 99.0, "desc": "Exclude neutral zone"},
    "Z3_6":    {"min_vel": 0.10, "max_vel": 99.0, "desc": "Moderate+ only"},
    "Z4_6":    {"min_vel": 0.30, "max_vel": 99.0, "desc": "Strong+ only"},
    "Z5_6":    {"min_vel": 0.50, "max_vel": 99.0, "desc": "Extreme+ only"},
}

# Spike detection (OU method)
SPIKE_LOOKBACK = 72
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10

# Z-score filter (AGGRESSIVE volatility range)
ZSCORE_LO = 0.0
ZSCORE_HI = 1.5

# Hedge pricing
DROP_MULTIPLIER = 0.50
DROP_INTERCEPT = 0.08
TARGET_PAIR_COST = 0.99

# Velocity thresholds
RAW_VELOCITY_THRESHOLD = 0.10      # For raw velocity
KALMAN_VELOCITY_THRESHOLD = 0.08  # Lower for filtered (less noise)
KALMAN_ACCEL_THRESHOLD = 0.005    # For Kalman acceleration

# Kalman filter parameters
KALMAN_PROCESS_VAR = 0.001    # Process noise variance (how much we expect state to change)
KALMAN_MEASURE_VAR = 0.01     # Measurement noise variance (observation noise)


@dataclass
class TradeResult:
    market_slug: str
    entry_time_remaining: float
    winner_side: str
    winner_fill_price: float
    loser_fill_price: float
    hedge_type: str
    pair_cost: float
    pnl: float
    correct_direction: bool
    raw_velocity: float
    kalman_velocity: float
    kalman_acceleration: float
    estimation_variance: float
    method_used: str


@dataclass
class BacktestResult:
    name: str
    total_trades: int
    total_pnl: float
    hourly_rate: float
    direction_accuracy: float
    trades_per_hour: float
    avg_raw_velocity: float
    avg_kalman_velocity: float
    avg_estimation_var: float
    zone_config: str = "ALL"


# =============================================================================
# KALMAN FILTER IMPLEMENTATION
# =============================================================================

class PriceKalmanFilter:
    """
    Kalman filter for price tracking with velocity and acceleration estimation.

    State vector: [price, velocity, acceleration]
    - price: current price level
    - velocity: rate of price change (1st derivative)
    - acceleration: rate of velocity change (2nd derivative)

    Uses constant acceleration model:
    - x_new = x + v*dt + 0.5*a*dt²
    - v_new = v + a*dt
    - a_new = a (constant between updates)
    """

    def __init__(self, process_var: float = 0.001, measurement_var: float = 0.01):
        """
        Initialize Kalman filter.

        Args:
            process_var: Process noise variance (state evolution uncertainty)
            measurement_var: Measurement noise variance (observation uncertainty)
        """
        self.process_var = process_var
        self.measurement_var = measurement_var

        # State vector: [price, velocity, acceleration]
        self.x = np.zeros(3)

        # State covariance matrix
        self.P = np.eye(3) * 1.0

        # Process noise covariance (Q)
        self.Q = np.eye(3) * process_var

        # Measurement noise covariance (R) - scalar since we only measure price
        self.R = measurement_var

        # Measurement matrix (H) - we only observe price directly
        self.H = np.array([[1.0, 0.0, 0.0]])

        # Track initialization
        self.initialized = False
        self.last_timestamp = None

    def reset(self, initial_price: float):
        """Reset filter with initial price."""
        self.x = np.array([initial_price, 0.0, 0.0])
        self.P = np.eye(3) * 1.0
        self.initialized = True
        self.last_timestamp = None

    def predict(self, dt: float):
        """
        Prediction step: project state forward by dt seconds.

        State transition:
        - x_new = x + v*dt + 0.5*a*dt²
        - v_new = v + a*dt
        - a_new = a
        """
        # State transition matrix
        F = np.array([
            [1, dt, 0.5 * dt**2],
            [0, 1, dt],
            [0, 0, 1]
        ])

        # Predict state
        self.x = F @ self.x

        # Predict covariance
        self.P = F @ self.P @ F.T + self.Q

    def update(self, price: float, timestamp_ms: float = None) -> Tuple[float, float, float, float]:
        """
        Update step: incorporate new price observation.

        Args:
            price: Observed price
            timestamp_ms: Optional timestamp for computing dt

        Returns:
            Tuple of (filtered_price, filtered_velocity, filtered_acceleration, estimation_variance)
        """
        if not self.initialized:
            self.reset(price)
            self.last_timestamp = timestamp_ms
            return self.x[0], self.x[1], self.x[2], self.P[1, 1]

        # Compute dt if we have timestamps
        if timestamp_ms is not None and self.last_timestamp is not None:
            dt = (timestamp_ms - self.last_timestamp) / 1000.0  # Convert to seconds
            dt = max(0.001, min(1.0, dt))  # Clamp to reasonable range
        else:
            dt = 0.0167  # Default ~60Hz (16.7ms)

        self.last_timestamp = timestamp_ms

        # Prediction step
        self.predict(dt)

        # Innovation (measurement residual)
        y = price - self.H @ self.x

        # Innovation covariance
        S = self.H @ self.P @ self.H.T + self.R

        # Kalman gain
        K = self.P @ self.H.T / S

        # Update state
        self.x = self.x + K.flatten() * y

        # Update covariance
        I = np.eye(3)
        self.P = (I - K @ self.H) @ self.P

        # Return filtered values
        # Velocity in basis points per second (convert from price units)
        # Assuming price is in $ and we want bps relative to price level
        velocity_bps = (self.x[1] / self.x[0]) * 10000 if self.x[0] > 0 else 0
        accel_bps = (self.x[2] / self.x[0]) * 10000 if self.x[0] > 0 else 0

        return self.x[0], velocity_bps, accel_bps, self.P[1, 1]


class MarketKalmanTracker:
    """Manages Kalman filters for multiple markets."""

    def __init__(self, process_var: float = 0.001, measurement_var: float = 0.01):
        self.filters = {}
        self.process_var = process_var
        self.measurement_var = measurement_var

    def get_filter(self, market_slug: str) -> PriceKalmanFilter:
        """Get or create Kalman filter for a market."""
        if market_slug not in self.filters:
            self.filters[market_slug] = PriceKalmanFilter(
                self.process_var, self.measurement_var
            )
        return self.filters[market_slug]

    def reset(self):
        """Reset all filters."""
        self.filters = {}


# =============================================================================
# OU PARAMETERS
# =============================================================================

_ou_params = None

def load_ou_params():
    global _ou_params
    try:
        from src.strategies.ou_volatility import OUParameters
        _ou_params = OUParameters.load("research/ou_params.json")
        print(f"[OU] Loaded: μ={_ou_params.mu:.4f}, σ_stat={_ou_params.sigma_stat:.4f}")
    except Exception as e:
        print(f"[OU] Warning: {e} - using defaults")
        _ou_params = None


def compute_ou_threshold(volatility: float) -> float:
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
# CONFIRMATION STRATEGIES
# =============================================================================

def confirm_baseline(spike_dir: str, raw_velocity: float, kalman_velocity: float,
                     kalman_accel: float, estimation_var: float) -> Tuple[bool, str]:
    """BASELINE: Current raw velocity confirmation (accepts neutral zone)."""
    if spike_dir == "UP":
        return raw_velocity > -RAW_VELOCITY_THRESHOLD, "BASELINE"
    else:
        return raw_velocity < RAW_VELOCITY_THRESHOLD, "BASELINE"


def confirm_kalman_velocity(spike_dir: str, raw_velocity: float, kalman_velocity: float,
                            kalman_accel: float, estimation_var: float) -> Tuple[bool, str]:
    """KALMAN_VEL: Use Kalman-filtered velocity instead of raw."""
    if spike_dir == "UP":
        return kalman_velocity > -KALMAN_VELOCITY_THRESHOLD, "KALMAN_VEL"
    else:
        return kalman_velocity < KALMAN_VELOCITY_THRESHOLD, "KALMAN_VEL"


def confirm_kalman_accel(spike_dir: str, raw_velocity: float, kalman_velocity: float,
                         kalman_accel: float, estimation_var: float) -> Tuple[bool, str]:
    """KALMAN_ACCEL: Require Kalman acceleration to confirm direction."""
    if spike_dir == "UP":
        vel_ok = raw_velocity > -RAW_VELOCITY_THRESHOLD
        accel_ok = kalman_accel > KALMAN_ACCEL_THRESHOLD
        return vel_ok and accel_ok, "KALMAN_ACCEL"
    else:
        vel_ok = raw_velocity < RAW_VELOCITY_THRESHOLD
        accel_ok = kalman_accel < -KALMAN_ACCEL_THRESHOLD
        return vel_ok and accel_ok, "KALMAN_ACCEL"


def confirm_kalman_both(spike_dir: str, raw_velocity: float, kalman_velocity: float,
                        kalman_accel: float, estimation_var: float) -> Tuple[bool, str]:
    """KALMAN_BOTH: Use both Kalman velocity and acceleration."""
    if spike_dir == "UP":
        vel_ok = kalman_velocity > -KALMAN_VELOCITY_THRESHOLD
        accel_ok = kalman_accel > 0  # Just direction alignment
        return vel_ok and accel_ok, "KALMAN_BOTH"
    else:
        vel_ok = kalman_velocity < KALMAN_VELOCITY_THRESHOLD
        accel_ok = kalman_accel < 0
        return vel_ok and accel_ok, "KALMAN_BOTH"


def confirm_kalman_adaptive(spike_dir: str, raw_velocity: float, kalman_velocity: float,
                            kalman_accel: float, estimation_var: float) -> Tuple[bool, str]:
    """KALMAN_ADAPTIVE: Adaptive threshold based on estimation variance.

    When estimation variance is high (uncertain), require stronger signal.
    When variance is low (confident), accept weaker signals.
    """
    # Scale threshold by estimation variance
    # High variance -> higher threshold (more conservative)
    # Low variance -> lower threshold (more aggressive)
    var_scale = min(max(estimation_var * 100, 0.5), 2.0)  # Clamp scale factor
    adaptive_threshold = KALMAN_VELOCITY_THRESHOLD * var_scale

    if spike_dir == "UP":
        return kalman_velocity > -adaptive_threshold, f"KALMAN_ADAPT_v{var_scale:.2f}"
    else:
        return kalman_velocity < adaptive_threshold, f"KALMAN_ADAPT_v{var_scale:.2f}"


def confirm_kalman_strict(spike_dir: str, raw_velocity: float, kalman_velocity: float,
                          kalman_accel: float, estimation_var: float) -> Tuple[bool, str]:
    """KALMAN_STRICT: Require Kalman velocity to STRONGLY confirm (reject neutral)."""
    if spike_dir == "UP":
        return kalman_velocity >= KALMAN_VELOCITY_THRESHOLD, "KALMAN_STRICT"
    else:
        return kalman_velocity <= -KALMAN_VELOCITY_THRESHOLD, "KALMAN_STRICT"


def confirm_hybrid_raw_kalman(spike_dir: str, raw_velocity: float, kalman_velocity: float,
                              kalman_accel: float, estimation_var: float) -> Tuple[bool, str]:
    """HYBRID: Accept if EITHER raw OR Kalman velocity confirms (more permissive)."""
    if spike_dir == "UP":
        raw_ok = raw_velocity > RAW_VELOCITY_THRESHOLD
        kalman_ok = kalman_velocity > KALMAN_VELOCITY_THRESHOLD
        return raw_ok or kalman_ok, "HYBRID_RAW_KF"
    else:
        raw_ok = raw_velocity < -RAW_VELOCITY_THRESHOLD
        kalman_ok = kalman_velocity < -KALMAN_VELOCITY_THRESHOLD
        return raw_ok or kalman_ok, "HYBRID_RAW_KF"


# =============================================================================
# SPIKE DETECTION
# =============================================================================

def detect_spikes_ou(btc_df: pd.DataFrame) -> pd.DataFrame:
    """Detect spikes using OU adaptive threshold."""
    print("  Detecting spikes (OU method)...")
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    df['price_prev'] = df['price'].shift(SPIKE_LOOKBACK)
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

    spike_count = df['spike_detected'].sum()
    print(f"    Found {spike_count:,} spikes in {len(df):,} ticks")

    return df[['timestamp_ms', 'price', 'spike_detected', 'spike_direction',
               'spike_magnitude', 'threshold', 'zscore']]


# =============================================================================
# SIMULATION
# =============================================================================

def calc_loser_bid(winner_entry: float, spike_mag: float) -> float:
    expected_drop = DROP_MULTIPLIER * spike_mag / 100 + DROP_INTERCEPT
    expected_drop = max(0.02, min(0.20, expected_drop))
    max_loser = TARGET_PAIR_COST - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid))


def simulate_market(spikes_df: pd.DataFrame, obs_df: pd.DataFrame, btc_df: pd.DataFrame,
                    slug: str, resolution: str,
                    confirm_func, kalman_tracker: MarketKalmanTracker,
                    zone_config: str = "ALL",
                    enable_cycling: bool = True) -> List[TradeResult]:
    """Simulate trading with Kalman-filtered signals.

    FIXED: Proper cycling logic - blocks new entries until hedge fills.
    """
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    # Get BTC prices for this market's time range
    market_btc = btc_df[(btc_df['timestamp_ms'] >= market_start - 60000) &
                         (btc_df['timestamp_ms'] <= market_end)].copy()
    market_btc = market_btc.sort_values('timestamp_ms').reset_index(drop=True)

    market_spikes = spikes_df[(spikes_df['timestamp_ms'] >= market_start) &
                               (spikes_df['timestamp_ms'] <= market_end)].copy()

    # Get Kalman filter for this market
    kf = kalman_tracker.get_filter(slug)

    # Pre-process: run Kalman filter through all BTC prices to build up state
    kalman_states = {}
    for _, row in market_btc.iterrows():
        ts = row['timestamp_ms']
        price = row['price']
        _, vel, accel, var = kf.update(price, ts)
        kalman_states[ts] = (vel, accel, var)

    trades = []

    # PROPER CYCLING: Track position state, not just time gap
    in_position = False
    last_hedge_ts = 0
    MIN_CYCLE_GAP_MS = 1000

    for _, spike_row in market_spikes.iterrows():
        spike_ts = spike_row['timestamp_ms']
        spike_dir = spike_row['spike_direction']
        spike_mag = spike_row['spike_magnitude']
        zscore = spike_row['zscore']

        # Z-score volatility filter (AGGRESSIVE spec)
        if zscore < ZSCORE_LO or zscore > ZSCORE_HI:
            continue

        # PROPER CYCLING: Block if still in position
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

        # Get raw velocity from observer
        raw_velocity = obs_row.get('velocity_bps', 0) or 0

        # Apply velocity zone filter (using raw velocity for zone filtering)
        zone = ZONE_CONFIGS.get(zone_config, ZONE_CONFIGS["ALL"])
        abs_velocity = abs(raw_velocity)
        if abs_velocity < zone["min_vel"] or abs_velocity > zone["max_vel"]:
            continue

        # Get Kalman-filtered values
        # Find closest Kalman state to spike timestamp
        closest_ts = min(kalman_states.keys(), key=lambda x: abs(x - spike_ts), default=None)
        if closest_ts and abs(closest_ts - spike_ts) < 1000:  # Within 1 second
            kalman_velocity, kalman_accel, estimation_var = kalman_states[closest_ts]
        else:
            kalman_velocity = raw_velocity
            kalman_accel = 0.0
            estimation_var = 0.01

        # Apply confirmation function
        confirms, method = confirm_func(spike_dir, raw_velocity, kalman_velocity,
                                        kalman_accel, estimation_var)

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

        # PROPER CYCLING: Enter position
        in_position = True

        # Scan forward for hedge and track fill timestamp
        hedge_type = "resolution"
        loser_fill = 0.0
        hedge_fill_ts = market_end

        for j in range(obs_idx + 1, len(mdf)):
            scan_row = mdf.iloc[j]

            if loser_side == "UP":
                curr_loser_ask = scan_row['up_ask']
            else:
                curr_loser_ask = scan_row['down_ask']

            if curr_loser_ask <= loser_target:
                loser_fill = loser_target
                hedge_type = "passive"
                hedge_fill_ts = scan_row['timestamp_ms']
                break

        if hedge_type == "resolution":
            if resolution == winner_side:
                hedge_type = "passive"
                loser_fill = loser_target
            else:
                loser_fill = 1.0

        pair_cost = winner_entry + loser_fill
        if hedge_type == "resolution":
            pnl = -winner_entry * TARGET_SHARES
        else:
            pnl = (1.0 - pair_cost) * TARGET_SHARES

        trades.append(TradeResult(
            market_slug=slug,
            entry_time_remaining=time_rem,
            winner_side=winner_side,
            winner_fill_price=winner_entry,
            loser_fill_price=loser_fill,
            hedge_type=hedge_type,
            pair_cost=pair_cost,
            pnl=pnl,
            correct_direction=(resolution == winner_side),
            raw_velocity=raw_velocity,
            kalman_velocity=kalman_velocity,
            kalman_acceleration=kalman_accel,
            estimation_variance=estimation_var,
            method_used=method,
        ))

        # PROPER CYCLING: Exit position after hedge simulation
        in_position = False
        last_hedge_ts = hedge_fill_ts

        # If cycling disabled, stop after first trade
        if not enable_cycling:
            break

    return trades


def run_backtest(spikes_df: pd.DataFrame, obs_df: pd.DataFrame, btc_df: pd.DataFrame,
                 hours: float, name: str, confirm_func,
                 zone_config: str = "ALL",
                 process_var: float = KALMAN_PROCESS_VAR,
                 measure_var: float = KALMAN_MEASURE_VAR) -> BacktestResult:
    """Run backtest with a specific confirmation function."""
    all_trades = []

    # Create Kalman tracker with specified parameters
    kalman_tracker = MarketKalmanTracker(process_var, measure_var)

    for slug in obs_df['market_slug'].unique():
        mdf = obs_df[obs_df['market_slug'] == slug]
        resolution = mdf['resolution'].iloc[0]
        trades = simulate_market(spikes_df, obs_df, btc_df, slug, resolution,
                                confirm_func, kalman_tracker, zone_config=zone_config)
        all_trades.extend(trades)

    if not all_trades:
        return BacktestResult(name=name, total_trades=0, total_pnl=0,
                              hourly_rate=0, direction_accuracy=0,
                              trades_per_hour=0, avg_raw_velocity=0,
                              avg_kalman_velocity=0, avg_estimation_var=0,
                              zone_config=zone_config)

    total_pnl = sum(t.pnl for t in all_trades)
    total_trades = len(all_trades)
    correct = sum(1 for t in all_trades if t.correct_direction)
    hourly_rate = total_pnl / hours if hours > 0 else 0
    avg_raw_velocity = np.mean([abs(t.raw_velocity) for t in all_trades])
    avg_kalman_velocity = np.mean([abs(t.kalman_velocity) for t in all_trades])
    avg_estimation_var = np.mean([t.estimation_variance for t in all_trades])

    return BacktestResult(
        name=name,
        total_trades=total_trades,
        total_pnl=total_pnl,
        hourly_rate=hourly_rate,
        direction_accuracy=correct / total_trades,
        trades_per_hour=total_trades / hours,
        avg_raw_velocity=avg_raw_velocity,
        avg_kalman_velocity=avg_kalman_velocity,
        avg_estimation_var=avg_estimation_var,
        zone_config=zone_config,
    )


# =============================================================================
# DATA LOADING
# =============================================================================

def load_data(period: str = "is_oos2"):
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
    else:
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
            if "combined" not in f.name and "oos5" not in f.name:
                df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
                obs_dfs.append(df)
        obs_df = pd.concat(obs_dfs, ignore_index=True)

    btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])

    print(f"  Binance: {len(btc_df):,} rows")
    print(f"  Observer: {len(obs_df):,} rows")

    spikes_df = detect_spikes_ou(btc_df)

    res_df = pd.read_csv("research/observer/market_resolutions_verified.csv")
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    btc_start, btc_end = spikes_df['timestamp_ms'].min(), spikes_df['timestamp_ms'].max()
    obs_start, obs_end = obs_df['timestamp_ms'].min(), obs_df['timestamp_ms'].max()
    overlap_start = max(btc_start, obs_start)
    overlap_end = min(btc_end, obs_end)

    hours = (overlap_end - overlap_start) / 3600000
    print(f"  Overlap: {hours:.2f} hours")

    # Keep full BTC data for Kalman processing
    btc_df_full = btc_df.copy()

    spikes_df = spikes_df[(spikes_df['timestamp_ms'] >= overlap_start) &
                           (spikes_df['timestamp_ms'] <= overlap_end)]
    obs_df = obs_df[(obs_df['timestamp_ms'] >= overlap_start) &
                     (obs_df['timestamp_ms'] <= overlap_end)].copy()

    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    valid_slugs = []
    for slug, mdf in obs_df.groupby('market_slug'):
        max_time = mdf['time_remaining_secs'].max()
        min_time = mdf['time_remaining_secs'].min()
        duration = max_time - min_time
        if duration >= MIN_RUNTIME_SECS and max_time >= 840:
            valid_slugs.append(slug)

    obs_df = obs_df[obs_df['market_slug'].isin(valid_slugs)]
    print(f"  Valid markets: {len(valid_slugs)}")

    spikes_only = spikes_df[spikes_df['spike_detected'] == True].copy()
    print(f"  Spike events: {len(spikes_only):,}")

    return spikes_only, obs_df, btc_df_full, hours


# =============================================================================
# MAIN
# =============================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Kalman Filter Signal Backtest")
    parser.add_argument("--is-oos2", action="store_true", help="Use IS+OOS2 data (default)")
    parser.add_argument("--oos34", action="store_true", help="Use OOS3+OOS4 data")
    parser.add_argument("--oos5", action="store_true", help="Use OOS5 data")
    parser.add_argument("--all", action="store_true", help="Run on all periods")
    parser.add_argument("--tune-kalman", action="store_true", help="Test different Kalman parameters")
    parser.add_argument("--zone-filter", choices=list(ZONE_CONFIGS.keys()),
                        default="ALL", help="Velocity zone filter")
    parser.add_argument("--grid-zones", action="store_true",
                        help="Run grid search across all zone configs")
    parser.add_argument("--csv-output", type=str, default=None,
                        help="Output CSV path for results")
    args = parser.parse_args()

    if args.all:
        periods = ["is_oos2", "oos34", "oos5"]
    elif args.oos5:
        periods = ["oos5"]
    elif args.oos34:
        periods = ["oos34"]
    else:
        periods = ["is_oos2"]

    # Determine zone configs to test
    zone_configs = list(ZONE_CONFIGS.keys()) if args.grid_zones else [args.zone_filter]

    print("=" * 80)
    print("KALMAN FILTER SIGNAL BACKTEST - WITH ZONE GRID SEARCH")
    print("=" * 80)
    print()

    load_ou_params()

    all_period_results = {}

    # NOTE: BASELINE is ONLY run in velocity_options_backtest.py to avoid redundant runs
    strategies = [
        # ("BASELINE", confirm_baseline),  # SKIPPED - run only in velocity_options_backtest.py
        ("KALMAN_VEL", confirm_kalman_velocity),
        ("KALMAN_ACCEL", confirm_kalman_accel),
        ("KALMAN_BOTH", confirm_kalman_both),
        ("KALMAN_ADAPTIVE", confirm_kalman_adaptive),
        ("KALMAN_STRICT", confirm_kalman_strict),
        ("HYBRID_RAW_KF", confirm_hybrid_raw_kalman),
    ]

    for period in periods:
        period_name = {
            "is_oos2": "IS+OOS2 (Jan 16-19, ~82h)",
            "oos34": "OOS3+OOS4 (Jan 22-24, ~47h)",
            "oos5": "OOS5 (Jan 26, ~42h)"
        }[period]

        print()
        print("=" * 80)
        print(f"PERIOD: {period_name}")
        print("=" * 80)

        spikes_df, obs_df, btc_df, hours = load_data(period)
        print(f"\nBacktest: {hours:.2f} hours")
        print()
        print("Running backtests...")
        print("-" * 80)

        results = []

        for zone in zone_configs:
            for name, func in strategies:
                r = run_backtest(spikes_df, obs_df, btc_df, hours, name, func, zone_config=zone)
                results.append(r)
                if r.total_trades > 0:
                    print(f"  {r.name:18} | Zone={zone:5} | Trades={r.total_trades:4} | $/hr=${r.hourly_rate:7.2f} | Acc={r.direction_accuracy:.1%}")

        # Kalman parameter tuning
        if args.tune_kalman:
            print("\n  Kalman Parameter Tuning:")
            for proc_var in [0.0001, 0.001, 0.01]:
                for meas_var in [0.001, 0.01, 0.1]:
                    r = run_backtest(spikes_df, obs_df, btc_df, hours,
                                    f"KF_p{proc_var}_m{meas_var}",
                                    confirm_kalman_velocity,
                                    process_var=proc_var, measure_var=meas_var)
                    if r.total_trades > 0:
                        results.append(r)
                        print(f"    proc={proc_var:.4f}, meas={meas_var:.3f}: "
                              f"${r.hourly_rate:.2f}/hr, {r.direction_accuracy:.1%} acc")

        all_period_results[period] = results

        # Period summary
        print()
        print(f"{'Strategy':<20} {'Zone':>6} {'Trades':>7} {'$/hr':>10} {'Acc%':>8}")
        print("-" * 60)
        for r in sorted(results, key=lambda x: x.hourly_rate, reverse=True)[:20]:
            print(f"{r.name:<20} {r.zone_config:>6} {r.total_trades:>7} ${r.hourly_rate:>8.2f} "
                  f"{r.direction_accuracy:>7.1%}")

    # Cross-period comparison (with zone grid search)
    if len(periods) > 1 and args.grid_zones:
        print()
        print("=" * 90)
        print("CROSS-PERIOD COMPARISON - STRATEGY + ZONE COMBINATIONS")
        print("=" * 90)
        print()

        # Get all unique method+zone combinations
        combos = set()
        for results in all_period_results.values():
            for r in results:
                combos.add((r.name, r.zone_config))

        # Calculate stats for each combo
        combo_stats = []
        for method, zone in sorted(combos):
            rates = []
            for period in periods:
                r = next((x for x in all_period_results[period]
                         if x.name == method and x.zone_config == zone), None)
                if r and r.total_trades > 0:
                    rates.append(r.hourly_rate)

            if len(rates) == len(periods):
                avg = np.mean(rates)
                std = np.std(rates)
                combo_stats.append((method, zone, avg, std, rates))

        # Sort by average and show top results
        combo_stats.sort(key=lambda x: -x[2])

        print(f"{'Strategy':<20} {'Zone':>6}", end="")
        for period in periods:
            print(f" {period:>12}", end="")
        print(f" {'Avg':>10} {'Std':>8}")
        print("-" * (26 + 13 * len(periods) + 20))

        for method, zone, avg, std, rates in combo_stats[:20]:
            print(f"{method:<20} {zone:>6}", end="")
            for rate in rates:
                print(f" ${rate:>10.2f}", end="")
            print(f" ${avg:>8.2f} {std:>7.2f}")

        # Recommendation
        print()
        print("=" * 90)
        print("RECOMMENDATION - Best Strategy+Zone Combo")
        print("=" * 90)

        # Score = avg - 0.5*std (penalize variance)
        combo_stats.sort(key=lambda x: x[2] - 0.5 * x[3], reverse=True)
        best = combo_stats[0]

        print(f"\nBest combo: {best[0]} @ {best[1]}")
        print(f"  Avg $/hr:     ${best[2]:.2f}")
        print(f"  Std dev:      ${best[3]:.2f}")
        print(f"  Score:        ${best[2] - 0.5 * best[3]:.2f}")
        print(f"  Per-period:   {', '.join(f'${r:.2f}' for r in best[4])}")

        # Compare to BASELINE@ALL
        baseline_stats = next((s for s in combo_stats if s[0] == "BASELINE" and s[1] == "ALL"), None)
        if baseline_stats and (best[0] != "BASELINE" or best[1] != "ALL"):
            print(f"\n  vs BASELINE@ALL:")
            print(f"    Avg diff:   ${best[2] - baseline_stats[2]:+.2f}/hr")
            print(f"    Std diff:   ${best[3] - baseline_stats[3]:+.2f}")

        # Show best zone per strategy
        print()
        print("Best Zone per Strategy:")
        print("-" * 50)
        method_names = [s[0] for s in strategies]
        for method in method_names:
            method_combos = [c for c in combo_stats if c[0] == method]
            if method_combos:
                best_for_method = max(method_combos, key=lambda x: x[2])
                print(f"  {method:<20}: {best_for_method[1]:>6} (avg ${best_for_method[2]:.2f}/hr)")

    elif len(periods) > 1:
        # Original cross-period comparison without zone grid
        print()
        print("=" * 80)
        print("CROSS-PERIOD COMPARISON")
        print("=" * 80)
        print()

        method_names = [s[0] for s in strategies]

        print(f"{'Strategy':<20}", end="")
        for period in periods:
            print(f" {period:>12}", end="")
        print(f" {'Avg':>10} {'Std':>8}")
        print("-" * (20 + 13 * len(periods) + 20))

        method_stats = []
        for method in method_names:
            rates = []
            print(f"{method:<20}", end="")
            for period in periods:
                r = next((x for x in all_period_results[period] if x.name == method), None)
                if r and r.total_trades > 0:
                    rates.append(r.hourly_rate)
                    print(f" ${r.hourly_rate:>10.2f}", end="")
                else:
                    print(f" {'N/A':>11}", end="")

            if len(rates) == len(periods):
                avg = np.mean(rates)
                std = np.std(rates)
                print(f" ${avg:>8.2f} {std:>7.2f}")
                method_stats.append((method, avg, std, rates))
            else:
                print()

        # Recommendation
        if method_stats:
            print()
            print("=" * 80)
            print("RECOMMENDATION")
            print("=" * 80)

            method_stats.sort(key=lambda x: x[1] - 0.5 * x[2], reverse=True)
            best = method_stats[0]

            print(f"\nBest strategy: {best[0]}")
            print(f"  Avg $/hr:     ${best[1]:.2f}")
            print(f"  Std dev:      ${best[2]:.2f}")
            print(f"  Per-period:   {', '.join(f'${r:.2f}' for r in best[3])}")

    # Save results to CSV
    if args.csv_output or args.grid_zones:
        output_path = args.csv_output if args.csv_output else "research/kalman_signal_results.csv"
        all_rows = []
        for period in periods:
            for r in all_period_results.get(period, []):
                all_rows.append({
                    'period': period,
                    'method': r.name,
                    'zone': r.zone_config,
                    'trades': r.total_trades,
                    'total_pnl': r.total_pnl,
                    'hourly_rate': r.hourly_rate,
                    'direction_accuracy': r.direction_accuracy,
                    'trades_per_hour': r.trades_per_hour,
                    'avg_raw_velocity': r.avg_raw_velocity,
                    'avg_kalman_velocity': r.avg_kalman_velocity,
                    'avg_estimation_var': r.avg_estimation_var,
                })
        if all_rows:
            results_df = pd.DataFrame(all_rows)
            results_df = results_df.sort_values('hourly_rate', ascending=False)
            results_df.to_csv(output_path, index=False)
            print(f"\nResults saved to: {output_path}")


if __name__ == "__main__":
    main()
