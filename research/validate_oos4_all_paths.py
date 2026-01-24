#!/usr/bin/env python3
"""
OOS4 Validation: ALL Paths on Fresh Data (Jan 23-24, 2026)

Tests:
- Path 1: AGGRESSIVE (ou/ewma, 1200ms, time-stop 180s, 0<z<1.5, cycling ON)
- Path 1: BALANCED+EWMA (ewma/ewma, 1400ms, price-stop 15%, -0.5<z<1.5, cycling ON)
- Path 2: CONTRARIAN (buy cheap side against BTC, $0.30 entry, adaptive gate)

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

from research.volatility_filter_analysis import (
    load_ou_params, compute_zscore_series, BacktestConfig, run_backtest_with_zscore,
    TradeWithZScore, estimate_active_hours_zone
)

# =============================================================================
# CONFIGURATION
# =============================================================================

BASE_DIR = Path("/Users/rananjaybika/polymarket-amm-bot")

# Default: OOS4 only. Use --combined for OOS3+OOS4
OOS4_BTC_FILE = BASE_DIR / "research/binance_hf/btc_prices_oos4_combined.csv"
OOS4_OBS_FILE = BASE_DIR / "research/observer/grid_obs_oos4_combined.csv"
COMBINED_BTC_FILE = BASE_DIR / "research/observer/btc_prices_oos3_oos4_combined.csv"
COMBINED_OBS_FILE = BASE_DIR / "research/observer/grid_obs_oos3_oos4_combined.csv"
OOS4_RES_FILE = BASE_DIR / "research/observer/market_resolutions_verified.csv"

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


def run_contrarian_strategy(btc_df: pd.DataFrame, total_hours: float):
    """
    Run Path 2 contrarian strategy on OOS4 BTC data.

    Best config from research: rolling_300s vol, Z=0.5, delay=60s, entry=$0.30,
    adaptive gate k=0.5, halflife=50.
    """
    print(f"\n  {'='*70}")
    print(f"  Path2: CONTRARIAN (mean-reversion)")
    print(f"  Buy cheap side ($0.30) against BTC direction | delay=60s | Z>=0.5")
    print(f"  Adaptive gate: k=0.5, halflife=50 windows")
    print(f"  {'='*70}")

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
    SHARES_PER_TRADE = 2500
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

                # Resolution: BTC at end of window
                btc_close = win_prices[-1]
                close_move = (btc_close - btc_open) / btc_open * 100
                winner = "UP" if close_move >= 0 else "DOWN"

                won = (entry_direction == winner)
                pnl = SHARES_PER_TRADE * ((1.0 - ENTRY_PRICE) if won else -ENTRY_PRICE)

                trades.append({
                    'entry_time_s': elapsed_s,
                    'btc_move_pct': btc_move_pct,
                    'z_score': z_score,
                    'entry_direction': entry_direction,
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

    print(f"  Windows: {windows_total} total, {windows_gated} gated out ({windows_gated/windows_total*100:.0f}%)")
    print(f"  Trades: {len(trades)} ({len(trades)/windows_total*100:.0f}% of windows)")
    print(f"  Win Rate: {win_rate:.1f}% (breakeven = 30%)")
    print(f"  PnL: ${total_pnl:,.0f} ({SHARES_PER_TRADE} shares/trade)")
    print(f"  $/hr: ${hourly_rate:,.0f}")
    print(f"  Avg Z-score: {avg_z:.2f} | Avg entry time: {avg_entry_time:.0f}s")

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
        'shares_per_trade': SHARES_PER_TRADE,
        'entry_price': ENTRY_PRICE,
    }


# =============================================================================
# MAIN
# =============================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Validate strategies on OOS data")
    parser.add_argument("--combined", action="store_true",
                        help="Use combined OOS3+OOS4 dataset (~50.6h) instead of OOS4 only")
    args = parser.parse_args()

    # Select data files based on mode
    global OOS4_BTC_FILE, OOS4_OBS_FILE
    if args.combined:
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

    contrarian_result = run_contrarian_strategy(btc_df, total_hours)

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
        print(f"        (2500 shares/trade, entry=$0.30, breakeven=30%)")

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
        if cr['win_rate'] >= 40:
            verdict = "STRONG (well above 30% breakeven)"
        elif cr['win_rate'] >= 35:
            verdict = "MODERATE (above breakeven)"
        elif cr['win_rate'] >= 30:
            verdict = "MARGINAL (near breakeven)"
        else:
            verdict = "WEAK (below breakeven)"
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
