#!/usr/bin/env python3
"""
Signal Accuracy Analyzer - Phase 0 Signal Study

Pure signal analysis WITHOUT trading/PnL mechanics.
Goal: Determine if signal quality is the issue or execution mechanics.

Key Questions:
1. Does composite score predict future price movement?
2. Is BTC correlated with Polymarket spread changes?
3. Which lookback period has best direction accuracy?
4. How long does a signal remain valid (signal decay)?

Usage:
    python research/signal_accuracy_analyzer.py --output research/signal_study_results.csv
"""

import argparse
import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from datetime import datetime
import scipy.stats as stats
from collections import defaultdict
import warnings
warnings.filterwarnings('ignore')


# =============================================================================
# CONSTANTS (from spike_param_optimizer.py)
# =============================================================================

MIN_TIME = 60  # Minimum seconds remaining
MIN_RUNTIME_SECS = 300  # Minimum market duration

# Spike detection
SPIKE_THRESHOLD = 0.02  # Base threshold

# Enhanced filtering
VELOCITY_CONFIRM_THRESHOLD = 0.10
ENHANCED_SCORE_THRESHOLD = 0.40

# ATR for adaptive volatility
ATR_PERIOD = 14
ATR_WINDOW = 300
LOW_PERCENTILE = 25
HIGH_PERCENTILE = 75
REGIME_THRESHOLDS = {
    "LOW": 0.010,
    "MEDIUM": 0.020,
    "HIGH": 0.035,
}

# Forward-looking analysis windows (in seconds)
FORWARD_WINDOWS = [5, 10, 30, 60, 300]  # 5s, 10s, 30s, 60s, 5min

# Lookbacks by path (ticks at 60Hz)
PATH1_LOOKBACKS = [48, 60, 72]  # 800ms, 1000ms, 1200ms
PATH2_LOOKBACKS = [18, 24, 30]  # 300ms, 400ms, 500ms
ALL_LOOKBACKS = [18, 24, 30, 48, 60, 72]  # All lookbacks combined


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class SignalEvent:
    """A detected signal event with forward-looking outcomes."""
    timestamp_ms: int
    lookback_ticks: int
    lookback_ms: int
    market_slug: str
    predicted_side: str  # UP or DOWN
    spike_magnitude: float
    velocity_bps: float
    composite_score: float
    time_remaining: float
    regime: str

    # BTC data at signal time
    btc_price: float
    btc_change_pct: float  # Change over lookback

    # Polymarket data at signal time
    spread_at_signal: float  # up_ask - down_bid (or vice versa)

    # Forward outcomes (filled after detection)
    direction_5s: Optional[bool] = None
    direction_10s: Optional[bool] = None
    direction_30s: Optional[bool] = None
    direction_60s: Optional[bool] = None
    direction_300s: Optional[bool] = None
    direction_resolution: Optional[bool] = None

    # Price movements (for regression)
    spread_change_5s: Optional[float] = None
    spread_change_10s: Optional[float] = None
    spread_change_30s: Optional[float] = None
    spread_change_60s: Optional[float] = None
    spread_change_300s: Optional[float] = None

    # Resolution outcome
    resolution_winner: Optional[str] = None
    resolution_pnl: Optional[float] = None  # Simulated PnL if held to resolution


# =============================================================================
# SPIKE DETECTION (from spike_param_optimizer.py)
# =============================================================================

def calculate_rolling_atr(prices: pd.Series, period: int = ATR_PERIOD) -> pd.Series:
    """Calculate rolling ATR (using price changes as proxy for true range)."""
    tr = prices.diff().abs()
    atr = tr.rolling(window=period).mean()
    return atr


def classify_regime_vectorized(atr_series: pd.Series, window: int = ATR_WINDOW) -> pd.Series:
    """Classify volatility regime for each point based on ATR percentile."""
    percentile = atr_series.rolling(window=window, min_periods=window//2).apply(
        lambda x: (pd.Series(x).rank().iloc[-1] / len(x)) * 100, raw=False
    )

    regime = pd.Series('MEDIUM', index=atr_series.index)
    regime[percentile < LOW_PERCENTILE] = 'LOW'
    regime[percentile > HIGH_PERCENTILE] = 'HIGH'

    return regime


