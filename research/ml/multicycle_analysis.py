#!/usr/bin/env python3
"""
Multi-Cycle Parallel Positions Analysis

Goal: Quantify opportunity cost of single-cycle trading and evaluate multi-cycle approach.

Current approach:
- Enter 50 shares on spike
- Wait up to 180s for hedge
- Miss good spikes while holding

Proposed approach:
- Divide into 4 × 10 shares (or 4 × 12.5 shares)
- Each entry tracked separately
- Can enter on new spike while other positions still hedging

Analysis:
1. How many good spikes occur during average hold time?
2. What's the expected improvement with 4 parallel cycles?
3. Position sizing and risk considerations

Usage:
    python research/ml/multicycle_analysis.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from tqdm import tqdm
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from collections import deque

# =============================================================================
# CONFIGURATION
# =============================================================================

TIME_STOP_SECONDS = 180  # Current time-stop
MIN_LOSER_DROP = 0.12    # Good spike threshold
SHARES_SINGLE = 50       # Current single-cycle size
SHARES_PER_CYCLE = 10    # Proposed per-cycle size (4 × 10 = 40 total)
MAX_CYCLES = 4           # Maximum concurrent cycles


# =============================================================================
# DATA LOADING
# =============================================================================

def load_oos7_data():
    """Load OOS7 observer data."""
    base_dir = Path("/Users/rananjaybika/polymarket-amm-bot")

    obs_dir = base_dir / "research/observer"
    obs_files = [
        obs_dir / "grid_obs_20260129.csv",
        obs_dir / "grid_obs_20260130.csv",
    ]

    print("Loading Observer data:")
    obs_dfs = []
    for f in obs_files:
        if f.exists():
            df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)
            print(f"  {f.name}: {len(df):,} rows")

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    print(f"  Combined: {len(obs_df):,} rows")

    # Load resolutions
    res_path = obs_dir / "market_resolutions_verified.csv"
    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    return obs_df, res_map


def load_spike_features():
    """Load precomputed spike features."""
    path = Path("/Users/rananjaybika/polymarket-amm-bot/research/ml/spike_quality_features.csv")
    if path.exists():
        return pd.read_csv(path)
    return None


# =============================================================================
# SPIKE ANALYSIS
# =============================================================================

@dataclass
class SpikeEvent:
    timestamp_ms: int
    market_slug: str
    is_good: bool
    hold_time_ms: int = 0  # Time until hedge or timeout


def extract_spikes(obs_df: pd.DataFrame) -> List[SpikeEvent]:
    """Extract all spike events with timing info."""
    spike_rows = obs_df[obs_df['spike_detected'] == True].copy()
    spike_rows = spike_rows.sort_values('timestamp_ms')

    spikes = []
    for _, row in tqdm(spike_rows.iterrows(), total=len(spike_rows), desc="Extracting spikes"):
        spike_ts = int(row['timestamp_ms'])
        market = row['market_slug']
        spike_dir = row.get('spike_direction', 'UP')

        # Determine loser side
        if spike_dir == "UP":
            loser_col = 'down_ask'
        else:
            loser_col = 'up_ask'

        # Get initial loser ask
        initial_loser = row.get(loser_col, 0.5)
        if pd.isna(initial_loser) or initial_loser <= 0:
            continue

        # Find future data within time window
        end_ts = spike_ts + TIME_STOP_SECONDS * 1000
        future = obs_df[
            (obs_df['market_slug'] == market) &
            (obs_df['timestamp_ms'] > spike_ts) &
            (obs_df['timestamp_ms'] <= end_ts)
        ]

        if len(future) == 0:
            # No future data - assume timeout
            spikes.append(SpikeEvent(
                timestamp_ms=spike_ts,
                market_slug=market,
                is_good=False,
                hold_time_ms=TIME_STOP_SECONDS * 1000
            ))
            continue

        # Find when loser drops enough for hedge (or timeout)
        min_loser = future[loser_col].min()
        drop = initial_loser - min_loser if pd.notna(min_loser) else 0

        is_good = drop >= MIN_LOSER_DROP

        if is_good:
            # Find time to hedge fill (when drop >= threshold)
            target_price = initial_loser - MIN_LOSER_DROP
            hedge_rows = future[future[loser_col] <= target_price]
            if len(hedge_rows) > 0:
                hedge_ts = hedge_rows['timestamp_ms'].iloc[0]
                hold_time_ms = int(hedge_ts - spike_ts)
            else:
                hold_time_ms = TIME_STOP_SECONDS * 1000
        else:
            hold_time_ms = TIME_STOP_SECONDS * 1000  # Timeout

        spikes.append(SpikeEvent(
            timestamp_ms=spike_ts,
            market_slug=market,
            is_good=is_good,
            hold_time_ms=hold_time_ms
        ))

    return spikes


# =============================================================================
# SIMULATION
# =============================================================================

@dataclass
class CycleState:
    """Tracks a single trading cycle."""
    entry_ts: int
    market_slug: str
    is_good: bool
    exit_ts: int


def simulate_single_cycle(spikes: List[SpikeEvent]) -> Dict:
    """Simulate current single-cycle trading."""
    trades = []
    missed_good = 0
    missed_bad = 0

    in_position = False
    current_exit_ts = 0

    for spike in sorted(spikes, key=lambda x: x.timestamp_ms):
        if in_position:
            # Still holding - check if we can exit
            if spike.timestamp_ms >= current_exit_ts:
                in_position = False
            else:
                # Missed this spike
                if spike.is_good:
                    missed_good += 1
                else:
                    missed_bad += 1
                continue

        # Can enter this spike
        if spike.is_good:
            trades.append({
                'timestamp_ms': spike.timestamp_ms,
                'market_slug': spike.market_slug,
                'is_good': True,
                'hold_time_ms': spike.hold_time_ms
            })
        else:
            trades.append({
                'timestamp_ms': spike.timestamp_ms,
                'market_slug': spike.market_slug,
                'is_good': False,
                'hold_time_ms': spike.hold_time_ms
            })

        in_position = True
        current_exit_ts = spike.timestamp_ms + spike.hold_time_ms

    return {
        'trades': trades,
        'total_trades': len(trades),
        'good_trades': sum(1 for t in trades if t['is_good']),
        'missed_good': missed_good,
        'missed_bad': missed_bad
    }


def simulate_multi_cycle(spikes: List[SpikeEvent], max_cycles: int = 4) -> Dict:
    """Simulate multi-cycle parallel trading."""
    trades = []
    active_cycles: List[CycleState] = []

    for spike in sorted(spikes, key=lambda x: x.timestamp_ms):
        # Clean up finished cycles
        active_cycles = [c for c in active_cycles if c.exit_ts > spike.timestamp_ms]

        # Check if we can enter new cycle
        if len(active_cycles) < max_cycles:
            cycle = CycleState(
                entry_ts=spike.timestamp_ms,
                market_slug=spike.market_slug,
                is_good=spike.is_good,
                exit_ts=spike.timestamp_ms + spike.hold_time_ms
            )
            active_cycles.append(cycle)

            trades.append({
                'timestamp_ms': spike.timestamp_ms,
                'market_slug': spike.market_slug,
                'is_good': spike.is_good,
                'hold_time_ms': spike.hold_time_ms,
                'concurrent_cycles': len(active_cycles)
            })

    return {
        'trades': trades,
        'total_trades': len(trades),
        'good_trades': sum(1 for t in trades if t['is_good']),
        'avg_concurrent': np.mean([t['concurrent_cycles'] for t in trades]) if trades else 0
    }


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 70)
    print("MULTI-CYCLE PARALLEL POSITIONS ANALYSIS")
    print("=" * 70)

    # Load data
    obs_df, res_map = load_oos7_data()

    # Extract spikes
    spikes = extract_spikes(obs_df)
    print(f"\nTotal spikes: {len(spikes)}")

    good_spikes = [s for s in spikes if s.is_good]
    bad_spikes = [s for s in spikes if not s.is_good]
    print(f"Good spikes (loser drop >= 12c): {len(good_spikes)} ({100*len(good_spikes)/len(spikes):.1f}%)")
    print(f"Bad spikes: {len(bad_spikes)}")

    # Analyze hold times
    print("\n" + "=" * 70)
    print("HOLD TIME ANALYSIS")
    print("=" * 70)

    good_hold_times = [s.hold_time_ms / 1000 for s in good_spikes]
    bad_hold_times = [s.hold_time_ms / 1000 for s in bad_spikes]

    print(f"\nGood spikes hold time:")
    print(f"  Mean: {np.mean(good_hold_times):.1f}s")
    print(f"  Median: {np.median(good_hold_times):.1f}s")
    print(f"  25th percentile: {np.percentile(good_hold_times, 25):.1f}s")
    print(f"  75th percentile: {np.percentile(good_hold_times, 75):.1f}s")

    print(f"\nBad spikes hold time:")
    print(f"  Mean: {np.mean(bad_hold_times):.1f}s (usually timeout)")
    print(f"  Median: {np.median(bad_hold_times):.1f}s")

    # Simulate single-cycle
    print("\n" + "=" * 70)
    print("SINGLE-CYCLE SIMULATION (Current Approach)")
    print("=" * 70)

    single_result = simulate_single_cycle(spikes)
    print(f"\nTotal trades: {single_result['total_trades']}")
    print(f"Good trades: {single_result['good_trades']}")
    print(f"Missed good spikes: {single_result['missed_good']}")
    print(f"Missed bad spikes: {single_result['missed_bad']}")

    total_good = single_result['good_trades'] + single_result['missed_good']
    capture_rate = single_result['good_trades'] / total_good if total_good > 0 else 0
    print(f"\nGood spike capture rate: {100*capture_rate:.1f}%")
    print(f"Missed opportunity: {single_result['missed_good']} good spikes")

    # Estimate missed $/hr
    # Assume each good spike = ~$0.10 profit at 50 shares (conservative)
    missed_profit_per_spike = 0.10 * SHARES_SINGLE  # $5 per missed good spike
    hours = (max(s.timestamp_ms for s in spikes) - min(s.timestamp_ms for s in spikes)) / 3600000
    missed_per_hour = single_result['missed_good'] / hours
    missed_profit_per_hour = missed_per_hour * missed_profit_per_spike

    print(f"\nMissed good spikes per hour: {missed_per_hour:.1f}")
    print(f"Estimated missed profit per hour: ${missed_profit_per_hour:.2f}")

    # Simulate multi-cycle
    print("\n" + "=" * 70)
    print("MULTI-CYCLE SIMULATION (4 Parallel Cycles)")
    print("=" * 70)

    for n_cycles in [2, 3, 4]:
        multi_result = simulate_multi_cycle(spikes, max_cycles=n_cycles)
        print(f"\n{n_cycles} parallel cycles:")
        print(f"  Total trades: {multi_result['total_trades']}")
        print(f"  Good trades: {multi_result['good_trades']}")
        print(f"  Avg concurrent: {multi_result['avg_concurrent']:.1f}")

        # Calculate improvement
        improvement = multi_result['good_trades'] - single_result['good_trades']
        improvement_pct = 100 * improvement / single_result['good_trades'] if single_result['good_trades'] > 0 else 0
        print(f"  Additional good trades: +{improvement} ({improvement_pct:+.1f}%)")

        # Estimated profit improvement
        profit_per_trade = 0.10 * (SHARES_SINGLE / n_cycles)  # Smaller size per trade
        additional_profit = improvement * profit_per_trade
        print(f"  Estimated additional profit: ${additional_profit:.2f} ({additional_profit/hours:.2f}/hr)")

    # Risk analysis
    print("\n" + "=" * 70)
    print("RISK ANALYSIS: SINGLE vs MULTI-CYCLE")
    print("=" * 70)

    print("\nSingle Cycle (50 shares):")
    print(f"  Max loss per trade: ${0.50 * SHARES_SINGLE:.2f} (50% price drop)")
    print(f"  Max concurrent exposure: $50 (1 position)")
    print(f"  Polymarket min order: $1 @ 10 shares minimum")

    print("\nMulti-Cycle (4 × 10 shares = 40 total):")
    print(f"  Max loss per trade: ${0.50 * SHARES_PER_CYCLE:.2f}")
    print(f"  Max concurrent exposure: ${4 * 0.50 * SHARES_PER_CYCLE:.2f} (4 positions)")
    print(f"  Polymarket min order: $1 @ 10 shares ✓ (meets minimum)")

    print("\nDiversification benefit:")
    print("  - Single bad spike = limited impact (10 shares vs 50)")
    print("  - More frequent small wins vs fewer large wins")
    print("  - Better capital efficiency (rarely fully utilized with single)")

    # Conclusion
    print("\n" + "=" * 70)
    print("CONCLUSIONS")
    print("=" * 70)

    print(f"""
1. MISSED OPPORTUNITY COST:
   - Currently miss {single_result['missed_good']} good spikes ({missed_per_hour:.1f}/hr)
   - Estimated missed profit: ${missed_profit_per_hour:.2f}/hr

2. MULTI-CYCLE BENEFITS:
   - 4 cycles captures {multi_result['good_trades'] - single_result['good_trades']} more good spikes
   - Improvement: {improvement_pct:+.1f}% more good trades

3. TRADE-OFFS:
   - Smaller size per trade (10 shares vs 50)
   - More complex position tracking
   - Higher transaction frequency

4. RECOMMENDATION:
   - Multi-cycle is worthwhile if edge per trade is consistent
   - Start with 2 cycles for simpler implementation
   - Scale to 4 cycles after validation
""")


if __name__ == "__main__":
    main()
