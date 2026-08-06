#!/usr/bin/env python3
"""
PHOENIX V3 Single Config Backtest — Run one config across datasets

=============================================================================
PHOENIX V3 SINGLE CONFIG BACKTEST (Feb 18, 2026)
=============================================================================

COPIED FROM: phoenix_v3_improvements_grid_search.py (validated V3 deliverable)
CHANGES: Replaced grid search with single-config run. NO hour filter by default.
Execution engine: UNCHANGED from V3 grid search.

Default config = V3 winner (same as TRADING_CONFIGS.py PHOENIX):
- expensive_threshold: 0.80, entry_offset: 0.02
- entry_window: 300-120s, max_entries: 99
- max_pair_cost: 0.96, base_shares: 25
- NO hour filter (override with --hours to enable)

Usage:
    python research/backtests/phoenix_v3_single_backtest.py --data all
    python research/backtests/phoenix_v3_single_backtest.py --data train
    python research/backtests/phoenix_v3_single_backtest.py --data all --hours  # enable hour filter
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
    # Deceleration filter
    decel_required: bool = False
    # Hedge (hedge_offset DEAD in V1 — hardcoded, max_pair_cost cap always binds)
    hedge_offset: float = 0.02
    max_pair_cost: float = 0.96
    # Sizing
    base_shares: int = 25
    # Cycling: max independent entries per market (V2 feature)
    max_entries_per_market: int = 99
    # V3: Improvement parameters
    decel_boost: float = 1.0      # Idea 7: multiply shares when decel detected (1.0=off)
    taper_factor: float = 1.0     # Idea 2: entry N gets base_shares * taper^(N-1) (1.0=off)
    cooldown_secs: int = 10       # Idea 3: seconds between spikes (was fixed at 10)
    panic_max_pc: float = 0.0     # Panic hedge: relaxed pair cost for late hedge (0=disabled)
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
def precompute_spikes_ewma(btc_df: pd.DataFrame, halflife_ms: int = EWMA_HALFLIFE_MS) -> pd.DataFrame:
    """EWMA spike detection - matches aggressive_main_backtest.py exactly."""
    halflife_ticks = halflife_ms / 16.67
    alpha = 1 - 0.5 ** (1.0 / halflife_ticks)
    gap_threshold_ms = 30 * 60 * 1000

    print(f"    [EWMA_{halflife_ms}] Half-life={halflife_ms}ms, α={alpha:.4f}")

    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    original_len = len(df)
    df = df.drop_duplicates(subset=['timestamp_ms'], keep='first').reset_index(drop=True)
    if len(df) < original_len:
        print(f"    [EWMA_{halflife_ms}] Deduplicated: {original_len:,} → {len(df):,} rows")

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

    ou_params = load_ou_params()
    returns = df['price'].pct_change() * 100
    ewma_halflife = 300
    var_alpha = 1 - 0.5 ** (1.0 / ewma_halflife)

    variance = returns.iloc[:60].var() if len(returns) > 60 else 0.01
    thresholds = []
    z_scores = []
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

    df['threshold'] = thresholds
    df['z_score'] = z_scores
    df['spike_detected'] = df['spike_magnitude'] >= df['threshold']

    spike_count = df['spike_detected'].sum()
    print(f"    [EWMA_{halflife_ms}] Found {spike_count:,} spikes")

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
        # Place hedge bid at min(cheap_ask - hedge_offset, max_pair_cost - fill_price)
        cheap_at_fill = hedge_asks[fill_global]
        if np.isnan(cheap_at_fill):
            hedge_bid = config.max_pair_cost - fill_price
        else:
            hedge_bid_raw = cheap_at_fill - config.hedge_offset
            hedge_bid_max = config.max_pair_cost - fill_price
            hedge_bid = min(hedge_bid_raw, hedge_bid_max)

        is_hedged = False
        hedge_price = None

        if hedge_bid >= 0.01 and fill_global + 1 < len(hedge_asks):
            hedge_slice = hedge_asks[fill_global + 1:]
            hedge_fill_mask = hedge_slice <= hedge_bid
            hedge_indices = np.where(hedge_fill_mask)[0]
            if len(hedge_indices) > 0:
                is_hedged = True
                hedge_price = hedge_bid

        # ------- PANIC HEDGE (V3: relax pair cost in final 60s) -------
        if not is_hedged and config.panic_max_pc > 0:
            # Find observations with time_remaining < PANIC_TIME_SECS
            panic_bid = config.panic_max_pc - fill_price
            if panic_bid >= 0.01 and fill_global + 1 < len(hedge_asks):
                time_rem_after = time_rem[fill_global + 1:]
                hedge_asks_after = hedge_asks[fill_global + 1:]
                # Only check in the panic window (last 60s)
                panic_mask = time_rem_after < PANIC_TIME_SECS
                if panic_mask.any():
                    # Apply panic bid only to observations in panic window
                    panic_fill_mask = panic_mask & (hedge_asks_after <= panic_bid)
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

    parser = argparse.ArgumentParser(description="PHOENIX V3 Single Config Backtest")
    parser.add_argument('--data', default='all',
                       help='Comma-separated datasets, "train", "validation", or "all"')
    parser.add_argument('--output', default='research/findings/data/phoenix_v3_single_results.csv')
    parser.add_argument('--checkpoint', default='research/findings/data/phoenix_v3_single_checkpoint.csv')
    parser.add_argument('--hours', action='store_true', help='Enable hour filter (skip UTC 3,4,8,14,20)')
    parser.add_argument('--pair-cost', type=float, default=0.96, help='Max pair cost (default 0.96)')
    parser.add_argument('--offset', type=float, default=0.02, help='Entry offset (default 0.02)')
    parser.add_argument('--shares', type=int, default=25, help='Base shares per entry (default 25)')
    parser.add_argument('--max-entries', type=int, default=99, help='Max entries per market (default 99)')
    args = parser.parse_args()

    # Enable hour filter if requested
    if args.hours:
        SKIP_UTC_HOURS = SKIP_UTC_HOURS_ENABLED

    print("=" * 80)
    print("PHOENIX V3 SINGLE CONFIG BACKTEST (Feb 18, 2026)")
    print("=" * 80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Starting Capital: ${STARTING_CAPITAL}")
    print(f"Max Per Market: {MAX_CAPITAL_FRACTION*100:.0f}% of balance")
    print(f"Hour Filter: {'ON ' + str(sorted(SKIP_UTC_HOURS_ENABLED)) if args.hours else 'OFF'}")
    print(f"Config: offset={args.offset}, pair_cost={args.pair_cost}, shares={args.shares}, max_entries={args.max_entries}")
    print(f"Session Stop: ADAPT25 (check@25 trades, threshold=-$5, DD20%)")

    if args.data == 'train':
        datasets = TRAIN_DATASETS
    elif args.data == 'validation':
        datasets = VALIDATION_DATASETS
    elif args.data == 'all':
        datasets = list(DATASETS.keys())
    else:
        datasets = [d.strip() for d in args.data.split(',')]

    print(f"Datasets: {datasets}")

    # Single config — V3 winner with CLI overrides
    config = PhoenixConfig(
        name=f"PHX_O{int(args.offset*100)}_PC{int(args.pair_cost*100)}_S{args.shares}_C{args.max_entries}",
        expensive_threshold=0.80,
        entry_start_secs=300.0,
        entry_end_secs=120.0,
        entry_offset=args.offset,
        decel_required=False,
        hedge_offset=0.02,
        max_pair_cost=args.pair_cost,
        base_shares=args.shares,
        max_entries_per_market=args.max_entries,
        decel_boost=1.0,
        taper_factor=1.0,
        cooldown_secs=10,
        panic_max_pc=0.0,
    )
    configs = [config]
    print(f"\nConfig: {config.name}")

    all_results = []
    total_runs = len(configs) * len(datasets)
    print(f"\nTotal runs: {total_runs} (1 config × {len(datasets)} datasets)")

    for dataset_key in datasets:
        obs_df, btc_df, resolutions, duration_hours = load_dataset(dataset_key)
        if obs_df is None:
            continue

        # Spike detection (once per dataset)
        print(f"\n  Precomputing EWMA_{EWMA_HALFLIFE_MS} spikes...")
        btc_spikes_df = precompute_spikes_ewma(btc_df, EWMA_HALFLIFE_MS)

        # Extract spike arrays for fast lookup (sorted by timestamp)
        spike_mask = btc_spikes_df['spike_detected'].values
        spike_ts_all = btc_spikes_df.loc[spike_mask, 'timestamp_ms'].values
        spike_mag_all = btc_spikes_df.loc[spike_mask, 'spike_magnitude'].values
        # Ensure sorted for binary search
        sort_idx = np.argsort(spike_ts_all)
        spike_ts_all = spike_ts_all[sort_idx]
        spike_mag_all = spike_mag_all[sort_idx]

        print(f"  Found {len(spike_ts_all):,} spikes")

        # Precompute market data (once per dataset)
        print(f"  Precomputing market data...")
        market_data = precompute_markets(obs_df, spike_ts_all, spike_mag_all, resolutions)
        print(f"  Precomputed {len(market_data)} markets")

        # Sort markets chronologically for session stop
        market_starts = {}
        for slug, md in market_data.items():
            market_starts[slug] = md['ts'][0]
        markets_ordered = sorted(market_data.keys(), key=lambda s: market_starts[s])

        print(f"\n  Running {len(configs)} configs on {dataset_key}...")

        for i, config in enumerate(tqdm(configs, desc=f"  {dataset_key}")):
            session_result = run_backtest_with_session_stops(
                config=config,
                market_data=market_data,
                markets_ordered=markets_ordered,
                dataset_name=dataset_key,
            )

            metrics = calculate_metrics(
                session_result.trades, duration_hours, config, session_result
            )
            metrics['config_name'] = config.name
            metrics['dataset'] = dataset_key
            metrics['expensive_threshold'] = config.expensive_threshold
            metrics['entry_start_secs'] = config.entry_start_secs
            metrics['entry_end_secs'] = config.entry_end_secs
            metrics['entry_offset'] = config.entry_offset
            metrics['decel_required'] = config.decel_required
            metrics['max_pair_cost'] = config.max_pair_cost
            metrics['base_shares'] = config.base_shares
            metrics['max_entries_per_market'] = config.max_entries_per_market
            # V3 parameters
            metrics['decel_boost'] = config.decel_boost
            metrics['taper_factor'] = config.taper_factor
            metrics['cooldown_secs'] = config.cooldown_secs
            metrics['panic_max_pc'] = config.panic_max_pc
            all_results.append(metrics)

            if (i + 1) % 100 == 0 or (i + 1) == len(configs):
                pd.DataFrame(all_results).to_csv(args.checkpoint, index=False)

        print(f"  Checkpoint saved: {len(all_results)} results")

    # Final results
    results_df = pd.DataFrame(all_results)
    results_df.to_csv(args.output, index=False)
    print(f"\n{'='*80}")
    print(f"COMPLETE: {len(all_results)} results saved to {args.output}")
    print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Summary
    if len(results_df) > 0:
        print("\n" + "=" * 80)
        print(f"PHOENIX V3 SINGLE CONFIG RESULTS: {config.name}")
        print(f"Hour Filter: {'ON' if args.hours else 'OFF'}")
        print("=" * 80)

        # Per-dataset breakdown
        total_pnl = results_df['total_pnl'].sum()
        total_trades = int(results_df['trades'].sum())
        total_hours = 0

        print(f"\n{'Dataset':<12} {'PnL':>8} {'$/hr':>7} {'Trades':>6} {'WR%':>5} "
              f"{'Hedge%':>6} {'PairC':>7} {'Unhedged%':>9} {'Balance':>9} {'DD%':>5}")
        print("-" * 100)

        for _, row in results_df.iterrows():
            print(f"{row['dataset']:<12} "
                  f"${row['total_pnl']:>7.2f} "
                  f"${row['pnl_per_hr']:>6.2f} "
                  f"{int(row['trades']):>6} "
                  f"{row['win_rate']:>5.1f} "
                  f"{row['hedge_rate']:>5.1f}% "
                  f"${row['avg_pair_cost']:>.4f} "
                  f"{row['unhedged_pct']:>8.1f}% "
                  f"${row['ending_balance']:>8.2f} "
                  f"{row['max_drawdown_pct']:>5.1f}")

        print("-" * 100)
        avg_wr = results_df['win_rate'].mean()
        avg_hr = results_df['hedge_rate'].mean()
        avg_pc = results_df['avg_pair_cost'].mean()
        avg_uh = results_df['unhedged_pct'].mean()
        avg_pnl_hr = results_df['pnl_per_hr'].mean()
        print(f"{'TOTAL':<12} "
              f"${total_pnl:>7.2f} "
              f"${avg_pnl_hr:>6.2f} "
              f"{total_trades:>6} "
              f"{avg_wr:>5.1f} "
              f"{avg_hr:>5.1f}% "
              f"${avg_pc:>.4f} "
              f"{avg_uh:>8.1f}% "
              f"{'':>9} "
              f"{results_df['max_drawdown_pct'].max():>5.1f}")


if __name__ == "__main__":
    main()
