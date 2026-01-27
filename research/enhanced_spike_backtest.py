#!/usr/bin/env python3
"""
Enhanced Spike Backtest - 60Hz OPTIMIZED

Pre-computes spikes from 60Hz data, then runs efficient simulation.

CRITICAL FIX (Jan 17, 2026):
    Fill logic correction - if direction is correct, loser side goes to $0 at
    resolution, meaning our loser bid MUST fill. Previously 14% of trades showed
    as "unhedged/resolution" due to observer data ending early (~35s before
    resolution). With fix, 100% passive hedge rate - matching expected behavior.

    User validated: This matches the true behavior of the strategy.
"""

import argparse
import math
import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime
import sys

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# =============================================================================
# DEFAULT CONFIGURATION (can be overridden via command line)
# =============================================================================

# Order sizing - OPTIMAL CONFIG from optimization (Jan 17)
DEFAULT_TARGET_SHARES = 30  # 30 shares per grid level
DEFAULT_GRID_LEVELS = 3     # 3 grid levels = 90 total shares
DEFAULT_GRID_SPACING = 0.01  # 1 cent between grid levels

# Timing
DEFAULT_MIN_TIME = 60  # Minimum seconds remaining to enter
MIN_RUNTIME_SECS = 300  # Minimum market duration

STOP_LOSS_OPTIONS = [None, 0.07]  # Test no SL and 7% SL

# Spike detection - OPTIMAL: 1000ms lookback
DEFAULT_SPIKE_LOOKBACK = 60  # 1000ms at 60Hz (optimal from backtest)
DEFAULT_SPIKE_THRESHOLD = 0.02  # Base threshold (will be adapted)

# Adaptive Volatility - OPTIMAL: ON
DEFAULT_ADAPTIVE_VOLATILITY = True  # Enabled (optimal from backtest)
ATR_PERIOD = 14  # Period for ATR calculation
ATR_WINDOW = 300  # Window for percentile calculation
LOW_PERCENTILE = 25
HIGH_PERCENTILE = 75
DEFAULT_REGIME_THRESHOLDS = {
    "LOW": 0.010,      # More sensitive in calm markets
    "MEDIUM": 0.020,   # Standard threshold
    "HIGH": 0.035,     # Higher threshold in volatile markets
}

# OU-based adaptive threshold parameters (see PLAN_OU_ADAPTIVE_THRESHOLD.md)
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015  # Raised from 0.005 to filter noise
OU_MAX_THRESHOLD = 0.10

# Global OU parameters (loaded at runtime if threshold_method="ou")
_ou_params = None


def load_ou_params(filepath: str):
    """Load OU parameters from JSON file."""
    global _ou_params
    try:
        from src.strategies.ou_volatility import OUParameters
        _ou_params = OUParameters.load(filepath)
        print(f"[OU] Loaded: μ={_ou_params.mu:.4f}, σ_stat={_ou_params.sigma_stat:.4f}, half_life={_ou_params.half_life_sec:.1f}s")
    except Exception as e:
        print(f"[OU] ERROR loading from {filepath}: {e}")
        _ou_params = None


def compute_ou_threshold(volatility: float) -> float:
    """Compute OU-based adaptive threshold from volatility."""
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

# Enhanced filtering
VELOCITY_CONFIRM_THRESHOLD = 0.10
ENHANCED_SCORE_THRESHOLD = 0.40

# Loser bid (v2: recalibrated Jan 18, 2026 - see HEDGE_PRICING_FINDINGS.md)
DROP_MULTIPLIER = 0.50   # Reduced from 0.68 - spike has weak predictive power
DROP_INTERCEPT = 0.08    # Increased from 0.01 - matches actual mean drop
DROP_REGIME_BONUS = {'LOW': 0.0, 'MEDIUM': 0.01, 'HIGH': 0.02}
TARGET_PAIR_COST = 0.99

MIN_CYCLE_GAP_MS = 1000

# =============================================================================
# RUNTIME CONFIG (set by parse_args)
# =============================================================================

class Config:
    """Runtime configuration, populated from command line args."""
    target_shares: int = DEFAULT_TARGET_SHARES
    grid_levels: int = DEFAULT_GRID_LEVELS
    grid_spacing: float = DEFAULT_GRID_SPACING
    min_time: int = DEFAULT_MIN_TIME
    spike_lookback: int = DEFAULT_SPIKE_LOOKBACK
    spike_threshold: float = DEFAULT_SPIKE_THRESHOLD
    adaptive_volatility: bool = DEFAULT_ADAPTIVE_VOLATILITY
    threshold_method: str = "regime"  # "fixed", "regime", or "ou"
    regime_thresholds: dict = None

    def __init__(self):
        self.regime_thresholds = DEFAULT_REGIME_THRESHOLDS.copy()

    @property
    def total_shares(self) -> int:
        """Total shares per trade = target_shares * grid_levels."""
        return self.target_shares * self.grid_levels

