#!/usr/bin/env python3
"""
Contrarian V2 Backtest — Mean-Reversion on Cheap Side

=============================================================================
COPIED FROM: directional_maker_v2_2_backtest.py (V2.2 validated execution engine)
MODIFIED: Signal replaced with CONTRARIAN mean-reversion from
          src/strategies/contrarian.py. Execution engine UNCHANGED.
=============================================================================

Strategy:
  1. Track BTC peak/trough from window open (from contrarian.py:386-408)
  2. Detect reversal: pullback >= threshold AND retracement >= min fraction
  3. Determine cheap side (opposite to BTC direction)
  4. MAKER bid at cheap_ask - offset (or taker if configured)
  5. Hold to resolution — NO hedge, NO stop loss (pure directional)
  6. Win = $1.00 - entry, Loss = -entry_price (2.33:1 R:R at $0.30 entry)

Execution Engine (IDENTICAL to V2.2):
  - Maker fills: 0% fee, price-touch (ask <= our_bid), rise-above guard
  - Taker fills: 542ms delay (500ms exchange + 42ms network), fee formula
  - Capital constraints: 50% of CURRENT balance per market
  - Session stops: adaptive, loss limit, drawdown
  - Polymarket order minimums: 5 shares, $1.00 min order value

Usage:
    python research/backtests/contrarian_v2_backtest.py --data OOS7
    python research/backtests/contrarian_v2_backtest.py --data train
    python research/backtests/contrarian_v2_backtest.py --data test
    python research/backtests/contrarian_v2_backtest.py --data all
"""

# ═══════════════════════════════════════════════════════════════
# SECTION: Imports & sys.path
# STATUS: COPY VERBATIM from V2.2
# ═══════════════════════════════════════════════════════════════
import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Any
import sys
import os
import json
import math
import argparse
from datetime import datetime
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.core.trading_utils import polymarket_taker_fee

# ═══════════════════════════════════════════════════════════════
# SECTION: Constants
# STATUS: COPY VERBATIM from V2.2
# ═══════════════════════════════════════════════════════════════
STARTING_CAPITAL = 170.0
MIN_TIME = 90.0
MAX_CAPITAL_FRACTION = 0.50
TAKER_DELAY_MS = 542  # 500ms exchange + 42ms network

# Polymarket order constraints
POLY_MIN_SHARES = 5
POLY_MIN_ORDER_VALUE = 1.00  # $1.00 minimum


# ═══════════════════════════════════════════════════════════════
# SECTION: ContrarianConfig dataclass
# STATUS: NEW — replaces V2Config, based on src/strategies/contrarian.py
# ═══════════════════════════════════════════════════════════════
@dataclass
class ContrarianConfig:
    name: str
    # Signal — mean-reversion from src/strategies/contrarian.py
    pullback_threshold: float = 0.0001   # 0.01% pullback from peak (line 41)
    retracement_min: float = 0.30        # 30% retracement of move (line 42)
    entry_price_min: float = 0.20        # Don't buy if cheap side < $0.20 (line 43)
    entry_price_max: float = 0.40        # Don't buy if cheap side > $0.40
    min_delay_seconds: float = 60.0      # Wait 60s for direction to establish (line 51)
    # Entry type
    entry_mode: str = "maker"            # "maker" or "taker"
    bid_offset_cents: float = 0.03       # For maker: bid at cheap_ask - offset
    # Sizing
    base_shares: int = 15
    # NO hedge (pure directional per user request)
    # Timing
    entry_window_start: float = 800.0    # Start looking after 100s elapsed
    entry_window_end: float = 120.0      # Stop looking with 120s left
    min_time_remaining: float = MIN_TIME
    # Capital constraint (COPY VERBATIM from V2.2)
    use_capital_constraint: bool = True
    max_capital_fraction: float = MAX_CAPITAL_FRACTION
    # Session stops (COPY VERBATIM from V2.2)
    session_loss_limit: Optional[float] = None
    session_dd_pct: Optional[float] = None
    buffer_threshold: Optional[float] = None
    buffer_trail_pct: Optional[float] = None
    adaptive_check_trades: Optional[int] = None
    adaptive_pnl_threshold: Optional[float] = None
    adaptive_stop_type: Optional[str] = None
    cooldown_minutes: float = 0.0        # No cooldown by default


# ═══════════════════════════════════════════════════════════════
# SECTION: ContrarianMarketResult dataclass
# STATUS: NEW — simplified from V2MarketResult (no hedge/flip)
# ═══════════════════════════════════════════════════════════════
@dataclass
class ContrarianMarketResult:
    market_slug: str
    resolution: str
    dataset: str
    config_name: str
    # Signal
    btc_direction: str = "NONE"       # UP or DOWN (BTC moved this way)
    entry_side: str = "NONE"          # Contrarian side (opposite to BTC direction)
    final_state: str = "WAITING"
    # Reversal metrics
    pullback_pct: float = 0.0
    retracement_pct: float = 0.0
    peak_move_bps: float = 0.0       # How much BTC moved before reversal
    # Entry
    entry_fill_price: float = 0.0
    entry_shares: int = 0
    entry_cost: float = 0.0
    entry_is_taker: bool = False
    entry_fill_ts: int = 0
    # Fees
    total_taker_fees: float = 0.0
    # PnL
    total_pnl: float = 0.0
    # Meta
    signal_correct: bool = False      # Entry side == resolution
    skip_reason: str = ""
    last_fill_ts: int = 0
    # BTC prices for debugging
    window_start_btc: float = 0.0
    window_peak_btc: float = 0.0
    window_trough_btc: float = 0.0
    cheap_ask_at_signal: float = 0.0


