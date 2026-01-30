#!/usr/bin/env python3
"""
Comprehensive Strategy Backtest - Velocity vs Spike with Full Analysis

Addresses user requirements:
1. Determine best stop-loss % (7% vs 12% vs others)
2. Analyze stop-loss fill prices - are we overpaying?
3. Backtest both velocity and spike with optimal stop-loss
4. Compare cycling ON vs OFF
5. Parameters: Starting balance $170, target shares 15
6. Exclude incomplete markets (< 5 min, irregular start times)

Usage:
    python research/comprehensive_strategy_backtest.py
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

# User requirements
STARTING_BALANCE = 170.0
TARGET_SHARES = 15
MIN_TIME = 60  # Entry cutoff (seconds remaining)

# Polymarket order restrictions
MIN_ORDER_QTY = 5      # Minimum 5 shares per order
MIN_ORDER_VALUE = 1.0  # Minimum $1 per order


def validate_order(shares: int, price: float) -> bool:
    """
    Validate order meets Polymarket restrictions.

    Returns True if order is valid, False otherwise.
    """
    if shares < MIN_ORDER_QTY:
        return False
    if shares * price < MIN_ORDER_VALUE:
        return False
    return True


def get_valid_order_size(price: float, target_shares: int = TARGET_SHARES) -> int:
    """
    Get valid order size that meets Polymarket restrictions.

    Returns target_shares if valid, or minimum valid size, or 0 if impossible.
    """
    if price <= 0:
        return 0

    # Check if target size is valid
    if target_shares >= MIN_ORDER_QTY and target_shares * price >= MIN_ORDER_VALUE:
        return target_shares

    # Calculate minimum shares to meet $1 minimum
    min_shares_for_value = int(np.ceil(MIN_ORDER_VALUE / price))

    # Take the larger of qty minimum and value minimum
    min_valid_shares = max(MIN_ORDER_QTY, min_shares_for_value)

    return min_valid_shares

# Market filtering
MIN_RUNTIME_SECS = 300  # 5 minutes minimum
REQUIRE_STANDARD_START = True  # 00/15/30/45 minute marks only

# Stop-loss grid to test
STOP_LOSS_OPTIONS = [0.03, 0.05, 0.07, 0.10, 0.12, 0.15]

# Strategy parameters
MIN_CYCLE_GAP_SAMPLES = 5  # ~1 second between cycles

# Spike detection - CANONICAL from TRADING_CONFIGS.py (Jan 27, 2026)
SPIKE_LOOKBACK = 72  # 72 ticks = 1200ms at 60Hz (CANONICAL)
SPIKE_THRESHOLD = 0.02
DROP_MULTIPLIER = 0.68
DROP_INTERCEPT = 0.01

# Velocity parameters
VELOCITY_THRESHOLD = 0.50  # Zone 5-6
VELOCITY_LOSER_OFFSET = 0.05
STOP_LOSS_PCT_DEFAULT = 0.07


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class TradeResult:
    """Result of a single trade cycle with fill analysis."""
    strategy: str              # "velocity" or "spike"
    cycle_num: int
    market_slug: str
    entry_time_remaining: float
    winner_side: str
    winner_fill_price: float
    loser_fill_price: float
    hedge_type: str            # "passive", "stoploss", "unhedged"
    pair_cost: float
    pnl: float
    resolution: str
    prediction_correct: bool
    samples_to_hedge: int

    # Stop-loss fill analysis (NEW)
    stoploss_target_bid: float = 0.0    # What we WANTED to fill at (passive)
    stoploss_actual_fill: float = 0.0   # What we ACTUALLY filled at (ask)
    stoploss_overpay: float = 0.0       # Per-share overpay (actual - target)
    stoploss_overpay_total: float = 0.0 # Total overpay (overpay * shares)

    # Signal-specific
    signal_strength: float = 0.0  # velocity_bps or spike_magnitude


@dataclass
class MarketResult:
    """Result from one market."""
    slug: str
    total_samples: int
    total_cycles: int
    cycles: List[TradeResult]
    total_pnl: float
    resolution: str


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
        """Check if we can afford a trade."""
        return self.current_balance >= cost

    def execute_trade(self, cost: float, pnl: float):
        """Execute a trade and update balance."""
        self.current_balance += pnl
        self.trades_executed += 1

        # Track peak and drawdown
        if self.current_balance > self.peak_balance:
            self.peak_balance = self.current_balance

        drawdown = self.peak_balance - self.current_balance
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown

    def skip_trade(self):
        """Record a skipped trade due to insufficient funds."""
        self.trades_skipped_insufficient_funds += 1


# =============================================================================
# MARKET FILTERING
# =============================================================================

def is_valid_market(mdf: pd.DataFrame, slug: str) -> Tuple[bool, str]:
    """
    Validate market completeness per user requirements.

    Filters:
    1. Minimum samples (data quality)
    2. Runtime >= 5 minutes
    3. Standard start time (00/15/30/45)
    4. Observed from near-start to near-end

    Returns (is_valid, reason) tuple.
    """
    # Check minimum samples
    if len(mdf) < 25:  # ~5 seconds at 5 samples/sec
        return False, "too_few_samples"

    first = mdf.iloc[0]['time_remaining_secs']
    last = mdf.iloc[-1]['time_remaining_secs']

    # Check runtime >= 5 minutes (300 seconds)
    runtime = first - last
    if runtime < MIN_RUNTIME_SECS:
        return False, "runtime_under_5min"

    # Check standard start time (00/15/30/45)
    if REQUIRE_STANDARD_START:
        try:
            # Extract timestamp from slug (e.g., btc-updown-15m-1768592700)
            timestamp = int(slug.split('-')[-1])
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            if dt.minute % 15 != 0:
                return False, "irregular_start_time"
        except:
            pass  # Non-standard slug format, allow it

    # Standard completeness check (observed most of market)
    if first < 800 or last > 60:
        return False, "incomplete_observation"

    return True, "valid"


# =============================================================================
# SPIKE DETECTION
# =============================================================================

def add_spike_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate spike detection from binance_price data."""
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


