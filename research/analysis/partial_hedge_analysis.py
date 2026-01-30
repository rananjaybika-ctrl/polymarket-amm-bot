#!/usr/bin/env python3
"""
PARTIAL HEDGE ANALYSIS

Test what happens with 80%, 90%, 100% hedge ratios.
Due to Polymarket's $1 minimum, taker orders sometimes fill 4.9 instead of 5 shares.

Key insight:
- Hedged portion: protected by pair (profit/loss based on pair cost)
- Unhedged portion: naked exposure (if direction correct, big win; if wrong, total loss)

This uses actual resolutions to determine profitability.
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
TIME_STOP_SECONDS = 120  # TIME120s config
MIN_TIME = 180  # time_stop + 60s buffer
MIN_CYCLE_GAP_MS = 1000

# Skip threshold
SKIP_THRESHOLD = 0.90

# OU parameters
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
class TradeResult:
    market_slug: str
    winner_side: str
    resolution: str
    direction_correct: bool
    winner_entry: float
    loser_fill: float
    pair_cost: float
    hedge_type: str  # passive, timestop, resolution
    hedge_ratio: float
    hedged_shares: float
    naked_shares: float
    hedged_pnl: float
    naked_pnl: float
    total_pnl: float


@dataclass
class ConfigResult:
    name: str
    hedge_ratio: float
    trades: int
    total_pnl: float
    hourly_rate: float
    direction_accuracy: float
    avg_pnl_per_trade: float
    hedge_passive: int
    hedge_timestop: int
    hedge_resolution: int
    naked_wins: int
    naked_losses: int


def run_backtest(spikes_df, obs_df, hedge_ratio: float, hours: float, num_markets: int) -> Tuple[ConfigResult, List[TradeResult]]:
    """Run backtest with partial hedging."""

    all_trades = []
    hedge_types = {'passive': 0, 'timestop': 0, 'resolution': 0}
    naked_wins = 0
    naked_losses = 0

    hedged_shares = TARGET_SHARES * hedge_ratio
    naked_shares = TARGET_SHARES * (1 - hedge_ratio)

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

        # Cycling state
        in_position = False
        last_hedge_ts = 0

        for _, spike_row in market_spikes.iterrows():
            spike_ts = spike_row['timestamp_ms']
            spike_dir = spike_row['spike_direction']
            zscore = spike_row['zscore']
            spike_mag = spike_row['spike_magnitude']

            # Z-ZONE FILTER: 0 <= z < 1.5
            if zscore < 0.0 or zscore > 1.5:
                continue

            # Cycling
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

            # Skip high entry
            if winner_entry >= SKIP_THRESHOLD:
                continue

            # Calculate loser target
            # FIX: Do NOT divide by 100 - spike_mag is already percentage (0.05 = 0.05%)
            expected_drop = DROP_MULTIPLIER * spike_mag + DROP_INTERCEPT
            expected_drop = max(0.02, min(0.20, expected_drop))
            max_loser = TARGET_PAIR_COST - winner_entry
            loser_target = min((1.0 - winner_entry) - expected_drop, max_loser)
            loser_target = max(0.01, min(0.95, loser_target))

            # Enter position
            in_position = True
            entry_ts = spike_ts

            # Simulate hedge
            hedge_type = "resolution"
            loser_fill = 0.0
            hedge_fill_ts = market_end

            for j in range(obs_idx + 1, len(mdf)):
                scan_row = mdf.iloc[j]
                scan_ts = scan_row['timestamp_ms']

                if loser_side == "UP":
                    curr_loser_ask = scan_row['up_ask']
                else:
                    curr_loser_ask = scan_row['down_ask']

                # Passive fill
                if curr_loser_ask <= loser_target:
                    loser_fill = loser_target
                    hedge_type = "passive"
                    hedge_fill_ts = scan_ts
                    break

                # Time-stop
                elapsed_secs = (scan_ts - entry_ts) / 1000.0
                if elapsed_secs >= TIME_STOP_SECONDS:
                    loser_fill = curr_loser_ask
                    hedge_type = "timestop"
                    hedge_fill_ts = scan_ts
                    break

            # Resolution exit (data gap)
            if hedge_type == "resolution":
                if resolution == winner_side:
                    loser_fill = loser_target
                else:
                    loser_fill = 1.0

            pair_cost = winner_entry + loser_fill
            direction_correct = (resolution == winner_side)

            # ====== PARTIAL HEDGE PNL CALCULATION ======
            # Hedged portion: standard pair PnL
            if hedge_type == "resolution" and not direction_correct:
                # Resolution exit with wrong direction - hedge didn't help
                hedged_pnl = -winner_entry * hedged_shares
            else:
                hedged_pnl = (1.0 - pair_cost) * hedged_shares

            # Naked portion: depends on resolution
            if direction_correct:
                # Winner pays $1, we paid winner_entry
                naked_pnl = (1.0 - winner_entry) * naked_shares
                if naked_shares > 0:
                    naked_wins += 1
            else:
                # Winner pays $0, we lose entire entry cost
                naked_pnl = -winner_entry * naked_shares
                if naked_shares > 0:
                    naked_losses += 1

            total_pnl = hedged_pnl + naked_pnl

            trade = TradeResult(
                market_slug=slug,
                winner_side=winner_side,
                resolution=resolution,
                direction_correct=direction_correct,
                winner_entry=winner_entry,
                loser_fill=loser_fill,
                pair_cost=pair_cost,
                hedge_type=hedge_type,
                hedge_ratio=hedge_ratio,
                hedged_shares=hedged_shares,
                naked_shares=naked_shares,
                hedged_pnl=hedged_pnl,
                naked_pnl=naked_pnl,
                total_pnl=total_pnl,
            )
            all_trades.append(trade)
            hedge_types[hedge_type] += 1

            # Exit position
            in_position = False
            last_hedge_ts = hedge_fill_ts

    trades = len(all_trades)
    total_pnl = sum(t.total_pnl for t in all_trades)
    correct = sum(1 for t in all_trades if t.direction_correct)

    return ConfigResult(
        name=f"HEDGE_{int(hedge_ratio*100)}%",
        hedge_ratio=hedge_ratio,
        trades=trades,
        total_pnl=total_pnl,
        hourly_rate=total_pnl / hours if hours > 0 else 0,
        direction_accuracy=correct / trades if trades > 0 else 0,
        avg_pnl_per_trade=total_pnl / trades if trades > 0 else 0,
        hedge_passive=hedge_types['passive'],
        hedge_timestop=hedge_types['timestop'],
        hedge_resolution=hedge_types['resolution'],
        naked_wins=naked_wins,
        naked_losses=naked_losses,
    ), all_trades


def main():
    print("=" * 80)
    print("PARTIAL HEDGE ANALYSIS")
    print("=" * 80)
    print()
    print("Testing hedge ratios: 80%, 90%, 98% (4.9/5), 100%")
    print("Config: TIME120s_SKIP (time_stop=120s, min_time=180s, skip>=0.90)")
    print()

    load_ou_params()

    # Load IS+OOS2 data (same as final_timestop_comparison.py)
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

    # Test hedge ratios
    hedge_ratios = [0.80, 0.90, 0.98, 1.00]  # 98% = 4.9/5 shares
    results = []
    all_trades_by_ratio = {}

    for ratio in hedge_ratios:
        result, trades = run_backtest(spikes_only, obs_df, ratio, hours, num_markets)
        results.append(result)
        all_trades_by_ratio[ratio] = trades

    # Print results
    print("=" * 80)
    print("RESULTS")
    print("=" * 80)
    print()

    print(f"{'Config':<12} {'Trades':>7} {'$/hr':>9} {'DirAcc':>7} {'AvgPnL':>8} "
          f"{'Passive':>8} {'TStop':>7} {'NakedW':>7} {'NakedL':>7}")
    print("-" * 90)

    for r in results:
        print(f"{r.name:<12} {r.trades:>7} ${r.hourly_rate:>8.2f} {r.direction_accuracy:>6.1%} "
              f"${r.avg_pnl_per_trade:>7.2f} {r.hedge_passive:>8} {r.hedge_timestop:>7} "
              f"{r.naked_wins:>7} {r.naked_losses:>7}")

    print()
    print("=" * 80)
    print("ANALYSIS")
    print("=" * 80)
    print()

    # Compare 100% vs 98% (the actual issue)
    r100 = next(r for r in results if r.hedge_ratio == 1.0)
    r98 = next(r for r in results if r.hedge_ratio == 0.98)
    r90 = next(r for r in results if r.hedge_ratio == 0.90)
    r80 = next(r for r in results if r.hedge_ratio == 0.80)

    print("Impact of Polymarket $1 minimum (4.9 vs 5 shares = 98% hedge):")
    print(f"  100% hedge: ${r100.hourly_rate:.2f}/hr")
    print(f"   98% hedge: ${r98.hourly_rate:.2f}/hr")
    print(f"  Difference: ${r98.hourly_rate - r100.hourly_rate:+.2f}/hr ({(r98.hourly_rate/r100.hourly_rate - 1)*100:+.1f}%)")
    print()

    print("If we intentionally reduce hedge further:")
    print(f"   90% hedge: ${r90.hourly_rate:.2f}/hr ({(r90.hourly_rate/r100.hourly_rate - 1)*100:+.1f}% vs 100%)")
    print(f"   80% hedge: ${r80.hourly_rate:.2f}/hr ({(r80.hourly_rate/r100.hourly_rate - 1)*100:+.1f}% vs 100%)")
    print()

    # Naked position analysis
    print("Naked Position Performance (unhedged shares):")
    for r in results:
        if r.hedge_ratio < 1.0:
            naked_total = r.naked_wins + r.naked_losses
            naked_wr = r.naked_wins / naked_total if naked_total > 0 else 0
            print(f"  {r.name}: {r.naked_wins}W / {r.naked_losses}L = {naked_wr:.1%} win rate")

    print()

    # Breakeven analysis
    print("Breakeven Analysis:")
    print("  At ~70% direction accuracy (our actual), partial hedging should help")
    print("  Naked wins: +$(1 - entry) per share")
    print("  Naked losses: -$entry per share")
    print()

    # Example trade breakdown
    print("Example Trade Breakdown (avg entry=$0.50):")
    print("  100% hedge (50sh): pair_cost=$1.00 -> PnL = $0.00")
    print("   90% hedge (45sh hedged, 5sh naked):")
    print("     If direction CORRECT: hedged=$0 + naked=5*$0.50 = +$2.50")
    print("     If direction WRONG:   hedged=$0 + naked=-5*$0.50 = -$2.50")
    print("     At 70% accuracy: EV = 0.7*$2.50 + 0.3*(-$2.50) = +$1.00/trade")
    print()

    # Save results
    rows = [{
        'config': r.name,
        'hedge_ratio': r.hedge_ratio,
        'trades': r.trades,
        'total_pnl': r.total_pnl,
        'hourly_rate': r.hourly_rate,
        'direction_accuracy': r.direction_accuracy,
        'avg_pnl_per_trade': r.avg_pnl_per_trade,
        'hedge_passive': r.hedge_passive,
        'hedge_timestop': r.hedge_timestop,
        'hedge_resolution': r.hedge_resolution,
        'naked_wins': r.naked_wins,
        'naked_losses': r.naked_losses,
    } for r in results]

    output_path = "research/partial_hedge_results.csv"
    pd.DataFrame(rows).to_csv(output_path, index=False)
    print(f"Results saved to: {output_path}")

    # Save detailed trades for 90% ratio
    trades_90 = all_trades_by_ratio[0.90]
    trade_rows = [{
        'market_slug': t.market_slug,
        'winner_side': t.winner_side,
        'resolution': t.resolution,
        'direction_correct': t.direction_correct,
        'winner_entry': t.winner_entry,
        'loser_fill': t.loser_fill,
        'pair_cost': t.pair_cost,
        'hedge_type': t.hedge_type,
        'hedged_pnl': t.hedged_pnl,
        'naked_pnl': t.naked_pnl,
        'total_pnl': t.total_pnl,
    } for t in trades_90]

    trades_path = "research/partial_hedge_trades_90pct.csv"
    pd.DataFrame(trade_rows).to_csv(trades_path, index=False)
    print(f"90% hedge trades saved to: {trades_path}")

    print()
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