def detect_spikes_for_lookback(btc_df: pd.DataFrame, lookback: int,
                               adaptive_volatility: bool = True) -> pd.DataFrame:
    """Detect spikes using specified lookback period."""
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    # Calculate % change over lookback period
    df['price_prev'] = df['price'].shift(lookback)
    df['change_pct'] = (df['price'] - df['price_prev']) / df['price_prev'] * 100
    df['magnitude'] = df['change_pct'].abs()

    if adaptive_volatility:
        df['atr'] = calculate_rolling_atr(df['price'])
        df['regime'] = classify_regime_vectorized(df['atr'])
        df['threshold'] = df['regime'].map(REGIME_THRESHOLDS)
        df['threshold'] = df['threshold'].fillna(SPIKE_THRESHOLD)
        df['spike_detected'] = df['magnitude'] >= df['threshold']
    else:
        df['spike_detected'] = df['magnitude'] >= SPIKE_THRESHOLD
        df['regime'] = 'MEDIUM'
        df['threshold'] = SPIKE_THRESHOLD

    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['change_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['change_pct'] < 0), 'spike_direction'] = 'DOWN'
    df['spike_magnitude'] = df['magnitude'].where(df['spike_detected'], 0)

    return df


def velocity_confirms(spike_dir: str, velocity_bps: float) -> bool:
    """Check if velocity confirms spike direction."""
    if spike_dir == "UP":
        return velocity_bps > -VELOCITY_CONFIRM_THRESHOLD
    elif spike_dir == "DOWN":
        return velocity_bps < VELOCITY_CONFIRM_THRESHOLD
    return True


def compute_score(spike_mag: float, velocity_bps: float, spike_dir: str,
                  time_rem: float, regime: str = "MEDIUM") -> Tuple[float, Dict[str, float]]:
    """
    Compute enhanced signal score v2 (interaction-based).

    Based on statistical analysis findings:
    - Current additive formula has R²=0.017, p=0.85 (useless)
    - Spike × Velocity interaction is significant (p=0.001)
    - Time window 300-600s has 88.9% accuracy
    - HIGH regime bonus, LOW regime already filtered
    """
    # Legacy components (kept for backward compatibility in analysis)
    spike_score = min(spike_mag / 0.05, 1.0)
    velocity_score = min(abs(velocity_bps) / 0.50, 1.0)
    vel_confirms = (spike_dir == "UP" and velocity_bps > 0) or \
                   (spike_dir == "DOWN" and velocity_bps < 0)
    confirm_bonus = 1.0 if vel_confirms else 0.0
    urgency = 1.0 - min(time_rem / 900.0, 1.0)

    # Legacy score (for comparison)
    legacy_score = 0.40 * spike_score + 0.30 * velocity_score + 0.20 * confirm_bonus + 0.10 * urgency

    # NEW V2 SCORE: Interaction-based
    # Regime weight
    if regime == "LOW":
        regime_weight = 0.0  # Should be filtered upstream
    elif regime == "HIGH":
        regime_weight = 1.2
    else:
        regime_weight = 1.0

    # Time window weight
    if 300 <= time_rem <= 600:
        time_weight = 1.0  # Optimal
    elif 180 <= time_rem <= 750:
        time_weight = 0.6
    else:
        time_weight = 0.3

    # Core: interaction effect (p=0.001)
    interaction = spike_mag * abs(velocity_bps)
    v2_score = interaction * time_weight * regime_weight

    # Return v2 score as the composite, but include both in components
    components = {
        'spike_score': spike_score,
        'velocity_score': velocity_score,
        'confirm_bonus': confirm_bonus,
        'urgency_score': urgency,
        'legacy_score': legacy_score,
        'interaction': interaction,
        'time_weight': time_weight,
        'regime_weight': regime_weight,
        'v2_score': v2_score
    }

    return legacy_score, components  # Return legacy for now to not break existing analysis


# =============================================================================
# DATA LOADING (from spike_param_optimizer.py)
# =============================================================================

def load_data(btc_file=None, obs_file=None, res_file=None):
    """Load Binance and observer data."""
    print("Loading data...")

    # Load Binance - load ALL files if no specific file given
    if btc_file:
        btc_path = Path(btc_file)
        btc_df = pd.read_csv(btc_path)
        print(f"  Binance: {len(btc_df):,} rows ({btc_path.name})")
    else:
        btc_dir = Path("research/binance_hf")
        if not btc_dir.exists():
            btc_dir = Path("/Users/rananjaybika/polymarket-amm-bot/research/binance_hf")

        btc_dfs = []
        for f in sorted(btc_dir.glob("btc_prices_*.csv")):
            df = pd.read_csv(f)
            btc_dfs.append(df)
            print(f"  Binance: {len(df):,} rows ({f.name})")
        btc_df = pd.concat(btc_dfs, ignore_index=True)
        btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
        print(f"  Binance TOTAL: {len(btc_df):,} rows")

    # Load observer
    if obs_file:
        obs_df = pd.read_csv(obs_file, on_bad_lines='skip', low_memory=False)
        obs_dir = Path(obs_file).parent
        print(f"  Observer: {len(obs_df):,} rows ({Path(obs_file).name})")
    else:
        obs_dir = Path("research/observer")
        if not obs_dir.exists():
            obs_dir = Path("/Users/rananjaybika/polymarket-amm-bot/research/observer")

        obs_dfs = []
        for f in sorted(obs_dir.glob("grid_obs_*.csv")):
            df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)
        obs_df = pd.concat(obs_dfs, ignore_index=True)
        obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
        print(f"  Observer: {len(obs_df):,} rows")

    # Load resolutions
    if res_file:
        res_path = Path(res_file)
    else:
        res_path = obs_dir / "market_resolutions_verified.csv"
        if not res_path.exists():
            res_path = Path("/Users/rananjaybika/polymarket-amm-bot/research/observer/market_resolutions_verified.csv")
    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Find actual binance time ranges (may have gaps)
    btc_df = btc_df.sort_values('timestamp_ms')

    # Detect gaps > 5 minutes in binance data
    btc_df['time_diff'] = btc_df['timestamp_ms'].diff()
    gap_threshold = 5 * 60 * 1000  # 5 minutes
    gaps = btc_df[btc_df['time_diff'] > gap_threshold]

    if len(gaps) > 0:
        print(f"  WARNING: {len(gaps)} gaps detected in binance data")

    # Build list of valid time ranges (between gaps)
    valid_ranges = []
    range_start = btc_df['timestamp_ms'].iloc[0]
    for _, gap in gaps.iterrows():
        range_end = gap['timestamp_ms'] - gap['time_diff']
        valid_ranges.append((range_start, range_end))
        range_start = gap['timestamp_ms']
    valid_ranges.append((range_start, btc_df['timestamp_ms'].iloc[-1]))

    total_btc_hours = sum((e - s) / 3600000 for s, e in valid_ranges)
    print(f"  Binance coverage: {total_btc_hours:.2f} hours across {len(valid_ranges)} sessions")

    # Add resolutions
    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    # Filter markets that have actual binance coverage
    valid_slugs = []
    for slug, mdf in obs_df.groupby('market_slug'):
        max_time = mdf['time_remaining_secs'].max()
        min_time = mdf['time_remaining_secs'].min()
        duration = max_time - min_time

        if duration < MIN_RUNTIME_SECS or max_time < 840:
            continue

        # Check if market has binance data coverage
        m_start = mdf['timestamp_ms'].min()
        m_end = mdf['timestamp_ms'].max()

        has_coverage = False
        for r_start, r_end in valid_ranges:
            overlap_start = max(m_start, r_start)
            overlap_end = min(m_end, r_end)
            if overlap_end > overlap_start:
                coverage = (overlap_end - overlap_start) / (m_end - m_start)
                if coverage > 0.8:
                    has_coverage = True
                    break

        if has_coverage:
            valid_slugs.append(slug)

    obs_df = obs_df[obs_df['market_slug'].isin(valid_slugs)]

    # Filter btc_df to valid ranges only
    btc_mask = pd.Series(False, index=btc_df.index)
    for r_start, r_end in valid_ranges:
        btc_mask |= (btc_df['timestamp_ms'] >= r_start) & (btc_df['timestamp_ms'] <= r_end)
    btc_df = btc_df[btc_mask].drop(columns=['time_diff'])

    hours = total_btc_hours if len(valid_slugs) > 0 else 0

    print(f"  Valid markets (with binance coverage): {len(valid_slugs)}")

    return btc_df, obs_df, hours, res_map, valid_ranges


