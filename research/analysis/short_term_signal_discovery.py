#!/usr/bin/env python3
"""
Short-Term Signal Discovery Framework v2

APPROACH: Start from AGGRESSIVE mode baseline, test incremental improvements.

Instead of blind grid search, we:
1. Use existing spike detection on BTC 60Hz data (longer lookbacks: 1200-2000ms)
2. Apply AGGRESSIVE baseline filters (z-score 0-1.5, velocity confirmation)
3. Test additional signal combinations on top of that baseline
4. Measure accuracy at short horizons (5s, 10s, 15s, 30s)

Target: >=70% accuracy at 5-30s horizons with sufficient sample size.
"""

import argparse
import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple, Callable
from datetime import datetime
from itertools import combinations
from scipy import stats
import math
import warnings

warnings.filterwarnings('ignore')

# =============================================================================
# AGGRESSIVE MODE BASELINE PARAMETERS
# =============================================================================

# Spike detection lookbacks (ticks at 60Hz BTC data)
# Original: [18, 24, 30, 48, 60, 72] = 300-1200ms
# Extended: [72, 96, 120] = 1200ms, 1600ms, 2000ms
LOOKBACK_TICKS = [72, 96, 120]  # User requested longer lookbacks

# AGGRESSIVE mode z-score filter
ZSCORE_LO = 0.0
ZSCORE_HI = 1.5

# Spike thresholds by regime
REGIME_THRESHOLDS = {
    "LOW": 0.010,
    "MEDIUM": 0.020,
    "HIGH": 0.035,
}

# Velocity confirmation
VELOCITY_CONFIRM_THRESHOLD = 0.10

# Time filters
MIN_TIME = 60  # Minimum seconds remaining
MIN_RUNTIME_SECS = 300

# ATR for adaptive volatility
ATR_PERIOD = 14
ATR_WINDOW = 300
LOW_PERCENTILE = 25
HIGH_PERCENTILE = 75

# Horizons for forward analysis
HORIZONS = [5, 10, 15, 30]

# Statistical thresholds
MIN_ACCURACY = 0.70
MIN_SAMPLE_SIZE = 100
P_VALUE_THRESHOLD = 0.01


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class SignalEvent:
    """A signal event with all context and forward outcomes."""
    timestamp_ms: int
    lookback_ms: int
    market_slug: str
    resolution: str  # Market outcome (UP/DOWN)

    # Spike info
    spike_direction: str
    spike_magnitude: float

    # BTC context
    btc_price: float
    zscore: float
    regime: str

    # Observer context
    time_remaining: float
    velocity_bps: float
    velocity_zone: str
    acceleration_bps2: float
    momentum_5s: float
    up_imbalance: float
    down_imbalance: float

    # Derived signals
    velocity_confirms: bool
    accel_confirms: bool
    momentum_confirms: bool

    # Forward outcomes at each horizon
    correct_5s: Optional[bool] = None
    correct_10s: Optional[bool] = None
    correct_15s: Optional[bool] = None
    correct_30s: Optional[bool] = None

    # Resolution outcome
    correct_resolution: Optional[bool] = None


@dataclass
class ConditionResult:
    """Result of testing a condition."""
    name: str
    description: str

    # Accuracy at each horizon
    acc_5s: float
    acc_10s: float
    acc_15s: float
    acc_30s: float

    # Sample sizes
    n_5s: int
    n_10s: int
    n_15s: int
    n_30s: int

    # P-values
    p_5s: float
    p_10s: float
    p_15s: float
    p_30s: float

    # Derived
    best_horizon: str = ""
    best_accuracy: float = 0.0
    edge: float = 0.0


# =============================================================================
# BTC SPIKE DETECTION (from signal_accuracy_analyzer.py)
# =============================================================================

def calculate_rolling_atr(prices: pd.Series, period: int = ATR_PERIOD) -> pd.Series:
    """Calculate rolling ATR."""
    tr = prices.diff().abs()
    atr = tr.rolling(window=period).mean()
    return atr


def classify_regime_vectorized(atr_series: pd.Series, window: int = ATR_WINDOW) -> pd.Series:
    """Classify volatility regime based on ATR percentile."""
    percentile = atr_series.rolling(window=window, min_periods=window//2).apply(
        lambda x: (pd.Series(x).rank().iloc[-1] / len(x)) * 100, raw=False
    )

    regime = pd.Series('MEDIUM', index=atr_series.index)
    regime[percentile < LOW_PERCENTILE] = 'LOW'
    regime[percentile > HIGH_PERCENTILE] = 'HIGH'

    return regime