# Resolution cache from actual Polymarket data
_RESOLUTION_CACHE: Dict[str, str] = {}


def load_resolution_cache():
    """Load actual market resolutions from market_resolutions.csv."""
    global _RESOLUTION_CACHE
    resolution_file = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer/market_resolutions.csv')

    if resolution_file.exists():
        try:
            df = pd.read_csv(resolution_file)
            for _, row in df.iterrows():
                slug = row['market']
                winner = row['winner']
                # Only use clear resolutions (UP or DOWN)
                if winner in ('UP', 'DOWN'):
                    _RESOLUTION_CACHE[slug] = winner
            print(f"  Loaded {len(_RESOLUTION_CACHE)} resolutions from market_resolutions.csv")
        except Exception as e:
            print(f"  Warning: Could not load resolutions: {e}")


def get_resolution(mdf: pd.DataFrame, slug: str = "") -> str:
    """
    Determine market resolution - prefer actual Polymarket data over orderbook inference.

    Priority:
    1. Actual resolution from market_resolutions.csv (if available)
    2. Infer from final orderbook state (fallback)
    """
    # Check cache first (actual Polymarket resolution)
    if slug and slug in _RESOLUTION_CACHE:
        return _RESOLUTION_CACHE[slug]

    # Fallback: infer from final orderbook state
    final = mdf.iloc[-1]
    if final['up_bid'] >= 0.90:
        return 'UP'
    elif final['down_bid'] >= 0.90:
        return 'DOWN'
    else:
        return 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'


# =============================================================================
# STRATEGY SIMULATIONS
# =============================================================================

