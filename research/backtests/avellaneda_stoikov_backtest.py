#!/usr/bin/env python3
"""
Avellaneda-Stoikov Market Making Backtest (v2 - Redesigned)

CRITICAL DESIGN INSIGHTS:
=========================
1. MM vs AGGRESSIVE: MM has FULL FREEDOM to build inventory (no "enter + hedge" cycling)
2. Profit mechanisms: Directional carry, spread capture, or hybrid
3. Merge function: Burns UP+DOWN pairs, returns $1.00 USDC per pair (FREE via Builder Relayer)
4. Fill model: touch = fill for small size (10-50 shares)

DATA SPLIT:
===========
- TRAINING: IS+OOS2 (Jan 16-19) - use for parameter optimization
- VALIDATION: OOS3+4, OOS5 - use for final validation only

SIGNALS (precomputed):
======================
- Velocity: (price_now - price_180_ticks_ago) / price_180_ticks_ago * 10000 (bps)
- EWMA Z-score: (price - ewma_slow) / ewma_std (adapts to regime changes)

PROFILES (~195 configs):
========================
1. Pure Spread Capture (36) - Quote both sides, rely on order pulling
2. Velocity-Filtered Spread (9) - Quote both when |velocity| low
3. Asymmetric Velocity (27) - Only quote winning side per velocity
4. Asymmetric EWMA (27) - Only quote winning side per z-score
5. Directional Carry Velocity (48) - Build inventory using velocity
6. Directional Carry EWMA (48) - Build inventory using z-score

MANDATORY CHECKLIST (from CLAUDE_MISTAKES.md):
==============================================
- [x] Progress bar (tqdm) for loops > 10 iterations
- [x] Checkpoint saves every 10 configs
- [x] Precompute signals ONCE before grid search
- [x] Test on small subset before full run

Usage:
    python research/avellaneda_stoikov_backtest.py --forensic  # Analyze profitable markets
    python research/avellaneda_stoikov_backtest.py             # Default run
    python research/avellaneda_stoikov_backtest.py --grid-search --profile all
    python research/avellaneda_stoikov_backtest.py --grid-search --profile 3  # Asymmetric velocity only

Author: Claude Code
Date: January 28, 2026
"""

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
import pandas as pd
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent))


# =============================================================================
# CONSTANTS
# =============================================================================

MIN_TIME = 60  # Minimum seconds remaining to trade
MIN_RUNTIME_SECS = 300  # Minimum market duration for backtest

# Velocity computation (60Hz BTC data)
VELOCITY_LOOKBACK_TICKS = 180  # 3 seconds at 60Hz

# EWMA spans (60Hz data)
EWMA_FAST_SPAN = 60   # ~1 second
EWMA_SLOW_SPAN = 300  # ~5 seconds


# =============================================================================
# ENUMS
# =============================================================================

class StrategyMode(Enum):
    """Strategy mode determines quoting behavior.

    Phase 2 Note: Velocity modes removed - velocity signal has only 2.5%
    autocorrelation at 15s (basically useless). DIRECTIONAL_EWMA removed
    as it was identical to ASYMMETRIC_EWMA.
    """
    PURE_SPREAD = "pure_spread"           # Quote both sides, merge pairs
    VELOCITY_FILTERED = "velocity_filtered"  # Quote both when |vel| low (legacy)
    ASYMMETRIC_EWMA = "asymmetric_ewma"     # Quote winning side (z-score) - PRIMARY


class OrderPullReason(Enum):
    """Reason for pulling an order."""
    TIME_EXPIRED = "time_expired"
    VELOCITY_FLIP = "velocity_flip"
    PRICE_ADVERSE = "price_adverse"
    ZSCORE_FLIP = "zscore_flip"


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class ASConfig:
    """Configuration for AS strategy backtest.

    Phase 2 additions:
    - Polarization filter: Only trade polarized markets (<X or >1-X)
    - Time window filter: Only enter when time_remaining in [min, max]
    - Extended EWMA spans: Up to 5400 (90s)
    - Dynamic spread: Signal-strength-based spread adjustment
    """
    # Core AS parameters
    gamma: float = 0.1          # Risk aversion (higher = more conservative)
    k: float = 1.0              # Signal skew multiplier
    base_spread: float = 0.02   # Base spread (2%)
    shares: int = 10            # Shares per order
    max_inventory: int = 50     # Max inventory per side

    # Strategy mode
    mode: StrategyMode = StrategyMode.ASYMMETRIC_EWMA

    # Signal thresholds (legacy velocity kept for backwards compat)
    min_velocity: float = 0.10      # Min |velocity| for directional signal
    velocity_filter_threshold: float = 0.20  # Max |velocity| for pure spread
    z_threshold: float = 1.0        # Z-score threshold for asymmetric

    # EWMA parameters (Phase 2 - extended spans)
    ewma_slow_span: int = 300       # EWMA slow span (ticks at 60Hz)
                                    # Options: 300 (5s), 600 (10s), 900 (15s),
                                    # 1800 (30s), 3600 (60s), 5400 (90s)

    # Order management
    spread_widening_k: float = 0.0  # Spread widening factor for |velocity|
    max_order_age_ms: int = 5000    # Pull if unfilled after this time
    max_adverse_move: float = 0.03  # Pull if price moved against us by this %
    disable_pulling: bool = False   # Disable order pulling entirely

    # Phase 2: Polarization filter (CRITICAL - 87% accuracy discovery)
    # Only trade if market price < threshold or > (1-threshold)
    # E.g., threshold=0.25 means only trade when price < 0.25 or > 0.75
    polarization_threshold: float = 0.30
    enable_polarization_filter: bool = False  # Default off for backwards compat

    # Phase 2: Time window filter (80% accuracy at 400-500s)
    # Only enter if time_remaining in [min_secs, max_secs]
    # 0 = disabled
    entry_window_min_secs: int = 0    # Min time remaining to enter (0=disabled)
    entry_window_max_secs: int = 900  # Max time remaining to enter (0=disabled)

    # Phase 2: Dynamic spread based on signal strength
    enable_dynamic_spread: bool = False
    # When enabled: weak signal (|z| < z_threshold) -> spread * 1.5
    #               strong signal (|z| > 2*z_threshold) -> spread * 0.75

    # Phase 3: Signal Discovery Integration (Jan 29, 2026)
    # Option A: Spike-triggered entry mode
    enable_spike_entry: bool = False  # Only enter when spike_detected=True
    spike_direction_filter: str = ""  # "UP", "DOWN", or "" for both

    # Option B: Velocity alignment filter (works with existing EWMA)
    require_velocity_aligned: bool = False  # vel_aligned condition
    # velocity_aligned = (velocity > 0 and zscore > 0) or (velocity < 0 and zscore < 0)

    # Shared filter: minimum |velocity| for entry (zone_not_neutral)
    min_velocity_for_entry: float = 0.0  # 0 = disabled, 0.10 = zone_not_neutral

    # Execution gap: prevent multiple entries on same signal
    min_entry_gap_ms: int = 200  # 200ms minimum between entries

    def to_dict(self) -> Dict[str, Any]:
        return {
            'mode': self.mode.value,
            'gamma': self.gamma,
            'k': self.k,
            'base_spread': self.base_spread,
            'min_velocity': self.min_velocity,
            'velocity_filter': self.velocity_filter_threshold,
            'z_threshold': self.z_threshold,
            'ewma_slow_span': self.ewma_slow_span,
            'spread_widening_k': self.spread_widening_k,
            'max_order_age_ms': self.max_order_age_ms,
            'max_adverse_move': self.max_adverse_move,
            'disable_pulling': self.disable_pulling,
            'polarization_threshold': self.polarization_threshold,
            'enable_polarization_filter': self.enable_polarization_filter,
            'entry_window_min_secs': self.entry_window_min_secs,
            'entry_window_max_secs': self.entry_window_max_secs,
            'enable_dynamic_spread': self.enable_dynamic_spread,
            # Phase 3
            'enable_spike_entry': self.enable_spike_entry,
            'spike_direction_filter': self.spike_direction_filter,
            'require_velocity_aligned': self.require_velocity_aligned,
            'min_velocity_for_entry': self.min_velocity_for_entry,
            'min_entry_gap_ms': self.min_entry_gap_ms,
        }


@dataclass
class PendingOrder:
    """Represents a pending order waiting for fill."""
    timestamp_ms: int
    side: str  # 'UP' or 'DOWN'
    price: float
    velocity_at_place: float
    zscore_at_place: float


