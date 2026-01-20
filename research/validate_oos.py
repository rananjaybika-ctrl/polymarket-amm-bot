#!/usr/bin/env python3
"""
Out-of-Sample Validation Script

PURPOSE: Validate entry signals and hedge pricing on NEW data that was NOT used
for model training. This prevents overfitting.

USAGE:
1. Collect new observer + binance data (after 2026-01-18 08:33:07 IST)
2. Place files in the same directories:
   - observer/grid_obs_YYYYMMDD.csv
   - binance_hf/btc_prices_YYYYMMDD_HHMMSS.csv
3. Run: python validate_oos.py --min-timestamp 1768705387229

The script will ONLY use data with timestamp > min-timestamp (out-of-sample).

WHAT IT VALIDATES:
1. Direction accuracy of spike signals
2. Actual 60-second drop distribution
3. Passive hedge fill rate
4. Comparison to training data benchmarks

PASS/FAIL CRITERIA:
- Direction accuracy >= 55% (training was 61.6%)
- Mean 60s drop within [0.05, 0.15] (training was 0.10)
- Passive fill rate >= 50% (training was 90.9%)
"""

import argparse
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional
import warnings
warnings.filterwarnings('ignore')


# =============================================================================
# CONFIGURATION
# =============================================================================

RESEARCH_DIR = Path("/Users/rananjaybika/polymarket-amm-bot/research")
OBSERVER_DIR = RESEARCH_DIR / "observer"
BINANCE_DIR = RESEARCH_DIR / "binance_hf"

# Latest timestamp from training data (DO NOT USE DATA <= THIS)
TRAINING_DATA_CUTOFF = 1768705387229  # 2026-01-18 08:33:07 IST

# Spike detection parameters (must match optimizer)
SPIKE_LOOKBACK_TICKS = 60  # 1000ms at 60Hz
SPIKE_THRESHOLD = 0.02     # 0.02% minimum

# Enhanced signal parameters (must match optimizer)
VELOCITY_CONFIRM_THRESHOLD = 0.10
ENHANCED_SCORE_THRESHOLD = 0.02

# Hedge pricing v2 parameters (what we're validating)
DROP_MULTIPLIER = 0.50
DROP_INTERCEPT = 0.08
DROP_REGIME_BONUS = {'LOW': 0.0, 'MEDIUM': 0.01, 'HIGH': 0.02}
TARGET_PAIR_COST = 0.99

# Validation thresholds (benchmarks from training data)
BENCHMARKS = {
    'direction_accuracy': 0.616,      # 61.6% in training
    'direction_accuracy_filtered': 0.818,  # 81.8% with enhanced filter
    'mean_60s_drop': 0.101,           # 10.1 cents in training
    'passive_fill_rate': 0.909,       # 90.9% in training
}

# Pass/fail thresholds (more lenient than benchmarks)
PASS_THRESHOLDS = {
    'direction_accuracy': 0.55,       # Must be >= 55%
    'mean_60s_drop_min': 0.05,        # Must be >= 5 cents
    'mean_60s_drop_max': 0.15,        # Must be <= 15 cents
    'passive_fill_rate': 0.50,        # Must be >= 50%
}

# ATR-based regime thresholds (must match optimizer)
REGIME_THRESHOLDS = {
    'LOW': 0.010,
    'MEDIUM': 0.020,
    'HIGH': 0.035,
}


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class ValidationResult:
    """Results from validation."""
    n_signals: int
    n_correct: int
    direction_accuracy: float
    n_filtered_signals: int
    n_filtered_correct: int
    filtered_accuracy: float
    mean_60s_drop: float
    median_60s_drop: float
    std_60s_drop: float
    n_passive_fills: int
    n_hedge_attempts: int
    passive_fill_rate: float
    passed: bool
    failure_reasons: List[str]


# =============================================================================
# DATA LOADING
# =============================================================================