# ═══════════════════════════════════════════════════════════════
# SECTION: check_session_stop()
# STATUS: COPY VERBATIM from V2.2
# ═══════════════════════════════════════════════════════════════
def check_session_stop(config: ContrarianConfig, session_pnl: float, session_peak_pnl: float) -> bool:
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
# SECTION: simulate_taker_fill()
# STATUS: COPY VERBATIM from V2.2
# ═══════════════════════════════════════════════════════════════
def simulate_taker_fill(
    mdf: pd.DataFrame,
    current_idx: int,
    side: str,
) -> Optional[Tuple[float, int, float]]:
    """
    Simulate taker fill with 542ms delay (500ms exchange + 42ms network).
    Fill at CURRENT ask AFTER the delay has elapsed.

    Args:
        mdf: Market DataFrame (sorted by timestamp_ms)
        current_idx: Current row index in mdf
        side: "UP" or "DOWN" — side to buy

    Returns:
        (fill_price, fill_ts, fee_per_share) or None if no fill possible
    """
    order_ts = int(mdf.iloc[current_idx]['timestamp_ms'])
    target_ts = order_ts + TAKER_DELAY_MS
    ask_col = 'up_ask' if side == "UP" else 'down_ask'

    for j in range(current_idx + 1, len(mdf)):
        row_ts = int(mdf.iloc[j]['timestamp_ms'])
        if row_ts >= target_ts:
            fill_price = mdf.iloc[j][ask_col]
            if pd.isna(fill_price):
                continue
            fill_price = float(fill_price)
            if fill_price <= 0 or fill_price >= 1.0:
                continue
            fee_rate = polymarket_taker_fee(fill_price)
            fee_per_share = fee_rate * fill_price
            return fill_price, row_ts, fee_per_share
    return None