def simulate_velocity_market(
    mdf: pd.DataFrame,
    slug: str,
    stop_loss_pct: float,
    enable_cycling: bool = True,
    balance_state: Optional[BalanceState] = None,
) -> Optional[MarketResult]:
    """
    Simulate velocity-based strategy with stop-loss fill analysis.

    Key addition: Track target bid vs actual fill for stop-loss trades.
    """
    mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)
    resolution = get_resolution(mdf, slug)  # Use actual resolution if available
    cycles = []
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
            # Look for velocity signal (Zone 5-6)
            if abs(vel) >= VELOCITY_THRESHOLD:
                winner_side = "UP" if vel > 0 else "DOWN"

                # Winner fills at ASK
                if winner_side == "UP":
                    winner_fill_price = row['up_ask']
                    loser_ask = row['down_ask']
                    loser_bid = row['down_bid']  # For live-matching formula
                else:
                    winner_fill_price = row['down_ask']
                    loser_ask = row['up_ask']
                    loser_bid = row['up_bid']  # For live-matching formula

                # Check if we can afford this trade
                trade_cost = winner_fill_price * TARGET_SHARES
                if balance_state and not balance_state.can_afford(trade_cost):
                    balance_state.skip_trade()
                    i += 1
                    continue

                in_trade = True
                cycle_num += 1
                entry_time = time_rem

                # FIXED: Use loser_bid - offset (matching live code)
                # Live code: loser_bid = loser_bid_price - offset (spike_capture.py:937)
                loser_target_bid = loser_bid - VELOCITY_LOSER_OFFSET
                loser_target_bid = max(0.01, min(0.95, loser_target_bid))

                # Scan forward for hedge
                loser_filled = False
                loser_fill_price = 0.0
                hedge_type = "unhedged"
                samples_to_hedge = 0
                stoploss_target = loser_target_bid
                stoploss_actual = 0.0
                stoploss_overpay = 0.0

                for j in range(i + 1, len(mdf)):
                    check_row = mdf.iloc[j]
                    check_time = check_row['time_remaining_secs']

                    if check_time < 10:
                        break

                    if winner_side == "UP":
                        loser_ask_now = check_row['down_ask']
                        winner_bid_now = check_row['up_bid']
                    else:
                        loser_ask_now = check_row['up_ask']
                        winner_bid_now = check_row['down_bid']

                    # Check passive fill
                    if loser_ask_now <= loser_target_bid:
                        loser_filled = True
                        loser_fill_price = loser_target_bid  # Fill at our bid
                        hedge_type = "passive"
                        samples_to_hedge = j - i
                        i = j + MIN_CYCLE_GAP_SAMPLES if enable_cycling else len(mdf)
                        break

                    # Check stop-loss
                    if winner_fill_price > 0:
                        drop_pct = (winner_fill_price - winner_bid_now) / winner_fill_price
                        if drop_pct >= stop_loss_pct:
                            loser_filled = True
                            loser_fill_price = loser_ask_now  # Fill at ASK (market order)
                            hedge_type = "stoploss"
                            samples_to_hedge = j - i

                            # CRITICAL: Track overpay
                            stoploss_actual = loser_ask_now
                            stoploss_overpay = stoploss_actual - stoploss_target

                            i = j + MIN_CYCLE_GAP_SAMPLES if enable_cycling else len(mdf)
                            break

                # Calculate PnL
                prediction_correct = (winner_side == resolution)

                if loser_filled:
                    pair_cost = winner_fill_price + loser_fill_price
                    pnl = (1.0 - pair_cost) * TARGET_SHARES
                else:
                    # UNHEDGED: advance i to end of scan to prevent duplicate entries
                    # Bug fix: previously i only incremented by 1, allowing overlapping trades
                    i = j + MIN_CYCLE_GAP_SAMPLES if enable_cycling else len(mdf)
                    if prediction_correct:
                        pnl = (1.0 - winner_fill_price) * TARGET_SHARES
                    else:
                        pnl = (0.0 - winner_fill_price) * TARGET_SHARES
                    pair_cost = winner_fill_price

                # Update balance if tracking
                if balance_state:
                    balance_state.execute_trade(trade_cost, pnl)

                cycles.append(TradeResult(
                    strategy="velocity",
                    cycle_num=cycle_num,
                    market_slug=slug,
                    entry_time_remaining=entry_time,
                    winner_side=winner_side,
                    winner_fill_price=winner_fill_price,
                    loser_fill_price=loser_fill_price,
                    hedge_type=hedge_type,
                    pair_cost=pair_cost,
                    pnl=pnl,
                    resolution=resolution,
                    prediction_correct=prediction_correct,
                    samples_to_hedge=samples_to_hedge,
                    stoploss_target_bid=stoploss_target if hedge_type == "stoploss" else 0.0,
                    stoploss_actual_fill=stoploss_actual if hedge_type == "stoploss" else 0.0,
                    stoploss_overpay=stoploss_overpay if hedge_type == "stoploss" else 0.0,
                    stoploss_overpay_total=stoploss_overpay * TARGET_SHARES if hedge_type == "stoploss" else 0.0,
                    signal_strength=abs(vel),
                ))

                in_trade = False

                if not enable_cycling:
                    break

        i += 1

    if not cycles:
        return None

    return MarketResult(
        slug=slug,
        total_samples=len(mdf),
        total_cycles=len(cycles),
        cycles=cycles,
        total_pnl=sum(c.pnl for c in cycles),
        resolution=resolution,
    )


