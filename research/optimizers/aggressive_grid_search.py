#!/usr/bin/env python3
"""
AGGRESSIVE Grid Search - Main Optimization File

=============================================================================
THIS IS THE CANONICAL GRID SEARCH FILE (Jan 31, 2026)
Location: research/optimizers/aggressive_grid_search.py
Legacy: research/optimizers/aggressive_grid_search_v1_legacy.py
=============================================================================

Grid dimensions:
1. Time-Stop: 30s, 180s, 240s
2. Loser Offset: TIGHT, CURRENT
3. Cycle Mode: SINGLE only (multi-cycle DEPRECATED)

Grid: 2 offsets × 3 time-stops × 1 cycle-mode = 6 configurations

Loser bid formula:
    expected_drop = DROP_MULTIPLIER * spike_magnitude + DROP_INTERCEPT
    loser_bid = (1.0 - winner_entry) - expected_drop

Offset presets:
    TIGHT:   DROP_MULT=0.30, DROP_INT=0.05 (aggressive, faster fill)
    CURRENT: DROP_MULT=0.50, DROP_INT=0.08 (baseline)

Cycle mode (Jan 31, 2026 - SINGLE ONLY):
    SINGLE: 1 cycle × 50 shares (LIVE-READY, production config)

    MULTI-CYCLE ABANDONED (Jan 31, 2026):
    Multi-cycle destroyed profitability even with direction consistency fix.
    - SINGLE: 54.3% win rate, +$1.37/hr (LIVE-READY)
    - MULTI: 39.8% win rate, -$26.70/hr (10x trades, 15pp lower win rate)
    Root cause: Stacking same-direction trades catches weak follow-on spikes.

Features:
- Fee model with gross/net PnL separation
- Dataset coverage validation
- Checkpoint saves after each config
- Offset + time-stop + cycle-mode sensitivity analysis
- OU adaptive threshold (per TRADING_CONFIGS.py)
- Binary OBI filter (simple: obi > 0 = confirm, obi <= 0 = reject)
- Direction consistency check for multi-cycle modes

Datasets (~130h total):
- IS+OOS2 - 23h (partial)
- OOS3+4 - 47h
- OOS5 - 41h
- OOS7 - 19h

Usage:
    python research/optimizers/aggressive_grid_search.py

For quick single-config validation, use:
    python research/backtests/aggressive_main_backtest.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict
from tqdm import tqdm
import math
import sys
import time

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# =============================================================================
# SHARED LOGIC - FROM src/core (Single Source of Truth for HOW to calculate)
# =============================================================================
from src.core import (
    polymarket_taker_fee,
    calculate_pnl_with_fees,
    velocity_confirms_spike,
    obi_confirms_spike,
    should_take_spike_enhanced,
    compute_enhanced_score,
    calculate_loser_bid as calculate_loser_bid_core,
    BacktestCycle,
    # Multi-cycle direction modes
    DIRECTION_MODE_SINGLE,
    DIRECTION_MODE_BUILD,
    DIRECTION_MODE_CLEAR,
    can_enter_direction,
)

# =============================================================================
# GRID SEARCH PARAMETERS - Define here, NOT from TRADING_CONFIGS
# =============================================================================
# Grid search tests MANY values to find the winner.
# Winner values then get saved to TRADING_CONFIGS.py for live trading.
#
# FIXED PARAMS (not part of grid - same for all tests):
TARGET_SHARES = 50
MIN_TIME = 240              # Entry cutoff (time_stop + 60s buffer)
MIN_RUNTIME_SECS = 300      # 5 min market duration filter
HIGH_ENTRY_THRESHOLD = 0.90 # Skip entries >= $0.90
SPIKE_LOOKBACK = 72         # 72 ticks (1200ms at 60Hz)

# OU ADAPTIVE THRESHOLD params
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10

# Signal filtering thresholds
VELOCITY_CONFIRM_THRESHOLD = 0.10
ENHANCED_SCORE_THRESHOLD = 0.40

# Loser bid defaults (CURRENT baseline - also tested in grid)
DEFAULT_DROP_MULTIPLIER = 0.50
DEFAULT_DROP_INTERCEPT = 0.08
TARGET_PAIR_COST = 0.99

# Cycling
MIN_CYCLE_GAP_MS = 200


# FEE MODEL: imported from src.core (polymarket_taker_fee, calculate_pnl_with_fees)

# =============================================================================
# CONFIG CLASS - Combines time-stop, loser offset, AND multi-cycle
# =============================================================================

@dataclass
class TestConfig:
    """Configuration for time-stop + loser offset + multi-cycle test."""
    name: str
    time_stop_seconds: float
    drop_multiplier: float
    drop_intercept: float
    offset_name: str  # TIGHT, CURRENT
    # Multi-cycle parameters
    max_cycles: int = 1      # 1 = single-cycle, 2+ = multi-cycle
    shares_per_cycle: int = 50  # Shares per cycle
    cycle_mode: str = "SINGLE"  # SINGLE, MULTI_BUILD, MULTI_CLEAR
    direction_mode: str = DIRECTION_MODE_SINGLE  # Direction consistency mode
    # Loss mechanism parameters (Feb 1, 2026)
    stop_loss_pct: Optional[float] = None  # None = disabled, 0.15/0.20/0.30 = exit if drop >= X%
    max_market_losses: Optional[int] = None  # None = disabled, 2/3 = stop trading after N losses in market

    @property
    def total_shares(self) -> int:
        """Total shares across all cycles."""
        return self.max_cycles * self.shares_per_cycle

    def calculate_loser_bid(self, winner_entry: float, spike_magnitude: float) -> float:
        """Calculate loser bid with this config's offset parameters."""
        expected_drop = self.drop_multiplier * spike_magnitude + self.drop_intercept
        max_loser = TARGET_PAIR_COST - winner_entry
        loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
        return max(0.01, min(0.95, loser_bid))


