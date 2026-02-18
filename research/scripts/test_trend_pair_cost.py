#!/usr/bin/env python3
"""
Test trend prediction and pair cost achievability.

Strategy:
1. Detect BTC spike (EWMA 1s + OU 1s)
2. Enter expensive side at best_bid (maker)
3. Buy cheap side at ask
4. Check if pair_cost < $1

Key questions:
1. When spike UP detected, does UP side stay expensive? (trend holds)
2. Can we get filled at best_bid? (price doesn't gap away)
3. What pair_cost is achievable?
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Tuple
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# OU params (combined 1s calibration)
OU_MU = -6.3978
OU_SIGMA = 1.7051

# EWMA params (1s = 1000ms halflife)
EWMA_HALFLIFE_MS = 1000

# OU threshold constants
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5


@dataclass
class SpikeEvent:
    """A detected spike event with outcome tracking."""
    timestamp_ms: int
    market_slug: str
    spike_direction: str  # UP or DOWN
    spike_magnitude: float
    velocity_bps: float
    interaction: float  # spike_magnitude × |velocity|
    z_score: float

    # Market state at detection
    up_bid: float
    up_ask: float
    down_bid: float
    down_ask: float
    expensive_side: str
    expensive_ask: float
    cheap_ask: float
    time_remaining: float

    # Entry simulation (maker at best_bid)
    entry_bid: float  # best_bid of expensive side

    # Outcomes (filled in later)
    trend_held_5s: Optional[bool] = None  # expensive side still expensive after 5s
    trend_held_10s: Optional[bool] = None
    trend_held_30s: Optional[bool] = None

    min_pair_cost_5s: Optional[float] = None  # min pair cost achievable in 5s
    min_pair_cost_10s: Optional[float] = None
    min_pair_cost_30s: Optional[float] = None

    filled_at_bid: Optional[bool] = None  # did price touch our bid?


def compute_ewma_spikes(btc_df: pd.DataFrame, halflife_ms: int = 1000) -> pd.DataFrame:
    """Compute EWMA-based spike detection at 1s intervals with velocity."""
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)
    df = df.drop_duplicates(subset='timestamp_ms', keep='first')

    # Resample to 1s
    df['bucket'] = (df['timestamp_ms'] // 1000).astype(int)
    df = df.groupby('bucket').agg({
        'timestamp_ms': 'last',
        'price': 'last'
    }).reset_index(drop=True)

    # EWMA price
    alpha = 1 - 0.5 ** (1.0 / (halflife_ms / 1000))  # halflife in seconds at 1s data
    df['ewma_price'] = df['price'].ewm(alpha=alpha, adjust=False).mean()

    # Deviation
    df['deviation_pct'] = (df['price'] - df['ewma_price']) / df['ewma_price'] * 100
    df['spike_magnitude'] = df['deviation_pct'].abs()

    # Velocity (bps per second over 5s lookback)
    lookback = 5
    df['price_5s_ago'] = df['price'].shift(lookback)
    df['velocity_bps'] = (df['price'] - df['price_5s_ago']) / df['price_5s_ago'] * 10000 / lookback
    df['velocity_bps'] = df['velocity_bps'].fillna(0)

    # Raw interaction: spike_magnitude × |velocity|
    df['interaction'] = df['spike_magnitude'] * df['velocity_bps'].abs()

    # Compute volatility and z-score (EWMA variance)
    returns = df['price'].pct_change() * 100
    var_ewma = (returns ** 2).ewm(halflife=300).mean()  # 5 min halflife
    vol = np.sqrt(var_ewma.values)
    vol = np.maximum(vol, 1e-6)

    log_vol = np.log(vol)
    df['z_score'] = (log_vol - OU_MU) / OU_SIGMA

    # Adaptive threshold
    z_clamped = np.clip(df['z_score'].values * OU_SIGMOID_STEEPNESS, -10, 10)
    sigmoid = 1.0 / (1.0 + np.exp(-z_clamped))
    multiplier = OU_K_LOW + (OU_K_HIGH - OU_K_LOW) * sigmoid
    df['threshold'] = np.clip(OU_BASE_THRESHOLD * multiplier, 0.015, 0.10)

    # Spike detection (use lower threshold to get more signals, filter by interaction later)
    df['spike_detected'] = df['spike_magnitude'] >= df['threshold'] * 0.5  # More permissive
    df['spike_direction'] = np.where(df['deviation_pct'] > 0, 'UP', 'DOWN')
    df.loc[~df['spike_detected'], 'spike_direction'] = None

    return df


def analyze_spike_outcomes(obs_df: pd.DataFrame, btc_spikes: pd.DataFrame) -> List[SpikeEvent]:
    """Analyze each spike event for trend holding and pair cost."""

    events = []
    cooldown_ms = 30000  # 30s cooldown per market+direction for actionable signals
    last_spike_ts = {}

    # Get spike timestamps
    spike_rows = btc_spikes[btc_spikes['spike_detected']].copy()
    print(f"  Raw spikes: {len(spike_rows)}")

    for _, spike in spike_rows.iterrows():
        ts = spike['timestamp_ms']
        direction = spike['spike_direction']
        velocity = spike['velocity_bps']
        interaction = spike['interaction']

        # Find matching observer row
        obs_match = obs_df[
            (obs_df['timestamp_ms'] >= ts - 500) &
            (obs_df['timestamp_ms'] <= ts + 500)
        ]
        if obs_match.empty:
            continue

        obs = obs_match.iloc[0]
        market_slug = obs['market_slug']

        # Cooldown per market + direction (dedup for actionable signals)
        key = (market_slug, direction)
        if key in last_spike_ts and (ts - last_spike_ts[key]) < cooldown_ms:
            continue
        last_spike_ts[key] = ts

        up_bid = obs.get('up_bid', 0.5)
        up_ask = obs.get('up_ask', 0.5)
        down_bid = obs.get('down_bid', 0.5)
        down_ask = obs.get('down_ask', 0.5)
        time_remaining = obs.get('time_remaining_secs', 450)

        # Skip if too close to expiry
        if time_remaining < 60:
            continue

        # Determine expensive side
        if up_ask > down_ask:
            expensive_side = 'UP'
            expensive_ask = up_ask
            expensive_bid = up_bid
            cheap_ask = down_ask
        else:
            expensive_side = 'DOWN'
            expensive_ask = down_ask
            expensive_bid = down_bid
            cheap_ask = up_ask

        # Skip if not expensive enough
        if expensive_ask < 0.60:
            continue

        event = SpikeEvent(
            timestamp_ms=ts,
            market_slug=market_slug,
            spike_direction=direction,
            spike_magnitude=spike['spike_magnitude'],
            velocity_bps=velocity,
            interaction=interaction,
            z_score=spike['z_score'],
            up_bid=up_bid,
            up_ask=up_ask,
            down_bid=down_bid,
            down_ask=down_ask,
            expensive_side=expensive_side,
            expensive_ask=expensive_ask,
            cheap_ask=cheap_ask,
            time_remaining=time_remaining,
            entry_bid=expensive_bid,  # We'd enter at best_bid
        )

        # Find future observations for this market
        market_slug = obs['market_slug']
        future_obs = obs_df[
            (obs_df['market_slug'] == market_slug) &
            (obs_df['timestamp_ms'] > ts) &
            (obs_df['timestamp_ms'] <= ts + 30000)  # Next 30s
        ].sort_values('timestamp_ms')

        if len(future_obs) < 5:
            continue

        # Track outcomes at different time horizons
        for horizon_s, attr_trend, attr_cost in [
            (5, 'trend_held_5s', 'min_pair_cost_5s'),
            (10, 'trend_held_10s', 'min_pair_cost_10s'),
            (30, 'trend_held_30s', 'min_pair_cost_30s'),
        ]:
            horizon_obs = future_obs[
                future_obs['timestamp_ms'] <= ts + horizon_s * 1000
            ]
            if horizon_obs.empty:
                continue

            # Check if expensive side stayed expensive
            if expensive_side == 'UP':
                future_exp_asks = horizon_obs['up_ask'].values
                future_cheap_asks = horizon_obs['down_ask'].values
            else:
                future_exp_asks = horizon_obs['down_ask'].values
                future_cheap_asks = horizon_obs['up_ask'].values

            # Trend held = expensive side is still more expensive at end
            end_exp_ask = future_exp_asks[-1]
            end_cheap_ask = future_cheap_asks[-1]
            trend_held = end_exp_ask > end_cheap_ask
            setattr(event, attr_trend, trend_held)

            # Min pair cost = entry_bid + min(cheap_ask) over window
            # If we entered at best_bid of expensive side
            min_cheap = future_cheap_asks.min()
            min_pair_cost = event.entry_bid + min_cheap
            setattr(event, attr_cost, min_pair_cost)

        # Did price ever touch our bid? (could we get filled?)
        if expensive_side == 'UP':
            future_bids = future_obs['up_bid'].values
        else:
            future_bids = future_obs['down_bid'].values

        # Check if ask ever dropped to our bid level (we'd get filled)
        if expensive_side == 'UP':
            future_asks = future_obs['up_ask'].values
        else:
            future_asks = future_obs['down_ask'].values

        # Filled if ask dropped to or below our entry bid
        event.filled_at_bid = any(future_asks <= event.entry_bid)

        events.append(event)

    print(f"  Deduped signals (30s cooldown per market+dir): {len(events)}")
    return events


def print_results(events: List[SpikeEvent], label: str):
    """Print analysis results."""
    print(f"\n{'='*60}")
    print(f"RESULTS: {label}")
    print(f"{'='*60}")
    print(f"Total spike events: {len(events)}")

    if not events:
        return

    # Interaction stats
    interactions = [e.interaction for e in events]
    print(f"\n🔗 INTERACTION (spike × |velocity|):")
    print(f"  Mean: {np.mean(interactions):.4f}, Median: {np.median(interactions):.4f}")
    print(f"  P75: {np.percentile(interactions, 75):.4f}, P90: {np.percentile(interactions, 90):.4f}")

    # Trend holding rates
    print(f"\n📈 TREND HOLDING (expensive side stays expensive):")
    for horizon, attr in [(5, 'trend_held_5s'), (10, 'trend_held_10s'), (30, 'trend_held_30s')]:
        valid = [e for e in events if getattr(e, attr) is not None]
        if valid:
            held = sum(1 for e in valid if getattr(e, attr))
            pct = held / len(valid) * 100
            print(f"  {horizon}s: {pct:.1f}% ({held}/{len(valid)})")

    # Pair cost distribution
    print(f"\n💰 PAIR COST (entry_bid + min_cheap_ask):")
    for horizon, attr in [(5, 'min_pair_cost_5s'), (10, 'min_pair_cost_10s'), (30, 'min_pair_cost_30s')]:
        costs = [getattr(e, attr) for e in events if getattr(e, attr) is not None]
        if costs:
            costs = np.array(costs)
            below_1 = np.mean(costs < 1.0) * 100
            below_95 = np.mean(costs < 0.95) * 100
            below_90 = np.mean(costs < 0.90) * 100
            print(f"  {horizon}s window:")
            print(f"    Mean: ${np.mean(costs):.3f}, Median: ${np.median(costs):.3f}")
            print(f"    < $1.00: {below_1:.1f}%, < $0.95: {below_95:.1f}%, < $0.90: {below_90:.1f}%")

    # Fill rate
    filled = [e for e in events if e.filled_at_bid is not None]
    if filled:
        fill_rate = sum(1 for e in filled if e.filled_at_bid) / len(filled) * 100
        print(f"\n🎯 FILL RATE (ask touched bid): {fill_rate:.1f}%")

    # By expensive_ask threshold
    print(f"\n📊 BY EXPENSIVE ASK THRESHOLD:")
    for threshold in [0.60, 0.70, 0.80, 0.90]:
        subset = [e for e in events if e.expensive_ask >= threshold]
        if len(subset) < 10:
            continue

        # Trend at 10s
        valid = [e for e in subset if e.trend_held_10s is not None]
        trend_pct = sum(1 for e in valid if e.trend_held_10s) / len(valid) * 100 if valid else 0

        # Pair cost at 10s
        costs = [e.min_pair_cost_10s for e in subset if e.min_pair_cost_10s is not None]
        below_1 = np.mean(np.array(costs) < 1.0) * 100 if costs else 0

        print(f"  exp_ask >= ${threshold}: n={len(subset)}, trend_10s={trend_pct:.1f}%, pair<$1={below_1:.1f}%")

    # BY INTERACTION PERCENTILE (key test for spike × |velocity|)
    print(f"\n⚡ BY INTERACTION (spike × |velocity|):")
    if len(events) >= 20:
        p50 = np.percentile(interactions, 50)
        p75 = np.percentile(interactions, 75)
        p90 = np.percentile(interactions, 90)

        for name, threshold in [("above median", p50), ("above P75", p75), ("above P90", p90)]:
            subset = [e for e in events if e.interaction >= threshold]
            if len(subset) < 5:
                continue

            # Trend at 10s
            valid = [e for e in subset if e.trend_held_10s is not None]
            trend_pct = sum(1 for e in valid if e.trend_held_10s) / len(valid) * 100 if valid else 0

            # Pair cost at 10s
            costs = [e.min_pair_cost_10s for e in subset if e.min_pair_cost_10s is not None]
            below_1 = np.mean(np.array(costs) < 1.0) * 100 if costs else 0
            mean_cost = np.mean(costs) if costs else 0

            # Fill rate
            fills = [e for e in subset if e.filled_at_bid is not None]
            fill_pct = sum(1 for e in fills if e.filled_at_bid) / len(fills) * 100 if fills else 0

            print(f"  {name}: n={len(subset)}, trend={trend_pct:.1f}%, pair<$1={below_1:.1f}%, fill={fill_pct:.1f}%, avg_cost=${mean_cost:.3f}")


def main():
    print("="*60)
    print("TREND PREDICTION & PAIR COST TEST")
    print("EWMA=1s, OU=1s (combined calibration)")
    print("="*60)

    # Load data
    obs_path = Path("research/observer/PROTECTED_grid_obs_is_oos2_combined.csv")
    btc_path = Path("research/binance_hf/btc_prices_is_oos2_5hz.csv")

    print(f"\nLoading data...")
    obs_df = pd.read_csv(obs_path, low_memory=False)
    btc_df = pd.read_csv(btc_path)

    print(f"  Observer: {len(obs_df):,} rows")
    print(f"  BTC: {len(btc_df):,} rows")

    # Compute spikes
    print(f"\nComputing EWMA spikes (halflife={EWMA_HALFLIFE_MS}ms)...")
    btc_spikes = compute_ewma_spikes(btc_df, EWMA_HALFLIFE_MS)
    n_spikes = btc_spikes['spike_detected'].sum()
    print(f"  Found {n_spikes} spikes")

    # Z-score distribution
    z_arr = btc_spikes['z_score'].values
    print(f"  Z-score: mean={np.mean(z_arr):.2f}, std={np.std(z_arr):.2f}")

    # Analyze outcomes
    print(f"\nAnalyzing spike outcomes...")
    events = analyze_spike_outcomes(obs_df, btc_spikes)

    print_results(events, "IS+OOS2")

    # Also test on OOS9 if available
    oos9_obs = Path("research/observer/grid_obs_oos9.csv")
    oos9_btc = Path("research/binance_hf/btc_prices_oos9.csv")

    if oos9_obs.exists() and oos9_btc.exists():
        print(f"\n\nLoading OOS9...")
        obs_df2 = pd.read_csv(oos9_obs, low_memory=False)
        btc_df2 = pd.read_csv(oos9_btc)

        print(f"  Observer: {len(obs_df2):,} rows")
        print(f"  BTC: {len(btc_df2):,} rows")

        btc_spikes2 = compute_ewma_spikes(btc_df2, EWMA_HALFLIFE_MS)
        n_spikes2 = btc_spikes2['spike_detected'].sum()
        print(f"  Found {n_spikes2} spikes")

        events2 = analyze_spike_outcomes(obs_df2, btc_spikes2)
        print_results(events2, "OOS9")


if __name__ == "__main__":
    main()