def simulate_spike_market(
    mdf: pd.DataFrame,
    slug: str,
    stop_loss_pct: float,
    enable_cycling: bool = True,
    balance_state: Optional[BalanceState] = None,
) -> Optional[MarketResult]:
    """
    Simulate spike capture strategy with stop-loss fill analysis.
    """
    mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)
    mdf = add_spike_columns(mdf)

    resolution = get_resolution(mdf, slug)  # Use actual resolution if available
    cycles = []
    cycle_num = 0

    i = 0
    in_trade = False

    while i < len(mdf):
        row = mdf.iloc[i]
        time_rem = row['time_remaining_secs']

        if time_rem < MIN_TIME:
            break

        if not in_trade:
            # Look for spike signal
            if row.get('spike_detected', False) and pd.notna(row.get('spike_direction')):
                winner_side = row['spike_direction']
                spike_mag = row['spike_magnitude']

                # Winner fills at ASK
                if winner_side == "UP":
                    winner_fill_price = row['up_ask']
                    loser_ask = row['down_ask']
                else:
                    winner_fill_price = row['down_ask']
                    loser_ask = row['up_ask']

                # Check if we can afford this trade
                trade_cost = winner_fill_price * TARGET_SHARES
                if balance_state and not balance_state.can_afford(trade_cost):
                    balance_state.skip_trade()
                    i += 1
                    continue

                in_trade = True
                cycle_num += 1
                entry_time = time_rem

                # Magnitude-based loser bid
                expected_drop = DROP_MULTIPLIER * spike_mag + DROP_INTERCEPT
                loser_target_bid = loser_ask - expected_drop
                loser_target_bid = max(0.01, min(0.95, loser_target_bid))

                # Scan forward for hedge
                loser_filled = False
                loser_fill_price = 0.0
                hedge_type = "unhedged"
                samples_to_hedge = 0
                stoploss_target = loser_target_bid
                stoploss_actual = 0.0
                stoploss_overpay = 0.0

                for j in range(i + 1, len(mdf)):
                    check_row = mdf.iloc[j]
                    check_time = check_row['time_remaining_secs']

                    if check_time < 10:
                        break

                    if winner_side == "UP":
                        loser_ask_now = check_row['down_ask']
                        winner_bid_now = check_row['up_bid']
                    else:
                        loser_ask_now = check_row['up_ask']
                        winner_bid_now = check_row['down_bid']

                    # Check passive fill
                    if loser_ask_now <= loser_target_bid:
                        loser_filled = True
                        loser_fill_price = loser_target_bid
                        hedge_type = "passive"
                        samples_to_hedge = j - i
                        i = j + MIN_CYCLE_GAP_SAMPLES if enable_cycling else len(mdf)
                        break

                    # Check stop-loss
                    if winner_fill_price > 0:
                        drop_pct = (winner_fill_price - winner_bid_now) / winner_fill_price
                        if drop_pct >= stop_loss_pct:
                            loser_filled = True
                            loser_fill_price = loser_ask_now
                            hedge_type = "stoploss"
                            samples_to_hedge = j - i

                            # Track overpay
                            stoploss_actual = loser_ask_now
                            stoploss_overpay = stoploss_actual - stoploss_target

                            i = j + MIN_CYCLE_GAP_SAMPLES if enable_cycling else len(mdf)
                            break

                # Calculate PnL
                prediction_correct = (winner_side == resolution)

                if loser_filled:
                    pair_cost = winner_fill_price + loser_fill_price
                    pnl = (1.0 - pair_cost) * TARGET_SHARES
                else:
                    # UNHEDGED: advance i to end of scan to prevent duplicate entries
                    i = j + MIN_CYCLE_GAP_SAMPLES if enable_cycling else len(mdf)
                    if prediction_correct:
                        pnl = (1.0 - winner_fill_price) * TARGET_SHARES
                    else:
                        pnl = (0.0 - winner_fill_price) * TARGET_SHARES
                    pair_cost = winner_fill_price

                # Update balance if tracking
                if balance_state:
                    balance_state.execute_trade(trade_cost, pnl)

                cycles.append(TradeResult(
                    strategy="spike",
                    cycle_num=cycle_num,
                    market_slug=slug,
                    entry_time_remaining=entry_time,
                    winner_side=winner_side,
                    winner_fill_price=winner_fill_price,
                    loser_fill_price=loser_fill_price,
                    hedge_type=hedge_type,
                    pair_cost=pair_cost,
                    pnl=pnl,
                    resolution=resolution,
                    prediction_correct=prediction_correct,
                    samples_to_hedge=samples_to_hedge,
                    stoploss_target_bid=stoploss_target if hedge_type == "stoploss" else 0.0,
                    stoploss_actual_fill=stoploss_actual if hedge_type == "stoploss" else 0.0,
                    stoploss_overpay=stoploss_overpay if hedge_type == "stoploss" else 0.0,
                    stoploss_overpay_total=stoploss_overpay * TARGET_SHARES if hedge_type == "stoploss" else 0.0,
                    signal_strength=spike_mag,
                ))

                in_trade = False

                if not enable_cycling:
                    break

        i += 1

    if not cycles:
        return None

    return MarketResult(
        slug=slug,
        total_samples=len(mdf),
        total_cycles=len(cycles),
        cycles=cycles,
        total_pnl=sum(c.pnl for c in cycles),
        resolution=resolution,
    )


