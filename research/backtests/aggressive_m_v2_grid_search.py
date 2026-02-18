#!/usr/bin/env python3
"""
AGGRESSIVE_M V2 Grid Search - FADE the Spike with MAKER Execution

=============================================================================
FADE STRATEGY (Feb 6, 2026 Findings)
=============================================================================

Strategy: Buy expensive_side when BTC spikes but Polymarket doesn't react.
- 94.7% FADE accuracy with proper filters
- $0.31/trade EV (5 shares)
- MAKER entry (0% fees), TAKER only for stop-loss (2%)

Signal Filter:
- expensive_ask >= $0.80 (optimal EV/share)
- NOT(spike=DOWN AND velocity<0) - skip confirmed DOWN spikes
- time_remaining >= 90s
- 10s cooldown per (market, direction)

Grid Parameters (144 configs):
- entry_offset_cents: [0, 1, 2, 3]
- order_pull_seconds: [None, 10, 30]
- stop_loss_pct: [None, 15%, 25%]
- z_hi: [None, -6.0, -6.5, -7.0] - Skip high volatility (trending) regimes
  Note: z-scores are negative (OU calibrated on low-vol IS+OOS2)
  Higher z (less negative) = higher volatility = skip

Fixed Parameters (based on findings):
- min_expensive_ask: 0.80 (optimal)
- hedge_ratio: 0% (resolution > spread capture)
- time_stop_seconds: None (NO time stop)
- z_lo: None (no lower bound)

Usage:
    python research/backtests/aggressive_m_v2_grid_search.py --data IS+OOS2
    python research/backtests/aggressive_m_v2_grid_search.py --data all
"""

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

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# =============================================================================
# IMPORTS - FROM src/core (Single Source of Truth)
# =============================================================================
from src.core import (
    polymarket_taker_fee,
    calculate_pnl_with_fees,
)

from research.reference.TRADING_CONFIGS import AGGRESSIVE as AGGRESSIVE_CONFIG

# =============================================================================
# CONSTANTS (Based on Feb 6, 2026 FADE Findings)
# =============================================================================
STARTING_CAPITAL = 170.0  # Session starting balance
COOLDOWN_SECONDS = 10
MIN_TIME = 90.0  # Minimum time remaining (avoids manipulation zone)
MIN_EXPENSIVE_ASK = 0.80  # Optimal threshold (94.7% FADE accuracy, $0.026 EV/share)
EWMA_HALFLIFE_MS = 1000
SHARES_PER_TRADE = 15  # Position size (scaled from 5)

# OU ADAPTIVE THRESHOLD params (calibrated on IS+OOS2)
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10

# =============================================================================
# DATA CLASSES
# =============================================================================
@dataclass
class GridConfig:
    name: str
    stop_loss_pct: Optional[float] = None  # None, 0.15, 0.25
    order_pull_seconds: Optional[float] = None  # None, 10, 30
    entry_offset_cents: float = 0.02  # 0, 0.01, 0.02, 0.03
    z_lo: Optional[float] = None  # Lower bound for z-score filter (None = no lower bound)
    z_hi: Optional[float] = None  # Upper bound for z-score filter (None = no upper bound)

    # Session stop parameters (Feb 7, 2026)
    session_loss_limit: Optional[float] = None  # -50, -70, -100 (stop when cumul PnL hits this)
    session_dd_pct: Optional[float] = None  # 0.20, 0.30, 0.40 (stop when DD from peak hits this % of starting capital)
    buffer_threshold: Optional[float] = None  # 30, 50 (profit buffer before trailing activates)
    buffer_trail_pct: Optional[float] = None  # 0.50 (trail % of peak PnL after buffer hit)

    # Adaptive session stop parameters (Feb 7, 2026)
    # Only enable session stops if early trades indicate losing regime
    adaptive_check_trades: Optional[int] = None  # Check after N trades (e.g., 20)
    adaptive_pnl_threshold: Optional[float] = None  # If PnL < this after check, enable stops (e.g., -5)
    adaptive_stop_type: Optional[str] = None  # "dd20", "dd30", "loss50" - which stop to enable

    # Per-market entry cap (Feb 9, 2026)
    max_entries_per_market: Optional[int] = None  # Max filled entries per market (None = unlimited)

    # Hour filter (Feb 9, 2026 — from loser analysis)
    skip_utc_hours: tuple = ()  # UTC hours to skip, e.g. (14, 20, 8, 4, 3)

    # Fixed parameters (based on findings)
    min_expensive_ask: float = MIN_EXPENSIVE_ASK  # 0.80
    entry_shares: int = SHARES_PER_TRADE  # 15