# =============================================================================
# GRID PARAMETERS: OFFSET × TIME-STOP × CYCLE-MODE
# =============================================================================

# Offset parameters: (DROP_MULTIPLIER, DROP_INTERCEPT, description)
# TIGHT removed (Feb 1, 2026) - focus on CURRENT baseline only
OFFSET_PRESETS = {
    "CURRENT": (0.50, 0.08, "Current baseline"),
}

# Time-stops to test
# 0 = no time-stop (rely on stop-loss or passive fill only)
TIME_STOPS = [0.0, 30.0, 180.0]

# Stop-loss percentages to test (Feb 1, 2026)
# None = disabled, percentage = exit if winner drops >= X%
STOP_LOSS_PCTS = [None, 0.15, 0.20, 0.30]

# Market loss limits to test (Feb 1, 2026)
# None = disabled, N = stop trading in market after N total losses
# Analysis showed: 2 total losses saves $91.66 (18.3% improvement)
MAX_MARKET_LOSSES = [None, 2, 3]

# Cycle modes: (max_cycles, shares_per_cycle, direction_mode, description)
# SINGLE-CYCLE ONLY - Multi-cycle DEPRECATED (Jan 31, 2026)
#
# MULTI-CYCLE ABANDONED: Destroyed profitability even with direction fix.
#   - SINGLE: 54.3% win rate, +$1.37/hr (LIVE-READY)
#   - MULTI: 39.8% win rate, -$26.70/hr (10x trades, 15pp lower win rate)
# Root cause: Stacking same-direction trades catches weak follow-on spikes.
CYCLE_MODES = {
    "SINGLE": (1, 50, DIRECTION_MODE_SINGLE, "1 cycle × 50 shares (PRODUCTION)"),
    # DEPRECATED - kept for reference only:
    # "MULTI_BUILD": (2, 25, DIRECTION_MODE_BUILD, "DEPRECATED - destroyed profitability"),
    # "MULTI_CLEAR": (2, 25, DIRECTION_MODE_CLEAR, "DEPRECATED - destroyed profitability"),
}

# Generate all configs: 2 offsets × 3 time-stops × 4 stop-losses × 3 market-limits = ~70 configs
# (minus invalid configs where TS=0 AND SL=None AND MML=None)
CONFIGS = []
for offset_name, (mult, intercept, offset_desc) in OFFSET_PRESETS.items():
    for ts in TIME_STOPS:
        for sl_pct in STOP_LOSS_PCTS:
            for mml in MAX_MARKET_LOSSES:
                # Skip invalid: no time-stop AND no stop-loss AND no market limit
                # (would never exit losing positions)
                if ts == 0 and sl_pct is None and mml is None:
                    continue

                # Build descriptive name
                sl_label = f"SL{int(sl_pct*100)}" if sl_pct else "NOSL"
                mml_label = f"MML{mml}" if mml else "NOMML"
                name = f"{offset_name}_TS{int(ts)}_{sl_label}_{mml_label}"

                # Use SINGLE cycle mode only (multi-cycle deprecated)
                max_cycles, shares_per, dir_mode, _ = CYCLE_MODES["SINGLE"]

                CONFIGS.append(TestConfig(
                    name=name,
                    time_stop_seconds=ts,
                    drop_multiplier=mult,
                    drop_intercept=intercept,
                    offset_name=offset_name,
                    max_cycles=max_cycles,
                    shares_per_cycle=shares_per,
                    cycle_mode="SINGLE",
                    direction_mode=dir_mode,
                    stop_loss_pct=sl_pct,
                    max_market_losses=mml,
                ))

print(f"Generated {len(CONFIGS)} configs: {len(OFFSET_PRESETS)} offsets × {len(TIME_STOPS)} time-stops × {len(STOP_LOSS_PCTS)} stop-losses × {len(MAX_MARKET_LOSSES)} market-limits")


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
    # ==========================================================================
    # ALL DATASETS - For comprehensive grid search (Jan 31, 2026)
    # ==========================================================================
    "IS+OOS2": {
        "name": "IS+OOS2 (Jan 16-19)",
        "btc_file": "research/binance_hf/btc_prices_20260118_060340.csv",
        "obs_files": [
            "research/observer/grid_obs_20260116.csv",
            "research/observer/grid_obs_20260117.csv",
            "research/observer/grid_obs_20260118.csv",
            "research/observer/grid_obs_20260119.csv",
        ],
        "use_obi": True,  # Auto: uses OBI if columns exist, skips if not
        "expected_hours": 23.0,  # Reduced from 69h - partial data available
    },
    "OOS3+4": {
        "name": "OOS3+4 (Jan 22-24)",
        "btc_file": "research/observer/PROTECTED_btc_prices_oos3_oos4_combined.csv",
        "obs_files": [
            "research/observer/PROTECTED_grid_obs_oos3_oos4_combined.csv",
        ],
        "use_obi": True,  # Auto: uses OBI if columns exist, skips if not
        "expected_hours": 47.0,
    },
    "OOS5": {
        "name": "OOS5 (Jan 26)",
        "btc_file": None,  # Will use observer binance_price at 5Hz
        "obs_files": [
            "research/observer/PROTECTED_grid_obs_oos5_recovered.csv",
        ],
        "use_obi": True,  # Auto: uses OBI if columns exist, skips if not
        "expected_hours": 41.0,
    },
    "OOS7": {
        "name": "OOS7 (Jan 29-30)",
        "btc_file": "research/binance_hf/btc_prices_20260129_160523.csv",
        "obs_files": [
            "research/observer/grid_obs_20260129.csv",
            "research/observer/grid_obs_20260130.csv",
        ],
        "use_obi": True,  # OBI ON for OOS7
        "expected_hours": 19.0,
    },
    "OOS8": {
        "name": "OOS8 (Jan 31)",
        "btc_file": "research/binance_hf/btc_prices_20260131_055231.csv",
        "obs_files": [
            "research/observer/grid_obs_20260131.csv",
        ],
        "use_obi": True,  # OBI ON for OOS8
        "expected_hours": 24.0,  # Estimate
    },
}

