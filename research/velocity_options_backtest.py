#!/usr/bin/env python3
"""
Velocity Options Backtest

Compares velocity confirmation approaches for AGGRESSIVE strategy:
1. BASELINE: Current backtest (accepts neutral zone: v > -0.10 for UP)
2. CONSERVATIVE: Reject neutral zone (require v >= +0.10 for UP)
3. DYNAMIC: Time + z-score adjusted threshold
4. EWMA: Smoothed velocity + dynamic threshold

Data: IS + OOS2 (81.7 hours, Jan 16-19)
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
# CONFIGURATION
# =============================================================================

TARGET_SHARES = 50  # Match AGGRESSIVE spec
MIN_TIME = 60  # Minimum seconds to enter
MIN_RUNTIME_SECS = 300

# =============================================================================
# VELOCITY ZONE CONFIGURATIONS
# =============================================================================
# Zone filtering was never grid-searched in original optimization
# LOSING_PATTERNS.md shows: "strong" zone = 54.5% WR vs "neutral" = 36.9%

ZONE_CONFIGS = {
    "ALL":     {"min_vel": 0.00, "max_vel": 99.0, "desc": "No velocity filter (current)"},
    "Z2_6":    {"min_vel": 0.05, "max_vel": 99.0, "desc": "Exclude neutral zone"},
    "Z3_6":    {"min_vel": 0.10, "max_vel": 99.0, "desc": "Moderate+ only"},
    "Z4_6":    {"min_vel": 0.30, "max_vel": 99.0, "desc": "Strong+ only"},
    "Z5_6":    {"min_vel": 0.50, "max_vel": 99.0, "desc": "Extreme+ only"},
}

# Spike detection (OU method)
SPIKE_LOOKBACK = 72  # 1200ms at 60Hz
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10

# Z-score filter (AGGRESSIVE spec)
ZSCORE_LO = 0.0
ZSCORE_HI = 1.5

# Hedge pricing
DROP_MULTIPLIER = 0.50
DROP_INTERCEPT = 0.08
TARGET_PAIR_COST = 0.99

# =============================================================================
# VELOCITY CONFIRMATION OPTIONS
# =============================================================================

# Option 1: BASELINE (current backtest behavior)
# v > -0.10 for UP, v < +0.10 for DOWN (accepts neutral zone)
BASELINE_THRESHOLD = 0.10

# Option 2: CONSERVATIVE (our current live fix)
# v >= +0.10 for UP, v <= -0.10 for DOWN (rejects neutral zone)
CONSERVATIVE_THRESHOLD = 0.10

# Option 3: DYNAMIC (z-score + time adjusted)
DYNAMIC_BASE = 0.10
DYNAMIC_ZSCORE_SCALE = {
    "LOW": 0.8,    # Lower threshold in calm markets
    "MEDIUM": 1.0, # Standard
    "HIGH": 1.2,   # Higher in volatile markets
}
DYNAMIC_TIME_SCALE = {
    "OPTIMAL": 0.7,    # 300-600s window: 30% lower
    "ACCEPTABLE": 1.0, # 180-750s: standard
    "POOR": 1.3,       # Outside: 30% higher
}

# Option 4: EWMA smoothing
EWMA_HALFLIFE_TICKS = 10  # ~167ms at 60Hz
EWMA_ALPHA = 1 - 0.5 ** (1.0 / EWMA_HALFLIFE_TICKS)


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
    velocity_raw: float
    velocity_used: float
    zscore: float
    time_window: str


@dataclass
class BacktestResult:
    name: str
    total_trades: int
    total_pnl: float
    hourly_rate: float
    direction_accuracy: float
    trades_per_hour: float
    avg_velocity: float
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
        print(f"[OU] ERROR: {e}")
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
# VELOCITY CONFIRMATION FUNCTIONS
# =============================================================================

def velocity_baseline(spike_dir: str, velocity_bps: float, time_rem: float, zscore: float) -> Tuple[bool, float]:
    """
    BASELINE: Current backtest behavior (accepts neutral zone)
    UP: v > -0.10 (pass), DOWN: v < +0.10 (pass)
    """
    if spike_dir == "UP":
        return velocity_bps > -BASELINE_THRESHOLD, velocity_bps
    elif spike_dir == "DOWN":
        return velocity_bps < BASELINE_THRESHOLD, velocity_bps
    return True, velocity_bps


def velocity_conservative(spike_dir: str, velocity_bps: float, time_rem: float, zscore: float) -> Tuple[bool, float]:
    """
    CONSERVATIVE: Our current live fix (rejects neutral zone)
    UP: v >= +0.10 (must confirm), DOWN: v <= -0.10 (must confirm)
    """
    if spike_dir == "UP":
        return velocity_bps >= CONSERVATIVE_THRESHOLD, velocity_bps
    elif spike_dir == "DOWN":
        return velocity_bps <= -CONSERVATIVE_THRESHOLD, velocity_bps
    return True, velocity_bps


def velocity_dynamic(spike_dir: str, velocity_bps: float, time_rem: float, zscore: float) -> Tuple[bool, float]:
    """
    DYNAMIC: Time + z-score adjusted threshold
    - Lower threshold in optimal time window (300-600s)
    - Lower threshold in calm markets (z < 0.5)
    """
    # Determine time window
    if 300 <= time_rem <= 600:
        time_scale = DYNAMIC_TIME_SCALE["OPTIMAL"]
        time_window = "OPTIMAL"
    elif 180 <= time_rem <= 750:
        time_scale = DYNAMIC_TIME_SCALE["ACCEPTABLE"]
        time_window = "ACCEPTABLE"
    else:
        time_scale = DYNAMIC_TIME_SCALE["POOR"]
        time_window = "POOR"

    # Determine z-score regime
    if zscore < 0.5:
        z_scale = DYNAMIC_ZSCORE_SCALE["LOW"]
    elif zscore > 1.2:
        z_scale = DYNAMIC_ZSCORE_SCALE["HIGH"]
    else:
        z_scale = DYNAMIC_ZSCORE_SCALE["MEDIUM"]

    # Final threshold
    threshold = DYNAMIC_BASE * time_scale * z_scale

    if spike_dir == "UP":
        return velocity_bps >= threshold, velocity_bps
    elif spike_dir == "DOWN":
        return velocity_bps <= -threshold, velocity_bps
    return True, velocity_bps


def velocity_ewma(spike_dir: str, velocity_bps: float, time_rem: float, zscore: float,
                  ewma_state: dict) -> Tuple[bool, float]:
    """
    EWMA: Smooth velocity before applying dynamic threshold
    Reduces noise from momentary spikes
    """
    # Update EWMA
    if 'smoothed' not in ewma_state:
        ewma_state['smoothed'] = velocity_bps
    else:
        ewma_state['smoothed'] = EWMA_ALPHA * velocity_bps + (1 - EWMA_ALPHA) * ewma_state['smoothed']

    smoothed_v = ewma_state['smoothed']

    # Apply dynamic threshold to smoothed velocity
    if 300 <= time_rem <= 600:
        time_scale = DYNAMIC_TIME_SCALE["OPTIMAL"]
    elif 180 <= time_rem <= 750:
        time_scale = DYNAMIC_TIME_SCALE["ACCEPTABLE"]
    else:
        time_scale = DYNAMIC_TIME_SCALE["POOR"]

    if zscore < 0.5:
        z_scale = DYNAMIC_ZSCORE_SCALE["LOW"]
    elif zscore > 1.2:
        z_scale = DYNAMIC_ZSCORE_SCALE["HIGH"]
    else:
        z_scale = DYNAMIC_ZSCORE_SCALE["MEDIUM"]

    threshold = DYNAMIC_BASE * time_scale * z_scale

    if spike_dir == "UP":
        return smoothed_v >= threshold, smoothed_v
    elif spike_dir == "DOWN":
        return smoothed_v <= -threshold, smoothed_v
    return True, smoothed_v


# =============================================================================
# SPIKE DETECTION
# =============================================================================

def detect_spikes_ou(btc_df: pd.DataFrame) -> pd.DataFrame:
    """Detect spikes using OU adaptive threshold."""
    print("  Detecting spikes (OU method)...")
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    # Price change over lookback
    df['price_prev'] = df['price'].shift(SPIKE_LOOKBACK)
    df['change_pct'] = (df['price'] - df['price_prev']) / df['price_prev'] * 100
    df['magnitude'] = df['change_pct'].abs()

    # EWMA volatility
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

        # Compute z-score for filtering
        if _ou_params:
            log_vol = math.log(vol)
            z = (log_vol - _ou_params.mu) / _ou_params.sigma_stat
            zscores.append(max(0, min(3, z)))  # Clamp to reasonable range
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


def simulate_market_with_velocity(spikes_df: pd.DataFrame, obs_df: pd.DataFrame,
                                   slug: str, resolution: str,
                                   velocity_func, ewma_state: dict = None,
                                   zone_config: str = "ALL",
                                   enable_cycling: bool = True) -> List[TradeResult]:
    """Simulate trading with a specific velocity confirmation function.

    FIXED: Proper cycling logic - blocks new entries until hedge fills.
    """
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    market_spikes = spikes_df[(spikes_df['timestamp_ms'] >= market_start) &
                               (spikes_df['timestamp_ms'] <= market_end)].copy()

    trades = []

    # PROPER CYCLING: Track position state, not just time gap
    in_position = False
    position_data = None
    last_hedge_ts = 0  # Track when last hedge filled (not entry)
    MIN_CYCLE_GAP_MS = 1000  # Gap after hedge fill before next entry

    for _, spike_row in market_spikes.iterrows():
        spike_ts = spike_row['timestamp_ms']
        spike_dir = spike_row['spike_direction']
        spike_mag = spike_row['spike_magnitude']
        zscore = spike_row['zscore']

        # Z-score filter (AGGRESSIVE spec)
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

        velocity_bps = obs_row.get('velocity_bps', 0) or 0

        # Apply velocity zone filter
        zone = ZONE_CONFIGS.get(zone_config, ZONE_CONFIGS["ALL"])
        abs_velocity = abs(velocity_bps)
        if abs_velocity < zone["min_vel"] or abs_velocity > zone["max_vel"]:
            continue

        # Apply velocity confirmation function
        if ewma_state is not None:
            confirms, velocity_used = velocity_func(spike_dir, velocity_bps, time_rem, zscore, ewma_state)
        else:
            confirms, velocity_used = velocity_func(spike_dir, velocity_bps, time_rem, zscore)

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
        hedge_fill_ts = market_end  # Default to market end

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

        # Time window classification
        if 300 <= time_rem <= 600:
            time_window = "OPTIMAL"
        elif 180 <= time_rem <= 750:
            time_window = "ACCEPTABLE"
        else:
            time_window = "POOR"

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
            velocity_raw=velocity_bps,
            velocity_used=velocity_used,
            zscore=zscore,
            time_window=time_window,
        ))

        # PROPER CYCLING: Exit position after hedge simulation
        in_position = False
        last_hedge_ts = hedge_fill_ts

        # If cycling disabled, stop after first trade
        if not enable_cycling:
            break

    return trades


def run_velocity_backtest(spikes_df: pd.DataFrame, obs_df: pd.DataFrame,
                          hours: float, name: str, velocity_func,
                          use_ewma: bool = False,
                          zone_config: str = "ALL",
                          enable_cycling: bool = True) -> BacktestResult:
    """Run backtest with a specific velocity function."""
    all_trades = []

    for slug in obs_df['market_slug'].unique():
        mdf = obs_df[obs_df['market_slug'] == slug]
        resolution = mdf['resolution'].iloc[0]

        ewma_state = {} if use_ewma else None
        trades = simulate_market_with_velocity(spikes_df, obs_df, slug, resolution,
                                                velocity_func, ewma_state,
                                                zone_config=zone_config,
                                                enable_cycling=enable_cycling)
        all_trades.extend(trades)

    if not all_trades:
        return BacktestResult(name=name, total_trades=0, total_pnl=0,
                              hourly_rate=0, direction_accuracy=0,
                              trades_per_hour=0, avg_velocity=0,
                              zone_config=zone_config)

    total_pnl = sum(t.pnl for t in all_trades)
    total_trades = len(all_trades)
    correct = sum(1 for t in all_trades if t.correct_direction)
    hourly_rate = total_pnl / hours if hours > 0 else 0
    avg_velocity = np.mean([abs(t.velocity_used) for t in all_trades])

    return BacktestResult(
        name=name,
        total_trades=total_trades,
        total_pnl=total_pnl,
        hourly_rate=hourly_rate,
        direction_accuracy=correct / total_trades,
        trades_per_hour=total_trades / hours,
        avg_velocity=avg_velocity,
        zone_config=zone_config,
    )


# =============================================================================
# DATA LOADING
# =============================================================================

def load_data(use_oos=False, use_oos5=False):
    """Load data for backtest.

    Args:
        use_oos: If True, load OOS3+OOS4 combined data (Jan 22-24, ~50.6 hours)
        use_oos5: If True, load OOS5 data (Jan 26, ~9 hours)
                 If both False, load IS+OOS2 data (Jan 16-19, ~81.7 hours)
    """
    print("Loading data...")

    if use_oos5:
        # Load OOS5 data (Jan 26)
        print("  Using OOS5 data (Jan 26)...")
        btc_df = pd.read_csv("research/binance_hf/btc_prices_20260124_recovered.csv")
    elif use_oos:
        # Load OOS3+OOS4 combined data
        print("  Using OOS3+OOS4 combined data...")
        btc_df = pd.read_csv("research/observer/btc_prices_oos3_oos4_combined.csv")
        btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
        print(f"  Binance OOS: {len(btc_df):,} rows")
    else:
        # Load IS+OOS2 (all binance_hf files)
        btc_dir = Path("research/binance_hf")
        btc_dfs = []
        for f in sorted(btc_dir.glob("btc_prices_*.csv")):
            if "recovered" not in f.name:  # Skip OOS5 file
                df = pd.read_csv(f)
                btc_dfs.append(df)
                print(f"  Binance: {len(df):,} rows ({f.name})")
        btc_df = pd.concat(btc_dfs, ignore_index=True)
        btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
        print(f"  Binance TOTAL: {len(btc_df):,} rows")

    # Detect spikes
    spikes_df = detect_spikes_ou(btc_df)

    # Load observer
    if use_oos5:
        # Load OOS5 observer data
        obs_df = pd.read_csv("research/observer/grid_obs_oos5.csv",
                             on_bad_lines='skip', low_memory=False)
        obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
        print(f"  Observer OOS5: {len(obs_df):,} rows")
    elif use_oos:
        # Load OOS3+OOS4 combined observer data
        obs_df = pd.read_csv("research/observer/grid_obs_oos3_oos4_combined.csv",
                             on_bad_lines='skip', low_memory=False)
        obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
        print(f"  Observer OOS: {len(obs_df):,} rows")
    else:
        obs_dir = Path("research/observer")
        obs_dfs = []
        for f in sorted(obs_dir.glob("grid_obs_*.csv")):
            if "combined" in f.name:
                continue  # Skip combined files
            df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)
        obs_df = pd.concat(obs_dfs, ignore_index=True)
        obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
        print(f"  Observer: {len(obs_df):,} rows")

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
    spikes_df = spikes_df[(spikes_df['timestamp_ms'] >= overlap_start) &
                           (spikes_df['timestamp_ms'] <= overlap_end)]
    obs_df = obs_df[(obs_df['timestamp_ms'] >= overlap_start) &
                     (obs_df['timestamp_ms'] <= overlap_end)].copy()

    # Add resolutions and filter
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

    # Keep only spike events
    spikes_only = spikes_df[spikes_df['spike_detected'] == True].copy()
    print(f"  Spike events: {len(spikes_only):,}")

    return spikes_only, obs_df, hours


# =============================================================================
# MAIN
# =============================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Velocity Options Backtest")
    parser.add_argument("--oos", action="store_true",
                        help="Use OOS3+OOS4 data instead of IS+OOS2")
    parser.add_argument("--oos5", action="store_true",
                        help="Use OOS5 data (Jan 26, ~9 hours)")
    parser.add_argument("--all", action="store_true",
                        help="Run on all periods")
    parser.add_argument("--zone-filter", choices=list(ZONE_CONFIGS.keys()),
                        default="ALL", help="Velocity zone filter")
    parser.add_argument("--grid-zones", action="store_true",
                        help="Run grid search across all zone configs")
    parser.add_argument("--csv-output", type=str, default=None,
                        help="Output CSV path for results")
    args = parser.parse_args()

    # Determine periods to run
    if args.all:
        periods = [("is_oos2", False, False), ("oos34", True, False), ("oos5", False, True)]
    elif args.oos5:
        periods = [("oos5", False, True)]
    elif args.oos:
        periods = [("oos34", True, False)]
    else:
        periods = [("is_oos2", False, False)]

    # Determine zone configs to test
    zone_configs = list(ZONE_CONFIGS.keys()) if args.grid_zones else [args.zone_filter]

    print("=" * 80)
    print("VELOCITY OPTIONS BACKTEST - WITH ZONE GRID SEARCH")
    print("=" * 80)
    print()

    # Load OU parameters
    load_ou_params()

    # Velocity functions to test
    velocity_funcs = [
        ("BASELINE", velocity_baseline, False),
        ("CONSERVATIVE", velocity_conservative, False),
        ("DYNAMIC", velocity_dynamic, False),
        ("EWMA+DYNAMIC", velocity_ewma, True),
    ]

    all_period_results = {}

    for period_name, use_oos, use_oos5 in periods:
        dataset_name = {
            "is_oos2": "IS+OOS2 (Jan 16-19)",
            "oos34": "OOS3+OOS4 (Jan 22-24)",
            "oos5": "OOS5 (Jan 26)"
        }[period_name]

        print()
        print("=" * 80)
        print(f"PERIOD: {dataset_name}")
        print("=" * 80)

        # Load data
        spikes_df, obs_df, hours = load_data(use_oos=use_oos, use_oos5=use_oos5)
        print(f"\nBacktest: {hours:.2f} hours")
        print()

        results = []

        # Run each velocity option with each zone config
        print("Running backtests...")
        print("-" * 80)

        for zone in zone_configs:
            for func_name, func, use_ewma in velocity_funcs:
                name = f"{func_name}@{zone}"
                r = run_velocity_backtest(spikes_df, obs_df, hours, func_name, func,
                                         use_ewma=use_ewma, zone_config=zone)
                r = BacktestResult(
                    name=func_name,
                    total_trades=r.total_trades,
                    total_pnl=r.total_pnl,
                    hourly_rate=r.hourly_rate,
                    direction_accuracy=r.direction_accuracy,
                    trades_per_hour=r.trades_per_hour,
                    avg_velocity=r.avg_velocity,
                    zone_config=zone,
                )
                results.append(r)
                if r.total_trades > 0:
                    print(f"  {r.name:15} | Zone={zone:5} | Trades={r.total_trades:4} | $/hr=${r.hourly_rate:7.2f} | Acc={r.direction_accuracy:.1%}")

        all_period_results[period_name] = results

        # Period summary
        print()
        print(f"{'Method':<15} {'Zone':>6} {'Trades':>7} {'$/hr':>10} {'Acc%':>8} {'Trd/hr':>8}")
        print("-" * 62)

        for r in sorted(results, key=lambda x: x.hourly_rate, reverse=True)[:15]:
            print(f"{r.name:<15} {r.zone_config:>6} {r.total_trades:>7} ${r.hourly_rate:>8.2f} "
                  f"{r.direction_accuracy:>7.1%} {r.trades_per_hour:>7.1f}")

    # Cross-period comparison (when running --all with --grid-zones)
    if len(periods) > 1 and args.grid_zones:
        print()
        print("=" * 90)
        print("CROSS-PERIOD COMPARISON - METHOD + ZONE COMBINATIONS")
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
            for period_name, _, _ in periods:
                r = next((x for x in all_period_results[period_name]
                         if x.name == method and x.zone_config == zone), None)
                if r and r.total_trades > 0:
                    rates.append(r.hourly_rate)

            if len(rates) == len(periods):
                avg = np.mean(rates)
                std = np.std(rates)
                combo_stats.append((method, zone, avg, std, rates))

        # Sort by average and show top results
        combo_stats.sort(key=lambda x: -x[2])

        print(f"{'Method':<15} {'Zone':>6}", end="")
        for period_name, _, _ in periods:
            print(f" {period_name:>12}", end="")
        print(f" {'Avg':>10} {'Std':>8}")
        print("-" * (21 + 13 * len(periods) + 20))

        for method, zone, avg, std, rates in combo_stats[:20]:
            print(f"{method:<15} {zone:>6}", end="")
            for rate in rates:
                print(f" ${rate:>10.2f}", end="")
            print(f" ${avg:>8.2f} {std:>7.2f}")

        # Find best combo (considering consistency)
        print()
        print("=" * 90)
        print("RECOMMENDATION - Best Method+Zone Combo")
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

        # Show best zone per method
        print()
        print("Best Zone per Method:")
        print("-" * 50)
        for method in ["BASELINE", "CONSERVATIVE", "DYNAMIC", "EWMA+DYNAMIC"]:
            method_combos = [c for c in combo_stats if c[0] == method]
            if method_combos:
                best_for_method = max(method_combos, key=lambda x: x[2])
                print(f"  {method:<15}: {best_for_method[1]:>6} (avg ${best_for_method[2]:.2f}/hr)")

    # Single period recommendation
    elif len(periods) == 1:
        results = all_period_results[periods[0][0]]
        print()
        print("=" * 80)
        print("RECOMMENDATION")
        print("=" * 80)

        best = max(results, key=lambda x: x.hourly_rate)
        baseline = next((r for r in results if r.name == "BASELINE" and r.zone_config == "ALL"), None)

        print(f"\nBest option: {best.name} @ {best.zone_config}")
        print(f"  $/hr:      ${best.hourly_rate:.2f}")
        if baseline:
            print(f"  vs BASELINE@ALL: {(best.hourly_rate - baseline.hourly_rate):+.2f} $/hr")
        print(f"  Accuracy:  {best.direction_accuracy:.1%}")
        print(f"  Trades:    {best.total_trades} ({best.trades_per_hour:.1f}/hr)")

    # Save results to CSV
    if args.csv_output or args.grid_zones:
        output_path = args.csv_output if args.csv_output else "research/velocity_options_results.csv"
        all_rows = []
        for period_name, _, _ in periods:
            for r in all_period_results.get(period_name, []):
                all_rows.append({
                    'period': period_name,
                    'method': r.name,
                    'zone': r.zone_config,
                    'trades': r.total_trades,
                    'total_pnl': r.total_pnl,
                    'hourly_rate': r.hourly_rate,
                    'direction_accuracy': r.direction_accuracy,
                    'trades_per_hour': r.trades_per_hour,
                    'avg_velocity': r.avg_velocity,
                })
        if all_rows:
            results_df = pd.DataFrame(all_rows)
            results_df = results_df.sort_values('hourly_rate', ascending=False)
            results_df.to_csv(output_path, index=False)
            print(f"\nResults saved to: {output_path}")


if __name__ == "__main__":
    main()
