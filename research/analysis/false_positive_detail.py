#!/usr/bin/env python3
"""
Detailed analysis of FALSE POSITIVES in high-entry range.
Find every case where direction was WRONG.
"""

import pandas as pd
import numpy as np
from pathlib import Path
import sys
import math

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


def main():
    print("=" * 80)
    print("FALSE POSITIVE DETAIL ANALYSIS")
    print("=" * 80)

    load_ou_params()

    # Load data
    btc_dir = Path("research/binance_hf")
    btc_dfs = []
    for f in sorted(btc_dir.glob("btc_prices_*.csv")):
        if "recovered" not in f.name:
            btc_dfs.append(pd.read_csv(f))
    btc_df = pd.concat(btc_dfs, ignore_index=True)

    obs_dir = Path("research/observer")
    obs_dfs = []
    for f in sorted(obs_dir.glob("grid_obs_*.csv")):
        if "combined" not in f.name and "oos5" not in f.name and "recovered" not in f.name:
            obs_dfs.append(pd.read_csv(f, on_bad_lines='skip', low_memory=False))
    obs_df = pd.concat(obs_dfs, ignore_index=True)

    btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])

    spikes_df = detect_spikes_ou(btc_df)

    res_df = pd.read_csv("research/observer/market_resolutions_verified.csv")
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Overlap
    btc_start, btc_end = spikes_df['timestamp_ms'].min(), spikes_df['timestamp_ms'].max()
    obs_start, obs_end = obs_df['timestamp_ms'].min(), obs_df['timestamp_ms'].max()
    overlap_start, overlap_end = max(btc_start, obs_start), min(btc_end, obs_end)

    spikes_df = spikes_df[(spikes_df['timestamp_ms'] >= overlap_start) &
                          (spikes_df['timestamp_ms'] <= overlap_end)]
    obs_df = obs_df[(obs_df['timestamp_ms'] >= overlap_start) &
                    (obs_df['timestamp_ms'] <= overlap_end)].copy()

    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    valid_slugs = []
    for slug, mdf in obs_df.groupby('market_slug'):
        max_time = mdf['time_remaining_secs'].max()
        min_time = mdf['time_remaining_secs'].min()
        if (max_time - min_time) >= 300 and max_time >= 840:
            valid_slugs.append(slug)

    obs_df = obs_df[obs_df['market_slug'].isin(valid_slugs)]
    spikes_only = spikes_df[spikes_df['spike_detected'] == True].copy()

    print(f"\nAnalyzing {len(valid_slugs)} markets...")
    print()

    # Find ALL false positives in high-entry range
    false_positives = []

    for slug in obs_df['market_slug'].unique():
        mdf = obs_df[obs_df['market_slug'] == slug].copy()
        mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)
        resolution = mdf['resolution'].iloc[0]

        market_start = mdf['timestamp_ms'].min()
        market_end = mdf['timestamp_ms'].max()

        market_spikes = spikes_only[
            (spikes_only['timestamp_ms'] >= market_start) &
            (spikes_only['timestamp_ms'] <= market_end)
        ]

        in_position = False
        last_hedge_ts = 0

        for _, spike_row in market_spikes.iterrows():
            spike_ts = spike_row['timestamp_ms']
            spike_dir = spike_row['spike_direction']
            zscore = spike_row['zscore']
            spike_mag = spike_row['spike_magnitude']

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

            if winner_side == "UP":
                winner_entry = obs_row['up_ask']
            else:
                winner_entry = obs_row['down_ask']

            # Track this trade
            in_position = True

            # Simulate time-stop at 120s
            hedge_fill_ts = market_end
            for j in range(obs_idx + 1, len(mdf)):
                scan_row = mdf.iloc[j]
                scan_ts = scan_row['timestamp_ms']
                if (scan_ts - spike_ts) / 1000.0 >= 120:
                    hedge_fill_ts = scan_ts
                    break

            in_position = False
            last_hedge_ts = hedge_fill_ts

            direction_correct = (resolution == winner_side)

            # Check if this is a false positive in high-entry range
            if winner_entry >= 0.85 and not direction_correct:
                loss = -winner_entry * TARGET_SHARES
                false_positives.append({
                    'market': slug,
                    'entry': winner_entry,
                    'our_side': winner_side,
                    'resolution': resolution,
                    'loss_naked': loss,
                    'time_remaining': time_rem,
                    'zscore': zscore,
                    'velocity_bps': velocity_bps,
                    'spike_mag': spike_mag,
                })

    print("=" * 80)
    print("ALL FALSE POSITIVES (entry >= $0.85, wrong direction)")
    print("=" * 80)
    print()

    if not false_positives:
        print("NO FALSE POSITIVES FOUND!")
    else:
        print(f"Found {len(false_positives)} false positives:\n")

        for i, fp in enumerate(sorted(false_positives, key=lambda x: -x['entry']), 1):
            print(f"FALSE POSITIVE #{i}")
            print(f"  Market:      {fp['market']}")
            print(f"  Entry:       ${fp['entry']:.4f}")
            print(f"  We bet:      {fp['our_side']}")
            print(f"  Resolved:    {fp['resolution']}")
            print(f"  LOSS:        ${fp['loss_naked']:.2f} (if no hedge)")
            print(f"  Time rem:    {fp['time_remaining']:.0f}s")
            print(f"  Z-score:     {fp['zscore']:.3f}")
            print(f"  Velocity:    {fp['velocity_bps']:.2f} bps")
            print(f"  Spike mag:   {fp['spike_mag']:.4f}%")
            print()

        total_loss = sum(fp['loss_naked'] for fp in false_positives)
        print(f"TOTAL POTENTIAL LOSS: ${total_loss:.2f}")

        # Bucket breakdown
        print()
        print("By entry bucket:")
        for lo, hi, label in [(0.85, 0.88, "0.85-0.88"), (0.88, 0.90, "0.88-0.90"),
                               (0.90, 0.92, "0.90-0.92"), (0.92, 0.95, "0.92-0.95"),
                               (0.95, 1.00, "0.95-1.00")]:
            bucket_fp = [fp for fp in false_positives if lo <= fp['entry'] < hi]
            if bucket_fp:
                print(f"  {label}: {len(bucket_fp)} false positives, "
                      f"total loss ${sum(f['loss_naked'] for f in bucket_fp):.2f}")


if __name__ == "__main__":
    main()