# =============================================================================
# ANALYSIS FUNCTIONS
# =============================================================================

def analyze_stoploss_fills(trades: List[TradeResult]) -> Dict:
    """Analyze stop-loss fill quality."""
    stoploss_trades = [t for t in trades if t.hedge_type == "stoploss"]

    if not stoploss_trades:
        return {
            "count": 0,
            "target_avg": 0,
            "actual_avg": 0,
            "overpay_per_share": 0,
            "overpay_per_trade": 0,
            "total_overpay": 0,
        }

    targets = [t.stoploss_target_bid for t in stoploss_trades]
    actuals = [t.stoploss_actual_fill for t in stoploss_trades]
    overpays = [t.stoploss_overpay for t in stoploss_trades]
    overpays_total = [t.stoploss_overpay_total for t in stoploss_trades]

    return {
        "count": len(stoploss_trades),
        "target_avg": np.mean(targets),
        "actual_avg": np.mean(actuals),
        "overpay_per_share": np.mean(overpays),
        "overpay_per_trade": np.mean(overpays_total),
        "total_overpay": sum(overpays_total),
    }


def analyze_results(trades: List[TradeResult], total_hours: float) -> Dict:
    """Comprehensive trade analysis."""
    if not trades:
        return {"error": "No trades"}

    # Breakdown by hedge type
    passive = [t for t in trades if t.hedge_type == "passive"]
    stoploss = [t for t in trades if t.hedge_type == "stoploss"]
    unhedged = [t for t in trades if t.hedge_type == "unhedged"]

    # PnL by type
    passive_pnl = sum(t.pnl for t in passive)
    stoploss_pnl = sum(t.pnl for t in stoploss)
    unhedged_pnl = sum(t.pnl for t in unhedged)
    total_pnl = sum(t.pnl for t in trades)

    # Accuracy
    correct = [t for t in trades if t.prediction_correct]
    accuracy = len(correct) / len(trades) * 100 if trades else 0

    # Unhedged accuracy
    unhedged_correct = [t for t in unhedged if t.prediction_correct]
    unhedged_accuracy = len(unhedged_correct) / len(unhedged) * 100 if unhedged else 0

    # Stop-loss fill analysis
    sl_analysis = analyze_stoploss_fills(trades)

    return {
        "total_trades": len(trades),
        "total_pnl": total_pnl,
        "hourly_rate": total_pnl / total_hours if total_hours > 0 else 0,
        # By type
        "passive_count": len(passive),
        "passive_pct": len(passive) / len(trades) * 100 if trades else 0,
        "passive_pnl": passive_pnl,
        "stoploss_count": len(stoploss),
        "stoploss_pct": len(stoploss) / len(trades) * 100 if trades else 0,
        "stoploss_pnl": stoploss_pnl,
        "unhedged_count": len(unhedged),
        "unhedged_pct": len(unhedged) / len(trades) * 100 if trades else 0,
        "unhedged_pnl": unhedged_pnl,
        # Accuracy
        "overall_accuracy": accuracy,
        "unhedged_accuracy": unhedged_accuracy,
        # Stop-loss fill analysis
        "sl_target_avg": sl_analysis["target_avg"],
        "sl_actual_avg": sl_analysis["actual_avg"],
        "sl_overpay_per_share": sl_analysis["overpay_per_share"],
        "sl_overpay_per_trade": sl_analysis["overpay_per_trade"],
        "sl_total_overpay": sl_analysis["total_overpay"],
    }


# =============================================================================
# DATA LOADING
# =============================================================================

def load_market_data() -> Tuple[Dict[str, pd.DataFrame], Dict]:
    """Load and filter market data from observer CSVs."""
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    # Load both spread_capture_obs and grid_obs files
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))
    csv_files.extend(sorted(observer_dir.glob('grid_obs_*.csv')))

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

                # Apply filtering
                is_valid, reason = is_valid_market(mdf, slug)

                if is_valid:
                    # Keep most complete version
                    if slug not in all_markets or len(mdf) > len(all_markets[slug]):
                        all_markets[slug] = mdf.copy()
                    filter_stats["valid"] += 1
                else:
                    filter_stats[reason] += 1

        except Exception as e:
            continue

    # Deduplicate valid count
    filter_stats["valid"] = len(all_markets)

    print(f"Unique valid markets: {len(all_markets)}")
    return all_markets, dict(filter_stats)