# Global config instance
CONFIG = Config()

# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class TradeResult:
    market_slug: str
    cycle_num: int
    entry_time_remaining: float
    signal_type: str
    signal_score: float
    winner_side: str
    winner_fill_price: float
    loser_fill_price: float
    hedge_type: str
    pair_cost: float
    pnl: float
    correct_direction: bool
    spike_magnitude: float


@dataclass
class BacktestResult:
    strategy_name: str
    stop_loss_pct: Optional[float]
    cycling: bool
    total_trades: int
    total_pnl: float
    hourly_rate: float
    win_rate: float
    avg_pair_cost: float
    passive_hedge_pct: float
    stoploss_hedge_pct: float
    resolution_pct: float
    direction_accuracy: float
    # PnL breakdown by hedge type
    passive_pnl: float = 0.0
    stoploss_pnl: float = 0.0
    resolution_pnl: float = 0.0
    hedged_pnl: float = 0.0
    unhedged_pnl: float = 0.0
    trades: List[TradeResult] = field(default_factory=list)


# =============================================================================
# VECTORIZED SPIKE DETECTION
# =============================================================================

def calculate_rolling_atr(prices: pd.Series, period: int = ATR_PERIOD) -> pd.Series:
    """Calculate rolling ATR (using price changes as proxy for true range)."""
    # Use absolute price changes as proxy for true range
    tr = prices.diff().abs()
    atr = tr.rolling(window=period).mean()
    return atr


def classify_regime_vectorized(atr_series: pd.Series, window: int = ATR_WINDOW) -> pd.Series:
    """Classify volatility regime for each point based on ATR percentile."""
    # Calculate rolling percentile of ATR
    def get_percentile(x):
        if len(x) < window // 2:
            return 50.0
        return (x.rank().iloc[-1] / len(x)) * 100

    percentile = atr_series.rolling(window=window, min_periods=window//2).apply(
        lambda x: (pd.Series(x).rank().iloc[-1] / len(x)) * 100, raw=False
    )

    # Classify regime
    regime = pd.Series('MEDIUM', index=atr_series.index)
    regime[percentile < LOW_PERCENTILE] = 'LOW'
    regime[percentile > HIGH_PERCENTILE] = 'HIGH'

    return regime


