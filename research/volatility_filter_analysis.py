#!/usr/bin/env python3
"""
Volatility Filter Analysis

Analyzes whether skipping extreme volatility periods improves PnL.
Runs post-hoc on optimizer results without re-running full optimization.

Key Question: What's the optimal z-score cutoff that maximizes $/hr while minimizing time sitting out?

Usage:
    # Using CLI params directly:
    python research/volatility_filter_analysis.py --lookback 84 --shares 50 --stop-loss 0.12

    # Using best config from optimizer CSV:
    python research/volatility_filter_analysis.py --from-csv research/optimizer_ou_combined.csv

    # Use specific time range:
    python research/volatility_filter_analysis.py --lookback 84 --shares 50 --start-ts 1768705387229

Author: Claude Code
Date: January 20, 2026
"""

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from tqdm import tqdm

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


# =============================================================================
# CONSTANTS (from spike_param_optimizer_ewma.py)
# =============================================================================

MIN_TIME = 60  # Minimum seconds remaining to enter
MIN_RUNTIME_SECS = 300  # Minimum market duration
MIN_CYCLE_GAP_MS = 200  # Matches observer data rate (5Hz)

# Spike detection
SPIKE_THRESHOLD = 0.02  # Base threshold

# EWMA parameters
EWMA_BASE_THRESHOLD = 0.02
EWMA_MIN_THRESHOLD = 0.010
EWMA_MAX_THRESHOLD = 0.10
EWMA_MIN_RATIO = 0.5
EWMA_MAX_RATIO = 3.0

# OU-based adaptive threshold parameters
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10

# Enhanced filtering
VELOCITY_CONFIRM_THRESHOLD = 0.10
ENHANCED_SCORE_THRESHOLD = 0.005

# Loser bid calculation
DROP_MULTIPLIER = 0.50
DROP_INTERCEPT = 0.08
DROP_REGIME_BONUS = {'LOW': 0.0, 'MEDIUM': 0.01, 'HIGH': 0.02}
TARGET_PAIR_COST = 0.99

# Capital
AVG_PAIR_COST = 0.99

# Z-score volatility window (for computing EWMA volatility)
ZSCORE_EWMA_HALFLIFE_TICKS = 300  # 5 seconds at 60Hz


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class OUParams:
    """OU parameters for z-score computation."""
    mu: float
    theta: float
    xi: float
    sigma_stat: float
    half_life_sec: float
    n_samples: int = 0
    dt_seconds: float = 1/60
    estimation_timestamp: float = 0.0

    @classmethod
    def load(cls, filepath: str) -> "OUParams":
        """Load from JSON file."""
        with open(filepath, 'r') as f:
            data = json.load(f)
        return cls(**data)


@dataclass
class TradeWithZScore:
    """Trade result with z-score at entry."""
    market_slug: str
    cycle_num: int
    entry_time_remaining: float
    signal_score: float
    winner_side: str
    winner_fill_price: float
    loser_fill_price: float
    hedge_type: str
    pair_cost: float
    pnl: float
    pnl_gross: float
    entry_fee: float
    hedge_fee: float
    correct_direction: bool
    spike_magnitude: float
    shares_filled: int
    entry_ts: int
    exit_ts: Optional[int] = None
    zscore_at_entry: float = 0.0
    regime_at_entry: str = "UNKNOWN"


@dataclass
class BacktestConfig:
    """Configuration for backtest."""
    target_shares: int = 50
    grid_levels: int = 1
    grid_spacing: float = 0.01
    spike_lookback: int = 84  # ticks at 60Hz (1400ms)
    stop_loss_pct: Optional[float] = 0.12
    order_pulling: bool = False
    ewma_fast_halflife: float = 60.0
    ewma_slow_halflife: float = 300.0
    entry_order_pull_timeout: float = 10.0
    hedge_ratio: float = 1.0
    aggressive_hedge_timeout: Optional[float] = None
    grid_buycount: int = 1
    use_cycling: bool = False
    time_stop_seconds: Optional[float] = None  # Time-based stop: exit after N seconds if hedge not filled

    @property
    def order_size_per_level(self) -> int:
        return self.target_shares // self.grid_levels

    @property
    def lookback_ms(self) -> int:
        return int(self.spike_lookback * 1000 / 60)


# =============================================================================
# EWMA THRESHOLD TRACKER
# =============================================================================

class EWMAThresholdTracker:
    """Pure EWMA-based adaptive threshold."""

    def __init__(self, fast_halflife_sec: float = 60, slow_halflife_sec: float = 300,
                 tick_interval_sec: float = 1/60):
        self.fast_halflife = fast_halflife_sec
        self.slow_halflife = slow_halflife_sec
        self.tick_interval = tick_interval_sec

        self.fast_alpha = 1 - math.exp(-math.log(2) * tick_interval_sec / fast_halflife_sec)
        self.slow_alpha = 1 - math.exp(-math.log(2) * tick_interval_sec / slow_halflife_sec)

        self.fast_var = None
        self.slow_var = None
        self.last_price = None

    def update(self, price: float) -> float:
        """Update with new price, return adaptive threshold."""
        if self.last_price is None:
            self.last_price = price
            return EWMA_BASE_THRESHOLD

        ret = (price - self.last_price) / self.last_price
        ret_sq = ret * ret
        self.last_price = price

        if self.fast_var is None:
            self.fast_var = ret_sq
            self.slow_var = ret_sq
            return EWMA_BASE_THRESHOLD

        self.fast_var = self.fast_alpha * ret_sq + (1 - self.fast_alpha) * self.fast_var
        self.slow_var = self.slow_alpha * ret_sq + (1 - self.slow_alpha) * self.slow_var

        slow_vol = math.sqrt(max(self.slow_var, 1e-12))
        fast_vol = math.sqrt(max(self.fast_var, 1e-12))
        ratio = fast_vol / slow_vol if slow_vol > 1e-8 else 1.0
        ratio = max(EWMA_MIN_RATIO, min(EWMA_MAX_RATIO, ratio))

        threshold = EWMA_BASE_THRESHOLD * ratio
        return max(EWMA_MIN_THRESHOLD, min(EWMA_MAX_THRESHOLD, threshold))

    def get_current_volatility(self) -> float:
        """Get current fast volatility."""
        if self.fast_var is None:
            return 0.0
        return math.sqrt(self.fast_var)


def compute_ou_threshold(volatility: float, ou_params: OUParams) -> float:
    """
    Compute OU-based adaptive threshold using z-score sigmoid mapping.

    Maps current volatility to a threshold using the OU process parameters.
    Lower vol -> lower threshold (more selective)
    Higher vol -> higher threshold (more permissive)
    """
    vol = max(volatility, 1e-10)
    log_vol = math.log(vol)
    z_score = (log_vol - ou_params.mu) / ou_params.sigma_stat

    # Sigmoid mapping: z-score to multiplier
    z_clamped = max(-10, min(10, z_score * OU_SIGMOID_STEEPNESS))
    sigmoid = 1.0 / (1.0 + math.exp(-z_clamped))
    multiplier = OU_K_LOW + (OU_K_HIGH - OU_K_LOW) * sigmoid

    threshold = OU_BASE_THRESHOLD * multiplier
    return max(OU_MIN_THRESHOLD, min(OU_MAX_THRESHOLD, threshold))


# =============================================================================
# DATA LOADING
# =============================================================================

def load_ou_params(filepath: str = "research/ou_params.json") -> OUParams:
    """Load OU parameters from JSON."""
    path = Path(filepath)
    if not path.exists():
        path = Path("/Users/rananjaybika/polymarket-amm-bot") / filepath
    return OUParams.load(str(path))


def load_btc_data(btc_dir: str = "research/binance_hf",
                  start_ts: Optional[int] = None,
                  end_ts: Optional[int] = None) -> pd.DataFrame:
    """Load BTC price data, preferring combined file if available."""
    data_path = Path(btc_dir)
    if not data_path.exists():
        data_path = Path("/Users/rananjaybika/polymarket-amm-bot") / btc_dir

    # Try combined file first (faster)
    combined_file = data_path / "btc_prices_combined.csv"
    if combined_file.exists():
        print(f"  Loading combined file: {combined_file.name}")
        btc_df = pd.read_csv(combined_file)
        print(f"  Loaded {len(btc_df):,} rows")
    else:
        # Fall back to individual files
        dfs = []
        for f in sorted(data_path.glob("btc_prices_*.csv")):
            df = pd.read_csv(f)
            dfs.append(df)
            print(f"  Loaded {len(df):,} rows from {f.name}")
        btc_df = pd.concat(dfs, ignore_index=True)

    btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')

    if start_ts:
        btc_df = btc_df[btc_df['timestamp_ms'] >= start_ts]
    if end_ts:
        btc_df = btc_df[btc_df['timestamp_ms'] <= end_ts]

    print(f"  BTC TOTAL: {len(btc_df):,} rows")
    return btc_df.reset_index(drop=True)


