#!/usr/bin/env python3
"""
Enhanced Spike Parameter Optimization Grid Search

Tests all parameter combinations to find optimal settings for the enhanced spike strategy.

Parameter Grid:
- target_shares: 5, 10, 15, 30 (TOTAL shares per trade, split across grid levels)
- grid_levels: 1, 2, 3 (e.g., 30 total / 3 levels = 10 per level)
- grid_spacing: 0.01, 0.02 (only when levels > 1)
- spike_lookback: 18, 30, 36, 48, 60, 72 ticks (300/500/600/800/1000/1200ms at 60Hz)
- stop_loss: 0.03, 0.05, 0.07, 0.12, None (added 3% and 5% for tighter SL testing)
- order_pulling: True, False (entry order pulling)
- entry_order_pull_timeout: 3s, 5s, 7s, 10s, 15s, 20s, 25s, 30s (Path 1: cancel stale entries)
- hedge_ratio: 0.25, 0.50, 0.75, 1.00 (Path 2: partial hedge T1/T2 split)
- aggressive_hedge_timeout: None, 5s, 10s, 15s (Path 2: take market if passive doesn't fill)

Note: Configurations where target_shares % grid_levels != 0 are skipped.
Note: Partial hedge (< 100%) requires stop-loss enabled for T2 protection.
"""

import argparse
import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from datetime import datetime
import multiprocessing as mp
import time
import sys

# =============================================================================
# CONSTANTS
# =============================================================================

# Fixed parameters
SIGNAL_TYPE = "enhanced"
MIN_TIME = 60  # Minimum seconds remaining to enter
MIN_RUNTIME_SECS = 300  # Minimum market duration

# Spike detection (fixed)
SPIKE_THRESHOLD = 0.02  # Base threshold

# Enhanced filtering (fixed)
VELOCITY_CONFIRM_THRESHOLD = 0.10
ENHANCED_SCORE_THRESHOLD = 0.02  # v2 formula: spike_mag * velocity_bps, 0.02 = 2% spike with 1bps vel

# Loser bid calculation
DROP_MULTIPLIER = 0.68
DROP_INTERCEPT = 0.01
TARGET_PAIR_COST = 0.99

# Cycling
MIN_CYCLE_GAP_MS = 200  # Matches observer data rate (5Hz), allows fast cycling

# Capital constraint (for flagging, not blocking)
CAPITAL_LIMIT = 170.0
AVG_PAIR_COST = 0.99  # Approximate cost per share pair

# ATR for adaptive volatility
ATR_PERIOD = 14
ATR_WINDOW = 300
LOW_PERCENTILE = 25
HIGH_PERCENTILE = 75
REGIME_THRESHOLDS = {
    "LOW": 0.010,
    "MEDIUM": 0.020,
    "HIGH": 0.035,
}

# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class OptConfig:
    """Single parameter configuration to test."""
    target_shares: int    # Total shares per trade (e.g., 30)
    grid_levels: int      # How many levels to split into (e.g., 3)
    grid_spacing: float
    spike_lookback: int   # ticks at 60Hz
    stop_loss_pct: Optional[float]
    order_pulling: bool
    # NOTE: order_pull_timeout (40s) was REMOVED - it was never used for hedge orders
    # Hedge orders either fill passively, get stopped out, or ride to resolution
    entry_order_pull_timeout: float = 10.0  # seconds (entry order timeout - Path 1)
    hedge_ratio: float = 1.0  # 1.0 = full hedge, 0.5 = 50% hedge (Path 2)
    aggressive_hedge_timeout: Optional[float] = None  # seconds - take market if passive doesn't fill (None = disabled)
    grid_buycount: int = 1  # How many buy cycles per market (1=single entry, 6=buy 5 shares × 6 cycles)

    @property
    def order_size_per_cycle(self) -> int:
        """Calculate order size per buy cycle (target_shares / grid_buycount)."""
        size = self.target_shares // self.grid_buycount
        return max(5, size)  # Minimum 5 shares (Polymarket constraint)

    @property
    def order_size_per_level(self) -> int:
        """Calculate order size per grid level (target_shares / grid_levels)."""
        return self.target_shares // self.grid_levels  # e.g., 30 / 3 = 10 per level

    @property
    def estimated_cost(self) -> float:
        """Estimate max capital needed per trade."""
        return self.target_shares * AVG_PAIR_COST

    @property
    def lookback_ms(self) -> int:
        """Convert lookback ticks to milliseconds (at 60Hz)."""
        return int(self.spike_lookback * 1000 / 60)

    def __hash__(self):
        return hash((self.target_shares, self.grid_levels, self.grid_spacing,
                    self.spike_lookback, self.stop_loss_pct, self.order_pulling,
                    self.entry_order_pull_timeout, self.hedge_ratio,
                    self.aggressive_hedge_timeout, self.grid_buycount))

    def to_dict(self) -> dict:
        return {
            'target_shares': self.target_shares,
            'order_size_per_level': self.order_size_per_level,
            'order_size_per_cycle': self.order_size_per_cycle,
            'grid_levels': self.grid_levels,
            'grid_spacing': self.grid_spacing,
            'grid_buycount': self.grid_buycount,
            'spike_lookback': self.spike_lookback,
            'lookback_ms': self.lookback_ms,
            'stop_loss_pct': self.stop_loss_pct,
            'order_pulling': self.order_pulling,
            'entry_order_pull_timeout': self.entry_order_pull_timeout,
            'hedge_ratio': self.hedge_ratio,
            'aggressive_hedge_timeout': self.aggressive_hedge_timeout,
            'estimated_cost': self.estimated_cost,
        }


@dataclass
class TradeResult:
    """Result from a single trade."""
    market_slug: str
    cycle_num: int
    entry_time_remaining: float
    signal_score: float
    winner_side: str
    winner_fill_price: float
    loser_fill_price: float
    hedge_type: str  # 'passive', 'stoploss', 'resolution', 'pulled'
    pair_cost: float
    pnl: float
    correct_direction: bool
    spike_magnitude: float
    shares_filled: int
    entry_ts: int
    exit_ts: Optional[int] = None


