#!/usr/bin/env python3
"""
Directional Maker V1 Backtest — Baguette-Style Partial Hedge

=============================================================================
COPIED FROM: pair_capture_v1_backtest.py (validated execution engine)
MODIFIED: Added directional signal (BTC EMA + OBI contrarian), partial hedge
          sizing, cooldown, and martingale flip recovery.
=============================================================================

Strategy: Place MAKER bids on BOTH UP and DOWN sides at a fixed bid_level
(e.g. $0.48), but TILT toward the predicted winner side using BTC EMA trend
+ OBI contrarian signal. Partial hedge limits losses on wrong predictions.

Key mechanics:
- MAKER entry (0% fees), hold to resolution
- "rise_above" guard: fill only triggers when ask drops to bid AFTER being above it
- Capital constraint: 50% of current balance per market
- Directional signal: BTC EMA crossover + OBI contrarian filter
- Partial hedge: more shares on predicted winner, fewer on hedge side
- Cooldown: minimum time between entries across markets
- Martingale flip: reverse direction + increase size after losses

Usage:
    python research/backtests/directional_maker_v1_backtest.py --data OOS7
    python research/backtests/directional_maker_v1_backtest.py --data all
"""

# ═══════════════════════════════════════════════════════════════
# SECTION: Imports & sys.path
# STATUS: COPY VERBATIM from pair_capture_v1
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

# ═══════════════════════════════════════════════════════════════
# SECTION: Constants
# STATUS: COPY VERBATIM from pair_capture_v1 (capital + timing only)
# ═══════════════════════════════════════════════════════════════
STARTING_CAPITAL = 170.0
MIN_TIME = 90.0
MAX_CAPITAL_FRACTION = 0.50

# ═══════════════════════════════════════════════════════════════
# SECTION: Config dataclass (DirectionalMakerConfig)
# STATUS: NEW — replaces PairCaptureConfig
# ═══════════════════════════════════════════════════════════════
@dataclass
class DirectionalMakerConfig:
    name: str
    bid_level: float = 0.48          # Fixed MAKER bid for BOTH sides
    base_shares: int = 15            # Base shares per market (total both sides)
    hedge_ratio: float = 0.3         # 30% on hedge side, 70% on directional
    cooldown_minutes: float = 5.0    # Min time between entries (minutes)
    flip_enabled: bool = True        # Martingale flip after loss
    flip_size_increment: float = 0.5 # +50% per consecutive loss
    max_multiplier: float = 3.0      # Cap at 3x base
    signal_mode: str = "ema_obi"     # "ema_only" or "ema_obi"
    ema_short_span: int = 300        # ~5 min at 1Hz
    ema_long_span: int = 1800        # ~30 min at 1Hz
    min_time_remaining: float = MIN_TIME  # Skip last 90s
    entry_window_start: float = 800.0    # Only fill during this window (seconds remaining)
    entry_window_end: float = 600.0      # Lower bound of entry window

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
# SECTION: DirectionalMarketResult dataclass
# STATUS: NEW — extends PairMarketResult with signal metrics
# ═══════════════════════════════════════════════════════════════
@dataclass
class DirectionalMarketResult:
    market_slug: str
    resolution: str  # "UP" or "DOWN"
    dataset: str
    config_name: str

    # Fill tracking
    up_fills: int = 0
    down_fills: int = 0
    up_shares: int = 0
    down_shares: int = 0
    up_cost: float = 0.0
    down_cost: float = 0.0

    # Pair metrics
    paired_shares: int = 0
    excess_side: str = "NONE"   # "UP", "DOWN", or "NONE"
    excess_shares: int = 0

    # PnL breakdown
    pair_profit: float = 0.0     # (1.00 - 2 * bid_level) * paired_shares
    excess_pnl: float = 0.0     # Directional PnL on unmatched shares
    total_pnl: float = 0.0      # pair_profit + excess_pnl

    # Directional metrics (NEW)
    predicted_side: str = "NONE"    # "UP" or "DOWN"
    was_flipped: bool = False       # Martingale override was active
    size_multiplier: float = 1.0    # Multiplier at time of entry
    signal_correct: bool = False    # predicted_side == resolution
    last_fill_ts: int = 0           # Timestamp of last fill (for cooldown tracking)

    # Skip reason (for debugging)
    skip_reason: str = ""           # "cooldown", "obi_filter", "no_fills", ""