# =============================================================================
# SIGNAL ANALYSIS
# =============================================================================

def analyze_market_signals(
    spikes_df: pd.DataFrame,
    obs_df: pd.DataFrame,
    slug: str,
    resolution: str,
    lookback: int
) -> List[SignalEvent]:
    """Analyze signals for a single market at a specific lookback.

    NOTE: spikes_df should be pre-computed for this lookback (not computed per-market).
    """

    # Filter market data
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    # Filter pre-computed spikes to this market's time range
    market_spikes = spikes_df[
        (spikes_df['timestamp_ms'] >= market_start) &
        (spikes_df['timestamp_ms'] <= market_end)
    ].copy()

    signals = []
    last_signal_ts = 0
    min_gap_ms = 1000  # Minimum gap between signals

    for _, spike_row in market_spikes.iterrows():
        spike_ts = spike_row['timestamp_ms']
        spike_dir = spike_row['spike_direction']
        spike_mag = spike_row['spike_magnitude']
        regime = spike_row.get('regime', 'MEDIUM')

        # Check cycle gap
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

        velocity_bps = obs_row.get('velocity_bps', 0) or 0

        # Apply enhanced signal filter
        if not velocity_confirms(spike_dir, velocity_bps):
            continue

        score, components = compute_score(spike_mag, velocity_bps, spike_dir, time_rem)
        if score < ENHANCED_SCORE_THRESHOLD:
            continue

        # Calculate spread at signal time
        if spike_dir == 'UP':
            spread = obs_row['up_ask'] - obs_row['down_bid']
        else:
            spread = obs_row['down_ask'] - obs_row['up_bid']

        # Create signal event
        signal = SignalEvent(
            timestamp_ms=spike_ts,
            lookback_ticks=lookback,
            lookback_ms=int(lookback * 1000 / 60),
            market_slug=slug,
            predicted_side=spike_dir,
            spike_magnitude=spike_mag,
            velocity_bps=velocity_bps,
            composite_score=score,
            time_remaining=time_rem,
            regime=regime,
            btc_price=spike_row['price'],
            btc_change_pct=spike_row.get('change_pct', 0),
            spread_at_signal=spread,
            resolution_winner=resolution
        )

        # Look ahead to calculate forward outcomes
        for window in FORWARD_WINDOWS:
            window_ms = window * 1000
            target_ts = spike_ts + window_ms

            # Find observer row at target time
            future_idx = mdf['timestamp_ms'].searchsorted(target_ts)
            if future_idx >= len(mdf):
                future_idx = len(mdf) - 1

            future_row = mdf.iloc[future_idx]

            # Calculate spread change
            if spike_dir == 'UP':
                future_spread = future_row['up_ask'] - future_row['down_bid']
                # Direction correct if UP side price increased (or DOWN decreased)
                direction_correct = future_row['up_bid'] > obs_row['up_ask']
            else:
                future_spread = future_row['down_ask'] - future_row['up_bid']
                direction_correct = future_row['down_bid'] > obs_row['down_ask']

            spread_change = future_spread - spread

            # Set attributes based on window
            if window == 5:
                signal.direction_5s = direction_correct
                signal.spread_change_5s = spread_change
            elif window == 10:
                signal.direction_10s = direction_correct
                signal.spread_change_10s = spread_change
            elif window == 30:
                signal.direction_30s = direction_correct
                signal.spread_change_30s = spread_change
            elif window == 60:
                signal.direction_60s = direction_correct
                signal.spread_change_60s = spread_change
            elif window == 300:
                signal.direction_300s = direction_correct
                signal.spread_change_300s = spread_change

        # Resolution outcome
        signal.direction_resolution = (resolution == spike_dir)

        # Simulated resolution PnL (buy at ask, resolution determines outcome)
        if spike_dir == 'UP':
            entry_price = obs_row['up_ask']
        else:
            entry_price = obs_row['down_ask']

        if signal.direction_resolution:
            signal.resolution_pnl = 1.0 - entry_price  # Win: get $1 per share
        else:
            signal.resolution_pnl = -entry_price  # Lose: lose entry cost

        signals.append(signal)
        last_signal_ts = spike_ts

    return signals