@dataclass
class OptResult:
    """Result from a single configuration backtest."""
    config: OptConfig
    total_pnl: float
    hourly_rate: float
    total_trades: int
    win_rate: float
    direction_accuracy: float
    passive_hedge_pct: float
    stoploss_hedge_pct: float
    aggressive_hedge_pct: float  # Percentage of trades exited via aggressive hedge (market take)
    resolution_pct: float
    pulled_pct: float  # Percentage of trades cancelled due to order pulling
    capital_exceeded: bool  # True if any trade would exceed $170
    max_capital_used: float
    avg_pair_cost: float
    # PnL breakdown
    passive_pnl: float = 0.0
    stoploss_pnl: float = 0.0
    aggressive_pnl: float = 0.0  # PnL from aggressive hedge exits
    resolution_pnl: float = 0.0
    trades: List[TradeResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        result = self.config.to_dict()
        result.update({
            'total_pnl': self.total_pnl,
            'hourly_rate': self.hourly_rate,
            'total_trades': self.total_trades,
            'win_rate': self.win_rate,
            'direction_accuracy': self.direction_accuracy,
            'passive_hedge_pct': self.passive_hedge_pct,
            'stoploss_hedge_pct': self.stoploss_hedge_pct,
            'aggressive_hedge_pct': self.aggressive_hedge_pct,
            'resolution_pct': self.resolution_pct,
            'pulled_pct': self.pulled_pct,
            'capital_exceeded': self.capital_exceeded,
            'max_capital_used': self.max_capital_used,
            'avg_pair_cost': self.avg_pair_cost,
            'passive_pnl': self.passive_pnl,
            'stoploss_pnl': self.stoploss_pnl,
            'aggressive_pnl': self.aggressive_pnl,
            'resolution_pnl': self.resolution_pnl,
        })
        return result


# =============================================================================
# PARAMETER GRID GENERATION
# =============================================================================

def generate_param_grid(quick: bool = False, path: str = "both") -> List[OptConfig]:
    """Generate all valid parameter combinations.

    Args:
        quick: If True, use reduced parameter space for quick testing
        path: "path1" for entry pulling experiment, "path2" for partial hedge,
              "both" for full grid including both experiments

    Returns:
        List of OptConfig instances to test
    """
    configs = []

    if quick:
        # Quick test mode: reduced parameter space
        target_shares_list = [10, 15]
        grid_levels_list = [1, 2]
        grid_spacings = [0.01]
        lookbacks = [36, 60]
        stop_losses = [None, 0.12]
        order_pulling_opts = [False]
        entry_pull_timeouts = [10.0]  # Default only
        hedge_ratios = [1.0]  # Default only
        aggressive_hedge_timeouts = [None]  # Default only (disabled)
        # Quick mode lookbacks and buycounts
        path1_lookbacks = [60]  # 1000ms only for quick test
        path2_lookbacks = [36]  # 600ms only for quick test
        grid_buycount_path1 = [1, 2]  # Reduced for quick test
        grid_buycount_path2 = [1]  # Single for quick test
    else:
        # Full grid
        # target_shares is the TOTAL shares per trade (split across grid levels)
        target_shares_list = [5, 10, 15, 30]
        grid_levels_list = [1, 2, 3]
        grid_spacings = [0.01, 0.02]
        lookbacks = [18, 24, 30, 60, 72, 84]  # ticks at 60Hz: 300, 400, 500, 1000, 1200, 1400ms
        stop_losses = [0.03, 0.05, 0.07, 0.12, None]  # Added 3% and 5% for tighter SL testing
        order_pulling_opts = [True, False]

        # PATH 1: Entry Order Pulling Experiment
        # Test entry timeouts with 1000ms lookback (best balance of signals/accuracy)
        entry_pull_timeouts = [3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 25.0, 30.0]
        path1_lookbacks = [60, 72, 84]  # 1000, 1200, 1400ms at 60Hz

        # PATH 2: Partial Hedge + Aggressive Hedge Experiment
        # Test hedge ratios with 400ms (best resolution) + 1400ms (high accuracy experiment)
        hedge_ratios = [0.25, 0.50, 0.75, 1.00]
        path2_lookbacks = [18, 24, 30, 84]  # 300, 400, 500ms + 1400ms (75.7% res) at 60Hz
        # Aggressive hedge: take market if passive doesn't fill in X seconds
        aggressive_hedge_timeouts = [None, 5.0, 10.0, 15.0]

        # Grid buycount: how many buy cycles per market
        # Path 1 (volume): more cycles for accumulation
        # Path 2 (quality): fewer cycles, focus on quality signals
        grid_buycount_path1 = [1, 2, 3, 6]
        grid_buycount_path2 = [1, 2]

    for target_shares in target_shares_list:
        for grid_levels in grid_levels_list:
            # Skip if target_shares doesn't divide evenly by grid_levels
            if target_shares % grid_levels != 0:
                continue
            # Only use multiple spacings when grid_levels > 1
            spacings = [0.01] if grid_levels == 1 else grid_spacings
            for spacing in spacings:
                for lookback in lookbacks:
                    for stop_loss in stop_losses:
                        for pulling in order_pulling_opts:
                            # Determine which experiments to run
                            if path == "path1":
                                # PATH 1: Entry pulling with 1000ms, 1200ms, 1400ms lookbacks
                                # Test both pulling ON and OFF to compare
                                if lookback not in path1_lookbacks:
                                    continue
                                for entry_timeout in entry_pull_timeouts:
                                    for buycount in grid_buycount_path1:
                                        # Skip invalid: order size < 5 shares (Polymarket min)
                                        if target_shares // buycount < 5:
                                            continue
                                        configs.append(OptConfig(
                                            target_shares=target_shares,
                                            grid_levels=grid_levels,
                                            grid_spacing=spacing,
                                            spike_lookback=lookback,
                                            stop_loss_pct=stop_loss,
                                            order_pulling=pulling,
                                            entry_order_pull_timeout=entry_timeout,
                                            hedge_ratio=1.0,  # Full hedge for Path 1
                                            grid_buycount=buycount,
                                        ))
                            elif path == "path2":
                                # PATH 2: Partial hedge + aggressive hedge
                                # Only test 300ms, 400ms, 500ms + 1400ms lookbacks
                                if lookback not in path2_lookbacks:
                                    continue
                                for hedge_ratio in hedge_ratios:
                                    # Partial hedge requires stop-loss for T2 protection
                                    if hedge_ratio < 1.0 and stop_loss is None:
                                        continue  # Skip unsafe: partial hedge without SL
                                    # Test all aggressive hedge timeouts (including None = disabled)
                                    for aggressive_timeout in aggressive_hedge_timeouts:
                                        for buycount in grid_buycount_path2:
                                            # Skip invalid: order size < 5 shares (Polymarket min)
                                            if target_shares // buycount < 5:
                                                continue
                                            configs.append(OptConfig(
                                                target_shares=target_shares,
                                                grid_levels=grid_levels,
                                                grid_spacing=spacing,
                                                spike_lookback=lookback,
                                                stop_loss_pct=stop_loss,
                                                order_pulling=pulling,
                                                entry_order_pull_timeout=10.0,  # Default for Path 2
                                                hedge_ratio=hedge_ratio,
                                                aggressive_hedge_timeout=aggressive_timeout,
                                                grid_buycount=buycount,
                                            ))
                            else:
                                # Default: standard grid (backward compatible)
                                configs.append(OptConfig(
                                    target_shares=target_shares,
                                    grid_levels=grid_levels,
                                    grid_spacing=spacing,
                                    spike_lookback=lookback,
                                    stop_loss_pct=stop_loss,
                                    order_pulling=pulling,
                                    entry_order_pull_timeout=10.0,  # Default
                                    hedge_ratio=1.0,  # Default full hedge
                                ))
    return configs


# =============================================================================
# SPIKE DETECTION
# =============================================================================

def calculate_rolling_atr(prices: pd.Series, period: int = ATR_PERIOD) -> pd.Series:
    """Calculate rolling ATR (using price changes as proxy for true range)."""
    tr = prices.diff().abs()
    atr = tr.rolling(window=period).mean()
    return atr


def classify_regime_vectorized(atr_series: pd.Series, window: int = ATR_WINDOW) -> pd.Series:
    """Classify volatility regime for each point based on ATR percentile."""
    percentile = atr_series.rolling(window=window, min_periods=window//2).apply(
        lambda x: (pd.Series(x).rank().iloc[-1] / len(x)) * 100, raw=False
    )

    regime = pd.Series('MEDIUM', index=atr_series.index)
    regime[percentile < LOW_PERCENTILE] = 'LOW'
    regime[percentile > HIGH_PERCENTILE] = 'HIGH'

    return regime


def detect_spikes_for_lookback(btc_df: pd.DataFrame, lookback: int,
                               adaptive_volatility: bool = True) -> pd.DataFrame:
    """Detect spikes using specified lookback period."""
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    # Calculate % change over lookback period
    df['price_prev'] = df['price'].shift(lookback)
    df['change_pct'] = (df['price'] - df['price_prev']) / df['price_prev'] * 100
    df['magnitude'] = df['change_pct'].abs()

    if adaptive_volatility:
        df['atr'] = calculate_rolling_atr(df['price'])
        df['regime'] = classify_regime_vectorized(df['atr'])
        df['threshold'] = df['regime'].map(REGIME_THRESHOLDS)
        df['threshold'] = df['threshold'].fillna(SPIKE_THRESHOLD)
        df['spike_detected'] = df['magnitude'] >= df['threshold']
    else:
        df['spike_detected'] = df['magnitude'] >= SPIKE_THRESHOLD
        df['regime'] = 'MEDIUM'
        df['threshold'] = SPIKE_THRESHOLD

    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['change_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['change_pct'] < 0), 'spike_direction'] = 'DOWN'
    df['spike_magnitude'] = df['magnitude'].where(df['spike_detected'], 0)

    return df[['timestamp_ms', 'price', 'spike_detected', 'spike_direction',
               'spike_magnitude', 'regime']]


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def velocity_confirms(spike_dir: str, velocity_bps: float) -> bool:
    """Check if velocity confirms spike direction."""
    if spike_dir == "UP":
        return velocity_bps > -VELOCITY_CONFIRM_THRESHOLD
    elif spike_dir == "DOWN":
        return velocity_bps < VELOCITY_CONFIRM_THRESHOLD
    return True


def compute_score(spike_mag: float, velocity_bps: float, spike_dir: str,
                  time_rem: float, regime: str = "MEDIUM") -> float:
    """
    Compute enhanced signal score v2 (interaction-based).

    Based on statistical analysis:
    - Spike × Velocity interaction is significant (p=0.001)
    - Time window 300-600s is optimal (88.9% accuracy)
    - HIGH regime gets bonus, LOW already filtered
    """
    # Regime weight
    if regime == "LOW":
        return 0.0  # Should be filtered upstream
    regime_weight = 1.2 if regime == "HIGH" else 1.0

    # Time window weight
    if 300 <= time_rem <= 600:
        time_weight = 1.0  # Optimal
    elif 180 <= time_rem <= 750:
        time_weight = 0.6
    else:
        time_weight = 0.3

    # Core: interaction effect
    interaction = spike_mag * abs(velocity_bps)

    return interaction * time_weight * regime_weight


def compute_score_v1_legacy(spike_mag: float, velocity_bps: float, spike_dir: str,
                            time_rem: float) -> float:
    """Legacy scoring formula - kept for comparison."""
    spike_score = min(spike_mag / 0.05, 1.0)
    velocity_score = min(abs(velocity_bps) / 0.50, 1.0)

    vel_confirms = (spike_dir == "UP" and velocity_bps > 0) or \
                   (spike_dir == "DOWN" and velocity_bps < 0)
    confirm_bonus = 1.0 if vel_confirms else 0.0

    urgency = 1.0 - min(time_rem / 900.0, 1.0)

    return 0.40 * spike_score + 0.30 * velocity_score + 0.20 * confirm_bonus + 0.10 * urgency


def calc_loser_bid(winner_entry: float, spike_mag: float) -> float:
    """Calculate loser side bid price."""
    expected_drop = DROP_MULTIPLIER * spike_mag / 100 + DROP_INTERCEPT
    max_loser = TARGET_PAIR_COST - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid))


