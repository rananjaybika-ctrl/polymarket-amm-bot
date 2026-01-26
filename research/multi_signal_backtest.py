#!/usr/bin/env python3
"""
Multi-Signal Combination Backtest

Tests combining multiple independent signals for AGGRESSIVE strategy confirmation.

Hypothesis: Combining multiple independent signals reduces false positives and
improves consistency across regimes.

Strategies:
1. BASELINE: Current velocity-only confirmation
2. STACK_1of3: Accept if at least 1 of 3 signals confirms
3. STACK_2of3: Accept if at least 2 of 3 signals confirm
4. STACK_3of3: Accept if all 3 signals confirm
5. WEIGHTED: Weighted composite score with grid search optimization
6. TIERED: Progressive filtering based on signal strength

Signals used:
- velocity_bps: Price velocity (1st derivative)
- acceleration_bps2: Velocity change (2nd derivative)
- momentum_5s: 5-second rolling velocity average

Data periods:
  --is-oos2: IS+OOS2 (Jan 16-19, ~82 hours)
  --oos34:   OOS3+OOS4 (Jan 22-24, ~47 hours)
  --oos5:    OOS5 (Jan 26, ~42 hours)
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict
import sys
import math
from itertools import product

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

# Signal thresholds
VELOCITY_THRESHOLD = 0.10      # BPS
ACCEL_THRESHOLD = 0.01         # BPS/s²
MOMENTUM_THRESHOLD = 0.05      # BPS (5s avg)

# Weighted score thresholds to test
WEIGHTED_SCORE_THRESHOLDS = [0.3, 0.4, 0.5, 0.6]

# Weight combinations for grid search
WEIGHT_COMBINATIONS = [
    (0.5, 0.3, 0.2),   # Velocity-heavy
    (0.4, 0.4, 0.2),   # Velocity + Accel
    (0.33, 0.33, 0.34), # Equal weights
    (0.3, 0.5, 0.2),   # Accel-heavy
    (0.4, 0.2, 0.4),   # Velocity + Momentum
]


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
    velocity_bps: float
    acceleration_bps2: float
    momentum_5s: float
    signal_score: float
    confirmations: int
    method_used: str


@dataclass
class BacktestResult:
    name: str
    total_trades: int
    total_pnl: float
    hourly_rate: float
    direction_accuracy: float
    trades_per_hour: float
    avg_confirmations: float
    avg_score: float
    zone_config: str = "ALL"


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
# SIGNAL SCORING FUNCTIONS
# =============================================================================

def score_velocity(spike_dir: str, velocity: float) -> Tuple[bool, float]:
    """Score velocity signal. Returns (confirms, score 0-1)."""
    if spike_dir == "UP":
        confirms = velocity > VELOCITY_THRESHOLD
        score = min(max(velocity / 0.5, 0), 1.0)  # Normalize to 0-1
    else:  # DOWN
        confirms = velocity < -VELOCITY_THRESHOLD
        score = min(max(-velocity / 0.5, 0), 1.0)
    return confirms, score


def score_acceleration(spike_dir: str, acceleration: float) -> Tuple[bool, float]:
    """Score acceleration signal. Returns (confirms, score 0-1)."""
    if spike_dir == "UP":
        confirms = acceleration > ACCEL_THRESHOLD
        score = 1.0 if acceleration > ACCEL_THRESHOLD else 0.0
    else:  # DOWN
        confirms = acceleration < -ACCEL_THRESHOLD
        score = 1.0 if acceleration < -ACCEL_THRESHOLD else 0.0
    return confirms, score


def score_momentum(spike_dir: str, momentum: float) -> Tuple[bool, float]:
    """Score momentum signal. Returns (confirms, score 0-1)."""
    if spike_dir == "UP":
        confirms = momentum > MOMENTUM_THRESHOLD
        score = min(max(momentum / 0.3, 0), 1.0)
    else:  # DOWN
        confirms = momentum < -MOMENTUM_THRESHOLD
        score = min(max(-momentum / 0.3, 0), 1.0)
    return confirms, score


def count_confirmations(spike_dir: str, velocity: float, acceleration: float,
                        momentum: float) -> int:
    """Count how many signals confirm the spike direction."""
    confirmations = 0

    if spike_dir == "UP":
        if velocity > 0:
            confirmations += 1
        if acceleration > 0:
            confirmations += 1
        if momentum > 0:
            confirmations += 1
    else:  # DOWN
        if velocity < 0:
            confirmations += 1
        if acceleration < 0:
            confirmations += 1
        if momentum < 0:
            confirmations += 1

    return confirmations


def compute_weighted_score(spike_dir: str, velocity: float, acceleration: float,
                           momentum: float, weights: Tuple[float, float, float]) -> float:
    """Compute weighted composite score."""
    v_confirms, v_score = score_velocity(spike_dir, velocity)
    a_confirms, a_score = score_acceleration(spike_dir, acceleration)
    m_confirms, m_score = score_momentum(spike_dir, momentum)

    w_v, w_a, w_m = weights
    return w_v * v_score + w_a * a_score + w_m * m_score


# =============================================================================
# CONFIRMATION STRATEGIES
# =============================================================================

def confirm_baseline(spike_dir: str, velocity: float, acceleration: float,
                     momentum: float) -> Tuple[bool, float, int, str]:
    """BASELINE: Current velocity-only confirmation (accepts neutral)."""
    if spike_dir == "UP":
        confirms = velocity > -VELOCITY_THRESHOLD
    else:
        confirms = velocity < VELOCITY_THRESHOLD

    confirmations = count_confirmations(spike_dir, velocity, acceleration, momentum)
    score = compute_weighted_score(spike_dir, velocity, acceleration, momentum, (1.0, 0, 0))

    return confirms, score, confirmations, "BASELINE"


def confirm_stack_1of3(spike_dir: str, velocity: float, acceleration: float,
                       momentum: float) -> Tuple[bool, float, int, str]:
    """STACK_1of3: Accept if at least 1 of 3 signals confirms."""
    confirmations = count_confirmations(spike_dir, velocity, acceleration, momentum)
    confirms = confirmations >= 1
    score = compute_weighted_score(spike_dir, velocity, acceleration, momentum, (0.33, 0.33, 0.34))

    return confirms, score, confirmations, "STACK_1of3"


def confirm_stack_2of3(spike_dir: str, velocity: float, acceleration: float,
                       momentum: float) -> Tuple[bool, float, int, str]:
    """STACK_2of3: Accept if at least 2 of 3 signals confirm."""
    confirmations = count_confirmations(spike_dir, velocity, acceleration, momentum)
    confirms = confirmations >= 2
    score = compute_weighted_score(spike_dir, velocity, acceleration, momentum, (0.33, 0.33, 0.34))

    return confirms, score, confirmations, "STACK_2of3"


def confirm_stack_3of3(spike_dir: str, velocity: float, acceleration: float,
                       momentum: float) -> Tuple[bool, float, int, str]:
    """STACK_3of3: Accept if all 3 signals confirm."""
    confirmations = count_confirmations(spike_dir, velocity, acceleration, momentum)
    confirms = confirmations >= 3
    score = compute_weighted_score(spike_dir, velocity, acceleration, momentum, (0.33, 0.33, 0.34))

    return confirms, score, confirmations, "STACK_3of3"


def confirm_weighted(spike_dir: str, velocity: float, acceleration: float,
                     momentum: float, weights: Tuple[float, float, float],
                     threshold: float) -> Tuple[bool, float, int, str]:
    """WEIGHTED: Accept if weighted score exceeds threshold."""
    score = compute_weighted_score(spike_dir, velocity, acceleration, momentum, weights)
    confirms = score >= threshold
    confirmations = count_confirmations(spike_dir, velocity, acceleration, momentum)

    w_str = f"{weights[0]:.1f}v_{weights[1]:.1f}a_{weights[2]:.1f}m"
    return confirms, score, confirmations, f"W_{w_str}_T{threshold}"


def confirm_tiered(spike_dir: str, velocity: float, acceleration: float,
                   momentum: float) -> Tuple[bool, float, int, str]:
    """TIERED: Progressive filtering based on signal strength.

    - Strong velocity (>0.2): Accept immediately
    - Medium velocity (0.1-0.2): Require acceleration alignment
    - Weak velocity (<0.1): Require all 3 signals
    """
    confirmations = count_confirmations(spike_dir, velocity, acceleration, momentum)
    score = compute_weighted_score(spike_dir, velocity, acceleration, momentum, (0.4, 0.3, 0.3))

    abs_vel = abs(velocity)

    if abs_vel >= 0.2:
        # Strong velocity - accept if direction matches
        if spike_dir == "UP":
            confirms = velocity > 0
        else:
            confirms = velocity < 0
        tier = "STRONG"
    elif abs_vel >= 0.1:
        # Medium velocity - need acceleration
        if spike_dir == "UP":
            confirms = velocity > 0 and acceleration > 0
        else:
            confirms = velocity < 0 and acceleration < 0
        tier = "MEDIUM"
    else:
        # Weak velocity - need all signals
        confirms = confirmations >= 3
        tier = "WEAK"

    return confirms, score, confirmations, f"TIERED_{tier}"


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


def simulate_market(spikes_df: pd.DataFrame, obs_df: pd.DataFrame,
                    slug: str, resolution: str,
                    confirm_func, zone_config: str = "ALL", **kwargs) -> List[TradeResult]:
    """Simulate trading with a specific confirmation function."""
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    market_spikes = spikes_df[(spikes_df['timestamp_ms'] >= market_start) &
                               (spikes_df['timestamp_ms'] <= market_end)].copy()

    trades = []
    MIN_CYCLE_GAP_MS = 1000
    last_trade_ts = 0

    for _, spike_row in market_spikes.iterrows():
        spike_ts = spike_row['timestamp_ms']
        spike_dir = spike_row['spike_direction']
        spike_mag = spike_row['spike_magnitude']
        zscore = spike_row['zscore']

        # Z-score volatility filter (AGGRESSIVE spec)
        if zscore < ZSCORE_LO or zscore > ZSCORE_HI:
            continue

        if (spike_ts - last_trade_ts) < MIN_CYCLE_GAP_MS:
            continue

        # Find nearest observer row
        obs_idx = mdf['timestamp_ms'].searchsorted(spike_ts)
        if obs_idx >= len(mdf):
            obs_idx = len(mdf) - 1

        obs_row = mdf.iloc[obs_idx]
        time_rem = obs_row['time_remaining_secs']

        if time_rem < MIN_TIME:
            continue

        # Get all signals
        velocity_bps = obs_row.get('velocity_bps', 0) or 0
        acceleration_bps2 = obs_row.get('acceleration_bps2', 0) or 0
        momentum_5s = obs_row.get('momentum_5s', 0) or 0

        # Apply velocity zone filter
        zone = ZONE_CONFIGS.get(zone_config, ZONE_CONFIGS["ALL"])
        abs_velocity = abs(velocity_bps)
        if abs_velocity < zone["min_vel"] or abs_velocity > zone["max_vel"]:
            continue

        # If momentum_5s not available, estimate from velocity
        if momentum_5s == 0:
            momentum_5s = velocity_bps * 0.8  # Rough approximation

        # Apply confirmation function
        confirms, score, confirmations, method = confirm_func(
            spike_dir, velocity_bps, acceleration_bps2, momentum_5s, **kwargs
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

        # Scan forward for hedge
        hedge_type = "resolution"
        loser_fill = 0.0

        for j in range(obs_idx + 1, len(mdf)):
            scan_row = mdf.iloc[j]

            if loser_side == "UP":
                curr_loser_ask = scan_row['up_ask']
            else:
                curr_loser_ask = scan_row['down_ask']

            if curr_loser_ask <= loser_target:
                loser_fill = loser_target
                hedge_type = "passive"
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
            velocity_bps=velocity_bps,
            acceleration_bps2=acceleration_bps2,
            momentum_5s=momentum_5s,
            signal_score=score,
            confirmations=confirmations,
            method_used=method,
        ))

        last_trade_ts = spike_ts

    return trades


def run_backtest(spikes_df: pd.DataFrame, obs_df: pd.DataFrame,
                 hours: float, name: str, confirm_func,
                 zone_config: str = "ALL", **kwargs) -> BacktestResult:
    """Run backtest with a specific confirmation function."""
    all_trades = []

    for slug in obs_df['market_slug'].unique():
        mdf = obs_df[obs_df['market_slug'] == slug]
        resolution = mdf['resolution'].iloc[0]
        trades = simulate_market(spikes_df, obs_df, slug, resolution, confirm_func,
                                zone_config=zone_config, **kwargs)
        all_trades.extend(trades)

    if not all_trades:
        return BacktestResult(name=name, total_trades=0, total_pnl=0,
                              hourly_rate=0, direction_accuracy=0,
                              trades_per_hour=0, avg_confirmations=0, avg_score=0,
                              zone_config=zone_config)

    total_pnl = sum(t.pnl for t in all_trades)
    total_trades = len(all_trades)
    correct = sum(1 for t in all_trades if t.correct_direction)
    hourly_rate = total_pnl / hours if hours > 0 else 0
    avg_confirmations = np.mean([t.confirmations for t in all_trades])
    avg_score = np.mean([t.signal_score for t in all_trades])

    return BacktestResult(
        name=name,
        total_trades=total_trades,
        total_pnl=total_pnl,
        hourly_rate=hourly_rate,
        direction_accuracy=correct / total_trades,
        trades_per_hour=total_trades / hours,
        avg_confirmations=avg_confirmations,
        avg_score=avg_score,
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

    return spikes_only, obs_df, hours


# =============================================================================
# MAIN
# =============================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Multi-Signal Combination Backtest")
    parser.add_argument("--is-oos2", action="store_true", help="Use IS+OOS2 data (default)")
    parser.add_argument("--oos34", action="store_true", help="Use OOS3+OOS4 data")
    parser.add_argument("--oos5", action="store_true", help="Use OOS5 data")
    parser.add_argument("--all", action="store_true", help="Run on all periods")
    parser.add_argument("--grid-search", action="store_true", help="Run weighted score grid search")
    parser.add_argument("--zone-filter", choices=list(ZONE_CONFIGS.keys()),
                        default="ALL", help="Velocity zone filter")
    parser.add_argument("--grid-zones", action="store_true",
                        help="Run grid search across all zone configs")
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
    print("MULTI-SIGNAL COMBINATION BACKTEST - WITH ZONE GRID SEARCH")
    print("=" * 80)
    print()

    load_ou_params()

    all_period_results = {}

    # Core strategies to always test
    core_strategies = [
        ("BASELINE", confirm_baseline, {}),
        ("STACK_1of3", confirm_stack_1of3, {}),
        ("STACK_2of3", confirm_stack_2of3, {}),
        ("STACK_3of3", confirm_stack_3of3, {}),
        ("TIERED", confirm_tiered, {}),
    ]

    # Add weighted strategies
    weighted_strategies = []
    if args.grid_search:
        # Full grid search
        for weights in WEIGHT_COMBINATIONS:
            for threshold in WEIGHTED_SCORE_THRESHOLDS:
                name = f"W_{weights[0]:.1f}v{weights[1]:.1f}a{weights[2]:.1f}m_T{threshold}"
                weighted_strategies.append(
                    (name, confirm_weighted, {"weights": weights, "threshold": threshold})
                )
    else:
        # Just the most promising ones
        weighted_strategies = [
            ("WEIGHTED_VelHeavy", confirm_weighted, {"weights": (0.5, 0.3, 0.2), "threshold": 0.4}),
            ("WEIGHTED_Equal", confirm_weighted, {"weights": (0.33, 0.33, 0.34), "threshold": 0.4}),
            ("WEIGHTED_AccelHeavy", confirm_weighted, {"weights": (0.3, 0.5, 0.2), "threshold": 0.4}),
        ]

    strategies = core_strategies + weighted_strategies

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

        spikes_df, obs_df, hours = load_data(period)
        print(f"\nBacktest: {hours:.2f} hours")
        print()
        print("Running backtests...")
        print("-" * 80)

        results = []

        for zone in zone_configs:
            for name, func, kwargs in strategies:
                r = run_backtest(spikes_df, obs_df, hours, name, func,
                                zone_config=zone, **kwargs)
                results.append(r)
                if r.total_trades > 0:
                    print(f"  {r.name:25} | Zone={zone:5} | Trades={r.total_trades:4} | $/hr=${r.hourly_rate:7.2f} | Acc={r.direction_accuracy:.1%}")

        all_period_results[period] = results

        # Period summary
        print()
        print(f"{'Strategy':<25} {'Zone':>6} {'Trades':>7} {'$/hr':>10} {'Acc%':>8}")
        print("-" * 68)
        for r in sorted(results, key=lambda x: x.hourly_rate, reverse=True)[:20]:
            print(f"{r.name:<25} {r.zone_config:>6} {r.total_trades:>7} ${r.hourly_rate:>8.2f} "
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

        print(f"{'Strategy':<25} {'Zone':>6}", end="")
        for period in periods:
            print(f" {period:>12}", end="")
        print(f" {'Avg':>10} {'Std':>8}")
        print("-" * (31 + 13 * len(periods) + 20))

        for method, zone, avg, std, rates in combo_stats[:20]:
            print(f"{method:<25} {zone:>6}", end="")
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
        strategy_names = list(set(c[0] for c in combo_stats))
        for method in sorted(strategy_names)[:10]:
            method_combos = [c for c in combo_stats if c[0] == method]
            if method_combos:
                best_for_method = max(method_combos, key=lambda x: x[2])
                print(f"  {method:<25}: {best_for_method[1]:>6} (avg ${best_for_method[2]:.2f}/hr)")

    elif len(periods) > 1:
        # Original cross-period comparison without zone grid
        print()
        print("=" * 80)
        print("CROSS-PERIOD COMPARISON (Top 10 by avg)")
        print("=" * 80)
        print()

        method_names = list(set(r.name for results in all_period_results.values() for r in results))

        method_stats = []
        for method in method_names:
            rates = []
            for period in periods:
                r = next((x for x in all_period_results[period] if x.name == method), None)
                if r and r.total_trades > 0:
                    rates.append(r.hourly_rate)

            if len(rates) == len(periods):
                avg = np.mean(rates)
                std = np.std(rates)
                method_stats.append((method, avg, std, rates))

        method_stats.sort(key=lambda x: -x[1])

        print(f"{'Strategy':<25}", end="")
        for period in periods:
            print(f" {period:>12}", end="")
        print(f" {'Avg':>10} {'Std':>8}")
        print("-" * (25 + 13 * len(periods) + 20))

        for method, avg, std, rates in method_stats[:10]:
            print(f"{method:<25}", end="")
            for r in rates:
                print(f" ${r:>10.2f}", end="")
            print(f" ${avg:>8.2f} {std:>7.2f}")

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


if __name__ == "__main__":
    main()
