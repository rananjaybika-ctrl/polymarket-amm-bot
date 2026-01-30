#!/usr/bin/env python3
"""
OBI Comparison Backtest - OOS7 Data

Compares AGGRESSIVE mode performance with OBI filter ON vs OFF.
Uses OOS7 data (Jan 29-30, 2026).

Expected: OBI ON should show ~89% direction accuracy vs ~77-85% with OBI OFF

Usage:
    python research/backtests/test_obi_comparison_oos7.py
"""

import pandas as pd
import numpy as np
import math
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
from collections import deque
from tqdm import tqdm

# =============================================================================
# CONFIGURATION (matching live strategy - enhanced_spike.py)
# =============================================================================

TARGET_SHARES = 50          # PRODUCTION: 50 shares
MIN_TIME = 180              # Entry cutoff (seconds remaining) - matches live
MIN_RUNTIME_SECS = 300      # 5 minutes minimum market duration
HIGH_ENTRY_THRESHOLD = 0.90  # Skip entries >= $0.90 (unhedgeable)

# Spike detection - CANONICAL from TRADING_CONFIGS.py (Jan 27, 2026)
# Source of truth: research/reference/TRADING_CONFIGS.py AGGRESSIVE config
# lookback_ticks=72, lookback_ms=1200 (validated ~$9.00/hr @ 50 shares)
# CRITICAL: Uses OU adaptive threshold, NOT fixed 0.02
SPIKE_LOOKBACK_TICKS = 72   # 1200ms at 60Hz (CANONICAL)

# OU adaptive threshold parameters (per TRADING_CONFIGS.py threshold_method="ou")
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10

# Enhanced signal filtering
VELOCITY_CONFIRM_THRESHOLD = 0.10
ENHANCED_SCORE_THRESHOLD = 0.40

# Time-stop (matching live)
TIME_STOP_SECONDS = 120.0   # 2 minutes

# Loser bid calculation (from live enhanced_spike.py)
DROP_MULTIPLIER = 0.50      # Recalibrated Jan 18
DROP_INTERCEPT = 0.08       # Recalibrated Jan 18
TARGET_PAIR_COST = 0.99

# Cycling
MIN_CYCLE_GAP_MS = 200      # Minimum gap between trades


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class TradeResult:
    market_slug: str
    cycle_num: int
    entry_time_remaining: float
    signal_score: float
    winner_side: str
    winner_fill_price: float
    loser_fill_price: float
    hedge_type: str
    pair_cost: float
    pnl: float
    correct_direction: bool
    spike_magnitude: float
    obi_available: bool
    obi_confirmed: bool


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def velocity_confirms_spike(spike_dir: str, velocity_bps: float) -> bool:
    """Check if velocity confirms spike direction."""
    if spike_dir == "UP":
        return velocity_bps > -VELOCITY_CONFIRM_THRESHOLD
    elif spike_dir == "DOWN":
        return velocity_bps < VELOCITY_CONFIRM_THRESHOLD
    return True


def obi_confirms_spike(spike_dir: str, up_imbalance: Optional[float],
                       down_imbalance: Optional[float]) -> Tuple[bool, bool]:
    """
    Check if Order Book Imbalance confirms spike direction.

    Returns: (obi_available, obi_confirms)
    """
    if spike_dir == "UP":
        if up_imbalance is not None and not np.isnan(up_imbalance):
            return True, up_imbalance > 0
    elif spike_dir == "DOWN":
        if down_imbalance is not None and not np.isnan(down_imbalance):
            return True, down_imbalance > 0
    return False, True  # Not available = don't filter


