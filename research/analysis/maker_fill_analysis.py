#!/usr/bin/env python3
"""
MAKER FILL ANALYSIS - Would posting at best_bid get filled?

Currently live trading takes at ASK (taker) - pays 4.9 shares due to $1 min.
This script analyzes: if we posted at BID (maker), how often would we get fills?

Approach:
1. At each spike signal, record the best_bid
2. Scan forward to see if ask ever drops to that bid level
3. Calculate fill rate and time-to-fill

Using data from time_stop_skip_comparison.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
import sys
import math
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

# =============================================================================
# CONSTANTS
# =============================================================================

TARGET_SHARES = 50
MIN_TIME = 180  # TIME120s_SKIP config
MIN_RUNTIME_SECS = 300
MIN_CYCLE_GAP_MS = 1000

# Skip rule threshold
HIGH_ENTRY_THRESHOLD = 0.90

# Spike detection (OU method)
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10

# Hedge pricing
DROP_MULTIPLIER = 0.50
DROP_INTERCEPT = 0.08
TARGET_PAIR_COST = 0.99

# Velocity
RAW_VELOCITY_THRESHOLD = 0.10

# Time windows to check for maker fill
MAKER_FILL_WINDOWS = [5, 10, 15, 30, 60, 120]  # seconds


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class MakerFillResult:
    market_slug: str
    entry_time_remaining: float
    winner_side: str
    taker_price: float  # What we'd pay as taker (ask)
    maker_price: float  # What we'd post as maker (bid)
    filled_within_5s: bool
    filled_within_10s: bool
    filled_within_15s: bool
    filled_within_30s: bool
    filled_within_60s: bool
    filled_within_120s: bool
    time_to_fill_ms: Optional[int]  # None if never filled
    fill_price: Optional[float]  # Actual fill price if filled


# =============================================================================
# OU PARAMETERS
# =============================================================================

_ou_params = None


def load_ou_params():
    global _ou_params
    try:
        from src.strategies.ou_volatility import OUParameters
        _ou_params = OUParameters.load("research/ou_params.json")
        print(f"[OU] Loaded: mu={_ou_params.mu:.4f}, sigma_stat={_ou_params.sigma_stat:.4f}")
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
# SPIKE DETECTION
# =============================================================================

def detect_spikes_ou(btc_df: pd.DataFrame, lookback: int = 72) -> pd.DataFrame:
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    df['price_prev'] = df['price'].shift(lookback)
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

    return df[['timestamp_ms', 'price', 'spike_detected', 'spike_direction',
               'spike_magnitude', 'threshold', 'zscore']]


def confirm_baseline(spike_dir: str, velocity: float) -> bool:
    """BASELINE: Current velocity-only confirmation."""
    if spike_dir == "UP":
        return velocity > -RAW_VELOCITY_THRESHOLD
    else:
        return velocity < RAW_VELOCITY_THRESHOLD


# =============================================================================
# MAKER FILL SIMULATION
# =============================================================================

def simulate_maker_fills(
    spikes_df: pd.DataFrame,
    obs_df: pd.DataFrame,
    slug: str,
    z_lo: float = 0.0,
    z_hi: float = 1.5,
) -> List[MakerFillResult]:
    """
    Simulate posting at best_bid and check if we'd get filled.

    A maker order at best_bid gets filled when the ask drops to bid level
    (i.e., someone crosses the spread and hits our bid).
    """
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    market_spikes = spikes_df[
        (spikes_df['timestamp_ms'] >= market_start) &
        (spikes_df['timestamp_ms'] <= market_end)
    ].copy()

    results = []

    # Cycling state
    in_position = False
    last_hedge_ts = 0

    for _, spike_row in market_spikes.iterrows():
        spike_ts = spike_row['timestamp_ms']
        spike_dir = spike_row['spike_direction']
        zscore = spike_row['zscore']

        # Z-score volatility filter
        if zscore < z_lo or zscore > z_hi:
            continue

        # Cycling: Block if still in position
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

        # Get velocity
        velocity_bps = obs_row.get('velocity_bps', 0) or 0

        # Velocity confirmation
        if not confirm_baseline(spike_dir, velocity_bps):
            continue

        # Entry prices
        winner_side = spike_dir
        if winner_side == "UP":
            taker_price = obs_row['up_ask']
            maker_price = obs_row['up_bid']
        else:
            taker_price = obs_row['down_ask']
            maker_price = obs_row['down_bid']

        # Skip rule
        if taker_price > HIGH_ENTRY_THRESHOLD:
            continue

        # Check if maker order would get filled
        # A maker bid gets filled when ask drops to bid level
        filled_times = {5: False, 10: False, 15: False, 30: False, 60: False, 120: False}
        time_to_fill = None
        fill_price = None

        for j in range(obs_idx + 1, len(mdf)):
            scan_row = mdf.iloc[j]
            scan_ts = scan_row['timestamp_ms']
            elapsed_s = (scan_ts - spike_ts) / 1000.0

            # Check if ask dropped to our bid level
            if winner_side == "UP":
                current_ask = scan_row['up_ask']
            else:
                current_ask = scan_row['down_ask']

            # Maker fill condition: ask <= our bid price
            if current_ask <= maker_price:
                if time_to_fill is None:
                    time_to_fill = int(scan_ts - spike_ts)
                    fill_price = maker_price  # We get filled at our bid

                for window in filled_times.keys():
                    if elapsed_s <= window:
                        filled_times[window] = True

            # Stop checking after 120s
            if elapsed_s > 120:
                break

        results.append(MakerFillResult(
            market_slug=slug,
            entry_time_remaining=time_rem,
            winner_side=winner_side,
            taker_price=taker_price,
            maker_price=maker_price,
            filled_within_5s=filled_times[5],
            filled_within_10s=filled_times[10],
            filled_within_15s=filled_times[15],
            filled_within_30s=filled_times[30],
            filled_within_60s=filled_times[60],
            filled_within_120s=filled_times[120],
            time_to_fill_ms=time_to_fill,
            fill_price=fill_price,
        ))

        # Simulate exiting position after 120s (for cycling)
        in_position = True
        last_hedge_ts = spike_ts + 120000  # 120s later
        in_position = False

    return results


# =============================================================================
# DATA LOADING
# =============================================================================

def load_data(period: str = "is_oos2") -> Tuple[pd.DataFrame, pd.DataFrame, float]:
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
    elif period == "all":
        # Combine all periods
        btc_dfs = []
        obs_dfs = []

        # IS+OOS2
        btc_dir = Path("research/binance_hf")
        for f in sorted(btc_dir.glob("btc_prices_*.csv")):
            if "recovered" not in f.name:
                btc_dfs.append(pd.read_csv(f))

        obs_dir = Path("research/observer")
        for f in sorted(obs_dir.glob("grid_obs_*.csv")):
            if "combined" not in f.name and "oos5" not in f.name and "recovered" not in f.name:
                obs_dfs.append(pd.read_csv(f, on_bad_lines='skip', low_memory=False))

        # OOS3+4
        btc_dfs.append(pd.read_csv("research/observer/btc_prices_oos3_oos4_combined.csv"))
        obs_dfs.append(pd.read_csv("research/observer/grid_obs_oos3_oos4_combined.csv",
                                   on_bad_lines='skip', low_memory=False))

        # OOS5
        btc_dfs.append(pd.read_csv("research/binance_hf/btc_prices_20260124_recovered.csv"))
        obs_dfs.append(pd.read_csv("research/observer/grid_obs_oos5.csv",
                                   on_bad_lines='skip', low_memory=False))

        btc_df = pd.concat(btc_dfs, ignore_index=True)
        obs_df = pd.concat(obs_dfs, ignore_index=True)
    else:  # is_oos2
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
            if "combined" not in f.name and "oos5" not in f.name and "recovered" not in f.name:
                df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
                obs_dfs.append(df)
        obs_df = pd.concat(obs_dfs, ignore_index=True)

    btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])

    print(f"  BTC prices: {len(btc_df):,} rows")
    print(f"  Observer: {len(obs_df):,} rows")

    # Detect spikes
    spikes_df = detect_spikes_ou(btc_df)

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
    spikes_df = spikes_df[
        (spikes_df['timestamp_ms'] >= overlap_start) &
        (spikes_df['timestamp_ms'] <= overlap_end)
    ]
    obs_df = obs_df[
        (obs_df['timestamp_ms'] >= overlap_start) &
        (obs_df['timestamp_ms'] <= overlap_end)
    ].copy()

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

    spikes_only = spikes_df[spikes_df['spike_detected'] == True].copy()
    print(f"  Spike events: {len(spikes_only):,}")

    return spikes_only, obs_df, hours


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 80)
    print("MAKER FILL ANALYSIS - Would posting at best_bid get filled?")
    print("=" * 80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    load_ou_params()

    # Load all data
    spikes_df, obs_df, hours = load_data("all")
    print()

    # Run simulation
    print("Simulating maker fills...")
    all_results = []

    for slug in obs_df['market_slug'].unique():
        results = simulate_maker_fills(spikes_df, obs_df, slug)
        all_results.extend(results)

    print(f"Total signals analyzed: {len(all_results)}")
    print()

    # Analyze fill rates
    print("=" * 80)
    print("MAKER FILL RATE ANALYSIS")
    print("=" * 80)
    print()

    n = len(all_results)
    if n == 0:
        print("No signals found!")
        return

    fill_5s = sum(1 for r in all_results if r.filled_within_5s)
    fill_10s = sum(1 for r in all_results if r.filled_within_10s)
    fill_15s = sum(1 for r in all_results if r.filled_within_15s)
    fill_30s = sum(1 for r in all_results if r.filled_within_30s)
    fill_60s = sum(1 for r in all_results if r.filled_within_60s)
    fill_120s = sum(1 for r in all_results if r.filled_within_120s)

    print(f"Maker Fill Rates (n={n} signals):")
    print(f"  Within  5s: {fill_5s:4} / {n} = {fill_5s/n*100:5.1f}%")
    print(f"  Within 10s: {fill_10s:4} / {n} = {fill_10s/n*100:5.1f}%")
    print(f"  Within 15s: {fill_15s:4} / {n} = {fill_15s/n*100:5.1f}%")
    print(f"  Within 30s: {fill_30s:4} / {n} = {fill_30s/n*100:5.1f}%")
    print(f"  Within 60s: {fill_60s:4} / {n} = {fill_60s/n*100:5.1f}%")
    print(f"  Within 120s: {fill_120s:4} / {n} = {fill_120s/n*100:5.1f}%")
    print()

    # Time to fill distribution for those that filled
    filled = [r for r in all_results if r.time_to_fill_ms is not None]
    if filled:
        times = [r.time_to_fill_ms / 1000 for r in filled]
        print(f"Time-to-Fill Distribution (n={len(filled)} filled):")
        print(f"  Min:    {min(times):6.1f}s")
        print(f"  Median: {np.median(times):6.1f}s")
        print(f"  Mean:   {np.mean(times):6.1f}s")
        print(f"  Max:    {max(times):6.1f}s")
        print()

    # Spread analysis
    spreads = [r.taker_price - r.maker_price for r in all_results]
    print(f"Spread Analysis (Taker - Maker price):")
    print(f"  Min:    ${min(spreads):.3f}")
    print(f"  Median: ${np.median(spreads):.3f}")
    print(f"  Mean:   ${np.mean(spreads):.3f}")
    print(f"  Max:    ${max(spreads):.3f}")
    print()

    # Savings analysis
    print("=" * 80)
    print("COST COMPARISON: TAKER vs MAKER")
    print("=" * 80)
    print()

    # Calculate costs for taker
    taker_costs = [r.taker_price for r in all_results]
    avg_taker = np.mean(taker_costs)

    # Calculate costs for maker (only for those that filled within time limit)
    maker_costs_60s = [r.maker_price for r in all_results if r.filled_within_60s]
    avg_maker_60s = np.mean(maker_costs_60s) if maker_costs_60s else 0

    maker_costs_120s = [r.maker_price for r in all_results if r.filled_within_120s]
    avg_maker_120s = np.mean(maker_costs_120s) if maker_costs_120s else 0

    print(f"Average Entry Cost:")
    print(f"  Taker (100% fill):      ${avg_taker:.4f}")
    print(f"  Maker (60s fill rate):  ${avg_maker_60s:.4f} ({fill_60s/n*100:.1f}% fill rate)")
    print(f"  Maker (120s fill rate): ${avg_maker_120s:.4f} ({fill_120s/n*100:.1f}% fill rate)")
    print()

    # PnL impact
    spread_saved = avg_taker - avg_maker_120s if maker_costs_120s else 0
    pnl_diff_per_trade = spread_saved * TARGET_SHARES

    print(f"Per-Trade Savings (if filled as maker):")
    print(f"  Spread saved: ${spread_saved:.4f} x {TARGET_SHARES} shares = ${pnl_diff_per_trade:.2f}")
    print()

    # But we need to factor in fill rate
    expected_trades_maker = fill_120s
    expected_trades_taker = n

    taker_pnl_component = expected_trades_taker * avg_taker * TARGET_SHARES
    maker_pnl_component = expected_trades_maker * avg_maker_120s * TARGET_SHARES

    print(f"Expected Entry Costs (over {hours:.1f} hours):")
    print(f"  Taker: {expected_trades_taker} trades x ${avg_taker:.3f} x {TARGET_SHARES} = ${taker_pnl_component:.2f}")
    print(f"  Maker: {expected_trades_maker} trades x ${avg_maker_120s:.3f} x {TARGET_SHARES} = ${maker_pnl_component:.2f}")
    print(f"  Difference: ${taker_pnl_component - maker_pnl_component:.2f} (maker is {'cheaper' if maker_pnl_component < taker_pnl_component else 'more expensive'})")
    print()

    # Save detailed results
    rows = []
    for r in all_results:
        rows.append({
            'market_slug': r.market_slug,
            'entry_time_remaining': r.entry_time_remaining,
            'winner_side': r.winner_side,
            'taker_price': r.taker_price,
            'maker_price': r.maker_price,
            'spread': r.taker_price - r.maker_price,
            'filled_5s': r.filled_within_5s,
            'filled_10s': r.filled_within_10s,
            'filled_15s': r.filled_within_15s,
            'filled_30s': r.filled_within_30s,
            'filled_60s': r.filled_within_60s,
            'filled_120s': r.filled_within_120s,
            'time_to_fill_ms': r.time_to_fill_ms,
            'time_to_fill_s': r.time_to_fill_ms / 1000 if r.time_to_fill_ms else None,
        })

    output_path = "research/maker_fill_analysis_results.csv"
    pd.DataFrame(rows).to_csv(output_path, index=False)
    print(f"Detailed results saved to: {output_path}")
    print()

    # Recommendation
    print("=" * 80)
    print("RECOMMENDATION")
    print("=" * 80)
    print()

    if fill_60s / n > 0.8:
        print("HIGH fill rate (>80% within 60s) - MAKER orders are viable!")
        print("Recommend: Post at best_bid, save spread cost")
    elif fill_60s / n > 0.5:
        print("MODERATE fill rate (50-80% within 60s) - Mixed results")
        print("Recommend: Consider hybrid approach (maker first, then taker if unfilled)")
    else:
        print("LOW fill rate (<50% within 60s) - TAKER orders preferred")
        print("Recommend: Continue taking at ask for guaranteed fills")

    print()
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