@dataclass
class TradeResult:
    market_slug: str
    entry_time_remaining: float
    entry_side: str  # UP or DOWN (what we bought - expensive_side)
    spike_side: str  # Opposite of entry_side (spike direction)
    entry_price: float  # Our MAKER fill price
    exit_type: str  # "resolution" or "stop_loss"
    exit_price: Optional[float]  # Stop loss exit price (None for resolution)
    pnl_gross: float
    pnl_net: float
    exit_fee: float  # 0 for resolution, taker fee for stop
    correct_direction: bool  # Did entry_side win at resolution?
    spike_magnitude: float
    velocity_bps: float  # For analysis
    dataset: str
    config_name: str
    shares: int


# =============================================================================
# OU ADAPTIVE THRESHOLD
# =============================================================================
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


# =============================================================================
# SPIKE DETECTION - EWMA (copied from aggressive_main_backtest.py)
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

    # Compute OU adaptive threshold and z-score
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
    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['deviation_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['deviation_pct'] < 0), 'spike_direction'] = 'DOWN'

    spike_count = df['spike_detected'].sum()
    print(f"    [EWMA_{halflife_ms}] Found {spike_count:,} spikes")

    return df


# =============================================================================
# FADE SIGNAL FILTER (Feb 6, 2026 Findings)
# =============================================================================
def is_valid_fade_signal(spike_dir: str, velocity_bps: float, expensive_ask: float, time_remaining: float) -> bool:
    """
    FADE signal filter based on research findings.

    Filter logic:
    - expensive_ask >= $0.80 (optimal EV/share)
    - NOT(spike=DOWN AND velocity<0) - skip confirmed DOWN spikes
    - time_remaining >= 90s (avoid manipulation zone)

    Returns True if signal is valid for FADE entry.
    """
    # Threshold check
    if expensive_ask < MIN_EXPENSIVE_ASK:
        return False

    # Time remaining check
    if time_remaining < MIN_TIME:
        return False

    # Exclude confirmed DOWN spikes (spike is REAL, not noise)
    # When spike=DOWN AND velocity<0, BTC is falling and spike confirms it
    # FADE accuracy drops to 79.7% - SKIP these
    if spike_dir == 'DOWN' and velocity_bps < 0:
        return False

    return True


# =============================================================================
# SESSION STOP CHECK (Feb 7, 2026)
# =============================================================================
def check_session_stop(config: GridConfig, session_pnl: float, session_peak_pnl: float) -> bool:
    """
    Check if session should stop trading based on stop mechanisms.

    Three mechanisms:
    1. Cumulative PnL limit: Stop when session loss hits threshold
    2. Drawdown from peak: Stop when balance drops X% of starting capital from session high
    3. Buffer + trailing: After profit buffer, apply trailing stop on PnL

    Returns True if session should stop.
    """
    # 1. Cumulative loss limit
    if config.session_loss_limit is not None:
        if session_pnl <= config.session_loss_limit:
            return True

    # 2. Drawdown from peak (as % of starting capital)
    if config.session_dd_pct is not None:
        dd = (session_peak_pnl - session_pnl) / STARTING_CAPITAL
        if dd >= config.session_dd_pct:
            return True

    # 3. Buffer + trailing stop
    if config.buffer_threshold is not None and config.buffer_trail_pct is not None:
        if session_pnl >= config.buffer_threshold:
            # Buffer reached, apply trailing stop on peak PnL
            if session_peak_pnl > 0 and session_pnl < session_peak_pnl * (1 - config.buffer_trail_pct):
                return True

    return False


# =============================================================================
# SIMULATION - CYCLING FADE (Multiple Concurrent Positions)
# =============================================================================
def simulate_market(
    btc_spikes: pd.DataFrame,
    obs_df: pd.DataFrame,
    slug: str,
    resolution: str,
    config: GridConfig,
    dataset_name: str,
) -> List[TradeResult]:
    """
    Simulate FADE trading with MAKER execution and CYCLING.

    Cycling: Multiple concurrent positions allowed per market.
    - pending_orders: list of orders waiting for MAKER fill
    - open_positions: list of filled positions waiting for resolution/stop

    Each observer row:
    1. Check pending orders for fills
    2. Check open positions for stop-loss
    3. Check for new spike signals (with cooldown)

    At end: resolve all open positions at market resolution.
    """
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
    ].copy().reset_index(drop=True)

    if len(market_spikes) == 0:
        return []

    trades = []
    cooldown_ms = COOLDOWN_SECONDS * 1000
    last_signal_ts = {'UP': 0, 'DOWN': 0}
    order_pull_ms = (config.order_pull_seconds or float('inf')) * 1000

    # CYCLING: Multiple concurrent positions
    pending_orders = []  # List of pending order dicts
    open_positions = []  # List of filled position dicts
    entries_this_market = 0  # Count of filled entries (for max_entries_per_market cap)

    spike_idx = 0

    # Iterate through ALL observer rows
    for obs_idx, obs_row in mdf.iterrows():
        obs_ts = obs_row['timestamp_ms']

        # Get current prices
        up_ask = obs_row.get('up_ask', None)
        down_ask = obs_row.get('down_ask', None)
        up_bid = obs_row.get('up_bid', None)
        down_bid = obs_row.get('down_bid', None)

        # =================================================================
        # STEP 1: Check pending orders for MAKER fills
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
            else:
                still_pending.append(order)

        pending_orders = still_pending

        # =================================================================
        # STEP 2: Check open positions for stop-loss
        # =================================================================
        if config.stop_loss_pct is not None:
            still_open = []
            for pos in open_positions:
                entry_fill_price = pos['entry_fill_price']
                entry_side = pos['entry_side']

                # Get current bid
                if entry_side == "UP":
                    current_bid = up_bid
                else:
                    current_bid = down_bid

                if pd.notna(current_bid):
                    drop_pct = (entry_fill_price - current_bid) / entry_fill_price
                    if drop_pct >= config.stop_loss_pct:
                        # Stop-loss triggered - exit at bid (TAKER)
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
                        continue  # Position closed

                still_open.append(pos)

            open_positions = still_open

        # =================================================================
        # STEP 3: Check for new spike signals
        # =================================================================
        while spike_idx < len(market_spikes):
            spike_row = market_spikes.iloc[spike_idx]
            spike_ts = spike_row['timestamp_ms']

            # Only process spikes up to current obs_ts
            if spike_ts > obs_ts:
                break

            spike_dir = spike_row['spike_direction']
            spike_mag = spike_row['spike_magnitude']
            spike_z_score = spike_row.get('z_score', 0.0)

            # Z-score filter: skip high volatility (trending) regimes
            if config.z_hi is not None and spike_z_score > config.z_hi:
                spike_idx += 1
                continue
            if config.z_lo is not None and spike_z_score < config.z_lo:
                spike_idx += 1
                continue

            # Deduplication (10s cooldown per direction)
            if (spike_ts - last_signal_ts[spike_dir]) < cooldown_ms:
                spike_idx += 1
                continue

            # Get observer data at spike time
            time_rem = obs_row['time_remaining_secs']
            velocity_bps = obs_row.get('velocity_bps', 0.0) or 0.0

            # Get prices - FADE strategy: we BUY expensive_side
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

            # ===== HOUR FILTER =====
            if config.skip_utc_hours:
                hour_utc = pd.Timestamp(obs_ts, unit='ms', tz='UTC').hour
                if hour_utc in config.skip_utc_hours:
                    spike_idx += 1
                    continue

            # ===== PER-MARKET ENTRY CAP =====
            # Count pending + filled — cap on total entries attempted
            if config.max_entries_per_market is not None:
                total_entries = entries_this_market + len(pending_orders)
                if total_entries >= config.max_entries_per_market:
                    spike_idx += 1
                    continue

            # ===== FADE SIGNAL FILTER =====
            if not is_valid_fade_signal(spike_dir, velocity_bps, expensive_ask, time_rem):
                spike_idx += 1
                continue

            # ===== SIGNAL PASSED - Create pending order =====
            last_signal_ts[spike_dir] = spike_ts

            # Our bid is at expensive_ask - offset
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

    # =================================================================
    # END OF MARKET: Resolve all open positions
    # =================================================================
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