def precompute_spikes(btc_df: pd.DataFrame, lookbacks: List[int]) -> Dict[int, pd.DataFrame]:
    """Pre-compute spikes for all lookback values (major optimization)."""
    print("\nPre-computing spikes for all lookback values...")
    spikes_by_lookback = {}

    for lookback in lookbacks:
        ms = lookback * 1000 // 60
        print(f"  Lookback {lookback} ticks ({ms}ms)...", end=' ', flush=True)
        spikes_df = detect_spikes_for_lookback(btc_df, lookback, adaptive_volatility=True)
        # Filter out LOW regime spikes (48% accuracy = worse than coin flip)
        spikes_only = spikes_df[
            (spikes_df['spike_detected'] == True) &
            (spikes_df['regime'] != 'LOW')
        ].copy()
        spikes_by_lookback[lookback] = spikes_only
        print(f"{len(spikes_only):,} spikes (excl. LOW regime)")

    return spikes_by_lookback


def run_signal_analysis(
    btc_df: pd.DataFrame,
    obs_df: pd.DataFrame,
    res_map: Dict[str, str],
    lookbacks: List[int] = None
) -> Dict[int, List[SignalEvent]]:
    """Run signal analysis across all markets and lookbacks."""

    if lookbacks is None:
        lookbacks = ALL_LOOKBACKS

    results = {lb: [] for lb in lookbacks}

    # Pre-compute spikes for all lookbacks ONCE (major optimization)
    spikes_by_lookback = precompute_spikes(btc_df, lookbacks)

    # Get unique markets
    markets = obs_df['market_slug'].unique()
    print(f"\nAnalyzing {len(markets)} markets across {len(lookbacks)} lookback values...")

    for i, slug in enumerate(markets):
        resolution = res_map.get(slug, 'UP')

        for lookback in lookbacks:
            spikes_df = spikes_by_lookback[lookback]
            signals = analyze_market_signals(spikes_df, obs_df, slug, resolution, lookback)
            results[lookback].extend(signals)

        if (i + 1) % 10 == 0:
            print(f"  Processed {i+1}/{len(markets)} markets...")

    # Print summary
    print("\nSignals detected by lookback:")
    for lb in lookbacks:
        ms = lb * 1000 // 60
        print(f"  {ms}ms ({lb} ticks): {len(results[lb])} signals")

    return results


# =============================================================================
# STATISTICAL ANALYSIS
# =============================================================================

def calculate_direction_accuracy(signals: List[SignalEvent]) -> Dict[str, float]:
    """Calculate direction accuracy at different time windows."""
    if not signals:
        return {}

    results = {}

    # Direction accuracy by window
    for window_name, attr_name in [
        ('5s', 'direction_5s'),
        ('10s', 'direction_10s'),
        ('30s', 'direction_30s'),
        ('60s', 'direction_60s'),
        ('300s', 'direction_300s'),
        ('resolution', 'direction_resolution')
    ]:
        # Use == True instead of 'is True' to handle numpy bools
        correct = sum(1 for s in signals if getattr(s, attr_name, None) == True)
        total = sum(1 for s in signals if getattr(s, attr_name, None) is not None)
        results[f'accuracy_{window_name}'] = correct / total if total > 0 else 0
        results[f'n_{window_name}'] = total

    return results


def calculate_correlations(signals: List[SignalEvent]) -> Dict[str, float]:
    """Calculate BTC-Polymarket and score-outcome correlations."""
    if len(signals) < 3:
        return {}

    results = {}

    # Extract arrays
    btc_changes = [s.btc_change_pct for s in signals if s.btc_change_pct is not None]
    spread_changes_30s = [s.spread_change_30s for s in signals if s.spread_change_30s is not None]
    composite_scores = [s.composite_score for s in signals]
    resolution_pnl = [s.resolution_pnl for s in signals if s.resolution_pnl is not None]

    # BTC-Polymarket correlation (30s window)
    if len(btc_changes) > 2 and len(spread_changes_30s) == len(btc_changes):
        try:
            corr, p_val = stats.pearsonr(btc_changes, spread_changes_30s)
            results['btc_polymarket_corr'] = corr
            results['btc_polymarket_pval'] = p_val
        except:
            pass

    # Composite score vs resolution PnL regression
    if len(composite_scores) > 2 and len(resolution_pnl) == len(composite_scores):
        try:
            corr, p_val = stats.pearsonr(composite_scores, resolution_pnl)
            results['score_pnl_corr'] = corr
            results['score_pnl_pval'] = p_val

            # Linear regression for R²
            slope, intercept, r_value, p_value, std_err = stats.linregress(
                composite_scores, resolution_pnl
            )
            results['score_pnl_r2'] = r_value ** 2
            results['score_pnl_slope'] = slope
        except:
            pass

    return results