MIN_COVERAGE_PCT = 80.0


def validate_dataset_coverage() -> bool:
    """Validate that all datasets have sufficient data coverage."""
    print("=" * 80)
    print("DATASET COVERAGE VALIDATION")
    print("=" * 80)
    print(f"Minimum required coverage: {MIN_COVERAGE_PCT}%\n")

    base_dir = Path("/Users/rananjaybika/polymarket-amm-bot")
    all_passed = True

    for ds_key, ds_config in DATASETS.items():
        expected_hours = ds_config.get('expected_hours', 0)

        obs_dfs = []
        for fname in ds_config['obs_files']:
            fpath = base_dir / fname
            if fpath.exists():
                df = pd.read_csv(fpath, usecols=['timestamp_ms'], nrows=None)
                obs_dfs.append(df)

        if not obs_dfs:
            print(f"  {ds_key}: FAIL - No observer files found!")
            all_passed = False
            continue

        obs_combined = pd.concat(obs_dfs, ignore_index=True)
        obs_start = obs_combined['timestamp_ms'].min()
        obs_end = obs_combined['timestamp_ms'].max()
        obs_hours = (obs_end - obs_start) / 3600000

        if ds_config['btc_file']:
            btc_path = base_dir / ds_config['btc_file']
            if btc_path.exists():
                btc_df = pd.read_csv(btc_path, usecols=['timestamp_ms'])
                btc_start = btc_df['timestamp_ms'].min()
                btc_end = btc_df['timestamp_ms'].max()
                overlap_start = max(obs_start, btc_start)
                overlap_end = min(obs_end, btc_end)
                overlap_hours = max(0, (overlap_end - overlap_start) / 3600000)
            else:
                overlap_hours = 0
        else:
            overlap_hours = obs_hours

        coverage_pct = (overlap_hours / expected_hours * 100) if expected_hours > 0 else 100
        passed = coverage_pct >= MIN_COVERAGE_PCT
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False

        print(f"  {ds_key}: Expected: {expected_hours:.1f}h | Actual: {overlap_hours:.1f}h | {coverage_pct:.0f}% {status}")

    print("=" * 80)
    return all_passed


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
    hedge_type: str  # "passive", "time_stop", "stop_loss", "resolution"
    pair_cost: float
    pnl_gross: float
    pnl_net: float
    entry_fee: float
    exit_fee: float
    correct_direction: bool
    spike_magnitude: float
    dataset: str
    offset_name: str  # Track which offset was used
    cycle_mode: str = "SINGLE"  # SINGLE or MULTI
    shares: int = 50  # Shares for this trade
    # Loss mechanism tracking (Feb 1, 2026)
    stop_loss_pct: Optional[float] = None  # Config stop-loss % used
    max_market_losses: Optional[int] = None  # Config market loss limit used
    skipped_by_mml: bool = False  # True if this trade was skipped due to market loss limit


@dataclass
class BacktestCycle:
    """Track a single cycle in multi-cycle mode."""
    cycle_id: int
    entry_ts: int
    winner_side: str
    loser_side: str
    winner_entry: float
    loser_target: float
    entry_time_rem: float
    spike_magnitude: float
    score: float
    shares: int


# =============================================================================
# SPIKE DETECTION - VECTORIZED PRECOMPUTATION
# =============================================================================

def precompute_spikes_ou(btc_df: pd.DataFrame, lookback: int = SPIKE_LOOKBACK) -> pd.DataFrame:
    """Vectorized spike detection with OU ADAPTIVE threshold."""
    print("    Using OU ADAPTIVE threshold (per TRADING_CONFIGS.py)")
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    df['price_prev'] = df['price'].shift(lookback)
    df['change_pct'] = (df['price'] - df['price_prev']) / df['price_prev'] * 100
    df['spike_magnitude'] = df['change_pct'].abs()

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


# HELPER FUNCTIONS: imported from src.core
# (velocity_confirms_spike, obi_confirms_spike, should_take_spike_enhanced, compute_enhanced_score)

# =============================================================================
# SIMULATION - Uses TestConfig for both time-stop AND loser offset
# =============================================================================

def simulate_market(btc_spikes: pd.DataFrame, obs_df: pd.DataFrame,
                    slug: str, resolution: str,
                    use_obi_filter: bool, dataset_name: str,
                    config: TestConfig) -> List[TradeResult]:
    """
    Simulate trading with configurable time-stop, loser offset, and cycle mode.
    Dispatches to single-cycle or multi-cycle based on config.max_cycles.
    """
    if config.max_cycles > 1:
        return simulate_market_multicycle(
            btc_spikes, obs_df, slug, resolution, use_obi_filter, dataset_name, config
        )
    return simulate_market_single(
        btc_spikes, obs_df, slug, resolution, use_obi_filter, dataset_name, config
    )