# ═══════════════════════════════════════════════════════════════
# SECTION: simulate_market() — Contrarian State Machine
# STATUS: NEW — replaces V2 directional MM state machine
# States: WAITING → MONITORING → ENTRY_BID → POSITIONED → RESOLVED
# No hedge, no flip — pure directional contrarian
# ═══════════════════════════════════════════════════════════════
def simulate_market(
    obs_df: pd.DataFrame,
    slug: str,
    resolution: str,
    config: ContrarianConfig,
    dataset_name: str,
    current_balance: float,
) -> ContrarianMarketResult:
    """
    Contrarian state machine for a single market (15-min window).

    Signal: Track BTC peak/trough from window open, detect reversal.
    Entry: MAKER bid on cheap side (opposite to BTC direction).
    Exit: Hold to resolution. Win = $1 - entry, Loss = -entry.
    """
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    result = ContrarianMarketResult(
        market_slug=slug,
        resolution=resolution,
        dataset=dataset_name,
        config_name=config.name,
    )

    if len(mdf) == 0:
        return result

    # Need binance_price column for signal detection
    if 'binance_price' not in mdf.columns:
        result.skip_reason = "no_btc_price"
        return result

    mdf['binance_price'] = pd.to_numeric(mdf['binance_price'], errors='coerce')

    # Capital constraint
    max_capital = config.max_capital_fraction * current_balance if config.use_capital_constraint else float('inf')

    # ── State machine variables ──
    state = "WAITING"
    window_start_price = 0.0
    window_peak_price = 0.0
    window_trough_price = 0.0
    window_start_ts = 0

    # Entry tracking
    entry_bid = 0.0
    entry_side = ""
    entry_fill_price = 0.0
    entry_shares = 0
    entry_cost = 0.0
    entry_is_taker = False
    entry_fill_ts = 0
    total_taker_fees = 0.0

    # Rise-above guard for maker entry
    entry_ask_was_above = False

    # Signal metrics (for reporting)
    signal_pullback = 0.0
    signal_retracement = 0.0
    signal_btc_direction = ""
    cheap_ask_at_signal = 0.0

    for idx in range(len(mdf)):
        row = mdf.iloc[idx]
        time_rem = float(row['time_remaining_secs'])
        btc_price = row['binance_price']

        if pd.isna(btc_price):
            continue
        btc_price = float(btc_price)

        # Parse Polymarket prices
        up_ask_raw = row.get('up_ask')
        down_ask_raw = row.get('down_ask')
        if pd.isna(up_ask_raw) or pd.isna(down_ask_raw):
            continue
        up_ask = float(up_ask_raw)
        down_ask = float(down_ask_raw)

        # ── STATE: WAITING ──────────────────────────────────────
        if state == "WAITING":
            if time_rem <= config.entry_window_start:
                # Initialize window tracking
                window_start_price = btc_price
                window_peak_price = btc_price
                window_trough_price = btc_price
                window_start_ts = int(row['timestamp_ms'])
                state = "MONITORING"

        # ── STATE: MONITORING ─────────────────────────────────
        elif state == "MONITORING":
            # Update peak and trough
            window_peak_price = max(window_peak_price, btc_price)
            window_trough_price = min(window_trough_price, btc_price)

            elapsed_s = (int(row['timestamp_ms']) - window_start_ts) / 1000.0

            # Entry window expired
            if time_rem < config.entry_window_end:
                result.skip_reason = "no_signal"
                state = "NO_SIGNAL"
                break

            # Wait for minimum delay before checking signal
            if elapsed_s < config.min_delay_seconds:
                continue

            # ── Reversal detection (from contrarian.py:386-408) ──
            up_move = window_peak_price - window_start_price
            down_move = window_start_price - window_trough_price

            if up_move > down_move and up_move > 0:
                # BTC went UP → contrarian bets DOWN
                btc_direction = "UP"
                peak = window_peak_price
                pullback = (peak - btc_price) / peak if peak > 0 else 0
                retracement = (peak - btc_price) / up_move if up_move > 0 else 0
                contrarian_side = "DOWN"
                cheap_ask = down_ask
            elif down_move > 0:
                # BTC went DOWN → contrarian bets UP
                btc_direction = "DOWN"
                trough = window_trough_price
                pullback = (btc_price - trough) / trough if trough > 0 else 0
                retracement = (btc_price - trough) / down_move if down_move > 0 else 0
                contrarian_side = "UP"
                cheap_ask = up_ask
            else:
                continue  # No significant move yet

            # Check pullback threshold
            if pullback < config.pullback_threshold:
                continue

            # Check retracement minimum
            if retracement < config.retracement_min:
                continue

            # Check entry price range
            if cheap_ask < config.entry_price_min:
                continue
            if cheap_ask > config.entry_price_max:
                continue

            # ── Signal fired! Place entry ──
            signal_pullback = pullback
            signal_retracement = retracement
            signal_btc_direction = btc_direction
            cheap_ask_at_signal = cheap_ask
            entry_side = contrarian_side

            if config.entry_mode == "taker":
                # Taker entry: 542ms delay
                taker_result = simulate_taker_fill(mdf, idx, contrarian_side)
                if taker_result is not None:
                    t_price, t_ts, t_fee = taker_result
                    # Check price still in range after delay
                    if t_price >= config.entry_price_min and t_price <= config.entry_price_max:
                        e_shares = config.base_shares
                        e_cost = t_price * e_shares
                        e_total_fee = t_fee * e_shares
                        total_after = e_cost + e_total_fee
                        if not config.use_capital_constraint or total_after <= max_capital:
                            entry_fill_price = t_price
                            entry_shares = e_shares
                            entry_cost = e_cost
                            entry_is_taker = True
                            total_taker_fees = e_total_fee
                            entry_fill_ts = t_ts
                            state = "POSITIONED"
                            continue
                        else:
                            result.skip_reason = "capital_limit"
                            state = "SKIPPED"
                            break
                # Taker failed
                result.skip_reason = "taker_fail"
                state = "SKIPPED"
                break
            else:
                # Maker entry: bid at cheap_ask - offset
                entry_bid = round(cheap_ask - config.bid_offset_cents, 4)
                if entry_bid <= 0:
                    continue  # Invalid bid, keep monitoring
                if entry_bid < config.entry_price_min:
                    continue  # Bid too low after offset
                # Check capital before placing bid
                e_cost_estimate = entry_bid * config.base_shares
                if config.use_capital_constraint and e_cost_estimate > max_capital:
                    result.skip_reason = "capital_limit"
                    state = "SKIPPED"
                    break
                entry_ask_was_above = (cheap_ask > entry_bid)  # Initialize rise-above
                state = "ENTRY_BID"

        # ── STATE: ENTRY_BID (maker waiting for fill) ─────────
        elif state == "ENTRY_BID":
            # Entry window expired without fill
            if time_rem < config.entry_window_end:
                result.skip_reason = "entry_expired"
                state = "EXPIRED"
                break

            # Get cheap side ask
            cheap_ask_col = 'down_ask' if entry_side == "DOWN" else 'up_ask'
            cheap_ask_val = row.get(cheap_ask_col)
            if pd.isna(cheap_ask_val):
                continue
            cheap_ask_now = float(cheap_ask_val)

            # Rise-above guard
            if cheap_ask_now > entry_bid:
                entry_ask_was_above = True

            # Fill check: ask drops to our bid after being above it
            if cheap_ask_now <= entry_bid and entry_ask_was_above:
                e_shares = config.base_shares
                e_cost = entry_bid * e_shares
                # Polymarket min order value check
                if e_cost < POLY_MIN_ORDER_VALUE:
                    e_shares = max(e_shares, int(POLY_MIN_ORDER_VALUE / entry_bid) + 1)
                    e_cost = entry_bid * e_shares
                if not config.use_capital_constraint or e_cost <= max_capital:
                    entry_fill_price = entry_bid
                    entry_shares = e_shares
                    entry_cost = e_cost
                    entry_is_taker = False
                    entry_fill_ts = int(row['timestamp_ms'])
                    state = "POSITIONED"
                    continue
                else:
                    result.skip_reason = "capital_limit_at_fill"
                    state = "SKIPPED"
                    break

        # ── STATE: POSITIONED (holding to resolution) ─────────
        elif state == "POSITIONED":
            # No stops, no hedge — just hold
            pass

    # ═══════════════════════════════════════════════════════════════
    # RESOLUTION: Compute PnL
    # Pure directional: shares on entry_side only
    # Win: payout = shares * $1.00, PnL = payout - cost - fees
    # Loss: payout = 0, PnL = -cost - fees
    # ═══════════════════════════════════════════════════════════════
    result.final_state = state
    result.btc_direction = signal_btc_direction or "NONE"
    result.entry_side = entry_side or "NONE"
    result.pullback_pct = round(signal_pullback * 100, 4)
    result.retracement_pct = round(signal_retracement * 100, 2)
    result.entry_fill_price = round(entry_fill_price, 4)
    result.entry_shares = entry_shares
    result.entry_cost = round(entry_cost, 4)
    result.entry_is_taker = entry_is_taker
    result.entry_fill_ts = entry_fill_ts
    result.total_taker_fees = round(total_taker_fees, 4)
    result.last_fill_ts = entry_fill_ts
    result.skip_reason = result.skip_reason or ""
    result.window_start_btc = round(window_start_price, 2)
    result.window_peak_btc = round(window_peak_price, 2)
    result.window_trough_btc = round(window_trough_price, 2)
    result.cheap_ask_at_signal = round(cheap_ask_at_signal, 4)

    # Peak move in bps
    if window_start_price > 0:
        up_bps = (window_peak_price - window_start_price) / window_start_price * 10000
        down_bps = (window_start_price - window_trough_price) / window_start_price * 10000
        result.peak_move_bps = round(max(up_bps, down_bps), 2)

    if entry_shares == 0:
        result.total_pnl = 0.0
        result.signal_correct = False
        return result

    # Signal correctness: our entry_side matches resolution
    result.signal_correct = (entry_side == resolution)

    total_cost = entry_cost + total_taker_fees

    if resolution == entry_side:
        # WIN: payout = shares * $1.00
        payout = entry_shares * 1.0
        result.total_pnl = round(payout - total_cost, 4)
    else:
        # LOSS: payout = 0
        result.total_pnl = round(-total_cost, 4)

    return result