def detect_spikes_for_lookback(btc_df: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """Detect spikes using specified lookback period with adaptive volatility."""
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    # Calculate % change over lookback period
    df['price_prev'] = df['price'].shift(lookback)
    df['change_pct'] = (df['price'] - df['price_prev']) / df['price_prev'] * 100
    df['magnitude'] = df['change_pct'].abs()

    # Adaptive volatility regime
    df['atr'] = calculate_rolling_atr(df['price'])
    df['regime'] = classify_regime_vectorized(df['atr'])
    df['threshold'] = df['regime'].map(REGIME_THRESHOLDS)
    df['threshold'] = df['threshold'].fillna(0.02)

    # Detect spikes
    df['spike_detected'] = df['magnitude'] >= df['threshold']
    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['change_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['change_pct'] < 0), 'spike_direction'] = 'DOWN'
    df['spike_magnitude'] = df['magnitude'].where(df['spike_detected'], 0)

    # Compute z-score for AGGRESSIVE filter
    returns = df['price'].pct_change() * 100
    ewma_halflife = 300
    alpha = 1 - 0.5 ** (1.0 / ewma_halflife)

    variance = returns.iloc[:60].var() if len(returns) > 60 else 0.01
    zscores = []

    for i, r in enumerate(returns):
        if pd.isna(r):
            zscores.append(0.5)
            continue
        variance = alpha * (r ** 2) + (1 - alpha) * variance
        vol = max(np.sqrt(variance), 1e-6)

        log_vol = math.log(vol)
        # Assume mu and sigma from typical values
        mu, sigma = -4.5, 0.8  # Typical log-volatility parameters
        z = (log_vol - mu) / sigma
        zscores.append(max(0, min(3, z)))

    df['zscore'] = zscores

    return df


def load_btc_data() -> pd.DataFrame:
    """Load BTC 60Hz price data."""
    btc_dir = Path("research/binance_hf")
    if not btc_dir.exists():
        btc_dir = Path("/Users/rananjaybika/polymarket-amm-bot/research/binance_hf")

    btc_dfs = []
    for f in sorted(btc_dir.glob("btc_prices_*.csv")):
        if "combined" in f.name or "recovered" in f.name:
            continue
        df = pd.read_csv(f)
        btc_dfs.append(df)
        print(f"  BTC: {len(df):,} rows ({f.name})")

    if not btc_dfs:
        # Try combined
        combined = btc_dir / "btc_prices_combined.csv"
        if combined.exists():
            df = pd.read_csv(combined)
            btc_dfs.append(df)
            print(f"  BTC: {len(df):,} rows (combined)")

    btc_df = pd.concat(btc_dfs, ignore_index=True)
    btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
    print(f"  BTC total: {len(btc_df):,} rows")
    return btc_df


def load_observer_data() -> Tuple[pd.DataFrame, Dict[str, str]]:
    """Load observer data and resolutions."""
    obs_dir = Path("research/observer")
    if not obs_dir.exists():
        obs_dir = Path("/Users/rananjaybika/polymarket-amm-bot/research/observer")

    obs_dfs = []
    for f in sorted(obs_dir.glob("grid_obs_202601*.csv")):
        if "combined" in f.name or "oos" in f.name.lower():
            continue
        df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
        obs_dfs.append(df)
        print(f"  Observer: {len(df):,} rows ({f.name})")

    obs_df = pd.concat(obs_dfs, ignore_index=True) if obs_dfs else pd.DataFrame()
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])

    # Load resolutions
    res_path = obs_dir / "market_resolutions_verified.csv"
    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Add resolution to observer
    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    print(f"  Observer total: {len(obs_df):,} rows, {obs_df['market_slug'].nunique()} markets")

    return obs_df, res_map


# =============================================================================
# SIGNAL EVENT GENERATION
# =============================================================================