# =============================================================================
# DATASETS
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
            # Handle different column formats
            if 'slug' in res_df.columns and 'winner' in res_df.columns:
                # Format: slug, winner (market_resolutions.csv)
                for _, row in res_df.iterrows():
                    resolutions[row['slug']] = row['winner']
            elif 'market_slug' in res_df.columns and 'resolution' in res_df.columns:
                # Format: timestamp, market_slug, resolution, source
                for _, row in res_df.iterrows():
                    resolutions[row['market_slug']] = row['resolution']
            print(f"  {Path(res_fname).name}: {len(res_df)} resolutions")
    print(f"  Total resolutions: {len(resolutions)} markets")

    # Duration
    duration_hours = (obs_df['timestamp_ms'].max() - obs_df['timestamp_ms'].min()) / (1000 * 3600)
    print(f"  Duration: {duration_hours:.2f} hours")

    return obs_df, btc_df, resolutions, duration_hours


# =============================================================================
# GRID CONFIGS (36 configs based on Feb 6, 2026 Findings)
# =============================================================================
def generate_grid_configs() -> List[GridConfig]:
    """
    Generate grid configs for FADE strategy — per-market cap + SL test (Feb 9, 2026).

    User's proposed config: 15 shares, max 5 buys per market, 10% or 15% SL.
    SL exits do NOT count toward the 5-buy cap (SL is protection, not a "buy").

    Tests:
    - Cap 5 vs no cap (isolate exposure reduction)
    - SL 10% vs 15% vs no SL (isolate SL impact)
    - Hour filter on/off
    - With/without ADAPT25
    """
    configs = []

    # ─── REFERENCE CONFIGS ───
    configs.append(GridConfig(
        name="BASELINE",
        entry_offset_cents=0.03,
    ))
    configs.append(GridConfig(
        name="ADAPT25",
        entry_offset_cents=0.03,
        adaptive_check_trades=25,
        adaptive_pnl_threshold=-5,
        adaptive_stop_type="dd20",
    ))

    # ─── CAP ONLY (no SL) — isolate exposure reduction ───
    configs.append(GridConfig(
        name="CAP5",
        entry_offset_cents=0.03,
        max_entries_per_market=5,
    ))
    configs.append(GridConfig(
        name="CAP3",
        entry_offset_cents=0.03,
        max_entries_per_market=3,
    ))
    configs.append(GridConfig(
        name="CAP1",
        entry_offset_cents=0.03,
        max_entries_per_market=1,
    ))

    # ─── USER'S PROPOSED CONFIGS: CAP5 + SL ───
    configs.append(GridConfig(
        name="CAP5_SL10",
        entry_offset_cents=0.03,
        stop_loss_pct=0.10,
        max_entries_per_market=5,
    ))
    configs.append(GridConfig(
        name="CAP5_SL15",
        entry_offset_cents=0.03,
        stop_loss_pct=0.15,
        max_entries_per_market=5,
    ))

    # ─── CAP1 + SL variants ───
    configs.append(GridConfig(
        name="CAP1_SL10",
        entry_offset_cents=0.03,
        stop_loss_pct=0.10,
        max_entries_per_market=1,
    ))
    configs.append(GridConfig(
        name="CAP1_SL15",
        entry_offset_cents=0.03,
        stop_loss_pct=0.15,
        max_entries_per_market=1,
    ))

    # ─── CAP5 + SL + ADAPT10 ───
    configs.append(GridConfig(
        name="CAP5_SL10_A10",
        entry_offset_cents=0.03,
        stop_loss_pct=0.10,
        max_entries_per_market=5,
        adaptive_check_trades=10,
        adaptive_pnl_threshold=-5,
        adaptive_stop_type="dd20",
    ))
    configs.append(GridConfig(
        name="CAP5_SL15_A10",
        entry_offset_cents=0.03,
        stop_loss_pct=0.15,
        max_entries_per_market=5,
        adaptive_check_trades=10,
        adaptive_pnl_threshold=-5,
        adaptive_stop_type="dd20",
    ))

    # ─── CAP5 + SL + ADAPT25 ───
    configs.append(GridConfig(
        name="CAP5_SL10_A25",
        entry_offset_cents=0.03,
        stop_loss_pct=0.10,
        max_entries_per_market=5,
        adaptive_check_trades=25,
        adaptive_pnl_threshold=-5,
        adaptive_stop_type="dd20",
    ))
    configs.append(GridConfig(
        name="CAP5_SL15_A25",
        entry_offset_cents=0.03,
        stop_loss_pct=0.15,
        max_entries_per_market=5,
        adaptive_check_trades=25,
        adaptive_pnl_threshold=-5,
        adaptive_stop_type="dd20",
    ))

    # ─── CAP5 + SL + ADAPT40 ───
    configs.append(GridConfig(
        name="CAP5_SL10_A40",
        entry_offset_cents=0.03,
        stop_loss_pct=0.10,
        max_entries_per_market=5,
        adaptive_check_trades=40,
        adaptive_pnl_threshold=-5,
        adaptive_stop_type="dd20",
    ))
    configs.append(GridConfig(
        name="CAP5_SL15_A40",
        entry_offset_cents=0.03,
        stop_loss_pct=0.15,
        max_entries_per_market=5,
        adaptive_check_trades=40,
        adaptive_pnl_threshold=-5,
        adaptive_stop_type="dd20",
    ))

    return configs


