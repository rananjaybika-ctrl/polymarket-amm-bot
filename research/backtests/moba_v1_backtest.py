#!/usr/bin/env python3
"""
Multi-Phase Observation-Based Accumulation (MOBA) V1 Backtest

=============================================================================
COPIED FROM: directional_maker_v2_2_backtest.py (V2.2 validated execution engine)
MODIFIED: Signal replaced (EMA crossover -> expensive side observation),
          state machine replaced (winner-first -> 4-phase accumulation),
          all maker entries (0% fee), no signal prediction needed.
=============================================================================

Strategy:
  1. Phase 1 (800-600s): MAKER bid on BOTH sides at ask - 3c (hedge foundation)
  2. Phase 2 (600-400s): MAKER bid on expensive side at ask - 1c (initial tilt)
  3. Phase 3 (400-200s): MAKER bid on expensive side if ask >= threshold (confirmation)
  4. Phase 4 (200-90s):  MAKER bid on expensive side if ask >= threshold (high confidence)
  5. All orders are MAKER (0% fee). No taker entries.
  6. Hold ALL positions to resolution.

Key insight: Polymarket's expensive side IS the signal (66.7% at 800s -> 91.3% at 100s).
Hedge foundation from Phase 1 protects against wrong calls. Phases 2-4 tilt toward winner.

Usage:
    python research/backtests/moba_v1_backtest.py --data OOS7
    python research/backtests/moba_v1_backtest.py --data train
    python research/backtests/moba_v1_backtest.py --data all
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
# SECTION: MOBAConfig dataclass
# STATUS: NEW — replaces V2Config
# ═══════════════════════════════════════════════════════════════
@dataclass
class MOBAConfig:
    name: str
    # Phase 1: Both sides (hedge foundation)
    phase1_start: float = 800.0
    phase1_end: float = 600.0
    phase1_offset: float = 0.03    # bid at ask - offset on BOTH sides
    phase1_shares: int = 5
    # Phase 2: Expensive side (initial tilt)
    phase2_start: float = 600.0
    phase2_end: float = 400.0
    phase2_offset: float = 0.01    # tighter offset (research: ask-1c = 81% fill)
    phase2_threshold: float = 0.55  # min expensive_ask to place Phase 2 bid
    phase2_shares: int = 5
    # Phase 3: Expensive side (confirmation)
    phase3_start: float = 400.0
    phase3_end: float = 200.0
    phase3_offset: float = 0.01
    phase3_threshold: float = 0.65
    phase3_shares: int = 5
    # Phase 4: Expensive side (high confidence)
    phase4_start: float = 200.0
    phase4_end: float = 90.0
    phase4_offset: float = 0.01
    phase4_threshold: float = 0.80
    phase4_shares: int = 5
    # General
    min_time_remaining: float = MIN_TIME
    cooldown_minutes: float = 3.0
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
    # Maker stop: sell excess shares when expensive side flips
    stop_enabled: bool = False
    stop_check_time: float = 300.0   # time remaining to trigger check
    stop_sell_offset: float = 0.01   # ask at bid + offset (maker sell)


# ═══════════════════════════════════════════════════════════════
# SECTION: MOBAMarketResult dataclass
# STATUS: NEW — tracks 4-phase accumulation
# ═══════════════════════════════════════════════════════════════
@dataclass
class MOBAMarketResult:
    market_slug: str
    resolution: str
    dataset: str
    config_name: str

    # Phase 1 fills
    p1_up_filled: bool = False
    p1_up_price: float = 0.0
    p1_down_filled: bool = False
    p1_down_price: float = 0.0

    # Phase 2 fill
    p2_filled: bool = False
    p2_side: str = "NONE"
    p2_price: float = 0.0

    # Phase 3 fill
    p3_filled: bool = False
    p3_side: str = "NONE"
    p3_price: float = 0.0

    # Phase 4 fill
    p4_filled: bool = False
    p4_side: str = "NONE"
    p4_price: float = 0.0

    # Aggregated shares
    up_shares: int = 0
    up_cost: float = 0.0
    down_shares: int = 0
    down_cost: float = 0.0
    total_cost: float = 0.0

    # Market info
    expensive_side_at_entry: str = "NONE"
    flips_observed: int = 0

    # PnL
    total_pnl: float = 0.0

    # Timing
    last_fill_ts: int = 0
    phases_filled: int = 0
    skip_reason: str = ""

    # Stop info
    stop_triggered: bool = False
    stop_shares_sold: int = 0
    stop_sell_price: float = 0.0
    stop_sell_proceeds: float = 0.0
    stop_filled: bool = False


# ═══════════════════════════════════════════════════════════════
# SECTION: check_session_stop()
# STATUS: COPY VERBATIM from V2.2 (only type hint changed)
# ═══════════════════════════════════════════════════════════════
def check_session_stop(config: MOBAConfig, session_pnl: float, session_peak_pnl: float) -> bool:
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
# STATUS: COPY VERBATIM from V2.2 (kept for reference, not used)
# ═══════════════════════════════════════════════════════════════
def simulate_taker_fill(
    mdf: pd.DataFrame,
    current_idx: int,
    side: str,
) -> Optional[Tuple[float, int, float]]:
    """
    Simulate taker fill with 542ms delay. Kept for reference.
    MOBA uses only maker fills — this function is not called.
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
# SECTION: simulate_market() — MOBA 4-Phase Accumulation
# STATUS: NEW — replaces V2.2's winner-first state machine
# ═══════════════════════════════════════════════════════════════
def simulate_market(
    obs_df: pd.DataFrame,
    slug: str,
    resolution: str,
    config: MOBAConfig,
    dataset_name: str,
    current_balance: float,
) -> MOBAMarketResult:
    """
    4-phase observation-based accumulation for a single market.

    Phase 1 (800-600s): MAKER bid on BOTH sides at ask-offset (hedge foundation)
    Phase 2 (600-400s): MAKER bid on EXPENSIVE side at ask-offset (tilt)
    Phase 3 (400-200s): MAKER bid on EXPENSIVE side if ask >= threshold
    Phase 4 (200-90s):  MAKER bid on EXPENSIVE side if ask >= threshold

    All bids FIXED once placed. All fills are MAKER (0% fee).
    Rise-above guard: fill only after ask has been above bid price.
    """
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    result = MOBAMarketResult(
        market_slug=slug,
        resolution=resolution,
        dataset=dataset_name,
        config_name=config.name,
    )

    if len(mdf) == 0:
        return result

    # Capital constraint
    max_capital = config.max_capital_fraction * current_balance if config.use_capital_constraint else float('inf')

    # Share tracking by side
    up_shares = 0
    up_cost = 0.0
    down_shares = 0
    down_cost = 0.0

    # Pending bids: list of dicts with keys:
    #   side, price, shares, was_above, filled, placed_idx, phase, fill_ts
    pending_bids = []

    # Phase activation flags
    p1_placed = False
    p2_placed = False
    p3_placed = False
    p4_placed = False

    # Tracking
    last_fill_ts = 0
    expensive_side_at_entry = None
    prev_expensive_side = None
    flips_observed = 0

    # Maker stop state
    stop_triggered = False
    stop_check_done = False
    pending_sells = []
    sell_proceeds = 0.0

    for idx in range(len(mdf)):
        row = mdf.iloc[idx]
        time_rem = float(row['time_remaining_secs'])

        up_ask_raw = row.get('up_ask')
        down_ask_raw = row.get('down_ask')
        if pd.isna(up_ask_raw) or pd.isna(down_ask_raw):
            continue

        up_ask = float(up_ask_raw)
        down_ask = float(down_ask_raw)

        if up_ask <= 0 or up_ask >= 1.0 or down_ask <= 0 or down_ask >= 1.0:
            continue

        # Determine expensive side
        expensive_side = "UP" if up_ask >= down_ask else "DOWN"
        expensive_ask = max(up_ask, down_ask)

        if expensive_side_at_entry is None:
            expensive_side_at_entry = expensive_side

        # Track flips
        if prev_expensive_side is not None and expensive_side != prev_expensive_side:
            flips_observed += 1
        prev_expensive_side = expensive_side

        current_total_cost = up_cost + down_cost

        # ── Step 1: Check fills for all pending bids ──
        for bid in pending_bids:
            if bid['filled']:
                continue
            if bid['placed_idx'] >= idx:
                continue  # Don't fill on same tick as placement

            bid_side = bid['side']
            bid_price = bid['price']
            ask_now = up_ask if bid_side == "UP" else down_ask

            # Rise-above guard update
            if ask_now > bid_price:
                bid['was_above'] = True

            # Fill check
            if ask_now <= bid_price and bid['was_above']:
                fill_cost = bid_price * bid['shares']
                total_after = current_total_cost + fill_cost

                # Capital constraint
                if config.use_capital_constraint and total_after > max_capital:
                    continue

                # Polymarket min order value
                if fill_cost < POLY_MIN_ORDER_VALUE:
                    continue

                # FILL! (0% maker fee)
                bid['filled'] = True
                bid['fill_ts'] = int(row['timestamp_ms'])

                if bid_side == "UP":
                    up_shares += bid['shares']
                    up_cost += fill_cost
                else:
                    down_shares += bid['shares']
                    down_cost += fill_cost

                current_total_cost = up_cost + down_cost
                last_fill_ts = int(row['timestamp_ms'])

        # ── Step 1b: Check fills for pending sells ──
        for sell in pending_sells:
            if sell['filled'] or sell['placed_idx'] >= idx:
                continue
            sell_side = sell['side']
            bid_col = 'up_bid' if sell_side == "UP" else 'down_bid'
            bid_raw = row.get(bid_col)
            if pd.isna(bid_raw):
                continue
            bid_now = float(bid_raw)
            if bid_now <= 0:
                continue
            if bid_now >= sell['price']:
                # FILL! (0% maker fee)
                sell['filled'] = True
                sell['fill_ts'] = int(row['timestamp_ms'])
                proceeds = sell['price'] * sell['shares']
                sell_proceeds += proceeds
                if sell_side == "UP":
                    up_shares -= sell['shares']
                else:
                    down_shares -= sell['shares']

        # ── Step 2: Place new bids based on phase transitions ──

        # Phase 1: Both sides (hedge foundation)
        if not p1_placed and config.phase1_end <= time_rem <= config.phase1_start:
            p1_placed = True

            p1_up_price = round(up_ask - config.phase1_offset, 4)
            p1_down_price = round(down_ask - config.phase1_offset, 4)

            if p1_up_price > 0:
                pending_bids.append({
                    'side': 'UP', 'price': p1_up_price, 'shares': config.phase1_shares,
                    'was_above': True, 'filled': False, 'placed_idx': idx,
                    'phase': 1, 'fill_ts': 0,
                })
            if p1_down_price > 0:
                pending_bids.append({
                    'side': 'DOWN', 'price': p1_down_price, 'shares': config.phase1_shares,
                    'was_above': True, 'filled': False, 'placed_idx': idx,
                    'phase': 1, 'fill_ts': 0,
                })

        # Phase 2: Expensive side (initial tilt)
        if not p2_placed and config.phase2_end <= time_rem < config.phase2_start:
            if expensive_ask >= config.phase2_threshold:
                p2_placed = True
                exp_ask_now = up_ask if expensive_side == "UP" else down_ask
                p2_price = round(exp_ask_now - config.phase2_offset, 4)

                if p2_price > 0:
                    pending_bids.append({
                        'side': expensive_side, 'price': p2_price,
                        'shares': config.phase2_shares,
                        'was_above': True, 'filled': False, 'placed_idx': idx,
                        'phase': 2, 'fill_ts': 0,
                    })

        # Phase 3: Expensive side (confirmation)
        if not p3_placed and config.phase3_end <= time_rem < config.phase3_start:
            if expensive_ask >= config.phase3_threshold:
                p3_placed = True
                exp_ask_now = up_ask if expensive_side == "UP" else down_ask
                p3_price = round(exp_ask_now - config.phase3_offset, 4)

                if p3_price > 0:
                    pending_bids.append({
                        'side': expensive_side, 'price': p3_price,
                        'shares': config.phase3_shares,
                        'was_above': True, 'filled': False, 'placed_idx': idx,
                        'phase': 3, 'fill_ts': 0,
                    })

        # Phase 4: Expensive side (high confidence)
        if not p4_placed and config.phase4_end <= time_rem < config.phase4_start:
            if expensive_ask >= config.phase4_threshold:
                p4_placed = True
                exp_ask_now = up_ask if expensive_side == "UP" else down_ask
                p4_price = round(exp_ask_now - config.phase4_offset, 4)

                if p4_price > 0:
                    pending_bids.append({
                        'side': expensive_side, 'price': p4_price,
                        'shares': config.phase4_shares,
                        'was_above': True, 'filled': False, 'placed_idx': idx,
                        'phase': 4, 'fill_ts': 0,
                    })

        # ── Step 3: Maker stop check ──
        if config.stop_enabled and not stop_check_done:
            if time_rem <= config.stop_check_time:
                stop_check_done = True  # Only check once

                # Determine tilt side (side with more filled shares)
                if up_shares > down_shares:
                    tilt_side = "UP"
                    excess = up_shares - down_shares
                elif down_shares > up_shares:
                    tilt_side = "DOWN"
                    excess = down_shares - up_shares
                else:
                    tilt_side = None
                    excess = 0

                if excess > 0 and tilt_side is not None:
                    # Expensive side flipped AWAY from our tilt = we're likely wrong
                    if expensive_side != tilt_side:
                        stop_triggered = True

                        # Place maker sell for excess shares
                        bid_col = 'up_bid' if tilt_side == "UP" else 'down_bid'
                        tilt_bid_raw = row.get(bid_col)
                        if not pd.isna(tilt_bid_raw):
                            tilt_bid = float(tilt_bid_raw)
                            sell_price = round(tilt_bid + config.stop_sell_offset, 4)
                            if 0 < sell_price < 1.0:
                                pending_sells.append({
                                    'side': tilt_side, 'price': sell_price,
                                    'shares': excess, 'filled': False,
                                    'placed_idx': idx, 'fill_ts': 0,
                                })

                        # Cancel unfilled Phase 2-4 bids on tilt side
                        for bid in pending_bids:
                            if not bid['filled'] and bid['side'] == tilt_side and bid['phase'] > 1:
                                bid['filled'] = True  # Prevent future fills

    # ═══════════════════════════════════════════════════════════════
    # RESOLUTION: Compute PnL
    # pnl = payout + sell_proceeds - total_buy_cost (all maker, 0% fee)
    # ═══════════════════════════════════════════════════════════════
    total_cost = up_cost + down_cost  # Gross buy cost (does NOT include sell proceeds)

    # Extract per-phase fill info
    p1_up_fill = next((b for b in pending_bids if b['phase'] == 1 and b['side'] == 'UP' and b['filled']), None)
    p1_down_fill = next((b for b in pending_bids if b['phase'] == 1 and b['side'] == 'DOWN' and b['filled']), None)
    p2_fill = next((b for b in pending_bids if b['phase'] == 2 and b['filled']), None)
    p3_fill = next((b for b in pending_bids if b['phase'] == 3 and b['filled']), None)
    p4_fill = next((b for b in pending_bids if b['phase'] == 4 and b['filled']), None)

    result.p1_up_filled = p1_up_fill is not None
    result.p1_up_price = round(p1_up_fill['price'], 4) if p1_up_fill else 0.0
    result.p1_down_filled = p1_down_fill is not None
    result.p1_down_price = round(p1_down_fill['price'], 4) if p1_down_fill else 0.0
    result.p2_filled = p2_fill is not None
    result.p2_side = p2_fill['side'] if p2_fill else "NONE"
    result.p2_price = round(p2_fill['price'], 4) if p2_fill else 0.0
    result.p3_filled = p3_fill is not None
    result.p3_side = p3_fill['side'] if p3_fill else "NONE"
    result.p3_price = round(p3_fill['price'], 4) if p3_fill else 0.0
    result.p4_filled = p4_fill is not None
    result.p4_side = p4_fill['side'] if p4_fill else "NONE"
    result.p4_price = round(p4_fill['price'], 4) if p4_fill else 0.0

    result.up_shares = up_shares
    result.up_cost = round(up_cost, 4)
    result.down_shares = down_shares
    result.down_cost = round(down_cost, 4)
    result.total_cost = round(total_cost, 4)
    result.expensive_side_at_entry = expensive_side_at_entry or "NONE"
    result.flips_observed = flips_observed
    result.last_fill_ts = last_fill_ts

    phases_filled = sum([
        result.p1_up_filled or result.p1_down_filled,
        result.p2_filled,
        result.p3_filled,
        result.p4_filled,
    ])
    result.phases_filled = phases_filled

    # Stop result tracking
    result.stop_triggered = stop_triggered
    stop_sell = next((s for s in pending_sells), None)
    if stop_sell:
        result.stop_shares_sold = stop_sell['shares']
        result.stop_sell_price = round(stop_sell['price'], 4)
        result.stop_filled = stop_sell['filled']
        result.stop_sell_proceeds = round(stop_sell['price'] * stop_sell['shares'], 4) if stop_sell['filled'] else 0.0

    if up_shares == 0 and down_shares == 0:
        # All shares sold or never bought
        result.total_pnl = round(sell_proceeds - total_cost, 4) if sell_proceeds > 0 else 0.0
        return result

    # PnL: payout on winning side + sell proceeds - total buy cost
    if resolution == "UP":
        payout = up_shares * 1.0
    else:
        payout = down_shares * 1.0

    result.total_pnl = round(payout + sell_proceeds - total_cost, 4)

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
# SECTION: calculate_metrics() — MOBA-Specific
# STATUS: ADAPTED from V2.2 — phase fill rates, tilt ratio
# ═══════════════════════════════════════════════════════════════
def calculate_metrics(
    results: List[MOBAMarketResult],
    duration_hours: float,
    config: MOBAConfig,
    session_result: Optional['SessionResult'] = None,
    cooldown_skips: int = 0,
) -> Dict:
    if not results:
        return {
            "markets": 0, "markets_traded": 0, "total_pnl": 0,
            "pnl_per_hr": 0, "sharpe": 0, "roi_pct": 0, "win_rate": 0,
            "max_drawdown_pct": 0, "ending_balance": STARTING_CAPITAL,
            "worst_market_loss": 0, "p1_both_fill_rate": 0,
            "p2_fill_rate": 0, "p3_fill_rate": 0, "p4_fill_rate": 0,
            "avg_up_shares": 0, "avg_down_shares": 0, "avg_tilt_ratio": 0.5,
            "avg_p1_pair_cost": 0, "avg_total_cost": 0, "avg_flips": 0,
            "cooldown_skips": cooldown_skips,
            "session_stopped": False, "stop_reason": None,
            "stops_triggered": 0, "stops_filled": 0,
            "stop_trigger_rate": 0, "stop_fill_rate": 0,
            "total_sell_proceeds": 0,
        }

    # Markets with any fills
    traded = [r for r in results if r.up_shares > 0 or r.down_shares > 0]
    n_markets = len(results)
    n_traded = len(traded)

    if n_traded == 0:
        return {
            "markets": n_markets, "markets_traded": 0, "total_pnl": 0,
            "pnl_per_hr": 0, "sharpe": 0, "roi_pct": 0, "win_rate": 0,
            "max_drawdown_pct": 0, "ending_balance": STARTING_CAPITAL,
            "worst_market_loss": 0, "p1_both_fill_rate": 0,
            "p2_fill_rate": 0, "p3_fill_rate": 0, "p4_fill_rate": 0,
            "avg_up_shares": 0, "avg_down_shares": 0, "avg_tilt_ratio": 0.5,
            "avg_p1_pair_cost": 0, "avg_total_cost": 0, "avg_flips": 0,
            "cooldown_skips": cooldown_skips,
            "session_stopped": False, "stop_reason": None,
            "stops_triggered": 0, "stops_filled": 0,
            "stop_trigger_rate": 0, "stop_fill_rate": 0,
            "total_sell_proceeds": 0,
        }

    # Phase fill rates (of traded markets)
    p1_both_rate = sum(1 for r in traded if r.p1_up_filled and r.p1_down_filled) / n_traded * 100
    p2_rate = sum(1 for r in traded if r.p2_filled) / n_traded * 100
    p3_rate = sum(1 for r in traded if r.p3_filled) / n_traded * 100
    p4_rate = sum(1 for r in traded if r.p4_filled) / n_traded * 100

    # Win rate (PnL > 0)
    win_rate = sum(1 for r in traded if r.total_pnl > 0) / n_traded * 100

    # Average shares
    avg_up_shares = np.mean([r.up_shares for r in traded])
    avg_down_shares = np.mean([r.down_shares for r in traded])

    # Average tilt ratio (max_side_shares / total_shares)
    tilt_ratios = []
    for r in traded:
        total = r.up_shares + r.down_shares
        if total > 0:
            tilt_ratios.append(max(r.up_shares, r.down_shares) / total)
    avg_tilt = np.mean(tilt_ratios) if tilt_ratios else 0.5

    # Phase 1 pair cost (both filled)
    p1_pair_costs = []
    for r in traded:
        if r.p1_up_filled and r.p1_down_filled:
            p1_pair_costs.append(r.p1_up_price + r.p1_down_price)
    avg_p1_pair_cost = np.mean(p1_pair_costs) if p1_pair_costs else 0

    # Average total cost per traded market
    avg_total_cost = np.mean([r.total_cost for r in traded])

    # Flips
    avg_flips = np.mean([r.flips_observed for r in traded])

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

    # Stop metrics
    stops_triggered = sum(1 for r in traded if r.stop_triggered)
    stops_filled = sum(1 for r in traded if r.stop_triggered and r.stop_filled)
    stop_trigger_rate = (stops_triggered / n_traded * 100) if n_traded > 0 else 0
    stop_fill_rate = (stops_filled / stops_triggered * 100) if stops_triggered > 0 else 0
    total_sell_proceeds = sum(r.stop_sell_proceeds for r in traded if r.stop_filled)

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
        "p1_both_fill_rate": round(p1_both_rate, 1),
        "p2_fill_rate": round(p2_rate, 1),
        "p3_fill_rate": round(p3_rate, 1),
        "p4_fill_rate": round(p4_rate, 1),
        "avg_up_shares": round(avg_up_shares, 1),
        "avg_down_shares": round(avg_down_shares, 1),
        "avg_tilt_ratio": round(avg_tilt, 3),
        "avg_p1_pair_cost": round(avg_p1_pair_cost, 4),
        "avg_total_cost": round(avg_total_cost, 2),
        "avg_flips": round(avg_flips, 1),
        "cooldown_skips": cooldown_skips,
        "session_stopped": session_stopped,
        "stop_reason": stop_reason,
        "stops_triggered": stops_triggered,
        "stops_filled": stops_filled,
        "stop_trigger_rate": round(stop_trigger_rate, 1),
        "stop_fill_rate": round(stop_fill_rate, 1),
        "total_sell_proceeds": round(total_sell_proceeds, 2),
    }


