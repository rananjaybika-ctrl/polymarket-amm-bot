#!/usr/bin/env python3
"""
Fair Value Market Maker V3 — Grid Search with Validated Execution Engine

=============================================================================
COPIED FROM: aggressive_m_v2_grid_search.py (validated FADE execution engine)
MODIFIED: Signal logic only (Steps 3 replaced with FV model)
=============================================================================

Strategy: Compute fair value P(UP) = N(ln(S/K) / (sigma_eff * sqrt(T/900))).
Buy the undervalued side when |FV - market_price| > edge_threshold.
- MR-Vol sigma (mean-reverting volatility model)
- MAKER entry (0% fees), hold to resolution
- Capital constraint: 50% of current balance per market

Includes FADE baseline mode for apples-to-apples comparison.

Agent Findings Incorporated (Feb 9-10, 2026):
- MR-Vol (Model 5): sigma_eff with mean-reversion, kappa=0.00419/sec
- Hour-specific sigma multipliers (IV-calibrated)
- Time-weighted entry thresholds
- Moneyness filter: |ln(S/K)| > 10 bps
- DO NOT implement: Drift/momentum, Jump-diffusion, Asymmetric vol

Usage:
    python research/backtests/fair_value_mm_v3_backtest.py --data IS+OOS2
    python research/backtests/fair_value_mm_v3_backtest.py --data all
"""

# ═══════════════════════════════════════════════════════════════
# SECTION: Imports & sys.path
# STATUS: COPY VERBATIM
# PURPOSE: Standard imports + src/core fee functions
# ═══════════════════════════════════════════════════════════════
import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Any
import sys
import math
import argparse
from datetime import datetime
from tqdm import tqdm
from scipy.stats import norm as sp_norm

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.core import (
    polymarket_taker_fee,
    calculate_pnl_with_fees,
)

from research.reference.TRADING_CONFIGS import AGGRESSIVE as AGGRESSIVE_CONFIG

# ═══════════════════════════════════════════════════════════════
# SECTION: Constants
# STATUS: COPY VERBATIM
# PURPOSE: Starting capital, cooldown, EWMA params, OU params
# ═══════════════════════════════════════════════════════════════
STARTING_CAPITAL = 170.0  # Session starting balance
COOLDOWN_SECONDS = 10
MIN_TIME = 90.0  # Minimum time remaining (avoids manipulation zone)
MIN_EXPENSIVE_ASK = 0.80  # FADE threshold (only used in FADE mode)
EWMA_HALFLIFE_MS = 1000
SHARES_PER_TRADE = 15  # Position size

# OU ADAPTIVE THRESHOLD params (calibrated on IS+OOS2)
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10

# MR-Vol params (calibrated by agent a258f79, Feb 9 2026)
SIGMA_LONG = 0.000128   # Long-run vol per second
KAPPA = 0.00419          # Mean-reversion speed per second (half-life 165s)
MONEYNESS_MIN_BPS = 10   # Minimum |ln(S/K)| in basis points
MAX_CAPITAL_FRACTION = 0.50  # Max fraction of balance per market

# ═══════════════════════════════════════════════════════════════
# SECTION: Config dataclass (FVMMConfig)
# STATUS: MODIFY FOR NEW STRATEGY
# PURPOSE: All tunable parameters for FV MM + FADE baseline
# ═══════════════════════════════════════════════════════════════
@dataclass
class FVMMConfig:
    name: str

    # Mode: "fv" = fair value model, "fade" = spike-based FADE baseline
    mode: str = "fv"

    # FV model params
    use_mr_vol: bool = True              # MR-Vol sigma_eff vs constant EWMA sigma
    sigma_long: float = SIGMA_LONG       # Long-run vol (per second)
    kappa: float = KAPPA                 # Mean-reversion speed (per second)
    base_edge_threshold: float = 0.05    # Min |FV - market_price| to enter
    use_time_weighted_threshold: bool = False  # Time-dependent edge threshold
    use_moneyness_filter: bool = False    # Require |ln(S/K)| > 10 bps
    moneyness_min_bps: float = MONEYNESS_MIN_BPS
    confidence_min: float = 0.0          # Min |FV - 0.5| to trade (0 = disabled)
    use_hour_sigma_multiplier: bool = False  # Hour-specific sigma scaling

    # Entry execution (MAKER — same as FADE)
    entry_offset_cents: float = 0.03     # Bid at undervalued_ask - offset
    entry_shares: int = SHARES_PER_TRADE
    order_pull_seconds: Optional[float] = None  # Max order age before pull
    cooldown_seconds: int = COOLDOWN_SECONDS

    # Filters
    min_time_remaining: float = MIN_TIME  # Skip last 90s
    skip_utc_hours: tuple = ()           # UTC hours to skip
    max_entries_per_market: Optional[int] = None

    # Capital constraint
    use_capital_constraint: bool = True   # Enforce 50% of current balance per market
    max_capital_fraction: float = MAX_CAPITAL_FRACTION

    # Stop loss (pct drop from entry — TAKER exit)
    stop_loss_pct: Optional[float] = None

    # Session stops (same as FADE)
    session_loss_limit: Optional[float] = None
    session_dd_pct: Optional[float] = None
    buffer_threshold: Optional[float] = None
    buffer_trail_pct: Optional[float] = None
    adaptive_check_trades: Optional[int] = None
    adaptive_pnl_threshold: Optional[float] = None
    adaptive_stop_type: Optional[str] = None

    # FADE-only params (used when mode="fade")
    min_expensive_ask: float = MIN_EXPENSIVE_ASK
    z_lo: Optional[float] = None
    z_hi: Optional[float] = None


# ═══════════════════════════════════════════════════════════════
# SECTION: TradeResult dataclass
# STATUS: COPY VERBATIM
# PURPOSE: Single trade record for metrics computation
# ═══════════════════════════════════════════════════════════════
@dataclass
class TradeResult:
    market_slug: str
    entry_time_remaining: float
    entry_side: str  # UP or DOWN (what we bought)
    spike_side: str  # Opposite of entry_side (spike direction) or 'N/A' for FV
    entry_price: float  # Our MAKER fill price
    exit_type: str  # "resolution" or "stop_loss"
    exit_price: Optional[float]
    pnl_gross: float
    pnl_net: float
    exit_fee: float  # 0 for resolution, taker fee for stop
    correct_direction: bool
    spike_magnitude: float  # For FV mode: stores confidence |FV - 0.5|
    velocity_bps: float
    dataset: str
    config_name: str
    shares: int