# ═══════════════════════════════════════════════════════════════
# SECTION: DATASETS dict
# STATUS: COPY VERBATIM from V2.2
# ═══════════════════════════════════════════════════════════════
DATASETS = {
    "IS+OOS2": {
        "name": "IS+OOS2 (Jan 16-19)",
        "split": "train",
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
        "split": "test",
        "btc_file": "research/observer/PROTECTED_btc_prices_oos3_oos4_combined.csv",
        "obs_files": [
            "research/observer/PROTECTED_grid_obs_oos3_oos4_combined.csv",
        ],
        "res_files": ["research/observer/market_resolutions_verified.csv"],
    },
    "OOS7": {
        "name": "OOS7 (Jan 29-30)",
        "split": "train",
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
        "split": "test",
        "btc_file": "research/binance_hf/btc_prices_20260131_055231.csv",
        "obs_files": [
            "research/observer/grid_obs_20260131.csv",
        ],
        "res_files": ["research/observer/resolutions_20260131.csv"],
    },
    "OOS9": {
        "name": "OOS9 (Feb 1-3)",
        "split": "train",
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
        "split": "test",
        "btc_file": "research/binance_hf/btc_prices_20260204_190733.csv",
        "obs_files": [
            "research/observer/grid_obs_20260205.csv",
        ],
        "res_files": ["research/observer/resolutions_20260205.csv"],
    },
}

TRAIN_DATASETS = [k for k, v in DATASETS.items() if v["split"] == "train"]
TEST_DATASETS = [k for k, v in DATASETS.items() if v["split"] == "test"]


# ═══════════════════════════════════════════════════════════════
# SECTION: load_dataset()
# STATUS: COPY VERBATIM from V2.2
# ═══════════════════════════════════════════════════════════════
def load_dataset(dataset_key: str):
    """Load observer + resolution data for a dataset."""
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
        return None, {}, 0

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    print(f"  Combined observer: {len(obs_df):,} rows")

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

    return obs_df, resolutions, duration_hours


