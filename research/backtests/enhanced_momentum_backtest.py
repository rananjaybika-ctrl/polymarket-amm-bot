#!/usr/bin/env python3
"""
Enhanced Momentum Strategy Backtest with Partial Hedging

Improves on the baseline velocity strategy ($20.46/hr) with:
1. Higher-order derivatives (acceleration, jerk) for signal quality
2. Partial hedging innovation - hedge 50%, let 50% ride to resolution

Target: $25-35/hr (vs $20.46/hr baseline)

Key Innovation - Partial Hedging:
    Instead of hedging 100% at loser_offset, split into tranches:
    - T1 (Safe): 50% hedged at loser_offset (guaranteed profit)
    - T2 (Ride): 50% rides to resolution (potential 2x if correct)

Usage:
    python research/enhanced_momentum_backtest.py
    python research/enhanced_momentum_backtest.py --hedge-ratio 0.50
    python research/enhanced_momentum_backtest.py --test-all-ratios
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
from datetime import datetime, timezone
from collections import defaultdict
import argparse


# =============================================================================
# CONFIGURATION
# =============================================================================

STARTING_BALANCE = 170.0
TARGET_SHARES = 15
MIN_TIME = 60  # Entry cutoff (seconds remaining)

# Polymarket order restrictions
MIN_ORDER_QTY = 5      # Minimum 5 shares per order
MIN_ORDER_VALUE = 1.0  # Minimum $1 per order


def validate_order(shares: int, price: float) -> bool:
    """Validate order meets Polymarket restrictions."""
    if shares < MIN_ORDER_QTY:
        return False
    if shares * price < MIN_ORDER_VALUE:
        return False
    return True

# Market filtering
MIN_RUNTIME_SECS = 300  # 5 minutes minimum
REQUIRE_STANDARD_START = True

# Stop-loss (for T2 tranche only - T1 always hedges passively)
# Set to None to disable stop-loss and let T2 ride to resolution
T2_STOP_LOSS_PCT = None  # Disabled - let T2 ride to resolution (was 0.20)

# Velocity parameters (baseline)
VELOCITY_THRESHOLD = 0.50  # Zone 5-6
VELOCITY_LOSER_OFFSET = 0.12

# Signal quality thresholds
MIN_SIGNAL_QUALITY = 0.40  # Minimum quality to enter

# Hedge ratio test grid
HEDGE_RATIO_OPTIONS = [0.25, 0.50, 0.75, 1.00]

# Spike detection - CANONICAL from TRADING_CONFIGS.py (Jan 27, 2026)
SPIKE_LOOKBACK = 72  # 72 ticks = 1200ms at 60Hz (CANONICAL)
SPIKE_THRESHOLD = 0.02
DROP_MULTIPLIER = 0.68
DROP_INTERCEPT = 0.01

# Cycling
MIN_CYCLE_GAP_SAMPLES = 5


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class TrancheResult:
    """Result of a single tranche (T1 or T2)."""
    tranche: str  # "T1" or "T2"
    shares: int
    entry_price: float
    hedge_type: str  # "passive", "stoploss", "resolution"
    hedge_price: float
    pair_cost: float
    pnl: float
    prediction_correct: bool


@dataclass
class EnhancedTradeResult:
    """Result of an enhanced momentum trade with partial hedging."""
    strategy: str = "enhanced_momentum"
    cycle_num: int = 0
    market_slug: str = ""
    entry_time_remaining: float = 0.0
    winner_side: str = ""
    resolution: str = ""
    prediction_correct: bool = False

    # Entry
    winner_fill_price: float = 0.0
    total_shares: int = 0

    # Tranches
    t1_shares: int = 0
    t1_hedge_type: str = ""
    t1_hedge_price: float = 0.0
    t1_pair_cost: float = 0.0
    t1_pnl: float = 0.0

    t2_shares: int = 0
    t2_hedge_type: str = ""  # "passive", "stoploss", "resolution_win", "resolution_loss"
    t2_hedge_price: float = 0.0
    t2_pair_cost: float = 0.0
    t2_pnl: float = 0.0

    # Signal quality
    signal_quality: float = 0.0
    velocity_bps: float = 0.0
    accel_aligned: bool = False

    # Totals
    total_pnl: float = 0.0
    samples_to_t1_hedge: int = 0
    samples_to_t2_outcome: int = 0


@dataclass
class BalanceState:
    """Track balance throughout simulation."""
    starting_balance: float
    current_balance: float
    peak_balance: float
    max_drawdown: float
    trades_executed: int
    trades_skipped_insufficient_funds: int

    def can_afford(self, cost: float) -> bool:
        return self.current_balance >= cost

    def execute_trade(self, cost: float, pnl: float):
        self.current_balance += pnl
        self.trades_executed += 1
        if self.current_balance > self.peak_balance:
            self.peak_balance = self.current_balance
        drawdown = self.peak_balance - self.current_balance
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown

    def skip_trade(self):
        self.trades_skipped_insufficient_funds += 1


# =============================================================================
# RESOLUTION CACHE
# =============================================================================

_RESOLUTION_CACHE: Dict[str, str] = {}
_RESOLUTION_STATS = {"known": 0, "guessed": 0, "skipped": 0}

# Set to True to REQUIRE known resolutions (no guessing)
# WARNING: If True and resolution cache doesn't overlap with data, NO trades will run!
REQUIRE_KNOWN_RESOLUTION = False  # Allow guessed resolutions but track them


def load_resolution_cache():
    """Load actual market resolutions."""
    global _RESOLUTION_CACHE
    resolution_file = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer/market_resolutions.csv')

    if resolution_file.exists():
        try:
            df = pd.read_csv(resolution_file)
            for _, row in df.iterrows():
                slug = row['market']
                winner = row['winner']
                if winner in ('UP', 'DOWN'):
                    _RESOLUTION_CACHE[slug] = winner
            print(f"  Loaded {len(_RESOLUTION_CACHE)} VERIFIED resolutions")
            print(f"  WARNING: Only markets with verified resolutions will be used!")
        except Exception as e:
            print(f"  Warning: Could not load resolutions: {e}")


def get_resolution(mdf: pd.DataFrame, slug: str = "") -> Optional[str]:
    """
    Get market resolution.

    Returns None if resolution is unknown and REQUIRE_KNOWN_RESOLUTION is True.
    This prevents inflated accuracy metrics from guessed resolutions.

    GUESSING METHOD: Uses final observed prices (at ~60s before market end)
    to infer probable winner. This is UNRELIABLE because:
    - Market can move significantly in final 60 seconds
    - Price >= 0.90 is reasonably confident but not guaranteed
    - Price < 0.90 is a pure guess
    """
    global _RESOLUTION_STATS

    # Check cache first (verified resolutions)
    if slug and slug in _RESOLUTION_CACHE:
        _RESOLUTION_STATS["known"] += 1
        return _RESOLUTION_CACHE[slug]

    # If we require known resolutions, return None for unknown markets
    if REQUIRE_KNOWN_RESOLUTION:
        _RESOLUTION_STATS["skipped"] += 1
        return None

    # Fallback: guess from final prices (UNRELIABLE!)
    final = mdf.iloc[-1]

    # High-confidence guess (price >= 0.90)
    if final['up_bid'] >= 0.90:
        _RESOLUTION_STATS["guessed"] += 1
        return 'UP'
    elif final['down_bid'] >= 0.90:
        _RESOLUTION_STATS["guessed"] += 1
        return 'DOWN'
    else:
        # Low-confidence guess - just use higher bid
        _RESOLUTION_STATS["guessed"] += 1
        return 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'


def print_resolution_stats():
    """Print resolution statistics with warnings."""
    total = _RESOLUTION_STATS['known'] + _RESOLUTION_STATS['guessed']
    guessed_pct = _RESOLUTION_STATS['guessed'] / total * 100 if total > 0 else 0

    print(f"\n  Resolution stats:")
    print(f"    Known (verified): {_RESOLUTION_STATS['known']}")
    print(f"    Guessed (unreliable): {_RESOLUTION_STATS['guessed']}")
    print(f"    Skipped (no resolution): {_RESOLUTION_STATS['skipped']}")

    if guessed_pct > 50:
        print(f"\n  ⚠️  WARNING: {guessed_pct:.0f}% of resolutions are GUESSED!")
        print(f"      Accuracy metrics may be UNRELIABLE.")
        print(f"      Guessing uses final observed prices (~60s before market end).")
        print(f"      Markets can move significantly in final 60 seconds.")


# =============================================================================
# MARKET FILTERING
# =============================================================================

def is_valid_market(mdf: pd.DataFrame, slug: str) -> Tuple[bool, str]:
    """Validate market completeness."""
    if len(mdf) < 25:
        return False, "too_few_samples"

    first = mdf.iloc[0]['time_remaining_secs']
    last = mdf.iloc[-1]['time_remaining_secs']

    runtime = first - last
    if runtime < MIN_RUNTIME_SECS:
        return False, "runtime_under_5min"

    if REQUIRE_STANDARD_START:
        try:
            timestamp = int(slug.split('-')[-1])
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            if dt.minute % 15 != 0:
                return False, "irregular_start_time"
        except:
            pass

    if first < 800 or last > 60:
        return False, "incomplete_observation"

    return True, "valid"


# =============================================================================
# SIGNAL QUALITY CALCULATION
# =============================================================================

def calculate_signal_quality(row: pd.Series) -> float:
    """
    Calculate signal quality score (0-1) based on multiple factors.

    Components:
    - Velocity magnitude (30%)
    - Acceleration alignment (25%)
    - Spike confirmation (25%)
    - Duration in zone (20%)
    """
    quality = 0.0

    # Velocity magnitude (30%)
    vel = abs(row.get('velocity_bps', 0))
    vel_component = min(vel / 1.0, 1.0) * 0.30
    quality += vel_component

    # Acceleration alignment (25%)
    accel_aligned = row.get('accel_aligned', False)
    if accel_aligned:
        quality += 0.25

    # Spike confirmation (25%)
    spike_detected = row.get('spike_detected', False)
    if spike_detected:
        # Check if spike direction matches velocity direction
        spike_dir = row.get('spike_direction', None)
        vel_dir = "UP" if row.get('velocity_bps', 0) > 0 else "DOWN"
        if spike_dir == vel_dir:
            quality += 0.25
        elif spike_detected:
            quality += 0.10  # Spike in wrong direction = partial credit

    # Duration component (20%) - use streak if available
    streak = row.get('vel_direction_streak', 1)
    duration_component = min(streak / 20, 1.0) * 0.20
    quality += duration_component

    return quality


def get_dynamic_hedge_ratio(signal_quality: float, time_remaining: float) -> float:
    """
    Calculate dynamic hedge ratio based on signal quality and time.

    Higher quality = hedge less, let more ride
    Less time = hedge more (reduce risk)
    """
    base = 0.50

    # High quality = hedge less (let more ride to resolution)
    quality_adj = (0.50 - signal_quality) * 0.30

    # Less time = hedge more
    time_adj = 0.15 if time_remaining < 300 else 0

    ratio = base + quality_adj + time_adj
    return max(0.25, min(0.75, ratio))


# =============================================================================
# ENHANCED MOMENTUM SIMULATION
# =============================================================================

def simulate_enhanced_momentum_market(
    mdf: pd.DataFrame,
    slug: str,
    hedge_ratio: float,
    use_dynamic_ratio: bool = False,
    balance_state: Optional[BalanceState] = None,
) -> Optional[List[EnhancedTradeResult]]:
    """
    Simulate enhanced momentum strategy with partial hedging.

    Args:
        mdf: Market dataframe
        slug: Market slug
        hedge_ratio: Fraction of position to hedge (T1), rest rides (T2)
        use_dynamic_ratio: Use signal quality to adjust ratio
        balance_state: Balance tracking

    Returns:
        List of trade results or None
    """
    mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)

    # Add spike columns if not present
    if 'spike_detected' not in mdf.columns:
        mdf = add_spike_columns(mdf)

    # Add derived columns if not present
    if 'signal_quality' not in mdf.columns:
        mdf['signal_quality'] = mdf.apply(calculate_signal_quality, axis=1)

    resolution = get_resolution(mdf, slug)

    # CRITICAL: Skip markets without verified resolution
    # This prevents inflated accuracy from guessed resolutions
    if resolution is None:
        return None

    trades = []
    cycle_num = 0

    i = 0
    in_trade = False

    while i < len(mdf):
        row = mdf.iloc[i]
        time_rem = row['time_remaining_secs']
        vel = row['velocity_bps']

        if time_rem < MIN_TIME:
            break

        if not in_trade:
            # Entry condition: strong velocity (zone 5-6)
            if abs(vel) < VELOCITY_THRESHOLD:
                i += 1
                continue

            # Calculate signal quality
            signal_quality = row.get('signal_quality', calculate_signal_quality(row))

            # Quality gate
            if signal_quality < MIN_SIGNAL_QUALITY:
                i += 1
                continue

            # Determine winner side
            winner_side = "UP" if vel > 0 else "DOWN"

            # Winner fills at ASK
            if winner_side == "UP":
                winner_fill_price = row['up_ask']
                loser_ask = row['down_ask']
                loser_bid = row['down_bid']
            else:
                winner_fill_price = row['down_ask']
                loser_ask = row['up_ask']
                loser_bid = row['up_bid']

            # Check balance
            trade_cost = winner_fill_price * TARGET_SHARES
            if balance_state and not balance_state.can_afford(trade_cost):
                balance_state.skip_trade()
                i += 1
                continue

            in_trade = True
            cycle_num += 1
            entry_time = time_rem
            entry_velocity = vel
            entry_signal_quality = signal_quality
            entry_accel_aligned = row.get('accel_aligned', False)

            # Calculate hedge ratio (static or dynamic)
            if use_dynamic_ratio:
                effective_ratio = get_dynamic_hedge_ratio(signal_quality, time_rem)
            else:
                effective_ratio = hedge_ratio

            # Split into tranches
            t1_shares = int(TARGET_SHARES * effective_ratio)
            t2_shares = TARGET_SHARES - t1_shares

            # Ensure minimum viable sizes per Polymarket restrictions
            # T1 must be >= MIN_ORDER_QTY (5 shares) for hedge order
            # T2 must be >= MIN_ORDER_QTY (5 shares) or zero
            if t1_shares < MIN_ORDER_QTY:
                t1_shares = MIN_ORDER_QTY
                t2_shares = TARGET_SHARES - t1_shares
            if t2_shares > 0 and t2_shares < MIN_ORDER_QTY:
                # T2 too small - give all to T1
                t2_shares = 0
                t1_shares = TARGET_SHARES

            # Calculate T1 loser target (standard hedge)
            loser_target_bid = loser_bid - VELOCITY_LOSER_OFFSET
            loser_target_bid = max(0.01, min(0.95, loser_target_bid))

            # Scan forward for outcomes
            t1_filled = False
            t1_fill_price = 0.0
            t1_hedge_type = "unhedged"
            samples_to_t1 = 0

            t2_outcome = "riding"  # Will become: "passive", "stoploss", "resolution_win", "resolution_loss"
            t2_fill_price = 0.0
            samples_to_t2 = 0

            j = i + 1
            end_j = j  # Track where we exit
            reached_data_end = False

            while j < len(mdf):
                check_row = mdf.iloc[j]
                check_time = check_row['time_remaining_secs']

                if winner_side == "UP":
                    loser_ask_now = check_row['down_ask']
                    winner_bid_now = check_row['up_bid']
                else:
                    loser_ask_now = check_row['up_ask']
                    winner_bid_now = check_row['down_bid']

                # T1: Check passive fill for hedged tranche
                if not t1_filled and t1_shares > 0:
                    if loser_ask_now <= loser_target_bid:
                        t1_filled = True
                        t1_fill_price = loser_target_bid
                        t1_hedge_type = "passive"
                        samples_to_t1 = j - i

                # T2: Check stop-loss for riding tranche (only if T2 has shares and stop-loss enabled)
                if t2_outcome == "riding" and t2_shares > 0 and T2_STOP_LOSS_PCT is not None:
                    drop_pct = (winner_fill_price - winner_bid_now) / winner_fill_price
                    if drop_pct >= T2_STOP_LOSS_PCT:
                        t2_outcome = "stoploss"
                        t2_fill_price = loser_ask_now
                        samples_to_t2 = j - i

                # Check for market end (resolution)
                # Note: Data often only goes to ~60s remaining, so also check for end of data
                is_market_end = check_time <= 10 or j >= len(mdf) - 1

                if is_market_end:
                    # Market ending - resolve T2 if still riding
                    if t2_outcome == "riding" and t2_shares > 0:
                        prediction_correct_check = (winner_side == resolution)
                        if prediction_correct_check:
                            t2_outcome = "resolution_win"
                            t2_fill_price = 0.0  # Loser worthless, winner = $1
                        else:
                            t2_outcome = "resolution_loss"
                            t2_fill_price = 1.0  # Winner worthless, loser = $1
                        samples_to_t2 = j - i

                    # T1: If still not filled at market end, hedge at current ask
                    if not t1_filled and t1_shares > 0:
                        t1_filled = True
                        t1_fill_price = loser_ask_now
                        t1_hedge_type = "forced"
                        samples_to_t1 = j - i

                    end_j = j
                    break

                # If both T1 filled and T2 resolved, we can exit early
                if t1_filled and t2_outcome != "riding":
                    end_j = j
                    break

                j += 1
                end_j = j

            # Handle case where we reached end of data without resolution
            # This happens because data ends at ~60s, not at market end
            if t2_outcome == "riding" and t2_shares > 0:
                # Use known resolution to determine T2 outcome
                prediction_correct_check = (winner_side == resolution)
                if prediction_correct_check:
                    t2_outcome = "resolution_win"
                    t2_fill_price = 0.0  # Winner pays $1
                else:
                    t2_outcome = "resolution_loss"
                    t2_fill_price = 1.0  # Winner worthless
                samples_to_t2 = end_j - i

            # Handle T1 if still not filled
            if not t1_filled and t1_shares > 0:
                # Get last available prices
                last_row = mdf.iloc[min(end_j, len(mdf) - 1)]
                if winner_side == "UP":
                    loser_ask_final = last_row['down_ask']
                else:
                    loser_ask_final = last_row['up_ask']
                t1_filled = True
                t1_fill_price = loser_ask_final
                t1_hedge_type = "forced"
                samples_to_t1 = end_j - i

            # Calculate PnL for each tranche
            prediction_correct = (winner_side == resolution)

            # T1 PnL (hedged)
            if t1_shares > 0 and t1_filled:
                t1_pair_cost = winner_fill_price + t1_fill_price
                t1_pnl = (1.0 - t1_pair_cost) * t1_shares
            else:
                t1_pair_cost = winner_fill_price
                t1_pnl = 0.0

            # T2 PnL (riding to resolution or stopped out)
            if t2_shares > 0:
                if t2_outcome == "resolution_win":
                    t2_pair_cost = winner_fill_price  # Only paid for winner
                    t2_pnl = (1.0 - winner_fill_price) * t2_shares  # Winner pays $1
                elif t2_outcome == "resolution_loss":
                    t2_pair_cost = winner_fill_price
                    t2_pnl = (0.0 - winner_fill_price) * t2_shares  # Winner worthless
                elif t2_outcome == "stoploss":
                    t2_pair_cost = winner_fill_price + t2_fill_price
                    t2_pnl = (1.0 - t2_pair_cost) * t2_shares
                elif t2_outcome == "passive":
                    # T2 also got passive fill (same as T1)
                    t2_pair_cost = winner_fill_price + t2_fill_price
                    t2_pnl = (1.0 - t2_pair_cost) * t2_shares
                else:
                    t2_pair_cost = winner_fill_price
                    t2_pnl = 0.0
            else:
                t2_pair_cost = 0.0
                t2_pnl = 0.0

            total_pnl = t1_pnl + t2_pnl

            # Update balance
            if balance_state:
                balance_state.execute_trade(trade_cost, total_pnl)

            trades.append(EnhancedTradeResult(
                strategy="enhanced_momentum",
                cycle_num=cycle_num,
                market_slug=slug,
                entry_time_remaining=entry_time,
                winner_side=winner_side,
                resolution=resolution,
                prediction_correct=prediction_correct,
                winner_fill_price=winner_fill_price,
                total_shares=TARGET_SHARES,
                t1_shares=t1_shares,
                t1_hedge_type=t1_hedge_type,
                t1_hedge_price=t1_fill_price,
                t1_pair_cost=t1_pair_cost,
                t1_pnl=t1_pnl,
                t2_shares=t2_shares,
                t2_hedge_type=t2_outcome,
                t2_hedge_price=t2_fill_price,
                t2_pair_cost=t2_pair_cost,
                t2_pnl=t2_pnl,
                signal_quality=entry_signal_quality,
                velocity_bps=entry_velocity,
                accel_aligned=entry_accel_aligned,
                total_pnl=total_pnl,
                samples_to_t1_hedge=samples_to_t1,
                samples_to_t2_outcome=samples_to_t2,
            ))

            in_trade = False

            # Advance past this trade
            i = end_j + MIN_CYCLE_GAP_SAMPLES

        i += 1

    return trades if trades else None


def add_spike_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add spike detection columns."""
    df = df.copy()

    if 'binance_price' not in df.columns:
        df['spike_detected'] = False
        df['spike_direction'] = None
        df['spike_magnitude'] = 0.0
        return df

    df['price_change_3tick'] = df['binance_price'].pct_change(periods=SPIKE_LOOKBACK) * 100
    df['spike_magnitude'] = df['price_change_3tick'].abs()
    df['spike_detected'] = df['spike_magnitude'] >= SPIKE_THRESHOLD
    df['spike_direction'] = df['price_change_3tick'].apply(
        lambda x: 'UP' if x >= SPIKE_THRESHOLD else ('DOWN' if x <= -SPIKE_THRESHOLD else None)
    )

    return df


