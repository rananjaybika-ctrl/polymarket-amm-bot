#!/usr/bin/env python3
"""
TIME-STOP + SKIP RULE COMPARISON

Tests TIME120s, TIME180s, TIME300s with high-entry skip rule.
Skip rule: entry > $0.90 → skip (cannot hedge with $1 min order)

Applied WITHIN the selected vol zone (0 < z < 1.5).

Based on grid search results:
- TIME120s: $12.97/hr (WINNER without skip rule)
- TIME180s: $10.00/hr

Hypothesis: Longer time-stop = more passive fills, but fewer cycles.
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
MIN_TIME = 60
MIN_RUNTIME_SECS = 300
MIN_CYCLE_GAP_MS = 1000

# Skip rule threshold
HIGH_ENTRY_THRESHOLD = 0.90  # Skip if winner_entry > $0.90

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


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class TradeResult:
    config_name: str
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
    zscore: float
    hedge_fill_ts: int
    skipped_high_entry: bool = False  # Track if this would have been skipped


@dataclass
class ConfigResult:
    config_name: str
    trades: int
    total_pnl: float
    hourly_rate: float
    direction_accuracy: float
    trades_per_hour: float
    skipped_count: int = 0  # How many trades were skipped due to high entry
    hedge_breakdown: Dict[str, int] = field(default_factory=dict)


@dataclass
class BacktestConfig:
    name: str
    time_stop_seconds: float
    skip_high_entry: bool
    z_lo: float = 0.0
    z_hi: float = 1.5


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


# =============================================================================
# SIMULATION
# =============================================================================

def calc_loser_bid(winner_entry: float, spike_mag: float) -> float:
    expected_drop = DROP_MULTIPLIER * spike_mag / 100 + DROP_INTERCEPT
    expected_drop = max(0.02, min(0.20, expected_drop))
    max_loser = TARGET_PAIR_COST - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid))


def confirm_baseline(spike_dir: str, velocity: float) -> bool:
    """BASELINE: Current velocity-only confirmation."""
    if spike_dir == "UP":
        return velocity > -RAW_VELOCITY_THRESHOLD
    else:
        return velocity < RAW_VELOCITY_THRESHOLD


def simulate_market(
    spikes_df: pd.DataFrame,
    obs_df: pd.DataFrame,
    slug: str,
    resolution: str,
    config: BacktestConfig,
) -> Tuple[List[TradeResult], int]:
    """
    Simulate trading with FIXED cycling and optional high-entry skip.

    Returns: (trades, skipped_count)
    """
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return [], 0

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    market_spikes = spikes_df[
        (spikes_df['timestamp_ms'] >= market_start) &
        (spikes_df['timestamp_ms'] <= market_end)
    ].copy()

    trades = []
    skipped_count = 0

    # Cycling state
    in_position = False
    last_hedge_ts = 0

    for _, spike_row in market_spikes.iterrows():
        spike_ts = spike_row['timestamp_ms']
        spike_dir = spike_row['spike_direction']
        spike_mag = spike_row['spike_magnitude']
        zscore = spike_row['zscore']

        # Z-score volatility filter (within vol zone!)
        if zscore < config.z_lo or zscore > config.z_hi:
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

        # Entry
        winner_side = spike_dir
        loser_side = "DOWN" if winner_side == "UP" else "UP"

        if winner_side == "UP":
            winner_entry = obs_row['up_ask']
        else:
            winner_entry = obs_row['down_ask']

        # HIGH-ENTRY SKIP RULE (within vol zone!)
        if config.skip_high_entry and winner_entry > HIGH_ENTRY_THRESHOLD:
            skipped_count += 1
            continue

        loser_target = calc_loser_bid(winner_entry, spike_mag)

        # Enter position
        in_position = True
        entry_ts = spike_ts

        # Scan forward for hedge
        hedge_type = "resolution"
        loser_fill = 0.0
        hedge_fill_ts = market_end

        for j in range(obs_idx + 1, len(mdf)):
            scan_row = mdf.iloc[j]
            scan_ts = scan_row['timestamp_ms']

            # Time-based stop check
            elapsed_secs = (scan_ts - entry_ts) / 1000.0
            if elapsed_secs >= config.time_stop_seconds:
                if loser_side == "UP":
                    loser_fill = scan_row['up_ask']
                else:
                    loser_fill = scan_row['down_ask']
                hedge_type = "timestop"
                hedge_fill_ts = scan_ts
                break

            # Passive fill check
            if loser_side == "UP":
                curr_loser_ask = scan_row['up_ask']
            else:
                curr_loser_ask = scan_row['down_ask']

            if curr_loser_ask <= loser_target:
                loser_fill = loser_target
                hedge_type = "passive"
                hedge_fill_ts = scan_ts
                break

        # If no hedge, resolve at market end
        if hedge_type == "resolution":
            if resolution == winner_side:
                loser_fill = loser_target
            else:
                loser_fill = 1.0

        # Calculate PnL
        pair_cost = winner_entry + loser_fill
        if hedge_type == "resolution" and resolution != winner_side:
            pnl = -winner_entry * TARGET_SHARES
        else:
            pnl = (1.0 - pair_cost) * TARGET_SHARES

        trades.append(TradeResult(
            config_name=config.name,
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
            zscore=zscore,
            hedge_fill_ts=hedge_fill_ts,
        ))

        # Exit position
        in_position = False
        last_hedge_ts = hedge_fill_ts

    return trades, skipped_count


def run_config(
    spikes_df: pd.DataFrame,
    obs_df: pd.DataFrame,
    config: BacktestConfig,
    hours: float,
) -> ConfigResult:
    """Run backtest for a single config."""
    all_trades = []
    total_skipped = 0

    for slug in obs_df['market_slug'].unique():
        mdf = obs_df[obs_df['market_slug'] == slug]
        resolution = mdf['resolution'].iloc[0]
        trades, skipped = simulate_market(spikes_df, obs_df, slug, resolution, config)
        all_trades.extend(trades)
        total_skipped += skipped

    if not all_trades:
        return ConfigResult(
            config_name=config.name,
            trades=0,
            total_pnl=0,
            hourly_rate=0,
            direction_accuracy=0,
            trades_per_hour=0,
            skipped_count=total_skipped,
        )

    total_pnl = sum(t.pnl for t in all_trades)
    total_trades = len(all_trades)
    correct = sum(1 for t in all_trades if t.correct_direction)
    hourly_rate = total_pnl / hours if hours > 0 else 0

    # Hedge breakdown
    hedge_breakdown = {}
    for t in all_trades:
        hedge_breakdown[t.hedge_type] = hedge_breakdown.get(t.hedge_type, 0) + 1

    return ConfigResult(
        config_name=config.name,
        trades=total_trades,
        total_pnl=total_pnl,
        hourly_rate=hourly_rate,
        direction_accuracy=correct / total_trades,
        trades_per_hour=total_trades / hours if hours > 0 else 0,
        skipped_count=total_skipped,
        hedge_breakdown=hedge_breakdown,
    )


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
    print("TIME-STOP + SKIP RULE COMPARISON")
    print("=" * 80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    print(f"Skip rule: entry > ${HIGH_ENTRY_THRESHOLD:.2f} → skip (cannot hedge)")
    print(f"Vol zone: 0 < z < 1.5 (standard)")
    print()

    load_ou_params()

    # Define configs to test
    configs = [
        # Without skip rule (baseline comparison)
        BacktestConfig("TIME120s", time_stop_seconds=120, skip_high_entry=False),
        BacktestConfig("TIME180s", time_stop_seconds=180, skip_high_entry=False),
        BacktestConfig("TIME300s", time_stop_seconds=300, skip_high_entry=False),
        # With skip rule
        BacktestConfig("TIME120s_SKIP", time_stop_seconds=120, skip_high_entry=True),
        BacktestConfig("TIME180s_SKIP", time_stop_seconds=180, skip_high_entry=True),
        BacktestConfig("TIME300s_SKIP", time_stop_seconds=300, skip_high_entry=True),
    ]

    print(f"Configs to test: {len(configs)}")
    print()

    # Load data
    spikes_df, obs_df, hours = load_data("is_oos2")
    print()

    # Run backtests
    results = []
    print("Running backtests...")
    print("-" * 80)

    for config in configs:
        result = run_config(spikes_df, obs_df, config, hours)
        results.append(result)

        passive = result.hedge_breakdown.get('passive', 0)
        timestop = result.hedge_breakdown.get('timestop', 0)
        resolution = result.hedge_breakdown.get('resolution', 0)

        print(f"  {config.name:15} | Trades={result.trades:4} | "
              f"$/hr=${result.hourly_rate:8.2f} | Acc={result.direction_accuracy:.1%} | "
              f"Skipped={result.skipped_count:3} | P:{passive} T:{timestop} R:{resolution}")

    # Summary table
    print()
    print("=" * 80)
    print("SUMMARY - TIME-STOP + SKIP RULE COMPARISON")
    print("=" * 80)
    print()

    print(f"{'Config':<20} {'Trades':>7} {'$/hr':>10} {'Dir Acc':>8} {'Skipped':>8} "
          f"{'Passive%':>9} {'TimeStop%':>10} {'Resol%':>7}")
    print("-" * 90)

    for r in results:
        total = r.trades
        passive = r.hedge_breakdown.get('passive', 0)
        timestop = r.hedge_breakdown.get('timestop', 0)
        resolution = r.hedge_breakdown.get('resolution', 0)

        passive_pct = passive / total * 100 if total > 0 else 0
        timestop_pct = timestop / total * 100 if total > 0 else 0
        resolution_pct = resolution / total * 100 if total > 0 else 0

        print(f"{r.config_name:<20} {r.trades:>7} ${r.hourly_rate:>9.2f} "
              f"{r.direction_accuracy:>7.1%} {r.skipped_count:>8} "
              f"{passive_pct:>8.1f}% {timestop_pct:>9.1f}% {resolution_pct:>6.1f}%")

    # Save results
    output_path = "research/time_stop_skip_comparison_results.csv"
    rows = []
    for r in results:
        rows.append({
            'config_name': r.config_name,
            'trades': r.trades,
            'total_pnl': r.total_pnl,
            'hourly_rate': r.hourly_rate,
            'direction_accuracy': r.direction_accuracy,
            'trades_per_hour': r.trades_per_hour,
            'skipped_count': r.skipped_count,
            'hedge_passive': r.hedge_breakdown.get('passive', 0),
            'hedge_timestop': r.hedge_breakdown.get('timestop', 0),
            'hedge_resolution': r.hedge_breakdown.get('resolution', 0),
        })
    pd.DataFrame(rows).to_csv(output_path, index=False)
    print()
    print(f"Results saved to: {output_path}")

    # Insights
    print()
    print("=" * 80)
    print("KEY INSIGHTS")
    print("=" * 80)

    # Compare with/without skip
    for ts in [120, 180, 300]:
        no_skip = [r for r in results if r.config_name == f"TIME{ts}s"][0]
        with_skip = [r for r in results if r.config_name == f"TIME{ts}s_SKIP"][0]

        delta_hr = with_skip.hourly_rate - no_skip.hourly_rate
        delta_pct = (delta_hr / no_skip.hourly_rate * 100) if no_skip.hourly_rate > 0 else 0

        print(f"\nTIME{ts}s:")
        print(f"  Without skip: ${no_skip.hourly_rate:.2f}/hr ({no_skip.trades} trades)")
        print(f"  With skip:    ${with_skip.hourly_rate:.2f}/hr ({with_skip.trades} trades, {with_skip.skipped_count} skipped)")
        print(f"  Delta:        ${delta_hr:+.2f}/hr ({delta_pct:+.1f}%)")

    # Find best config
    best = max(results, key=lambda r: r.hourly_rate)
    print()
    print(f"BEST CONFIG: {best.config_name} at ${best.hourly_rate:.2f}/hr")
    print()
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