def generate_signal_events(btc_df: pd.DataFrame, obs_df: pd.DataFrame,
                           res_map: Dict[str, str],
                           lookbacks: List[int] = LOOKBACK_TICKS,
                           apply_aggressive_filter: bool = True) -> List[SignalEvent]:
    """Generate signal events by detecting spikes on BTC and joining with observer."""

    all_events = []

    for lookback in lookbacks:
        ms = int(lookback * 1000 / 60)
        print(f"\n  Processing lookback {lookback} ticks ({ms}ms)...")

        # Detect spikes on BTC data
        spikes_df = detect_spikes_for_lookback(btc_df, lookback)
        spikes_only = spikes_df[spikes_df['spike_detected'] == True].copy()

        # Apply AGGRESSIVE z-score filter
        if apply_aggressive_filter:
            before = len(spikes_only)
            spikes_only = spikes_only[
                (spikes_only['zscore'] >= ZSCORE_LO) &
                (spikes_only['zscore'] <= ZSCORE_HI)
            ]
            print(f"    Z-score filter: {before} -> {len(spikes_only)} spikes")

        # Filter out LOW regime (known to be ~48% accuracy = worse than random)
        before = len(spikes_only)
        spikes_only = spikes_only[spikes_only['regime'] != 'LOW']
        print(f"    Regime filter: {before} -> {len(spikes_only)} spikes")

        # Process each market
        for slug in obs_df['market_slug'].unique():
            resolution = res_map.get(slug)
            if resolution not in ['UP', 'DOWN']:
                continue

            mdf = obs_df[obs_df['market_slug'] == slug].copy()
            mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

            if len(mdf) == 0:
                continue

            market_start = mdf['timestamp_ms'].min()
            market_end = mdf['timestamp_ms'].max()

            # Get spikes in this market's time range
            market_spikes = spikes_only[
                (spikes_only['timestamp_ms'] >= market_start) &
                (spikes_only['timestamp_ms'] <= market_end)
            ]

            last_signal_ts = 0
            min_gap_ms = 1000

            for _, spike_row in market_spikes.iterrows():
                spike_ts = spike_row['timestamp_ms']
                spike_dir = spike_row['spike_direction']
                spike_mag = spike_row['spike_magnitude']
                zscore = spike_row['zscore']
                regime = spike_row['regime']

                # Enforce gap between signals
                if (spike_ts - last_signal_ts) < min_gap_ms:
                    continue

                # Find nearest observer row
                obs_idx = mdf['timestamp_ms'].searchsorted(spike_ts)
                if obs_idx >= len(mdf):
                    obs_idx = len(mdf) - 1

                obs_row = mdf.iloc[obs_idx]
                time_rem = obs_row['time_remaining_secs']

                if time_rem < MIN_TIME:
                    continue

                # Extract observer signals
                velocity_bps = float(obs_row.get('velocity_bps', 0) or 0)
                velocity_zone = str(obs_row.get('velocity_zone', 'neutral'))
                accel = float(obs_row.get('acceleration_bps2', 0) or 0)
                momentum = float(obs_row.get('momentum_5s', velocity_bps * 0.8) or 0)
                up_imb = float(obs_row.get('up_imbalance', 0) or 0)
                down_imb = float(obs_row.get('down_imbalance', 0) or 0)

                # Compute confirmation signals
                if spike_dir == 'UP':
                    vel_confirms = velocity_bps > -VELOCITY_CONFIRM_THRESHOLD
                    accel_confirms = accel > 0
                    momentum_confirms = momentum > 0
                else:
                    vel_confirms = velocity_bps < VELOCITY_CONFIRM_THRESHOLD
                    accel_confirms = accel < 0
                    momentum_confirms = momentum < 0

                # Apply velocity confirmation filter (AGGRESSIVE baseline)
                if apply_aggressive_filter and not vel_confirms:
                    continue

                # Compute forward outcomes
                # "Correct" = Could we complete a PROFITABLE HEDGE within N seconds?
                # Entry: buy winner at ask
                # Hedge: buy loser when ask drops low enough
                # Profitable: pair_cost = winner_entry + loser_hedge < TARGET (0.99)

                TARGET_PAIR_COST = 0.99

                if spike_dir == 'UP':
                    winner_entry = float(obs_row.get('up_ask', 0.55) or 0.55)
                    loser_col = 'down_ask'
                else:
                    winner_entry = float(obs_row.get('down_ask', 0.55) or 0.55)
                    loser_col = 'up_ask'

                max_loser_price = TARGET_PAIR_COST - winner_entry  # Max we can pay for loser

                correct_5s = None
                correct_10s = None
                correct_15s = None
                correct_30s = None

                for horizon in HORIZONS:
                    target_ts = spike_ts + horizon * 1000

                    # Scan forward to find minimum loser ask within horizon
                    # (simulates: could we have filled the hedge?)
                    scan_start_idx = obs_idx
                    scan_end_idx = mdf['timestamp_ms'].searchsorted(target_ts)
                    if scan_end_idx >= len(mdf):
                        scan_end_idx = len(mdf) - 1

                    if scan_end_idx > scan_start_idx:
                        window = mdf.iloc[scan_start_idx:scan_end_idx+1]
                        min_loser_ask = window[loser_col].min()
                    else:
                        min_loser_ask = float(mdf.iloc[scan_start_idx].get(loser_col, 0.5) or 0.5)

                    # Could we hedge profitably?
                    hedge_possible = min_loser_ask <= max_loser_price

                    if horizon == 5:
                        correct_5s = hedge_possible
                    elif horizon == 10:
                        correct_10s = hedge_possible
                    elif horizon == 15:
                        correct_15s = hedge_possible
                    elif horizon == 30:
                        correct_30s = hedge_possible

                # Resolution outcome
                correct_resolution = (resolution == spike_dir)

                event = SignalEvent(
                    timestamp_ms=spike_ts,
                    lookback_ms=ms,
                    market_slug=slug,
                    resolution=resolution,
                    spike_direction=spike_dir,
                    spike_magnitude=spike_mag,
                    btc_price=spike_row['price'],
                    zscore=zscore,
                    regime=regime,
                    time_remaining=time_rem,
                    velocity_bps=velocity_bps,
                    velocity_zone=velocity_zone,
                    acceleration_bps2=accel,
                    momentum_5s=momentum,
                    up_imbalance=up_imb,
                    down_imbalance=down_imb,
                    velocity_confirms=vel_confirms,
                    accel_confirms=accel_confirms,
                    momentum_confirms=momentum_confirms,
                    correct_5s=correct_5s,
                    correct_10s=correct_10s,
                    correct_15s=correct_15s,
                    correct_30s=correct_30s,
                    correct_resolution=correct_resolution,
                )

                all_events.append(event)
                last_signal_ts = spike_ts

    print(f"\n  Total signal events: {len(all_events)}")
    return all_events