def detect_spikes_vectorized(btc_df: pd.DataFrame) -> pd.DataFrame:
    """
    Vectorized spike detection on 60Hz data with ADAPTIVE thresholds.

    Supports three threshold methods:
    - "fixed": Use fixed CONFIG.spike_threshold
    - "regime": Use ATR-based regime thresholds (LOW/MEDIUM/HIGH)
    - "ou": Use OU-based adaptive thresholds (z-score sigmoid)
    """
    print(f"  Detecting spikes (method={CONFIG.threshold_method})...")

    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    # Calculate % change over lookback period
    df['price_prev'] = df['price'].shift(CONFIG.spike_lookback)
    df['change_pct'] = (df['price'] - df['price_prev']) / df['price_prev'] * 100
    df['magnitude'] = df['change_pct'].abs()

    if CONFIG.threshold_method == "ou":
        print("    Using OU-based adaptive thresholds...")
        # Compute EWMA volatility
        returns = df['price'].pct_change() * 100
        ewma_halflife = 300  # 5 seconds at 60Hz
        alpha = 1 - 0.5 ** (1.0 / ewma_halflife)

        variance = returns.iloc[:60].var() if len(returns) > 60 else 0.01
        volatilities = []

        for i, r in enumerate(returns):
            if pd.isna(r):
                volatilities.append(0.01)
                continue
            variance = alpha * (r ** 2) + (1 - alpha) * variance
            vol = max(np.sqrt(variance), 1e-6)
            volatilities.append(vol)

        df['volatility'] = volatilities
        df['threshold'] = df['volatility'].apply(compute_ou_threshold)
        df['regime'] = df['threshold'].apply(lambda t:
            'LOW' if t < 0.015 else ('HIGH' if t > 0.025 else 'MEDIUM'))
        df['spike_detected'] = df['magnitude'] >= df['threshold']

        # Print threshold stats
        mean_thresh = df['threshold'].mean()
        print(f"    Mean threshold: {mean_thresh:.4f}%")
        print(f"    Threshold range: [{df['threshold'].min():.4f}%, {df['threshold'].max():.4f}%]")

    elif CONFIG.threshold_method == "regime" or CONFIG.adaptive_volatility:
        print("    Using ATR-based regime thresholds...")
        # Calculate rolling ATR
        df['atr'] = calculate_rolling_atr(df['price'])

        # Classify regime
        df['regime'] = classify_regime_vectorized(df['atr'])

        # Map regime to threshold
        df['threshold'] = df['regime'].map(CONFIG.regime_thresholds)
        df['threshold'] = df['threshold'].fillna(CONFIG.spike_threshold)

        # Detect spikes using adaptive threshold
        df['spike_detected'] = df['magnitude'] >= df['threshold']

        # Print regime distribution
        regime_counts = df['regime'].value_counts()
        print(f"    Regime distribution: {dict(regime_counts)}")
        print(f"    Thresholds: LOW={CONFIG.regime_thresholds['LOW']}%, MED={CONFIG.regime_thresholds['MEDIUM']}%, HIGH={CONFIG.regime_thresholds['HIGH']}%")

    else:  # "fixed"
        print("    Using FIXED threshold...")
        df['spike_detected'] = df['magnitude'] >= CONFIG.spike_threshold
        df['regime'] = 'MEDIUM'
        df['threshold'] = CONFIG.spike_threshold
        print(f"    Threshold: {CONFIG.spike_threshold}%")

    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['change_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['change_pct'] < 0), 'spike_direction'] = 'DOWN'
    df['spike_magnitude'] = df['magnitude'].where(df['spike_detected'], 0)

    spike_count = df['spike_detected'].sum()
    print(f"    Found {spike_count:,} spikes in {len(df):,} ticks")

    return df[['timestamp_ms', 'price', 'spike_detected', 'spike_direction', 'spike_magnitude', 'regime', 'threshold']]


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def velocity_confirms(spike_dir: str, velocity_bps: float) -> bool:
    if spike_dir == "UP":
        return velocity_bps > -VELOCITY_CONFIRM_THRESHOLD
    elif spike_dir == "DOWN":
        return velocity_bps < VELOCITY_CONFIRM_THRESHOLD
    return True


def compute_score(spike_mag: float, velocity_bps: float, spike_dir: str, time_rem: float, regime: str = "MEDIUM") -> float:
    """
    Scoring formula v2 - interaction-based.

    Based on statistical analysis findings:
    - Current additive formula has R²=0.017, p=0.85 (useless)
    - Spike × Velocity interaction is significant (p=0.001)
    - Time window 300-600s has 88.9% accuracy
    - HIGH regime bonus, LOW regime already filtered

    Changes from v1:
    1. Multiplicative interaction (not additive)
    2. Time window gating
    3. Regime weighting
    """
    # LOW regime already filtered upstream, but double-check
    if regime == "LOW":
        return 0.0

    # Regime weight: HIGH gets bonus
    regime_weight = 1.2 if regime == "HIGH" else 1.0

    # Time window weight: 300-600s is optimal
    if 300 <= time_rem <= 600:
        time_weight = 1.0  # Optimal window
    elif 180 <= time_rem <= 750:
        time_weight = 0.6  # Acceptable
    else:
        time_weight = 0.3  # Poor

    # Core: interaction effect (the key statistical finding)
    interaction = spike_mag * abs(velocity_bps)

    # Final score
    return interaction * time_weight * regime_weight


def compute_score_v1_legacy(spike_mag: float, velocity_bps: float, spike_dir: str, time_rem: float) -> float:
    """Legacy scoring formula - kept for comparison."""
    spike_score = min(spike_mag / 0.05, 1.0)
    velocity_score = min(abs(velocity_bps) / 0.50, 1.0)

    vel_confirms = (spike_dir == "UP" and velocity_bps > 0) or \
                   (spike_dir == "DOWN" and velocity_bps < 0)
    confirm_bonus = 1.0 if vel_confirms else 0.0

    urgency = 1.0 - min(time_rem / 900.0, 1.0)

    return 0.40 * spike_score + 0.30 * velocity_score + 0.20 * confirm_bonus + 0.10 * urgency


def calc_loser_bid(winner_entry: float, spike_mag: float, regime: str = "MEDIUM") -> float:
    """Calculate loser bid with v2 formula (recalibrated Jan 18, 2026)."""
    regime_bonus = DROP_REGIME_BONUS.get(regime, 0.01)
    expected_drop = DROP_MULTIPLIER * spike_mag / 100 + DROP_INTERCEPT + regime_bonus
    expected_drop = max(0.02, min(0.20, expected_drop))
    max_loser = TARGET_PAIR_COST - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid))


# =============================================================================
# DATA LOADING
# =============================================================================