@dataclass
class OrderPullEvent:
    """Logged when an order is pulled (cancelled)."""
    timestamp_ms: int
    side: str
    price: float
    reason: OrderPullReason
    order_age_ms: int
    velocity_at_place: float
    velocity_at_pull: float
    zscore_at_place: float
    zscore_at_pull: float


@dataclass
class Fill:
    """Single fill event."""
    timestamp_ms: int
    side: str  # 'UP' or 'DOWN'
    direction: str  # 'buy' or 'sell'
    price: float
    shares: int
    velocity_bps: float
    zscore: float
    inventory_after: int
    is_winning_side: bool  # Did we fill on the side that eventually won?


@dataclass
class MergeEvent:
    """Logged when UP+DOWN pairs are merged."""
    timestamp_ms: int
    pairs_merged: int
    up_avg_cost: float
    down_avg_cost: float
    pair_cost: float  # up_avg_cost + down_avg_cost
    profit_per_pair: float  # 1.00 - pair_cost
    total_profit: float


@dataclass
class InventoryState:
    """Track inventory with costs."""
    up_shares: int = 0
    down_shares: int = 0
    up_total_cost: float = 0.0
    down_total_cost: float = 0.0

    @property
    def up_avg_cost(self) -> float:
        return self.up_total_cost / self.up_shares if self.up_shares > 0 else 0.0

    @property
    def down_avg_cost(self) -> float:
        return self.down_total_cost / self.down_shares if self.down_shares > 0 else 0.0

    @property
    def hedged_pairs(self) -> int:
        return min(self.up_shares, self.down_shares)

    @property
    def net_inventory(self) -> int:
        """Positive = long UP, Negative = long DOWN."""
        return self.up_shares - self.down_shares

    def add_fill(self, side: str, price: float, shares: int):
        """Record a fill."""
        if side == 'UP':
            self.up_shares += shares
            self.up_total_cost += price * shares
        else:
            self.down_shares += shares
            self.down_total_cost += price * shares


@dataclass
class MarketResult:
    """Result for a single market."""
    market_slug: str
    resolution: str  # 'UP' or 'DOWN'
    fills: List[Fill]
    merges: List[MergeEvent]
    pulls: List[OrderPullEvent]

    # PnL breakdown
    realized_pnl: float  # From merges during market
    unrealized_pnl: float  # From final inventory at resolution
    merge_profit: float  # Sum of merge profits
    total_pnl: float

    # Final state
    final_inventory: InventoryState

    # Stats
    loser_fills: int  # Fills on the side that lost
    winner_fills: int  # Fills on the side that won


@dataclass
class BacktestResult:
    """Aggregate backtest results with enhanced metrics."""
    # Core metrics
    total_pnl: float
    hourly_rate: float
    sharpe_ratio: float
    max_drawdown: float
    max_drawdown_pct: float

    # PnL breakdown
    realized_pnl: float
    unrealized_pnl: float
    merge_profit: float

    # Activity stats
    total_fills: int
    merge_count: int
    markets_traded: int
    profitable_markets: int
    win_rate: float  # % of markets with positive PnL

    # Order pull stats
    total_pulls: int
    pulls_by_reason: Dict[str, int]
    pull_rate: float  # pulls / (pulls + fills)

    # Adverse selection indicator
    loser_fill_rate: float  # % of fills on losing side

    # Time period
    hours: float

    # Config used
    config: ASConfig

    def to_dict(self) -> Dict[str, Any]:
        return {
            'hourly_rate': self.hourly_rate,
            'total_pnl': self.total_pnl,
            'sharpe_ratio': self.sharpe_ratio,
            'max_drawdown': self.max_drawdown,
            'max_drawdown_pct': self.max_drawdown_pct,
            'realized_pnl': self.realized_pnl,
            'unrealized_pnl': self.unrealized_pnl,
            'merge_profit': self.merge_profit,
            'total_fills': self.total_fills,
            'merge_count': self.merge_count,
            'markets_traded': self.markets_traded,
            'profitable_markets': self.profitable_markets,
            'win_rate': self.win_rate,
            'total_pulls': self.total_pulls,
            'pull_rate': self.pull_rate,
            'loser_fill_rate': self.loser_fill_rate,
            'hours': self.hours,
            **self.config.to_dict(),
        }


# =============================================================================
# SIGNAL PRECOMPUTATION
# =============================================================================

def precompute_velocity(btc_df: pd.DataFrame, lookback: int = VELOCITY_LOOKBACK_TICKS) -> pd.DataFrame:
    """
    Precompute velocity for all BTC timestamps.

    Velocity = (price_now - price_N_ticks_ago) / price_N_ticks_ago * 10000 (bps)
    """
    print(f"  Precomputing velocity (lookback={lookback} ticks)...")

    btc_df = btc_df.sort_values('timestamp_ms').reset_index(drop=True)
    prices = btc_df['price'].values

    velocities = np.zeros(len(prices))
    for i in range(lookback, len(prices)):
        old_price = prices[i - lookback]
        new_price = prices[i]
        if old_price > 0:
            velocities[i] = (new_price - old_price) / old_price * 10000

    btc_df['velocity_bps'] = velocities
    print(f"  Velocity: min={velocities.min():.2f}, max={velocities.max():.2f} bps")

    return btc_df


def precompute_ewma_zscore(btc_df: pd.DataFrame, slow_span: int = EWMA_SLOW_SPAN) -> pd.DataFrame:
    """
    Precompute EWMA z-score for all BTC timestamps.

    Uses EWMA (not OU) per CLAUDE_MISTAKES.md #7 - adapts to regime changes.

    Z-score = (price - ewma_slow) / ewma_std
    - Positive z_score = price above mean = UP likely winning
    - Negative z_score = price below mean = DOWN likely winning

    Args:
        btc_df: DataFrame with price column
        slow_span: EWMA slow span in ticks (Phase 2: parameterized)
    """
    print(f"  Precomputing EWMA z-score (fast={EWMA_FAST_SPAN}, slow={slow_span})...")

    btc_df = btc_df.sort_values('timestamp_ms').reset_index(drop=True)
    prices = btc_df['price']

    ewma_slow = prices.ewm(span=slow_span, adjust=False).mean()
    deviation = prices - ewma_slow
    ewma_std = deviation.ewm(span=slow_span, adjust=False).std()

    # Avoid division by zero
    ewma_std = ewma_std.replace(0, np.nan).fillna(method='ffill').fillna(0.01)

    zscores = deviation / ewma_std

    # Clip extreme values
    zscores = zscores.clip(-5, 5)

    btc_df['zscore'] = zscores
    print(f"  Z-score: min={zscores.min():.2f}, max={zscores.max():.2f}")

    return btc_df


# Signal cache for different EWMA spans (Phase 2)
_EWMA_ZSCORE_CACHE: Dict[int, np.ndarray] = {}


def get_zscore_for_span(btc_df: pd.DataFrame, span: int) -> np.ndarray:
    """
    Get z-scores for a specific EWMA span, using cache to avoid recomputation.

    Args:
        btc_df: DataFrame with price column (must be sorted by timestamp_ms)
        span: EWMA slow span in ticks

    Returns:
        numpy array of z-scores aligned with btc_df index
    """
    global _EWMA_ZSCORE_CACHE

    if span not in _EWMA_ZSCORE_CACHE:
        print(f"  Computing z-scores for span={span}...")
        prices = btc_df['price']
        ewma_slow = prices.ewm(span=span, adjust=False).mean()
        deviation = prices - ewma_slow
        ewma_std = deviation.ewm(span=span, adjust=False).std()
        ewma_std = ewma_std.replace(0, np.nan).fillna(method='ffill').fillna(0.01)
        zscores = (deviation / ewma_std).clip(-5, 5)
        _EWMA_ZSCORE_CACHE[span] = zscores.values
        print(f"    Cached span={span}: min={zscores.min():.2f}, max={zscores.max():.2f}")

    return _EWMA_ZSCORE_CACHE[span]


def clear_zscore_cache():
    """Clear the EWMA z-score cache."""
    global _EWMA_ZSCORE_CACHE
    _EWMA_ZSCORE_CACHE = {}


# =============================================================================
# QUOTING LOGIC
# =============================================================================