# =============================================================================
# STOP-LOSS OPTIMIZATION
# =============================================================================

def optimize_stop_loss(all_markets: Dict, strategy: str) -> Tuple[float, Dict]:
    """Find optimal stop-loss percentage for a strategy."""
    total_hours = len(all_markets) * 15 / 60

    best_pnl = float('-inf')
    best_sl = 0.07
    results_by_sl = {}

    for sl_pct in STOP_LOSS_OPTIONS:
        all_trades = []

        for slug, mdf in all_markets.items():
            if strategy == "velocity":
                result = simulate_velocity_market(mdf, slug, sl_pct, enable_cycling=True)
            else:
                result = simulate_spike_market(mdf, slug, sl_pct, enable_cycling=True)

            if result:
                all_trades.extend(result.cycles)

        analysis = analyze_results(all_trades, total_hours)
        results_by_sl[sl_pct] = analysis

        if analysis.get('total_pnl', 0) > best_pnl:
            best_pnl = analysis['total_pnl']
            best_sl = sl_pct

    return best_sl, results_by_sl


# =============================================================================
# BALANCE SIMULATION
# =============================================================================

def run_balance_simulation(
    all_markets: Dict,
    strategy: str,
    stop_loss_pct: float,
    enable_cycling: bool = True,
) -> Tuple[BalanceState, List[TradeResult]]:
    """Run simulation with balance tracking."""
    balance = BalanceState(
        starting_balance=STARTING_BALANCE,
        current_balance=STARTING_BALANCE,
        peak_balance=STARTING_BALANCE,
        max_drawdown=0.0,
        trades_executed=0,
        trades_skipped_insufficient_funds=0,
    )

    all_trades = []

    # Sort markets by slug (chronological by timestamp)
    sorted_markets = sorted(all_markets.items(), key=lambda x: x[0])

    for slug, mdf in sorted_markets:
        if strategy == "velocity":
            result = simulate_velocity_market(
                mdf, slug, stop_loss_pct, enable_cycling, balance
            )
        else:
            result = simulate_spike_market(
                mdf, slug, stop_loss_pct, enable_cycling, balance
            )

        if result:
            all_trades.extend(result.cycles)

    return balance, all_trades


# =============================================================================
# MAIN REPORT
# =============================================================================