def check_capital_constraint(config: OptConfig, capital: float = CAPITAL_LIMIT) -> bool:
    """Check if trade would exceed capital constraint."""
    return config.estimated_cost > capital


# =============================================================================
# GRID LEVELS SIMULATION
# =============================================================================

def simulate_entry_with_grid(obs_row: pd.Series, config: OptConfig,
                             winner_side: str, cycle_shares: int = None) -> List[Dict]:
    """
    Simulate entry across multiple grid levels.

    Returns list of orders, each with price, size, and level.

    Args:
        obs_row: Observer data row
        config: OptConfig
        winner_side: 'UP' or 'DOWN'
        cycle_shares: Shares for this cycle (uses order_size_per_cycle if None)
    """
    if winner_side == 'UP':
        base_price = obs_row['up_ask']
    else:
        base_price = obs_row['down_ask']

    # Determine shares for this cycle
    if cycle_shares is None:
        cycle_shares = config.order_size_per_cycle

    # Split across grid levels
    shares_per_level = max(5, cycle_shares // config.grid_levels)

    orders = []
    for level in range(config.grid_levels):
        price = base_price + (level * config.grid_spacing)
        orders.append({
            'price': price,
            'size': shares_per_level,
            'level': level,
            'placed_at': obs_row['timestamp_ms'],
            'filled': False,
            'fill_price': None,
        })

    return orders


def check_order_fill(order: Dict, current_ask: float) -> bool:
    """Check if a passive order would fill at current ask."""
    # Order fills if ask drops to or below our bid price
    return current_ask <= order['price']


def check_order_pull(order: Dict, current_ts: int, config: OptConfig,
                     order_type: str = "entry") -> bool:
    """Check if entry order should be cancelled due to staleness.

    Note: Hedge order pulling (40s timeout) was REMOVED - it was never used.
    Hedge orders either fill passively, get stopped out, use aggressive exit, or ride to resolution.

    Args:
        order: Order dict with 'placed_at' timestamp
        current_ts: Current timestamp in ms
        config: OptConfig with timeout settings
        order_type: "entry" only (hedge order pulling is not used)

    Returns:
        True if order should be pulled (cancelled)
    """
    if not config.order_pulling:
        return False

    # Only entry orders use pulling now
    timeout = config.entry_order_pull_timeout

    age_secs = (current_ts - order['placed_at']) / 1000
    return age_secs > timeout


# =============================================================================
# MARKET SIMULATION
# =============================================================================

def simulate_market(spikes_df: pd.DataFrame, obs_df: pd.DataFrame,
                    slug: str, resolution: str, config: OptConfig) -> List[TradeResult]:
    """Simulate trading on a single market using specified configuration."""
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    # Get spikes in this market's time range
    market_spikes = spikes_df[(spikes_df['timestamp_ms'] >= market_start) &
                               (spikes_df['timestamp_ms'] <= market_end)].copy()

    trades = []
    cycle_num = 0
    last_trade_ts = 0
    cycles_completed = 0  # Successful buy cycles (limited by grid_buycount)
    total_shares_accumulated = 0  # Total shares bought across all cycles

    # Iterate through spike events
    for _, spike_row in market_spikes.iterrows():
        # Check if we've hit the cycle limit
        if cycles_completed >= config.grid_buycount:
            break  # Stop processing this market

        spike_ts = spike_row['timestamp_ms']
        spike_dir = spike_row['spike_direction']
        spike_mag = spike_row['spike_magnitude']

        # Check cycle gap
        if (spike_ts - last_trade_ts) < MIN_CYCLE_GAP_MS:
            continue

        # Find nearest observer row
        obs_idx = mdf['timestamp_ms'].searchsorted(spike_ts)
        if obs_idx >= len(mdf):
            obs_idx = len(mdf) - 1

        obs_row = mdf.iloc[obs_idx]
        time_rem = obs_row['time_remaining_secs']

        if time_rem < MIN_TIME:
            continue

        velocity_bps = obs_row.get('velocity_bps', 0) or 0
        regime = spike_row.get('regime', 'MEDIUM')

        # Apply enhanced signal filter
        if not velocity_confirms(spike_dir, velocity_bps):
            continue
        score = compute_score(spike_mag, velocity_bps, spike_dir, time_rem, regime)
        if score < ENHANCED_SCORE_THRESHOLD:
            continue

        # Entry signal detected
        cycle_num += 1
        winner_side = spike_dir
        loser_side = "DOWN" if winner_side == "UP" else "UP"

        # Calculate shares for this cycle (may be less if approaching target)
        remaining_shares = config.target_shares - total_shares_accumulated
        cycle_shares = min(config.order_size_per_cycle, remaining_shares)
        if cycle_shares < 5:  # Polymarket minimum
            break  # Can't place valid order

        # Create grid orders for winner side entry
        winner_orders = simulate_entry_with_grid(obs_row, config, winner_side, cycle_shares)

        # Track order filling with grid levels
        total_winner_filled = 0
        total_winner_cost = 0.0
        entry_complete = False
        pulled = False

        # Scan forward to fill winner orders (and check for order pulling)
        for j in range(obs_idx, len(mdf)):
            scan_row = mdf.iloc[j]
            scan_ts = scan_row['timestamp_ms']

            if winner_side == "UP":
                current_ask = scan_row['up_ask']
            else:
                current_ask = scan_row['down_ask']

            # Check each unfilled order
            for order in winner_orders:
                if order['filled']:
                    continue

                # Check for entry order pull first (uses entry_order_pull_timeout)
                if check_order_pull(order, scan_ts, config, order_type="entry"):
                    pulled = True
                    break

                # Check for fill
                if check_order_fill(order, current_ask):
                    order['filled'] = True
                    order['fill_price'] = order['price']  # Limit order fill at our price
                    total_winner_filled += order['size']
                    total_winner_cost += order['price'] * order['size']

            if pulled:
                break

            # Check if all orders filled
            if all(o['filled'] for o in winner_orders):
                entry_complete = True
                entry_obs_idx = j
                break

        # If order was pulled or not all levels filled, skip this trade
        if pulled or not entry_complete:
            if pulled:
                trades.append(TradeResult(
                    market_slug=slug, cycle_num=cycle_num, entry_time_remaining=time_rem,
                    signal_score=score, winner_side=winner_side,
                    winner_fill_price=0, loser_fill_price=0,
                    hedge_type="pulled", pair_cost=0, pnl=0,
                    correct_direction=(resolution == winner_side),
                    spike_magnitude=spike_mag, shares_filled=0,
                    entry_ts=spike_ts
                ))
            continue

        # Calculate average winner entry price
        avg_winner_entry = total_winner_cost / total_winner_filled if total_winner_filled > 0 else 0

        # Calculate loser bid target
        loser_target = calc_loser_bid(avg_winner_entry, spike_mag)

        # Scan forward for hedge
        hedge_type = "resolution"
        loser_fill = 0.0
        entry_fill_ts = mdf.iloc[entry_obs_idx]['timestamp_ms']  # Track entry fill timestamp

        for j in range(entry_obs_idx + 1, len(mdf)):
            scan_row = mdf.iloc[j]
            scan_ts = scan_row['timestamp_ms']

            if loser_side == "UP":
                curr_loser_ask = scan_row['up_ask']
                winner_bid = scan_row['down_bid']
            else:
                curr_loser_ask = scan_row['down_ask']
                winner_bid = scan_row['up_bid']

            # Passive fill
            if curr_loser_ask <= loser_target:
                loser_fill = loser_target
                hedge_type = "passive"
                break

            # Aggressive hedge: take market if passive doesn't fill within timeout
            if config.aggressive_hedge_timeout is not None:
                hedge_age_secs = (scan_ts - entry_fill_ts) / 1000
                if hedge_age_secs > config.aggressive_hedge_timeout:
                    loser_fill = curr_loser_ask  # Take current market price
                    hedge_type = "aggressive"
                    break

            # Stop-loss
            if config.stop_loss_pct is not None:
                drop = (avg_winner_entry - winner_bid) / avg_winner_entry
                if drop >= config.stop_loss_pct:
                    loser_fill = curr_loser_ask
                    hedge_type = "stoploss"
                    break

        # Resolution handling - if direction correct, loser MUST fill (goes to $0)
        if hedge_type == "resolution":
            if resolution == winner_side:
                hedge_type = "passive"
                loser_fill = loser_target
            else:
                loser_fill = 1.0

        # Calculate PnL with partial hedge support (Path 2 experiment)
        # T1 = hedged portion (hedge_ratio), T2 = unhedged portion (1 - hedge_ratio)
        # Note: Use cycle_shares (not target_shares) for this cycle's PnL
        hedge_ratio = config.hedge_ratio
        t1_shares = int(cycle_shares * hedge_ratio)
        t2_shares = cycle_shares - t1_shares

        pair_cost = avg_winner_entry + loser_fill

        if hedge_ratio >= 1.0:
            # Full hedge (standard behavior)
            if hedge_type == "resolution":
                pnl = -avg_winner_entry * cycle_shares
            else:
                pnl = (1.0 - pair_cost) * cycle_shares
        else:
            # Partial hedge: T1 hedged immediately, T2 rides to resolution
            # T1 PnL (hedged portion)
            if hedge_type == "resolution":
                t1_pnl = -avg_winner_entry * t1_shares
            else:
                t1_pnl = (1.0 - pair_cost) * t1_shares

            # T2 PnL (unhedged portion - rides to resolution)
            if resolution == winner_side:
                # Winner wins: T2 gets $1 per share
                t2_pnl = (1.0 - avg_winner_entry) * t2_shares
            else:
                # Winner loses: T2 goes to $0
                t2_pnl = -avg_winner_entry * t2_shares

            pnl = t1_pnl + t2_pnl

        trades.append(TradeResult(
            market_slug=slug, cycle_num=cycle_num, entry_time_remaining=time_rem,
            signal_score=score, winner_side=winner_side,
            winner_fill_price=avg_winner_entry, loser_fill_price=loser_fill,
            hedge_type=hedge_type, pair_cost=pair_cost, pnl=pnl,
            correct_direction=(resolution == winner_side),
            spike_magnitude=spike_mag, shares_filled=cycle_shares,
            entry_ts=spike_ts
        ))

        # Track cycle completion for grid_buycount limit
        cycles_completed += 1
        total_shares_accumulated += cycle_shares
        last_trade_ts = spike_ts

    return trades


# =============================================================================
# BACKTEST RUNNER
# =============================================================================

def run_single_config(config: OptConfig, spikes_by_lookback: Dict[int, pd.DataFrame],
                      obs_df: pd.DataFrame, hours: float,
                      market_resolutions: Dict[str, str]) -> OptResult:
    """Run backtest for a single configuration."""
    # Use pre-computed spikes for this lookback
    spikes_only = spikes_by_lookback[config.spike_lookback]

    # Run simulation across all markets
    all_trades = []
    max_capital = 0.0

    for slug in obs_df['market_slug'].unique():
        mdf = obs_df[obs_df['market_slug'] == slug]
        resolution = market_resolutions.get(slug, 'UP')

        trades = simulate_market(spikes_only, obs_df, slug, resolution, config)
        all_trades.extend(trades)

        # Track max capital (simplified: single trade capital)
        if trades:
            max_capital = max(max_capital, config.estimated_cost)

    # Calculate results
    if not all_trades:
        return OptResult(
            config=config, total_pnl=0, hourly_rate=0, total_trades=0,
            win_rate=0, direction_accuracy=0, passive_hedge_pct=0,
            stoploss_hedge_pct=0, aggressive_hedge_pct=0, resolution_pct=0, pulled_pct=0,
            capital_exceeded=check_capital_constraint(config),
            max_capital_used=0, avg_pair_cost=0
        )

    # Filter out pulled trades for main stats
    executed_trades = [t for t in all_trades if t.hedge_type != "pulled"]
    pulled_trades = [t for t in all_trades if t.hedge_type == "pulled"]

    total_trades = len(all_trades)
    executed_count = len(executed_trades)

    if executed_count == 0:
        return OptResult(
            config=config, total_pnl=0, hourly_rate=0, total_trades=total_trades,
            win_rate=0, direction_accuracy=0, passive_hedge_pct=0,
            stoploss_hedge_pct=0, aggressive_hedge_pct=0, resolution_pct=0,
            pulled_pct=len(pulled_trades) / total_trades if total_trades > 0 else 0,
            capital_exceeded=check_capital_constraint(config),
            max_capital_used=max_capital, avg_pair_cost=0
        )

    total_pnl = sum(t.pnl for t in executed_trades)
    hourly_rate = total_pnl / hours if hours > 0 else 0

    wins = sum(1 for t in executed_trades if t.pnl > 0)
    hedged = [t for t in executed_trades if t.hedge_type != "resolution"]
    avg_pair = np.mean([t.pair_cost for t in hedged]) if hedged else 0

    passive = sum(1 for t in executed_trades if t.hedge_type == "passive")
    stoploss = sum(1 for t in executed_trades if t.hedge_type == "stoploss")
    aggressive = sum(1 for t in executed_trades if t.hedge_type == "aggressive")
    resolution = sum(1 for t in executed_trades if t.hedge_type == "resolution")
    correct = sum(1 for t in executed_trades if t.correct_direction)

    passive_pnl = sum(t.pnl for t in executed_trades if t.hedge_type == "passive")
    stoploss_pnl = sum(t.pnl for t in executed_trades if t.hedge_type == "stoploss")
    aggressive_pnl = sum(t.pnl for t in executed_trades if t.hedge_type == "aggressive")
    resolution_pnl = sum(t.pnl for t in executed_trades if t.hedge_type == "resolution")

    return OptResult(
        config=config,
        total_pnl=total_pnl,
        hourly_rate=hourly_rate,
        total_trades=total_trades,
        win_rate=wins / executed_count,
        direction_accuracy=correct / executed_count,
        passive_hedge_pct=passive / executed_count,
        stoploss_hedge_pct=stoploss / executed_count,
        aggressive_hedge_pct=aggressive / executed_count,
        resolution_pct=resolution / executed_count,
        pulled_pct=len(pulled_trades) / total_trades if total_trades > 0 else 0,
        capital_exceeded=check_capital_constraint(config),
        max_capital_used=max_capital,
        avg_pair_cost=avg_pair,
        passive_pnl=passive_pnl,
        stoploss_pnl=stoploss_pnl,
        aggressive_pnl=aggressive_pnl,
        resolution_pnl=resolution_pnl,
        trades=all_trades
    )


# Global variables for multiprocessing (set before fork)
_GLOBAL_SPIKES_BY_LOOKBACK = None
_GLOBAL_OBS_DF = None
_GLOBAL_HOURS = None
_GLOBAL_RESOLUTIONS = None


def _run_config_worker(config: OptConfig) -> OptResult:
    """Worker function that uses global data."""
    return run_single_config(config, _GLOBAL_SPIKES_BY_LOOKBACK, _GLOBAL_OBS_DF,
                            _GLOBAL_HOURS, _GLOBAL_RESOLUTIONS)


def precompute_spikes(btc_df: pd.DataFrame, lookbacks: List[int],
                      adaptive_volatility: bool = True) -> Dict[int, pd.DataFrame]:
    """Pre-compute spikes for all lookback values."""
    print("\nPre-computing spikes for all lookback values...")
    spikes_by_lookback = {}

    for lookback in lookbacks:
        ms = lookback * 1000 // 60
        print(f"  Lookback {lookback} ticks ({ms}ms)...", end=' ', flush=True)
        spikes_df = detect_spikes_for_lookback(btc_df, lookback, adaptive_volatility)
        # Filter out LOW regime spikes (48% accuracy = worse than coin flip)
        spikes_only = spikes_df[
            (spikes_df['spike_detected'] == True) &
            (spikes_df['regime'] != 'LOW')
        ].copy()
        spikes_by_lookback[lookback] = spikes_only
        print(f"{len(spikes_only):,} spikes (excl. LOW regime)")

    return spikes_by_lookback


def run_optimization(btc_df: pd.DataFrame, obs_df: pd.DataFrame, hours: float,
                     market_resolutions: Dict[str, str], n_workers: int = 4,
                     quick: bool = False, path: str = "both") -> List[OptResult]:
    """Run all configurations in parallel."""
    configs = generate_param_grid(quick=quick, path=path)
    print(f"\nTotal configurations to test: {len(configs)}")

    # Get unique lookback values
    lookbacks = list(set(c.spike_lookback for c in configs))
    print(f"Unique lookback values: {lookbacks}")

    # Pre-compute spikes for all lookback values (major optimization)
    spikes_by_lookback = precompute_spikes(btc_df, lookbacks, adaptive_volatility=True)

    results = []
    start_time = time.time()

    if n_workers == 1:
        # Sequential execution for debugging
        for i, config in enumerate(configs):
            result = run_single_config(config, spikes_by_lookback, obs_df, hours, market_resolutions)
            results.append(result)
            if (i + 1) % 10 == 0 or (i + 1) == len(configs):
                elapsed = time.time() - start_time
                rate = (i + 1) / elapsed
                remaining = (len(configs) - i - 1) / rate if rate > 0 else 0
                print(f"  Progress: {i+1}/{len(configs)} ({(i+1)/len(configs)*100:.1f}%) "
                      f"- ETA: {remaining/60:.1f}m")
    else:
        # Parallel execution using fork for data sharing
        ctx = mp.get_context('fork')  # Use fork to share memory on macOS/Linux

        # Set up global data first (for fork-based sharing)
        global _GLOBAL_SPIKES_BY_LOOKBACK, _GLOBAL_OBS_DF, _GLOBAL_HOURS, _GLOBAL_RESOLUTIONS
        _GLOBAL_SPIKES_BY_LOOKBACK = spikes_by_lookback
        _GLOBAL_OBS_DF = obs_df
        _GLOBAL_HOURS = hours
        _GLOBAL_RESOLUTIONS = market_resolutions

        with ctx.Pool(processes=n_workers) as pool:
            # Use imap_unordered for better progress tracking
            completed = 0
            for result in pool.imap_unordered(_run_config_worker, configs, chunksize=1):
                results.append(result)
                completed += 1

                if completed % 20 == 0 or completed == len(configs):
                    elapsed = time.time() - start_time
                    rate = completed / elapsed
                    remaining = (len(configs) - completed) / rate if rate > 0 else 0
                    pct = completed / len(configs) * 100
                    bar_len = 30
                    filled = int(bar_len * completed / len(configs))
                    bar = '#' * filled + '-' * (bar_len - filled)
                    print(f"\rProgress: [{bar}] {pct:.0f}% ({completed}/{len(configs)}) "
                          f"- ETA: {remaining/60:.1f}m", end='', flush=True)

    print()  # Newline after progress
    elapsed = time.time() - start_time
    print(f"Completed in {elapsed/60:.1f} minutes ({elapsed/len(configs):.2f}s per config)")

    return results


# =============================================================================
# DATA LOADING
# =============================================================================

def load_data(btc_file=None, obs_file=None, res_file=None):
    """Load Binance and observer data."""
    print("Loading data...")

    # Load Binance - load ALL files if no specific file given
    if btc_file:
        btc_path = Path(btc_file)
        btc_df = pd.read_csv(btc_path)
        print(f"  Binance: {len(btc_df):,} rows ({btc_path.name})")
    else:
        btc_dir = Path("research/binance_hf")
        if not btc_dir.exists():
            btc_dir = Path("/Users/rananjaybika/polymarket-amm-bot/research/binance_hf")

        btc_dfs = []
        for f in sorted(btc_dir.glob("btc_prices_*.csv")):
            df = pd.read_csv(f)
            btc_dfs.append(df)
            print(f"  Binance: {len(df):,} rows ({f.name})")
        btc_df = pd.concat(btc_dfs, ignore_index=True)
        btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
        print(f"  Binance TOTAL: {len(btc_df):,} rows")

    # Load observer
    if obs_file:
        obs_df = pd.read_csv(obs_file, on_bad_lines='skip', low_memory=False)
        obs_dir = Path(obs_file).parent
        print(f"  Observer: {len(obs_df):,} rows ({Path(obs_file).name})")
    else:
        obs_dir = Path("research/observer")
        if not obs_dir.exists():
            obs_dir = Path("/Users/rananjaybika/polymarket-amm-bot/research/observer")

        obs_dfs = []
        for f in sorted(obs_dir.glob("grid_obs_*.csv")):
            df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)
        obs_df = pd.concat(obs_dfs, ignore_index=True)
        obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
        print(f"  Observer: {len(obs_df):,} rows")

    # Load resolutions
    if res_file:
        res_path = Path(res_file)
    else:
        res_path = obs_dir / "market_resolutions_verified.csv"
        if not res_path.exists():
            res_path = Path("/Users/rananjaybika/polymarket-amm-bot/research/observer/market_resolutions_verified.csv")
    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Find actual binance time ranges (may have gaps)
    btc_df = btc_df.sort_values('timestamp_ms')

    # Detect gaps > 5 minutes in binance data
    btc_df['time_diff'] = btc_df['timestamp_ms'].diff()
    gap_threshold = 5 * 60 * 1000  # 5 minutes
    gaps = btc_df[btc_df['time_diff'] > gap_threshold]

    if len(gaps) > 0:
        print(f"  WARNING: {len(gaps)} gaps detected in binance data")
        for _, gap in gaps.iterrows():
            gap_hours = gap['time_diff'] / 3600000
            print(f"    Gap of {gap_hours:.2f} hours at {gap['timestamp_ms']}")

    # Build list of valid time ranges (between gaps)
    valid_ranges = []
    range_start = btc_df['timestamp_ms'].iloc[0]
    for _, gap in gaps.iterrows():
        range_end = gap['timestamp_ms'] - gap['time_diff']
        valid_ranges.append((range_start, range_end))
        range_start = gap['timestamp_ms']
    valid_ranges.append((range_start, btc_df['timestamp_ms'].iloc[-1]))

    total_btc_hours = sum((e - s) / 3600000 for s, e in valid_ranges)
    print(f"  Binance coverage: {total_btc_hours:.2f} hours across {len(valid_ranges)} sessions")

    # Add resolutions
    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    # Filter markets that have actual binance coverage
    valid_slugs = []
    for slug, mdf in obs_df.groupby('market_slug'):
        max_time = mdf['time_remaining_secs'].max()
        min_time = mdf['time_remaining_secs'].min()
        duration = max_time - min_time

        if duration < MIN_RUNTIME_SECS or max_time < 840:
            continue

        # Check if market has binance data coverage
        m_start = mdf['timestamp_ms'].min()
        m_end = mdf['timestamp_ms'].max()

        has_coverage = False
        for r_start, r_end in valid_ranges:
            # Market must be mostly within a valid range
            overlap_start = max(m_start, r_start)
            overlap_end = min(m_end, r_end)
            if overlap_end > overlap_start:
                coverage = (overlap_end - overlap_start) / (m_end - m_start)
                if coverage > 0.8:  # At least 80% coverage
                    has_coverage = True
                    break

        if has_coverage:
            valid_slugs.append(slug)

    obs_df = obs_df[obs_df['market_slug'].isin(valid_slugs)]

    # Filter btc_df to valid ranges only
    btc_mask = pd.Series(False, index=btc_df.index)
    for r_start, r_end in valid_ranges:
        btc_mask |= (btc_df['timestamp_ms'] >= r_start) & (btc_df['timestamp_ms'] <= r_end)
    btc_df = btc_df[btc_mask].drop(columns=['time_diff'])

    # Recalculate hours based on valid market coverage
    if len(valid_slugs) > 0:
        hours = total_btc_hours
    else:
        hours = 0

    print(f"  Valid markets (with binance coverage): {len(valid_slugs)}")

    return btc_df, obs_df, hours, res_map


