#!/usr/bin/env python3
"""Quick check: what are cheap side prices during spikes?"""

import pandas as pd
import numpy as np
from pathlib import Path

obs_path = Path("research/observer/PROTECTED_grid_obs_is_oos2_combined.csv")
btc_path = Path("research/binance_hf/btc_prices_is_oos2_5hz.csv")

print("Loading...")
obs_df = pd.read_csv(obs_path, low_memory=False)
btc_df = pd.read_csv(btc_path)

# Quick EWMA spike detection
btc = btc_df.copy().sort_values('timestamp_ms')
alpha = 1 - 0.5 ** (1.0 / 60)  # 1s halflife at 60Hz
btc['ewma'] = btc['price'].ewm(halflife=60).mean()
btc['dev'] = (btc['price'] - btc['ewma']) / btc['ewma'] * 100
btc['spike'] = btc['dev'].abs() > 0.02  # 2% threshold
btc['spike_dir'] = np.where(btc['dev'] > 0, 'UP', 'DOWN')

# Merge
btc_merge = btc[['timestamp_ms', 'spike', 'spike_dir']].copy()
obs = pd.merge_asof(
    obs_df.sort_values('timestamp_ms'),
    btc_merge.sort_values('timestamp_ms'),
    on='timestamp_ms',
    direction='nearest',
    tolerance=500
)

# During spikes, what's the cheap side price?
obs['expensive_side'] = np.where(obs['up_ask'] > obs['down_ask'], 'UP', 'DOWN')
obs['cheap_ask'] = np.where(obs['up_ask'] < obs['down_ask'], obs['up_ask'], obs['down_ask'])
obs['expensive_ask'] = np.where(obs['up_ask'] > obs['down_ask'], obs['up_ask'], obs['down_ask'])

spikes = obs[obs['spike'] == True]
print(f"\nSpike observations: {len(spikes):,}")

print("\nCheap side ask distribution during spikes:")
print(spikes['cheap_ask'].describe())

print("\nCheap ask percentiles:")
for p in [5, 10, 25, 50, 75, 90, 95]:
    val = spikes['cheap_ask'].quantile(p/100)
    print(f"  {p}th: ${val:.2f}")

print("\nExpensive ask percentiles:")
for p in [5, 10, 25, 50, 75, 90, 95]:
    val = spikes['expensive_ask'].quantile(p/100)
    print(f"  {p}th: ${val:.2f}")

# How many spikes have cheap_ask <= 0.30?
cheap_30 = (spikes['cheap_ask'] <= 0.30).mean() * 100
cheap_20 = (spikes['cheap_ask'] <= 0.20).mean() * 100
cheap_15 = (spikes['cheap_ask'] <= 0.15).mean() * 100

print(f"\nCheap ask <= $0.30: {cheap_30:.1f}%")
print(f"Cheap ask <= $0.20: {cheap_20:.1f}%")
print(f"Cheap ask <= $0.15: {cheap_15:.1f}%")
