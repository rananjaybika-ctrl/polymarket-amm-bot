#!/usr/bin/env python3
"""
AS Hybrid Strategy Backtest

Combines:
- AGGRESSIVE's proven signal stack (spike detection, EWMA z-score, ~70% directional accuracy)
- AS inventory management (gamma-based rebalancing, pair completion)
- Buy-only execution (exit via merge only)
- Three trust levels (Conservative, Moderate, Aggressive)

MANDATORY CHECKLIST (from CLAUDE_MISTAKES.md):
- [x] Progress bar (tqdm) for loops > 10 iterations
- [x] Checkpoint saves every 10 configs
- [x] Precompute signals ONCE before grid search
- [x] Test on small subset before full run

Usage:
    # Test with 5 configs first
    python research/as_hybrid_backtest.py --test-run

    # Run reduced grid search (243 configs)
    python research/as_hybrid_backtest.py --grid-search --reduced

    # Run full grid search (6000 configs)
    python research/as_hybrid_backtest.py --grid-search

    # Single config run
    python research/as_hybrid_backtest.py --gamma 0.1 --k 1.0 --z-lo 0 --z-hi 1.5

Author: Claude Code
Date: January 29, 2026
"""

import argparse
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
MIN_CYCLE_GAP_MS = 200  # Minimum gap between order placements (matches AGGRESSIVE)

# EWMA parameters for z-score (from volatility_filter_analysis.py)
EWMA_FAST_SPAN = 60   # ~1 second at 60Hz
EWMA_SLOW_SPAN = 300  # ~5 seconds at 60Hz

# Spike detection (from AGGRESSIVE - 72 ticks = 1.2s lookback)
SPIKE_LOOKBACK_TICKS = 72
SPIKE_THRESHOLD = 0.02  # Base threshold 0.02%


# =============================================================================
# ENUMS
# =============================================================================

class TrustLevel(Enum):
    """Signal trust level determines quoting behavior."""
    CONSERVATIVE = "conservative"   # Equal bids, signal only affects small price diff
    MODERATE = "moderate"           # Skewed bids, allow 60-40 imbalance
    AGGRESSIVE = "aggressive"       # Strong skew, allow 80-20 imbalance


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class ASHybridConfig:
    """Configuration for AS Hybrid strategy backtest."""
    # Core AS parameters
    gamma: float = 0.1              # Inventory risk aversion (higher = more conservative)
    k: float = 1.0                  # Signal strength multiplier
    target_pair_cost: float = 0.97  # Max pair cost for profit
    base_spread: float = 0.02       # Base spread (2%)

    # Signal parameters (AGGRESSIVE style)
    z_lo: float = 0.0               # Z-score lower bound
    z_hi: float = 1.5               # Z-score upper bound
    lookback_ticks: int = 72        # 1.2s at 60Hz (AGGRESSIVE default)

    # Trust level
    trust_level: TrustLevel = TrustLevel.MODERATE
    max_imbalance_ratio: float = 0.6  # Max allowed imbalance (0.5=equal, 1.0=all one side)

    # Order management
    max_order_age_ms: int = 5000    # Cancel unfilled after this time
    max_inventory: int = 50         # Max inventory per side
    shares_per_order: int = 10      # Shares per order

    def to_dict(self) -> Dict[str, Any]:
        return {
            'gamma': self.gamma,
            'k': self.k,
            'target_pair_cost': self.target_pair_cost,
            'base_spread': self.base_spread,
            'z_lo': self.z_lo,
            'z_hi': self.z_hi,
            'lookback_ticks': self.lookback_ticks,
            'trust_level': self.trust_level.value,
            'max_imbalance_ratio': self.max_imbalance_ratio,
            'max_order_age_ms': self.max_order_age_ms,
            'max_inventory': self.max_inventory,
            'shares_per_order': self.shares_per_order,
        }


@dataclass
class PendingOrder:
    """Represents a pending order waiting for fill."""
    side: str  # 'UP' or 'DOWN'
    price: float
    timestamp_ms: int
    shares: int