def calculate_component_analysis(signals: List[SignalEvent]) -> Dict[str, Dict]:
    """Analyze individual score components vs outcome."""
    if len(signals) < 3:
        return {}

    results = {}

    # Re-compute component scores
    spike_scores = []
    velocity_scores = []
    confirm_bonuses = []
    urgency_scores = []
    resolution_outcomes = []

    for s in signals:
        _, components = compute_score(s.spike_magnitude, s.velocity_bps,
                                       s.predicted_side, s.time_remaining)
        spike_scores.append(components['spike_score'])
        velocity_scores.append(components['velocity_score'])
        confirm_bonuses.append(components['confirm_bonus'])
        urgency_scores.append(components['urgency_score'])
        if s.direction_resolution is not None:
            resolution_outcomes.append(1.0 if s.direction_resolution else 0.0)
        else:
            resolution_outcomes.append(np.nan)

    # Filter out NaN
    valid_idx = [i for i, v in enumerate(resolution_outcomes) if not np.isnan(v)]

    if len(valid_idx) < 3:
        return results

    # Calculate correlation for each component
    for name, scores in [
        ('spike', spike_scores),
        ('velocity', velocity_scores),
        ('confirm', confirm_bonuses),
        ('urgency', urgency_scores)
    ]:
        valid_scores = [scores[i] for i in valid_idx]
        valid_outcomes = [resolution_outcomes[i] for i in valid_idx]

        try:
            corr, p_val = stats.pearsonr(valid_scores, valid_outcomes)
            results[name] = {
                'correlation': corr,
                'p_value': p_val,
                'predictive': abs(corr) > 0.1 and p_val < 0.05
            }
        except:
            pass

    return results


def calculate_regime_analysis(signals: List[SignalEvent]) -> Dict[str, Dict]:
    """Analyze signal accuracy by volatility regime."""
    results = {}

    for regime in ['LOW', 'MEDIUM', 'HIGH']:
        regime_signals = [s for s in signals if s.regime == regime]
        if regime_signals:
            accuracy = calculate_direction_accuracy(regime_signals)
            results[regime] = {
                'n_signals': len(regime_signals),
                'accuracy_30s': accuracy.get('accuracy_30s', 0),
                'accuracy_60s': accuracy.get('accuracy_60s', 0),
                'accuracy_resolution': accuracy.get('accuracy_resolution', 0)
            }

    return results


def calculate_signal_decay(signals: List[SignalEvent]) -> Dict[str, float]:
    """Calculate how signal accuracy decays over time."""
    if not signals:
        return {}

    results = {}

    # Accuracy at each window
    windows = ['5s', '10s', '30s', '60s', '300s', 'resolution']
    for window in windows:
        # Use == True instead of 'is True' to handle numpy bools
        correct = sum(1 for s in signals if getattr(s, f'direction_{window}', None) == True)
        total = sum(1 for s in signals if getattr(s, f'direction_{window}', None) is not None)
        results[window] = correct / total if total > 0 else 0

    return results


# =============================================================================
# REPORT GENERATION
# =============================================================================

