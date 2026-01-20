#!/usr/bin/env python3
"""
Enhanced Spike Strategy Backtest

Uses ONLY data where:
1. Both observer AND price logger data exists
2. Market is valid (proper start time, >= 5 min duration)
3. Resolution is verified from Polymarket API

Tests:
- Enhanced spike with velocity confirmation
- Cycling ON vs OFF
- Stop-loss grid (5%, 7%, 10%, None)
- Realistic fill model (ask crosses through bid)

Usage:
    python research/enhanced_spike_backtest.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from datetime import datetime, timezone
from collections import defaultdict

# =============================================================================
# CONFIGURATION
# =============================================================================

STARTING_BALANCE = 170.0
TARGET_SHARES = 50  # Updated Jan 20: optimizer found 50 > 30 > 15
MIN_TIME = 60  # Entry cutoff (seconds remaining)
MIN_ORDER_QTY = 5
MIN_ORDER_VALUE = 1.0

# Market filtering
MIN_RUNTIME_SECS = 300  # 5 minutes minimum

# Stop-loss grid to test
# Updated Jan 20: 12% is optimal, None and 15% for comparison
STOP_LOSS_OPTIONS = [None, 0.12, 0.15]

# Enhanced spike config (updated Jan 20 from optimizer)
# At 5Hz observer data: 7 ticks = 1400ms (best lookback from optimizer)
SPIKE_LOOKBACK = 7  # 7 ticks = 1400ms at 5Hz (was 3 = 600ms)
SPIKE_THRESHOLD = 0.02  # 0.02% minimum spike
VELOCITY_CONFIRM_THRESHOLD = 0.10  # Reject if velocity contradicts

# Cycling
MIN_CYCLE_GAP_SAMPLES = 5  # ~1 second between cycles

# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class TradeResult:
    """Single trade result."""
    market_slug: str
    cycle_num: int
    entry_time_remaining: float
    signal_type: str  # "spike", "velocity", "enhanced"
    signal_score: float
    winner_side: str
    winner_fill_price: float
    loser_fill_price: float
    hedge_type: str  # "passive", "stoploss", "resolution"
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

def load_and_align_data(observer_path: str, binance_path: str, resolutions_path: str) -> pd.DataFrame:
    """
    Load and align observer data with Binance HF prices.

    Only returns rows where both data sources have valid data.
    """
    print(f"Loading observer data: {observer_path}")
    obs_df = pd.read_csv(observer_path)
    print(f"  Rows: {len(obs_df):,}")

    print(f"Loading Binance HF data: {binance_path}")
    btc_df = pd.read_csv(binance_path)
    print(f"  Rows: {len(btc_df):,}")

    print(f"Loading resolutions: {resolutions_path}")
    res_df = pd.read_csv(resolutions_path)
    print(f"  Markets: {len(res_df):,}")

    # Get time ranges
    obs_start = obs_df['timestamp_ms'].min()
    obs_end = obs_df['timestamp_ms'].max()
    btc_start = btc_df['timestamp_ms'].min()
    btc_end = btc_df['timestamp_ms'].max()

    # Find overlap
    overlap_start = max(obs_start, btc_start)
    overlap_end = min(obs_end, btc_end)

    print(f"\nTime overlap:")
    print(f"  Observer: {obs_start} - {obs_end}")
    print(f"  Binance:  {btc_start} - {btc_end}")
    print(f"  Overlap:  {overlap_start} - {overlap_end}")

    # Filter to overlap period
    obs_df = obs_df[(obs_df['timestamp_ms'] >= overlap_start) &
                     (obs_df['timestamp_ms'] <= overlap_end)].copy()
    btc_df = btc_df[(btc_df['timestamp_ms'] >= overlap_start) &
                     (btc_df['timestamp_ms'] <= overlap_end)].copy()

    print(f"\nAfter alignment:")
    print(f"  Observer rows: {len(obs_df):,}")
    print(f"  Binance rows: {len(btc_df):,}")

    # Merge Binance prices into observer data (nearest timestamp)
    obs_df = obs_df.sort_values('timestamp_ms').reset_index(drop=True)
    btc_df = btc_df.sort_values('timestamp_ms').reset_index(drop=True)

    # Use merge_asof for efficient nearest-neighbor join
    obs_df = pd.merge_asof(
        obs_df,
        btc_df[['timestamp_ms', 'price']].rename(columns={'price': 'btc_price_hf'}),
        on='timestamp_ms',
        direction='nearest',
        tolerance=1000  # 1 second tolerance
    )

    # Drop rows without Binance price
    before = len(obs_df)
    obs_df = obs_df.dropna(subset=['btc_price_hf'])
    print(f"  After HF merge: {len(obs_df):,} (dropped {before - len(obs_df)} without BTC price)")

    # Add resolution data from verified API resolutions
    # Resolution file uses 'slug' column, observer uses 'market_slug'
    res_map = dict(zip(res_df['slug'], res_df['winner']))
    obs_df['resolution'] = obs_df['market_slug'].map(res_map)

    # Filter to only markets with verified UP/DOWN resolutions
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    # Filter to markets with resolutions
    before = len(obs_df)
    obs_df = obs_df.dropna(subset=['resolution'])
    markets_with_res = obs_df['market_slug'].nunique()
    print(f"  With resolutions: {len(obs_df):,} rows, {markets_with_res} markets")

    return obs_df


def filter_valid_markets(df: pd.DataFrame) -> pd.DataFrame:
    """Filter to valid markets (proper timing, enough data)."""
    valid_slugs = []

    for slug, mdf in df.groupby('market_slug'):
        # Check duration
        max_time = mdf['time_remaining_secs'].max()
        min_time = mdf['time_remaining_secs'].min()
        duration = max_time - min_time

        if duration < MIN_RUNTIME_SECS:
            continue

        # Check we have data from start (within first 60s)
        if max_time < 840:  # Need at least from 14:00 mark
            continue

        valid_slugs.append(slug)

    df = df[df['market_slug'].isin(valid_slugs)].copy()
    print(f"Valid markets: {len(valid_slugs)} ({len(df):,} rows)")

    return df


# =============================================================================
# SPIKE DETECTION
# =============================================================================

def detect_spike_from_hf(prices: List[float], lookback: int = 3, threshold: float = 0.02) -> Tuple[Optional[str], float]:
    """
    Detect spike from high-frequency BTC prices.

    Args:
        prices: Recent BTC prices (newest last)
        lookback: Number of ticks to look back
        threshold: Minimum % change for spike

    Returns:
        (direction, magnitude) or (None, 0)
    """
    if len(prices) < lookback + 1:
        return None, 0.0

    old_price = prices[-(lookback + 1)]
    new_price = prices[-1]

    if old_price <= 0:
        return None, 0.0

    change_pct = (new_price - old_price) / old_price * 100

    if abs(change_pct) >= threshold:
        direction = "UP" if change_pct > 0 else "DOWN"
        return direction, abs(change_pct)

    return None, 0.0


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
    spike_score = min(spike_mag / 0.05, 1.0)  # 0-5% range
    velocity_score = min(abs(velocity_bps) / 0.50, 1.0)

    # Confirmation bonus
    vel_confirms = (spike_dir == "UP" and velocity_bps > 0) or \
                   (spike_dir == "DOWN" and velocity_bps < 0)
    confirm_bonus = 1.0 if vel_confirms else 0.0

    # Urgency (higher score near end)
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
    """
    Realistic fill check: ask must cross through our bid.

    Returns True if we would get filled.
    """
    return prev_ask > our_bid and curr_ask <= our_bid


def calculate_loser_bid(entry_price: float, spike_magnitude: float, regime: str = "MEDIUM") -> float:
    """Calculate loser bid based on magnitude (v2 formula - recalibrated Jan 18, 2026)."""
    DROP_MULTIPLIER = 0.50
    DROP_INTERCEPT = 0.08
    DROP_REGIME_BONUS = {'LOW': 0.0, 'MEDIUM': 0.01, 'HIGH': 0.02}
    regime_bonus = DROP_REGIME_BONUS.get(regime, 0.01)
    expected_drop = DROP_MULTIPLIER * spike_magnitude / 100 + DROP_INTERCEPT + regime_bonus
    expected_drop = max(0.02, min(0.20, expected_drop))
    loser_bid = (1.0 - entry_price) - expected_drop
    return max(0.01, min(0.95, loser_bid))


# =============================================================================
# BACKTEST SIMULATION
# =============================================================================

def simulate_market(mdf: pd.DataFrame, slug: str, resolution: str,
                    stop_loss_pct: Optional[float], enable_cycling: bool,
                    signal_type: str = "enhanced") -> List[TradeResult]:
    """
    Simulate trading on a single market.

    Args:
        mdf: Market dataframe (sorted by time_remaining desc)
        slug: Market slug
        resolution: "UP" or "DOWN"
        stop_loss_pct: Stop-loss threshold (None = no stop-loss)
        enable_cycling: Whether to allow multiple entries
        signal_type: "spike", "velocity", or "enhanced"

    Returns:
        List of trade results
    """
    mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)
    trades = []

    # State
    i = 0
    cycle_num = 0
    btc_prices = []  # Rolling window for spike detection

    while i < len(mdf):
        row = mdf.iloc[i]
        time_rem = row['time_remaining_secs']

        # Skip if too close to end
        if time_rem < MIN_TIME:
            break

        # Update BTC price window
        btc_prices.append(row['btc_price_hf'])
        if len(btc_prices) > 20:
            btc_prices.pop(0)

        # Get velocity
        velocity_bps = row.get('velocity_bps', 0.0)

        # Detect signal based on type
        signal_dir = None
        signal_score = 0.0
        spike_mag = 0.0

        if signal_type == "spike":
            spike_dir, spike_mag = detect_spike_from_hf(btc_prices, SPIKE_LOOKBACK, SPIKE_THRESHOLD)
            if spike_dir:
                signal_dir = spike_dir
                signal_score = spike_mag

        elif signal_type == "velocity":
            if abs(velocity_bps) >= 0.10:
                signal_dir = "UP" if velocity_bps > 0 else "DOWN"
                signal_score = abs(velocity_bps)

        elif signal_type == "enhanced":
            spike_dir, spike_mag = detect_spike_from_hf(btc_prices, SPIKE_LOOKBACK, SPIKE_THRESHOLD)
            if spike_dir:
                # Check velocity confirmation
                if velocity_confirms_spike(spike_dir, velocity_bps):
                    score = compute_enhanced_score(spike_mag, velocity_bps, spike_dir, time_rem)
                    if score >= 0.40:
                        signal_dir = spike_dir
                        signal_score = score

        # No signal, continue
        if signal_dir is None:
            i += 1
            continue

        # ENTRY SIGNAL DETECTED
        cycle_num += 1
        winner_side = signal_dir
        loser_side = "DOWN" if winner_side == "UP" else "UP"

        # Entry prices (bid + 0.01, capped at ask - 0.01 to match live behavior)
        if winner_side == "UP":
            winner_bid = row['up_bid']
            winner_ask = row['up_ask']
            loser_ask = row['down_ask']
            loser_bid_col = 'down_bid'
        else:
            winner_bid = row['down_bid']
            winner_ask = row['down_ask']
            loser_ask = row['up_ask']
            loser_bid_col = 'up_bid'

        # Match live behavior: bid + 0.01, capped at ask - 0.01
        winner_fill_price = min(winner_bid + 0.01, winner_ask - 0.01)
        winner_fill_price = round(winner_fill_price, 2)
        winner_fill_price = max(0.01, min(0.95, winner_fill_price))

        # Calculate loser bid target
        loser_target_bid = calculate_loser_bid(winner_fill_price, spike_mag if spike_mag > 0 else 2.0)

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

            # Check passive fill (realistic: ask crosses through our bid)
            if check_passive_fill(prev_ask, curr_ask, loser_target_bid):
                loser_filled = True
                loser_fill_price = loser_target_bid
                hedge_type = "passive"
                samples_to_hedge = j - entry_idx
                i = j + MIN_CYCLE_GAP_SAMPLES if enable_cycling else len(mdf)
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
                    loser_fill_price = curr_ask  # Market order at ask
                    hedge_type = "stoploss"
                    samples_to_hedge = j - entry_idx
                    i = j + MIN_CYCLE_GAP_SAMPLES if enable_cycling else len(mdf)
                    break

        # If not filled, resolve at market end
        if not loser_filled:
            if resolution == winner_side:
                # Winner wins - unhedged profit
                loser_fill_price = 0.0  # Loser expires worthless
                hedge_type = "resolution"
            else:
                # Winner loses - unhedged loss
                loser_fill_price = 1.0  # Would have needed to pay $1
                hedge_type = "resolution"
            samples_to_hedge = len(mdf) - entry_idx
            i = len(mdf)

        # Calculate P&L
        pair_cost = winner_fill_price + loser_fill_price
        if resolution == winner_side:
            pnl = 1.0 - pair_cost  # Winner pays $1
        else:
            pnl = loser_fill_price - pair_cost if loser_fill_price > 0 else -winner_fill_price

        # Adjust for unhedged resolution
        if hedge_type == "resolution":
            if resolution == winner_side:
                pnl = 1.0 - winner_fill_price  # Full win (no hedge cost)
            else:
                pnl = -winner_fill_price  # Full loss

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
                 enable_cycling: bool) -> BacktestResult:
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

    # Calculate metrics
    total_pnl = sum(t.pnl for t in all_trades)
    total_trades = len(all_trades)

    # Hours of data
    total_samples = len(df)
    hours = total_samples / (5 * 3600)  # 5 Hz sampling
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

def load_combined_data(binance_path: str, resolutions_path: str) -> pd.DataFrame:
    """Load and combine all observer data with Binance prices."""
    from pathlib import Path

    # Load all observer files
    observer_dir = Path("research/observer")
    obs_files = sorted(observer_dir.glob("grid_obs_*.csv"))

    dfs = []
    for f in obs_files:
        if 'aws' not in f.name:  # Skip aws backup files
            df = pd.read_csv(f)
            print(f"  {f.name}: {len(df):,} rows")
            dfs.append(df)

    obs_df = pd.concat(dfs, ignore_index=True)
    print(f"Combined observer: {len(obs_df):,} rows")

    # Load Binance data
    btc_df = pd.read_csv(binance_path)
    print(f"Binance: {len(btc_df):,} rows")

    # Load resolutions
    res_df = pd.read_csv(resolutions_path)

    # Find overlap
    overlap_start = max(obs_df['timestamp_ms'].min(), btc_df['timestamp_ms'].min())
    overlap_end = min(obs_df['timestamp_ms'].max(), btc_df['timestamp_ms'].max())

    obs_df = obs_df[(obs_df['timestamp_ms'] >= overlap_start) &
                     (obs_df['timestamp_ms'] <= overlap_end)].copy()
    btc_df = btc_df[(btc_df['timestamp_ms'] >= overlap_start) &
                     (btc_df['timestamp_ms'] <= overlap_end)].copy()

    print(f"After overlap: {len(obs_df):,} observer, {len(btc_df):,} binance")

    # Merge
    obs_df = obs_df.sort_values('timestamp_ms').reset_index(drop=True)
    btc_df = btc_df.sort_values('timestamp_ms').reset_index(drop=True)

    obs_df = pd.merge_asof(
        obs_df,
        btc_df[['timestamp_ms', 'price']].rename(columns={'price': 'btc_price_hf'}),
        on='timestamp_ms',
        direction='nearest',
        tolerance=1000
    )
    obs_df = obs_df.dropna(subset=['btc_price_hf'])

    # Add resolutions
    res_map = dict(zip(res_df['slug'], res_df['winner']))
    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    print(f"With resolutions: {len(obs_df):,} rows, {obs_df['market_slug'].nunique()} markets")

    return obs_df


def main():
    import sys

    # OOS2 time filtering (Jan 18 15:00 - Jan 19 17:11 UTC)
    OOS2_START = 1768748400000
    OOS2_END = 1768842660000

    # Check for --oos2 flag
    oos2_mode = "--oos2" in sys.argv

    print("=" * 70)
    if oos2_mode:
        print("ENHANCED SPIKE STRATEGY BACKTEST - OOS2 ONLY")
        from datetime import datetime
        print(f"Time Range: {datetime.utcfromtimestamp(OOS2_START/1000)} to {datetime.utcfromtimestamp(OOS2_END/1000)} UTC")
    else:
        print("ENHANCED SPIKE STRATEGY BACKTEST")
    print("=" * 70)
    print()

    # Load combined data
    binance_path = "research/binance_hf/btc_prices_combined.csv"
    resolutions_path = "research/observer/market_resolutions_verified.csv"

    df = load_combined_data(binance_path, resolutions_path)

    # Apply OOS2 filter if requested
    if oos2_mode:
        before_count = len(df)
        df = df[(df['timestamp_ms'] >= OOS2_START) & (df['timestamp_ms'] <= OOS2_END)]
        print(f"OOS2 filter: {before_count:,} -> {len(df):,} rows")

    df = filter_valid_markets(df)

    # Calculate hours
    hours = len(df) / (5 * 3600)
    print(f"\nTotal data: {hours:.2f} hours")
    print()

    # Run all combinations
    results = []

    signal_types = ["velocity", "spike", "enhanced"]
    cycling_options = [False, True]

    print("Running backtests...")
    print("-" * 70)

    for signal_type in signal_types:
        for cycling in cycling_options:
            for stop_loss in STOP_LOSS_OPTIONS:
                result = run_backtest(df, signal_type, stop_loss, cycling)
                results.append(result)

                sl_str = f"{stop_loss*100:.0f}%" if stop_loss else "None"
                cyc_str = "ON" if cycling else "OFF"
                print(f"  {signal_type:10} | SL={sl_str:5} | Cycling={cyc_str:3} | "
                      f"Trades={result.total_trades:4} | PnL=${result.total_pnl:7.2f} | "
                      f"$/hr=${result.hourly_rate:6.2f}")

    # Summary table
    print()
    print("=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)
    print()
    print(f"{'Strategy':<12} {'SL':<6} {'Cycle':<6} {'Trades':<7} {'PnL':>9} {'$/hr':>8} {'Win%':>7} {'Pair$':>7} {'Passive':>8} {'SL%':>6} {'Res%':>6}")
    print("-" * 100)

    # Sort by hourly rate
    results.sort(key=lambda x: x.hourly_rate, reverse=True)

    for r in results:
        sl_str = f"{r.stop_loss_pct*100:.0f}%" if r.stop_loss_pct else "None"
        cyc_str = "ON" if r.cycling else "OFF"
        print(f"{r.strategy_name:<12} {sl_str:<6} {cyc_str:<6} {r.total_trades:<7} "
              f"${r.total_pnl:>7.2f} ${r.hourly_rate:>7.2f} {r.win_rate:>6.1%} "
              f"${r.avg_pair_cost:>6.3f} {r.passive_hedge_pct:>7.1%} "
              f"{r.stoploss_hedge_pct:>5.1%} {r.resolution_pct:>5.1%}")

    # Best result
    best = results[0]
    print()
    print("=" * 70)
    print(f"BEST: {best.strategy_name} (SL={best.stop_loss_pct}, Cycling={'ON' if best.cycling else 'OFF'})")
    print(f"  Hourly rate: ${best.hourly_rate:.2f}/hr")
    print(f"  Total P&L:   ${best.total_pnl:.2f}")
    print(f"  Win rate:    {best.win_rate:.1%}")
    print(f"  Avg pair:    ${best.avg_pair_cost:.3f}")
    print("=" * 70)


if __name__ == "__main__":
    main()