def resample_to_seconds(df: pd.DataFrame, interval_seconds: int = 1) -> pd.DataFrame:
    """Resample high-frequency data to specified time intervals."""
    df = df.copy()
    interval_ms = interval_seconds * 1000
    df['bucket'] = df['timestamp_ms'] // interval_ms
    resampled = df.groupby('bucket').agg({
        'timestamp_ms': 'last',
        'price': 'last'
    }).reset_index(drop=True)
    return resampled


def compute_zscore_series(btc_df: pd.DataFrame, ou_params: OUParams,
                          ewma_window: int = 60,
                          resample_interval_s: int = 60,
                          zscore_method: str = "ou") -> pd.DataFrame:
    """
    Compute z-score for every timestamp (at resampled frequency).

    Args:
        btc_df: Raw BTC price data (60Hz)
        ou_params: Calibrated OU parameters (only used if zscore_method="ou")
        ewma_window: EWMA window in samples (default 60)
        resample_interval_s: Resample interval in seconds (default 1)
        zscore_method: How to compute z-score:
            - "ou": Use pre-calibrated OU params (static μ, σ)
            - "ewma": Fully adaptive EWMA-based z-score (rolling μ, σ)
            - "percentile": Simple percentile rank (no z-score, just 0-100)

    Returns:
        DataFrame with timestamp_ms, price, volatility, zscore, regime
    """
    # Resample to match OU calibration frequency
    print(f"  Resampling to {resample_interval_s}s intervals...")
    df = resample_to_seconds(btc_df, resample_interval_s)
    print(f"  Resampled from {len(btc_df):,} to {len(df):,} rows")

    # Compute percentage returns (matching OU calibration)
    prices = df['price'].values
    returns = np.zeros(len(prices))
    returns[1:] = np.diff(prices) / prices[:-1] * 100  # Percentage returns

    # Filter extreme returns (data gaps)
    returns = np.where(np.abs(returns) > 5.0, 0.0, returns)

    # Compute rolling EWMA volatility
    alpha = 2.0 / (ewma_window + 1)
    variance = np.var(returns[:ewma_window]) if len(returns) >= ewma_window else np.var(returns)
    volatilities = []

    for i, r in enumerate(returns):
        if i < ewma_window:
            # Warm-up: use expanding window std
            vol = np.std(returns[:i+1]) if i > 0 else np.sqrt(variance)
        else:
            # EWMA update
            variance = alpha * (r ** 2) + (1 - alpha) * variance
            vol = np.sqrt(variance)
        vol = max(vol, 1e-6)
        volatilities.append(vol)

    df['volatility'] = volatilities
    df['log_vol'] = np.log(df['volatility'].clip(lower=1e-10))

    # Compute z-score based on method
    if zscore_method == "ou":
        # Use pre-calibrated OU parameters (static)
        df['zscore'] = (df['log_vol'] - ou_params.mu) / ou_params.sigma_stat
        print(f"  Z-score method: OU (static μ={ou_params.mu:.4f}, σ={ou_params.sigma_stat:.4f})")

    elif zscore_method == "ewma":
        # Fully adaptive: rolling EWMA of mean and std of log_vol
        # Use 5-minute (300 samples at 1s) for fast adaptation
        ewma_zscore_window = 300
        alpha_z = 2.0 / (ewma_zscore_window + 1)

        log_vols = df['log_vol'].values
        zscores = np.zeros(len(log_vols))

        # Initialize with first window
        ewma_mean = np.mean(log_vols[:ewma_zscore_window]) if len(log_vols) >= ewma_zscore_window else log_vols[0]
        ewma_var = np.var(log_vols[:ewma_zscore_window]) if len(log_vols) >= ewma_zscore_window else 0.1

        for i, lv in enumerate(log_vols):
            if i < ewma_zscore_window:
                # Warm-up: use expanding window
                if i > 0:
                    window_mean = np.mean(log_vols[:i+1])
                    window_std = np.std(log_vols[:i+1])
                    zscores[i] = (lv - window_mean) / max(window_std, 0.01)
                else:
                    zscores[i] = 0.0
            else:
                # EWMA update for mean and variance
                ewma_mean = alpha_z * lv + (1 - alpha_z) * ewma_mean
                ewma_var = alpha_z * ((lv - ewma_mean) ** 2) + (1 - alpha_z) * ewma_var
                ewma_std = max(np.sqrt(ewma_var), 0.01)
                zscores[i] = (lv - ewma_mean) / ewma_std

        df['zscore'] = zscores
        final_mean = ewma_mean
        final_std = np.sqrt(ewma_var)
        print(f"  Z-score method: EWMA (adaptive, final μ={final_mean:.4f}, σ={final_std:.4f})")

    elif zscore_method == "percentile":
        # Simple percentile rank over rolling window
        percentile_window = 300  # 5 minutes
        log_vols = df['log_vol'].values
        percentiles = np.zeros(len(log_vols))

        for i in range(len(log_vols)):
            if i < percentile_window:
                window = log_vols[:i+1]
            else:
                window = log_vols[i-percentile_window+1:i+1]

            # Percentile rank: what fraction of window is below current value
            pct = np.sum(window < log_vols[i]) / len(window) * 100
            # Convert to z-score-like scale: 50th percentile -> 0, 97.5th -> 2, 2.5th -> -2
            from scipy import stats
            percentiles[i] = stats.norm.ppf(max(0.001, min(0.999, pct / 100)))

        df['zscore'] = percentiles
        print(f"  Z-score method: Percentile (rolling {percentile_window}s window)")

    elif zscore_method == "ewma_ratio":
        # EWMA ratio z-score: z-score of log(fast_vol / slow_vol)
        # This captures "is volatility spiking relative to recent baseline?"
        fast_halflife = 60   # 1 minute (in seconds, since resampled to 1s)
        slow_halflife = 300  # 5 minutes
        zscore_window = 300  # 5 minute lookback for z-score normalization

        # Compute fast and slow EWMA volatilities
        alpha_fast = 2.0 / (fast_halflife + 1)
        alpha_slow = 2.0 / (slow_halflife + 1)

        log_vols = df['log_vol'].values
        n = len(log_vols)

        # Initialize
        fast_vol = np.exp(log_vols[0]) if n > 0 else 0.01
        slow_vol = np.exp(log_vols[0]) if n > 0 else 0.01

        log_ratios = np.zeros(n)
        zscores = np.zeros(n)

        # Compute EWMA of actual volatility (not log_vol) for ratio
        vols = df['volatility'].values

        for i in range(n):
            if i == 0:
                fast_vol = vols[i]
                slow_vol = vols[i]
            else:
                fast_vol = alpha_fast * vols[i] + (1 - alpha_fast) * fast_vol
                slow_vol = alpha_slow * vols[i] + (1 - alpha_slow) * slow_vol

            # Ratio: fast / slow (> 1 means vol spiking, < 1 means vol dropping)
            ratio = fast_vol / max(slow_vol, 1e-10)
            # Clamp ratio to reasonable range
            ratio = max(0.1, min(10.0, ratio))
            log_ratios[i] = np.log(ratio)

        # Now compute z-score of log_ratios using rolling window
        alpha_z = 2.0 / (zscore_window + 1)
        ewma_mean = 0.0  # log(1) = 0 is neutral
        ewma_var = 0.01

        for i in range(n):
            if i < zscore_window:
                # Warm-up: use expanding window
                if i > 0:
                    window_mean = np.mean(log_ratios[:i+1])
                    window_std = np.std(log_ratios[:i+1])
                    zscores[i] = (log_ratios[i] - window_mean) / max(window_std, 0.01)
                else:
                    zscores[i] = 0.0
            else:
                # EWMA update for mean and variance
                ewma_mean = alpha_z * log_ratios[i] + (1 - alpha_z) * ewma_mean
                ewma_var = alpha_z * ((log_ratios[i] - ewma_mean) ** 2) + (1 - alpha_z) * ewma_var
                ewma_std = max(np.sqrt(ewma_var), 0.01)
                zscores[i] = (log_ratios[i] - ewma_mean) / ewma_std

        df['zscore'] = zscores
        df['log_ratio'] = log_ratios  # Store for debugging
        final_mean = ewma_mean
        final_std = np.sqrt(ewma_var)
        print(f"  Z-score method: EWMA Ratio (fast={fast_halflife}s, slow={slow_halflife}s)")
        print(f"    Final log_ratio stats: μ={final_mean:.4f}, σ={final_std:.4f}")

    else:
        raise ValueError(f"Unknown zscore_method: {zscore_method}")

    # Classify regime based on z-score
    def classify_regime(z):
        if pd.isna(z):
            return "UNKNOWN"
        if z < -1.0:
            return "LOW"
        elif z < 1.0:
            return "MEDIUM"
        elif z < 2.0:
            return "HIGH"
        else:
            return "EXTREME"

    df['regime'] = df['zscore'].apply(classify_regime)

    return df[['timestamp_ms', 'price', 'volatility', 'log_vol', 'zscore', 'regime']]