# ═══════════════════════════════════════════════════════════════
# SECTION: OU Params & EWMA Spike Detection
# STATUS: COPY VERBATIM
# PURPOSE: Produces btc_spikes df with spike_detected + ewma_sigma columns
# ═══════════════════════════════════════════════════════════════
@dataclass
class OUParams:
    mu: float = -3.9845
    sigma_stat: float = 0.3877


def load_ou_params() -> OUParams:
    """Load OU params calibrated on IS+OOS2."""
    return OUParams()


def compute_ou_threshold(volatility: float, ou_params: OUParams) -> Tuple[float, float]:
    """Returns (threshold, z_score)."""
    log_vol = math.log(max(volatility, 1e-6))
    z_score = (log_vol - ou_params.mu) / ou_params.sigma_stat
    z_clamped = max(-10, min(10, z_score * OU_SIGMOID_STEEPNESS))
    sigmoid = 1.0 / (1.0 + math.exp(-z_clamped))
    multiplier = OU_K_LOW + (OU_K_HIGH - OU_K_LOW) * sigmoid
    threshold = OU_BASE_THRESHOLD * multiplier
    return max(OU_MIN_THRESHOLD, min(OU_MAX_THRESHOLD, threshold)), z_score


def precompute_spikes_ewma(btc_df: pd.DataFrame, halflife_ms: int = EWMA_HALFLIFE_MS) -> pd.DataFrame:
    """EWMA spike detection - matches aggressive_main_backtest.py exactly.
    Also computes ewma_sigma column for FV model sigma lookup."""
    halflife_ticks = halflife_ms / 16.67
    alpha = 1 - 0.5 ** (1.0 / halflife_ticks)
    gap_threshold_ms = 30 * 60 * 1000

    print(f"    [EWMA_{halflife_ms}] Half-life={halflife_ms}ms, alpha={alpha:.4f}")

    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    original_len = len(df)
    df = df.drop_duplicates(subset=['timestamp_ms'], keep='first').reset_index(drop=True)
    if len(df) < original_len:
        print(f"    [EWMA_{halflife_ms}] Deduplicated: {original_len:,} -> {len(df):,} rows")

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

    # Compute OU adaptive threshold and z-score + EWMA variance for sigma
    ou_params = load_ou_params()
    returns = df['price'].pct_change() * 100
    ewma_halflife = 300
    var_alpha = 1 - 0.5 ** (1.0 / ewma_halflife)

    variance = returns.iloc[:60].var() if len(returns) > 60 else 0.01
    thresholds = []
    z_scores = []
    ewma_sigmas = []
    for i, r in enumerate(returns):
        if pd.isna(r):
            thresholds.append(OU_BASE_THRESHOLD)
            z_scores.append(0.0)
            ewma_sigmas.append(0.0001)  # Default small sigma
            continue
        variance = var_alpha * (r ** 2) + (1 - var_alpha) * variance
        vol = max(np.sqrt(variance), 1e-6)
        threshold, z_score = compute_ou_threshold(vol, ou_params)
        thresholds.append(threshold)
        z_scores.append(z_score)
        # EWMA sigma: sqrt(variance) of pct returns, convert to per-second
        # Returns are per-tick (~16.67ms). Convert to per-second: vol / sqrt(16.67/1000)
        # vol is in pct (e.g., 0.01 = 0.01%). Convert to decimal fraction first.
        sigma_per_tick = vol / 100.0  # decimal fraction per tick
        sigma_per_sec = sigma_per_tick / math.sqrt(16.67 / 1000.0)  # per second
        ewma_sigmas.append(max(sigma_per_sec, 1e-8))

    df['threshold'] = thresholds
    df['z_score'] = z_scores
    df['ewma_sigma'] = ewma_sigmas
    df['spike_detected'] = df['spike_magnitude'] >= df['threshold']
    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['deviation_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['deviation_pct'] < 0), 'spike_direction'] = 'DOWN'

    spike_count = df['spike_detected'].sum()
    print(f"    [EWMA_{halflife_ms}] Found {spike_count:,} spikes")
    print(f"    [EWMA_{halflife_ms}] EWMA sigma: median={np.median(ewma_sigmas):.6f}/sec, "
          f"p95={np.percentile(ewma_sigmas, 95):.6f}/sec")

    return df


# ═══════════════════════════════════════════════════════════════
# SECTION: FADE Signal Filter
# STATUS: COPY VERBATIM — needed for FADE baseline mode
# PURPOSE: Validates FADE spike-based signals
# ═══════════════════════════════════════════════════════════════
def is_valid_fade_signal(spike_dir: str, velocity_bps: float, expensive_ask: float, time_remaining: float) -> bool:
    """
    FADE signal filter based on research findings.
    - expensive_ask >= $0.80
    - NOT(spike=DOWN AND velocity<0)
    - time_remaining >= 90s
    """
    if expensive_ask < MIN_EXPENSIVE_ASK:
        return False
    if time_remaining < MIN_TIME:
        return False
    if spike_dir == 'DOWN' and velocity_bps < 0:
        return False
    return True


# ═══════════════════════════════════════════════════════════════
# SECTION: Strategy-Specific Functions (FV Model)
# STATUS: MODIFY FOR NEW STRATEGY
# PURPOSE: FV computation, MR-Vol sigma, thresholds, hour multipliers
# ═══════════════════════════════════════════════════════════════

# --- 3a. MR-Vol Sigma ---
def compute_mr_vol_sigma(ewma_sigma: float, time_remaining_sec: float,
                          sigma_long: float = SIGMA_LONG, kappa: float = KAPPA) -> float:
    """
    Mean-reverting volatility effective sigma.
    sigma_eff = sqrt(a^2 + 2ab(1-e^{-kT})/(kT) + b^2(1-e^{-2kT})/(2kT))
    a = sigma_long (long-run vol), b = ewma_sigma (current), T = time in seconds
    """
    a = sigma_long
    b = ewma_sigma
    T = max(time_remaining_sec, 1.0)
    kT = kappa * T

    term1 = a ** 2
    term2 = 2 * a * b * (1 - math.exp(-kT)) / kT
    term3 = b ** 2 * (1 - math.exp(-2 * kT)) / (2 * kT)

    return math.sqrt(max(term1 + term2 + term3, 1e-12))


# --- 3b. Fair Value Computation ---
def compute_fair_value_up(btc_price: float, strike: float, sigma: float,
                           time_remaining_sec: float) -> float:
    """
    P(UP) = N(ln(S/K) / (sigma * sqrt(T)))
    Returns probability that BTC will be above strike at resolution.
    sigma is in per-sqrt(second) units, T is in seconds.
    sigma * sqrt(T) gives the total std dev of log-returns over T seconds.
    """
    T = max(time_remaining_sec, 0.1)
    denom = sigma * math.sqrt(T)
    if denom < 1e-10:
        return 1.0 if btc_price > strike else 0.0
    d = math.log(btc_price / strike) / denom
    return float(sp_norm.cdf(d))


# --- 3c. Time-Weighted Edge Threshold ---
def get_edge_threshold(time_remaining: float, base: float) -> float:
    """Scale edge threshold by time remaining in market."""
    T_frac = time_remaining / 900.0
    if T_frac > 0.67:       return base * 1.5   # First 5 min: require 7.5% edge
    elif T_frac > 0.33:     return base * 0.8   # Middle 5 min: sweet spot, 4% edge
    elif T_frac > 0.10:     return base * 1.0   # Late: standard
    else:                   return base * 3.0   # Last 90s: effectively skip


# --- 3d. Hour-Specific Sigma Multiplier (IV-calibrated) ---
HOUR_SIGMA_MULTIPLIER = {
    0: 2.5, 1: 4.0, 2: 4.0, 3: 3.0, 4: 2.5,
    5: 2.2, 6: 2.2, 7: 2.0, 8: 2.0, 9: 1.75,
    10: 1.75, 11: 2.0, 12: 2.0, 13: 2.0, 14: 2.2,
    15: 2.2, 16: 2.2, 17: 2.2, 18: 2.2, 19: 2.2,
    20: 2.2, 21: 2.0, 22: 1.75, 23: 1.75,
}


# --- 3e. EWMA Sigma Lookup ---
def get_ewma_sigma_at(obs_ts: int, btc_spikes: pd.DataFrame, _ts_array=None) -> float:
    """Get EWMA sigma nearest to obs_ts from precomputed btc_spikes."""
    if _ts_array is None:
        _ts_array = btc_spikes['timestamp_ms'].values
    idx = np.searchsorted(_ts_array, obs_ts)
    idx = min(idx, len(btc_spikes) - 1)
    return btc_spikes.iloc[idx]['ewma_sigma']


# ═══════════════════════════════════════════════════════════════
# SECTION: check_session_stop()
# STATUS: COPY VERBATIM
# PURPOSE: Session-level stop loss mechanisms
# ═══════════════════════════════════════════════════════════════
def check_session_stop(config: FVMMConfig, session_pnl: float, session_peak_pnl: float) -> bool:
    """
    Check if session should stop trading.
    1. Cumulative PnL limit
    2. Drawdown from peak
    3. Buffer + trailing
    """
    if config.session_loss_limit is not None:
        if session_pnl <= config.session_loss_limit:
            return True
    if config.session_dd_pct is not None:
        dd = (session_peak_pnl - session_pnl) / STARTING_CAPITAL
        if dd >= config.session_dd_pct:
            return True
    if config.buffer_threshold is not None and config.buffer_trail_pct is not None:
        if session_pnl >= config.buffer_threshold:
            if session_peak_pnl > 0 and session_pnl < session_peak_pnl * (1 - config.buffer_trail_pct):
                return True
    return False


# ═══════════════════════════════════════════════════════════════
# SECTION: simulate_market() — fill engine (Steps 1+2) + signal logic (Step 3)
# STATUS: Steps 1+2 COPY VERBATIM, Step 3 MODIFY FOR NEW STRATEGY
# PURPOSE: Core simulation loop. MAKER fill, SL check, signal generation
# ═══════════════════════════════════════════════════════════════
def simulate_market(
    btc_spikes: pd.DataFrame,
    obs_df: pd.DataFrame,
    slug: str,
    resolution: str,
    config: FVMMConfig,
    dataset_name: str,
    current_balance: float = STARTING_CAPITAL,
) -> List[TradeResult]:
    """
    Simulate trading with MAKER execution and CYCLING.

    Supports two modes:
    - mode="fv": Fair value model signal (new)
    - mode="fade": Spike-based FADE signal (baseline)

    Fill engine (Steps 1+2) is identical for both modes.
    """
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    # Spike data needed for both modes (FADE uses spikes, FV uses ewma_sigma)
    market_spikes = btc_spikes[
        (btc_spikes['timestamp_ms'] >= market_start) &
        (btc_spikes['timestamp_ms'] <= market_end) &
        (btc_spikes['spike_detected'] == True)
    ].copy().reset_index(drop=True)

    # For FADE mode, need spikes to exist
    if config.mode == "fade" and len(market_spikes) == 0:
        return []

    # For FV mode: compute strike price (BTC price at market epoch)
    # The epoch is encoded in the slug: "btc-updown-15m-{epoch_seconds}"
    strike_price = None
    if config.mode == "fv":
        # Parse epoch from slug to get true market open time
        try:
            epoch_sec = int(slug.split('-')[-1])
            epoch_ms = epoch_sec * 1000
        except (ValueError, IndexError):
            epoch_ms = market_start

        # Try BTC HF data first (most accurate), but only if epoch is within data range
        btc_min_ts = btc_spikes['timestamp_ms'].values[0]
        btc_max_ts = btc_spikes['timestamp_ms'].values[-1]
        if btc_min_ts <= epoch_ms <= btc_max_ts:
            epoch_idx = np.searchsorted(btc_spikes['timestamp_ms'].values, epoch_ms)
            epoch_idx = min(epoch_idx, len(btc_spikes) - 1)
            strike_price = float(btc_spikes.iloc[epoch_idx]['price'])

        # Fallback: use earliest observer row's binance_price (always available)
        if strike_price is None or strike_price <= 0:
            btc_at_start = mdf.iloc[0].get('binance_price', None)
            if pd.notna(btc_at_start) and btc_at_start > 0:
                strike_price = float(btc_at_start)
        if strike_price is None or strike_price <= 0:
            return []

    # Pre-extract btc_spikes timestamp array for fast lookups
    btc_ts_array = btc_spikes['timestamp_ms'].values

    trades = []
    cooldown_ms = config.cooldown_seconds * 1000
    last_signal_ts = {'UP': 0, 'DOWN': 0, 'FV': 0}
    order_pull_ms = (config.order_pull_seconds or float('inf')) * 1000

    # CYCLING: Multiple concurrent positions
    pending_orders = []
    open_positions = []
    entries_this_market = 0

    spike_idx = 0  # For FADE mode

    # Track balance within market (for capital constraint)
    market_balance = current_balance

    # Iterate through ALL observer rows
    for obs_idx, obs_row in mdf.iterrows():
        obs_ts = obs_row['timestamp_ms']

        # Get current prices
        up_ask = obs_row.get('up_ask', None)
        down_ask = obs_row.get('down_ask', None)
        up_bid = obs_row.get('up_bid', None)
        down_bid = obs_row.get('down_bid', None)

        # =================================================================
        # STEP 1: Check pending orders for MAKER fills (COPY VERBATIM)
        # =================================================================
        still_pending = []
        for order in pending_orders:
            order_ts = order['order_ts']
            entry_bid = order['entry_bid']
            entry_side = order['entry_side']

            # Check order pull timeout
            elapsed_ms = obs_ts - order_ts
            if elapsed_ms >= order_pull_ms:
                continue  # Order expired, drop it

            # Check MAKER fill: ask drops to our bid
            if entry_side == "UP":
                entry_ask = up_ask
            else:
                entry_ask = down_ask

            if pd.notna(entry_ask) and entry_ask <= entry_bid:
                # FILLED! Move to open_positions
                order['entry_fill_ts'] = obs_ts
                order['entry_fill_price'] = entry_bid  # MAKER fills at our price
                open_positions.append(order)
                entries_this_market += 1
                # Update balance for capital tracking (cost = entry_bid * shares)
                market_balance -= entry_bid * config.entry_shares
            else:
                still_pending.append(order)

        pending_orders = still_pending

        # =================================================================
        # STEP 2: Check open positions for stop-loss (COPY VERBATIM)
        # =================================================================
        if config.stop_loss_pct is not None:
            still_open = []
            for pos in open_positions:
                entry_fill_price = pos['entry_fill_price']
                entry_side = pos['entry_side']

                if entry_side == "UP":
                    current_bid = up_bid
                else:
                    current_bid = down_bid

                if pd.notna(current_bid):
                    drop_pct = (entry_fill_price - current_bid) / entry_fill_price
                    if drop_pct >= config.stop_loss_pct:
                        exit_price = current_bid
                        pnl_gross = (exit_price - entry_fill_price) * config.entry_shares
                        exit_fee = polymarket_taker_fee(exit_price) * exit_price * config.entry_shares
                        pnl_net = pnl_gross - exit_fee

                        trades.append(TradeResult(
                            market_slug=slug,
                            entry_time_remaining=pos['entry_time_rem'],
                            entry_side=entry_side,
                            spike_side=pos['spike_side'],
                            entry_price=entry_fill_price,
                            exit_type="stop_loss",
                            exit_price=exit_price,
                            pnl_gross=pnl_gross,
                            pnl_net=pnl_net,
                            exit_fee=exit_fee,
                            correct_direction=(resolution == entry_side),
                            spike_magnitude=pos['spike_magnitude'],
                            velocity_bps=pos['velocity_bps'],
                            dataset=dataset_name,
                            config_name=config.name,
                            shares=config.entry_shares,
                        ))
                        # Return capital from closed position
                        market_balance += exit_price * config.entry_shares
                        continue  # Position closed

                still_open.append(pos)

            open_positions = still_open

        # =================================================================
        # STEP 3: Signal Logic (MODE-DEPENDENT)
        # =================================================================
        if config.mode == "fade":
            # ─── FADE BASELINE: Spike-based signal (original logic) ───
            while spike_idx < len(market_spikes):
                spike_row = market_spikes.iloc[spike_idx]
                spike_ts = spike_row['timestamp_ms']

                if spike_ts > obs_ts:
                    break

                spike_dir = spike_row['spike_direction']
                spike_mag = spike_row['spike_magnitude']
                spike_z_score = spike_row.get('z_score', 0.0)

                if config.z_hi is not None and spike_z_score > config.z_hi:
                    spike_idx += 1
                    continue
                if config.z_lo is not None and spike_z_score < config.z_lo:
                    spike_idx += 1
                    continue

                if (spike_ts - last_signal_ts[spike_dir]) < cooldown_ms:
                    spike_idx += 1
                    continue

                time_rem = obs_row['time_remaining_secs']
                velocity_bps = obs_row.get('velocity_bps', 0.0) or 0.0

                if spike_dir == "UP":
                    expensive_ask = down_ask
                    entry_side = "DOWN"
                    spike_side = "UP"
                else:
                    expensive_ask = up_ask
                    entry_side = "UP"
                    spike_side = "DOWN"

                if pd.isna(expensive_ask):
                    spike_idx += 1
                    continue

                if config.skip_utc_hours:
                    hour_utc = pd.Timestamp(obs_ts, unit='ms', tz='UTC').hour
                    if hour_utc in config.skip_utc_hours:
                        spike_idx += 1
                        continue

                if config.max_entries_per_market is not None:
                    total_entries = entries_this_market + len(pending_orders)
                    if total_entries >= config.max_entries_per_market:
                        spike_idx += 1
                        continue

                if not is_valid_fade_signal(spike_dir, velocity_bps, expensive_ask, time_rem):
                    spike_idx += 1
                    continue

                # Capital constraint for FADE too
                if config.use_capital_constraint:
                    current_exposure = sum(p['entry_fill_price'] * config.entry_shares
                                           for p in open_positions)
                    pending_exposure = sum(o['entry_bid'] * config.entry_shares
                                           for o in pending_orders)
                    max_capital = config.max_capital_fraction * current_balance
                    trade_cost = expensive_ask * config.entry_shares
                    if current_exposure + pending_exposure + trade_cost > max_capital:
                        spike_idx += 1
                        continue

                last_signal_ts[spike_dir] = spike_ts
                entry_bid = max(0.01, expensive_ask - config.entry_offset_cents)

                pending_orders.append({
                    'order_ts': spike_ts,
                    'entry_bid': entry_bid,
                    'entry_side': entry_side,
                    'spike_side': spike_side,
                    'spike_magnitude': spike_mag,
                    'velocity_bps': velocity_bps,
                    'entry_time_rem': time_rem,
                })

                spike_idx += 1

        else:
            # ─── FV MODEL: Fair value-based signal ───

            btc_price = obs_row.get('binance_price', None)
            if pd.isna(btc_price) or btc_price is None or btc_price <= 0:
                continue

            btc_price = float(btc_price)
            time_rem = obs_row['time_remaining_secs']

            # Time filter
            if time_rem < config.min_time_remaining:
                continue

            # Hour filter
            if config.skip_utc_hours:
                hour_utc = pd.Timestamp(obs_ts, unit='ms', tz='UTC').hour
                if hour_utc in config.skip_utc_hours:
                    continue

            # Cooldown check
            if (obs_ts - last_signal_ts['FV']) < cooldown_ms:
                continue

            # Get EWMA sigma at this time
            ewma_sigma = get_ewma_sigma_at(obs_ts, btc_spikes, btc_ts_array)

            # Apply hour multiplier if configured
            if config.use_hour_sigma_multiplier:
                hour = pd.Timestamp(obs_ts, unit='ms', tz='UTC').hour
                ewma_sigma *= HOUR_SIGMA_MULTIPLIER.get(hour, 2.2)

            # Compute effective sigma
            if config.use_mr_vol:
                sigma = compute_mr_vol_sigma(ewma_sigma, time_rem, config.sigma_long, config.kappa)
            else:
                sigma = ewma_sigma

            # Compute fair value
            fv_up = compute_fair_value_up(btc_price, strike_price, sigma, time_rem)
            fv_down = 1.0 - fv_up

            # Moneyness filter
            if config.use_moneyness_filter:
                if strike_price > 0 and btc_price > 0:
                    moneyness_bps = abs(math.log(btc_price / strike_price)) * 10000
                    if moneyness_bps < config.moneyness_min_bps:
                        continue

            # Confidence filter
            confidence = abs(fv_up - 0.5)
            if confidence < config.confidence_min:
                continue

            # Determine edge on each side
            edge_up = (fv_up - float(up_ask)) if pd.notna(up_ask) else 0.0
            edge_down = (fv_down - float(down_ask)) if pd.notna(down_ask) else 0.0

            # Get threshold
            if config.use_time_weighted_threshold:
                threshold = get_edge_threshold(time_rem, config.base_edge_threshold)
            else:
                threshold = config.base_edge_threshold

            # Determine which side to buy (buy the side with larger edge above threshold)
            entry_side = None
            entry_ask = None
            if edge_up > threshold and edge_up >= edge_down:
                entry_side = "UP"
                entry_ask = float(up_ask)
            elif edge_down > threshold:
                entry_side = "DOWN"
                entry_ask = float(down_ask)

            if entry_side is None:
                continue

            # Capital constraint check
            if config.use_capital_constraint:
                current_exposure = sum(p['entry_fill_price'] * config.entry_shares
                                       for p in open_positions)
                pending_exposure = sum(o['entry_bid'] * config.entry_shares
                                       for o in pending_orders)
                max_capital = config.max_capital_fraction * current_balance
                trade_cost = entry_ask * config.entry_shares
                if current_exposure + pending_exposure + trade_cost > max_capital:
                    continue

            # Per-market entry cap
            if config.max_entries_per_market is not None:
                total_entries = entries_this_market + len(pending_orders)
                if total_entries >= config.max_entries_per_market:
                    continue

            # Place MAKER order at entry_ask - offset
            entry_bid = max(0.01, entry_ask - config.entry_offset_cents)
            last_signal_ts['FV'] = obs_ts

            velocity_bps = obs_row.get('velocity_bps', 0.0) or 0.0

            pending_orders.append({
                'order_ts': obs_ts,
                'entry_bid': entry_bid,
                'entry_side': entry_side,
                'spike_side': 'N/A',
                'spike_magnitude': confidence,  # Store confidence as proxy
                'velocity_bps': velocity_bps,
                'entry_time_rem': time_rem,
            })

    # ═══════════════════════════════════════════════════════════════
    # SECTION: simulate_market() — resolution (COPY VERBATIM)
    # ═══════════════════════════════════════════════════════════════
    # END OF MARKET: Resolve all open positions
    for pos in open_positions:
        entry_fill_price = pos['entry_fill_price']
        entry_side = pos['entry_side']

        if resolution == entry_side:
            pnl_gross = (1.0 - entry_fill_price) * config.entry_shares
        else:
            pnl_gross = (0.0 - entry_fill_price) * config.entry_shares

        trades.append(TradeResult(
            market_slug=slug,
            entry_time_remaining=pos['entry_time_rem'],
            entry_side=entry_side,
            spike_side=pos['spike_side'],
            entry_price=entry_fill_price,
            exit_type="resolution",
            exit_price=None,
            pnl_gross=pnl_gross,
            pnl_net=pnl_gross,  # MAKER = 0% fees
            exit_fee=0.0,
            correct_direction=(resolution == entry_side),
            spike_magnitude=pos['spike_magnitude'],
            velocity_bps=pos['velocity_bps'],
            dataset=dataset_name,
            config_name=config.name,
            shares=config.entry_shares,
        ))

    return trades


# ═══════════════════════════════════════════════════════════════
# SECTION: DATASETS dict
# STATUS: COPY VERBATIM
# PURPOSE: Dataset file paths for all training + holdout periods
# ═══════════════════════════════════════════════════════════════
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


# ═══════════════════════════════════════════════════════════════
# SECTION: load_dataset()
# STATUS: COPY VERBATIM
# PURPOSE: Load observer, BTC, resolution data for a dataset
# ═══════════════════════════════════════════════════════════════
def load_dataset(dataset_key: str):
    """Load a dataset."""
    config = DATASETS[dataset_key]
    base_dir = Path("/Users/rananjaybika/polymarket-amm-bot")

    print(f"\n{'='*60}")
    print(f"Loading {config['name']}")
    print(f"{'='*60}")

    # Load observer
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

    # Load BTC
    btc_path = base_dir / config['btc_file']
    if btc_path.exists():
        btc_df = pd.read_csv(btc_path)
        print(f"  Binance HF: {len(btc_df):,} rows")
    else:
        print(f"  Binance HF: NOT FOUND")
        return None, None, {}, 0

    # Load resolutions from dataset-specific files
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

    # Duration
    duration_hours = (obs_df['timestamp_ms'].max() - obs_df['timestamp_ms'].min()) / (1000 * 3600)
    print(f"  Duration: {duration_hours:.2f} hours")

    return obs_df, btc_df, resolutions, duration_hours


# ═══════════════════════════════════════════════════════════════
# SECTION: calculate_metrics()
# STATUS: COPY VERBATIM
# PURPOSE: Compute PnL, accuracy, Sharpe, drawdown, etc.
# ═══════════════════════════════════════════════════════════════
def calculate_metrics(
    trades: List[TradeResult],
    duration_hours: float,
    config: FVMMConfig,
    session_result: Optional['SessionResult'] = None,
) -> Dict:
    if not trades:
        return {
            "trades": 0,
            "resolution_trades": 0,
            "stopped_trades": 0,
            "total_pnl": 0,
            "pnl_per_hr": 0,
            "sharpe": 0,
            "roi_pct": 0,
            "fade_accuracy": 0,
            "win_rate": 0,
            "profitable_mkts_pct": 0,
            "max_drawdown_pct": 0,
            "avg_pnl_per_trade": 0,
            "pnl_per_100_deployed": 0,
            "session_stopped": False,
            "trades_before_stop": 0,
            "final_session_pnl": 0,
            "stop_reason": None,
            "ending_balance": STARTING_CAPITAL,
            "adaptive_activated": False,
            "pnl_at_check": None,
            "max_entries_per_mkt": 0,
            "max_exposure_per_mkt": 0,
            "worst_market_loss": 0,
        }

    pnls = [t.pnl_net for t in trades]
    total_pnl = sum(pnls)

    resolution_trades = sum(1 for t in trades if t.exit_type == "resolution")
    stopped_trades = sum(1 for t in trades if t.exit_type == "stop_loss")

    if len(pnls) > 1 and np.std(pnls) > 0:
        sharpe = (np.mean(pnls) / np.std(pnls)) * np.sqrt(252 * 24)
    else:
        sharpe = 0

    correct_trades = sum(1 for t in trades if t.correct_direction)
    fade_accuracy = correct_trades / len(trades) if trades else 0

    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / len(pnls) if pnls else 0

    market_pnl = {}
    market_entries = {}
    market_exposure = {}
    for t in trades:
        market_pnl[t.market_slug] = market_pnl.get(t.market_slug, 0) + t.pnl_net
        market_entries[t.market_slug] = market_entries.get(t.market_slug, 0) + 1
        market_exposure[t.market_slug] = market_exposure.get(t.market_slug, 0) + t.entry_price * t.shares
    profitable_mkts = sum(1 for p in market_pnl.values() if p > 0)
    profitable_mkts_pct = profitable_mkts / len(market_pnl) if market_pnl else 0
    max_entries_any_market = max(market_entries.values()) if market_entries else 0
    max_exposure_any_market = round(max(market_exposure.values()), 2) if market_exposure else 0
    worst_market_loss = round(min(market_pnl.values()), 2) if market_pnl else 0

    cumulative = np.cumsum(pnls)
    peak = np.maximum.accumulate(cumulative)
    drawdown = peak - cumulative
    max_dd = np.max(drawdown) if len(drawdown) > 0 else 0
    max_dd_pct = (max_dd / STARTING_CAPITAL) * 100 if STARTING_CAPITAL > 0 else 0

    total_deployed = sum(t.entry_price * t.shares for t in trades)
    pnl_per_100 = (total_pnl / total_deployed * 100) if total_deployed > 0 else 0

    session_stopped = session_result.session_stopped if session_result else False
    trades_before_stop = session_result.trades_before_stop if session_result else len(trades)
    final_session_pnl = session_result.final_session_pnl if session_result else total_pnl
    stop_reason = session_result.stop_reason if session_result else None
    ending_balance = STARTING_CAPITAL + final_session_pnl
    adaptive_activated = session_result.adaptive_activated if session_result else False
    pnl_at_check = session_result.pnl_at_check if session_result else None

    return {
        "trades": len(trades),
        "resolution_trades": resolution_trades,
        "stopped_trades": stopped_trades,
        "total_pnl": round(total_pnl, 2),
        "pnl_per_hr": round(total_pnl / duration_hours, 2) if duration_hours > 0 else 0,
        "sharpe": round(sharpe, 2),
        "roi_pct": round(total_pnl / STARTING_CAPITAL * 100, 1),
        "fade_accuracy": round(fade_accuracy * 100, 1),
        "win_rate": round(win_rate * 100, 1),
        "profitable_mkts_pct": round(profitable_mkts_pct * 100, 1),
        "max_drawdown_pct": round(max_dd_pct, 1),
        "avg_pnl_per_trade": round(total_pnl / len(trades), 3) if trades else 0,
        "pnl_per_100_deployed": round(pnl_per_100, 2),
        "session_stopped": session_stopped,
        "trades_before_stop": trades_before_stop,
        "final_session_pnl": round(final_session_pnl, 2),
        "stop_reason": stop_reason,
        "ending_balance": round(ending_balance, 2),
        "adaptive_activated": adaptive_activated,
        "pnl_at_check": round(pnl_at_check, 2) if pnl_at_check is not None else None,
        "max_entries_per_mkt": max_entries_any_market,
        "max_exposure_per_mkt": max_exposure_any_market,
        "worst_market_loss": worst_market_loss,
    }


# ═══════════════════════════════════════════════════════════════
# SECTION: SessionResult + run_backtest_with_session_stops()
# STATUS: COPY VERBATIM (added current_balance tracking)
# PURPOSE: Session-level stop + capital tracking across markets
# ═══════════════════════════════════════════════════════════════
@dataclass
class SessionResult:
    """Result of a session backtest with stop tracking."""
    trades: List[TradeResult]
    session_stopped: bool
    trades_before_stop: int
    final_session_pnl: float
    session_peak_pnl: float
    stop_reason: Optional[str]
    adaptive_activated: bool = False
    pnl_at_check: Optional[float] = None


def run_backtest_with_session_stops(
    config: FVMMConfig,
    btc_spikes: pd.DataFrame,
    obs_df: pd.DataFrame,
    markets_with_res: List[str],
    resolutions: Dict[str, str],
    dataset_name: str,
) -> SessionResult:
    """
    Run backtest with session-level stop tracking and capital tracking.
    current_balance tracks running equity for capital constraint.
    """
    session_pnl = 0.0
    session_peak_pnl = 0.0
    session_stopped = False
    stop_reason = None
    all_trades = []
    trades_before_stop = 0

    # Capital tracking
    current_balance = STARTING_CAPITAL

    # Adaptive stop state
    adaptive_activated = False
    adaptive_checked = False
    pnl_at_check = None
    active_dd_pct = config.session_dd_pct
    active_loss_limit = config.session_loss_limit

    for market_slug in markets_with_res:
        if session_stopped:
            break

        resolution = resolutions[market_slug]
        market_trades = simulate_market(
            btc_spikes, obs_df, market_slug, resolution, config, dataset_name,
            current_balance=current_balance,
        )

        for trade in market_trades:
            session_pnl += trade.pnl_net
            session_peak_pnl = max(session_peak_pnl, session_pnl)
            trades_before_stop += 1
            all_trades.append(trade)

            # Update running balance
            current_balance = STARTING_CAPITAL + session_pnl

            # Adaptive check
            if (config.adaptive_check_trades is not None and
                not adaptive_checked and
                trades_before_stop >= config.adaptive_check_trades):

                adaptive_checked = True
                pnl_at_check = session_pnl

                if session_pnl < config.adaptive_pnl_threshold:
                    adaptive_activated = True
                    if config.adaptive_stop_type == "dd20":
                        active_dd_pct = 0.20
                    elif config.adaptive_stop_type == "dd30":
                        active_dd_pct = 0.30
                    elif config.adaptive_stop_type == "loss50":
                        active_loss_limit = -50
                    elif config.adaptive_stop_type == "dd20_loss50":
                        active_dd_pct = 0.20
                        active_loss_limit = -50

            # Check session stops
            should_stop = False
            if config.adaptive_check_trades is None:
                should_stop = check_session_stop(config, session_pnl, session_peak_pnl)
            elif adaptive_activated:
                if active_loss_limit is not None and session_pnl <= active_loss_limit:
                    should_stop = True
                    stop_reason = "adaptive_loss"
                elif active_dd_pct is not None:
                    dd = (session_peak_pnl - session_pnl) / STARTING_CAPITAL
                    if dd >= active_dd_pct:
                        should_stop = True
                        stop_reason = "adaptive_dd"

            if should_stop:
                session_stopped = True
                if stop_reason is None:
                    if config.session_loss_limit is not None and session_pnl <= config.session_loss_limit:
                        stop_reason = "loss_limit"
                    elif config.session_dd_pct is not None:
                        dd = (session_peak_pnl - session_pnl) / STARTING_CAPITAL
                        if dd >= config.session_dd_pct:
                            stop_reason = "drawdown"
                    elif config.buffer_threshold is not None and config.buffer_trail_pct is not None:
                        stop_reason = "buffer_trail"
                break

    return SessionResult(
        trades=all_trades,
        session_stopped=session_stopped,
        trades_before_stop=trades_before_stop if session_stopped else len(all_trades),
        final_session_pnl=session_pnl,
        session_peak_pnl=session_peak_pnl,
        stop_reason=stop_reason,
        adaptive_activated=adaptive_activated,
        pnl_at_check=pnl_at_check,
    )


# ═══════════════════════════════════════════════════════════════
# SECTION: generate_grid_configs()
# STATUS: MODIFY FOR NEW STRATEGY
# PURPOSE: 24 FV configs + 1 FADE baseline
# ═══════════════════════════════════════════════════════════════
def generate_grid_configs() -> List[FVMMConfig]:
    """
    Generate grid configs for FV MM V3 strategy (Feb 10, 2026).

    Groups:
    1. Standard Sigma (EWMA only) — 4 configs
    2. MR-Vol Sigma — 4 configs
    3. MR-Vol + Time-Weighted threshold — 3 configs
    4. MR-Vol + Moneyness filter — 3 configs
    5. Full Stack (MR-Vol + TW + Mon + Hour) — 3 configs
    6. Hour-Specific Sigma Only — 2 configs
    7. Best + Session Stops — 2 configs
    8. Best + Hour Filter from FADE — 2 configs
    + FADE Baseline — 1 config
    Total: 24 configs
    """
    configs = []

    # ─── Group 1: Standard Sigma Baseline (EWMA only, no MR-Vol) ───
    for edge in [0.03, 0.05, 0.07, 0.10]:
        edge_str = f"{int(edge*100):02d}"
        configs.append(FVMMConfig(
            name=f"FV_STD_E{edge_str}",
            mode="fv",
            use_mr_vol=False,
            base_edge_threshold=edge,
        ))

    # ─── Group 2: MR-Vol Sigma ───
    for edge in [0.03, 0.05, 0.07, 0.10]:
        edge_str = f"{int(edge*100):02d}"
        configs.append(FVMMConfig(
            name=f"FV_MRV_E{edge_str}",
            mode="fv",
            use_mr_vol=True,
            base_edge_threshold=edge,
        ))

    # ─── Group 3: MR-Vol + Time-Weighted threshold ───
    for edge in [0.03, 0.05, 0.07]:
        edge_str = f"{int(edge*100):02d}"
        configs.append(FVMMConfig(
            name=f"FV_MRV_TW_E{edge_str}",
            mode="fv",
            use_mr_vol=True,
            base_edge_threshold=edge,
            use_time_weighted_threshold=True,
        ))

    # ─── Group 4: MR-Vol + Moneyness filter ───
    for edge in [0.03, 0.05, 0.07]:
        edge_str = f"{int(edge*100):02d}"
        configs.append(FVMMConfig(
            name=f"FV_MRV_MON_E{edge_str}",
            mode="fv",
            use_mr_vol=True,
            base_edge_threshold=edge,
            use_moneyness_filter=True,
        ))

    # ─── Group 5: Full Stack (MR-Vol + TW + Moneyness + Hour Sigma) ───
    for edge in [0.03, 0.05, 0.07]:
        edge_str = f"{int(edge*100):02d}"
        configs.append(FVMMConfig(
            name=f"FV_FULL_E{edge_str}",
            mode="fv",
            use_mr_vol=True,
            base_edge_threshold=edge,
            use_time_weighted_threshold=True,
            use_moneyness_filter=True,
            use_hour_sigma_multiplier=True,
        ))

    # ─── Group 6: Hour-Specific Sigma Only ───
    for edge in [0.03, 0.05]:
        edge_str = f"{int(edge*100):02d}"
        configs.append(FVMMConfig(
            name=f"FV_HOUR_E{edge_str}",
            mode="fv",
            use_mr_vol=True,
            base_edge_threshold=edge,
            use_hour_sigma_multiplier=True,
        ))

    # ─── Group 7: Best Config + Session Stops (ADAPT25_T5_DD20) ───
    configs.append(FVMMConfig(
        name="FV_MRV_E05_A25",
        mode="fv",
        use_mr_vol=True,
        base_edge_threshold=0.05,
        adaptive_check_trades=25,
        adaptive_pnl_threshold=-5,
        adaptive_stop_type="dd20",
    ))
    configs.append(FVMMConfig(
        name="FV_FULL_E05_A25",
        mode="fv",
        use_mr_vol=True,
        base_edge_threshold=0.05,
        use_time_weighted_threshold=True,
        use_moneyness_filter=True,
        use_hour_sigma_multiplier=True,
        adaptive_check_trades=25,
        adaptive_pnl_threshold=-5,
        adaptive_stop_type="dd20",
    ))

    # ─── Group 8: Best + Hour Filter from FADE ───
    configs.append(FVMMConfig(
        name="FV_MRV_E05_HF",
        mode="fv",
        use_mr_vol=True,
        base_edge_threshold=0.05,
        skip_utc_hours=(14, 20, 8, 4, 3),
    ))
    configs.append(FVMMConfig(
        name="FV_FULL_E05_HF",
        mode="fv",
        use_mr_vol=True,
        base_edge_threshold=0.05,
        use_time_weighted_threshold=True,
        use_moneyness_filter=True,
        use_hour_sigma_multiplier=True,
        skip_utc_hours=(14, 20, 8, 4, 3),
    ))

    # ─── FADE Baseline Reference (no capital constraint for apples-to-apples) ───
    configs.append(FVMMConfig(
        name="FADE_BASELINE",
        mode="fade",
        entry_offset_cents=0.03,
        min_expensive_ask=0.80,
        use_capital_constraint=False,  # Match original grid search behavior
    ))

    # ─── FADE CAP3 (current live config) ───
    configs.append(FVMMConfig(
        name="FADE_CAP3",
        mode="fade",
        entry_offset_cents=0.03,
        min_expensive_ask=0.80,
        max_entries_per_market=3,
        use_capital_constraint=False,
    ))

    return configs


# ═══════════════════════════════════════════════════════════════
# SECTION: main()
# STATUS: COPY VERBATIM (changed output paths + banner only)
# PURPOSE: CLI entry point, dataset loop, grid search, results output
# ═══════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='IS+OOS2', help='Comma-separated: IS+OOS2,OOS7,OOS9 or "all"')
    parser.add_argument('--output', default='research/findings/data/fv_mm_v3_results.csv')
    parser.add_argument('--checkpoint', default='research/findings/data/fv_mm_v3_checkpoint.csv')
    args = parser.parse_args()

    print("=" * 80)
    print("FAIR VALUE MM V3 GRID SEARCH (Feb 10, 2026)")
    print("Copied from: aggressive_m_v2_grid_search.py (validated execution engine)")
    print("=" * 80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Starting Capital: ${STARTING_CAPITAL}")
    print(f"Max Capital/Market: {MAX_CAPITAL_FRACTION*100:.0f}% of current balance")
    print(f"Cooldown: {COOLDOWN_SECONDS}s")
    print(f"Min Time Remaining: {MIN_TIME}s")
    print(f"Shares per Trade: {SHARES_PER_TRADE}")
    print(f"MR-Vol: sigma_long={SIGMA_LONG}, kappa={KAPPA} (half-life={0.693/KAPPA:.0f}s)")

    ou_params = load_ou_params()
    print(f"[OU] Loaded: mu={ou_params.mu:.4f}, sigma_stat={ou_params.sigma_stat:.4f}")

    configs = generate_grid_configs()
    print(f"\nTotal configs: {len(configs)}")
    for c in configs:
        print(f"  - {c.name} (mode={c.mode})")

    if args.data == 'all':
        datasets = list(DATASETS.keys())
    else:
        datasets = [d.strip() for d in args.data.split(',')]
    all_results = []

    for dataset_key in datasets:
        obs_df, btc_df, resolutions, duration_hours = load_dataset(dataset_key)

        if obs_df is None:
            continue

        print(f"\n  Precomputing EWMA_{EWMA_HALFLIFE_MS} spikes + sigma...")
        btc_spikes = precompute_spikes_ewma(btc_df, EWMA_HALFLIFE_MS)
        print(f"  Found {btc_spikes['spike_detected'].sum():,} spikes")

        markets = obs_df['market_slug'].unique()
        markets_with_res = [m for m in markets if m in resolutions]
        print(f"  Markets with resolution: {len(markets_with_res)}")

        print(f"\n  Running {len(configs)} configs on {dataset_key}...")

        for i, config in enumerate(tqdm(configs, desc=f"  {dataset_key}")):
            session_result = run_backtest_with_session_stops(
                config=config,
                btc_spikes=btc_spikes,
                obs_df=obs_df,
                markets_with_res=markets_with_res,
                resolutions=resolutions,
                dataset_name=dataset_key,
            )

            metrics = calculate_metrics(
                session_result.trades, duration_hours, config, session_result
            )
            metrics['config_name'] = config.name
            metrics['dataset'] = dataset_key
            metrics['mode'] = config.mode
            all_results.append(metrics)

            # Checkpoint after each config
            checkpoint_df = pd.DataFrame(all_results)
            checkpoint_df.to_csv(args.checkpoint, index=False)

        print(f"  Checkpoint saved: {len(all_results)} results")

    # Final results
    results_df = pd.DataFrame(all_results)
    results_df.to_csv(args.output, index=False)
    print(f"\n{'='*60}")
    print(f"COMPLETE: {len(all_results)} results saved to {args.output}")

    # Results summary
    if len(results_df) > 0:
        print("\n" + "=" * 60)
        print("FV MM V3 RESULTS SUMMARY")
        print("=" * 60)

        for dataset in results_df['dataset'].unique():
            print(f"\n  {dataset}:")
            subset = results_df[results_df['dataset'] == dataset].copy()
            subset = subset.sort_values('total_pnl', ascending=False)

            cols = ['config_name', 'mode', 'trades', 'total_pnl', 'pnl_per_hr',
                    'fade_accuracy', 'win_rate', 'ending_balance', 'max_drawdown_pct']
            available_cols = [c for c in cols if c in subset.columns]
            print(subset[available_cols].to_string(index=False))

        # Cross-dataset summary
        if len(results_df['dataset'].unique()) > 1:
            print("\n" + "=" * 60)
            print("CROSS-DATASET SUMMARY (Combined PnL)")
            print("=" * 60)
            combined = results_df.groupby('config_name').agg({
                'total_pnl': 'sum',
                'trades': 'sum',
                'fade_accuracy': 'mean',
                'win_rate': 'mean',
                'max_drawdown_pct': 'max',
            }).round(2)
            combined = combined.sort_values('total_pnl', ascending=False)
            print(combined.to_string())

        # FADE baseline comparison
        fade_rows = results_df[results_df['config_name'] == 'FADE_BASELINE']
        if len(fade_rows) > 0:
            fade_total = fade_rows['total_pnl'].sum()
            fade_trades = fade_rows['trades'].sum()
            print(f"\n  FADE BASELINE: ${fade_total:.2f} total PnL, {fade_trades} trades")


if __name__ == "__main__":
    main()
