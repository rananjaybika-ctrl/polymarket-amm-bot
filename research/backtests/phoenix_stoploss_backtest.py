#!/usr/bin/env python3
"""
PHOENIX + Stop-Loss Backtest
============================

COPIED FROM: phoenix_main_backtest.py (validated execution engine)
PURPOSE: Test adding taker stop-loss exits to PHOENIX V1 to reduce max loss

Key modification: After maker entry fill on expensive side, monitor price.
If expensive_ask drops below fill_price - stop_threshold:
  → Taker sell after 500ms delay at bid price, pay taker fee
  → Optionally sell only partial position (Baguette-style 64% exit)

Grid search over:
  - stop_threshold: how much drop triggers stop
  - stop_min_delay_secs: minimum time before stop can activate
  - stop_exit_pct: fraction of position to exit (1.0=full, 0.64=Baguette)
  - Baseline: no stop (current PHOENIX V1)

Usage:
    python research/backtests/phoenix_stoploss_backtest.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
import sys
import math
from datetime import datetime
from itertools import product
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.core import polymarket_taker_fee

# =============================================================================
# CONSTANTS (identical to phoenix_main_backtest.py)
# =============================================================================
STARTING_CAPITAL = 170.0
MAX_CAPITAL_FRACTION = 0.50
EWMA_HALFLIFE_MS = 1000
SKIP_UTC_HOURS = frozenset()
DECEL_WINDOWS = [(600, 180), (600, 120), (300, 180), (300, 120)]

# OU ADAPTIVE THRESHOLD params
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10

TAKER_DELAY_MS = 542  # 500ms exchange + 42ms network


# =============================================================================
# DATA CLASSES
# =============================================================================
@dataclass
class StopLossConfig:
    name: str
    # PHOENIX base params (fixed at V1 winner)
    expensive_threshold: float = 0.80
    entry_start_secs: float = 300.0
    entry_end_secs: float = 120.0
    entry_offset: float = 0.02
    hedge_offset: float = 0.02
    max_pair_cost: float = 0.96
    base_shares: int = 25
    max_entries_per_market: int = 99
    cooldown_secs: int = 10
    # Session stop
    adapt_trades: int = 25
    adapt_threshold: float = -5.0
    max_drawdown_pct: float = 0.20
    # STOP LOSS params (NEW)
    stop_enabled: bool = False
    stop_threshold: float = 0.08       # Price drop to trigger stop
    stop_min_delay_secs: float = 15.0  # Min seconds after fill before stop can trigger
    stop_exit_pct: float = 1.0         # Fraction of position to exit (1.0=all, 0.64=Baguette)
    # PROFIT TARGET params (NEW — Baguette-style partial exit)
    profit_target: float = 0.0         # Price rise to trigger profit-taking (0=disabled)
    profit_exit_pct: float = 0.64      # Fraction to take profit on


@dataclass
class TradeResult:
    market_slug: str
    entry_time_remaining: float
    expensive_side: str
    entry_price: float
    hedge_price: Optional[float]
    pair_cost: Optional[float]
    is_hedged: bool
    pnl_gross: float
    pnl_net: float
    correct_direction: bool
    shares: int
    spike_magnitude: float
    entry_number: int
    dataset: str
    config_name: str
    # Stop-loss fields
    was_stopped: bool = False
    stop_exit_price: float = 0.0
    stop_exit_shares: int = 0
    stop_fee: float = 0.0
    held_shares: int = 0  # shares held to resolution after partial stop
    # Profit target fields
    profit_taken: bool = False
    profit_exit_price: float = 0.0
    profit_exit_shares: int = 0
    profit_fee: float = 0.0


# =============================================================================
# OU ADAPTIVE THRESHOLD (identical to phoenix_main_backtest.py)
# =============================================================================
@dataclass
class OUParams:
    mu: float = -3.9845
    sigma_stat: float = 0.3877


def compute_ou_threshold(volatility, ou_params):
    log_vol = math.log(max(volatility, 1e-6))
    z_score = (log_vol - ou_params.mu) / ou_params.sigma_stat
    z_clamped = max(-10, min(10, z_score * OU_SIGMOID_STEEPNESS))
    sigmoid = 1.0 / (1.0 + math.exp(-z_clamped))
    multiplier = OU_K_LOW + (OU_K_HIGH - OU_K_LOW) * sigmoid
    threshold = OU_BASE_THRESHOLD * multiplier
    return max(OU_MIN_THRESHOLD, min(OU_MAX_THRESHOLD, threshold)), z_score


# =============================================================================
# SPIKE DETECTION (identical to phoenix_main_backtest.py)
# =============================================================================
def precompute_spikes_ewma(btc_df, halflife_ms=EWMA_HALFLIFE_MS):
    halflife_ticks = halflife_ms / 16.67
    alpha = 1 - 0.5 ** (1.0 / halflife_ticks)
    gap_threshold_ms = 30 * 60 * 1000

    df = btc_df.copy().sort_values('timestamp_ms').reset_index(drop=True)
    original_len = len(df)
    df = df.drop_duplicates(subset=['timestamp_ms'], keep='first').reset_index(drop=True)

    prices = df['price'].values
    timestamps = df['timestamp_ms'].values
    ewma_prices = np.zeros(len(prices))
    ewma_prices[0] = prices[0]

    for i in range(1, len(prices)):
        time_diff = timestamps[i] - timestamps[i-1]
        if time_diff > gap_threshold_ms:
            ewma_prices[i] = prices[i]
        else:
            ewma_prices[i] = alpha * prices[i] + (1 - alpha) * ewma_prices[i-1]

    df['ewma_price'] = ewma_prices
    df['deviation_pct'] = (df['price'] - df['ewma_price']) / df['ewma_price'] * 100
    df['spike_magnitude'] = df['deviation_pct'].abs()

    ou_params = OUParams()
    returns = df['price'].pct_change() * 100
    ewma_halflife = 300
    var_alpha = 1 - 0.5 ** (1.0 / ewma_halflife)

    variance = returns.iloc[:60].var() if len(returns) > 60 else 0.01
    thresholds = []
    for i, r in enumerate(returns):
        if pd.isna(r):
            thresholds.append(OU_BASE_THRESHOLD)
            continue
        variance = var_alpha * (r ** 2) + (1 - var_alpha) * variance
        vol = max(np.sqrt(variance), 1e-6)
        threshold, _ = compute_ou_threshold(vol, ou_params)
        thresholds.append(threshold)

    df['threshold'] = thresholds
    df['spike_detected'] = df['spike_magnitude'] >= df['threshold']
    return df


# =============================================================================
# DECELERATION (identical to phoenix_main_backtest.py)
# =============================================================================
def compute_deceleration(vel_arr, time_rem_arr, entry_start, entry_end):
    mask = (time_rem_arr <= entry_start) & (time_rem_arr >= entry_end)
    vel = vel_arr[mask]
    vel = vel[~np.isnan(vel)]
    if len(vel) < 6:
        return False
    mid = len(vel) // 2
    first_mag = np.abs(vel[:mid]).mean()
    second_mag = np.abs(vel[mid:]).mean()
    if first_mag < 0.001:
        return False
    return bool(second_mag < first_mag * 0.70)


# =============================================================================
# MARKET DATA PRECOMPUTATION — MODIFIED to include bid prices
# =============================================================================
def precompute_markets(obs_df, spike_ts_all, spike_mag_all, resolutions):
    market_data = {}

    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions:
            continue

        mdf = group.sort_values('timestamp_ms')
        n = len(mdf)
        if n == 0:
            continue

        ts = mdf['timestamp_ms'].values.copy()
        up_ask = mdf['up_ask'].values.astype(float)
        down_ask = mdf['down_ask'].values.astype(float)
        # NEW: Extract bid prices for stop-loss selling
        up_bid = mdf['up_bid'].values.astype(float) if 'up_bid' in mdf.columns else up_ask - 0.01
        down_bid = mdf['down_bid'].values.astype(float) if 'down_bid' in mdf.columns else down_ask - 0.01
        time_rem = mdf['time_remaining_secs'].values.astype(float)
        vel = mdf['velocity_bps'].fillna(0).values.astype(float) if 'velocity_bps' in mdf.columns else np.zeros(n)
        hours = pd.to_datetime(ts, unit='ms', utc=True).hour.values

        start_idx = np.searchsorted(spike_ts_all, ts[0])
        end_idx = np.searchsorted(spike_ts_all, ts[-1], side='right')
        m_spike_ts = spike_ts_all[start_idx:end_idx]
        m_spike_mag = spike_mag_all[start_idx:end_idx]
        m_spike_obs_idx = np.searchsorted(ts, m_spike_ts, side='right') - 1
        m_spike_obs_idx = np.clip(m_spike_obs_idx, 0, n - 1)

        decel = {}
        for start, end in DECEL_WINDOWS:
            decel[(start, end)] = compute_deceleration(vel, time_rem, start, end)

        market_data[slug] = {
            'resolution': resolutions[slug],
            'n': n, 'ts': ts,
            'up_ask': up_ask, 'down_ask': down_ask,
            'up_bid': up_bid, 'down_bid': down_bid,  # NEW
            'time_rem': time_rem, 'hours': hours,
            'spike_ts': m_spike_ts, 'spike_mag': m_spike_mag,
            'spike_obs_idx': m_spike_obs_idx, 'decel': decel,
        }

    return market_data


# =============================================================================
# SIMULATION — PHOENIX + STOP LOSS + PROFIT TARGET
# =============================================================================
def simulate_market_fast(slug, md, config, dataset_name, current_balance):
    resolution = md['resolution']
    ts = md['ts']
    up_ask = md['up_ask']
    down_ask = md['down_ask']
    up_bid = md['up_bid']
    down_bid = md['down_bid']
    time_rem = md['time_rem']
    hours = md['hours']
    spike_ts = md['spike_ts']
    spike_mag = md['spike_mag']
    spike_obs_idx = md['spike_obs_idx']

    if len(spike_ts) == 0:
        return []

    max_per_market = current_balance * MAX_CAPITAL_FRACTION
    cooldown_ms = config.cooldown_secs * 1000
    stop_min_delay_ms = config.stop_min_delay_secs * 1000

    trades = []
    entries = 0
    total_capital_deployed = 0.0
    last_signal_ts = 0

    for si in range(len(spike_ts)):
        if entries >= config.max_entries_per_market:
            break

        oi = spike_obs_idx[si]
        tr = time_rem[oi]

        if tr > config.entry_start_secs or tr < config.entry_end_secs:
            continue
        if spike_ts[si] - last_signal_ts < cooldown_ms:
            continue
        if hours[oi] in SKIP_UTC_HOURS:
            continue

        ua, da = up_ask[oi], down_ask[oi]
        if np.isnan(ua) or np.isnan(da) or ua <= 0 or da <= 0:
            continue

        if ua >= da:
            exp_ask = ua
            exp_side = "UP"
            entry_asks = up_ask
            hedge_asks = down_ask
            exit_bids = up_bid  # For selling our position
        else:
            exp_ask = da
            exp_side = "DOWN"
            entry_asks = down_ask
            hedge_asks = up_ask
            exit_bids = down_bid

        if exp_ask < config.expensive_threshold:
            continue

        last_signal_ts = spike_ts[si]
        entry_bid = max(0.01, exp_ask - config.entry_offset)

        # ENTRY FILL CHECK (maker, price-touch)
        if oi + 1 >= len(entry_asks):
            continue
        entry_slice = entry_asks[oi + 1:]
        fill_indices = np.where(entry_slice <= entry_bid)[0]
        if len(fill_indices) == 0:
            continue

        fill_global = oi + 1 + fill_indices[0]
        fill_price = entry_bid
        fill_time_ms = ts[fill_global]

        # SHARE SIZING
        remaining_capital = max_per_market - total_capital_deployed
        if remaining_capital <= 0:
            break
        max_affordable = int(remaining_capital / fill_price) if fill_price > 0 else 0
        shares = min(config.base_shares, max_affordable)
        if shares <= 0:
            break

        entries += 1
        total_capital_deployed += shares * fill_price

        # =================================================================
        # STOP-LOSS & PROFIT TARGET CHECK (NEW)
        # =================================================================
        was_stopped = False
        stop_exit_price = 0.0
        stop_exit_shares = 0
        stop_fee = 0.0
        held_shares_after_stop = shares

        profit_taken = False
        profit_exit_price = 0.0
        profit_exit_shares = 0
        profit_fee = 0.0
        held_shares_after_profit = shares

        if config.stop_enabled or config.profit_target > 0:
            for j in range(fill_global + 1, len(ts)):
                elapsed_ms = ts[j] - fill_time_ms

                current_exp_ask = entry_asks[j]
                if np.isnan(current_exp_ask):
                    continue

                # --- PROFIT TARGET CHECK (Baguette-style) ---
                if not profit_taken and config.profit_target > 0:
                    price_rise = current_exp_ask - fill_price
                    if price_rise >= config.profit_target:
                        # Profit target hit! Taker sell partial position
                        # 500ms delay
                        target_ts = ts[j] + TAKER_DELAY_MS
                        sell_idx = np.searchsorted(ts, target_ts, side='left')
                        sell_idx = min(sell_idx, len(ts) - 1)

                        sell_bid = exit_bids[sell_idx]
                        if np.isnan(sell_bid) or sell_bid <= 0:
                            sell_bid = entry_asks[sell_idx] - 0.01 if not np.isnan(entry_asks[sell_idx]) else fill_price

                        profit_exit_shares = int(shares * config.profit_exit_pct)
                        if profit_exit_shares > 0 and sell_bid > 0:
                            fee_per_share = polymarket_taker_fee(sell_bid)
                            profit_fee = fee_per_share * profit_exit_shares
                            profit_exit_price = sell_bid
                            profit_taken = True
                            held_shares_after_profit = shares - profit_exit_shares

                # --- STOP LOSS CHECK ---
                if not was_stopped and config.stop_enabled:
                    if elapsed_ms < stop_min_delay_ms:
                        continue

                    price_drop = fill_price - current_exp_ask
                    if price_drop >= config.stop_threshold:
                        # Stop triggered! Taker sell after 500ms delay
                        target_ts = ts[j] + TAKER_DELAY_MS
                        sell_idx = np.searchsorted(ts, target_ts, side='left')
                        sell_idx = min(sell_idx, len(ts) - 1)

                        sell_bid = exit_bids[sell_idx]
                        if np.isnan(sell_bid) or sell_bid <= 0:
                            sell_bid = entry_asks[sell_idx] - 0.01 if not np.isnan(entry_asks[sell_idx]) else 0

                        # How many shares to stop out
                        remaining_shares = held_shares_after_profit if profit_taken else shares
                        stop_exit_shares = int(remaining_shares * config.stop_exit_pct)

                        if stop_exit_shares > 0 and sell_bid > 0:
                            fee_per_share = polymarket_taker_fee(sell_bid)
                            stop_fee = fee_per_share * stop_exit_shares
                            stop_exit_price = sell_bid
                            was_stopped = True
                            held_shares_after_stop = remaining_shares - stop_exit_shares
                        break  # Stop checking after stop triggers

        # =================================================================
        # HEDGE FILL CHECK (for remaining shares held to resolution)
        # =================================================================
        # Only hedge shares that are held to resolution
        final_held_shares = held_shares_after_stop if was_stopped else (held_shares_after_profit if profit_taken else shares)

        is_hedged = False
        hedge_price = None

        if final_held_shares > 0 and fill_global + 1 < len(hedge_asks):
            hedge_slice = hedge_asks[fill_global + 1:]
            cheap_at_fill = hedge_asks[fill_global]
            if np.isnan(cheap_at_fill):
                hedge_bid = config.max_pair_cost - fill_price
            else:
                hedge_bid_raw = cheap_at_fill - config.hedge_offset
                hedge_bid_max = config.max_pair_cost - fill_price
                hedge_bid = min(hedge_bid_raw, hedge_bid_max)

            if hedge_bid >= 0.01:
                hedge_fill_mask = hedge_slice <= hedge_bid
                hedge_indices = np.where(hedge_fill_mask)[0]
                if len(hedge_indices) > 0:
                    is_hedged = True
                    hedge_price = hedge_bid

        # =================================================================
        # PnL CALCULATION (accounting for stops and profit-taking)
        # =================================================================
        pnl = 0.0

        # 1. Profit from partial profit-taking exit
        if profit_taken and profit_exit_shares > 0:
            pnl += (profit_exit_price - fill_price) * profit_exit_shares - profit_fee

        # 2. Loss/profit from stop-loss exit
        if was_stopped and stop_exit_shares > 0:
            pnl += (stop_exit_price - fill_price) * stop_exit_shares - stop_fee

        # 3. PnL from shares held to resolution
        if final_held_shares > 0:
            if is_hedged:
                pair_cost = fill_price + hedge_price
                pnl += (1.0 - pair_cost) * final_held_shares
            else:
                if resolution == exp_side:
                    pnl += (1.0 - fill_price) * final_held_shares
                else:
                    pnl += -fill_price * final_held_shares

        trades.append(TradeResult(
            market_slug=slug,
            entry_time_remaining=tr,
            expensive_side=exp_side,
            entry_price=fill_price,
            hedge_price=hedge_price,
            pair_cost=(fill_price + hedge_price) if is_hedged else None,
            is_hedged=is_hedged,
            pnl_gross=pnl,
            pnl_net=pnl,
            correct_direction=(resolution == exp_side),
            shares=shares,
            spike_magnitude=spike_mag[si],
            entry_number=entries,
            dataset=dataset_name,
            config_name=config.name,
            was_stopped=was_stopped,
            stop_exit_price=stop_exit_price,
            stop_exit_shares=stop_exit_shares,
            stop_fee=stop_fee,
            held_shares=final_held_shares,
            profit_taken=profit_taken,
            profit_exit_price=profit_exit_price,
            profit_exit_shares=profit_exit_shares,
            profit_fee=profit_fee,
        ))

    return trades


# =============================================================================
# DATASETS (identical to phoenix_main_backtest.py)
# =============================================================================
DATASETS = {
    "IS+OOS2": {
        "name": "IS+OOS2 (Jan 16-19)",
        "btc_file": "research/binance_hf/btc_prices_20260118_060340.csv",
        "obs_files": [
            "research/observer/grid_obs_20260116.csv",
            "research/observer/grid_obs_20260117.csv",
            "research/observer/grid_obs_20260118.csv",
            "research/observer/grid_obs_20260119.csv",
        ],
        "res_files": ["research/observer/market_resolutions.csv"],
    },
    "OOS3+4": {
        "name": "OOS3+4 (Jan 22-24)",
        "btc_file": "research/observer/PROTECTED_btc_prices_oos3_oos4_combined.csv",
        "obs_files": ["research/observer/PROTECTED_grid_obs_oos3_oos4_combined.csv"],
        "res_files": ["research/observer/market_resolutions_verified.csv"],
    },
    "OOS7": {
        "name": "OOS7 (Jan 29-30)",
        "btc_file": "research/binance_hf/btc_prices_20260129_160523.csv",
        "obs_files": [
            "research/observer/grid_obs_20260129.csv",
            "research/observer/grid_obs_20260130.csv",
        ],
        "res_files": [
            "research/observer/resolutions_20260129.csv",
            "research/observer/resolutions_20260130.csv",
        ],
    },
    "OOS8": {
        "name": "OOS8 (Jan 31)",
        "btc_file": "research/binance_hf/btc_prices_20260131_055231.csv",
        "obs_files": ["research/observer/grid_obs_20260131.csv"],
        "res_files": ["research/observer/resolutions_20260131.csv"],
    },
    "OOS9": {
        "name": "OOS9 (Feb 1-3)",
        "btc_file": "research/binance_hf/btc_prices_oos9.csv",
        "obs_files": ["research/observer/grid_obs_oos9.csv"],
        "res_files": [
            "research/observer/resolutions_oos9_1.csv",
            "research/observer/resolutions_oos9_2.csv",
        ],
    },
}


def load_dataset(dataset_key):
    config = DATASETS[dataset_key]
    base_dir = Path("/Users/rananjaybika/polymarket-amm-bot")

    print(f"\n{'='*60}")
    print(f"Loading {config['name']}")
    print(f"{'='*60}")

    obs_dfs = []
    for fname in config['obs_files']:
        fpath = base_dir / fname
        if fpath.exists():
            df = pd.read_csv(fpath, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)
            print(f"  {fpath.name}: {len(df):,} rows")

    if not obs_dfs:
        return None, None, {}, 0

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])

    btc_path = base_dir / config['btc_file']
    if btc_path.exists():
        btc_df = pd.read_csv(btc_path)
    else:
        return None, None, {}, 0

    resolutions = {}
    for res_fname in config.get('res_files', []):
        res_path = base_dir / res_fname
        if res_path.exists():
            res_df = pd.read_csv(res_path)
            if 'slug' in res_df.columns and 'winner' in res_df.columns:
                for _, row in res_df.iterrows():
                    resolutions[row['slug']] = row['winner']
            elif 'market_slug' in res_df.columns and 'resolution' in res_df.columns:
                for _, row in res_df.iterrows():
                    resolutions[row['market_slug']] = row['resolution']

    duration_hours = (obs_df['timestamp_ms'].max() - obs_df['timestamp_ms'].min()) / (1000 * 3600)
    print(f"  Duration: {duration_hours:.1f}h, {len(resolutions)} markets resolved")

    return obs_df, btc_df, resolutions, duration_hours


# =============================================================================
# SESSION STOP (identical logic)
# =============================================================================
def run_with_session_stops(config, market_data, markets_ordered, dataset_name):
    session_pnl = 0.0
    session_peak_pnl = 0.0
    session_stopped = False
    all_trades = []
    trade_count = 0
    current_balance = STARTING_CAPITAL
    adaptive_activated = False
    adaptive_checked = False

    for slug in markets_ordered:
        if session_stopped:
            break
        if slug not in market_data:
            continue

        md = market_data[slug]
        market_trades = simulate_market_fast(slug, md, config, dataset_name, current_balance)

        for trade in market_trades:
            session_pnl += trade.pnl_net
            session_peak_pnl = max(session_peak_pnl, session_pnl)
            current_balance = STARTING_CAPITAL + session_pnl
            trade_count += 1
            all_trades.append(trade)

            if not adaptive_checked and trade_count >= config.adapt_trades:
                adaptive_checked = True
                if session_pnl < config.adapt_threshold:
                    adaptive_activated = True

            if adaptive_activated:
                dd = (session_peak_pnl - session_pnl) / STARTING_CAPITAL
                if dd >= config.max_drawdown_pct:
                    session_stopped = True
                    break

    return all_trades, session_pnl, session_stopped


# =============================================================================
# GRID CONFIGS
# =============================================================================
def generate_configs():
    configs = []

    # 1. BASELINE: No stop loss (current PHOENIX V1)
    configs.append(StopLossConfig(
        name="BASELINE_NO_STOP",
        stop_enabled=False,
        profit_target=0.0,
    ))

    # 2. STOP LOSS grid
    stop_thresholds = [0.03, 0.05, 0.08, 0.12, 0.20]
    stop_delays = [5, 15, 30, 60, 120]
    stop_exit_pcts = [0.64, 1.0]

    for thresh, delay, exit_pct in product(stop_thresholds, stop_delays, stop_exit_pcts):
        name = f"STOP_T{int(thresh*100):02d}_D{delay}_E{int(exit_pct*100)}"
        configs.append(StopLossConfig(
            name=name,
            stop_enabled=True,
            stop_threshold=thresh,
            stop_min_delay_secs=delay,
            stop_exit_pct=exit_pct,
        ))

    # 3. PROFIT TARGET + STOP combos (Baguette-style)
    profit_targets = [0.05, 0.08]
    for pt, thresh, delay in product(profit_targets, [0.05, 0.10], [15, 60]):
        name = f"BAGUETTE_PT{int(pt*100)}_ST{int(thresh*100)}_D{delay}"
        configs.append(StopLossConfig(
            name=name,
            stop_enabled=True,
            stop_threshold=thresh,
            stop_min_delay_secs=delay,
            stop_exit_pct=1.0,
            profit_target=pt,
            profit_exit_pct=0.64,
        ))

    return configs


# =============================================================================
# METRICS
# =============================================================================
def calc_metrics(trades, duration_hours, config_name, dataset_name):
    if not trades:
        return None

    pnls = [t.pnl_net for t in trades]
    total_pnl = sum(pnls)
    stops = sum(1 for t in trades if t.was_stopped)
    profits_taken = sum(1 for t in trades if t.profit_taken)
    correct = sum(1 for t in trades if t.correct_direction)

    # Worst single-trade loss
    worst_loss = min(pnls) if pnls else 0

    # Stopped trades that were wrong direction
    wrong_stopped = sum(1 for t in trades if t.was_stopped and not t.correct_direction)
    wrong_not_stopped = sum(1 for t in trades if not t.was_stopped and not t.correct_direction)

    # Average loss when wrong
    wrong_pnls = [t.pnl_net for t in trades if not t.correct_direction]
    avg_wrong_pnl = np.mean(wrong_pnls) if wrong_pnls else 0

    # Average loss when stopped
    stopped_pnls = [t.pnl_net for t in trades if t.was_stopped]
    avg_stopped_pnl = np.mean(stopped_pnls) if stopped_pnls else 0

    # False stop rate: stopped but would have been correct (holds to resolution = profit)
    false_stops = sum(1 for t in trades if t.was_stopped and t.correct_direction)

    # Max drawdown
    cumulative = np.cumsum(pnls)
    peak = np.maximum.accumulate(cumulative)
    max_dd = np.max(peak - cumulative) if len(cumulative) > 0 else 0

    return {
        'config': config_name,
        'dataset': dataset_name,
        'trades': len(trades),
        'total_pnl': round(total_pnl, 2),
        'pnl_per_hr': round(total_pnl / duration_hours, 2) if duration_hours > 0 else 0,
        'win_rate': round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1),
        'accuracy': round(correct / len(trades) * 100, 1),
        'worst_loss': round(worst_loss, 2),
        'avg_wrong_pnl': round(avg_wrong_pnl, 2),
        'avg_stopped_pnl': round(avg_stopped_pnl, 2),
        'stops': stops,
        'stop_rate': round(stops / len(trades) * 100, 1),
        'false_stops': false_stops,
        'false_stop_rate': round(false_stops / max(stops, 1) * 100, 1),
        'wrong_stopped': wrong_stopped,
        'wrong_not_stopped': wrong_not_stopped,
        'profits_taken': profits_taken,
        'max_dd': round(max_dd, 2),
        'max_dd_pct': round(max_dd / STARTING_CAPITAL * 100, 1),
        'sharpe': round((np.mean(pnls) / np.std(pnls) * np.sqrt(252*24)) if len(pnls) > 1 and np.std(pnls) > 0 else 0, 2),
    }


# =============================================================================
# MAIN
# =============================================================================
def main():
    print("=" * 80)
    print("PHOENIX + STOP LOSS BACKTEST")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    configs = generate_configs()
    print(f"Configs: {len(configs)} ({len(configs)-1} stop variants + 1 baseline)")
    dataset_keys = ["IS+OOS2", "OOS3+4", "OOS7", "OOS8", "OOS9"]
    total_runs = len(configs) * len(dataset_keys)
    print(f"Total runs: {total_runs}")

    output_dir = Path("/Users/rananjaybika/polymarket-amm-bot/research/findings/data")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "phoenix_stoploss_results.csv"
    checkpoint_file = output_dir / "phoenix_stoploss_checkpoint.csv"

    all_results = []

    for dataset_key in dataset_keys:
        obs_df, btc_df, resolutions, duration_hours = load_dataset(dataset_key)
        if obs_df is None:
            continue

        print(f"\n  Precomputing spikes...")
        btc_spikes_df = precompute_spikes_ewma(btc_df)
        spike_mask = btc_spikes_df['spike_detected'].values
        spike_ts_all = btc_spikes_df.loc[spike_mask, 'timestamp_ms'].values
        spike_mag_all = btc_spikes_df.loc[spike_mask, 'spike_magnitude'].values
        sort_idx = np.argsort(spike_ts_all)
        spike_ts_all = spike_ts_all[sort_idx]
        spike_mag_all = spike_mag_all[sort_idx]

        print(f"  Precomputing markets...")
        market_data = precompute_markets(obs_df, spike_ts_all, spike_mag_all, resolutions)
        market_starts = {slug: md['ts'][0] for slug, md in market_data.items()}
        markets_ordered = sorted(market_data.keys(), key=lambda s: market_starts[s])
        print(f"  {len(market_data)} markets, {len(spike_ts_all):,} spikes")

        for config in tqdm(configs, desc=f"  {dataset_key}"):
            trades, session_pnl, stopped = run_with_session_stops(
                config, market_data, markets_ordered, dataset_key
            )
            metrics = calc_metrics(trades, duration_hours, config.name, dataset_key)
            if metrics:
                metrics['stop_threshold'] = config.stop_threshold if config.stop_enabled else 0
                metrics['stop_delay'] = config.stop_min_delay_secs if config.stop_enabled else 0
                metrics['stop_exit_pct'] = config.stop_exit_pct if config.stop_enabled else 0
                metrics['profit_target'] = config.profit_target
                all_results.append(metrics)

        pd.DataFrame(all_results).to_csv(checkpoint_file, index=False)
        print(f"  Checkpoint: {len(all_results)} results")

    results_df = pd.DataFrame(all_results)
    results_df.to_csv(output_file, index=False)

    # =========================================================================
    # SUMMARY
    # =========================================================================
    print("\n" + "=" * 80)
    print("RESULTS SUMMARY")
    print("=" * 80)

    # Aggregate across datasets
    agg = results_df.groupby('config').agg({
        'total_pnl': 'sum',
        'pnl_per_hr': 'mean',
        'trades': 'sum',
        'win_rate': 'mean',
        'worst_loss': 'min',
        'avg_wrong_pnl': 'mean',
        'stops': 'sum',
        'false_stops': 'sum',
        'false_stop_rate': 'mean',
        'max_dd': 'max',
        'sharpe': 'mean',
    }).sort_values('pnl_per_hr', ascending=False)

    print(f"\n{'Config':<35} {'$/hr':>7} {'PnL':>8} {'WR%':>5} {'Worst':>7} "
          f"{'AvgWrong':>8} {'Stops':>5} {'FalseStop%':>10} {'Sharpe':>7}")
    print("-" * 110)
    for name, row in agg.head(20).iterrows():
        print(f"{name:<35} ${row['pnl_per_hr']:>6.2f} ${row['total_pnl']:>7.2f} "
              f"{row['win_rate']:>5.1f} ${row['worst_loss']:>6.2f} "
              f"${row['avg_wrong_pnl']:>7.2f} {int(row['stops']):>5} "
              f"{row['false_stop_rate']:>9.1f}% {row['sharpe']:>7.2f}")

    # Show baseline vs best stop
    baseline = agg.loc['BASELINE_NO_STOP'] if 'BASELINE_NO_STOP' in agg.index else None
    if baseline is not None:
        print(f"\n{'='*80}")
        print(f"BASELINE (NO STOP):  ${baseline['pnl_per_hr']:.2f}/hr, "
              f"worst=${baseline['worst_loss']:.2f}, avg_wrong=${baseline['avg_wrong_pnl']:.2f}")

        # Best by pnl_per_hr (excluding baseline)
        stop_configs = agg.drop('BASELINE_NO_STOP', errors='ignore')
        if len(stop_configs) > 0:
            best = stop_configs.iloc[0]
            best_name = stop_configs.index[0]
            print(f"BEST STOP CONFIG:    ${best['pnl_per_hr']:.2f}/hr, "
                  f"worst=${best['worst_loss']:.2f}, avg_wrong=${best['avg_wrong_pnl']:.2f}")
            print(f"  Config: {best_name}")
            print(f"  Improvement: ${best['pnl_per_hr'] - baseline['pnl_per_hr']:.2f}/hr, "
                  f"worst loss improved by ${baseline['worst_loss'] - best['worst_loss']:.2f}")

    print(f"\nResults saved to: {output_file}")
    print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