def compute_enhanced_score(spike_mag: float, velocity_bps: float,
                           spike_dir: str, time_remaining: float) -> float:
    """Compute composite score (matching live strategy)."""
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
        spike_magnitude: BTC % change (e.g., 0.05 for 0.05%) - NOT divided by 100!
    """
    # FIX: Do NOT divide by 100 - matches enhanced_spike.py:526
    expected_drop = DROP_MULTIPLIER * spike_magnitude + DROP_INTERCEPT
    max_loser = TARGET_PAIR_COST - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid))


# =============================================================================
# OU PARAMETERS (for adaptive threshold)
# =============================================================================

_ou_params = None


def load_ou_params():
    """Load OU parameters for adaptive threshold."""
    global _ou_params
    import math
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from src.strategies.ou_volatility import OUParameters
        _ou_params = OUParameters.load("research/ou_params.json")
        print(f"[OU] Loaded: mu={_ou_params.mu:.4f}, sigma_stat={_ou_params.sigma_stat:.4f}")
    except Exception as e:
        print(f"[OU] Warning: {e} - using fixed threshold 0.02")
        _ou_params = None


def compute_ou_threshold(volatility: float) -> float:
    """Compute OU adaptive threshold from current volatility."""
    import math
    global _ou_params
    if _ou_params is None:
        return OU_BASE_THRESHOLD
    vol = max(volatility, 1e-6)
    log_vol = math.log(vol)
    z_score = (log_vol - _ou_params.mu) / _ou_params.sigma_stat
    z_clamped = max(-10, min(10, z_score * OU_SIGMOID_STEEPNESS))
    sigmoid = 1.0 / (1.0 + math.exp(-z_clamped))
    multiplier = OU_K_LOW + (OU_K_HIGH - OU_K_LOW) * sigmoid
    threshold = OU_BASE_THRESHOLD * multiplier
    return max(OU_MIN_THRESHOLD, min(OU_MAX_THRESHOLD, threshold))


class SpikeDetector:
    """60Hz spike detection with OU adaptive threshold."""

    def __init__(self, lookback: int = SPIKE_LOOKBACK_TICKS):
        self.lookback = lookback
        self.price_history = deque(maxlen=100)
        # EWMA volatility tracking for OU adaptive threshold
        self.ewma_halflife = 300
        self.alpha = 1 - 0.5 ** (1.0 / self.ewma_halflife)
        self.variance = 0.01

    def detect(self, price: float) -> Tuple[Optional[str], float]:
        self.price_history.append(price)

        if len(self.price_history) < self.lookback + 1:
            return None, 0.0

        current = self.price_history[-1]
        previous = self.price_history[-(self.lookback + 1)]

        if previous <= 0:
            return None, 0.0

        change_pct = (current - previous) / previous * 100
        magnitude = abs(change_pct)

        # Update EWMA volatility for OU adaptive threshold
        if len(self.price_history) >= 2:
            ret = (current - self.price_history[-2]) / self.price_history[-2] * 100
            self.variance = self.alpha * (ret ** 2) + (1 - self.alpha) * self.variance

        vol = max(np.sqrt(self.variance), 1e-6)
        threshold = compute_ou_threshold(vol)

        if magnitude >= threshold:
            direction = "UP" if change_pct > 0 else "DOWN"
            return direction, magnitude

        return None, 0.0

    def reset(self):
        self.price_history.clear()
        self.variance = 0.01


# =============================================================================
# DATA LOADING
# =============================================================================

def load_oos7_data():
    """Load OOS7 data (Jan 29-30, 2026)."""
    print("=" * 60)
    print("Loading OOS7 Data (Jan 29-30, 2026)")
    print("=" * 60)

    base_dir = Path("/Users/rananjaybika/polymarket-amm-bot")

    # Load Binance 60Hz data
    btc_path = base_dir / "research/binance_hf/btc_prices_20260129_160523.csv"
    print(f"\nLoading Binance HF: {btc_path.name}")
    btc_df = pd.read_csv(btc_path)
    print(f"  Rows: {len(btc_df):,}")

    # Load Observer data (OOS7 = Jan 29-30)
    obs_dir = base_dir / "research/observer"
    obs_files = [
        obs_dir / "grid_obs_20260129.csv",
        obs_dir / "grid_obs_20260130.csv",
    ]

    print("\nLoading Observer data:")
    obs_dfs = []
    for f in obs_files:
        if f.exists():
            df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)
            print(f"  {f.name}: {len(df):,} rows")

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    print(f"  Combined: {len(obs_df):,} rows")

    # Check OBI columns exist
    obi_cols = ['up_imbalance', 'down_imbalance']
    has_obi = all(col in obs_df.columns for col in obi_cols)
    print(f"\n  OBI columns present: {has_obi}")
    if has_obi:
        up_imb_valid = obs_df['up_imbalance'].notna().sum()
        down_imb_valid = obs_df['down_imbalance'].notna().sum()
        print(f"  up_imbalance valid: {up_imb_valid:,} ({100*up_imb_valid/len(obs_df):.1f}%)")
        print(f"  down_imbalance valid: {down_imb_valid:,} ({100*down_imb_valid/len(obs_df):.1f}%)")

    # Load resolutions
    res_path = obs_dir / "market_resolutions_verified.csv"
    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))
    print(f"\nResolutions loaded: {len(res_map)} markets")

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

    # Add resolutions
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

    print(f"\nValid markets: {len(valid_slugs)}")
    print(f"Observer rows: {len(obs_df):,}")
    print(f"Binance rows: {len(btc_df):,}")

    return btc_df, obs_df, res_map, duration_hours


# =============================================================================
# BACKTEST SIMULATION
# =============================================================================

def simulate_market(btc_df: pd.DataFrame, obs_df: pd.DataFrame,
                    slug: str, resolution: str,
                    use_obi_filter: bool) -> List[TradeResult]:
    """
    Simulate trading on a single market.

    Args:
        use_obi_filter: If True, skip entries when OBI disagrees with spike
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
    last_hedge_ts = 0
    in_position = False
    position_data = None

    btc_idx = 0
    obs_idx = 0

    time_stop_ms = TIME_STOP_SECONDS * 1000

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

        # If in position, check for hedge/time-stop
        if in_position and position_data is not None:
            winner_side = position_data['winner_side']
            loser_side = position_data['loser_side']
            winner_entry = position_data['winner_entry']
            loser_target = position_data['loser_target']
            entry_ts = position_data['entry_ts']
            spike_mag = position_data['spike_magnitude']
            score = position_data['score']
            obi_available = position_data['obi_available']
            obi_confirmed = position_data['obi_confirmed']

            # Get current prices
            if loser_side == "UP":
                loser_ask = obs_row['up_ask']
            else:
                loser_ask = obs_row['down_ask']

            # Check passive fill
            if loser_ask <= loser_target:
                loser_fill = loser_target
                pair_cost = winner_entry + loser_fill
                pnl = (1.0 - pair_cost) * TARGET_SHARES

                trades.append(TradeResult(
                    market_slug=slug,
                    cycle_num=cycle_num,
                    entry_time_remaining=position_data['entry_time_rem'],
                    signal_score=score,
                    winner_side=winner_side,
                    winner_fill_price=winner_entry,
                    loser_fill_price=loser_fill,
                    hedge_type="passive",
                    pair_cost=pair_cost,
                    pnl=pnl,
                    correct_direction=(resolution == winner_side),
                    spike_magnitude=spike_mag,
                    obi_available=obi_available,
                    obi_confirmed=obi_confirmed,
                ))

                in_position = False
                position_data = None
                last_hedge_ts = btc_ts
                detector.reset()
                btc_idx += 1
                continue

            # Check time-stop (ONLY if NOT in profit - matches live enhanced_spike.py)
            elapsed_ms = btc_ts - entry_ts
            if elapsed_ms >= time_stop_ms:
                # Get current winner bid to check if in profit
                if winner_side == "UP":
                    winner_bid_current = obs_row['up_bid']
                else:
                    winner_bid_current = obs_row['down_bid']

                # Check if in profit: winner_bid >= entry price
                in_profit = pd.notna(winner_bid_current) and winner_bid_current >= winner_entry

                if not in_profit:
                    # NOT in profit - execute time-stop
                    loser_fill = loser_ask  # Market order at ask
                    pair_cost = winner_entry + loser_fill
                    pnl = (1.0 - pair_cost) * TARGET_SHARES

                    trades.append(TradeResult(
                        market_slug=slug,
                        cycle_num=cycle_num,
                        entry_time_remaining=position_data['entry_time_rem'],
                        signal_score=score,
                        winner_side=winner_side,
                        winner_fill_price=winner_entry,
                        loser_fill_price=loser_fill,
                        hedge_type="time_stop",
                        pair_cost=pair_cost,
                        pnl=pnl,
                        correct_direction=(resolution == winner_side),
                        spike_magnitude=spike_mag,
                        obi_available=obi_available,
                        obi_confirmed=obi_confirmed,
                    ))

                    in_position = False
                    position_data = None
                    last_hedge_ts = btc_ts
                    detector.reset()
                    btc_idx += 1
                    continue
                # else: in profit, keep waiting for passive fill

            btc_idx += 1
            continue

        # Not in position - look for entry signal
        # Enforce minimum gap after hedge
        if (btc_ts - last_hedge_ts) < MIN_CYCLE_GAP_MS:
            btc_idx += 1
            continue

        # Detect spike at 60Hz
        spike_dir, spike_mag = detector.detect(btc_price)

        if spike_dir is not None:
            # Velocity confirmation filter
            if not velocity_confirms_spike(spike_dir, velocity_bps):
                btc_idx += 1
                continue

            # Enhanced score filter
            score = compute_enhanced_score(spike_mag, velocity_bps, spike_dir, time_rem)
            if score < ENHANCED_SCORE_THRESHOLD:
                btc_idx += 1
                continue

            # OBI confirmation filter
            up_imbalance = obs_row.get('up_imbalance', None)
            down_imbalance = obs_row.get('down_imbalance', None)
            obi_available, obi_confirmed = obi_confirms_spike(spike_dir, up_imbalance, down_imbalance)

            if use_obi_filter and obi_available and not obi_confirmed:
                btc_idx += 1
                continue

            # High entry threshold check
            winner_side = spike_dir
            if winner_side == "UP":
                winner_ask = obs_row['up_ask']
            else:
                winner_ask = obs_row['down_ask']

            if winner_ask >= HIGH_ENTRY_THRESHOLD:
                btc_idx += 1
                continue

            # ENTRY SIGNAL
            cycle_num += 1
            loser_side = "DOWN" if winner_side == "UP" else "UP"
            winner_entry = winner_ask
            loser_target = calculate_loser_bid(winner_entry, spike_mag)

            in_position = True
            position_data = {
                'winner_side': winner_side,
                'loser_side': loser_side,
                'winner_entry': winner_entry,
                'loser_target': loser_target,
                'entry_ts': btc_ts,
                'entry_time_rem': time_rem,
                'spike_magnitude': spike_mag,
                'score': score,
                'obi_available': obi_available,
                'obi_confirmed': obi_confirmed,
            }

        btc_idx += 1

    # Handle unresolved position at market end
    if in_position and position_data is not None:
        winner_side = position_data['winner_side']
        winner_entry = position_data['winner_entry']
        spike_mag = position_data['spike_magnitude']
        score = position_data['score']
        obi_available = position_data['obi_available']
        obi_confirmed = position_data['obi_confirmed']

        if resolution == winner_side:
            pnl = (1.0 - winner_entry) * TARGET_SHARES
            loser_fill = 0.0
        else:
            pnl = (0.0 - winner_entry) * TARGET_SHARES
            loser_fill = 1.0

        trades.append(TradeResult(
            market_slug=slug,
            cycle_num=cycle_num,
            entry_time_remaining=position_data['entry_time_rem'],
            signal_score=score,
            winner_side=winner_side,
            winner_fill_price=winner_entry,
            loser_fill_price=loser_fill,
            hedge_type="resolution",
            pair_cost=winner_entry + loser_fill,
            pnl=pnl,
            correct_direction=(resolution == winner_side),
            spike_magnitude=spike_mag,
            obi_available=obi_available,
            obi_confirmed=obi_confirmed,
        ))

    return trades