# ═══════════════════════════════════════════════════════════════
# SECTION: check_session_stop()
# STATUS: COPY VERBATIM from pair_capture_v1
# ═══════════════════════════════════════════════════════════════
def check_session_stop(config: DirectionalMakerConfig, session_pnl: float, session_peak_pnl: float) -> bool:
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
# SECTION: simulate_market() — Directional Maker Core Logic
# STATUS: MODIFIED from pair_capture_v1 — takes directional params
# FILL ENGINE: MAKER fill with rise_above guard (VERBATIM from V1)
# ═══════════════════════════════════════════════════════════════
def simulate_market(
    obs_df: pd.DataFrame,
    slug: str,
    resolution: str,
    config: DirectionalMakerConfig,
    dataset_name: str,
    current_balance: float,
    predicted_side: str,        # "UP" or "DOWN"
    directional_shares: int,    # Shares on predicted winner side
    hedge_shares: int,          # Shares on hedge side
) -> DirectionalMarketResult:
    """
    Simulate directional maker for a single market.

    Places MAKER bids at config.bid_level on BOTH UP and DOWN sides,
    but with UNEQUAL shares: more on predicted winner, fewer on hedge side.
    Fill requires ask to drop to bid_level AFTER having been above it
    (rise_above guard prevents multiple fills from a single sustained low).
    """
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    result = DirectionalMarketResult(
        market_slug=slug,
        resolution=resolution,
        dataset=dataset_name,
        config_name=config.name,
        predicted_side=predicted_side,
        signal_correct=(predicted_side == resolution),
    )

    if len(mdf) == 0:
        return result

    bid_level = config.bid_level

    # Assign shares per side based on prediction
    if predicted_side == "UP":
        up_max_shares = directional_shares
        down_max_shares = hedge_shares
    else:
        up_max_shares = hedge_shares
        down_max_shares = directional_shares

    # State tracking (VERBATIM fill logic from V1)
    up_shares_filled = 0
    down_shares_filled = 0
    up_cost = 0.0
    down_cost = 0.0
    last_fill_ts = 0

    # Rise-above guard: must see ask > bid_level before a fill can trigger
    up_ask_was_above = True   # Start True so first dip can fill
    down_ask_was_above = True

    # Max capital for this market
    max_capital = config.max_capital_fraction * current_balance if config.use_capital_constraint else float('inf')

    # shares_per_fill = 1 share per fill event, max_fills controls total
    # This allows fine-grained filling up to the allocated share count
    shares_per_fill = 1

    for _, row in mdf.iterrows():
        time_rem = row['time_remaining_secs']
        if time_rem < config.min_time_remaining:
            continue

        # Entry window: only process fills during specified window
        if time_rem > config.entry_window_start or time_rem < config.entry_window_end:
            continue

        up_ask = row.get('up_ask')
        down_ask = row.get('down_ask')

        if pd.isna(up_ask) or pd.isna(down_ask):
            continue

        up_ask = float(up_ask)
        down_ask = float(down_ask)

        # ─── UP fill check (VERBATIM from V1) ───
        if up_shares_filled < up_max_shares:
            if up_ask > bid_level:
                up_ask_was_above = True
            if up_ask <= bid_level and up_ask_was_above:
                fill_cost = bid_level * shares_per_fill
                total_cost_after = up_cost + down_cost + fill_cost
                if not config.use_capital_constraint or total_cost_after <= max_capital:
                    up_shares_filled += shares_per_fill
                    up_cost += fill_cost
                    up_ask_was_above = False  # Reset: need ask to rise again
                    last_fill_ts = int(row['timestamp_ms'])

        # ─── DOWN fill check (VERBATIM from V1) ───
        if down_shares_filled < down_max_shares:
            if down_ask > bid_level:
                down_ask_was_above = True
            if down_ask <= bid_level and down_ask_was_above:
                fill_cost = bid_level * shares_per_fill
                total_cost_after = up_cost + down_cost + fill_cost
                if not config.use_capital_constraint or total_cost_after <= max_capital:
                    down_shares_filled += shares_per_fill
                    down_cost += fill_cost
                    down_ask_was_above = False
                    last_fill_ts = int(row['timestamp_ms'])

        # Early exit if both sides maxed
        if up_shares_filled >= up_max_shares and down_shares_filled >= down_max_shares:
            break

    # ═══════════════════════════════════════════════════════════════
    # RESOLUTION: Compute PnL (VERBATIM from V1)
    # ═══════════════════════════════════════════════════════════════
    paired = min(up_shares_filled, down_shares_filled)
    pair_profit = (1.00 - 2 * bid_level) * paired  # 0% maker fees

    # Excess (unmatched) shares
    if up_shares_filled > down_shares_filled:
        excess_side = "UP"
        excess = up_shares_filled - down_shares_filled
    elif down_shares_filled > up_shares_filled:
        excess_side = "DOWN"
        excess = down_shares_filled - up_shares_filled
    else:
        excess_side = "NONE"
        excess = 0

    # Excess PnL: depends on resolution
    excess_pnl = 0.0
    if excess > 0:
        if resolution == excess_side:
            # Excess is on the winning side
            excess_pnl = (1.0 - bid_level) * excess
        else:
            # Excess is on the losing side
            excess_pnl = (0.0 - bid_level) * excess

    total_pnl = pair_profit + excess_pnl

    result.up_fills = up_shares_filled  # Each share is a fill event
    result.down_fills = down_shares_filled
    result.up_shares = up_shares_filled
    result.down_shares = down_shares_filled
    result.up_cost = round(up_cost, 4)
    result.down_cost = round(down_cost, 4)
    result.paired_shares = paired
    result.excess_side = excess_side
    result.excess_shares = excess
    result.pair_profit = round(pair_profit, 4)
    result.excess_pnl = round(excess_pnl, 4)
    result.total_pnl = round(total_pnl, 4)
    result.last_fill_ts = last_fill_ts

    return result