def load_data(start_ts: int = None, end_ts: int = None):
    print("Loading data...")

    # Load ALL Binance files
    btc_dir = Path("research/binance_hf")
    btc_dfs = []
    for f in sorted(btc_dir.glob("btc_prices_*.csv")):
        df = pd.read_csv(f)
        btc_dfs.append(df)
        print(f"  Binance: {len(df):,} rows ({f.name})")
    btc_df = pd.concat(btc_dfs, ignore_index=True)
    btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
    print(f"  Binance TOTAL: {len(btc_df):,} rows")

    # Apply timestamp filters if specified
    if start_ts is not None:
        btc_df = btc_df[btc_df['timestamp_ms'] >= start_ts]
        print(f"  Filtered to start_ts >= {start_ts}: {len(btc_df):,} rows")
    if end_ts is not None:
        btc_df = btc_df[btc_df['timestamp_ms'] <= end_ts]
        print(f"  Filtered to end_ts <= {end_ts}: {len(btc_df):,} rows")

    # Detect spikes
    btc_spikes = detect_spikes_vectorized(btc_df)

    # Load observer
    obs_dir = Path("research/observer")
    obs_dfs = []
    for f in sorted(obs_dir.glob("grid_obs_*.csv")):
        df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
        obs_dfs.append(df)
    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    print(f"  Observer: {len(obs_df):,} rows")

    # Load resolutions
    res_df = pd.read_csv("research/observer/market_resolutions_verified.csv")
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Find overlap
    btc_start, btc_end = btc_spikes['timestamp_ms'].min(), btc_spikes['timestamp_ms'].max()
    obs_start, obs_end = obs_df['timestamp_ms'].min(), obs_df['timestamp_ms'].max()
    overlap_start = max(btc_start, obs_start)
    overlap_end = min(btc_end, obs_end)

    hours = (overlap_end - overlap_start) / 3600000
    print(f"  Overlap: {hours:.2f} hours")

    # Filter to overlap
    btc_spikes = btc_spikes[(btc_spikes['timestamp_ms'] >= overlap_start) &
                            (btc_spikes['timestamp_ms'] <= overlap_end)]
    obs_df = obs_df[(obs_df['timestamp_ms'] >= overlap_start) &
                     (obs_df['timestamp_ms'] <= overlap_end)].copy()

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
    print(f"  Observer rows in overlap: {len(obs_df):,}")
    print(f"  Binance ticks in overlap: {len(btc_spikes):,}")

    # Only keep spike rows from Binance (huge reduction)
    btc_spikes_only = btc_spikes[btc_spikes['spike_detected'] == True].copy()
    print(f"  Spike events: {len(btc_spikes_only):,}")

    return btc_spikes_only, obs_df, hours, valid_slugs


# =============================================================================
# SIMULATION
# =============================================================================