def print_report(
    all_markets: Dict,
    filter_stats: Dict,
    velocity_sl: float,
    velocity_results: Dict,
    spike_sl: float,
    spike_results: Dict,
):
    """Print comprehensive report."""
    total_hours = len(all_markets) * 15 / 60

    print("=" * 80)
    print("COMPREHENSIVE STRATEGY BACKTEST")
    print("=" * 80)

    # Market filtering
    print("\nMARKET FILTERING:")
    total_processed = sum(filter_stats.values())
    print(f"  Total markets processed: {total_processed}")
    for reason, count in sorted(filter_stats.items()):
        if reason != "valid":
            print(f"  Excluded ({reason}): {count}")
    print(f"  Valid markets: {filter_stats.get('valid', 0)}")
    print(f"  Total hours: {total_hours:.1f}")

    # Stop-loss optimization
    print("\n" + "=" * 80)
    print("STOP-LOSS OPTIMIZATION")
    print("=" * 80)

    for strategy, optimal_sl, results in [
        ("VELOCITY", velocity_sl, velocity_results),
        ("SPIKE", spike_sl, spike_results),
    ]:
        print(f"\n{strategy}:")
        print(f"  {'SL%':>5} {'Trades':>7} {'Passive':>8} {'StopLoss':>8} {'Unhedged':>8} "
              f"{'P_PnL':>9} {'SL_PnL':>9} {'U_PnL':>9} {'Total':>9} {'$/hr':>8}")
        print("  " + "-" * 95)

        for sl_pct in STOP_LOSS_OPTIONS:
            r = results[sl_pct]
            if "error" in r:
                continue
            marker = " *" if sl_pct == optimal_sl else "  "
            print(f"{marker}{sl_pct*100:>4.0f}% {r['total_trades']:>7} "
                  f"{r['passive_count']:>8} {r['stoploss_count']:>8} {r['unhedged_count']:>8} "
                  f"${r['passive_pnl']:>7.2f} ${r['stoploss_pnl']:>7.2f} ${r['unhedged_pnl']:>7.2f} "
                  f"${r['total_pnl']:>7.2f} ${r['hourly_rate']:>6.2f}")

        best = results[optimal_sl]
        print(f"\n  OPTIMAL: {optimal_sl*100:.0f}% -> ${best['total_pnl']:.2f} (${best['hourly_rate']:.2f}/hr)")

    # Stop-loss fill analysis
    print("\n" + "=" * 80)
    print("STOP-LOSS FILL ANALYSIS")
    print("=" * 80)

    for strategy, optimal_sl, results in [
        ("VELOCITY", velocity_sl, velocity_results),
        ("SPIKE", spike_sl, spike_results),
    ]:
        r = results[optimal_sl]
        print(f"\n{strategy} ({optimal_sl*100:.0f}% SL):")
        print(f"  Target bid avg: ${r['sl_target_avg']:.4f}")
        print(f"  Actual fill avg: ${r['sl_actual_avg']:.4f}")
        print(f"  Overpay per share: ${r['sl_overpay_per_share']:.4f}")
        print(f"  Overpay per trade ({TARGET_SHARES} shares): ${r['sl_overpay_per_trade']:.2f}")
        print(f"  Total overpay: ${r['sl_total_overpay']:.2f}")
        print(f"\n  Stop-loss fills at ASK (market order) - this is REALISTIC")
        print(f"  No additional slippage modeled beyond bid-ask spread")

    # Cycling comparison
    print("\n" + "=" * 80)
    print("CYCLING COMPARISON (Optimal Stop-Loss)")
    print("=" * 80)

    for strategy, optimal_sl in [("velocity", velocity_sl), ("spike", spike_sl)]:
        print(f"\n{strategy.upper()} ({optimal_sl*100:.0f}% SL):")

        # Cycling ON
        trades_on = []
        for slug, mdf in all_markets.items():
            if strategy == "velocity":
                result = simulate_velocity_market(mdf, slug, optimal_sl, enable_cycling=True)
            else:
                result = simulate_spike_market(mdf, slug, optimal_sl, enable_cycling=True)
            if result:
                trades_on.extend(result.cycles)

        # Cycling OFF
        trades_off = []
        for slug, mdf in all_markets.items():
            if strategy == "velocity":
                result = simulate_velocity_market(mdf, slug, optimal_sl, enable_cycling=False)
            else:
                result = simulate_spike_market(mdf, slug, optimal_sl, enable_cycling=False)
            if result:
                trades_off.extend(result.cycles)

        on_analysis = analyze_results(trades_on, total_hours)
        off_analysis = analyze_results(trades_off, total_hours)

        print(f"  Cycling ON:  {on_analysis['total_trades']:>5} trades, "
              f"${on_analysis['total_pnl']:>8.2f} (${on_analysis['hourly_rate']:.2f}/hr)")
        print(f"  Cycling OFF: {off_analysis['total_trades']:>5} trades, "
              f"${off_analysis['total_pnl']:>8.2f} (${off_analysis['hourly_rate']:.2f}/hr)")

        if off_analysis['total_trades'] > 0:
            multiplier = on_analysis['total_trades'] / off_analysis['total_trades']
            improvement = (on_analysis['total_pnl'] - off_analysis['total_pnl'])
            pct_improvement = improvement / abs(off_analysis['total_pnl']) * 100 if off_analysis['total_pnl'] != 0 else 0
            print(f"  Trade multiplier: {multiplier:.2f}x")
            print(f"  PnL improvement: ${improvement:.2f} ({pct_improvement:+.0f}%)")

    # Strategy comparison
    print("\n" + "=" * 80)
    print("STRATEGY COMPARISON (Optimal SL, Cycling ON)")
    print("=" * 80)

    v = velocity_results[velocity_sl]
    s = spike_results[spike_sl]

    print(f"\n  {'Metric':<25} {'Velocity':<18} {'Spike':<18} {'Winner':<10}")
    print(f"  {'-'*70}")
    print(f"  {'Stop-Loss %':<25} {velocity_sl*100:<18.0f}% {spike_sl*100:<18.0f}%")
    print(f"  {'Total Trades':<25} {v['total_trades']:<18} {s['total_trades']:<18} "
          f"{'Velocity' if v['total_trades'] > s['total_trades'] else 'Spike':<10}")
    print(f"  {'Total PnL':<25} ${v['total_pnl']:<17.2f} ${s['total_pnl']:<17.2f} "
          f"{'Velocity' if v['total_pnl'] > s['total_pnl'] else 'Spike':<10}")
    print(f"  {'Hourly Rate':<25} ${v['hourly_rate']:<17.2f} ${s['hourly_rate']:<17.2f} "
          f"{'Velocity' if v['hourly_rate'] > s['hourly_rate'] else 'Spike':<10}")
    print(f"  {'Passive %':<25} {v['passive_pct']:<17.1f}% {s['passive_pct']:<17.1f}% "
          f"{'Velocity' if v['passive_pct'] > s['passive_pct'] else 'Spike':<10}")
    print(f"  {'Unhedged Accuracy':<25} {v['unhedged_accuracy']:<17.1f}% {s['unhedged_accuracy']:<17.1f}% "
          f"{'Velocity' if v['unhedged_accuracy'] > s['unhedged_accuracy'] else 'Spike':<10}")
    print(f"  {'SL Overpay/Trade':<25} ${v['sl_overpay_per_trade']:<17.2f} ${s['sl_overpay_per_trade']:<17.2f} "
          f"{'Velocity' if v['sl_overpay_per_trade'] < s['sl_overpay_per_trade'] else 'Spike':<10}")

    winner = "VELOCITY" if v['total_pnl'] > s['total_pnl'] else "SPIKE"
    print(f"\n  WINNER: {winner}")

    # Balance simulation
    print("\n" + "=" * 80)
    print(f"BALANCE SIMULATION (${STARTING_BALANCE} start, {TARGET_SHARES} shares)")
    print("=" * 80)

    for strategy, optimal_sl in [("velocity", velocity_sl), ("spike", spike_sl)]:
        balance, trades = run_balance_simulation(
            all_markets, strategy, optimal_sl, enable_cycling=True
        )

        roi = (balance.current_balance - balance.starting_balance) / balance.starting_balance * 100
        drawdown_pct = balance.max_drawdown / balance.peak_balance * 100 if balance.peak_balance > 0 else 0

        print(f"\n  {strategy.upper()} ({optimal_sl*100:.0f}% SL, cycling ON):")
        print(f"    Starting: ${balance.starting_balance:.2f}")
        print(f"    Ending: ${balance.current_balance:.2f}")
        print(f"    Peak: ${balance.peak_balance:.2f}")
        print(f"    Max Drawdown: ${balance.max_drawdown:.2f} ({drawdown_pct:.1f}%)")
        print(f"    ROI: {roi:+.1f}%")
        print(f"    Trades executed: {balance.trades_executed}")
        print(f"    Trades skipped (insufficient funds): {balance.trades_skipped_insufficient_funds}")

    print("\n" + "=" * 80)
    print("BACKTEST COMPLETE")
    print("=" * 80)