def print_comprehensive_report(
    results_by_lookback: Dict[int, List[SignalEvent]],
    hours: float
):
    """Print comprehensive signal analysis report."""

    print("\n" + "=" * 100)
    print("SIGNAL ACCURACY ANALYSIS REPORT")
    print("=" * 100)
    print(f"\nData Duration: {hours:.2f} hours")

    # Direction Accuracy by Lookback
    print("\n" + "-" * 100)
    print("DIRECTION ACCURACY BY LOOKBACK")
    print("-" * 100)
    print(f"\n{'Lookback':<12} {'Signals':<10} {'5s':<10} {'10s':<10} {'30s':<10} {'60s':<10} {'5min':<10} {'Resolve':<10}")
    print("-" * 92)

    for lookback in sorted(results_by_lookback.keys()):
        signals = results_by_lookback[lookback]
        if not signals:
            continue

        ms = lookback * 1000 // 60
        accuracy = calculate_direction_accuracy(signals)

        print(f"{ms}ms ({lookback}t) {len(signals):<10} "
              f"{accuracy.get('accuracy_5s', 0)*100:>5.1f}%    "
              f"{accuracy.get('accuracy_10s', 0)*100:>5.1f}%    "
              f"{accuracy.get('accuracy_30s', 0)*100:>5.1f}%    "
              f"{accuracy.get('accuracy_60s', 0)*100:>5.1f}%    "
              f"{accuracy.get('accuracy_300s', 0)*100:>5.1f}%    "
              f"{accuracy.get('accuracy_resolution', 0)*100:>5.1f}%")

    # Best Lookback Analysis
    print("\n" + "-" * 100)
    print("BEST LOOKBACK ANALYSIS")
    print("-" * 100)

    best_by_window = {}
    for window in ['5s', '10s', '30s', '60s', '300s', 'resolution']:
        best_acc = 0
        best_lb = None
        for lb, signals in results_by_lookback.items():
            if signals:
                acc = calculate_direction_accuracy(signals).get(f'accuracy_{window}', 0)
                if acc > best_acc:
                    best_acc = acc
                    best_lb = lb
        if best_lb:
            ms = best_lb * 1000 // 60
            best_by_window[window] = (ms, best_acc)
            print(f"  Best for {window:>10}: {ms}ms lookback ({best_acc*100:.1f}% accuracy)")

    # Correlation Analysis (use combined signals)
    all_signals = []
    for signals in results_by_lookback.values():
        all_signals.extend(signals)

    if all_signals:
        print("\n" + "-" * 100)
        print("CORRELATION ANALYSIS")
        print("-" * 100)

        corrs = calculate_correlations(all_signals)
        if corrs:
            print(f"\n  BTC-Polymarket Correlation (30s): {corrs.get('btc_polymarket_corr', 0):.4f} "
                  f"(p={corrs.get('btc_polymarket_pval', 1):.4f})")
            print(f"  Score-PnL Correlation:            {corrs.get('score_pnl_corr', 0):.4f} "
                  f"(p={corrs.get('score_pnl_pval', 1):.4f})")
            print(f"  Score-PnL R²:                     {corrs.get('score_pnl_r2', 0):.4f}")

    # Component Analysis
    print("\n" + "-" * 100)
    print("COMPONENT ANALYSIS (correlation with resolution outcome)")
    print("-" * 100)

    # Use shortest available lookback for component analysis (highest quality signals)
    shortest_lb = min(results_by_lookback.keys()) if results_by_lookback else None
    if shortest_lb and results_by_lookback[shortest_lb]:
        ms = shortest_lb * 1000 // 60
        print(f"\n  Using {ms}ms lookback for component analysis:")
        comp_analysis = calculate_component_analysis(results_by_lookback[shortest_lb])
        print(f"\n  {'Component':<15} {'Correlation':<15} {'P-Value':<15} {'Predictive?':<12}")
        print("  " + "-" * 55)
        for name, data in comp_analysis.items():
            predictive = "YES" if data.get('predictive', False) else "no"
            print(f"  {name:<15} {data.get('correlation', 0):>10.4f}     "
                  f"{data.get('p_value', 1):>10.4f}     {predictive}")

    # Regime Analysis
    print("\n" + "-" * 100)
    print("REGIME ANALYSIS")
    print("-" * 100)

    for lb in sorted(results_by_lookback.keys()):  # Test at all available lookbacks
        if lb not in results_by_lookback or not results_by_lookback[lb]:
            continue

        ms = lb * 1000 // 60
        regime_analysis = calculate_regime_analysis(results_by_lookback[lb])

        if regime_analysis:
            print(f"\n  Lookback {ms}ms:")
            print(f"  {'Regime':<10} {'Signals':<10} {'30s Acc':<12} {'60s Acc':<12} {'Resolution':<12}")
            print("  " + "-" * 55)
            for regime in ['LOW', 'MEDIUM', 'HIGH']:
                if regime in regime_analysis:
                    data = regime_analysis[regime]
                    print(f"  {regime:<10} {data['n_signals']:<10} "
                          f"{data['accuracy_30s']*100:>6.1f}%      "
                          f"{data['accuracy_60s']*100:>6.1f}%      "
                          f"{data['accuracy_resolution']*100:>6.1f}%")

    # Signal Decay Analysis
    print("\n" + "-" * 100)
    print("SIGNAL DECAY ANALYSIS")
    print("-" * 100)

    for lb in sorted(results_by_lookback.keys()):
        if lb not in results_by_lookback or not results_by_lookback[lb]:
            continue

        ms = lb * 1000 // 60
        decay = calculate_signal_decay(results_by_lookback[lb])

        print(f"\n  Lookback {ms}ms:")
        print(f"  Time Window: ", end="")
        for window in ['5s', '10s', '30s', '60s', '300s', 'resolution']:
            print(f"{window:>10}", end="")
        print()
        print(f"  Accuracy:    ", end="")
        for window in ['5s', '10s', '30s', '60s', '300s', 'resolution']:
            print(f"{decay.get(window, 0)*100:>9.1f}%", end="")
        print()

    # Diagnosis
    print("\n" + "=" * 100)
    print("DIAGNOSIS")
    print("=" * 100)

    # Find best resolution accuracy
    best_resolution_acc = 0
    best_resolution_lb = None
    for lb, signals in results_by_lookback.items():
        if signals:
            acc = calculate_direction_accuracy(signals).get('accuracy_resolution', 0)
            if acc > best_resolution_acc:
                best_resolution_acc = acc
                best_resolution_lb = lb

    if best_resolution_lb:
        ms = best_resolution_lb * 1000 // 60
        print(f"\n  Best Resolution Accuracy: {best_resolution_acc*100:.1f}% at {ms}ms lookback")

        if best_resolution_acc < 0.50:
            print("\n  >>> SIGNAL FLAW DETECTED <<<")
            print("  Direction accuracy is WORSE than random (< 50%)")
            print("  The signal does NOT predict future direction.")
            print("  Recommendation: Redesign signal detection entirely")
        elif best_resolution_acc < 0.55:
            print("\n  >>> WEAK SIGNAL <<<")
            print("  Direction accuracy is barely above random (50-55%)")
            print("  The signal has marginal predictive value.")
            print("  Recommendation: Test alternative signal sources")
        else:
            print("\n  >>> SIGNAL IS VIABLE <<<")
            print(f"  Direction accuracy ({best_resolution_acc*100:.1f}%) shows predictive power")
            print("  Issue may be in execution mechanics, not signal quality")
            print("  Recommendation: Optimize execution timing and hedge mechanics")

    # Check BTC-Polymarket correlation
    if all_signals:
        corrs = calculate_correlations(all_signals)
        btc_corr = corrs.get('btc_polymarket_corr', 0)

        print(f"\n  BTC-Polymarket Correlation: {btc_corr:.4f}")
        if abs(btc_corr) < 0.1:
            print("  >>> LOW CORRELATION <<<")
            print("  BTC price moves are NOT strongly linked to Polymarket spreads")
            print("  The fundamental signal hypothesis may be flawed")
        elif abs(btc_corr) < 0.3:
            print("  >>> WEAK CORRELATION <<<")
            print("  Some relationship exists but it's not strong")
            print("  Consider additional signal confirmation sources")
        else:
            print("  >>> REASONABLE CORRELATION <<<")
            print("  BTC moves have meaningful relationship with spreads")


