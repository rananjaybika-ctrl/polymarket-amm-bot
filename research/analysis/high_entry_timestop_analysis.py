#!/usr/bin/env python3
"""
HIGH-ENTRY TIME-STOP ANALYSIS

The REAL turkey problem: High entry + time-stop = LOSS even when direction correct

When entry > $0.90:
- At time-stop, loser side may not have dropped
- Pair cost = winner_entry + loser_ask_at_timestop
- If pair_cost > $1.00 → LOSS even with correct direction

Analyze time-stop exits for high-entry trades across TIME120s, TIME180s, TIME300s.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict
import sys
import math
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))

TARGET_SHARES = 50
MIN_TIME = 60
MIN_CYCLE_GAP_MS = 1000

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
class TimeStopTrade:
    market: str
    winner_entry: float
    loser_fill_at_stop: float
    pair_cost: float
    pnl: float
    time_stop_seconds: int
    direction_correct: bool
    winner_side: str


def analyze_timestops(spikes_df, obs_df, time_stop_seconds: int) -> List[TimeStopTrade]:
    """Analyze all time-stop exits for given time-stop setting."""

    trades = []

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

            # Z-zone filter (0 < z < 1.5)
            if zscore < 0.0 or zscore > 1.5:
                continue
            if in_position:
                continue
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

            # Calculate loser target for passive fill check
            # FIX: Do NOT divide by 100 - spike_mag is already percentage (0.05 = 0.05%)
            expected_drop = DROP_MULTIPLIER * spike_mag + DROP_INTERCEPT
            expected_drop = max(0.02, min(0.20, expected_drop))
            max_loser = TARGET_PAIR_COST - winner_entry
            loser_target = min((1.0 - winner_entry) - expected_drop, max_loser)
            loser_target = max(0.01, min(0.95, loser_target))

            in_position = True
            entry_ts = spike_ts

            # Simulate - look for passive fill or time-stop
            hedge_type = "resolution"
            loser_fill = 0.0
            hedge_fill_ts = market_end

            for j in range(obs_idx + 1, len(mdf)):
                scan_row = mdf.iloc[j]
                scan_ts = scan_row['timestamp_ms']

                # Check passive fill first
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

            direction_correct = (resolution == winner_side)

            # Only record time-stop exits for high-entry trades
            if hedge_type == "timestop" and winner_entry >= 0.85:
                trades.append(TimeStopTrade(
                    market=slug,
                    winner_entry=winner_entry,
                    loser_fill_at_stop=loser_fill,
                    pair_cost=pair_cost,
                    pnl=pnl,
                    time_stop_seconds=time_stop_seconds,
                    direction_correct=direction_correct,
                    winner_side=winner_side,
                ))

            in_position = False
            last_hedge_ts = hedge_fill_ts

    return trades


def main():
    print("=" * 80)
    print("HIGH-ENTRY TIME-STOP ANALYSIS")
    print("Turkey = High entry + bad time-stop exit = LOSS even with correct direction")
    print("=" * 80)
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

    print(f"Data: {len(valid_slugs)} markets, {len(spikes_only):,} spikes")
    print()

    # Analyze each time-stop setting
    for ts in [120, 180, 300]:
        print("=" * 80)
        print(f"TIME-STOP: {ts}s")
        print("=" * 80)

        trades = analyze_timestops(spikes_only, obs_df, ts)

        if not trades:
            print("  No high-entry time-stop trades found")
            continue

        # Bucket by entry price
        buckets = [
            (0.85, 0.88, "$0.85-$0.88"),
            (0.88, 0.90, "$0.88-$0.90"),
            (0.90, 0.92, "$0.90-$0.92"),
            (0.92, 0.95, "$0.92-$0.95"),
            (0.95, 1.00, "$0.95-$1.00"),
        ]

        print(f"\n{'Entry Bucket':<14} {'Count':>6} {'Losses':>7} {'Loss$':>10} {'AvgPnL':>8} {'Worst':>10}")
        print("-" * 65)

        total_trades = 0
        total_losses = 0
        total_loss_amount = 0

        for lo, hi, label in buckets:
            bucket = [t for t in trades if lo <= t.winner_entry < hi]
            if not bucket:
                continue

            losses = [t for t in bucket if t.pnl < 0]
            loss_amount = sum(t.pnl for t in losses)
            avg_pnl = sum(t.pnl for t in bucket) / len(bucket)
            worst = min(t.pnl for t in bucket) if bucket else 0

            total_trades += len(bucket)
            total_losses += len(losses)
            total_loss_amount += loss_amount

            print(f"{label:<14} {len(bucket):>6} {len(losses):>7} ${loss_amount:>9.2f} ${avg_pnl:>7.2f} ${worst:>9.2f}")

        print("-" * 65)
        print(f"{'TOTAL':<14} {total_trades:>6} {total_losses:>7} ${total_loss_amount:>9.2f}")

        # Show worst cases
        worst_trades = sorted(trades, key=lambda t: t.pnl)[:5]
        print(f"\nWORST 5 TRADES (TIME{ts}s):")
        for t in worst_trades:
            print(f"  Entry ${t.winner_entry:.2f} + Loser ${t.loser_fill_at_stop:.2f} = "
                  f"Pair ${t.pair_cost:.2f} → PnL ${t.pnl:.2f} "
                  f"({'✓' if t.direction_correct else '✗'} dir)")

        # High-entry (>$0.90) summary
        high_entry = [t for t in trades if t.winner_entry > 0.90]
        if high_entry:
            he_losses = [t for t in high_entry if t.pnl < 0]
            print(f"\nHIGH-ENTRY (>$0.90) SUMMARY:")
            print(f"  Total time-stop exits: {len(high_entry)}")
            print(f"  Losses: {len(he_losses)} ({len(he_losses)/len(high_entry)*100:.1f}%)")
            print(f"  Total loss: ${sum(t.pnl for t in he_losses):.2f}")

        print()


if __name__ == "__main__":
    main()