# =============================================================================
# MAIN
# =============================================================================

def main():
    global STARTING_BALANCE, TARGET_SHARES, VELOCITY_LOSER_OFFSET

    parser = argparse.ArgumentParser(description="Comprehensive Strategy Backtest")
    parser.add_argument('--balance', type=float, default=170.0,
                        help='Starting balance (default: $170)')
    parser.add_argument('--shares', type=int, default=15,
                        help='Target shares per trade (default: 15)')
    parser.add_argument('--loser-offset', type=float, default=0.12,
                        help='Loser offset for velocity strategy (default: 0.12)')
    args = parser.parse_args()

    STARTING_BALANCE = args.balance
    TARGET_SHARES = args.shares
    VELOCITY_LOSER_OFFSET = args.loser_offset

    print("=" * 80)
    print("COMPREHENSIVE STRATEGY BACKTEST")
    print("=" * 80)
    print(f"\nParameters:")
    print(f"  Starting balance: ${STARTING_BALANCE}")
    print(f"  Target shares: {TARGET_SHARES}")
    print(f"  Loser offset: {VELOCITY_LOSER_OFFSET}")
    print(f"  Formula: loser_bid - {VELOCITY_LOSER_OFFSET} (matching live code)")
    print(f"  Min time: {MIN_TIME}s")
    print(f"  Min runtime: {MIN_RUNTIME_SECS}s (5 min)")
    print(f"  Stop-loss options: {[f'{sl*100:.0f}%' for sl in STOP_LOSS_OPTIONS]}")

    # Load actual resolution data
    print("\nLoading resolution data...")
    load_resolution_cache()

    # Load data
    all_markets, filter_stats = load_market_data()

    if not all_markets:
        print("No valid markets found!")
        return

    # Optimize stop-loss for both strategies
    print("\nOptimizing velocity stop-loss...")
    velocity_sl, velocity_results = optimize_stop_loss(all_markets, "velocity")

    print("Optimizing spike stop-loss...")
    spike_sl, spike_results = optimize_stop_loss(all_markets, "spike")

    # Print full report
    print_report(
        all_markets,
        filter_stats,
        velocity_sl,
        velocity_results,
        spike_sl,
        spike_results,
    )


if __name__ == "__main__":
    main()
