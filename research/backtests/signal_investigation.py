#!/usr/bin/env python3
"""
Deep Signal Investigation — EMA Span Sweep + OBI/Velocity Analysis

=============================================================================
COPIED FROM: directional_maker_v2_backtest.py (DATASETS, load_dataset)
PURPOSE: Pure signal accuracy analysis. No execution engine.
=============================================================================

Sweeps EMA short/long spans, OBI contrarian modes, velocity filters,
and confidence thresholds across all OBI-enabled datasets (OOS7-10).

Outputs:
  - Per-config signal accuracy across all datasets
  - Per-dataset accuracy breakdown
  - Rolling accuracy within datasets
  - OBI contribution analysis
  - Regime analysis (trending vs ranging)
  - Trade count vs accuracy tradeoff

Usage:
    python research/backtests/signal_investigation.py
    python research/backtests/signal_investigation.py --data OOS7
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
import sys
import argparse
from datetime import datetime
from tqdm import tqdm
from itertools import product

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ═══════════════════════════════════════════════════════════════
# SECTION: DATASETS — COPY VERBATIM from V2
# ═══════════════════════════════════════════════════════════════
DATASETS = {
    "OOS7": {
        "name": "OOS7 (Jan 29-30)",
        "btc_file": "research/binance_hf/btc_prices_20260129_160523.csv",
        "obs_files": [
            "research/observer/grid_obs_20260129.csv",
            "research/observer/grid_obs_20260130.csv",
        ],
        "res_files": [
            "research/observer/resolutions_20260129.csv",
            "research/observer/resolutions_20260130.csv",
        ],
    },
    "OOS8": {
        "name": "OOS8 (Jan 31)",
        "btc_file": "research/binance_hf/btc_prices_20260131_055231.csv",
        "obs_files": [
            "research/observer/grid_obs_20260131.csv",
        ],
        "res_files": ["research/observer/resolutions_20260131.csv"],
    },
    "OOS9": {
        "name": "OOS9 (Feb 1-3)",
        "btc_file": "research/binance_hf/btc_prices_oos9.csv",
        "obs_files": [
            "research/observer/grid_obs_oos9.csv",
        ],
        "res_files": [
            "research/observer/resolutions_oos9_1.csv",
            "research/observer/resolutions_oos9_2.csv",
        ],
    },
    "OOS10": {
        "name": "OOS10 (Feb 5)",
        "btc_file": "research/binance_hf/btc_prices_20260204_190733.csv",
        "obs_files": [
            "research/observer/grid_obs_20260205.csv",
        ],
        "res_files": ["research/observer/resolutions_20260205.csv"],
    },
}


# ═══════════════════════════════════════════════════════════════
# SECTION: load_dataset() — COPY VERBATIM from V2
# ═══════════════════════════════════════════════════════════════
def load_dataset(dataset_key: str):
    """Load observer + resolution data for a dataset."""
    config = DATASETS[dataset_key]
    base_dir = Path("/Users/rananjaybika/polymarket-amm-bot")

    print(f"\n{'='*60}")
    print(f"Loading {config['name']}")
    print(f"{'='*60}")

    obs_dfs = []
    for fname in config['obs_files']:
        fpath = base_dir / fname
        if fpath.exists():
            df = pd.read_csv(fpath, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)
            print(f"  {fpath.name}: {len(df):,} rows")

    if not obs_dfs:
        return None, {}, 0

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    print(f"  Combined observer: {len(obs_df):,} rows")

    resolutions = {}
    for res_fname in config.get('res_files', []):
        res_path = base_dir / res_fname
        if res_path.exists():
            res_df = pd.read_csv(res_path)
            if 'slug' in res_df.columns and 'winner' in res_df.columns:
                for _, row in res_df.iterrows():
                    resolutions[row['slug']] = row['winner']
            elif 'market_slug' in res_df.columns and 'resolution' in res_df.columns:
                for _, row in res_df.iterrows():
                    resolutions[row['market_slug']] = row['resolution']
            print(f"  {Path(res_fname).name}: {len(res_df)} resolutions")
    print(f"  Total resolutions: {len(resolutions)} markets")

    duration_hours = (obs_df['timestamp_ms'].max() - obs_df['timestamp_ms'].min()) / (1000 * 3600)
    print(f"  Duration: {duration_hours:.2f} hours")

    return obs_df, resolutions, duration_hours


# ═══════════════════════════════════════════════════════════════
# SECTION: Signal computation functions
# ═══════════════════════════════════════════════════════════════
def compute_ema_signals(obs_df: pd.DataFrame, short_span: int, long_span: int) -> pd.DataFrame:
    """Compute EMA trend + velocity for a given span pair."""
    btc_cols = ['timestamp_ms', 'binance_price']
    btc_ts = obs_df[btc_cols].drop_duplicates('timestamp_ms').sort_values('timestamp_ms').copy()
    btc_ts = btc_ts.reset_index(drop=True)
    btc_ts['binance_price'] = pd.to_numeric(btc_ts['binance_price'], errors='coerce')
    btc_ts = btc_ts.dropna(subset=['binance_price']).reset_index(drop=True)

    btc_ts['ema_short'] = btc_ts['binance_price'].ewm(span=short_span, adjust=False).mean()
    btc_ts['ema_long'] = btc_ts['binance_price'].ewm(span=long_span, adjust=False).mean()
    btc_ts['btc_trend'] = np.where(btc_ts['ema_short'] > btc_ts['ema_long'], 1, -1)

    # EMA gap (distance between EMAs in bps — regime indicator)
    btc_ts['ema_gap_bps'] = (btc_ts['ema_short'] - btc_ts['ema_long']) / btc_ts['binance_price'] * 10000

    # Velocity: bps/sec over 30-tick window
    btc_ts['velocity_bps'] = btc_ts['binance_price'].pct_change(periods=30) * 10000 / 30
    btc_ts['velocity_bps'] = btc_ts['velocity_bps'].fillna(0.0)

    # BTC volatility: rolling std in bps over 300-tick window
    btc_ts['btc_vol_bps'] = btc_ts['binance_price'].pct_change().rolling(300).std() * 10000
    btc_ts['btc_vol_bps'] = btc_ts['btc_vol_bps'].fillna(0.0)

    return btc_ts


def evaluate_signal_per_market(
    obs_df: pd.DataFrame,
    resolutions: Dict[str, str],
    markets: List[str],
    btc_ts: pd.DataFrame,
    entry_window_start: float = 800.0,
    entry_window_end: float = 300.0,
    require_obi_contrarian: bool = True,
    require_velocity: bool = False,
    min_confidence: int = 2,
) -> List[Dict]:
    """
    Evaluate signal accuracy for each market.

    Returns list of dicts with per-market signal info.
    """
    btc_timestamps = btc_ts['timestamp_ms'].values
    btc_trends = btc_ts['btc_trend'].values
    btc_velocities = btc_ts['velocity_bps'].values
    btc_ema_gaps = btc_ts['ema_gap_bps'].values
    btc_vols = btc_ts['btc_vol_bps'].values

    has_obi = 'up_imbalance' in obs_df.columns and 'down_imbalance' in obs_df.columns

    results = []
    for market_slug in markets:
        resolution = resolutions[market_slug]
        mdf = obs_df[obs_df['market_slug'] == market_slug]

        if len(mdf) == 0:
            continue

        # Entry window
        entry_mask = (
            (mdf['time_remaining_secs'] >= entry_window_end) &
            (mdf['time_remaining_secs'] <= entry_window_start)
        )
        entry_rows = mdf[entry_mask]
        if len(entry_rows) == 0:
            continue
        entry_row = entry_rows.iloc[0]
        entry_ts = int(entry_row['timestamp_ms'])

        # Layer 1: EMA
        nearest_idx = np.searchsorted(btc_timestamps, entry_ts)
        nearest_idx = min(nearest_idx, len(btc_trends) - 1)
        btc_trend = btc_trends[nearest_idx]
        ema_prediction = "UP" if btc_trend == 1 else "DOWN"

        # Regime info
        ema_gap = btc_ema_gaps[nearest_idx]
        btc_vol = btc_vols[nearest_idx]

        # Layer 2: OBI contrarian
        obi_contrarian = False
        obi_available = False
        net_obi = 0.0
        if has_obi:
            up_imb = entry_row.get('up_imbalance')
            down_imb = entry_row.get('down_imbalance')
            if not pd.isna(up_imb) and not pd.isna(down_imb):
                obi_available = True
                net_obi = float(up_imb) - float(down_imb)
                obi_direction = 1 if net_obi > 0 else -1
                obi_contrarian = (obi_direction != btc_trend)

        # Layer 3: Velocity
        velocity = btc_velocities[nearest_idx]
        velocity_confirms = (
            (btc_trend == 1 and velocity > 0) or
            (btc_trend == -1 and velocity < 0)
        )

        # Confidence scoring
        layers = 1  # EMA base
        if obi_contrarian:
            layers += 1
        if velocity_confirms:
            layers += 1

        # Apply filters
        if require_obi_contrarian and not obi_available:
            continue
        if min_confidence > layers:
            continue  # Skip low-confidence

        # Final prediction = EMA direction (with optional filters)
        predicted_side = ema_prediction
        signal_correct = (predicted_side == resolution)

        # Also compute gabagool prediction for comparison
        up_ask_val = entry_row.get('up_ask')
        down_ask_val = entry_row.get('down_ask')
        gabagool_pred = None
        gabagool_correct = None
        if not pd.isna(up_ask_val) and not pd.isna(down_ask_val):
            gabagool_pred = "UP" if float(up_ask_val) >= float(down_ask_val) else "DOWN"
            gabagool_correct = (gabagool_pred == resolution)

        results.append({
            'market_slug': market_slug,
            'dataset': '',  # filled by caller
            'entry_ts': entry_ts,
            'resolution': resolution,
            'predicted_side': predicted_side,
            'signal_correct': signal_correct,
            'confidence_layers': layers,
            'obi_contrarian': obi_contrarian,
            'obi_available': obi_available,
            'velocity_confirms': velocity_confirms,
            'net_obi': round(net_obi, 4),
            'ema_gap_bps': round(ema_gap, 2),
            'btc_vol_bps': round(btc_vol, 2),
            'velocity_bps': round(velocity, 4),
            'gabagool_pred': gabagool_pred,
            'gabagool_correct': gabagool_correct,
        })

    return results


# ═══════════════════════════════════════════════════════════════
# SECTION: Grid search configs
# ═══════════════════════════════════════════════════════════════
@dataclass
class SignalConfig:
    name: str
    ema_short: int
    ema_long: int
    require_obi: bool = True
    require_velocity: bool = False
    min_confidence: int = 2  # 1=EMA only, 2=EMA+1, 3=all three


def generate_signal_configs() -> List[SignalConfig]:
    """Generate signal sweep configurations."""
    configs = []

    ema_shorts = [50, 100, 200, 300, 500]
    ema_longs = [300, 600, 900, 1200, 1800, 3600]
    obi_modes = [True, False]  # require OBI contrarian
    min_confs = [1, 2]  # 1 = EMA only (no filter), 2 = require 1 confirmer

    for short, long in product(ema_shorts, ema_longs):
        if short >= long:
            continue

        for req_obi in obi_modes:
            for min_conf in min_confs:
                # Skip contradictory: require OBI=False but min_conf=2 needs a confirmer
                # (velocity alone can still provide the second layer)
                tag_obi = "OBI" if req_obi else "noOBI"
                tag_conf = f"c{min_conf}"
                name = f"EMA({short},{long})_{tag_obi}_{tag_conf}"
                configs.append(SignalConfig(
                    name=name,
                    ema_short=short,
                    ema_long=long,
                    require_obi=req_obi,
                    min_confidence=min_conf,
                ))

    return configs


# ═══════════════════════════════════════════════════════════════
# SECTION: main()
# ═══════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='all', help='OOS7,OOS8,OOS9,OOS10 or "all"')
    parser.add_argument('--output', default='research/findings/data/signal_investigation_results.csv')
    args = parser.parse_args()

    print("=" * 80)
    print("DEEP SIGNAL INVESTIGATION (Feb 11, 2026)")
    print("Sweep EMA spans, OBI modes, confidence thresholds")
    print("=" * 80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    all_configs = generate_signal_configs()
    print(f"\nTotal signal configs: {len(all_configs)}")
    for c in all_configs[:5]:
        print(f"  - {c.name}")
    if len(all_configs) > 5:
        print(f"  ... and {len(all_configs) - 5} more")

    if args.data == 'all':
        datasets = list(DATASETS.keys())
    else:
        datasets = [d.strip() for d in args.data.split(',')]

    all_results = []       # Per-config-dataset summary
    all_market_results = []  # Per-market detail (for regime analysis)

    for dataset_key in datasets:
        obs_df, resolutions, duration_hours = load_dataset(dataset_key)
        if obs_df is None:
            continue

        # Check OBI
        has_obi = 'up_imbalance' in obs_df.columns and 'down_imbalance' in obs_df.columns
        if not has_obi:
            print(f"  SKIPPING {dataset_key}: no OBI columns")
            continue

        markets = obs_df['market_slug'].unique()
        markets_with_res = [m for m in markets if m in resolutions]
        print(f"  Markets with resolution: {len(markets_with_res)}")

        # Pre-compute EMA signals for each unique span pair
        ema_cache = {}
        unique_spans = set((c.ema_short, c.ema_long) for c in all_configs)
        print(f"  Computing EMAs for {len(unique_spans)} span pairs...")
        for short, long in unique_spans:
            ema_cache[(short, long)] = compute_ema_signals(obs_df, short, long)

        # Evaluate each config
        print(f"  Evaluating {len(all_configs)} configs...")
        for config in tqdm(all_configs, desc=f"  {dataset_key}"):
            btc_ts = ema_cache[(config.ema_short, config.ema_long)]

            market_results = evaluate_signal_per_market(
                obs_df, resolutions, markets_with_res, btc_ts,
                require_obi_contrarian=config.require_obi,
                require_velocity=config.require_velocity,
                min_confidence=config.min_confidence,
            )

            # Tag with dataset
            for r in market_results:
                r['dataset'] = dataset_key
                r['config_name'] = config.name
                r['ema_short'] = config.ema_short
                r['ema_long'] = config.ema_long
                r['require_obi'] = config.require_obi
                r['min_confidence'] = config.min_confidence

            all_market_results.extend(market_results)

            # Summary
            n_markets = len(market_results)
            n_correct = sum(1 for r in market_results if r['signal_correct'])
            accuracy = (n_correct / n_markets * 100) if n_markets > 0 else 0

            # Gabagool comparison
            gab_results = [r for r in market_results if r['gabagool_correct'] is not None]
            gab_correct = sum(1 for r in gab_results if r['gabagool_correct'])
            gab_accuracy = (gab_correct / len(gab_results) * 100) if gab_results else 0

            # Confidence breakdown
            high_conf = [r for r in market_results if r['confidence_layers'] == 3]
            med_conf = [r for r in market_results if r['confidence_layers'] == 2]
            high_acc = (sum(1 for r in high_conf if r['signal_correct']) / len(high_conf) * 100) if high_conf else 0
            med_acc = (sum(1 for r in med_conf if r['signal_correct']) / len(med_conf) * 100) if med_conf else 0

            # Regime: split by EMA gap magnitude
            strong_trend = [r for r in market_results if abs(r['ema_gap_bps']) > 5]
            weak_trend = [r for r in market_results if abs(r['ema_gap_bps']) <= 5]
            strong_acc = (sum(1 for r in strong_trend if r['signal_correct']) / len(strong_trend) * 100) if strong_trend else 0
            weak_acc = (sum(1 for r in weak_trend if r['signal_correct']) / len(weak_trend) * 100) if weak_trend else 0

            all_results.append({
                'config_name': config.name,
                'dataset': dataset_key,
                'ema_short': config.ema_short,
                'ema_long': config.ema_long,
                'require_obi': config.require_obi,
                'min_confidence': config.min_confidence,
                'n_markets': n_markets,
                'n_correct': n_correct,
                'accuracy_pct': round(accuracy, 1),
                'gab_accuracy_pct': round(gab_accuracy, 1),
                'n_high_conf': len(high_conf),
                'high_conf_acc': round(high_acc, 1),
                'n_med_conf': len(med_conf),
                'med_conf_acc': round(med_acc, 1),
                'n_strong_trend': len(strong_trend),
                'strong_trend_acc': round(strong_acc, 1),
                'n_weak_trend': len(weak_trend),
                'weak_trend_acc': round(weak_acc, 1),
                'duration_hours': round(duration_hours, 2),
            })

    # Save results
    results_df = pd.DataFrame(all_results)
    results_df.to_csv(args.output, index=False)
    print(f"\n{'='*60}")
    print(f"COMPLETE: {len(all_results)} results saved to {args.output}")

    # Save detailed market-level results
    detail_path = args.output.replace('.csv', '_detail.csv')
    detail_df = pd.DataFrame(all_market_results)
    detail_df.to_csv(detail_path, index=False)
    print(f"Detail: {len(all_market_results)} market results saved to {detail_path}")

    # ═══════════════════════════════════════════════════════════════
    # ANALYSIS
    # ═══════════════════════════════════════════════════════════════
    if len(results_df) == 0:
        print("No results to analyze.")
        return

    print("\n" + "=" * 80)
    print("SIGNAL INVESTIGATION RESULTS")
    print("=" * 80)

    # 1. Top 20 configs by cross-dataset accuracy (weighted by n_markets)
    print("\n" + "=" * 80)
    print("TOP 20 CONFIGS BY WEIGHTED ACCURACY (cross-dataset)")
    print("=" * 80)
    cross = results_df.groupby('config_name').agg({
        'n_markets': 'sum',
        'n_correct': 'sum',
        'accuracy_pct': 'mean',
        'n_high_conf': 'sum',
        'high_conf_acc': 'mean',
        'n_strong_trend': 'sum',
        'strong_trend_acc': 'mean',
        'n_weak_trend': 'sum',
        'weak_trend_acc': 'mean',
    }).round(1)
    cross['weighted_acc'] = (cross['n_correct'] / cross['n_markets'] * 100).round(1)
    cross = cross.sort_values('weighted_acc', ascending=False)
    print(cross.head(20).to_string())

    # 2. Best EMA span pair (aggregated across OBI modes)
    print("\n" + "=" * 80)
    print("BEST EMA SPAN PAIRS (aggregated)")
    print("=" * 80)
    span_group = results_df.groupby(['ema_short', 'ema_long']).agg({
        'n_markets': 'sum',
        'n_correct': 'sum',
        'accuracy_pct': 'mean',
    }).round(1)
    span_group['weighted_acc'] = (span_group['n_correct'] / span_group['n_markets'] * 100).round(1)
    span_group = span_group.sort_values('weighted_acc', ascending=False)
    print(span_group.head(15).to_string())

    # 3. OBI contribution
    print("\n" + "=" * 80)
    print("OBI CONTRIBUTION (same EMA, with vs without OBI filter)")
    print("=" * 80)
    obi_group = results_df.groupby('require_obi').agg({
        'n_markets': 'sum',
        'n_correct': 'sum',
        'accuracy_pct': 'mean',
    }).round(1)
    obi_group['weighted_acc'] = (obi_group['n_correct'] / obi_group['n_markets'] * 100).round(1)
    print(obi_group.to_string())

    # 4. Confidence filter impact
    print("\n" + "=" * 80)
    print("MIN CONFIDENCE IMPACT (1=EMA only, 2=require confirmer)")
    print("=" * 80)
    conf_group = results_df.groupby('min_confidence').agg({
        'n_markets': 'sum',
        'n_correct': 'sum',
        'accuracy_pct': 'mean',
    }).round(1)
    conf_group['weighted_acc'] = (conf_group['n_correct'] / conf_group['n_markets'] * 100).round(1)
    print(conf_group.to_string())

    # 5. Per-dataset accuracy for top 5 configs
    print("\n" + "=" * 80)
    print("TOP 5 CONFIGS — PER-DATASET BREAKDOWN")
    print("=" * 80)
    top5_names = cross.head(5).index.tolist()
    for name in top5_names:
        subset = results_df[results_df['config_name'] == name]
        print(f"\n  {name}:")
        for _, row in subset.iterrows():
            print(f"    {row['dataset']}: {row['accuracy_pct']:.1f}% ({row['n_markets']} markets)"
                  f" | HIGH: {row['high_conf_acc']:.1f}% ({row['n_high_conf']})"
                  f" | strong_trend: {row['strong_trend_acc']:.1f}% ({row['n_strong_trend']})"
                  f" | weak_trend: {row['weak_trend_acc']:.1f}% ({row['n_weak_trend']})")

    # 6. Regime analysis: strong trend vs weak trend
    print("\n" + "=" * 80)
    print("REGIME ANALYSIS: STRONG TREND (|ema_gap| > 5bps) vs WEAK")
    print("=" * 80)
    regime_group = results_df.groupby('config_name').agg({
        'strong_trend_acc': 'mean',
        'weak_trend_acc': 'mean',
        'n_strong_trend': 'sum',
        'n_weak_trend': 'sum',
    }).round(1)
    regime_group['trend_edge'] = (regime_group['strong_trend_acc'] - regime_group['weak_trend_acc']).round(1)
    regime_group = regime_group.sort_values('trend_edge', ascending=False)
    print("Top 10 configs with biggest trend edge:")
    print(regime_group.head(10).to_string())

    # 7. Trade count vs accuracy tradeoff
    print("\n" + "=" * 80)
    print("TRADE COUNT vs ACCURACY TRADEOFF")
    print("=" * 80)
    tradeoff = cross[['n_markets', 'weighted_acc']].copy()
    tradeoff = tradeoff.sort_values('weighted_acc', ascending=False)
    # Show configs with >100 markets AND >60% accuracy
    good = tradeoff[(tradeoff['n_markets'] >= 100) & (tradeoff['weighted_acc'] >= 60)]
    print(f"Configs with >=100 markets AND >=60% accuracy: {len(good)}")
    if len(good) > 0:
        print(good.head(20).to_string())

    # 8. Current baseline comparison
    print("\n" + "=" * 80)
    print("CURRENT BASELINE: EMA(300,1800)_OBI_c2")
    print("=" * 80)
    baseline = results_df[results_df['config_name'] == "EMA(300,1800)_OBI_c2"]
    if len(baseline) > 0:
        for _, row in baseline.iterrows():
            print(f"  {row['dataset']}: {row['accuracy_pct']:.1f}% ({row['n_markets']} markets)")
        total_m = baseline['n_markets'].sum()
        total_c = baseline['n_correct'].sum()
        print(f"  TOTAL: {total_c / total_m * 100:.1f}% ({total_m} markets)")

    print(f"\nDone: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