def load_observer_data(obs_dir: str = "research/observer",
                       start_ts: Optional[int] = None,
                       end_ts: Optional[int] = None) -> Tuple[pd.DataFrame, Dict[str, str]]:
    """Load observer data and resolutions."""
    data_path = Path(obs_dir)
    if not data_path.exists():
        data_path = Path("/Users/rananjaybika/polymarket-amm-bot") / obs_dir

    # Load observer CSVs
    dfs = []
    for f in sorted(data_path.glob("grid_obs_*.csv")):
        df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
        dfs.append(df)

    obs_df = pd.concat(dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])

    if start_ts:
        obs_df = obs_df[obs_df['timestamp_ms'] >= start_ts]
    if end_ts:
        obs_df = obs_df[obs_df['timestamp_ms'] <= end_ts]

    print(f"  Observer: {len(obs_df):,} rows")

    # Load resolutions
    res_path = data_path / "market_resolutions_verified.csv"
    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    return obs_df, res_map


# =============================================================================
# SPIKE DETECTION
# =============================================================================

def detect_spikes(btc_df: pd.DataFrame, lookback: int,
                  ewma_fast_halflife: float = 60.0,
                  ewma_slow_halflife: float = 300.0,
                  method: str = "ewma",
                  ou_params: Optional[OUParams] = None) -> pd.DataFrame:
    """
    Detect spikes using adaptive thresholds.

    Args:
        btc_df: BTC price data
        lookback: Lookback period in ticks
        ewma_fast_halflife: EWMA fast halflife in seconds (for method="ewma")
        ewma_slow_halflife: EWMA slow halflife in seconds (for method="ewma")
        method: Threshold method - "ewma" (adaptive EWMA ratio) or "ou" (OU z-score sigmoid)
        ou_params: OU parameters (required if method="ou")

    Returns:
        DataFrame with spike detection results
    """
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    # Calculate % change over lookback period
    df['price_prev'] = df['price'].shift(lookback)
    df['change_pct'] = (df['price'] - df['price_prev']) / df['price_prev'] * 100
    df['magnitude'] = df['change_pct'].abs()

    # Calculate adaptive threshold based on method
    if method == "ou":
        if ou_params is None:
            raise ValueError("ou_params required for method='ou'")

        # Use EWMA to compute volatility, then apply OU z-score sigmoid
        tracker = EWMAThresholdTracker(
            fast_halflife_sec=ewma_fast_halflife,
            slow_halflife_sec=ewma_slow_halflife
        )
        thresholds = []
        for price in df['price'].values:
            # Update tracker to compute volatility (we use its volatility, not its threshold)
            tracker.update(price)
            vol = tracker.get_current_volatility()
            # Convert to percentage (matching OU calibration)
            vol_pct = vol * 100 if vol > 0 else 0.01
            threshold = compute_ou_threshold(vol_pct, ou_params)
            thresholds.append(threshold)
    else:
        # EWMA method: use fast/slow ratio directly
        tracker = EWMAThresholdTracker(
            fast_halflife_sec=ewma_fast_halflife,
            slow_halflife_sec=ewma_slow_halflife
        )
        thresholds = []
        for price in df['price'].values:
            threshold = tracker.update(price)
            thresholds.append(threshold)

    df['threshold'] = thresholds
    df['spike_detected'] = df['magnitude'] >= df['threshold']

    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['change_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['change_pct'] < 0), 'spike_direction'] = 'DOWN'
    df['spike_magnitude'] = df['magnitude'].where(df['spike_detected'], 0)

    return df[['timestamp_ms', 'price', 'spike_detected', 'spike_direction',
               'spike_magnitude', 'threshold']]


# =============================================================================
# FEE CALCULATIONS
# =============================================================================

def get_taker_fee_rate(price: float) -> float:
    """Calculate taker fee rate based on Polymarket formula."""
    return 0.0156 * (1 - abs(2 * price - 1))


def calculate_taker_fee(price: float, shares: int) -> float:
    """Calculate total taker fee for a trade."""
    fee_rate = get_taker_fee_rate(price)
    return fee_rate * price * shares


# =============================================================================
# BACKTEST WITH Z-SCORE TRACKING
# =============================================================================

def velocity_confirms(spike_dir: str, velocity_bps: float) -> bool:
    """Check if velocity confirms spike direction."""
    if spike_dir == "UP":
        return velocity_bps > -VELOCITY_CONFIRM_THRESHOLD
    elif spike_dir == "DOWN":
        return velocity_bps < VELOCITY_CONFIRM_THRESHOLD
    return True


def compute_score(spike_mag: float, velocity_bps: float, spike_dir: str,
                  time_rem: float, regime: str = "MEDIUM") -> float:
    """Compute enhanced signal score."""
    if regime == "LOW":
        return 0.0
    regime_weight = 1.2 if regime == "HIGH" else 1.0

    if 300 <= time_rem <= 600:
        time_weight = 1.0
    elif 180 <= time_rem <= 750:
        time_weight = 0.6
    else:
        time_weight = 0.3

    interaction = spike_mag * abs(velocity_bps)
    return interaction * time_weight * regime_weight


def calc_loser_bid(winner_entry: float, spike_mag: float, regime: str = "MEDIUM") -> float:
    """Calculate loser side bid price."""
    expected_drop = DROP_MULTIPLIER * spike_mag + DROP_INTERCEPT
    expected_drop += DROP_REGIME_BONUS.get(regime, 0.0)
    loser_bid = 1.0 - winner_entry - expected_drop
    loser_bid = max(0.01, min(0.48, loser_bid))
    return loser_bid


def simulate_market_with_zscore(
    spikes_df: pd.DataFrame,
    obs_df: pd.DataFrame,
    zscore_df: pd.DataFrame,
    slug: str,
    resolution: str,
    config: BacktestConfig,
) -> List[TradeWithZScore]:
    """Simulate trading on a single market, tracking z-score per trade."""
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    # Get spikes in this market's time range
    market_spikes = spikes_df[
        (spikes_df['timestamp_ms'] >= market_start) &
        (spikes_df['timestamp_ms'] <= market_end)
    ].copy()

    trades = []
    cycle_num = 0
    last_trade_ts = 0

    for i, row in mdf.iterrows():
        time_rem = row['time_remaining_secs']
        if time_rem < MIN_TIME:
            continue

        ts = row['timestamp_ms']
        if config.use_cycling and (ts - last_trade_ts) < MIN_CYCLE_GAP_MS:
            continue

        # Look for spike signal
        recent_spikes = market_spikes[
            (market_spikes['timestamp_ms'] <= ts) &
            (market_spikes['timestamp_ms'] > ts - config.lookback_ms)
        ]

        if recent_spikes.empty or not recent_spikes['spike_detected'].any():
            continue

        spike_row = recent_spikes[recent_spikes['spike_detected']].iloc[-1]
        spike_dir = spike_row['spike_direction']
        spike_mag = spike_row['spike_magnitude']

        if spike_dir not in ['UP', 'DOWN']:
            continue

        # Check velocity confirmation
        velocity_bps = row.get('velocity_bps', 0.0)
        if pd.isna(velocity_bps):
            velocity_bps = 0.0

        if not velocity_confirms(spike_dir, velocity_bps):
            continue

        # Compute score and check threshold
        signal_score = compute_score(spike_mag, velocity_bps, spike_dir, time_rem)
        if signal_score < ENHANCED_SCORE_THRESHOLD:
            continue

        # Get z-score at entry time (z-score is at 1s intervals, find nearest)
        zscore_at_entry = 0.0
        regime_at_entry = "UNKNOWN"
        zscore_rows = zscore_df[zscore_df['timestamp_ms'] <= ts]
        if not zscore_rows.empty:
            zscore_at_entry = zscore_rows.iloc[-1]['zscore']
            regime_at_entry = zscore_rows.iloc[-1]['regime']
            if pd.isna(zscore_at_entry):
                zscore_at_entry = 0.0
                regime_at_entry = "UNKNOWN"

        # Entry pricing
        winner_side = spike_dir
        if winner_side == "UP":
            winner_entry = row.get('up_ask', 0.50) if pd.notna(row.get('up_ask')) else 0.50
        else:
            winner_entry = row.get('down_ask', 0.50) if pd.notna(row.get('down_ask')) else 0.50

        winner_entry = max(0.01, min(0.99, winner_entry))

        # Calculate loser bid
        loser_bid = calc_loser_bid(winner_entry, spike_mag, regime_at_entry)
        pair_cost = winner_entry + loser_bid

        # Entry fee (taker)
        entry_fee = calculate_taker_fee(winner_entry, config.target_shares)

        # Simulate hedge outcome - scan forward for fill or stop
        hedge_type = "resolution"
        exit_ts = None
        loser_fill = loser_bid
        hedge_fee = 0.0

        # Simulate hedge outcome - scan forward for passive fill, stop-loss, or resolution
        # Order matters: check passive fill FIRST (matches original optimizer)
        for j in range(i + 1, len(mdf)):
            future_row = mdf.iloc[j]
            future_ts = future_row['timestamp_ms']
            future_time_rem = future_row['time_remaining_secs']

            if future_time_rem < 5:  # Near resolution
                hedge_type = "resolution"
                exit_ts = future_ts
                break

            # Get current prices for both sides
            if winner_side == "UP":
                current_loser_ask = future_row.get('down_ask', 1.0)
                current_winner_bid = future_row.get('up_bid', 0.50)
            else:
                current_loser_ask = future_row.get('up_ask', 1.0)
                current_winner_bid = future_row.get('down_bid', 0.50)

            # FIX Bug 3: Check passive fill FIRST (before stop-loss)
            if pd.notna(current_loser_ask) and current_loser_ask <= loser_bid:
                hedge_type = "passive"
                loser_fill = loser_bid
                exit_ts = future_ts
                break

            # TIME-BASED STOP: Exit after N seconds if hedge not filled AND not in profit
            # Only exit if we're losing - if winning, let it ride until passive fill or resolution
            if config.time_stop_seconds is not None:
                elapsed_seconds = (future_ts - ts) / 1000.0
                if elapsed_seconds >= config.time_stop_seconds:
                    # Check if we're in profit (winner price >= entry)
                    in_profit = pd.notna(current_winner_bid) and current_winner_bid >= winner_entry
                    if not in_profit:
                        # Only time-stop if NOT in profit
                        hedge_type = "timestop"
                        loser_fill = current_loser_ask if pd.notna(current_loser_ask) else loser_bid
                        exit_ts = future_ts
                        hedge_fee = calculate_taker_fee(loser_fill, config.target_shares)
                        break
                    # If in profit, skip time-stop and let it ride

            # FIX Bug 2: Stop-loss checks WINNER price drop (not loser price rise)
            if config.stop_loss_pct is not None and pd.notna(current_winner_bid):
                drop = (winner_entry - current_winner_bid) / winner_entry
                if drop >= config.stop_loss_pct:
                    hedge_type = "stoploss"
                    loser_fill = current_loser_ask if pd.notna(current_loser_ask) else loser_bid
                    exit_ts = future_ts
                    hedge_fee = calculate_taker_fee(loser_fill, config.target_shares)
                    break

        # Calculate PnL
        # Winner side: correct direction = payout, wrong = 0
        correct_direction = (winner_side == resolution)

        if hedge_type == "resolution":
            # Held to resolution
            if correct_direction:
                # Direction correct: winner pays $1, we keep (1 - winner_entry)
                # Loser goes to $0, we never bought it (no hedge completed)
                # But we aimed to hedge at loser_bid, so effective pair_cost matters
                pnl_gross = (1.0 - pair_cost) * config.target_shares
            else:
                # FIX Bug 1: Wrong direction - only lose winner entry cost
                # Winner goes to $0, loser goes to $1 (but we don't hold loser)
                pnl_gross = -winner_entry * config.target_shares
            hedge_fee = 0.0
        else:
            # Hedged out (passive or stop-loss)
            merge_value = winner_entry + loser_fill
            pnl_gross = (1.0 - merge_value) * config.target_shares

        pnl = pnl_gross - entry_fee - hedge_fee

        trade = TradeWithZScore(
            market_slug=slug,
            cycle_num=cycle_num,
            entry_time_remaining=time_rem,
            signal_score=signal_score,
            winner_side=winner_side,
            winner_fill_price=winner_entry,
            loser_fill_price=loser_fill,
            hedge_type=hedge_type,
            pair_cost=pair_cost,
            pnl=pnl,
            pnl_gross=pnl_gross,
            entry_fee=entry_fee,
            hedge_fee=hedge_fee,
            correct_direction=correct_direction,
            spike_magnitude=spike_mag,
            shares_filled=config.target_shares,
            entry_ts=ts,
            exit_ts=exit_ts,
            zscore_at_entry=zscore_at_entry,
            regime_at_entry=regime_at_entry,
        )
        trades.append(trade)

        cycle_num += 1
        # Use EXIT time for proper cycling (not entry time)
        last_trade_ts = exit_ts if exit_ts else ts

        if not config.use_cycling:
            break

    return trades


def run_backtest_with_zscore(
    config: BacktestConfig,
    btc_df: pd.DataFrame,
    obs_df: pd.DataFrame,
    zscore_df: pd.DataFrame,
    res_map: Dict[str, str],
    method: str = "ewma",
    ou_params: Optional[OUParams] = None,
    quiet: bool = False,
) -> List[TradeWithZScore]:
    """Run full backtest across all markets, tracking z-score per trade."""
    if not quiet:
        print(f"\nRunning backtest with z-score tracking...")
        print(f"  Config: {config.spike_lookback} ticks ({config.lookback_ms}ms), "
              f"{config.target_shares} shares, SL={config.stop_loss_pct}")
        print(f"  Method: {method}")

    # Detect spikes
    spikes_df = detect_spikes(
        btc_df,
        config.spike_lookback,
        config.ewma_fast_halflife,
        config.ewma_slow_halflife,
        method=method,
        ou_params=ou_params
    )

    # Add resolution to observer data
    obs_df = obs_df.copy()
    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    # Get valid markets
    valid_slugs = []
    for slug, mdf in obs_df.groupby('market_slug'):
        max_time = mdf['time_remaining_secs'].max()
        min_time = mdf['time_remaining_secs'].min()
        duration = max_time - min_time
        if duration >= MIN_RUNTIME_SECS and max_time >= 840:
            valid_slugs.append(slug)

    if not quiet:
        print(f"  Valid markets: {len(valid_slugs)}")

    # Run backtest on each market
    all_trades = []
    for slug in valid_slugs:
        resolution = res_map.get(slug)
        if resolution not in ['UP', 'DOWN']:
            continue

        trades = simulate_market_with_zscore(
            spikes_df, obs_df, zscore_df, slug, resolution, config
        )
        all_trades.extend(trades)

    if not quiet:
        print(f"  Total trades: {len(all_trades)}")
    return all_trades


# =============================================================================
# CUTOFF ANALYSIS
# =============================================================================

def estimate_active_hours(trades: List[TradeWithZScore], total_hours: float,
                          zscore_df: pd.DataFrame, cutoff: float) -> float:
    """
    Estimate hours active when filtering by z-score cutoff.

    Computes fraction of time where z-score < cutoff.
    """
    if cutoff >= 10.0:  # No filter
        return total_hours

    valid_zscores = zscore_df[zscore_df['zscore'].notna()]
    below_cutoff = valid_zscores[valid_zscores['zscore'] < cutoff]
    fraction = len(below_cutoff) / len(valid_zscores) if len(valid_zscores) > 0 else 1.0

    return total_hours * fraction


def estimate_active_hours_zone(total_hours: float, zscore_df: pd.DataFrame,
                               z_lo: Optional[float], z_hi: Optional[float]) -> float:
    """
    Estimate hours active when filtering by z-score zone (dual bounds).

    Args:
        total_hours: Total hours in dataset
        zscore_df: DataFrame with z-scores
        z_lo: Lower bound (exclusive). None means no lower bound.
        z_hi: Upper bound (exclusive). None means no upper bound.

    Returns:
        Hours where z_lo < z-score < z_hi
    """
    if z_lo is None and z_hi is None:
        return total_hours

    valid_zscores = zscore_df[zscore_df['zscore'].notna()]
    if len(valid_zscores) == 0:
        return total_hours

    mask = pd.Series([True] * len(valid_zscores), index=valid_zscores.index)

    if z_lo is not None:
        mask &= valid_zscores['zscore'] > z_lo
    if z_hi is not None:
        mask &= valid_zscores['zscore'] < z_hi

    in_zone = valid_zscores[mask]
    fraction = len(in_zone) / len(valid_zscores)

    return total_hours * fraction


def analyze_cutoffs(
    trades: List[TradeWithZScore],
    total_hours: float,
    zscore_df: pd.DataFrame,
) -> List[Dict]:
    """Sweep z-score cutoffs and report PnL impact."""
    cutoffs = [float('inf'), 3.0, 2.5, 2.0, 1.75, 1.5, 1.25, 1.0, 0.5, 0.0]

    results = []
    for cutoff in cutoffs:
        if cutoff == float('inf'):
            kept = trades
            skipped = []
            cutoff_label = "No limit"
        else:
            kept = [t for t in trades if t.zscore_at_entry < cutoff]
            skipped = [t for t in trades if t.zscore_at_entry >= cutoff]
            cutoff_label = f"z < {cutoff}"

        hours_active = estimate_active_hours(trades, total_hours, zscore_df, cutoff)

        pnl_kept = sum(t.pnl for t in kept)
        pnl_skipped = sum(t.pnl for t in skipped)
        hourly_rate = pnl_kept / hours_active if hours_active > 0 else 0

        # Direction accuracy
        correct_kept = sum(1 for t in kept if t.correct_direction)
        correct_skipped = sum(1 for t in skipped if t.correct_direction)
        dir_acc_kept = correct_kept / len(kept) * 100 if kept else 0
        dir_acc_skipped = correct_skipped / len(skipped) * 100 if skipped else 0

        # Win rate (profitable trades)
        wins_kept = sum(1 for t in kept if t.pnl > 0)
        wins_skipped = sum(1 for t in skipped if t.pnl > 0)
        win_rate_kept = wins_kept / len(kept) * 100 if kept else 0
        win_rate_skipped = wins_skipped / len(skipped) * 100 if skipped else 0

        pct_time_out = (total_hours - hours_active) / total_hours * 100 if total_hours > 0 else 0

        results.append({
            'cutoff': cutoff,
            'cutoff_label': cutoff_label,
            'trades_kept': len(kept),
            'trades_skipped': len(skipped),
            'pnl_kept': pnl_kept,
            'pnl_skipped': pnl_skipped,
            'hourly_rate': hourly_rate,
            'pct_time_out': pct_time_out,
            'dir_acc_kept': dir_acc_kept,
            'dir_acc_skipped': dir_acc_skipped,
            'win_rate_kept': win_rate_kept,
            'win_rate_skipped': win_rate_skipped,
        })

    return results


# =============================================================================
# OUTPUT FORMATTING
# =============================================================================

def print_zscore_distribution(zscore_df: pd.DataFrame):
    """Print z-score distribution summary."""
    valid_z = zscore_df['zscore'].dropna()

    print("\nZ-Score Distribution:")
    print(f"  Mean: {valid_z.mean():.2f}, Std: {valid_z.std():.2f}")
    print(f"  Min: {valid_z.min():.2f}, Max: {valid_z.max():.2f}")

    pct_low = (valid_z < 0).mean() * 100
    pct_medium = ((valid_z >= 0) & (valid_z < 1.5)).mean() * 100
    pct_high = ((valid_z >= 1.5) & (valid_z < 2.5)).mean() * 100
    pct_extreme = (valid_z >= 2.5).mean() * 100

    print(f"  LOW (z < 0):       {pct_low:.1f}% of time")
    print(f"  MEDIUM (0-1.5):    {pct_medium:.1f}% of time")
    print(f"  HIGH (1.5-2.5):    {pct_high:.1f}% of time")
    print(f"  EXTREME (z > 2.5): {pct_extreme:.1f}% of time")


def print_analysis_table(results: List[Dict], config: BacktestConfig):
    """Print the cutoff analysis table."""
    print("\nZ-Score Cutoff Analysis:")
    print("=" * 100)

    # Header
    print(f"{'Cutoff':<12} {'Trades':<8} {'Skipped':<8} {'$/hr':<10} "
          f"{'Skip PnL':<12} {'% Out':<8} {'Dir Acc':<10} {'Skip Dir':<10}")
    print("-" * 100)

    # Find best hourly rate for highlighting
    best_idx = max(range(len(results)), key=lambda i: results[i]['hourly_rate'])

    for i, r in enumerate(results):
        marker = " <-- BEST" if i == best_idx and i > 0 else ""
        print(f"{r['cutoff_label']:<12} {r['trades_kept']:<8} {r['trades_skipped']:<8} "
              f"${r['hourly_rate']:<9.2f} ${r['pnl_skipped']:<11.2f} {r['pct_time_out']:<7.1f}% "
              f"{r['dir_acc_kept']:<9.1f}% {r['dir_acc_skipped']:<9.1f}%{marker}")

    print("=" * 100)


def analyze_by_market(trades: List[TradeWithZScore]) -> pd.DataFrame:
    """Analyze PnL breakdown by market to identify which markets to avoid."""
    if not trades:
        return pd.DataFrame()

    # Group by market
    market_stats = {}
    for t in trades:
        slug = t.market_slug
        if slug not in market_stats:
            market_stats[slug] = {
                'trades': 0,
                'pnl': 0.0,
                'correct': 0,
                'zscores': [],
                'spike_mags': [],
                'time_remaining': [],
            }
        market_stats[slug]['trades'] += 1
        market_stats[slug]['pnl'] += t.pnl
        market_stats[slug]['correct'] += 1 if t.correct_direction else 0
        market_stats[slug]['zscores'].append(t.zscore_at_entry)
        market_stats[slug]['spike_mags'].append(t.spike_magnitude)
        market_stats[slug]['time_remaining'].append(t.entry_time_remaining)

    # Convert to DataFrame
    rows = []
    for slug, stats in market_stats.items():
        rows.append({
            'market': slug,
            'trades': stats['trades'],
            'pnl': stats['pnl'],
            'pnl_per_trade': stats['pnl'] / stats['trades'] if stats['trades'] > 0 else 0,
            'dir_acc': stats['correct'] / stats['trades'] * 100 if stats['trades'] > 0 else 0,
            'avg_zscore': np.mean(stats['zscores']),
            'avg_spike_mag': np.mean(stats['spike_mags']),
            'avg_time_rem': np.mean(stats['time_remaining']),
        })

    df = pd.DataFrame(rows)
    df = df.sort_values('pnl', ascending=True)  # Worst first
    return df


def print_market_analysis(trades: List[TradeWithZScore]):
    """Print per-market PnL analysis."""
    df = analyze_by_market(trades)
    if df.empty:
        return

    print("\n" + "=" * 100)
    print("PER-MARKET PnL ANALYSIS")
    print("=" * 100)

    # Summary stats
    winners = df[df['pnl'] > 0]
    losers = df[df['pnl'] < 0]
    breakeven = df[df['pnl'] == 0]

    print(f"\nMarket Summary:")
    print(f"  Winners:   {len(winners)} markets (${winners['pnl'].sum():.2f} total)")
    print(f"  Losers:    {len(losers)} markets (${losers['pnl'].sum():.2f} total)")
    print(f"  Breakeven: {len(breakeven)} markets")

    # Show worst markets
    print(f"\nWORST MARKETS (biggest losers):")
    print("-" * 100)
    print(f"{'Market':<50} {'Trades':<8} {'PnL':<10} {'$/Trade':<10} {'DirAcc':<8} {'AvgZ':<8}")
    print("-" * 100)
    for _, row in df.head(10).iterrows():
        print(f"{row['market'][:48]:<50} {row['trades']:<8} ${row['pnl']:<9.2f} ${row['pnl_per_trade']:<9.2f} {row['dir_acc']:<7.1f}% {row['avg_zscore']:<7.2f}")

    # Show best markets
    print(f"\nBEST MARKETS (biggest winners):")
    print("-" * 100)
    for _, row in df.tail(10).iloc[::-1].iterrows():
        print(f"{row['market'][:48]:<50} {row['trades']:<8} ${row['pnl']:<9.2f} ${row['pnl_per_trade']:<9.2f} {row['dir_acc']:<7.1f}% {row['avg_zscore']:<7.2f}")

    # What if we skip the worst N markets?
    print(f"\n" + "=" * 100)
    print("WHAT-IF: Skip Worst Markets")
    print("=" * 100)

    total_pnl = df['pnl'].sum()
    total_trades = df['trades'].sum()

    print(f"{'Skip worst N':<15} {'Markets left':<15} {'Trades left':<15} {'New PnL':<15} {'Change':<15}")
    print("-" * 75)

    for n_skip in [1, 2, 3, 5, 10, 15, 20]:
        if n_skip >= len(df):
            break
        remaining = df.iloc[n_skip:]
        new_pnl = remaining['pnl'].sum()
        new_trades = remaining['trades'].sum()
        change = new_pnl - total_pnl
        print(f"{n_skip:<15} {len(remaining):<15} {int(new_trades):<15} ${new_pnl:<14.2f} ${change:+.2f}")

    # Characteristics of losing vs winning markets
    print(f"\n" + "=" * 100)
    print("CHARACTERISTICS: Losing vs Winning Markets")
    print("=" * 100)

    if len(losers) > 0 and len(winners) > 0:
        print(f"{'Metric':<25} {'Losing Markets':<20} {'Winning Markets':<20}")
        print("-" * 65)
        print(f"{'Avg Z-Score':<25} {losers['avg_zscore'].mean():<20.2f} {winners['avg_zscore'].mean():<20.2f}")
        print(f"{'Avg Spike Magnitude':<25} {losers['avg_spike_mag'].mean():<20.4f} {winners['avg_spike_mag'].mean():<20.4f}")
        print(f"{'Avg Time Remaining':<25} {losers['avg_time_rem'].mean():<20.1f} {winners['avg_time_rem'].mean():<20.1f}")
        print(f"{'Avg Direction Acc':<25} {losers['dir_acc'].mean():<19.1f}% {winners['dir_acc'].mean():<19.1f}%")
        print(f"{'Avg Trades/Market':<25} {losers['trades'].mean():<20.1f} {winners['trades'].mean():<20.1f}")


def print_trade_characteristics(trades: List[TradeWithZScore]):
    """Analyze what trade characteristics predict winners vs losers."""
    if not trades:
        return

    print("\n" + "=" * 100)
    print("TRADE CHARACTERISTIC ANALYSIS: What predicts winners?")
    print("=" * 100)

    winners = [t for t in trades if t.pnl > 0]
    losers = [t for t in trades if t.pnl <= 0]

    if not winners or not losers:
        print("Not enough winners/losers to analyze")
        return

    print(f"\nWinners: {len(winners)} trades, Losers: {len(losers)} trades")
    print("-" * 80)

    # Compare characteristics
    metrics = [
        ('Z-Score at Entry', lambda t: t.zscore_at_entry),
        ('Spike Magnitude', lambda t: t.spike_magnitude),
        ('Time Remaining (s)', lambda t: t.entry_time_remaining),
        ('Signal Score', lambda t: t.signal_score),
        ('Winner Fill Price', lambda t: t.winner_fill_price),
        ('Pair Cost', lambda t: t.pair_cost),
    ]

    print(f"\n{'Metric':<25} {'Winners (mean)':<18} {'Losers (mean)':<18} {'Diff':<12} {'Signal':<10}")
    print("-" * 85)

    for name, getter in metrics:
        w_vals = [getter(t) for t in winners]
        l_vals = [getter(t) for t in losers]
        w_mean = np.mean(w_vals)
        l_mean = np.mean(l_vals)
        diff = w_mean - l_mean

        # Determine if this is a useful signal
        if abs(diff) > 0.1 * max(abs(w_mean), abs(l_mean), 0.01):
            signal = "YES" if diff > 0 else "YES (inv)"
        else:
            signal = "weak"

        print(f"{name:<25} {w_mean:<18.4f} {l_mean:<18.4f} {diff:<+12.4f} {signal:<10}")

    # Analyze by time remaining buckets
    print(f"\n" + "-" * 80)
    print("PnL BY TIME REMAINING:")
    print("-" * 80)

    time_buckets = [
        (60, 180, "1-3 min"),
        (180, 300, "3-5 min"),
        (300, 450, "5-7.5 min"),
        (450, 600, "7.5-10 min"),
        (600, 900, "10-15 min"),
    ]

    print(f"{'Time Bucket':<15} {'Trades':<10} {'PnL':<12} {'$/Trade':<12} {'DirAcc':<10}")
    print("-" * 60)

    for low, high, label in time_buckets:
        bucket_trades = [t for t in trades if low <= t.entry_time_remaining < high]
        if bucket_trades:
            pnl = sum(t.pnl for t in bucket_trades)
            correct = sum(1 for t in bucket_trades if t.correct_direction)
            dir_acc = correct / len(bucket_trades) * 100
            print(f"{label:<15} {len(bucket_trades):<10} ${pnl:<11.2f} ${pnl/len(bucket_trades):<11.2f} {dir_acc:<9.1f}%")

    # Analyze by spike magnitude buckets
    print(f"\n" + "-" * 80)
    print("PnL BY SPIKE MAGNITUDE:")
    print("-" * 80)

    spike_buckets = [
        (0, 0.02, "<2%"),
        (0.02, 0.03, "2-3%"),
        (0.03, 0.05, "3-5%"),
        (0.05, 0.10, "5-10%"),
        (0.10, 1.0, ">10%"),
    ]

    print(f"{'Spike Mag':<15} {'Trades':<10} {'PnL':<12} {'$/Trade':<12} {'DirAcc':<10}")
    print("-" * 60)

    for low, high, label in spike_buckets:
        bucket_trades = [t for t in trades if low <= t.spike_magnitude < high]
        if bucket_trades:
            pnl = sum(t.pnl for t in bucket_trades)
            correct = sum(1 for t in bucket_trades if t.correct_direction)
            dir_acc = correct / len(bucket_trades) * 100
            print(f"{label:<15} {len(bucket_trades):<10} ${pnl:<11.2f} ${pnl/len(bucket_trades):<11.2f} {dir_acc:<9.1f}%")


def recommend_cutoff(results: List[Dict]):
    """Recommend optimal cutoff based on analysis."""
    # Find cutoff that maximizes hourly rate with reasonable time in market
    best = None
    best_score = -float('inf')

    baseline_hourly = results[0]['hourly_rate']  # No limit

    for r in results[1:]:  # Skip "No limit"
        if r['pct_time_out'] > 50:  # Don't sit out more than 50% of time
            continue

        # Score: hourly rate improvement, penalized by time sitting out
        improvement = (r['hourly_rate'] - baseline_hourly) / abs(baseline_hourly) if baseline_hourly != 0 else 0
        time_penalty = r['pct_time_out'] / 100

        # Bonus if skipped trades are net negative
        skip_bonus = 1.0 if r['pnl_skipped'] < 0 else 0.5

        score = improvement * skip_bonus * (1 - time_penalty * 0.5)

        if score > best_score:
            best_score = score
            best = r

    print("\n" + "=" * 80)
    print("RECOMMENDATION")
    print("=" * 80)

    if best is None or best_score <= 0:
        print("No significant benefit from z-score filtering.")
        print("Recommendation: Trade through all volatility regimes")
    else:
        baseline = results[0]
        improvement = ((best['hourly_rate'] / baseline['hourly_rate']) - 1) * 100 if baseline['hourly_rate'] > 0 else 0

        print(f"Recommended cutoff: {best['cutoff_label']}")
        print(f"  - Sit out {best['pct_time_out']:.1f}% of time (extreme volatility)")
        print(f"  - Improve $/hr by {improvement:.1f}% "
              f"(${baseline['hourly_rate']:.2f} -> ${best['hourly_rate']:.2f})")
        print(f"  - Skipped trades net PnL: ${best['pnl_skipped']:.2f} "
              f"({'losers' if best['pnl_skipped'] < 0 else 'winners'})")
        print(f"  - Skipped trades direction accuracy: {best['dir_acc_skipped']:.1f}% "
              f"({'near random' if 45 < best['dir_acc_skipped'] < 55 else 'biased'})")


# =============================================================================
# GRID SEARCH
# =============================================================================

def run_grid_search(
    btc_df: pd.DataFrame,
    obs_df: pd.DataFrame,
    zscore_df: pd.DataFrame,
    res_map: Dict[str, str],
    ou_params: OUParams,
    total_hours: float,
    output_csv: str,
) -> pd.DataFrame:
    """
    Run grid search over parameters and z-score cutoffs.

    Grid parameters:
    - method: ewma, ou
    - lookback: 60, 72, 84 ticks
    - shares: 10, 30, 50
    - stop_loss: 0.07, 0.12, 0.15
    - cycling: True, False
    - z_cutoff: inf, 3.0, 2.5, 2.0, 1.5, 1.0
    """
    # Grid parameters
    methods = ["ewma", "ou"]
    lookbacks = [60, 72, 84]  # ticks at 60Hz
    shares_list = [5]  # Small position size for cycling tests
    stop_losses = [0.07, 0.12, 0.15]
    cycling_opts = [False, True]  # OFF = 1 entry/hedge per market, ON = can re-enter after merge

    # Z-score filter zones: (lower_bound, upper_bound)
    # None means no bound on that side
    z_zones = [
        (None, None),   # no_limit - all trades
        (None, 3.0),    # z < 3.0 - exclude extreme high vol
        (None, 2.0),    # z < 2.0 - exclude high vol
        (None, 1.5),    # z < 1.5 - exclude high vol (tighter)
        (None, 1.0),    # z < 1.0 - only low vol
        (0, None),      # z > 0 - exclude very low vol
        (0, 2.0),       # 0 < z < 2.0 - medium vol only
        (0, 1.5),       # 0 < z < 1.5 - low-medium vol only
        (-0.5, 1.5),    # -0.5 < z < 1.5 - exclude extremes both sides
        (-1, 2.0),      # -1 < z < 2 - wider medium zone
    ]

    # EWMA parameters (fixed for now)
    ewma_fast = 30.0
    ewma_slow = 180.0

    total_configs = len(methods) * len(lookbacks) * len(shares_list) * len(stop_losses) * len(cycling_opts)
    print(f"\nGrid search: {total_configs} base configs x {len(z_zones)} z-zones = {total_configs * len(z_zones)} total", flush=True)

    results = []

    # Build list of all configs for progress bar
    all_configs = [
        (method, lookback, shares, sl, cycling)
        for method in methods
        for lookback in lookbacks
        for shares in shares_list
        for sl in stop_losses
        for cycling in cycling_opts
    ]

    # Main progress bar over configs
    pbar = tqdm(all_configs, desc="Grid Search", unit="config",
                bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]")

    for method, lookback, shares, sl, cycling in pbar:
        # Update progress bar description
        pbar.set_description(f"{method} {lookback}t {shares}sh SL{sl:.0%} {'Cyc' if cycling else ''}")

        config = BacktestConfig(
            target_shares=shares,
            spike_lookback=lookback,
            stop_loss_pct=sl,
            ewma_fast_halflife=ewma_fast,
            ewma_slow_halflife=ewma_slow,
            use_cycling=cycling,
        )

        # Run backtest (suppress verbose output)
        try:
            trades = run_backtest_with_zscore(
                config, btc_df, obs_df, zscore_df, res_map,
                method=method,
                ou_params=ou_params,
                quiet=True
            )
        except Exception as e:
            tqdm.write(f"  ERROR ({method}, {lookback}): {e}")
            continue

        if not trades:
            continue

        # Analyze each z-zone filter
        for z_lo, z_hi in z_zones:
            # Build zone label
            if z_lo is None and z_hi is None:
                zone_label = "no_limit"
            elif z_lo is None:
                zone_label = f"z<{z_hi}"
            elif z_hi is None:
                zone_label = f"z>{z_lo}"
            else:
                zone_label = f"{z_lo}<z<{z_hi}"

            # Filter trades by zone
            kept = []
            for t in trades:
                z = t.zscore_at_entry
                if z_lo is not None and z <= z_lo:
                    continue
                if z_hi is not None and z >= z_hi:
                    continue
                kept.append(t)

            if not kept:
                continue

            # Compute metrics
            hours_active = estimate_active_hours_zone(total_hours, zscore_df, z_lo, z_hi)
            pnl = sum(t.pnl for t in kept)
            hourly_rate = pnl / hours_active if hours_active > 0 else 0
            correct = sum(1 for t in kept if t.correct_direction)
            dir_acc = correct / len(kept) * 100 if kept else 0
            wins = sum(1 for t in kept if t.pnl > 0)
            win_rate = wins / len(kept) * 100 if kept else 0

            results.append({
                'method': method,
                'lookback_ticks': lookback,
                'lookback_ms': int(lookback * 1000 / 60),
                'shares': shares,
                'stop_loss': sl,
                'cycling': cycling,
                'z_zone_lo': z_lo if z_lo is not None else -999,
                'z_zone_hi': z_hi if z_hi is not None else 999,
                'z_zone_label': zone_label,
                'trades': len(kept),
                'total_pnl': pnl,
                'hourly_rate': hourly_rate,
                'direction_acc': dir_acc,
                'win_rate': win_rate,
                'hours_active': hours_active,
                'pct_time_active': hours_active / total_hours * 100 if total_hours > 0 else 0,
            })

        # Print best cutoff for this config
        config_results = [r for r in results if
                          r['method'] == method and
                          r['lookback_ticks'] == lookback and
                          r['shares'] == shares and
                          r['stop_loss'] == sl and
                          r['cycling'] == cycling]
        if config_results:
            best = max(config_results, key=lambda x: x['hourly_rate'])
            tqdm.write(f"  {method}/{lookback}t: Best={best['z_zone_label']}, "
                      f"${best['hourly_rate']:.2f}/hr, {best['trades']} trades")

    # Convert to DataFrame
    df = pd.DataFrame(results)
    df = df.sort_values('hourly_rate', ascending=False)

    # Save to CSV
    df.to_csv(output_csv, index=False)
    print(f"\nSaved {len(df)} results to {output_csv}", flush=True)

    return df


def print_grid_summary(df: pd.DataFrame, top_n: int = 20):
    """Print summary of grid search results."""
    print("\n" + "=" * 130)
    print(f"TOP {top_n} CONFIGURATIONS BY $/HR")
    print("=" * 130)

    print(f"{'Rank':<5} {'Method':<6} {'Lookback':<10} {'Shares':<7} {'SL':<6} {'Cycling':<8} "
          f"{'Z-Zone':<15} {'$/hr':<10} {'Trades':<8} {'Dir%':<8} {'Win%':<8}")
    print("-" * 130)

    for i, row in df.head(top_n).iterrows():
        rank = list(df.index).index(i) + 1
        print(f"{rank:<5} {row['method']:<6} {row['lookback_ms']}ms    "
              f"{row['shares']:<7} {row['stop_loss']:.0%}   {'ON' if row['cycling'] else 'OFF':<8} "
              f"{row['z_zone_label']:<15} ${row['hourly_rate']:<9.2f} {row['trades']:<8} "
              f"{row['direction_acc']:<7.1f}% {row['win_rate']:<7.1f}%")

    # Summary by method
    print("\n" + "=" * 80)
    print("BEST BY METHOD")
    print("=" * 80)
    for method in df['method'].unique():
        method_df = df[df['method'] == method]
        best = method_df.iloc[0]
        print(f"\n{method.upper()}:")
        print(f"  Best $/hr: ${best['hourly_rate']:.2f}")
        print(f"  Config: {best['lookback_ms']}ms, {best['shares']} shares, "
              f"SL={best['stop_loss']:.0%}, Cycling={'ON' if best['cycling'] else 'OFF'}")
        print(f"  Z-zone: {best['z_zone_label']}")
        print(f"  Trades: {best['trades']}, Dir Acc: {best['direction_acc']:.1f}%")

    # Summary by z-zone
    print("\n" + "=" * 80)
    print("AVERAGE $/HR BY Z-ZONE")
    print("=" * 80)
    for zone_label in sorted(df['z_zone_label'].unique()):
        zone_df = df[df['z_zone_label'] == zone_label]
        avg_hr = zone_df['hourly_rate'].mean()
        max_hr = zone_df['hourly_rate'].max()
        print(f"  {zone_label:<18}: avg=${avg_hr:.2f}/hr, max=${max_hr:.2f}/hr")


# =============================================================================
# MAIN
# =============================================================================

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Volatility Filter Analysis - analyze z-score cutoffs for improved PnL",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    # Config params
    parser.add_argument("--lookback", type=int, default=84,
                        help="Spike lookback in ticks at 60Hz (84 = 1400ms)")
    parser.add_argument("--shares", type=int, default=50,
                        help="Target shares per trade")
    parser.add_argument("--stop-loss", type=float, default=0.12,
                        help="Stop-loss percentage (0.12 = 12 percent)")
    parser.add_argument("--ewma-fast", type=float, default=60.0,
                        help="EWMA fast halflife in seconds")
    parser.add_argument("--ewma-slow", type=float, default=300.0,
                        help="EWMA slow halflife in seconds")
    parser.add_argument("--cycling", action="store_true",
                        help="Enable position cycling")
    parser.add_argument("--method", type=str, default="ewma", choices=["ewma", "ou"],
                        help="Threshold method: ewma (adaptive EWMA ratio) or ou (OU z-score sigmoid)")
    parser.add_argument("--zscore-method", type=str, default="ou",
                        choices=["ou", "ewma", "percentile", "ewma_ratio", "all"],
                        help="Z-score calculation method for volatility filter: "
                             "ou (static OU params), ewma (fully adaptive), "
                             "percentile (rolling rank), ewma_ratio (fast/slow vol ratio), "
                             "all (run all methods sequentially)")

    # Data filtering
    parser.add_argument("--start-ts", type=int, default=None,
                        help="Filter data to only include timestamps >= this (ms)")
    parser.add_argument("--end-ts", type=int, default=None,
                        help="Filter data to only include timestamps <= this (ms)")

    # Load from CSV
    parser.add_argument("--from-csv", type=str, default=None,
                        help="Load best config from optimizer CSV file")

    # File paths
    parser.add_argument("--ou-params", type=str, default="research/ou_params.json",
                        help="Path to OU parameters JSON file")

    # Grid search mode
    parser.add_argument("--grid-search", action="store_true",
                        help="Run grid search over parameters and z-score cutoffs")
    parser.add_argument("--output-csv", type=str, default="research/vol_filter_grid_results.csv",
                        help="Output CSV file for grid search results")

    return parser.parse_args()


def main():
    args = parse_args()

    print("=" * 80, flush=True)
    print("VOLATILITY FILTER ANALYSIS", flush=True)
    print("=" * 80, flush=True)

    # Load OU params
    print("\nLoading OU parameters...")
    ou_params = load_ou_params(args.ou_params)
    print(f"  mu={ou_params.mu:.4f}, sigma_stat={ou_params.sigma_stat:.4f}")

    # Build config
    if args.from_csv:
        print(f"\nLoading best config from: {args.from_csv}")
        csv_df = pd.read_csv(args.from_csv)
        csv_df = csv_df.sort_values('hourly_rate', ascending=False)
        best_row = csv_df.iloc[0]

        config = BacktestConfig(
            target_shares=int(best_row['target_shares']),
            spike_lookback=int(best_row['spike_lookback']),
            stop_loss_pct=best_row['stop_loss_pct'] if pd.notna(best_row['stop_loss_pct']) else None,
            ewma_fast_halflife=best_row.get('ewma_fast_halflife', 60.0),
            ewma_slow_halflife=best_row.get('ewma_slow_halflife', 300.0),
            use_cycling=bool(best_row.get('use_cycling', False)),
        )
        print(f"  Best config: lookback={config.spike_lookback}, shares={config.target_shares}, "
              f"SL={config.stop_loss_pct}, cycling={config.use_cycling}")
    else:
        config = BacktestConfig(
            target_shares=args.shares,
            spike_lookback=args.lookback,
            stop_loss_pct=args.stop_loss if args.stop_loss > 0 else None,
            ewma_fast_halflife=args.ewma_fast,
            ewma_slow_halflife=args.ewma_slow,
            use_cycling=args.cycling,
        )

    # Load data
    print("\nLoading BTC data...")
    btc_df = load_btc_data(start_ts=args.start_ts, end_ts=args.end_ts)

    print("\nLoading observer data...")
    obs_df, res_map = load_observer_data(start_ts=args.start_ts, end_ts=args.end_ts)

    # Compute z-scores (skip if using "all" - will compute per-method later)
    zscore_df = None
    if args.zscore_method != "all":
        print("\nComputing z-scores...")
        zscore_df = compute_zscore_series(btc_df, ou_params, zscore_method=args.zscore_method)
        print(f"  Computed z-scores for {len(zscore_df):,} timestamps")

    # Calculate total hours
    btc_start = btc_df['timestamp_ms'].min()
    btc_end = btc_df['timestamp_ms'].max()
    total_hours = (btc_end - btc_start) / 3600000
    n_markets = obs_df['market_slug'].nunique()

    print(f"\nDataset: {total_hours:.2f} hours, {n_markets} markets")

    # Print z-score distribution (if computed)
    if zscore_df is not None:
        print_zscore_distribution(zscore_df)

    # Grid search mode
    if args.grid_search:
        print("\n" + "=" * 80)
        print("GRID SEARCH MODE")
        print("=" * 80)

        # Handle --zscore-method all: run all methods sequentially
        if args.zscore_method == "all":
            all_methods = ["ou", "ewma", "percentile", "ewma_ratio"]
            all_results = []

            for i, zscore_method in enumerate(all_methods, 1):
                print(f"\n{'='*80}")
                print(f"[{i}/4] Z-SCORE METHOD: {zscore_method.upper()}")
                print(f"{'='*80}")

                # Compute z-scores for this method
                print(f"\nComputing z-scores with method={zscore_method}...")
                zscore_df = compute_zscore_series(btc_df, ou_params, zscore_method=zscore_method)
                print(f"  Computed z-scores for {len(zscore_df):,} timestamps")
                print_zscore_distribution(zscore_df)

                # Run grid search
                output_csv = args.output_csv.replace(".csv", f"_{zscore_method}.csv")
                results_df = run_grid_search(
                    btc_df, obs_df, zscore_df, res_map, ou_params, total_hours,
                    output_csv
                )

                # Add zscore_method column
                results_df['zscore_method'] = zscore_method
                all_results.append(results_df)

                print(f"\n{zscore_method.upper()} complete. Saved to {output_csv}")

            # Combine all results
            combined_df = pd.concat(all_results, ignore_index=True)
            combined_df = combined_df.sort_values('hourly_rate', ascending=False)
            combined_output = args.output_csv.replace(".csv", "_all_combined.csv")
            combined_df.to_csv(combined_output, index=False)
            print(f"\n{'='*80}")
            print(f"ALL METHODS COMPLETE")
            print(f"Combined results saved to: {combined_output}")
            print(f"{'='*80}")

            print_grid_summary(combined_df, top_n=30)
        else:
            results_df = run_grid_search(
                btc_df, obs_df, zscore_df, res_map, ou_params, total_hours,
                args.output_csv
            )

            print_grid_summary(results_df, top_n=30)
        return

    # Single config mode
    # Check if "all" was used without --grid-search
    if args.zscore_method == "all":
        print("\nERROR: --zscore-method all requires --grid-search flag")
        print("Usage: python volatility_filter_analysis.py --grid-search --zscore-method all")
        return

    # Run backtest with z-score tracking
    trades = run_backtest_with_zscore(
        config, btc_df, obs_df, zscore_df, res_map,
        method=args.method,
        ou_params=ou_params
    )

    if not trades:
        print("\nNo trades generated. Check data overlap and config.")
        return

    # Analyze cutoffs
    print("\nAnalyzing z-score cutoffs...")
    results = analyze_cutoffs(trades, total_hours, zscore_df)

    # Print results
    print(f"\nConfig: {config.lookback_ms}ms lookback, {config.grid_levels} grid level, "
          f"{config.target_shares} shares, SL={config.stop_loss_pct}, Method={args.method.upper()}, "
          f"Cycling={'ON' if config.use_cycling else 'OFF'}")

    print_analysis_table(results, config)

    # Recommendation
    recommend_cutoff(results)

    # Per-market analysis
    print_market_analysis(trades)

    # Trade characteristic analysis
    print_trade_characteristics(trades)

    # Summary stats
    print("\n" + "-" * 80)
    print("TRADE-LEVEL Z-SCORE STATS:")
    zscores = [t.zscore_at_entry for t in trades]
    pnls = [t.pnl for t in trades]
    print(f"  Trade z-scores: mean={np.mean(zscores):.2f}, std={np.std(zscores):.2f}")
    print(f"  Total PnL: ${sum(pnls):.2f}")
    print(f"  Total trades: {len(trades)}")

    # Correlation between z-score and PnL
    if len(zscores) > 1:
        corr = np.corrcoef(zscores, pnls)[0, 1]
        print(f"  Correlation (z-score, PnL): {corr:.3f}")


if __name__ == "__main__":
    main()