@dataclass
class InventoryState:
    """Track inventory with costs for both sides."""
    up_tokens: int = 0
    down_tokens: int = 0
    up_total_cost: float = 0.0
    down_total_cost: float = 0.0
    total_merges: int = 0

    @property
    def up_avg_cost(self) -> float:
        return self.up_total_cost / self.up_tokens if self.up_tokens > 0 else 0.0

    @property
    def down_avg_cost(self) -> float:
        return self.down_total_cost / self.down_tokens if self.down_tokens > 0 else 0.0

    @property
    def pairs(self) -> int:
        """Number of complete pairs (can be merged)."""
        return min(self.up_tokens, self.down_tokens)

    @property
    def net_exposure(self) -> int:
        """Positive = long UP, Negative = long DOWN."""
        return self.up_tokens - self.down_tokens

    @property
    def imbalance_ratio(self) -> float:
        """Ratio of max side to total. 0.5 = balanced, 1.0 = all one side."""
        total = self.up_tokens + self.down_tokens
        if total == 0:
            return 0.5
        return max(self.up_tokens, self.down_tokens) / total

    def add_up(self, price: float, shares: int):
        """Record UP fill."""
        self.up_tokens += shares
        self.up_total_cost += price * shares

    def add_down(self, price: float, shares: int):
        """Record DOWN fill."""
        self.down_tokens += shares
        self.down_total_cost += price * shares


@dataclass
class MergeEvent:
    """Logged when UP+DOWN pairs are merged."""
    timestamp_ms: int
    pairs_merged: int
    up_avg_cost: float
    down_avg_cost: float
    pair_cost: float
    profit_per_pair: float
    total_profit: float


@dataclass
class MarketResult:
    """Result for a single market."""
    market_slug: str
    resolution: str
    realized_pnl: float      # From merges during market
    unrealized_pnl: float    # From final inventory at resolution
    merge_profit: float      # Sum of merge profits
    total_pnl: float
    total_fills: int
    up_fills: int
    down_fills: int
    merges: int
    final_up_tokens: int
    final_down_tokens: int


@dataclass
class BacktestResult:
    """Aggregate backtest results."""
    config: ASHybridConfig
    total_pnl: float
    hourly_rate: float
    realized_pnl: float
    unrealized_pnl: float
    merge_profit: float
    total_fills: int
    merge_count: int
    markets_traded: int
    profitable_markets: int
    win_rate: float
    hours: float
    avg_pair_cost: float
    direction_accuracy: float  # % of fills on winning side

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.config.to_dict(),
            'total_pnl': self.total_pnl,
            'hourly_rate': self.hourly_rate,
            'realized_pnl': self.realized_pnl,
            'unrealized_pnl': self.unrealized_pnl,
            'merge_profit': self.merge_profit,
            'total_fills': self.total_fills,
            'merge_count': self.merge_count,
            'markets_traded': self.markets_traded,
            'profitable_markets': self.profitable_markets,
            'win_rate': self.win_rate,
            'avg_pair_cost': self.avg_pair_cost,
            'direction_accuracy': self.direction_accuracy,
            'hours': self.hours,
        }


# =============================================================================
# SIGNAL PRECOMPUTATION
# =============================================================================

def precompute_ewma_zscore(btc_df: pd.DataFrame, slow_span: int = EWMA_SLOW_SPAN) -> pd.DataFrame:
    """
    Precompute EWMA z-score for all BTC timestamps.

    Uses EWMA (not OU) per CLAUDE_MISTAKES.md #7 - adapts to regime changes.

    Z-score = (price - ewma_slow) / ewma_std
    - Positive z_score = price above mean = UP likely winning
    - Negative z_score = price below mean = DOWN likely winning
    """
    print(f"  Precomputing EWMA z-score (fast={EWMA_FAST_SPAN}, slow={slow_span})...")

    btc_df = btc_df.sort_values('timestamp_ms').reset_index(drop=True)
    prices = btc_df['price']

    ewma_slow = prices.ewm(span=slow_span, adjust=False).mean()
    deviation = prices - ewma_slow
    ewma_std = deviation.ewm(span=slow_span, adjust=False).std()

    # Avoid division by zero
    ewma_std = ewma_std.replace(0, np.nan).ffill().fillna(0.01)

    zscores = deviation / ewma_std
    zscores = zscores.clip(-5, 5)

    btc_df['zscore'] = zscores
    print(f"    Z-score: min={zscores.min():.2f}, max={zscores.max():.2f}")

    return btc_df


