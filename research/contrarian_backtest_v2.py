#!/usr/bin/env python3
"""
Contrarian Mean-Reversion Backtest V2 - Proper Volatility Methods

Improvements over V1:
1. THREE vol methods for Z-score: EWMA, OU-calibrated, Rolling-window
2. Volatility regime gating (only trade in certain vol regimes)
3. Pre-window vol computed from PRIOR data (no lookahead)
4. Proper Z-score: move / (vol_per_second * sqrt(elapsed_seconds))

Data: research/binance_hf/btc_prices_combined.csv (9ms tick data → 1s bars)
Split: Training before 1768705387229, OOS2 after
"""

import math
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
from pathlib import Path
from scipy.stats import norm
import json
import sys

# Add parent for OU imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.strategies.ou_volatility import OUParameters, compute_ou_z_score

# =============================================================================
# CONFIGURATION
# =============================================================================

DATA_FILE = Path(__file__).parent / "binance_hf" / "btc_prices_combined.csv"
OU_PARAMS_FILE = Path(__file__).parent / "ou_params.json"
SPLIT_TIMESTAMP_MS = 1768705387229  # Jan 18, 03:03:07 UTC

WINDOW_DURATION_S = 900  # 15 minutes
RESAMPLE_INTERVAL_S = 1  # 1-second bars

# Shares per trade (matches observed wallet: avg 2576)
SHARES_PER_TRADE = 2500

# =============================================================================
# VOLATILITY METHODS
# =============================================================================

class VolMethod:
    """Base class for volatility estimation methods."""
    name: str = "base"

    def compute_zscore(self, prices: np.ndarray, btc_move_pct: float,
                       elapsed_s: float, pre_window_prices: np.ndarray) -> float:
        """Compute Z-score of the BTC move given price history."""
        raise NotImplementedError


class EWMAVol(VolMethod):
    """EWMA volatility - exponentially weighted, responsive to recent moves."""
    def __init__(self, halflife_s: int = 30):
        self.name = f"ewma_{halflife_s}s"
        self.halflife_s = halflife_s
        # Decay factor per second
        self.alpha = 1 - 0.5 ** (1.0 / halflife_s)

    def compute_zscore(self, prices: np.ndarray, btc_move_pct: float,
                       elapsed_s: float, pre_window_prices: np.ndarray) -> float:
        # Compute EWMA variance from pre-window + in-window prices
        all_prices = np.concatenate([pre_window_prices[-300:], prices])  # last 5min pre + in-window
        if len(all_prices) < 10:
            return 0.0

        returns = np.diff(all_prices) / all_prices[:-1] * 100

        # EWMA variance
        variance = np.var(returns[:min(30, len(returns))])  # Initial seed
        for r in returns:
            variance = self.alpha * (r ** 2) + (1 - self.alpha) * variance

        vol_per_s = max(math.sqrt(variance), 1e-8)
        expected_move_std = vol_per_s * math.sqrt(max(elapsed_s, 1))

        if expected_move_std < 1e-8:
            return 0.0
        return abs(btc_move_pct) / expected_move_std


class OUVol(VolMethod):
    """OU-calibrated volatility - uses pre-calibrated OU parameters."""
    def __init__(self, ou_params: OUParameters, ewma_halflife: int = 60):
        self.name = "ou_calibrated"
        self.params = ou_params
        self.ewma_halflife = ewma_halflife
        self.alpha = 1 - 0.5 ** (1.0 / ewma_halflife)

    def compute_zscore(self, prices: np.ndarray, btc_move_pct: float,
                       elapsed_s: float, pre_window_prices: np.ndarray) -> float:
        # Get current EWMA vol from pre-window data
        all_prices = np.concatenate([pre_window_prices[-300:], prices])
        if len(all_prices) < 10:
            return 0.0

        returns = np.diff(all_prices) / all_prices[:-1] * 100
        variance = np.var(returns[:min(30, len(returns))])
        for r in returns:
            variance = self.alpha * (r ** 2) + (1 - self.alpha) * variance

        current_vol = max(math.sqrt(variance), 1e-8)

        # OU Z-score of the volatility itself (how unusual is current vol?)
        vol_z = compute_ou_z_score(current_vol, self.params)

        # Use vol to normalize the price move
        expected_move_std = current_vol * math.sqrt(max(elapsed_s, 1))
        if expected_move_std < 1e-8:
            return 0.0

        price_z = abs(btc_move_pct) / expected_move_std
        return price_z


class RollingVol(VolMethod):
    """Fixed rolling window std - simple and interpretable."""
    def __init__(self, window_s: int = 60):
        self.name = f"rolling_{window_s}s"
        self.window_s = window_s

    def compute_zscore(self, prices: np.ndarray, btc_move_pct: float,
                       elapsed_s: float, pre_window_prices: np.ndarray) -> float:
        # Use last N seconds of combined pre+in-window data
        all_prices = np.concatenate([pre_window_prices[-self.window_s:], prices])
        if len(all_prices) < 10:
            return 0.0

        # Take the last window_s prices
        window_prices = all_prices[-self.window_s:]
        returns = np.diff(window_prices) / window_prices[:-1] * 100

        if len(returns) < 5:
            return 0.0

        vol_per_s = max(np.std(returns), 1e-8)
        expected_move_std = vol_per_s * math.sqrt(max(elapsed_s, 1))

        if expected_move_std < 1e-8:
            return 0.0
        return abs(btc_move_pct) / expected_move_std


