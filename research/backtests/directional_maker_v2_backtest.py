#!/usr/bin/env python3
"""
Directional Maker V2 Backtest — Winner-First + Flip Bias

=============================================================================
COPIED FROM: directional_maker_v1_backtest.py (validated execution engine)
MODIFIED: Sequential winner→hedge entry, flip bias recovery, inverse sizing,
          dynamic best-bid maker entry, taker flip with 542ms delay.
=============================================================================

Strategy:
  1. Predict winner side (BTC EMA + OBI contrarian OR gabagool)
  2. MAKER bid at best bid on winner side → wait for fill
  3. Once winner fills, MAKER bid for hedge on loser side at
     (target_pair_cost - winner_fill_price)
  4. On adverse spike: avg_down (buy more winner) or flip (taker opposite)
  5. Last 90s: taker fallback for any unhedged position

Key differences from V1:
  - Sequential: winner FIRST, hedge SECOND (not simultaneous)
  - Dynamic bid: tracks current best bid (not fixed $0.48)
  - Inverse sizing: hedge ratio depends on signal confidence
  - Flip bias: taker entry on opposite side with 542ms delay simulation
  - Gabagool mode: predict whichever side has higher ask (85.7% accuracy)

Usage:
    python research/backtests/directional_maker_v2_backtest.py --data OOS7
    python research/backtests/directional_maker_v2_backtest.py --data all
"""

# ═══════════════════════════════════════════════════════════════
# SECTION: Imports & sys.path
# STATUS: COPY VERBATIM from V1 + polymarket_taker_fee import
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
# STATUS: COPY VERBATIM from V1
# ═══════════════════════════════════════════════════════════════
STARTING_CAPITAL = 170.0
MIN_TIME = 90.0
MAX_CAPITAL_FRACTION = 0.50
TAKER_DELAY_MS = 542  # 500ms exchange + 42ms network


# ═══════════════════════════════════════════════════════════════
# SECTION: V2Config dataclass
# STATUS: NEW — replaces DirectionalMakerConfig
# ═══════════════════════════════════════════════════════════════
@dataclass
class V2Config:
    name: str
    # Signal
    signal_mode: str = "ema_obi"      # "ema_obi" | "gabagool"
    ema_short_span: int = 300
    ema_long_span: int = 1800
    # Entry (MAKER at ask - offset, fixed once placed)
    bid_offset_cents: float = 0.03    # Bid this many cents below ask
    base_shares: int = 15
    # Hedge
    target_pair_cost: float = 0.98
    # Inverse sizing (confidence → hedge ratio)
    high_conf_hedge_ratio: float = 0.3    # 30% hedge on HIGH confidence
    med_conf_hedge_ratio: float = 0.5     # 50% hedge on MEDIUM
    # Spike response
    spike_mode: str = "flip"              # "avg_down" | "flip" | "none"
    spike_threshold_cents: float = 0.03   # 3c drop triggers spike response
    # Flip-specific
    flip_multiplier: float = 2.0
    flip_target_pair_cost: float = 0.99
    # Avg-down-specific
    avg_down_max_additions: int = 1       # Max 1 average-down per market
    # Timing
    entry_window_start: float = 800.0
    entry_window_end: float = 300.0
    min_time_remaining: float = MIN_TIME
    taker_fallback_time: float = 90.0     # Last 90s: taker hedge fallback
    cooldown_minutes: float = 3.0
    # Capital constraint (COPY VERBATIM from V1)
    use_capital_constraint: bool = True
    max_capital_fraction: float = MAX_CAPITAL_FRACTION
    # Session stops (COPY VERBATIM from V1)
    session_loss_limit: Optional[float] = None
    session_dd_pct: Optional[float] = None
    buffer_threshold: Optional[float] = None
    buffer_trail_pct: Optional[float] = None
    adaptive_check_trades: Optional[int] = None
    adaptive_pnl_threshold: Optional[float] = None
    adaptive_stop_type: Optional[str] = None


# ═══════════════════════════════════════════════════════════════
# SECTION: V2MarketResult dataclass
# STATUS: NEW — tracks state machine progression
# ═══════════════════════════════════════════════════════════════
@dataclass
class V2MarketResult:
    market_slug: str
    resolution: str
    dataset: str
    config_name: str
    predicted_side: str = "NONE"
    confidence_level: str = "NONE"  # HIGH, MEDIUM, LOW
    final_state: str = "WAITING"

    # Winner fill
    winner_fill_price: float = 0.0
    winner_shares: int = 0
    winner_cost: float = 0.0
    winner_fill_ts: int = 0

    # Hedge fill
    hedge_fill_price: float = 0.0
    hedge_shares: int = 0
    hedge_cost: float = 0.0
    hedge_is_taker: bool = False

    # Avg down
    avg_down_fills: int = 0

    # Flip
    flipped: bool = False
    flip_side: str = "NONE"
    flip_fill_price: float = 0.0
    flip_shares: int = 0
    flip_cost: float = 0.0
    flip_hedge_fill_price: float = 0.0
    flip_hedge_shares: int = 0
    flip_hedge_cost: float = 0.0
    flip_hedge_is_taker: bool = False

    # Fees
    total_taker_fees: float = 0.0

    # PnL
    total_pnl: float = 0.0
    pair_cost: float = 0.0   # winner_avg + hedge_fill (when HEDGED)

    # Signal info
    signal_correct: bool = False
    last_fill_ts: int = 0
    skip_reason: str = ""


