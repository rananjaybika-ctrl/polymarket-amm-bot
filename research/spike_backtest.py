#!/usr/bin/env python3
"""
Spike Capture Strategy - Comprehensive Backtest

Compares spike detection (3-tick) vs velocity detection (10-second average)
with full support for:
- Cycling ON/OFF comparison
- Hedged/Unhedged/Stop-loss trade breakdowns
- Stop-loss percentage optimization
- Resolution accuracy tracking

Key Findings from Research:
- Spike detection: 4x faster (16ms vs 517ms with @bookTicker)
- Need to optimize loser offset and stop-loss for spike strategy

Usage:
    python research/spike_backtest.py
    python research/spike_backtest.py --stop-loss 0.07
    python research/spike_backtest.py --optimize-stoploss
    python research/spike_backtest.py --no-cycling
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
from collections import defaultdict
import argparse

# =============================================================================
# CONFIGURATION
# =============================================================================

# Trade parameters
SHARES = 15
MIN_TIME = 60  # Changed from 120 to 60 seconds
MIN_CYCLE_GAP_SAMPLES = 5  # ~1 second between cycles

# Spike detection parameters
SPIKE_LOOKBACK = 3           # 3 ticks (~600ms at 5 ticks/sec, ~16ms with bookTicker)
SPIKE_THRESHOLD = 0.02       # 0.02% minimum to trigger (~$20 on $100k BTC)
# Hedge pricing (v2: recalibrated Jan 18, 2026 - see HEDGE_PRICING_FINDINGS.md)
DROP_MULTIPLIER = 0.50       # Reduced from 0.68 - spike has weak predictive power
DROP_INTERCEPT = 0.08        # Increased from 0.01 - matches actual mean drop
DROP_REGIME_BONUS = {'LOW': 0.0, 'MEDIUM': 0.01, 'HIGH': 0.02}

# Velocity parameters (for comparison)
VELOCITY_THRESHOLD = 0.50    # Zone 5-6 threshold
VELOCITY_LOSER_OFFSET = 0.12 # Fixed loser offset

# Default stop-loss
DEFAULT_STOP_LOSS_PCT = 0.07  # 7%


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class TradeResult:
    """Result of a single trade cycle."""
    strategy: str              # "spike" or "velocity"
    cycle_num: int
    market_slug: str
    entry_time_remaining: float
    winner_side: str
    winner_fill_price: float
    loser_fill_price: float
    hedge_type: str            # "passive", "stoploss", "unhedged"
    pair_cost: float
    pnl: float
    resolution: str            # Actual resolution: "UP" or "DOWN"
    velocity_correct: bool     # Was signal direction correct?
    samples_to_hedge: int      # How many samples until hedge
    # Spike-specific
    spike_magnitude: float = 0.0
    # Velocity-specific
    velocity_bps: float = 0.0


@dataclass
class MarketResult:
    """Result from one market."""
    slug: str
    total_samples: int
    total_cycles: int
    cycles: List[TradeResult]
    total_pnl: float
    resolution: str


# =============================================================================
# SPIKE DETECTION
# =============================================================================

def add_spike_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate spike detection from existing binance_price data."""
    df = df.copy()
    df['price_change_3tick'] = df['binance_price'].pct_change(periods=SPIKE_LOOKBACK) * 100
    df['spike_magnitude'] = df['price_change_3tick'].abs()
    df['spike_detected'] = df['spike_magnitude'] >= SPIKE_THRESHOLD
    df['spike_direction'] = df['price_change_3tick'].apply(
        lambda x: 'UP' if x >= SPIKE_THRESHOLD else ('DOWN' if x <= -SPIKE_THRESHOLD else None)
    )
    # V2 formula: expected_drop = 0.50 * spike + 0.08 + regime_bonus (default MEDIUM=0.01)
    df['expected_drop'] = DROP_MULTIPLIER * df['spike_magnitude'] + DROP_INTERCEPT + DROP_REGIME_BONUS.get('MEDIUM', 0.01)
    df['expected_drop'] = df['expected_drop'].clip(lower=0.02, upper=0.20)
    return df


def get_resolution(mdf: pd.DataFrame) -> str:
    """Determine market resolution from final orderbook state."""
    final = mdf.iloc[-1]
    if final['up_bid'] >= 0.90:
        return 'UP'
    elif final['down_bid'] >= 0.90:
        return 'DOWN'
    else:
        return 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'


