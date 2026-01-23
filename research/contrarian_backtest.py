#!/usr/bin/env python3
"""
Contrarian Mean-Reversion Backtest

Replicates the strategy of wallet 0xa5e8...95f5:
- BTC 15-minute binary markets
- Wait 5-7 min after window opens
- When BTC moves, buy the OPPOSITE side (contrarian)
- Entry signal: Z-score of BTC move from window open exceeds threshold
- Hold to resolution (end of 15-min window)
- Profit from asymmetric payoff (buy cheap side at ~$0.30, win $1.00)

Data: research/binance_hf/btc_prices_combined.csv (9ms tick data)
Split: Training before 1768705387229, OOS2 after
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from pathlib import Path
from scipy.stats import norm
import json

# =============================================================================
# CONFIGURATION
# =============================================================================

DATA_FILE = Path(__file__).parent / "binance_hf" / "btc_prices_combined.csv"
SPLIT_TIMESTAMP_MS = 1768705387229  # Jan 18, 03:03:07 UTC

WINDOW_DURATION_S = 900  # 15 minutes in seconds
RESAMPLE_INTERVAL_S = 1  # Resample to 1-second bars for efficiency

# Strategy parameters to sweep
Z_SCORE_THRESHOLDS = [0.5, 0.7, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5]
MIN_ENTRY_DELAYS_S = [60, 120, 180, 300, 360, 420]  # Min seconds before entry
MAX_ENTRY_TIME_S = 780  # Max 13 min into window (must leave some time)

# Position sizing (matches observed wallet behavior)
SHARES_PER_TRADE = 2500  # Avg observed: 2576

# Minimum BTC move (%) to consider entry (filter noise)
MIN_BTC_MOVE_PCT = 0.01  # 0.01% minimum move


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class ContrарianTrade:
    """Single contrarian trade result."""
    window_start_ms: int
    entry_time_s: float  # Seconds into window
    btc_open: float
    btc_at_entry: float
    btc_at_close: float
    btc_move_at_entry_pct: float  # BTC move from open at entry time
    z_score_at_entry: float
    entry_direction: str  # "UP" or "DOWN" (what we bought)
    entry_price: float  # Price paid for contrarian side
    winner: str  # "UP" or "DOWN" at resolution
    pnl: float
    won: bool


@dataclass
class BacktestConfig:
    """Configuration for a single backtest run."""
    z_threshold: float
    min_entry_delay_s: int
    label: str = ""

    def __post_init__(self):
        if not self.label:
            self.label = f"z={self.z_threshold:.1f}_delay={self.min_entry_delay_s}s"


@dataclass
class BacktestResult:
    """Results of a backtest run."""
    config: BacktestConfig
    period: str  # "training" or "oos2"
    total_windows: int
    trades_entered: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    avg_pnl_per_trade: float
    avg_entry_price: float
    avg_z_score: float
    avg_btc_move_pct: float
    pnl_per_hour: float
    max_win: float
    max_loss: float
    trades: List[ContrарianTrade] = field(default_factory=list)


# =============================================================================
# DATA LOADING
# =============================================================================

def load_and_resample(filepath: Path) -> pd.DataFrame:
    """Load tick data and resample to 1-second bars."""
    print(f"Loading data from {filepath}...")
    df = pd.read_csv(filepath)
    print(f"  Loaded {len(df):,} ticks")

    # Resample to 1-second using floor division
    df['second_ms'] = (df['timestamp_ms'] // 1000) * 1000
    resampled = df.groupby('second_ms').agg({
        'price': 'last',  # Close price
        'bid': 'last',
        'ask': 'last',
    }).reset_index()
    resampled.rename(columns={'second_ms': 'timestamp_ms'}, inplace=True)

    print(f"  Resampled to {len(resampled):,} 1-second bars")
    print(f"  Time range: {pd.Timestamp(resampled['timestamp_ms'].iloc[0], unit='ms', tz='UTC')} "
          f"to {pd.Timestamp(resampled['timestamp_ms'].iloc[-1], unit='ms', tz='UTC')}")

    return resampled


def split_data(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Split into training and OOS2 periods."""
    training = df[df['timestamp_ms'] < SPLIT_TIMESTAMP_MS].copy()
    oos2 = df[df['timestamp_ms'] >= SPLIT_TIMESTAMP_MS].copy()

    print(f"\n  Training: {len(training):,} bars "
          f"({pd.Timestamp(training['timestamp_ms'].iloc[0], unit='ms', tz='UTC').strftime('%Y-%m-%d %H:%M')} "
          f"to {pd.Timestamp(training['timestamp_ms'].iloc[-1], unit='ms', tz='UTC').strftime('%Y-%m-%d %H:%M')})")
    print(f"  OOS2:     {len(oos2):,} bars "
          f"({pd.Timestamp(oos2['timestamp_ms'].iloc[0], unit='ms', tz='UTC').strftime('%Y-%m-%d %H:%M')} "
          f"to {pd.Timestamp(oos2['timestamp_ms'].iloc[-1], unit='ms', tz='UTC').strftime('%Y-%m-%d %H:%M')})")

    return training, oos2