# ═══════════════════════════════════════════════════════════════
# SECTION: check_session_stop()
# STATUS: COPY VERBATIM from V1
# ═══════════════════════════════════════════════════════════════
def check_session_stop(config: V2Config, session_pnl: float, session_peak_pnl: float) -> bool:
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
# STATUS: NEW — taker with 542ms delay, matches paper_trading.py
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
# SECTION: simulate_market() — V2 State Machine
# STATUS: NEW — replaces V1's simultaneous fill logic
# States: WAITING → WINNER_BID → WINNER_FILLED → HEDGED
#         with optional FLIP_ENTRY → FLIP_HEDGED branch
# ═══════════════════════════════════════════════════════════════
def simulate_market(
    obs_df: pd.DataFrame,
    slug: str,
    resolution: str,
    config: V2Config,
    dataset_name: str,
    current_balance: float,
    predicted_side: str,
    confidence_level: str,
) -> V2MarketResult:
    """
    V2 state machine for a single market.

    Entry: MAKER at best bid on predicted winner side.
    Hedge: MAKER at (target_pair_cost - winner_fill_price) on loser side.
    Spike response: avg_down (maker more winner) or flip (taker opposite).
    Fallback: taker hedge in last 90 seconds.
    """
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    result = V2MarketResult(
        market_slug=slug,
        resolution=resolution,
        dataset=dataset_name,
        config_name=config.name,
        predicted_side=predicted_side,
        confidence_level=confidence_level,
        signal_correct=(predicted_side == resolution),
    )

    if len(mdf) == 0:
        return result

    # Column names based on predicted side
    winner_ask_col = 'up_ask' if predicted_side == "UP" else 'down_ask'
    winner_bid_col = 'up_bid' if predicted_side == "UP" else 'down_bid'
    loser_ask_col = 'down_ask' if predicted_side == "UP" else 'up_ask'

    # Hedge ratio from inverse sizing
    if confidence_level == "HIGH":
        hedge_ratio = config.high_conf_hedge_ratio
    elif confidence_level == "MEDIUM":
        hedge_ratio = config.med_conf_hedge_ratio
    else:
        hedge_ratio = 0.7  # LOW — maximum protection

    # Capital constraint
    max_capital = config.max_capital_fraction * current_balance if config.use_capital_constraint else float('inf')

    # ── State machine variables ──
    state = "WAITING"
    winner_bid = 0.0
    winner_fill_price = 0.0
    winner_fill_ts = 0
    winner_shares = 0
    winner_cost = 0.0

    hedge_fill_price = 0.0
    hedge_shares = 0
    hedge_cost = 0.0
    hedge_is_taker = False

    flip_fill_price = 0.0
    flip_shares = 0
    flip_cost = 0.0
    flip_side = None
    flip_done = False
    flip_hedge_fill_price = 0.0
    flip_hedge_shares = 0
    flip_hedge_cost = 0.0
    flip_hedge_is_taker = False
    total_taker_fees = 0.0

    avg_down_fills = 0
    avg_down_pending = False
    avg_down_bid = 0.0
    avg_down_was_above = False

    # Rise-above guards
    winner_ask_was_above = True  # Start True so first dip can fill (V1 convention)
    hedge_ask_was_above = False
    flip_hedge_was_above = False

    last_fill_ts = 0

    for idx in range(len(mdf)):
        row = mdf.iloc[idx]
        time_rem = float(row['time_remaining_secs'])

        # Parse prices
        w_ask_raw = row.get(winner_ask_col)
        l_ask_raw = row.get(loser_ask_col)
        w_bid_raw = row.get(winner_bid_col)

        if pd.isna(w_ask_raw) or pd.isna(l_ask_raw):
            continue

        winner_ask = float(w_ask_raw)
        loser_ask = float(l_ask_raw)
        winner_cur_bid = float(w_bid_raw) if not pd.isna(w_bid_raw) else None

        # ── STATE: WAITING ──────────────────────────────────────
        if state == "WAITING":
            if config.entry_window_end <= time_rem <= config.entry_window_start:
                if winner_ask > 0:
                    # Bid at ask - offset, FIXED once placed (no dynamic tracking)
                    winner_bid = round(winner_ask - config.bid_offset_cents, 4)
                    if winner_bid > 0:
                        state = "WINNER_BID"
                        winner_ask_was_above = True  # Allow first dip to fill

        # ── STATE: WINNER_BID ───────────────────────────────────
        elif state == "WINNER_BID":
            # Bid is FIXED at ask-offset from WAITING state — no dynamic update

            # Entry window expired without fill
            if time_rem < config.entry_window_end:
                state = "EXPIRED"
                break

            # Rise-above guard
            if winner_ask > winner_bid:
                winner_ask_was_above = True

            # Fill check: ask drops to our bid after being above it
            if winner_ask <= winner_bid and winner_ask_was_above:
                fill_cost = winner_bid * config.base_shares
                if not config.use_capital_constraint or fill_cost <= max_capital:
                    winner_fill_price = winner_bid
                    winner_shares = config.base_shares
                    winner_cost = fill_cost
                    winner_fill_ts = int(row['timestamp_ms'])
                    last_fill_ts = winner_fill_ts
                    state = "WINNER_FILLED"
                    # Initialize hedge: bid at max_hedge_price on loser side
                    max_hedge_price = config.target_pair_cost - winner_fill_price
                    hedge_ask_was_above = (loser_ask > max_hedge_price)

        # ── STATE: WINNER_FILLED ────────────────────────────────
        elif state == "WINNER_FILLED":
            avg_winner_price = winner_cost / winner_shares
            max_hedge_price = config.target_pair_cost - avg_winner_price

            # 1. Check hedge fill (MAKER at max_hedge_price)
            if max_hedge_price > 0:
                if loser_ask > max_hedge_price:
                    hedge_ask_was_above = True

                if loser_ask <= max_hedge_price and hedge_ask_was_above:
                    h_shares = max(1, int(winner_shares * hedge_ratio))
                    h_cost = max_hedge_price * h_shares
                    total_after = winner_cost + h_cost
                    if not config.use_capital_constraint or total_after <= max_capital:
                        hedge_fill_price = max_hedge_price
                        hedge_shares = h_shares
                        hedge_cost = h_cost
                        last_fill_ts = int(row['timestamp_ms'])
                        state = "HEDGED"
                        continue

            # 2. Check avg_down fill (if pending from prior spike)
            if avg_down_pending:
                if winner_ask > avg_down_bid:
                    avg_down_was_above = True
                if winner_ask <= avg_down_bid and avg_down_was_above:
                    add_shares = config.base_shares
                    add_cost = avg_down_bid * add_shares
                    total_after = winner_cost + add_cost
                    if not config.use_capital_constraint or total_after <= max_capital:
                        winner_shares += add_shares
                        winner_cost += add_cost
                        avg_down_fills += 1
                        avg_down_pending = False
                        last_fill_ts = int(row['timestamp_ms'])
                        # Recalculate hedge target
                        avg_winner_price = winner_cost / winner_shares
                        max_hedge_price = config.target_pair_cost - avg_winner_price
                        hedge_ask_was_above = (loser_ask > max_hedge_price)

            # 3. Check spike conditions (trigger avg_down or flip)
            if not avg_down_pending and not flip_done and config.spike_mode != "none":
                avg_winner_price = winner_cost / winner_shares
                drop_from_entry = avg_winner_price - winner_ask
                if drop_from_entry >= config.spike_threshold_cents:
                    if config.spike_mode == "avg_down" and avg_down_fills < config.avg_down_max_additions:
                        # Place avg_down maker bid at current winner ask
                        avg_down_bid = winner_ask
                        avg_down_pending = True
                        avg_down_was_above = False

                    elif config.spike_mode == "flip" and not flip_done:
                        # FLIP: taker buy opposite side
                        flip_side = "DOWN" if predicted_side == "UP" else "UP"
                        flip_shares_target = int(winner_shares * config.flip_multiplier)
                        taker_result = simulate_taker_fill(mdf, idx, flip_side)
                        if taker_result is not None:
                            f_price, f_ts, f_fee = taker_result
                            f_cost = f_price * flip_shares_target
                            f_total_fee = f_fee * flip_shares_target
                            total_after = winner_cost + f_cost + f_total_fee
                            if not config.use_capital_constraint or total_after <= max_capital:
                                flip_fill_price = f_price
                                flip_shares = flip_shares_target
                                flip_cost = f_cost
                                total_taker_fees += f_total_fee
                                flip_done = True
                                last_fill_ts = f_ts
                                state = "FLIP_ENTRY"
                                # Initialize flip hedge guard
                                flip_max_hedge = config.flip_target_pair_cost - flip_fill_price
                                flip_hedge_was_above = (winner_ask > flip_max_hedge)
                                continue

            # 4. Last 90s taker fallback
            if time_rem < config.taker_fallback_time:
                loser_side = "DOWN" if predicted_side == "UP" else "UP"
                taker_result = simulate_taker_fill(mdf, idx, loser_side)
                if taker_result is not None:
                    h_price, h_ts, h_fee = taker_result
                    h_shares = max(1, int(winner_shares * hedge_ratio))
                    h_cost = h_price * h_shares
                    h_total_fee = h_fee * h_shares
                    total_after = winner_cost + h_cost + h_total_fee
                    if not config.use_capital_constraint or total_after <= max_capital:
                        hedge_fill_price = h_price
                        hedge_shares = h_shares
                        hedge_cost = h_cost
                        hedge_is_taker = True
                        total_taker_fees += h_total_fee
                        last_fill_ts = h_ts
                        state = "HEDGED"
                        break

        # ── STATE: FLIP_ENTRY ───────────────────────────────────
        elif state == "FLIP_ENTRY":
            # Flip hedge: MAKER bid on original predicted side
            # (the old winner side, which should be dropping since we flipped)
            flip_max_hedge = config.flip_target_pair_cost - flip_fill_price

            if flip_max_hedge > 0:
                if winner_ask > flip_max_hedge:
                    flip_hedge_was_above = True

                if winner_ask <= flip_max_hedge and flip_hedge_was_above:
                    fh_shares = flip_shares  # Fully pair the flip
                    fh_cost = flip_max_hedge * fh_shares
                    total_after = winner_cost + flip_cost + total_taker_fees + fh_cost
                    if not config.use_capital_constraint or total_after <= max_capital:
                        flip_hedge_fill_price = flip_max_hedge
                        flip_hedge_shares = fh_shares
                        flip_hedge_cost = fh_cost
                        last_fill_ts = int(row['timestamp_ms'])
                        state = "FLIP_HEDGED"
                        continue

            # Last 90s taker fallback for flip hedge
            if time_rem < config.taker_fallback_time:
                taker_result = simulate_taker_fill(mdf, idx, predicted_side)
                if taker_result is not None:
                    fh_price, fh_ts, fh_fee = taker_result
                    fh_shares = flip_shares
                    fh_cost = fh_price * fh_shares
                    fh_total_fee = fh_fee * fh_shares
                    total_after = winner_cost + flip_cost + total_taker_fees + fh_cost + fh_total_fee
                    if not config.use_capital_constraint or total_after <= max_capital:
                        flip_hedge_fill_price = fh_price
                        flip_hedge_shares = fh_shares
                        flip_hedge_cost = fh_cost
                        flip_hedge_is_taker = True
                        total_taker_fees += fh_total_fee
                        last_fill_ts = fh_ts
                        state = "FLIP_HEDGED"
                        break

    # ═══════════════════════════════════════════════════════════════
    # RESOLUTION: Compute PnL
    # Generic formula works for ALL states:
    #   shares_on_predicted = winner_shares + flip_hedge_shares
    #   shares_on_opposite  = hedge_shares  + flip_shares
    #   payout = winning_side_shares * $1.00
    #   pnl    = payout - total_cost
    # ═══════════════════════════════════════════════════════════════
    result.final_state = state
    result.winner_fill_price = round(winner_fill_price, 4)
    result.winner_shares = winner_shares
    result.winner_cost = round(winner_cost, 4)
    result.winner_fill_ts = winner_fill_ts
    result.hedge_fill_price = round(hedge_fill_price, 4)
    result.hedge_shares = hedge_shares
    result.hedge_cost = round(hedge_cost, 4)
    result.hedge_is_taker = hedge_is_taker
    result.avg_down_fills = avg_down_fills
    result.flipped = flip_done
    result.flip_side = flip_side or "NONE"
    result.flip_fill_price = round(flip_fill_price, 4)
    result.flip_shares = flip_shares
    result.flip_cost = round(flip_cost, 4)
    result.flip_hedge_fill_price = round(flip_hedge_fill_price, 4)
    result.flip_hedge_shares = flip_hedge_shares
    result.flip_hedge_cost = round(flip_hedge_cost, 4)
    result.flip_hedge_is_taker = flip_hedge_is_taker
    result.total_taker_fees = round(total_taker_fees, 4)
    result.last_fill_ts = last_fill_ts

    if winner_shares == 0:
        result.total_pnl = 0.0
        return result

    # Total shares by side
    shares_on_predicted = winner_shares + flip_hedge_shares
    shares_on_opposite = hedge_shares + flip_shares
    total_cost = winner_cost + hedge_cost + flip_cost + flip_hedge_cost + total_taker_fees

    if resolution == predicted_side:
        payout = shares_on_predicted * 1.0
    else:
        payout = shares_on_opposite * 1.0

    total_pnl = payout - total_cost
    result.total_pnl = round(total_pnl, 4)

    # Pair cost diagnostic (for HEDGED state)
    if state == "HEDGED" and winner_shares > 0:
        avg_w = winner_cost / winner_shares
        result.pair_cost = round(avg_w + hedge_fill_price, 4)

    return result