# =============================================================================
# SPIKE STRATEGY SIMULATION
# =============================================================================

def simulate_spike_market(
    mdf: pd.DataFrame,
    slug: str,
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
    enable_cycling: bool = True,
    use_magnitude_offset: bool = True,  # Use dynamic offset based on spike magnitude
) -> Optional[MarketResult]:
    """
    Simulate spike capture strategy on market data.

    Entry Logic:
    - When spike_detected and magnitude >= threshold
    - Winner side = spike direction
    - Winner entry = winner_ask (aggressive)
    - Loser bid = magnitude-based OR fixed offset
    """
    mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)
    mdf = add_spike_columns(mdf)

    resolution = get_resolution(mdf)
    cycles = []
    cycle_num = 0

    i = 0
    in_trade = False
    winner_side = None
    winner_fill_price = 0.0
    loser_target_bid = 0.0
    entry_time = 0.0
    spike_mag = 0.0

    while i < len(mdf):
        row = mdf.iloc[i]
        time_rem = row['time_remaining_secs']

        if time_rem < MIN_TIME:
            break

        if not in_trade:
            # Look for spike signal
            if row.get('spike_detected', False) and pd.notna(row.get('spike_direction')):
                in_trade = True
                cycle_num += 1
                winner_side = row['spike_direction']
                entry_time = time_rem
                spike_mag = row['spike_magnitude']

                # Winner fills at ASK (aggressive entry)
                if winner_side == "UP":
                    winner_fill_price = row['up_ask']
                    loser_ask = row['down_ask']
                else:
                    winner_fill_price = row['down_ask']
                    loser_ask = row['up_ask']

                # Calculate loser bid
                if use_magnitude_offset:
                    # V2 formula: expected_drop = 0.50 * magnitude + 0.08 + regime_bonus
                    regime_bonus = DROP_REGIME_BONUS.get('MEDIUM', 0.01)  # Default to MEDIUM
                    expected_drop = DROP_MULTIPLIER * spike_mag + DROP_INTERCEPT + regime_bonus
                    expected_drop = max(0.02, min(0.20, expected_drop))
                    loser_target_bid = loser_ask - expected_drop
                else:
                    # Fixed offset like velocity strategy
                    loser_target_bid = loser_ask - VELOCITY_LOSER_OFFSET

                loser_target_bid = max(0.01, min(0.95, loser_target_bid))

                # Scan forward for hedge
                loser_filled = False
                loser_fill_price = 0.0
                hedge_type = "unhedged"
                samples_to_hedge = 0

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
                            i = j + MIN_CYCLE_GAP_SAMPLES if enable_cycling else len(mdf)
                            break

                # Calculate PnL
                velocity_correct = (winner_side == resolution)

                if loser_filled:
                    pair_cost = winner_fill_price + loser_fill_price
                    pnl = (1.0 - pair_cost) * SHARES
                else:
                    # Unhedged - PnL depends on resolution
                    if velocity_correct:
                        pnl = (1.0 - winner_fill_price) * SHARES
                    else:
                        pnl = (0.0 - winner_fill_price) * SHARES
                    pair_cost = winner_fill_price

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
                    velocity_correct=velocity_correct,
                    samples_to_hedge=samples_to_hedge,
                    spike_magnitude=spike_mag,
                ))

                in_trade = False

                if not enable_cycling:
                    break  # Only one entry per market

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
# VELOCITY STRATEGY SIMULATION (BASELINE)
# =============================================================================

def simulate_velocity_market(
    mdf: pd.DataFrame,
    slug: str,
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
    enable_cycling: bool = True,
) -> Optional[MarketResult]:
    """
    Simulate velocity-based strategy on market data (baseline for comparison).

    Entry Logic:
    - When |velocity_bps| >= 0.50 (Zone 5-6)
    - Winner side = velocity direction
    - Winner entry = winner_ask
    - Loser bid = loser_ask - 0.12 (fixed offset)
    """
    mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)
    resolution = get_resolution(mdf)
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
                in_trade = True
                cycle_num += 1
                winner_side = "UP" if vel > 0 else "DOWN"
                entry_time = time_rem

                # Winner fills at ASK
                if winner_side == "UP":
                    winner_fill_price = row['up_ask']
                    loser_ask = row['down_ask']
                else:
                    winner_fill_price = row['down_ask']
                    loser_ask = row['up_ask']

                # Fixed loser offset
                loser_target_bid = loser_ask - VELOCITY_LOSER_OFFSET
                loser_target_bid = max(0.01, min(0.95, loser_target_bid))

                # Scan forward for hedge
                loser_filled = False
                loser_fill_price = 0.0
                hedge_type = "unhedged"
                samples_to_hedge = 0

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
                            i = j + MIN_CYCLE_GAP_SAMPLES if enable_cycling else len(mdf)
                            break

                # Calculate PnL
                velocity_correct = (winner_side == resolution)

                if loser_filled:
                    pair_cost = winner_fill_price + loser_fill_price
                    pnl = (1.0 - pair_cost) * SHARES
                else:
                    if velocity_correct:
                        pnl = (1.0 - winner_fill_price) * SHARES
                    else:
                        pnl = (0.0 - winner_fill_price) * SHARES
                    pair_cost = winner_fill_price

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
                    velocity_correct=velocity_correct,
                    samples_to_hedge=samples_to_hedge,
                    velocity_bps=vel,
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