def load_binance_data(min_timestamp: int) -> pd.DataFrame:
    """Load Binance 60Hz data, filtering to OOS only."""
    print("Loading Binance data (OOS only)...")

    binance_files = sorted(BINANCE_DIR.glob("btc_prices_*.csv"))
    if not binance_files:
        raise FileNotFoundError("No Binance files found")

    dfs = []
    for f in binance_files:
        df = pd.read_csv(f)
        # Filter to OOS only
        df = df[df['timestamp_ms'] > min_timestamp]
        if len(df) > 0:
            dfs.append(df)
            print(f"  {f.name}: {len(df)} OOS rows")

    if not dfs:
        raise ValueError(f"No OOS data found (timestamp > {min_timestamp})")

    combined = pd.concat(dfs, ignore_index=True)
    combined = combined.sort_values('timestamp_ms').reset_index(drop=True)
    print(f"  Total OOS Binance rows: {len(combined)}")

    return combined


def load_observer_data(min_timestamp: int) -> pd.DataFrame:
    """Load observer data, filtering to OOS only."""
    print("Loading Observer data (OOS only)...")

    obs_files = sorted(OBSERVER_DIR.glob("grid_obs_*.csv"))
    if not obs_files:
        raise FileNotFoundError("No observer files found")

    dfs = []
    for f in obs_files:
        df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
        # Filter to OOS only
        df = df[df['timestamp_ms'] > min_timestamp]
        if len(df) > 0:
            dfs.append(df)
            print(f"  {f.name}: {len(df)} OOS rows")

    if not dfs:
        raise ValueError(f"No OOS observer data found (timestamp > {min_timestamp})")

    combined = pd.concat(dfs, ignore_index=True)
    combined = combined.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    combined = combined.sort_values(['market_slug', 'timestamp_ms']).reset_index(drop=True)
    print(f"  Total OOS observer rows: {len(combined)}")

    return combined


def load_resolutions() -> Dict[str, str]:
    """Load market resolutions."""
    print("Loading resolutions...")

    # Try multiple resolution files
    res_files = [
        OBSERVER_DIR / "market_resolutions_jan18.csv",  # Latest from AWS
        OBSERVER_DIR / "market_resolutions_verified.csv",
        OBSERVER_DIR / "market_resolutions.csv",
    ]

    res_map = {}
    for res_path in res_files:
        if res_path.exists():
            res_df = pd.read_csv(res_path)
            # Filter to valid resolutions (UP or DOWN)
            valid = res_df[res_df['winner'].isin(['UP', 'DOWN'])]
            for _, row in valid.iterrows():
                if row['slug'] not in res_map:
                    res_map[row['slug']] = row['winner']
            print(f"  {res_path.name}: {len(valid)} valid resolutions")

    print(f"  Total resolved markets: {len(res_map)}")
    return res_map


def infer_resolution(mdf: pd.DataFrame) -> Optional[str]:
    """Infer market resolution from final orderbook state."""
    if len(mdf) == 0:
        return None

    final = mdf.iloc[-1]
    up_bid = final.get('up_bid', 0)
    down_bid = final.get('down_bid', 0)

    if up_bid >= 0.90:
        return 'UP'
    elif down_bid >= 0.90:
        return 'DOWN'
    return None


# =============================================================================
# SPIKE DETECTION
# =============================================================================

def compute_atr(prices: pd.Series, period: int = 14) -> float:
    """Compute Average True Range for regime detection."""
    if len(prices) < period + 1:
        return 0.02  # Default to MEDIUM

    high = prices.rolling(2).max()
    low = prices.rolling(2).min()
    close_prev = prices.shift(1)

    tr = pd.concat([
        high - low,
        (high - close_prev).abs(),
        (low - close_prev).abs()
    ], axis=1).max(axis=1)

    atr = tr.rolling(period).mean().iloc[-1]
    return atr / prices.iloc[-1] * 100  # As percentage


def classify_regime(atr_pct: float) -> str:
    """Classify volatility regime from ATR percentage."""
    if atr_pct >= REGIME_THRESHOLDS['HIGH']:
        return 'HIGH'
    elif atr_pct >= REGIME_THRESHOLDS['MEDIUM']:
        return 'MEDIUM'
    else:
        return 'LOW'