# ═══════════════════════════════════════════════════════════════
# SECTION: DATASETS dict
# STATUS: COPY VERBATIM from V1
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
# STATUS: COPY VERBATIM from V1
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
# SECTION: precompute_signals() — BTC EMA + Velocity
# STATUS: ADAPTED from V1 — added velocity computation
# ═══════════════════════════════════════════════════════════════
def precompute_signals(obs_df: pd.DataFrame, config: V2Config) -> pd.DataFrame:
    """
    Pre-compute BTC EMA trend and velocity on the observer data.

    Returns a DataFrame with columns:
    - timestamp_ms, binance_price, ema_short, ema_long, btc_trend, velocity_bps
    """
    btc_cols = ['timestamp_ms', 'binance_price']
    if not all(c in obs_df.columns for c in btc_cols):
        raise ValueError(f"Observer data missing columns: {btc_cols}")

    btc_ts = obs_df[btc_cols].drop_duplicates('timestamp_ms').sort_values('timestamp_ms').copy()
    btc_ts = btc_ts.reset_index(drop=True)

    btc_ts['binance_price'] = pd.to_numeric(btc_ts['binance_price'], errors='coerce')
    btc_ts = btc_ts.dropna(subset=['binance_price']).reset_index(drop=True)

    # Compute EMAs
    btc_ts['ema_short'] = btc_ts['binance_price'].ewm(span=config.ema_short_span, adjust=False).mean()
    btc_ts['ema_long'] = btc_ts['binance_price'].ewm(span=config.ema_long_span, adjust=False).mean()

    # BTC trend: 1 = UP, -1 = DOWN
    btc_ts['btc_trend'] = np.where(btc_ts['ema_short'] > btc_ts['ema_long'], 1, -1)

    # Velocity: bps/sec over 30-tick window
    btc_ts['velocity_bps'] = btc_ts['binance_price'].pct_change(periods=30) * 10000 / 30
    btc_ts['velocity_bps'] = btc_ts['velocity_bps'].fillna(0.0)

    return btc_ts


