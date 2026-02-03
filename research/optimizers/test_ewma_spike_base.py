#!/usr/bin/env python3
"""
EWMA Spike Base Test - Compare Fixed Lookback vs EWMA Reference

Hypothesis:
- Fixed 72-tick lookback generates multiple spikes from ONE price move
- EWMA reference adapts after spike, reducing redundant signals
- Should get same $/hr with fewer trades (better capital efficiency)

Test Matrix:
- FIXED: Current method (72-tick lookback)
- EWMA_200: EWMA with 200ms half-life (α ≈ 0.17 at 60Hz)
- EWMA_300: EWMA with 300ms half-life (α ≈ 0.12 at 60Hz)
- EWMA_500: EWMA with 500ms half-life (α ≈ 0.07 at 60Hz)

All use:
- OU adaptive THRESHOLD (proven +30% vs EWMA threshold)
- Same TS30_OLD config (best performer from 2x2 matrix)
- OOS8 dataset (most trades, best for comparison)
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict
from tqdm import tqdm
import math
import sys
import time

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.core import (
    polymarket_taker_fee,
    calculate_pnl_with_fees,
    velocity_confirms_spike,
    should_take_spike_enhanced,
    compute_enhanced_score,
)

# =============================================================================
# FIXED PARAMETERS (same as TS30_OLD winner config)
# =============================================================================
TARGET_SHARES = 50
TIME_STOP_SECONDS = 30.0
MIN_TIME = 90.0  # time_stop + 60s buffer
DROP_MULTIPLIER = 0.50
DROP_INTERCEPT = 0.08
TARGET_PAIR_COST = 0.99
HIGH_ENTRY_THRESHOLD = 0.90
MIN_CYCLE_GAP_MS = 50
MIN_RUNTIME_SECS = 300
SPIKE_LOOKBACK = 72  # For FIXED method

# OU threshold params
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10

# Signal thresholds
VELOCITY_CONFIRM_THRESHOLD = 0.10
ENHANCED_SCORE_THRESHOLD = 0.40

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
        print(f"[OU] Warning: {e} - using fixed threshold 0.02")
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
# SPIKE DETECTION METHODS
# =============================================================================

def precompute_spikes_fixed(btc_df: pd.DataFrame, lookback: int = SPIKE_LOOKBACK) -> pd.DataFrame:
    """FIXED: Current method - compare to price N ticks ago."""
    print(f"    [FIXED] Using {lookback}-tick lookback (current method)")
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    # Fixed lookback comparison
    df['price_prev'] = df['price'].shift(lookback)
    df['change_pct'] = (df['price'] - df['price_prev']) / df['price_prev'] * 100
    df['spike_magnitude'] = df['change_pct'].abs()

    # OU adaptive threshold (same for all methods)
    returns = df['price'].pct_change() * 100
    ewma_halflife = 300
    alpha = 1 - 0.5 ** (1.0 / ewma_halflife)

    variance = returns.iloc[:60].var() if len(returns) > 60 else 0.01
    thresholds = []

    for i, r in enumerate(returns):
        if pd.isna(r):
            thresholds.append(OU_BASE_THRESHOLD)
            continue
        variance = alpha * (r ** 2) + (1 - alpha) * variance
        vol = max(np.sqrt(variance), 1e-6)
        threshold = compute_ou_threshold(vol)
        thresholds.append(threshold)

    df['threshold'] = thresholds
    df['spike_detected'] = df['spike_magnitude'] >= df['threshold']
    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['change_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['change_pct'] < 0), 'spike_direction'] = 'DOWN'

    spike_count = df['spike_detected'].sum()
    print(f"    [FIXED] Found {spike_count:,} spikes")
    return df


def precompute_spikes_ewma(btc_df: pd.DataFrame, halflife_ms: int) -> pd.DataFrame:
    """EWMA: Compare to exponentially weighted moving average of price."""
    # At 60Hz, each tick is ~16.67ms
    # halflife_ticks = halflife_ms / 16.67
    halflife_ticks = halflife_ms / 16.67
    alpha = 1 - 0.5 ** (1.0 / halflife_ticks)

    print(f"    [EWMA_{halflife_ms}] Half-life={halflife_ms}ms, α={alpha:.4f}")

    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    # Compute EWMA of price
    prices = df['price'].values
    ewma_prices = np.zeros(len(prices))
    ewma_prices[0] = prices[0]

    for i in range(1, len(prices)):
        ewma_prices[i] = alpha * prices[i] + (1 - alpha) * ewma_prices[i-1]

    df['ewma_price'] = ewma_prices

    # Spike = deviation from EWMA
    df['change_pct'] = (df['price'] - df['ewma_price']) / df['ewma_price'] * 100
    df['spike_magnitude'] = df['change_pct'].abs()

    # OU adaptive threshold (same as FIXED)
    returns = df['price'].pct_change() * 100
    vol_halflife = 300
    vol_alpha = 1 - 0.5 ** (1.0 / vol_halflife)

    variance = returns.iloc[:60].var() if len(returns) > 60 else 0.01
    thresholds = []

    for i, r in enumerate(returns):
        if pd.isna(r):
            thresholds.append(OU_BASE_THRESHOLD)
            continue
        variance = vol_alpha * (r ** 2) + (1 - vol_alpha) * variance
        vol = max(np.sqrt(variance), 1e-6)
        threshold = compute_ou_threshold(vol)
        thresholds.append(threshold)

    df['threshold'] = thresholds
    df['spike_detected'] = df['spike_magnitude'] >= df['threshold']
    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['change_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['change_pct'] < 0), 'spike_direction'] = 'DOWN'

    spike_count = df['spike_detected'].sum()
    print(f"    [EWMA_{halflife_ms}] Found {spike_count:,} spikes")
    return df


# =============================================================================
# TRADE RESULT
# =============================================================================

@dataclass
class TradeResult:
    market_slug: str
    cycle_num: int
    entry_time_remaining: float
    signal_score: float
    winner_side: str
    winner_fill_price: float
    loser_fill_price: float
    hedge_type: str
    pair_cost: float
    pnl_gross: float
    pnl_net: float
    entry_fee: float
    exit_fee: float
    correct_direction: bool
    spike_magnitude: float
    dataset: str
    method: str  # FIXED, EWMA_200, EWMA_300, EWMA_500


def calculate_loser_bid(winner_entry: float, spike_magnitude: float) -> float:
    """Calculate loser bid with TS30_OLD formula."""
    expected_drop = DROP_MULTIPLIER * spike_magnitude + DROP_INTERCEPT
    max_loser = TARGET_PAIR_COST - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid))


# =============================================================================
# SIMULATION (same as test_short_term.py single-cycle)
# =============================================================================

def simulate_market(btc_spikes: pd.DataFrame, obs_df: pd.DataFrame,
                    slug: str, resolution: str, method_name: str) -> List[TradeResult]:
    """Simulate trading on a single market."""
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    market_spikes = btc_spikes[
        (btc_spikes['timestamp_ms'] >= market_start) &
        (btc_spikes['timestamp_ms'] <= market_end) &
        (btc_spikes['spike_detected'] == True)
    ].copy()

    if len(market_spikes) == 0:
        return []

    trades = []
    cycle_num = 0
    last_hedge_ts = 0
    in_position = False
    position_data = None
    time_stop_ms = TIME_STOP_SECONDS * 1000

    spike_idx = 0
    obs_idx = 0

    while spike_idx < len(market_spikes) or in_position:
        # In position - check for hedge
        if in_position and position_data is not None:
            entry_ts = position_data['entry_ts']

            while obs_idx < len(mdf):
                obs_row = mdf.iloc[obs_idx]
                obs_ts = obs_row['timestamp_ms']

                if obs_ts < entry_ts:
                    obs_idx += 1
                    continue

                loser_side = position_data['loser_side']
                loser_target = position_data['loser_target']
                winner_entry = position_data['winner_entry']
                spike_mag = position_data['spike_magnitude']
                score = position_data['score']

                if loser_side == "UP":
                    loser_ask = obs_row['up_ask']
                else:
                    loser_ask = obs_row['down_ask']

                # Passive fill
                if pd.notna(loser_ask) and loser_ask <= loser_target:
                    pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                        winner_entry, loser_target, TARGET_SHARES,
                        is_taker_entry=True, is_taker_exit=False
                    )
                    trades.append(TradeResult(
                        market_slug=slug, cycle_num=cycle_num,
                        entry_time_remaining=position_data['entry_time_rem'],
                        signal_score=score, winner_side=position_data['winner_side'],
                        winner_fill_price=winner_entry, loser_fill_price=loser_target,
                        hedge_type="passive", pair_cost=winner_entry + loser_target,
                        pnl_gross=pnl_gross, pnl_net=pnl_net,
                        entry_fee=entry_fee, exit_fee=exit_fee,
                        correct_direction=(resolution == position_data['winner_side']),
                        spike_magnitude=spike_mag, dataset="OOS8", method=method_name
                    ))
                    in_position = False
                    position_data = None
                    last_hedge_ts = obs_ts
                    obs_idx += 1
                    break

                # Time-stop
                elapsed_ms = obs_ts - entry_ts
                if time_stop_ms > 0 and elapsed_ms >= time_stop_ms:
                    winner_side_current = position_data['winner_side']
                    if winner_side_current == "UP":
                        winner_bid_current = obs_row['up_bid']
                    else:
                        winner_bid_current = obs_row['down_bid']

                    in_profit = pd.notna(winner_bid_current) and winner_bid_current >= winner_entry

                    if not in_profit:
                        loser_fill = loser_ask if pd.notna(loser_ask) else loser_target * 1.05
                        pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                            winner_entry, loser_fill, TARGET_SHARES,
                            is_taker_entry=True, is_taker_exit=True
                        )
                        trades.append(TradeResult(
                            market_slug=slug, cycle_num=cycle_num,
                            entry_time_remaining=position_data['entry_time_rem'],
                            signal_score=score, winner_side=position_data['winner_side'],
                            winner_fill_price=winner_entry, loser_fill_price=loser_fill,
                            hedge_type="time_stop", pair_cost=winner_entry + loser_fill,
                            pnl_gross=pnl_gross, pnl_net=pnl_net,
                            entry_fee=entry_fee, exit_fee=exit_fee,
                            correct_direction=(resolution == position_data['winner_side']),
                            spike_magnitude=spike_mag, dataset="OOS8", method=method_name
                        ))
                        in_position = False
                        position_data = None
                        last_hedge_ts = obs_ts
                        obs_idx += 1
                        break

                obs_idx += 1

            # Resolution
            if in_position and obs_idx >= len(mdf):
                winner_side = position_data['winner_side']
                winner_entry = position_data['winner_entry']
                entry_fee = polymarket_taker_fee(winner_entry) * winner_entry * TARGET_SHARES

                if resolution == winner_side:
                    pnl_gross = (1.0 - winner_entry) * TARGET_SHARES
                    loser_fill = 0.0
                else:
                    pnl_gross = (0.0 - winner_entry) * TARGET_SHARES
                    loser_fill = 1.0

                pnl_net = pnl_gross - entry_fee
                trades.append(TradeResult(
                    market_slug=slug, cycle_num=cycle_num,
                    entry_time_remaining=position_data['entry_time_rem'],
                    signal_score=position_data['score'], winner_side=winner_side,
                    winner_fill_price=winner_entry, loser_fill_price=loser_fill,
                    hedge_type="resolution", pair_cost=winner_entry + loser_fill,
                    pnl_gross=pnl_gross, pnl_net=pnl_net,
                    entry_fee=entry_fee, exit_fee=0.0,
                    correct_direction=(resolution == winner_side),
                    spike_magnitude=position_data['spike_magnitude'],
                    dataset="OOS8", method=method_name
                ))
                break

            continue

        # Not in position - check next spike
        if spike_idx >= len(market_spikes):
            break

        spike_row = market_spikes.iloc[spike_idx]
        spike_ts = spike_row['timestamp_ms']
        spike_dir = spike_row['spike_direction']
        spike_mag = spike_row['spike_magnitude']

        # Cycle gap
        if (spike_ts - last_hedge_ts) < MIN_CYCLE_GAP_MS:
            spike_idx += 1
            continue

        # Find observer row
        while obs_idx < len(mdf) - 1 and mdf.iloc[obs_idx + 1]['timestamp_ms'] <= spike_ts:
            obs_idx += 1

        if obs_idx >= len(mdf):
            break

        obs_row = mdf.iloc[obs_idx]
        time_rem = obs_row['time_remaining_secs']
        velocity_bps = obs_row.get('velocity_bps', 0.0) or 0.0

        # Entry filters
        if time_rem < MIN_TIME:
            spike_idx += 1
            continue

        if not velocity_confirms_spike(spike_dir, velocity_bps):
            spike_idx += 1
            continue

        score = compute_enhanced_score(spike_mag, velocity_bps, spike_dir, time_rem)
        if score < ENHANCED_SCORE_THRESHOLD:
            spike_idx += 1
            continue

        # Get prices
        winner_side = spike_dir
        if winner_side == "UP":
            winner_ask = obs_row['up_ask']
            obi_winner = obs_row.get('up_imbalance', None)
        else:
            winner_ask = obs_row['down_ask']
            obi_winner = obs_row.get('down_imbalance', None)

        if pd.isna(winner_ask) or winner_ask >= HIGH_ENTRY_THRESHOLD:
            spike_idx += 1
            continue

        # OBI filter
        if obi_winner is not None and not np.isnan(obi_winner):
            loser_bid = obs_row.get('down_bid' if winner_side == "UP" else 'up_bid', None)
            loser_ask_val = obs_row.get('down_ask' if winner_side == "UP" else 'up_ask', None)
            loser_spread = 0.05
            if pd.notna(loser_bid) and pd.notna(loser_ask_val):
                loser_spread = loser_ask_val - loser_bid

            should_take, _ = should_take_spike_enhanced(
                spike_direction=spike_dir, obi_winner=obi_winner,
                loser_spread=loser_spread, time_remaining=time_rem,
                winner_ask_depth=None
            )
            if not should_take:
                spike_idx += 1
                continue

        # ENTRY
        cycle_num += 1
        loser_side = "DOWN" if winner_side == "UP" else "UP"
        winner_entry = winner_ask
        loser_target = calculate_loser_bid(winner_entry, spike_mag)

        in_position = True
        position_data = {
            'winner_side': winner_side,
            'loser_side': loser_side,
            'winner_entry': winner_entry,
            'loser_target': loser_target,
            'entry_ts': spike_ts,
            'entry_time_rem': time_rem,
            'spike_magnitude': spike_mag,
            'score': score,
        }

        spike_idx += 1

    return trades


# =============================================================================
# MAIN TEST
# =============================================================================

def load_oos8_data():
    """Load OOS8 dataset."""
    base_dir = Path("/Users/rananjaybika/polymarket-amm-bot")

    print("Loading OOS8 data...")

    # BTC prices
    btc_path = base_dir / "research/binance_hf/btc_prices_20260131_055231.csv"
    btc_df = pd.read_csv(btc_path)
    print(f"  BTC prices: {len(btc_df):,} rows")

    # Observer
    obs_path = base_dir / "research/observer/grid_obs_20260131.csv"
    obs_df = pd.read_csv(obs_path, on_bad_lines='skip', low_memory=False)
    print(f"  Observer: {len(obs_df):,} rows")

    # Resolutions
    res_path = base_dir / "research/observer/market_resolutions_verified.csv"
    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Align timestamps
    btc_start, btc_end = btc_df['timestamp_ms'].min(), btc_df['timestamp_ms'].max()
    obs_start, obs_end = obs_df['timestamp_ms'].min(), obs_df['timestamp_ms'].max()

    overlap_start = max(btc_start, obs_start)
    overlap_end = min(btc_end, obs_end)

    hours = (overlap_end - overlap_start) / 3600000
    print(f"  Overlap: {hours:.2f} hours")

    btc_df = btc_df[(btc_df['timestamp_ms'] >= overlap_start) &
                     (btc_df['timestamp_ms'] <= overlap_end)].copy()
    obs_df = obs_df[(obs_df['timestamp_ms'] >= overlap_start) &
                     (obs_df['timestamp_ms'] <= overlap_end)].copy()

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

    return btc_df, obs_df, res_map, hours


def run_test():
    """Run EWMA spike base comparison."""
    load_ou_params()
    btc_df, obs_df, res_map, hours = load_oos8_data()

    # Test methods
    methods = [
        ("FIXED", lambda df: precompute_spikes_fixed(df)),
        ("EWMA_200", lambda df: precompute_spikes_ewma(df, 200)),
        ("EWMA_300", lambda df: precompute_spikes_ewma(df, 300)),
        ("EWMA_500", lambda df: precompute_spikes_ewma(df, 500)),
        ("EWMA_1000", lambda df: precompute_spikes_ewma(df, 1000)),
    ]

    all_results = []

    for method_name, spike_func in methods:
        print(f"\n{'='*60}")
        print(f"Testing: {method_name}")
        print(f"{'='*60}")

        # Precompute spikes
        btc_spikes = spike_func(btc_df)

        # Simulate all markets
        all_trades = []
        slugs = obs_df['market_slug'].unique()

        for slug in tqdm(slugs, desc=f"  Markets"):
            resolution = res_map.get(slug)
            if resolution not in ['UP', 'DOWN']:
                continue

            trades = simulate_market(btc_spikes, obs_df, slug, resolution, method_name)
            all_trades.extend(trades)

        # Compute metrics
        if all_trades:
            total_pnl = sum(t.pnl_net for t in all_trades)
            total_trades = len(all_trades)
            hourly_rate = total_pnl / hours
            win_rate = sum(1 for t in all_trades if t.correct_direction) / total_trades * 100
            passive_pct = sum(1 for t in all_trades if t.hedge_type == "passive") / total_trades * 100
            avg_pair_cost = np.mean([t.pair_cost for t in all_trades])

            # Consecutive trade analysis
            trades_df = pd.DataFrame([t.__dict__ for t in all_trades])
            trades_df = trades_df.sort_values(['market_slug', 'entry_time_remaining'], ascending=[True, False])

            # Count rapid sequences (< 10s apart in same market)
            rapid_sequences = 0
            for slug in trades_df['market_slug'].unique():
                mkt_trades = trades_df[trades_df['market_slug'] == slug].sort_values('entry_time_remaining', ascending=False)
                if len(mkt_trades) > 1:
                    time_gaps = mkt_trades['entry_time_remaining'].diff().abs()
                    rapid_sequences += (time_gaps < 10).sum()

            result = {
                'method': method_name,
                'trades': total_trades,
                'pnl_net': total_pnl,
                'hourly_rate': hourly_rate,
                'win_rate': win_rate,
                'passive_pct': passive_pct,
                'avg_pair_cost': avg_pair_cost,
                'rapid_sequences': rapid_sequences,
                'trades_per_hour': total_trades / hours,
            }
            all_results.append(result)

            print(f"  Trades: {total_trades}, $/hr: ${hourly_rate:.2f}, Win%: {win_rate:.1f}%")
            print(f"  Passive: {passive_pct:.1f}%, Rapid sequences: {rapid_sequences}")

            # Save per-trade CSV
            trades_path = Path(f"research/findings/data/ewma_spike_trades_{method_name}_OOS8.csv")
            trades_df.to_csv(trades_path, index=False)

    # Summary
    print("\n" + "=" * 80)
    print("EWMA SPIKE BASE COMPARISON - OOS8 (18.12h)")
    print("=" * 80)
    print()
    print(f"{'Method':<12} {'Trades':>8} {'$/hr':>10} {'Win%':>8} {'Passive%':>10} {'Rapid<10s':>10} {'Tr/hr':>8}")
    print("-" * 80)

    for r in all_results:
        print(f"{r['method']:<12} {r['trades']:>8} ${r['hourly_rate']:>9.2f} {r['win_rate']:>7.1f}% "
              f"{r['passive_pct']:>9.1f}% {r['rapid_sequences']:>10} {r['trades_per_hour']:>7.1f}")

    # Save summary
    summary_df = pd.DataFrame(all_results)
    summary_path = Path("research/findings/data/ewma_spike_base_comparison.csv")
    summary_df.to_csv(summary_path, index=False)
    print(f"\nResults saved to: {summary_path}")

    # Analysis
    print("\n" + "=" * 80)
    print("ANALYSIS")
    print("=" * 80)

    fixed = next(r for r in all_results if r['method'] == 'FIXED')

    for r in all_results:
        if r['method'] == 'FIXED':
            continue

        trade_reduction = (fixed['trades'] - r['trades']) / fixed['trades'] * 100
        pnl_change = (r['hourly_rate'] - fixed['hourly_rate']) / abs(fixed['hourly_rate']) * 100
        rapid_reduction = (fixed['rapid_sequences'] - r['rapid_sequences']) / fixed['rapid_sequences'] * 100 if fixed['rapid_sequences'] > 0 else 0

        print(f"\n{r['method']} vs FIXED:")
        print(f"  Trade reduction: {trade_reduction:.1f}%")
        print(f"  $/hr change: {pnl_change:+.1f}%")
        print(f"  Rapid sequence reduction: {rapid_reduction:.1f}%")
        print(f"  Efficiency ($/trade): ${r['pnl_net']/r['trades']:.3f} vs ${fixed['pnl_net']/fixed['trades']:.3f}")


if __name__ == "__main__":
    run_test()