# =============================================================================
# PRICE ESTIMATION (Binary option pricing)
# =============================================================================

def estimate_contrarian_price(btc_move_pct: float, time_remaining_s: float,
                               rolling_vol_pct: float) -> float:
    """
    Estimate the Polymarket price of the contrarian (cheap) side.

    Uses Black-Scholes digital option pricing:
    P(UP wins) = Φ(d / (σ√T)) where d = current move, σ = vol, T = time remaining

    The contrarian side price = 1 - P(current direction continues)
    """
    if time_remaining_s <= 0 or rolling_vol_pct <= 0:
        return 0.50

    # Normalize to annualized for the formula, but we work in window-relative terms
    # σ_remaining = rolling_vol * sqrt(time_remaining / window_duration)
    vol_remaining = rolling_vol_pct * np.sqrt(time_remaining_s / WINDOW_DURATION_S)

    if vol_remaining < 1e-8:
        # Very low vol - current direction likely continues
        return 0.10 if abs(btc_move_pct) > 0.01 else 0.50

    # d1 analog: how many "remaining vols" is the current move?
    d = btc_move_pct / vol_remaining

    # P(current direction wins) = Φ(d) for positive move
    p_current_wins = norm.cdf(abs(d))

    # Contrarian side price = 1 - P(current wins)
    contrarian_price = 1.0 - p_current_wins

    # Clamp to realistic range [0.02, 0.98]
    return max(0.02, min(0.98, contrarian_price))


# =============================================================================
# BACKTEST ENGINE
# =============================================================================