# =============================================================================
# CONDITION TESTING
# =============================================================================

def binomial_test(successes: int, n: int, p0: float = 0.5) -> float:
    """Binomial test p-value."""
    if n == 0:
        return 1.0
    result = stats.binomtest(successes, n, p0, alternative='greater')
    return result.pvalue


def test_condition(events: List[SignalEvent], condition_func: Callable) -> ConditionResult:
    """Test a condition on signal events."""

    # Filter events by condition
    filtered = [e for e in events if condition_func(e)]

    results = {}
    for h in HORIZONS:
        attr = f'correct_{h}s'
        valid = [getattr(e, attr) for e in filtered if getattr(e, attr) is not None]
        n = len(valid)

        if n == 0:
            results[h] = {'acc': 0, 'n': 0, 'p': 1.0}
        else:
            successes = sum(1 for v in valid if v)
            acc = successes / n
            p = binomial_test(successes, n, 0.5)
            results[h] = {'acc': acc, 'n': n, 'p': p}

    # Find best horizon
    accuracies = {h: results[h]['acc'] for h in HORIZONS}
    best_h = max(accuracies, key=accuracies.get)
    best_acc = accuracies[best_h]

    return ConditionResult(
        name="", description="",  # Filled by caller
        acc_5s=results[5]['acc'],
        acc_10s=results[10]['acc'],
        acc_15s=results[15]['acc'],
        acc_30s=results[30]['acc'],
        n_5s=results[5]['n'],
        n_10s=results[10]['n'],
        n_15s=results[15]['n'],
        n_30s=results[30]['n'],
        p_5s=results[5]['p'],
        p_10s=results[10]['p'],
        p_15s=results[15]['p'],
        p_30s=results[30]['p'],
        best_horizon=f'{best_h}s',
        best_accuracy=best_acc,
        edge=best_acc - 0.5,
    )