# =============================================================================
# METRICS CALCULATION (FADE-specific)
# =============================================================================
def calculate_metrics(
    trades: List[TradeResult],
    duration_hours: float,
    config: GridConfig,
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
            "fade_accuracy": 0,  # % of trades where entry_side won
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

    # Exit type breakdown
    resolution_trades = sum(1 for t in trades if t.exit_type == "resolution")
    stopped_trades = sum(1 for t in trades if t.exit_type == "stop_loss")

    # Sharpe (annualized)
    if len(pnls) > 1 and np.std(pnls) > 0:
        sharpe = (np.mean(pnls) / np.std(pnls)) * np.sqrt(252 * 24)  # Hourly
    else:
        sharpe = 0

    # FADE accuracy: did we correctly bet on expensive_side winning?
    correct_trades = sum(1 for t in trades if t.correct_direction)
    fade_accuracy = correct_trades / len(trades) if trades else 0

    # Win rate (positive PnL, not just correct direction)
    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / len(pnls) if pnls else 0

    # Profitable markets + per-market exposure
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

    # Max drawdown
    cumulative = np.cumsum(pnls)
    peak = np.maximum.accumulate(cumulative)
    drawdown = peak - cumulative
    max_dd = np.max(drawdown) if len(drawdown) > 0 else 0
    max_dd_pct = (max_dd / STARTING_CAPITAL) * 100 if STARTING_CAPITAL > 0 else 0

    # PnL per $100 deployed
    total_deployed = sum(t.entry_price * t.shares for t in trades)
    pnl_per_100 = (total_pnl / total_deployed * 100) if total_deployed > 0 else 0

    # Session stop info
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


# =============================================================================
# RUN BACKTEST WITH SESSION STOPS (Feb 7, 2026)
# =============================================================================
@dataclass
class SessionResult:
    """Result of a session backtest with stop tracking."""
    trades: List[TradeResult]
    session_stopped: bool
    trades_before_stop: int
    final_session_pnl: float
    session_peak_pnl: float
    stop_reason: Optional[str]  # "loss_limit", "drawdown", "buffer_trail", "adaptive_dd", None
    adaptive_activated: bool = False  # True if adaptive stop was enabled
    pnl_at_check: Optional[float] = None  # PnL when adaptive check happened


def run_backtest_with_session_stops(
    config: GridConfig,
    btc_spikes: pd.DataFrame,
    obs_df: pd.DataFrame,
    markets_with_res: List[str],
    resolutions: Dict[str, str],
    dataset_name: str,
) -> SessionResult:
    """
    Run backtest with session-level stop tracking.

    Session state is tracked ACROSS markets (not per-market).
    When session stop triggers, we stop trading entirely.

    Adaptive mode: After N trades, check if PnL < threshold.
    If so, enable the specified stop type. Otherwise, let it ride.
    """
    session_pnl = 0.0
    session_peak_pnl = 0.0
    session_stopped = False
    stop_reason = None
    all_trades = []
    trades_before_stop = 0

    # Adaptive stop state
    adaptive_activated = False
    adaptive_checked = False
    pnl_at_check = None
    active_dd_pct = config.session_dd_pct  # May be set by adaptive logic
    active_loss_limit = config.session_loss_limit

    for market_slug in markets_with_res:
        if session_stopped:
            break

        resolution = resolutions[market_slug]
        market_trades = simulate_market(
            btc_spikes, obs_df, market_slug, resolution, config, dataset_name
        )

        for trade in market_trades:
            # Update session PnL
            session_pnl += trade.pnl_net
            session_peak_pnl = max(session_peak_pnl, session_pnl)
            trades_before_stop += 1
            all_trades.append(trade)

            # Adaptive check: after N trades, decide if we enable stops
            if (config.adaptive_check_trades is not None and
                not adaptive_checked and
                trades_before_stop >= config.adaptive_check_trades):

                adaptive_checked = True
                pnl_at_check = session_pnl

                if session_pnl < config.adaptive_pnl_threshold:
                    # Losing regime detected - enable stops
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
                # else: winning regime, no stops enabled

            # Check session stops (only if adaptive activated or non-adaptive config)
            should_stop = False

            # Non-adaptive stops (always check if configured)
            if config.adaptive_check_trades is None:
                should_stop = check_session_stop(config, session_pnl, session_peak_pnl)
            # Adaptive stops (only check if activated)
            elif adaptive_activated:
                # Check with active stop parameters
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
                # Determine stop reason if not already set
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


# =============================================================================
# MAIN
# =============================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='IS+OOS2', help='Comma-separated: IS+OOS2,OOS7,OOS9 or "all"')
    parser.add_argument('--output', default='research/findings/data/aggressive_m_v2_session_stops.csv')
    parser.add_argument('--checkpoint', default='research/findings/data/aggressive_m_v2_session_checkpoint.csv')
    args = parser.parse_args()

    print("=" * 80)
    print("FADE STRATEGY SESSION STOP GRID SEARCH (Feb 7, 2026)")
    print("=" * 80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Starting Capital: ${STARTING_CAPITAL}")
    print(f"Cooldown: {COOLDOWN_SECONDS}s")
    print(f"Min Expensive Ask: ${MIN_EXPENSIVE_ASK}")
    print(f"Min Time Remaining: {MIN_TIME}s")
    print(f"Shares per Trade: {SHARES_PER_TRADE}")
    print(f"Filter: expensive_ask >= ${MIN_EXPENSIVE_ASK}, NOT(DOWN+vel<0), time >= {MIN_TIME}s")
    print(f"\nBase config: FADE80_3c_INF_NOSLP")
    print(f"Session stop configs: 10 variations")

    ou_params = load_ou_params()
    print(f"[OU] Loaded: mu={ou_params.mu:.4f}, sigma_stat={ou_params.sigma_stat:.4f}")

    configs = generate_grid_configs()
    print(f"\nTotal configs: {len(configs)}")
    for c in configs:
        print(f"  - {c.name}")

    if args.data == 'all':
        datasets = list(DATASETS.keys())
    else:
        datasets = [d.strip() for d in args.data.split(',')]
    all_results = []

    for dataset_key in datasets:
        obs_df, btc_df, resolutions, duration_hours = load_dataset(dataset_key)

        if obs_df is None:
            continue

        print(f"\n  Precomputing EWMA_{EWMA_HALFLIFE_MS} spikes...")
        btc_spikes = precompute_spikes_ewma(btc_df, EWMA_HALFLIFE_MS)
        print(f"  Found {btc_spikes['spike_detected'].sum():,} spikes")

        markets = obs_df['market_slug'].unique()
        markets_with_res = [m for m in markets if m in resolutions]
        print(f"  Markets with resolution: {len(markets_with_res)}")

        print(f"\n  Running {len(configs)} configs on {dataset_key}...")

        for i, config in enumerate(tqdm(configs, desc=f"  {dataset_key}")):
            # Run backtest with session stop tracking
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
            all_results.append(metrics)

            # Checkpoint after each config (only 10 configs, so checkpoint each)
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
        print("SESSION STOP COMPARISON")
        print("=" * 60)

        for dataset in results_df['dataset'].unique():
            print(f"\n  {dataset}:")
            subset = results_df[results_df['dataset'] == dataset].copy()
            subset = subset.sort_values('ending_balance', ascending=False)

            cols = ['config_name', 'trades', 'ending_balance', 'total_pnl', 'session_stopped', 'trades_before_stop', 'stop_reason']
            available_cols = [c for c in cols if c in subset.columns]
            print(subset[available_cols].to_string(index=False))

            # Baseline comparison
            baseline = subset[subset['config_name'].str.contains('NOSESS')]
            if len(baseline) > 0:
                baseline_balance = baseline.iloc[0]['ending_balance']
                print(f"\n  Baseline (NOSESS) ending balance: ${baseline_balance:.2f}")
                print(f"  Best session stop ending balance: ${subset.iloc[0]['ending_balance']:.2f}")
                improvement = subset.iloc[0]['ending_balance'] - baseline_balance
                print(f"  Improvement: ${improvement:.2f}")


if __name__ == "__main__":
    main()