# ═══════════════════════════════════════════════════════════════
# SECTION: SessionResult + run_backtest()
# STATUS: ADAPTED from V2.2 — no EMA, signal is expensive side
# ═══════════════════════════════════════════════════════════════
@dataclass
class SessionResult:
    results: List[MOBAMarketResult]
    session_stopped: bool
    markets_before_stop: int
    final_session_pnl: float
    session_peak_pnl: float
    stop_reason: Optional[str]
    adaptive_activated: bool = False
    pnl_at_check: Optional[float] = None
    cooldown_skips: int = 0


def run_backtest(
    config: MOBAConfig,
    obs_df: pd.DataFrame,
    markets_with_res: List[str],
    resolutions: Dict[str, str],
    dataset_name: str,
) -> SessionResult:
    """
    Run MOBA backtest with session-level stops and cross-market state.
    No signal prediction needed — expensive side is observed per-market.
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
        mdf = obs_df[obs_df['market_slug'] == market_slug]

        if len(mdf) == 0:
            continue

        # Entry timestamp: first observation in tradeable window
        entry_window_mask = (
            (mdf['time_remaining_secs'] >= config.min_time_remaining) &
            (mdf['time_remaining_secs'] <= config.phase1_start)
        )
        entry_rows = mdf[entry_window_mask]
        if len(entry_rows) == 0:
            continue
        entry_ts = int(entry_rows.iloc[0]['timestamp_ms'])

        # Cooldown check
        cooldown_ms = config.cooldown_minutes * 60 * 1000
        if last_fill_ts > 0 and (entry_ts - last_fill_ts) < cooldown_ms:
            cooldown_skips += 1
            continue

        # Simulate market (signal is embedded — observes expensive side)
        market_result = simulate_market(
            obs_df, market_slug, resolution, config, dataset_name,
            current_balance=current_balance,
        )

        # Post-resolution: update cross-market state
        has_fills = market_result.up_shares > 0 or market_result.down_shares > 0
        if has_fills:
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
# SECTION: generate_grid_configs() — MOBA Parameter Sweep
# STATUS: NEW
# ═══════════════════════════════════════════════════════════════
def generate_grid_configs() -> List[MOBAConfig]:
    """
    MOBA grid: expensive-side offset x threshold sets x phase count x P1 offset.

    Key parameters:
    - phase2-4 offset: how aggressive on expensive side (1c/2c/3c)
    - threshold set: when to start tilting (low/mid/high/none)
    - phase count: 2/3/4 phases
    - phase1 offset: hedge foundation tightness
    """
    configs = []

    # === Tier 1: Expensive-side offset sweep (mid thresholds) ===
    for exp_off in [0.01, 0.02, 0.03]:
        tag = f"{int(exp_off*100)}c"
        configs.append(MOBAConfig(
            name=f"MOBA_e{tag}_mid",
            phase2_offset=exp_off, phase3_offset=exp_off, phase4_offset=exp_off,
            phase2_threshold=0.55, phase3_threshold=0.65, phase4_threshold=0.80,
        ))

    # === Tier 2: Threshold sweep (1c expensive offset) ===
    for thr_name, t2, t3, t4 in [
        ("lo", 0.52, 0.58, 0.70),
        ("hi", 0.60, 0.70, 0.85),
        ("none", 0.50, 0.50, 0.50),
    ]:
        configs.append(MOBAConfig(
            name=f"MOBA_e1c_{thr_name}",
            phase2_offset=0.01, phase3_offset=0.01, phase4_offset=0.01,
            phase2_threshold=t2, phase3_threshold=t3, phase4_threshold=t4,
        ))

    # === Tier 3: Phase count variations ===
    configs.append(MOBAConfig(
        name="MOBA_e1c_2ph",
        phase2_offset=0.01,
        phase2_threshold=0.55,
        phase3_threshold=2.0, phase4_threshold=2.0,  # disabled
    ))
    configs.append(MOBAConfig(
        name="MOBA_e1c_3ph",
        phase2_offset=0.01, phase3_offset=0.01,
        phase2_threshold=0.55, phase3_threshold=0.65,
        phase4_threshold=2.0,  # disabled
    ))

    # === Tier 4: Phase 1 offset sweep ===
    for p1_off in [0.01, 0.02, 0.04, 0.05]:
        tag = f"{int(p1_off*100)}c"
        configs.append(MOBAConfig(
            name=f"MOBA_p1{tag}_e1c_mid",
            phase1_offset=p1_off,
            phase2_offset=0.01, phase3_offset=0.01, phase4_offset=0.01,
            phase2_threshold=0.55, phase3_threshold=0.65, phase4_threshold=0.80,
        ))

    # === Tier 5: Shares variation ===
    configs.append(MOBAConfig(
        name="MOBA_10sh_e1c_mid",
        phase1_shares=10, phase2_shares=10, phase3_shares=10, phase4_shares=10,
        phase2_offset=0.01, phase3_offset=0.01, phase4_offset=0.01,
        phase2_threshold=0.55, phase3_threshold=0.65, phase4_threshold=0.80,
    ))

    # === Tier 6: Maker stop variants ===
    # Test stops on the most promising base configs
    base_configs = [
        # (name_suffix, p1_offset, exp_offset, t2, t3, t4)
        ("e1c_mid", 0.03, 0.01, 0.55, 0.65, 0.80),
        ("p14c_e1c_mid", 0.04, 0.01, 0.55, 0.65, 0.80),
        ("e1c_hi", 0.03, 0.01, 0.60, 0.70, 0.85),
        ("e1c_lo", 0.03, 0.01, 0.52, 0.58, 0.70),
        ("e1c_none", 0.03, 0.01, 0.50, 0.50, 0.50),
    ]
    for stop_time in [300.0, 200.0]:
        st_tag = f"s{int(stop_time)}"
        for base_name, p1_off, exp_off, t2, t3, t4 in base_configs:
            configs.append(MOBAConfig(
                name=f"MOBA_{base_name}_{st_tag}",
                phase1_offset=p1_off,
                phase2_offset=exp_off, phase3_offset=exp_off, phase4_offset=exp_off,
                phase2_threshold=t2, phase3_threshold=t3, phase4_threshold=t4,
                stop_enabled=True,
                stop_check_time=stop_time,
                stop_sell_offset=0.01,
            ))

    # Stop offset 2c variant on best base
    configs.append(MOBAConfig(
        name="MOBA_e1c_mid_s200_2c",
        phase2_offset=0.01, phase3_offset=0.01, phase4_offset=0.01,
        phase2_threshold=0.55, phase3_threshold=0.65, phase4_threshold=0.80,
        stop_enabled=True,
        stop_check_time=200.0,
        stop_sell_offset=0.02,
    ))

    return configs


# ═══════════════════════════════════════════════════════════════
# SECTION: main()
# STATUS: ADAPTED from V2.2 — MOBA branding, no EMA, new grid
# ═══════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='OOS7',
                        help='"train", "test", "all", or comma-separated: OOS7,OOS9')
    parser.add_argument('--output', default='research/findings/data/moba_v1_stops_results.csv')
    parser.add_argument('--checkpoint', default='research/findings/data/moba_v1_stops_checkpoint.csv')
    args = parser.parse_args()

    print("=" * 80)
    print("MOBA V1 + STOPS: Multi-Phase Observation-Based Accumulation (Feb 17, 2026)")
    print("Copied from: directional_maker_v2_2_backtest.py (execution engine)")
    print("Signal: Expensive side observation (no EMA, no prediction)")
    print("Entry: All MAKER (0% fee). 4-phase accumulation with hedge foundation.")
    print("NEW: Maker sell stops — sell excess shares when expensive side flips.")
    print("=" * 80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Starting Capital: ${STARTING_CAPITAL}")
    print(f"Max Capital/Market: {MAX_CAPITAL_FRACTION*100:.0f}% of current balance")
    print(f"Min Time Remaining: {MIN_TIME}s")

    all_configs = generate_grid_configs()
    print(f"\nTotal configs: {len(all_configs)}")

    for c in all_configs[:5]:
        print(f"  - {c.name}: P1={c.phase1_offset}, P2={c.phase2_offset}/"
              f"t{c.phase2_threshold}, P3=t{c.phase3_threshold}, P4=t{c.phase4_threshold}")
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
        print(f"  Configs for this dataset: {len(all_configs)}")

        print(f"\n  Running {len(all_configs)} configs on {dataset_key}...")

        for i, config in enumerate(tqdm(all_configs, desc=f"  {dataset_key}")):
            session_result = run_backtest(
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
            metrics['phase1_offset'] = config.phase1_offset
            metrics['phase2_offset'] = config.phase2_offset
            metrics['phase2_threshold'] = config.phase2_threshold
            metrics['phase3_threshold'] = config.phase3_threshold
            metrics['phase4_threshold'] = config.phase4_threshold
            metrics['phase1_shares'] = config.phase1_shares
            metrics['stop_enabled'] = config.stop_enabled
            metrics['stop_check_time'] = config.stop_check_time if config.stop_enabled else 0
            metrics['stop_sell_offset'] = config.stop_sell_offset if config.stop_enabled else 0
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
        print("MOBA V1 RESULTS SUMMARY")
        print("=" * 80)

        for dataset in results_df['dataset'].unique():
            print(f"\n  {dataset}:")
            subset = results_df[results_df['dataset'] == dataset].copy()
            subset = subset.sort_values('total_pnl', ascending=False)

            cols = ['config_name', 'markets_traded', 'win_rate',
                    'total_pnl', 'pnl_per_hr', 'sharpe',
                    'p1_both_fill_rate', 'p2_fill_rate', 'p3_fill_rate', 'p4_fill_rate',
                    'avg_tilt_ratio', 'avg_p1_pair_cost',
                    'stops_triggered', 'stop_fill_rate', 'total_sell_proceeds',
                    'max_drawdown_pct', 'worst_market_loss', 'ending_balance']
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
                'avg_tilt_ratio': 'mean',
                'avg_p1_pair_cost': 'mean',
                'max_drawdown_pct': 'max',
                'p1_both_fill_rate': 'mean',
                'p2_fill_rate': 'mean',
                'p3_fill_rate': 'mean',
                'p4_fill_rate': 'mean',
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
                    'avg_tilt_ratio': 'mean',
                    'max_drawdown_pct': 'max',
                }).round(2)
                combined_split = combined_split.sort_values('total_pnl', ascending=False)
                print(combined_split.head(10).to_string())

            # Train-top configs -> their test performance
            print("\n" + "-" * 80)
            print("TOP 5 TRAIN CONFIGS -> TEST PERFORMANCE")
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

        # Analysis by expensive-side offset
        print("\n" + "=" * 80)
        print("ANALYSIS BY EXPENSIVE-SIDE OFFSET")
        print("=" * 80)
        by_offset = results_df.groupby('phase2_offset').agg({
            'total_pnl': ['sum', 'mean'],
            'markets_traded': 'sum',
            'win_rate': 'mean',
            'avg_tilt_ratio': 'mean',
            'p2_fill_rate': 'mean',
        }).round(2)
        print(by_offset.to_string())

        # Analysis by threshold set
        print("\n" + "=" * 80)
        print("ANALYSIS BY PHASE 2 THRESHOLD")
        print("=" * 80)
        by_thresh = results_df.groupby('phase2_threshold').agg({
            'total_pnl': ['sum', 'mean'],
            'markets_traded': 'sum',
            'win_rate': 'mean',
            'p2_fill_rate': 'mean',
            'avg_tilt_ratio': 'mean',
        }).round(2)
        print(by_thresh.to_string())

        # STOP vs NO-STOP COMPARISON
        if 'stop_enabled' in results_df.columns:
            print("\n" + "=" * 80)
            print("STOP vs NO-STOP COMPARISON")
            print("=" * 80)
            for has_stop in [False, True]:
                label = "WITH STOP" if has_stop else "NO STOP"
                sub = results_df[results_df['stop_enabled'] == has_stop]
                if len(sub) > 0:
                    combined_stop = sub.groupby('config_name').agg({
                        'total_pnl': 'sum',
                        'markets_traded': 'sum',
                        'win_rate': 'mean',
                        'avg_tilt_ratio': 'mean',
                        'stops_triggered': 'sum',
                        'stops_filled': 'sum',
                        'total_sell_proceeds': 'sum',
                        'worst_market_loss': 'min',
                    }).round(2)
                    combined_stop = combined_stop.sort_values('total_pnl', ascending=False)
                    print(f"\n  {label} ({len(sub)} results):")
                    print(combined_stop.head(10).to_string())

            # Side-by-side for matching base configs
            print("\n" + "-" * 80)
            print("MATCHED COMPARISON: Same base config with/without stops")
            print("-" * 80)
            stop_configs = results_df[results_df['stop_enabled'] == True]
            if len(stop_configs) > 0:
                by_check_time = stop_configs.groupby('stop_check_time')
                for check_time, group in by_check_time:
                    print(f"\n  Stop check time: {check_time}s")
                    combined_st = group.groupby('config_name').agg({
                        'total_pnl': 'sum',
                        'stops_triggered': 'sum',
                        'stops_filled': 'sum',
                        'stop_trigger_rate': 'mean',
                        'stop_fill_rate': 'mean',
                        'total_sell_proceeds': 'sum',
                    }).round(2)
                    combined_st = combined_st.sort_values('total_pnl', ascending=False)
                    print(combined_st.to_string())

        # MANDATORY METRICS
        print("\n" + "=" * 80)
        print("MANDATORY STABILITY METRICS (Top 5 configs by PnL)")
        print("=" * 80)
        top5 = results_df.sort_values('total_pnl', ascending=False).head(5)
        for _, row in top5.iterrows():
            print(f"\n  {row['config_name']} ({row['dataset']}):")
            print(f"    Total PnL: ${row['total_pnl']:.2f}")
            print(f"    PnL/hr: ${row['pnl_per_hr']:.2f}")
            print(f"    Win Rate: {row['win_rate']:.1f}%")
            print(f"    Sharpe: {row['sharpe']:.2f}")
            print(f"    Tilt Ratio: {row['avg_tilt_ratio']:.3f}")
            print(f"    P1 Pair Cost: ${row['avg_p1_pair_cost']:.4f}")
            print(f"    Fill Rates: P1={row['p1_both_fill_rate']:.0f}% P2={row['p2_fill_rate']:.0f}% "
                  f"P3={row['p3_fill_rate']:.0f}% P4={row['p4_fill_rate']:.0f}%")
            print(f"    Worst market: ${row['worst_market_loss']:.2f}")
            print(f"    Max drawdown: {row['max_drawdown_pct']:.1f}%")


if __name__ == "__main__":
    main()