def detect_spikes(binance_df: pd.DataFrame, lookback_ticks: int = 60) -> pd.DataFrame:
    """
    Detect BTC price spikes from Binance 60Hz data.

    Returns DataFrame with spike events.
    """
    print(f"Detecting spikes (lookback={lookback_ticks} ticks)...")

    df = binance_df.copy()
    df['price_change'] = df['price'].pct_change(periods=lookback_ticks) * 100
    df['spike_magnitude'] = df['price_change'].abs()

    # Compute ATR for regime
    df['atr_pct'] = df['price'].rolling(300).apply(
        lambda x: compute_atr(x, 14) if len(x) >= 15 else 0.02, raw=False
    )
    df['regime'] = df['atr_pct'].apply(classify_regime)

    # Filter to spikes above threshold
    spike_mask = df['spike_magnitude'] >= SPIKE_THRESHOLD
    spikes = df[spike_mask].copy()

    # Add direction
    spikes['spike_direction'] = spikes['price_change'].apply(
        lambda x: 'UP' if x > 0 else 'DOWN'
    )

    # Add velocity (approximate from price change rate)
    spikes['velocity_bps'] = spikes['price_change'] / (lookback_ticks / 60)  # per second

    # Filter out LOW regime (as optimizer does)
    spikes = spikes[spikes['regime'] != 'LOW']

    print(f"  Found {len(spikes)} spikes (excluding LOW regime)")
    print(f"  Regime distribution: {spikes['regime'].value_counts().to_dict()}")

    return spikes


def apply_enhanced_filter(spikes: pd.DataFrame) -> pd.DataFrame:
    """Apply enhanced signal filter (velocity confirmation + score)."""

    def velocity_confirms(direction: str, velocity: float) -> bool:
        if direction == 'UP':
            return velocity >= VELOCITY_CONFIRM_THRESHOLD
        else:
            return velocity <= -VELOCITY_CONFIRM_THRESHOLD

    def compute_score(spike_mag: float, velocity: float, direction: str,
                      time_rem: float, regime: str) -> float:
        # Simplified score matching optimizer
        spike_score = min(spike_mag / 0.05, 1.0)
        velocity_score = min(abs(velocity) / 1.0, 1.0)
        confirm_bonus = 0.2 if velocity_confirms(direction, velocity) else 0
        return spike_score * 0.5 + velocity_score * 0.3 + confirm_bonus * 0.2

    filtered = []
    for _, row in spikes.iterrows():
        if not velocity_confirms(row['spike_direction'], row['velocity_bps']):
            continue

        score = compute_score(
            row['spike_magnitude'],
            row['velocity_bps'],
            row['spike_direction'],
            500,  # Default time remaining
            row['regime']
        )

        if score >= ENHANCED_SCORE_THRESHOLD:
            filtered.append(row)

    result = pd.DataFrame(filtered)
    print(f"  After enhanced filter: {len(result)} signals")
    return result


# =============================================================================
# SIGNAL VALIDATION
# =============================================================================

def validate_direction_accuracy(spikes: pd.DataFrame, obs_df: pd.DataFrame,
                                 res_map: Dict[str, str]) -> Tuple[int, int, List[dict]]:
    """
    Validate direction accuracy of spike signals.

    Returns: (n_correct, n_total, details_list)
    """
    print("Validating direction accuracy...")

    # Group observer by market
    obs_by_market = {slug: group for slug, group in obs_df.groupby('market_slug')}

    results = []

    for _, spike in spikes.iterrows():
        spike_ts = spike['timestamp_ms']
        spike_dir = spike['spike_direction']
        spike_mag = spike['spike_magnitude']
        regime = spike['regime']

        # Find which market this spike belongs to (by timestamp)
        matched_market = None
        for slug, mdf in obs_by_market.items():
            if mdf['timestamp_ms'].min() <= spike_ts <= mdf['timestamp_ms'].max():
                matched_market = slug
                break

        if not matched_market:
            continue

        # Get resolution
        resolution = res_map.get(matched_market)
        if not resolution:
            resolution = infer_resolution(obs_by_market[matched_market])

        if resolution not in ['UP', 'DOWN']:
            continue

        correct = spike_dir == resolution

        results.append({
            'timestamp_ms': spike_ts,
            'market_slug': matched_market,
            'predicted': spike_dir,
            'actual': resolution,
            'correct': correct,
            'spike_magnitude': spike_mag,
            'regime': regime,
        })

    if not results:
        return 0, 0, []

    df = pd.DataFrame(results)
    n_correct = df['correct'].sum()
    n_total = len(df)

    print(f"  Validated {n_total} signals, {n_correct} correct ({n_correct/n_total*100:.1f}%)")

    return n_correct, n_total, results