# =============================================================================
# OUTPUT FORMATTING
# =============================================================================

def print_results(results: List[OptResult], hours: float, n_markets: int):
    """Print comprehensive results summary."""
    print()
    print("=" * 100)
    print("ENHANCED SPIKE PARAMETER OPTIMIZATION RESULTS")
    print("=" * 100)
    print(f"\nData: {hours:.2f} hours, {n_markets} markets")
    print(f"Configurations tested: {len(results)}")

    # Sort by hourly rate
    results.sort(key=lambda x: x.hourly_rate, reverse=True)

    # Top 10 configurations
    print()
    print("=" * 100)
    print("TOP 10 CONFIGURATIONS (by $/hr)")
    print("=" * 100)
    print()
    print(f"{'Rank':<5} {'Total':<6} {'Lvls':<5} {'Spc':<6} {'Look':<8} {'SL':<6} "
          f"{'Pull':<5} {'$/hr':>8} {'Trades':>7} {'Acc%':>6} {'Win%':>6} {'Capital':>8}")
    print("-" * 100)

    for i, r in enumerate(results[:10], 1):
        sl_str = f"{r.config.stop_loss_pct*100:.0f}%" if r.config.stop_loss_pct else "None"
        pull_str = "ON" if r.config.order_pulling else "OFF"
        look_str = f"{r.config.lookback_ms}ms"
        cap_warn = " (!)" if r.capital_exceeded else ""

        print(f"{i:<5} {r.config.target_shares:<6} {r.config.grid_levels:<5} "
              f"{r.config.grid_spacing:<6.2f} {look_str:<8} {sl_str:<6} {pull_str:<5} "
              f"${r.hourly_rate:>7.2f} {r.total_trades:>7} {r.direction_accuracy*100:>5.1f}% "
              f"{r.win_rate*100:>5.1f}% ${r.config.estimated_cost:>6.2f}{cap_warn}")

    # Capital constraint analysis
    print()
    print("=" * 100)
    print("CAPITAL CONSTRAINT ANALYSIS ($170 limit)")
    print("=" * 100)

    exceeding = [r for r in results if r.capital_exceeded]
    print(f"\nConfigs exceeding $170 per trade: {len(exceeding)}/{len(results)}")

    if exceeding:
        # Group by config type
        exceed_configs = set((r.config.target_shares, r.config.grid_levels) for r in exceeding)
        for target_shares, grid_levels in sorted(exceed_configs):
            cost = target_shares * AVG_PAIR_COST
            per_level = target_shares // grid_levels
            print(f"  - target={target_shares} (split {per_level}×{grid_levels} levels): ~${cost:.2f}/trade")

    # Parameter sensitivity analysis
    print()
    print("=" * 100)
    print("PARAMETER SENSITIVITY ANALYSIS")
    print("=" * 100)

    # Target shares (total per trade)
    print("\nTarget Shares (total per trade):")
    for size in [5, 10, 15, 30]:
        matching = [r for r in results if r.config.target_shares == size]
        if matching:
            avg_rate = np.mean([r.hourly_rate for r in matching])
            print(f"  {size:>2} shares: ${avg_rate:.2f}/hr (avg)")

    # Grid levels
    print("\nGrid Levels:")
    for levels in [1, 2, 3]:
        matching = [r for r in results if r.config.grid_levels == levels]
        if matching:
            avg_rate = np.mean([r.hourly_rate for r in matching])
            print(f"  {levels} level(s): ${avg_rate:.2f}/hr (avg)")

    # Spike lookback
    print("\nSpike Lookback:")
    for lookback in [18, 30, 36, 60]:
        matching = [r for r in results if r.config.spike_lookback == lookback]
        if matching:
            avg_rate = np.mean([r.hourly_rate for r in matching])
            ms = lookback * 1000 // 60
            print(f"  {ms}ms ({lookback} ticks): ${avg_rate:.2f}/hr (avg)")

    # Stop loss
    print("\nStop Loss:")
    for sl in [0.07, 0.12, None]:
        matching = [r for r in results if r.config.stop_loss_pct == sl]
        if matching:
            avg_rate = np.mean([r.hourly_rate for r in matching])
            sl_str = f"{sl*100:.0f}%" if sl else "None"
            print(f"  {sl_str:>5}: ${avg_rate:.2f}/hr (avg)")

    # Order pulling
    print("\nOrder Pulling:")
    for pulling in [True, False]:
        matching = [r for r in results if r.config.order_pulling == pulling]
        if matching:
            avg_rate = np.mean([r.hourly_rate for r in matching])
            pull_str = "ON" if pulling else "OFF"
            print(f"  {pull_str:>4}: ${avg_rate:.2f}/hr (avg)")

    # Entry Pull Timeout (Path 1 experiment)
    unique_timeouts = sorted(set(r.config.entry_order_pull_timeout for r in results))
    if len(unique_timeouts) > 1:
        print("\nEntry Pull Timeout (Path 1):")
        for timeout in unique_timeouts:
            matching = [r for r in results if r.config.entry_order_pull_timeout == timeout]
            if matching:
                avg_rate = np.mean([r.hourly_rate for r in matching])
                print(f"  {timeout:.0f}s: ${avg_rate:.2f}/hr (avg)")

    # Hedge Ratio (Path 2 experiment)
    unique_ratios = sorted(set(r.config.hedge_ratio for r in results))
    if len(unique_ratios) > 1:
        print("\nHedge Ratio (Path 2):")
        for ratio in unique_ratios:
            matching = [r for r in results if r.config.hedge_ratio == ratio]
            if matching:
                avg_rate = np.mean([r.hourly_rate for r in matching])
                print(f"  {ratio:.0%}: ${avg_rate:.2f}/hr (avg)")

    # Aggressive Hedge Timeout (Path 2 experiment)
    unique_agg_timeouts = sorted(set(r.config.aggressive_hedge_timeout for r in results if r.config.aggressive_hedge_timeout is not None))
    has_none = any(r.config.aggressive_hedge_timeout is None for r in results)
    if unique_agg_timeouts or (has_none and len(unique_agg_timeouts) > 0):
        print("\nAggressive Hedge Timeout (Path 2):")
        if has_none:
            matching = [r for r in results if r.config.aggressive_hedge_timeout is None]
            if matching:
                avg_rate = np.mean([r.hourly_rate for r in matching])
                print(f"  None (disabled): ${avg_rate:.2f}/hr (avg)")
        for timeout in unique_agg_timeouts:
            matching = [r for r in results if r.config.aggressive_hedge_timeout == timeout]
            if matching:
                avg_rate = np.mean([r.hourly_rate for r in matching])
                print(f"  {timeout:.0f}s: ${avg_rate:.2f}/hr (avg)")

    # Best config details
    if results:
        print()
        print("=" * 100)
        print("BEST CONFIGURATION DETAILS")
        print("=" * 100)

        best = results[0]
        print(f"\nConfiguration:")
        print(f"  Target Shares:  {best.config.target_shares} (total per trade)")
        print(f"  Grid Levels:    {best.config.grid_levels} ({best.config.order_size_per_level} per level)")
        print(f"  Grid Spacing:   {best.config.grid_spacing:.2f}")
        print(f"  Spike Lookback: {best.config.lookback_ms}ms ({best.config.spike_lookback} ticks)")
        print(f"  Stop Loss:      {best.config.stop_loss_pct*100:.0f}%" if best.config.stop_loss_pct else "  Stop Loss:      None")
        print(f"  Order Pulling:  {'ON' if best.config.order_pulling else 'OFF'}")
        print(f"  Entry Timeout:  {best.config.entry_order_pull_timeout:.0f}s")
        print(f"  Hedge Ratio:    {best.config.hedge_ratio:.0%}")
        agg_timeout_str = f"{best.config.aggressive_hedge_timeout:.0f}s" if best.config.aggressive_hedge_timeout else "None"
        print(f"  Aggressive Hdg: {agg_timeout_str}")

        print(f"\nPerformance:")
        print(f"  Total PnL:      ${best.total_pnl:.2f}")
        print(f"  Hourly Rate:    ${best.hourly_rate:.2f}/hr")
        print(f"  Total Trades:   {best.total_trades}")
        print(f"  Win Rate:       {best.win_rate*100:.1f}%")
        print(f"  Direction Acc:  {best.direction_accuracy*100:.1f}%")

        print(f"\nHedge Breakdown:")
        print(f"  Passive:        {best.passive_hedge_pct*100:.1f}% (${best.passive_pnl:.2f})")
        print(f"  Stop-Loss:      {best.stoploss_hedge_pct*100:.1f}% (${best.stoploss_pnl:.2f})")
        print(f"  Aggressive:     {best.aggressive_hedge_pct*100:.1f}% (${best.aggressive_pnl:.2f})")
        print(f"  Resolution:     {best.resolution_pct*100:.1f}% (${best.resolution_pnl:.2f})")
        if best.pulled_pct > 0:
            print(f"  Pulled:         {best.pulled_pct*100:.1f}%")

        print(f"\nCapital:")
        print(f"  Est. per trade: ${best.config.estimated_cost:.2f}")
        print(f"  Exceeds $170:   {'Yes' if best.capital_exceeded else 'No'}")