def analyze_results(results: List[TradeResult], strategy_name: str, total_hours: float) -> Dict:
    """Analyze trade results with full breakdown."""
    if not results:
        return {"error": "No trades"}

    # Breakdown by hedge type
    passive = [r for r in results if r.hedge_type == "passive"]
    stoploss = [r for r in results if r.hedge_type == "stoploss"]
    unhedged = [r for r in results if r.hedge_type == "unhedged"]

    # PnL by type
    passive_pnl = sum(r.pnl for r in passive)
    stoploss_pnl = sum(r.pnl for r in stoploss)
    unhedged_pnl = sum(r.pnl for r in unhedged)
    total_pnl = sum(r.pnl for r in results)

    # Accuracy
    correct = [r for r in results if r.velocity_correct]
    accuracy = len(correct) / len(results) * 100 if results else 0

    # Unhedged accuracy (critical metric)
    unhedged_correct = [r for r in unhedged if r.velocity_correct]
    unhedged_accuracy = len(unhedged_correct) / len(unhedged) * 100 if unhedged else 0

    # Pair costs
    hedged = passive + stoploss
    hedged_costs = [r.pair_cost for r in hedged]
    passive_costs = [r.pair_cost for r in passive]
    stoploss_costs = [r.pair_cost for r in stoploss]

    under_dollar = sum(1 for c in hedged_costs if c < 1.0)
    under_dollar_pct = under_dollar / len(hedged) * 100 if hedged else 0

    return {
        "strategy": strategy_name,
        "total_trades": len(results),
        "total_pnl": total_pnl,
        "hourly_rate": total_pnl / total_hours if total_hours > 0 else 0,
        # Breakdown by type
        "passive_count": len(passive),
        "passive_pct": len(passive) / len(results) * 100 if results else 0,
        "passive_pnl": passive_pnl,
        "passive_avg_pnl": passive_pnl / len(passive) if passive else 0,
        "stoploss_count": len(stoploss),
        "stoploss_pct": len(stoploss) / len(results) * 100 if results else 0,
        "stoploss_pnl": stoploss_pnl,
        "stoploss_avg_pnl": stoploss_pnl / len(stoploss) if stoploss else 0,
        "unhedged_count": len(unhedged),
        "unhedged_pct": len(unhedged) / len(results) * 100 if results else 0,
        "unhedged_pnl": unhedged_pnl,
        "unhedged_avg_pnl": unhedged_pnl / len(unhedged) if unhedged else 0,
        # Accuracy
        "overall_accuracy": accuracy,
        "unhedged_accuracy": unhedged_accuracy,
        "unhedged_correct": len(unhedged_correct),
        "unhedged_wrong": len(unhedged) - len(unhedged_correct),
        # Pair costs
        "mean_pair_cost": np.mean(hedged_costs) if hedged_costs else 0,
        "median_pair_cost": np.median(hedged_costs) if hedged_costs else 0,
        "passive_avg_cost": np.mean(passive_costs) if passive_costs else 0,
        "stoploss_avg_cost": np.mean(stoploss_costs) if stoploss_costs else 0,
        "under_dollar_count": under_dollar,
        "under_dollar_pct": under_dollar_pct,
    }


