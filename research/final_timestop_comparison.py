#!/usr/bin/env python3
"""
FINAL TIME-STOP COMPARISON

Settings:
- Cycling: ON (correct mechanism - blocks during position, gap after hedge fill)
- Skip high entry: >= $0.90 (cannot hedge, turkey risk)
- Vol zone: 0 < z < 1.5
- Compare: TIME120s, TIME180s, TIME300s

This is the definitive test for the plan file.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Tuple
import sys
import math
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

TARGET_SHARES = 50
MIN_TIME = 60
MIN_CYCLE_GAP_MS = 1000

# SKIP THRESHOLD: >= 0.90 (boundary case had false positive)
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
    """Run backtest with correct cycling, skip >= 0.90, z-zone 0-1.5."""

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

        # CORRECT CYCLING STATE
        in_position = False
        last_hedge_ts = 0

        for _, spike_row in market_spikes.iterrows():
            spike_ts = spike_row['timestamp_ms']
            spike_dir = spike_row['spike_direction']
            zscore = spike_row['zscore']
            spike_mag = spike_row['spike_magnitude']

            # Z-ZONE FILTER: 0 <= z < 1.5 (z clamped at 0 from below)
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
            if time_rem < MIN_TIME:
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
    print("FINAL TIME-STOP COMPARISON")
    print("=" * 80)
    print()
    print("Settings:")
    print(f"  - Cycling: ON (correct mechanism)")
    print(f"  - Skip high entry: >= ${SKIP_THRESHOLD:.2f}")
    print(f"  - Vol zone: 0 < z < 1.5")
    print(f"  - Compare: TIME120s, TIME180s, TIME300s")
    print()

    load_ou_params()

    # Load data
    btc_dir = Path("research/binance_hf")
    btc_dfs = [pd.read_csv(f) for f in sorted(btc_dir.glob("btc_prices_*.csv"))
               if "recovered" not in f.name]
    btc_df = pd.concat(btc_dfs, ignore_index=True)

    obs_dir = Path("research/observer")
    obs_dfs = [pd.read_csv(f, on_bad_lines='skip', low_memory=False)
               for f in sorted(obs_dir.glob("grid_obs_*.csv"))
               if "combined" not in f.name and "oos5" not in f.name and "recovered" not in f.name]
    obs_df = pd.concat(obs_dfs, ignore_index=True)

    btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])

    spikes_df = detect_spikes_ou(btc_df)

    res_df = pd.read_csv("research/observer/market_resolutions_verified.csv")
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    btc_start, btc_end = spikes_df['timestamp_ms'].min(), spikes_df['timestamp_ms'].max()
    obs_start, obs_end = obs_df['timestamp_ms'].min(), obs_df['timestamp_ms'].max()
    overlap_start, overlap_end = max(btc_start, obs_start), min(btc_end, obs_end)

    hours = (overlap_end - overlap_start) / 3600000

    spikes_df = spikes_df[(spikes_df['timestamp_ms'] >= overlap_start) &
                          (spikes_df['timestamp_ms'] <= overlap_end)]
    obs_df = obs_df[(obs_df['timestamp_ms'] >= overlap_start) &
                    (obs_df['timestamp_ms'] <= overlap_end)].copy()

    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    valid_slugs = [slug for slug, mdf in obs_df.groupby('market_slug')
                   if (mdf['time_remaining_secs'].max() - mdf['time_remaining_secs'].min()) >= 300
                   and mdf['time_remaining_secs'].max() >= 840]
    obs_df = obs_df[obs_df['market_slug'].isin(valid_slugs)]
    spikes_only = spikes_df[spikes_df['spike_detected'] == True].copy()

    num_markets = len(valid_slugs)
    print(f"Data: {hours:.1f} hours, {num_markets} markets, {len(spikes_only):,} spikes")
    print()

    # Run backtests
    results = []
    for ts in [120, 180, 300]:
        result = run_backtest(spikes_only, obs_df, ts, hours, num_markets)
        results.append(result)

    # Print results
    print("=" * 80)
    print("RESULTS (Cycling ON, Skip >= $0.90, Z-zone 0 < z < 1.5)")
    print("=" * 80)
    print()

    print(f"{'Config':<16} {'Trades':>7} {'$/hr':>9} {'DirAcc':>7} {'Cyc/Mkt':>8} "
          f"{'Skipped':>8} {'Passive':>8} {'TStop':>7} {'Resol':>7}")
    print("-" * 95)

    for r in results:
        passive_pct = r.hedge_passive / r.trades * 100 if r.trades > 0 else 0
        print(f"{r.name:<16} {r.trades:>7} ${r.hourly_rate:>8.2f} {r.direction_accuracy:>6.1%} "
              f"{r.cycles_per_market:>8.2f} {r.skipped_high_entry:>8} "
              f"{r.hedge_passive:>7} {r.hedge_timestop:>7} {r.hedge_resolution:>7}")

    # Summary
    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)

    best = max(results, key=lambda r: r.hourly_rate)
    print(f"\nWINNER: {best.name} at ${best.hourly_rate:.2f}/hr")
    print()

    for r in results:
        passive_pct = r.hedge_passive / r.trades * 100 if r.trades > 0 else 0
        timestop_pct = r.hedge_timestop / r.trades * 100 if r.trades > 0 else 0
        resol_pct = r.hedge_resolution / r.trades * 100 if r.trades > 0 else 0

        print(f"{r.name}:")
        print(f"  Trades: {r.trades} ({r.cycles_per_market:.2f}/market)")
        print(f"  $/hr: ${r.hourly_rate:.2f}")
        print(f"  Avg PnL/trade: ${r.avg_pnl_per_trade:.2f}")
        print(f"  Direction accuracy: {r.direction_accuracy:.1%}")
        print(f"  Exit breakdown: Passive {passive_pct:.0f}%, TimeStop {timestop_pct:.0f}%, Resolution {resol_pct:.0f}%")
        print(f"  High-entry skipped: {r.skipped_high_entry}")
        print()

    # Save results
    rows = [{
        'config': r.name,
        'trades': r.trades,
        'total_pnl': r.total_pnl,
        'hourly_rate': r.hourly_rate,
        'direction_accuracy': r.direction_accuracy,
        'cycles_per_market': r.cycles_per_market,
        'skipped_high_entry': r.skipped_high_entry,
        'hedge_passive': r.hedge_passive,
        'hedge_timestop': r.hedge_timestop,
        'hedge_resolution': r.hedge_resolution,
        'avg_pnl_per_trade': r.avg_pnl_per_trade,
    } for r in results]

    output_path = "research/final_timestop_results.csv"
    pd.DataFrame(rows).to_csv(output_path, index=False)
    print(f"Results saved to: {output_path}")
    print()
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
