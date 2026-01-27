#!/usr/bin/env python3
"""
OOS5 VALIDATION

Validate TIME120s_SKIP, TIME180s_SKIP, TIME300s_SKIP on fresh OOS5 data.

Settings (same as final_timestop_comparison.py):
- Cycling: ON (correct mechanism)
- Skip high entry: >= $0.90
- Vol zone: 0 <= z <= 1.5
- MIN_TIME = time_stop + 60s buffer
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict
import sys
import math
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

TARGET_SHARES = 50
MIN_CYCLE_GAP_MS = 1000
SKIP_THRESHOLD = 0.90

OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10

DROP_MULTIPLIER = 0.50
DROP_INTERCEPT = 0.08
TARGET_PAIR_COST = 0.99
RAW_VELOCITY_THRESHOLD = 0.10

_ou_params = None


def load_ou_params():
    global _ou_params
    try:
        from src.strategies.ou_volatility import OUParameters
        _ou_params = OUParameters.load("research/ou_params.json")
        print(f"[OU] Loaded: mu={_ou_params.mu:.4f}")
    except:
        pass


def compute_ou_threshold(volatility):
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


def detect_spikes_ou(btc_df, lookback=72):
    df = btc_df.copy().sort_values('timestamp_ms').reset_index(drop=True)
    df['price_prev'] = df['price'].shift(lookback)
    df['change_pct'] = (df['price'] - df['price_prev']) / df['price_prev'] * 100
    df['magnitude'] = df['change_pct'].abs()

    returns = df['price'].pct_change() * 100
    alpha = 1 - 0.5 ** (1.0 / 300)
    variance = returns.iloc[:60].var() if len(returns) > 60 else 0.01
    volatilities, zscores = [], []

    for r in returns:
        if pd.isna(r):
            volatilities.append(0.01)
            zscores.append(0.5)
            continue
        variance = alpha * (r ** 2) + (1 - alpha) * variance
        vol = max(np.sqrt(variance), 1e-6)
        volatilities.append(vol)
        if _ou_params:
            z = (math.log(vol) - _ou_params.mu) / _ou_params.sigma_stat
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


@dataclass
class ConfigResult:
    name: str
    trades: int
    total_pnl: float
    hourly_rate: float
    direction_accuracy: float
    cycles_per_market: float
    skipped_high_entry: int
    hedge_passive: int
    hedge_timestop: int
    hedge_resolution: int
    avg_pnl_per_trade: float


def run_backtest(spikes_df, obs_df, time_stop_seconds: int, hours: float, num_markets: int) -> ConfigResult:
    """Run backtest with correct MIN_TIME = time_stop + 60s."""

    # CRITICAL: MIN_TIME = time_stop + 60s buffer
    min_time = time_stop_seconds + 60

    all_pnl = []
    all_correct = []
    hedge_types = {'passive': 0, 'timestop': 0, 'resolution': 0}
    skipped = 0

    for slug in obs_df['market_slug'].unique():
        mdf = obs_df[obs_df['market_slug'] == slug].copy()
        mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)
        resolution = mdf['resolution'].iloc[0]

        market_start = mdf['timestamp_ms'].min()
        market_end = mdf['timestamp_ms'].max()

        market_spikes = spikes_df[
            (spikes_df['timestamp_ms'] >= market_start) &
            (spikes_df['timestamp_ms'] <= market_end)
        ]

        in_position = False
        last_hedge_ts = 0

        for _, spike_row in market_spikes.iterrows():
            spike_ts = spike_row['timestamp_ms']
            spike_dir = spike_row['spike_direction']
            zscore = spike_row['zscore']
            spike_mag = spike_row['spike_magnitude']

            # Z-ZONE FILTER: 0 <= z <= 1.5
            if zscore < 0.0 or zscore > 1.5:
                continue

            # CYCLING: Block if in position
            if in_position:
                continue

            # CYCLING: Gap after hedge fill
            if (spike_ts - last_hedge_ts) < MIN_CYCLE_GAP_MS:
                continue

            obs_idx = mdf['timestamp_ms'].searchsorted(spike_ts)
            if obs_idx >= len(mdf):
                obs_idx = len(mdf) - 1

            obs_row = mdf.iloc[obs_idx]
            time_rem = obs_row['time_remaining_secs']

            # CRITICAL: MIN_TIME = time_stop + 60s (only blocks NEW entries)
            if time_rem < min_time:
                continue

            velocity_bps = obs_row.get('velocity_bps', 0) or 0
            if spike_dir == "UP" and velocity_bps <= -RAW_VELOCITY_THRESHOLD:
                continue
            if spike_dir == "DOWN" and velocity_bps >= RAW_VELOCITY_THRESHOLD:
                continue

            winner_side = spike_dir
            loser_side = "DOWN" if winner_side == "UP" else "UP"

            if winner_side == "UP":
                winner_entry = obs_row['up_ask']
            else:
                winner_entry = obs_row['down_ask']

            # SKIP HIGH ENTRY: >= 0.90
            if winner_entry >= SKIP_THRESHOLD:
                skipped += 1
                continue

            # Calculate loser target
            expected_drop = DROP_MULTIPLIER * spike_mag / 100 + DROP_INTERCEPT
            expected_drop = max(0.02, min(0.20, expected_drop))
            max_loser = TARGET_PAIR_COST - winner_entry
            loser_target = min((1.0 - winner_entry) - expected_drop, max_loser)
            loser_target = max(0.01, min(0.95, loser_target))

            # ENTER POSITION
            in_position = True
            entry_ts = spike_ts

            # Simulate hedge
            hedge_type = "resolution"
            loser_fill = 0.0
            hedge_fill_ts = market_end

            for j in range(obs_idx + 1, len(mdf)):
                scan_row = mdf.iloc[j]
                scan_ts = scan_row['timestamp_ms']

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

                # Time-stop check
                elapsed_secs = (scan_ts - entry_ts) / 1000.0
                if elapsed_secs >= time_stop_seconds:
                    loser_fill = curr_loser_ask
                    hedge_type = "timestop"
                    hedge_fill_ts = scan_ts
                    break

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

            all_pnl.append(pnl)
            all_correct.append(resolution == winner_side)
            hedge_types[hedge_type] += 1

            # EXIT POSITION
            in_position = False
            last_hedge_ts = hedge_fill_ts

    trades = len(all_pnl)
    total_pnl = sum(all_pnl)

    return ConfigResult(
        name=f"TIME{time_stop_seconds}s_SKIP",
        trades=trades,
        total_pnl=total_pnl,
        hourly_rate=total_pnl / hours if hours > 0 else 0,
        direction_accuracy=sum(all_correct) / trades if trades > 0 else 0,
        cycles_per_market=trades / num_markets if num_markets > 0 else 0,
        skipped_high_entry=skipped,
        hedge_passive=hedge_types['passive'],
        hedge_timestop=hedge_types['timestop'],
        hedge_resolution=hedge_types['resolution'],
        avg_pnl_per_trade=total_pnl / trades if trades > 0 else 0,
    )


def main():
    print("=" * 80)
    print("OOS5 VALIDATION")
    print("=" * 80)
    print()
    print("Settings:")
    print(f"  - Cycling: ON (correct mechanism)")
    print(f"  - Skip high entry: >= ${SKIP_THRESHOLD:.2f}")
    print(f"  - Vol zone: 0 <= z <= 1.5")
    print(f"  - MIN_TIME = time_stop + 60s buffer")
    print()

    load_ou_params()

    # Load OOS5 data
    obs_path = Path("research/observer/grid_obs_oos5.csv")
    if not obs_path.exists():
        print(f"ERROR: OOS5 data not found at {obs_path}")
        return

    print(f"Loading OOS5 observer data from {obs_path}...")
    obs_df = pd.read_csv(obs_path, on_bad_lines='skip', low_memory=False)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])

    # Load BTC price data - OOS5 needs recovered file (Jan 24-26)
    btc_path = Path("research/binance_hf/btc_prices_20260124_recovered.csv")
    if not btc_path.exists():
        # Fallback to combined file
        btc_path = Path("research/binance_hf/btc_prices_combined.csv")

    if not btc_path.exists():
        print(f"ERROR: BTC price data not found")
        return

    print(f"Loading BTC price data from {btc_path}...")
    btc_df = pd.read_csv(btc_path)
    btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')

    # Detect spikes
    spikes_df = detect_spikes_ou(btc_df)

    # Load resolutions
    res_df = pd.read_csv("research/observer/market_resolutions_verified.csv")
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Calculate overlap
    btc_start, btc_end = spikes_df['timestamp_ms'].min(), spikes_df['timestamp_ms'].max()
    obs_start, obs_end = obs_df['timestamp_ms'].min(), obs_df['timestamp_ms'].max()
    overlap_start, overlap_end = max(btc_start, obs_start), min(btc_end, obs_end)

    hours = (overlap_end - overlap_start) / 3600000

    # Filter to overlap
    spikes_df = spikes_df[(spikes_df['timestamp_ms'] >= overlap_start) &
                          (spikes_df['timestamp_ms'] <= overlap_end)]
    obs_df = obs_df[(obs_df['timestamp_ms'] >= overlap_start) &
                    (obs_df['timestamp_ms'] <= overlap_end)].copy()

    # Add resolutions
    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    # Filter valid markets (at least 300s coverage, starts at 840s+)
    valid_slugs = [slug for slug, mdf in obs_df.groupby('market_slug')
                   if (mdf['time_remaining_secs'].max() - mdf['time_remaining_secs'].min()) >= 300
                   and mdf['time_remaining_secs'].max() >= 840]
    obs_df = obs_df[obs_df['market_slug'].isin(valid_slugs)]
    spikes_only = spikes_df[spikes_df['spike_detected'] == True].copy()

    num_markets = len(valid_slugs)
    print(f"\nOOS5 Data: {hours:.1f} hours, {num_markets} markets, {len(spikes_only):,} spikes")
    print()

    # Run backtests
    results = []
    for ts in [120, 180, 300]:
        result = run_backtest(spikes_only, obs_df, ts, hours, num_markets)
        results.append(result)

    # Print results
    print("=" * 80)
    print("OOS5 RESULTS (Cycling ON, Skip >= $0.90, MIN_TIME = time_stop + 60s)")
    print("=" * 80)
    print()

    print(f"{'Config':<16} {'MinTime':>7} {'Trades':>7} {'$/hr':>9} {'DirAcc':>7} "
          f"{'Passive':>8} {'TStop':>7} {'Resol':>7}")
    print("-" * 85)

    for r in results:
        ts = int(r.name.replace("TIME", "").replace("s_SKIP", ""))
        min_time = ts + 60
        print(f"{r.name:<16} {min_time:>5}s {r.trades:>7} ${r.hourly_rate:>8.2f} {r.direction_accuracy:>6.1%} "
              f"{r.hedge_passive:>8} {r.hedge_timestop:>7} {r.hedge_resolution:>7}")

    # Summary
    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)

    best = max(results, key=lambda r: r.hourly_rate)
    print(f"\nWINNER: {best.name} at ${best.hourly_rate:.2f}/hr")

    # Verify no resolution exits
    total_res = sum(r.hedge_resolution for r in results)
    if total_res == 0:
        print("\nAll trades properly hedged (resolution exits = 0)")
    else:
        print(f"\nWARNING: {total_res} resolution exits found!")

    print()
    for r in results:
        ts = int(r.name.replace("TIME", "").replace("s_SKIP", ""))
        min_time = ts + 60
        passive_pct = r.hedge_passive / r.trades * 100 if r.trades > 0 else 0
        timestop_pct = r.hedge_timestop / r.trades * 100 if r.trades > 0 else 0

        print(f"{r.name} (min_time={min_time}s):")
        print(f"  Trades: {r.trades} ({r.cycles_per_market:.2f}/market)")
        print(f"  $/hr: ${r.hourly_rate:.2f}")
        print(f"  Avg PnL/trade: ${r.avg_pnl_per_trade:.2f}")
        print(f"  Direction accuracy: {r.direction_accuracy:.1%}")
        print(f"  Exit breakdown: Passive {passive_pct:.0f}%, TimeStop {timestop_pct:.0f}%, Resolution {r.hedge_resolution}")
        print(f"  High-entry skipped: {r.skipped_high_entry}")
        print()

    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