def should_quote_sides(
    velocity_bps: float,
    zscore: float,
    inventory: int,
    config: ASConfig,
) -> Tuple[bool, bool]:
    """
    Determine which sides to quote based on mode and signals.

    Returns: (quote_up_bid, quote_down_bid)
    - quote_up_bid: Should we place a bid for UP? (buying UP)
    - quote_down_bid: Should we place a bid for DOWN? (buying DOWN)

    Phase 2 Note: Velocity modes removed (2.5% autocorrelation = useless).
    Primary mode is ASYMMETRIC_EWMA with z-score threshold.
    """
    mode = config.mode

    # Profile 1: Pure Spread Capture - quote both unless high velocity
    if mode == StrategyMode.PURE_SPREAD:
        # Protection: velocity filter (don't quote during strong trends)
        if abs(velocity_bps) > config.velocity_filter_threshold:
            return (False, False)
        return (True, True)

    # Profile 2: Velocity-Filtered Spread - only when low velocity (legacy)
    if mode == StrategyMode.VELOCITY_FILTERED:
        if abs(velocity_bps) <= config.velocity_filter_threshold:
            return (True, True)
        return (False, False)

    # Profile 4/12/13/14: Asymmetric EWMA - PRIMARY MODE
    # FIXED: Quote BOTH sides always, but with different aggressiveness
    # The aggressiveness is handled in compute_as_quotes via spread adjustment
    if mode == StrategyMode.ASYMMETRIC_EWMA:
        # Always quote both sides to accumulate pairs
        # Signal determines which side is aggressive vs passive (in compute_as_quotes)
        return (True, True)

    return (False, False)


def compute_as_quotes(
    up_mid: float,
    velocity_bps: float,
    zscore: float,
    time_remaining: float,
    inventory: int,
    config: ASConfig,
) -> Tuple[float, float]:
    """
    Compute AS bid prices for UP and DOWN.

    Returns (up_bid, down_bid).

    FIXED: Now uses asymmetric spreads based on signal direction.
    - Predicted winner: tight spread (aggressive, near top of book)
    - Predicted loser: wide spread (passive, but still accumulating for pairs)

    This allows us to:
    1. Use 800ms Binance edge to get filled on winner
    2. Still accumulate loser side for pair completion
    3. Merge pairs for guaranteed profit
    """
    # Normalize time to [0, 1] where 1 = 15 minutes
    T = max(time_remaining / 900.0, 0.01)

    # Estimate volatility
    sigma = 0.01  # 1% baseline

    # Inventory adjustment to reservation price
    inv_adjustment = inventory * config.gamma * (sigma ** 2) * T

    # Signal skew (EWMA-based only now)
    signal_skew = config.k * zscore * 0.01 * T

    # Reservation price for UP
    reservation = up_mid - inv_adjustment + signal_skew
    reservation = max(0.02, min(0.98, reservation))

    # FIXED: Asymmetric spreads based on signal
    base = config.base_spread

    # Time factor (spread widens as time runs out)
    time_factor = 1 + 0.5 * (1 - T)

    # Determine signal direction and strength
    signal_strength = abs(zscore) / config.z_threshold if config.z_threshold > 0 else 0.0

    if zscore >= config.z_threshold:
        # Signal: UP is winning
        # UP side: aggressive (tight spread, closer to ask)
        # DOWN side: passive (wide spread, but still quoting)
        up_spread = base * 0.5 * time_factor  # Tight - want to get filled
        down_spread = base * 2.0 * time_factor  # Wide - passive accumulation
    elif zscore <= -config.z_threshold:
        # Signal: DOWN is winning
        # DOWN side: aggressive (tight spread)
        # UP side: passive (wide spread)
        up_spread = base * 2.0 * time_factor  # Wide - passive
        down_spread = base * 0.5 * time_factor  # Tight - want to get filled
    else:
        # No strong signal: both sides passive (equal spreads)
        up_spread = base * 1.5 * time_factor
        down_spread = base * 1.5 * time_factor

    # Compute final bids
    up_bid = max(0.01, reservation - up_spread / 2)
    down_bid = max(0.01, (1 - reservation) - down_spread / 2)

    return up_bid, down_bid


def should_pull_order(
    order: PendingOrder,
    current_ts: int,
    current_velocity: float,
    current_zscore: float,
    current_mid: float,
    config: ASConfig,
) -> Tuple[bool, Optional[OrderPullReason]]:
    """
    Check if a pending order should be pulled.

    Returns (should_pull, reason).

    Phase 2: Simplified - velocity mode checks removed.
    """
    # Disable pulling entirely if configured
    if config.disable_pulling:
        return (False, None)

    # Condition 1: Time-based expiry
    age_ms = current_ts - order.timestamp_ms
    if age_ms > config.max_order_age_ms:
        return (True, OrderPullReason.TIME_EXPIRED)

    # Condition 2: Z-score direction change (for EWMA-based modes)
    if config.mode == StrategyMode.ASYMMETRIC_EWMA:
        if order.zscore_at_place != 0:
            if np.sign(current_zscore) != np.sign(order.zscore_at_place):
                if abs(current_zscore) > config.z_threshold:
                    return (True, OrderPullReason.ZSCORE_FLIP)

    # Condition 3: Price moved against us
    if order.side == 'UP':
        # If UP price dropped significantly, our bid is now too high
        price_move = (current_mid - order.price) / order.price if order.price > 0 else 0
        if price_move < -config.max_adverse_move:
            return (True, OrderPullReason.PRICE_ADVERSE)
    else:  # DOWN
        # If DOWN price dropped (UP rose), our DOWN bid is too high
        down_mid = 1 - current_mid
        price_move = (down_mid - order.price) / order.price if order.price > 0 else 0
        if price_move < -config.max_adverse_move:
            return (True, OrderPullReason.PRICE_ADVERSE)

    return (False, None)


# =============================================================================
# DATA LOADING
# =============================================================================

def load_data(
    data_split: str = "training",
    verbose: bool = True,
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, str], float]:
    """
    Load data with precomputed signals.

    Args:
        data_split: "training" (IS+OOS2) or "validation" (OOS3+4+5)

    Returns: (btc_df, obs_df, resolution_map, hours)
    """
    if verbose:
        print(f"Loading {data_split} data...")

    # Load 60Hz BTC data
    btc_path = Path("research/binance_hf/btc_prices_combined.csv")
    if verbose:
        print(f"  Loading BTC: {btc_path}")
    btc_df = pd.read_csv(btc_path)
    btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
    if verbose:
        print(f"  BTC total: {len(btc_df):,} rows")

    # Precompute signals ONCE
    btc_df = precompute_velocity(btc_df)
    btc_df = precompute_ewma_zscore(btc_df)

    # Load observer data based on split
    obs_path = Path("research/observer")

    if data_split == "training":
        # IS+OOS2: Jan 16-19
        files = ["grid_obs_20260116.csv", "grid_obs_20260117.csv",
                 "grid_obs_20260118.csv", "grid_obs_20260119.csv"]
    else:
        # Validation: OOS3+4+5
        files = ["grid_obs_20260120.csv", "grid_obs_20260121.csv",
                 "grid_obs_20260122.csv", "grid_obs_20260123.csv",
                 "grid_obs_20260124.csv"]

    obs_dfs = []
    for fname in files:
        f = obs_path / fname
        if f.exists():
            df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)
            if verbose:
                print(f"  Observer: {fname} ({len(df):,} rows)")

    if not obs_dfs:
        raise FileNotFoundError(f"No observer files found for {data_split}")

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    if verbose:
        print(f"  Observer total: {len(obs_df):,} rows")

    # Load resolutions
    res_df = pd.read_csv(obs_path / "market_resolutions_verified.csv")
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Find overlap period
    btc_start, btc_end = btc_df['timestamp_ms'].min(), btc_df['timestamp_ms'].max()
    obs_start, obs_end = obs_df['timestamp_ms'].min(), obs_df['timestamp_ms'].max()

    overlap_start = max(btc_start, obs_start)
    overlap_end = min(btc_end, obs_end)
    hours = (overlap_end - overlap_start) / 3600000

    # Filter to overlap
    btc_df = btc_df[(btc_df['timestamp_ms'] >= overlap_start) &
                     (btc_df['timestamp_ms'] <= overlap_end)].copy()
    obs_df = obs_df[(obs_df['timestamp_ms'] >= overlap_start) &
                     (obs_df['timestamp_ms'] <= overlap_end)].copy()

    # Add resolution and filter valid markets
    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    valid_slugs = []
    for slug, mdf in obs_df.groupby('market_slug'):
        duration = mdf['time_remaining_secs'].max() - mdf['time_remaining_secs'].min()
        if duration >= MIN_RUNTIME_SECS:
            valid_slugs.append(slug)

    obs_df = obs_df[obs_df['market_slug'].isin(valid_slugs)]

    if verbose:
        print(f"  Overlap: {hours:.2f} hours, {len(valid_slugs)} valid markets")

    return btc_df, obs_df, res_map, hours