# =============================================================================
# DATA LOADING
# =============================================================================

def load_market_data() -> Tuple[Dict[str, pd.DataFrame], Dict]:
    """Load and filter market data."""
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('grid_obs_*.csv'))
    csv_files.extend(sorted(observer_dir.glob('spread_capture_obs_*.csv')))

    print(f"Loading data from {len(csv_files)} files...")

    all_markets = {}
    filter_stats = defaultdict(int)

    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath, on_bad_lines='skip', low_memory=False)
            if df.empty:
                continue

            for slug in df['market_slug'].unique():
                mdf = df[df['market_slug'] == slug]
                is_valid, reason = is_valid_market(mdf, slug)

                if is_valid:
                    if slug not in all_markets or len(mdf) > len(all_markets[slug]):
                        all_markets[slug] = mdf.copy()
                    filter_stats["valid"] += 1
                else:
                    filter_stats[reason] += 1

        except Exception as e:
            continue

    filter_stats["valid"] = len(all_markets)
    print(f"Unique valid markets: {len(all_markets)}")
    return all_markets, dict(filter_stats)


# =============================================================================
# ANALYSIS
# =============================================================================

def analyze_enhanced_results(trades: List[EnhancedTradeResult], total_hours: float) -> Dict:
    """Comprehensive analysis of enhanced momentum trades."""
    if not trades:
        return {"error": "No trades"}

    # Overall metrics
    total_pnl = sum(t.total_pnl for t in trades)
    hourly_rate = total_pnl / total_hours if total_hours > 0 else 0

    # Accuracy
    correct = [t for t in trades if t.prediction_correct]
    accuracy = len(correct) / len(trades) * 100

    # T1 analysis (hedged tranche)
    t1_total_pnl = sum(t.t1_pnl for t in trades)
    t1_passive = [t for t in trades if t.t1_hedge_type == "passive"]
    t1_forced = [t for t in trades if t.t1_hedge_type == "forced"]

    # T2 analysis (riding tranche)
    t2_total_pnl = sum(t.t2_pnl for t in trades)
    t2_resolution_win = [t for t in trades if t.t2_hedge_type == "resolution_win"]
    t2_resolution_loss = [t for t in trades if t.t2_hedge_type == "resolution_loss"]
    t2_stoploss = [t for t in trades if t.t2_hedge_type == "stoploss"]

    # Signal quality analysis
    high_quality = [t for t in trades if t.signal_quality >= 0.60]
    low_quality = [t for t in trades if t.signal_quality < 0.40]

    high_quality_accuracy = len([t for t in high_quality if t.prediction_correct]) / len(high_quality) * 100 if high_quality else 0

    return {
        "total_trades": len(trades),
        "total_pnl": total_pnl,
        "hourly_rate": hourly_rate,
        "accuracy": accuracy,
        # T1 breakdown
        "t1_total_pnl": t1_total_pnl,
        "t1_passive_count": len(t1_passive),
        "t1_forced_count": len(t1_forced),
        "t1_avg_pair_cost": np.mean([t.t1_pair_cost for t in trades if t.t1_shares > 0]),
        # T2 breakdown
        "t2_total_pnl": t2_total_pnl,
        "t2_resolution_win_count": len(t2_resolution_win),
        "t2_resolution_loss_count": len(t2_resolution_loss),
        "t2_stoploss_count": len(t2_stoploss),
        "t2_win_rate": len(t2_resolution_win) / (len(t2_resolution_win) + len(t2_resolution_loss)) * 100 if (t2_resolution_win or t2_resolution_loss) else 0,
        # Signal quality
        "high_quality_trades": len(high_quality),
        "high_quality_accuracy": high_quality_accuracy,
        "avg_signal_quality": np.mean([t.signal_quality for t in trades]),
    }