def precompute_spike_detection(btc_df: pd.DataFrame, lookback: int = SPIKE_LOOKBACK_TICKS) -> pd.DataFrame:
    """
    Precompute spike detection for all BTC timestamps.

    Spike = price moved > threshold over lookback period.
    Direction = UP if price increased, DOWN if decreased.
    """
    print(f"  Precomputing spike detection (lookback={lookback} ticks)...")

    btc_df = btc_df.sort_values('timestamp_ms').reset_index(drop=True)
    prices = btc_df['price'].values

    # Calculate % change over lookback period
    spike_directions = np.full(len(prices), '', dtype=object)
    spike_magnitudes = np.zeros(len(prices))

    for i in range(lookback, len(prices)):
        old_price = prices[i - lookback]
        new_price = prices[i]
        if old_price > 0:
            change_pct = (new_price - old_price) / old_price * 100
            magnitude = abs(change_pct)

            if magnitude >= SPIKE_THRESHOLD:
                spike_directions[i] = 'UP' if change_pct > 0 else 'DOWN'
                spike_magnitudes[i] = magnitude

    btc_df['spike_direction'] = spike_directions
    btc_df['spike_magnitude'] = spike_magnitudes

    n_spikes = (spike_magnitudes > 0).sum()
    print(f"    Spikes detected: {n_spikes:,} ({n_spikes / len(btc_df) * 100:.2f}%)")

    return btc_df


# =============================================================================
# QUOTING LOGIC
# =============================================================================

def compute_spike_signal(
    spike_direction: str,
    spike_magnitude: float,
    zscore: float,
    config: ASHybridConfig,
) -> Tuple[Optional[str], float]:
    """
    Compute spike signal using AGGRESSIVE's method.

    Returns: (spike_direction, magnitude)
    - spike_direction: "UP", "DOWN", or None
    - magnitude: spike magnitude if detected
    """
    # No spike detected
    if not spike_direction or spike_magnitude == 0:
        return None, 0.0

    # Z-zone filter: only act on signals within z_lo <= |z| <= z_hi
    abs_z = abs(zscore)
    if not (config.z_lo <= abs_z <= config.z_hi):
        return None, 0.0

    return spike_direction, spike_magnitude


def compute_bids(
    up_mid: float,
    down_mid: float,
    inventory: InventoryState,
    spike_direction: Optional[str],
    zscore: float,
    time_remaining: float,
    config: ASHybridConfig,
) -> Tuple[float, float]:
    """
    Compute bid prices for UP and DOWN tokens.

    Combines AS inventory management with AGGRESSIVE signal skew.

    Returns: (up_bid, down_bid)
    """
    # Normalize time to [0, 1] where 1 = 15 minutes
    time_factor = max(time_remaining / 900.0, 0.01)

    # Base bid (aim for target pair cost)
    base_bid = config.target_pair_cost / 2

    # Inventory adjustment (push toward balance)
    # Positive net_exposure = too many UP -> lower UP bid, raise DOWN bid
    inv_adjust = config.gamma * inventory.net_exposure * time_factor * 0.001

    # Signal adjustment (favor predicted winner)
    signal_adjust = 0.0
    if spike_direction == "UP":
        # Bullish signal: raise UP bid, lower DOWN bid
        signal_adjust = config.k * (abs(zscore) - config.z_lo) * time_factor * 0.01
    elif spike_direction == "DOWN":
        # Bearish signal: lower UP bid, raise DOWN bid
        signal_adjust = -config.k * (abs(zscore) - config.z_lo) * time_factor * 0.01

    # Trust level adjustments
    if config.trust_level == TrustLevel.CONSERVATIVE:
        # Equal bids, signal only affects small price difference
        signal_adjust *= 0.3
    elif config.trust_level == TrustLevel.AGGRESSIVE:
        # Strong signal preference
        signal_adjust *= 1.5

    # Final bids
    # For UP: lower inv_adjust means we have too many UP, so lower our UP bid
    # signal_adjust > 0 means bullish, raise UP bid
    up_bid = base_bid - inv_adjust + signal_adjust
    down_bid = base_bid + inv_adjust - signal_adjust

    # Apply base spread
    up_bid = up_bid - config.base_spread / 2
    down_bid = down_bid - config.base_spread / 2

    # Clamp to valid range (must be below mid to be a bid)
    up_bid = max(0.01, min(up_bid, up_mid - 0.01))
    down_bid = max(0.01, min(down_bid, down_mid - 0.01))

    return up_bid, down_bid