# =============================================================================
# MERGE SIMULATION
# =============================================================================

def check_and_merge(
    inventory: InventoryState,
    timestamp_ms: int,
    merges: List[MergeEvent],
) -> float:
    """
    Check for mergeable pairs and execute merge.

    Returns profit from merge (0 if no merge).
    """
    pairs = inventory.hedged_pairs
    if pairs <= 0:
        return 0.0

    up_avg = inventory.up_avg_cost
    down_avg = inventory.down_avg_cost
    pair_cost = up_avg + down_avg
    profit_per_pair = 1.0 - pair_cost
    total_profit = pairs * profit_per_pair

    # Record merge event
    merges.append(MergeEvent(
        timestamp_ms=timestamp_ms,
        pairs_merged=pairs,
        up_avg_cost=up_avg,
        down_avg_cost=down_avg,
        pair_cost=pair_cost,
        profit_per_pair=profit_per_pair,
        total_profit=total_profit,
    ))

    # Remove merged pairs from inventory
    # Proportionally reduce cost
    if inventory.up_shares > 0:
        up_cost_per_share = inventory.up_total_cost / inventory.up_shares
        inventory.up_shares -= pairs
        inventory.up_total_cost = inventory.up_shares * up_cost_per_share

    if inventory.down_shares > 0:
        down_cost_per_share = inventory.down_total_cost / inventory.down_shares
        inventory.down_shares -= pairs
        inventory.down_total_cost = inventory.down_shares * down_cost_per_share

    return total_profit


# =============================================================================
# SIMULATION
# =============================================================================

def simulate_market(
    btc_df: pd.DataFrame,
    mdf: pd.DataFrame,
    resolution: str,
    config: ASConfig,
) -> MarketResult:
    """
    Simulate AS market making on a single market.

    Phase 2: Added polarization filter and time-window filter.
    """
    slug = mdf['market_slug'].iloc[0]
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    # Get BTC data with precomputed signals
    market_btc = btc_df[(btc_df['timestamp_ms'] >= market_start - 5000) &
                         (btc_df['timestamp_ms'] <= market_end + 1000)]

    if len(market_btc) == 0:
        return MarketResult(
            market_slug=slug, resolution=resolution, fills=[], merges=[], pulls=[],
            realized_pnl=0, unrealized_pnl=0, merge_profit=0, total_pnl=0,
            final_inventory=InventoryState(), loser_fills=0, winner_fills=0
        )

    # Convert to numpy for speed
    btc_ts_arr = market_btc['timestamp_ms'].values
    btc_vel_arr = market_btc['velocity_bps'].values
    btc_zscore_arr = market_btc['zscore'].values

    obs_ts_arr = mdf['timestamp_ms'].values
    obs_up_bid = mdf['up_bid'].values
    obs_up_ask = mdf['up_ask'].values
    obs_time_rem = mdf['time_remaining_secs'].values

    # Phase 3: Extract spike signals from observer (if available)
    obs_spike_detected = mdf['spike_detected'].values if 'spike_detected' in mdf.columns else np.zeros(len(mdf), dtype=bool)
    obs_spike_direction = mdf['spike_direction'].values if 'spike_direction' in mdf.columns else np.array([''] * len(mdf))

    fills: List[Fill] = []
    merges: List[MergeEvent] = []
    pulls: List[OrderPullEvent] = []
    inventory = InventoryState()
    realized_pnl = 0.0

    # Pending orders
    pending_up: Optional[PendingOrder] = None
    pending_down: Optional[PendingOrder] = None

    obs_idx = 0

    # Phase 2: Track filter bypasses for debugging
    skipped_time_window = 0
    skipped_polarization = 0

    # Phase 3: Track for 200ms execution gap
    last_entry_ts = 0  # Timestamp of last entry attempt
    skipped_spike_filter = 0
    skipped_velocity_aligned = 0
    skipped_min_velocity = 0
    skipped_entry_gap = 0

    for btc_idx in range(len(btc_ts_arr)):
        btc_ts = btc_ts_arr[btc_idx]
        velocity_bps = btc_vel_arr[btc_idx]
        zscore = btc_zscore_arr[btc_idx]

        # Find matching observer row
        while obs_idx < len(obs_ts_arr) - 1 and obs_ts_arr[obs_idx + 1] <= btc_ts:
            obs_idx += 1

        time_rem = obs_time_rem[obs_idx]
        if time_rem < MIN_TIME:
            continue

        up_bid_market = obs_up_bid[obs_idx]
        up_ask_market = obs_up_ask[obs_idx]

        if np.isnan(up_bid_market) or np.isnan(up_ask_market):
            continue

        up_mid = (up_bid_market + up_ask_market) / 2
        down_mid = 1 - up_mid

        # =====================================================================
        # Phase 2: Entry Filters (only apply to NEW order placement, not fills)
        # =====================================================================
        # These flags determine if we can place NEW orders this tick
        can_place_new_orders = True

        # Phase 2: Time-window filter
        # Only place new orders if time_remaining in [min, max]
        if config.entry_window_min_secs > 0 or config.entry_window_max_secs < 900:
            if time_rem < config.entry_window_min_secs:
                can_place_new_orders = False
                skipped_time_window += 1
            elif config.entry_window_max_secs > 0 and time_rem > config.entry_window_max_secs:
                can_place_new_orders = False
                skipped_time_window += 1

        # Phase 2: Polarization filter
        # Only place new orders if market is polarized (< threshold or > 1-threshold)
        if config.enable_polarization_filter:
            pol_thresh = config.polarization_threshold
            if pol_thresh < up_mid < (1 - pol_thresh):
                # Market is in mid-range, skip new orders
                can_place_new_orders = False
                skipped_polarization += 1

        # =====================================================================
        # Phase 3: Signal Discovery Filters
        # =====================================================================

        # Phase 3a: 200ms execution gap - prevent multiple entries on same signal
        if can_place_new_orders and config.min_entry_gap_ms > 0:
            if last_entry_ts > 0 and (btc_ts - last_entry_ts) < config.min_entry_gap_ms:
                can_place_new_orders = False
                skipped_entry_gap += 1

        # Phase 3b: Spike entry filter - only enter on spike signals
        if can_place_new_orders and config.enable_spike_entry:
            spike_detected = obs_spike_detected[obs_idx]
            spike_dir = obs_spike_direction[obs_idx] if spike_detected else ''

            if not spike_detected:
                can_place_new_orders = False
                skipped_spike_filter += 1
            elif config.spike_direction_filter and spike_dir != config.spike_direction_filter:
                # Filter to specific direction (UP or DOWN)
                can_place_new_orders = False
                skipped_spike_filter += 1

        # Phase 3c: Velocity alignment filter (vel_aligned condition)
        # velocity_aligned = same sign for velocity and zscore
        if can_place_new_orders and config.require_velocity_aligned:
            vel_aligned = (velocity_bps > 0 and zscore > 0) or (velocity_bps < 0 and zscore < 0)
            if not vel_aligned:
                can_place_new_orders = False
                skipped_velocity_aligned += 1

        # Phase 3d: Minimum velocity filter (zone_not_neutral condition)
        if can_place_new_orders and config.min_velocity_for_entry > 0:
            if abs(velocity_bps) < config.min_velocity_for_entry:
                can_place_new_orders = False
                skipped_min_velocity += 1

        # Check for fills on pending orders (ALWAYS process, regardless of filters)
        if pending_up is not None:
            # Check if should pull
            should_pull, reason = should_pull_order(
                pending_up, btc_ts, velocity_bps, zscore, up_mid, config
            )
            if should_pull:
                pulls.append(OrderPullEvent(
                    timestamp_ms=btc_ts,
                    side='UP',
                    price=pending_up.price,
                    reason=reason,
                    order_age_ms=btc_ts - pending_up.timestamp_ms,
                    velocity_at_place=pending_up.velocity_at_place,
                    velocity_at_pull=velocity_bps,
                    zscore_at_place=pending_up.zscore_at_place,
                    zscore_at_pull=zscore,
                ))
                pending_up = None
            elif up_ask_market <= pending_up.price:
                # Fill! Market ask touched our bid
                fill_price = pending_up.price
                is_winner = (resolution == 'UP')

                inventory.add_fill('UP', fill_price, config.shares)
                fills.append(Fill(
                    timestamp_ms=btc_ts,
                    side='UP',
                    direction='buy',
                    price=fill_price,
                    shares=config.shares,
                    velocity_bps=velocity_bps,
                    zscore=zscore,
                    inventory_after=inventory.net_inventory,
                    is_winning_side=is_winner,
                ))
                pending_up = None

                # Check for merge (ALL modes that quote both sides)
                merge_profit = check_and_merge(inventory, btc_ts, merges)
                realized_pnl += merge_profit

        if pending_down is not None:
            # Check if should pull
            should_pull, reason = should_pull_order(
                pending_down, btc_ts, velocity_bps, zscore, up_mid, config
            )
            if should_pull:
                pulls.append(OrderPullEvent(
                    timestamp_ms=btc_ts,
                    side='DOWN',
                    price=pending_down.price,
                    reason=reason,
                    order_age_ms=btc_ts - pending_down.timestamp_ms,
                    velocity_at_place=pending_down.velocity_at_place,
                    velocity_at_pull=velocity_bps,
                    zscore_at_place=pending_down.zscore_at_place,
                    zscore_at_pull=zscore,
                ))
                pending_down = None
            else:
                # DOWN fill check: market ask for DOWN = 1 - up_bid
                down_ask_market = 1 - up_bid_market
                if down_ask_market <= pending_down.price:
                    fill_price = pending_down.price
                    is_winner = (resolution == 'DOWN')

                    inventory.add_fill('DOWN', fill_price, config.shares)
                    fills.append(Fill(
                        timestamp_ms=btc_ts,
                        side='DOWN',
                        direction='buy',
                        price=fill_price,
                        shares=config.shares,
                        velocity_bps=velocity_bps,
                        zscore=zscore,
                        inventory_after=inventory.net_inventory,
                        is_winning_side=is_winner,
                    ))
                    pending_down = None

                    # Check for merge (ALL modes)
                    merge_profit = check_and_merge(inventory, btc_ts, merges)
                    realized_pnl += merge_profit

        # Decide what to quote
        quote_up, quote_down = should_quote_sides(
            velocity_bps, zscore, inventory.net_inventory, config
        )

        # Compute our bid prices
        our_up_bid, our_down_bid = compute_as_quotes(
            up_mid, velocity_bps, zscore, time_rem, inventory.net_inventory, config
        )

        # Place new orders if:
        # 1. Entry filters allow (time window + polarization + Phase 3 filters)
        # 2. Strategy says to quote this side
        # 3. No pending order on this side
        # 4. Inventory limits allow
        if can_place_new_orders:
            placed_order = False

            if quote_up and pending_up is None and inventory.up_shares < config.max_inventory:
                pending_up = PendingOrder(
                    timestamp_ms=btc_ts,
                    side='UP',
                    price=our_up_bid,
                    velocity_at_place=velocity_bps,
                    zscore_at_place=zscore,
                )
                placed_order = True

            if quote_down and pending_down is None and inventory.down_shares < config.max_inventory:
                pending_down = PendingOrder(
                    timestamp_ms=btc_ts,
                    side='DOWN',
                    price=our_down_bid,
                    velocity_at_place=velocity_bps,
                    zscore_at_place=zscore,
                )
                placed_order = True

            # Update last entry timestamp for 200ms gap enforcement
            if placed_order:
                last_entry_ts = btc_ts

    # Calculate unrealized PnL from final inventory
    settle_up = 1.0 if resolution == 'UP' else 0.0
    settle_down = 1.0 - settle_up

    unrealized_pnl = 0.0
    if inventory.up_shares > 0:
        unrealized_pnl += (settle_up - inventory.up_avg_cost) * inventory.up_shares
    if inventory.down_shares > 0:
        unrealized_pnl += (settle_down - inventory.down_avg_cost) * inventory.down_shares

    # Count winner/loser fills
    winner_fills = sum(1 for f in fills if f.is_winning_side)
    loser_fills = len(fills) - winner_fills

    merge_profit = sum(m.total_profit for m in merges)

    return MarketResult(
        market_slug=slug,
        resolution=resolution,
        fills=fills,
        merges=merges,
        pulls=pulls,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        merge_profit=merge_profit,
        total_pnl=realized_pnl + unrealized_pnl,
        final_inventory=inventory,
        loser_fills=loser_fills,
        winner_fills=winner_fills,
    )