def print_analysis(analysis: Dict, show_detail: bool = True):
    """Print analysis results."""
    if "error" in analysis:
        print(f"  {analysis['strategy']}: {analysis['error']}")
        return

    print(f"\n{analysis['strategy'].upper()} STRATEGY:")
    print(f"  Total trades: {analysis['total_trades']}")
    print(f"  Total PnL: ${analysis['total_pnl']:.2f}")
    print(f"  Hourly rate: ${analysis['hourly_rate']:.2f}/hr")

    if show_detail:
        print(f"\n  Hedge Type Breakdown:")
        print(f"    Passive:  {analysis['passive_count']:4} ({analysis['passive_pct']:5.1f}%) "
              f"PnL: ${analysis['passive_pnl']:7.2f} (avg ${analysis['passive_avg_pnl']:+.2f}/trade)")
        print(f"    Stop-loss:{analysis['stoploss_count']:4} ({analysis['stoploss_pct']:5.1f}%) "
              f"PnL: ${analysis['stoploss_pnl']:7.2f} (avg ${analysis['stoploss_avg_pnl']:+.2f}/trade)")
        print(f"    Unhedged: {analysis['unhedged_count']:4} ({analysis['unhedged_pct']:5.1f}%) "
              f"PnL: ${analysis['unhedged_pnl']:7.2f} (avg ${analysis['unhedged_avg_pnl']:+.2f}/trade)")

        print(f"\n  Accuracy:")
        print(f"    Overall: {analysis['overall_accuracy']:.1f}%")
        if analysis['unhedged_count'] > 0:
            print(f"    Unhedged: {analysis['unhedged_accuracy']:.1f}% "
                  f"({analysis['unhedged_correct']} correct, {analysis['unhedged_wrong']} wrong)")

        print(f"\n  Pair Cost Analysis (hedged trades):")
        print(f"    Mean: ${analysis['mean_pair_cost']:.4f}")
        print(f"    Median: ${analysis['median_pair_cost']:.4f}")
        print(f"    Passive avg: ${analysis['passive_avg_cost']:.4f}")
        print(f"    Stop-loss avg: ${analysis['stoploss_avg_cost']:.4f}")
        print(f"    Under $1.00: {analysis['under_dollar_count']}/{analysis['passive_count'] + analysis['stoploss_count']} "
              f"({analysis['under_dollar_pct']:.1f}%)")


# =============================================================================
# STOP-LOSS OPTIMIZATION
# =============================================================================

def optimize_stop_loss(all_markets: Dict, strategy: str = "spike") -> Dict:
    """Find optimal stop-loss percentage."""
    print("\n" + "=" * 80)
    print(f"STOP-LOSS OPTIMIZATION ({strategy.upper()})")
    print("=" * 80)

    stop_loss_options = [0.03, 0.05, 0.07, 0.10, 0.12, 0.15]
    total_hours = len(all_markets) * 15 / 60

    print(f"\n{'SL%':>5} {'Trades':>7} {'Passive':>8} {'SL':>8} {'Unh':>6} "
          f"{'P_PnL':>9} {'SL_PnL':>9} {'U_PnL':>9} {'Total':>9} {'$/hr':>8}")
    print("-" * 95)

    best_pnl = float('-inf')
    best_config = None
    results_by_sl = {}

    for sl_pct in stop_loss_options:
        all_trades = []

        for slug, mdf in all_markets.items():
            if strategy == "spike":
                result = simulate_spike_market(mdf, slug, stop_loss_pct=sl_pct, enable_cycling=True)
            else:
                result = simulate_velocity_market(mdf, slug, stop_loss_pct=sl_pct, enable_cycling=True)

            if result:
                all_trades.extend(result.cycles)

        analysis = analyze_results(all_trades, strategy, total_hours)
        results_by_sl[sl_pct] = analysis

        print(f"{sl_pct*100:>4.0f}% {analysis['total_trades']:>7} "
              f"{analysis['passive_count']:>8} {analysis['stoploss_count']:>8} {analysis['unhedged_count']:>6} "
              f"${analysis['passive_pnl']:>7.2f} ${analysis['stoploss_pnl']:>7.2f} ${analysis['unhedged_pnl']:>7.2f} "
              f"${analysis['total_pnl']:>7.2f} ${analysis['hourly_rate']:>6.2f}")

        if analysis['total_pnl'] > best_pnl:
            best_pnl = analysis['total_pnl']
            best_config = {'stop_loss': sl_pct, **analysis}

    print(f"\n  OPTIMAL: {best_config['stop_loss']*100:.0f}% stop-loss → ${best_config['total_pnl']:.2f} "
          f"(${best_config['hourly_rate']:.2f}/hr)")

    return best_config