def save_results(results: List[OptResult], output_path: str):
    """Save results to CSV file."""
    rows = [r.to_dict() for r in results]
    df = pd.DataFrame(rows)
    df = df.sort_values('hourly_rate', ascending=False)
    df.to_csv(output_path, index=False)
    print(f"\nResults saved to: {output_path}")


# =============================================================================
# MAIN
# =============================================================================

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Enhanced Spike Parameter Optimization Grid Search",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument("--quick", action="store_true",
                        help="Run quick test with reduced parameter space")
    parser.add_argument("--workers", type=int, default=4,
                        help="Number of parallel workers")
    parser.add_argument("--output", type=str, default=None,
                        help="Output CSV file path")
    parser.add_argument("--btc-file", type=str, default=None,
                        help="Path to Binance BTC price CSV file")
    parser.add_argument("--obs-file", type=str, default=None,
                        help="Path to observer CSV file")
    parser.add_argument("--res-file", type=str, default=None,
                        help="Path to market resolutions CSV file")
    parser.add_argument("--path", type=str, default="both",
                        choices=["path1", "path2", "both"],
                        help="Experiment path: path1=entry pulling (800-1200ms), "
                             "path2=partial hedge + aggressive hedge (ALL lookbacks), both=full grid")

    return parser.parse_args()


