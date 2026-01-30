#!/usr/bin/env python3
"""
Entry Spike Magnitude Test - MAKER vs TAKER Entry Methods

Tests whether posting as MAKER (to avoid 500ms taker delay) can work by using
spike magnitude to set an aggressive bid price.

IMPORTANT: Hedge mechanism (calculate_loser_bid) stays UNCHANGED!

Entry Methods Tested:
1. TAKER (current): Always buy at winner_ask (instant fill, 500ms delay in live)
2. MAKER_RAW: entry_price = winner_bid + spike_magnitude
3. MAKER_OU: entry_price = winner_bid + spike_magnitude * (1 + z_score)
4. MAKER_THRESH: entry_price = winner_bid + spike_magnitude / threshold

Time-Stops: 20s, 60s, 120s, 180s (120s = current AGGRESSIVE config)

Grid: 4 entry methods × 4 time-stops = 16 configurations

Datasets:
- IS+OOS2 (OBI OFF)
- OOS3+4 (OBI OFF)
- OOS5 (OBI OFF)
- OOS7 (OBI ON)

Usage:
    python research/backtests/entry_spike_magnitude_test.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
from tqdm import tqdm
import math
import sys
import time

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# =============================================================================
# CONFIGURATION - SOURCED FROM TRADING_CONFIGS.py (Jan 31, 2026)
# =============================================================================

from research.reference.TRADING_CONFIGS import AGGRESSIVE as AGGRESSIVE_CONFIG

TARGET_SHARES = 50  # PRODUCTION: 50 shares
MIN_TIME = int(AGGRESSIVE_CONFIG.min_time_remaining)  # 180 from config
MIN_RUNTIME_SECS = 300  # 5 minutes minimum market duration
HIGH_ENTRY_THRESHOLD = AGGRESSIVE_CONFIG.high_entry_threshold  # 0.90 from config

# Spike detection at 60Hz - CANONICAL from TRADING_CONFIGS.py
SPIKE_LOOKBACK = AGGRESSIVE_CONFIG.lookback_ticks  # 72 ticks (1200ms)

# OU ADAPTIVE THRESHOLD - NOT fixed 0.02! (per TRADING_CONFIGS.py threshold_method="ou")
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10

# Enhanced signal filtering
VELOCITY_CONFIRM_THRESHOLD = 0.10
ENHANCED_SCORE_THRESHOLD = 0.40

# Loser bid calculation (FIXED - no /100 bug) - DO NOT MODIFY!
DROP_MULTIPLIER = 0.50
DROP_INTERCEPT = 0.08
TARGET_PAIR_COST = 0.99

# Cycling
MIN_CYCLE_GAP_MS = 200


# =============================================================================
# FEE MODEL (from aggressive_grid_search.py)
# =============================================================================

def polymarket_taker_fee(price: float) -> float:
    """
    Polymarket taker fee: 1.56% * (1 - |2*price - 1|)

    Examples:
        price = 0.50 -> fee = 1.56%
        price = 0.40 -> fee = 1.25%
        price = 0.90 -> fee = 0.31%
    """
    return 0.0156 * (1 - abs(2 * price - 1))


def calculate_pnl_with_fees(winner_entry: float, loser_fill: float, shares: int,
                            is_taker_entry: bool, is_taker_exit: bool) -> Tuple[float, float, float, float]:
    """
    Calculate PnL with proper maker/taker fee handling.

    Args:
        winner_entry: Entry price for winner side
        loser_fill: Fill price for loser hedge
        shares: Number of shares
        is_taker_entry: True if entry was taker (crossed spread)
        is_taker_exit: True if exit was taker (time-stop market order)

    Returns:
        Tuple of (net_pnl, gross_pnl, entry_fee, exit_fee)
    """
    pair_cost = winner_entry + loser_fill
    gross_pnl = (1.0 - pair_cost) * shares

    entry_fee = polymarket_taker_fee(winner_entry) * winner_entry * shares if is_taker_entry else 0
    exit_fee = polymarket_taker_fee(loser_fill) * loser_fill * shares if is_taker_exit else 0

    net_pnl = gross_pnl - entry_fee - exit_fee
    return net_pnl, gross_pnl, entry_fee, exit_fee


# =============================================================================
# ENTRY CONFIG
# =============================================================================

@dataclass
class EntryConfig:
    """Configuration for entry method and time-stop."""
    name: str
    time_stop_seconds: float
    entry_method: str  # "taker", "maker_raw", "maker_ou", "maker_thresh"

    def compute_entry_price(self, winner_bid: float, winner_ask: float,
                            spike_magnitude: float, threshold: float,
                            z_score: float = 0.0) -> Tuple[float, bool]:
        """
        Compute entry price and whether we're maker or taker.

        Returns: (entry_price, is_maker)
        """
        if self.entry_method == "taker":
            # Always TAKER: buy at ask
            return winner_ask, False

        # MAKER methods: compute aggressive bid
        if self.entry_method == "maker_raw":
            entry_price = winner_bid + spike_magnitude
        elif self.entry_method == "maker_ou":
            # Scale by volatility z-score
            entry_price = winner_bid + spike_magnitude * (1 + z_score)
        elif self.entry_method == "maker_thresh":
            # Scale relative to threshold
            entry_price = winner_bid + (spike_magnitude / threshold if threshold > 0 else spike_magnitude)
        else:
            # Fallback to taker
            return winner_ask, False

        # Check if crosses spread (becomes taker)
        is_maker = entry_price < winner_ask
        return entry_price, is_maker


# Generate 16 configs: 4 entry methods × 4 time-stops
TIME_STOPS = [20.0, 60.0, 120.0, 180.0]  # 120s = current AGGRESSIVE config
ENTRY_METHODS = ["taker", "maker_raw", "maker_ou", "maker_thresh"]

CONFIGS = []
for method in ENTRY_METHODS:
    for ts in TIME_STOPS:
        label = {
            "taker": "TAKER",
            "maker_raw": "MAKER_RAW",
            "maker_ou": "MAKER_OU",
            "maker_thresh": "MAKER_THRESH"
        }[method]
        name = f"{label}_TS{int(ts)}"
        CONFIGS.append(EntryConfig(name, ts, method))


# =============================================================================
# OU PARAMETERS (for adaptive threshold)
# =============================================================================

_ou_params = None


def load_ou_params():
    """Load OU parameters for adaptive threshold."""
    global _ou_params
    try:
        from src.strategies.ou_volatility import OUParameters
        _ou_params = OUParameters.load("research/ou_params.json")
        print(f"[OU] Loaded: mu={_ou_params.mu:.4f}, sigma_stat={_ou_params.sigma_stat:.4f}")
    except Exception as e:
        print(f"[OU] Warning: {e} - using fixed threshold 0.02")
        _ou_params = None


def compute_ou_threshold(volatility: float) -> float:
    """Compute OU adaptive threshold from current volatility."""
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


def compute_ou_zscore(volatility: float) -> float:
    """Compute OU z-score for volatility."""
    global _ou_params
    if _ou_params is None:
        return 0.0
    vol = max(volatility, 1e-6)
    log_vol = math.log(vol)
    z_score = (log_vol - _ou_params.mu) / _ou_params.sigma_stat
    return z_score


# =============================================================================
# DATASET CONFIGURATION
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
        "use_obi": False,
    },
    "OOS3+4": {
        "name": "OOS3+4 (Jan 22-24)",
        "btc_file": "research/observer/PROTECTED_btc_prices_oos3_oos4_combined.csv",
        "obs_files": [
            "research/observer/PROTECTED_grid_obs_oos3_oos4_combined.csv",
        ],
        "use_obi": False,
    },
    "OOS5": {
        "name": "OOS5 (Jan 26)",
        "btc_file": None,  # Will use observer binance_price at 5Hz
        "obs_files": [
            "research/observer/PROTECTED_grid_obs_oos5_recovered.csv",
        ],
        "use_obi": False,
    },
    "OOS7": {
        "name": "OOS7 (Jan 29-30)",
        "btc_file": "research/binance_hf/btc_prices_20260129_160523.csv",
        "obs_files": [
            "research/observer/grid_obs_20260129.csv",
            "research/observer/grid_obs_20260130.csv",
        ],
        "use_obi": True,  # OBI ON for OOS7
    },
}


# =============================================================================
# DATA CLASSES
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
    pnl_gross: float  # PnL before fees
    pnl_net: float    # PnL after fees
    entry_fee: float
    exit_fee: float
    correct_direction: bool
    spike_magnitude: float
    dataset: str
    entry_method: str  # NEW: track entry method
    is_maker_entry: bool  # NEW: was entry a maker order?


# =============================================================================
# SPIKE DETECTION - VECTORIZED PRECOMPUTATION
# =============================================================================

def precompute_spikes_ou(btc_df: pd.DataFrame, lookback: int = SPIKE_LOOKBACK) -> pd.DataFrame:
    """
    Vectorized spike detection with OU ADAPTIVE threshold.
    Also computes z-score for MAKER_OU entry method.
    """
    print("    Using OU ADAPTIVE threshold (per TRADING_CONFIGS.py)")
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    # Calculate price change over lookback
    df['price_prev'] = df['price'].shift(lookback)
    df['change_pct'] = (df['price'] - df['price_prev']) / df['price_prev'] * 100
    df['spike_magnitude'] = df['change_pct'].abs()

    # Compute EWMA volatility for OU adaptive threshold
    returns = df['price'].pct_change() * 100
    ewma_halflife = 300
    alpha = 1 - 0.5 ** (1.0 / ewma_halflife)

    variance = returns.iloc[:60].var() if len(returns) > 60 else 0.01
    thresholds = []
    zscores = []

    for i, r in enumerate(returns):
        if pd.isna(r):
            thresholds.append(OU_BASE_THRESHOLD)
            zscores.append(0.0)
            continue
        variance = alpha * (r ** 2) + (1 - alpha) * variance
        vol = max(np.sqrt(variance), 1e-6)
        threshold = compute_ou_threshold(vol)
        z_score = compute_ou_zscore(vol)
        thresholds.append(threshold)
        zscores.append(z_score)

    df['threshold'] = thresholds
    df['z_score'] = zscores
    df['spike_detected'] = df['spike_magnitude'] >= df['threshold']
    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['change_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['change_pct'] < 0), 'spike_direction'] = 'DOWN'

    spike_count = df['spike_detected'].sum()
    print(f"    Found {spike_count:,} spikes (OU adaptive)")

    return df


# =============================================================================
# HELPER FUNCTIONS (COPIED from multi_dataset_validated_backtest.py)
# =============================================================================

def velocity_confirms_spike(spike_dir: str, velocity_bps: float) -> bool:
    """Check if velocity confirms spike direction."""
    if spike_dir == "UP":
        return velocity_bps > -VELOCITY_CONFIRM_THRESHOLD
    elif spike_dir == "DOWN":
        return velocity_bps < VELOCITY_CONFIRM_THRESHOLD
    return True


def obi_confirms_spike(spike_dir: str, up_imbalance: Optional[float],
                       down_imbalance: Optional[float]) -> Tuple[bool, bool]:
    """Check if OBI confirms spike direction."""
    if spike_dir == "UP":
        if up_imbalance is not None and not np.isnan(up_imbalance):
            return True, up_imbalance > 0
    elif spike_dir == "DOWN":
        if down_imbalance is not None and not np.isnan(down_imbalance):
            return True, down_imbalance > 0
    return False, True  # Not available = don't filter


def compute_enhanced_score(spike_mag: float, velocity_bps: float,
                           spike_dir: str, time_remaining: float) -> float:
    """Compute composite score (matching live strategy)."""
    spike_score = min(spike_mag / 0.05, 1.0)
    velocity_score = min(abs(velocity_bps) / 0.50, 1.0)

    vel_confirms = (spike_dir == "UP" and velocity_bps > 0) or \
                   (spike_dir == "DOWN" and velocity_bps < 0)
    confirm_bonus = 1.0 if vel_confirms else 0.0

    urgency = 1.0 - min(time_remaining / 900.0, 1.0)

    score = (0.40 * spike_score +
             0.30 * velocity_score +
             0.20 * confirm_bonus +
             0.10 * urgency)

    return round(score, 3)


def calculate_loser_bid(winner_entry: float, spike_magnitude: float) -> float:
    """
    Calculate loser bid. FIXED: No /100 division.

    DO NOT MODIFY - validated hedge pricing.
    """
    expected_drop = DROP_MULTIPLIER * spike_magnitude + DROP_INTERCEPT
    max_loser = TARGET_PAIR_COST - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid))


# =============================================================================
# SIMULATION - OPTIMIZED WITH ENTRY METHOD TESTING
# =============================================================================

def simulate_market_with_entry_method(btc_spikes: pd.DataFrame, obs_df: pd.DataFrame,
                                       slug: str, resolution: str,
                                       use_obi_filter: bool, dataset_name: str,
                                       entry_config: EntryConfig) -> List[TradeResult]:
    """
    Simulate trading with different entry methods.

    Entry methods:
    - TAKER: instant fill at winner_ask
    - MAKER_*: post bid at winner_bid + f(spike_magnitude), wait for fill
    """
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    # Get only spikes in this market's time range
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
    waiting_for_entry_fill = False
    entry_order_data = None
    time_stop_ms = entry_config.time_stop_seconds * 1000

    spike_idx = 0
    obs_idx = 0

    while spike_idx < len(market_spikes) or in_position or waiting_for_entry_fill:

        # =================================================================
        # STATE 1: Waiting for MAKER entry fill
        # =================================================================
        if waiting_for_entry_fill and entry_order_data is not None:
            order_ts = entry_order_data['order_ts']
            entry_bid = entry_order_data['entry_bid']

            # Scan observer rows after order placement
            while obs_idx < len(mdf):
                obs_row = mdf.iloc[obs_idx]
                obs_ts = obs_row['timestamp_ms']

                if obs_ts < order_ts:
                    obs_idx += 1
                    continue

                winner_side = entry_order_data['winner_side']
                if winner_side == "UP":
                    winner_ask = obs_row['up_ask']
                else:
                    winner_ask = obs_row['down_ask']

                # Check if we got filled (ask dropped to our bid)
                if pd.notna(winner_ask) and winner_ask <= entry_bid:
                    # MAKER ENTRY FILLED!
                    cycle_num += 1
                    loser_side = "DOWN" if winner_side == "UP" else "UP"
                    winner_entry = entry_bid  # Filled at our bid price
                    loser_target = calculate_loser_bid(winner_entry, entry_order_data['spike_magnitude'])

                    in_position = True
                    waiting_for_entry_fill = False
                    position_data = {
                        'winner_side': winner_side,
                        'loser_side': loser_side,
                        'winner_entry': winner_entry,
                        'loser_target': loser_target,
                        'entry_ts': obs_ts,
                        'entry_time_rem': obs_row['time_remaining_secs'],
                        'spike_magnitude': entry_order_data['spike_magnitude'],
                        'score': entry_order_data['score'],
                        'is_maker_entry': True,
                    }
                    entry_order_data = None
                    obs_idx += 1
                    break

                # Check time-stop on entry order (didn't fill in time)
                elapsed_ms = obs_ts - order_ts
                if elapsed_ms >= time_stop_ms:
                    # Entry order timed out - cancel and reset
                    waiting_for_entry_fill = False
                    entry_order_data = None
                    obs_idx += 1
                    break

                obs_idx += 1

            # If we ran out of data while waiting
            if waiting_for_entry_fill and obs_idx >= len(mdf):
                waiting_for_entry_fill = False
                entry_order_data = None
                break

            continue

        # =================================================================
        # STATE 2: In position - check for hedge
        # =================================================================
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
                is_maker_entry = position_data.get('is_maker_entry', False)

                if loser_side == "UP":
                    loser_ask = obs_row['up_ask']
                else:
                    loser_ask = obs_row['down_ask']

                # Check passive fill
                if pd.notna(loser_ask) and loser_ask <= loser_target:
                    # Passive hedge = MAKER exit (no exit fee)
                    pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                        winner_entry, loser_target, TARGET_SHARES,
                        is_taker_entry=(not is_maker_entry),  # Taker if not maker entry
                        is_taker_exit=False  # Passive = maker
                    )

                    trades.append(TradeResult(
                        market_slug=slug,
                        cycle_num=cycle_num,
                        entry_time_remaining=position_data['entry_time_rem'],
                        signal_score=score,
                        winner_side=position_data['winner_side'],
                        winner_fill_price=winner_entry,
                        loser_fill_price=loser_target,
                        hedge_type="passive",
                        pair_cost=winner_entry + loser_target,
                        pnl_gross=pnl_gross,
                        pnl_net=pnl_net,
                        entry_fee=entry_fee,
                        exit_fee=exit_fee,
                        correct_direction=(resolution == position_data['winner_side']),
                        spike_magnitude=spike_mag,
                        dataset=dataset_name,
                        entry_method=entry_config.entry_method,
                        is_maker_entry=is_maker_entry,
                    ))

                    in_position = False
                    position_data = None
                    last_hedge_ts = obs_ts
                    obs_idx += 1
                    break

                # Check time-stop (ONLY if NOT in profit - matches live enhanced_spike.py:1177-1195)
                elapsed_ms = obs_ts - entry_ts
                if elapsed_ms >= time_stop_ms:
                    # Get current winner bid to check if in profit
                    winner_side_current = position_data['winner_side']
                    if winner_side_current == "UP":
                        winner_bid_current = obs_row['up_bid']
                    else:
                        winner_bid_current = obs_row['down_bid']

                    # Check if in profit: winner_bid >= entry price
                    # (if we could sell winner now at bid, would we profit?)
                    in_profit = pd.notna(winner_bid_current) and winner_bid_current >= winner_entry

                    if not in_profit:
                        # NOT in profit - execute time-stop
                        loser_fill = loser_ask if pd.notna(loser_ask) else loser_target * 1.05
                        # Time-stop = TAKER exit (pay exit fee)
                        pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                            winner_entry, loser_fill, TARGET_SHARES,
                            is_taker_entry=(not is_maker_entry),
                            is_taker_exit=True  # Time-stop = taker
                        )

                        trades.append(TradeResult(
                            market_slug=slug,
                            cycle_num=cycle_num,
                            entry_time_remaining=position_data['entry_time_rem'],
                            signal_score=score,
                            winner_side=position_data['winner_side'],
                            winner_fill_price=winner_entry,
                            loser_fill_price=loser_fill,
                            hedge_type="time_stop",
                            pair_cost=winner_entry + loser_fill,
                            pnl_gross=pnl_gross,
                            pnl_net=pnl_net,
                            entry_fee=entry_fee,
                            exit_fee=exit_fee,
                            correct_direction=(resolution == position_data['winner_side']),
                            spike_magnitude=spike_mag,
                            dataset=dataset_name,
                            entry_method=entry_config.entry_method,
                            is_maker_entry=is_maker_entry,
                        ))

                        in_position = False
                        position_data = None
                        last_hedge_ts = obs_ts
                        obs_idx += 1
                        break
                    # else: in profit, keep waiting for passive fill (don't time-stop)

                obs_idx += 1

            # If we ran out of observer data while in position
            if in_position and obs_idx >= len(mdf):
                winner_side = position_data['winner_side']
                winner_entry = position_data['winner_entry']
                is_maker_entry = position_data.get('is_maker_entry', False)

                # Resolution: no hedge trade, just entry fee
                entry_fee = polymarket_taker_fee(winner_entry) * winner_entry * TARGET_SHARES if not is_maker_entry else 0

                if resolution == winner_side:
                    pnl_gross = (1.0 - winner_entry) * TARGET_SHARES
                    loser_fill = 0.0
                else:
                    pnl_gross = (0.0 - winner_entry) * TARGET_SHARES
                    loser_fill = 1.0

                pnl_net = pnl_gross - entry_fee

                trades.append(TradeResult(
                    market_slug=slug,
                    cycle_num=cycle_num,
                    entry_time_remaining=position_data['entry_time_rem'],
                    signal_score=position_data['score'],
                    winner_side=winner_side,
                    winner_fill_price=winner_entry,
                    loser_fill_price=loser_fill,
                    hedge_type="resolution",
                    pair_cost=winner_entry + loser_fill,
                    pnl_gross=pnl_gross,
                    pnl_net=pnl_net,
                    entry_fee=entry_fee,
                    exit_fee=0.0,  # No exit trade
                    correct_direction=(resolution == winner_side),
                    spike_magnitude=position_data['spike_magnitude'],
                    dataset=dataset_name,
                    entry_method=entry_config.entry_method,
                    is_maker_entry=is_maker_entry,
                ))
                break

            continue

        # =================================================================
        # STATE 3: Not in position - check next spike
        # =================================================================
        if spike_idx >= len(market_spikes):
            break

        spike_row = market_spikes.iloc[spike_idx]
        spike_ts = spike_row['timestamp_ms']
        spike_dir = spike_row['spike_direction']
        spike_mag = spike_row['spike_magnitude']
        threshold = spike_row['threshold']
        z_score = spike_row.get('z_score', 0.0)

        # Enforce cycle gap
        if (spike_ts - last_hedge_ts) < MIN_CYCLE_GAP_MS:
            spike_idx += 1
            continue

        # Find nearest observer row for this spike
        while obs_idx < len(mdf) - 1 and mdf.iloc[obs_idx + 1]['timestamp_ms'] <= spike_ts:
            obs_idx += 1

        if obs_idx >= len(mdf):
            break

        obs_row = mdf.iloc[obs_idx]
        time_rem = obs_row['time_remaining_secs']
        velocity_bps = obs_row.get('velocity_bps', 0.0) or 0.0

        # Skip if too close to end
        if time_rem < MIN_TIME:
            spike_idx += 1
            continue

        # Velocity confirmation
        if not velocity_confirms_spike(spike_dir, velocity_bps):
            spike_idx += 1
            continue

        # Enhanced score
        score = compute_enhanced_score(spike_mag, velocity_bps, spike_dir, time_rem)
        if score < ENHANCED_SCORE_THRESHOLD:
            spike_idx += 1
            continue

        # OBI filter
        if use_obi_filter:
            up_imbalance = obs_row.get('up_imbalance', None)
            down_imbalance = obs_row.get('down_imbalance', None)
            obi_available, obi_confirmed = obi_confirms_spike(spike_dir, up_imbalance, down_imbalance)
            if obi_available and not obi_confirmed:
                spike_idx += 1
                continue

        # Get prices
        winner_side = spike_dir
        if winner_side == "UP":
            winner_ask = obs_row['up_ask']
            winner_bid = obs_row['up_bid']
        else:
            winner_ask = obs_row['down_ask']
            winner_bid = obs_row['down_bid']

        if pd.isna(winner_ask) or winner_ask >= HIGH_ENTRY_THRESHOLD:
            spike_idx += 1
            continue

        if pd.isna(winner_bid):
            winner_bid = winner_ask - 0.01  # Fallback spread

        # Compute entry price based on method
        entry_price, is_maker = entry_config.compute_entry_price(
            winner_bid, winner_ask, spike_mag, threshold, z_score
        )

        if entry_config.entry_method == "taker" or not is_maker:
            # TAKER: immediate fill at ask
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
                'is_maker_entry': False,
            }
        else:
            # MAKER: post bid and wait for fill
            waiting_for_entry_fill = True
            entry_order_data = {
                'winner_side': winner_side,
                'entry_bid': entry_price,
                'order_ts': spike_ts,
                'spike_magnitude': spike_mag,
                'score': score,
            }

        spike_idx += 1

    return trades


# =============================================================================
# DATA LOADING (COPIED from multi_dataset_validated_backtest.py)
# =============================================================================

def load_dataset(dataset_key: str) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame], Dict[str, str], float]:
    """Load a dataset."""
    config = DATASETS[dataset_key]
    base_dir = Path("/Users/rananjaybika/polymarket-amm-bot")

    print(f"\n{'='*60}")
    print(f"Loading {config['name']}")
    print(f"{'='*60}")

    # Load observer data
    obs_dfs = []
    for fname in config['obs_files']:
        fpath = base_dir / fname
        if fpath.exists():
            df = pd.read_csv(fpath, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)
            print(f"  {fpath.name}: {len(df):,} rows")
        else:
            print(f"  {fpath.name}: NOT FOUND")

    if not obs_dfs:
        return None, None, {}, 0

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    print(f"  Combined observer: {len(obs_df):,} rows")

    # Load BTC data
    btc_df = None
    if config['btc_file']:
        btc_path = base_dir / config['btc_file']
        if btc_path.exists():
            btc_df = pd.read_csv(btc_path)
            print(f"  Binance HF: {len(btc_df):,} rows")
        else:
            print(f"  Binance HF: NOT FOUND - will use observer binance_price")

    # If no 60Hz data, create from observer binance_price (5Hz)
    if btc_df is None:
        if 'binance_price' in obs_df.columns:
            btc_df = obs_df[['timestamp_ms', 'binance_price']].copy()
            btc_df = btc_df.rename(columns={'binance_price': 'price'})
            btc_df = btc_df.dropna()
            btc_df = btc_df.drop_duplicates(subset='timestamp_ms')
            print(f"  Using observer binance_price: {len(btc_df):,} rows (5Hz)")
        else:
            print(f"  ERROR: No price data available")
            return None, None, {}, 0

    # Load resolutions
    res_path = base_dir / "research/observer/market_resolutions_verified.csv"
    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Find overlap
    btc_start, btc_end = btc_df['timestamp_ms'].min(), btc_df['timestamp_ms'].max()
    obs_start, obs_end = obs_df['timestamp_ms'].min(), obs_df['timestamp_ms'].max()

    overlap_start = max(btc_start, obs_start)
    overlap_end = min(btc_end, obs_end)

    duration_hours = (overlap_end - overlap_start) / 3600000
    print(f"  Overlap: {duration_hours:.2f} hours")

    # Filter
    btc_df = btc_df[(btc_df['timestamp_ms'] >= overlap_start) &
                     (btc_df['timestamp_ms'] <= overlap_end)].copy()
    obs_df = obs_df[(obs_df['timestamp_ms'] >= overlap_start) &
                     (obs_df['timestamp_ms'] <= overlap_end)].copy()

    # Add resolutions
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

    return btc_df, obs_df, res_map, duration_hours


def run_backtest_dataset(dataset_key: str, entry_config: EntryConfig) -> Tuple[List[TradeResult], float]:
    """Run backtest on a single dataset with a specific entry config."""
    config = DATASETS[dataset_key]
    btc_df, obs_df, res_map, hours = load_dataset(dataset_key)

    if btc_df is None or obs_df is None or len(obs_df) == 0:
        print(f"  Skipping {dataset_key} - no valid data")
        return [], 0

    # Precompute spikes with OU adaptive threshold
    print(f"  Precomputing spikes with OU adaptive threshold...")
    btc_spikes = precompute_spikes_ou(btc_df)

    use_obi = config['use_obi']
    print(f"  Running simulation (OBI {'ON' if use_obi else 'OFF'}, Entry: {entry_config.entry_method})...")

    all_trades = []
    slugs = obs_df['market_slug'].unique()

    for slug in slugs:
        resolution = res_map.get(slug)
        if resolution not in ['UP', 'DOWN']:
            continue

        trades = simulate_market_with_entry_method(
            btc_spikes, obs_df, slug, resolution, use_obi, dataset_key, entry_config
        )
        all_trades.extend(trades)

    return all_trades, hours


# =============================================================================
# MAIN GRID SEARCH
# =============================================================================

def run_grid_search():
    """Run grid search across all 16 configurations × 4 datasets."""
    print("=" * 80)
    print("ENTRY SPIKE MAGNITUDE TEST")
    print("=" * 80)
    print(f"\nConfigurations: {len(CONFIGS)} (4 entry methods × 4 time-stops)")
    print(f"Datasets: {len(DATASETS)} (IS+OOS2, OOS3+4, OOS5, OOS7)")
    print(f"Total runs: {len(CONFIGS) * len(DATASETS)}")
    print()

    # Load OU parameters for adaptive threshold
    load_ou_params()

    # Cache dataset loads to avoid repeated I/O
    dataset_cache = {}

    all_results = []
    checkpoint_path = Path("research/findings/data/entry_magnitude_checkpoint.csv")
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    total_iterations = len(CONFIGS) * len(DATASETS)
    iteration = 0
    start_time = time.time()

    for config in CONFIGS:
        config_results = []

        for dataset_key in DATASETS.keys():
            iteration += 1
            print(f"\n[{iteration}/{total_iterations}] {config.name} on {dataset_key}")

            # Run backtest
            if dataset_key not in dataset_cache:
                ds_config = DATASETS[dataset_key]
                btc_df, obs_df, res_map, hours = load_dataset(dataset_key)
                if btc_df is not None:
                    btc_spikes = precompute_spikes_ou(btc_df)
                    dataset_cache[dataset_key] = (btc_spikes, obs_df, res_map, hours, ds_config['use_obi'])
                else:
                    continue
            else:
                btc_spikes, obs_df, res_map, hours, use_obi = dataset_cache[dataset_key]
                ds_config = DATASETS[dataset_key]

            # Run simulation
            all_trades = []
            slugs = obs_df['market_slug'].unique()

            for slug in tqdm(slugs, desc=f"  Markets", leave=False):
                resolution = res_map.get(slug)
                if resolution not in ['UP', 'DOWN']:
                    continue

                trades = simulate_market_with_entry_method(
                    btc_spikes, obs_df, slug, resolution, ds_config['use_obi'], dataset_key, config
                )
                all_trades.extend(trades)

            # Calculate metrics
            if all_trades:
                total_pnl_net = sum(t.pnl_net for t in all_trades)
                total_pnl_gross = sum(t.pnl_gross for t in all_trades)
                total_entry_fees = sum(t.entry_fee for t in all_trades)
                total_exit_fees = sum(t.exit_fee for t in all_trades)
                total_trades = len(all_trades)
                win_rate = sum(1 for t in all_trades if t.correct_direction) / total_trades * 100
                avg_pair_cost = np.mean([t.pair_cost for t in all_trades])
                passive_pct = sum(1 for t in all_trades if t.hedge_type == "passive") / total_trades * 100
                maker_entry_pct = sum(1 for t in all_trades if t.is_maker_entry) / total_trades * 100
                hourly_rate = total_pnl_net / hours if hours > 0 else 0

                result = {
                    'config': config.name,
                    'entry_method': config.entry_method,
                    'time_stop': config.time_stop_seconds,
                    'dataset': dataset_key,
                    'hours': hours,
                    'trades': total_trades,
                    'pnl_net': total_pnl_net,
                    'pnl_gross': total_pnl_gross,
                    'entry_fees': total_entry_fees,
                    'exit_fees': total_exit_fees,
                    'hourly_rate': hourly_rate,
                    'win_rate': win_rate,
                    'avg_pair_cost': avg_pair_cost,
                    'passive_pct': passive_pct,
                    'maker_entry_pct': maker_entry_pct,
                }
                config_results.append(result)
                all_results.append(result)

                print(f"    Trades: {total_trades}, $/hr: ${hourly_rate:.2f}, Win%: {win_rate:.1f}%, Maker%: {maker_entry_pct:.1f}%")

        # Checkpoint save after each config
        if all_results:
            pd.DataFrame(all_results).to_csv(checkpoint_path, index=False)

    elapsed = time.time() - start_time
    print(f"\n\nCompleted in {elapsed / 60:.1f} minutes")

    return all_results


def print_results(results: List[dict]):
    """Print comprehensive results summary."""
    df = pd.DataFrame(results)

    print("\n" + "=" * 100)
    print("ENTRY SPIKE MAGNITUDE TEST RESULTS")
    print("=" * 100)

    # Per-dataset breakdown
    for dataset in DATASETS.keys():
        ds_df = df[df['dataset'] == dataset]
        if len(ds_df) == 0:
            continue

        hours = ds_df['hours'].iloc[0]
        print(f"\n{'='*80}")
        print(f"Dataset: {dataset} ({hours:.1f}h, OBI {'ON' if DATASETS[dataset]['use_obi'] else 'OFF'})")
        print(f"{'='*80}")
        print()
        print(f"{'Config':<25} {'Trades':>8} {'$/hr':>10} {'Win%':>8} {'Passive%':>10} {'Maker%':>10} {'Avg Pair':>10}")
        print("-" * 90)

        ds_df_sorted = ds_df.sort_values('hourly_rate', ascending=False)
        for _, row in ds_df_sorted.iterrows():
            print(f"{row['config']:<25} {row['trades']:>8} ${row['hourly_rate']:>9.2f} "
                  f"{row['win_rate']:>7.1f}% {row['passive_pct']:>9.1f}% "
                  f"{row['maker_entry_pct']:>9.1f}% ${row['avg_pair_cost']:>9.4f}")

    # Combined results
    print("\n" + "=" * 100)
    print("COMBINED RESULTS (All Datasets)")
    print("=" * 100)

    combined = df.groupby('config').agg({
        'trades': 'sum',
        'pnl_net': 'sum',
        'hours': 'sum',
        'win_rate': 'mean',
        'passive_pct': 'mean',
        'maker_entry_pct': 'mean',
        'avg_pair_cost': 'mean',
    }).reset_index()

    combined['hourly_rate'] = combined['pnl_net'] / combined['hours']
    combined = combined.sort_values('hourly_rate', ascending=False)

    print()
    print(f"{'Config':<25} {'Trades':>8} {'$/hr':>10} {'Win%':>8} {'Passive%':>10} {'Maker%':>10} {'Winner':>8}")
    print("-" * 90)

    best_rate = combined['hourly_rate'].max()
    for _, row in combined.iterrows():
        winner = "  *" if row['hourly_rate'] == best_rate else ""
        print(f"{row['config']:<25} {row['trades']:>8} ${row['hourly_rate']:>9.2f} "
              f"{row['win_rate']:>7.1f}% {row['passive_pct']:>9.1f}% "
              f"{row['maker_entry_pct']:>9.1f}% {winner:>8}")

    # Parameter sensitivity
    print("\n" + "=" * 100)
    print("PARAMETER SENSITIVITY ANALYSIS")
    print("=" * 100)

    # Entry method
    print("\nEntry Method:")
    for method in ENTRY_METHODS:
        method_df = df[df['entry_method'] == method]
        if len(method_df) > 0:
            total_pnl = method_df['pnl_net'].sum()
            total_hours = method_df['hours'].sum()
            avg_rate = total_pnl / total_hours
            avg_maker = method_df['maker_entry_pct'].mean()
            print(f"  {method:>15}: ${avg_rate:.2f}/hr, {avg_maker:.1f}% maker entries")

    # Time-stop
    print("\nTime-Stop:")
    for ts in TIME_STOPS:
        ts_df = df[df['time_stop'] == ts]
        if len(ts_df) > 0:
            total_pnl = ts_df['pnl_net'].sum()
            total_hours = ts_df['hours'].sum()
            avg_rate = total_pnl / total_hours
            avg_passive = ts_df['passive_pct'].mean()
            print(f"  {int(ts):>4}s: ${avg_rate:.2f}/hr, {avg_passive:.1f}% passive")

    # Save final results
    output_path = Path("research/findings/data/entry_magnitude_results.csv")
    df.to_csv(output_path, index=False)
    print(f"\nResults saved to: {output_path}")


def main():
    print("=" * 80)
    print("ENTRY SPIKE MAGNITUDE TEST")
    print("Config from TRADING_CONFIGS.py - OU ADAPTIVE threshold")
    print("=" * 80)
    print()
    print("Testing 4 entry methods:")
    print("  1. TAKER: Always buy at ask (current behavior)")
    print("  2. MAKER_RAW: bid = winner_bid + spike_magnitude")
    print("  3. MAKER_OU: bid = winner_bid + spike_magnitude * (1 + z_score)")
    print("  4. MAKER_THRESH: bid = winner_bid + spike_magnitude / threshold")
    print()
    print("Time-stops: 20s, 60s, 120s, 180s (120s = current AGGRESSIVE config)")
    print()

    results = run_grid_search()

    if results:
        print_results(results)


if __name__ == "__main__":
    main()
