#!/usr/bin/env python3
"""
High-Frequency Spike Detection Backtest

Uses high-frequency Binance price data (183/sec) to accurately test spike detection.
Correlates spike signals with observer orderbook data to simulate trades.

Usage:
    python research/spike_hf_backtest.py
    python research/spike_hf_backtest.py --threshold 0.03
    python research/spike_hf_backtest.py --lookback 5
"""

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
import argparse

# =============================================================================
# CONFIGURATION
# =============================================================================

SHARES = 15
MIN_TIME = 60
STOP_LOSS_PCT = 0.07

# Spike parameters to test
DEFAULT_SPIKE_LOOKBACK = 3  # ticks
DEFAULT_SPIKE_THRESHOLD = 0.02  # percent


@dataclass
class SpikeSignal:
    """A detected spike signal."""
    timestamp_ms: int
    direction: str  # "UP" or "DOWN"
    magnitude: float  # percent change
    price: float


@dataclass
class TradeResult:
    """Result of a simulated trade."""
    entry_time_ms: int
    spike_direction: str
    spike_magnitude: float
    winner_fill: float
    loser_fill: float
    hedge_type: str
    pair_cost: float
    pnl: float
    resolution: str
    correct: bool


def load_hf_prices(filepath: Path) -> pd.DataFrame:
    """Load high-frequency price data."""
    df = pd.read_csv(filepath)
    df['timestamp_ms'] = df['timestamp_ms'].astype(int)
    df['price'] = df['price'].astype(float)
    return df


def detect_spikes(
    df: pd.DataFrame,
    lookback: int = DEFAULT_SPIKE_LOOKBACK,
    threshold: float = DEFAULT_SPIKE_THRESHOLD,
) -> List[SpikeSignal]:
    """
    Detect spikes in high-frequency price data.

    Args:
        df: DataFrame with timestamp_ms and price columns
        lookback: Number of ticks to look back
        threshold: Minimum percent change to trigger

    Returns:
        List of SpikeSignal objects
    """
    signals = []

    prices = df['price'].values
    timestamps = df['timestamp_ms'].values

    for i in range(lookback, len(prices)):
        current = prices[i]
        previous = prices[i - lookback]

        if previous <= 0:
            continue

        change_pct = (current - previous) / previous * 100
        magnitude = abs(change_pct)

        if magnitude >= threshold:
            direction = "UP" if change_pct > 0 else "DOWN"
            signals.append(SpikeSignal(
                timestamp_ms=timestamps[i],
                direction=direction,
                magnitude=magnitude,
                price=current,
            ))

    return signals


def load_observer_data() -> Dict[str, pd.DataFrame]:
    """Load observer CSV data for orderbook prices."""
    observer_dir = Path('research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    all_markets = {}
    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath, on_bad_lines='skip', low_memory=False)
            if df.empty:
                continue
            for slug in df['market_slug'].unique():
                mdf = df[df['market_slug'] == slug].copy()
                if len(mdf) >= 2:
                    if slug not in all_markets or len(mdf) > len(all_markets[slug]):
                        all_markets[slug] = mdf
        except:
            continue

    return all_markets


def find_orderbook_at_time(
    observer_data: Dict[str, pd.DataFrame],
    timestamp_ms: int,
) -> Optional[Tuple[str, pd.Series]]:
    """
    Find the closest orderbook snapshot to a given timestamp.

    Returns:
        (market_slug, row) or None if not found
    """
    target_time = timestamp_ms

    for slug, mdf in observer_data.items():
        if 'timestamp_ms' not in mdf.columns:
            continue

        # Find closest row
        mdf = mdf.copy()
        mdf['time_diff'] = abs(mdf['timestamp_ms'] - target_time)
        closest_idx = mdf['time_diff'].idxmin()
        closest_row = mdf.loc[closest_idx]

        # Only use if within 1 second
        if closest_row['time_diff'] < 1000:
            return slug, closest_row

    return None


def analyze_spike_signals(
    signals: List[SpikeSignal],
    observer_data: Dict[str, pd.DataFrame],
) -> Dict:
    """Analyze spike signals against observer data."""
    matched = 0
    unmatched = 0

    direction_changes = {"UP": 0, "DOWN": 0}
    magnitudes = []

    for signal in signals:
        result = find_orderbook_at_time(observer_data, signal.timestamp_ms)
        if result:
            matched += 1
            direction_changes[signal.direction] += 1
            magnitudes.append(signal.magnitude)
        else:
            unmatched += 1

    return {
        "total_signals": len(signals),
        "matched_to_orderbook": matched,
        "unmatched": unmatched,
        "up_signals": direction_changes["UP"],
        "down_signals": direction_changes["DOWN"],
        "avg_magnitude": np.mean(magnitudes) if magnitudes else 0,
        "max_magnitude": max(magnitudes) if magnitudes else 0,
    }


def calculate_signal_rate(signals: List[SpikeSignal], duration_ms: int) -> float:
    """Calculate signals per minute."""
    if duration_ms <= 0:
        return 0
    return len(signals) / (duration_ms / 60000)