def main():
    args = parse_args()

    print("=" * 100)
    print("ENHANCED SPIKE PARAMETER OPTIMIZATION")
    print("=" * 100)
    print()

    # Load data
    btc_df, obs_df, hours, res_map = load_data(
        btc_file=args.btc_file,
        obs_file=args.obs_file,
        res_file=args.res_file
    )

    n_markets = obs_df['market_slug'].nunique()

    # Print configuration info
    configs = generate_param_grid(quick=args.quick, path=args.path)
    path_desc = {
        "path1": "PATH 1: Entry Pulling (800ms, 1000ms, 1200ms lookbacks)",
        "path2": "PATH 2: Partial Hedge + Aggressive Hedge (ALL lookbacks)",
        "both": "Full Grid (all lookbacks)"
    }
    print(f"\nOptimization Settings:")
    print(f"  Mode:         {'Quick (reduced grid)' if args.quick else path_desc[args.path]}")
    print(f"  Workers:      {args.workers}")
    print(f"  Configs:      {len(configs)}")
    print(f"  Signal:       enhanced (fixed)")
    print(f"  Adaptive Vol: ON (fixed)")
    print(f"  Min Time:     {MIN_TIME}s (fixed)")
    print(f"  Cycling:      ON (fixed)")
    if args.path == "path1":
        print(f"  Lookbacks:    Testing 800ms, 1000ms, 1200ms")
        print(f"  Entry Pull:   Testing 3s, 5s, 7s, 10s, 15s, 20s, 25s, 30s timeouts")
        print(f"  Order Pull:   Testing ON and OFF")
    elif args.path == "path2":
        print(f"  Lookbacks:    Testing ALL (300ms, 500ms, 600ms, 800ms, 1000ms, 1200ms)")
        print(f"  Stop Losses:  Testing 3%, 5%, 7%, 12%, None")
        print(f"  Hedge Ratio:  Testing 25%, 50%, 75%, 100%")
        print(f"  Aggressive:   Testing None, 5s, 10s, 15s timeouts")

    # Run optimization
    print("\n" + "-" * 100)
    print("Running optimization...")
    results = run_optimization(btc_df, obs_df, hours, res_map,
                              n_workers=args.workers, quick=args.quick,
                              path=args.path)

    # Print results
    print_results(results, hours, n_markets)

    # Save to CSV if requested
    if args.output:
        save_results(results, args.output)

    print()
    print("=" * 100)


if __name__ == "__main__":
    main()