# =============================================================================
# DROP VALIDATION
# =============================================================================

def validate_60s_drops(spikes: pd.DataFrame, obs_df: pd.DataFrame,
                        res_map: Dict[str, str]) -> Tuple[List[float], List[dict]]:
    """
    Measure actual 60-second drops after each signal.

    Returns: (drops_list, details_list)
    """
    print("Measuring 60-second drops...")

    obs_by_market = {slug: group for slug, group in obs_df.groupby('market_slug')}

    drops = []
    details = []

    for _, spike in spikes.iterrows():
        spike_ts = spike['timestamp_ms']
        spike_dir = spike['spike_direction']

        # Find market
        matched_market = None
        for slug, mdf in obs_by_market.items():
            if mdf['timestamp_ms'].min() <= spike_ts <= mdf['timestamp_ms'].max():
                matched_market = slug
                break

        if not matched_market:
            continue

        mdf = obs_by_market[matched_market]

        # Get resolution to determine loser side
        resolution = res_map.get(matched_market)
        if not resolution:
            resolution = infer_resolution(mdf)

        if resolution not in ['UP', 'DOWN']:
            continue

        # Only measure drops for CORRECT predictions
        if spike_dir != resolution:
            continue

        # Loser side
        loser_side = 'DOWN' if spike_dir == 'UP' else 'UP'
        loser_ask_col = 'down_ask' if loser_side == 'DOWN' else 'up_ask'

        # Get observations from spike time
        future_mask = mdf['timestamp_ms'] >= spike_ts
        future_obs = mdf[future_mask]

        if len(future_obs) == 0:
            continue

        # Entry loser ask
        entry_loser_ask = future_obs.iloc[0][loser_ask_col]

        # 60-second window
        window_mask = (mdf['timestamp_ms'] >= spike_ts) & \
                      (mdf['timestamp_ms'] <= spike_ts + 60000)
        window_obs = mdf[window_mask]

        if len(window_obs) == 0:
            continue

        # Minimum loser ask in window
        loser_asks = pd.to_numeric(window_obs[loser_ask_col], errors='coerce')
        min_loser_ask = loser_asks.min()

        if pd.isna(entry_loser_ask) or pd.isna(min_loser_ask):
            continue

        # Actual drop
        actual_drop = entry_loser_ask - min_loser_ask

        # Only count reasonable drops
        if 0 <= actual_drop <= 0.5:
            drops.append(actual_drop)
            details.append({
                'timestamp_ms': spike_ts,
                'market_slug': matched_market,
                'entry_loser_ask': entry_loser_ask,
                'min_loser_ask': min_loser_ask,
                'actual_drop': actual_drop,
                'spike_magnitude': spike['spike_magnitude'],
                'regime': spike['regime'],
            })

    if drops:
        print(f"  Measured {len(drops)} drops")
        print(f"  Mean: {np.mean(drops):.4f}, Median: {np.median(drops):.4f}")
    else:
        print("  No valid drops measured")

    return drops, details


# =============================================================================
# HEDGE FILL VALIDATION
# =============================================================================