def simulate_market_single(btc_spikes: pd.DataFrame, obs_df: pd.DataFrame,
                           slug: str, resolution: str,
                           use_obi_filter: bool, dataset_name: str,
                           config: TestConfig) -> List[TradeResult]:
    """Single-cycle simulation (original behavior)."""
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
    ].copy()

    if len(market_spikes) == 0:
        return []

    trades = []
    cycle_num = 0
    last_hedge_ts = 0
    in_position = False
    position_data = None
    time_stop_ms = config.time_stop_seconds * 1000

    # Market loss counter (Feb 1, 2026) - tracks losses in THIS market
    market_loss_count = 0
    market_blocked = False  # Set True when max_market_losses reached

    spike_idx = 0
    obs_idx = 0

    while spike_idx < len(market_spikes) or in_position:

        # STATE 1: In position - check for hedge
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

                if loser_side == "UP":
                    loser_ask = obs_row['up_ask']
                else:
                    loser_ask = obs_row['down_ask']

                # Check passive fill
                if pd.notna(loser_ask) and loser_ask <= loser_target:
                    pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                        winner_entry, loser_target, config.shares_per_cycle,
                        is_taker_entry=True,
                        is_taker_exit=False
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
                        offset_name=config.offset_name,
                        cycle_mode=config.cycle_mode,
                        shares=config.shares_per_cycle,
                        stop_loss_pct=config.stop_loss_pct,
                        max_market_losses=config.max_market_losses,
                    ))

                    # Increment loss counter if negative PnL
                    if pnl_net < 0:
                        market_loss_count += 1

                    in_position = False
                    position_data = None
                    last_hedge_ts = obs_ts
                    obs_idx += 1
                    break

                # Check STOP-LOSS FIRST (before time-stop) - Feb 1, 2026
                if config.stop_loss_pct is not None:
                    winner_side_current = position_data['winner_side']
                    if winner_side_current == "UP":
                        winner_bid_current = obs_row['up_bid']
                    else:
                        winner_bid_current = obs_row['down_bid']

                    if pd.notna(winner_bid_current) and winner_entry > 0:
                        drop_pct = (winner_entry - winner_bid_current) / winner_entry
                        if drop_pct >= config.stop_loss_pct:
                            # Stop-loss triggered - exit immediately
                            loser_fill = loser_ask if pd.notna(loser_ask) else loser_target * 1.05
                            pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                                winner_entry, loser_fill, config.shares_per_cycle,
                                is_taker_entry=True, is_taker_exit=True
                            )
                            trades.append(TradeResult(
                                market_slug=slug,
                                cycle_num=cycle_num,
                                entry_time_remaining=position_data['entry_time_rem'],
                                signal_score=score,
                                winner_side=position_data['winner_side'],
                                winner_fill_price=winner_entry,
                                loser_fill_price=loser_fill,
                                hedge_type="stop_loss",
                                pair_cost=winner_entry + loser_fill,
                                pnl_gross=pnl_gross,
                                pnl_net=pnl_net,
                                entry_fee=entry_fee,
                                exit_fee=exit_fee,
                                correct_direction=(resolution == position_data['winner_side']),
                                spike_magnitude=spike_mag,
                                dataset=dataset_name,
                                offset_name=config.offset_name,
                                cycle_mode=config.cycle_mode,
                                shares=config.shares_per_cycle,
                                stop_loss_pct=config.stop_loss_pct,
                                max_market_losses=config.max_market_losses,
                            ))

                            # Increment loss counter (stop-loss always a loss)
                            if pnl_net < 0:
                                market_loss_count += 1

                            in_position = False
                            position_data = None
                            last_hedge_ts = obs_ts
                            obs_idx += 1
                            break

                # Check time-stop (ONLY if NOT in profit)
                elapsed_ms = obs_ts - entry_ts
                if time_stop_ms > 0 and elapsed_ms >= time_stop_ms:
                    winner_side_current = position_data['winner_side']
                    if winner_side_current == "UP":
                        winner_bid_current = obs_row['up_bid']
                    else:
                        winner_bid_current = obs_row['down_bid']

                    in_profit = pd.notna(winner_bid_current) and winner_bid_current >= winner_entry

                    if not in_profit:
                        loser_fill = loser_ask if pd.notna(loser_ask) else loser_target * 1.05
                        pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                            winner_entry, loser_fill, config.shares_per_cycle,
                            is_taker_entry=True,
                            is_taker_exit=True
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
                            offset_name=config.offset_name,
                            cycle_mode=config.cycle_mode,
                            shares=config.shares_per_cycle,
                            stop_loss_pct=config.stop_loss_pct,
                            max_market_losses=config.max_market_losses,
                        ))

                        # Increment loss counter if negative PnL
                        if pnl_net < 0:
                            market_loss_count += 1

                        in_position = False
                        position_data = None
                        last_hedge_ts = obs_ts
                        obs_idx += 1
                        break

                obs_idx += 1

            # If we ran out of observer data while in position
            if in_position and obs_idx >= len(mdf):
                winner_side = position_data['winner_side']
                winner_entry = position_data['winner_entry']
                shares = config.shares_per_cycle

                entry_fee = polymarket_taker_fee(winner_entry) * winner_entry * shares

                if resolution == winner_side:
                    pnl_gross = (1.0 - winner_entry) * shares
                    loser_fill = 0.0
                else:
                    pnl_gross = (0.0 - winner_entry) * shares
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
                    exit_fee=0.0,
                    correct_direction=(resolution == winner_side),
                    spike_magnitude=position_data['spike_magnitude'],
                    dataset=dataset_name,
                    offset_name=config.offset_name,
                    cycle_mode=config.cycle_mode,
                    shares=shares,
                    stop_loss_pct=config.stop_loss_pct,
                    max_market_losses=config.max_market_losses,
                ))
                # Note: No need to increment market_loss_count for resolution - market is ending
                break

            continue

        # STATE 2: Not in position - check next spike
        if spike_idx >= len(market_spikes):
            break

        spike_row = market_spikes.iloc[spike_idx]
        spike_ts = spike_row['timestamp_ms']
        spike_dir = spike_row['spike_direction']
        spike_mag = spike_row['spike_magnitude']

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

        # Get prices first (needed for enhanced OBI filter)
        winner_side = spike_dir
        if winner_side == "UP":
            winner_ask = obs_row['up_ask']
            loser_bid = obs_row.get('down_bid', None)
            loser_ask = obs_row.get('down_ask', None)
            obi_winner = obs_row.get('up_imbalance', None)
        else:
            winner_ask = obs_row['down_ask']
            loser_bid = obs_row.get('up_bid', None)
            loser_ask = obs_row.get('up_ask', None)
            obi_winner = obs_row.get('down_imbalance', None)

        if pd.isna(winner_ask) or winner_ask >= HIGH_ENTRY_THRESHOLD:
            spike_idx += 1
            continue

        # Enhanced OBI filter (uses loser spread, time remaining, OBI magnitude)
        if use_obi_filter:
            if obi_winner is not None and not np.isnan(obi_winner):
                # Calculate loser spread
                loser_spread = 0.05  # Default
                if pd.notna(loser_bid) and pd.notna(loser_ask):
                    loser_spread = loser_ask - loser_bid

                should_take, reject_reason = should_take_spike_enhanced(
                    spike_direction=spike_dir,
                    obi_winner=obi_winner,
                    loser_spread=loser_spread,
                    time_remaining=time_rem,
                    winner_ask_depth=None,  # Depth not available in observer data
                )
                if not should_take:
                    spike_idx += 1
                    continue

        # MARKET LOSS LIMIT check (Feb 1, 2026) - stop trading if too many losses
        if config.max_market_losses is not None:
            if market_loss_count >= config.max_market_losses:
                # Market is blocked - skip all remaining spikes
                if not market_blocked:
                    market_blocked = True
                spike_idx += 1
                continue

        # ENTRY - use config's loser bid calculation
        cycle_num += 1
        loser_side = "DOWN" if winner_side == "UP" else "UP"
        winner_entry = winner_ask
        loser_target = config.calculate_loser_bid(winner_entry, spike_mag)

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
        }

        spike_idx += 1

    return trades