def get_conditions_to_test() -> List[Tuple[str, str, Callable]]:
    """Define conditions to test on top of AGGRESSIVE baseline."""

    conditions = []

    # Baseline: AGGRESSIVE filter already applied (z-score, velocity confirm, no LOW regime)
    conditions.append((
        "BASELINE",
        "AGGRESSIVE baseline (z 0-1.5, vel confirms, no LOW regime)",
        lambda e: True  # All events already filtered
    ))

    # --- Additional filters on top of baseline ---

    # Velocity strength
    conditions.append((
        "vel_strong_pos",
        "Baseline + velocity > 0.2 bps",
        lambda e: e.velocity_bps > 0.2
    ))
    conditions.append((
        "vel_strong_neg",
        "Baseline + velocity < -0.2 bps",
        lambda e: e.velocity_bps < -0.2
    ))
    conditions.append((
        "vel_aligned_strong",
        "Baseline + strong velocity aligned with spike",
        lambda e: (e.spike_direction == 'UP' and e.velocity_bps > 0.2) or
                  (e.spike_direction == 'DOWN' and e.velocity_bps < -0.2)
    ))

    # Acceleration confirms
    conditions.append((
        "accel_confirms",
        "Baseline + acceleration confirms spike",
        lambda e: e.accel_confirms
    ))
    conditions.append((
        "vel_and_accel",
        "Baseline + both velocity AND acceleration confirm",
        lambda e: e.velocity_confirms and e.accel_confirms
    ))

    # Momentum confirms
    conditions.append((
        "momentum_confirms",
        "Baseline + momentum confirms spike",
        lambda e: e.momentum_confirms
    ))
    conditions.append((
        "all_three_confirm",
        "Baseline + velocity + accel + momentum all confirm",
        lambda e: e.velocity_confirms and e.accel_confirms and e.momentum_confirms
    ))

    # Time windows
    conditions.append((
        "time_300_600",
        "Baseline + time remaining 300-600s",
        lambda e: 300 <= e.time_remaining <= 600
    ))
    conditions.append((
        "time_180_450",
        "Baseline + time remaining 180-450s",
        lambda e: 180 <= e.time_remaining <= 450
    ))
    conditions.append((
        "time_over_600",
        "Baseline + time remaining > 600s",
        lambda e: e.time_remaining > 600
    ))

    # Spike magnitude
    conditions.append((
        "spike_mag_high",
        "Baseline + spike magnitude >= 0.04",
        lambda e: e.spike_magnitude >= 0.04
    ))
    conditions.append((
        "spike_mag_med",
        "Baseline + spike magnitude 0.025-0.04",
        lambda e: 0.025 <= e.spike_magnitude < 0.04
    ))

    # Regime
    conditions.append((
        "regime_high",
        "Baseline + HIGH volatility regime",
        lambda e: e.regime == 'HIGH'
    ))
    conditions.append((
        "regime_medium",
        "Baseline + MEDIUM volatility regime",
        lambda e: e.regime == 'MEDIUM'
    ))

    # Velocity zones
    for zone in ['strong', 'very_strong']:
        conditions.append((
            f"zone_{zone}",
            f"Baseline + velocity zone = {zone}",
            lambda e, z=zone: e.velocity_zone.lower() == z
        ))

    # Z-score sub-ranges
    conditions.append((
        "zscore_low",
        "Baseline + z-score 0.0-0.5",
        lambda e: 0.0 <= e.zscore <= 0.5
    ))
    conditions.append((
        "zscore_mid",
        "Baseline + z-score 0.5-1.0",
        lambda e: 0.5 < e.zscore <= 1.0
    ))
    conditions.append((
        "zscore_high",
        "Baseline + z-score 1.0-1.5",
        lambda e: 1.0 < e.zscore <= 1.5
    ))

    # Combinations
    conditions.append((
        "vel_accel_time300_600",
        "Baseline + vel+accel confirm + time 300-600s",
        lambda e: e.velocity_confirms and e.accel_confirms and 300 <= e.time_remaining <= 600
    ))
    conditions.append((
        "all_confirm_time300_600",
        "Baseline + all confirm + time 300-600s",
        lambda e: e.velocity_confirms and e.accel_confirms and e.momentum_confirms and 300 <= e.time_remaining <= 600
    ))
    conditions.append((
        "strong_spike_all_confirm",
        "Baseline + spike >= 0.04 + all confirm",
        lambda e: e.spike_magnitude >= 0.04 and e.velocity_confirms and e.accel_confirms and e.momentum_confirms
    ))
    conditions.append((
        "high_regime_all_confirm",
        "Baseline + HIGH regime + all confirm",
        lambda e: e.regime == 'HIGH' and e.velocity_confirms and e.accel_confirms and e.momentum_confirms
    ))

    # Lookback-specific
    conditions.append((
        "lookback_1200",
        "Baseline + lookback = 1200ms only",
        lambda e: e.lookback_ms == 1200
    ))
    conditions.append((
        "lookback_1600",
        "Baseline + lookback = 1600ms only",
        lambda e: e.lookback_ms == 1600
    ))
    conditions.append((
        "lookback_2000",
        "Baseline + lookback = 2000ms only",
        lambda e: e.lookback_ms == 2000
    ))

    return conditions