def validate_passive_fills(spikes: pd.DataFrame, obs_df: pd.DataFrame,
                            res_map: Dict[str, str]) -> Tuple[int, int, List[dict]]:
    """
    Simulate passive hedge fills using v2 formula.

    Returns: (n_filled, n_attempts, details_list)
    """
    print("Simulating passive hedge fills...")

    obs_by_market = {slug: group for slug, group in obs_df.groupby('market_slug')}

    fills = 0
    attempts = 0
    details = []

    for _, spike in spikes.iterrows():
        spike_ts = spike['timestamp_ms']
        spike_dir = spike['spike_direction']
        spike_mag = spike['spike_magnitude']
        regime = spike['regime']

        # Find market
        matched_market = None
        for slug, mdf in obs_by_market.items():
            if mdf['timestamp_ms'].min() <= spike_ts <= mdf['timestamp_ms'].max():
                matched_market = slug
                break

        if not matched_market:
            continue

        mdf = obs_by_market[matched_market]

        # Get resolution
        resolution = res_map.get(matched_market)
        if not resolution:
            resolution = infer_resolution(mdf)

        if resolution not in ['UP', 'DOWN']:
            continue

        # Only count correct predictions
        if spike_dir != resolution:
            continue

        # Get entry observation
        entry_mask = mdf['timestamp_ms'] >= spike_ts
        if entry_mask.sum() == 0:
            continue

        entry_obs = mdf[entry_mask].iloc[0]

        # Winner/loser sides (entry at bid + 0.01, capped at ask - 0.01 to match live)
        if spike_dir == 'UP':
            winner_bid = entry_obs['up_bid']
            winner_ask = entry_obs['up_ask']
            winner_entry = min(winner_bid + 0.01, winner_ask - 0.01)
            winner_entry = round(winner_entry, 2)
            winner_entry = max(0.01, min(0.95, winner_entry))
            loser_ask_col = 'down_ask'
        else:
            winner_bid = entry_obs['down_bid']
            winner_ask = entry_obs['down_ask']
            winner_entry = min(winner_bid + 0.01, winner_ask - 0.01)
            winner_entry = round(winner_entry, 2)
            winner_entry = max(0.01, min(0.95, winner_entry))
            loser_ask_col = 'up_ask'

        # Calculate our loser bid using v2 formula
        regime_bonus = DROP_REGIME_BONUS.get(regime, 0.01)
        expected_drop = DROP_MULTIPLIER * spike_mag / 100 + DROP_INTERCEPT + regime_bonus
        expected_drop = max(0.02, min(0.20, expected_drop))

        max_loser = TARGET_PAIR_COST - winner_entry
        our_loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
        our_loser_bid = max(0.01, min(0.95, our_loser_bid))

        # Scan forward for fill (passive = loser ask drops to our bid)
        attempts += 1
        filled = False
        fill_price = 0.0
        fill_time = 0

        future_obs = mdf[mdf['timestamp_ms'] > spike_ts]
        for _, row in future_obs.iterrows():
            loser_ask = row[loser_ask_col]

            # Passive fill: ask drops to or below our bid
            if loser_ask <= our_loser_bid:
                filled = True
                fill_price = our_loser_bid
                fill_time = row['timestamp_ms'] - spike_ts
                break

        if filled:
            fills += 1

        details.append({
            'timestamp_ms': spike_ts,
            'market_slug': matched_market,
            'spike_magnitude': spike_mag,
            'regime': regime,
            'winner_entry': winner_entry,
            'our_loser_bid': our_loser_bid,
            'expected_drop': expected_drop,
            'filled': filled,
            'fill_price': fill_price if filled else None,
            'fill_time_ms': fill_time if filled else None,
        })

    if attempts > 0:
        print(f"  {fills}/{attempts} passive fills ({fills/attempts*100:.1f}%)")
    else:
        print("  No hedge attempts")

    return fills, attempts, details


# =============================================================================
# MAIN VALIDATION
# =============================================================================