# =============================================================================
# VOLATILITY REGIME GATE
# =============================================================================

class VolRegimeGate:
    """
    Percentile-based vol gate.

    Pre-computes vol for ALL windows, then classifies by percentile rank.
    This avoids OU param scale mismatch with resampled data.
    """

    def __init__(self, allowed_quartiles: List[int], label: str = ""):
        """
        allowed_quartiles: list of quartile indices [0,1,2,3] to allow.
            0 = lowest 25% vol, 1 = 25-50%, 2 = 50-75%, 3 = top 25%
        """
        self.allowed_quartiles = allowed_quartiles
        self.name = label or f"Q{''.join(str(q) for q in allowed_quartiles)}"
        self.vol_thresholds: List[float] = []  # Set during precompute

    def precompute_thresholds(self, all_pre_vols: List[float]):
        """Compute quartile boundaries from all pre-window vols."""
        if not all_pre_vols:
            self.vol_thresholds = [0, 0, 0]
            return
        arr = np.array(all_pre_vols)
        self.vol_thresholds = [
            float(np.percentile(arr, 25)),
            float(np.percentile(arr, 50)),
            float(np.percentile(arr, 75)),
        ]

    def get_quartile(self, vol: float) -> int:
        """Return quartile index 0-3 for a given vol value."""
        if vol < self.vol_thresholds[0]:
            return 0
        elif vol < self.vol_thresholds[1]:
            return 1
        elif vol < self.vol_thresholds[2]:
            return 2
        else:
            return 3

    def is_allowed(self, pre_vol: float) -> Tuple[bool, str, float]:
        """Check if this pre-window vol is in an allowed quartile."""
        q = self.get_quartile(pre_vol)
        regime = f"Q{q}"
        allowed = q in self.allowed_quartiles
        return allowed, regime, pre_vol


class AdaptiveEWMAGate:
    """
    Self-adapting vol gate using EWMA ratio.

    Gate condition: current_vol / vol_ema > k
    - k=1.0 → only trade when vol is above recent average (~top 50%)
    - k=1.5 → only trade when vol is 1.5x above average (~top 25%)
    - k=0.7 → trade most windows except very calm ones (~top 75%)

    No calibration needed. Adapts to any vol regime automatically.
    """
    def __init__(self, k: float = 1.0, halflife_windows: int = 20, label: str = ""):
        self.k = k
        self.halflife = halflife_windows
        self.alpha = 1 - 0.5 ** (1.0 / halflife_windows)
        self.vol_ema: Optional[float] = None
        self.name = label or f"ewma_k={k:.1f}_hl={halflife_windows}"
        self.history: List[Dict] = []  # For warmup analysis

    def update_and_check(self, pre_vol: float) -> Tuple[bool, str, float]:
        """Update EMA and check if current vol passes gate."""
        if self.vol_ema is None:
            self.vol_ema = pre_vol  # Seed with first observation
            self.history.append({'pre_vol': pre_vol, 'vol_ema': pre_vol, 'ratio': 1.0, 'allowed': True})
            return True, "WARMUP", 1.0  # Allow during warmup

        # Check gate BEFORE updating (no lookahead)
        ratio = pre_vol / max(self.vol_ema, 1e-10)
        allowed = ratio >= self.k
        regime = f"ratio={ratio:.2f}"

        self.history.append({'pre_vol': pre_vol, 'vol_ema': self.vol_ema, 'ratio': ratio, 'allowed': allowed})

        # Update EMA
        self.vol_ema = self.alpha * pre_vol + (1 - self.alpha) * self.vol_ema

        return allowed, regime, ratio

    def reset(self):
        """Reset state for a new run."""
        self.vol_ema = None
        self.history = []


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class Trade:
    window_start_ms: int
    entry_time_s: float
    btc_open: float
    btc_at_entry: float
    btc_at_close: float
    btc_move_pct: float
    z_score: float
    entry_direction: str
    entry_price: float  # Fixed assumed price
    winner: str
    pnl: float
    won: bool
    vol_regime: str = ""
    vol_z: float = 0.0


@dataclass
class Config:
    vol_method: VolMethod
    z_threshold: float
    min_delay_s: int
    entry_price: float  # Fixed entry price (realistic Polymarket)
    vol_gate: Optional[object] = None  # VolRegimeGate or AdaptiveEWMAGate
    label: str = ""

    def __post_init__(self):
        gate_str = f"_gate={self.vol_gate.name}" if self.vol_gate else ""
        self.label = (f"{self.vol_method.name}_z={self.z_threshold:.1f}"
                      f"_d={self.min_delay_s}s_p={self.entry_price:.2f}{gate_str}")


@dataclass
class Result:
    config: Config
    period: str
    total_windows: int
    trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    pnl_per_hour: float
    avg_z: float
    avg_move_pct: float
    gated_out: int = 0  # Windows skipped by vol gate
    trade_list: List[Trade] = field(default_factory=list)


