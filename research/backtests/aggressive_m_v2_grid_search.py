#!/usr/bin/env python3
"""
AGGRESSIVE_M V2 Grid Search - FADE the Spike

Adapted from fixed_cycling_grid_backtest.py for the FADE strategy.

Key Differences from Taker Strategy:
1. FADE: Buy expensive_side (opposite of spike), not spike_side
2. OBI_FOLLOW: OBI > 0 on spike_side (market confirms spike = better FADE signal)
3. MAKER entry: 0% fee (vs 2% taker)
4. Hold to resolution: Win $1 - entry or lose entry
5. 10s cooldown deduplication per (market, direction)

Grid Parameters:
- min_expensive_ask: [0.65, 0.70, 0.75, 0.80]
- obi_filter: [NO_OBI, OBI_FOLLOW]
- time_stop_seconds: [None, 30, 60, 120]
- shares_per_trade: [5, 10, 25, 50]

Usage:
    python research/backtests/aggressive_m_v2_grid_search.py --data is_oos2
    python research/backtests/aggressive_m_v2_grid_search.py --data oos7
    python research/backtests/aggressive_m_v2_grid_search.py --data all
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
import sys
import math
import argparse
from datetime import datetime
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.core import (
    velocity_confirms_spike,
    compute_enhanced_score,
    ENHANCED_SCORE_THRESHOLD,
)

from research.backtests.aggressive_main_backtest import (
    precompute_spikes_ewma,
    load_ou_params,
    MIN_TIME,
    HIGH_ENTRY_THRESHOLD,
)

# =============================================================================
# CONSTANTS
# =============================================================================

BASE_DIR = Path("/Users/rananjaybika/polymarket-amm-bot")

# AGGRESSIVE_M V2 defaults
DEFAULT_MIN_EXPENSIVE_ASK = 0.70
DEFAULT_COOLDOWN_SECONDS = 10
DEFAULT_SHARES = 5

# Spike detection
EWMA_HALFLIFE_MS = 1000

# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class TradeResult:
    """Result of a single FADE trade."""
    config_name: str
    market_slug: str
    spike_ts: int
    spike_direction: str
    resolution: str
    entry_price: float  # expensive_ask (our entry)
    time_remaining: float
    exit_type: str  # "resolution", "time_stop"
    exit_price: float  # $1.00 if win, $0.00 if lose, or stop price
    pnl: float
    fade_correct: bool
    obi_spike: Optional[float]


@dataclass
class ConfigResult:
    """Result for a single config."""
    config_name: str
    trades: int
    total_pnl: float
    hourly_rate: float
    fade_accuracy: float
    avg_entry: float
    ev_per_trade: float
    exit_breakdown: Dict[str, int] = field(default_factory=dict)


@dataclass
class GridConfig:
    """Configuration for grid search."""
    name: str

    # AGGRESSIVE_M V2 filters
    min_expensive_ask: float = 0.70
    obi_filter: str = "OBI_FOLLOW"  # "NO_OBI" or "OBI_FOLLOW"
    cooldown_seconds: int = 10

    # Position sizing
    shares_per_trade: int = 5

    # Exit
    time_stop_seconds: Optional[float] = None  # None = hold to resolution


# =============================================================================
# DATASETS
# =============================================================================

DATASETS = {
    "IS+OOS2": {
        "name": "IS+OOS2 (Jan 16-19)",
        "btc_file": "research/binance_hf/btc_prices_20260118_060340.csv",
        "obs_files": [
            "research/observer/grid_obs_20260116.csv",
            "research/observer/grid_obs_20260117.csv",
            "research/observer/grid_obs_20260118.csv",
            "research/observer/grid_obs_20260119.csv",
        ],
    },
    "OOS7": {
        "name": "OOS7 (Jan 29-30)",
        "btc_file": "research/binance_hf/btc_prices_20260129_160523.csv",
        "obs_files": [
            "research/observer/grid_obs_20260129.csv",
            "research/observer/grid_obs_20260130.csv",
        ],
    },
}


# =============================================================================
# DATA LOADING
# =============================================================================

def load_dataset(dataset_key: str) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame], Dict[str, str], float]:
    """Load a dataset and return (btc_df, obs_df, res_map, hours)."""
    config = DATASETS[dataset_key]

    print(f"\n{'='*60}")
    print(f"Loading {config['name']}")
    print(f"{'='*60}")

    # Load observer data
    obs_dfs = []
    for fname in config['obs_files']:
        fpath = BASE_DIR / fname
        if fpath.exists():
            df = pd.read_csv(fpath, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)
            print(f"  {fpath.name}: {len(df):,} rows")

    if not obs_dfs:
        return None, None, {}, 0

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    obs_df = obs_df.sort_values('timestamp_ms').reset_index(drop=True)
    print(f"  Combined observer: {len(obs_df):,} rows")

    # Load BTC data
    btc_path = BASE_DIR / config['btc_file']
    if not btc_path.exists():
        print(f"  BTC file not found: {btc_path}")
        return None, None, {}, 0

    btc_df = pd.read_csv(btc_path)
    print(f"  Binance HF: {len(btc_df):,} rows")

    # Load resolutions
    res_path = BASE_DIR / "research/observer/market_resolutions_verified.csv"
    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))
    print(f"  Resolutions: {len(res_map)} markets")

    # Calculate hours
    start_ts = obs_df['timestamp_ms'].min()
    end_ts = obs_df['timestamp_ms'].max()
    hours = (end_ts - start_ts) / 3600000
    print(f"  Duration: {hours:.2f} hours")

    return btc_df, obs_df, res_map, hours


# =============================================================================
# SIMULATION
# =============================================================================

def simulate_market(
    btc_spikes: pd.DataFrame,
    obs_df: pd.DataFrame,
    slug: str,
    resolution: str,
    config: GridConfig,
) -> List[TradeResult]:
    """
    Simulate FADE trading in a single market.

    FADE Strategy:
    1. Detect spike with AGGRESSIVE filters
    2. Check OBI filter (if enabled)
    3. Check expensive_ask >= threshold
    4. BUY expensive_side (opposite of spike)
    5. Hold to resolution or time-stop
    """
    mdf = obs_df[obs_df['market_slug'] == slug].copy()
    mdf = mdf.sort_values('timestamp_ms').reset_index(drop=True)

    if len(mdf) == 0:
        return []

    market_start = mdf['timestamp_ms'].min()
    market_end = mdf['timestamp_ms'].max()

    # Get spikes within market time
    market_spikes = btc_spikes[
        (btc_spikes['timestamp_ms'] >= market_start) &
        (btc_spikes['timestamp_ms'] <= market_end) &
        (btc_spikes['spike_detected'] == True)
    ].copy()

    if len(market_spikes) == 0:
        return []

    trades = []
    obs_idx = 0
    cooldown_ms = config.cooldown_seconds * 1000

    # Deduplication: track last signal per direction
    last_signal_ts = {'UP': 0, 'DOWN': 0}

    for _, spike_row in market_spikes.iterrows():
        spike_ts = spike_row['timestamp_ms']
        spike_dir = spike_row['spike_direction']
        spike_mag = spike_row['spike_magnitude']

        # Deduplication check
        if spike_ts - last_signal_ts[spike_dir] < cooldown_ms:
            continue

        # Find nearest observer row
        while obs_idx < len(mdf) - 1 and mdf.iloc[obs_idx + 1]['timestamp_ms'] <= spike_ts:
            obs_idx += 1

        if obs_idx >= len(mdf):
            break

        obs_row = mdf.iloc[obs_idx]
        time_rem = obs_row['time_remaining_secs']
        velocity_bps = obs_row.get('velocity_bps', 0.0) or 0.0

        # ===== FILTER 1: Time remaining =====
        if time_rem < MIN_TIME:
            continue

        # ===== FILTER 2: Velocity confirmation =====
        if not velocity_confirms_spike(spike_dir, velocity_bps):
            continue

        # ===== FILTER 3: Enhanced score =====
        score = compute_enhanced_score(spike_mag, velocity_bps, spike_dir, time_rem)
        if score < ENHANCED_SCORE_THRESHOLD:
            continue

        # ===== Get prices =====
        if spike_dir == "UP":
            spike_ask = obs_row['up_ask']
            expensive_ask = obs_row['down_ask']
            obi_spike = obs_row.get('up_imbalance', None)
        else:
            spike_ask = obs_row['down_ask']
            expensive_ask = obs_row['up_ask']
            obi_spike = obs_row.get('down_imbalance', None)

        # ===== FILTER 4: High entry threshold (spike side) =====
        if pd.isna(spike_ask) or spike_ask >= HIGH_ENTRY_THRESHOLD:
            continue

        if pd.isna(expensive_ask):
            continue

        # ===== FILTER 5: OBI filter =====
        if config.obi_filter == "OBI_FOLLOW":
            if obi_spike is None or np.isnan(obi_spike) or obi_spike <= 0:
                continue

        # ===== FILTER 6: Expensive side threshold =====
        if expensive_ask < config.min_expensive_ask:
            continue

        # ===== SIGNAL PASSED - Execute FADE =====
        last_signal_ts[spike_dir] = spike_ts

        entry_price = expensive_ask
        fade_correct = (spike_dir != resolution)

        # Determine exit
        exit_type = "resolution"
        exit_price = 1.0 if fade_correct else 0.0

        # Check for time-stop
        if config.time_stop_seconds is not None:
            stop_ts = spike_ts + (config.time_stop_seconds * 1000)

            # Find observer row at stop time
            for j in range(obs_idx + 1, len(mdf)):
                if mdf.iloc[j]['timestamp_ms'] >= stop_ts:
                    stop_row = mdf.iloc[j]
                    # Exit at current expensive_side bid (what we can sell at)
                    if spike_dir == "UP":
                        exit_bid = stop_row.get('down_bid', stop_row['down_ask'] - 0.02)
                    else:
                        exit_bid = stop_row.get('up_bid', stop_row['up_ask'] - 0.02)

                    if pd.isna(exit_bid):
                        exit_bid = expensive_ask - 0.05  # Assume 5 cent slippage

                    exit_type = "time_stop"
                    exit_price = max(0, exit_bid)
                    break

        # Calculate PnL
        if exit_type == "resolution":
            if fade_correct:
                pnl = (1.0 - entry_price) * config.shares_per_trade
            else:
                pnl = -entry_price * config.shares_per_trade
        else:  # time_stop
            pnl = (exit_price - entry_price) * config.shares_per_trade

        trades.append(TradeResult(
            config_name=config.name,
            market_slug=slug,
            spike_ts=spike_ts,
            spike_direction=spike_dir,
            resolution=resolution,
            entry_price=entry_price,
            time_remaining=time_rem,
            exit_type=exit_type,
            exit_price=exit_price,
            pnl=pnl,
            fade_correct=fade_correct,
            obi_spike=obi_spike if obi_spike is not None and not np.isnan(obi_spike) else None,
        ))

    return trades


def run_config(
    btc_spikes: pd.DataFrame,
    obs_df: pd.DataFrame,
    res_map: Dict[str, str],
    config: GridConfig,
    hours: float,
) -> ConfigResult:
    """Run backtest for a single config."""
    all_trades = []

    markets = obs_df['market_slug'].unique()

    for slug in markets:
        resolution = res_map.get(slug)
        if resolution not in ['UP', 'DOWN']:
            continue

        trades = simulate_market(btc_spikes, obs_df, slug, resolution, config)
        all_trades.extend(trades)

    if not all_trades:
        return ConfigResult(
            config_name=config.name,
            trades=0,
            total_pnl=0,
            hourly_rate=0,
            fade_accuracy=0,
            avg_entry=0,
            ev_per_trade=0,
        )

    # Calculate metrics
    total_pnl = sum(t.pnl for t in all_trades)
    total_trades = len(all_trades)
    correct = sum(1 for t in all_trades if t.fade_correct)
    fade_accuracy = correct / total_trades
    avg_entry = np.mean([t.entry_price for t in all_trades])
    hourly_rate = total_pnl / hours if hours > 0 else 0
    ev_per_trade = total_pnl / total_trades if total_trades > 0 else 0

    # Exit breakdown
    exit_breakdown = {}
    for t in all_trades:
        exit_breakdown[t.exit_type] = exit_breakdown.get(t.exit_type, 0) + 1

    return ConfigResult(
        config_name=config.name,
        trades=total_trades,
        total_pnl=total_pnl,
        hourly_rate=hourly_rate,
        fade_accuracy=fade_accuracy,
        avg_entry=avg_entry,
        ev_per_trade=ev_per_trade,
        exit_breakdown=exit_breakdown,
    )


# =============================================================================
# CONFIG GENERATION
# =============================================================================

def generate_grid_configs() -> List[GridConfig]:
    """Generate all grid search configurations."""
    configs = []

    # Grid parameters
    min_expensive_asks = [0.65, 0.70, 0.75, 0.80]
    obi_filters = ["NO_OBI", "OBI_FOLLOW"]
    time_stops = [None, 30, 60, 120]
    shares = [5, 10, 25, 50]

    for min_exp in min_expensive_asks:
        for obi in obi_filters:
            for time_stop in time_stops:
                for share in shares:
                    time_label = f"TS{time_stop}s" if time_stop else "HOLD"
                    name = f"EXP{int(min_exp*100)}_{obi}_{time_label}_SH{share}"

                    configs.append(GridConfig(
                        name=name,
                        min_expensive_ask=min_exp,
                        obi_filter=obi,
                        cooldown_seconds=10,
                        shares_per_trade=share,
                        time_stop_seconds=time_stop,
                    ))

    return configs


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="AGGRESSIVE_M V2 Grid Search")
    parser.add_argument("--data", choices=["IS+OOS2", "OOS7", "all"],
                        default="all", help="Dataset to use")
    parser.add_argument("--output", type=str,
                        default="research/findings/data/aggressive_m_v2_grid_results.csv",
                        help="Output CSV path")
    args = parser.parse_args()

    print("=" * 80)
    print("AGGRESSIVE_M V2 GRID SEARCH")
    print("=" * 80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # Load OU parameters
    load_ou_params()

    # Generate configs
    configs = generate_grid_configs()
    print(f"\nTotal configs: {len(configs)}")

    # Determine datasets
    if args.data == "all":
        datasets = list(DATASETS.keys())
    else:
        datasets = [args.data]

    all_results = []

    for dataset_key in datasets:
        btc_df, obs_df, res_map, hours = load_dataset(dataset_key)

        if btc_df is None:
            print(f"Skipping {dataset_key} - data not available")
            continue

        # Compute spikes
        print(f"\n  Computing EWMA spikes...")
        btc_spikes = precompute_spikes_ewma(btc_df, EWMA_HALFLIFE_MS)

        # Run configs
        print(f"\n  Running {len(configs)} configs...")

        for config in tqdm(configs, desc=f"  {dataset_key}"):
            result = run_config(btc_spikes, obs_df, res_map, config, hours)

            result_dict = {
                'dataset': dataset_key,
                'config_name': result.config_name,
                'min_expensive_ask': config.min_expensive_ask,
                'obi_filter': config.obi_filter,
                'time_stop': config.time_stop_seconds,
                'shares': config.shares_per_trade,
                'trades': result.trades,
                'total_pnl': result.total_pnl,
                'hourly_rate': result.hourly_rate,
                'fade_accuracy': result.fade_accuracy,
                'avg_entry': result.avg_entry,
                'ev_per_trade': result.ev_per_trade,
                'exit_resolution': result.exit_breakdown.get('resolution', 0),
                'exit_time_stop': result.exit_breakdown.get('time_stop', 0),
            }
            all_results.append(result_dict)

    # Save results
    results_df = pd.DataFrame(all_results)
    results_df = results_df.sort_values(['dataset', 'hourly_rate'], ascending=[True, False])

    output_path = BASE_DIR / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    results_df.to_csv(output_path, index=False)
    print(f"\nSaved: {output_path}")

    # Summary
    print(f"\n{'='*80}")
    print("TOP 10 BY HOURLY RATE (per dataset)")
    print(f"{'='*80}")

    for dataset in datasets:
        print(f"\n{dataset}:")
        print("-" * 75)
        ds_df = results_df[results_df['dataset'] == dataset]
        top10 = ds_df.nlargest(10, 'hourly_rate')

        print(f"{'Config':<35} {'Trades':>7} {'$/hr':>8} {'Acc':>7} {'$/trade':>8}")
        print("-" * 75)

        for _, row in top10.iterrows():
            print(f"{row['config_name']:<35} {row['trades']:>7} "
                  f"${row['hourly_rate']:>6.2f} {row['fade_accuracy']:>6.1%} "
                  f"${row['ev_per_trade']:>6.2f}")

    # Best overall
    print(f"\n{'='*80}")
    print("BEST CONFIG BY DATASET")
    print(f"{'='*80}")

    for dataset in datasets:
        ds_df = results_df[results_df['dataset'] == dataset]
        if len(ds_df) > 0:
            best = ds_df.loc[ds_df['hourly_rate'].idxmax()]
            print(f"\n{dataset}:")
            print(f"  Config: {best['config_name']}")
            print(f"  Trades: {best['trades']}")
            print(f"  $/hr: ${best['hourly_rate']:.2f}")
            print(f"  Accuracy: {best['fade_accuracy']:.1%}")
            print(f"  Avg entry: ${best['avg_entry']:.3f}")
            print(f"  EV/trade: ${best['ev_per_trade']:.2f}")

    print(f"\nCompleted: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