# ═══════════════════════════════════════════════════════════════
# SECTION: DATASETS dict
# STATUS: COPY VERBATIM from pair_capture_v1
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
# STATUS: COPY VERBATIM from pair_capture_v1
# ═══════════════════════════════════════════════════════════════
def load_dataset(dataset_key: str):
    """Load observer + resolution data for a dataset. No BTC HF needed."""
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
# SECTION: precompute_signals() — BTC EMA + OBI
# STATUS: NEW — pre-computation before market loop
# ═══════════════════════════════════════════════════════════════
def precompute_signals(obs_df: pd.DataFrame, config: DirectionalMakerConfig) -> pd.DataFrame:
    """
    Pre-compute BTC EMA trend and OBI columns on the observer data.

    Returns a DataFrame with columns:
    - timestamp_ms, binance_price, ema_short, ema_long, btc_trend
    Sorted and deduplicated by timestamp_ms.
    """
    # Extract BTC price series from observer data
    btc_cols = ['timestamp_ms', 'binance_price']
    if not all(c in obs_df.columns for c in btc_cols):
        raise ValueError(f"Observer data missing columns: {btc_cols}")

    btc_ts = obs_df[btc_cols].drop_duplicates('timestamp_ms').sort_values('timestamp_ms').copy()
    btc_ts = btc_ts.reset_index(drop=True)

    # Convert to numeric, drop NaN
    btc_ts['binance_price'] = pd.to_numeric(btc_ts['binance_price'], errors='coerce')
    btc_ts = btc_ts.dropna(subset=['binance_price']).reset_index(drop=True)

    # Compute EMAs
    btc_ts['ema_short'] = btc_ts['binance_price'].ewm(span=config.ema_short_span, adjust=False).mean()
    btc_ts['ema_long'] = btc_ts['binance_price'].ewm(span=config.ema_long_span, adjust=False).mean()

    # BTC trend: 1 = UP (bullish), -1 = DOWN (bearish)
    btc_ts['btc_trend'] = np.where(btc_ts['ema_short'] > btc_ts['ema_long'], 1, -1)

    return btc_ts