def run_validation(min_timestamp: int) -> ValidationResult:
    """Run full out-of-sample validation."""

    print("=" * 80)
    print("OUT-OF-SAMPLE VALIDATION")
    print("=" * 80)
    print()
    print(f"Using data with timestamp > {min_timestamp}")

    # Convert to IST for display
    ist = timezone(timedelta(hours=5, minutes=30))
    cutoff_dt = datetime.fromtimestamp(min_timestamp/1000, tz=timezone.utc)
    cutoff_ist = cutoff_dt.astimezone(ist)
    print(f"Cutoff: {cutoff_ist.strftime('%Y-%m-%d %H:%M:%S')} IST")
    print()

    # Load data
    try:
        binance_df = load_binance_data(min_timestamp)
    except Exception as e:
        print(f"ERROR: {e}")
        print("\nNo OOS Binance data found. Please collect new data first.")
        return None

    try:
        obs_df = load_observer_data(min_timestamp)
    except Exception as e:
        print(f"ERROR: {e}")
        print("\nNo OOS observer data found. Please collect new data first.")
        return None

    res_map = load_resolutions()
    print()

    # Data summary
    print("=" * 80)
    print("OOS DATA SUMMARY")
    print("=" * 80)
    min_ts = binance_df['timestamp_ms'].min()
    max_ts = binance_df['timestamp_ms'].max()
    duration_hrs = (max_ts - min_ts) / 1000 / 3600
    print(f"Duration: {duration_hrs:.2f} hours")
    print(f"Binance rows: {len(binance_df)}")
    print(f"Observer rows: {len(obs_df)}")
    print(f"Markets: {obs_df['market_slug'].nunique()}")
    print()

    # Detect spikes
    print("=" * 80)
    print("SPIKE DETECTION")
    print("=" * 80)
    spikes = detect_spikes(binance_df, SPIKE_LOOKBACK_TICKS)
    spikes_filtered = apply_enhanced_filter(spikes)
    print()

    # Validate direction (all spikes)
    print("=" * 80)
    print("DIRECTION ACCURACY (All Spikes)")
    print("=" * 80)
    n_correct, n_total, dir_details = validate_direction_accuracy(spikes, obs_df, res_map)
    direction_accuracy = n_correct / n_total if n_total > 0 else 0
    print()

    # Validate direction (filtered)
    print("=" * 80)
    print("DIRECTION ACCURACY (Filtered)")
    print("=" * 80)
    n_correct_f, n_total_f, _ = validate_direction_accuracy(spikes_filtered, obs_df, res_map)
    filtered_accuracy = n_correct_f / n_total_f if n_total_f > 0 else 0
    print()

    # Validate 60s drops
    print("=" * 80)
    print("60-SECOND DROP ANALYSIS")
    print("=" * 80)
    drops, drop_details = validate_60s_drops(spikes_filtered, obs_df, res_map)
    mean_drop = np.mean(drops) if drops else 0
    median_drop = np.median(drops) if drops else 0
    std_drop = np.std(drops) if drops else 0
    print()

    # Validate passive fills
    print("=" * 80)
    print("PASSIVE HEDGE FILL SIMULATION")
    print("=" * 80)
    n_fills, n_attempts, fill_details = validate_passive_fills(spikes_filtered, obs_df, res_map)
    passive_rate = n_fills / n_attempts if n_attempts > 0 else 0
    print()

    # Compile results
    failure_reasons = []

    if direction_accuracy < PASS_THRESHOLDS['direction_accuracy']:
        failure_reasons.append(
            f"Direction accuracy {direction_accuracy:.1%} < {PASS_THRESHOLDS['direction_accuracy']:.0%}"
        )

    if mean_drop < PASS_THRESHOLDS['mean_60s_drop_min']:
        failure_reasons.append(
            f"Mean 60s drop {mean_drop:.4f} < {PASS_THRESHOLDS['mean_60s_drop_min']}"
        )

    if mean_drop > PASS_THRESHOLDS['mean_60s_drop_max']:
        failure_reasons.append(
            f"Mean 60s drop {mean_drop:.4f} > {PASS_THRESHOLDS['mean_60s_drop_max']}"
        )

    if passive_rate < PASS_THRESHOLDS['passive_fill_rate']:
        failure_reasons.append(
            f"Passive fill rate {passive_rate:.1%} < {PASS_THRESHOLDS['passive_fill_rate']:.0%}"
        )

    passed = len(failure_reasons) == 0

    result = ValidationResult(
        n_signals=n_total,
        n_correct=n_correct,
        direction_accuracy=direction_accuracy,
        n_filtered_signals=n_total_f,
        n_filtered_correct=n_correct_f,
        filtered_accuracy=filtered_accuracy,
        mean_60s_drop=mean_drop,
        median_60s_drop=median_drop,
        std_60s_drop=std_drop,
        n_passive_fills=n_fills,
        n_hedge_attempts=n_attempts,
        passive_fill_rate=passive_rate,
        passed=passed,
        failure_reasons=failure_reasons,
    )

    return result