# ═══════════════════════════════════════════════════════════════
# SECTION: calculate_metrics() — V2-Specific
# STATUS: ADAPTED from V1 — new fields for state machine
# ═══════════════════════════════════════════════════════════════
def calculate_metrics(
    results: List[V2MarketResult],
    duration_hours: float,
    config: V2Config,
    session_result: Optional['SessionResult'] = None,
    cooldown_skips: int = 0,
    obi_skips: int = 0,
) -> Dict:
    if not results:
        return {
            "markets": 0, "markets_with_fills": 0, "total_trades": 0,
            "total_pnl": 0, "pnl_per_hr": 0, "sharpe": 0, "roi_pct": 0,
            "profitable_mkts_pct": 0, "max_drawdown_pct": 0,
            "ending_balance": STARTING_CAPITAL, "worst_market_loss": 0,
            "signal_accuracy": 0, "hedge_rate": 0, "flip_frequency": 0,
            "avg_pair_cost": 0, "total_taker_fees": 0,
            "state_hedged": 0, "state_winner_filled": 0,
            "state_flip_hedged": 0, "state_flip_entry": 0,
            "state_expired": 0, "state_waiting": 0,
            "cooldown_skips": cooldown_skips, "obi_skips": obi_skips,
            "session_stopped": False, "stop_reason": None,
        }

    # Markets with actual winner fills
    traded = [r for r in results if r.winner_shares > 0]
    n_markets = len(results)
    n_traded = len(traded)

    # State distribution
    state_counts = {}
    for r in results:
        state_counts[r.final_state] = state_counts.get(r.final_state, 0) + 1

    # Signal accuracy (of traded markets)
    signal_correct_count = sum(1 for r in traded if r.signal_correct)
    signal_accuracy = (signal_correct_count / n_traded * 100) if n_traded > 0 else 0

    # Hedge rate: % of traded markets that reached HEDGED or FLIP_HEDGED
    hedged_count = sum(1 for r in traded if r.final_state in ("HEDGED", "FLIP_HEDGED"))
    hedge_rate = (hedged_count / n_traded * 100) if n_traded > 0 else 0

    # Flip frequency
    flip_count = sum(1 for r in traded if r.flipped)
    flip_frequency = (flip_count / n_traded * 100) if n_traded > 0 else 0

    # Average pair cost (for HEDGED markets only)
    hedged_results = [r for r in traded if r.final_state == "HEDGED" and r.pair_cost > 0]
    avg_pair_cost = np.mean([r.pair_cost for r in hedged_results]) if hedged_results else 0

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

    # Profitable markets
    profitable = sum(1 for r in results if r.total_pnl > 0)
    profitable_pct = (profitable / n_markets * 100) if n_markets > 0 else 0

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
        "markets_with_fills": n_traded,
        "total_trades": n_traded,
        "total_pnl": round(total_pnl, 2),
        "pnl_per_hr": round(total_pnl / duration_hours, 2) if duration_hours > 0 else 0,
        "sharpe": round(sharpe, 2),
        "roi_pct": round(total_pnl / STARTING_CAPITAL * 100, 1),
        "profitable_mkts_pct": round(profitable_pct, 1),
        "max_drawdown_pct": round(max_dd_pct, 1),
        "ending_balance": round(ending_balance, 2),
        "worst_market_loss": round(worst_market, 2),
        "signal_accuracy": round(signal_accuracy, 1),
        "hedge_rate": round(hedge_rate, 1),
        "flip_frequency": round(flip_frequency, 1),
        "avg_pair_cost": round(avg_pair_cost, 4),
        "total_taker_fees": round(total_fees, 4),
        "state_hedged": state_counts.get("HEDGED", 0),
        "state_winner_filled": state_counts.get("WINNER_FILLED", 0),
        "state_flip_hedged": state_counts.get("FLIP_HEDGED", 0),
        "state_flip_entry": state_counts.get("FLIP_ENTRY", 0),
        "state_expired": state_counts.get("EXPIRED", 0),
        "state_waiting": state_counts.get("WAITING", 0),
        "cooldown_skips": cooldown_skips,
        "obi_skips": obi_skips,
        "session_stopped": session_stopped,
        "stop_reason": stop_reason,
    }


