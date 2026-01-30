#!/usr/bin/env python3
"""
Multi-Phase Strategy Backtest

Implements user's hybrid idea:
  Phase 1 (Accumulation, 500-900s): Bid BOTH sides cheaply (symmetric, deep)
  Phase 2 (Signal Skew, 220-500s):  Aggressive on predicted winner, passive on loser
  Phase 3 (Time Stop, <220s):       No new orders, let positions ride

Key insight: Time stop (220-500s window) recovers ~12pp of adverse selection.
The winning AS config showed +$18.04/hr with this approach.

Based on: avellaneda_stoikov_backtest.py
Reference: research/findings/AS_TIME_STOP_CRITICAL_FINDING.md

Author: Claude Code
Date: January 29, 2026
"""

import sys
from dataclasses import dataclass, field
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
MIN_RUNTIME_SECS = 300  # Minimum market duration

# Signal computation (60Hz BTC data)
VELOCITY_LOOKBACK_TICKS = 180  # 3 seconds at 60Hz
EWMA_FAST_SPAN = 60   # ~1 second
EWMA_SLOW_SPAN = 300  # ~5 seconds


# =============================================================================
# MULTI-PHASE CONFIG
# =============================================================================

@dataclass
class MultiPhaseConfig:
    """Configuration for multi-phase strategy."""

    # Position sizing
    shares: int = 10
    max_inventory: int = 50

    # Phase 1: Accumulation (early market)
    phase1_min_time: int = 500   # Start accumulation phase
    phase1_max_time: int = 900   # End accumulation (= market start)
    phase1_spread: float = 0.08  # Deep bids (8% from mid) - want cheap fills
    phase1_symmetric: bool = True  # Bid both sides equally
    phase1_signal_bias: float = 0.0  # 0 = symmetric, 0.5 = only winner, 0.25 = slight bias
    phase1_z_threshold: float = 0.5  # Lower threshold for weak signal in phase 1

    # Phase 2: Signal-based skew (mid market)
    phase2_min_time: int = 220   # Start signal phase (= time stop)
    phase2_max_time: int = 500   # End signal phase
    phase2_tight_spread: float = 0.02  # Aggressive on winner (2%)
    phase2_wide_spread: float = 0.06   # Passive on loser (6%)
    phase2_z_threshold: float = 1.5    # Z-score threshold for signal
    phase2_require_velocity_aligned: bool = True  # Velocity confirmation

    # Phase 3: Time stop (late market)
    # No new orders below phase2_min_time

    # Order management
    max_order_age_ms: int = 5000  # Pull if unfilled (5s = slow pulling)
    max_adverse_move: float = 0.03
    min_entry_gap_ms: int = 200

    def to_dict(self) -> Dict[str, Any]:
        return {
            'shares': self.shares,
            'phase1_min_time': self.phase1_min_time,
            'phase1_max_time': self.phase1_max_time,
            'phase1_spread': self.phase1_spread,
            'phase2_min_time': self.phase2_min_time,
            'phase2_max_time': self.phase2_max_time,
            'phase2_tight_spread': self.phase2_tight_spread,
            'phase2_wide_spread': self.phase2_wide_spread,
            'phase2_z_threshold': self.phase2_z_threshold,
            'phase2_require_velocity_aligned': self.phase2_require_velocity_aligned,
            'max_order_age_ms': self.max_order_age_ms,
        }


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class PendingOrder:
    timestamp_ms: int
    side: str  # 'UP' or 'DOWN'
    price: float
    velocity_at_place: float
    zscore_at_place: float
    phase: int  # 1 or 2


@dataclass
class Fill:
    timestamp_ms: int
    side: str
    price: float
    shares: int
    velocity_bps: float
    zscore: float
    is_winning_side: bool
    phase: int


@dataclass
class Merge:
    timestamp_ms: int
    up_cost: float
    down_cost: float
    pair_cost: float
    profit: float
    shares: int