def should_pull_order(order: PendingOrder, current_ts: int, config: ASHybridConfig) -> bool:
    """Check if a pending order should be pulled (cancelled)."""
    age_ms = current_ts - order.timestamp_ms
    return age_ms > config.max_order_age_ms


# =============================================================================
# MERGE LOGIC
# =============================================================================

def execute_merge(inventory: InventoryState, timestamp_ms: int) -> Tuple[float, Optional[MergeEvent]]:
    """
    Execute merge of all available pairs.

    Returns: (profit, MergeEvent or None)
    """
    pairs = inventory.pairs
    if pairs <= 0:
        return 0.0, None

    up_avg = inventory.up_avg_cost
    down_avg = inventory.down_avg_cost
    pair_cost = up_avg + down_avg
    profit_per_pair = 1.0 - pair_cost
    total_profit = pairs * profit_per_pair

    # Record merge event
    merge_event = MergeEvent(
        timestamp_ms=timestamp_ms,
        pairs_merged=pairs,
        up_avg_cost=up_avg,
        down_avg_cost=down_avg,
        pair_cost=pair_cost,
        profit_per_pair=profit_per_pair,
        total_profit=total_profit,
    )

    # Remove merged pairs from inventory (proportionally reduce cost)
    if inventory.up_tokens > 0:
        up_cost_per_share = inventory.up_total_cost / inventory.up_tokens
        inventory.up_tokens -= pairs
        inventory.up_total_cost = inventory.up_tokens * up_cost_per_share

    if inventory.down_tokens > 0:
        down_cost_per_share = inventory.down_total_cost / inventory.down_tokens
        inventory.down_tokens -= pairs
        inventory.down_total_cost = inventory.down_tokens * down_cost_per_share

    inventory.total_merges += pairs

    return total_profit, merge_event


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

    base_path = Path("/Users/rananjaybika/polymarket-amm-bot/research")

    # Load 60Hz BTC data
    btc_path = base_path / "binance_hf/btc_prices_combined.csv"
    if verbose:
        print(f"  Loading BTC: {btc_path}")
    btc_df = pd.read_csv(btc_path)
    btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
    if verbose:
        print(f"  BTC total: {len(btc_df):,} rows")

    # Precompute signals ONCE
    btc_df = precompute_ewma_zscore(btc_df)
    btc_df = precompute_spike_detection(btc_df)

    # Load observer data based on split
    obs_path = base_path / "observer"

    if data_split == "training":
        # IS+OOS2: Jan 16-19
        files = ["grid_obs_20260116.csv", "grid_obs_20260117.csv",
                 "grid_obs_20260118.csv", "grid_obs_20260119.csv"]
    else:
        # Validation: OOS3+4+5
        files = ["grid_obs_oos3_oos4_combined.csv", "grid_obs_oos5.csv"]

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
# SIMULATION
# =============================================================================

