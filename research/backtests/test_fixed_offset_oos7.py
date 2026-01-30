#!/usr/bin/env python3
"""
3-WAY CONFIG COMPARISON - OOS7 Data

Compares three configurations to investigate PnL variance between scripts:
1. OLD CONFIG (time_stop_skip_comparison.py settings)
2. CURRENT CONFIG (test_obi_comparison_oos7.py settings)
3. /100 BUG CONFIG (fixed loser offset ~0.08)

The /100 bug hypothesis: A fixed loser offset (~0.08) might outperform because:
- More aggressive (lower) loser bids consistently
- Higher passive fill rate
- Dynamic spike-based component may not add value

OPTIMIZATION: Precomputes spikes ONCE per threshold method (OU vs fixed),
then iterates over spike events instead of tick-by-tick.

Usage:
    python research/backtests/test_fixed_offset_oos7.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
from tqdm import tqdm
import math

# =============================================================================
# SHARED CONFIGURATION
# =============================================================================

TARGET_SHARES = 50
MIN_RUNTIME_SECS = 300

# Spike detection (canonical from TRADING_CONFIGS.py)
SPIKE_LOOKBACK_TICKS = 72  # 1200ms at 60Hz
SPIKE_THRESHOLD = 0.02     # Fixed threshold for CURRENT config

# Hedge pricing (same across all configs)
DROP_MULTIPLIER = 0.50
DROP_INTERCEPT = 0.08
TARGET_PAIR_COST = 0.99

# Time-stop
TIME_STOP_SECONDS = 120.0

# Skip threshold
HIGH_ENTRY_THRESHOLD = 0.90

# OU threshold parameters (for OLD config)
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10

# Velocity threshold
RAW_VELOCITY_THRESHOLD = 0.10


# =============================================================================
# CONFIG DEFINITIONS
# =============================================================================

@dataclass
class ConfigSettings:
    """Settings that differ between configs."""
    name: str
    min_time: int                    # Entry cutoff (seconds remaining)
    min_cycle_gap_ms: int            # Minimum gap between trades
    use_enhanced_score: bool         # Enhanced score >= 0.40 filter
    use_obi_filter: bool             # OBI confirmation filter
    skip_operator_gte: bool          # True = >= 0.90, False = > 0.90
    use_ou_threshold: bool           # True = OU adaptive, False = fixed
    use_fixed_loser_offset: bool     # True = /100 bug (fixed ~0.08)

    # For OLD config z-score filter
    z_lo: Optional[float] = None
    z_hi: Optional[float] = None


OLD_CONFIG = ConfigSettings(
    name="OLD (time_stop_skip.py)",
    min_time=60,
    min_cycle_gap_ms=1000,
    use_enhanced_score=False,
    use_obi_filter=False,
    skip_operator_gte=False,        # > 0.90
    use_ou_threshold=True,          # OU adaptive threshold (per TRADING_CONFIGS.py)
    use_fixed_loser_offset=False,
    z_lo=0.0,
    z_hi=1.5,
)

CURRENT_CONFIG = ConfigSettings(
    name="CURRENT (FIXED - OU)",
    min_time=180,
    min_cycle_gap_ms=200,
    use_enhanced_score=True,        # Score >= 0.40
    use_obi_filter=True,            # OBI filter ON
    skip_operator_gte=True,         # >= 0.90
    use_ou_threshold=True,          # OU adaptive (FIXED - was wrongly using fixed 0.02)
    use_fixed_loser_offset=False,
)

BUG_CONFIG = ConfigSettings(
    name="/100 BUG (fixed offset)",
    min_time=180,
    min_cycle_gap_ms=200,
    use_enhanced_score=True,
    use_obi_filter=True,
    skip_operator_gte=True,
    use_ou_threshold=True,          # OU adaptive (same as CURRENT)
    use_fixed_loser_offset=True,    # THE KEY DIFFERENCE
)

ALL_CONFIGS = [OLD_CONFIG, CURRENT_CONFIG, BUG_CONFIG]


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
    expected_drop: float  # Track this for analysis


@dataclass
class ConfigResult:
    config_name: str
    trades: int
    total_pnl: float
    hourly_rate: float
    sharpe_ratio: float
    passive_fill_pct: float
    avg_pair_cost: float
    direction_acc: float
    trades_list: List[TradeResult] = field(default_factory=list)


# =============================================================================
# OU PARAMETERS (for OLD config)
# =============================================================================

_ou_params = None


def load_ou_params():
    global _ou_params
    try:
        from src.strategies.ou_volatility import OUParameters
        _ou_params = OUParameters.load("research/ou_params.json")
        print(f"[OU] Loaded: mu={_ou_params.mu:.4f}, sigma_stat={_ou_params.sigma_stat:.4f}")
    except Exception as e:
        print(f"[OU] Warning: {e} - using defaults")
        _ou_params = None


def compute_ou_threshold(volatility: float) -> float:
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


# =============================================================================
# PRECOMPUTE SPIKES (vectorized for speed)
# =============================================================================

def precompute_spikes_ou(btc_df: pd.DataFrame, lookback: int = SPIKE_LOOKBACK_TICKS) -> pd.DataFrame:
    """
    Precompute spikes using OU adaptive threshold (for OLD config).
    Returns DataFrame with spike events only.
    """
    print("  Precomputing spikes with OU adaptive threshold...")
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    df['price_prev'] = df['price'].shift(lookback)
    df['change_pct'] = (df['price'] - df['price_prev']) / df['price_prev'] * 100
    df['magnitude'] = df['change_pct'].abs()

    # Compute EWMA volatility for OU threshold
    returns = df['price'].pct_change() * 100
    ewma_halflife = 300
    alpha = 1 - 0.5 ** (1.0 / ewma_halflife)

    variance = returns.iloc[:60].var() if len(returns) > 60 else 0.01
    volatilities = []
    zscores = []

    for i, r in enumerate(returns):
        if pd.isna(r):
            volatilities.append(0.01)
            zscores.append(0.5)
            continue
        variance = alpha * (r ** 2) + (1 - alpha) * variance
        vol = max(np.sqrt(variance), 1e-6)
        volatilities.append(vol)

        if _ou_params:
            log_vol = math.log(vol)
            z = (log_vol - _ou_params.mu) / _ou_params.sigma_stat
            zscores.append(max(0, min(3, z)))
        else:
            zscores.append(0.5)

    df['volatility'] = volatilities
    df['zscore'] = zscores
    df['threshold'] = df['volatility'].apply(compute_ou_threshold)
    df['spike_detected'] = df['magnitude'] >= df['threshold']

    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['change_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['change_pct'] < 0), 'spike_direction'] = 'DOWN'
    df['spike_magnitude'] = df['magnitude'].where(df['spike_detected'], 0)

    spikes_only = df[df['spike_detected'] == True].copy()
    print(f"    Found {len(spikes_only):,} spike events (OU adaptive)")

    return spikes_only[['timestamp_ms', 'price', 'spike_direction', 'spike_magnitude', 'zscore']]


def precompute_spikes_fixed(btc_df: pd.DataFrame, lookback: int = SPIKE_LOOKBACK_TICKS,
                            threshold: float = SPIKE_THRESHOLD) -> pd.DataFrame:
    """
    Precompute spikes using fixed threshold (for CURRENT/BUG configs).
    Returns DataFrame with spike events only.
    """
    print(f"  Precomputing spikes with fixed threshold ({threshold}%)...")
    df = btc_df.copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)

    df['price_prev'] = df['price'].shift(lookback)
    df['change_pct'] = (df['price'] - df['price_prev']) / df['price_prev'] * 100
    df['magnitude'] = df['change_pct'].abs()

    df['spike_detected'] = df['magnitude'] >= threshold

    df['spike_direction'] = None
    df.loc[(df['spike_detected']) & (df['change_pct'] > 0), 'spike_direction'] = 'UP'
    df.loc[(df['spike_detected']) & (df['change_pct'] < 0), 'spike_direction'] = 'DOWN'
    df['spike_magnitude'] = df['magnitude'].where(df['spike_detected'], 0)

    spikes_only = df[df['spike_detected'] == True].copy()
    print(f"    Found {len(spikes_only):,} spike events (fixed threshold)")

    return spikes_only[['timestamp_ms', 'price', 'spike_direction', 'spike_magnitude']]


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def velocity_confirms_spike(spike_dir: str, velocity_bps: float,
                           threshold: float = 0.10) -> bool:
    """Check if velocity confirms spike direction."""
    if spike_dir == "UP":
        return velocity_bps > -threshold
    elif spike_dir == "DOWN":
        return velocity_bps < threshold
    return True


def obi_confirms_spike(spike_dir: str, up_imbalance: Optional[float],
                       down_imbalance: Optional[float]) -> Tuple[bool, bool]:
    """Check if Order Book Imbalance confirms spike direction."""
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


def calculate_loser_bid_dynamic(winner_entry: float, spike_magnitude: float) -> Tuple[float, float]:
    """
    Calculate loser bid - DYNAMIC (current correct method).

    Returns: (loser_bid, expected_drop)
    """
    expected_drop = DROP_MULTIPLIER * spike_magnitude + DROP_INTERCEPT
    max_loser = TARGET_PAIR_COST - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid)), expected_drop


def calculate_loser_bid_fixed(winner_entry: float, spike_magnitude: float) -> Tuple[float, float]:
    """
    Calculate loser bid - WITH /100 BUG (effectively fixed offset).

    The /100 bug makes:
        expected_drop = 0.50 * spike_mag / 100 + 0.08
                      ≈ 0.0005 + 0.08
                      ≈ 0.08 (fixed)

    Returns: (loser_bid, expected_drop)
    """
    expected_drop = DROP_MULTIPLIER * spike_magnitude / 100 + DROP_INTERCEPT
    max_loser = TARGET_PAIR_COST - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid)), expected_drop


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
# SIMULATION (using precomputed spikes for speed)
# =============================================================================

def simulate_market(spikes_df: pd.DataFrame, obs_df: pd.DataFrame,
                    slug: str, resolution: str,
                    config: ConfigSettings) -> List[TradeResult]:
    """
    Simulate trading on a single market using precomputed spikes.

    Based on time_stop_skip_comparison.py simulation logic.
    """
    # Get market data
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    # Get spikes in this market's time range
    market_spikes = spikes_df[
        (spikes_df['timestamp_ms'] >= market_start) &
        (spikes_df['timestamp_ms'] <= market_end)
    ].copy()

    if len(market_spikes) == 0:
        return []

    trades = []
    cycle_num = 0
    in_position = False
    last_hedge_ts = 0

    time_stop_ms = TIME_STOP_SECONDS * 1000

    for _, spike_row in market_spikes.iterrows():
        spike_ts = spike_row['timestamp_ms']
        spike_dir = spike_row['spike_direction']
        spike_mag = spike_row['spike_magnitude']
        zscore = spike_row.get('zscore', 0.5)  # Only in OU spikes

        # Cycling: block if still in position
        if in_position:
            continue

        # Enforce gap after hedge fill
        if (spike_ts - last_hedge_ts) < config.min_cycle_gap_ms:
            continue

        # Z-score filter (OLD config only)
        if config.z_lo is not None and zscore < config.z_lo:
            continue
        if config.z_hi is not None and zscore > config.z_hi:
            continue

        # Find nearest observer row
        obs_idx = mdf['timestamp_ms'].searchsorted(spike_ts)
        if obs_idx >= len(mdf):
            obs_idx = len(mdf) - 1

        obs_row = mdf.iloc[obs_idx]
        time_rem = obs_row['time_remaining_secs']

        if time_rem < config.min_time:
            continue

        # Get velocity
        velocity_bps = obs_row.get('velocity_bps', 0) or 0

        # Velocity confirmation
        if not velocity_confirms_spike(spike_dir, velocity_bps):
            continue

        # Enhanced score filter (CURRENT and BUG configs)
        score = compute_enhanced_score(spike_mag, velocity_bps, spike_dir, time_rem)
        if config.use_enhanced_score and score < 0.40:
            continue

        # OBI confirmation filter
        up_imbalance = obs_row.get('up_imbalance', None)
        down_imbalance = obs_row.get('down_imbalance', None)
        obi_available, obi_confirmed = obi_confirms_spike(spike_dir, up_imbalance, down_imbalance)

        if config.use_obi_filter and obi_available and not obi_confirmed:
            continue

        # Entry pricing
        winner_side = spike_dir
        loser_side = "DOWN" if winner_side == "UP" else "UP"

        if winner_side == "UP":
            winner_entry = obs_row['up_ask']
        else:
            winner_entry = obs_row['down_ask']

        # Skip rule: >= vs > depending on config
        if config.skip_operator_gte:
            if winner_entry >= HIGH_ENTRY_THRESHOLD:
                continue
        else:
            if winner_entry > HIGH_ENTRY_THRESHOLD:
                continue

        # Calculate loser bid - KEY DIFFERENCE
        if config.use_fixed_loser_offset:
            loser_target, expected_drop = calculate_loser_bid_fixed(winner_entry, spike_mag)
        else:
            loser_target, expected_drop = calculate_loser_bid_dynamic(winner_entry, spike_mag)

        # Enter position
        cycle_num += 1
        in_position = True
        entry_ts = spike_ts

        # Scan forward for hedge (at 5Hz - observer rows)
        hedge_type = "resolution"
        loser_fill = 0.0
        hedge_fill_ts = market_end

        for j in range(obs_idx + 1, len(mdf)):
            scan_row = mdf.iloc[j]
            scan_ts = scan_row['timestamp_ms']

            # Time-based stop check
            elapsed_ms = scan_ts - entry_ts
            if elapsed_ms >= time_stop_ms:
                if loser_side == "UP":
                    loser_fill = scan_row['up_ask']
                else:
                    loser_fill = scan_row['down_ask']
                hedge_type = "time_stop"
                hedge_fill_ts = scan_ts
                break

            # Passive fill check
            if loser_side == "UP":
                curr_loser_ask = scan_row['up_ask']
            else:
                curr_loser_ask = scan_row['down_ask']

            if curr_loser_ask <= loser_target:
                loser_fill = loser_target
                hedge_type = "passive"
                hedge_fill_ts = scan_ts
                break

        # If no hedge, resolve at market end
        if hedge_type == "resolution":
            if resolution == winner_side:
                loser_fill = 0.0
                pnl = (1.0 - winner_entry) * TARGET_SHARES
            else:
                loser_fill = 1.0
                pnl = -winner_entry * TARGET_SHARES
        else:
            pair_cost = winner_entry + loser_fill
            pnl = (1.0 - pair_cost) * TARGET_SHARES

        trades.append(TradeResult(
            market_slug=slug,
            cycle_num=cycle_num,
            entry_time_remaining=time_rem,
            signal_score=score,
            winner_side=winner_side,
            winner_fill_price=winner_entry,
            loser_fill_price=loser_fill,
            hedge_type=hedge_type,
            pair_cost=winner_entry + loser_fill,
            pnl=pnl,
            correct_direction=(resolution == winner_side),
            spike_magnitude=spike_mag,
            obi_available=obi_available,
            obi_confirmed=obi_confirmed,
            expected_drop=expected_drop,
        ))

        # Exit position
        in_position = False
        last_hedge_ts = hedge_fill_ts

    return trades


def run_backtest(spikes_df: pd.DataFrame, obs_df: pd.DataFrame,
                 res_map: Dict[str, str], config: ConfigSettings) -> List[TradeResult]:
    """Run backtest on all markets for a given config."""
    all_trades = []
    slugs = obs_df['market_slug'].unique()

    for slug in tqdm(slugs, desc=config.name[:20]):
        resolution = res_map.get(slug)
        if resolution not in ['UP', 'DOWN']:
            continue

        trades = simulate_market(spikes_df, obs_df, slug, resolution, config)
        all_trades.extend(trades)

    return all_trades


def analyze_results(trades: List[TradeResult], config_name: str,
                   hours: float) -> ConfigResult:
    """Analyze and return backtest results."""
    if not trades:
        return ConfigResult(
            config_name=config_name,
            trades=0,
            total_pnl=0.0,
            hourly_rate=0.0,
            sharpe_ratio=0.0,
            passive_fill_pct=0.0,
            avg_pair_cost=0.0,
            direction_acc=0.0,
        )

    total_pnl = sum(t.pnl for t in trades)
    hourly_rate = total_pnl / hours if hours > 0 else 0

    correct = sum(1 for t in trades if t.correct_direction)
    direction_acc = correct / len(trades) * 100

    passive = [t for t in trades if t.hedge_type == 'passive']
    passive_pct = len(passive) / len(trades) * 100 if trades else 0

    hedged = [t for t in trades if t.hedge_type in ['passive', 'time_stop']]
    avg_pair_cost = np.mean([t.pair_cost for t in hedged]) if hedged else 0

    # Compute Sharpe ratio (per-trade PnL)
    pnls = [t.pnl for t in trades]
    if len(pnls) > 1:
        mean_pnl = np.mean(pnls)
        std_pnl = np.std(pnls, ddof=1)
        sharpe = (mean_pnl / std_pnl) * np.sqrt(len(trades) / hours) if std_pnl > 0 else 0
    else:
        sharpe = 0.0

    return ConfigResult(
        config_name=config_name,
        trades=len(trades),
        total_pnl=total_pnl,
        hourly_rate=hourly_rate,
        sharpe_ratio=sharpe,
        passive_fill_pct=passive_pct,
        avg_pair_cost=avg_pair_cost,
        direction_acc=direction_acc,
        trades_list=trades,
    )


def print_config_details(trades: List[TradeResult], config_name: str, hours: float):
    """Print detailed breakdown for a config."""
    if not trades:
        print(f"\n{config_name}: No trades")
        return

    total_pnl = sum(t.pnl for t in trades)
    hourly_rate = total_pnl / hours if hours > 0 else 0

    correct = sum(1 for t in trades if t.correct_direction)
    direction_acc = correct / len(trades) * 100

    passive = [t for t in trades if t.hedge_type == 'passive']
    time_stop = [t for t in trades if t.hedge_type == 'time_stop']
    resolution = [t for t in trades if t.hedge_type == 'resolution']

    hedged = [t for t in trades if t.hedge_type in ['passive', 'time_stop']]
    avg_pair_cost = np.mean([t.pair_cost for t in hedged]) if hedged else 0

    # Average expected drop
    avg_expected_drop = np.mean([t.expected_drop for t in trades]) if trades else 0

    print(f"\n--- {config_name} ---")
    print(f"Total trades: {len(trades)}")
    print(f"Direction accuracy: {direction_acc:.1f}%")
    print(f"Total PnL: ${total_pnl:.2f}")
    print(f"Hourly rate: ${hourly_rate:.2f}/hr")
    print(f"\nHedge breakdown:")
    print(f"  Passive: {len(passive)} ({100*len(passive)/len(trades):.1f}%)")
    print(f"  Time-stop: {len(time_stop)} ({100*len(time_stop)/len(trades):.1f}%)")
    print(f"  Resolution: {len(resolution)} ({100*len(resolution)/len(trades):.1f}%)")
    print(f"  Avg pair cost: ${avg_pair_cost:.4f}")
    print(f"  Avg expected_drop: ${avg_expected_drop:.4f}")


def main():
    print("=" * 70)
    print("3-WAY CONFIG COMPARISON - OOS7 DATA")
    print("Testing /100 Bug (Fixed Loser Offset) Hypothesis")
    print("=" * 70)

    # Load OU params for OLD config
    load_ou_params()

    # Load data
    btc_df, obs_df, res_map, hours = load_oos7_data()

    # Precompute spikes ONCE with OU adaptive threshold (per TRADING_CONFIGS.py)
    # ALL configs now use OU adaptive - fixed 0.02 was WRONG
    print("\n" + "=" * 60)
    print("PRECOMPUTING SPIKES (OU ADAPTIVE - per TRADING_CONFIGS.py)")
    print("=" * 60)
    spikes_ou = precompute_spikes_ou(btc_df)

    # Run all configs
    results = []
    for config in ALL_CONFIGS:
        print(f"\n{'='*60}")
        print(f"Running: {config.name}")
        print(f"  min_time={config.min_time}, cycle_gap={config.min_cycle_gap_ms}ms")
        print(f"  enhanced_score={config.use_enhanced_score}, obi_filter={config.use_obi_filter}")
        print(f"  ou_threshold={config.use_ou_threshold}, fixed_offset={config.use_fixed_loser_offset}")
        print(f"{'='*60}")

        trades = run_backtest(spikes_ou, obs_df, res_map, config)
        print_config_details(trades, config.name, hours)

        result = analyze_results(trades, config.name, hours)
        results.append(result)

    # =================================================================
    # COMPARISON SUMMARY
    # =================================================================
    print("\n" + "=" * 80)
    print("=== 3-WAY CONFIG COMPARISON - OOS7 ===")
    print("=" * 80)

    for r in results:
        config = [c for c in ALL_CONFIGS if c.name == r.config_name][0]
        print(f"\n{r.config_name}:")
        if config == OLD_CONFIG:
            print(f"  MIN_TIME={config.min_time}, MIN_CYCLE_GAP_MS={config.min_cycle_gap_ms}, 5Hz hedge check")
            print(f"  OU adaptive threshold, EWMA z-score, basic velocity filter")
        elif config == CURRENT_CONFIG:
            print(f"  MIN_TIME={config.min_time}, MIN_CYCLE_GAP_MS={config.min_cycle_gap_ms}, 60Hz hedge check")
            print(f"  Fixed threshold, enhanced score filter, OBI filter")
        else:  # BUG_CONFIG
            print(f"  Same as CURRENT but loser_bid uses fixed ~0.08 offset")
            print(f"  expected_drop = DROP_MULTIPLIER * spike_mag / 100 + DROP_INTERCEPT")

        print(f"  Trades: {r.trades}")
        print(f"  Total PnL: ${r.total_pnl:.2f}")
        print(f"  Hourly rate: ${r.hourly_rate:.2f}/hr")
        print(f"  Sharpe ratio: {r.sharpe_ratio:.2f}")
        print(f"  Passive fill %: {r.passive_fill_pct:.1f}%")
        print(f"  Avg pair cost: ${r.avg_pair_cost:.4f}")

    # Table summary
    print("\n" + "=" * 80)
    print("COMPARISON SUMMARY")
    print("=" * 80)
    print(f"\n{'Config':<25} {'$/hr':>10} {'Sharpe':>8} {'Passive%':>10} {'Pair Cost':>12}")
    print("-" * 70)

    for r in results:
        print(f"{r.config_name:<25} ${r.hourly_rate:>8.2f} {r.sharpe_ratio:>8.2f} "
              f"{r.passive_fill_pct:>9.1f}% ${r.avg_pair_cost:>10.4f}")

    # Find winner
    best = max(results, key=lambda r: r.hourly_rate)
    print(f"\nWINNER: {best.config_name} at ${best.hourly_rate:.2f}/hr")

    # /100 bug analysis
    print("\n" + "=" * 80)
    print("/100 BUG HYPOTHESIS ANALYSIS")
    print("=" * 80)

    current = [r for r in results if "CURRENT" in r.config_name][0]
    bug = [r for r in results if "/100" in r.config_name][0]

    delta_hr = bug.hourly_rate - current.hourly_rate
    delta_passive = bug.passive_fill_pct - current.passive_fill_pct
    delta_sharpe = bug.sharpe_ratio - current.sharpe_ratio

    print(f"\nCURRENT (dynamic offset) vs /100 BUG (fixed offset):")
    print(f"  Hourly rate delta: ${delta_hr:+.2f}/hr")
    print(f"  Passive fill delta: {delta_passive:+.1f}pp")
    print(f"  Sharpe delta: {delta_sharpe:+.2f}")

    if bug.hourly_rate > current.hourly_rate:
        print(f"\n✓ /100 BUG (fixed offset) OUTPERFORMS dynamic offset!")
        print(f"  Consider simplifying loser_bid to fixed DROP_INTERCEPT={DROP_INTERCEPT}")
    else:
        print(f"\n✗ Dynamic offset is BETTER - keep current approach")

    # Save detailed results
    output_path = Path("research/backtests/fixed_offset_comparison_results.csv")
    rows = []
    for r in results:
        for t in r.trades_list:
            rows.append({
                'config': r.config_name,
                'market_slug': t.market_slug,
                'cycle_num': t.cycle_num,
                'entry_time_remaining': t.entry_time_remaining,
                'winner_side': t.winner_side,
                'winner_fill_price': t.winner_fill_price,
                'loser_fill_price': t.loser_fill_price,
                'hedge_type': t.hedge_type,
                'pair_cost': t.pair_cost,
                'pnl': t.pnl,
                'correct_direction': t.correct_direction,
                'spike_magnitude': t.spike_magnitude,
                'expected_drop': t.expected_drop,
            })

    if rows:
        pd.DataFrame(rows).to_csv(output_path, index=False)
        print(f"\nDetailed results saved to: {output_path}")

    print("\n" + "=" * 80)
    print("TEST COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    main()