def run_backtest(
    btc_df: pd.DataFrame,
    obs_df: pd.DataFrame,
    config: ASConfig,
    hours: float,
    show_progress: bool = False,
) -> Tuple[BacktestResult, List[MarketResult]]:
    """
    Run backtest across all markets.

    Returns: (BacktestResult, List[MarketResult])
    """
    # Phase 2: Handle non-default EWMA spans
    # If config uses a non-default span, get cached z-scores and update btc_df
    if config.ewma_slow_span != EWMA_SLOW_SPAN:
        span_zscores = get_zscore_for_span(btc_df, config.ewma_slow_span)
        btc_df = btc_df.copy()  # Don't modify original
        btc_df['zscore'] = span_zscores

    market_results = []
    slugs = obs_df['market_slug'].unique()

    iterator = tqdm(slugs, desc="Markets") if show_progress else slugs

    # Track PnL timeseries for Sharpe/drawdown
    pnl_series = []
    cumulative_pnl = 0.0

    for slug in iterator:
        mdf = obs_df[obs_df['market_slug'] == slug]
        resolution = mdf['resolution'].iloc[0]

        result = simulate_market(btc_df, mdf, resolution, config)
        market_results.append(result)

        cumulative_pnl += result.total_pnl
        pnl_series.append(cumulative_pnl)

    # Aggregate results
    total_fills = sum(len(r.fills) for r in market_results)
    total_pulls = sum(len(r.pulls) for r in market_results)
    realized_pnl = sum(r.realized_pnl for r in market_results)
    unrealized_pnl = sum(r.unrealized_pnl for r in market_results)
    merge_profit = sum(r.merge_profit for r in market_results)
    total_pnl = realized_pnl + unrealized_pnl
    merge_count = sum(len(r.merges) for r in market_results)

    # Count profitable markets
    profitable = sum(1 for r in market_results if r.total_pnl > 0)
    win_rate = profitable / len(market_results) if market_results else 0

    # Adverse selection
    total_loser = sum(r.loser_fills for r in market_results)
    total_winner = sum(r.winner_fills for r in market_results)
    loser_fill_rate = total_loser / total_fills if total_fills > 0 else 0

    # Pull stats by reason
    pulls_by_reason = {}
    for r in market_results:
        for p in r.pulls:
            reason = p.reason.value
            pulls_by_reason[reason] = pulls_by_reason.get(reason, 0) + 1

    pull_rate = total_pulls / (total_pulls + total_fills) if (total_pulls + total_fills) > 0 else 0

    # Sharpe ratio (if enough data)
    pnl_array = np.array(pnl_series)
    if len(pnl_array) > 1:
        returns = np.diff(pnl_array)
        sharpe = np.mean(returns) / np.std(returns) if np.std(returns) > 0 else 0
        sharpe = sharpe * np.sqrt(len(returns))  # Annualize-ish
    else:
        sharpe = 0

    # Max drawdown
    if len(pnl_array) > 0:
        running_max = np.maximum.accumulate(pnl_array)
        drawdowns = pnl_array - running_max
        max_dd = abs(drawdowns.min())
        max_dd_pct = max_dd / running_max[np.argmin(drawdowns)] if running_max[np.argmin(drawdowns)] > 0 else 0
    else:
        max_dd = 0
        max_dd_pct = 0

    hourly_rate = total_pnl / hours if hours > 0 else 0

    result = BacktestResult(
        total_pnl=total_pnl,
        hourly_rate=hourly_rate,
        sharpe_ratio=sharpe,
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        merge_profit=merge_profit,
        total_fills=total_fills,
        merge_count=merge_count,
        markets_traded=len(market_results),
        profitable_markets=profitable,
        win_rate=win_rate,
        total_pulls=total_pulls,
        pulls_by_reason=pulls_by_reason,
        pull_rate=pull_rate,
        loser_fill_rate=loser_fill_rate,
        hours=hours,
        config=config,
    )

    return result, market_results


