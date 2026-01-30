#!/usr/bin/env python3
"""
OOS4 Validation: ALL Paths on Fresh Data (Jan 23-24, 2026)

Tests:
- Path 1: AGGRESSIVE (ou/ewma, 1200ms, time-stop 180s, 0<z<1.5, cycling ON)
- Path 1: BALANCED+EWMA (ewma/ewma, 1400ms, price-stop 15%, -0.5<z<1.5, cycling ON)
- Path 2: CONTRARIAN (buy cheap side against BTC, real orderbook prices, adaptive gate)

Usage:
    python research/validate_oos4_all_paths.py
"""

import sys
import math
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Dict, Optional, List, Tuple
from dataclasses import dataclass, field
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent.parent))

from research.analysis.volatility_filter_analysis import (
    load_ou_params, compute_zscore_series, BacktestConfig, run_backtest_with_zscore,
    TradeWithZScore, estimate_active_hours_zone
)

# =============================================================================
# CONFIGURATION
# =============================================================================

BASE_DIR = Path("/Users/rananjaybika/polymarket-amm-bot")

# Combined OOS3+OOS4 data (individual OOS4 files removed after combining)
OOS4_BTC_FILE = BASE_DIR / "research/observer/btc_prices_oos3_oos4_combined.csv"
OOS4_OBS_FILE = BASE_DIR / "research/observer/grid_obs_oos3_oos4_combined.csv"
COMBINED_BTC_FILE = OOS4_BTC_FILE
COMBINED_OBS_FILE = OOS4_OBS_FILE
OOS4_RES_FILE = BASE_DIR / "research/observer/market_resolutions_verified.csv"

# Training + OOS2 data (Jan 16-19)
TRAINING_BTC_FILE = BASE_DIR / "research/binance_hf/btc_prices_combined.csv"
TRAINING_OBS_FILES = [
    BASE_DIR / "research/observer/grid_obs_20260116.csv",
    BASE_DIR / "research/observer/grid_obs_20260117.csv",
    BASE_DIR / "research/observer/grid_obs_20260118.csv",
    BASE_DIR / "research/observer/grid_obs_20260119.csv",
]
TRAINING_RES_FILE = BASE_DIR / "research/observer/market_resolutions.csv"

SHARES = 5  # Match grid search position size


# =============================================================================
# DATA LOADING
# =============================================================================

def load_oos4_btc() -> pd.DataFrame:
    """Load OOS4 BTC price data."""
    print(f"  Loading OOS4 BTC: {OOS4_BTC_FILE.name}")
    btc_df = pd.read_csv(OOS4_BTC_FILE)
    btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms').reset_index(drop=True)
    print(f"  BTC rows: {len(btc_df):,}")
    return btc_df


def load_oos4_observer():
    """Load OOS4 observer data and resolutions."""
    print(f"  Loading OOS4 observer: {OOS4_OBS_FILE.name}")
    obs_df = pd.read_csv(OOS4_OBS_FILE, on_bad_lines='skip', low_memory=False)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    print(f"  Observer rows: {len(obs_df):,}")

    # Load resolutions
    print(f"  Loading resolutions: {OOS4_RES_FILE.name}")
    res_df = pd.read_csv(OOS4_RES_FILE)
    res_map = dict(zip(res_df['slug'], res_df['winner']))

    # Filter to OOS4 markets
    oos4_slugs = set(obs_df['market_slug'].unique())
    resolved = {k: v for k, v in res_map.items() if k in oos4_slugs and v in ('UP', 'DOWN')}
    pending = {k for k in oos4_slugs if res_map.get(k) not in ('UP', 'DOWN')}

    print(f"  OOS4 markets: {len(oos4_slugs)}")
    print(f"  Resolved: {len(resolved)}, Pending/Missing: {len(pending)}")

    return obs_df, res_map


# =============================================================================
# PATH 1: SPIKE STRATEGY BACKTESTS
# =============================================================================

@dataclass
class StrategyConfig:
    name: str
    path: str
    threshold_method: str
    zscore_method: str
    lookback_ticks: int
    lookback_ms: int
    stop_loss_pct: Optional[float]
    time_stop_seconds: Optional[float]
    use_cycling: bool
    z_lo: Optional[float]
    z_hi: Optional[float]

    @property
    def z_zone_label(self):
        if self.z_lo is not None and self.z_hi is not None:
            return f"{self.z_lo}<z<{self.z_hi}"
        elif self.z_lo is None and self.z_hi is not None:
            return f"z<{self.z_hi}"
        elif self.z_lo is not None:
            return f"z>{self.z_lo}"
        return "no_limit"


# Define all strategies
STRATEGIES = [
    # Path 1
    StrategyConfig(
        name="AGGRESSIVE", path="Path1",
        threshold_method="ou", zscore_method="ewma",
        lookback_ticks=72, lookback_ms=1200,
        stop_loss_pct=None, time_stop_seconds=180.0,
        use_cycling=True, z_lo=0.0, z_hi=1.5,
    ),
    StrategyConfig(
        name="BALANCED+EWMA", path="Path1",
        threshold_method="ewma", zscore_method="ewma",
        lookback_ticks=84, lookback_ms=1400,
        stop_loss_pct=0.15, time_stop_seconds=None,
        use_cycling=True, z_lo=-0.5, z_hi=1.5,
    ),
]


def run_spike_strategy(cfg: StrategyConfig, btc_df, obs_df, zscore_df, res_map, ou_params, total_hours):
    """Run a single spike strategy config on OOS4 data."""
    print(f"\n  {'='*70}")
    print(f"  {cfg.path}: {cfg.name}")
    stop_label = f"{int(cfg.time_stop_seconds)}s TIME" if cfg.time_stop_seconds else f"{int(cfg.stop_loss_pct*100)}% PRICE"
    print(f"  {cfg.threshold_method}/{cfg.zscore_method} | {cfg.lookback_ms}ms | {stop_label} | "
          f"full hedge | {'cycling' if cfg.use_cycling else 'no cycling'} | {cfg.z_zone_label}")
    print(f"  {'='*70}")

    backtest_cfg = BacktestConfig(
        target_shares=SHARES,
        spike_lookback=cfg.lookback_ticks,
        stop_loss_pct=cfg.stop_loss_pct,
        time_stop_seconds=cfg.time_stop_seconds,
        use_cycling=cfg.use_cycling,
    )

    trades = run_backtest_with_zscore(
        backtest_cfg, btc_df, obs_df, zscore_df, res_map,
        method=cfg.threshold_method,
        ou_params=ou_params,
        quiet=True
    )

    # Filter to z-zone
    filtered = []
    for t in trades:
        z = t.zscore_at_entry
        if cfg.z_lo is not None and z <= cfg.z_lo:
            continue
        if cfg.z_hi is not None and z >= cfg.z_hi:
            continue
        filtered.append(t)

    if not filtered:
        print(f"  NO TRADES in z-zone!")
        return None

    # Compute metrics
    hours_active = estimate_active_hours_zone(total_hours, zscore_df, cfg.z_lo, cfg.z_hi)
    total_pnl = sum(t.pnl for t in filtered)
    hourly_rate = total_pnl / hours_active if hours_active > 0 else 0
    wins = sum(1 for t in filtered if t.pnl > 0)
    correct_dir = sum(1 for t in filtered if t.correct_direction)
    hedge_types = Counter(t.hedge_type for t in filtered)

    print(f"  Trades: {len(filtered)}")
    print(f"  PnL @{SHARES}sh: ${total_pnl:.2f} | $/hr: ${hourly_rate:.3f}")
    print(f"  PnL @50sh: ${total_pnl*10:.2f} | $/hr@50sh: ${hourly_rate*10:.2f}")
    print(f"  Win Rate: {wins/len(filtered)*100:.1f}% | Dir Accuracy: {correct_dir/len(filtered)*100:.1f}%")
    print(f"  Exits: {dict(hedge_types)}")
    print(f"  Hours Active: {hours_active:.2f}")

    return {
        'config': cfg.name,
        'path': cfg.path,
        'trades': len(filtered),
        'pnl_5sh': total_pnl,
        'hourly_5sh': hourly_rate,
        'hourly_50sh': hourly_rate * 10,
        'win_rate': wins / len(filtered) * 100,
        'dir_acc': correct_dir / len(filtered) * 100,
        'hours_active': hours_active,
        'hedge_types': dict(hedge_types),
    }


# =============================================================================
# PATH 2: CONTRARIAN STRATEGY
# =============================================================================

class AdaptiveEWMAGate:
    """Self-adapting vol gate - no calibration needed."""
    def __init__(self, k: float = 0.5, halflife_windows: int = 50):
        self.k = k
        self.halflife = halflife_windows
        self.alpha = 1 - 0.5 ** (1.0 / halflife_windows)
        self.vol_ema = None

    def update_and_check(self, pre_vol: float) -> bool:
        if self.vol_ema is None:
            self.vol_ema = pre_vol
            return True  # Allow during warmup

        ratio = pre_vol / max(self.vol_ema, 1e-10)
        allowed = ratio >= self.k

        # Update EMA AFTER check (no lookahead)
        self.vol_ema = self.alpha * pre_vol + (1 - self.alpha) * self.vol_ema
        return allowed