# =============================================================================
# CYCLING COMPARISON
# =============================================================================

def compare_cycling(all_markets: Dict, strategy: str = "spike", stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT):
    """Compare cycling ON vs OFF."""
    print("\n" + "=" * 80)
    print(f"CYCLING COMPARISON ({strategy.upper()}, {stop_loss_pct*100:.0f}% stop-loss)")
    print("=" * 80)

    total_hours = len(all_markets) * 15 / 60

    results = {}
    for cycling in [True, False]:
        all_trades = []

        for slug, mdf in all_markets.items():
            if strategy == "spike":
                result = simulate_spike_market(mdf, slug, stop_loss_pct=stop_loss_pct, enable_cycling=cycling)
            else:
                result = simulate_velocity_market(mdf, slug, stop_loss_pct=stop_loss_pct, enable_cycling=cycling)

            if result:
                all_trades.extend(result.cycles)

        label = "CYCLING ON" if cycling else "CYCLING OFF"
        analysis = analyze_results(all_trades, label, total_hours)
        results[cycling] = analysis
        print_analysis(analysis, show_detail=False)

    # Comparison
    on = results[True]
    off = results[False]

    print(f"\n  Cycling Impact:")
    print(f"    Trade multiplier: {on['total_trades'] / off['total_trades']:.2f}x" if off['total_trades'] > 0 else "")
    print(f"    PnL improvement: ${on['total_pnl'] - off['total_pnl']:.2f} "
          f"({(on['total_pnl'] - off['total_pnl']) / abs(off['total_pnl']) * 100:+.0f}%)" if off['total_pnl'] != 0 else "")

    return results


# =============================================================================
# MAIN
# =============================================================================

def load_market_data() -> Dict[str, pd.DataFrame]:
    """Load and deduplicate market data from observer CSVs."""
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    print(f"Loading data from {len(csv_files)} files...")

    all_markets = {}

    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath, on_bad_lines='skip', low_memory=False)
            if df.empty or 'binance_price' not in df.columns:
                continue

            for slug in df['market_slug'].unique():
                mdf = df[df['market_slug'] == slug]
                if len(mdf) >= 2:
                    first = mdf.iloc[0]['time_remaining_secs']
                    last = mdf.iloc[-1]['time_remaining_secs']
                    if first >= 800 and last <= 60:
                        if slug not in all_markets or len(mdf) > len(all_markets[slug]):
                            all_markets[slug] = mdf.copy()
        except Exception as e:
            continue

    print(f"Unique complete markets: {len(all_markets)}")
    return all_markets