# =============================================================================
# FORENSIC ANALYSIS
# =============================================================================

def forensic_analysis(
    btc_df: pd.DataFrame,
    obs_df: pd.DataFrame,
    hours: float,
    top_n: int = 10,
):
    """
    Deep dive into profitable markets to understand WHY they made money.
    """
    print("\n" + "=" * 80)
    print("FORENSIC ANALYSIS - Investigating Profitable Markets")
    print("=" * 80)

    # Run with V1 best config to find profitable markets
    config = ASConfig(
        mode=StrategyMode.ASYMMETRIC_EWMA,
        gamma=0.2,
        k=1.0,
        base_spread=0.01,
        z_threshold=1.5,
    )

    result, market_results = run_backtest(btc_df, obs_df, config, hours, show_progress=True)

    # Sort by PnL
    sorted_results = sorted(market_results, key=lambda x: x.total_pnl, reverse=True)

    print(f"\n{'='*80}")
    print(f"TOP {top_n} PROFITABLE MARKETS")
    print(f"{'='*80}")

    for i, mr in enumerate(sorted_results[:top_n], 1):
        print(f"\n[{i}] {mr.market_slug}")
        print(f"    Resolution: {mr.resolution}")
        print(f"    Total PnL: ${mr.total_pnl:.2f}")
        print(f"    Realized: ${mr.realized_pnl:.2f} | Unrealized: ${mr.unrealized_pnl:.2f}")
        print(f"    Fills: {len(mr.fills)} (Winner: {mr.winner_fills}, Loser: {mr.loser_fills})")
        print(f"    Merges: {len(mr.merges)} (Profit: ${mr.merge_profit:.2f})")
        print(f"    Pulls: {len(mr.pulls)}")
        print(f"    Final inventory: UP={mr.final_inventory.up_shares}, DOWN={mr.final_inventory.down_shares}")

        if mr.fills:
            # Analyze fill timing
            velocities = [f.velocity_bps for f in mr.fills]
            zscores = [f.zscore for f in mr.fills]
            print(f"    Fill velocity: mean={np.mean(velocities):.2f}, std={np.std(velocities):.2f}")
            print(f"    Fill z-score: mean={np.mean(zscores):.2f}, std={np.std(zscores):.2f}")

            # Winning fill analysis
            winner_fills = [f for f in mr.fills if f.is_winning_side]
            if winner_fills:
                winner_vels = [f.velocity_bps for f in winner_fills]
                print(f"    Winner fills velocity: mean={np.mean(winner_vels):.2f}")

    print(f"\n{'='*80}")
    print(f"BOTTOM {top_n} MARKETS (Worst Performers)")
    print(f"{'='*80}")

    for i, mr in enumerate(sorted_results[-top_n:], 1):
        print(f"\n[{i}] {mr.market_slug}")
        print(f"    Resolution: {mr.resolution}")
        print(f"    Total PnL: ${mr.total_pnl:.2f}")
        print(f"    Fills: {len(mr.fills)} (Winner: {mr.winner_fills}, Loser: {mr.loser_fills})")
        print(f"    Loser fill rate: {mr.loser_fills / len(mr.fills) * 100:.1f}%" if mr.fills else "N/A")

    # Overall stats
    print(f"\n{'='*80}")
    print("OVERALL STATISTICS")
    print(f"{'='*80}")
    print(f"Hourly rate: ${result.hourly_rate:.2f}/hr")
    print(f"Total PnL: ${result.total_pnl:.2f}")
    print(f"Sharpe ratio: {result.sharpe_ratio:.2f}")
    print(f"Max drawdown: ${result.max_drawdown:.2f} ({result.max_drawdown_pct*100:.1f}%)")
    print(f"Win rate: {result.win_rate*100:.1f}% ({result.profitable_markets}/{result.markets_traded})")
    print(f"Loser fill rate: {result.loser_fill_rate*100:.1f}%")
    print(f"Pull rate: {result.pull_rate*100:.1f}%")


# =============================================================================
# GRID SEARCH
# =============================================================================