# ═══════════════════════════════════════════════════════════════
# SECTION: calculate_metrics() — Directional-Specific
# STATUS: NEW — extends V1 metrics with signal accuracy
# ═══════════════════════════════════════════════════════════════
def calculate_metrics(
    results: List[DirectionalMarketResult],
    duration_hours: float,
    config: DirectionalMakerConfig,
    session_result: Optional['SessionResult'] = None,
    cooldown_skips: int = 0,
    obi_skips: int = 0,
) -> Dict:
    if not results:
        return {
            "markets": 0, "markets_with_fills": 0, "total_trades": 0,
            "both_fill_pct": 0, "one_fill_pct": 0, "zero_fill_pct": 0,
            "pair_completion_rate": 0,
            "total_up_fills": 0, "total_down_fills": 0,
            "total_paired_shares": 0, "total_excess_shares": 0,
            "total_pair_profit": 0, "total_excess_pnl": 0,
            "total_pnl": 0, "pnl_per_hr": 0,
            "excess_win_rate": 0,
            "signal_accuracy": 0, "flip_frequency": 0, "avg_multiplier": 0,
            "cooldown_skips": cooldown_skips, "obi_skips": obi_skips,
            "sharpe": 0, "roi_pct": 0,
            "profitable_mkts_pct": 0,
            "max_drawdown_pct": 0, "ending_balance": STARTING_CAPITAL,
            "worst_market_loss": 0,
            "session_stopped": False, "stop_reason": None,
        }

    # Filter to markets that actually traded (had fills)
    traded = [r for r in results if r.up_fills > 0 or r.down_fills > 0]
    n_markets = len(results)
    n_traded = len(traded)

    both_fill = sum(1 for r in results if r.up_fills > 0 and r.down_fills > 0)
    one_fill = sum(1 for r in results if (r.up_fills > 0) != (r.down_fills > 0))
    zero_fill = sum(1 for r in results if r.up_fills == 0 and r.down_fills == 0)

    total_up_fills = sum(r.up_fills for r in results)
    total_down_fills = sum(r.down_fills for r in results)
    total_paired = sum(r.paired_shares for r in results)
    total_excess = sum(r.excess_shares for r in results)
    total_shares = total_paired + total_excess

    total_pair_profit = sum(r.pair_profit for r in results)
    total_excess_pnl = sum(r.excess_pnl for r in results)
    total_pnl = sum(r.total_pnl for r in results)

    # Excess win rate
    excess_markets = [r for r in results if r.excess_shares > 0]
    excess_wins = sum(1 for r in excess_markets if r.excess_pnl > 0)
    excess_win_rate = (excess_wins / len(excess_markets) * 100) if excess_markets else 0

    # Pair completion rate
    pair_completion = (total_paired / total_shares * 100) if total_shares > 0 else 0

    # Signal accuracy: of traded markets, how often was prediction correct?
    signal_correct_count = sum(1 for r in traded if r.signal_correct)
    signal_accuracy = (signal_correct_count / n_traded * 100) if n_traded > 0 else 0

    # Flip frequency: of traded markets, how often was flip active?
    flip_count = sum(1 for r in traded if r.was_flipped)
    flip_frequency = (flip_count / n_traded * 100) if n_traded > 0 else 0

    # Average multiplier
    avg_multiplier = np.mean([r.size_multiplier for r in traded]) if traded else 1.0

    # Sharpe (per-market PnL)
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
        "both_fill_pct": round(both_fill / n_markets * 100, 1) if n_markets > 0 else 0,
        "one_fill_pct": round(one_fill / n_markets * 100, 1) if n_markets > 0 else 0,
        "zero_fill_pct": round(zero_fill / n_markets * 100, 1) if n_markets > 0 else 0,
        "pair_completion_rate": round(pair_completion, 1),
        "total_up_fills": total_up_fills,
        "total_down_fills": total_down_fills,
        "total_paired_shares": total_paired,
        "total_excess_shares": total_excess,
        "total_pair_profit": round(total_pair_profit, 2),
        "total_excess_pnl": round(total_excess_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "pnl_per_hr": round(total_pnl / duration_hours, 2) if duration_hours > 0 else 0,
        "excess_win_rate": round(excess_win_rate, 1),
        "signal_accuracy": round(signal_accuracy, 1),
        "flip_frequency": round(flip_frequency, 1),
        "avg_multiplier": round(avg_multiplier, 2),
        "cooldown_skips": cooldown_skips,
        "obi_skips": obi_skips,
        "sharpe": round(sharpe, 2),
        "roi_pct": round(total_pnl / STARTING_CAPITAL * 100, 1),
        "profitable_mkts_pct": round(profitable_pct, 1),
        "max_drawdown_pct": round(max_dd_pct, 1),
        "ending_balance": round(ending_balance, 2),
        "worst_market_loss": round(worst_market, 2),
        "session_stopped": session_stopped,
        "stop_reason": stop_reason,
    }