# ═══════════════════════════════════════════════════════════════
# SECTION: calculate_metrics()
# STATUS: ADAPTED from V2.2 — simplified for contrarian (no hedge/flip)
# ═══════════════════════════════════════════════════════════════
def calculate_metrics(
    results: List[ContrarianMarketResult],
    duration_hours: float,
    config: ContrarianConfig,
    session_result: Optional['SessionResult'] = None,
    cooldown_skips: int = 0,
) -> Dict:
    if not results:
        return {
            "markets": 0, "markets_traded": 0, "total_pnl": 0, "pnl_per_hr": 0,
            "sharpe": 0, "roi_pct": 0, "win_rate": 0, "max_drawdown_pct": 0,
            "ending_balance": STARTING_CAPITAL, "worst_market_loss": 0,
            "avg_entry_price": 0, "total_taker_fees": 0,
            "state_positioned": 0, "state_no_signal": 0, "state_expired": 0,
            "state_skipped": 0, "state_monitoring": 0,
            "avg_pullback_pct": 0, "avg_retracement_pct": 0,
            "cooldown_skips": cooldown_skips,
            "session_stopped": False, "stop_reason": None,
            "taker_entries": 0, "maker_entries": 0,
        }

    # Markets with actual fills
    traded = [r for r in results if r.entry_shares > 0]
    n_markets = len(results)
    n_traded = len(traded)

    # State distribution
    state_counts = {}
    for r in results:
        state_counts[r.final_state] = state_counts.get(r.final_state, 0) + 1

    # Win rate (of traded markets where entry side == resolution)
    wins = sum(1 for r in traded if r.signal_correct)
    win_rate = (wins / n_traded * 100) if n_traded > 0 else 0

    # Entry type breakdown
    taker_entries = sum(1 for r in traded if r.entry_is_taker)
    maker_entries = n_traded - taker_entries

    # Average entry price
    avg_entry = np.mean([r.entry_fill_price for r in traded]) if traded else 0

    # Average signal metrics
    avg_pullback = np.mean([r.pullback_pct for r in traded]) if traded else 0
    avg_retracement = np.mean([r.retracement_pct for r in traded]) if traded else 0

    # Total taker fees
    total_fees = sum(r.total_taker_fees for r in results)

    # PnL
    total_pnl = sum(r.total_pnl for r in results)

    # Sharpe (per-market PnL of traded markets)
    pnls = [r.total_pnl for r in traded]
    if len(pnls) > 1 and np.std(pnls) > 0:
        sharpe = (np.mean(pnls) / np.std(pnls)) * np.sqrt(252 * 24)
    else:
        sharpe = 0

    # Max drawdown (sequential market PnL)
    cumulative = np.cumsum([r.total_pnl for r in results])
    peak = np.maximum.accumulate(cumulative)
    drawdown = peak - cumulative
    max_dd = np.max(drawdown) if len(drawdown) > 0 else 0
    max_dd_pct = (max_dd / STARTING_CAPITAL) * 100

    # Worst single market
    worst_market = min(r.total_pnl for r in results) if results else 0

    # Session info
    session_stopped = session_result.session_stopped if session_result else False
    stop_reason = session_result.stop_reason if session_result else None
    final_pnl = session_result.final_session_pnl if session_result else total_pnl
    ending_balance = STARTING_CAPITAL + final_pnl

    return {
        "markets": n_markets,
        "markets_traded": n_traded,
        "total_pnl": round(total_pnl, 2),
        "pnl_per_hr": round(total_pnl / duration_hours, 2) if duration_hours > 0 else 0,
        "sharpe": round(sharpe, 2),
        "roi_pct": round(total_pnl / STARTING_CAPITAL * 100, 1),
        "win_rate": round(win_rate, 1),
        "max_drawdown_pct": round(max_dd_pct, 1),
        "ending_balance": round(ending_balance, 2),
        "worst_market_loss": round(worst_market, 2),
        "avg_entry_price": round(avg_entry, 4),
        "total_taker_fees": round(total_fees, 4),
        "state_positioned": state_counts.get("POSITIONED", 0),
        "state_no_signal": state_counts.get("NO_SIGNAL", 0),
        "state_expired": state_counts.get("EXPIRED", 0),
        "state_skipped": state_counts.get("SKIPPED", 0),
        "state_monitoring": state_counts.get("MONITORING", 0),
        "avg_pullback_pct": round(avg_pullback, 4),
        "avg_retracement_pct": round(avg_retracement, 2),
        "cooldown_skips": cooldown_skips,
        "session_stopped": session_stopped,
        "stop_reason": stop_reason,
        "taker_entries": taker_entries,
        "maker_entries": maker_entries,
    }


# ═══════════════════════════════════════════════════════════════
# SECTION: SessionResult + run_backtest_with_session_stops()
# STATUS: ADAPTED from V2.2 — no signal pre-computation needed,
#         contrarian signal is detected per-market inside simulate_market()
# ═══════════════════════════════════════════════════════════════
@dataclass
class SessionResult:
    results: List[ContrarianMarketResult]
    session_stopped: bool
    markets_before_stop: int
    final_session_pnl: float
    session_peak_pnl: float
    stop_reason: Optional[str]
    adaptive_activated: bool = False
    pnl_at_check: Optional[float] = None
    cooldown_skips: int = 0