def simulate_market(spikes_df: pd.DataFrame, obs_df: pd.DataFrame,
                    slug: str, resolution: str,
                    stop_loss_pct: Optional[float], enable_cycling: bool,
                    signal_type: str) -> List[TradeResult]:
    """Simulate trading on a single market using pre-computed spikes."""

    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    # Get spikes in this market's time range
    market_spikes = spikes_df[(spikes_df['timestamp_ms'] >= market_start) &
                               (spikes_df['timestamp_ms'] <= market_end)].copy()

    trades = []
    cycle_num = 0
    last_trade_ts = 0

    # For velocity signals, we scan observer data directly
    if signal_type == "velocity":
        for i, row in mdf.iterrows():
            time_rem = row['time_remaining_secs']
            if time_rem < CONFIG.min_time:
                continue

            ts = row['timestamp_ms']
            if enable_cycling and (ts - last_trade_ts) < MIN_CYCLE_GAP_MS:
                continue

            velocity_bps = row.get('velocity_bps', 0) or 0

            if abs(velocity_bps) < 0.10:
                continue

            # Entry signal
            cycle_num += 1
            winner_side = "UP" if velocity_bps > 0 else "DOWN"
            loser_side = "DOWN" if winner_side == "UP" else "UP"

            if winner_side == "UP":
                winner_entry = row['up_ask']
                loser_ask = row['down_ask']
            else:
                winner_entry = row['down_ask']
                loser_ask = row['up_ask']

            loser_target = calc_loser_bid(winner_entry, 0.02)

            # Scan forward for hedge
            hedge_type = "resolution"
            loser_fill = 0.0

            for j in range(i + 1, len(mdf)):
                scan_row = mdf.iloc[j]

                if loser_side == "UP":
                    curr_loser_ask = scan_row['up_ask']
                    winner_bid = scan_row['down_bid']
                else:
                    curr_loser_ask = scan_row['down_ask']
                    winner_bid = scan_row['up_bid']

                # Passive fill
                if curr_loser_ask <= loser_target:
                    loser_fill = loser_target
                    hedge_type = "passive"
                    break

                # Stop-loss
                if stop_loss_pct is not None:
                    drop = (winner_entry - winner_bid) / winner_entry
                    if drop >= stop_loss_pct:
                        loser_fill = curr_loser_ask
                        hedge_type = "stoploss"
                        break

            # Resolution - but if direction correct, loser MUST have filled (goes to $0)
            if hedge_type == "resolution":
                if resolution == winner_side:
                    # Direction correct: loser goes to $0, our bid definitely filled
                    # Count as passive hedge at our target price
                    hedge_type = "passive"
                    loser_fill = loser_target
                else:
                    # Direction wrong: loser goes to $1, we lose
                    loser_fill = 1.0

            pair_cost = winner_entry + loser_fill
            if hedge_type == "resolution":
                # Only true resolution losses (direction wrong)
                pnl = -winner_entry * CONFIG.target_shares
            else:
                pnl = (1.0 - pair_cost) * CONFIG.target_shares

            trades.append(TradeResult(
                market_slug=slug, cycle_num=cycle_num, entry_time_remaining=time_rem,
                signal_type=signal_type, signal_score=abs(velocity_bps),
                winner_side=winner_side, winner_fill_price=winner_entry,
                loser_fill_price=loser_fill, hedge_type=hedge_type,
                pair_cost=pair_cost, pnl=pnl,
                correct_direction=(resolution == winner_side),
                spike_magnitude=0.0
            ))

            last_trade_ts = ts
            if not enable_cycling:
                break

        return trades

    # For spike/enhanced signals, use spike events
    for _, spike_row in market_spikes.iterrows():
        spike_ts = spike_row['timestamp_ms']
        spike_dir = spike_row['spike_direction']
        spike_mag = spike_row['spike_magnitude']

        if enable_cycling and (spike_ts - last_trade_ts) < MIN_CYCLE_GAP_MS:
            continue

        # Find nearest observer row
        obs_idx = mdf['timestamp_ms'].searchsorted(spike_ts)
        if obs_idx >= len(mdf):
            obs_idx = len(mdf) - 1

        obs_row = mdf.iloc[obs_idx]
        time_rem = obs_row['time_remaining_secs']

        if time_rem < CONFIG.min_time:
            continue

        velocity_bps = obs_row.get('velocity_bps', 0) or 0
        regime = spike_row.get('regime', 'MEDIUM')

        # Apply signal type filter
        if signal_type == "enhanced":
            if not velocity_confirms(spike_dir, velocity_bps):
                continue
            score = compute_score(spike_mag, velocity_bps, spike_dir, time_rem, regime)
            if score < ENHANCED_SCORE_THRESHOLD:
                continue
            signal_score = score
        else:  # spike
            signal_score = spike_mag

        # Entry
        cycle_num += 1
        winner_side = spike_dir
        loser_side = "DOWN" if winner_side == "UP" else "UP"

        if winner_side == "UP":
            winner_entry = obs_row['up_ask']
        else:
            winner_entry = obs_row['down_ask']

        loser_target = calc_loser_bid(winner_entry, spike_mag)

        # Scan forward for hedge
        hedge_type = "resolution"
        loser_fill = 0.0

        for j in range(obs_idx + 1, len(mdf)):
            scan_row = mdf.iloc[j]

            if loser_side == "UP":
                curr_loser_ask = scan_row['up_ask']
                winner_bid = scan_row['down_bid']
            else:
                curr_loser_ask = scan_row['down_ask']
                winner_bid = scan_row['up_bid']

            # Passive fill
            if curr_loser_ask <= loser_target:
                loser_fill = loser_target
                hedge_type = "passive"
                break

            # Stop-loss
            if stop_loss_pct is not None:
                drop = (winner_entry - winner_bid) / winner_entry
                if drop >= stop_loss_pct:
                    loser_fill = curr_loser_ask
                    hedge_type = "stoploss"
                    break

        # Resolution - but if direction correct, loser MUST have filled (goes to $0)
        if hedge_type == "resolution":
            if resolution == winner_side:
                # Direction correct: loser goes to $0, our bid definitely filled
                # Count as passive hedge at our target price
                hedge_type = "passive"
                loser_fill = loser_target
            else:
                # Direction wrong: loser goes to $1, we lose
                loser_fill = 1.0

        pair_cost = winner_entry + loser_fill
        if hedge_type == "resolution":
            # Only true resolution losses (direction wrong)
            pnl = -winner_entry * CONFIG.target_shares
        else:
            pnl = (1.0 - pair_cost) * CONFIG.target_shares

        trades.append(TradeResult(
            market_slug=slug, cycle_num=cycle_num, entry_time_remaining=time_rem,
            signal_type=signal_type, signal_score=signal_score,
            winner_side=winner_side, winner_fill_price=winner_entry,
            loser_fill_price=loser_fill, hedge_type=hedge_type,
            pair_cost=pair_cost, pnl=pnl,
            correct_direction=(resolution == winner_side),
            spike_magnitude=spike_mag
        ))

        last_trade_ts = spike_ts
        if not enable_cycling:
            break

    return trades