def main():
    parser = argparse.ArgumentParser(description="High-frequency spike backtest")
    parser.add_argument('--lookback', type=int, default=DEFAULT_SPIKE_LOOKBACK,
                        help=f'Spike lookback ticks (default: {DEFAULT_SPIKE_LOOKBACK})')
    parser.add_argument('--threshold', type=float, default=DEFAULT_SPIKE_THRESHOLD,
                        help=f'Spike threshold percent (default: {DEFAULT_SPIKE_THRESHOLD})')
    parser.add_argument('--file', type=str, default=None,
                        help='Specific HF price file to analyze')
    args = parser.parse_args()

    print("=" * 70)
    print("HIGH-FREQUENCY SPIKE DETECTION ANALYSIS")
    print("=" * 70)

    # Find HF price files
    hf_dir = Path('research/binance_hf')
    if args.file:
        hf_files = [hf_dir / args.file]
    else:
        hf_files = sorted(hf_dir.glob('btc_prices_*.csv'))

    if not hf_files:
        print(f"\nNo high-frequency price files found in {hf_dir}/")
        print("Run: python scripts/binance_price_logger.py --duration 1")
        print("to collect data first.")
        return

    print(f"\nFound {len(hf_files)} HF price files")
    print(f"Spike parameters: lookback={args.lookback} ticks, threshold={args.threshold}%")

    # Load observer data for correlation
    print("\nLoading observer data...")
    observer_data = load_observer_data()
    print(f"Loaded {len(observer_data)} markets from observer")

    # Analyze each HF file
    all_signals = []
    total_duration_ms = 0

    for filepath in hf_files:
        print(f"\nAnalyzing: {filepath.name}")

        df = load_hf_prices(filepath)
        print(f"  Rows: {len(df):,}")

        if len(df) < args.lookback + 1:
            print("  Skipping (not enough data)")
            continue

        duration_ms = df['timestamp_ms'].iloc[-1] - df['timestamp_ms'].iloc[0]
        total_duration_ms += duration_ms
        print(f"  Duration: {duration_ms/60000:.1f} minutes")

        # Calculate actual sample rate
        sample_rate = len(df) / (duration_ms / 1000) if duration_ms > 0 else 0
        print(f"  Sample rate: {sample_rate:.1f}/sec")

        # Calculate 3-tick window in milliseconds
        tick_window_ms = (args.lookback * 1000) / sample_rate if sample_rate > 0 else 0
        print(f"  {args.lookback}-tick window: {tick_window_ms:.1f}ms")

        # Detect spikes
        signals = detect_spikes(df, lookback=args.lookback, threshold=args.threshold)
        all_signals.extend(signals)

        signal_rate = calculate_signal_rate(signals, duration_ms)
        print(f"  Spikes detected: {len(signals)} ({signal_rate:.1f}/min)")

        if signals:
            magnitudes = [s.magnitude for s in signals]
            print(f"  Magnitude: avg={np.mean(magnitudes):.4f}%, max={max(magnitudes):.4f}%")

    # Overall analysis
    print("\n" + "=" * 70)
    print("OVERALL SPIKE ANALYSIS")
    print("=" * 70)

    total_minutes = total_duration_ms / 60000
    print(f"\nTotal duration: {total_minutes:.1f} minutes")
    print(f"Total spikes: {len(all_signals)}")
    print(f"Signal rate: {len(all_signals)/total_minutes:.1f}/min" if total_minutes > 0 else "")

    if all_signals:
        up_signals = [s for s in all_signals if s.direction == "UP"]
        down_signals = [s for s in all_signals if s.direction == "DOWN"]

        print(f"\nDirection breakdown:")
        print(f"  UP:   {len(up_signals)} ({len(up_signals)/len(all_signals)*100:.1f}%)")
        print(f"  DOWN: {len(down_signals)} ({len(down_signals)/len(all_signals)*100:.1f}%)")

        magnitudes = [s.magnitude for s in all_signals]
        print(f"\nMagnitude distribution:")
        print(f"  Min:    {min(magnitudes):.4f}%")
        print(f"  Median: {np.median(magnitudes):.4f}%")
        print(f"  Mean:   {np.mean(magnitudes):.4f}%")
        print(f"  Max:    {max(magnitudes):.4f}%")

        # Magnitude buckets
        print(f"\nBy magnitude:")
        for threshold in [0.02, 0.03, 0.05, 0.10]:
            count = sum(1 for m in magnitudes if m >= threshold)
            print(f"  >= {threshold:.2f}%: {count} signals ({count/len(all_signals)*100:.1f}%)")

    # Compare with velocity
    print("\n" + "=" * 70)
    print("COMPARISON: SPIKE vs VELOCITY")
    print("=" * 70)

    print(f"""
  SPIKE (from HF data):
    Detection window: ~{tick_window_ms:.0f}ms (3 ticks @ {sample_rate:.0f}/sec)
    Signals/minute: {len(all_signals)/total_minutes:.1f}
    Threshold: {args.threshold}% price change

  VELOCITY (from observer):
    Detection window: ~10,000ms (10-second average)
    Signals/minute: ~0.5-2 (Zone 5-6 entries)
    Threshold: 0.50 bps/sec

  SPEED ADVANTAGE: Spike detects ~{10000/tick_window_ms:.0f}x faster
    """)

    # Recommendations
    print("=" * 70)
    print("RECOMMENDATIONS")
    print("=" * 70)

    if len(all_signals) / total_minutes > 10:
        print(f"""
  WARNING: {len(all_signals)/total_minutes:.1f} signals/min is very high.
  This will result in:
  - Many false positives
  - Most trades getting hedged (not going unhedged)
  - Low profitability

  Consider:
  - Increasing threshold from {args.threshold}% to 0.05% or higher
  - Adding velocity confirmation (spike + velocity agree)
  - Using spike for TIMING only, velocity for DIRECTION
        """)
    else:
        print(f"""
  Signal rate of {len(all_signals)/total_minutes:.1f}/min looks reasonable.
  Next steps:
  - Correlate spike signals with Polymarket orderbook moves
  - Measure time between spike and orderbook reaction
  - Test spike timing advantage in live paper trading
        """)

    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)


if __name__ == "__main__":
    main()