# =============================================================================
# MULTI-CYCLE SIMULATION
# =============================================================================

def simulate_market_multicycle(btc_spikes: pd.DataFrame, obs_df: pd.DataFrame,
                                slug: str, resolution: str,
                                use_obi_filter: bool, dataset_name: str,
                                config: TestConfig) -> List[TradeResult]:
    """
    Multi-cycle simulation: allows multiple concurrent entry/hedge cycles.

    Key difference from single-cycle:
    - Tracks multiple active cycles (up to config.max_cycles)
    - New entries allowed while existing cycles pending hedge
    - Each cycle uses config.shares_per_cycle shares
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
    ].copy()

    if len(market_spikes) == 0:
        return []

    trades = []
    cycle_num = 0
    active_cycles: List[BacktestCycle] = []
    time_stop_ms = config.time_stop_seconds * 1000
    shares = config.shares_per_cycle

    spike_idx = 0
    obs_idx = 0

    while spike_idx < len(market_spikes) or active_cycles:
        # Get current timestamp
        current_ts = None
        if spike_idx < len(market_spikes):
            current_ts = market_spikes.iloc[spike_idx]['timestamp_ms']

        # Find observer row for current time
        if obs_idx < len(mdf) and current_ts:
            while obs_idx < len(mdf) - 1 and mdf.iloc[obs_idx + 1]['timestamp_ms'] <= current_ts:
                obs_idx += 1

        if obs_idx >= len(mdf):
            break

        obs_row = mdf.iloc[obs_idx]
        obs_ts = obs_row['timestamp_ms']

        # Process active cycles: check for hedge fills and time-stops
        completed_cycles = []
        for cycle in active_cycles:
            loser_side = cycle.loser_side
            if loser_side == "UP":
                loser_ask = obs_row['up_ask']
            else:
                loser_ask = obs_row['down_ask']

            # Check passive fill
            if pd.notna(loser_ask) and loser_ask <= cycle.loser_target:
                pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                    cycle.winner_entry, cycle.loser_target, shares,
                    is_taker_entry=True, is_taker_exit=False
                )
                trades.append(TradeResult(
                    market_slug=slug,
                    cycle_num=cycle.cycle_id,
                    entry_time_remaining=cycle.entry_time_rem,
                    signal_score=cycle.score,
                    winner_side=cycle.winner_side,
                    winner_fill_price=cycle.winner_entry,
                    loser_fill_price=cycle.loser_target,
                    hedge_type="passive",
                    pair_cost=cycle.winner_entry + cycle.loser_target,
                    pnl_gross=pnl_gross,
                    pnl_net=pnl_net,
                    entry_fee=entry_fee,
                    exit_fee=exit_fee,
                    correct_direction=(resolution == cycle.winner_side),
                    spike_magnitude=cycle.spike_magnitude,
                    dataset=dataset_name,
                    offset_name=config.offset_name,
                    cycle_mode=config.cycle_mode,
                    shares=shares,
                    stop_loss_pct=config.stop_loss_pct,
                    max_market_losses=config.max_market_losses,
                ))
                completed_cycles.append(cycle)
                continue

            # Check time-stop
            elapsed_ms = obs_ts - cycle.entry_ts
            if time_stop_ms > 0 and elapsed_ms >= time_stop_ms:
                winner_side = cycle.winner_side
                if winner_side == "UP":
                    winner_bid = obs_row['up_bid']
                else:
                    winner_bid = obs_row['down_bid']

                in_profit = pd.notna(winner_bid) and winner_bid >= cycle.winner_entry

                if not in_profit:
                    loser_fill = loser_ask if pd.notna(loser_ask) else cycle.loser_target * 1.05
                    pnl_net, pnl_gross, entry_fee, exit_fee = calculate_pnl_with_fees(
                        cycle.winner_entry, loser_fill, shares,
                        is_taker_entry=True, is_taker_exit=True
                    )
                    trades.append(TradeResult(
                        market_slug=slug,
                        cycle_num=cycle.cycle_id,
                        entry_time_remaining=cycle.entry_time_rem,
                        signal_score=cycle.score,
                        winner_side=cycle.winner_side,
                        winner_fill_price=cycle.winner_entry,
                        loser_fill_price=loser_fill,
                        hedge_type="time_stop",
                        pair_cost=cycle.winner_entry + loser_fill,
                        pnl_gross=pnl_gross,
                        pnl_net=pnl_net,
                        entry_fee=entry_fee,
                        exit_fee=exit_fee,
                        correct_direction=(resolution == cycle.winner_side),
                        spike_magnitude=cycle.spike_magnitude,
                        dataset=dataset_name,
                        offset_name=config.offset_name,
                        cycle_mode=config.cycle_mode,
                        shares=shares,
                        stop_loss_pct=config.stop_loss_pct,
                        max_market_losses=config.max_market_losses,
                    ))
                    completed_cycles.append(cycle)

        # Remove completed cycles
        for c in completed_cycles:
            active_cycles.remove(c)

        # Check for new entry if capacity available
        if len(active_cycles) < config.max_cycles and spike_idx < len(market_spikes):
            spike_row = market_spikes.iloc[spike_idx]
            spike_ts = spike_row['timestamp_ms']
            spike_dir = spike_row['spike_direction']
            spike_mag = spike_row['spike_magnitude']

            time_rem = obs_row['time_remaining_secs']
            velocity_bps = obs_row.get('velocity_bps', 0.0) or 0.0

            # Get prices first (needed for enhanced OBI)
            winner_side = spike_dir
            if winner_side == "UP":
                winner_ask = obs_row['up_ask']
                loser_bid = obs_row.get('down_bid', None)
                loser_ask = obs_row.get('down_ask', None)
                obi_winner = obs_row.get('up_imbalance', None)
            else:
                winner_ask = obs_row['down_ask']
                loser_bid = obs_row.get('up_bid', None)
                loser_ask = obs_row.get('up_ask', None)
                obi_winner = obs_row.get('down_imbalance', None)

            # Entry filters
            can_enter = True
            score = 0.0
            if time_rem < MIN_TIME:
                can_enter = False
            elif not velocity_confirms_spike(spike_dir, velocity_bps):
                can_enter = False
            elif pd.isna(winner_ask) or winner_ask >= HIGH_ENTRY_THRESHOLD:
                can_enter = False
            else:
                score = compute_enhanced_score(spike_mag, velocity_bps, spike_dir, time_rem)
                if score < ENHANCED_SCORE_THRESHOLD:
                    can_enter = False
                elif use_obi_filter and obi_winner is not None and not np.isnan(obi_winner):
                    # Binary OBI filter (simple check - does orderbook confirm spike?)
                    loser_spread = 0.05
                    if pd.notna(loser_bid) and pd.notna(loser_ask):
                        loser_spread = loser_ask - loser_bid
                    should_take, _ = should_take_spike_enhanced(
                        spike_direction=spike_dir,
                        obi_winner=obi_winner,
                        loser_spread=loser_spread,
                        time_remaining=time_rem,
                        winner_ask_depth=None,
                    )
                    if not should_take:
                        can_enter = False

            # Direction consistency check (CRITICAL FIX - Jan 31, 2026)
            # Prevents conflicting positions (long UP while also long DOWN)
            if can_enter:
                can_enter_dir, dir_reason = can_enter_direction(
                    spike_direction=spike_dir,
                    active_cycles=active_cycles,
                    direction_mode=config.direction_mode,
                )
                if not can_enter_dir:
                    can_enter = False

            if can_enter:
                cycle_num += 1
                loser_side = "DOWN" if winner_side == "UP" else "UP"
                loser_target = config.calculate_loser_bid(winner_ask, spike_mag)

                active_cycles.append(BacktestCycle(
                    cycle_id=cycle_num,
                    entry_ts=spike_ts,
                    winner_side=winner_side,
                    loser_side=loser_side,
                    winner_entry=winner_ask,
                    loser_target=loser_target,
                    entry_time_rem=time_rem,
                    spike_magnitude=spike_mag,
                    score=score,
                    shares=shares,
                ))

            spike_idx += 1
        elif spike_idx < len(market_spikes):
            spike_idx += 1  # Skip spike if at capacity

        obs_idx += 1

    # Handle resolution for any remaining active cycles
    for cycle in active_cycles:
        entry_fee = polymarket_taker_fee(cycle.winner_entry) * cycle.winner_entry * shares
        if resolution == cycle.winner_side:
            pnl_gross = (1.0 - cycle.winner_entry) * shares
            loser_fill = 0.0
        else:
            pnl_gross = (0.0 - cycle.winner_entry) * shares
            loser_fill = 1.0
        pnl_net = pnl_gross - entry_fee

        trades.append(TradeResult(
            market_slug=slug,
            cycle_num=cycle.cycle_id,
            entry_time_remaining=cycle.entry_time_rem,
            signal_score=cycle.score,
            winner_side=cycle.winner_side,
            winner_fill_price=cycle.winner_entry,
            loser_fill_price=loser_fill,
            hedge_type="resolution",
            pair_cost=cycle.winner_entry + loser_fill,
            pnl_gross=pnl_gross,
            pnl_net=pnl_net,
            entry_fee=entry_fee,
            exit_fee=0.0,
            correct_direction=(resolution == cycle.winner_side),
            spike_magnitude=cycle.spike_magnitude,
            dataset=dataset_name,
            offset_name=config.offset_name,
            cycle_mode=config.cycle_mode,
            shares=shares,
            stop_loss_pct=config.stop_loss_pct,
            max_market_losses=config.max_market_losses,
        ))

    return trades


# =============================================================================
# DATA LOADING
# =============================================================================

def load_dataset(dataset_key: str) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame], Dict[str, str], float]:
    """Load a dataset."""
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
        else:
            print(f"  {fpath.name}: NOT FOUND")

    if not obs_dfs:
        return None, None, {}, 0

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    print(f"  Combined observer: {len(obs_df):,} rows")

    btc_df = None
    if config['btc_file']:
        btc_path = base_dir / config['btc_file']
        if btc_path.exists():
            btc_df = pd.read_csv(btc_path)
            print(f"  Binance HF: {len(btc_df):,} rows")
        else:
            print(f"  Binance HF: NOT FOUND - will use observer binance_price")

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

    res_path = base_dir / "research/observer/market_resolutions_verified.csv"
    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    btc_start, btc_end = btc_df['timestamp_ms'].min(), btc_df['timestamp_ms'].max()
    obs_start, obs_end = obs_df['timestamp_ms'].min(), obs_df['timestamp_ms'].max()

    overlap_start = max(btc_start, obs_start)
    overlap_end = min(btc_end, obs_end)

    duration_hours = (overlap_end - overlap_start) / 3600000
    print(f"  Overlap: {duration_hours:.2f} hours")

    btc_df = btc_df[(btc_df['timestamp_ms'] >= overlap_start) &
                     (btc_df['timestamp_ms'] <= overlap_end)].copy()
    obs_df = obs_df[(obs_df['timestamp_ms'] >= overlap_start) &
                     (obs_df['timestamp_ms'] <= overlap_end)].copy()

    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

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


# =============================================================================
# MAIN GRID SEARCH
# =============================================================================

def run_grid_search():
    """Run grid search across all 9 configurations x 4 datasets."""

    if not validate_dataset_coverage():
        print("\n  ABORTING: Fix dataset coverage issues before running backtest!")
        return None

    print("=" * 80)
    print("TIME-STOP & LOSER OFFSET OPTIMIZATION TEST")
    print("=" * 80)
    print(f"\nConfigurations: {len(CONFIGS)} ({len(OFFSET_PRESETS)} offsets x {len(TIME_STOPS)} time-stops)")
    print(f"Datasets: {len(DATASETS)} ({', '.join(DATASETS.keys())})")
    print(f"Total runs: {len(CONFIGS) * len(DATASETS)}")
    print()
    print("Offset presets:")
    for name, (mult, intercept, desc) in OFFSET_PRESETS.items():
        print(f"  {name}: DROP_MULT={mult}, DROP_INT={intercept} ({desc})")
    print()

    load_ou_params()

    dataset_cache = {}
    all_results = []
    checkpoint_path = Path("research/findings/data/timestop_offset_v2_checkpoint.csv")
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    total_iterations = len(CONFIGS) * len(DATASETS)
    iteration = 0
    start_time = time.time()

    for config in CONFIGS:
        for dataset_key in DATASETS.keys():
            iteration += 1
            print(f"\n[{iteration}/{total_iterations}] {config.name} on {dataset_key}")

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

            all_trades = []
            slugs = obs_df['market_slug'].unique()

            for slug in tqdm(slugs, desc=f"  Markets", leave=False):
                resolution = res_map.get(slug)
                if resolution not in ['UP', 'DOWN']:
                    continue

                trades = simulate_market(
                    btc_spikes, obs_df, slug, resolution, ds_config['use_obi'], dataset_key, config
                )
                all_trades.extend(trades)

            if all_trades:
                total_pnl_net = sum(t.pnl_net for t in all_trades)
                total_trades = len(all_trades)
                win_rate = sum(1 for t in all_trades if t.correct_direction) / total_trades * 100
                avg_pair_cost = np.mean([t.pair_cost for t in all_trades])
                passive_pct = sum(1 for t in all_trades if t.hedge_type == "passive") / total_trades * 100
                hourly_rate = total_pnl_net / hours if hours > 0 else 0

                result = {
                    'config': config.name,
                    'time_stop': config.time_stop_seconds,
                    'drop_multiplier': config.drop_multiplier,
                    'drop_intercept': config.drop_intercept,
                    'offset_name': config.offset_name,
                    'cycle_mode': config.cycle_mode,
                    'max_cycles': config.max_cycles,
                    'shares_per_cycle': config.shares_per_cycle,
                    'dataset': dataset_key,
                    'hours': hours,
                    'trades': total_trades,
                    'pnl_net': total_pnl_net,
                    'hourly_rate': hourly_rate,
                    'win_rate': win_rate,
                    'avg_pair_cost': avg_pair_cost,
                    'passive_pct': passive_pct,
                }
                all_results.append(result)

                print(f"    [{config.cycle_mode}] Trades: {total_trades}, $/hr: ${hourly_rate:.2f}, Win%: {win_rate:.1f}%")

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
    print("TIME-STOP & LOSER OFFSET TEST RESULTS")
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
        print(f"{'Config':<20} {'Trades':>8} {'$/hr':>10} {'Win%':>8} {'Passive%':>10} {'Avg Pair':>10}")
        print("-" * 80)

        ds_df_sorted = ds_df.sort_values('hourly_rate', ascending=False)
        for _, row in ds_df_sorted.iterrows():
            print(f"{row['config']:<20} {row['trades']:>8} ${row['hourly_rate']:>9.2f} "
                  f"{row['win_rate']:>7.1f}% {row['passive_pct']:>9.1f}% ${row['avg_pair_cost']:>9.4f}")

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
        'avg_pair_cost': 'mean',
    }).reset_index()

    combined['hourly_rate'] = combined['pnl_net'] / combined['hours']
    combined = combined.sort_values('hourly_rate', ascending=False)

    print()
    print(f"{'Config':<20} {'Trades':>8} {'$/hr':>10} {'Win%':>8} {'Passive%':>10} {'Winner':>8}")
    print("-" * 80)

    best_rate = combined['hourly_rate'].max()
    for _, row in combined.iterrows():
        winner = "  *" if row['hourly_rate'] == best_rate else ""
        print(f"{row['config']:<20} {row['trades']:>8} ${row['hourly_rate']:>9.2f} "
              f"{row['win_rate']:>7.1f}% {row['passive_pct']:>9.1f}% {winner:>8}")

    # Offset sensitivity
    print("\n" + "=" * 100)
    print("OFFSET SENSITIVITY ANALYSIS")
    print("=" * 100)

    print("\nBy Offset Type:")
    for offset_name in OFFSET_PRESETS.keys():
        offset_df = df[df['offset_name'] == offset_name]
        if len(offset_df) > 0:
            total_pnl = offset_df['pnl_net'].sum()
            total_hours = offset_df['hours'].sum()
            avg_rate = total_pnl / total_hours
            avg_passive = offset_df['passive_pct'].mean()
            mult, intercept, desc = OFFSET_PRESETS[offset_name]
            print(f"  {offset_name:<8}: ${avg_rate:.2f}/hr, {avg_passive:.1f}% passive (mult={mult}, int={intercept})")

    # Time-stop sensitivity
    print("\nBy Time-Stop:")
    for ts in TIME_STOPS:
        ts_df = df[df['time_stop'] == ts]
        if len(ts_df) > 0:
            total_pnl = ts_df['pnl_net'].sum()
            total_hours = ts_df['hours'].sum()
            avg_rate = total_pnl / total_hours
            avg_passive = ts_df['passive_pct'].mean()
            print(f"  {int(ts):>4}s: ${avg_rate:.2f}/hr, {avg_passive:.1f}% passive")

    # Cycle-mode sensitivity
    print("\nBy Cycle Mode:")
    for mode_name, (max_cycles, shares_per, dir_mode, desc) in CYCLE_MODES.items():
        mode_df = df[df['cycle_mode'] == mode_name]
        if len(mode_df) > 0:
            total_pnl = mode_df['pnl_net'].sum()
            total_hours = mode_df['hours'].sum()
            total_trades = mode_df['trades'].sum()
            avg_rate = total_pnl / total_hours
            avg_win = mode_df['win_rate'].mean()
            print(f"  {mode_name:<12}: ${avg_rate:.2f}/hr, {total_trades} trades, {avg_win:.1f}% win ({desc})")

    # Key comparison
    print("\n" + "=" * 100)
    print("KEY COMPARISON: SINGLE vs MULTI-CYCLE")
    print("=" * 100)
    print("\nQuestion: Does multi-cycle improve hourly rate?")
    print()

    # Compare SINGLE vs MULTI for each offset+timestop combo
    for offset_name in OFFSET_PRESETS.keys():
        for ts in TIME_STOPS:
            single_name = f"{offset_name}_TS{int(ts)}_SINGLE"
            multi_name = f"{offset_name}_TS{int(ts)}_MULTI"

            single_df = df[df['config'] == single_name]
            multi_df = df[df['config'] == multi_name]

            if len(single_df) > 0 and len(multi_df) > 0:
                single_rate = single_df['pnl_net'].sum() / single_df['hours'].sum()
                multi_rate = multi_df['pnl_net'].sum() / multi_df['hours'].sum()
                single_trades = single_df['trades'].sum()
                multi_trades = multi_df['trades'].sum()
                diff = multi_rate - single_rate
                trade_increase = (multi_trades / single_trades - 1) * 100 if single_trades > 0 else 0
                winner = "MULTI*" if multi_rate > single_rate else "SINGLE"
                print(f"  {offset_name}_TS{int(ts)}: SINGLE ${single_rate:.2f}/hr ({single_trades} trades) | "
                      f"MULTI ${multi_rate:.2f}/hr ({multi_trades} trades, +{trade_increase:.0f}%) → {winner}")

    # Save final results
    output_path = Path("research/findings/data/timestop_offset_v2_results.csv")
    df.to_csv(output_path, index=False)
    print(f"\nResults saved to: {output_path}")


def main():
    print("=" * 80)
    print("AGGRESSIVE GRID SEARCH - Offset × Time-Stop × Cycle-Mode")
    print("Config from TRADING_CONFIGS.py - OU ADAPTIVE threshold")
    print("=" * 80)
    print()
    print("Grid dimensions:")
    print()
    print("1. Offset presets:")
    for name, (mult, intercept, desc) in OFFSET_PRESETS.items():
        print(f"   {name}: DROP_MULT={mult}, DROP_INT={intercept} ({desc})")
    print()
    print(f"2. Time-stops: {[int(t) for t in TIME_STOPS]}s")
    print()
    print("3. Cycle modes:")
    for name, (max_cycles, shares, dir_mode, desc) in CYCLE_MODES.items():
        print(f"   {name}: {max_cycles} cycles × {shares} shares, mode={dir_mode} ({desc})")
    print()
    print(f"Total configs: {len(CONFIGS)} = {len(OFFSET_PRESETS)} offsets × {len(TIME_STOPS)} time-stops × {len(CYCLE_MODES)} cycle-modes")
    print()

    results = run_grid_search()

    if results:
        print_results(results)


if __name__ == "__main__":
    main()