@dataclass
class Inventory:
    up_shares: int = 0
    down_shares: int = 0
    up_cost: float = 0.0
    down_cost: float = 0.0

    @property
    def net_inventory(self) -> int:
        return self.up_shares - self.down_shares

    def add_fill(self, side: str, price: float, shares: int):
        if side == 'UP':
            self.up_cost = (self.up_cost * self.up_shares + price * shares) / (self.up_shares + shares) if self.up_shares + shares > 0 else price
            self.up_shares += shares
        else:
            self.down_cost = (self.down_cost * self.down_shares + price * shares) / (self.down_shares + shares) if self.down_shares + shares > 0 else price
            self.down_shares += shares


# =============================================================================
# SIGNAL COMPUTATION
# =============================================================================

def precompute_velocity(btc_df: pd.DataFrame) -> pd.DataFrame:
    """Precompute velocity (bps) from BTC price changes."""
    prices = btc_df['price'].values
    velocity = np.zeros(len(prices))

    for i in range(VELOCITY_LOOKBACK_TICKS, len(prices)):
        old_price = prices[i - VELOCITY_LOOKBACK_TICKS]
        velocity[i] = (prices[i] - old_price) / old_price * 10000  # bps

    btc_df['velocity_bps'] = velocity
    return btc_df


def precompute_ewma_zscore(btc_df: pd.DataFrame) -> pd.DataFrame:
    """Precompute EWMA z-score."""
    prices = btc_df['price'].values

    # EWMA with spans
    ewma_fast = pd.Series(prices).ewm(span=EWMA_FAST_SPAN).mean().values
    ewma_slow = pd.Series(prices).ewm(span=EWMA_SLOW_SPAN).mean().values
    ewma_std = pd.Series(prices).ewm(span=EWMA_SLOW_SPAN).std().values

    # Z-score: how far is fast from slow, normalized
    zscore = np.where(ewma_std > 0, (ewma_fast - ewma_slow) / ewma_std, 0)
    zscore = np.clip(zscore, -5, 5)

    btc_df['zscore'] = zscore
    return btc_df


# =============================================================================
# PHASE LOGIC
# =============================================================================

def get_phase(time_remaining_secs: float, config: MultiPhaseConfig) -> int:
    """Determine current phase based on time remaining."""
    if time_remaining_secs >= config.phase1_min_time:
        return 1  # Accumulation phase
    elif time_remaining_secs >= config.phase2_min_time:
        return 2  # Signal phase
    else:
        return 3  # Time stop (no new orders)


def compute_quotes_phase1(
    up_mid: float,
    zscore: float,
    config: MultiPhaseConfig,
) -> Tuple[Optional[float], Optional[float]]:
    """
    Phase 1: Deep bids with optional signal bias.
    Goal: Accumulate cheap inventory, preferring predicted winner.

    signal_bias=0: symmetric (bid both equally)
    signal_bias=0.5: only bid winner side
    signal_bias=0.25: bid both but winner more aggressive
    """
    spread = config.phase1_spread
    bias = config.phase1_signal_bias

    if bias == 0:
        # Pure symmetric
        up_bid = max(0.01, up_mid - spread)
        down_bid = max(0.01, (1 - up_mid) - spread)
        return up_bid, down_bid

    # Signal-biased accumulation
    z_thresh = config.phase1_z_threshold

    if zscore >= z_thresh:
        # UP predicted winner
        winner_spread = spread * (1 - bias * 0.5)  # Tighter for winner
        loser_spread = spread * (1 + bias)  # Wider for loser (or skip)
        up_bid = max(0.01, up_mid - winner_spread)
        down_bid = max(0.01, (1 - up_mid) - loser_spread) if bias < 0.5 else None
    elif zscore <= -z_thresh:
        # DOWN predicted winner
        winner_spread = spread * (1 - bias * 0.5)
        loser_spread = spread * (1 + bias)
        up_bid = max(0.01, up_mid - loser_spread) if bias < 0.5 else None
        down_bid = max(0.01, (1 - up_mid) - winner_spread)
    else:
        # No signal - symmetric
        up_bid = max(0.01, up_mid - spread)
        down_bid = max(0.01, (1 - up_mid) - spread)

    return up_bid, down_bid