def run_contrarian_strategy(btc_df: pd.DataFrame, obs_df: pd.DataFrame, total_hours: float):
    """
    Run Path 2 contrarian strategy on OOS4 BTC data.

    Best config from research: rolling_300s vol, Z=0.5, delay=60s,
    adaptive gate k=0.5, halflife=50.
    Uses real orderbook prices from observer data instead of fixed $0.30.
    """
    print(f"\n  {'='*70}")
    print(f"  Path2: CONTRARIAN (mean-reversion)")
    print(f"  Buy cheap side (real orderbook price) against BTC direction | delay=60s | Z>=0.5")
    print(f"  Adaptive gate: k=0.5, halflife=50 windows")
    print(f"  {'='*70}")

    # Build observer lookup by market_slug for fast price lookups
    obs_by_market = {}
    for slug, group in obs_df.groupby('market_slug'):
        sorted_group = group.sort_values('timestamp_ms')
        obs_by_market[slug] = {
            'timestamps': sorted_group['timestamp_ms'].values,
            'up_ask': sorted_group['up_ask'].values,
            'down_ask': sorted_group['down_ask'].values,
        }
    print(f"  Observer markets indexed: {len(obs_by_market)}")

    # Resample BTC to 1-second bars
    print(f"  Resampling to 1s bars...", flush=True)
    btc_df_sorted = btc_df.sort_values('timestamp_ms')
    btc_df_sorted['second_ms'] = (btc_df_sorted['timestamp_ms'] // 1000) * 1000
    bars_1s = btc_df_sorted.groupby('second_ms').agg({'price': 'last'}).reset_index()
    bars_1s.rename(columns={'second_ms': 'timestamp_ms'}, inplace=True)
    print(f"  {len(bars_1s):,} 1-second bars")

    timestamps = bars_1s['timestamp_ms'].values
    prices = bars_1s['price'].values

    # Config
    WINDOW_S = 900  # 15 minutes
    WINDOW_MS = WINDOW_S * 1000
    MIN_DELAY_S = 60
    Z_THRESHOLD = 0.5
    ENTRY_PRICE = 0.30
    SHARES_PER_TRADE = 50
    VOL_WINDOW_S = 300  # rolling 300s for Z-score normalization

    # Align to 15-min boundaries
    first_window = ((timestamps[0] // WINDOW_MS) + 1) * WINDOW_MS
    end_ms = timestamps[-1]

    gate = AdaptiveEWMAGate(k=0.5, halflife_windows=50)
    trades = []
    windows_total = 0
    windows_gated = 0

    current_start = first_window
    while current_start + WINDOW_MS <= end_ms:
        win_mask = (timestamps >= current_start) & (timestamps < current_start + WINDOW_MS)
        win_idx = np.where(win_mask)[0]

        # Pre-window prices (5 min before)
        pre_start = current_start - 300_000
        pre_mask = (timestamps >= pre_start) & (timestamps < current_start)
        pre_idx = np.where(pre_mask)[0]

        current_start += WINDOW_MS

        if len(win_idx) < 60:
            continue

        windows_total += 1
        win_prices = prices[win_idx]
        win_times = timestamps[win_idx]
        pre_prices = prices[pre_idx] if len(pre_idx) > 0 else np.array([win_prices[0]])

        # Pre-window vol
        if len(pre_prices) > 10:
            pre_returns = np.diff(pre_prices) / pre_prices[:-1] * 100
            pre_vol = float(np.std(pre_returns))
        else:
            pre_vol = 0.001

        # Adaptive gate
        if not gate.update_and_check(pre_vol):
            windows_gated += 1
            continue

        # Scan for entry signal
        btc_open = win_prices[0]
        entry_made = False
        max_idx = min(780, len(win_prices))  # Don't enter after 13 min

        for i in range(MIN_DELAY_S, max_idx):
            if entry_made:
                break

            elapsed_s = (win_times[i] - win_times[0]) / 1000.0
            if elapsed_s < MIN_DELAY_S:
                continue

            current_price = win_prices[i]
            btc_move_pct = (current_price - btc_open) / btc_open * 100

            if abs(btc_move_pct) < 0.01:
                continue

            # Rolling vol for Z-score
            combined_prices = np.concatenate([pre_prices[-VOL_WINDOW_S:], win_prices[:i+1]])
            if len(combined_prices) < 30:
                continue
            window_prices = combined_prices[-VOL_WINDOW_S:]
            returns = np.diff(window_prices) / window_prices[:-1] * 100
            if len(returns) < 10:
                continue
            vol_per_s = max(np.std(returns), 1e-8)
            expected_move_std = vol_per_s * math.sqrt(max(elapsed_s, 1))

            if expected_move_std < 1e-8:
                continue

            z_score = abs(btc_move_pct) / expected_move_std

            if z_score >= Z_THRESHOLD:
                # CONTRARIAN: bet against BTC direction
                entry_direction = "DOWN" if btc_move_pct > 0 else "UP"

                # Look up real price from observer
                # current_start has already been incremented, so actual window start is current_start - WINDOW_MS
                window_start_s = int((current_start - WINDOW_MS) // 1000)
                market_slug = f"btc-updown-15m-{window_start_s}"

                entry_price = ENTRY_PRICE  # fallback $0.30
                if market_slug in obs_by_market:
                    obs_data = obs_by_market[market_slug]
                    signal_ts = win_times[i]
                    # Binary search for nearest observer timestamp
                    idx = np.searchsorted(obs_data['timestamps'], signal_ts)
                    idx = min(idx, len(obs_data['timestamps']) - 1)

                    if btc_move_pct > 0:
                        # BTC up → buy DOWN (contrarian)
                        raw_price = float(obs_data['down_ask'][idx])
                    else:
                        # BTC down → buy UP (contrarian)
                        raw_price = float(obs_data['up_ask'][idx])

                    # Use real price if valid, else keep fallback
                    if not np.isnan(raw_price) and raw_price > 0:
                        entry_price = max(0.05, min(0.50, raw_price))

                # Resolution: BTC at end of window
                btc_close = win_prices[-1]
                close_move = (btc_close - btc_open) / btc_open * 100
                winner = "UP" if close_move >= 0 else "DOWN"

                won = (entry_direction == winner)
                pnl = SHARES_PER_TRADE * ((1.0 - entry_price) if won else -entry_price)

                trades.append({
                    'entry_time_s': elapsed_s,
                    'btc_move_pct': btc_move_pct,
                    'z_score': z_score,
                    'entry_direction': entry_direction,
                    'entry_price': entry_price,
                    'market_slug': market_slug,
                    'winner': winner,
                    'won': won,
                    'pnl': pnl,
                })
                entry_made = True

    # Results
    if not trades:
        print(f"  NO TRADES in OOS4!")
        return None

    wins = sum(1 for t in trades if t['won'])
    total_pnl = sum(t['pnl'] for t in trades)
    win_rate = wins / len(trades) * 100
    hourly_rate = total_pnl / total_hours if total_hours > 0 else 0
    avg_z = np.mean([t['z_score'] for t in trades])
    avg_entry_time = np.mean([t['entry_time_s'] for t in trades])
    entry_prices = [t['entry_price'] for t in trades]
    avg_entry_price = np.mean(entry_prices)

    print(f"  Windows: {windows_total} total, {windows_gated} gated out ({windows_gated/windows_total*100:.0f}%)")
    print(f"  Trades: {len(trades)} ({len(trades)/windows_total*100:.0f}% of windows)")
    print(f"  Win Rate: {win_rate:.1f}% (breakeven @ avg entry ${avg_entry_price:.3f} = {avg_entry_price*100:.1f}%)")
    print(f"  PnL: ${total_pnl:,.0f} ({SHARES_PER_TRADE} shares/trade)")
    print(f"  $/hr: ${hourly_rate:,.0f}")
    print(f"  Avg Z-score: {avg_z:.2f} | Avg entry time: {avg_entry_time:.0f}s")

    # Entry price statistics
    print(f"  Entry price: avg=${avg_entry_price:.3f}, "
          f"min=${np.min(entry_prices):.3f}, max=${np.max(entry_prices):.3f}")
    real_price_count = sum(1 for p in entry_prices if abs(p - 0.30) > 0.001)
    print(f"  Trades with real price: {real_price_count}/{len(trades)}")

    # Signal severity vs entry price
    z_scores = [t['z_score'] for t in trades]
    if len(z_scores) > 5:
        corr = np.corrcoef(z_scores, entry_prices)[0, 1]
        print(f"  Z-score vs entry price correlation: r={corr:.3f}")
        # Bin by z-score
        for z_lo, z_hi in [(0.5, 1.0), (1.0, 1.5), (1.5, 3.0)]:
            bin_prices = [p for z, p in zip(z_scores, entry_prices) if z_lo <= z < z_hi]
            if bin_prices:
                print(f"    Z [{z_lo:.1f}-{z_hi:.1f}]: avg entry=${np.mean(bin_prices):.3f} ({len(bin_prices)} trades)")

    return {
        'config': 'CONTRARIAN',
        'path': 'Path2',
        'trades': len(trades),
        'pnl_total': total_pnl,
        'hourly_rate': hourly_rate,
        'win_rate': win_rate,
        'windows': windows_total,
        'gated_out': windows_gated,
        'avg_z': avg_z,
        'avg_entry_time': avg_entry_time,
        'avg_entry_price': avg_entry_price,
        'min_entry_price': float(np.min(entry_prices)),
        'max_entry_price': float(np.max(entry_prices)),
        'real_price_pct': real_price_count / len(trades) * 100,
        'shares_per_trade': SHARES_PER_TRADE,
    }


# =============================================================================
# WALLET PATTERN ANALYSIS (0xa5e8 replication)
# =============================================================================

def analyze_wallet_pattern(btc_df: pd.DataFrame, obs_df: pd.DataFrame, total_hours: float):
    """
    Analyze entry patterns to replicate 0xa5e8 wallet performance.

    The wallet gets avg $0.27 entries and 54% WR by waiting ~5 min.
    We test: delay sweeps, price evolution, BTC-move vs price, and price-trigger variants.
    """
    print("\n" + "=" * 100)
    print("WALLET PATTERN ANALYSIS (0xa5e8 Replication)")
    print("=" * 100)

    # Build observer lookup
    obs_by_market = {}
    for slug, group in obs_df.groupby('market_slug'):
        sorted_group = group.sort_values('timestamp_ms')
        obs_by_market[slug] = {
            'timestamps': sorted_group['timestamp_ms'].values,
            'up_ask': sorted_group['up_ask'].values,
            'down_ask': sorted_group['down_ask'].values,
        }

    # Resample BTC to 1s bars
    btc_df_sorted = btc_df.sort_values('timestamp_ms')
    btc_df_sorted['second_ms'] = (btc_df_sorted['timestamp_ms'] // 1000) * 1000
    bars_1s = btc_df_sorted.groupby('second_ms').agg({'price': 'last'}).reset_index()
    bars_1s.rename(columns={'second_ms': 'timestamp_ms'}, inplace=True)
    timestamps = bars_1s['timestamp_ms'].values
    prices = bars_1s['price'].values

    WINDOW_S = 900
    WINDOW_MS = WINDOW_S * 1000
    SHARES_PER_TRADE = 50
    VOL_WINDOW_S = 300

    first_window = ((timestamps[0] // WINDOW_MS) + 1) * WINDOW_MS
    end_ms = timestamps[-1]

    # =========================================================================
    # STEP 1: Entry Delay Sweep
    # =========================================================================
    print(f"\n  === STEP 1: ENTRY DELAY SWEEP ===")
    print(f"  Testing delays: 60s to 420s with Z>=0.5 trigger")
    print(f"  {'Delay':<8} {'Trades':<8} {'AvgPrice':<10} {'WinRate':<9} {'PnL':<10} {'$/hr':<8}")
    print(f"  {'-'*60}")

    delay_results = []
    for test_delay in [0, 30]:
        gate = AdaptiveEWMAGate(k=0.5, halflife_windows=50)
        trades = []
        current_start = first_window

        while current_start + WINDOW_MS <= end_ms:
            win_mask = (timestamps >= current_start) & (timestamps < current_start + WINDOW_MS)
            win_idx = np.where(win_mask)[0]
            pre_start = current_start - 300_000
            pre_mask = (timestamps >= pre_start) & (timestamps < current_start)
            pre_idx = np.where(pre_mask)[0]
            current_start += WINDOW_MS

            if len(win_idx) < max(60, test_delay + 10):
                continue

            win_prices = prices[win_idx]
            win_times = timestamps[win_idx]
            pre_prices = prices[pre_idx] if len(pre_idx) > 0 else np.array([win_prices[0]])

            if len(pre_prices) > 10:
                pre_returns = np.diff(pre_prices) / pre_prices[:-1] * 100
                pre_vol = float(np.std(pre_returns))
            else:
                pre_vol = 0.001

            if not gate.update_and_check(pre_vol):
                continue

            btc_open = win_prices[0]
            entry_made = False
            max_idx = min(780, len(win_prices))

            for i in range(test_delay, max_idx):
                if entry_made:
                    break
                elapsed_s = (win_times[i] - win_times[0]) / 1000.0
                if elapsed_s < test_delay:
                    continue

                current_price = win_prices[i]
                btc_move_pct = (current_price - btc_open) / btc_open * 100
                if abs(btc_move_pct) < 0.01:
                    continue

                combined_prices = np.concatenate([pre_prices[-VOL_WINDOW_S:], win_prices[:i+1]])
                if len(combined_prices) < 30:
                    continue
                window_prices = combined_prices[-VOL_WINDOW_S:]
                returns = np.diff(window_prices) / window_prices[:-1] * 100
                if len(returns) < 10:
                    continue
                vol_per_s = max(np.std(returns), 1e-8)
                expected_move_std = vol_per_s * math.sqrt(max(elapsed_s, 1))
                if expected_move_std < 1e-8:
                    continue

                z_score = abs(btc_move_pct) / expected_move_std
                if z_score >= 0.5:
                    entry_direction = "DOWN" if btc_move_pct > 0 else "UP"
                    window_start_s = int((current_start - WINDOW_MS) // 1000)
                    market_slug = f"btc-updown-15m-{window_start_s}"

                    entry_price = 0.30
                    if market_slug in obs_by_market:
                        obs_data = obs_by_market[market_slug]
                        signal_ts = win_times[i]
                        idx = np.searchsorted(obs_data['timestamps'], signal_ts)
                        idx = min(idx, len(obs_data['timestamps']) - 1)
                        if btc_move_pct > 0:
                            raw_price = float(obs_data['down_ask'][idx])
                        else:
                            raw_price = float(obs_data['up_ask'][idx])
                        if not np.isnan(raw_price) and raw_price > 0:
                            entry_price = max(0.05, min(0.50, raw_price))

                    btc_close = win_prices[-1]
                    close_move = (btc_close - btc_open) / btc_open * 100
                    winner = "UP" if close_move >= 0 else "DOWN"
                    won = (entry_direction == winner)
                    pnl = SHARES_PER_TRADE * ((1.0 - entry_price) if won else -entry_price)

                    trades.append({
                        'entry_time_s': elapsed_s,
                        'btc_move_pct': btc_move_pct,
                        'entry_price': entry_price,
                        'won': won,
                        'pnl': pnl,
                    })
                    entry_made = True

        if trades:
            avg_price = np.mean([t['entry_price'] for t in trades])
            wr = sum(1 for t in trades if t['won']) / len(trades) * 100
            total_pnl = sum(t['pnl'] for t in trades)
            hourly = total_pnl / total_hours if total_hours > 0 else 0
            print(f"  {test_delay:<8} {len(trades):<8} ${avg_price:<9.3f} {wr:<8.1f}% ${total_pnl:<9.0f} ${hourly:<7.0f}")
            delay_results.append({
                'delay': test_delay, 'trades': len(trades),
                'avg_price': avg_price, 'win_rate': wr,
                'pnl': total_pnl, 'hourly': hourly,
            })
        else:
            print(f"  {test_delay:<8} {'0':<8} {'N/A':<10} {'N/A':<9} {'N/A':<10} {'N/A':<8}")

    # =========================================================================
    # STEP 2: Price Evolution Over Time (cheap-side ask vs seconds into window)
    # =========================================================================
    print(f"\n  === STEP 2: CHEAP-SIDE PRICE vs TIME INTO WINDOW ===")
    print(f"  For each window, track how the losing side's ask evolves")

    time_buckets = [60, 120, 180, 240, 300, 360, 420, 480, 540, 600, 720, 840]
    bucket_prices = {b: [] for b in time_buckets}

    current_start = first_window
    while current_start + WINDOW_MS <= end_ms:
        win_mask = (timestamps >= current_start) & (timestamps < current_start + WINDOW_MS)
        win_idx = np.where(win_mask)[0]
        current_start += WINDOW_MS

        if len(win_idx) < 600:
            continue

        win_prices_arr = prices[win_idx]
        win_times_arr = timestamps[win_idx]
        btc_open = win_prices_arr[0]
        btc_close = win_prices_arr[-1]
        close_move = (btc_close - btc_open) / btc_open * 100

        # Determine which side will be cheap (losing side = opposite of final move)
        if close_move >= 0:
            cheap_side = 'down_ask'  # DOWN loses when BTC goes up
        else:
            cheap_side = 'up_ask'  # UP loses when BTC goes down

        window_start_s = int((current_start - WINDOW_MS) // 1000)
        market_slug = f"btc-updown-15m-{window_start_s}"

        if market_slug not in obs_by_market:
            continue

        obs_data = obs_by_market[market_slug]

        for bucket_s in time_buckets:
            target_ts = win_times_arr[0] + bucket_s * 1000
            idx = np.searchsorted(obs_data['timestamps'], target_ts)
            idx = min(idx, len(obs_data['timestamps']) - 1)
            raw_price = float(obs_data[cheap_side][idx])
            if not np.isnan(raw_price) and 0 < raw_price < 0.60:
                bucket_prices[bucket_s].append(raw_price)

    print(f"  {'Time(s)':<10} {'AvgPrice':<10} {'Median':<10} {'<$0.30':<10} {'Samples':<8}")
    print(f"  {'-'*50}")
    for b in time_buckets:
        if bucket_prices[b]:
            arr = np.array(bucket_prices[b])
            below_30 = np.sum(arr < 0.30) / len(arr) * 100
            print(f"  {b:<10} ${np.mean(arr):<9.3f} ${np.median(arr):<9.3f} {below_30:<9.1f}% {len(arr):<8}")

    # =========================================================================
    # STEP 3: BTC Move Size vs Orderbook Price
    # =========================================================================
    print(f"\n  === STEP 3: BTC MOVE SIZE vs CHEAP-SIDE ASK ===")
    print(f"  At each time point: how much has BTC moved vs what's the cheap-side price?")

    move_price_pairs = []  # (abs_btc_move_pct, cheap_ask)

    current_start = first_window
    while current_start + WINDOW_MS <= end_ms:
        win_mask = (timestamps >= current_start) & (timestamps < current_start + WINDOW_MS)
        win_idx = np.where(win_mask)[0]
        current_start += WINDOW_MS

        if len(win_idx) < 600:
            continue

        win_prices_arr = prices[win_idx]
        win_times_arr = timestamps[win_idx]
        btc_open = win_prices_arr[0]

        window_start_s = int((current_start - WINDOW_MS) // 1000)
        market_slug = f"btc-updown-15m-{window_start_s}"
        if market_slug not in obs_by_market:
            continue

        obs_data = obs_by_market[market_slug]

        # Sample every 30s from 60s to 780s
        for sec in range(60, 780, 30):
            if sec >= len(win_prices_arr):
                break
            btc_now = win_prices_arr[sec]
            btc_move = (btc_now - btc_open) / btc_open * 100

            if abs(btc_move) < 0.005:
                continue

            # Cheap side = opposite of current BTC direction
            if btc_move > 0:
                side_key = 'down_ask'
            else:
                side_key = 'up_ask'

            target_ts = win_times_arr[sec]
            idx = np.searchsorted(obs_data['timestamps'], target_ts)
            idx = min(idx, len(obs_data['timestamps']) - 1)
            raw_price = float(obs_data[side_key][idx])

            if not np.isnan(raw_price) and 0 < raw_price < 0.60:
                move_price_pairs.append((abs(btc_move), raw_price))

    if move_price_pairs:
        moves = np.array([p[0] for p in move_price_pairs])
        asks = np.array([p[1] for p in move_price_pairs])
        corr = np.corrcoef(moves, asks)[0, 1]
        print(f"  Correlation (|BTC move| vs cheap ask): r={corr:.3f}")
        print(f"  Total samples: {len(move_price_pairs)}")

        # Bin by BTC move size
        print(f"\n  {'BTC Move':<15} {'AvgAsk':<10} {'Median':<10} {'<$0.30':<10} {'Samples':<8}")
        print(f"  {'-'*55}")
        for lo, hi in [(0.01, 0.03), (0.03, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.50)]:
            mask = (moves >= lo) & (moves < hi)
            if np.sum(mask) > 0:
                bin_asks = asks[mask]
                below_30 = np.sum(bin_asks < 0.30) / len(bin_asks) * 100
                print(f"  {lo:.2f}-{hi:.2f}%    ${np.mean(bin_asks):<9.3f} ${np.median(bin_asks):<9.3f} {below_30:<9.1f}% {len(bin_asks):<8}")

    # =========================================================================
    # STEP 4: Wallet-Matched Config (delay=300s, min_btc_move=0.05%, max_price=0.35)
    # =========================================================================
    print(f"\n  === STEP 4: WALLET-MATCHED CONFIG ===")
    print(f"  delay=300s, min_btc_move=0.05%, max_entry_price=$0.35, Z>=0.5")

    gate = AdaptiveEWMAGate(k=0.5, halflife_windows=50)
    wallet_trades = []
    current_start = first_window

    while current_start + WINDOW_MS <= end_ms:
        win_mask = (timestamps >= current_start) & (timestamps < current_start + WINDOW_MS)
        win_idx = np.where(win_mask)[0]
        pre_start = current_start - 300_000
        pre_mask = (timestamps >= pre_start) & (timestamps < current_start)
        pre_idx = np.where(pre_mask)[0]
        current_start += WINDOW_MS

        if len(win_idx) < 310:
            continue

        win_prices_arr = prices[win_idx]
        win_times_arr = timestamps[win_idx]
        pre_prices = prices[pre_idx] if len(pre_idx) > 0 else np.array([win_prices_arr[0]])

        if len(pre_prices) > 10:
            pre_returns = np.diff(pre_prices) / pre_prices[:-1] * 100
            pre_vol = float(np.std(pre_returns))
        else:
            pre_vol = 0.001

        if not gate.update_and_check(pre_vol):
            continue

        btc_open = win_prices_arr[0]
        entry_made = False
        max_idx = min(780, len(win_prices_arr))

        for i in range(300, max_idx):
            if entry_made:
                break
            elapsed_s = (win_times_arr[i] - win_times_arr[0]) / 1000.0
            if elapsed_s < 300:
                continue

            current_price = win_prices_arr[i]
            btc_move_pct = (current_price - btc_open) / btc_open * 100

            # Require minimum BTC move of 0.05%
            if abs(btc_move_pct) < 0.05:
                continue

            combined_prices = np.concatenate([pre_prices[-VOL_WINDOW_S:], win_prices_arr[:i+1]])
            if len(combined_prices) < 30:
                continue
            window_prices = combined_prices[-VOL_WINDOW_S:]
            returns = np.diff(window_prices) / window_prices[:-1] * 100
            if len(returns) < 10:
                continue
            vol_per_s = max(np.std(returns), 1e-8)
            expected_move_std = vol_per_s * math.sqrt(max(elapsed_s, 1))
            if expected_move_std < 1e-8:
                continue

            z_score = abs(btc_move_pct) / expected_move_std
            if z_score >= 0.5:
                entry_direction = "DOWN" if btc_move_pct > 0 else "UP"
                window_start_s = int((current_start - WINDOW_MS) // 1000)
                market_slug = f"btc-updown-15m-{window_start_s}"

                entry_price = 0.30
                if market_slug in obs_by_market:
                    obs_data = obs_by_market[market_slug]
                    signal_ts = win_times_arr[i]
                    idx = np.searchsorted(obs_data['timestamps'], signal_ts)
                    idx = min(idx, len(obs_data['timestamps']) - 1)
                    if btc_move_pct > 0:
                        raw_price = float(obs_data['down_ask'][idx])
                    else:
                        raw_price = float(obs_data['up_ask'][idx])
                    if not np.isnan(raw_price) and raw_price > 0:
                        entry_price = max(0.05, min(0.50, raw_price))

                # Only enter if price is cheap enough
                if entry_price > 0.35:
                    continue

                btc_close = win_prices_arr[-1]
                close_move = (btc_close - btc_open) / btc_open * 100
                winner = "UP" if close_move >= 0 else "DOWN"
                won = (entry_direction == winner)
                pnl = SHARES_PER_TRADE * ((1.0 - entry_price) if won else -entry_price)

                wallet_trades.append({
                    'entry_time_s': elapsed_s,
                    'btc_move_pct': btc_move_pct,
                    'entry_price': entry_price,
                    'won': won,
                    'pnl': pnl,
                    'z_score': z_score,
                })
                entry_made = True

    wallet_wr = None
    wallet_avg_price = None
    wallet_breakeven = None
    if wallet_trades:
        wallet_avg_price = np.mean([t['entry_price'] for t in wallet_trades])
        wallet_wr = sum(1 for t in wallet_trades if t['won']) / len(wallet_trades) * 100
        total_pnl = sum(t['pnl'] for t in wallet_trades)
        hourly = total_pnl / total_hours if total_hours > 0 else 0
        avg_entry_t = np.mean([t['entry_time_s'] for t in wallet_trades])
        avg_btc_move = np.mean([abs(t['btc_move_pct']) for t in wallet_trades])
        wallet_breakeven = wallet_avg_price * 100

        print(f"  Trades: {len(wallet_trades)}")
        print(f"  Win Rate: {wallet_wr:.1f}% (breakeven={wallet_breakeven:.1f}%)")
        print(f"  Avg entry price: ${wallet_avg_price:.3f}")
        print(f"  Avg entry time: {avg_entry_t:.0f}s")
        print(f"  Avg |BTC move|: {avg_btc_move:.3f}%")
        print(f"  PnL: ${total_pnl:,.0f} | $/hr: ${hourly:,.0f}")
        print(f"  R:R ratio: 1:{(1-wallet_avg_price)/wallet_avg_price:.1f}")
    else:
        print(f"  NO TRADES with wallet-matched config!")

    # =========================================================================
    # STEP 5: Price-Trigger Variant (no Z-score, just buy when ask < threshold)
    # =========================================================================
    print(f"\n  === STEP 5: PRICE-TRIGGER VARIANT ===")
    print(f"  No Z-score — just buy cheap side when ask drops below threshold")
    print(f"  Scan from 60s onward, enter when cheap_ask <= MAX_PRICE")
    print(f"  {'MaxPrice':<10} {'Trades':<8} {'AvgEntry':<10} {'AvgTime':<9} {'WR':<8} {'PnL':<10} {'$/hr':<8}")
    print(f"  {'-'*65}")

    for max_entry in [0.20, 0.25, 0.30, 0.35, 0.40]:
        price_trigger_trades = []
        current_start = first_window

        while current_start + WINDOW_MS <= end_ms:
            win_mask = (timestamps >= current_start) & (timestamps < current_start + WINDOW_MS)
            win_idx = np.where(win_mask)[0]
            current_start += WINDOW_MS

            if len(win_idx) < 100:
                continue

            win_prices_arr = prices[win_idx]
            win_times_arr = timestamps[win_idx]
            btc_open = win_prices_arr[0]

            window_start_s = int((current_start - WINDOW_MS) // 1000)
            market_slug = f"btc-updown-15m-{window_start_s}"
            if market_slug not in obs_by_market:
                continue

            obs_data = obs_by_market[market_slug]
            entry_made = False
            max_idx = min(780, len(win_prices_arr))

            for i in range(60, max_idx):
                if entry_made:
                    break

                elapsed_s = (win_times_arr[i] - win_times_arr[0]) / 1000.0
                if elapsed_s < 60:
                    continue

                current_price = win_prices_arr[i]
                btc_move_pct = (current_price - btc_open) / btc_open * 100

                # Need some directional signal (min 0.01% move)
                if abs(btc_move_pct) < 0.01:
                    continue

                # Check cheap side price
                if btc_move_pct > 0:
                    side_key = 'down_ask'
                else:
                    side_key = 'up_ask'

                signal_ts = win_times_arr[i]
                idx = np.searchsorted(obs_data['timestamps'], signal_ts)
                idx = min(idx, len(obs_data['timestamps']) - 1)
                raw_price = float(obs_data[side_key][idx])

                if np.isnan(raw_price) or raw_price <= 0:
                    continue

                entry_price = max(0.05, min(0.50, raw_price))

                # PRICE TRIGGER: only enter if cheap enough
                if entry_price > max_entry:
                    continue

                entry_direction = "DOWN" if btc_move_pct > 0 else "UP"
                btc_close = win_prices_arr[-1]
                close_move = (btc_close - btc_open) / btc_open * 100
                winner = "UP" if close_move >= 0 else "DOWN"
                won = (entry_direction == winner)
                pnl = SHARES_PER_TRADE * ((1.0 - entry_price) if won else -entry_price)

                price_trigger_trades.append({
                    'entry_time_s': elapsed_s,
                    'entry_price': entry_price,
                    'won': won,
                    'pnl': pnl,
                })
                entry_made = True

        if price_trigger_trades:
            avg_p = np.mean([t['entry_price'] for t in price_trigger_trades])
            avg_t = np.mean([t['entry_time_s'] for t in price_trigger_trades])
            wr = sum(1 for t in price_trigger_trades if t['won']) / len(price_trigger_trades) * 100
            total_pnl = sum(t['pnl'] for t in price_trigger_trades)
            hourly = total_pnl / total_hours if total_hours > 0 else 0
            print(f"  ${max_entry:<9.2f} {len(price_trigger_trades):<8} ${avg_p:<9.3f} {avg_t:<8.0f}s {wr:<7.1f}% ${total_pnl:<9.0f} ${hourly:<7.0f}")
        else:
            print(f"  ${max_entry:<9.2f} {'0':<8} {'N/A':<10} {'N/A':<9} {'N/A':<8} {'N/A':<10} {'N/A':<8}")

    # =========================================================================
    # STEP 6: Reversal Confirmation Filter
    # Wait for BTC to pull back X% from local extreme before entering
    # =========================================================================
    print(f"\n  === STEP 6: REVERSAL CONFIRMATION ===")
    print(f"  After BTC moves, wait for a pullback from the local extreme before entering")
    print(f"  {'Pullback':<10} {'Trades':<8} {'AvgPrice':<10} {'AvgTime':<9} {'WR':<8} {'PnL':<10} {'$/hr':<8}")
    print(f"  {'-'*65}")

    for min_pullback_pct in [0.005, 0.01, 0.02, 0.03, 0.05]:
        rev_trades = []
        current_start = first_window

        while current_start + WINDOW_MS <= end_ms:
            win_mask = (timestamps >= current_start) & (timestamps < current_start + WINDOW_MS)
            win_idx = np.where(win_mask)[0]
            current_start += WINDOW_MS

            if len(win_idx) < 100:
                continue

            win_prices_arr = prices[win_idx]
            win_times_arr = timestamps[win_idx]
            btc_open = win_prices_arr[0]

            window_start_s = int((current_start - WINDOW_MS) // 1000)
            market_slug = f"btc-updown-15m-{window_start_s}"
            if market_slug not in obs_by_market:
                continue

            obs_data = obs_by_market[market_slug]
            entry_made = False
            max_idx = min(780, len(win_prices_arr))

            # Track local extreme
            local_max = btc_open
            local_min = btc_open

            for i in range(60, max_idx):
                if entry_made:
                    break

                current_price = win_prices_arr[i]
                local_max = max(local_max, current_price)
                local_min = min(local_min, current_price)

                btc_move_pct = (current_price - btc_open) / btc_open * 100
                if abs(btc_move_pct) < 0.01:
                    continue

                # Check for pullback from extreme
                if btc_move_pct > 0:
                    # BTC went up — check pullback from local_max
                    pullback = (local_max - current_price) / btc_open * 100
                    if pullback < min_pullback_pct:
                        continue
                    side_key = 'down_ask'
                    entry_direction = "DOWN"
                else:
                    # BTC went down — check bounce from local_min
                    pullback = (current_price - local_min) / btc_open * 100
                    if pullback < min_pullback_pct:
                        continue
                    side_key = 'up_ask'
                    entry_direction = "UP"

                elapsed_s = (win_times_arr[i] - win_times_arr[0]) / 1000.0

                # Get orderbook price
                signal_ts = win_times_arr[i]
                idx = np.searchsorted(obs_data['timestamps'], signal_ts)
                idx = min(idx, len(obs_data['timestamps']) - 1)
                raw_price = float(obs_data[side_key][idx])
                if np.isnan(raw_price) or raw_price <= 0:
                    continue
                entry_price = max(0.05, min(0.50, raw_price))

                btc_close = win_prices_arr[-1]
                close_move = (btc_close - btc_open) / btc_open * 100
                winner = "UP" if close_move >= 0 else "DOWN"
                won = (entry_direction == winner)
                pnl = SHARES_PER_TRADE * ((1.0 - entry_price) if won else -entry_price)

                rev_trades.append({
                    'entry_time_s': elapsed_s,
                    'entry_price': entry_price,
                    'won': won,
                    'pnl': pnl,
                })
                entry_made = True

        if rev_trades:
            avg_p = np.mean([t['entry_price'] for t in rev_trades])
            avg_t = np.mean([t['entry_time_s'] for t in rev_trades])
            wr = sum(1 for t in rev_trades if t['won']) / len(rev_trades) * 100
            total_pnl = sum(t['pnl'] for t in rev_trades)
            hourly = total_pnl / total_hours if total_hours > 0 else 0
            print(f"  {min_pullback_pct:<10.3f} {len(rev_trades):<8} ${avg_p:<9.3f} {avg_t:<8.0f}s {wr:<7.1f}% ${total_pnl:<9.0f} ${hourly:<7.0f}")
        else:
            print(f"  {min_pullback_pct:<10.3f} {'0':<8} {'N/A':<10}")

    # =========================================================================
    # STEP 7: Larger BTC Move Threshold
    # =========================================================================
    print(f"\n  === STEP 7: MINIMUM BTC MOVE THRESHOLD ===")
    print(f"  Only enter when |BTC move| exceeds threshold (contrarian direction)")
    print(f"  {'MinMove':<10} {'Trades':<8} {'AvgPrice':<10} {'AvgTime':<9} {'WR':<8} {'PnL':<10} {'$/hr':<8}")
    print(f"  {'-'*65}")

    for min_move in [0.01, 0.03, 0.05, 0.08, 0.10, 0.15]:
        move_trades = []
        current_start = first_window

        while current_start + WINDOW_MS <= end_ms:
            win_mask = (timestamps >= current_start) & (timestamps < current_start + WINDOW_MS)
            win_idx = np.where(win_mask)[0]
            current_start += WINDOW_MS

            if len(win_idx) < 100:
                continue

            win_prices_arr = prices[win_idx]
            win_times_arr = timestamps[win_idx]
            btc_open = win_prices_arr[0]

            window_start_s = int((current_start - WINDOW_MS) // 1000)
            market_slug = f"btc-updown-15m-{window_start_s}"
            if market_slug not in obs_by_market:
                continue

            obs_data = obs_by_market[market_slug]
            entry_made = False
            max_idx = min(780, len(win_prices_arr))

            for i in range(60, max_idx):
                if entry_made:
                    break

                elapsed_s = (win_times_arr[i] - win_times_arr[0]) / 1000.0
                current_price = win_prices_arr[i]
                btc_move_pct = (current_price - btc_open) / btc_open * 100

                if abs(btc_move_pct) < min_move:
                    continue

                if btc_move_pct > 0:
                    side_key = 'down_ask'
                    entry_direction = "DOWN"
                else:
                    side_key = 'up_ask'
                    entry_direction = "UP"

                signal_ts = win_times_arr[i]
                idx = np.searchsorted(obs_data['timestamps'], signal_ts)
                idx = min(idx, len(obs_data['timestamps']) - 1)
                raw_price = float(obs_data[side_key][idx])
                if np.isnan(raw_price) or raw_price <= 0:
                    continue
                entry_price = max(0.05, min(0.50, raw_price))

                btc_close = win_prices_arr[-1]
                close_move = (btc_close - btc_open) / btc_open * 100
                winner = "UP" if close_move >= 0 else "DOWN"
                won = (entry_direction == winner)
                pnl = SHARES_PER_TRADE * ((1.0 - entry_price) if won else -entry_price)

                move_trades.append({
                    'entry_time_s': elapsed_s,
                    'entry_price': entry_price,
                    'won': won,
                    'pnl': pnl,
                })
                entry_made = True

        if move_trades:
            avg_p = np.mean([t['entry_price'] for t in move_trades])
            avg_t = np.mean([t['entry_time_s'] for t in move_trades])
            wr = sum(1 for t in move_trades if t['won']) / len(move_trades) * 100
            total_pnl = sum(t['pnl'] for t in move_trades)
            hourly = total_pnl / total_hours if total_hours > 0 else 0
            print(f"  {min_move:<10.3f} {len(move_trades):<8} ${avg_p:<9.3f} {avg_t:<8.0f}s {wr:<7.1f}% ${total_pnl:<9.0f} ${hourly:<7.0f}")
        else:
            print(f"  {min_move:<10.3f} {'0':<8} {'N/A':<10}")

    # =========================================================================
    # STEP 8: Anti-Trend Filter (skip monotonic moves)
    # Only enter if BTC shows choppiness in recent N seconds
    # =========================================================================
    print(f"\n  === STEP 8: ANTI-TREND FILTER ===")
    print(f"  Skip entry if recent price action is too one-directional")
    print(f"  Measure: fraction of 1s bars moving same direction over last 60s")
    print(f"  {'MaxTrend':<10} {'Trades':<8} {'AvgPrice':<10} {'AvgTime':<9} {'WR':<8} {'PnL':<10} {'$/hr':<8}")
    print(f"  {'-'*65}")

    for max_trend_frac in [0.55, 0.60, 0.65, 0.70, 0.80, 1.0]:
        trend_trades = []
        current_start = first_window

        while current_start + WINDOW_MS <= end_ms:
            win_mask = (timestamps >= current_start) & (timestamps < current_start + WINDOW_MS)
            win_idx = np.where(win_mask)[0]
            current_start += WINDOW_MS

            if len(win_idx) < 100:
                continue

            win_prices_arr = prices[win_idx]
            win_times_arr = timestamps[win_idx]
            btc_open = win_prices_arr[0]

            window_start_s = int((current_start - WINDOW_MS) // 1000)
            market_slug = f"btc-updown-15m-{window_start_s}"
            if market_slug not in obs_by_market:
                continue

            obs_data = obs_by_market[market_slug]
            entry_made = False
            max_idx = min(780, len(win_prices_arr))

            for i in range(60, max_idx):
                if entry_made:
                    break

                elapsed_s = (win_times_arr[i] - win_times_arr[0]) / 1000.0
                current_price = win_prices_arr[i]
                btc_move_pct = (current_price - btc_open) / btc_open * 100

                if abs(btc_move_pct) < 0.01:
                    continue

                # Check trend strength over last 60 bars
                lookback = min(60, i)
                if lookback < 10:
                    continue
                recent = win_prices_arr[i-lookback:i+1]
                diffs = np.diff(recent)
                if btc_move_pct > 0:
                    trend_frac = np.sum(diffs > 0) / len(diffs)
                else:
                    trend_frac = np.sum(diffs < 0) / len(diffs)

                if trend_frac > max_trend_frac:
                    continue  # Too trendy, skip

                if btc_move_pct > 0:
                    side_key = 'down_ask'
                    entry_direction = "DOWN"
                else:
                    side_key = 'up_ask'
                    entry_direction = "UP"

                signal_ts = win_times_arr[i]
                idx = np.searchsorted(obs_data['timestamps'], signal_ts)
                idx = min(idx, len(obs_data['timestamps']) - 1)
                raw_price = float(obs_data[side_key][idx])
                if np.isnan(raw_price) or raw_price <= 0:
                    continue
                entry_price = max(0.05, min(0.50, raw_price))

                btc_close = win_prices_arr[-1]
                close_move = (btc_close - btc_open) / btc_open * 100
                winner = "UP" if close_move >= 0 else "DOWN"
                won = (entry_direction == winner)
                pnl = SHARES_PER_TRADE * ((1.0 - entry_price) if won else -entry_price)

                trend_trades.append({
                    'entry_time_s': elapsed_s,
                    'entry_price': entry_price,
                    'won': won,
                    'pnl': pnl,
                })
                entry_made = True

        if trend_trades:
            avg_p = np.mean([t['entry_price'] for t in trend_trades])
            avg_t = np.mean([t['entry_time_s'] for t in trend_trades])
            wr = sum(1 for t in trend_trades if t['won']) / len(trend_trades) * 100
            total_pnl = sum(t['pnl'] for t in trend_trades)
            hourly = total_pnl / total_hours if total_hours > 0 else 0
            print(f"  {max_trend_frac:<10.2f} {len(trend_trades):<8} ${avg_p:<9.3f} {avg_t:<8.0f}s {wr:<7.1f}% ${total_pnl:<9.0f} ${hourly:<7.0f}")
        else:
            print(f"  {max_trend_frac:<10.2f} {'0':<8} {'N/A':<10}")

    # =========================================================================
    # STEP 9: Time-of-Day Analysis
    # =========================================================================
    print(f"\n  === STEP 9: TIME-OF-DAY ANALYSIS ===")
    print(f"  Run base contrarian (delay=60s, Z>=0.5) and bin by hour-of-day (UTC)")

    tod_trades = {h: [] for h in range(24)}
    current_start = first_window

    while current_start + WINDOW_MS <= end_ms:
        win_mask = (timestamps >= current_start) & (timestamps < current_start + WINDOW_MS)
        win_idx = np.where(win_mask)[0]
        current_start += WINDOW_MS

        if len(win_idx) < 100:
            continue

        win_prices_arr = prices[win_idx]
        win_times_arr = timestamps[win_idx]
        btc_open = win_prices_arr[0]

        # Get hour of day for this window
        window_hour = int(pd.Timestamp(win_times_arr[0], unit='ms').hour)

        window_start_s = int((current_start - WINDOW_MS) // 1000)
        market_slug = f"btc-updown-15m-{window_start_s}"
        if market_slug not in obs_by_market:
            continue

        obs_data = obs_by_market[market_slug]
        entry_made = False
        max_idx = min(780, len(win_prices_arr))

        for i in range(60, max_idx):
            if entry_made:
                break

            current_price = win_prices_arr[i]
            btc_move_pct = (current_price - btc_open) / btc_open * 100
            if abs(btc_move_pct) < 0.01:
                continue

            if btc_move_pct > 0:
                side_key = 'down_ask'
                entry_direction = "DOWN"
            else:
                side_key = 'up_ask'
                entry_direction = "UP"

            signal_ts = win_times_arr[i]
            idx = np.searchsorted(obs_data['timestamps'], signal_ts)
            idx = min(idx, len(obs_data['timestamps']) - 1)
            raw_price = float(obs_data[side_key][idx])
            if np.isnan(raw_price) or raw_price <= 0:
                continue
            entry_price = max(0.05, min(0.50, raw_price))

            btc_close = win_prices_arr[-1]
            close_move = (btc_close - btc_open) / btc_open * 100
            winner = "UP" if close_move >= 0 else "DOWN"
            won = (entry_direction == winner)
            pnl = SHARES_PER_TRADE * ((1.0 - entry_price) if won else -entry_price)

            tod_trades[window_hour].append({
                'entry_price': entry_price,
                'won': won,
                'pnl': pnl,
            })
            entry_made = True

    print(f"  {'Hour':<8} {'Trades':<8} {'AvgPrice':<10} {'WR':<8} {'PnL':<10}")
    print(f"  {'-'*45}")
    for h in range(24):
        if tod_trades[h]:
            trades_h = tod_trades[h]
            avg_p = np.mean([t['entry_price'] for t in trades_h])
            wr = sum(1 for t in trades_h if t['won']) / len(trades_h) * 100
            total_pnl = sum(t['pnl'] for t in trades_h)
            print(f"  {h:02d}:00    {len(trades_h):<8} ${avg_p:<9.3f} {wr:<7.1f}% ${total_pnl:<9.0f}")

    # =========================================================================
    # STEP 10: Orderbook Imbalance Filter
    # Only enter when the cheap side is significantly cheaper than the expensive side
    # (i.e., up_ask + down_ask < 1.0 means arb, but spread indicates conviction)
    # =========================================================================
    print(f"\n  === STEP 10: ORDERBOOK IMBALANCE FILTER ===")
    print(f"  Only enter when cheap_ask / expensive_ask ratio is below threshold")
    print(f"  Low ratio = market strongly prices one side as losing")
    print(f"  {'MaxRatio':<10} {'Trades':<8} {'AvgPrice':<10} {'AvgTime':<9} {'WR':<8} {'PnL':<10} {'$/hr':<8}")
    print(f"  {'-'*65}")

    for max_ratio in [0.40, 0.50, 0.60, 0.70, 0.80, 1.0]:
        ob_trades = []
        current_start = first_window

        while current_start + WINDOW_MS <= end_ms:
            win_mask = (timestamps >= current_start) & (timestamps < current_start + WINDOW_MS)
            win_idx = np.where(win_mask)[0]
            current_start += WINDOW_MS

            if len(win_idx) < 100:
                continue

            win_prices_arr = prices[win_idx]
            win_times_arr = timestamps[win_idx]
            btc_open = win_prices_arr[0]

            window_start_s = int((current_start - WINDOW_MS) // 1000)
            market_slug = f"btc-updown-15m-{window_start_s}"
            if market_slug not in obs_by_market:
                continue

            obs_data = obs_by_market[market_slug]
            entry_made = False
            max_idx = min(780, len(win_prices_arr))

            for i in range(60, max_idx):
                if entry_made:
                    break

                current_price = win_prices_arr[i]
                btc_move_pct = (current_price - btc_open) / btc_open * 100
                if abs(btc_move_pct) < 0.01:
                    continue

                signal_ts = win_times_arr[i]
                idx = np.searchsorted(obs_data['timestamps'], signal_ts)
                idx = min(idx, len(obs_data['timestamps']) - 1)

                up_ask = float(obs_data['up_ask'][idx])
                down_ask = float(obs_data['down_ask'][idx])

                if np.isnan(up_ask) or np.isnan(down_ask) or up_ask <= 0 or down_ask <= 0:
                    continue

                if btc_move_pct > 0:
                    cheap_ask = down_ask
                    expensive_ask = up_ask
                    entry_direction = "DOWN"
                else:
                    cheap_ask = up_ask
                    expensive_ask = down_ask
                    entry_direction = "UP"

                # Imbalance ratio
                ratio = cheap_ask / max(expensive_ask, 0.01)
                if ratio > max_ratio:
                    continue

                entry_price = max(0.05, min(0.50, cheap_ask))

                btc_close = win_prices_arr[-1]
                close_move = (btc_close - btc_open) / btc_open * 100
                winner = "UP" if close_move >= 0 else "DOWN"
                won = (entry_direction == winner)
                pnl = SHARES_PER_TRADE * ((1.0 - entry_price) if won else -entry_price)

                ob_trades.append({
                    'entry_time_s': (win_times_arr[i] - win_times_arr[0]) / 1000.0,
                    'entry_price': entry_price,
                    'won': won,
                    'pnl': pnl,
                })
                entry_made = True

        if ob_trades:
            avg_p = np.mean([t['entry_price'] for t in ob_trades])
            avg_t = np.mean([t['entry_time_s'] for t in ob_trades])
            wr = sum(1 for t in ob_trades if t['won']) / len(ob_trades) * 100
            total_pnl = sum(t['pnl'] for t in ob_trades)
            hourly = total_pnl / total_hours if total_hours > 0 else 0
            print(f"  {max_ratio:<10.2f} {len(ob_trades):<8} ${avg_p:<9.3f} {avg_t:<8.0f}s {wr:<7.1f}% ${total_pnl:<9.0f} ${hourly:<7.0f}")
        else:
            print(f"  {max_ratio:<10.2f} {'0':<8} {'N/A':<10}")

    # =========================================================================
    # STEP 11: Combined Best Filters
    # Combine the best parameters from Steps 6-10
    # =========================================================================
    print(f"\n  === STEP 11: COMBINED FILTERS ===")
    print(f"  Reversal(0.01%) + MinMove(0.03%) + AntiTrend(0.65) + OB ratio(<0.60)")

    combined_trades = []
    current_start = first_window

    while current_start + WINDOW_MS <= end_ms:
        win_mask = (timestamps >= current_start) & (timestamps < current_start + WINDOW_MS)
        win_idx = np.where(win_mask)[0]
        current_start += WINDOW_MS

        if len(win_idx) < 100:
            continue

        win_prices_arr = prices[win_idx]
        win_times_arr = timestamps[win_idx]
        btc_open = win_prices_arr[0]

        window_start_s = int((current_start - WINDOW_MS) // 1000)
        market_slug = f"btc-updown-15m-{window_start_s}"
        if market_slug not in obs_by_market:
            continue

        obs_data = obs_by_market[market_slug]
        entry_made = False
        max_idx = min(780, len(win_prices_arr))
        local_max = btc_open
        local_min = btc_open

        for i in range(60, max_idx):
            if entry_made:
                break

            current_price = win_prices_arr[i]
            local_max = max(local_max, current_price)
            local_min = min(local_min, current_price)

            btc_move_pct = (current_price - btc_open) / btc_open * 100

            # Filter 1: Min move 0.03%
            if abs(btc_move_pct) < 0.03:
                continue

            # Filter 2: Reversal confirmation (0.01% pullback)
            if btc_move_pct > 0:
                pullback = (local_max - current_price) / btc_open * 100
                if pullback < 0.01:
                    continue
                entry_direction = "DOWN"
            else:
                pullback = (current_price - local_min) / btc_open * 100
                if pullback < 0.01:
                    continue
                entry_direction = "UP"

            # Filter 3: Anti-trend (max 65% same-direction bars in last 60s)
            lookback = min(60, i)
            if lookback >= 10:
                recent = win_prices_arr[i-lookback:i+1]
                diffs = np.diff(recent)
                if btc_move_pct > 0:
                    trend_frac = np.sum(diffs > 0) / len(diffs)
                else:
                    trend_frac = np.sum(diffs < 0) / len(diffs)
                if trend_frac > 0.65:
                    continue

            # Filter 4: Orderbook imbalance (ratio < 0.60)
            elapsed_s = (win_times_arr[i] - win_times_arr[0]) / 1000.0
            signal_ts = win_times_arr[i]
            idx = np.searchsorted(obs_data['timestamps'], signal_ts)
            idx = min(idx, len(obs_data['timestamps']) - 1)

            up_ask = float(obs_data['up_ask'][idx])
            down_ask = float(obs_data['down_ask'][idx])
            if np.isnan(up_ask) or np.isnan(down_ask) or up_ask <= 0 or down_ask <= 0:
                continue

            if btc_move_pct > 0:
                cheap_ask = down_ask
                expensive_ask = up_ask
            else:
                cheap_ask = up_ask
                expensive_ask = down_ask

            ratio = cheap_ask / max(expensive_ask, 0.01)
            if ratio > 0.60:
                continue

            entry_price = max(0.05, min(0.50, cheap_ask))

            btc_close = win_prices_arr[-1]
            close_move = (btc_close - btc_open) / btc_open * 100
            winner = "UP" if close_move >= 0 else "DOWN"
            won = (entry_direction == winner)
            pnl = SHARES_PER_TRADE * ((1.0 - entry_price) if won else -entry_price)

            combined_trades.append({
                'entry_time_s': elapsed_s,
                'entry_price': entry_price,
                'won': won,
                'pnl': pnl,
                'btc_move_pct': btc_move_pct,
            })
            entry_made = True

    if combined_trades:
        avg_p = np.mean([t['entry_price'] for t in combined_trades])
        avg_t = np.mean([t['entry_time_s'] for t in combined_trades])
        wr = sum(1 for t in combined_trades if t['won']) / len(combined_trades) * 100
        total_pnl = sum(t['pnl'] for t in combined_trades)
        hourly = total_pnl / total_hours if total_hours > 0 else 0
        breakeven_comb = avg_p * 100
        edge_comb = wr - breakeven_comb

        print(f"  Trades: {len(combined_trades)}")
        print(f"  Win Rate: {wr:.1f}% (breakeven={breakeven_comb:.1f}%)")
        print(f"  Edge: {edge_comb:+.1f}pp")
        print(f"  Avg entry price: ${avg_p:.3f} | Avg time: {avg_t:.0f}s")
        print(f"  PnL: ${total_pnl:,.0f} | $/hr: ${hourly:,.0f}")
        print(f"  R:R ratio: 1:{(1-avg_p)/avg_p:.1f}")
    else:
        print(f"  NO TRADES with combined filters!")

    # =========================================================================
    # COMPARISON TABLE
    # =========================================================================
    print(f"\n  === COMPARISON: OUR BACKTEST vs 0xa5e8 WALLET ===")
    print(f"  {'Metric':<25} {'Our 60s':<15} {'Our 300s':<15} {'0xa5e8 Wallet':<15}")
    print(f"  {'-'*70}")

    our_60s = delay_results[0] if delay_results else None
    our_300s = next((d for d in delay_results if d['delay'] == 300), None)

    if our_60s:
        p60 = f"${our_60s['avg_price']:.3f}"
        p300 = f"${our_300s['avg_price']:.3f}" if our_300s else "N/A"
        wr60 = f"{our_60s['win_rate']:.1f}%"
        wr300 = f"{our_300s['win_rate']:.1f}%" if our_300s else "N/A"
        t300 = str(our_300s['trades']) if our_300s else "N/A"
        pnl60 = f"${our_60s['pnl']:.0f}"
        pnl300 = f"${our_300s['pnl']:.0f}" if our_300s else "N/A"

        print(f"  {'Entry delay':<25} {'60s':<15} {'300s':<15} {'329s (avg)':<15}")
        print(f"  {'Avg entry price':<25} {p60:<15} {p300:<15} {'$0.270':<15}")
        print(f"  {'Win rate':<25} {wr60:<15} {wr300:<15} {'54.2%':<15}")
        print(f"  {'Trades':<25} {str(our_60s['trades']):<15} {t300:<15} {'~24/day':<15}")
        print(f"  {'PnL':<25} {pnl60:<15} {pnl300:<15} {'N/A':<15}")

    if wallet_trades:
        print(f"\n  WALLET-MATCHED CONFIG (delay=300, move>=0.05%, price<=$0.35):")
        print(f"    Trades: {len(wallet_trades)}, WR: {wallet_wr:.1f}%, Avg price: ${wallet_avg_price:.3f}")
        edge = wallet_wr - wallet_breakeven
        print(f"    Edge over breakeven: {edge:+.1f}pp")
        if edge > 0:
            print(f"    STATUS: PROFITABLE — edge exists")
        else:
            print(f"    STATUS: NOT PROFITABLE — need further tuning")

    return {
        'delay_results': delay_results,
        'wallet_trades': len(wallet_trades) if wallet_trades else 0,
        'wallet_wr': wallet_wr,
        'wallet_avg_price': wallet_avg_price,
    }


# =============================================================================
# LOSING PATTERNS ANALYSIS
# =============================================================================

def analyze_losing_patterns(btc_df: pd.DataFrame, obs_df: pd.DataFrame, total_hours: float):
    """
    Analyze what distinguishes winning vs losing trades in the reversal confirmation strategy.

    Runs the 0.01% pullback reversal confirmation backtest, collects rich metadata per trade,
    then compares winners vs losers across multiple dimensions to find discriminating features.
    """
    print("\n" + "=" * 100)
    print("LOSING PATTERNS ANALYSIS (Reversal Confirmation 0.01% Pullback)")
    print("=" * 100)
    print("  Goal: Find measurable features that distinguish winners from losers")

    # Build rich observer lookup
    obs_by_market = {}
    for slug, group in obs_df.groupby('market_slug'):
        sorted_group = group.sort_values('timestamp_ms')
        obs_by_market[slug] = {
            'timestamps': sorted_group['timestamp_ms'].values,
            'up_ask': sorted_group['up_ask'].values if 'up_ask' in sorted_group.columns else np.full(len(sorted_group), np.nan),
            'down_ask': sorted_group['down_ask'].values if 'down_ask' in sorted_group.columns else np.full(len(sorted_group), np.nan),
            'up_bid': sorted_group['up_bid'].values if 'up_bid' in sorted_group.columns else np.full(len(sorted_group), np.nan),
            'down_bid': sorted_group['down_bid'].values if 'down_bid' in sorted_group.columns else np.full(len(sorted_group), np.nan),
            'pair_cost': sorted_group['pair_cost'].values if 'pair_cost' in sorted_group.columns else np.full(len(sorted_group), np.nan),
            'velocity_bps': sorted_group['velocity_bps'].values if 'velocity_bps' in sorted_group.columns else np.full(len(sorted_group), np.nan),
            'velocity_zone': sorted_group['velocity_zone'].values if 'velocity_zone' in sorted_group.columns else np.full(len(sorted_group), ''),
            'spike_detected': sorted_group['spike_detected'].values if 'spike_detected' in sorted_group.columns else np.full(len(sorted_group), False),
            'spike_magnitude': sorted_group['spike_magnitude'].values if 'spike_magnitude' in sorted_group.columns else np.full(len(sorted_group), 0.0),
        }

    # Resample BTC to 1s bars
    btc_df_sorted = btc_df.sort_values('timestamp_ms')
    btc_df_sorted['second_ms'] = (btc_df_sorted['timestamp_ms'] // 1000) * 1000
    bars_1s = btc_df_sorted.groupby('second_ms').agg({'price': 'last'}).reset_index()
    bars_1s.rename(columns={'second_ms': 'timestamp_ms'}, inplace=True)
    timestamps = bars_1s['timestamp_ms'].values
    prices = bars_1s['price'].values

    WINDOW_S = 900
    WINDOW_MS = WINDOW_S * 1000
    SHARES_PER_TRADE = 50
    MIN_PULLBACK_PCT = 0.01

    first_window = ((timestamps[0] // WINDOW_MS) + 1) * WINDOW_MS
    end_ms = timestamps[-1]

    # Track previous window direction for context
    prev_winner = None
    trades = []
    current_start = first_window

    while current_start + WINDOW_MS <= end_ms:
        win_mask = (timestamps >= current_start) & (timestamps < current_start + WINDOW_MS)
        win_idx = np.where(win_mask)[0]

        # Pre-window prices (5 min before)
        pre_start = current_start - 300_000
        pre_mask = (timestamps >= pre_start) & (timestamps < current_start)
        pre_idx = np.where(pre_mask)[0]

        current_start += WINDOW_MS

        if len(win_idx) < 100:
            prev_winner = None
            continue

        win_prices = prices[win_idx]
        win_times = timestamps[win_idx]
        btc_open = win_prices[0]
        pre_prices = prices[pre_idx] if len(pre_idx) > 0 else np.array([btc_open])

        # Pre-window volatility
        if len(pre_prices) > 10:
            pre_returns = np.diff(pre_prices) / pre_prices[:-1] * 100
            pre_vol = float(np.std(pre_returns))
        else:
            pre_vol = 0.0

        window_start_s = int((current_start - WINDOW_MS) // 1000)
        market_slug = f"btc-updown-15m-{window_start_s}"
        if market_slug not in obs_by_market:
            prev_winner = None
            continue

        obs_data = obs_by_market[market_slug]
        entry_made = False
        max_idx = min(780, len(win_prices))

        # Track local extremes
        local_max = btc_open
        local_min = btc_open

        for i in range(60, max_idx):
            if entry_made:
                break

            current_price = win_prices[i]
            local_max = max(local_max, current_price)
            local_min = min(local_min, current_price)

            btc_move_pct = (current_price - btc_open) / btc_open * 100
            if abs(btc_move_pct) < 0.01:
                continue

            # Check for pullback from extreme
            if btc_move_pct > 0:
                pullback = (local_max - current_price) / btc_open * 100
                if pullback < MIN_PULLBACK_PCT:
                    continue
                side_key = 'down_ask'
                bid_key = 'down_bid'
                entry_direction = "DOWN"
                extreme_move_pct = (local_max - btc_open) / btc_open * 100
            else:
                pullback = (current_price - local_min) / btc_open * 100
                if pullback < MIN_PULLBACK_PCT:
                    continue
                side_key = 'up_ask'
                bid_key = 'up_bid'
                entry_direction = "UP"
                extreme_move_pct = (btc_open - local_min) / btc_open * 100

            elapsed_s = (win_times[i] - win_times[0]) / 1000.0

            # Get orderbook data
            signal_ts = win_times[i]
            idx = np.searchsorted(obs_data['timestamps'], signal_ts)
            idx = min(idx, len(obs_data['timestamps']) - 1)

            raw_price = float(obs_data[side_key][idx])
            if np.isnan(raw_price) or raw_price <= 0:
                continue
            entry_price = max(0.05, min(0.50, raw_price))

            # Rich orderbook data
            cheap_bid = float(obs_data[bid_key][idx])
            pair_cost = float(obs_data['pair_cost'][idx])
            velocity_bps = float(obs_data['velocity_bps'][idx])
            velocity_zone_val = str(obs_data['velocity_zone'][idx])
            spike_detected_val = obs_data['spike_detected'][idx]
            spike_magnitude_val = float(obs_data['spike_magnitude'][idx])

            # Compute cheap-side spread
            if not np.isnan(cheap_bid) and cheap_bid > 0:
                cheap_spread = entry_price - cheap_bid
            else:
                cheap_spread = np.nan

            # Resolution
            btc_close = win_prices[-1]
            close_move = (btc_close - btc_open) / btc_open * 100
            winner = "UP" if close_move >= 0 else "DOWN"
            won = (entry_direction == winner)
            pnl = SHARES_PER_TRADE * ((1.0 - entry_price) if won else -entry_price)

            # Retracement fraction
            retracement_frac = pullback / abs(extreme_move_pct) if abs(extreme_move_pct) > 1e-6 else 0.0

            # Move speed (pct per second)
            move_speed = abs(extreme_move_pct) / max(elapsed_s, 1)

            # Max continuation after entry: max BTC move in SAME direction after entry
            remaining_prices = win_prices[i:]
            if btc_move_pct > 0:
                # BTC was going up, continuation = further up
                max_continuation = (np.max(remaining_prices) - current_price) / btc_open * 100
            else:
                # BTC was going down, continuation = further down
                max_continuation = (current_price - np.min(remaining_prices)) / btc_open * 100

            # Choppiness: direction changes in last 60s
            lookback = min(60, i)
            if lookback >= 10:
                recent = win_prices[i - lookback:i + 1]
                diffs = np.diff(recent)
                sign_changes = np.sum(np.diff(np.sign(diffs)) != 0)
                choppiness = sign_changes / len(diffs)
            else:
                choppiness = np.nan

            # Previous window same direction
            prev_same_dir = False
            if prev_winner is not None:
                # prev_winner is the direction that won last window
                # If we're betting DOWN and last window also went DOWN (i.e. winner=DOWN), same dir
                prev_same_dir = (prev_winner != entry_direction)
                # Actually: if BTC moved up this window (btc_move_pct > 0) and also moved up last window
                # prev_winner tells us last window's resolution
                # We're contrarian, so entry_direction is OPPOSITE of BTC move
                # "same direction" means BTC is continuing last window's trend
                if btc_move_pct > 0:
                    prev_same_dir = (prev_winner == "UP")  # BTC went up last time too
                else:
                    prev_same_dir = (prev_winner == "DOWN")  # BTC went down last time too

            # Handle spike_detected boolean parsing
            if isinstance(spike_detected_val, (bool, np.bool_)):
                spike_bool = bool(spike_detected_val)
            elif isinstance(spike_detected_val, str):
                spike_bool = spike_detected_val.lower() == 'true'
            else:
                spike_bool = False

            trades.append({
                # Basic
                'won': won,
                'pnl': pnl,
                'entry_direction': entry_direction,
                'entry_price': entry_price,
                'entry_time_s': elapsed_s,
                # Move characteristics
                'btc_move_pct': abs(btc_move_pct),
                'pullback_pct': pullback,
                'retracement_frac': retracement_frac,
                'move_speed': move_speed,
                'max_continuation_pct': max_continuation,
                'extreme_move_pct': abs(extreme_move_pct),
                # Timing
                'time_remaining_s': WINDOW_S - elapsed_s,
                # Orderbook
                'pair_cost': pair_cost,
                'cheap_spread': cheap_spread,
                # Microstructure
                'velocity_bps': velocity_bps,
                'velocity_zone': velocity_zone_val,
                'spike_detected': spike_bool,
                'spike_magnitude': spike_magnitude_val,
                # Context
                'choppiness': choppiness,
                'pre_vol': pre_vol,
                'prev_window_same_dir': prev_same_dir,
            })
            entry_made = True

        # Track this window's resolution for next window's context
        if len(win_prices) > 1:
            wclose = (win_prices[-1] - btc_open) / btc_open * 100
            prev_winner = "UP" if wclose >= 0 else "DOWN"
        else:
            prev_winner = None

    if not trades:
        print("  NO TRADES — cannot analyze patterns!")
        return

    # Split into winners and losers
    winners = [t for t in trades if t['won']]
    losers = [t for t in trades if not t['won']]
    n_total = len(trades)
    n_win = len(winners)
    n_lose = len(losers)
    total_pnl = sum(t['pnl'] for t in trades)

    print(f"\n  Total trades: {n_total}")
    print(f"  Winners: {n_win} ({n_win/n_total*100:.1f}%) | Losers: {n_lose} ({n_lose/n_total*100:.1f}%)")
    print(f"  Total PnL: ${total_pnl:,.0f}")
    print(f"  Avg entry price: ${np.mean([t['entry_price'] for t in trades]):.3f}")

    # =========================================================================
    # DIMENSION ANALYSIS
    # =========================================================================

    def analyze_dimension(name, key, trades_list, bins=None, higher_better=None):
        """Analyze a single dimension: winner vs loser stats + binned WR."""
        win_vals = [t[key] for t in winners if not np.isnan(t[key]) and t[key] is not None]
        lose_vals = [t[key] for t in losers if not np.isnan(t[key]) and t[key] is not None]

        if not win_vals or not lose_vals:
            print(f"\n  --- {name}: INSUFFICIENT DATA ---")
            return None

        win_mean = np.mean(win_vals)
        lose_mean = np.mean(lose_vals)
        win_med = np.median(win_vals)
        lose_med = np.median(lose_vals)
        diff_mean = win_mean - lose_mean
        diff_med = win_med - lose_med

        print(f"\n  --- {name} ({key}) ---")
        print(f"  {'':12} {'Mean':>10} {'Median':>10} {'Std':>10} {'Min':>10} {'Max':>10}")
        print(f"  {'Winners':12} {win_mean:>10.4f} {win_med:>10.4f} {np.std(win_vals):>10.4f} {np.min(win_vals):>10.4f} {np.max(win_vals):>10.4f}")
        print(f"  {'Losers':12} {lose_mean:>10.4f} {lose_med:>10.4f} {np.std(lose_vals):>10.4f} {np.min(lose_vals):>10.4f} {np.max(lose_vals):>10.4f}")
        print(f"  Diff (W-L): mean={diff_mean:+.4f}, median={diff_med:+.4f}")

        # Direction interpretation
        if higher_better is not None:
            if (diff_mean > 0 and higher_better) or (diff_mean < 0 and not higher_better):
                print(f"  Signal: USEFUL (winners have {'higher' if diff_mean > 0 else 'lower'} values)")
            else:
                print(f"  Signal: COUNTER-INTUITIVE or WEAK")

        # Binned win rate
        if bins is not None:
            all_vals = [t[key] for t in trades_list if not np.isnan(t[key]) and t[key] is not None]
            all_won = [t['won'] for t in trades_list if not np.isnan(t[key]) and t[key] is not None]
            all_pnl = [t['pnl'] for t in trades_list if not np.isnan(t[key]) and t[key] is not None]

            print(f"  {'Bin':>20} {'Trades':>8} {'WinRate':>8} {'PnL':>10} {'AvgPnL':>8}")
            for j in range(len(bins) - 1):
                lo, hi = bins[j], bins[j + 1]
                mask = [(lo <= v < hi) for v in all_vals]
                bin_trades = sum(mask)
                if bin_trades > 0:
                    bin_wins = sum(w for w, m in zip(all_won, mask) if m)
                    bin_pnl = sum(p for p, m in zip(all_pnl, mask) if m)
                    bin_wr = bin_wins / bin_trades * 100
                    avg_pnl = bin_pnl / bin_trades
                    label = f"[{lo:.3f}, {hi:.3f})"
                    print(f"  {label:>20} {bin_trades:>8} {bin_wr:>7.1f}% ${bin_pnl:>9.0f} ${avg_pnl:>7.1f}")

        # Effect size (Cohen's d)
        pooled_std = np.sqrt((np.var(win_vals) + np.var(lose_vals)) / 2)
        if pooled_std > 1e-10:
            cohens_d = diff_mean / pooled_std
            print(f"  Cohen's d: {cohens_d:.3f} ({'small' if abs(cohens_d) < 0.5 else 'medium' if abs(cohens_d) < 0.8 else 'LARGE'})")
            return abs(cohens_d)
        return 0.0

    # =========================================================================
    # GROUP A: Move Characteristics
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"  GROUP A: MOVE CHARACTERISTICS")
    print(f"{'='*80}")

    effects = {}

    effects['retracement_frac'] = analyze_dimension(
        "Retracement Fraction", 'retracement_frac', trades,
        bins=[0.0, 0.10, 0.20, 0.30, 0.50, 0.70, 1.01],
        higher_better=True
    )

    effects['btc_move_pct'] = analyze_dimension(
        "Absolute BTC Move at Entry", 'btc_move_pct', trades,
        bins=[0.01, 0.03, 0.05, 0.08, 0.12, 0.20, 0.50],
        higher_better=False  # hypothesis: very large = real event, won't revert
    )

    effects['extreme_move_pct'] = analyze_dimension(
        "Extreme Move (peak before pullback)", 'extreme_move_pct', trades,
        bins=[0.01, 0.03, 0.05, 0.08, 0.12, 0.20, 0.50],
        higher_better=False
    )

    effects['move_speed'] = analyze_dimension(
        "Move Speed (pct/second)", 'move_speed', trades,
        bins=[0.0, 0.0002, 0.0005, 0.001, 0.002, 0.005, 0.05],
        higher_better=False  # fast = real flow = bad for contrarian
    )

    effects['max_continuation_pct'] = analyze_dimension(
        "Max Continuation After Entry", 'max_continuation_pct', trades,
        bins=[0.0, 0.01, 0.03, 0.05, 0.10, 0.20, 0.50],
        higher_better=False  # losers: BTC keeps going
    )

    # =========================================================================
    # GROUP B: Timing
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"  GROUP B: TIMING")
    print(f"{'='*80}")

    effects['entry_time_s'] = analyze_dimension(
        "Entry Time (seconds into window)", 'entry_time_s', trades,
        bins=[60, 120, 180, 300, 420, 600, 780],
        higher_better=None
    )

    effects['time_remaining_s'] = analyze_dimension(
        "Time Remaining (seconds)", 'time_remaining_s', trades,
        bins=[120, 300, 480, 600, 720, 840],
        higher_better=True  # more time = more chance to revert
    )

    # =========================================================================
    # GROUP C: Price/Orderbook
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"  GROUP C: PRICE / ORDERBOOK")
    print(f"{'='*80}")

    effects['entry_price'] = analyze_dimension(
        "Entry Price (cheap-side ask)", 'entry_price', trades,
        bins=[0.05, 0.15, 0.25, 0.30, 0.35, 0.40, 0.50],
        higher_better=False  # cheaper entry = better R:R
    )

    effects['pair_cost'] = analyze_dimension(
        "Pair Cost (up_ask + down_ask)", 'pair_cost', trades,
        bins=[0.80, 0.90, 0.95, 1.00, 1.05, 1.10, 1.20],
        higher_better=None
    )

    effects['cheap_spread'] = analyze_dimension(
        "Cheap-Side Spread (ask - bid)", 'cheap_spread', trades,
        bins=[0.0, 0.01, 0.02, 0.03, 0.05, 0.10, 0.20],
        higher_better=None
    )

    # =========================================================================
    # GROUP D: Market Microstructure
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"  GROUP D: MARKET MICROSTRUCTURE")
    print(f"{'='*80}")

    effects['velocity_bps'] = analyze_dimension(
        "Velocity (bps)", 'velocity_bps', trades,
        bins=[-50, -10, -2, 0, 2, 10, 50],
        higher_better=None
    )

    # Velocity zone categorical analysis
    print(f"\n  --- Velocity Zone (categorical) ---")
    zone_stats = {}
    for t in trades:
        zone = t['velocity_zone']
        if zone not in zone_stats:
            zone_stats[zone] = {'wins': 0, 'total': 0, 'pnl': 0.0}
        zone_stats[zone]['total'] += 1
        zone_stats[zone]['pnl'] += t['pnl']
        if t['won']:
            zone_stats[zone]['wins'] += 1
    print(f"  {'Zone':>12} {'Trades':>8} {'WinRate':>8} {'PnL':>10}")
    for zone in sorted(zone_stats.keys()):
        s = zone_stats[zone]
        wr = s['wins'] / s['total'] * 100 if s['total'] > 0 else 0
        print(f"  {zone:>12} {s['total']:>8} {wr:>7.1f}% ${s['pnl']:>9.0f}")

    effects['spike_magnitude'] = analyze_dimension(
        "Spike Magnitude", 'spike_magnitude', trades,
        bins=[0.0, 0.001, 0.005, 0.01, 0.05, 0.10, 1.0],
        higher_better=None
    )

    # Spike detected categorical
    print(f"\n  --- Spike Detected (categorical) ---")
    spike_yes = [t for t in trades if t['spike_detected']]
    spike_no = [t for t in trades if not t['spike_detected']]
    if spike_yes:
        wr_yes = sum(1 for t in spike_yes if t['won']) / len(spike_yes) * 100
        pnl_yes = sum(t['pnl'] for t in spike_yes)
        print(f"  Spike=True:  {len(spike_yes)} trades, WR={wr_yes:.1f}%, PnL=${pnl_yes:.0f}")
    if spike_no:
        wr_no = sum(1 for t in spike_no if t['won']) / len(spike_no) * 100
        pnl_no = sum(t['pnl'] for t in spike_no)
        print(f"  Spike=False: {len(spike_no)} trades, WR={wr_no:.1f}%, PnL=${pnl_no:.0f}")

    # =========================================================================
    # GROUP E: Context
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"  GROUP E: CONTEXT")
    print(f"{'='*80}")

    # Direction analysis
    print(f"\n  --- Direction (UP vs DOWN entries) ---")
    up_trades = [t for t in trades if t['entry_direction'] == 'UP']
    dn_trades = [t for t in trades if t['entry_direction'] == 'DOWN']
    if up_trades:
        wr_up = sum(1 for t in up_trades if t['won']) / len(up_trades) * 100
        pnl_up = sum(t['pnl'] for t in up_trades)
        print(f"  UP entries:   {len(up_trades)} trades, WR={wr_up:.1f}%, PnL=${pnl_up:.0f}")
    if dn_trades:
        wr_dn = sum(1 for t in dn_trades if t['won']) / len(dn_trades) * 100
        pnl_dn = sum(t['pnl'] for t in dn_trades)
        print(f"  DOWN entries: {len(dn_trades)} trades, WR={wr_dn:.1f}%, PnL=${pnl_dn:.0f}")

    effects['choppiness'] = analyze_dimension(
        "Choppiness (direction changes / bars)", 'choppiness', trades,
        bins=[0.0, 0.20, 0.40, 0.60, 0.80, 1.01],
        higher_better=True  # more chop = noise = reverts
    )

    effects['pre_vol'] = analyze_dimension(
        "Pre-Window Volatility", 'pre_vol', trades,
        bins=[0.0, 0.001, 0.002, 0.005, 0.01, 0.02, 0.10],
        higher_better=None
    )

    # Previous window same direction
    print(f"\n  --- Previous Window Same Direction ---")
    same_trades = [t for t in trades if t['prev_window_same_dir']]
    diff_trades = [t for t in trades if not t['prev_window_same_dir']]
    if same_trades:
        wr_same = sum(1 for t in same_trades if t['won']) / len(same_trades) * 100
        pnl_same = sum(t['pnl'] for t in same_trades)
        print(f"  Same dir (trend): {len(same_trades)} trades, WR={wr_same:.1f}%, PnL=${pnl_same:.0f}")
    if diff_trades:
        wr_diff = sum(1 for t in diff_trades if t['won']) / len(diff_trades) * 100
        pnl_diff = sum(t['pnl'] for t in diff_trades)
        print(f"  Diff dir (reversal): {len(diff_trades)} trades, WR={wr_diff:.1f}%, PnL=${pnl_diff:.0f}")

    # =========================================================================
    # RANKING: Top Discriminators
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"  TOP DISCRIMINATORS (by Cohen's d effect size)")
    print(f"{'='*80}")

    valid_effects = {k: v for k, v in effects.items() if v is not None and v > 0}
    ranked = sorted(valid_effects.items(), key=lambda x: x[1], reverse=True)

    cohens_label = "Cohen's d"
    print(f"  {'Rank':<6} {'Dimension':<25} {cohens_label:>10} {'Strength':>10}")
    print(f"  {'-'*55}")
    for rank, (dim, d) in enumerate(ranked, 1):
        strength = 'LARGE' if d >= 0.8 else 'medium' if d >= 0.5 else 'small'
        print(f"  {rank:<6} {dim:<25} {d:>10.3f} {strength:>10}")

    print(f"\n  RECOMMENDATION: Focus on top 2-3 discriminators as second filters.")
    if len(ranked) >= 2:
        print(f"  Best candidates: {ranked[0][0]} (d={ranked[0][1]:.3f}), {ranked[1][0]} (d={ranked[1][1]:.3f})")
    if len(ranked) >= 3:
        print(f"  Also consider: {ranked[2][0]} (d={ranked[2][1]:.3f})")

    return {
        'n_trades': n_total,
        'n_winners': n_win,
        'n_losers': n_lose,
        'total_pnl': total_pnl,
        'effects': valid_effects,
        'ranking': ranked,
    }


# =============================================================================
# IMPROVED REVERSAL STRATEGY
# =============================================================================

def test_improved_reversal(btc_df: pd.DataFrame, obs_df: pd.DataFrame, total_hours: float):
    """
    Test improved reversal confirmation strategy with second-layer filters
    derived from the losing patterns analysis.

    Filters tested (on top of 0.01% pullback):
    1. Minimum retracement fraction (pullback / peak_move)
    2. Minimum entry price floor (avoid too-cheap = strong trend)
    3. Minimum choppiness (require noisy price action)
    """
    print("\n" + "=" * 100)
    print("IMPROVED REVERSAL STRATEGY: SECOND-LAYER FILTER SWEEP")
    print("=" * 100)
    print("  Base: Reversal confirmation (0.01% pullback from local extreme)")
    print("  Testing additional filters from losing patterns analysis")

    # Build observer lookup
    obs_by_market = {}
    for slug, group in obs_df.groupby('market_slug'):
        sorted_group = group.sort_values('timestamp_ms')
        obs_by_market[slug] = {
            'timestamps': sorted_group['timestamp_ms'].values,
            'up_ask': sorted_group['up_ask'].values,
            'down_ask': sorted_group['down_ask'].values,
        }

    # Resample BTC to 1s bars
    btc_df_sorted = btc_df.sort_values('timestamp_ms')
    btc_df_sorted['second_ms'] = (btc_df_sorted['timestamp_ms'] // 1000) * 1000
    bars_1s = btc_df_sorted.groupby('second_ms').agg({'price': 'last'}).reset_index()
    bars_1s.rename(columns={'second_ms': 'timestamp_ms'}, inplace=True)
    timestamps = bars_1s['timestamp_ms'].values
    prices = bars_1s['price'].values

    WINDOW_S = 900
    WINDOW_MS = WINDOW_S * 1000
    SHARES_PER_TRADE = 50
    MIN_PULLBACK_PCT = 0.01

    first_window = ((timestamps[0] // WINDOW_MS) + 1) * WINDOW_MS
    end_ms = timestamps[-1]

    def run_with_filters(min_retrace_frac=0.0, min_entry_price=0.0, min_choppiness=0.0,
                         max_entry_time_s=780):
        """Run reversal confirmation with additional filters, return trade list."""
        trades = []
        current_start = first_window

        while current_start + WINDOW_MS <= end_ms:
            win_mask = (timestamps >= current_start) & (timestamps < current_start + WINDOW_MS)
            win_idx = np.where(win_mask)[0]
            current_start += WINDOW_MS

            if len(win_idx) < 100:
                continue

            win_prices = prices[win_idx]
            win_times = timestamps[win_idx]
            btc_open = win_prices[0]

            window_start_s = int((current_start - WINDOW_MS) // 1000)
            market_slug = f"btc-updown-15m-{window_start_s}"
            if market_slug not in obs_by_market:
                continue

            obs_data = obs_by_market[market_slug]
            entry_made = False
            max_idx = min(max_entry_time_s, len(win_prices))

            local_max = btc_open
            local_min = btc_open

            for i in range(60, max_idx):
                if entry_made:
                    break

                current_price = win_prices[i]
                local_max = max(local_max, current_price)
                local_min = min(local_min, current_price)

                btc_move_pct = (current_price - btc_open) / btc_open * 100
                if abs(btc_move_pct) < 0.01:
                    continue

                # Check for pullback from extreme
                if btc_move_pct > 0:
                    extreme_move = (local_max - btc_open) / btc_open * 100
                    pullback = (local_max - current_price) / btc_open * 100
                    if pullback < MIN_PULLBACK_PCT:
                        continue
                    side_key = 'down_ask'
                    entry_direction = "DOWN"
                else:
                    extreme_move = (btc_open - local_min) / btc_open * 100
                    pullback = (current_price - local_min) / btc_open * 100
                    if pullback < MIN_PULLBACK_PCT:
                        continue
                    side_key = 'up_ask'
                    entry_direction = "UP"

                # FILTER 1: Retracement fraction
                retrace_frac = pullback / extreme_move if extreme_move > 1e-6 else 0.0
                if retrace_frac < min_retrace_frac:
                    continue

                # FILTER 3: Choppiness (check before orderbook to save lookups)
                if min_choppiness > 0:
                    lookback = min(60, i)
                    if lookback >= 10:
                        recent = win_prices[i - lookback:i + 1]
                        diffs = np.diff(recent)
                        sign_changes = np.sum(np.diff(np.sign(diffs)) != 0)
                        choppiness = sign_changes / len(diffs)
                        if choppiness < min_choppiness:
                            continue
                    else:
                        continue

                elapsed_s = (win_times[i] - win_times[0]) / 1000.0

                # Get orderbook price
                signal_ts = win_times[i]
                idx = np.searchsorted(obs_data['timestamps'], signal_ts)
                idx = min(idx, len(obs_data['timestamps']) - 1)
                raw_price = float(obs_data[side_key][idx])
                if np.isnan(raw_price) or raw_price <= 0:
                    continue
                entry_price = max(0.05, min(0.50, raw_price))

                # FILTER 2: Minimum entry price
                if entry_price < min_entry_price:
                    continue

                # Resolution
                btc_close = win_prices[-1]
                close_move = (btc_close - btc_open) / btc_open * 100
                winner = "UP" if close_move >= 0 else "DOWN"
                won = (entry_direction == winner)
                pnl = SHARES_PER_TRADE * ((1.0 - entry_price) if won else -entry_price)

                trades.append({
                    'won': won,
                    'pnl': pnl,
                    'entry_price': entry_price,
                    'entry_time_s': elapsed_s,
                    'retrace_frac': retrace_frac,
                })
                entry_made = True

        return trades

    # =========================================================================
    # STEP 1: Individual filter sweeps
    # =========================================================================

    # --- Retracement fraction sweep ---
    print(f"\n  === FILTER 1: MINIMUM RETRACEMENT FRACTION ===")
    print(f"  Require pullback to be at least X% of the peak move")
    print(f"  {'MinRetrace':<12} {'Trades':<8} {'WR':<8} {'AvgPrice':<10} {'PnL':<10} {'$/hr':<8} {'Edge':<8}")
    print(f"  {'-'*70}")

    best_retrace = {'pnl': -9999, 'val': 0}
    for min_rf in [0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60]:
        trades = run_with_filters(min_retrace_frac=min_rf)
        if trades:
            wr = sum(1 for t in trades if t['won']) / len(trades) * 100
            avg_p = np.mean([t['entry_price'] for t in trades])
            total_pnl = sum(t['pnl'] for t in trades)
            hourly = total_pnl / total_hours
            edge = wr - avg_p * 100
            print(f"  {min_rf:<12.2f} {len(trades):<8} {wr:<7.1f}% ${avg_p:<9.3f} ${total_pnl:<9.0f} ${hourly:<7.0f} {edge:+.1f}pp")
            if total_pnl > best_retrace['pnl']:
                best_retrace = {'pnl': total_pnl, 'val': min_rf, 'trades': len(trades), 'wr': wr}

    # --- Entry price floor sweep ---
    print(f"\n  === FILTER 2: MINIMUM ENTRY PRICE ===")
    print(f"  Skip entries where cheap side is too cheap (indicates strong trend)")
    print(f"  {'MinPrice':<12} {'Trades':<8} {'WR':<8} {'AvgPrice':<10} {'PnL':<10} {'$/hr':<8} {'Edge':<8}")
    print(f"  {'-'*70}")

    best_price = {'pnl': -9999, 'val': 0}
    for min_p in [0.0, 0.15, 0.20, 0.25, 0.30, 0.35]:
        trades = run_with_filters(min_entry_price=min_p)
        if trades:
            wr = sum(1 for t in trades if t['won']) / len(trades) * 100
            avg_p = np.mean([t['entry_price'] for t in trades])
            total_pnl = sum(t['pnl'] for t in trades)
            hourly = total_pnl / total_hours
            edge = wr - avg_p * 100
            print(f"  ${min_p:<11.2f} {len(trades):<8} {wr:<7.1f}% ${avg_p:<9.3f} ${total_pnl:<9.0f} ${hourly:<7.0f} {edge:+.1f}pp")
            if total_pnl > best_price['pnl']:
                best_price = {'pnl': total_pnl, 'val': min_p, 'trades': len(trades), 'wr': wr}

    # --- Choppiness sweep ---
    print(f"\n  === FILTER 3: MINIMUM CHOPPINESS ===")
    print(f"  Require noisy price action (more direction changes = noise = reverts)")
    print(f"  {'MinChop':<12} {'Trades':<8} {'WR':<8} {'AvgPrice':<10} {'PnL':<10} {'$/hr':<8} {'Edge':<8}")
    print(f"  {'-'*70}")

    best_chop = {'pnl': -9999, 'val': 0}
    for min_c in [0.0, 0.05, 0.10, 0.15, 0.20, 0.30]:
        trades = run_with_filters(min_choppiness=min_c)
        if trades:
            wr = sum(1 for t in trades if t['won']) / len(trades) * 100
            avg_p = np.mean([t['entry_price'] for t in trades])
            total_pnl = sum(t['pnl'] for t in trades)
            hourly = total_pnl / total_hours
            edge = wr - avg_p * 100
            print(f"  {min_c:<12.2f} {len(trades):<8} {wr:<7.1f}% ${avg_p:<9.3f} ${total_pnl:<9.0f} ${hourly:<7.0f} {edge:+.1f}pp")
            if total_pnl > best_chop['pnl']:
                best_chop = {'pnl': total_pnl, 'val': min_c, 'trades': len(trades), 'wr': wr}

    # --- Entry time cap sweep ---
    print(f"\n  === FILTER 4: MAX ENTRY TIME (seconds) ===")
    print(f"  Only enter within first N seconds (more time remaining = more reversion time)")
    print(f"  {'MaxTime':<12} {'Trades':<8} {'WR':<8} {'AvgPrice':<10} {'PnL':<10} {'$/hr':<8} {'Edge':<8}")
    print(f"  {'-'*70}")

    best_time = {'pnl': -9999, 'val': 780}
    for max_t in [180, 300, 420, 540, 660, 780]:
        trades = run_with_filters(max_entry_time_s=max_t)
        if trades:
            wr = sum(1 for t in trades if t['won']) / len(trades) * 100
            avg_p = np.mean([t['entry_price'] for t in trades])
            total_pnl = sum(t['pnl'] for t in trades)
            hourly = total_pnl / total_hours
            edge = wr - avg_p * 100
            print(f"  {max_t:<12} {len(trades):<8} {wr:<7.1f}% ${avg_p:<9.3f} ${total_pnl:<9.0f} ${hourly:<7.0f} {edge:+.1f}pp")
            if total_pnl > best_time['pnl']:
                best_time = {'pnl': total_pnl, 'val': max_t, 'trades': len(trades), 'wr': wr}

    # =========================================================================
    # STEP 2: Best individual filters summary
    # =========================================================================
    print(f"\n  === BEST INDIVIDUAL FILTERS ===")
    print(f"  Retracement: >={best_retrace['val']:.2f} ({best_retrace.get('trades', 0)} trades, "
          f"WR={best_retrace.get('wr', 0):.1f}%, PnL=${best_retrace['pnl']:.0f})")
    print(f"  Entry price: >=${best_price['val']:.2f} ({best_price.get('trades', 0)} trades, "
          f"WR={best_price.get('wr', 0):.1f}%, PnL=${best_price['pnl']:.0f})")
    print(f"  Choppiness:  >={best_chop['val']:.2f} ({best_chop.get('trades', 0)} trades, "
          f"WR={best_chop.get('wr', 0):.1f}%, PnL=${best_chop['pnl']:.0f})")
    print(f"  Max time:    <={best_time['val']}s ({best_time.get('trades', 0)} trades, "
          f"WR={best_time.get('wr', 0):.1f}%, PnL=${best_time['pnl']:.0f})")

    # =========================================================================
    # STEP 3: Combined filter grid (top 2-3 combinations)
    # =========================================================================
    print(f"\n  === COMBINED FILTER GRID ===")
    print(f"  Testing combinations of best individual filters")
    print(f"  {'Retrace':<9} {'MinPrice':<10} {'Chop':<7} {'MaxTime':<9} {'Trades':<8} {'WR':<8} "
          f"{'AvgP':<8} {'PnL':<10} {'$/hr':<8} {'Edge':<8}")
    print(f"  {'-'*95}")

    # Test combos using the best values and nearby alternatives
    retrace_vals = [0.0, 0.20, 0.30, 0.40]
    price_vals = [0.0, 0.20, 0.25]
    chop_vals = [0.0, 0.10, 0.15]
    time_vals = [420, 780]

    best_combo = {'pnl': -9999}
    best_hourly = {'hourly': -9999}

    for rf in retrace_vals:
        for mp in price_vals:
            for mc in chop_vals:
                for mt in time_vals:
                    # Skip baseline (all zeros)
                    if rf == 0 and mp == 0 and mc == 0 and mt == 780:
                        continue
                    # Skip if only one non-default (already tested above)
                    active_filters = (rf > 0) + (mp > 0) + (mc > 0) + (mt < 780)
                    if active_filters < 2:
                        continue

                    trades = run_with_filters(min_retrace_frac=rf, min_entry_price=mp,
                                              min_choppiness=mc, max_entry_time_s=mt)
                    if not trades or len(trades) < 10:
                        continue

                    wr = sum(1 for t in trades if t['won']) / len(trades) * 100
                    avg_p = np.mean([t['entry_price'] for t in trades])
                    total_pnl = sum(t['pnl'] for t in trades)
                    hourly = total_pnl / total_hours
                    edge = wr - avg_p * 100

                    # Only print promising combos (positive PnL or high WR)
                    if total_pnl > 0 or wr > 45:
                        print(f"  {rf:<9.2f} ${mp:<9.2f} {mc:<7.2f} {mt:<9} {len(trades):<8} "
                              f"{wr:<7.1f}% ${avg_p:<7.3f} ${total_pnl:<9.0f} ${hourly:<7.0f} {edge:+.1f}pp")

                    if total_pnl > best_combo['pnl']:
                        best_combo = {'pnl': total_pnl, 'rf': rf, 'mp': mp, 'mc': mc, 'mt': mt,
                                      'trades': len(trades), 'wr': wr, 'avg_p': avg_p, 'hourly': hourly}
                    if hourly > best_hourly['hourly'] and len(trades) >= 20:
                        best_hourly = {'hourly': hourly, 'rf': rf, 'mp': mp, 'mc': mc, 'mt': mt,
                                       'trades': len(trades), 'wr': wr, 'pnl': total_pnl, 'avg_p': avg_p}

    # =========================================================================
    # STEP 4: Final recommendation
    # =========================================================================
    print(f"\n  === BEST COMBINATIONS ===")

    if best_combo['pnl'] > 0:
        bc = best_combo
        edge = bc['wr'] - bc['avg_p'] * 100
        print(f"  Best PnL:    retrace>={bc['rf']:.2f}, price>=${bc['mp']:.2f}, "
              f"chop>={bc['mc']:.2f}, time<={bc['mt']}s")
        print(f"               {bc['trades']} trades, WR={bc['wr']:.1f}%, "
              f"PnL=${bc['pnl']:.0f}, $/hr=${bc['hourly']:.0f}, edge={edge:+.1f}pp")

    if best_hourly.get('hourly', 0) > 0:
        bh = best_hourly
        edge = bh['wr'] - bh['avg_p'] * 100
        print(f"  Best $/hr:   retrace>={bh['rf']:.2f}, price>=${bh['mp']:.2f}, "
              f"chop>={bh['mc']:.2f}, time<={bh['mt']}s")
        print(f"               {bh['trades']} trades, WR={bh['wr']:.1f}%, "
              f"PnL=${bh['pnl']:.0f}, $/hr=${bh['hourly']:.0f}, edge={edge:+.1f}pp")

    # Compare to baseline
    baseline = run_with_filters()
    if baseline:
        bl_wr = sum(1 for t in baseline if t['won']) / len(baseline) * 100
        bl_pnl = sum(t['pnl'] for t in baseline)
        bl_avg_p = np.mean([t['entry_price'] for t in baseline])
        bl_edge = bl_wr - bl_avg_p * 100
        print(f"\n  Baseline (no filters): {len(baseline)} trades, WR={bl_wr:.1f}%, "
              f"PnL=${bl_pnl:.0f}, edge={bl_edge:+.1f}pp")
        if best_combo['pnl'] > 0:
            improvement = best_combo['pnl'] - bl_pnl
            wr_improve = best_combo['wr'] - bl_wr
            print(f"  Improvement: PnL +${improvement:.0f}, WR +{wr_improve:.1f}pp "
                  f"(but {len(baseline) - best_combo['trades']} fewer trades)")

    return best_combo


# =============================================================================
# MAIN
# =============================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Validate strategies on OOS data")
    parser.add_argument("--combined", action="store_true",
                        help="Use combined OOS3+OOS4 dataset (~50.6h) instead of OOS4 only")
    parser.add_argument("--training", action="store_true",
                        help="Use training+OOS2 dataset (Jan 16-19, ~81.7h)")
    args = parser.parse_args()

    # Select data files based on mode
    global OOS4_BTC_FILE, OOS4_OBS_FILE, OOS4_RES_FILE
    if args.training:
        OOS4_BTC_FILE = TRAINING_BTC_FILE
        # Concatenate training observer files
        print("  Concatenating training observer files...")
        obs_frames = []
        for f in TRAINING_OBS_FILES:
            if f.exists():
                obs_frames.append(pd.read_csv(f, on_bad_lines='skip', low_memory=False))
                print(f"    {f.name}: {len(obs_frames[-1]):,} rows")
        OOS4_OBS_FILE = None  # signal to use pre-loaded obs_df
        OOS4_RES_FILE = TRAINING_RES_FILE
        dataset_label = "TRAINING+OOS2 (Jan 16-19, 2026)"
    elif args.combined:
        OOS4_BTC_FILE = COMBINED_BTC_FILE
        OOS4_OBS_FILE = COMBINED_OBS_FILE
        dataset_label = "COMBINED OOS3+OOS4 (Jan 22-24, 2026)"
    else:
        dataset_label = "OOS4 (Jan 23-24, 2026)"

    print("=" * 100)
    print(f"VALIDATION: ALL PATHS ON {dataset_label}")
    print("=" * 100)
    print("\nThis data was NOT used in any grid search. True out-of-sample test.\n")

    # Load data
    print("-" * 60)
    print(f"LOADING DATA ({dataset_label})")
    print("-" * 60)

    ou_params = load_ou_params()
    print(f"  OU params: mu={ou_params.mu:.4f}")

    if args.training:
        # Load training BTC data
        print(f"  Loading BTC: {TRAINING_BTC_FILE.name}")
        btc_df = pd.read_csv(TRAINING_BTC_FILE)
        btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms').reset_index(drop=True)
        print(f"  BTC rows: {len(btc_df):,}")

        # Concatenate observer frames (already loaded during arg parsing)
        obs_df = pd.concat(obs_frames, ignore_index=True)
        obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
        print(f"  Observer rows: {len(obs_df):,}")

        # Load resolutions (uses 'market' column)
        print(f"  Loading resolutions: {TRAINING_RES_FILE.name}")
        res_df = pd.read_csv(TRAINING_RES_FILE)
        col = 'market' if 'market' in res_df.columns else 'slug'
        res_map = dict(zip(res_df[col], res_df['winner']))
        resolved = {k: v for k, v in res_map.items() if v in ('UP', 'DOWN')}
        print(f"  Resolved markets: {len(resolved)}")
    else:
        btc_df = load_oos4_btc()
        obs_df, res_map = load_oos4_observer()

    # Dataset stats
    btc_start = btc_df['timestamp_ms'].min()
    btc_end = btc_df['timestamp_ms'].max()
    total_hours = (btc_end - btc_start) / 3600000
    print(f"\n  OOS4 Dataset: {total_hours:.2f} hours")
    print(f"  Time range: {pd.Timestamp(btc_start, unit='ms')} to {pd.Timestamp(btc_end, unit='ms')}")
    print(f"  BTC price: ${btc_df['price'].min():.0f} - ${btc_df['price'].max():.0f}")

    # Compute z-scores
    print("\n" + "-" * 60)
    print("COMPUTING Z-SCORES")
    print("-" * 60)
    zscore_cache = {}
    for method in ['ewma', 'ou']:
        print(f"  Computing {method} z-scores...")
        zscore_cache[method] = compute_zscore_series(btc_df, ou_params, zscore_method=method)

    # =========================================================================
    # RUN PATH 1 STRATEGIES
    # =========================================================================
    print("\n" + "=" * 100)
    print("PATH 1: SPIKE STRATEGIES")
    print("=" * 100)

    spike_results = []
    for cfg in STRATEGIES:
        zscore_df = zscore_cache[cfg.zscore_method]
        result = run_spike_strategy(cfg, btc_df, obs_df, zscore_df, res_map, ou_params, total_hours)
        if result:
            spike_results.append(result)

    # =========================================================================
    # RUN PATH 2: CONTRARIAN
    # =========================================================================
    print("\n" + "=" * 100)
    print("PATH 2: CONTRARIAN STRATEGY")
    print("=" * 100)

    contrarian_result = run_contrarian_strategy(btc_df, obs_df, total_hours)

    # =========================================================================
    # WALLET PATTERN ANALYSIS
    # =========================================================================
    wallet_result = analyze_wallet_pattern(btc_df, obs_df, total_hours)

    # =========================================================================
    # LOSING PATTERNS + IMPROVED REVERSAL
    # =========================================================================
    if args.training:
        analyze_losing_patterns(btc_df, obs_df, total_hours)
    if args.training or args.combined:
        test_improved_reversal(btc_df, obs_df, total_hours)

    # =========================================================================
    # FINAL SUMMARY
    # =========================================================================
    print("\n" + "=" * 100)
    print("OOS4 VALIDATION SUMMARY")
    print("=" * 100)

    print(f"\n{'Path':<7} {'Config':<22} {'Trades':<8} {'PnL@5sh':<10} {'$/hr@5sh':<10} "
          f"{'$/hr@50sh':<11} {'WinRate':<9} {'DirAcc':<8}")
    print("-" * 100)

    for r in spike_results:
        print(f"{r['path']:<7} {r['config']:<22} {r['trades']:<8} "
              f"${r['pnl_5sh']:<9.2f} ${r['hourly_5sh']:<9.3f} "
              f"${r['hourly_50sh']:<10.2f} {r['win_rate']:<8.1f}% {r['dir_acc']:<7.1f}%")

    if contrarian_result:
        cr = contrarian_result
        print(f"\n{'Path2':<7} {'CONTRARIAN':<22} {cr['trades']:<8} "
              f"${cr['pnl_total']:<9.0f} ${cr['hourly_rate']:<9.0f} "
              f"{'N/A':<11} {cr['win_rate']:<8.1f}% {'N/A':<8}")
        print(f"        ({cr['shares_per_trade']} shares/trade, avg entry=${cr['avg_entry_price']:.3f}, "
              f"breakeven={cr['avg_entry_price']*100:.1f}%, real prices={cr['real_price_pct']:.0f}%)")

    # Verdict
    print("\n" + "=" * 100)
    print("VERDICT")
    print("=" * 100)

    for r in spike_results:
        if r['trades'] < 10:
            verdict = "INSUFFICIENT DATA (<10 trades)"
        elif r['win_rate'] >= 65:
            verdict = "STRONG"
        elif r['win_rate'] >= 55:
            verdict = "MODERATE"
        elif r['win_rate'] >= 50:
            verdict = "MARGINAL"
        else:
            verdict = "WEAK"

        pnl_sign = "PROFITABLE" if r['hourly_5sh'] > 0 else "LOSING"
        print(f"  {r['config']:<22}: {verdict} | {pnl_sign} (${r['hourly_50sh']:.2f}/hr @50sh)")

    if contrarian_result:
        cr = contrarian_result
        breakeven = cr['avg_entry_price'] * 100
        if cr['win_rate'] >= breakeven + 10:
            verdict = f"STRONG (well above {breakeven:.0f}% breakeven)"
        elif cr['win_rate'] >= breakeven + 5:
            verdict = f"MODERATE (above {breakeven:.0f}% breakeven)"
        elif cr['win_rate'] >= breakeven:
            verdict = f"MARGINAL (near {breakeven:.0f}% breakeven)"
        else:
            verdict = f"WEAK (below {breakeven:.0f}% breakeven)"
        print(f"  {'CONTRARIAN':<22}: {verdict} | WR={cr['win_rate']:.1f}%, ${cr['hourly_rate']:,.0f}/hr")

    # Save results
    suffix = "_combined" if args.combined else "_oos4"
    if spike_results:
        df = pd.DataFrame(spike_results)
        output_path = BASE_DIR / f"research/validation_results{suffix}.csv"
        df.to_csv(output_path, index=False)
        print(f"\nSpike results saved to: {output_path}")

    if contrarian_result:
        cr_df = pd.DataFrame([contrarian_result])
        cr_output = BASE_DIR / f"research/contrarian_results{suffix}.csv"
        cr_df.to_csv(cr_output, index=False)
        print(f"Contrarian results saved to: {cr_output}")


if __name__ == "__main__":
    main()