def simulate_market(
    btc_df: pd.DataFrame,
    mdf: pd.DataFrame,
    resolution: str,
    config: ASHybridConfig,
) -> MarketResult:
    """
    Simulate AS Hybrid market making on a single market.

    Buy-only execution: place bids on both sides, exit via merge only.
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
            market_slug=slug, resolution=resolution,
            realized_pnl=0, unrealized_pnl=0, merge_profit=0, total_pnl=0,
            total_fills=0, up_fills=0, down_fills=0, merges=0,
            final_up_tokens=0, final_down_tokens=0,
        )

    # Convert to numpy for speed
    btc_ts_arr = market_btc['timestamp_ms'].values
    btc_zscore_arr = market_btc['zscore'].values
    btc_spike_dir_arr = market_btc['spike_direction'].values
    btc_spike_mag_arr = market_btc['spike_magnitude'].values

    obs_ts_arr = mdf['timestamp_ms'].values
    obs_up_bid = mdf['up_bid'].values
    obs_up_ask = mdf['up_ask'].values
    obs_down_bid = mdf['down_bid'].values
    obs_down_ask = mdf['down_ask'].values
    obs_time_rem = mdf['time_remaining_secs'].values

    # State tracking
    inventory = InventoryState()
    realized_pnl = 0.0
    merge_events: List[MergeEvent] = []
    up_fills = 0
    down_fills = 0
    winner_fills = 0

    # Pending orders
    pending_up: Optional[PendingOrder] = None
    pending_down: Optional[PendingOrder] = None

    obs_idx = 0
    last_order_ts = 0

    for btc_idx in range(len(btc_ts_arr)):
        btc_ts = btc_ts_arr[btc_idx]
        zscore = btc_zscore_arr[btc_idx]
        spike_dir = btc_spike_dir_arr[btc_idx]
        spike_mag = btc_spike_mag_arr[btc_idx]

        # Find matching observer row
        while obs_idx < len(obs_ts_arr) - 1 and obs_ts_arr[obs_idx + 1] <= btc_ts:
            obs_idx += 1

        time_rem = obs_time_rem[obs_idx]
        if time_rem < MIN_TIME:
            continue

        up_bid_market = obs_up_bid[obs_idx]
        up_ask_market = obs_up_ask[obs_idx]
        down_bid_market = obs_down_bid[obs_idx]
        down_ask_market = obs_down_ask[obs_idx]

        if np.isnan(up_bid_market) or np.isnan(up_ask_market):
            continue
        if np.isnan(down_bid_market) or np.isnan(down_ask_market):
            continue

        up_mid = (up_bid_market + up_ask_market) / 2
        down_mid = (down_bid_market + down_ask_market) / 2

        # =====================================================================
        # 1. Process pending orders (check fills)
        # =====================================================================
        if pending_up is not None:
            if should_pull_order(pending_up, btc_ts, config):
                pending_up = None
            elif up_ask_market <= pending_up.price:
                # FILL - add to inventory
                inventory.add_up(pending_up.price, config.shares_per_order)
                up_fills += 1
                if resolution == 'UP':
                    winner_fills += 1
                pending_up = None

        if pending_down is not None:
            if should_pull_order(pending_down, btc_ts, config):
                pending_down = None
            elif down_ask_market <= pending_down.price:
                # FILL - add to inventory
                inventory.add_down(pending_down.price, config.shares_per_order)
                down_fills += 1
                if resolution == 'DOWN':
                    winner_fills += 1
                pending_down = None

        # =====================================================================
        # 2. Check for merge opportunity
        # =====================================================================
        if inventory.pairs > 0:
            merge_profit, merge_event = execute_merge(inventory, btc_ts)
            realized_pnl += merge_profit
            if merge_event:
                merge_events.append(merge_event)

        # =====================================================================
        # 3. Check imbalance constraint
        # =====================================================================
        if inventory.imbalance_ratio > config.max_imbalance_ratio:
            # Don't place more orders on heavy side
            can_bid_up = inventory.up_tokens <= inventory.down_tokens
            can_bid_down = inventory.down_tokens <= inventory.up_tokens
        else:
            can_bid_up = inventory.up_tokens < config.max_inventory
            can_bid_down = inventory.down_tokens < config.max_inventory

        # =====================================================================
        # 4. Compute signal
        # =====================================================================
        signal_dir, signal_mag = compute_spike_signal(spike_dir, spike_mag, zscore, config)

        # =====================================================================
        # 5. Enforce minimum cycle gap
        # =====================================================================
        if btc_ts - last_order_ts < MIN_CYCLE_GAP_MS:
            continue

        # =====================================================================
        # 6. Place new orders if allowed
        # =====================================================================
        # For CONSERVATIVE: always quote both sides
        # For others: only quote when signal is present
        should_quote = (signal_dir is not None) or (config.trust_level == TrustLevel.CONSERVATIVE)

        if should_quote:
            up_bid, down_bid = compute_bids(
                up_mid, down_mid, inventory, signal_dir, zscore, time_rem, config
            )

            if pending_up is None and can_bid_up:
                pending_up = PendingOrder(
                    side='UP',
                    price=up_bid,
                    timestamp_ms=btc_ts,
                    shares=config.shares_per_order,
                )
                last_order_ts = btc_ts

            if pending_down is None and can_bid_down:
                pending_down = PendingOrder(
                    side='DOWN',
                    price=down_bid,
                    timestamp_ms=btc_ts,
                    shares=config.shares_per_order,
                )
                last_order_ts = btc_ts

    # =========================================================================
    # Calculate unrealized PnL from final inventory at resolution
    # =========================================================================
    settle_up = 1.0 if resolution == 'UP' else 0.0
    settle_down = 1.0 - settle_up

    unrealized_pnl = 0.0
    if inventory.up_tokens > 0:
        unrealized_pnl += (settle_up - inventory.up_avg_cost) * inventory.up_tokens
    if inventory.down_tokens > 0:
        unrealized_pnl += (settle_down - inventory.down_avg_cost) * inventory.down_tokens

    merge_profit_total = sum(m.total_profit for m in merge_events)
    total_fills = up_fills + down_fills

    return MarketResult(
        market_slug=slug,
        resolution=resolution,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        merge_profit=merge_profit_total,
        total_pnl=realized_pnl + unrealized_pnl,
        total_fills=total_fills,
        up_fills=up_fills,
        down_fills=down_fills,
        merges=len(merge_events),
        final_up_tokens=inventory.up_tokens,
        final_down_tokens=inventory.down_tokens,
    )


def run_backtest(
    btc_df: pd.DataFrame,
    obs_df: pd.DataFrame,
    config: ASHybridConfig,
    hours: float,
    show_progress: bool = False,
) -> Tuple[BacktestResult, List[MarketResult]]:
    """
    Run backtest across all markets.

    Returns: (BacktestResult, List[MarketResult])
    """
    market_results = []
    slugs = obs_df['market_slug'].unique()

    iterator = tqdm(slugs, desc="Markets") if show_progress else slugs

    total_pair_costs = []
    total_winner_fills = 0
    total_fills = 0

    for slug in iterator:
        mdf = obs_df[obs_df['market_slug'] == slug]
        resolution = mdf['resolution'].iloc[0]

        result = simulate_market(btc_df, mdf, resolution, config)
        market_results.append(result)

        # Track winner fill rate for direction accuracy
        if result.total_fills > 0:
            if resolution == 'UP':
                total_winner_fills += result.up_fills
            else:
                total_winner_fills += result.down_fills
            total_fills += result.total_fills

    # Aggregate results
    total_pnl = sum(r.total_pnl for r in market_results)
    realized_pnl = sum(r.realized_pnl for r in market_results)
    unrealized_pnl = sum(r.unrealized_pnl for r in market_results)
    merge_profit = sum(r.merge_profit for r in market_results)
    merge_count = sum(r.merges for r in market_results)
    fill_count = sum(r.total_fills for r in market_results)

    # Count profitable markets
    profitable = sum(1 for r in market_results if r.total_pnl > 0)
    win_rate = profitable / len(market_results) if market_results else 0

    # Average pair cost (from merges)
    # This is a rough estimate - actual pair costs are in MergeEvents
    avg_pair_cost = config.target_pair_cost  # Default

    # Direction accuracy
    direction_accuracy = total_winner_fills / total_fills if total_fills > 0 else 0.5

    hourly_rate = total_pnl / hours if hours > 0 else 0

    result = BacktestResult(
        config=config,
        total_pnl=total_pnl,
        hourly_rate=hourly_rate,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        merge_profit=merge_profit,
        total_fills=fill_count,
        merge_count=merge_count,
        markets_traded=len(market_results),
        profitable_markets=profitable,
        win_rate=win_rate,
        avg_pair_cost=avg_pair_cost,
        direction_accuracy=direction_accuracy,
        hours=hours,
    )

    return result, market_results


# =============================================================================
# GRID SEARCH
# =============================================================================

def generate_grid_configs(reduced: bool = True) -> List[ASHybridConfig]:
    """
    Generate configs for grid search.

    Full set: ~6000 configs
    Reduced set: 243 configs (recommended first pass)
    """
    configs = []

    if reduced:
        # Reduced set: 3^5 * 3 = 729 configs (but some combinations filtered)
        gammas = [0.05, 0.1, 0.2]
        ks = [0.75, 1.0, 1.25]
        pair_costs = [0.95, 0.96, 0.97]
        z_ranges = [(0, 1.0), (0, 1.5), (0, 2.0)]
        trusts = [TrustLevel.CONSERVATIVE, TrustLevel.MODERATE, TrustLevel.AGGRESSIVE]
        spreads = [0.02]  # Fixed
    else:
        # Full set
        gammas = [0.05, 0.1, 0.15, 0.2]
        ks = [0.5, 0.75, 1.0, 1.25, 1.5]
        pair_costs = [0.94, 0.95, 0.96, 0.97, 0.98]
        z_ranges = [(0, 1.0), (0, 1.5), (0, 2.0), (0.5, 1.5), (0.5, 2.0)]
        trusts = [TrustLevel.CONSERVATIVE, TrustLevel.MODERATE, TrustLevel.AGGRESSIVE]
        spreads = [0.01, 0.02]

    for gamma in gammas:
        for k in ks:
            for pair_cost in pair_costs:
                for z_lo, z_hi in z_ranges:
                    for trust in trusts:
                        for spread in spreads:
                            # Set max_imbalance based on trust level
                            if trust == TrustLevel.CONSERVATIVE:
                                max_imbalance = 0.5
                            elif trust == TrustLevel.MODERATE:
                                max_imbalance = 0.6
                            else:  # AGGRESSIVE
                                max_imbalance = 0.8

                            configs.append(ASHybridConfig(
                                gamma=gamma,
                                k=k,
                                target_pair_cost=pair_cost,
                                base_spread=spread,
                                z_lo=z_lo,
                                z_hi=z_hi,
                                trust_level=trust,
                                max_imbalance_ratio=max_imbalance,
                            ))

    return configs


def run_grid_search(
    btc_df: pd.DataFrame,
    obs_df: pd.DataFrame,
    hours: float,
    reduced: bool = True,
    output_csv: str = "research/as_hybrid_results.csv",
    checkpoint_interval: int = 10,
    test_run: bool = False,
) -> pd.DataFrame:
    """
    Run grid search with checkpointing.

    MANDATORY (from CLAUDE_MISTAKES.md):
    - Progress bar for all configs
    - Checkpoint saves every 10 configs
    - Precompute signals ONCE (done in load_data)
    """
    configs = generate_grid_configs(reduced)

    if test_run:
        configs = configs[:5]
        print(f"\nTEST RUN: {len(configs)} configs")
    else:
        print(f"\nGrid search: {len(configs)} configs (reduced={reduced})")

    results = []
    checkpoint_path = Path(output_csv.replace('.csv', '_checkpoint.csv'))

    # Load existing checkpoint if available
    start_idx = 0
    if checkpoint_path.exists() and not test_run:
        checkpoint_df = pd.read_csv(checkpoint_path)
        results = checkpoint_df.to_dict('records')
        start_idx = len(results)
        print(f"  Resuming from checkpoint: {start_idx}/{len(configs)} done")

    pbar = tqdm(enumerate(configs[start_idx:], start_idx), total=len(configs),
                initial=start_idx, desc="Grid Search")

    for i, config in pbar:
        trust_str = config.trust_level.value[:4]
        pbar.set_description(f"γ={config.gamma} k={config.k} z=[{config.z_lo},{config.z_hi}] {trust_str}")

        result, _ = run_backtest(btc_df, obs_df, config, hours, show_progress=False)
        results.append(result.to_dict())

        # Checkpoint every N configs
        if (i + 1) % checkpoint_interval == 0:
            pd.DataFrame(results).to_csv(checkpoint_path, index=False)
            tqdm.write(f"  Checkpoint saved: {i+1}/{len(configs)}")

        # Log positive results
        if result.hourly_rate > 0:
            tqdm.write(f"  ${result.hourly_rate:.2f}/hr | fills={result.total_fills} "
                       f"| merges={result.merge_count} | dir_acc={result.direction_accuracy:.1%}")

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

    cols = ['hourly_rate', 'total_fills', 'merge_count', 'win_rate',
            'direction_accuracy', 'gamma', 'k', 'z_lo', 'z_hi', 'trust_level']

    available_cols = [c for c in cols if c in df.columns]
    print(df.head(top_n)[available_cols].to_string(index=False))

    # Compare trust levels
    print("\n" + "=" * 80)
    print("TRUST LEVEL COMPARISON")
    print("=" * 80)

    for trust in ['conservative', 'moderate', 'aggressive']:
        subset = df[df['trust_level'] == trust]
        if len(subset) > 0:
            print(f"\n{trust.upper()}:")
            print(f"  Configs: {len(subset)}")
            print(f"  Avg $/hr: ${subset['hourly_rate'].mean():.2f}")
            print(f"  Max $/hr: ${subset['hourly_rate'].max():.2f}")
            print(f"  Avg dir accuracy: {subset['direction_accuracy'].mean()*100:.1f}%")

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
    parser = argparse.ArgumentParser(description="AS Hybrid Strategy Backtest")
    parser.add_argument("--grid-search", action="store_true", help="Run grid search")
    parser.add_argument("--reduced", action="store_true", default=True,
                        help="Use reduced grid (243 configs)")
    parser.add_argument("--full", action="store_true", help="Use full grid (~6000 configs)")
    parser.add_argument("--test-run", action="store_true", help="Test with 5 configs")
    parser.add_argument("--output", default="research/as_hybrid_results.csv")
    parser.add_argument("--data-split", default="training", choices=["training", "validation"])

    # Single run params
    parser.add_argument("--gamma", type=float, default=0.1)
    parser.add_argument("--k", type=float, default=1.0)
    parser.add_argument("--target-pair-cost", type=float, default=0.97)
    parser.add_argument("--z-lo", type=float, default=0.0)
    parser.add_argument("--z-hi", type=float, default=1.5)
    parser.add_argument("--trust", default="moderate",
                        choices=["conservative", "moderate", "aggressive"])
    parser.add_argument("--spread", type=float, default=0.02)

    args = parser.parse_args()

    print("=" * 80)
    print("AS HYBRID STRATEGY BACKTEST")
    print("=" * 80)
    print(f"Started: {datetime.now()}")
    print(f"Data split: {args.data_split}")

    btc_df, obs_df, res_map, hours = load_data(args.data_split)

    if args.grid_search or args.test_run:
        reduced = not args.full
        df = run_grid_search(
            btc_df, obs_df, hours,
            reduced=reduced,
            output_csv=args.output,
            test_run=args.test_run,
        )
        print_grid_summary(df)

    else:
        # Single run
        trust_map = {
            'conservative': TrustLevel.CONSERVATIVE,
            'moderate': TrustLevel.MODERATE,
            'aggressive': TrustLevel.AGGRESSIVE,
        }

        config = ASHybridConfig(
            gamma=args.gamma,
            k=args.k,
            target_pair_cost=args.target_pair_cost,
            base_spread=args.spread,
            z_lo=args.z_lo,
            z_hi=args.z_hi,
            trust_level=trust_map[args.trust],
        )

        print(f"\nConfig: {config.to_dict()}")

        result, market_results = run_backtest(btc_df, obs_df, config, hours, show_progress=True)

        print(f"\n{'='*60}")
        print("RESULTS")
        print(f"{'='*60}")
        print(f"Hourly rate:       ${result.hourly_rate:.2f}/hr")
        print(f"Total PnL:         ${result.total_pnl:.2f}")
        print(f"Win rate:          {result.win_rate*100:.1f}%")
        print(f"Direction accuracy: {result.direction_accuracy*100:.1f}%")
        print(f"\nPnL Breakdown:")
        print(f"  Realized:        ${result.realized_pnl:.2f}")
        print(f"  Unrealized:      ${result.unrealized_pnl:.2f}")
        print(f"  Merge profit:    ${result.merge_profit:.2f}")
        print(f"\nActivity:")
        print(f"  Total fills:     {result.total_fills}")
        print(f"  Merges:          {result.merge_count}")
        print(f"  Markets:         {result.markets_traded}")
        print(f"  Profitable:      {result.profitable_markets}")

        # Top markets
        sorted_results = sorted(market_results, key=lambda x: x.total_pnl, reverse=True)
        print(f"\nTop 5 markets:")
        for r in sorted_results[:5]:
            print(f"  {r.market_slug[:40]}: ${r.total_pnl:.2f} ({r.total_fills} fills, {r.merges} merges)")

        print(f"\nBottom 5 markets:")
        for r in sorted_results[-5:]:
            print(f"  {r.market_slug[:40]}: ${r.total_pnl:.2f} ({r.total_fills} fills, {r.merges} merges)")

    print(f"\nCompleted: {datetime.now()}")


if __name__ == "__main__":
    main()