# ═══════════════════════════════════════════════════════════════
# SECTION: SessionResult + run_backtest_with_session_stops()
# STATUS: ADAPTED from V1 — V2 signal logic (laddered EMA+OBI+velocity
#         and gabagool), inverse sizing, cooldown
# ═══════════════════════════════════════════════════════════════
@dataclass
class SessionResult:
    results: List[V2MarketResult]
    session_stopped: bool
    markets_before_stop: int
    final_session_pnl: float
    session_peak_pnl: float
    stop_reason: Optional[str]
    adaptive_activated: bool = False
    pnl_at_check: Optional[float] = None
    cooldown_skips: int = 0
    obi_skips: int = 0


def run_backtest_with_session_stops(
    config: V2Config,
    obs_df: pd.DataFrame,
    markets_with_res: List[str],
    resolutions: Dict[str, str],
    dataset_name: str,
    btc_ts: pd.DataFrame,
) -> SessionResult:
    """
    Run V2 backtest with session-level stops and cross-market state.

    Signal determination per market:
    - ema_obi: BTC EMA direction + OBI contrarian + velocity → confidence
    - gabagool: predict side with higher ask (market consensus)
    """
    session_pnl = 0.0
    session_peak_pnl = 0.0
    session_stopped = False
    stop_reason = None
    all_results = []
    markets_before_stop = 0

    # Capital tracking
    current_balance = STARTING_CAPITAL

    # Adaptive stop state (VERBATIM from V1)
    adaptive_activated = False
    adaptive_checked = False
    pnl_at_check = None
    active_dd_pct = config.session_dd_pct
    active_loss_limit = config.session_loss_limit

    # Cross-market state
    last_fill_ts = 0
    cooldown_skips = 0
    obi_skips = 0

    # Pre-compute OBI availability
    has_obi = 'up_imbalance' in obs_df.columns and 'down_imbalance' in obs_df.columns

    # Pre-compute btc_ts arrays for fast lookups
    btc_timestamps = btc_ts['timestamp_ms'].values
    btc_trends = btc_ts['btc_trend'].values
    btc_velocities = btc_ts['velocity_bps'].values

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
        mdf = obs_df[obs_df['market_slug'] == market_slug]

        if len(mdf) == 0:
            continue

        # ── 1. Get market entry timestamp ──
        # Use first observation in the entry window
        entry_window_mask = (
            (mdf['time_remaining_secs'] >= config.entry_window_end) &
            (mdf['time_remaining_secs'] <= config.entry_window_start)
        )
        entry_rows = mdf[entry_window_mask]
        if len(entry_rows) == 0:
            continue
        entry_row = entry_rows.iloc[0]
        entry_ts = int(entry_row['timestamp_ms'])

        # ── 2. Cooldown check ──
        cooldown_ms = config.cooldown_minutes * 60 * 1000
        if last_fill_ts > 0 and (entry_ts - last_fill_ts) < cooldown_ms:
            cooldown_skips += 1
            continue

        # ── 3. Signal determination ──
        if config.signal_mode == "gabagool":
            # Gabagool: predict side with higher ask (market consensus)
            up_ask_val = entry_row.get('up_ask')
            down_ask_val = entry_row.get('down_ask')
            if pd.isna(up_ask_val) or pd.isna(down_ask_val):
                continue
            up_ask_val = float(up_ask_val)
            down_ask_val = float(down_ask_val)
            predicted_side = "UP" if up_ask_val >= down_ask_val else "DOWN"
            confidence_level = "MEDIUM"  # Gabagool always MEDIUM

        elif config.signal_mode == "ema_obi":
            # Layer 1: BTC EMA crossover
            nearest_idx = np.searchsorted(btc_timestamps, entry_ts)
            nearest_idx = min(nearest_idx, len(btc_trends) - 1)
            btc_trend = btc_trends[nearest_idx]
            predicted_side = "UP" if btc_trend == 1 else "DOWN"

            # Layer 2: OBI contrarian
            obi_contrarian = False
            if has_obi:
                up_imb = entry_row.get('up_imbalance')
                down_imb = entry_row.get('down_imbalance')
                if not pd.isna(up_imb) and not pd.isna(down_imb):
                    net_obi = float(up_imb) - float(down_imb)
                    obi_direction = 1 if net_obi > 0 else -1
                    obi_contrarian = (obi_direction != btc_trend)
            else:
                # No OBI data → skip for ema_obi mode
                obi_skips += 1
                continue

            # Layer 3: Velocity confirmation
            velocity = btc_velocities[nearest_idx]
            velocity_confirms = (
                (btc_trend == 1 and velocity > 0) or
                (btc_trend == -1 and velocity < 0)
            )

            # Confidence: count agreeing layers (EMA always = 1 base)
            layers = 1  # EMA base
            if obi_contrarian:
                layers += 1
            if velocity_confirms:
                layers += 1

            if layers >= 3:
                confidence_level = "HIGH"
            elif layers >= 2:
                confidence_level = "MEDIUM"
            else:
                # LOW confidence → skip (only EMA, no confirmation)
                obi_skips += 1
                continue

        else:
            # Fallback: ema_only (use EMA direction, MEDIUM confidence)
            nearest_idx = np.searchsorted(btc_timestamps, entry_ts)
            nearest_idx = min(nearest_idx, len(btc_trends) - 1)
            btc_trend = btc_trends[nearest_idx]
            predicted_side = "UP" if btc_trend == 1 else "DOWN"
            confidence_level = "MEDIUM"

        # ── 4. Simulate market ──
        market_result = simulate_market(
            obs_df, market_slug, resolution, config, dataset_name,
            current_balance=current_balance,
            predicted_side=predicted_side,
            confidence_level=confidence_level,
        )

        # ── 5. Post-resolution: update cross-market state ──
        if market_result.winner_shares > 0:
            session_pnl += market_result.total_pnl
            session_peak_pnl = max(session_peak_pnl, session_pnl)
            markets_before_stop += 1
            current_balance = STARTING_CAPITAL + session_pnl

            if market_result.last_fill_ts > 0:
                last_fill_ts = market_result.last_fill_ts

            # Adaptive check (VERBATIM from V1)
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

            # Check session stops (VERBATIM from V1)
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
        obi_skips=obi_skips,
    )