def signals_to_dataframe(results_by_lookback: Dict[int, List[SignalEvent]]) -> pd.DataFrame:
    """Convert signals to a DataFrame for CSV export."""
    rows = []

    for lookback, signals in results_by_lookback.items():
        for s in signals:
            rows.append({
                'lookback_ticks': s.lookback_ticks,
                'lookback_ms': s.lookback_ms,
                'timestamp_ms': s.timestamp_ms,
                'market_slug': s.market_slug,
                'predicted_side': s.predicted_side,
                'resolution_winner': s.resolution_winner,
                'spike_magnitude': s.spike_magnitude,
                'velocity_bps': s.velocity_bps,
                'composite_score': s.composite_score,
                'time_remaining': s.time_remaining,
                'regime': s.regime,
                'btc_price': s.btc_price,
                'btc_change_pct': s.btc_change_pct,
                'spread_at_signal': s.spread_at_signal,
                'direction_5s': s.direction_5s,
                'direction_10s': s.direction_10s,
                'direction_30s': s.direction_30s,
                'direction_60s': s.direction_60s,
                'direction_300s': s.direction_300s,
                'direction_resolution': s.direction_resolution,
                'spread_change_5s': s.spread_change_5s,
                'spread_change_10s': s.spread_change_10s,
                'spread_change_30s': s.spread_change_30s,
                'spread_change_60s': s.spread_change_60s,
                'spread_change_300s': s.spread_change_300s,
                'resolution_pnl': s.resolution_pnl,
            })

    return pd.DataFrame(rows)


# =============================================================================
# MAIN
# =============================================================================

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Signal Accuracy Analyzer - Phase 0 Signal Study",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument("--output", type=str, default="research/signal_study_results.csv",
                        help="Output CSV file path")
    parser.add_argument("--btc-file", type=str, default=None,
                        help="Path to Binance BTC price CSV file")
    parser.add_argument("--obs-file", type=str, default=None,
                        help="Path to observer CSV file")
    parser.add_argument("--res-file", type=str, default=None,
                        help="Path to market resolutions CSV file")
    parser.add_argument("--lookbacks", type=str, default=None,
                        help="Comma-separated lookback values in ticks (overrides --path)")
    parser.add_argument("--path", type=str, default="both",
                        choices=["path1", "path2", "both"],
                        help="path1=800-1200ms, path2=300-500ms, both=all lookbacks")

    return parser.parse_args()


def main():
    args = parse_args()

    print("=" * 100)
    print("SIGNAL ACCURACY ANALYZER - Phase 0 Signal Study")
    print("=" * 100)
    print("\nObjective: Determine if signal quality is the issue or execution mechanics")
    print()

    # Parse lookbacks based on path or explicit list
    if args.lookbacks:
        lookbacks = [int(x.strip()) for x in args.lookbacks.split(',')]
        path_name = "Custom"
    elif args.path == "path1":
        lookbacks = PATH1_LOOKBACKS
        path_name = "PATH 1 (800-1200ms)"
    elif args.path == "path2":
        lookbacks = PATH2_LOOKBACKS
        path_name = "PATH 2 (300-500ms)"
    else:
        lookbacks = ALL_LOOKBACKS
        path_name = "ALL LOOKBACKS"

    print(f"  Path:          {path_name}")

    # Load data
    btc_df, obs_df, hours, res_map, valid_ranges = load_data(
        btc_file=args.btc_file,
        obs_file=args.obs_file,
        res_file=args.res_file
    )

    n_markets = obs_df['market_slug'].nunique()
    print(f"\nAnalysis Parameters:")
    print(f"  Data Duration: {hours:.2f} hours")
    print(f"  Markets:       {n_markets}")
    print(f"  Lookbacks:     {[lb * 1000 // 60 for lb in lookbacks]}ms")

    # Run signal analysis
    results_by_lookback = run_signal_analysis(btc_df, obs_df, res_map, lookbacks)

    # Print comprehensive report
    print_comprehensive_report(results_by_lookback, hours)

    # Save results
    df = signals_to_dataframe(results_by_lookback)
    df.to_csv(args.output, index=False)
    print(f"\n\nResults saved to: {args.output}")
    print(f"Total signals analyzed: {len(df)}")

    # Summary statistics
    print("\n" + "=" * 100)
    print("SUMMARY BY LOOKBACK")
    print("=" * 100)

    summary_rows = []
    for lb in sorted(results_by_lookback.keys()):
        signals = results_by_lookback[lb]
        if not signals:
            continue

        ms = lb * 1000 // 60
        accuracy = calculate_direction_accuracy(signals)
        corrs = calculate_correlations(signals)

        summary_rows.append({
            'lookback_ms': ms,
            'n_signals': len(signals),
            'accuracy_5s': accuracy.get('accuracy_5s', 0),
            'accuracy_30s': accuracy.get('accuracy_30s', 0),
            'accuracy_60s': accuracy.get('accuracy_60s', 0),
            'accuracy_resolution': accuracy.get('accuracy_resolution', 0),
            'btc_corr': corrs.get('btc_polymarket_corr', 0),
            'score_r2': corrs.get('score_pnl_r2', 0),
        })

    summary_df = pd.DataFrame(summary_rows)
    summary_path = args.output.replace('.csv', '_summary.csv')
    summary_df.to_csv(summary_path, index=False)
    print(f"Summary saved to: {summary_path}")

    # Run multiple regression analysis
    run_multiple_regression_analysis(df)

    print("\n" + "=" * 100)


