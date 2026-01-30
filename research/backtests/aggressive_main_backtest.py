#!/usr/bin/env python3
"""
AGGRESSIVE Mode Main Backtest - 60Hz HF Data

Uses 60Hz Binance HF data for spike detection (matching live strategy).
Merges with observer orderbook data for fill simulation.

This is the ACCURATE backtest matching the live EnhancedSpikeStrategy (AGGRESSIVE mode).

Usage:
    python research/backtests/aggressive_main_backtest.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from collections import deque

# =============================================================================
# CONFIGURATION (matching live strategy)
# =============================================================================

TARGET_SHARES = 15
MIN_TIME = 60  # Entry cutoff (seconds remaining)
MIN_RUNTIME_SECS = 300  # 5 minutes minimum market duration

# Stop-loss options to test
STOP_LOSS_OPTIONS = [None, 0.05, 0.07, 0.10]

# Spike detection at 60Hz data - CANONICAL from TRADING_CONFIGS.py (Jan 27, 2026)
# Source of truth: research/reference/TRADING_CONFIGS.py AGGRESSIVE config
# lookback_ticks=72, lookback_ms=1200 (validated ~$9.00/hr @ 50 shares)
SPIKE_LOOKBACK = 72  # 72 ticks at 60Hz = 1200ms (CANONICAL)
SPIKE_THRESHOLD = 0.02  # 0.02% minimum spike

# Enhanced signal filtering
VELOCITY_CONFIRM_THRESHOLD = 0.10
ENHANCED_SCORE_THRESHOLD = 0.40

# Cycling
MIN_CYCLE_GAP_SECS = 1.0

# Loser bid calculation
DROP_MULTIPLIER = 0.68
DROP_INTERCEPT = 0.01
TARGET_PAIR_COST = 0.99

# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class TradeResult:
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
    spike_magnitude: float


@dataclass
class BacktestResult:
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
    direction_accuracy: float
    trades: List[TradeResult] = field(default_factory=list)


# =============================================================================
# SPIKE DETECTION (60Hz - matching live strategy)
# =============================================================================

class SpikeDetector:
    """60Hz spike detection matching EnhancedSpikeStrategy."""

    def __init__(self, lookback: int = SPIKE_LOOKBACK, threshold: float = SPIKE_THRESHOLD):
        self.lookback = lookback
        self.threshold = threshold
        self.price_history = deque(maxlen=50)

    def detect(self, price: float) -> Tuple[Optional[str], float]:
        """
        Detect spike from 60Hz price data.

        Returns: (direction, magnitude_pct) or (None, 0)
        """
        self.price_history.append(price)

        if len(self.price_history) < self.lookback + 1:
            return None, 0.0

        current = self.price_history[-1]
        previous = self.price_history[-(self.lookback + 1)]

        if previous <= 0:
            return None, 0.0

        change_pct = (current - previous) / previous * 100
        magnitude = abs(change_pct)

        if magnitude >= self.threshold:
            direction = "UP" if change_pct > 0 else "DOWN"
            return direction, magnitude

        return None, 0.0

    def reset(self):
        self.price_history.clear()


def velocity_confirms_spike(spike_dir: str, velocity_bps: float) -> bool:
    """Check if velocity confirms spike direction."""
    if spike_dir == "UP":
        return velocity_bps > -VELOCITY_CONFIRM_THRESHOLD
    elif spike_dir == "DOWN":
        return velocity_bps < VELOCITY_CONFIRM_THRESHOLD
    return True


def obi_confirms_spike(spike_dir: str, up_imbalance: Optional[float],
                       down_imbalance: Optional[float]) -> bool:
    """
    Check if Order Book Imbalance confirms spike direction.

    OBI CONFIRMATION FILTER (January 28, 2026):
    When OBI confirms spike direction: 89% accuracy vs 77% when disagrees (+4.1pp)

    Args:
        spike_dir: "UP" or "DOWN"
        up_imbalance: OBI for UP token (-1 to +1, positive = buying pressure)
        down_imbalance: OBI for DOWN token (-1 to +1, positive = buying pressure)

    Returns:
        True if OBI confirms or is unavailable, False if OBI disagrees
    """
    if spike_dir == "UP" and up_imbalance is not None:
        # UP spike needs positive UP imbalance (buying pressure on UP)
        return up_imbalance > 0
    elif spike_dir == "DOWN" and down_imbalance is not None:
        # DOWN spike needs positive DOWN imbalance (buying pressure on DOWN)
        return down_imbalance > 0
    # If imbalance not available, don't filter
    return True


def compute_enhanced_score(spike_mag: float, velocity_bps: float,
                           spike_dir: str, time_remaining: float) -> float:
    """
    Compute composite score (matching live strategy).

    Formula: 0.40*spike + 0.30*velocity + 0.20*confirmation + 0.10*urgency
    """
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


def calculate_loser_bid(winner_entry: float, spike_magnitude: float) -> float:
    """
    Calculate loser bid based on spike magnitude.

    Args:
        winner_entry: Price paid for winner side
        spike_magnitude: BTC % change (e.g., 0.05 for 0.05%) - NOT divided by 100!
    """
    # FIX: Do NOT divide by 100 - magnitude is already in percentage (0.05 = 0.05%)
    # Matches strategy code: enhanced_spike.py:526
    expected_drop = DROP_MULTIPLIER * spike_magnitude + DROP_INTERCEPT
    max_loser = TARGET_PAIR_COST - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid))


# =============================================================================
# DATA LOADING
# =============================================================================

def load_and_merge_data():
    """
    Load 60Hz Binance data and merge with observer orderbook data.
    """
    print("Loading 60Hz Binance HF data...")
    btc_path = Path("research/binance_hf/btc_prices_20260116_194712.csv")
    btc_df = pd.read_csv(btc_path)
    print(f"  Binance rows: {len(btc_df):,}")

    print("\nLoading observer data...")
    obs_dir = Path("research/observer")
    obs_dfs = []
    for f in sorted(obs_dir.glob("grid_obs_*.csv")):
        df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
        obs_dfs.append(df)
        print(f"  {f.name}: {len(df):,} rows")

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    print(f"  Combined observer: {len(obs_df):,} rows")

    print("\nLoading resolutions...")
    res_df = pd.read_csv("research/observer/market_resolutions_verified.csv")
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Find overlap period
    btc_start, btc_end = btc_df['timestamp_ms'].min(), btc_df['timestamp_ms'].max()
    obs_start, obs_end = obs_df['timestamp_ms'].min(), obs_df['timestamp_ms'].max()

    overlap_start = max(btc_start, obs_start)
    overlap_end = min(btc_end, obs_end)

    duration_hours = (overlap_end - overlap_start) / 3600000
    print(f"\nOverlap period: {duration_hours:.2f} hours")

    # Filter to overlap
    btc_df = btc_df[(btc_df['timestamp_ms'] >= overlap_start) &
                     (btc_df['timestamp_ms'] <= overlap_end)].copy()
    obs_df = obs_df[(obs_df['timestamp_ms'] >= overlap_start) &
                     (obs_df['timestamp_ms'] <= overlap_end)].copy()

    # Add resolutions to observer data
    obs_df['resolution'] = obs_df['market_slug'].map(res_map)
    obs_df = obs_df[obs_df['resolution'].isin(['UP', 'DOWN'])]

    # Filter valid markets
    valid_slugs = []
    for slug, mdf in obs_df.groupby('market_slug'):
        max_time = mdf['time_remaining_secs'].max()
        min_time = mdf['time_remaining_secs'].min()
        duration = max_time - min_time
        if duration >= MIN_RUNTIME_SECS and max_time >= 840:
            valid_slugs.append(slug)

    obs_df = obs_df[obs_df['market_slug'].isin(valid_slugs)]

    print(f"Valid markets: {len(valid_slugs)}")
    print(f"Observer rows in overlap: {len(obs_df):,}")
    print(f"Binance rows in overlap: {len(btc_df):,}")

    return btc_df, obs_df, duration_hours


# =============================================================================
# BACKTEST SIMULATION
# =============================================================================

def simulate_market_60hz(btc_df: pd.DataFrame, obs_df: pd.DataFrame,
                         slug: str, resolution: str,
                         stop_loss_pct: Optional[float], enable_cycling: bool,
                         signal_type: str = "enhanced") -> List[TradeResult]:
    """
    Simulate trading with 60Hz spike detection.
    """
    # Get market data
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    # Get Binance data for this market's time range
    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    btc_market = btc_df[(btc_df['timestamp_ms'] >= market_start - 1000) &
                        (btc_df['timestamp_ms'] <= market_end + 1000)].copy()
    btc_market = btc_market.sort_values('timestamp_ms').reset_index(drop=True)

    if len(btc_market) == 0:
        return []

    trades = []
    detector = SpikeDetector()

    cycle_num = 0
    last_trade_ts = 0
    in_position = False
    position_data = None

    # Process each Binance tick
    btc_idx = 0
    obs_idx = 0

    while btc_idx < len(btc_market):
        btc_row = btc_market.iloc[btc_idx]
        btc_ts = btc_row['timestamp_ms']
        btc_price = btc_row['price']

        # Find nearest observer row
        while obs_idx < len(mdf) - 1 and mdf.iloc[obs_idx + 1]['timestamp_ms'] <= btc_ts:
            obs_idx += 1

        if obs_idx >= len(mdf):
            break

        obs_row = mdf.iloc[obs_idx]
        time_rem = obs_row['time_remaining_secs']
        velocity_bps = obs_row.get('velocity_bps', 0.0) or 0.0

        # Skip if too close to end
        if time_rem < MIN_TIME:
            btc_idx += 1
            continue

        # If in position, check for hedge/stop-loss
        if in_position and position_data is not None:
            winner_side = position_data['winner_side']
            loser_side = position_data['loser_side']
            winner_entry = position_data['winner_entry']
            loser_target = position_data['loser_target']
            entry_ts = position_data['entry_ts']
            spike_mag = position_data['spike_magnitude']
            score = position_data['score']

            # Get current prices
            if loser_side == "UP":
                loser_ask = obs_row['up_ask']
                winner_bid = obs_row['down_bid']
            else:
                loser_ask = obs_row['down_ask']
                winner_bid = obs_row['up_bid']

            # Check passive fill (ask crosses through our bid)
            if loser_ask <= loser_target:
                # PASSIVE HEDGE FILL
                loser_fill = loser_target
                pair_cost = winner_entry + loser_fill
                pnl = (1.0 - pair_cost) * TARGET_SHARES

                trades.append(TradeResult(
                    market_slug=slug,
                    cycle_num=cycle_num,
                    entry_time_remaining=position_data['entry_time_rem'],
                    signal_type=signal_type,
                    signal_score=score,
                    winner_side=winner_side,
                    winner_fill_price=winner_entry,
                    loser_fill_price=loser_fill,
                    hedge_type="passive",
                    pair_cost=pair_cost,
                    pnl=pnl,
                    correct_direction=(resolution == winner_side),
                    spike_magnitude=spike_mag,
                ))

                in_position = False
                position_data = None
                last_trade_ts = btc_ts

                if not enable_cycling:
                    break

                detector.reset()
                btc_idx += 1
                continue

            # Check stop-loss
            if stop_loss_pct is not None:
                drop_pct = (winner_entry - winner_bid) / winner_entry
                if drop_pct >= stop_loss_pct:
                    # STOP-LOSS HEDGE
                    loser_fill = loser_ask  # Market order at ask
                    pair_cost = winner_entry + loser_fill
                    pnl = (1.0 - pair_cost) * TARGET_SHARES

                    trades.append(TradeResult(
                        market_slug=slug,
                        cycle_num=cycle_num,
                        entry_time_remaining=position_data['entry_time_rem'],
                        signal_type=signal_type,
                        signal_score=score,
                        winner_side=winner_side,
                        winner_fill_price=winner_entry,
                        loser_fill_price=loser_fill,
                        hedge_type="stoploss",
                        pair_cost=pair_cost,
                        pnl=pnl,
                        correct_direction=(resolution == winner_side),
                        spike_magnitude=spike_mag,
                    ))

                    in_position = False
                    position_data = None
                    last_trade_ts = btc_ts

                    if not enable_cycling:
                        break

                    detector.reset()
                    btc_idx += 1
                    continue

            btc_idx += 1
            continue

        # Not in position - look for entry signal
        # Enforce minimum gap between trades
        if enable_cycling and (btc_ts - last_trade_ts) < MIN_CYCLE_GAP_SECS * 1000:
            btc_idx += 1
            continue

        # Detect spike at 60Hz
        spike_dir, spike_mag = detector.detect(btc_price)

        signal_dir = None
        signal_score = 0.0

        if signal_type == "spike":
            if spike_dir is not None:
                signal_dir = spike_dir
                signal_score = spike_mag

        elif signal_type == "velocity":
            if abs(velocity_bps) >= 0.10:
                signal_dir = "UP" if velocity_bps > 0 else "DOWN"
                signal_score = abs(velocity_bps)

        elif signal_type == "enhanced":
            if spike_dir is not None:
                if velocity_confirms_spike(spike_dir, velocity_bps):
                    # OBI CONFIRMATION FILTER (Jan 28, 2026): +4.1pp accuracy
                    up_imbalance = obs_row.get('up_imbalance', None)
                    down_imbalance = obs_row.get('down_imbalance', None)
                    if obi_confirms_spike(spike_dir, up_imbalance, down_imbalance):
                        score = compute_enhanced_score(spike_mag, velocity_bps, spike_dir, time_rem)
                        if score >= ENHANCED_SCORE_THRESHOLD:
                            signal_dir = spike_dir
                            signal_score = score

        if signal_dir is not None:
            # ENTRY SIGNAL
            cycle_num += 1
            winner_side = signal_dir
            loser_side = "DOWN" if winner_side == "UP" else "UP"

            # Entry at ask
            if winner_side == "UP":
                winner_entry = obs_row['up_ask']
            else:
                winner_entry = obs_row['down_ask']

            # Calculate loser bid target
            loser_target = calculate_loser_bid(winner_entry, spike_mag if spike_mag > 0 else 0.02)

            in_position = True
            position_data = {
                'winner_side': winner_side,
                'loser_side': loser_side,
                'winner_entry': winner_entry,
                'loser_target': loser_target,
                'entry_ts': btc_ts,
                'entry_time_rem': time_rem,
                'spike_magnitude': spike_mag,
                'score': signal_score,
            }

        btc_idx += 1

    # Handle unresolved position at market end
    if in_position and position_data is not None:
        winner_side = position_data['winner_side']
        winner_entry = position_data['winner_entry']
        spike_mag = position_data['spike_magnitude']
        score = position_data['score']

        if resolution == winner_side:
            # Winner wins - unhedged profit
            pnl = (1.0 - winner_entry) * TARGET_SHARES
            loser_fill = 0.0
        else:
            # Winner loses - unhedged loss
            pnl = -winner_entry * TARGET_SHARES
            loser_fill = 1.0

        trades.append(TradeResult(
            market_slug=slug,
            cycle_num=cycle_num,
            entry_time_remaining=position_data['entry_time_rem'],
            signal_type=signal_type,
            signal_score=score,
            winner_side=winner_side,
            winner_fill_price=winner_entry,
            loser_fill_price=loser_fill,
            hedge_type="resolution",
            pair_cost=winner_entry + loser_fill,
            pnl=pnl,
            correct_direction=(resolution == winner_side),
            spike_magnitude=spike_mag,
        ))

    return trades


def run_backtest(btc_df: pd.DataFrame, obs_df: pd.DataFrame,
                 signal_type: str, stop_loss_pct: Optional[float],
                 enable_cycling: bool, hours: float) -> BacktestResult:
    """Run backtest across all markets."""
    all_trades = []

    for slug in obs_df['market_slug'].unique():
        mdf = obs_df[obs_df['market_slug'] == slug]
        resolution = mdf['resolution'].iloc[0]
        trades = simulate_market_60hz(btc_df, mdf, slug, resolution,
                                      stop_loss_pct, enable_cycling, signal_type)
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
            direction_accuracy=0,
        )

    total_pnl = sum(t.pnl for t in all_trades)
    total_trades = len(all_trades)
    hourly_rate = total_pnl / hours if hours > 0 else 0

    wins = sum(1 for t in all_trades if t.pnl > 0)
    win_rate = wins / total_trades

    hedged = [t for t in all_trades if t.hedge_type != "resolution"]
    avg_pair_cost = np.mean([t.pair_cost for t in hedged]) if hedged else 0

    passive = sum(1 for t in all_trades if t.hedge_type == "passive")
    stoploss = sum(1 for t in all_trades if t.hedge_type == "stoploss")
    resolution = sum(1 for t in all_trades if t.hedge_type == "resolution")

    correct = sum(1 for t in all_trades if t.correct_direction)
    direction_accuracy = correct / total_trades

    return BacktestResult(
        strategy_name=signal_type,
        stop_loss_pct=stop_loss_pct,
        cycling=enable_cycling,
        total_trades=total_trades,
        total_pnl=total_pnl,
        hourly_rate=hourly_rate,
        win_rate=win_rate,
        avg_pair_cost=avg_pair_cost,
        passive_hedge_pct=passive / total_trades,
        stoploss_hedge_pct=stoploss / total_trades,
        resolution_pct=resolution / total_trades,
        direction_accuracy=direction_accuracy,
        trades=all_trades,
    )


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 80)
    print("ENHANCED SPIKE BACKTEST - 60Hz HF DATA (Matching Live Strategy)")
    print("=" * 80)
    print()

    # Load data
    btc_df, obs_df, hours = load_and_merge_data()

    print(f"\nBacktest period: {hours:.2f} hours")
    print(f"Markets: {obs_df['market_slug'].nunique()}")
    print()

    # Run backtests
    results = []
    signal_types = ["velocity", "spike", "enhanced"]
    cycling_options = [False, True]

    print("Running backtests (60Hz spike detection)...")
    print("-" * 80)

    for signal_type in signal_types:
        for cycling in cycling_options:
            for stop_loss in STOP_LOSS_OPTIONS:
                result = run_backtest(btc_df, obs_df, signal_type, stop_loss, cycling, hours)
                results.append(result)

                sl_str = f"{stop_loss*100:.0f}%" if stop_loss else "None"
                cyc_str = "ON" if cycling else "OFF"
                print(f"  {signal_type:10} | SL={sl_str:5} | Cycling={cyc_str:3} | "
                      f"Trades={result.total_trades:4} | PnL=${result.total_pnl:8.2f} | "
                      f"$/hr=${result.hourly_rate:7.2f} | Acc={result.direction_accuracy:.1%}")

    # Summary
    print()
    print("=" * 80)
    print("RESULTS SUMMARY (sorted by $/hr)")
    print("=" * 80)
    print()
    print(f"{'Strategy':<12} {'SL':<6} {'Cycle':<6} {'Trades':<7} {'PnL':>10} {'$/hr':>9} "
          f"{'Win%':>7} {'Acc%':>7} {'Pass%':>7} {'SL%':>6} {'Res%':>6}")
    print("-" * 110)

    results.sort(key=lambda x: x.hourly_rate, reverse=True)

    for r in results:
        sl_str = f"{r.stop_loss_pct*100:.0f}%" if r.stop_loss_pct else "None"
        cyc_str = "ON" if r.cycling else "OFF"
        print(f"{r.strategy_name:<12} {sl_str:<6} {cyc_str:<6} {r.total_trades:<7} "
              f"${r.total_pnl:>8.2f} ${r.hourly_rate:>8.2f} {r.win_rate:>6.1%} "
              f"{r.direction_accuracy:>6.1%} {r.passive_hedge_pct:>6.1%} "
              f"{r.stoploss_hedge_pct:>5.1%} {r.resolution_pct:>5.1%}")

    # Top 5
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
        print(f"    Direction accuracy: {r.direction_accuracy:.1%}")
        print(f"    Win rate:    {r.win_rate:.1%}")
        print(f"    Avg pair:    ${r.avg_pair_cost:.3f}")

    # Comparison to plan
    print()
    print("=" * 80)
    print("COMPARISON TO PLAN (happy-sauteeing-dewdrop.md)")
    print("=" * 80)
    print()
    print("Plan expectations (8.19h dataset):")
    print("  - Enhanced Signal: $7.54/hr")
    print("  - Spike Raw:       $7.03/hr")
    print("  - Velocity:        $2.37/hr")
    print()
    print(f"Actual results ({hours:.1f}h dataset, 60Hz detection):")

    for signal_type in ["enhanced", "spike", "velocity"]:
        # Best config for each type
        matches = [r for r in results if r.strategy_name == signal_type]
        if matches:
            best = max(matches, key=lambda x: x.hourly_rate)
            sl_str = f"{best.stop_loss_pct*100:.0f}%" if best.stop_loss_pct else "None"
            print(f"  - {signal_type:10}: ${best.hourly_rate:.2f}/hr "
                  f"(SL={sl_str}, Cycling={'ON' if best.cycling else 'OFF'}, "
                  f"{best.total_trades} trades, {best.direction_accuracy:.1%} accuracy)")

    print()
    print("=" * 80)


if __name__ == "__main__":
    main()