# ═══════════════════════════════════════════════════════════════
# SECTION: generate_grid_configs() — V2.1 Refined (baseline only)
# STATUS: UPDATED — spike_mode=none won. Now grid on bid_offset.
# ═══════════════════════════════════════════════════════════════
def generate_grid_configs() -> List[V2Config]:
    """
    V2.1 grid: baseline only (no spike response). Sweep bid_offset.

    2 signals × 2 pair_cost × 5 bid_offsets = 20 configs.

    Parameters varied:
    - signal_mode: ema_obi, gabagool
    - target_pair_cost: 0.96, 0.98
    - bid_offset_cents: 0.01, 0.02, 0.03, 0.04, 0.05
    """
    configs = []
    signal_modes = ["ema_obi", "gabagool"]
    target_pair_costs = [0.96, 0.98]
    bid_offsets = [0.01, 0.02, 0.03, 0.04, 0.05]

    for signal in signal_modes:
        sig_tag = "EO" if signal == "ema_obi" else "GAB"
        for tpc in target_pair_costs:
            tpc_tag = f"P{int(tpc * 100)}"
            for offset in bid_offsets:
                o_tag = f"{int(offset * 100)}c"
                configs.append(V2Config(
                    name=f"V2_{sig_tag}_{tpc_tag}_off{o_tag}",
                    signal_mode=signal,
                    target_pair_cost=tpc,
                    bid_offset_cents=offset,
                    spike_mode="none",
                ))

    return configs