def run_backtest(spikes_df: pd.DataFrame, obs_df: pd.DataFrame,
                 signal_type: str, stop_loss_pct: Optional[float],
                 enable_cycling: bool, hours: float) -> BacktestResult:
    """Run backtest across all markets."""
    all_trades = []

    for slug in obs_df['market_slug'].unique():
        mdf = obs_df[obs_df['market_slug'] == slug]
        resolution = mdf['resolution'].iloc[0]
        trades = simulate_market(spikes_df, obs_df, slug, resolution,
                                stop_loss_pct, enable_cycling, signal_type)
        all_trades.extend(trades)

    if not all_trades:
        return BacktestResult(
            strategy_name=signal_type, stop_loss_pct=stop_loss_pct, cycling=enable_cycling,
            total_trades=0, total_pnl=0, hourly_rate=0, win_rate=0, avg_pair_cost=0,
            passive_hedge_pct=0, stoploss_hedge_pct=0, resolution_pct=0, direction_accuracy=0
        )

    total_pnl = sum(t.pnl for t in all_trades)
    total_trades = len(all_trades)
    hourly_rate = total_pnl / hours if hours > 0 else 0

    wins = sum(1 for t in all_trades if t.pnl > 0)
    hedged = [t for t in all_trades if t.hedge_type != "resolution"]
    avg_pair = np.mean([t.pair_cost for t in hedged]) if hedged else 0

    passive = sum(1 for t in all_trades if t.hedge_type == "passive")
    stoploss = sum(1 for t in all_trades if t.hedge_type == "stoploss")
    resolution = sum(1 for t in all_trades if t.hedge_type == "resolution")
    correct = sum(1 for t in all_trades if t.correct_direction)

    # PnL breakdown by hedge type
    passive_pnl = sum(t.pnl for t in all_trades if t.hedge_type == "passive")
    stoploss_pnl = sum(t.pnl for t in all_trades if t.hedge_type == "stoploss")
    resolution_pnl = sum(t.pnl for t in all_trades if t.hedge_type == "resolution")
    hedged_pnl = passive_pnl + stoploss_pnl
    unhedged_pnl = resolution_pnl

    return BacktestResult(
        strategy_name=signal_type, stop_loss_pct=stop_loss_pct, cycling=enable_cycling,
        total_trades=total_trades, total_pnl=total_pnl, hourly_rate=hourly_rate,
        win_rate=wins/total_trades, avg_pair_cost=avg_pair,
        passive_hedge_pct=passive/total_trades, stoploss_hedge_pct=stoploss/total_trades,
        resolution_pct=resolution/total_trades, direction_accuracy=correct/total_trades,
        passive_pnl=passive_pnl, stoploss_pnl=stoploss_pnl, resolution_pnl=resolution_pnl,
        hedged_pnl=hedged_pnl, unhedged_pnl=unhedged_pnl,
        trades=all_trades
    )


# =============================================================================
# MAIN
# =============================================================================

