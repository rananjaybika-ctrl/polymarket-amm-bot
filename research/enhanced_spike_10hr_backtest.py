#!/usr/bin/env python3
"""
Enhanced Spike Strategy Backtest - 10 Hour Combined Data

Uses all grid observer data (10 hours, 41 markets) with pre-computed spike signals.

Tests:
- Enhanced spike with velocity confirmation
- Cycling ON vs OFF
- Stop-loss grid (5%, 7%, 10%, None)
- Realistic fill model (ask crosses through bid)

Compares: Enhanced Signal vs Raw Spike vs Velocity-only

Usage:
    python research/enhanced_spike_10hr_backtest.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from collections import defaultdict

# =============================================================================
# CONFIGURATION
# =============================================================================

TARGET_SHARES = 15
MIN_TIME = 60  # Entry cutoff (seconds remaining)
MIN_ORDER_QTY = 5
MIN_ORDER_VALUE = 1.0

# Market filtering
MIN_RUNTIME_SECS = 300  # 5 minutes minimum

# Stop-loss grid to test
STOP_LOSS_OPTIONS = [None, 0.05, 0.07, 0.10]

# Enhanced spike config
VELOCITY_CONFIRM_THRESHOLD = 0.10  # Reject if velocity contradicts
ENHANCED_SCORE_THRESHOLD = 0.40

# Cycling
MIN_CYCLE_GAP_SECS = 1.0  # 1 second between cycles

# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class TradeResult:
    """Single trade result."""
    market_slug: str
    cycle_num: int
    entry_time_remaining: float
    signal_type: str
    signal_score: float
    winner_side: str
    winner_fill_price: float
    loser_fill_price: float
    hedge_type: str
    pair_cost: float
    pnl: float
    correct_direction: bool
    samples_to_hedge: int


@dataclass
class BacktestResult:
    """Complete backtest result."""
    strategy_name: str
    stop_loss_pct: Optional[float]
    cycling: bool
    total_trades: int
    total_pnl: float
    hourly_rate: float
    win_rate: float
    avg_pair_cost: float
    passive_hedge_pct: float
    stoploss_hedge_pct: float
    resolution_pct: float
    trades: List[TradeResult] = field(default_factory=list)


# =============================================================================
# DATA LOADING
# =============================================================================

def load_all_observer_data() -> pd.DataFrame:
    """Load and combine all grid observer files."""
    obs_dir = Path("research/observer")
    all_data = []

    for f in sorted(obs_dir.glob("grid_obs_*.csv")):
        df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
        all_data.append(df)
        print(f"  {f.name}: {len(df):,} rows")

    combined = pd.concat(all_data, ignore_index=True)
    combined = combined.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    combined = combined.sort_values(['market_slug', 'timestamp_ms']).reset_index(drop=True)

    print(f"\nCombined: {len(combined):,} rows")

    return combined


def load_resolutions() -> pd.DataFrame:
    """Load verified market resolutions."""
    res_path = Path("research/observer/market_resolutions_verified.csv")
    return pd.read_csv(res_path)


def filter_valid_markets(df: pd.DataFrame, res_df: pd.DataFrame) -> pd.DataFrame:
    """Filter to valid markets with resolutions."""
    # Add resolutions
    res_map = dict(zip(res_df['slug'], res_df['winner']))
    df['resolution'] = df['market_slug'].map(res_map)

    # Keep only UP/DOWN resolutions
    df = df[df['resolution'].isin(['UP', 'DOWN'])].copy()

    valid_slugs = []
    for slug, mdf in df.groupby('market_slug'):
        max_time = mdf['time_remaining_secs'].max()
        min_time = mdf['time_remaining_secs'].min()
        duration = max_time - min_time

        if duration < MIN_RUNTIME_SECS:
            continue
        if max_time < 840:  # Need data from at least 14:00 mark
            continue

        valid_slugs.append(slug)

    df = df[df['market_slug'].isin(valid_slugs)].copy()
    print(f"Valid markets: {len(valid_slugs)} ({len(df):,} rows)")

    return df


# =============================================================================
# SIGNAL DETECTION (using pre-computed columns)
# =============================================================================

def velocity_confirms_spike(spike_dir: str, velocity_bps: float) -> bool:
    """Check if velocity confirms spike direction."""
    if spike_dir == "UP":
        return velocity_bps > -VELOCITY_CONFIRM_THRESHOLD
    elif spike_dir == "DOWN":
        return velocity_bps < VELOCITY_CONFIRM_THRESHOLD
    return True


def compute_enhanced_score(spike_mag: float, velocity_bps: float,
                           spike_dir: str, time_remaining: float) -> float:
    """Compute composite signal score."""
    spike_score = min(spike_mag / 0.05, 1.0)
    velocity_score = min(abs(velocity_bps) / 0.50, 1.0)

    vel_confirms = (spike_dir == "UP" and velocity_bps > 0) or \
                   (spike_dir == "DOWN" and velocity_bps < 0)
    confirm_bonus = 1.0 if vel_confirms else 0.0

    urgency = 1.0 - min(time_remaining / 900.0, 1.0)

    score = (0.40 * spike_score +
             0.30 * velocity_score +
             0.20 * confirm_bonus +
             0.10 * urgency)

    return round(score, 3)


# =============================================================================
# FILL SIMULATION
# =============================================================================

def check_passive_fill(prev_ask: float, curr_ask: float, our_bid: float) -> bool:
    """Realistic fill check: ask must cross through our bid."""
    return prev_ask > our_bid and curr_ask <= our_bid


def calculate_loser_bid(entry_price: float, spike_magnitude: float) -> float:
    """Calculate loser bid based on magnitude."""
    DROP_MULTIPLIER = 0.68
    expected_drop = DROP_MULTIPLIER * spike_magnitude / 100 + 0.01
    loser_bid = (1.0 - entry_price) - expected_drop
    return max(0.01, min(0.95, loser_bid))


# =============================================================================
# BACKTEST SIMULATION
# =============================================================================

def simulate_market(mdf: pd.DataFrame, slug: str, resolution: str,
                    stop_loss_pct: Optional[float], enable_cycling: bool,
                    signal_type: str = "enhanced") -> List[TradeResult]:
    """Simulate trading on a single market."""
    mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)
    trades = []

    i = 0
    cycle_num = 0
    last_trade_time = float('inf')

    while i < len(mdf):
        row = mdf.iloc[i]
        time_rem = row['time_remaining_secs']

        if time_rem < MIN_TIME:
            break

        # Enforce minimum gap between trades
        if enable_cycling and (last_trade_time - time_rem) < MIN_CYCLE_GAP_SECS:
            i += 1
            continue

        velocity_bps = row.get('velocity_bps', 0.0) or 0.0
        spike_detected = row.get('spike_detected', False)
        spike_direction = row.get('spike_direction', None)
        spike_magnitude = row.get('spike_magnitude', 0.0) or 0.0

        # Detect signal based on type
        signal_dir = None
        signal_score = 0.0

        if signal_type == "spike":
            if spike_detected and spike_direction:
                signal_dir = spike_direction
                signal_score = spike_magnitude

        elif signal_type == "velocity":
            if abs(velocity_bps) >= 0.10:
                signal_dir = "UP" if velocity_bps > 0 else "DOWN"
                signal_score = abs(velocity_bps)

        elif signal_type == "enhanced":
            if spike_detected and spike_direction:
                if velocity_confirms_spike(spike_direction, velocity_bps):
                    score = compute_enhanced_score(spike_magnitude, velocity_bps,
                                                   spike_direction, time_rem)
                    if score >= ENHANCED_SCORE_THRESHOLD:
                        signal_dir = spike_direction
                        signal_score = score

        if signal_dir is None:
            i += 1
            continue

        # ENTRY SIGNAL DETECTED
        cycle_num += 1
        last_trade_time = time_rem
        winner_side = signal_dir
        loser_side = "DOWN" if winner_side == "UP" else "UP"

        # Entry prices
        if winner_side == "UP":
            winner_ask = row['up_ask']
            loser_ask = row['down_ask']
        else:
            winner_ask = row['down_ask']
            loser_ask = row['up_ask']

        winner_fill_price = winner_ask

        # Calculate loser bid target
        spike_mag_for_calc = spike_magnitude if spike_magnitude > 0 else 0.02
        loser_target_bid = calculate_loser_bid(winner_fill_price, spike_mag_for_calc)

        # Scan forward for hedge fill
        entry_idx = i
        loser_filled = False
        loser_fill_price = 0.0
        hedge_type = "resolution"
        samples_to_hedge = 0

        for j in range(i + 1, len(mdf)):
            scan_row = mdf.iloc[j]

            if loser_side == "UP":
                prev_ask = mdf.iloc[j-1]['up_ask'] if j > 0 else scan_row['up_ask']
                curr_ask = scan_row['up_ask']
            else:
                prev_ask = mdf.iloc[j-1]['down_ask'] if j > 0 else scan_row['down_ask']
                curr_ask = scan_row['down_ask']

            # Check passive fill
            if check_passive_fill(prev_ask, curr_ask, loser_target_bid):
                loser_filled = True
                loser_fill_price = loser_target_bid
                hedge_type = "passive"
                samples_to_hedge = j - entry_idx
                i = j + 1 if enable_cycling else len(mdf)
                break

            # Check stop-loss
            if stop_loss_pct is not None:
                if winner_side == "UP":
                    winner_bid_now = scan_row['up_bid']
                else:
                    winner_bid_now = scan_row['down_bid']

                drop_pct = (winner_fill_price - winner_bid_now) / winner_fill_price

                if drop_pct >= stop_loss_pct:
                    loser_filled = True
                    loser_fill_price = curr_ask
                    hedge_type = "stoploss"
                    samples_to_hedge = j - entry_idx
                    i = j + 1 if enable_cycling else len(mdf)
                    break

        # If not filled, resolve at market end
        if not loser_filled:
            if resolution == winner_side:
                loser_fill_price = 0.0
            else:
                loser_fill_price = 1.0
            hedge_type = "resolution"
            samples_to_hedge = len(mdf) - entry_idx
            i = len(mdf)

        # Calculate P&L
        pair_cost = winner_fill_price + loser_fill_price

        if hedge_type == "resolution":
            if resolution == winner_side:
                pnl = 1.0 - winner_fill_price
            else:
                pnl = -winner_fill_price
        else:
            pnl = 1.0 - pair_cost

        correct = resolution == winner_side

        trades.append(TradeResult(
            market_slug=slug,
            cycle_num=cycle_num,
            entry_time_remaining=time_rem,
            signal_type=signal_type,
            signal_score=signal_score,
            winner_side=winner_side,
            winner_fill_price=winner_fill_price,
            loser_fill_price=loser_fill_price,
            hedge_type=hedge_type,
            pair_cost=pair_cost,
            pnl=pnl,
            correct_direction=correct,
            samples_to_hedge=samples_to_hedge,
        ))

        if not enable_cycling:
            break

        i += 1

    return trades


def run_backtest(df: pd.DataFrame, signal_type: str, stop_loss_pct: Optional[float],
                 enable_cycling: bool, hours: float) -> BacktestResult:
    """Run backtest across all markets."""
    all_trades = []

    for slug, mdf in df.groupby('market_slug'):
        resolution = mdf['resolution'].iloc[0]
        trades = simulate_market(mdf, slug, resolution, stop_loss_pct, enable_cycling, signal_type)
        all_trades.extend(trades)

    if not all_trades:
        return BacktestResult(
            strategy_name=signal_type,
            stop_loss_pct=stop_loss_pct,
            cycling=enable_cycling,
            total_trades=0,
            total_pnl=0,
            hourly_rate=0,
            win_rate=0,
            avg_pair_cost=0,
            passive_hedge_pct=0,
            stoploss_hedge_pct=0,
            resolution_pct=0,
        )

    total_pnl = sum(t.pnl for t in all_trades)
    total_trades = len(all_trades)
    hourly_rate = total_pnl / hours if hours > 0 else 0

    wins = sum(1 for t in all_trades if t.pnl > 0)
    win_rate = wins / total_trades if total_trades > 0 else 0

    hedged_trades = [t for t in all_trades if t.hedge_type != "resolution"]
    avg_pair_cost = np.mean([t.pair_cost for t in hedged_trades]) if hedged_trades else 0

    passive = sum(1 for t in all_trades if t.hedge_type == "passive")
    stoploss = sum(1 for t in all_trades if t.hedge_type == "stoploss")
    resolution = sum(1 for t in all_trades if t.hedge_type == "resolution")

    return BacktestResult(
        strategy_name=signal_type,
        stop_loss_pct=stop_loss_pct,
        cycling=enable_cycling,
        total_trades=total_trades,
        total_pnl=total_pnl,
        hourly_rate=hourly_rate,
        win_rate=win_rate,
        avg_pair_cost=avg_pair_cost,
        passive_hedge_pct=passive / total_trades if total_trades > 0 else 0,
        stoploss_hedge_pct=stoploss / total_trades if total_trades > 0 else 0,
        resolution_pct=resolution / total_trades if total_trades > 0 else 0,
        trades=all_trades,
    )


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 80)
    print("ENHANCED SPIKE STRATEGY BACKTEST - 10 HOUR COMBINED DATA")
    print("=" * 80)
    print()

    # Load data
    print("Loading observer data...")
    df = load_all_observer_data()

    print("\nLoading resolutions...")
    res_df = load_resolutions()

    print("\nFiltering valid markets...")
    df = filter_valid_markets(df, res_df)

    # Calculate hours
    total_duration_ms = df['timestamp_ms'].max() - df['timestamp_ms'].min()
    hours = total_duration_ms / 3600000
    print(f"\nTotal data span: {hours:.2f} hours")
    print(f"Markets: {df['market_slug'].nunique()}")
    print()

    # Run all combinations
    results = []
    signal_types = ["velocity", "spike", "enhanced"]
    cycling_options = [False, True]

    print("Running backtests...")
    print("-" * 80)

    for signal_type in signal_types:
        for cycling in cycling_options:
            for stop_loss in STOP_LOSS_OPTIONS:
                result = run_backtest(df, signal_type, stop_loss, cycling, hours)
                results.append(result)

                sl_str = f"{stop_loss*100:.0f}%" if stop_loss else "None"
                cyc_str = "ON" if cycling else "OFF"
                print(f"  {signal_type:10} | SL={sl_str:5} | Cycling={cyc_str:3} | "
                      f"Trades={result.total_trades:4} | PnL=${result.total_pnl:8.2f} | "
                      f"$/hr=${result.hourly_rate:7.2f}")

    # Summary table
    print()
    print("=" * 80)
    print("RESULTS SUMMARY (sorted by $/hr)")
    print("=" * 80)
    print()
    print(f"{'Strategy':<12} {'SL':<6} {'Cycle':<6} {'Trades':<7} {'PnL':>10} {'$/hr':>9} "
          f"{'Win%':>7} {'Pair$':>7} {'Pass%':>7} {'SL%':>6} {'Res%':>6}")
    print("-" * 110)

    results.sort(key=lambda x: x.hourly_rate, reverse=True)

    for r in results:
        sl_str = f"{r.stop_loss_pct*100:.0f}%" if r.stop_loss_pct else "None"
        cyc_str = "ON" if r.cycling else "OFF"
        print(f"{r.strategy_name:<12} {sl_str:<6} {cyc_str:<6} {r.total_trades:<7} "
              f"${r.total_pnl:>8.2f} ${r.hourly_rate:>8.2f} {r.win_rate:>6.1%} "
              f"${r.avg_pair_cost:>6.3f} {r.passive_hedge_pct:>6.1%} "
              f"{r.stoploss_hedge_pct:>5.1%} {r.resolution_pct:>5.1%}")

    # Top 5 analysis
    print()
    print("=" * 80)
    print("TOP 5 STRATEGIES")
    print("=" * 80)

    for i, r in enumerate(results[:5], 1):
        sl_str = f"{r.stop_loss_pct*100:.0f}%" if r.stop_loss_pct else "None"
        print(f"\n#{i}: {r.strategy_name} (SL={sl_str}, Cycling={'ON' if r.cycling else 'OFF'})")
        print(f"    Hourly rate: ${r.hourly_rate:.2f}/hr")
        print(f"    Total P&L:   ${r.total_pnl:.2f} over {hours:.1f}h")
        print(f"    Trades:      {r.total_trades}")
        print(f"    Win rate:    {r.win_rate:.1%}")
        print(f"    Avg pair:    ${r.avg_pair_cost:.3f}")
        print(f"    Hedge types: {r.passive_hedge_pct:.1%} passive, {r.stoploss_hedge_pct:.1%} stop-loss, {r.resolution_pct:.1%} resolution")

    # Comparison to plan expectations
    print()
    print("=" * 80)
    print("COMPARISON TO PLAN EXPECTATIONS (happy-sauteeing-dewdrop.md)")
    print("=" * 80)
    print()
    print("Plan Expected:")
    print("  - Enhanced Signal: $7.54/hr (8.19h backtest)")
    print("  - Spike Raw:       $7.03/hr")
    print("  - Velocity:        $2.37/hr")
    print()

    # Find matching strategies
    for signal_type in ["enhanced", "spike", "velocity"]:
        # Best version with cycling and 7% stop-loss (most comparable)
        match = [r for r in results if r.strategy_name == signal_type
                 and r.cycling == True and r.stop_loss_pct == 0.07]
        if match:
            r = match[0]
            print(f"Actual {signal_type:10}: ${r.hourly_rate:.2f}/hr ({r.total_trades} trades)")

    print()
    print("=" * 80)


if __name__ == "__main__":
    main()