# ═══════════════════════════════════════════════════════════════
# SECTION: main()
# STATUS: ADAPTED from V1 — V2 branding, grid, output paths
# ═══════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='OOS7', help='Comma-separated: OOS7,OOS9 or "all"')
    parser.add_argument('--output', default='research/findings/data/directional_maker_v2_1_results.csv')
    parser.add_argument('--checkpoint', default='research/findings/data/directional_maker_v2_1_checkpoint.csv')
    args = parser.parse_args()

    print("=" * 80)
    print("DIRECTIONAL MAKER V2.1 GRID SEARCH (Feb 11, 2026)")
    print("V2.0: spike_mode=none won. V2.1: bid at ask-offset, sweep offsets.")
    print("Strategy: Winner-first maker (ask-offset bid) + baseline (no spike)")
    print("=" * 80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Starting Capital: ${STARTING_CAPITAL}")
    print(f"Max Capital/Market: {MAX_CAPITAL_FRACTION*100:.0f}% of current balance")
    print(f"Min Time Remaining: {MIN_TIME}s")
    print(f"Entry Window: {800}s - {300}s remaining")
    print(f"Taker Delay: {TAKER_DELAY_MS}ms")

    all_configs = generate_grid_configs()
    print(f"\nTotal configs: {len(all_configs)}")

    for c in all_configs[:4]:
        print(f"  - {c.name}: signal={c.signal_mode}, pair_cost={c.target_pair_cost}, "
              f"bid_offset={c.bid_offset_cents}")
    if len(all_configs) > 4:
        print(f"  ... and {len(all_configs) - 4} more")

    if args.data == 'all':
        datasets = list(DATASETS.keys())
    else:
        datasets = [d.strip() for d in args.data.split(',')]
    all_results = []

    for dataset_key in datasets:
        obs_df, resolutions, duration_hours = load_dataset(dataset_key)

        if obs_df is None:
            continue

        markets = obs_df['market_slug'].unique()
        markets_with_res = [m for m in markets if m in resolutions]
        assert len(markets_with_res) > 0, f"No matched markets for {dataset_key}!"
        print(f"  Markets with resolution: {len(markets_with_res)}")

        # Check OBI availability
        has_obi = 'up_imbalance' in obs_df.columns and 'down_imbalance' in obs_df.columns
        print(f"  OBI columns available: {has_obi}")

        # Filter configs: ema_obi only on datasets WITH OBI
        dataset_configs = []
        for c in all_configs:
            if c.signal_mode == "ema_obi" and not has_obi:
                continue
            dataset_configs.append(c)
        print(f"  Configs for this dataset: {len(dataset_configs)} "
              f"(skipped {len(all_configs) - len(dataset_configs)} OBI configs)")

        # Pre-compute signals for EACH unique EMA scale (ONCE, reused)
        ema_cache = {}
        for c in dataset_configs:
            key = (c.ema_short_span, c.ema_long_span)
            if key not in ema_cache:
                ema_cache[key] = precompute_signals(obs_df, c)

        # Show BTC trend distribution
        for key, btc_ts in ema_cache.items():
            up_pct = (btc_ts['btc_trend'] == 1).mean() * 100
            down_pct = (btc_ts['btc_trend'] == -1).mean() * 100
            print(f"  EMA({key[0]},{key[1]}): trend UP={up_pct:.1f}%, DOWN={down_pct:.1f}%")

        print(f"\n  Running {len(dataset_configs)} configs on {dataset_key}...")

        for i, config in enumerate(tqdm(dataset_configs, desc=f"  {dataset_key}")):
            ema_key = (config.ema_short_span, config.ema_long_span)
            btc_ts = ema_cache[ema_key]

            session_result = run_backtest_with_session_stops(
                config=config,
                obs_df=obs_df,
                markets_with_res=markets_with_res,
                resolutions=resolutions,
                dataset_name=dataset_key,
                btc_ts=btc_ts,
            )

            metrics = calculate_metrics(
                session_result.results, duration_hours, config, session_result,
                cooldown_skips=session_result.cooldown_skips,
                obi_skips=session_result.obi_skips,
            )
            metrics['config_name'] = config.name
            metrics['dataset'] = dataset_key
            metrics['signal_mode'] = config.signal_mode
            metrics['target_pair_cost'] = config.target_pair_cost
            metrics['bid_offset_cents'] = config.bid_offset_cents
            metrics['spike_mode'] = config.spike_mode
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
        print("DIRECTIONAL MAKER V2 RESULTS SUMMARY")
        print("=" * 80)

        for dataset in results_df['dataset'].unique():
            print(f"\n  {dataset}:")
            subset = results_df[results_df['dataset'] == dataset].copy()
            subset = subset.sort_values('total_pnl', ascending=False)

            cols = ['config_name', 'total_trades', 'signal_accuracy',
                    'hedge_rate', 'total_pnl', 'pnl_per_hr', 'sharpe',
                    'profitable_mkts_pct', 'max_drawdown_pct',
                    'avg_pair_cost', 'flip_frequency',
                    'worst_market_loss', 'ending_balance']
            available_cols = [c for c in cols if c in subset.columns]
            print(subset[available_cols].head(10).to_string(index=False))

        # Cross-dataset summary
        if len(results_df['dataset'].unique()) > 1:
            print("\n" + "=" * 80)
            print("CROSS-DATASET SUMMARY (Combined PnL)")
            print("=" * 80)
            combined = results_df.groupby('config_name').agg({
                'total_pnl': 'sum',
                'total_trades': 'sum',
                'signal_accuracy': 'mean',
                'hedge_rate': 'mean',
                'profitable_mkts_pct': 'mean',
                'max_drawdown_pct': 'max',
                'total_taker_fees': 'sum',
                'cooldown_skips': 'sum',
                'obi_skips': 'sum',
            }).round(2)
            combined = combined.sort_values('total_pnl', ascending=False)
            print(combined.head(15).to_string())

        # Analysis by signal mode
        print("\n" + "=" * 80)
        print("ANALYSIS BY SIGNAL MODE")
        print("=" * 80)
        by_signal = results_df.groupby('signal_mode').agg({
            'total_pnl': ['sum', 'mean'],
            'signal_accuracy': 'mean',
            'hedge_rate': 'mean',
            'total_trades': 'sum',
            'total_taker_fees': 'sum',
        }).round(2)
        print(by_signal.to_string())

        # Analysis by bid offset
        print("\n" + "=" * 80)
        print("ANALYSIS BY BID OFFSET")
        print("=" * 80)
        by_offset = results_df.groupby('bid_offset_cents').agg({
            'total_pnl': ['sum', 'mean'],
            'total_trades': ['sum', 'mean'],
            'hedge_rate': 'mean',
            'signal_accuracy': 'mean',
            'total_taker_fees': 'sum',
        }).round(2)
        print(by_offset.to_string())

        # Analysis by target pair cost
        print("\n" + "=" * 80)
        print("ANALYSIS BY TARGET PAIR COST")
        print("=" * 80)
        by_tpc = results_df.groupby('target_pair_cost').agg({
            'total_pnl': ['sum', 'mean'],
            'hedge_rate': 'mean',
            'avg_pair_cost': 'mean',
            'total_trades': 'sum',
        }).round(2)
        print(by_tpc.to_string())

        # State distribution across all results
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
        print("MANDATORY STABILITY METRICS (Top 5 configs by PnL)")
        print("=" * 80)
        top5 = results_df.sort_values('total_pnl', ascending=False).head(5)
        for _, row in top5.iterrows():
            print(f"\n  {row['config_name']} ({row['dataset']}):")
            print(f"    Total PnL: ${row['total_pnl']:.2f}")
            print(f"    PnL/hr: ${row['pnl_per_hr']:.2f}")
            print(f"    Sharpe: {row['sharpe']:.2f}")
            print(f"    Profitable markets: {row['profitable_mkts_pct']:.1f}%")
            print(f"    Worst market loss: ${row['worst_market_loss']:.2f}")
            print(f"    Max drawdown: {row['max_drawdown_pct']:.1f}%")
            print(f"    Signal accuracy: {row['signal_accuracy']:.1f}%")
            print(f"    Hedge rate: {row['hedge_rate']:.1f}%")
            print(f"    Taker fees: ${row['total_taker_fees']:.4f}")


if __name__ == "__main__":
    main()