def parse_args():
    """Parse command line arguments and update CONFIG."""
    parser = argparse.ArgumentParser(
        description="Enhanced Spike Backtest - 60Hz Optimized",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    # Order sizing
    parser.add_argument("--target-shares", type=int, default=DEFAULT_TARGET_SHARES,
                        help="Number of shares per trade")
    parser.add_argument("--grid-levels", type=int, default=DEFAULT_GRID_LEVELS,
                        help="Number of grid levels (1 = single order)")
    parser.add_argument("--grid-spacing", type=float, default=DEFAULT_GRID_SPACING,
                        help="Price spacing between grid levels")

    # Timing
    parser.add_argument("--min-time", type=int, default=DEFAULT_MIN_TIME,
                        help="Minimum seconds remaining to enter trade")

    # Spike detection
    parser.add_argument("--spike-lookback", type=int, default=DEFAULT_SPIKE_LOOKBACK,
                        help="Lookback ticks for spike detection (36 = 600ms at 60Hz)")
    parser.add_argument("--spike-threshold", type=float, default=DEFAULT_SPIKE_THRESHOLD,
                        help="Base spike threshold in percent (0.02 = 0.02%%)")

    # Adaptive volatility - default ON (optimal config)
    parser.add_argument("--adaptive", action="store_true", default=DEFAULT_ADAPTIVE_VOLATILITY,
                        help="Enable adaptive volatility (regime-based thresholds)")
    parser.add_argument("--low-threshold", type=float, default=DEFAULT_REGIME_THRESHOLDS["LOW"],
                        help="Spike threshold for LOW volatility regime")
    parser.add_argument("--med-threshold", type=float, default=DEFAULT_REGIME_THRESHOLDS["MEDIUM"],
                        help="Spike threshold for MEDIUM volatility regime")
    parser.add_argument("--high-threshold", type=float, default=DEFAULT_REGIME_THRESHOLDS["HIGH"],
                        help="Spike threshold for HIGH volatility regime")

    # Threshold method (new - overrides --adaptive)
    parser.add_argument("--threshold-method", type=str, default="regime",
                        choices=["fixed", "regime", "ou"],
                        help="Spike threshold method: fixed (base), regime (ATR-based), ou (OU adaptive)")
    parser.add_argument("--ou-params", type=str, default="research/ou_params.json",
                        help="Path to OU parameters JSON file (for --threshold-method=ou)")
    parser.add_argument("--start-ts", type=int, default=None,
                        help="Filter data to start at this timestamp (ms). Use 1768705387229 for OOS2 start.")
    parser.add_argument("--end-ts", type=int, default=None,
                        help="Filter data to end at this timestamp (ms). Use 1768705387229 for training end.")

    args = parser.parse_args()

    # Load OU parameters if using OU method
    if args.threshold_method == "ou":
        load_ou_params(args.ou_params)
        if _ou_params is None:
            print("ERROR: Failed to load OU parameters. Run ou_calibration.py first.")
            print("  python research/ou_calibration.py --all")
            sys.exit(1)

    # Update global CONFIG
    CONFIG.target_shares = args.target_shares
    CONFIG.grid_levels = args.grid_levels
    CONFIG.grid_spacing = args.grid_spacing
    CONFIG.min_time = args.min_time
    CONFIG.spike_lookback = args.spike_lookback
    CONFIG.spike_threshold = args.spike_threshold
    CONFIG.adaptive_volatility = args.adaptive
    CONFIG.threshold_method = args.threshold_method
    CONFIG.regime_thresholds = {
        "LOW": args.low_threshold,
        "MEDIUM": args.med_threshold,
        "HIGH": args.high_threshold,
    }

    return args


def main():
    args = parse_args()

    print("=" * 80)
    print("ENHANCED SPIKE BACKTEST - 60Hz OPTIMIZED")
    print("=" * 80)
    print()

    # Print configuration
    threshold_desc = {
        "fixed": "Fixed (base threshold)",
        "regime": "ATR-based regime (LOW/MEDIUM/HIGH)",
        "ou": "OU-based adaptive (z-score sigmoid)"
    }
    print("Configuration:")
    print(f"  Target Shares:    {CONFIG.target_shares}")
    print(f"  Grid Levels:      {CONFIG.grid_levels}")
    print(f"  Min Time:         {CONFIG.min_time}s")
    print(f"  Spike Lookback:   {CONFIG.spike_lookback} ticks ({CONFIG.spike_lookback * 1000 / 60:.0f}ms)")
    print(f"  Threshold Method: {threshold_desc[CONFIG.threshold_method]}")
    if CONFIG.threshold_method == "regime":
        print(f"  Thresholds:       LOW={CONFIG.regime_thresholds['LOW']}%, MED={CONFIG.regime_thresholds['MEDIUM']}%, HIGH={CONFIG.regime_thresholds['HIGH']}%")
    elif CONFIG.threshold_method == "fixed":
        print(f"  Fixed Threshold:  {CONFIG.spike_threshold}%")
    elif CONFIG.threshold_method == "ou":
        print(f"  OU Params:        μ={_ou_params.mu:.4f}, σ_stat={_ou_params.sigma_stat:.4f}")
    print()

    spikes_df, obs_df, hours, valid_slugs = load_data(start_ts=args.start_ts, end_ts=args.end_ts)

    print(f"\nBacktest: {hours:.2f} hours, {len(valid_slugs)} markets")
    print()

    results = []
    # Test both spike (raw) and enhanced (velocity+score filtered)
    signal_types = ["spike", "enhanced"]

    print("Running backtests...")
    print("-" * 80)

    for signal_type in signal_types:
        for cycling in [False, True]:
            for stop_loss in STOP_LOSS_OPTIONS:
                result = run_backtest(spikes_df, obs_df, signal_type, stop_loss, cycling, hours)
                results.append(result)

                sl_str = f"{stop_loss*100:.0f}%" if stop_loss else "None"
                cyc_str = "ON" if cycling else "OFF"
                print(f"  {signal_type:10} | SL={sl_str:5} | Cyc={cyc_str:3} | "
                      f"Trades={result.total_trades:4} | PnL=${result.total_pnl:8.2f} | "
                      f"$/hr=${result.hourly_rate:7.2f} | Acc={result.direction_accuracy:.1%}")

    # Summary
    print()
    print("=" * 80)
    print("RESULTS SUMMARY (sorted by $/hr)")
    print("=" * 80)
    print()
    print(f"{'Strategy':<12} {'SL':<6} {'Cyc':<5} {'Trades':<7} {'PnL':>10} {'$/hr':>9} "
          f"{'Win%':>7} {'Acc%':>7} {'Pass%':>7} {'SL%':>6} {'Res%':>6}")
    print("-" * 105)

    results.sort(key=lambda x: x.hourly_rate, reverse=True)

    for r in results:
        sl_str = f"{r.stop_loss_pct*100:.0f}%" if r.stop_loss_pct else "None"
        cyc_str = "ON" if r.cycling else "OFF"
        print(f"{r.strategy_name:<12} {sl_str:<6} {cyc_str:<5} {r.total_trades:<7} "
              f"${r.total_pnl:>8.2f} ${r.hourly_rate:>8.2f} {r.win_rate:>6.1%} "
              f"{r.direction_accuracy:>6.1%} {r.passive_hedge_pct:>6.1%} "
              f"{r.stoploss_hedge_pct:>5.1%} {r.resolution_pct:>5.1%}")

    # Best configs
    print()
    print("=" * 80)
    print("BEST CONFIG PER STRATEGY")
    print("=" * 80)

    for signal_type in ["enhanced"]:
        matches = [r for r in results if r.strategy_name == signal_type]
        if matches:
            best = max(matches, key=lambda x: x.hourly_rate)
            sl_str = f"{best.stop_loss_pct*100:.0f}%" if best.stop_loss_pct else "None"
            print(f"\n{signal_type.upper()}:")
            print(f"  Config: SL={sl_str}, Cycling={'ON' if best.cycling else 'OFF'}")
            print(f"  $/hr:   ${best.hourly_rate:.2f}")
            print(f"  Trades: {best.total_trades} ({best.direction_accuracy:.1%} accuracy)")
            print(f"  Hedges: {best.passive_hedge_pct:.1%} passive, {best.stoploss_hedge_pct:.1%} SL, {best.resolution_pct:.1%} resolution")

    # PnL Breakdown by Hedge Type
    print()
    print("=" * 80)
    print("PNL BREAKDOWN BY HEDGE TYPE")
    print("=" * 80)

    for signal_type in ["enhanced"]:
        matches = [r for r in results if r.strategy_name == signal_type]
        if matches:
            best = max(matches, key=lambda x: x.hourly_rate)
            total_pnl = best.total_pnl
            total_trades = best.total_trades

            # Calculate trade counts
            passive_trades = int(best.passive_hedge_pct * total_trades)
            stoploss_trades = int(best.stoploss_hedge_pct * total_trades)
            resolution_trades = int(best.resolution_pct * total_trades)
            hedged_trades = passive_trades + stoploss_trades
            unhedged_trades = resolution_trades

            # Calculate PnL percentages
            passive_pnl_pct = (best.passive_pnl / total_pnl * 100) if total_pnl != 0 else 0
            stoploss_pnl_pct = (best.stoploss_pnl / total_pnl * 100) if total_pnl != 0 else 0
            resolution_pnl_pct = (best.resolution_pnl / total_pnl * 100) if total_pnl != 0 else 0
            hedged_pnl_pct = (best.hedged_pnl / total_pnl * 100) if total_pnl != 0 else 0
            unhedged_pnl_pct = (best.unhedged_pnl / total_pnl * 100) if total_pnl != 0 else 0

            print(f"\n{signal_type.upper()} (Total PnL: ${total_pnl:.2f})")
            print("+" + "-" * 62 + "+")
            print(f"| {'Type':<12} | {'Trades':>7} | {'Trade%':>7} | {'PnL':>10} | {'PnL%':>10} |")
            print("+" + "-" * 62 + "+")
            print(f"| {'Passive':<12} | {passive_trades:>7} | {best.passive_hedge_pct*100:>6.1f}% | ${best.passive_pnl:>8.2f} | {passive_pnl_pct:>9.1f}% |")
            print(f"| {'Stoploss':<12} | {stoploss_trades:>7} | {best.stoploss_hedge_pct*100:>6.1f}% | ${best.stoploss_pnl:>8.2f} | {stoploss_pnl_pct:>9.1f}% |")
            print(f"| {'Resolution':<12} | {resolution_trades:>7} | {best.resolution_pct*100:>6.1f}% | ${best.resolution_pnl:>8.2f} | {resolution_pnl_pct:>9.1f}% |")
            print("+" + "-" * 62 + "+")
            print(f"| {'HEDGED':<12} | {hedged_trades:>7} | {(best.passive_hedge_pct+best.stoploss_hedge_pct)*100:>6.1f}% | ${best.hedged_pnl:>8.2f} | {hedged_pnl_pct:>9.1f}% |")
            print(f"| {'UNHEDGED':<12} | {unhedged_trades:>7} | {best.resolution_pct*100:>6.1f}% | ${best.unhedged_pnl:>8.2f} | {unhedged_pnl_pct:>9.1f}% |")
            print("+" + "-" * 62 + "+")

    # Compare to plan
    print()
    print("=" * 80)
    print("VS PLAN EXPECTATIONS")
    print("=" * 80)
    print("\nPlan expectation:    Actual:")
    print("-" * 50)

    for signal_type, expected in [("enhanced", 7.54)]:
        matches = [r for r in results if r.strategy_name == signal_type]
        if matches:
            best = max(matches, key=lambda x: x.hourly_rate)
            diff = best.hourly_rate - expected
            diff_pct = (diff / expected * 100) if expected != 0 else 0
            print(f"{signal_type:10}: ${expected:.2f}/hr  ->  ${best.hourly_rate:.2f}/hr  ({diff:+.2f}, {diff_pct:+.1f}%)")

    print()
    print("=" * 80)


if __name__ == "__main__":
    main()