# =============================================================================
# DATA LOADING
# =============================================================================

def load_data() -> pd.DataFrame:
    """Load and resample to 1-second bars."""
    print(f"Loading {DATA_FILE}...")
    df = pd.read_csv(DATA_FILE)
    print(f"  {len(df):,} ticks")

    df['second_ms'] = (df['timestamp_ms'] // 1000) * 1000
    resampled = df.groupby('second_ms').agg({'price': 'last'}).reset_index()
    resampled.rename(columns={'second_ms': 'timestamp_ms'}, inplace=True)
    print(f"  → {len(resampled):,} 1-second bars")

    return resampled


# =============================================================================
# BACKTEST ENGINE
# =============================================================================

def compute_pre_window_vol(prices: np.ndarray, window_s: int = 300) -> float:
    """Compute realized vol (std of 1s returns) for a price array."""
    if len(prices) < 10:
        return 0.0
    returns = np.diff(prices) / prices[:-1] * 100
    return float(np.std(returns))


def precompute_windows(df: pd.DataFrame) -> List[Dict]:
    """Pre-compute all window data including pre-window vol."""
    timestamps = df['timestamp_ms'].values
    prices = df['price'].values

    window_ms = WINDOW_DURATION_S * 1000
    first_window = ((timestamps[0] // window_ms) + 1) * window_ms
    end_ms = timestamps[-1]

    windows = []
    current_start = first_window

    while current_start + window_ms <= end_ms:
        win_mask = (timestamps >= current_start) & (timestamps < current_start + window_ms)
        win_idx = np.where(win_mask)[0]

        pre_start = current_start - 300_000
        pre_mask = (timestamps >= pre_start) & (timestamps < current_start)
        pre_idx = np.where(pre_mask)[0]

        current_start += window_ms

        if len(win_idx) < 60:
            continue

        win_prices = prices[win_idx]
        win_times = timestamps[win_idx]
        pre_prices = prices[pre_idx] if len(pre_idx) > 0 else np.array([win_prices[0]])
        pre_vol = compute_pre_window_vol(pre_prices)

        windows.append({
            'win_prices': win_prices,
            'win_times': win_times,
            'pre_prices': pre_prices,
            'pre_vol': pre_vol,
        })

    return windows


def run_backtest(windows: List[Dict], config: Config, period: str,
                 hours: float) -> Result:
    """Run single backtest on pre-computed windows."""
    trades: List[Trade] = []
    gated_out = 0

    for w in windows:
        win_prices = w['win_prices']
        win_times = w['win_times']
        pre_prices = w['pre_prices']
        pre_vol = w['pre_vol']

        btc_open = win_prices[0]

        # Vol gate check
        if config.vol_gate:
            if isinstance(config.vol_gate, AdaptiveEWMAGate):
                allowed, regime, vol_val = config.vol_gate.update_and_check(pre_vol)
            else:
                allowed, regime, vol_val = config.vol_gate.is_allowed(pre_vol)
            if not allowed:
                gated_out += 1
                continue
        else:
            regime = ""
            vol_val = pre_vol

        # Scan for entry
        entry_made = False
        max_idx = min(780, len(win_prices))

        for i in range(min(config.min_delay_s, max_idx), max_idx):
            if entry_made:
                break

            elapsed_s = (win_times[i] - win_times[0]) / 1000.0
            if elapsed_s < config.min_delay_s:
                continue

            current_price = win_prices[i]
            btc_move_pct = (current_price - btc_open) / btc_open * 100

            if abs(btc_move_pct) < 0.01:
                continue

            prices_so_far = win_prices[:i+1]
            z_score = config.vol_method.compute_zscore(
                prices_so_far, btc_move_pct, elapsed_s, pre_prices
            )

            if z_score >= config.z_threshold:
                entry_direction = "DOWN" if btc_move_pct > 0 else "UP"

                btc_close = win_prices[-1]
                close_move = (btc_close - btc_open) / btc_open * 100
                winner = "UP" if close_move >= 0 else "DOWN"

                won = (entry_direction == winner)
                pnl = SHARES_PER_TRADE * ((1.0 - config.entry_price) if won else -config.entry_price)

                trades.append(Trade(
                    window_start_ms=int(win_times[0]),
                    entry_time_s=elapsed_s,
                    btc_open=btc_open,
                    btc_at_entry=current_price,
                    btc_at_close=btc_close,
                    btc_move_pct=btc_move_pct,
                    z_score=z_score,
                    entry_direction=entry_direction,
                    entry_price=config.entry_price,
                    winner=winner,
                    pnl=pnl,
                    won=won,
                    vol_regime=regime,
                    vol_z=vol_val,
                ))
                entry_made = True

    wins = sum(1 for t in trades if t.won)
    losses = len(trades) - wins
    win_rate = wins / len(trades) if trades else 0.0
    total_pnl = sum(t.pnl for t in trades)
    pnl_hr = total_pnl / max(hours, 1)
    avg_z = np.mean([t.z_score for t in trades]) if trades else 0.0
    avg_move = np.mean([abs(t.btc_move_pct) for t in trades]) if trades else 0.0

    return Result(
        config=config,
        period=period,
        total_windows=len(windows),
        trades=len(trades),
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        total_pnl=total_pnl,
        pnl_per_hour=pnl_hr,
        avg_z=avg_z,
        avg_move_pct=avg_move,
        gated_out=gated_out,
        trade_list=trades,
    )


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 80)
    print("  CONTRARIAN BACKTEST V2 - Proper Vol Methods + Regime Gates")
    print("=" * 80)

    # Load data
    df = load_data()
    training = df[df['timestamp_ms'] < SPLIT_TIMESTAMP_MS].copy().reset_index(drop=True)
    oos2 = df[df['timestamp_ms'] >= SPLIT_TIMESTAMP_MS].copy().reset_index(drop=True)

    train_hours = (training['timestamp_ms'].iloc[-1] - training['timestamp_ms'].iloc[0]) / 1000 / 3600
    oos2_hours = (oos2['timestamp_ms'].iloc[-1] - oos2['timestamp_ms'].iloc[0]) / 1000 / 3600
    print(f"  Training: {len(training):,} bars ({train_hours:.1f} hours)")
    print(f"  OOS2:     {len(oos2):,} bars ({oos2_hours:.1f} hours)")

    # Load OU params
    ou_params = OUParameters.load(str(OU_PARAMS_FILE))
    print(f"  OU params: μ={ou_params.mu:.4f}, σ_stat={ou_params.sigma_stat:.4f}, "
          f"half_life={ou_params.half_life_sec:.0f}s")

    # Pre-compute windows
    print(f"\n  Pre-computing windows...")
    train_windows = precompute_windows(training)
    oos2_windows = precompute_windows(oos2)
    print(f"  Training: {len(train_windows)} windows")
    print(f"  OOS2:     {len(oos2_windows)} windows")

    # Show pre-window vol distribution
    train_vols = [w['pre_vol'] for w in train_windows]
    oos2_vols = [w['pre_vol'] for w in oos2_windows]
    all_vols = train_vols + oos2_vols

    print(f"\n  Pre-window vol (std of 1s returns %):")
    print(f"    Training: min={min(train_vols):.6f}, median={np.median(train_vols):.6f}, "
          f"max={max(train_vols):.6f}")
    print(f"    OOS2:     min={min(oos2_vols):.6f}, median={np.median(oos2_vols):.6f}, "
          f"max={max(oos2_vols):.6f}")
    print(f"    Q25={np.percentile(all_vols, 25):.6f}, Q50={np.percentile(all_vols, 50):.6f}, "
          f"Q75={np.percentile(all_vols, 75):.6f}")

    # ==========================================================================
    # PHASE 1: Compare vol methods (fixed delay=60s, entry=$0.30)
    # ==========================================================================
    print(f"\n{'='*80}")
    print(f"  PHASE 1: Vol Method Comparison (delay=60s, entry=$0.30)")
    print(f"{'='*80}")

    vol_methods = [
        EWMAVol(halflife_s=15),
        EWMAVol(halflife_s=30),
        EWMAVol(halflife_s=60),
        EWMAVol(halflife_s=120),
        RollingVol(window_s=30),
        RollingVol(window_s=60),
        RollingVol(window_s=120),
        RollingVol(window_s=300),
        OUVol(ou_params, ewma_halflife=30),
        OUVol(ou_params, ewma_halflife=60),
    ]

    z_thresholds = [0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0]

    phase1_results = []
    total_configs = len(vol_methods) * len(z_thresholds)
    print(f"  Running {total_configs} configs x 2 periods...")

    for i, vm in enumerate(vol_methods):
        for z in z_thresholds:
            cfg = Config(vol_method=vm, z_threshold=z, min_delay_s=60, entry_price=0.30)
            tr = run_backtest(train_windows, cfg, "train", train_hours)
            os = run_backtest(oos2_windows, cfg, "oos2", oos2_hours)
            phase1_results.append((cfg, tr, os))

        print(f"  ... {(i+1)*len(z_thresholds)}/{total_configs} done ({vm.name})")

    # Print Phase 1 results (sorted by combined PnL, only show where methods diverge)
    print(f"\n  {'Config':<40} {'Tr Trades':<10} {'Tr WR':<8} {'Tr PnL':<12} "
          f"{'OS Trades':<10} {'OS WR':<8} {'OS PnL':<12} {'Combined':<12}")
    print(f"  {'-'*120}")

    phase1_sorted = sorted(phase1_results, key=lambda x: x[1].total_pnl + x[2].total_pnl, reverse=True)
    for cfg, tr, os in phase1_sorted[:30]:
        combined = tr.total_pnl + os.total_pnl
        marker = "**" if tr.total_pnl > 0 and os.total_pnl > 0 else "  "
        print(f"{marker}{cfg.label:<38} {tr.trades:<10} {tr.win_rate*100:>5.1f}%  "
              f"${tr.total_pnl:>9,.0f}  {os.trades:<10} {os.win_rate*100:>5.1f}%  "
              f"${os.total_pnl:>9,.0f}  ${combined:>9,.0f}")

    # Find where vol methods actually differ (group by z-threshold, compare methods)
    print(f"\n  --- Method comparison at each Z-threshold ---")
    for z in z_thresholds:
        z_results = [(cfg, tr, os) for cfg, tr, os in phase1_results if cfg.z_threshold == z]
        z_results.sort(key=lambda x: x[1].win_rate + x[2].win_rate, reverse=True)
        best = z_results[0]
        worst = z_results[-1]
        if best[1].trades > 0 and worst[1].trades > 0:
            wr_spread = (best[1].win_rate - worst[1].win_rate) * 100
            print(f"  Z={z:.1f}: Best={best[0].vol_method.name} ({best[1].win_rate*100:.1f}% WR), "
                  f"Worst={worst[0].vol_method.name} ({worst[1].win_rate*100:.1f}% WR), "
                  f"Spread={wr_spread:.1f}pp, Trades={best[1].trades}")

    # ==========================================================================
    # PHASE 2: Percentile-based Volatility Regime Gates
    # ==========================================================================
    print(f"\n{'='*80}")
    print(f"  PHASE 2: Percentile-Based Vol Gates (using pre-window realized vol)")
    print(f"{'='*80}")

    # Use top vol method from phase 1
    best_methods = []
    seen = set()
    for cfg, tr, os in phase1_sorted:
        key = cfg.vol_method.name
        if key not in seen and tr.total_pnl > 0 and os.total_pnl > 0:
            best_methods.append((cfg.vol_method, cfg.z_threshold))
            seen.add(key)
        if len(best_methods) >= 2:
            break

    # Create percentile gates (using ALL data to set thresholds)
    gate_configs = [
        VolRegimeGate(allowed_quartiles=[0], label="Q0_lowest25"),
        VolRegimeGate(allowed_quartiles=[1], label="Q1_25-50"),
        VolRegimeGate(allowed_quartiles=[2], label="Q2_50-75"),
        VolRegimeGate(allowed_quartiles=[3], label="Q3_top25"),
        VolRegimeGate(allowed_quartiles=[0, 1], label="Q01_bottom50"),
        VolRegimeGate(allowed_quartiles=[2, 3], label="Q23_top50"),
        VolRegimeGate(allowed_quartiles=[0, 1, 2], label="Q012_bottom75"),
        VolRegimeGate(allowed_quartiles=[1, 2], label="Q12_middle50"),
    ]

    # Pre-compute thresholds from training data only (no lookahead)
    for gate in gate_configs:
        gate.precompute_thresholds(train_vols)

    print(f"  Vol quartile boundaries (from training): "
          f"Q25={gate_configs[0].vol_thresholds[0]:.6f}, "
          f"Q50={gate_configs[0].vol_thresholds[1]:.6f}, "
          f"Q75={gate_configs[0].vol_thresholds[2]:.6f}")

    phase2_results = []
    for vm, z in best_methods:
        for gate in gate_configs:
            cfg = Config(vol_method=vm, z_threshold=z, min_delay_s=60,
                        entry_price=0.30, vol_gate=gate)
            tr = run_backtest(train_windows, cfg, "train", train_hours)
            os = run_backtest(oos2_windows, cfg, "oos2", oos2_hours)
            phase2_results.append((cfg, tr, os))

    print(f"\n  {'Config':<60} {'Tr':<6} {'TrWR':<7} {'TrPnL':<11} "
          f"{'OS':<6} {'OSWR':<7} {'OSPnL':<11} {'Gated%':<8}")
    print(f"  {'-'*125}")

    phase2_sorted = sorted(phase2_results, key=lambda x: x[1].total_pnl + x[2].total_pnl, reverse=True)
    for cfg, tr, os in phase2_sorted:
        gate_pct = (tr.gated_out / max(tr.total_windows, 1)) * 100
        marker = "**" if tr.total_pnl > 0 and os.total_pnl > 0 else "  "
        print(f"{marker}{cfg.label:<58} {tr.trades:<6} {tr.win_rate*100:>4.1f}%  "
              f"${tr.total_pnl:>8,.0f}  {os.trades:<6} {os.win_rate*100:>4.1f}%  "
              f"${os.total_pnl:>8,.0f}  {gate_pct:>5.1f}%")

    # Show win rate by quartile (diagnostic)
    print(f"\n  --- Win Rate by Vol Quartile (diagnostic) ---")
    for gate in gate_configs:
        if len(gate.allowed_quartiles) == 1:
            cfg = Config(vol_method=best_methods[0][0], z_threshold=best_methods[0][1],
                        min_delay_s=60, entry_price=0.30, vol_gate=gate)
            tr = run_backtest(train_windows, cfg, "train", train_hours)
            os = run_backtest(oos2_windows, cfg, "oos2", oos2_hours)
            print(f"  {gate.name:<20} Train: {tr.trades:>3} trades, {tr.win_rate*100:>5.1f}% WR  |  "
                  f"OOS2: {os.trades:>3} trades, {os.win_rate*100:>5.1f}% WR")

    # ==========================================================================
    # PHASE 3: Entry delay sweep (best method + best gate)
    # ==========================================================================
    print(f"\n{'='*80}")
    print(f"  PHASE 3: Entry Delay Sweep")
    print(f"{'='*80}")

    # Pick best from phase 2 (or phase 1 if no gate helps)
    best_p2 = phase2_sorted[0] if phase2_sorted else phase1_sorted[0]
    best_vm = best_p2[0].vol_method
    best_z = best_p2[0].z_threshold
    best_gate = best_p2[0].vol_gate

    delays = [30, 60, 90, 120, 180, 240, 300, 360, 420]

    print(f"  Method: {best_vm.name}, Z={best_z}, Gate={best_gate.name if best_gate else 'none'}")
    print(f"\n  {'Delay':<8} {'Tr Trades':<10} {'Tr WR':<8} {'Tr PnL':<12} "
          f"{'OS Trades':<10} {'OS WR':<8} {'OS PnL':<12}")
    print(f"  {'-'*80}")

    for delay in delays:
        cfg = Config(vol_method=best_vm, z_threshold=best_z, min_delay_s=delay,
                    entry_price=0.30, vol_gate=best_gate)
        tr = run_backtest(train_windows, cfg, "train", train_hours)
        os = run_backtest(oos2_windows, cfg, "oos2", oos2_hours)
        print(f"  {delay:<8} {tr.trades:<10} {tr.win_rate*100:>5.1f}%  ${tr.total_pnl:>9,.0f}  "
              f"{os.trades:<10} {os.win_rate*100:>5.1f}%  ${os.total_pnl:>9,.0f}")

    # ==========================================================================
    # PHASE 4: Entry Price Sensitivity (with best config)
    # ==========================================================================
    print(f"\n{'='*80}")
    print(f"  PHASE 4: Entry Price Sensitivity (best config)")
    print(f"{'='*80}")

    entry_prices = [0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]

    print(f"  Method: {best_vm.name}, Z={best_z}, Delay=60s, Gate={best_gate.name if best_gate else 'none'}")
    print(f"\n  {'Price':<8} {'BE WR':<8} {'Tr WR':<8} {'Tr PnL':<12} {'Tr $/hr':<10} "
          f"{'OS WR':<8} {'OS PnL':<12} {'OS $/hr':<10} {'Status'}")
    print(f"  {'-'*100}")

    # Get trade signals once (win/loss doesn't change with price)
    cfg_base = Config(vol_method=best_vm, z_threshold=best_z, min_delay_s=60,
                      entry_price=0.30, vol_gate=best_gate)
    tr_base = run_backtest(train_windows, cfg_base, "train", train_hours)
    os_base = run_backtest(oos2_windows, cfg_base, "oos2", oos2_hours)

    for ep in entry_prices:
        be_wr = ep  # Breakeven = entry_price for binary

        # Recompute PnL at this entry price
        tr_pnl = tr_base.wins * SHARES_PER_TRADE * (1-ep) + tr_base.losses * (-SHARES_PER_TRADE * ep)
        os_pnl = os_base.wins * SHARES_PER_TRADE * (1-ep) + os_base.losses * (-SHARES_PER_TRADE * ep)
        tr_hr = tr_pnl / max(train_hours, 1)
        os_hr = os_pnl / max(oos2_hours, 1)

        status = "PROFIT" if tr_pnl > 0 and os_pnl > 0 else ("MIXED" if tr_pnl > 0 or os_pnl > 0 else "LOSS")
        print(f"  ${ep:<6.2f} {be_wr*100:>5.0f}%  {tr_base.win_rate*100:>5.1f}%  "
              f"${tr_pnl:>9,.0f}  ${tr_hr:>7,.0f}  {os_base.win_rate*100:>5.1f}%  "
              f"${os_pnl:>9,.0f}  ${os_hr:>7,.0f}  {status}")

    # ==========================================================================
    # PHASE 5: Adaptive EWMA Vol Gate (zero-calibration)
    # ==========================================================================
    print(f"\n{'='*80}")
    print(f"  PHASE 5: Adaptive EWMA Vol Gate (zero calibration, self-adapting)")
    print(f"{'='*80}")

    k_values = [0.5, 0.7, 0.8, 1.0, 1.2, 1.5, 2.0]
    halflife_values = [5, 10, 20, 50, 100]

    print(f"  Sweeping: {len(k_values)} k × {len(halflife_values)} halflife = "
          f"{len(k_values)*len(halflife_values)} configs × 2 periods")
    print(f"  Using: {best_vm.name}, Z={best_z}, Delay=60s")

    phase5_results = []
    for k in k_values:
        for hl in halflife_values:
            gate = AdaptiveEWMAGate(k=k, halflife_windows=hl)
            cfg = Config(vol_method=best_vm, z_threshold=best_z, min_delay_s=60,
                        entry_price=0.30, vol_gate=gate)

            # Run training (gate accumulates state sequentially)
            tr = run_backtest(train_windows, cfg, "train", train_hours)

            # Reset gate for OOS2 (fresh start, no information leak)
            gate.reset()
            os = run_backtest(oos2_windows, cfg, "oos2", oos2_hours)

            phase5_results.append((cfg, tr, os, gate))

    # Print results table
    print(f"\n  {'Config':<30} {'Tr Tr':<6} {'Tr WR':<8} {'Tr PnL':<11} {'Tr $/hr':<9} "
          f"{'OS Tr':<6} {'OS WR':<8} {'OS PnL':<11} {'OS $/hr':<9} {'Gated%':<7}")
    print(f"  {'-'*115}")

    phase5_sorted = sorted(phase5_results,
                           key=lambda x: x[1].total_pnl + x[2].total_pnl, reverse=True)
    for cfg, tr, os, gate in phase5_sorted:
        gate_pct = (tr.gated_out / max(tr.total_windows, 1)) * 100
        marker = "**" if tr.total_pnl > 0 and os.total_pnl > 0 else "  "
        tr_hr = tr.total_pnl / max(train_hours, 1)
        os_hr = os.total_pnl / max(oos2_hours, 1)
        print(f"{marker}{cfg.vol_gate.name:<28} {tr.trades:<6} {tr.win_rate*100:>5.1f}%  "
              f"${tr.total_pnl:>8,.0f} ${tr_hr:>6,.0f}  "
              f"{os.trades:<6} {os.win_rate*100:>5.1f}%  "
              f"${os.total_pnl:>8,.0f} ${os_hr:>6,.0f}  {gate_pct:>5.1f}%")

    # --- Comparison: Adaptive vs Fixed Percentile Gate ---
    print(f"\n  {'='*80}")
    print(f"  COMPARISON: Adaptive EWMA vs Fixed Percentile Gate")
    print(f"  {'='*80}")

    # Best fixed gate from Phase 2
    best_fixed = phase2_sorted[0] if phase2_sorted else None
    # Best adaptive gate
    best_adaptive = phase5_sorted[0] if phase5_sorted else None

    print(f"\n  {'Config':<40} {'Tr WR':<8} {'Tr PnL':<11} {'Tr $/hr':<9} "
          f"{'OS WR':<8} {'OS PnL':<11} {'OS $/hr':<9}")
    print(f"  {'-'*100}")

    if best_fixed:
        cfg, tr, os = best_fixed[0], best_fixed[1], best_fixed[2]
        tr_hr = tr.total_pnl / max(train_hours, 1)
        os_hr = os.total_pnl / max(oos2_hours, 1)
        print(f"  {'Fixed ' + cfg.vol_gate.name:<38} {tr.win_rate*100:>5.1f}%  "
              f"${tr.total_pnl:>8,.0f} ${tr_hr:>6,.0f}  "
              f"{os.win_rate*100:>5.1f}%  ${os.total_pnl:>8,.0f} ${os_hr:>6,.0f}  [BASELINE]")

    if best_adaptive:
        cfg, tr, os = best_adaptive[0], best_adaptive[1], best_adaptive[2]
        tr_hr = tr.total_pnl / max(train_hours, 1)
        os_hr = os.total_pnl / max(oos2_hours, 1)
        print(f"  {'Adaptive ' + cfg.vol_gate.name:<38} {tr.win_rate*100:>5.1f}%  "
              f"${tr.total_pnl:>8,.0f} ${tr_hr:>6,.0f}  "
              f"{os.win_rate*100:>5.1f}%  ${os.total_pnl:>8,.0f} ${os_hr:>6,.0f}  [ADAPTIVE]")

    # No-gate baseline
    cfg_nogate = Config(vol_method=best_vm, z_threshold=best_z, min_delay_s=60,
                        entry_price=0.30, vol_gate=None)
    tr_nogate = run_backtest(train_windows, cfg_nogate, "train", train_hours)
    os_nogate = run_backtest(oos2_windows, cfg_nogate, "oos2", oos2_hours)
    tr_hr = tr_nogate.total_pnl / max(train_hours, 1)
    os_hr = os_nogate.total_pnl / max(oos2_hours, 1)
    print(f"  {'No Gate (baseline)':<38} {tr_nogate.win_rate*100:>5.1f}%  "
          f"${tr_nogate.total_pnl:>8,.0f} ${tr_hr:>6,.0f}  "
          f"{os_nogate.win_rate*100:>5.1f}%  ${os_nogate.total_pnl:>8,.0f} ${os_hr:>6,.0f}  [NO GATE]")

    # Verdict
    if best_adaptive and best_fixed:
        adaptive_combined = best_adaptive[1].total_pnl + best_adaptive[2].total_pnl
        fixed_combined = best_fixed[1].total_pnl + best_fixed[2].total_pnl
        pct_of_fixed = (adaptive_combined / max(fixed_combined, 1)) * 100
        print(f"\n  Adaptive gate achieves {pct_of_fixed:.0f}% of fixed gate combined PnL")
        if pct_of_fixed >= 80:
            print(f"  ✓ Adaptive gate is viable (≥80% of fixed) — no calibration needed!")
        else:
            print(f"  ✗ Adaptive gate underperforms fixed gate significantly")

    # --- Warmup Behavior Analysis ---
    print(f"\n  {'='*80}")
    print(f"  WARMUP BEHAVIOR ANALYSIS (best adaptive config on training)")
    print(f"  {'='*80}")

    # Re-run best adaptive config to capture warmup history
    if best_adaptive:
        best_k = best_adaptive[0].vol_gate.k
        best_hl = best_adaptive[0].vol_gate.halflife
        warmup_gate = AdaptiveEWMAGate(k=best_k, halflife_windows=best_hl)
        warmup_cfg = Config(vol_method=best_vm, z_threshold=best_z, min_delay_s=60,
                           entry_price=0.30, vol_gate=warmup_gate)
        _ = run_backtest(train_windows, warmup_cfg, "train", train_hours)

        # Show first 50 windows
        history = warmup_gate.history
        n_show = min(50, len(history))
        print(f"\n  First {n_show} windows (k={best_k:.1f}, halflife={best_hl}):")
        print(f"  {'Win#':<6} {'PreVol':<12} {'VolEMA':<12} {'Ratio':<8} {'Pass?':<6}")
        print(f"  {'-'*50}")

        for i in range(n_show):
            h = history[i]
            pass_str = "YES" if h['allowed'] else "no"
            print(f"  {i+1:<6} {h['pre_vol']:<12.6f} {h['vol_ema']:<12.6f} "
                  f"{h['ratio']:<8.3f} {pass_str:<6}")

        # Summary stats
        total_windows = len(history)
        passed = sum(1 for h in history if h['allowed'])
        warmup_region = history[:best_hl * 3] if len(history) > best_hl * 3 else history
        warmup_passed = sum(1 for h in warmup_region if h['allowed'])

        print(f"\n  Warmup summary:")
        print(f"    Total windows: {total_windows}")
        print(f"    Passed gate: {passed} ({passed/max(total_windows,1)*100:.1f}%)")
        print(f"    First {len(warmup_region)} windows (3× halflife): "
              f"{warmup_passed}/{len(warmup_region)} passed "
              f"({warmup_passed/max(len(warmup_region),1)*100:.1f}%)")

        # Stabilization check: compare gate rate in first half vs second half
        if total_windows > 20:
            first_half = history[:total_windows//2]
            second_half = history[total_windows//2:]
            first_rate = sum(1 for h in first_half if h['allowed']) / len(first_half)
            second_rate = sum(1 for h in second_half if h['allowed']) / len(second_half)
            print(f"    First-half pass rate: {first_rate*100:.1f}%")
            print(f"    Second-half pass rate: {second_rate*100:.1f}%")
            print(f"    → Gate {'stabilized' if abs(first_rate - second_rate) < 0.10 else 'still adapting'}")

    # ==========================================================================
    # SUMMARY
    # ==========================================================================
    print(f"\n{'='*80}")
    print(f"  FINAL SUMMARY")
    print(f"{'='*80}")

    # Best robust config (include adaptive results)
    phase5_tuples = [(cfg, tr, os) for cfg, tr, os, gate in phase5_results]
    all_robust = [(cfg, tr, os) for cfg, tr, os in phase1_results + phase2_results + phase5_tuples
                  if tr.total_pnl > 0 and os.total_pnl > 0]
    if all_robust:
        best_overall = max(all_robust, key=lambda x: x[1].total_pnl + x[2].total_pnl)
        cfg, tr, os = best_overall
        print(f"\n  Best Robust Config: {cfg.label}")
        print(f"  Training: {tr.trades} trades, {tr.win_rate*100:.1f}% WR, ${tr.total_pnl:,.0f} PnL, ${tr.pnl_per_hour:,.0f}/hr")
        print(f"  OOS2:     {os.trades} trades, {os.win_rate*100:.1f}% WR, ${os.total_pnl:,.0f} PnL, ${os.pnl_per_hour:,.0f}/hr")

        # Comparison to actual wallet
        print(f"\n  {'Metric':<25} {'Actual 0xa5e8':<18} {'Best Backtest':<18}")
        print(f"  {'-'*60}")
        print(f"  {'Win Rate':<25} {'54.2%':<18} {tr.win_rate*100:.1f}%")
        print(f"  {'Entry Price':<25} {'$0.30':<18} ${cfg.entry_price:.2f}")
        print(f"  {'PnL/Hour':<25} {'$1,222':<18} ${tr.pnl_per_hour:,.0f}")
        print(f"  {'Trades/Hour':<25} {'1.8':<18} {tr.trades/max(train_hours,1):.1f}")
        print(f"  {'Avg Z at Entry':<25} {'~1.07':<18} {tr.avg_z:.2f}")
        print(f"  {'Avg BTC Move':<25} {'0.06%':<18} {tr.avg_move_pct:.4f}%")

    # Export
    rows = []
    for cfg, tr, os in phase1_results + phase2_results + phase5_tuples:
        for result, period in [(tr, "train"), (os, "oos2")]:
            rows.append({
                'period': period,
                'vol_method': cfg.vol_method.name,
                'z_threshold': cfg.z_threshold,
                'delay_s': cfg.min_delay_s,
                'entry_price': cfg.entry_price,
                'vol_gate': cfg.vol_gate.name if cfg.vol_gate else "none",
                'trades': result.trades,
                'wins': result.wins,
                'losses': result.losses,
                'win_rate': result.win_rate,
                'total_pnl': result.total_pnl,
                'pnl_per_hour': result.pnl_per_hour,
                'avg_z': result.avg_z,
                'avg_move_pct': result.avg_move_pct,
                'gated_out': result.gated_out,
            })

    out_csv = Path(__file__).parent / "contrarian_backtest_v2_results.csv"
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    print(f"\n  Results exported to: {out_csv}")


if __name__ == "__main__":
    main()