def main():
    parser = argparse.ArgumentParser(description="Spike Capture Strategy Backtest")
    parser.add_argument('--stop-loss', type=float, default=DEFAULT_STOP_LOSS_PCT,
                        help=f'Stop-loss percentage (default: {DEFAULT_STOP_LOSS_PCT})')
    parser.add_argument('--optimize-stoploss', action='store_true',
                        help='Run stop-loss optimization')
    parser.add_argument('--no-cycling', action='store_true',
                        help='Disable cycling (1 entry per market)')
    parser.add_argument('--velocity-only', action='store_true',
                        help='Only run velocity strategy')
    parser.add_argument('--spike-only', action='store_true',
                        help='Only run spike strategy')
    parser.add_argument('--compare-cycling', action='store_true',
                        help='Compare cycling ON vs OFF')
    args = parser.parse_args()

    print("=" * 80)
    print("SPIKE CAPTURE STRATEGY - COMPREHENSIVE BACKTEST")
    print("=" * 80)

    print(f"\nParameters:")
    print(f"  MIN_TIME: {MIN_TIME}s")
    print(f"  SPIKE_LOOKBACK: {SPIKE_LOOKBACK} ticks")
    print(f"  SPIKE_THRESHOLD: {SPIKE_THRESHOLD}%")
    print(f"  VELOCITY_THRESHOLD: {VELOCITY_THRESHOLD} bps")
    print(f"  STOP_LOSS: {args.stop_loss * 100:.0f}%")
    print(f"  CYCLING: {'OFF' if args.no_cycling else 'ON'}")

    # Load data
    all_markets = load_market_data()

    if not all_markets:
        print("No valid markets found!")
        return

    total_hours = len(all_markets) * 15 / 60
    enable_cycling = not args.no_cycling

    # Run stop-loss optimization if requested
    if args.optimize_stoploss:
        print("\n" + "=" * 80)
        print("SPIKE STOP-LOSS OPTIMIZATION")
        print("=" * 80)
        spike_best = optimize_stop_loss(all_markets, "spike")

        print("\n" + "=" * 80)
        print("VELOCITY STOP-LOSS OPTIMIZATION")
        print("=" * 80)
        velocity_best = optimize_stop_loss(all_markets, "velocity")

        print("\n" + "=" * 80)
        print("OPTIMIZATION SUMMARY")
        print("=" * 80)
        print(f"\n  Spike optimal: {spike_best['stop_loss']*100:.0f}% SL → ${spike_best['hourly_rate']:.2f}/hr")
        print(f"  Velocity optimal: {velocity_best['stop_loss']*100:.0f}% SL → ${velocity_best['hourly_rate']:.2f}/hr")
        return

    # Run cycling comparison if requested
    if args.compare_cycling:
        compare_cycling(all_markets, "spike", args.stop_loss)
        compare_cycling(all_markets, "velocity", args.stop_loss)
        return

    # Run main comparison
    print("\n" + "=" * 80)
    print("RUNNING BACKTEST...")
    print("=" * 80)

    spike_trades = []
    velocity_trades = []

    for slug, mdf in all_markets.items():
        if not args.velocity_only:
            spike_result = simulate_spike_market(mdf, slug, stop_loss_pct=args.stop_loss,
                                                  enable_cycling=enable_cycling)
            if spike_result:
                spike_trades.extend(spike_result.cycles)

        if not args.spike_only:
            vel_result = simulate_velocity_market(mdf, slug, stop_loss_pct=args.stop_loss,
                                                   enable_cycling=enable_cycling)
            if vel_result:
                velocity_trades.extend(vel_result.cycles)

    # Analyze and print results
    print("\n" + "=" * 80)
    print("RESULTS")
    print("=" * 80)

    if not args.velocity_only:
        spike_analysis = analyze_results(spike_trades, "SPIKE", total_hours)
        print_analysis(spike_analysis)

    if not args.spike_only:
        velocity_analysis = analyze_results(velocity_trades, "VELOCITY", total_hours)
        print_analysis(velocity_analysis)

    # Head-to-head comparison
    if not args.velocity_only and not args.spike_only and spike_trades and velocity_trades:
        print("\n" + "=" * 80)
        print("HEAD-TO-HEAD COMPARISON")
        print("=" * 80)

        s = spike_analysis
        v = velocity_analysis

        print(f"\n  {'Metric':<25} {'Spike':<15} {'Velocity':<15} {'Winner':<10}")
        print(f"  {'-'*65}")
        print(f"  {'Total Trades':<25} {s['total_trades']:<15} {v['total_trades']:<15} "
              f"{'Spike' if s['total_trades'] > v['total_trades'] else 'Velocity':<10}")
        print(f"  {'Total PnL':<25} ${s['total_pnl']:<14.2f} ${v['total_pnl']:<14.2f} "
              f"{'Spike' if s['total_pnl'] > v['total_pnl'] else 'Velocity':<10}")
        print(f"  {'Hourly Rate':<25} ${s['hourly_rate']:<14.2f} ${v['hourly_rate']:<14.2f} "
              f"{'Spike' if s['hourly_rate'] > v['hourly_rate'] else 'Velocity':<10}")
        print(f"  {'Passive %':<25} {s['passive_pct']:<14.1f}% {v['passive_pct']:<14.1f}% "
              f"{'Spike' if s['passive_pct'] > v['passive_pct'] else 'Velocity':<10}")
        print(f"  {'Mean Pair Cost':<25} ${s['mean_pair_cost']:<14.4f} ${v['mean_pair_cost']:<14.4f} "
              f"{'Spike' if s['mean_pair_cost'] < v['mean_pair_cost'] else 'Velocity':<10}")
        print(f"  {'Under $1.00':<25} {s['under_dollar_pct']:<14.1f}% {v['under_dollar_pct']:<14.1f}% "
              f"{'Spike' if s['under_dollar_pct'] > v['under_dollar_pct'] else 'Velocity':<10}")
        print(f"  {'Unhedged Accuracy':<25} {s['unhedged_accuracy']:<14.1f}% {v['unhedged_accuracy']:<14.1f}% "
              f"{'Spike' if s['unhedged_accuracy'] > v['unhedged_accuracy'] else 'Velocity':<10}")

    print("\n" + "=" * 80)
    print("BACKTEST COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    main()