def run_backtest(btc_df: pd.DataFrame, obs_df: pd.DataFrame,
                 res_map: Dict[str, str], use_obi_filter: bool,
                 label: str) -> List[TradeResult]:
    """Run backtest on all markets."""
    print(f"\n{'='*60}")
    print(f"Running Backtest: {label}")
    print(f"{'='*60}")

    all_trades = []
    slugs = obs_df['market_slug'].unique()

    for slug in tqdm(slugs, desc=label):
        resolution = res_map.get(slug)
        if resolution not in ['UP', 'DOWN']:
            continue

        trades = simulate_market(btc_df, obs_df, slug, resolution, use_obi_filter)
        all_trades.extend(trades)

    return all_trades


def analyze_results(trades: List[TradeResult], label: str, hours: float):
    """Analyze and print backtest results."""
    if not trades:
        print(f"\n{label}: No trades")
        return {}

    total_pnl = sum(t.pnl for t in trades)
    hourly_rate = total_pnl / hours if hours > 0 else 0

    correct = sum(1 for t in trades if t.correct_direction)
    direction_acc = correct / len(trades) * 100

    hedged = [t for t in trades if t.hedge_type in ['passive', 'time_stop']]
    hedged_pnl = sum(t.pnl for t in hedged)

    passive = [t for t in trades if t.hedge_type == 'passive']
    time_stop = [t for t in trades if t.hedge_type == 'time_stop']
    resolution = [t for t in trades if t.hedge_type == 'resolution']

    avg_pair_cost = np.mean([t.pair_cost for t in hedged]) if hedged else 0

    # OBI analysis
    obi_available_trades = [t for t in trades if t.obi_available]
    obi_confirmed_trades = [t for t in trades if t.obi_available and t.obi_confirmed]
    obi_rejected_trades = [t for t in trades if t.obi_available and not t.obi_confirmed]

    print(f"\n--- {label} ---")
    print(f"Total trades: {len(trades)}")
    print(f"Direction accuracy: {direction_acc:.1f}%")
    print(f"Total PnL: ${total_pnl:.2f}")
    print(f"Hourly rate: ${hourly_rate:.2f}/hr")
    print(f"\nHedge breakdown:")
    print(f"  Passive: {len(passive)} ({100*len(passive)/len(trades):.1f}%)")
    print(f"  Time-stop: {len(time_stop)} ({100*len(time_stop)/len(trades):.1f}%)")
    print(f"  Resolution: {len(resolution)} ({100*len(resolution)/len(trades):.1f}%)")
    print(f"  Avg pair cost: ${avg_pair_cost:.4f}")

    if obi_available_trades:
        obi_confirmed_acc = sum(1 for t in obi_confirmed_trades if t.correct_direction) / len(obi_confirmed_trades) * 100 if obi_confirmed_trades else 0
        print(f"\nOBI Analysis:")
        print(f"  OBI available: {len(obi_available_trades)} trades")
        print(f"  OBI confirmed: {len(obi_confirmed_trades)} ({100*len(obi_confirmed_trades)/len(obi_available_trades):.1f}%)")
        print(f"  OBI confirmed accuracy: {obi_confirmed_acc:.1f}%")

    return {
        'label': label,
        'trades': len(trades),
        'direction_acc': direction_acc,
        'total_pnl': total_pnl,
        'hourly_rate': hourly_rate,
        'passive_pct': 100*len(passive)/len(trades),
        'time_stop_pct': 100*len(time_stop)/len(trades),
        'resolution_pct': 100*len(resolution)/len(trades),
        'avg_pair_cost': avg_pair_cost,
    }