def generate_grid_configs(profile: str = "all") -> List[ASConfig]:
    """
    Generate configs for grid search based on profile.

    Phase 2 REDESIGN - Velocity modes removed (2.5% autocorrelation = useless)

    Profiles (Active):
    1. Pure Spread Capture (36) - legacy
    2. Velocity-Filtered Spread (9) - legacy
    4. Asymmetric EWMA (27) - primary V1 mode
    7. Extended EWMA Asymmetric (~320) - z-threshold, EWMA span variations

    Profiles (NEW - Phase 2 with filters):
    12. Ultra-High Accuracy (~288) - tight polarization + time window, 80-87% expected
    13. Balanced Opportunity (~324) - good coverage + moderate accuracy, 75-82% expected
    14. High Volume (~144) - loose filters, most trades, 70-76% expected

    REMOVED Profiles (velocity useless):
    3, 5, 6, 8 - all used velocity signal
    """
    configs = []

    # Profile 1: Pure Spread Capture - 36 configs (legacy)
    if profile in ["all", "1", "pure_spread"]:
        for base_spread in [0.01, 0.02, 0.03]:
            for spread_widening_k in [0, 0.5, 1.0]:
                for max_order_age_ms in [2000, 5000]:
                    for max_adverse_move in [0.02, 0.05]:
                        configs.append(ASConfig(
                            mode=StrategyMode.PURE_SPREAD,
                            base_spread=base_spread,
                            spread_widening_k=spread_widening_k,
                            max_order_age_ms=max_order_age_ms,
                            max_adverse_move=max_adverse_move,
                            velocity_filter_threshold=0.5,
                        ))

    # Profile 2: Velocity-Filtered Spread - 9 configs (legacy)
    if profile in ["all", "2", "velocity_filtered"]:
        for base_spread in [0.01, 0.02, 0.03]:
            for velocity_filter in [0.10, 0.20, 0.50]:
                configs.append(ASConfig(
                    mode=StrategyMode.VELOCITY_FILTERED,
                    base_spread=base_spread,
                    velocity_filter_threshold=velocity_filter,
                ))

    # Profile 4: Asymmetric EWMA - 27 configs (primary V1 mode)
    if profile in ["all", "4", "asymmetric_ewma"]:
        for base_spread in [0.01, 0.02, 0.03]:
            for z_threshold in [0.5, 1.0, 1.5]:
                for gamma in [0.05, 0.1, 0.2]:
                    configs.append(ASConfig(
                        mode=StrategyMode.ASYMMETRIC_EWMA,
                        base_spread=base_spread,
                        z_threshold=z_threshold,
                        gamma=gamma,
                    ))

    # =========================================================================
    # PHASE 2 PROFILES - Extended EWMA with filters
    # =========================================================================

    # Profile 7: Extended EWMA Asymmetric - ~500 configs
    # Extended spans (up to 90s), K tuning (0.75, 1.0, 1.25)
    if profile in ["7", "extended_asymmetric"]:
        for ewma_span in [300, 600, 900, 1800, 3600, 5400]:  # Up to 90s
            for z_threshold in [1.5, 2.0, 2.5, 3.0]:
                for gamma in [0.1, 0.2]:
                    for base_spread in [0.01, 0.02]:
                        for k in [0.75, 1.0, 1.25]:  # K tuning
                            configs.append(ASConfig(
                                mode=StrategyMode.ASYMMETRIC_EWMA,
                                ewma_slow_span=ewma_span,
                                z_threshold=z_threshold,
                                gamma=gamma,
                                base_spread=base_spread,
                                k=k,
                            ))

    # Profile 9: Time-Windowed Entry - ~100 configs
    if profile in ["9", "time_windowed"]:
        for ewma_span in [300, 600, 1800, 3600]:
            for z_threshold in [1.5, 2.0, 2.5]:
                for window_min, window_max in [(300, 600), (200, 500), (400, 700)]:
                    for gamma in [0.1, 0.2]:
                        for base_spread in [0.01, 0.02]:
                            configs.append(ASConfig(
                                mode=StrategyMode.ASYMMETRIC_EWMA,
                                ewma_slow_span=ewma_span,
                                z_threshold=z_threshold,
                                gamma=gamma,
                                base_spread=base_spread,
                                entry_window_min_secs=window_min,
                                entry_window_max_secs=window_max,
                            ))

    # Profile 10: Dynamic Spread Test - ~50 configs
    if profile in ["10", "dynamic_spread"]:
        for ewma_span in [300, 600, 1800]:
            for z_threshold in [1.5, 2.0, 2.5]:
                for gamma in [0.1, 0.2]:
                    for base_spread in [0.01, 0.015, 0.02]:
                        configs.append(ASConfig(
                            mode=StrategyMode.ASYMMETRIC_EWMA,
                            ewma_slow_span=ewma_span,
                            z_threshold=z_threshold,
                            gamma=gamma,
                            base_spread=base_spread,
                            enable_dynamic_spread=True,
                        ))

    # =========================================================================
    # Profile 12: Ultra-High Accuracy - ~288 configs (PRIORITY)
    # Based on 87.4% accuracy finding: tight polarization + time window
    # Expected accuracy: 80-87%
    # =========================================================================
    if profile in ["12", "ultra_high_accuracy"]:
        for ewma_span in [3600, 5400]:  # 60s, 90s - best signal persistence
            for z_threshold in [1.25, 1.5, 1.75]:
                for window_min, window_max in [(400, 500), (450, 500), (350, 500)]:
                    for pol_threshold in [0.20, 0.25, 0.30]:
                        for gamma in [0.1, 0.2]:
                            for spread in [0.01, 0.015]:
                                configs.append(ASConfig(
                                    mode=StrategyMode.ASYMMETRIC_EWMA,
                                    ewma_slow_span=ewma_span,
                                    z_threshold=z_threshold,
                                    gamma=gamma,
                                    base_spread=spread,
                                    entry_window_min_secs=window_min,
                                    entry_window_max_secs=window_max,
                                    polarization_threshold=pol_threshold,
                                    enable_polarization_filter=True,
                                ))

    # =========================================================================
    # Profile 13: Balanced Opportunity - ~324 configs
    # 82.7% accuracy, 82% market coverage
    # Expected accuracy: 75-82%
    # =========================================================================
    if profile in ["13", "balanced"]:
        for ewma_span in [1800, 3600, 5400]:
            for z_threshold in [1.25, 1.5, 2.0]:
                for window_min, window_max in [(300, 600), (350, 550), (400, 500)]:
                    for pol_threshold in [0.25, 0.30, 0.35]:
                        for gamma in [0.1, 0.2]:
                            configs.append(ASConfig(
                                mode=StrategyMode.ASYMMETRIC_EWMA,
                                ewma_slow_span=ewma_span,
                                z_threshold=z_threshold,
                                gamma=gamma,
                                base_spread=0.01,
                                entry_window_min_secs=window_min,
                                entry_window_max_secs=window_max,
                                polarization_threshold=pol_threshold,
                                enable_polarization_filter=True,
                            ))

    # =========================================================================
    # Profile 14: High Volume - ~144 configs
    # More trades, slightly lower accuracy
    # Expected accuracy: 70-76%
    # =========================================================================
    if profile in ["14", "high_volume"]:
        for ewma_span in [1800, 3600]:
            for z_threshold in [1.0, 1.25, 1.5]:
                for window_min, window_max in [(200, 600), (300, 600)]:
                    for pol_threshold in [0.30, 0.35, 0.40]:
                        for gamma in [0.1, 0.2]:
                            configs.append(ASConfig(
                                mode=StrategyMode.ASYMMETRIC_EWMA,
                                ewma_slow_span=ewma_span,
                                z_threshold=z_threshold,
                                gamma=gamma,
                                base_spread=0.01,
                                entry_window_min_secs=window_min,
                                entry_window_max_secs=window_max,
                                polarization_threshold=pol_threshold,
                                enable_polarization_filter=True,
                            ))

    # =========================================================================
    # Profile 15: Signal Discovery Winners - ~48 configs (Phase 3)
    # Based on OOS-validated conditions from short_term_signal_discovery.py:
    # - vel_aligned+time_gt_300: 87% train, 82.7% validation
    # - spike_down+vel_negative: 84.7% train, 92.9% validation
    # - zone_not_neutral+time_gt_300: 85.8% train, 82.0% validation
    # =========================================================================
    if profile in ["15", "signal_discovery"]:
        # Condition 1: vel_aligned + time_gt_300 (no spike trigger)
        for ewma_span in [1800, 3600]:
            for z_threshold in [1.25, 1.5]:
                for min_time in [300, 400, 500]:
                    for gamma in [0.1, 0.2]:
                        configs.append(ASConfig(
                            mode=StrategyMode.ASYMMETRIC_EWMA,
                            ewma_slow_span=ewma_span,
                            z_threshold=z_threshold,
                            gamma=gamma,
                            base_spread=0.01,
                            entry_window_min_secs=min_time,
                            require_velocity_aligned=True,
                            min_entry_gap_ms=200,
                        ))

        # Condition 2: spike_down + vel_aligned (spike-triggered)
        for ewma_span in [1800, 3600]:
            for z_threshold in [1.25, 1.5]:
                for gamma in [0.1, 0.2]:
                    configs.append(ASConfig(
                        mode=StrategyMode.ASYMMETRIC_EWMA,
                        ewma_slow_span=ewma_span,
                        z_threshold=z_threshold,
                        gamma=gamma,
                        base_spread=0.01,
                        enable_spike_entry=True,
                        spike_direction_filter="DOWN",
                        require_velocity_aligned=True,
                        min_entry_gap_ms=200,
                    ))

        # Condition 3: zone_not_neutral + time_gt_300
        for ewma_span in [1800, 3600]:
            for z_threshold in [1.25, 1.5]:
                for min_time in [300, 400]:
                    for gamma in [0.1, 0.2]:
                        configs.append(ASConfig(
                            mode=StrategyMode.ASYMMETRIC_EWMA,
                            ewma_slow_span=ewma_span,
                            z_threshold=z_threshold,
                            gamma=gamma,
                            base_spread=0.01,
                            entry_window_min_secs=min_time,
                            min_velocity_for_entry=0.10,
                            min_entry_gap_ms=200,
                        ))

        # Condition 4: All three combined (vel_aligned + zone_not_neutral + time_gt_300)
        for ewma_span in [1800, 3600]:
            for z_threshold in [1.25, 1.5]:
                for min_time in [300, 400]:
                    for gamma in [0.1, 0.2]:
                        configs.append(ASConfig(
                            mode=StrategyMode.ASYMMETRIC_EWMA,
                            ewma_slow_span=ewma_span,
                            z_threshold=z_threshold,
                            gamma=gamma,
                            base_spread=0.01,
                            entry_window_min_secs=min_time,
                            require_velocity_aligned=True,
                            min_velocity_for_entry=0.10,
                            min_entry_gap_ms=200,
                        ))

    return configs


def run_grid_search(
    btc_df: pd.DataFrame,
    obs_df: pd.DataFrame,
    hours: float,
    profile: str = "all",
    output_csv: str = "research/as_backtest_results.csv",
    checkpoint_interval: int = 10,
) -> pd.DataFrame:
    """
    Run grid search with checkpointing.
    """
    configs = generate_grid_configs(profile)
    print(f"\nGrid search: {len(configs)} configs (profile={profile})")

    results = []
    checkpoint_path = Path(output_csv.replace('.csv', '_checkpoint.csv'))

    # Load existing checkpoint if available
    start_idx = 0
    if checkpoint_path.exists():
        checkpoint_df = pd.read_csv(checkpoint_path)
        results = checkpoint_df.to_dict('records')
        start_idx = len(results)
        print(f"  Resuming from checkpoint: {start_idx}/{len(configs)} done")

    pbar = tqdm(enumerate(configs[start_idx:], start_idx), total=len(configs),
                initial=start_idx, desc="Grid Search")

    for i, config in pbar:
        mode_str = config.mode.value[:10]
        pbar.set_description(f"{mode_str} γ={config.gamma} k={config.k}")

        result, _ = run_backtest(btc_df, obs_df, config, hours, show_progress=False)
        results.append(result.to_dict())

        # Checkpoint every N configs
        if (i + 1) % checkpoint_interval == 0:
            pd.DataFrame(results).to_csv(checkpoint_path, index=False)
            tqdm.write(f"  Checkpoint saved: {i+1}/{len(configs)}")

        # Log positive results
        if result.hourly_rate > 0:
            tqdm.write(f"  {mode_str}: ${result.hourly_rate:.2f}/hr "
                       f"(fills={result.total_fills}, pulls={result.total_pulls})")

    # Save final results
    df = pd.DataFrame(results).sort_values('hourly_rate', ascending=False)
    df.to_csv(output_csv, index=False)
    print(f"\nSaved to {output_csv}")

    # Remove checkpoint
    if checkpoint_path.exists():
        checkpoint_path.unlink()

    return df


