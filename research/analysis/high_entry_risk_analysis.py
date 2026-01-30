#!/usr/bin/env python3
"""
HIGH-ENTRY TRADE RISK ANALYSIS

Investigates: What happens to trades where entry > $0.90?

Key questions:
1. Can we actually hedge these trades? (loser_bid >= $0.02 for $1 min order)
2. How many resolve WRONG? (false positives = turkey moments)
3. What's the PnL if we CAN'T hedge vs backtest assumption?

The backtest might be incorrectly assuming we can hedge at $0.01 when
in production that order would be REJECTED.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Tuple
import sys
import math
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

# Constants
TARGET_SHARES = 50
MIN_TIME = 60
MIN_CYCLE_GAP_MS = 1000
MIN_ORDER_DOLLARS = 1.00  # Polymarket minimum
MIN_LOSER_BID = MIN_ORDER_DOLLARS / TARGET_SHARES  # $0.02

# OU params
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
        print(f"[OU] Loaded: mu={_ou_params.mu:.4f}, sigma_stat={_ou_params.sigma_stat:.4f}")
    except Exception as e:
        print(f"[OU] Warning: {e}")
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


def detect_spikes_ou(btc_df: pd.DataFrame, lookback: int = 72) -> pd.DataFrame:
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)
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


def calc_loser_bid(winner_entry: float, spike_mag: float) -> float:
    """Calculate loser bid - THIS IS WHAT BACKTEST USES."""
    # FIX: Do NOT divide by 100 - spike_mag is already percentage (0.05 = 0.05%)
    expected_drop = DROP_MULTIPLIER * spike_mag + DROP_INTERCEPT
    expected_drop = max(0.02, min(0.20, expected_drop))
    max_loser = TARGET_PAIR_COST - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid))  # Floor at $0.01


@dataclass
class HighEntryTrade:
    market_slug: str
    winner_entry: float
    loser_bid_backtest: float  # What backtest assumes
    can_actually_hedge: bool   # Can we place $1 order?
    resolution: str
    direction_correct: bool
    backtest_pnl: float        # What backtest shows
    real_pnl_no_hedge: float   # What happens if we CAN'T hedge
    spike_mag: float
    winner_side: str


def analyze_market(
    spikes_df: pd.DataFrame,
    obs_df: pd.DataFrame,
    slug: str,
    resolution: str,
    time_stop_seconds: float = 120,
) -> List[HighEntryTrade]:
    """Analyze trades in a market, focusing on high-entry cases."""

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

    trades = []
    in_position = False
    last_hedge_ts = 0

    for _, spike_row in market_spikes.iterrows():
        spike_ts = spike_row['timestamp_ms']
        spike_dir = spike_row['spike_direction']
        spike_mag = spike_row['spike_magnitude']
        zscore = spike_row['zscore']

        # Z-zone filter
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

        # Velocity confirmation
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

        # Calculate loser bid (what backtest uses)
        loser_bid_backtest = calc_loser_bid(winner_entry, spike_mag)

        # Can we actually hedge? Need loser_bid >= $0.02 for $1 minimum
        can_hedge = loser_bid_backtest >= MIN_LOSER_BID

        in_position = True
        entry_ts = spike_ts

        # Simulate hedge (same as backtest)
        hedge_type = "resolution"
        loser_fill = 0.0
        hedge_fill_ts = market_end

        for j in range(obs_idx + 1, len(mdf)):
            scan_row = mdf.iloc[j]
            scan_ts = scan_row['timestamp_ms']

            elapsed_secs = (scan_ts - entry_ts) / 1000.0
            if elapsed_secs >= time_stop_seconds:
                if loser_side == "UP":
                    loser_fill = scan_row['up_ask']
                else:
                    loser_fill = scan_row['down_ask']
                hedge_type = "timestop"
                hedge_fill_ts = scan_ts
                break

            if loser_side == "UP":
                curr_loser_ask = scan_row['up_ask']
            else:
                curr_loser_ask = scan_row['down_ask']

            if curr_loser_ask <= loser_bid_backtest:
                loser_fill = loser_bid_backtest
                hedge_type = "passive"
                hedge_fill_ts = scan_ts
                break

        if hedge_type == "resolution":
            if resolution == winner_side:
                loser_fill = loser_bid_backtest
            else:
                loser_fill = 1.0

        # Calculate PnL - what backtest shows
        pair_cost = winner_entry + loser_fill
        if hedge_type == "resolution" and resolution != winner_side:
            backtest_pnl = -winner_entry * TARGET_SHARES
        else:
            backtest_pnl = (1.0 - pair_cost) * TARGET_SHARES

        # Calculate REAL PnL if we CAN'T hedge (naked exposure)
        direction_correct = (resolution == winner_side)
        if direction_correct:
            # Win: we get $1 per share
            real_pnl_no_hedge = (1.0 - winner_entry) * TARGET_SHARES
        else:
            # LOSE: we lose entire entry stake
            real_pnl_no_hedge = -winner_entry * TARGET_SHARES

        # Only track high-entry trades (entry > 0.85 to see the gradient)
        if winner_entry > 0.85:
            trades.append(HighEntryTrade(
                market_slug=slug,
                winner_entry=winner_entry,
                loser_bid_backtest=loser_bid_backtest,
                can_actually_hedge=can_hedge,
                resolution=resolution,
                direction_correct=direction_correct,
                backtest_pnl=backtest_pnl,
                real_pnl_no_hedge=real_pnl_no_hedge,
                spike_mag=spike_mag,
                winner_side=winner_side,
            ))

        in_position = False
        last_hedge_ts = hedge_fill_ts

    return trades


def load_data():
    print("Loading IS+OOS2 data...")

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

    print(f"  Markets: {len(valid_slugs)}, Spikes: {len(spikes_only):,}")
    return spikes_only, obs_df


def main():
    print("=" * 80)
    print("HIGH-ENTRY TRADE RISK ANALYSIS")
    print("=" * 80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    print(f"Polymarket minimum order: ${MIN_ORDER_DOLLARS:.2f}")
    print(f"At {TARGET_SHARES} shares, min loser_bid: ${MIN_LOSER_BID:.2f}")
    print()

    load_ou_params()
    spikes_df, obs_df = load_data()

    # Analyze all markets
    all_trades = []
    for slug in obs_df['market_slug'].unique():
        mdf = obs_df[obs_df['market_slug'] == slug]
        resolution = mdf['resolution'].iloc[0]
        trades = analyze_market(spikes_df, obs_df, slug, resolution)
        all_trades.extend(trades)

    print(f"\nTotal high-entry trades (entry > $0.85): {len(all_trades)}")
    print()

    # Bucket analysis
    buckets = [
        (0.85, 0.88, "$0.85-$0.88"),
        (0.88, 0.90, "$0.88-$0.90"),
        (0.90, 0.92, "$0.90-$0.92"),
        (0.92, 0.95, "$0.92-$0.95"),
        (0.95, 1.00, "$0.95-$1.00"),
    ]

    print("=" * 80)
    print("ANALYSIS BY ENTRY PRICE BUCKET")
    print("=" * 80)
    print()
    print(f"{'Bucket':<14} {'Count':>6} {'Can Hedge':>10} {'Dir Acc':>8} "
          f"{'BT PnL':>10} {'Real PnL':>10} {'FALSE POS':>10}")
    print("-" * 80)

    for lo, hi, label in buckets:
        bucket_trades = [t for t in all_trades if lo <= t.winner_entry < hi]
        if not bucket_trades:
            continue

        count = len(bucket_trades)
        can_hedge = sum(1 for t in bucket_trades if t.can_actually_hedge)
        correct = sum(1 for t in bucket_trades if t.direction_correct)
        false_positives = [t for t in bucket_trades if not t.direction_correct]

        bt_pnl = sum(t.backtest_pnl for t in bucket_trades)
        real_pnl = sum(t.real_pnl_no_hedge for t in bucket_trades)

        print(f"{label:<14} {count:>6} {can_hedge:>10} {correct/count:>7.1%} "
              f"${bt_pnl:>9.2f} ${real_pnl:>9.2f} {len(false_positives):>10}")

    # Focus on unhedgeable trades (entry > 0.90)
    print()
    print("=" * 80)
    print("UNHEDGEABLE TRADES (entry > $0.90)")
    print("=" * 80)

    unhedgeable = [t for t in all_trades if t.winner_entry > 0.90]
    print(f"\nTotal unhedgeable trades: {len(unhedgeable)}")

    if unhedgeable:
        correct = sum(1 for t in unhedgeable if t.direction_correct)
        wrong = [t for t in unhedgeable if not t.direction_correct]

        bt_pnl = sum(t.backtest_pnl for t in unhedgeable)
        real_pnl = sum(t.real_pnl_no_hedge for t in unhedgeable)

        print(f"Direction correct: {correct}/{len(unhedgeable)} ({correct/len(unhedgeable):.1%})")
        print(f"FALSE POSITIVES (WRONG): {len(wrong)}")
        print()
        print(f"Backtest assumes PnL: ${bt_pnl:.2f}")
        print(f"REAL PnL (no hedge):   ${real_pnl:.2f}")
        print(f"Difference:            ${real_pnl - bt_pnl:.2f}")

        if wrong:
            print()
            print("=" * 80)
            print("FALSE POSITIVES - TURKEY MOMENTS")
            print("=" * 80)
            print()
            print(f"{'Market':<50} {'Entry':>7} {'Side':>5} {'Loss':>10}")
            print("-" * 80)

            total_loss = 0
            for t in sorted(wrong, key=lambda x: x.real_pnl_no_hedge):
                loss = t.real_pnl_no_hedge
                total_loss += loss
                print(f"{t.market_slug[:50]:<50} ${t.winner_entry:>6.2f} {t.winner_side:>5} ${loss:>9.2f}")

            print("-" * 80)
            print(f"{'TOTAL FALSE POSITIVE LOSSES:':<63} ${total_loss:>9.2f}")

            # Calculate how many winning trades this wipes out
            avg_win = 1.50  # Typical profit per winning trade
            trades_wiped = abs(total_loss) / avg_win
            print(f"Equivalent to {trades_wiped:.0f} winning trades wiped out")

    # Summary
    print()
    print("=" * 80)
    print("KEY FINDINGS")
    print("=" * 80)

    hedgeable = [t for t in all_trades if t.winner_entry <= 0.90]
    if hedgeable:
        h_bt_pnl = sum(t.backtest_pnl for t in hedgeable)
        h_real_pnl = sum(t.real_pnl_no_hedge for t in hedgeable)
        print(f"\nHedgeable trades (≤$0.90): {len(hedgeable)}")
        print(f"  Backtest PnL: ${h_bt_pnl:.2f}")
        print(f"  These trades CAN be hedged - backtest is accurate")

    if unhedgeable:
        u_bt_pnl = sum(t.backtest_pnl for t in unhedgeable)
        u_real_pnl = sum(t.real_pnl_no_hedge for t in unhedgeable)
        print(f"\nUnhedgeable trades (>$0.90): {len(unhedgeable)}")
        print(f"  Backtest ASSUMES: ${u_bt_pnl:.2f}")
        print(f"  REALITY (no hedge): ${u_real_pnl:.2f}")
        print(f"  BACKTEST OVERESTIMATES BY: ${u_bt_pnl - u_real_pnl:.2f}")

    print()
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