# =============================================================================
# MAIN REPORT
# =============================================================================

def print_report(
    all_markets: Dict,
    results_by_ratio: Dict[float, Dict],
    best_ratio: float,
):
    """Print comprehensive report."""
    total_hours = len(all_markets) * 15 / 60

    print("\n" + "=" * 80)
    print("ENHANCED MOMENTUM BACKTEST RESULTS")
    print("=" * 80)

    print(f"\nMarkets: {len(all_markets)}")
    print(f"Total hours: {total_hours:.1f}")
    print(f"T2 Stop-loss: {T2_STOP_LOSS_PCT:.0%}" if T2_STOP_LOSS_PCT else "T2 Stop-loss: Disabled (ride to resolution)")
    print(f"Min signal quality: {MIN_SIGNAL_QUALITY}")

    # Hedge ratio comparison
    print("\n" + "-" * 80)
    print("HEDGE RATIO COMPARISON")
    print("-" * 80)

    print(f"\n{'Ratio':>6} {'Trades':>7} {'Total $':>9} {'$/hr':>8} {'Acc%':>6} "
          f"{'T1 $':>8} {'T2 $':>8} {'T2 Win%':>8}")
    print("-" * 80)

    for ratio in sorted(results_by_ratio.keys()):
        r = results_by_ratio[ratio]
        if "error" in r:
            continue
        marker = " *" if ratio == best_ratio else "  "
        print(f"{marker}{ratio:>4.0%} {r['total_trades']:>7} ${r['total_pnl']:>8.2f} "
              f"${r['hourly_rate']:>7.2f} {r['accuracy']:>5.1f}% "
              f"${r['t1_total_pnl']:>7.2f} ${r['t2_total_pnl']:>7.2f} "
              f"{r['t2_win_rate']:>7.1f}%")

    # Best ratio details
    best = results_by_ratio[best_ratio]
    print(f"\n{'=' * 80}")
    print(f"OPTIMAL RATIO: {best_ratio:.0%} -> ${best['total_pnl']:.2f} (${best['hourly_rate']:.2f}/hr)")
    print(f"{'=' * 80}")

    print(f"\nT1 (Hedged) Breakdown:")
    print(f"  Total PnL: ${best['t1_total_pnl']:.2f}")
    print(f"  Passive fills: {best['t1_passive_count']}")
    print(f"  Forced fills: {best['t1_forced_count']}")
    print(f"  Avg pair cost: ${best['t1_avg_pair_cost']:.4f}")

    print(f"\nT2 (Riding) Breakdown:")
    print(f"  Total PnL: ${best['t2_total_pnl']:.2f}")
    print(f"  Resolution wins: {best['t2_resolution_win_count']}")
    print(f"  Resolution losses: {best['t2_resolution_loss_count']}")
    print(f"  Stop-losses: {best['t2_stoploss_count']}")
    print(f"  T2 Win Rate: {best['t2_win_rate']:.1f}%")

    print(f"\nSignal Quality Analysis:")
    print(f"  High quality (>=0.60): {best['high_quality_trades']} trades")
    print(f"  High quality accuracy: {best['high_quality_accuracy']:.1f}%")
    print(f"  Average quality: {best['avg_signal_quality']:.3f}")

    # Comparison with baseline
    print(f"\n{'=' * 80}")
    print("COMPARISON WITH BASELINE")
    print(f"{'=' * 80}")
    print(f"  Baseline (velocity 100% hedge): $20.46/hr")
    print(f"  Enhanced ({best_ratio:.0%} hedge):         ${best['hourly_rate']:.2f}/hr")
    improvement = (best['hourly_rate'] - 20.46) / 20.46 * 100
    print(f"  Improvement: {improvement:+.1f}%")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Enhanced Momentum Backtest")
    parser.add_argument('--hedge-ratio', type=float, default=0.50,
                        help='Hedge ratio (default: 0.50)')
    parser.add_argument('--test-all-ratios', action='store_true',
                        help='Test all hedge ratio options')
    parser.add_argument('--dynamic-ratio', action='store_true',
                        help='Use dynamic hedge ratio based on signal quality')
    args = parser.parse_args()

    print("=" * 80)
    print("ENHANCED MOMENTUM BACKTEST WITH PARTIAL HEDGING")
    print("=" * 80)

    # Load resolution data
    print("\nLoading resolution data...")
    load_resolution_cache()

    # Load market data
    all_markets, filter_stats = load_market_data()

    if not all_markets:
        print("No valid markets found!")
        return

    total_hours = len(all_markets) * 15 / 60
    print(f"Total hours: {total_hours:.1f}")

    # Test ratios
    ratios_to_test = HEDGE_RATIO_OPTIONS if args.test_all_ratios else [args.hedge_ratio]

    results_by_ratio = {}
    best_pnl = float('-inf')
    best_ratio = 0.50

    for ratio in ratios_to_test:
        print(f"\nTesting hedge ratio: {ratio:.0%}...")

        all_trades = []
        for slug, mdf in all_markets.items():
            trades = simulate_enhanced_momentum_market(
                mdf, slug, ratio, use_dynamic_ratio=args.dynamic_ratio
            )
            if trades:
                all_trades.extend(trades)

        analysis = analyze_enhanced_results(all_trades, total_hours)
        results_by_ratio[ratio] = analysis

        if "error" not in analysis and analysis.get('total_pnl', 0) > best_pnl:
            best_pnl = analysis['total_pnl']
            best_ratio = ratio

        print(f"  Trades: {analysis.get('total_trades', 0)}, "
              f"PnL: ${analysis.get('total_pnl', 0):.2f}, "
              f"$/hr: ${analysis.get('hourly_rate', 0):.2f}")

    # Print resolution stats
    print_resolution_stats()

    # Print report
    print_report(all_markets, results_by_ratio, best_ratio)


if __name__ == "__main__":
    main()