# =============================================================================
# REPORTING
# =============================================================================

def print_results(results: List[Tuple[str, str, ConditionResult]], title: str):
    """Print results table."""
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)

    print(f"\n{'Condition':<40} {'5s':>7} {'10s':>7} {'15s':>7} {'30s':>7} {'n':>6} {'p':>8} {'Edge':>6}")
    print("-" * 100)

    # Sort by best accuracy
    sorted_results = sorted(results, key=lambda x: x[2].best_accuracy, reverse=True)

    for name, desc, r in sorted_results:
        min_n = min(r.n_5s, r.n_10s, r.n_15s, r.n_30s)
        min_p = min(r.p_5s, r.p_10s, r.p_15s, r.p_30s)

        # Highlight if meets 70% threshold
        marker = " ***" if r.best_accuracy >= 0.70 and min_n >= 100 and min_p < 0.01 else ""

        print(f"{name:<40} {r.acc_5s:>6.1%} {r.acc_10s:>6.1%} {r.acc_15s:>6.1%} {r.acc_30s:>6.1%} "
              f"{min_n:>6} {min_p:>8.4f} {r.edge:>5.1%}{marker}")


def save_results(results: List[Tuple[str, str, ConditionResult]], output_path: Path):
    """Save results to CSV and markdown."""

    # CSV
    rows = []
    for name, desc, r in results:
        rows.append({
            'condition': name,
            'description': desc,
            'acc_5s': r.acc_5s,
            'acc_10s': r.acc_10s,
            'acc_15s': r.acc_15s,
            'acc_30s': r.acc_30s,
            'n_5s': r.n_5s,
            'n_10s': r.n_10s,
            'n_15s': r.n_15s,
            'n_30s': r.n_30s,
            'p_5s': r.p_5s,
            'p_10s': r.p_10s,
            'p_15s': r.p_15s,
            'p_30s': r.p_30s,
            'best_horizon': r.best_horizon,
            'best_accuracy': r.best_accuracy,
            'edge': r.edge,
        })

    df = pd.DataFrame(rows)
    csv_path = output_path / "short_term_signal_results.csv"
    df.to_csv(csv_path, index=False)
    print(f"\nSaved CSV to {csv_path}")

    # Markdown
    md_path = output_path / "SHORT_TERM_SIGNAL_DISCOVERY.md"
    with open(md_path, 'w') as f:
        f.write("# Short-Term Signal Discovery Results\n\n")
        f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write("## Approach\n\n")
        f.write("Starting from AGGRESSIVE mode baseline, testing incremental improvements.\n\n")
        f.write(f"- Lookbacks: {LOOKBACK_TICKS} ticks ({[int(l*1000/60) for l in LOOKBACK_TICKS]}ms)\n")
        f.write(f"- Z-score filter: {ZSCORE_LO} - {ZSCORE_HI}\n")
        f.write(f"- Velocity confirmation: > {-VELOCITY_CONFIRM_THRESHOLD} for UP, < {VELOCITY_CONFIRM_THRESHOLD} for DOWN\n")
        f.write(f"- Excluded: LOW volatility regime\n\n")

        f.write("## Results\n\n")
        f.write("| Condition | 5s | 10s | 15s | 30s | n | p | Edge |\n")
        f.write("|-----------|----|----|----|----|---|---|------|\n")

        sorted_results = sorted(results, key=lambda x: x[2].best_accuracy, reverse=True)
        for name, desc, r in sorted_results[:20]:
            min_n = min(r.n_5s, r.n_10s, r.n_15s, r.n_30s)
            min_p = min(r.p_5s, r.p_10s, r.p_15s, r.p_30s)
            f.write(f"| {name} | {r.acc_5s:.1%} | {r.acc_10s:.1%} | {r.acc_15s:.1%} | {r.acc_30s:.1%} | "
                    f"{min_n} | {min_p:.4f} | +{r.edge:.1%} |\n")

        f.write("\n## Qualifying Combinations (>=70%, n>=100, p<0.01)\n\n")
        qualifying = [(n, d, r) for n, d, r in results
                      if r.best_accuracy >= 0.70 and min(r.n_5s, r.n_10s, r.n_15s, r.n_30s) >= 100]

        if qualifying:
            for name, desc, r in qualifying:
                f.write(f"### {name}\n\n")
                f.write(f"- Description: {desc}\n")
                f.write(f"- Best accuracy: {r.best_accuracy:.1%} at {r.best_horizon}\n")
                f.write(f"- Edge: +{r.edge:.1%}\n\n")
        else:
            f.write("No combinations meeting all criteria.\n")

    print(f"Saved markdown to {md_path}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Short-Term Signal Discovery v2")
    parser.add_argument("--lookbacks", type=str, default="72,96,120",
                        help="Comma-separated lookback ticks (60Hz)")
    parser.add_argument("--no-aggressive-filter", action="store_true",
                        help="Disable AGGRESSIVE baseline filters")
    parser.add_argument("--output-dir", type=str,
                        default="/Users/rananjaybika/polymarket-amm-bot/research/findings")
    args = parser.parse_args()

    # Parse lookbacks
    lookbacks = [int(x.strip()) for x in args.lookbacks.split(',')]
    lookback_ms = [int(l * 1000 / 60) for l in lookbacks]

    print("=" * 80)
    print("SHORT-TERM SIGNAL DISCOVERY v2")
    print("=" * 80)
    print(f"\nApproach: Start from AGGRESSIVE baseline, test incremental improvements")
    print(f"Lookbacks: {lookbacks} ticks ({lookback_ms}ms)")
    print(f"AGGRESSIVE filter: {'DISABLED' if args.no_aggressive_filter else 'ENABLED'}")
    print()

    # Load data
    print("Loading data...")
    btc_df = load_btc_data()
    obs_df, res_map = load_observer_data()

    # Generate signal events
    print("\nGenerating signal events...")
    events = generate_signal_events(
        btc_df, obs_df, res_map,
        lookbacks=lookbacks,
        apply_aggressive_filter=not args.no_aggressive_filter
    )

    if not events:
        print("ERROR: No signal events generated!")
        return

    # Compute baseline accuracy
    print("\n" + "=" * 80)
    print("BASELINE ANALYSIS")
    print("=" * 80)

    baseline_result = test_condition(events, lambda e: True)
    print(f"\nBaseline accuracy (all {len(events)} events):")
    print(f"  5s:  {baseline_result.acc_5s:.1%} (n={baseline_result.n_5s})")
    print(f"  10s: {baseline_result.acc_10s:.1%} (n={baseline_result.n_10s})")
    print(f"  15s: {baseline_result.acc_15s:.1%} (n={baseline_result.n_15s})")
    print(f"  30s: {baseline_result.acc_30s:.1%} (n={baseline_result.n_30s})")
    print(f"  Resolution: ", end="")
    res_correct = sum(1 for e in events if e.correct_resolution)
    print(f"{res_correct/len(events):.1%} (n={len(events)})")

    # Test all conditions
    print("\nTesting conditions...")
    conditions = get_conditions_to_test()

    results = []
    for name, desc, func in conditions:
        r = test_condition(events, func)
        r.name = name
        r.description = desc
        results.append((name, desc, r))

    # Print results
    print_results(results, "CONDITION RESULTS")

    # Save
    output_path = Path(args.output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    save_results(results, output_path)

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    qualifying = [(n, d, r) for n, d, r in results
                  if r.best_accuracy >= 0.70 and min(r.n_5s, r.n_10s, r.n_15s, r.n_30s) >= 100]

    print(f"\nTotal conditions tested: {len(results)}")
    print(f"Qualifying (>=70%, n>=100): {len(qualifying)}")

    if qualifying:
        print("\nTop qualifying conditions:")
        for name, desc, r in sorted(qualifying, key=lambda x: x[2].best_accuracy, reverse=True)[:5]:
            print(f"  {name}: {r.best_accuracy:.1%} at {r.best_horizon}, edge +{r.edge:.1%}")


if __name__ == "__main__":
    main()