def run_multiple_regression_analysis(df: pd.DataFrame) -> None:
    """
    Run proper multiple regression analysis on signal data.

    This should have been done from the start - we have multiple independent
    variables (spike_magnitude, velocity_bps, time_remaining, etc.) predicting
    a binary outcome (direction_resolution).
    """
    try:
        import statsmodels.api as sm
    except ImportError:
        print("\n[WARNING] statsmodels not installed - skipping regression analysis")
        return

    if len(df) < 20:
        print("\n[WARNING] Too few signals for regression analysis")
        return

    print("\n" + "=" * 100)
    print("MULTIPLE REGRESSION ANALYSIS")
    print("=" * 100)

    # Prepare data
    y = (df['direction_resolution'] == True).astype(int)
    features = ['spike_magnitude', 'velocity_bps', 'composite_score', 'time_remaining']

    # Check which features exist
    available_features = [f for f in features if f in df.columns]
    if len(available_features) < 2:
        print("\n[WARNING] Not enough features for regression")
        return

    X = df[available_features].fillna(0)
    X_const = sm.add_constant(X)

    # OLS Regression
    print("\n--- OLS REGRESSION: direction_resolution ~ features ---")
    try:
        model = sm.OLS(y, X_const).fit()
        print(f"\n  R²:          {model.rsquared:.4f}")
        print(f"  Adjusted R²: {model.rsquared_adj:.4f}")
        print(f"  F-statistic: {model.fvalue:.4f} (p={model.f_pvalue:.4f})")

        print(f"\n  {'Variable':<20} {'Coef':<12} {'P>|t|':<10} {'Significant?'}")
        print("  " + "-" * 55)
        for var in ['const'] + available_features:
            coef = model.params[var]
            p = model.pvalues[var]
            sig = "***" if p < 0.01 else "**" if p < 0.05 else "*" if p < 0.1 else ""
            print(f"  {var:<20} {coef:<12.4f} {p:<10.4f} {sig}")
    except Exception as e:
        print(f"  OLS failed: {e}")

    # Logistic Regression
    print("\n--- LOGISTIC REGRESSION ---")
    try:
        log_model = sm.Logit(y, X_const).fit(disp=0)
        print(f"\n  Pseudo R²: {log_model.prsquared:.4f}")
        print(f"  AIC:       {log_model.aic:.2f}")
    except Exception as e:
        print(f"  Logistic regression failed: {e}")

    # Interaction effect test
    print("\n--- INTERACTION EFFECTS ---")
    try:
        X_interact = X.copy()
        X_interact['spike_x_velocity'] = X['spike_magnitude'] * X['velocity_bps']
        X_interact_const = sm.add_constant(X_interact)

        model_interact = sm.OLS(y, X_interact_const).fit()
        print(f"\n  R² (with interactions):     {model_interact.rsquared:.4f}")
        print(f"  Adj R² (with interactions): {model_interact.rsquared_adj:.4f}")

        p_interact = model_interact.pvalues.get('spike_x_velocity', 1.0)
        sig = "***" if p_interact < 0.01 else "**" if p_interact < 0.05 else "*" if p_interact < 0.1 else "ns"
        print(f"  spike_x_velocity: p={p_interact:.4f} ({sig})")

        if p_interact < 0.05:
            print("\n  >>> INTERACTION IS SIGNIFICANT - consider multiplicative formula <<<")
    except Exception as e:
        print(f"  Interaction test failed: {e}")

    # Feature importance
    print("\n--- FEATURE IMPORTANCE (standardized coefficients) ---")
    try:
        from sklearn.preprocessing import StandardScaler
        from sklearn.linear_model import LogisticRegression

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        lr = LogisticRegression(random_state=42, max_iter=1000)
        lr.fit(X_scaled, y)

        print(f"\n  {'Feature':<20} {'Importance':<15}")
        print("  " + "-" * 35)
        for feat, coef in sorted(zip(available_features, lr.coef_[0]),
                                  key=lambda x: abs(x[1]), reverse=True):
            print(f"  {feat:<20} {abs(coef):<15.4f}")
    except Exception as e:
        print(f"  Feature importance failed: {e}")

    print("\n  Full analysis: research/SIGNAL_ACCURACY_FINDINGS.md")


if __name__ == "__main__":
    main()