def compute_quotes_phase2(
    up_mid: float,
    zscore: float,
    velocity_bps: float,
    config: MultiPhaseConfig,
) -> Tuple[Optional[float], Optional[float]]:
    """
    Phase 2: Asymmetric bids based on signal.

    Returns (up_bid, down_bid) or None for sides we skip.
    """
    # Check velocity alignment if required
    if config.phase2_require_velocity_aligned:
        vel_aligned = (velocity_bps > 0 and zscore > 0) or (velocity_bps < 0 and zscore < 0)
        if not vel_aligned:
            return None, None  # Skip this tick

    z_thresh = config.phase2_z_threshold
    tight = config.phase2_tight_spread
    wide = config.phase2_wide_spread

    if zscore >= z_thresh:
        # UP predicted to win: aggressive UP, passive DOWN
        up_bid = max(0.01, up_mid - tight)
        down_bid = max(0.01, (1 - up_mid) - wide)
    elif zscore <= -z_thresh:
        # DOWN predicted to win: aggressive DOWN, passive UP
        up_bid = max(0.01, up_mid - wide)
        down_bid = max(0.01, (1 - up_mid) - tight)
    else:
        # Weak signal: skip or symmetric passive
        up_bid = max(0.01, up_mid - wide)
        down_bid = max(0.01, (1 - up_mid) - wide)

    return up_bid, down_bid


# =============================================================================
# MERGE LOGIC
# =============================================================================

def check_and_merge(inventory: Inventory, timestamp_ms: int, merges: List[Merge]) -> float:
    """Merge pairs if available. Returns profit."""
    pairs = min(inventory.up_shares, inventory.down_shares)
    if pairs == 0:
        return 0.0

    # Pair cost = weighted average of fills
    pair_cost = inventory.up_cost + inventory.down_cost
    profit = (1.0 - pair_cost) * pairs

    merges.append(Merge(
        timestamp_ms=timestamp_ms,
        up_cost=inventory.up_cost,
        down_cost=inventory.down_cost,
        pair_cost=pair_cost,
        profit=profit,
        shares=pairs,
    ))

    # Remove merged shares (keep excess on one side)
    inventory.up_shares -= pairs
    inventory.down_shares -= pairs

    # Reset costs for remaining if depleted on a side
    if inventory.up_shares == 0:
        inventory.up_cost = 0.0
    if inventory.down_shares == 0:
        inventory.down_cost = 0.0

    return profit


# =============================================================================
# DATA LOADING
# =============================================================================

def load_data(verbose: bool = True) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, str], float]:
    """Load training data with precomputed signals."""
    if verbose:
        print("Loading training data...")

    # Load 60Hz BTC data
    btc_path = Path("research/binance_hf/btc_prices_combined.csv")
    btc_df = pd.read_csv(btc_path)
    btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
    if verbose:
        print(f"  BTC: {len(btc_df):,} rows")

    # Precompute signals
    if verbose:
        print("  Precomputing velocity...")
    btc_df = precompute_velocity(btc_df)
    if verbose:
        print(f"  Velocity: min={btc_df['velocity_bps'].min():.2f}, max={btc_df['velocity_bps'].max():.2f} bps")

    if verbose:
        print("  Precomputing EWMA z-score...")
    btc_df = precompute_ewma_zscore(btc_df)
    if verbose:
        print(f"  Z-score: min={btc_df['zscore'].min():.2f}, max={btc_df['zscore'].max():.2f}")

    # Load observer data (training: Jan 16-19)
    obs_path = Path("research/observer")
    files = ["grid_obs_20260116.csv", "grid_obs_20260117.csv",
             "grid_obs_20260118.csv", "grid_obs_20260119.csv"]

    obs_dfs = []
    for fname in files:
        f = obs_path / fname
        if f.exists():
            df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)
            if verbose:
                print(f"  Observer: {fname} ({len(df):,} rows)")

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    if verbose:
        print(f"  Observer total: {len(obs_df):,} rows")

    # Load resolutions
    res_path = obs_path / "market_resolutions_verified.csv"
    res_df = pd.read_csv(res_path)
    resolutions = {row['slug']: row['winner'] for _, row in res_df.iterrows()
                   if row['winner'] in ['UP', 'DOWN']}
    if verbose:
        print(f"  Resolutions: {len(resolutions)} markets")

    # Compute hours
    market_slugs = obs_df['market_slug'].unique()
    total_hours = 0.0
    for slug in market_slugs:
        mdf = obs_df[obs_df['market_slug'] == slug]
        if len(mdf) > 1:
            duration = (mdf['timestamp_ms'].max() - mdf['timestamp_ms'].min()) / 1000 / 3600
            total_hours += duration

    if verbose:
        print(f"  Total hours: {total_hours:.2f}")

    return btc_df, obs_df, resolutions, total_hours