def print_grid_summary(df: pd.DataFrame, top_n: int = 20):
    """Print grid search summary."""
    print("\n" + "=" * 120)
    print(f"TOP {top_n} CONFIGURATIONS")
    print("=" * 120)

    cols = ['mode', 'hourly_rate', 'total_fills', 'sharpe_ratio', 'win_rate',
            'loser_fill_rate', 'pull_rate', 'gamma', 'k', 'base_spread']

    print(df.head(top_n)[cols].to_string(index=False))

    # Compare modes
    print("\n" + "=" * 80)
    print("MODE COMPARISON")
    print("=" * 80)

    for mode in df['mode'].unique():
        subset = df[df['mode'] == mode]
        if len(subset) > 0:
            print(f"\n{mode.upper()}:")
            print(f"  Configs: {len(subset)}")
            print(f"  Avg $/hr: ${subset['hourly_rate'].mean():.2f}")
            print(f"  Max $/hr: ${subset['hourly_rate'].max():.2f}")
            print(f"  Avg Sharpe: {subset['sharpe_ratio'].mean():.2f}")
            print(f"  Avg loser fill rate: {subset['loser_fill_rate'].mean()*100:.1f}%")

    # Best config overall
    print("\n" + "=" * 80)
    print("BEST CONFIGURATION")
    print("=" * 80)
    best = df.iloc[0]
    for col in df.columns:
        print(f"  {col}: {best[col]}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="AS Market Making Backtest v2 (Phase 2)")
    parser.add_argument("--forensic", action="store_true", help="Run forensic analysis")
    parser.add_argument("--grid-search", action="store_true", help="Run grid search")
    parser.add_argument("--profile", default="all",
                        help="Grid profile: all, 1, 2, 4, 7, 9, 10, 12 (ultra_high_accuracy), "
                             "13 (balanced), 14 (high_volume), 15 (signal_discovery)")
    parser.add_argument("--output", default="research/as_backtest_results.csv")
    parser.add_argument("--data-split", default="training", choices=["training", "validation"])

    # Single run params
    parser.add_argument("--mode", default="asymmetric_ewma",
                        choices=[m.value for m in StrategyMode])
    parser.add_argument("--gamma", type=float, default=0.1)
    parser.add_argument("--k", type=float, default=1.0)
    parser.add_argument("--spread", type=float, default=0.02)
    parser.add_argument("--min-velocity", type=float, default=0.10)
    parser.add_argument("--z-threshold", type=float, default=1.0)

    # Phase 2 EWMA params
    parser.add_argument("--ewma-span", type=int, default=300,
                        help="EWMA slow span in ticks (300=5s, 600=10s, 1800=30s, 3600=60s, 5400=90s)")
    parser.add_argument("--no-pulling", action="store_true",
                        help="Disable order pulling entirely")
    parser.add_argument("--max-order-age-ms", type=int, default=5000,
                        help="Max order age before pulling (ms)")

    # Phase 2 Filter params (CRITICAL)
    parser.add_argument("--polarization-threshold", type=float, default=0.30,
                        help="Polarization filter: only trade if price < X or > (1-X)")
    parser.add_argument("--enable-polarization", action="store_true",
                        help="Enable polarization filter")
    parser.add_argument("--entry-window-min", type=int, default=0,
                        help="Min time remaining (secs) to place new orders (0=disabled)")
    parser.add_argument("--entry-window-max", type=int, default=900,
                        help="Max time remaining (secs) to place new orders (0=disabled)")
    parser.add_argument("--dynamic-spread", action="store_true",
                        help="Enable signal-strength-based dynamic spread")

    # Phase 3: Signal Discovery params
    parser.add_argument("--enable-spike-entry", action="store_true",
                        help="Only enter on spike signals from observer")
    parser.add_argument("--spike-direction", default="",
                        help="Filter to specific spike direction: UP, DOWN, or empty for both")
    parser.add_argument("--require-velocity-aligned", action="store_true",
                        help="Require velocity and zscore to have same sign")
    parser.add_argument("--min-velocity-for-entry", type=float, default=0.0,
                        help="Min |velocity| for entry (0=disabled, 0.10=zone_not_neutral)")
    parser.add_argument("--min-entry-gap-ms", type=int, default=200,
                        help="Minimum ms between entries (200ms for Polymarket execution)")
    args = parser.parse_args()

    print("=" * 80)
    print("AVELLANEDA-STOIKOV MARKET MAKING BACKTEST v2")
    print("=" * 80)
    print(f"Started: {datetime.now()}")
    print(f"Data split: {args.data_split}")

    btc_df, obs_df, res_map, hours = load_data(args.data_split)

    if args.forensic:
        forensic_analysis(btc_df, obs_df, hours)

    elif args.grid_search:
        df = run_grid_search(btc_df, obs_df, hours, args.profile, args.output)
        print_grid_summary(df)

    else:
        # Single run
        config = ASConfig(
            mode=StrategyMode(args.mode),
            gamma=args.gamma,
            k=args.k,
            base_spread=args.spread,
            min_velocity=args.min_velocity,
            z_threshold=args.z_threshold,
            ewma_slow_span=args.ewma_span,
            disable_pulling=args.no_pulling,
            max_order_age_ms=args.max_order_age_ms,
            # Phase 2 filters
            polarization_threshold=args.polarization_threshold,
            enable_polarization_filter=args.enable_polarization,
            entry_window_min_secs=args.entry_window_min,
            entry_window_max_secs=args.entry_window_max,
            enable_dynamic_spread=args.dynamic_spread,
            # Phase 3 filters
            enable_spike_entry=args.enable_spike_entry,
            spike_direction_filter=args.spike_direction,
            require_velocity_aligned=args.require_velocity_aligned,
            min_velocity_for_entry=args.min_velocity_for_entry,
            min_entry_gap_ms=args.min_entry_gap_ms,
        )

        print(f"\nConfig: {config.to_dict()}")

        result, market_results = run_backtest(btc_df, obs_df, config, hours, show_progress=True)

        print(f"\n{'='*60}")
        print("RESULTS")
        print(f"{'='*60}")
        print(f"Hourly rate:     ${result.hourly_rate:.2f}/hr")
        print(f"Total PnL:       ${result.total_pnl:.2f}")
        print(f"Sharpe ratio:    {result.sharpe_ratio:.2f}")
        print(f"Max drawdown:    ${result.max_drawdown:.2f} ({result.max_drawdown_pct*100:.1f}%)")
        print(f"Win rate:        {result.win_rate*100:.1f}%")
        print(f"\nPnL Breakdown:")
        print(f"  Realized:      ${result.realized_pnl:.2f}")
        print(f"  Unrealized:    ${result.unrealized_pnl:.2f}")
        print(f"  Merge profit:  ${result.merge_profit:.2f}")
        print(f"\nActivity:")
        print(f"  Total fills:   {result.total_fills}")
        print(f"  Merges:        {result.merge_count}")
        print(f"  Markets:       {result.markets_traded}")
        print(f"\nAdverse Selection:")
        print(f"  Loser fill rate: {result.loser_fill_rate*100:.1f}%")
        print(f"\nOrder Pulls:")
        print(f"  Total pulls:   {result.total_pulls}")
        print(f"  Pull rate:     {result.pull_rate*100:.1f}%")
        for reason, count in result.pulls_by_reason.items():
            print(f"    {reason}: {count}")

        # Top markets
        sorted_results = sorted(market_results, key=lambda x: x.total_pnl, reverse=True)
        print(f"\nTop 5 markets:")
        for r in sorted_results[:5]:
            print(f"  {r.market_slug[:40]}: ${r.total_pnl:.2f} ({len(r.fills)} fills)")

        print(f"\nBottom 5 markets:")
        for r in sorted_results[-5:]:
            print(f"  {r.market_slug[:40]}: ${r.total_pnl:.2f} ({len(r.fills)} fills)")

    print(f"\nCompleted: {datetime.now()}")


if __name__ == "__main__":
    main()