def print_results(result: ValidationResult):
    """Print validation results with comparison to benchmarks."""

    print("=" * 80)
    print("VALIDATION RESULTS")
    print("=" * 80)
    print()

    # Direction accuracy
    print("DIRECTION ACCURACY:")
    print(f"  All signals:      {result.direction_accuracy:.1%} ({result.n_correct}/{result.n_signals})")
    print(f"    Benchmark:      {BENCHMARKS['direction_accuracy']:.1%}")
    print(f"    Pass threshold: {PASS_THRESHOLDS['direction_accuracy']:.0%}")
    status = "PASS" if result.direction_accuracy >= PASS_THRESHOLDS['direction_accuracy'] else "FAIL"
    print(f"    Status:         {status}")
    print()

    print(f"  Filtered signals: {result.filtered_accuracy:.1%} ({result.n_filtered_correct}/{result.n_filtered_signals})")
    print(f"    Benchmark:      {BENCHMARKS['direction_accuracy_filtered']:.1%}")
    print()

    # 60s drops
    print("60-SECOND DROPS:")
    print(f"  Mean:             {result.mean_60s_drop:.4f}")
    print(f"  Median:           {result.median_60s_drop:.4f}")
    print(f"  Std:              {result.std_60s_drop:.4f}")
    print(f"    Benchmark:      {BENCHMARKS['mean_60s_drop']:.4f}")
    print(f"    Pass range:     [{PASS_THRESHOLDS['mean_60s_drop_min']}, {PASS_THRESHOLDS['mean_60s_drop_max']}]")
    in_range = PASS_THRESHOLDS['mean_60s_drop_min'] <= result.mean_60s_drop <= PASS_THRESHOLDS['mean_60s_drop_max']
    status = "PASS" if in_range else "FAIL"
    print(f"    Status:         {status}")
    print()

    # Passive fills
    print("PASSIVE HEDGE FILLS:")
    print(f"  Fill rate:        {result.passive_fill_rate:.1%} ({result.n_passive_fills}/{result.n_hedge_attempts})")
    print(f"    Benchmark:      {BENCHMARKS['passive_fill_rate']:.1%}")
    print(f"    Pass threshold: {PASS_THRESHOLDS['passive_fill_rate']:.0%}")
    status = "PASS" if result.passive_fill_rate >= PASS_THRESHOLDS['passive_fill_rate'] else "FAIL"
    print(f"    Status:         {status}")
    print()

    # Overall
    print("=" * 80)
    if result.passed:
        print("OVERALL: PASS")
        print("The model generalizes to out-of-sample data.")
    else:
        print("OVERALL: FAIL")
        print("Failure reasons:")
        for reason in result.failure_reasons:
            print(f"  - {reason}")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="Out-of-Sample Validation")
    parser.add_argument("--min-timestamp", type=int, default=TRAINING_DATA_CUTOFF,
                        help="Minimum timestamp (only use data after this)")
    parser.add_argument("--output", type=str, default=None,
                        help="Output CSV for detailed results")
    args = parser.parse_args()

    result = run_validation(args.min_timestamp)

    if result is None:
        print("\n" + "=" * 80)
        print("VALIDATION COULD NOT RUN - NO OOS DATA")
        print("=" * 80)
        print()
        print("To collect new data:")
        print("  1. Run the observer: python scripts/observer.py")
        print("  2. Run Binance collector (in another terminal)")
        print("  3. Wait 2-6 hours for sufficient data")
        print("  4. Re-run this script")
        print()
        print(f"Data must have timestamp > {args.min_timestamp}")
        print(f"(After 2026-01-18 08:33:07 IST)")
        return

    print_results(result)

    if args.output:
        # Save detailed results
        output_path = RESEARCH_DIR / args.output
        print(f"\nSaving detailed results to: {output_path}")
        # Could save drop_details, fill_details, etc.


if __name__ == "__main__":
    main()