def run_backtest_with_session_stops(
    config: ContrarianConfig,
    obs_df: pd.DataFrame,
    markets_with_res: List[str],
    resolutions: Dict[str, str],
    dataset_name: str,
) -> SessionResult:
    """
    Run contrarian backtest with session-level stops and cross-market state.

    Unlike directional MM, the contrarian signal is detected INSIDE each market
    (from BTC price action during the window), not pre-computed from EMA crossover.
    """
    session_pnl = 0.0
    session_peak_pnl = 0.0
    session_stopped = False
    stop_reason = None
    all_results = []
    markets_before_stop = 0

    # Capital tracking
    current_balance = STARTING_CAPITAL

    # Adaptive stop state (VERBATIM from V2.2)
    adaptive_activated = False
    adaptive_checked = False
    pnl_at_check = None
    active_dd_pct = config.session_dd_pct
    active_loss_limit = config.session_loss_limit

    # Cross-market state
    last_fill_ts = 0
    cooldown_skips = 0

    # Sort markets by earliest observation (chronological order)
    market_start_times = obs_df.groupby('market_slug')['timestamp_ms'].min()

    sorted_markets = sorted(
        markets_with_res,
        key=lambda m: market_start_times.get(m, 0),
    )

    for market_slug in sorted_markets:
        if session_stopped:
            break

        resolution = resolutions[market_slug]

        # ── Cooldown check ──
        if config.cooldown_minutes > 0 and last_fill_ts > 0:
            mdf = obs_df[obs_df['market_slug'] == market_slug]
            if len(mdf) > 0:
                entry_ts = int(mdf.iloc[0]['timestamp_ms'])
                cooldown_ms = config.cooldown_minutes * 60 * 1000
                if (entry_ts - last_fill_ts) < cooldown_ms:
                    cooldown_skips += 1
                    continue

        # ── Simulate market ──
        market_result = simulate_market(
            obs_df, market_slug, resolution, config, dataset_name,
            current_balance=current_balance,
        )

        # ── Post-resolution: update cross-market state ──
        if market_result.entry_shares > 0:
            session_pnl += market_result.total_pnl
            session_peak_pnl = max(session_peak_pnl, session_pnl)
            markets_before_stop += 1
            current_balance = STARTING_CAPITAL + session_pnl

            if market_result.last_fill_ts > 0:
                last_fill_ts = market_result.last_fill_ts

            # Adaptive check (VERBATIM from V2.2)
            if (config.adaptive_check_trades is not None and
                not adaptive_checked and
                markets_before_stop >= config.adaptive_check_trades):

                adaptive_checked = True
                pnl_at_check = session_pnl

                if session_pnl < config.adaptive_pnl_threshold:
                    adaptive_activated = True
                    if config.adaptive_stop_type == "dd20":
                        active_dd_pct = 0.20
                    elif config.adaptive_stop_type == "dd30":
                        active_dd_pct = 0.30

            # Check session stops (VERBATIM from V2.2)
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
                        stop_reason = "drawdown"
                break

        all_results.append(market_result)

    return SessionResult(
        results=all_results,
        session_stopped=session_stopped,
        markets_before_stop=markets_before_stop if session_stopped else len(all_results),
        final_session_pnl=session_pnl,
        session_peak_pnl=session_peak_pnl,
        stop_reason=stop_reason,
        adaptive_activated=adaptive_activated,
        pnl_at_check=pnl_at_check,
        cooldown_skips=cooldown_skips,
    )


# ═══════════════════════════════════════════════════════════════
# SECTION: generate_grid_configs()
# STATUS: NEW — contrarian-specific parameter sweep
# ═══════════════════════════════════════════════════════════════
def generate_grid_configs() -> List[ContrarianConfig]:
    """
    Contrarian grid: pullback × retracement × entry price × timing × entry mode.

    Parameters varied:
    - pullback_threshold: 0.005%, 0.01%, 0.02%
    - retracement_min: 0.20, 0.30, 0.40
    - entry_price_max: 0.35, 0.40 (cheap side upper bound)
    - min_delay_seconds: 30, 60, 90
    - entry_mode: maker (0% fee) vs taker (542ms delay + fee)
    - bid_offset_cents: 0.02, 0.03 (maker only)
    """
    configs = []

    # ── Core sweep: pullback × retracement × delay ──
    pullbacks = [0.00005, 0.0001, 0.0002]
    retracements = [0.20, 0.30, 0.40]
    delays = [30.0, 60.0, 90.0]

    for pb in pullbacks:
        for ret in retracements:
            for delay in delays:
                pb_tag = f"pb{int(pb*100000)}"
                ret_tag = f"ret{int(ret*100)}"
                d_tag = f"d{int(delay)}"
                configs.append(ContrarianConfig(
                    name=f"CTR_{pb_tag}_{ret_tag}_{d_tag}_mk3c",
                    pullback_threshold=pb,
                    retracement_min=ret,
                    min_delay_seconds=delay,
                    entry_mode="maker",
                    bid_offset_cents=0.03,
                ))

    # ── Offset sweep on default params ──
    for offset in [0.01, 0.02, 0.04, 0.05]:
        o_tag = f"{int(offset * 100)}c"
        configs.append(ContrarianConfig(
            name=f"CTR_pb10_ret30_d60_mk{o_tag}",
            pullback_threshold=0.0001,
            retracement_min=0.30,
            min_delay_seconds=60.0,
            entry_mode="maker",
            bid_offset_cents=offset,
        ))

    # ── Entry price range sweep ──
    for p_max in [0.30, 0.35, 0.45, 0.50]:
        configs.append(ContrarianConfig(
            name=f"CTR_pb10_ret30_d60_pmax{int(p_max*100)}",
            pullback_threshold=0.0001,
            retracement_min=0.30,
            min_delay_seconds=60.0,
            entry_mode="maker",
            bid_offset_cents=0.03,
            entry_price_max=p_max,
        ))

    # ── Taker entry on best maker params ──
    configs.append(ContrarianConfig(
        name="CTR_pb10_ret30_d60_taker",
        pullback_threshold=0.0001,
        retracement_min=0.30,
        min_delay_seconds=60.0,
        entry_mode="taker",
    ))
    configs.append(ContrarianConfig(
        name="CTR_pb5_ret20_d30_taker",
        pullback_threshold=0.00005,
        retracement_min=0.20,
        min_delay_seconds=30.0,
        entry_mode="taker",
    ))

    return configs