# =============================================================================
# BACKTEST SINGLE MARKET
# =============================================================================

def backtest_market(
    market_slug: str,
    market_df: pd.DataFrame,
    btc_df: pd.DataFrame,
    resolution: str,
    config: MultiPhaseConfig,
) -> Dict[str, Any]:
    """Backtest a single market with multi-phase strategy."""

    # Filter to market time range
    start_ts = market_df['timestamp_ms'].min()
    end_ts = market_df['timestamp_ms'].max()

    btc_market = btc_df[
        (btc_df['timestamp_ms'] >= start_ts) &
        (btc_df['timestamp_ms'] <= end_ts)
    ].copy()

    if len(btc_market) < 100:
        return None

    # Arrays for fast access
    btc_ts_arr = btc_market['timestamp_ms'].values
    btc_vel_arr = btc_market['velocity_bps'].values
    btc_zscore_arr = btc_market['zscore'].values

    obs_ts_arr = market_df['timestamp_ms'].values
    obs_time_rem = market_df['time_remaining_secs'].values
    obs_up_bid = market_df['up_bid'].values
    obs_up_ask = market_df['up_ask'].values

    # State
    inventory = Inventory()
    pending_up: Optional[PendingOrder] = None
    pending_down: Optional[PendingOrder] = None

    fills: List[Fill] = []
    merges: List[Merge] = []
    realized_pnl = 0.0

    last_entry_ts = 0
    obs_idx = 0

    # Phase counters
    phase1_entries = 0
    phase2_entries = 0
    skipped_time_stop = 0
    skipped_vel_align = 0

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

        # Determine phase
        phase = get_phase(time_rem, config)

        # =====================================================================
        # FILL CHECKING (always process pending orders)
        # =====================================================================

        if pending_up is not None:
            # Check order age
            age_ms = btc_ts - pending_up.timestamp_ms
            if age_ms > config.max_order_age_ms:
                pending_up = None  # Pull
            elif up_ask_market <= pending_up.price:
                # Fill
                is_winner = (resolution == 'UP')
                inventory.add_fill('UP', pending_up.price, config.shares)
                fills.append(Fill(
                    timestamp_ms=btc_ts,
                    side='UP',
                    price=pending_up.price,
                    shares=config.shares,
                    velocity_bps=velocity_bps,
                    zscore=zscore,
                    is_winning_side=is_winner,
                    phase=pending_up.phase,
                ))
                pending_up = None

                # Check merge
                merge_profit = check_and_merge(inventory, btc_ts, merges)
                realized_pnl += merge_profit

        if pending_down is not None:
            age_ms = btc_ts - pending_down.timestamp_ms
            if age_ms > config.max_order_age_ms:
                pending_down = None  # Pull
            else:
                down_ask_market = 1 - up_bid_market
                if down_ask_market <= pending_down.price:
                    # Fill
                    is_winner = (resolution == 'DOWN')
                    inventory.add_fill('DOWN', pending_down.price, config.shares)
                    fills.append(Fill(
                        timestamp_ms=btc_ts,
                        side='DOWN',
                        price=pending_down.price,
                        shares=config.shares,
                        velocity_bps=velocity_bps,
                        zscore=zscore,
                        is_winning_side=is_winner,
                        phase=pending_down.phase,
                    ))
                    pending_down = None

                    merge_profit = check_and_merge(inventory, btc_ts, merges)
                    realized_pnl += merge_profit

        # =====================================================================
        # NEW ORDER PLACEMENT
        # =====================================================================

        # Entry gap check
        if config.min_entry_gap_ms > 0:
            if last_entry_ts > 0 and (btc_ts - last_entry_ts) < config.min_entry_gap_ms:
                continue

        if phase == 1:
            # Phase 1: Accumulation - deep bids with optional signal bias
            up_bid, down_bid = compute_quotes_phase1(up_mid, zscore, config)

            if up_bid is not None and pending_up is None and inventory.up_shares < config.max_inventory:
                pending_up = PendingOrder(
                    timestamp_ms=btc_ts,
                    side='UP',
                    price=up_bid,
                    velocity_at_place=velocity_bps,
                    zscore_at_place=zscore,
                    phase=1,
                )
                phase1_entries += 1
                last_entry_ts = btc_ts

            if down_bid is not None and pending_down is None and inventory.down_shares < config.max_inventory:
                pending_down = PendingOrder(
                    timestamp_ms=btc_ts,
                    side='DOWN',
                    price=down_bid,
                    velocity_at_place=velocity_bps,
                    zscore_at_place=zscore,
                    phase=1,
                )
                phase1_entries += 1
                last_entry_ts = btc_ts

        elif phase == 2:
            # Phase 2: Signal-based asymmetric
            quotes = compute_quotes_phase2(up_mid, zscore, velocity_bps, config)
            up_bid, down_bid = quotes

            if up_bid is None:
                skipped_vel_align += 1
                continue

            if pending_up is None and inventory.up_shares < config.max_inventory:
                pending_up = PendingOrder(
                    timestamp_ms=btc_ts,
                    side='UP',
                    price=up_bid,
                    velocity_at_place=velocity_bps,
                    zscore_at_place=zscore,
                    phase=2,
                )
                phase2_entries += 1
                last_entry_ts = btc_ts

            if pending_down is None and inventory.down_shares < config.max_inventory:
                pending_down = PendingOrder(
                    timestamp_ms=btc_ts,
                    side='DOWN',
                    price=down_bid,
                    velocity_at_place=velocity_bps,
                    zscore_at_place=zscore,
                    phase=2,
                )
                phase2_entries += 1
                last_entry_ts = btc_ts

        else:
            # Phase 3: Time stop - no new orders
            skipped_time_stop += 1

    # =========================================================================
    # SETTLEMENT
    # =========================================================================

    # Unrealized PnL from unhedged positions
    unrealized = 0.0
    if inventory.up_shares > 0:
        if resolution == 'UP':
            unrealized += (1.0 - inventory.up_cost) * inventory.up_shares
        else:
            unrealized += (0.0 - inventory.up_cost) * inventory.up_shares

    if inventory.down_shares > 0:
        if resolution == 'DOWN':
            unrealized += (1.0 - inventory.down_cost) * inventory.down_shares
        else:
            unrealized += (0.0 - inventory.down_cost) * inventory.down_shares

    total_pnl = realized_pnl + unrealized

    # Fill accuracy
    winner_fills = sum(1 for f in fills if f.is_winning_side)
    fill_accuracy = winner_fills / len(fills) if fills else 0.0

    return {
        'market': market_slug,
        'resolution': resolution,
        'fills': len(fills),
        'merges': len(merges),
        'realized_pnl': realized_pnl,
        'unrealized_pnl': unrealized,
        'total_pnl': total_pnl,
        'fill_accuracy': fill_accuracy,
        'phase1_entries': phase1_entries,
        'phase2_entries': phase2_entries,
        'skipped_time_stop': skipped_time_stop,
        'skipped_vel_align': skipped_vel_align,
        'final_up_shares': inventory.up_shares,
        'final_down_shares': inventory.down_shares,
        'avg_pair_cost': sum(m.pair_cost for m in merges) / len(merges) if merges else 0.0,
    }


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 70)
    print("MULTI-PHASE STRATEGY BACKTEST")
    print("=" * 70)
    print()
    print("Phases:")
    print("  1. Accumulation (500-900s): Symmetric deep bids, accumulate cheap")
    print("  2. Signal Skew (220-500s):  Asymmetric based on z-score + velocity")
    print("  3. Time Stop (<220s):       No new orders, let positions ride")
    print()

    # Load data
    btc_df, obs_df, resolutions, total_hours = load_data()

    # Test configs
    configs = [
        # Baseline: Symmetric accumulation (previous best)
        MultiPhaseConfig(
            shares=10,
            phase1_min_time=500,
            phase1_max_time=900,
            phase1_spread=0.08,
            phase1_signal_bias=0.0,  # Symmetric
            phase2_min_time=220,
            phase2_max_time=500,
            phase2_tight_spread=0.02,
            phase2_wide_spread=0.06,
            phase2_z_threshold=1.5,
            phase2_require_velocity_aligned=True,
            max_order_age_ms=5000,
        ),
        # NEW: Signal-biased accumulation (slight bias)
        MultiPhaseConfig(
            shares=10,
            phase1_min_time=500,
            phase1_max_time=900,
            phase1_spread=0.08,
            phase1_signal_bias=0.25,  # Slight winner bias
            phase1_z_threshold=0.5,
            phase2_min_time=220,
            phase2_max_time=500,
            phase2_tight_spread=0.02,
            phase2_wide_spread=0.06,
            phase2_z_threshold=1.5,
            phase2_require_velocity_aligned=True,
            max_order_age_ms=5000,
        ),
        # NEW: Strong signal bias (only winner)
        MultiPhaseConfig(
            shares=10,
            phase1_min_time=500,
            phase1_max_time=900,
            phase1_spread=0.08,
            phase1_signal_bias=0.5,  # Only bid winner side
            phase1_z_threshold=0.5,
            phase2_min_time=220,
            phase2_max_time=500,
            phase2_tight_spread=0.02,
            phase2_wide_spread=0.06,
            phase2_z_threshold=1.5,
            phase2_require_velocity_aligned=True,
            max_order_age_ms=5000,
        ),
        # NEW: Directional carry only (skip Phase 1 pairs)
        MultiPhaseConfig(
            shares=10,
            phase1_min_time=500,
            phase1_max_time=900,
            phase1_spread=0.06,  # Tighter
            phase1_signal_bias=0.5,  # Only winner
            phase1_z_threshold=0.3,  # Lower threshold
            phase2_min_time=220,
            phase2_max_time=500,
            phase2_tight_spread=0.02,
            phase2_wide_spread=0.10,  # Much wider loser
            phase2_z_threshold=1.0,
            phase2_require_velocity_aligned=True,
            max_order_age_ms=5000,
        ),
        # NEW: Extended accumulation window
        MultiPhaseConfig(
            shares=10,
            phase1_min_time=400,  # Earlier phase 2
            phase1_max_time=900,
            phase1_spread=0.08,
            phase1_signal_bias=0.25,
            phase1_z_threshold=0.5,
            phase2_min_time=220,
            phase2_max_time=400,
            phase2_tight_spread=0.02,
            phase2_wide_spread=0.06,
            phase2_z_threshold=1.5,
            phase2_require_velocity_aligned=True,
            max_order_age_ms=5000,
        ),
    ]

    # Get unique markets
    market_slugs = [s for s in obs_df['market_slug'].unique() if s in resolutions]
    print(f"\nBacktesting {len(market_slugs)} markets across {len(configs)} configs...")
    print()

    results = []

    for cfg_idx, config in enumerate(configs):
        print(f"Config {cfg_idx + 1}: P1 spread={config.phase1_spread}, bias={config.phase1_signal_bias}, P2 z={config.phase2_z_threshold}")

        cfg_results = []

        for slug in tqdm(market_slugs, desc=f"  Config {cfg_idx + 1}", leave=False):
            market_df = obs_df[obs_df['market_slug'] == slug].copy()

            if len(market_df) < MIN_RUNTIME_SECS:
                continue

            resolution = resolutions[slug]

            result = backtest_market(
                market_slug=slug,
                market_df=market_df,
                btc_df=btc_df,
                resolution=resolution,
                config=config,
            )

            if result is not None:
                cfg_results.append(result)

        # Aggregate results
        total_fills = sum(r['fills'] for r in cfg_results)
        total_merges = sum(r['merges'] for r in cfg_results)
        total_realized = sum(r['realized_pnl'] for r in cfg_results)
        total_unrealized = sum(r['unrealized_pnl'] for r in cfg_results)
        total_pnl = sum(r['total_pnl'] for r in cfg_results)

        avg_fill_acc = np.mean([r['fill_accuracy'] for r in cfg_results if r['fills'] > 0])
        avg_pair_cost = np.mean([r['avg_pair_cost'] for r in cfg_results if r['merges'] > 0])

        phase1_entries = sum(r['phase1_entries'] for r in cfg_results)
        phase2_entries = sum(r['phase2_entries'] for r in cfg_results)

        hourly = total_pnl / total_hours if total_hours > 0 else 0

        results.append({
            'config_idx': cfg_idx + 1,
            'p1_spread': config.phase1_spread,
            'p1_bias': config.phase1_signal_bias,
            'p2_z_thresh': config.phase2_z_threshold,
            'fills': total_fills,
            'merges': total_merges,
            'fill_acc': avg_fill_acc,
            'avg_pair_cost': avg_pair_cost,
            'realized': total_realized,
            'unrealized': total_unrealized,
            'total_pnl': total_pnl,
            'hourly': hourly,
            'phase1_entries': phase1_entries,
            'phase2_entries': phase2_entries,
        })

        print(f"    Fills: {total_fills}, Merges: {total_merges}, Fill Acc: {avg_fill_acc:.1%}")
        print(f"    Realized: ${total_realized:.2f}, Unrealized: ${total_unrealized:.2f}")
        print(f"    Total: ${total_pnl:.2f}, Hourly: ${hourly:.2f}/hr")
        print()

    # Summary table
    print("=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)
    print()
    print(f"{'Config':<8} {'P1 Sprd':<8} {'P1 Bias':<8} {'P2 Z':<6} {'Fills':<7} {'Mrg':<5} {'Fill%':<7} {'Pair$':<8} {'Realized':<10} {'Unreal':<10} {'Total':<10} {'$/hr':<8}")
    print("-" * 110)

    for r in results:
        print(f"{r['config_idx']:<8} {r['p1_spread']:<8.2f} {r['p1_bias']:<8.2f} {r['p2_z_thresh']:<6.1f} "
              f"{r['fills']:<7} {r['merges']:<5} {r['fill_acc']:<7.1%} ${r['avg_pair_cost']:<7.3f} "
              f"${r['realized']:<9.2f} ${r['unrealized']:<9.2f} ${r['total_pnl']:<9.2f} ${r['hourly']:<7.2f}")

    print()
    print("PnL Breakdown (best config):")
    best = max(results, key=lambda x: x['hourly'])
    print(f"  Realized (merges): ${best['realized']:.2f} (${best['realized']/total_hours:.2f}/hr)")
    print(f"  Unrealized (carry): ${best['unrealized']:.2f} (${best['unrealized']/total_hours:.2f}/hr)")
    print(f"  Total: ${best['total_pnl']:.2f} (${best['hourly']:.2f}/hr)")
    print()
    print(f"  Phase 1 entries: {best['phase1_entries']}")
    print(f"  Phase 2 entries: {best['phase2_entries']}")

    # Save results
    results_df = pd.DataFrame(results)
    results_df.to_csv("research/multi_phase_backtest_results.csv", index=False)
    print(f"\nResults saved to research/multi_phase_backtest_results.csv")


if __name__ == "__main__":
    main()