# ═══════════════════════════════════════════════════════════════
# SECTION: SessionResult + run_backtest_with_session_stops()
# STATUS: ADAPTED from V1 — adds cross-market state (cooldown,
#         flip, signal) per plan
# ═══════════════════════════════════════════════════════════════
@dataclass
class SessionResult:
    results: List[DirectionalMarketResult]
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
    config: DirectionalMakerConfig,
    obs_df: pd.DataFrame,
    markets_with_res: List[str],
    resolutions: Dict[str, str],
    dataset_name: str,
    btc_ts: pd.DataFrame,
) -> SessionResult:
    """
    Run directional maker backtest with session-level stops,
    cross-market state (cooldown, martingale flip), and signal lookup.
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

    # ═══════════════════════════════════════════════════════════════
    # Cross-market state (NEW)
    # ═══════════════════════════════════════════════════════════════
    last_fill_ts = 0
    consecutive_losses = 0
    flip_active = False
    size_multiplier = 1.0
    cooldown_skips = 0
    obi_skips = 0

    # Pre-compute OBI if columns exist
    has_obi = 'up_imbalance' in obs_df.columns and 'down_imbalance' in obs_df.columns
    if has_obi:
        obs_df = obs_df.copy()
        obs_df['net_obi'] = pd.to_numeric(obs_df['up_imbalance'], errors='coerce') - \
                            pd.to_numeric(obs_df['down_imbalance'], errors='coerce')

    # Pre-compute btc_ts searchsorted array for fast lookups
    btc_timestamps = btc_ts['timestamp_ms'].values
    btc_trends = btc_ts['btc_trend'].values

    for market_slug in markets_with_res:
        if session_stopped:
            break

        resolution = resolutions[market_slug]
        mdf = obs_df[obs_df['market_slug'] == market_slug]

        if len(mdf) == 0:
            continue

        # 1. Get market entry timestamp (first observation)
        entry_ts = int(mdf['timestamp_ms'].min())

        # 2. Cooldown check: skip if too soon after last fill
        cooldown_ms = config.cooldown_minutes * 60 * 1000
        if last_fill_ts > 0 and (entry_ts - last_fill_ts) < cooldown_ms:
            cooldown_skips += 1
            continue

        # 3. Signal lookup: btc_trend at market entry time
        nearest_idx = np.searchsorted(btc_timestamps, entry_ts)
        nearest_idx = min(nearest_idx, len(btc_trends) - 1)
        btc_trend = btc_trends[nearest_idx]
        predicted_side = "UP" if btc_trend == 1 else "DOWN"

        # 4. OBI contrarian filter (if required and available)
        if config.signal_mode == "ema_obi":
            if has_obi:
                market_obi = mdf['net_obi'].mean()
                if not pd.isna(market_obi):
                    obi_direction = 1 if market_obi > 0 else -1
                    if obi_direction == btc_trend:  # OBI AGREES = low confidence
                        obi_skips += 1
                        continue  # SKIP — only trade when OBI is contrarian
            else:
                # No OBI data available — skip this market for ema_obi mode
                obi_skips += 1
                continue

        # 5. Martingale flip (override signal direction)
        was_flipped = False
        if config.flip_enabled and flip_active:
            predicted_side = "DOWN" if predicted_side == "UP" else "UP"
            was_flipped = True

        # 6. Compute shares with multiplier
        effective_shares = int(config.base_shares * size_multiplier)
        directional_shares = int(effective_shares * (1 - config.hedge_ratio))
        hedge_shares = effective_shares - directional_shares

        # Ensure at least 1 share on each side
        directional_shares = max(1, directional_shares)
        hedge_shares = max(1, hedge_shares)

        # 7. Simulate market (VERBATIM fill engine)
        market_result = simulate_market(
            obs_df, market_slug, resolution, config, dataset_name,
            current_balance=current_balance,
            predicted_side=predicted_side,
            directional_shares=directional_shares,
            hedge_shares=hedge_shares,
        )
        market_result.was_flipped = was_flipped
        market_result.size_multiplier = size_multiplier

        # 8. Post-resolution: update cross-market state
        if market_result.up_fills > 0 or market_result.down_fills > 0:
            session_pnl += market_result.total_pnl
            session_peak_pnl = max(session_peak_pnl, session_pnl)
            markets_before_stop += 1

            # Update running balance
            current_balance = STARTING_CAPITAL + session_pnl

            # Update cooldown timestamp
            if market_result.last_fill_ts > 0:
                last_fill_ts = market_result.last_fill_ts

            # Update martingale state based on directional (excess) outcome
            # The excess side is where the directional bet lies
            if market_result.excess_shares > 0:
                if market_result.excess_side == resolution:
                    # Directional side WON
                    consecutive_losses = 0
                    flip_active = False
                    size_multiplier = 1.0
                else:
                    # Directional side LOST
                    consecutive_losses += 1
                    flip_active = True
                    size_multiplier = min(
                        1.0 + config.flip_size_increment * consecutive_losses,
                        config.max_multiplier,
                    )
            else:
                # Perfectly paired (no excess) — treat as neutral
                # Don't reset martingale state, just continue
                pass

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
# SECTION: generate_grid_configs() — 48 Directional Maker Configs
# STATUS: NEW
# ═══════════════════════════════════════════════════════════════
def generate_grid_configs() -> List[DirectionalMakerConfig]:
    """
    Grid search: hedge_ratio x cooldown x flip x signal_mode x ema_scale = 48 configs.

    - hedge_ratio: [0.3, 0.5] — directional aggressiveness vs protection
    - cooldown_minutes: [1, 3, 10] — trade frequency
    - flip_enabled: [True, False] — martingale recovery on/off
    - signal_mode: ["ema_only", "ema_obi"] — signal confidence filter
    - ema_scale: fast=(100,500), slow=(300,1800)
    """
    configs = []

    hedge_ratios = [0.3, 0.5]
    cooldown_options = [1, 3, 10]
    flip_options = [True, False]
    signal_modes = ["ema_only", "ema_obi"]
    ema_scales = {
        "fast": (100, 500),
        "slow": (300, 1800),
    }

    for hedge in hedge_ratios:
        for cooldown in cooldown_options:
            for flip in flip_options:
                for signal in signal_modes:
                    for ema_name, (ema_short, ema_long) in ema_scales.items():
                        hedge_str = f"H{int(hedge*100)}"
                        cd_str = f"CD{cooldown}"
                        flip_str = "FL" if flip else "NF"
                        sig_str = "OBI" if signal == "ema_obi" else "EMA"
                        ema_str = ema_name[0].upper()  # F or S

                        name = f"DM_{hedge_str}_{cd_str}_{flip_str}_{sig_str}_{ema_str}"

                        configs.append(DirectionalMakerConfig(
                            name=name,
                            hedge_ratio=hedge,
                            cooldown_minutes=cooldown,
                            flip_enabled=flip,
                            signal_mode=signal,
                            ema_short_span=ema_short,
                            ema_long_span=ema_long,
                        ))

    return configs


# ═══════════════════════════════════════════════════════════════
# SECTION: main()
# STATUS: ADAPTED from V1 — signal pre-computation, grid changes
# ═══════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='OOS7', help='Comma-separated: OOS7,OOS9 or "all"')
    parser.add_argument('--output', default='research/findings/data/directional_maker_v1_results.csv')
    parser.add_argument('--checkpoint', default='research/findings/data/directional_maker_v1_checkpoint.csv')
    args = parser.parse_args()

    print("=" * 80)
    print("DIRECTIONAL MAKER V1 GRID SEARCH (Feb 11, 2026)")
    print("Copied from: pair_capture_v1_backtest.py (validated engine)")
    print("Strategy: BTC EMA + OBI contrarian → partial hedge maker orders")
    print("=" * 80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Starting Capital: ${STARTING_CAPITAL}")
    print(f"Max Capital/Market: {MAX_CAPITAL_FRACTION*100:.0f}% of current balance")
    print(f"Min Time Remaining: {MIN_TIME}s")
    print(f"Entry Window: {800}s - {600}s remaining")
    print(f"Bid Level: $0.48")

    all_configs = generate_grid_configs()
    print(f"\nTotal configs: {len(all_configs)}")

    # Show a few example configs
    for c in all_configs[:4]:
        print(f"  - {c.name}: hedge={c.hedge_ratio}, cd={c.cooldown_minutes}min, "
              f"flip={c.flip_enabled}, signal={c.signal_mode}, "
              f"ema=({c.ema_short_span},{c.ema_long_span})")
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

        # Check OBI availability for this dataset
        has_obi = 'up_imbalance' in obs_df.columns and 'down_imbalance' in obs_df.columns
        print(f"  OBI columns available: {has_obi}")

        # Filter configs: ema_obi configs only run on datasets WITH OBI
        dataset_configs = []
        for c in all_configs:
            if c.signal_mode == "ema_obi" and not has_obi:
                continue  # Skip OBI configs on datasets without OBI
            dataset_configs.append(c)
        print(f"  Configs for this dataset: {len(dataset_configs)} "
              f"(skipped {len(all_configs) - len(dataset_configs)} OBI configs)")

        # Pre-compute signals for EACH unique EMA scale (ONCE, reused across configs)
        # This avoids recomputing EMAs for every config (Mistake #3)
        ema_cache = {}
        for c in dataset_configs:
            key = (c.ema_short_span, c.ema_long_span)
            if key not in ema_cache:
                ema_cache[key] = precompute_signals(obs_df, c)

        # Verify BTC trend distribution
        for key, btc_ts in ema_cache.items():
            up_pct = (btc_ts['btc_trend'] == 1).mean() * 100
            down_pct = (btc_ts['btc_trend'] == -1).mean() * 100
            print(f"  EMA({key[0]},{key[1]}): trend UP={up_pct:.1f}%, DOWN={down_pct:.1f}%")

        print(f"\n  Running {len(dataset_configs)} configs on {dataset_key}...")

        for i, config in enumerate(tqdm(dataset_configs, desc=f"  {dataset_key}")):
            # Look up pre-computed BTC trend for this config's EMA scale
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
            metrics['bid_level'] = config.bid_level
            metrics['hedge_ratio'] = config.hedge_ratio
            metrics['cooldown_minutes'] = config.cooldown_minutes
            metrics['flip_enabled'] = config.flip_enabled
            metrics['signal_mode'] = config.signal_mode
            metrics['ema_short'] = config.ema_short_span
            metrics['ema_long'] = config.ema_long_span
            all_results.append(metrics)

            # Checkpoint after each config (Mistake #2)
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
        print("DIRECTIONAL MAKER V1 RESULTS SUMMARY")
        print("=" * 80)

        for dataset in results_df['dataset'].unique():
            print(f"\n  {dataset}:")
            subset = results_df[results_df['dataset'] == dataset].copy()
            subset = subset.sort_values('total_pnl', ascending=False)

            cols = ['config_name', 'total_trades', 'signal_accuracy',
                    'total_pnl', 'pnl_per_hr', 'sharpe',
                    'profitable_mkts_pct', 'max_drawdown_pct',
                    'cooldown_skips', 'obi_skips', 'flip_frequency',
                    'ending_balance']
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
                'profitable_mkts_pct': 'mean',
                'max_drawdown_pct': 'max',
                'total_pair_profit': 'sum',
                'total_excess_pnl': 'sum',
                'cooldown_skips': 'sum',
                'obi_skips': 'sum',
                'flip_frequency': 'mean',
                'avg_multiplier': 'mean',
            }).round(2)
            combined = combined.sort_values('total_pnl', ascending=False)
            print(combined.head(15).to_string())

        # Key insight: by hedge ratio
        print("\n" + "=" * 80)
        print("ANALYSIS BY HEDGE RATIO")
        print("=" * 80)
        by_hedge = results_df.groupby('hedge_ratio').agg({
            'total_pnl': ['sum', 'mean'],
            'signal_accuracy': 'mean',
            'total_trades': 'sum',
            'profitable_mkts_pct': 'mean',
            'max_drawdown_pct': 'max',
        }).round(2)
        print(by_hedge.to_string())

        # By signal mode
        print("\n" + "=" * 80)
        print("ANALYSIS BY SIGNAL MODE")
        print("=" * 80)
        by_signal = results_df.groupby('signal_mode').agg({
            'total_pnl': ['sum', 'mean'],
            'signal_accuracy': 'mean',
            'total_trades': 'sum',
            'cooldown_skips': 'sum',
            'obi_skips': 'sum',
        }).round(2)
        print(by_signal.to_string())

        # By cooldown
        print("\n" + "=" * 80)
        print("ANALYSIS BY COOLDOWN")
        print("=" * 80)
        by_cooldown = results_df.groupby('cooldown_minutes').agg({
            'total_pnl': ['sum', 'mean'],
            'total_trades': 'sum',
            'cooldown_skips': 'sum',
        }).round(2)
        print(by_cooldown.to_string())

        # MANDATORY METRICS (from CLAUDE_MISTAKES.md)
        print("\n" + "=" * 80)
        print("MANDATORY STABILITY METRICS (Top 5 configs by PnL)")
        print("=" * 80)
        top5 = results_df.sort_values('total_pnl', ascending=False).head(5)
        for _, row in top5.iterrows():
            print(f"\n  {row['config_name']} ({row['dataset']}):")
            print(f"    Total PnL: ${row['total_pnl']:.2f}")
            print(f"    Sharpe: {row['sharpe']:.2f}")
            print(f"    Profitable markets: {row['profitable_mkts_pct']:.1f}%")
            print(f"    Worst market loss: ${row['worst_market_loss']:.2f}")
            print(f"    Max drawdown: {row['max_drawdown_pct']:.1f}%")
            print(f"    Signal accuracy: {row['signal_accuracy']:.1f}%")


if __name__ == "__main__":
    main()