def main():
    print("=" * 60)
    print("OBI COMPARISON BACKTEST - OOS7 DATA")
    print("Comparing AGGRESSIVE mode with OBI ON vs OFF")
    print("Using OU ADAPTIVE threshold (per TRADING_CONFIGS.py)")
    print("=" * 60)

    # Load OU parameters for adaptive threshold
    load_ou_params()

    # Load data
    btc_df, obs_df, res_map, hours = load_oos7_data()

    # Run backtests
    trades_obi_on = run_backtest(btc_df, obs_df, res_map, use_obi_filter=True, label="OBI ON")
    trades_obi_off = run_backtest(btc_df, obs_df, res_map, use_obi_filter=False, label="OBI OFF")

    # Analyze results
    results_on = analyze_results(trades_obi_on, "OBI ON", hours)
    results_off = analyze_results(trades_obi_off, "OBI OFF", hours)

    # Comparison summary
    print("\n" + "=" * 60)
    print("COMPARISON SUMMARY")
    print("=" * 60)

    if results_on and results_off:
        print(f"\n{'Metric':<25} {'OBI ON':>12} {'OBI OFF':>12} {'Diff':>12}")
        print("-" * 60)
        print(f"{'Trades':<25} {results_on['trades']:>12} {results_off['trades']:>12} {results_on['trades'] - results_off['trades']:>+12}")
        print(f"{'Direction Accuracy':<25} {results_on['direction_acc']:>11.1f}% {results_off['direction_acc']:>11.1f}% {results_on['direction_acc'] - results_off['direction_acc']:>+11.1f}%")
        print(f"{'Total PnL':<25} ${results_on['total_pnl']:>10.2f} ${results_off['total_pnl']:>10.2f} ${results_on['total_pnl'] - results_off['total_pnl']:>+10.2f}")
        print(f"{'Hourly Rate':<25} ${results_on['hourly_rate']:>10.2f} ${results_off['hourly_rate']:>10.2f} ${results_on['hourly_rate'] - results_off['hourly_rate']:>+10.2f}")
        print(f"{'Avg Pair Cost':<25} ${results_on['avg_pair_cost']:>10.4f} ${results_off['avg_pair_cost']:>10.4f} ${results_on['avg_pair_cost'] - results_off['avg_pair_cost']:>+10.4f}")

        print("\n" + "=" * 60)
        print("EXPECTED vs ACTUAL")
        print("=" * 60)
        print(f"Expected OBI improvement: +4.1pp accuracy (89% vs 77-85%)")
        print(f"Actual OBI improvement:   {results_on['direction_acc'] - results_off['direction_acc']:+.1f}pp accuracy")

        if results_on['direction_acc'] > results_off['direction_acc']:
            print("\n✓ OBI filter IMPROVES accuracy as expected")
        else:
            print("\n✗ OBI filter did NOT improve accuracy - investigate!")


if __name__ == "__main__":
    main()
