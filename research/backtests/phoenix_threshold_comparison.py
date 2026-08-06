#!/usr/bin/env python3
"""
PHOENIX Threshold Comparison — OU vs Regime (Fast/Slow Vol Crossover)

=============================================================================
COPIED FROM: phoenix_main_backtest.py (validated PHOENIX backtest)
Execution engine: IDENTICAL. Maker fills, 0ms delay, 0% fee.
=============================================================================

Tests RegimeThreshold (fast/slow vol crossover with discrete regime thresholds)
against OU adaptive sigmoid threshold across all 6 PHOENIX datasets.

Regime threshold is self-calibrating with NO fitted parameters.
OU threshold uses sigmoid mapping calibrated on Jan IS+OOS2 data (~69hr).

Usage:
    python research/backtests/phoenix_threshold_comparison.py --data all
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
import sys
import math
import argparse
from datetime import datetime
from itertools import product
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# =============================================================================
# IMPORTS - FROM src/core (Single Source of Truth)
# =============================================================================
from src.core import (
    polymarket_taker_fee,
    calculate_pnl_with_fees,
)

# =============================================================================
# CONSTANTS
# =============================================================================
STARTING_CAPITAL = 170.0
MAX_CAPITAL_FRACTION = 0.50
UNHEDGED_FRACTION = 0.25
DEFAULT_COOLDOWN_SECONDS = 10  # Now a grid parameter in V3
EWMA_HALFLIFE_MS = 1000
SKIP_UTC_HOURS = frozenset()  # Disabled by default. Use --hours to enable {3,4,8,14,20}
SKIP_UTC_HOURS_ENABLED = frozenset({3, 4, 8, 14, 20})
PANIC_TIME_SECS = 60  # When to activate panic hedge (last 60s of market)

# OU ADAPTIVE THRESHOLD params (calibrated on IS+OOS2)
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10

# EWMA THRESHOLD params (self-calibrating, no fitted params)
EWMA_MIN_THRESHOLD = 0.005
EWMA_MAX_THRESHOLD = 0.10
EWMA_K_VALUES = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0]

# REGIME THRESHOLD params (fast/slow vol crossover, fully self-calibrating)
REGIME_FAST_WINDOW = 300    # ~5s at 60Hz
REGIME_SLOW_WINDOW = 3600   # ~60s at 60Hz
REGIME_BASE_THRESHOLDS = {
    'CALM':   0.008,   # fast_vol < 0.5 * slow_vol
    'NORMAL': 0.015,   # 0.5 <= ratio <= 1.5
    'ACTIVE': 0.025,   # 1.5 < ratio <= 3.0
    'SPIKE':  0.050,   # ratio > 3.0
}
REGIME_BOUNDS = (0.5, 1.5)  # (low_ratio, high_ratio)
REGIME_SCALE_VALUES = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]

# Deceleration windows to precompute
DECEL_WINDOWS = [(600, 180), (600, 120), (300, 180), (300, 120)]


# =============================================================================
# DATA CLASSES
# =============================================================================
@dataclass
class PhoenixConfig:
    name: str
    # Bias
    expensive_threshold: float = 0.80
    # Entry timing
    entry_start_secs: float = 300.0
    entry_end_secs: float = 120.0
    # Entry execution
    entry_offset: float = 0.02
    max_fill_price: float = 1.0    # Cap fill price — skip entries above this (ensures hedgeability)
    # Deceleration filter
    decel_required: bool = False
    # Hedge
    hedge_offset: float = 0.02
    max_pair_cost: float = 0.96
    # Escalating hedge: relax pair cost over time (0=disabled, use single max_pair_cost)
    # When enabled: hedge bid starts at max_pair_cost, escalates to escalate_pc_final
    # over escalate_secs seconds after entry fill
    escalate_pc_final: float = 0.0   # Final relaxed pair cost (0=disabled)
    escalate_secs: float = 60.0      # Seconds over which to escalate
    # Sizing
    base_shares: int = 25
    # Cycling: max independent entries per market (V2 feature)
    max_entries_per_market: int = 99
    # V3: Improvement parameters
    decel_boost: float = 1.0      # Idea 7: multiply shares when decel detected (1.0=off)
    taper_factor: float = 1.0     # Idea 2: entry N gets base_shares * taper^(N-1) (1.0=off)
    cooldown_secs: int = 10       # Idea 3: seconds between spikes (was fixed at 10)
    panic_max_pc: float = 0.0     # Panic hedge: relaxed pair cost for late hedge (0=disabled)
    # Patient hedge: bid at fixed low price regardless of entry price
    # When enabled, replaces max_pair_cost logic. Hedge fills via convergence to $0.
    patient_hedge: bool = False
    patient_bid: float = 0.0  # Fixed hedge bid (0 = auto-calculate min viable: $1/shares)
    resolution_fills: bool = False  # Use resolution-based hedge fills (for non-patient too)
    # Session stop (ADAPT25)
    adapt_trades: int = 25
    adapt_threshold: float = -5.0
    max_drawdown_pct: float = 0.20


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
    decel_present: bool
    entry_number: int  # V2: which entry in this market (1, 2, 3...)
    dataset: str
    config_name: str


# =============================================================================
# OU ADAPTIVE THRESHOLD (identical to FADE grid search)
# =============================================================================
@dataclass
class OUParams:
    mu: float = -3.9845
    sigma_stat: float = 0.3877


def load_ou_params() -> OUParams:
    return OUParams()


def compute_ou_threshold(volatility: float, ou_params: OUParams) -> Tuple[float, float]:
    log_vol = math.log(max(volatility, 1e-6))
    z_score = (log_vol - ou_params.mu) / ou_params.sigma_stat
    z_clamped = max(-10, min(10, z_score * OU_SIGMOID_STEEPNESS))
    sigmoid = 1.0 / (1.0 + math.exp(-z_clamped))
    multiplier = OU_K_LOW + (OU_K_HIGH - OU_K_LOW) * sigmoid
    threshold = OU_BASE_THRESHOLD * multiplier
    return max(OU_MIN_THRESHOLD, min(OU_MAX_THRESHOLD, threshold)), z_score


# =============================================================================
# SPIKE DETECTION - EWMA (identical to FADE grid search)
# =============================================================================
def precompute_spikes_ewma(btc_df: pd.DataFrame, halflife_ms: int = EWMA_HALFLIFE_MS,
                           threshold_method: str = "ou", k_multiplier: float = 2.5,
                           regime_scale: float = 1.0) -> pd.DataFrame:
    """EWMA spike detection with configurable threshold method.

    threshold_method:
        "ou"     — OU adaptive sigmoid (original, uses fitted params from Jan data)
        "ewma"   — Pure EWMA: threshold = k * sqrt(ewma_variance). Self-calibrating.
        "regime" — Fast/slow vol crossover: discrete regime thresholds. Self-calibrating.
    """
    halflife_ticks = halflife_ms / 16.67
    alpha = 1 - 0.5 ** (1.0 / halflife_ticks)
    gap_threshold_ms = 30 * 60 * 1000

    if threshold_method == "ou":
        method_label = "OU"
    elif threshold_method == "ewma":
        method_label = f"EWMA_k{k_multiplier}"
    else:
        method_label = f"REGIME_x{regime_scale}"
    print(f"    [{method_label}] Half-life={halflife_ms}ms, α={alpha:.4f}")

    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    original_len = len(df)
    df = df.drop_duplicates(subset=['timestamp_ms'], keep='first').reset_index(drop=True)
    if len(df) < original_len:
        print(f"    [{method_label}] Deduplicated: {original_len:,} → {len(df):,} rows")

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

    returns = df['price'].pct_change() * 100
    thresholds = []
    z_scores = []

    if threshold_method == "ou":
        # OU adaptive sigmoid (fitted params)
        ewma_halflife = 300
        var_alpha = 1 - 0.5 ** (1.0 / ewma_halflife)
        variance = returns.iloc[:60].var() if len(returns) > 60 else 0.01
        ou_params = load_ou_params()
        for i, r in enumerate(returns):
            if pd.isna(r):
                thresholds.append(OU_BASE_THRESHOLD)
                z_scores.append(0.0)
                continue
            variance = var_alpha * (r ** 2) + (1 - var_alpha) * variance
            vol = max(np.sqrt(variance), 1e-6)
            threshold, z_score = compute_ou_threshold(vol, ou_params)
            thresholds.append(threshold)
            z_scores.append(z_score)

    elif threshold_method == "ewma":
        # Pure EWMA threshold: threshold = k * vol (self-calibrating)
        ewma_halflife = 300
        var_alpha = 1 - 0.5 ** (1.0 / ewma_halflife)
        variance = returns.iloc[:60].var() if len(returns) > 60 else 0.01
        for i, r in enumerate(returns):
            if pd.isna(r):
                thresholds.append(EWMA_MIN_THRESHOLD)
                z_scores.append(0.0)
                continue
            variance = var_alpha * (r ** 2) + (1 - var_alpha) * variance
            vol = max(np.sqrt(variance), 1e-6)
            threshold = max(EWMA_MIN_THRESHOLD, min(EWMA_MAX_THRESHOLD, k_multiplier * vol))
            thresholds.append(threshold)
            z_scores.append(0.0)

    elif threshold_method == "regime":
        # Fast/slow vol crossover with regime-based thresholds
        # VECTORIZED using pandas rolling.std (C-optimized) — O(N) not O(N*W)
        fast_win = REGIME_FAST_WINDOW
        slow_win = REGIME_SLOW_WINDOW
        low_ratio, high_ratio = REGIME_BOUNDS

        # Scale the regime thresholds
        calm_t = REGIME_BASE_THRESHOLDS['CALM'] * regime_scale
        normal_t = REGIME_BASE_THRESHOLDS['NORMAL'] * regime_scale
        active_t = REGIME_BASE_THRESHOLDS['ACTIVE'] * regime_scale
        spike_t = REGIME_BASE_THRESHOLDS['SPIKE'] * regime_scale

        # Vectorized rolling vol (pandas C-optimized rolling std)
        returns_clean = returns.fillna(0.0)
        fast_vol = returns_clean.rolling(fast_win, min_periods=fast_win).std()
        slow_vol = returns_clean.rolling(slow_win, min_periods=slow_win).std()

        # Ratio: fast_vol / slow_vol (default 1.0 when slow_vol unavailable)
        slow_vol_arr = slow_vol.values
        fast_vol_arr = fast_vol.values
        ratio_arr = np.where(
            np.isnan(slow_vol_arr) | (slow_vol_arr < 1e-10),
            1.0,
            fast_vol_arr / slow_vol_arr
        )

        # Classify regime (vectorized)
        threshold_arr = np.full(len(returns), normal_t)  # default = NORMAL
        threshold_arr[ratio_arr < low_ratio] = calm_t
        threshold_arr[(ratio_arr > high_ratio) & (ratio_arr <= high_ratio * 2)] = active_t
        threshold_arr[ratio_arr > high_ratio * 2] = spike_t

        # Before warm-up (fast_vol is NaN): force NORMAL
        threshold_arr[:fast_win] = normal_t

        thresholds = threshold_arr.tolist()
        z_scores = np.where(np.isnan(ratio_arr), 0.0, ratio_arr).tolist()

    df['threshold'] = thresholds
    df['z_score'] = z_scores
    df['spike_detected'] = df['spike_magnitude'] >= df['threshold']

    spike_count = df['spike_detected'].sum()
    print(f"    [{method_label}] Found {spike_count:,} spikes")

    return df


# =============================================================================
# DECELERATION DETECTION (from accel_velocity_signal_test.py)
# =============================================================================
def compute_deceleration(vel_arr: np.ndarray, time_rem_arr: np.ndarray,
                         entry_start: float, entry_end: float) -> bool:
    """
    Check if velocity decelerates in entry window (>30% magnitude drop).
    Uses numpy arrays directly for speed.
    """
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
# MARKET DATA PRECOMPUTATION (run once per dataset)
# =============================================================================
def precompute_markets(obs_df: pd.DataFrame, spike_ts_all: np.ndarray,
                       spike_mag_all: np.ndarray, resolutions: Dict[str, str]) -> Dict:
    """
    Pre-extract numpy arrays per market for fast per-config simulation.

    Precomputes:
    - Sorted observation arrays (timestamp, up_ask, down_ask, time_remaining, velocity)
    - UTC hour at each observation
    - Spike indices mapped to nearest observation
    - Deceleration flags for all possible entry windows
    """
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
        time_rem = mdf['time_remaining_secs'].values.astype(float)
        vel = mdf['velocity_bps'].fillna(0).values.astype(float) if 'velocity_bps' in mdf.columns else np.zeros(n)

        # UTC hours (vectorized)
        hours = pd.to_datetime(ts, unit='ms', utc=True).hour.values

        # Map spikes: binary search for spikes in this market's time range
        start_idx = np.searchsorted(spike_ts_all, ts[0])
        end_idx = np.searchsorted(spike_ts_all, ts[-1], side='right')
        m_spike_ts = spike_ts_all[start_idx:end_idx]
        m_spike_mag = spike_mag_all[start_idx:end_idx]

        # Map each spike to nearest observation (last obs at or before spike time)
        m_spike_obs_idx = np.searchsorted(ts, m_spike_ts, side='right') - 1
        m_spike_obs_idx = np.clip(m_spike_obs_idx, 0, n - 1)

        # Precompute deceleration for each possible window
        decel = {}
        for start, end in DECEL_WINDOWS:
            decel[(start, end)] = compute_deceleration(vel, time_rem, start, end)

        market_data[slug] = {
            'resolution': resolutions[slug],
            'n': n,
            'ts': ts,
            'up_ask': up_ask,
            'down_ask': down_ask,
            'time_rem': time_rem,
            'hours': hours,
            'spike_ts': m_spike_ts,
            'spike_mag': m_spike_mag,
            'spike_obs_idx': m_spike_obs_idx,
            'decel': decel,
        }

    return market_data


# =============================================================================
# FAST MARKET SIMULATION — V3 IMPROVEMENTS (numpy-optimized)
# =============================================================================
#
# V3 improvements over V2:
# [BUG FIX] Capital tracking: actual cost spent, not shares * current price
# [IDEA 7] Decel boost: decel_boost multiplier on shares when decel detected
# [IDEA 2] Taper: entry N gets base_shares * taper^(N-1)
# [IDEA 3] Cooldown: config.cooldown_secs (was hardcoded 10s)
# [PANIC] Late hedge: relax pair cost to panic_max_pc in final 60s
#
# ================================================================
def simulate_market_fast(
    slug: str,
    md: Dict,
    config: PhoenixConfig,
    dataset_name: str,
    current_balance: float,
) -> List[TradeResult]:
    """
    Fast PHOENIX V3 simulation — V2 cycling + improvements.

    Changes from V2:
    - BUG FIX: track total_capital_deployed as actual $ cost
    - Idea 7: decel_boost multiplies shares when decel is detected for market
    - Idea 2: taper_factor reduces shares for later entries
    - Idea 3: cooldown_secs is configurable
    - Panic hedge: relax pair cost in final 60s for unhedged positions

    Execution engine matches paper_trading.py (UNCHANGED):
    - Maker fill: price-touch when ask <= our_bid, 0ms delay, 0% fee
    """
    resolution = md['resolution']
    ts = md['ts']
    up_ask = md['up_ask']
    down_ask = md['down_ask']
    time_rem = md['time_rem']
    hours = md['hours']
    spike_ts = md['spike_ts']
    spike_mag = md['spike_mag']
    spike_obs_idx = md['spike_obs_idx']

    if len(spike_ts) == 0:
        return []

    # Deceleration check (precomputed — used for boost, not filtering)
    window_key = (config.entry_start_secs, config.entry_end_secs)
    decel_detected = md['decel'].get(window_key, False)
    if config.decel_required and not decel_detected:
        return []

    # Capital constraint
    max_per_market = current_balance * MAX_CAPITAL_FRACTION
    cooldown_ms = config.cooldown_secs * 1000  # V3: configurable cooldown

    trades = []
    entries = 0
    total_capital_deployed = 0.0  # V3 BUG FIX: track actual $ cost, not shares * current price
    last_signal_ts = 0

    for si in range(len(spike_ts)):
        if entries >= config.max_entries_per_market:
            break

        oi = spike_obs_idx[si]
        tr = time_rem[oi]

        # Entry window check
        if tr > config.entry_start_secs or tr < config.entry_end_secs:
            continue

        # Cooldown (V3: configurable)
        if spike_ts[si] - last_signal_ts < cooldown_ms:
            continue

        # Hour filter
        if hours[oi] in SKIP_UTC_HOURS:
            continue

        # Expensive side from current prices (re-evaluate each spike independently)
        ua, da = up_ask[oi], down_ask[oi]
        if np.isnan(ua) or np.isnan(da) or ua <= 0 or da <= 0:
            continue

        if ua >= da:
            exp_ask = ua
            exp_side = "UP"
            entry_asks = up_ask    # For fill checking on entry side
            hedge_asks = down_ask   # For fill checking on hedge side
        else:
            exp_ask = da
            exp_side = "DOWN"
            entry_asks = down_ask
            hedge_asks = up_ask

        # Bias threshold
        if exp_ask < config.expensive_threshold:
            continue

        # SIGNAL PASSED
        last_signal_ts = spike_ts[si]
        entry_bid = max(0.01, exp_ask - config.entry_offset)

        # V4: Cap fill price to ensure hedgeability
        if entry_bid > config.max_fill_price:
            continue

        # ------- ENTRY FILL CHECK (numpy vectorized) -------
        if oi + 1 >= len(entry_asks):
            continue
        entry_slice = entry_asks[oi + 1:]
        fill_mask = entry_slice <= entry_bid
        fill_indices = np.where(fill_mask)[0]

        if len(fill_indices) == 0:
            continue  # No fill

        fill_local = fill_indices[0]
        fill_global = oi + 1 + fill_local
        fill_price = entry_bid  # Maker fills at our bid price

        # ------- SHARE SIZING (V3: taper + decel boost + capital cap) -------
        # Idea 2: Taper — later entries get fewer shares
        tapered_shares = int(config.base_shares * (config.taper_factor ** entries))
        if tapered_shares <= 0:
            break  # Tapered to zero

        # Idea 7: Decel boost — more shares when decel detected (higher conviction)
        if decel_detected and config.decel_boost > 1.0:
            boosted_shares = int(tapered_shares * config.decel_boost)
        else:
            boosted_shares = tapered_shares

        # V3 BUG FIX: use actual capital deployed, not shares * current price
        remaining_capital = max_per_market - total_capital_deployed
        if remaining_capital <= 0:
            break  # Market capital exhausted
        max_affordable = int(remaining_capital / fill_price) if fill_price > 0 else 0
        shares = min(boosted_shares, max_affordable)
        if shares <= 0:
            break  # Can't afford any more shares

        entries += 1
        total_capital_deployed += shares * fill_price  # V3: track actual cost

        # ------- HEDGE FILL CHECK (numpy vectorized) -------
        # V4: ESCALATING HEDGE — start tight, relax over time
        # Phase 1 (0 to escalate_secs): bid at max_pair_cost - fill_price
        # Phase 2 (after escalate_secs): bid at escalate_pc_final - fill_price
        # This gets best price early, ensures hedge later
        is_hedged = False
        hedge_price = None

        if fill_global + 1 < len(hedge_asks):
            hedge_slice = hedge_asks[fill_global + 1:]
            fill_time_ms = ts[fill_global]

            if config.patient_hedge:
                # --- PATIENT HEDGE MODE (Resolution-based) ---
                # Binary market truth: if expensive side wins, cheap side → $0.
                # Cheap side MUST pass through any positive bid on the way to $0.
                # So hedge fills IFF we predicted correctly (resolution == exp_side).
                if config.patient_bid > 0:
                    hedge_bid = config.patient_bid
                else:
                    hedge_bid = math.ceil(1.0 / shares * 100) / 100  # min viable: $1/shares

                # Safety: skip hedge if pair_cost >= max_pair_cost (would lock in unacceptable loss)
                pair_cost_if_hedged = fill_price + hedge_bid
                if hedge_bid >= 0.01 and pair_cost_if_hedged <= config.max_pair_cost and resolution == exp_side:
                    # We're correct + profitable pair → hedge fills
                    is_hedged = True
                    hedge_price = hedge_bid

            elif config.escalate_pc_final > 0 and config.escalate_pc_final > config.max_pair_cost:
                # --- ESCALATING MODE: try tight bid first, then relax ---
                time_after = ts[fill_global + 1:]
                elapsed_secs = (time_after - fill_time_ms) / 1000.0

                # Phase 1: tight bid (first escalate_secs after fill)
                tight_bid_max = config.max_pair_cost - fill_price
                cheap_at_fill = hedge_asks[fill_global]
                if np.isnan(cheap_at_fill):
                    tight_bid = tight_bid_max
                else:
                    tight_bid = min(cheap_at_fill - config.hedge_offset, tight_bid_max)

                if tight_bid >= 0.01:
                    phase1_mask = (elapsed_secs <= config.escalate_secs) & (hedge_slice <= tight_bid)
                    phase1_idx = np.where(phase1_mask)[0]
                    if len(phase1_idx) > 0:
                        is_hedged = True
                        hedge_price = tight_bid

                # Phase 2: relaxed bid (after escalate_secs)
                if not is_hedged:
                    relaxed_bid_max = config.escalate_pc_final - fill_price
                    if np.isnan(cheap_at_fill):
                        relaxed_bid = relaxed_bid_max
                    else:
                        relaxed_bid = min(cheap_at_fill - config.hedge_offset, relaxed_bid_max)

                    if relaxed_bid >= 0.01:
                        phase2_mask = (elapsed_secs > config.escalate_secs) & (hedge_slice <= relaxed_bid)
                        phase2_idx = np.where(phase2_mask)[0]
                        if len(phase2_idx) > 0:
                            is_hedged = True
                            hedge_price = relaxed_bid
            else:
                # --- ORIGINAL MODE: single bid for entire lifetime ---
                cheap_at_fill = hedge_asks[fill_global]
                if np.isnan(cheap_at_fill):
                    hedge_bid = config.max_pair_cost - fill_price
                else:
                    hedge_bid_raw = cheap_at_fill - config.hedge_offset
                    hedge_bid_max = config.max_pair_cost - fill_price
                    hedge_bid = min(hedge_bid_raw, hedge_bid_max)

                if hedge_bid >= 0.01:
                    if config.resolution_fills:
                        # Resolution-based: hedge fills IFF we're correct AND pair_cost ok
                        pair_cost_if_hedged = fill_price + hedge_bid
                        if pair_cost_if_hedged <= config.max_pair_cost and resolution == exp_side:
                            is_hedged = True
                            hedge_price = hedge_bid
                    else:
                        # Observer-based: price-touch fill
                        hedge_fill_mask = hedge_slice <= hedge_bid
                        hedge_indices = np.where(hedge_fill_mask)[0]
                        if len(hedge_indices) > 0:
                            is_hedged = True
                            hedge_price = hedge_bid

                # --- PANIC HEDGE (V3: relax pair cost in final 60s) ---
                if not is_hedged and config.panic_max_pc > 0:
                    panic_bid = config.panic_max_pc - fill_price
                    if panic_bid >= 0.01:
                        time_rem_after = time_rem[fill_global + 1:]
                        panic_mask = time_rem_after < PANIC_TIME_SECS
                        if panic_mask.any():
                            panic_fill_mask = panic_mask & (hedge_slice <= panic_bid)
                            if panic_fill_mask.any():
                                is_hedged = True
                                hedge_price = panic_bid

        # ------- PnL CALCULATION -------
        if is_hedged:
            pair_cost = fill_price + hedge_price
            pnl = (1.0 - pair_cost) * shares
        else:
            if resolution == exp_side:
                pnl = (1.0 - fill_price) * shares
            else:
                pnl = -fill_price * shares

        trades.append(TradeResult(
            market_slug=slug,
            entry_time_remaining=tr,
            expensive_side=exp_side,
            entry_price=fill_price,
            hedge_price=hedge_price,
            pair_cost=(fill_price + hedge_price) if is_hedged else None,
            is_hedged=is_hedged,
            pnl_gross=pnl,
            pnl_net=pnl,  # ALL MAKER = 0% fees
            correct_direction=(resolution == exp_side),
            shares=shares,
            spike_magnitude=spike_mag[si],
            decel_present=decel_detected,
            entry_number=entries,
            dataset=dataset_name,
            config_name=config.name,
        ))

    return trades


# =============================================================================
# DATASETS (identical to FADE grid search)
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
        "obs_files": [
            "research/observer/PROTECTED_grid_obs_oos3_oos4_combined.csv",
        ],
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
        "obs_files": [
            "research/observer/grid_obs_20260131.csv",
        ],
        "res_files": ["research/observer/resolutions_20260131.csv"],
    },
    "OOS9": {
        "name": "OOS9 (Feb 1-3)",
        "btc_file": "research/binance_hf/btc_prices_oos9.csv",
        "obs_files": [
            "research/observer/grid_obs_oos9.csv",
        ],
        "res_files": [
            "research/observer/resolutions_oos9_1.csv",
            "research/observer/resolutions_oos9_2.csv",
        ],
    },
    "OOS10": {
        "name": "OOS10 (Feb 5)",
        "btc_file": "research/binance_hf/btc_prices_20260204_190733.csv",
        "obs_files": [
            "research/observer/grid_obs_20260205.csv",
        ],
        "res_files": ["research/observer/resolutions_20260205.csv"],
    },
}

TRAIN_DATASETS = ["IS+OOS2", "OOS7", "OOS8", "OOS9"]
VALIDATION_DATASETS = ["OOS3+4"]


def load_dataset(dataset_key: str):
    """Load a dataset (identical to FADE grid search)."""
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
    print(f"  Combined observer: {len(obs_df):,} rows")

    btc_path = base_dir / config['btc_file']
    if btc_path.exists():
        btc_df = pd.read_csv(btc_path)
        print(f"  Binance HF: {len(btc_df):,} rows")
    else:
        print(f"  Binance HF: NOT FOUND")
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
            print(f"  {Path(res_fname).name}: {len(res_df)} resolutions")
    print(f"  Total resolutions: {len(resolutions)} markets")

    duration_hours = (obs_df['timestamp_ms'].max() - obs_df['timestamp_ms'].min()) / (1000 * 3600)
    print(f"  Duration: {duration_hours:.2f} hours")

    return obs_df, btc_df, resolutions, duration_hours


# =============================================================================
# SESSION STOP — ADAPT25
# =============================================================================
@dataclass
class SessionResult:
    trades: List[TradeResult]
    session_stopped: bool
    trades_before_stop: int
    final_session_pnl: float
    session_peak_pnl: float
    stop_reason: Optional[str]
    adaptive_activated: bool = False
    pnl_at_check: Optional[float] = None


def run_backtest_with_session_stops(
    config: PhoenixConfig,
    market_data: Dict,
    markets_ordered: List[str],
    dataset_name: str,
) -> SessionResult:
    """
    Run PHOENIX backtest with session-level ADAPT25 stop.
    Uses precomputed market data for speed.
    """
    session_pnl = 0.0
    session_peak_pnl = 0.0
    session_stopped = False
    stop_reason = None
    all_trades = []
    trade_count = 0
    current_balance = STARTING_CAPITAL

    adaptive_activated = False
    adaptive_checked = False
    pnl_at_check = None

    for slug in markets_ordered:
        if session_stopped:
            break
        if slug not in market_data:
            continue

        md = market_data[slug]
        market_trades = simulate_market_fast(
            slug, md, config, dataset_name, current_balance,
        )

        for trade in market_trades:
            session_pnl += trade.pnl_net
            session_peak_pnl = max(session_peak_pnl, session_pnl)
            current_balance = STARTING_CAPITAL + session_pnl
            trade_count += 1
            all_trades.append(trade)

            # Adaptive check
            if not adaptive_checked and trade_count >= config.adapt_trades:
                adaptive_checked = True
                pnl_at_check = session_pnl
                if session_pnl < config.adapt_threshold:
                    adaptive_activated = True

            # Session stop (only if adaptive activated)
            if adaptive_activated:
                dd = (session_peak_pnl - session_pnl) / STARTING_CAPITAL
                if dd >= config.max_drawdown_pct:
                    session_stopped = True
                    stop_reason = "adaptive_dd"
                    break

    return SessionResult(
        trades=all_trades,
        session_stopped=session_stopped,
        trades_before_stop=trade_count if session_stopped else len(all_trades),
        final_session_pnl=session_pnl,
        session_peak_pnl=session_peak_pnl,
        stop_reason=stop_reason,
        adaptive_activated=adaptive_activated,
        pnl_at_check=pnl_at_check,
    )


# =============================================================================
# GRID CONFIGS
# =============================================================================
def generate_grid_configs(quick: bool = False) -> List[PhoenixConfig]:
    """
    V3 Grid: Test improvement ideas around V2 winning config.

    V2 winner: T80_W300-120_O2_ND_PC96_S25_C99
    Fixed from V2: T80, W300-120, PC96, ND, hedge_offset=0.02

    V3 tests (improvement ideas):
    - entry_offset: [0.02, 0.03]  (V2 winner + runner-up)
    - max_entries_per_market: [5, 99]  (C5 ≈ C99 in V2, test interaction)
    - decel_boost: [1.0, 1.5, 2.0]  (Idea 7: boost shares on decel)
    - taper_factor: [1.0, 0.75, 0.5]  (Idea 2: reduce later entry shares)
    - cooldown_secs: [5, 10, 15]  (Idea 3: tune cooldown)
    - panic_max_pc: [0.0, 1.00, 1.05]  (Panic hedge: 0=off, 1.0=breakeven, 1.05=loss cap)
    """
    if quick:
        grid = {
            "entry_offset": [0.02],
            "max_entries_per_market": [99],
            "decel_boost": [1.0, 1.5],
            "taper_factor": [1.0, 0.75],
            "cooldown_secs": [5, 10],
            "panic_max_pc": [0.0, 1.00],
        }
    else:
        grid = {
            "entry_offset": [0.02, 0.03],
            "max_entries_per_market": [5, 99],
            "decel_boost": [1.0, 1.5, 2.0],
            "taper_factor": [1.0, 0.75, 0.5],
            "cooldown_secs": [5, 10, 15],
            "panic_max_pc": [0.0, 1.00, 1.05],
        }

    # Fixed params from V2 winner
    fixed = {
        "expensive_threshold": 0.80,
        "entry_start_secs": 300,
        "entry_end_secs": 120,
        "decel_required": False,
        "hedge_offset": 0.02,
        "max_pair_cost": 0.96,
        "base_shares": 25,
    }

    configs = []
    keys = list(grid.keys())
    values = list(grid.values())

    for combo in product(*values):
        params = dict(zip(keys, combo))
        name_parts = [
            f"O{int(params['entry_offset']*100)}",
            f"C{params['max_entries_per_market']}",
            f"DB{params['decel_boost']:.1f}",
            f"TF{params['taper_factor']:.2f}",
            f"CD{params['cooldown_secs']}",
            f"PH{params['panic_max_pc']:.2f}",
        ]
        configs.append(PhoenixConfig(
            name="_".join(name_parts),
            expensive_threshold=fixed['expensive_threshold'],
            entry_start_secs=fixed['entry_start_secs'],
            entry_end_secs=fixed['entry_end_secs'],
            entry_offset=params['entry_offset'],
            decel_required=fixed['decel_required'],
            hedge_offset=fixed['hedge_offset'],
            max_pair_cost=fixed['max_pair_cost'],
            base_shares=fixed['base_shares'],
            max_entries_per_market=params['max_entries_per_market'],
            decel_boost=params['decel_boost'],
            taper_factor=params['taper_factor'],
            cooldown_secs=params['cooldown_secs'],
            panic_max_pc=params['panic_max_pc'],
        ))

    return configs


# =============================================================================
# METRICS (enhanced with hedge tracking)
# =============================================================================
def calculate_metrics(
    trades: List[TradeResult],
    duration_hours: float,
    config: PhoenixConfig,
    session_result: Optional[SessionResult] = None,
) -> Dict:
    if not trades:
        return {
            "trades": 0, "total_pnl": 0, "pnl_per_hr": 0, "sharpe": 0,
            "roi_pct": 0, "win_rate": 0, "fade_accuracy": 0,
            "profitable_mkts_pct": 0, "max_drawdown_pct": 0,
            "avg_pnl_per_trade": 0, "hedge_rate": 0, "avg_pair_cost": 0,
            "unhedged_pct": 0, "worst_trade_loss": 0, "worst_market_loss": 0,
            "markets_traded": 0, "session_stopped": False,
            "adaptive_activated": False, "trades_before_stop": 0,
            "ending_balance": STARTING_CAPITAL, "stop_reason": None,
            "pnl_at_check": None, "cycling_trades": 0,
            "max_entry_num": 0, "avg_entries_per_market": 0,
            "markets_with_cycling": 0,
        }

    pnls = [t.pnl_net for t in trades]
    total_pnl = sum(pnls)

    # Sharpe (annualized from hourly)
    if len(pnls) > 1 and np.std(pnls) > 0:
        sharpe = (np.mean(pnls) / np.std(pnls)) * np.sqrt(252 * 24)
    else:
        sharpe = 0

    # Accuracy
    correct = sum(1 for t in trades if t.correct_direction)
    fade_accuracy = correct / len(trades)

    # Win rate (positive PnL)
    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / len(pnls)

    # Hedge stats
    hedged = [t for t in trades if t.is_hedged]
    hedge_rate = len(hedged) / len(trades)
    avg_pair_cost = np.mean([t.pair_cost for t in hedged]) if hedged else 0
    unhedged_pct = 1.0 - hedge_rate

    # Per-market stats
    market_pnl = {}
    for t in trades:
        market_pnl[t.market_slug] = market_pnl.get(t.market_slug, 0) + t.pnl_net
    profitable_mkts = sum(1 for p in market_pnl.values() if p > 0)
    profitable_mkts_pct = profitable_mkts / len(market_pnl) if market_pnl else 0
    worst_market_loss = round(min(market_pnl.values()), 2) if market_pnl else 0
    worst_trade_loss = round(min(pnls), 2) if pnls else 0

    # Max drawdown
    cumulative = np.cumsum(pnls)
    peak = np.maximum.accumulate(cumulative)
    drawdown = peak - cumulative
    max_dd = np.max(drawdown) if len(drawdown) > 0 else 0
    max_dd_pct = (max_dd / STARTING_CAPITAL) * 100

    # V2: Cycling stats
    cycling_trades = sum(1 for t in trades if t.entry_number > 1)
    max_entry_num = max(t.entry_number for t in trades) if trades else 0
    entries_per_market = {}
    for t in trades:
        entries_per_market[t.market_slug] = max(entries_per_market.get(t.market_slug, 0), t.entry_number)
    avg_entries_per_market = np.mean(list(entries_per_market.values())) if entries_per_market else 0
    markets_with_cycling = sum(1 for v in entries_per_market.values() if v > 1)

    # Session info
    session_stopped = session_result.session_stopped if session_result else False
    adaptive_activated = session_result.adaptive_activated if session_result else False
    trades_before_stop = session_result.trades_before_stop if session_result else len(trades)
    stop_reason = session_result.stop_reason if session_result else None
    final_pnl = session_result.final_session_pnl if session_result else total_pnl
    pnl_at_check = session_result.pnl_at_check if session_result else None

    return {
        "trades": len(trades),
        "total_pnl": round(total_pnl, 2),
        "pnl_per_hr": round(total_pnl / duration_hours, 2) if duration_hours > 0 else 0,
        "sharpe": round(sharpe, 2),
        "roi_pct": round(total_pnl / STARTING_CAPITAL * 100, 1),
        "win_rate": round(win_rate * 100, 1),
        "fade_accuracy": round(fade_accuracy * 100, 1),
        "profitable_mkts_pct": round(profitable_mkts_pct * 100, 1),
        "max_drawdown_pct": round(max_dd_pct, 1),
        "avg_pnl_per_trade": round(total_pnl / len(trades), 3),
        "hedge_rate": round(hedge_rate * 100, 1),
        "avg_pair_cost": round(avg_pair_cost, 4),
        "unhedged_pct": round(unhedged_pct * 100, 1),
        "worst_trade_loss": worst_trade_loss,
        "worst_market_loss": worst_market_loss,
        "markets_traded": len(market_pnl),
        "cycling_trades": cycling_trades,
        "max_entry_num": max_entry_num,
        "avg_entries_per_market": round(avg_entries_per_market, 2),
        "markets_with_cycling": markets_with_cycling,
        "session_stopped": session_stopped,
        "adaptive_activated": adaptive_activated,
        "trades_before_stop": trades_before_stop,
        "ending_balance": round(STARTING_CAPITAL + final_pnl, 2),
        "stop_reason": stop_reason,
        "pnl_at_check": round(pnl_at_check, 2) if pnl_at_check is not None else None,
    }


# =============================================================================
# MAIN
# =============================================================================
def main():
    global SKIP_UTC_HOURS

    parser = argparse.ArgumentParser(description="PHOENIX Threshold Comparison: OU vs Regime")
    parser.add_argument('--data', default='all',
                       help='Comma-separated datasets, "train", "validation", or "all"')
    parser.add_argument('--output', default='research/findings/data/phoenix_threshold_comparison.csv')
    parser.add_argument('--checkpoint', default='research/findings/data/phoenix_threshold_comparison_checkpoint.csv')
    args = parser.parse_args()

    print("=" * 80)
    print("PHOENIX THRESHOLD COMPARISON: OU vs Regime (Fast/Slow Vol Crossover)")
    print("=" * 80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Starting Capital: ${STARTING_CAPITAL}")

    if args.data == 'train':
        datasets = TRAIN_DATASETS
    elif args.data == 'validation':
        datasets = VALIDATION_DATASETS
    elif args.data == 'all':
        datasets = list(DATASETS.keys())
    else:
        datasets = [d.strip() for d in args.data.split(',')]

    # Build threshold configs: 1 OU + N Regime scale values
    # Each tuple: (method, k_multiplier, regime_scale)
    threshold_configs = [("ou", 0.0, 0.0)]
    for scale in REGIME_SCALE_VALUES:
        threshold_configs.append(("regime", 0.0, scale))

    print(f"Datasets: {datasets}")
    print(f"Regime scales: {REGIME_SCALE_VALUES}")
    print(f"Regime base thresholds: {REGIME_BASE_THRESHOLDS}")
    print(f"Regime windows: fast={REGIME_FAST_WINDOW} ticks (~5s), slow={REGIME_SLOW_WINDOW} ticks (~60s)")
    total_runs = len(threshold_configs) * len(datasets)
    print(f"Total runs: {total_runs} ({len(threshold_configs)} thresholds × {len(datasets)} datasets)")

    # PHOENIX winner config (same for all threshold variants)
    base_config = PhoenixConfig(
        name="PHX_BASELINE",
        expensive_threshold=0.80,
        entry_start_secs=300.0,
        entry_end_secs=120.0,
        entry_offset=0.02,
        decel_required=False,
        hedge_offset=0.02,
        max_fill_price=1.0,
        max_pair_cost=0.96,
        base_shares=25,
        max_entries_per_market=99,
        decel_boost=1.0,
        taper_factor=1.0,
        cooldown_secs=10,
        panic_max_pc=0.0,
        escalate_pc_final=0.0,
        escalate_secs=60.0,
        patient_hedge=False,
        patient_bid=0.0,
        resolution_fills=False,
    )
    print(f"\nBase Config: offset=0.02, pair_cost=0.96, shares=25, entry_window=300-120s")
    print(f"Session Stop: ADAPT25 (check@25 trades, threshold=-$5, DD20%)")

    all_results = []
    pbar = tqdm(total=total_runs, desc="Running")

    for dataset_key in datasets:
        obs_df, btc_df, resolutions, duration_hours = load_dataset(dataset_key)
        if obs_df is None:
            pbar.update(len(threshold_configs))
            continue

        for thresh_method, k_mult, r_scale in threshold_configs:
            if thresh_method == "ou":
                thresh_label = "OU"
            elif thresh_method == "regime":
                thresh_label = f"REGIME_x{r_scale}"
            else:
                thresh_label = f"EWMA_k{k_mult}"
            pbar.set_description(f"{dataset_key}/{thresh_label}")

            # Spike detection with this threshold method
            btc_spikes_df = precompute_spikes_ewma(
                btc_df, EWMA_HALFLIFE_MS,
                threshold_method=thresh_method, k_multiplier=k_mult,
                regime_scale=r_scale
            )

            # Extract spike arrays
            spike_mask = btc_spikes_df['spike_detected'].values
            spike_ts_all = btc_spikes_df.loc[spike_mask, 'timestamp_ms'].values
            spike_mag_all = btc_spikes_df.loc[spike_mask, 'spike_magnitude'].values
            sort_idx = np.argsort(spike_ts_all)
            spike_ts_all = spike_ts_all[sort_idx]
            spike_mag_all = spike_mag_all[sort_idx]

            # Precompute market data with these spikes
            market_data = precompute_markets(obs_df, spike_ts_all, spike_mag_all, resolutions)

            market_starts = {}
            for slug, md in market_data.items():
                market_starts[slug] = md['ts'][0]
            markets_ordered = sorted(market_data.keys(), key=lambda s: market_starts[s])

            # Run backtest
            config = PhoenixConfig(
                name=f"PHX_{thresh_label}",
                expensive_threshold=base_config.expensive_threshold,
                entry_start_secs=base_config.entry_start_secs,
                entry_end_secs=base_config.entry_end_secs,
                entry_offset=base_config.entry_offset,
                decel_required=base_config.decel_required,
                hedge_offset=base_config.hedge_offset,
                max_fill_price=base_config.max_fill_price,
                max_pair_cost=base_config.max_pair_cost,
                base_shares=base_config.base_shares,
                max_entries_per_market=base_config.max_entries_per_market,
                decel_boost=base_config.decel_boost,
                taper_factor=base_config.taper_factor,
                cooldown_secs=base_config.cooldown_secs,
                panic_max_pc=base_config.panic_max_pc,
                escalate_pc_final=base_config.escalate_pc_final,
                escalate_secs=base_config.escalate_secs,
                patient_hedge=base_config.patient_hedge,
                patient_bid=base_config.patient_bid,
                resolution_fills=base_config.resolution_fills,
            )

            session_result = run_backtest_with_session_stops(
                config=config,
                market_data=market_data,
                markets_ordered=markets_ordered,
                dataset_name=dataset_key,
            )

            metrics = calculate_metrics(
                session_result.trades, duration_hours, config, session_result
            )
            metrics['threshold_method'] = thresh_method
            metrics['k_multiplier'] = k_mult
            metrics['threshold_label'] = thresh_label
            metrics['config_name'] = config.name
            metrics['dataset'] = dataset_key
            metrics['n_spikes'] = len(spike_ts_all)
            all_results.append(metrics)

            pbar.update(1)

            # Checkpoint every 10 runs
            if len(all_results) % 10 == 0:
                pd.DataFrame(all_results).to_csv(args.checkpoint, index=False)

    pbar.close()

    # Save final results
    results_df = pd.DataFrame(all_results)
    results_df.to_csv(args.output, index=False)
    print(f"\n{'='*80}")
    print(f"COMPLETE: {len(all_results)} results saved to {args.output}")

    # ==========================================================================
    # COMPARISON SUMMARY
    # ==========================================================================
    if len(results_df) > 0:
        print("\n" + "=" * 80)
        print("THRESHOLD COMPARISON: OU vs Regime (Fast/Slow Vol Crossover)")
        print("=" * 80)

        # Aggregate by threshold label
        all_labels = ["OU"] + [f"REGIME_x{s}" for s in REGIME_SCALE_VALUES]
        print(f"\n{'Threshold':<16} {'Avg $/hr':>9} {'Total PnL':>10} {'Trades':>7} {'WR%':>6} "
              f"{'Hedge%':>7} {'PairC':>7} {'Unhgd%':>7} {'Spikes':>8}")
        print("-" * 92)

        for thresh_label in all_labels:
            subset = results_df[results_df['threshold_label'] == thresh_label]
            if len(subset) == 0:
                continue
            total_pnl = subset['total_pnl'].sum()
            avg_pnl_hr = subset['pnl_per_hr'].mean()
            total_trades = int(subset['trades'].sum())
            avg_wr = subset['win_rate'].mean()
            avg_hr = subset['hedge_rate'].mean()
            avg_pc = subset['avg_pair_cost'].mean()
            avg_uh = subset['unhedged_pct'].mean()
            avg_spikes = int(subset['n_spikes'].mean())
            marker = " << BASELINE" if thresh_label == "OU" else ""
            print(f"{thresh_label:<16} ${avg_pnl_hr:>7.2f} ${total_pnl:>9.2f} {total_trades:>7} "
                  f"{avg_wr:>5.1f}% {avg_hr:>6.1f}% ${avg_pc:>.4f} {avg_uh:>6.1f}% {avg_spikes:>8,}{marker}")

        # Per-dataset breakdown for OU vs best Regime
        regime_results = results_df[results_df['threshold_method'] == 'regime']
        best_regime = regime_results.groupby('threshold_label')['pnl_per_hr'].mean()
        if len(best_regime) > 0:
            best_regime_label = best_regime.idxmax()
            print(f"\n{'='*80}")
            print(f"BEST REGIME: {best_regime_label} (avg ${best_regime.max():.2f}/hr)")
            print(f"{'='*80}")

            print(f"\n{'Dataset':<12} {'OU $/hr':>9} {f'{best_regime_label} $/hr':>16} {'Delta':>8} {'OU Trades':>10} {'Regime Trades':>14}")
            print("-" * 75)

            for ds in datasets:
                ou_row = results_df[(results_df['dataset'] == ds) & (results_df['threshold_label'] == 'OU')]
                rg_row = results_df[(results_df['dataset'] == ds) & (results_df['threshold_label'] == best_regime_label)]
                if len(ou_row) == 0 or len(rg_row) == 0:
                    continue
                ou_pnl = ou_row.iloc[0]['pnl_per_hr']
                rg_pnl = rg_row.iloc[0]['pnl_per_hr']
                delta = rg_pnl - ou_pnl
                ou_trades = int(ou_row.iloc[0]['trades'])
                rg_trades = int(rg_row.iloc[0]['trades'])
                winner = "REGIME" if delta > 0 else "OU"
                print(f"{ds:<12} ${ou_pnl:>8.2f} ${rg_pnl:>15.2f} ${delta:>+7.2f} {ou_trades:>10} {rg_trades:>14}  [{winner}]")

    print(f"\nFinished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
