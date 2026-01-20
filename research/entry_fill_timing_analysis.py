#!/usr/bin/env python3
"""
Entry Fill Timing Analysis

Analyzes:
1. Passive Entry Fill Timing - How often does price trade through our bid within 10/20/30s?
2. Target Hit Timing - How often do we hit hedge target within 10/20/30s?

Uses 5Hz observer data (200ms intervals).
OPTIMIZED: Uses vectorized operations and sampling for speed.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from datetime import datetime

# =============================================================================
# CONFIGURATION
# =============================================================================

DATA_DIR = Path("/Users/rananjaybika/polymarket-amm-bot/research/observer")
RESOLUTIONS_FILE = DATA_DIR / "market_resolutions_verified.csv"

# Entry offsets to test (cents below current ask)
ENTRY_OFFSETS = [0.01, 0.02]

# Time windows to analyze (seconds)
TIME_WINDOWS = [10, 20, 30]
HEDGE_TIME_WINDOWS = [10, 20, 30, 40]  # Extended windows for hedge timing

# Minimum signal requirements
MIN_TIME_REMAINING = 60  # Don't analyze signals too close to resolution
MIN_RUNTIME_SECS = 300   # Market must have at least 5 min of data

# Spike detection (use pre-computed or velocity threshold)
VELOCITY_THRESHOLD = 0.10  # abs(velocity_bps) >= 0.10 for signal

# Loser bid calculation (same as main backtest)
DROP_MULTIPLIER = 0.68
DROP_INTERCEPT = 0.01
TARGET_PAIR_COST = 0.99

# Sampling - only analyze every Nth signal to speed up
SIGNAL_SAMPLE_RATE = 5  # Analyze 1 in 5 signals


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class EntryFillResult:
    """Result of analyzing a single entry fill attempt."""
    market_slug: str
    signal_time_remaining: float
    signal_timestamp: int
    winner_side: str
    entry_offset: float
    entry_bid: float
    current_ask: float
    filled: bool
    fill_time_secs: Optional[float]  # Time from signal to fill
    fill_timestamp: Optional[int]
    resolution: Optional[str] = None  # Actual market resolution (UP/DOWN)


@dataclass
class TargetHitResult:
    """Result of analyzing hedge target hit timing."""
    market_slug: str
    signal_time_remaining: float
    signal_timestamp: int
    winner_side: str
    winner_entry: float
    loser_target: float
    hit: bool
    hit_time_secs: Optional[float]
    hit_timestamp: Optional[int]
    spike_magnitude: float
    resolution: Optional[str] = None  # Actual market resolution (UP/DOWN)


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def calc_loser_target(winner_entry: float, spike_mag: float) -> float:
    """Calculate loser bid target (same logic as main backtest)."""
    expected_drop = DROP_MULTIPLIER * spike_mag / 100 + DROP_INTERCEPT
    max_loser = TARGET_PAIR_COST - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid))


def get_fallback_resolution(market_df: pd.DataFrame) -> Optional[str]:
    """
    Use price closest to $1 at ~899s mark for markets without Polymarket resolution.
    Falls back to last available data if no resolution found.
    """
    # Look for data near the end (time_remaining <= 2 seconds)
    last_rows = market_df[market_df['time_remaining_secs'] <= 2.0]

    if len(last_rows) == 0:
        # Fall back to last row
        last_rows = market_df.tail(1)

    if len(last_rows) == 0:
        return None

    last_row = last_rows.iloc[-1]

    # Get mid prices
    up_price = (last_row['up_bid'] + last_row['up_ask']) / 2
    down_price = (last_row['down_bid'] + last_row['down_ask']) / 2

    # Closer to $1 = likely winner
    return "UP" if up_price > down_price else "DOWN"


# =============================================================================
# DATA LOADING
# =============================================================================

def load_observer_data() -> Tuple[pd.DataFrame, Dict[str, str]]:
    """Load observer data and resolutions."""
    print("Loading observer data...")

    # Load all grid observation files
    obs_files = sorted(DATA_DIR.glob("grid_obs_*.csv"))
    dfs = []

    for f in obs_files:
        try:
            df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
            df['source_file'] = f.name
            dfs.append(df)
            print(f"  Loaded {f.name}: {len(df):,} rows")
        except Exception as e:
            print(f"  Error loading {f.name}: {e}")

    if not dfs:
        raise ValueError("No observer data found")

    obs_df = pd.concat(dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    obs_df = obs_df.sort_values(['market_slug', 'timestamp_ms']).reset_index(drop=True)

    print(f"  Total rows: {len(obs_df):,}")

    # Load resolutions
    res_map = {}
    if RESOLUTIONS_FILE.exists():
        res_df = pd.read_csv(RESOLUTIONS_FILE)
        res_map = dict(zip(res_df['slug'], res_df['winner']))
        print(f"  Resolutions: {len(res_map)} markets")

    return obs_df, res_map


# =============================================================================
# OPTIMIZED ENTRY FILL TIMING ANALYSIS
# =============================================================================

def analyze_entry_fill_timing(obs_df: pd.DataFrame, res_map: Dict[str, str]) -> Dict[float, List[EntryFillResult]]:
    """
    Analyze passive entry fill timing for different offsets.
    OPTIMIZED: Pre-filters signals and uses numpy arrays for speed.
    """
    print("\nAnalyzing entry fill timing...")

    results = {offset: [] for offset in ENTRY_OFFSETS}

    # Pre-filter: identify signal rows (spike OR velocity)
    obs_df = obs_df.copy()
    obs_df['velocity_bps'] = obs_df['velocity_bps'].fillna(0)
    obs_df['spike_detected'] = obs_df['spike_detected'].fillna(False)

    # Get valid markets
    market_stats = obs_df.groupby('market_slug')['time_remaining_secs'].agg(['min', 'max'])
    market_stats['runtime'] = market_stats['max'] - market_stats['min']
    valid_markets = market_stats[market_stats['runtime'] >= MIN_RUNTIME_SECS].index.tolist()

    print(f"  Valid markets: {len(valid_markets)}")

    obs_df = obs_df[obs_df['market_slug'].isin(valid_markets)]

    # Identify signals
    is_spike = (obs_df['spike_detected'] == True) & (obs_df['spike_direction'].isin(['UP', 'DOWN']))
    is_velocity = obs_df['velocity_bps'].abs() >= VELOCITY_THRESHOLD
    is_signal = is_spike | is_velocity
    is_valid_time = obs_df['time_remaining_secs'] >= MIN_TIME_REMAINING

    signal_mask = is_signal & is_valid_time
    signal_df = obs_df[signal_mask].copy()

    # Sample signals for speed
    signal_df = signal_df.iloc[::SIGNAL_SAMPLE_RATE]
    print(f"  Signals to analyze: {len(signal_df)} (1/{SIGNAL_SAMPLE_RATE} sampled)")

    # Process each market
    processed = 0
    for slug in valid_markets:
        mdf = obs_df[obs_df['market_slug'] == slug].copy()
        mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

        market_signals = signal_df[signal_df['market_slug'] == slug]

        if len(market_signals) == 0:
            continue

        # Convert to numpy for speed
        timestamps = mdf['timestamp_ms'].values
        up_asks = mdf['up_ask'].values
        down_asks = mdf['down_ask'].values

        for _, sig_row in market_signals.iterrows():
            # Determine winner side
            if sig_row['spike_detected'] and sig_row['spike_direction'] in ['UP', 'DOWN']:
                winner_side = sig_row['spike_direction']
            else:
                winner_side = 'UP' if sig_row['velocity_bps'] > 0 else 'DOWN'

            current_ask = sig_row['up_ask'] if winner_side == 'UP' else sig_row['down_ask']
            signal_ts = sig_row['timestamp_ms']
            time_rem = sig_row['time_remaining_secs']

            # Find signal index in market data
            sig_idx = np.searchsorted(timestamps, signal_ts)
            if sig_idx >= len(timestamps):
                continue

            # Test each offset
            for offset in ENTRY_OFFSETS:
                entry_bid = current_ask - offset

                # Scan forward (vectorized)
                future_mask = (timestamps > signal_ts) & (timestamps <= signal_ts + 35000)
                future_idx = np.where(future_mask)[0]

                if len(future_idx) == 0:
                    results[offset].append(EntryFillResult(
                        market_slug=slug, signal_time_remaining=time_rem,
                        signal_timestamp=signal_ts, winner_side=winner_side,
                        entry_offset=offset, entry_bid=entry_bid,
                        current_ask=current_ask, filled=False,
                        fill_time_secs=None, fill_timestamp=None,
                        resolution=res_map.get(slug)
                    ))
                    continue

                # Check fill condition
                if winner_side == 'UP':
                    future_asks = up_asks[future_idx]
                else:
                    future_asks = down_asks[future_idx]

                fill_mask = future_asks <= entry_bid
                if fill_mask.any():
                    first_fill_idx = future_idx[np.argmax(fill_mask)]
                    fill_ts = timestamps[first_fill_idx]
                    fill_time = (fill_ts - signal_ts) / 1000.0
                    filled = True
                else:
                    fill_ts = None
                    fill_time = None
                    filled = False

                results[offset].append(EntryFillResult(
                    market_slug=slug, signal_time_remaining=time_rem,
                    signal_timestamp=signal_ts, winner_side=winner_side,
                    entry_offset=offset, entry_bid=entry_bid,
                    current_ask=current_ask, filled=filled,
                    fill_time_secs=fill_time, fill_timestamp=fill_ts,
                    resolution=res_map.get(slug)
                ))

        processed += 1
        if processed % 20 == 0:
            print(f"    Processed {processed}/{len(valid_markets)} markets...")

    for offset, res_list in results.items():
        print(f"  Offset {offset:.2f}: {len(res_list)} signals analyzed")

    return results


# =============================================================================
# OPTIMIZED TARGET HIT TIMING ANALYSIS
# =============================================================================

def analyze_target_hit_timing(obs_df: pd.DataFrame, res_map: Dict[str, str]) -> List[TargetHitResult]:
    """
    Analyze hedge target hit timing.
    OPTIMIZED: Uses numpy arrays and sampling.
    """
    print("\nAnalyzing target hit timing...")

    results = []

    # Pre-filter
    obs_df = obs_df.copy()
    obs_df['velocity_bps'] = obs_df['velocity_bps'].fillna(0)
    obs_df['spike_detected'] = obs_df['spike_detected'].fillna(False)
    obs_df['spike_magnitude'] = obs_df['spike_magnitude'].fillna(0)

    # Get valid markets
    market_stats = obs_df.groupby('market_slug')['time_remaining_secs'].agg(['min', 'max'])
    market_stats['runtime'] = market_stats['max'] - market_stats['min']
    valid_markets = market_stats[market_stats['runtime'] >= MIN_RUNTIME_SECS].index.tolist()

    obs_df = obs_df[obs_df['market_slug'].isin(valid_markets)]

    # Identify signals
    is_spike = (obs_df['spike_detected'] == True) & (obs_df['spike_direction'].isin(['UP', 'DOWN']))
    is_velocity = obs_df['velocity_bps'].abs() >= VELOCITY_THRESHOLD
    is_signal = is_spike | is_velocity
    is_valid_time = obs_df['time_remaining_secs'] >= MIN_TIME_REMAINING

    signal_mask = is_signal & is_valid_time
    signal_df = obs_df[signal_mask].copy()

    # Sample
    signal_df = signal_df.iloc[::SIGNAL_SAMPLE_RATE]
    print(f"  Signals to analyze: {len(signal_df)}")

    processed = 0
    for slug in valid_markets:
        mdf = obs_df[obs_df['market_slug'] == slug].copy()
        mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

        market_signals = signal_df[signal_df['market_slug'] == slug]

        if len(market_signals) == 0:
            continue

        timestamps = mdf['timestamp_ms'].values
        up_asks = mdf['up_ask'].values
        down_asks = mdf['down_ask'].values

        for _, sig_row in market_signals.iterrows():
            # Determine winner side and magnitude
            if sig_row['spike_detected'] and sig_row['spike_direction'] in ['UP', 'DOWN']:
                winner_side = sig_row['spike_direction']
                mag = sig_row['spike_magnitude'] if sig_row['spike_magnitude'] > 0 else 0.02
            else:
                winner_side = 'UP' if sig_row['velocity_bps'] > 0 else 'DOWN'
                mag = 0.02

            loser_side = 'DOWN' if winner_side == 'UP' else 'UP'
            winner_entry = sig_row['up_ask'] if winner_side == 'UP' else sig_row['down_ask']
            loser_target = calc_loser_target(winner_entry, mag)

            signal_ts = sig_row['timestamp_ms']
            time_rem = sig_row['time_remaining_secs']

            # Scan forward
            future_mask = (timestamps > signal_ts) & (timestamps <= signal_ts + 35000)
            future_idx = np.where(future_mask)[0]

            if len(future_idx) == 0:
                results.append(TargetHitResult(
                    market_slug=slug, signal_time_remaining=time_rem,
                    signal_timestamp=signal_ts, winner_side=winner_side,
                    winner_entry=winner_entry, loser_target=loser_target,
                    hit=False, hit_time_secs=None, hit_timestamp=None,
                    spike_magnitude=mag, resolution=res_map.get(slug)
                ))
                continue

            # Check loser ask
            if loser_side == 'UP':
                future_asks = up_asks[future_idx]
            else:
                future_asks = down_asks[future_idx]

            hit_mask = future_asks <= loser_target
            if hit_mask.any():
                first_hit_idx = future_idx[np.argmax(hit_mask)]
                hit_ts = timestamps[first_hit_idx]
                hit_time = (hit_ts - signal_ts) / 1000.0
                hit = True
            else:
                hit_ts = None
                hit_time = None
                hit = False

            results.append(TargetHitResult(
                market_slug=slug, signal_time_remaining=time_rem,
                signal_timestamp=signal_ts, winner_side=winner_side,
                winner_entry=winner_entry, loser_target=loser_target,
                hit=hit, hit_time_secs=hit_time, hit_timestamp=hit_ts,
                spike_magnitude=mag, resolution=res_map.get(slug)
            ))

        processed += 1
        if processed % 20 == 0:
            print(f"    Processed {processed}/{len(valid_markets)} markets...")

    print(f"  Total signals analyzed: {len(results)}")

    return results


# =============================================================================
# REPORTING
# =============================================================================

def compute_fill_rates(results: List, time_windows: List[int]) -> Dict[int, Dict]:
    """Compute fill rates for different time windows."""
    stats = {}

    for window in time_windows:
        filled_in_window = sum(
            1 for r in results
            if r.filled and r.fill_time_secs is not None and r.fill_time_secs <= window
        )

        fill_times = [
            r.fill_time_secs for r in results
            if r.filled and r.fill_time_secs is not None and r.fill_time_secs <= window
        ]

        total = len(results)
        fill_rate = filled_in_window / total if total > 0 else 0
        avg_time = np.mean(fill_times) if fill_times else 0
        median_time = np.median(fill_times) if fill_times else 0

        stats[window] = {
            'total': total,
            'filled': filled_in_window,
            'fill_rate': fill_rate,
            'avg_time': avg_time,
            'median_time': median_time
        }

    return stats


def compute_win_rates_by_entry_timing(results: List[EntryFillResult], time_windows: List[int]) -> Dict[int, Dict]:
    """Compute win rate for trades filled within each time window (direction accuracy)."""
    stats = {}

    for window in time_windows:
        # Get results that filled within this window
        filled_in_window = [
            r for r in results
            if r.filled and r.fill_time_secs is not None and r.fill_time_secs <= window
            and r.resolution is not None
        ]

        if not filled_in_window:
            stats[window] = {'win_rate': 0, 'count': 0, 'wins': 0}
            continue

        # Win = signal direction matches resolution
        wins = sum(1 for r in filled_in_window if r.winner_side == r.resolution)
        stats[window] = {
            'win_rate': wins / len(filled_in_window) if filled_in_window else 0,
            'count': len(filled_in_window),
            'wins': wins
        }

    return stats


def compute_win_rates_by_hedge_timing(results: List[TargetHitResult], time_windows: List[int]) -> Dict[int, Dict]:
    """Compute win rate for trades where hedge fills within each time window."""
    stats = {}

    for window in time_windows:
        # Get results that hit hedge target within this window
        hedged_in_window = [
            r for r in results
            if r.hit and r.hit_time_secs is not None and r.hit_time_secs <= window
            and r.resolution is not None
        ]

        if not hedged_in_window:
            stats[window] = {'win_rate': 0, 'count': 0, 'wins': 0}
            continue

        # Hedged trades are always winners (guaranteed ~$0.01/pair profit)
        # But we can also check direction accuracy for comparison
        wins = len(hedged_in_window)  # All hedged trades are wins
        direction_correct = sum(1 for r in hedged_in_window if r.winner_side == r.resolution)

        stats[window] = {
            'win_rate': 1.0,  # Hedged trades always win
            'count': len(hedged_in_window),
            'wins': wins,
            'direction_correct': direction_correct,
            'direction_accuracy': direction_correct / len(hedged_in_window) if hedged_in_window else 0
        }

    return stats


def print_entry_fill_report(entry_results: Dict[float, List[EntryFillResult]]):
    """Print entry fill timing report."""
    print("\n" + "=" * 70)
    print("ENTRY FILL TIMING (signal -> entry fill)")
    print("=" * 70)
    print()

    # Header
    print("ENTRY FILL RATES BY OFFSET")
    print("+" + "-" * 68 + "+")
    print(f"| {'Offset':^8} | {'10s Fill%':^12} | {'20s Fill%':^12} | {'30s Fill%':^12} | {'Avg Time':^10} |")
    print("+" + "-" * 68 + "+")

    for offset in ENTRY_OFFSETS:
        results = entry_results[offset]
        stats = compute_fill_rates(results, TIME_WINDOWS)

        s10 = stats[10]
        s20 = stats[20]
        s30 = stats[30]

        # Use 30s avg time for overall average
        avg_time = s30['avg_time']

        print(f"| {offset:.2f}    | {s10['fill_rate']*100:>8.1f}%   | {s20['fill_rate']*100:>8.1f}%   | {s30['fill_rate']*100:>8.1f}%   | {avg_time:>6.1f}s   |")

    print("+" + "-" * 68 + "+")

    # Detailed breakdown
    print("\nDETAILED BREAKDOWN:")
    for offset in ENTRY_OFFSETS:
        results = entry_results[offset]
        stats = compute_fill_rates(results, TIME_WINDOWS)

        print(f"\n  Offset: {offset:.2f} ({int(offset*100)} cents below ask)")
        print(f"  Total signals: {len(results)}")

        for window in TIME_WINDOWS:
            s = stats[window]
            print(f"    {window:2d}s window: {s['filled']:>4}/{s['total']} filled ({s['fill_rate']*100:>5.1f}%), "
                  f"avg={s['avg_time']:.1f}s, median={s['median_time']:.1f}s")


def print_target_hit_report(target_results: List[TargetHitResult]):
    """Print target hit timing report."""
    print("\n" + "=" * 70)
    print("TARGET HIT TIMING (entry -> hedge fill)")
    print("=" * 70)
    print()

    # Convert to generic format for fill rate computation
    @dataclass
    class GenericResult:
        filled: bool
        fill_time_secs: Optional[float]

    generic_results = [
        GenericResult(filled=r.hit, fill_time_secs=r.hit_time_secs)
        for r in target_results
    ]

    stats = compute_fill_rates(generic_results, HEDGE_TIME_WINDOWS)

    print("TARGET HIT RATES")
    print("+" + "-" * 58 + "+")
    print(f"| {'Window':^8} | {'Hit Rate':^12} | {'Avg Time':^12} | {'Median Time':^12} |")
    print("+" + "-" * 58 + "+")

    for window in HEDGE_TIME_WINDOWS:
        s = stats[window]
        print(f"| {window:>2}s     | {s['fill_rate']*100:>8.1f}%   | {s['avg_time']:>8.1f}s   | {s['median_time']:>8.1f}s   |")

    print("+" + "-" * 58 + "+")

    # Detailed
    print(f"\n  Total signals: {len(target_results)}")
    for window in HEDGE_TIME_WINDOWS:
        s = stats[window]
        print(f"  {window:2d}s window: {s['filled']:>4}/{s['total']} hit ({s['fill_rate']*100:>5.1f}%)")


def print_combined_summary(entry_results: Dict[float, List[EntryFillResult]],
                           target_results: List[TargetHitResult]):
    """Print combined summary comparing entry and hedge timing."""
    print("\n" + "=" * 70)
    print("COMBINED SUMMARY")
    print("=" * 70)

    # Entry stats for 1-cent offset (typical)
    entry_1c = entry_results[0.01]
    entry_stats = compute_fill_rates(entry_1c, TIME_WINDOWS)

    # Target stats
    @dataclass
    class GenericResult:
        filled: bool
        fill_time_secs: Optional[float]

    generic_target = [
        GenericResult(filled=r.hit, fill_time_secs=r.hit_time_secs)
        for r in target_results
    ]
    target_stats = compute_fill_rates(generic_target, TIME_WINDOWS)

    print("\n  FILL RATES AT 30 SECONDS:")
    print(f"    Entry (1 cent offset): {entry_stats[30]['fill_rate']*100:.1f}%")
    print(f"    Entry (2 cent offset): {compute_fill_rates(entry_results[0.02], TIME_WINDOWS)[30]['fill_rate']*100:.1f}%")
    print(f"    Hedge target:          {target_stats[30]['fill_rate']*100:.1f}%")

    # Calculate expected unfilled rates
    entry_unfilled_30s = 1 - entry_stats[30]['fill_rate']
    target_unfilled_30s = 1 - target_stats[30]['fill_rate']

    print(f"\n  UNFILLED AFTER 30 SECONDS:")
    print(f"    Entry unfilled: {entry_unfilled_30s*100:.1f}% of signals")
    print(f"    Hedge unfilled: {target_unfilled_30s*100:.1f}% of trades")

    print("\n  IMPLICATIONS:")
    print(f"    - {entry_unfilled_30s*100:.1f}% of signals won't fill passively in 30s")
    print(f"    - {target_unfilled_30s*100:.1f}% of trades may go to resolution unhedged")


def print_win_rate_by_entry_timing(entry_results: Dict[float, List[EntryFillResult]]):
    """Print win rate by entry fill timing report."""
    print("\n" + "=" * 70)
    print("WIN RATE BY ENTRY FILL TIMING (direction accuracy)")
    print("=" * 70)
    print()

    # Use 1-cent offset (typical)
    results = entry_results[0.01]
    stats = compute_win_rates_by_entry_timing(results, TIME_WINDOWS)

    print("+" + "-" * 48 + "+")
    print(f"| {'Window':^8} | {'Filled':^10} | {'Wins':^8} | {'Win Rate':^12} |")
    print("+" + "-" * 48 + "+")

    for window in TIME_WINDOWS:
        s = stats[window]
        print(f"| {window:>3}s    | {s['count']:>10} | {s['wins']:>8} | {s['win_rate']*100:>9.1f}%  |")

    print("+" + "-" * 48 + "+")

    print("\n  Note: Win rate = % of filled trades where signal direction matched resolution")


def print_win_rate_by_hedge_timing(target_results: List[TargetHitResult]):
    """Print win rate by hedge fill timing report."""
    print("\n" + "=" * 70)
    print("WIN RATE BY HEDGE FILL TIMING")
    print("=" * 70)
    print()

    stats = compute_win_rates_by_hedge_timing(target_results, HEDGE_TIME_WINDOWS)

    print("+" + "-" * 48 + "+")
    print(f"| {'Window':^8} | {'Hedged':^10} | {'Wins':^8} | {'Win Rate':^12} |")
    print("+" + "-" * 48 + "+")

    for window in HEDGE_TIME_WINDOWS:
        s = stats[window]
        print(f"| {window:>3}s    | {s['count']:>10} | {s['wins']:>8} | {s['win_rate']*100:>9.1f}%  |")

    print("+" + "-" * 48 + "+")

    print("\n  Direction accuracy of hedged trades:")
    print("  +" + "-" * 48 + "+")
    print(f"  | {'Window':^8} | {'Hedged':^10} | {'Correct':^8} | {'Accuracy':^12} |")
    print("  +" + "-" * 48 + "+")

    for window in HEDGE_TIME_WINDOWS:
        s = stats[window]
        if s['count'] > 0:
            print(f"  | {window:>3}s    | {s['count']:>10} | {s.get('direction_correct', 0):>8} | {s.get('direction_accuracy', 0)*100:>9.1f}%  |")
        else:
            print(f"  | {window:>3}s    | {s['count']:>10} | {0:>8} | {0:>9.1f}%  |")

    print("  +" + "-" * 48 + "+")

    print("\n  Note: Hedged trades are ALWAYS winners (guaranteed ~$0.01/pair profit)")
    print("        Direction accuracy shows how often the signal was correct regardless")


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 70)
    print("ENTRY FILL TIMING ANALYSIS")
    print("=" * 70)
    print(f"Entry offsets: {ENTRY_OFFSETS}")
    print(f"Time windows: {TIME_WINDOWS}")
    print(f"Hedge time windows: {HEDGE_TIME_WINDOWS}")
    print()

    # Load data
    obs_df, res_map = load_observer_data()

    # Run analyses
    entry_results = analyze_entry_fill_timing(obs_df, res_map)
    target_results = analyze_target_hit_timing(obs_df, res_map)

    # Print reports
    print_entry_fill_report(entry_results)
    print_target_hit_report(target_results)
    print_combined_summary(entry_results, target_results)

    # Win rate reports
    print_win_rate_by_entry_timing(entry_results)
    print_win_rate_by_hedge_timing(target_results)

    print("\n" + "=" * 70)
    print("ANALYSIS COMPLETE")
    print("=" * 70)

    return entry_results, target_results


if __name__ == "__main__":
    main()