def create_windows(df: pd.DataFrame) -> List[Tuple[int, int]]:
    """
    Create 15-minute windows aligned to round timestamps.
    Each window: [start_ms, start_ms + 900_000)
    """
    start_ms = df['timestamp_ms'].iloc[0]
    end_ms = df['timestamp_ms'].iloc[-1]

    # Align to 15-minute boundaries
    window_ms = WINDOW_DURATION_S * 1000
    first_window = (start_ms // window_ms) * window_ms
    if first_window < start_ms:
        first_window += window_ms

    windows = []
    current = first_window
    while current + window_ms <= end_ms:
        windows.append((current, current + window_ms))
        current += window_ms

    return windows


def compute_window_vol(prices: np.ndarray) -> float:
    """Compute % volatility (std of returns) for a price array."""
    if len(prices) < 10:
        return 0.10  # Default
    returns = np.diff(prices) / prices[:-1] * 100
    return max(np.std(returns) * np.sqrt(len(returns)), 0.001)


def run_backtest(df: pd.DataFrame, config: BacktestConfig,
                 period_name: str) -> BacktestResult:
    """
    Run contrarian backtest on a data period.

    For each 15-min window:
    1. Wait min_entry_delay seconds
    2. Compute rolling Z-score of BTC move from window open
    3. If |Z-score| >= threshold, enter contrarian position
    4. Hold to window end (resolution)
    5. Compute PnL
    """
    windows = create_windows(df)
    trades: List[ContrарianTrade] = []

    # Pre-index data for fast lookup
    timestamps = df['timestamp_ms'].values
    prices = df['price'].values

    for win_start, win_end in windows:
        # Get data within this window
        mask = (timestamps >= win_start) & (timestamps < win_end)
        win_idx = np.where(mask)[0]

        if len(win_idx) < 60:  # Need at least 60 seconds of data
            continue

        win_prices = prices[win_idx]
        win_times = timestamps[win_idx]
        btc_open = win_prices[0]

        # Compute window volatility from first 60s (pre-entry calibration)
        first_60s_mask = (win_times - win_start) <= 60000
        first_60s_prices = win_prices[first_60s_mask]
        if len(first_60s_prices) < 10:
            # Use full window vol estimate
            window_vol = compute_window_vol(win_prices[:min(120, len(win_prices))])
        else:
            window_vol = compute_window_vol(first_60s_prices)

        # Also compute running vol for better estimates
        # Use expanding std of returns from window start
        entry_made = False

        for i in range(config.min_entry_delay_s, min(MAX_ENTRY_TIME_S, len(win_idx))):
            if entry_made:
                break

            # Current time into window (seconds, since we have 1s bars)
            time_into_window_s = (win_times[i] - win_start) / 1000.0

            if time_into_window_s < config.min_entry_delay_s:
                continue
            if time_into_window_s > MAX_ENTRY_TIME_S:
                break

            current_price = win_prices[i]
            btc_move_pct = (current_price - btc_open) / btc_open * 100

            # Skip if move too small
            if abs(btc_move_pct) < MIN_BTC_MOVE_PCT:
                continue

            # Compute Z-score: move relative to rolling std
            # Use all prices from window start to current point
            prices_so_far = win_prices[:i+1]
            if len(prices_so_far) < 10:
                continue

            returns_so_far = np.diff(prices_so_far) / prices_so_far[:-1] * 100
            rolling_std = np.std(returns_so_far) * np.sqrt(len(returns_so_far))

            if rolling_std < 1e-8:
                continue

            z_score = abs(btc_move_pct) / rolling_std

            # Entry signal: Z-score exceeds threshold
            if z_score >= config.z_threshold:
                # Determine contrarian direction
                if btc_move_pct > 0:
                    entry_direction = "DOWN"  # BTC up → buy DOWN
                else:
                    entry_direction = "UP"  # BTC down → buy UP

                # Estimate entry price using binary option pricing
                time_remaining_s = WINDOW_DURATION_S - time_into_window_s
                entry_price = estimate_contrarian_price(
                    btc_move_pct, time_remaining_s, rolling_std
                )

                # Resolution: compare close to open
                btc_close = win_prices[-1]
                close_move_pct = (btc_close - btc_open) / btc_open * 100

                if close_move_pct >= 0:
                    winner = "UP"
                else:
                    winner = "DOWN"

                # PnL calculation
                won = (entry_direction == winner)
                if won:
                    pnl = SHARES_PER_TRADE * (1.0 - entry_price)
                else:
                    pnl = -SHARES_PER_TRADE * entry_price

                trade = ContrарianTrade(
                    window_start_ms=win_start,
                    entry_time_s=time_into_window_s,
                    btc_open=btc_open,
                    btc_at_entry=current_price,
                    btc_at_close=btc_close,
                    btc_move_at_entry_pct=btc_move_pct,
                    z_score_at_entry=z_score,
                    entry_direction=entry_direction,
                    entry_price=entry_price,
                    winner=winner,
                    pnl=pnl,
                    won=won,
                )
                trades.append(trade)
                entry_made = True

    # Compute summary stats
    wins = sum(1 for t in trades if t.won)
    losses = len(trades) - wins
    win_rate = wins / len(trades) if trades else 0.0
    total_pnl = sum(t.pnl for t in trades)
    avg_pnl = total_pnl / len(trades) if trades else 0.0
    avg_entry_price = np.mean([t.entry_price for t in trades]) if trades else 0.0
    avg_z = np.mean([t.z_score_at_entry for t in trades]) if trades else 0.0
    avg_move = np.mean([abs(t.btc_move_at_entry_pct) for t in trades]) if trades else 0.0

    # Time span for hourly rate
    if trades:
        time_span_hours = (timestamps[-1] - timestamps[0]) / 1000 / 3600
    else:
        time_span_hours = 1.0
    pnl_per_hour = total_pnl / time_span_hours if time_span_hours > 0 else 0.0

    max_win = max((t.pnl for t in trades if t.won), default=0.0)
    max_loss = min((t.pnl for t in trades if not t.won), default=0.0)

    return BacktestResult(
        config=config,
        period=period_name,
        total_windows=len(windows),
        trades_entered=len(trades),
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        total_pnl=total_pnl,
        avg_pnl_per_trade=avg_pnl,
        avg_entry_price=avg_entry_price,
        avg_z_score=avg_z,
        avg_btc_move_pct=avg_move,
        pnl_per_hour=pnl_per_hour,
        max_win=max_win,
        max_loss=max_loss,
        trades=trades,
    )


# =============================================================================
# REPORTING
# =============================================================================

def print_result(result: BacktestResult):
    """Print a single backtest result."""
    print(f"  {'Win Rate:':<20} {result.win_rate*100:.1f}% ({result.wins}W/{result.losses}L)")
    print(f"  {'Trades/Windows:':<20} {result.trades_entered}/{result.total_windows} "
          f"({result.trades_entered/result.total_windows*100:.0f}% entry rate)")
    print(f"  {'Total PnL:':<20} ${result.total_pnl:,.0f}")
    print(f"  {'PnL/Hour:':<20} ${result.pnl_per_hour:,.0f}")
    print(f"  {'Avg PnL/Trade:':<20} ${result.avg_pnl_per_trade:,.0f}")
    print(f"  {'Avg Entry Price:':<20} ${result.avg_entry_price:.3f}")
    print(f"  {'Avg Z-Score:':<20} {result.avg_z_score:.2f}")
    print(f"  {'Avg BTC Move:':<20} {result.avg_btc_move_pct:.4f}%")
    print(f"  {'Max Win:':<20} ${result.max_win:,.0f}")
    print(f"  {'Max Loss:':<20} ${result.max_loss:,.0f}")


def print_comparison_table(results: List[BacktestResult], period: str):
    """Print comparison table of all configurations."""
    print(f"\n{'='*100}")
    print(f"  PARAMETER SWEEP - {period.upper()}")
    print(f"{'='*100}")
    print(f"{'Config':<25} {'Trades':<8} {'WinRate':<8} {'TotalPnL':<12} "
          f"{'PnL/Hr':<10} {'AvgEntry':<10} {'AvgZ':<8} {'AvgMove%':<10}")
    print(f"{'-'*100}")

    for r in sorted(results, key=lambda x: x.total_pnl, reverse=True):
        print(f"{r.config.label:<25} {r.trades_entered:<8} "
              f"{r.win_rate*100:>5.1f}%  ${r.total_pnl:>9,.0f}  "
              f"${r.pnl_per_hour:>7,.0f}  ${r.avg_entry_price:>6.3f}  "
              f"{r.avg_z_score:>5.2f}  {r.avg_btc_move_pct:>7.4f}%")

    print(f"{'-'*100}")
    best = max(results, key=lambda x: x.total_pnl)
    print(f"  BEST: {best.config.label} → ${best.total_pnl:,.0f} total PnL, "
          f"{best.win_rate*100:.1f}% win rate")


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 70)
    print("  CONTRARIAN MEAN-REVERSION BACKTEST")
    print("  Replicating wallet 0xa5e8...95f5 strategy")
    print("=" * 70)

    # Load and prepare data
    df = load_and_resample(DATA_FILE)
    training, oos2 = split_data(df)

    # Run parameter sweep
    all_training_results = []
    all_oos2_results = []

    # Focused sweep: Z-score thresholds × entry delays
    configs = []
    for z_thresh in Z_SCORE_THRESHOLDS:
        for delay in MIN_ENTRY_DELAYS_S:
            configs.append(BacktestConfig(
                z_threshold=z_thresh,
                min_entry_delay_s=delay,
            ))

    print(f"\n  Running {len(configs)} configurations...")
    print(f"  Z-scores: {Z_SCORE_THRESHOLDS}")
    print(f"  Delays: {MIN_ENTRY_DELAYS_S}")

    for i, config in enumerate(configs):
        # Training
        train_result = run_backtest(training, config, "training")
        all_training_results.append(train_result)

        # OOS2
        oos2_result = run_backtest(oos2, config, "oos2")
        all_oos2_results.append(oos2_result)

        if (i + 1) % 10 == 0:
            print(f"  ... {i+1}/{len(configs)} configs done")

    # Print results
    print_comparison_table(all_training_results, "TRAINING")
    print_comparison_table(all_oos2_results, "OOS2 (Out-of-Sample)")

    # Find best training config and show its OOS2 performance
    best_train = max(all_training_results, key=lambda x: x.total_pnl)
    matching_oos2 = next(
        r for r in all_oos2_results
        if r.config.label == best_train.config.label
    )

    print(f"\n{'='*70}")
    print(f"  BEST TRAINING CONFIG: {best_train.config.label}")
    print(f"{'='*70}")
    print(f"\n  --- Training Performance ---")
    print_result(best_train)
    print(f"\n  --- OOS2 Performance (same config) ---")
    print_result(matching_oos2)

    # Also find best OOS2 independently
    best_oos2 = max(all_oos2_results, key=lambda x: x.total_pnl)
    if best_oos2.config.label != best_train.config.label:
        print(f"\n  --- Best OOS2 Config: {best_oos2.config.label} ---")
        print_result(best_oos2)

    # Robustness check: find configs profitable in BOTH periods
    print(f"\n{'='*70}")
    print(f"  ROBUSTNESS: Configs profitable in BOTH periods")
    print(f"{'='*70}")
    print(f"{'Config':<25} {'Train PnL':<12} {'OOS2 PnL':<12} "
          f"{'Train WR':<10} {'OOS2 WR':<10} {'Combined':<12}")
    print(f"{'-'*85}")

    robust_configs = []
    for tr, os in zip(all_training_results, all_oos2_results):
        if tr.total_pnl > 0 and os.total_pnl > 0:
            combined = tr.total_pnl + os.total_pnl
            robust_configs.append((tr, os, combined))

    robust_configs.sort(key=lambda x: x[2], reverse=True)
    for tr, os, combined in robust_configs[:15]:
        print(f"{tr.config.label:<25} ${tr.total_pnl:>9,.0f}  ${os.total_pnl:>9,.0f}  "
              f"{tr.win_rate*100:>6.1f}%   {os.win_rate*100:>6.1f}%   ${combined:>9,.0f}")

    if not robust_configs:
        print("  NO configs profitable in both periods!")

    # Trade-level detail for best config
    print(f"\n{'='*70}")
    print(f"  TRADE DETAIL (Best Training Config: {best_train.config.label})")
    print(f"{'='*70}")
    if best_train.trades:
        print(f"\n  First 10 trades:")
        for i, t in enumerate(best_train.trades[:10]):
            ts = pd.Timestamp(t.window_start_ms, unit='ms', tz='UTC').strftime('%m-%d %H:%M')
            status = "WIN" if t.won else "LOSS"
            print(f"    {ts} | Entry@{t.entry_time_s:.0f}s | BTC {t.btc_move_at_entry_pct:+.4f}% | "
                  f"Z={t.z_score_at_entry:.2f} | Buy {t.entry_direction} @${t.entry_price:.3f} | "
                  f"{status} | PnL ${t.pnl:+,.0f}")

    # Summary comparison with actual wallet performance
    print(f"\n{'='*70}")
    print(f"  COMPARISON: Backtest vs Actual Wallet 0xa5e8")
    print(f"{'='*70}")
    print(f"  {'Metric':<25} {'Actual Wallet':<18} {'Best Backtest':<18}")
    print(f"  {'-'*60}")
    print(f"  {'Win Rate':<25} {'54.2%':<18} {best_train.win_rate*100:.1f}%")
    print(f"  {'Avg Entry Price':<25} {'$0.30':<18} ${best_train.avg_entry_price:.3f}")
    print(f"  {'Avg Entry Delay':<25} {'329s (5.5min)':<18} {np.mean([t.entry_time_s for t in best_train.trades]):.0f}s")
    print(f"  {'Trades/Hour':<25} {'1.8':<18} {best_train.trades_entered / max(1, (training['timestamp_ms'].iloc[-1] - training['timestamp_ms'].iloc[0]) / 1000 / 3600):.1f}")
    print(f"  {'PnL/Hour':<25} {'$1,222':<18} ${best_train.pnl_per_hour:,.0f}")

    # ==========================================================================
    # REALISTIC POLYMARKET PRICING
    # The BS model gives theoretical prices ($0.05-$0.15), but on Polymarket
    # the contrarian side actually trades at $0.20-$0.40 due to market maker
    # spreads and less efficient pricing. Show PnL at realistic entry levels.
    # ==========================================================================
    print(f"\n{'='*100}")
    print(f"  REALISTIC POLYMARKET PRICING - PnL at different entry price levels")
    print(f"  (Win rates are signal-dependent, only payoff changes with entry price)")
    print(f"{'='*100}")

    fixed_prices = [0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45]

    # Show for top 5 robust configs
    top_configs = robust_configs[:5] if robust_configs else []

    for fixed_p in fixed_prices:
        breakeven_wr = fixed_p  # Breakeven WR = entry_price for binary
        print(f"\n  --- Entry Price: ${fixed_p:.2f} (Breakeven WR: {breakeven_wr*100:.0f}%) ---")
        print(f"  {'Config':<25} {'Period':<10} {'Trades':<8} {'WinRate':<8} "
              f"{'PnL/Trade':<12} {'TotalPnL':<12} {'PnL/Hour':<10}")
        print(f"  {'-'*95}")

        for tr, os, _ in top_configs:
            for result, period in [(tr, "Train"), (os, "OOS2")]:
                n_trades = result.trades_entered
                wins = result.wins
                losses = result.losses
                wr = result.win_rate

                # Recompute PnL at fixed entry price
                pnl_per_win = SHARES_PER_TRADE * (1.0 - fixed_p)
                pnl_per_loss = -SHARES_PER_TRADE * fixed_p
                total_pnl = wins * pnl_per_win + losses * pnl_per_loss
                avg_pnl = total_pnl / n_trades if n_trades else 0

                # Hours
                if period == "Train":
                    hours = (training['timestamp_ms'].iloc[-1] - training['timestamp_ms'].iloc[0]) / 1000 / 3600
                else:
                    hours = (oos2['timestamp_ms'].iloc[-1] - oos2['timestamp_ms'].iloc[0]) / 1000 / 3600
                pnl_hr = total_pnl / max(hours, 1)

                profitable = "✓" if total_pnl > 0 else "✗"
                print(f"  {result.config.label:<25} {period:<10} {n_trades:<8} "
                      f"{wr*100:>5.1f}%  ${avg_pnl:>8,.0f}  ${total_pnl:>9,.0f}  "
                      f"${pnl_hr:>7,.0f}  {profitable}")

    # Summary: which configs beat $0.30 entry (matching actual wallet)?
    print(f"\n{'='*100}")
    print(f"  PROFITABLE AT $0.30 ENTRY (actual wallet avg)")
    print(f"{'='*100}")
    print(f"  Need win rate > 30% to be profitable at $0.30 entry")
    print(f"  {'Config':<25} {'Train WR':<10} {'OOS2 WR':<10} "
          f"{'Train PnL@0.30':<16} {'OOS2 PnL@0.30':<16} {'$/hr Train':<12} {'$/hr OOS2':<12}")
    print(f"  {'-'*110}")

    train_hours = (training['timestamp_ms'].iloc[-1] - training['timestamp_ms'].iloc[0]) / 1000 / 3600
    oos2_hours = (oos2['timestamp_ms'].iloc[-1] - oos2['timestamp_ms'].iloc[0]) / 1000 / 3600

    profitable_at_30 = []
    for tr, os, _ in robust_configs:
        # Both must be profitable at $0.30
        tr_pnl = tr.wins * SHARES_PER_TRADE * 0.70 + tr.losses * (-SHARES_PER_TRADE * 0.30)
        os_pnl = os.wins * SHARES_PER_TRADE * 0.70 + os.losses * (-SHARES_PER_TRADE * 0.30)
        if tr_pnl > 0 and os_pnl > 0:
            profitable_at_30.append((tr, os, tr_pnl, os_pnl))

    profitable_at_30.sort(key=lambda x: x[2] + x[3], reverse=True)
    for tr, os, tr_pnl, os_pnl in profitable_at_30[:10]:
        print(f"  {tr.config.label:<25} {tr.win_rate*100:>6.1f}%   {os.win_rate*100:>6.1f}%   "
              f"${tr_pnl:>12,.0f}  ${os_pnl:>12,.0f}  "
              f"${tr_pnl/train_hours:>8,.0f}   ${os_pnl/oos2_hours:>8,.0f}")

    if not profitable_at_30:
        print("  NO configs profitable at $0.30 entry in both periods!")
        # Show which are profitable in at least one period
        print(f"\n  Configs profitable at $0.30 in TRAINING only:")
        for tr, os, _ in robust_configs[:5]:
            tr_pnl = tr.wins * SHARES_PER_TRADE * 0.70 + tr.losses * (-SHARES_PER_TRADE * 0.30)
            if tr_pnl > 0:
                print(f"    {tr.config.label}: WR={tr.win_rate*100:.1f}%, PnL=${tr_pnl:,.0f}")

    # Export results
    export_results(all_training_results, all_oos2_results)


def export_results(training_results: List[BacktestResult],
                   oos2_results: List[BacktestResult]):
    """Export results to CSV."""
    rows = []
    for r in training_results + oos2_results:
        rows.append({
            'period': r.period,
            'z_threshold': r.config.z_threshold,
            'min_delay_s': r.config.min_entry_delay_s,
            'total_windows': r.total_windows,
            'trades': r.trades_entered,
            'wins': r.wins,
            'losses': r.losses,
            'win_rate': r.win_rate,
            'total_pnl': r.total_pnl,
            'pnl_per_hour': r.pnl_per_hour,
            'avg_entry_price': r.avg_entry_price,
            'avg_z_score': r.avg_z_score,
            'avg_btc_move_pct': r.avg_btc_move_pct,
            'max_win': r.max_win,
            'max_loss': r.max_loss,
        })

    results_df = pd.DataFrame(rows)
    outpath = Path(__file__).parent / "contrarian_backtest_results.csv"
    results_df.to_csv(outpath, index=False)
    print(f"\n  Results exported to: {outpath}")


if __name__ == "__main__":
    main()