# ═══════════════════════════════════════════════════════════════
# SECTION: main()
# STATUS: ADAPTED from V2.2 — contrarian branding, no EMA precompute
# ═══════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='OOS7', help='"train", "test", "all", or comma-separated: OOS7,OOS9')
    parser.add_argument('--output', default='research/findings/data/contrarian_v2_results.csv')
    parser.add_argument('--checkpoint', default='research/findings/data/contrarian_v2_checkpoint.csv')
    args = parser.parse_args()

    print("=" * 80)
    print("CONTRARIAN V2 GRID SEARCH (Feb 17, 2026)")
    print("Copied from: directional_maker_v2_2_backtest.py (execution engine)")
    print("Signal: Mean-reversion on cheap side (from src/strategies/contrarian.py)")
    print("Entry: MAKER bid or TAKER on cheap side. Pure directional, NO hedge.")
    print("R:R: ~2.33:1 at $0.30 entry ($0.70 profit vs $0.30 risk)")
    print("=" * 80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Starting Capital: ${STARTING_CAPITAL}")
    print(f"Max Capital/Market: {MAX_CAPITAL_FRACTION*100:.0f}% of current balance")
    print(f"Min Time Remaining: {MIN_TIME}s")
    print(f"Taker Delay: {TAKER_DELAY_MS}ms")

    all_configs = generate_grid_configs()
    print(f"\nTotal configs: {len(all_configs)}")

    for c in all_configs[:5]:
        print(f"  - {c.name}: pb={c.pullback_threshold:.5f}, ret={c.retracement_min:.2f}, "
              f"delay={c.min_delay_seconds}s, mode={c.entry_mode}, offset={c.bid_offset_cents}")
    if len(all_configs) > 5:
        print(f"  ... and {len(all_configs) - 5} more")

    if args.data == 'all':
        datasets = list(DATASETS.keys())
    elif args.data == 'train':
        datasets = TRAIN_DATASETS
    elif args.data == 'test':
        datasets = TEST_DATASETS
    else:
        datasets = [d.strip() for d in args.data.split(',')]
    print(f"\nDatasets ({args.data}): {datasets}")
    all_results = []

    for dataset_key in datasets:
        obs_df, resolutions, duration_hours = load_dataset(dataset_key)

        if obs_df is None:
            continue

        markets = obs_df['market_slug'].unique()
        markets_with_res = [m for m in markets if m in resolutions]
        assert len(markets_with_res) > 0, f"No matched markets for {dataset_key}!"
        print(f"  Markets with resolution: {len(markets_with_res)}")

        # No EMA precompute needed — contrarian signal is per-market BTC action
        print(f"  Configs for this dataset: {len(all_configs)}")
        print(f"\n  Running {len(all_configs)} configs on {dataset_key}...")

        for i, config in enumerate(tqdm(all_configs, desc=f"  {dataset_key}")):
            session_result = run_backtest_with_session_stops(
                config=config,
                obs_df=obs_df,
                markets_with_res=markets_with_res,
                resolutions=resolutions,
                dataset_name=dataset_key,
            )

            metrics = calculate_metrics(
                session_result.results, duration_hours, config, session_result,
                cooldown_skips=session_result.cooldown_skips,
            )
            metrics['config_name'] = config.name
            metrics['dataset'] = dataset_key
            metrics['pullback_threshold'] = config.pullback_threshold
            metrics['retracement_min'] = config.retracement_min
            metrics['min_delay_seconds'] = config.min_delay_seconds
            metrics['entry_mode'] = config.entry_mode
            metrics['bid_offset_cents'] = config.bid_offset_cents
            metrics['entry_price_max'] = config.entry_price_max
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

    # ═══════════════════════════════════════════════════════════════
    # RESULTS SUMMARY
    # ═══════════════════════════════════════════════════════════════
    if len(results_df) > 0:
        print("\n" + "=" * 80)
        print("CONTRARIAN V2 RESULTS SUMMARY")
        print("=" * 80)

        for dataset in results_df['dataset'].unique():
            print(f"\n  {dataset}:")
            subset = results_df[results_df['dataset'] == dataset].copy()
            subset = subset.sort_values('total_pnl', ascending=False)

            cols = ['config_name', 'markets_traded', 'win_rate',
                    'total_pnl', 'pnl_per_hr', 'sharpe',
                    'avg_entry_price', 'max_drawdown_pct',
                    'worst_market_loss', 'ending_balance',
                    'maker_entries', 'taker_entries']
            available_cols = [c for c in cols if c in subset.columns]
            print(subset[available_cols].head(10).to_string(index=False))

        # Cross-dataset summary
        if len(results_df['dataset'].unique()) > 1:
            print("\n" + "=" * 80)
            print("CROSS-DATASET SUMMARY (Combined PnL)")
            print("=" * 80)
            combined = results_df.groupby('config_name').agg({
                'total_pnl': 'sum',
                'markets_traded': 'sum',
                'win_rate': 'mean',
                'avg_entry_price': 'mean',
                'max_drawdown_pct': 'max',
                'total_taker_fees': 'sum',
                'cooldown_skips': 'sum',
            }).round(2)
            combined = combined.sort_values('total_pnl', ascending=False)
            print(combined.head(15).to_string())

        # Train vs Test split summary
        results_df['split'] = results_df['dataset'].map(
            {k: v['split'] for k, v in DATASETS.items()}
        )
        train_df = results_df[results_df['split'] == 'train']
        test_df = results_df[results_df['split'] == 'test']

        if len(train_df) > 0 and len(test_df) > 0:
            print("\n" + "=" * 80)
            print("TRAIN vs TEST SPLIT SUMMARY")
            print("=" * 80)

            for split_name, split_df in [("TRAIN", train_df), ("TEST", test_df)]:
                split_datasets = split_df['dataset'].unique().tolist()
                print(f"\n  {split_name} datasets: {split_datasets}")
                combined_split = split_df.groupby('config_name').agg({
                    'total_pnl': 'sum',
                    'markets_traded': 'sum',
                    'win_rate': 'mean',
                    'avg_entry_price': 'mean',
                    'max_drawdown_pct': 'max',
                }).round(2)
                combined_split = combined_split.sort_values('total_pnl', ascending=False)
                print(combined_split.head(10).to_string())

            # Train-top configs → their test performance
            print("\n" + "-" * 80)
            print("TOP 5 TRAIN CONFIGS → TEST PERFORMANCE")
            print("-" * 80)
            train_ranked = train_df.groupby('config_name')['total_pnl'].sum().sort_values(ascending=False)
            for rank, (cfg_name, train_pnl) in enumerate(train_ranked.head(5).items(), 1):
                test_subset = test_df[test_df['config_name'] == cfg_name]
                test_pnl = test_subset['total_pnl'].sum() if len(test_subset) > 0 else 0
                test_wr = test_subset['win_rate'].mean() if len(test_subset) > 0 else 0
                train_wr = train_df[train_df['config_name'] == cfg_name]['win_rate'].mean()
                print(f"  #{rank} {cfg_name}:")
                print(f"    Train PnL: ${train_pnl:.2f} (WR: {train_wr:.1f}%)")
                print(f"    Test  PnL: ${test_pnl:.2f} (WR: {test_wr:.1f}%)")

        # Analysis by pullback threshold
        print("\n" + "=" * 80)
        print("ANALYSIS BY PULLBACK THRESHOLD")
        print("=" * 80)
        by_pb = results_df.groupby('pullback_threshold').agg({
            'total_pnl': ['sum', 'mean'],
            'win_rate': 'mean',
            'markets_traded': 'sum',
            'total_taker_fees': 'sum',
        }).round(2)
        by_pb = by_pb.sort_values(('total_pnl', 'sum'), ascending=False)
        print(by_pb.to_string())

        # Analysis by retracement min
        print("\n" + "=" * 80)
        print("ANALYSIS BY RETRACEMENT MIN")
        print("=" * 80)
        by_ret = results_df.groupby('retracement_min').agg({
            'total_pnl': ['sum', 'mean'],
            'win_rate': 'mean',
            'markets_traded': 'sum',
        }).round(2)
        by_ret = by_ret.sort_values(('total_pnl', 'sum'), ascending=False)
        print(by_ret.to_string())

        # Analysis by delay
        print("\n" + "=" * 80)
        print("ANALYSIS BY MIN DELAY")
        print("=" * 80)
        by_delay = results_df.groupby('min_delay_seconds').agg({
            'total_pnl': ['sum', 'mean'],
            'win_rate': 'mean',
            'markets_traded': 'sum',
        }).round(2)
        by_delay = by_delay.sort_values(('total_pnl', 'sum'), ascending=False)
        print(by_delay.to_string())

        # Analysis by entry mode
        print("\n" + "=" * 80)
        print("ANALYSIS BY ENTRY MODE")
        print("=" * 80)
        by_mode = results_df.groupby('entry_mode').agg({
            'total_pnl': ['sum', 'mean'],
            'win_rate': 'mean',
            'markets_traded': 'sum',
            'total_taker_fees': 'sum',
        }).round(2)
        print(by_mode.to_string())

        # State distribution
        print("\n" + "=" * 80)
        print("FINAL STATE DISTRIBUTION (All configs combined)")
        print("=" * 80)
        state_cols = [c for c in results_df.columns if c.startswith('state_')]
        if state_cols:
            state_totals = results_df[state_cols].sum()
            total_states = state_totals.sum()
            for col in state_cols:
                count = int(state_totals[col])
                pct = count / total_states * 100 if total_states > 0 else 0
                print(f"  {col.replace('state_', '').upper()}: {count} ({pct:.1f}%)")

        # MANDATORY METRICS
        print("\n" + "=" * 80)
        print("TOP 5 CONFIGS BY COMBINED PnL")
        print("=" * 80)
        combined_all = results_df.groupby('config_name').agg({
            'total_pnl': 'sum',
            'markets_traded': 'sum',
            'win_rate': 'mean',
            'avg_entry_price': 'mean',
            'pnl_per_hr': 'mean',
            'max_drawdown_pct': 'max',
            'total_taker_fees': 'sum',
        }).round(2)
        combined_all = combined_all.sort_values('total_pnl', ascending=False)
        for rank, (cfg_name, row) in enumerate(combined_all.head(5).iterrows(), 1):
            print(f"\n  #{rank} {cfg_name}:")
            print(f"    Combined PnL: ${row['total_pnl']:.2f}")
            print(f"    Markets traded: {int(row['markets_traded'])}")
            print(f"    Avg win rate: {row['win_rate']:.1f}%")
            print(f"    Avg entry price: ${row['avg_entry_price']:.4f}")
            print(f"    Avg PnL/hr: ${row['pnl_per_hr']:.2f}")
            print(f"    Max drawdown: {row['max_drawdown_pct']:.1f}%")
            print(f"    Total taker fees: ${row['total_taker_fees']:.4f}")


if __name__ == "__main__":
    main()
