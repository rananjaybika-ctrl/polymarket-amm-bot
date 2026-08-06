#!/usr/bin/env python3
"""
PHOENIX ML Entry Backtest — Guaranteed-Hedge Cycle Model

=============================================================================
COPIED FROM: phoenix_main_backtest.py (validated execution engine)
ML FROM: short_term_price_prediction.py (LODO XGBoost, FEATURE_SETS)
=============================================================================

Each cycle:
  1. ML signal fires (high confidence) → taker entry on selected side (5 shares)
  2. Place maker hedge bid on other side (confidence-based offset)
  3. If maker hedge not filled in 60s → taker escalation at current ask
  4. 100% hedge rate by design

Configs: 4 conf_thresholds × 2 windows × 2 sides = 16 configs × 6 LODO folds = 96 runs.

Usage:
    python research/backtests/phoenix_ml_entry_backtest.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Dict
import sys
from datetime import datetime
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# =============================================================================
# IMPORTS - FROM src/core (Single Source of Truth)
# =============================================================================
from src.core import polymarket_taker_fee

try:
    from xgboost import XGBClassifier
    HAS_XGB = True
except ImportError:
    HAS_XGB = False
    print("WARNING: xgboost not installed. Install with: pip install xgboost")

# =============================================================================
# CONSTANTS (from phoenix_main_backtest.py)
# =============================================================================
STARTING_CAPITAL = 170.0
MAX_CAPITAL_FRACTION = 0.50

BASE_DIR = Path("/Users/rananjaybika/polymarket-amm-bot")
FEATURES_DIR = BASE_DIR / "research" / "signal_research" / "results"
OUTPUT_DIR = BASE_DIR / "research" / "signal_research" / "results"

# ML settings (from short_term_price_prediction.py findings)
HORIZON = 30  # Best AUC (0.712) and high-conf accuracy (80.2%)
TARGET_COL = f'exp_up_{HORIZON}s'

# =============================================================================
# FEATURE SETS (from short_term_price_prediction.py — FEATURE_SETS['raw_all+micro'])
# =============================================================================
BEST_FEATURES = [
    # raw_1m
    'rsi_14_1m', 'rsi_2_1m', 'rsi_5_1m', 'rsi_7_1m',
    'macd_histogram_1m', 'bb_pct_b_1m', 'stoch_k_1m', 'stoch_d_1m',
    'roc_10_1m', 'atr_14_bps_1m',
    'ema_20_slope_5m', 'ema_20_slope_20m',
    'price_vs_ema20_1m', 'price_vs_ema50_1m', 'price_vs_ema200_1m',
    'ema20_vs_ema50_1m', 'ema50_vs_ema200_1m',
    'stoch_k_5_1m', 'stoch_k_10_1m',
    # raw_1s
    'rsi_14_1s', 'macd_histogram_1s', 'bb_pct_b_1s', 'stoch_k_1s',
    'roc_10_1s', 'atr_14_bps_1s',
    'price_vs_ema20_1s', 'price_vs_ema50_1s', 'price_vs_ema200_1s',
    'ema_20_slope_5s', 'ema_20_slope_20s',
    # ema_mtf
    'ema_20_1s_vs_1m', 'ema_50_1s_vs_1m', 'ema_200_1s_vs_1m',
    # poly_micro (dropped acceleration/jerk/momentum: missing in Jan18+OOS3+4, 0% ML importance)
    'velocity_bps', 'up_imbalance', 'down_imbalance',
    'spread', 'expensive_ask', 'time_remaining_secs',
]

# =============================================================================
# DATASETS (from short_term_price_prediction.py — matches feature CSV keys)
# =============================================================================
DATASETS = {
    "Jan18": {
        "name": "Jan 18 (IS subset)",
        "obs_files": ["research/observer/grid_obs_20260118.csv"],
        "res_files": ["research/observer/market_resolutions.csv"],
    },
    "OOS3+4": {
        "name": "OOS3+4 (Jan 22-24)",
        "obs_files": ["research/observer/PROTECTED_grid_obs_oos3_oos4_combined.csv"],
        "res_files": ["research/observer/market_resolutions_verified.csv"],
    },
    "OOS7": {
        "name": "OOS7 (Jan 29-30)",
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
        "obs_files": ["research/observer/grid_obs_20260131.csv"],
        "res_files": ["research/observer/resolutions_20260131.csv"],
    },
    "OOS9": {
        "name": "OOS9 (Feb 1-3)",
        "obs_files": ["research/observer/grid_obs_oos9.csv"],
        "res_files": [
            "research/observer/resolutions_oos9_1.csv",
            "research/observer/resolutions_oos9_2.csv",
        ],
    },
    "Feb5": {
        "name": "Feb 5",
        "obs_files": ["research/observer/grid_obs_20260205.csv"],
        "res_files": ["research/observer/resolutions_20260205.csv"],
    },
}


# =============================================================================
# DATA CLASSES
# =============================================================================
@dataclass
class CycleConfig:
    """Guaranteed-hedge cycle: taker entry → maker hedge → taker escalation at timeout."""
    name: str
    conf_threshold: float
    # Entry: always taker on selected side
    delay_ms: float = 0.0               # 0ms or 542ms taker delay
    shares: int = 5                      # fixed per cycle
    # Window
    entry_start_secs: float = 9999.0     # 9999 = no filter
    entry_end_secs: float = 0.0          # 0 = no filter
    cooldown_secs: int = 10
    # Side selection
    force_expensive_side: bool = True    # True=PHOENIX, False=ML decides
    # Hedge: maker bid first, escalate to taker after timeout
    hedge_timeout_secs: float = 60.0     # escalate to taker after this
    hedge_offset_tight: float = 0.01     # weak signal → tight offset
    hedge_offset_wide: float = 0.04      # strong signal → wide offset


@dataclass
class TradeResult:
    market_slug: str
    entry_time_remaining: float
    expensive_side: str
    entry_side: str
    entry_price: float
    hedge_price: Optional[float]
    pair_cost: Optional[float]
    is_hedged: bool
    hedge_type: str            # 'maker', 'taker_escalated', 'none'
    pnl_gross: float
    pnl_net: float
    correct_direction: bool
    shares: int
    ml_prob_up: float
    ml_confidence: float
    entry_fee: float
    hedge_fee: float           # 0 if maker, taker_fee if escalated
    dataset: str
    config_name: str


# =============================================================================
# DATA LOADING (adapted from phoenix_main_backtest.py load_dataset)
# =============================================================================
def load_observer_data(dataset_key: str):
    """Load observer data + resolutions for a dataset."""
    config = DATASETS[dataset_key]

    obs_dfs = []
    for fname in config['obs_files']:
        fpath = BASE_DIR / fname
        if fpath.exists():
            df = pd.read_csv(fpath, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)
            print(f"    {fpath.name}: {len(df):,} rows")
        else:
            print(f"    {fpath.name}: NOT FOUND")

    if not obs_dfs:
        return None, {}, 0

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])

    for col in ['up_ask', 'down_ask', 'time_remaining_secs', 'timestamp_ms']:
        if col in obs_df.columns:
            obs_df[col] = pd.to_numeric(obs_df[col], errors='coerce')

    resolutions = {}
    for res_fname in config.get('res_files', []):
        res_path = BASE_DIR / res_fname
        if res_path.exists():
            res_df = pd.read_csv(res_path)
            if 'slug' in res_df.columns and 'winner' in res_df.columns:
                for _, row in res_df.iterrows():
                    resolutions[row['slug']] = row['winner']
            elif 'market_slug' in res_df.columns and 'resolution' in res_df.columns:
                for _, row in res_df.iterrows():
                    resolutions[row['market_slug']] = row['resolution']
            print(f"    {Path(res_fname).name}: {len(res_df)} resolutions")
    print(f"    Total resolutions: {len(resolutions)} markets")

    duration_hours = (obs_df['timestamp_ms'].max() - obs_df['timestamp_ms'].min()) / (1000 * 3600)
    print(f"    Combined: {len(obs_df):,} rows, {obs_df['market_slug'].nunique()} markets, {duration_hours:.1f}h")

    return obs_df, resolutions, duration_hours


def precompute_market_arrays(obs_df: pd.DataFrame, resolutions: Dict[str, str]) -> Dict:
    """Pre-extract numpy arrays per market for fast simulation.
    Identical approach to phoenix_main_backtest.py precompute_markets()."""
    market_data = {}

    for slug, group in obs_df.groupby('market_slug'):
        if slug not in resolutions:
            continue

        mdf = group.sort_values('timestamp_ms')
        n = len(mdf)
        if n < 10:
            continue

        ts = mdf['timestamp_ms'].values.copy()
        up_ask = mdf['up_ask'].values.astype(float)
        down_ask = mdf['down_ask'].values.astype(float)
        time_rem = mdf['time_remaining_secs'].values.astype(float)

        market_data[slug] = {
            'resolution': resolutions[slug],
            'n': n,
            'ts': ts,
            'up_ask': up_ask,
            'down_ask': down_ask,
            'time_rem': time_rem,
        }

    return market_data


# =============================================================================
# ML MODEL TRAINING — LODO XGBoost
# =============================================================================
def train_lodo_model(combined_features: pd.DataFrame, test_ds: str):
    """
    Train XGBoost using Leave-One-Dataset-Out, return test predictions.

    Model config matches short_term_price_prediction.py (best performer).
    """
    if not HAS_XGB:
        print("  ERROR: xgboost not available")
        return None, []

    train = combined_features[combined_features['dataset'] != test_ds].copy()
    test = combined_features[combined_features['dataset'] == test_ds].copy()

    avail_features = [f for f in BEST_FEATURES if f in combined_features.columns]

    # Drop rows with NaN in features or target
    train_valid = train.dropna(subset=avail_features + [TARGET_COL])
    test_valid = test.dropna(subset=avail_features + [TARGET_COL])

    if len(train_valid) < 100 or len(test_valid) < 10:
        print(f"  Insufficient data: train={len(train_valid)}, test={len(test_valid)}")
        return None, avail_features

    X_train = train_valid[avail_features]
    y_train = train_valid[TARGET_COL].astype(int)
    X_test = test_valid[avail_features]

    model = XGBClassifier(
        n_estimators=200,
        learning_rate=0.1,
        max_depth=4,
        reg_alpha=0.5,
        reg_lambda=1.0,
        eval_metric='logloss',
        random_state=42,
        use_label_encoder=False,
        verbosity=0,
        n_jobs=1,  # Prevent memory blowup
    )
    model.fit(X_train, y_train)

    probs = model.predict_proba(X_test)[:, 1]
    test_valid = test_valid.copy()
    test_valid['ml_prob_up'] = probs
    test_valid['ml_confidence'] = np.abs(probs - 0.5)

    return test_valid, avail_features


# =============================================================================
# MARKET SIMULATION — Guaranteed-Hedge Cycle Model
# =============================================================================
def simulate_market_cycle(
    slug: str,
    md: Dict,
    predictions: pd.DataFrame,
    config: CycleConfig,
    dataset_name: str,
) -> List[TradeResult]:
    """
    Simulate guaranteed-hedge cycles for a single market.

    Each cycle:
    1. Signal fires (high ML confidence) → taker entry on selected side (fixed shares)
    2. Place maker hedge bid on other side (confidence-based offset)
    3. If maker hedge not filled in hedge_timeout → taker escalation at current ask
    4. Every trade is guaranteed hedged (100% hedge rate by design)

    Execution engine from paper_trading.py:
    - Taker entry: fill at ask after delay_ms, with taker fees
    - Maker hedge: price-touch (ask <= our_bid), 0% fee
    - Taker escalation: fill at ask at timeout moment, with taker fees
    """
    resolution = md['resolution']
    ts = md['ts']
    up_ask = md['up_ask']
    down_ask = md['down_ask']
    n = md['n']

    if len(predictions) == 0:
        return []

    cooldown_ms = config.cooldown_secs * 1000
    trades = []
    last_entry_ts = 0

    # Convert predictions to sorted arrays for fast iteration
    preds_sorted = predictions.sort_values('timestamp_ms')
    pred_ts_arr = preds_sorted['timestamp_ms'].values.astype(np.int64)
    pred_tr_arr = preds_sorted['time_remaining_secs'].values.astype(float)
    pred_ua_arr = preds_sorted['up_ask'].values.astype(float)
    pred_da_arr = preds_sorted['down_ask'].values.astype(float)
    pred_prob_arr = preds_sorted['ml_prob_up'].values.astype(float)
    pred_conf_arr = preds_sorted['ml_confidence'].values.astype(float)

    for pi in range(len(pred_ts_arr)):
        pred_ts = int(pred_ts_arr[pi])
        tr = pred_tr_arr[pi]

        # Entry window check (9999/0 = no filter)
        if tr > config.entry_start_secs or tr < config.entry_end_secs:
            continue

        # Cooldown
        if pred_ts - last_entry_ts < cooldown_ms:
            continue

        # Signal confidence — need strong signal in EITHER direction
        ml_prob_up = pred_prob_arr[pi]
        ml_conf = pred_conf_arr[pi]

        if ml_conf < config.conf_threshold:
            continue  # No strong signal

        # Determine expensive side
        ua_now = pred_ua_arr[pi]
        da_now = pred_da_arr[pi]
        if np.isnan(ua_now) or np.isnan(da_now) or ua_now <= 0 or da_now <= 0:
            continue

        exp_side = "UP" if ua_now >= da_now else "DOWN"

        # Determine signal direction and entry side
        signal_up = ml_prob_up > 0.5  # True = model predicts expensive going UP

        if config.force_expensive_side:
            # EXP mode: only take UP signals (enter expensive)
            if not signal_up:
                continue
            entry_side = exp_side
        else:
            # ML mode: UP → enter expensive, DOWN → enter cheap
            if signal_up:
                entry_side = exp_side
            else:
                entry_side = "DOWN" if exp_side == "UP" else "UP"

        # Set entry/hedge arrays based on entry_side
        if entry_side == "UP":
            entry_asks = up_ask
            hedge_asks = down_ask
        else:
            entry_asks = down_ask
            hedge_asks = up_ask

        # Map prediction timestamp to observer index
        oi = np.searchsorted(ts, pred_ts, side='right') - 1
        if oi < 0 or oi >= n:
            continue

        # ===== ENTRY: TAKER =====
        entry_ask_now = entry_asks[oi]
        if np.isnan(entry_ask_now) or entry_ask_now <= 0:
            continue

        if config.delay_ms == 0:
            fill_idx = oi
        else:
            future_ts = pred_ts + int(config.delay_ms)
            fill_idx = np.searchsorted(ts, future_ts, side='right') - 1
            if fill_idx < 0 or fill_idx >= n:
                continue

        fill_price = entry_asks[fill_idx]
        if np.isnan(fill_price) or fill_price <= 0:
            continue

        # Entry taker fee
        entry_fee_rate = polymarket_taker_fee(fill_price)
        entry_fee = entry_fee_rate * fill_price * config.shares

        last_entry_ts = pred_ts

        # ===== HEDGE: MAKER FIRST, THEN TAKER ESCALATION =====
        hedge_price = None
        hedge_type = 'none'
        hedge_fee = 0.0
        is_hedged = False

        if fill_idx + 1 < n:
            # Confidence-based hedge offset
            # Weak signal (conf_frac=0) → tight offset (need protection, fills fast)
            # Strong signal (conf_frac=1) → wide offset (confident, save on pair cost)
            conf_range = 0.5 - config.conf_threshold
            if conf_range > 0:
                conf_frac = min(1.0, (ml_conf - config.conf_threshold) / conf_range)
            else:
                conf_frac = 1.0
            hedge_offset = config.hedge_offset_tight + conf_frac * (config.hedge_offset_wide - config.hedge_offset_tight)

            # Compute maker hedge bid on other side
            hedge_at_fill = hedge_asks[fill_idx]
            if not np.isnan(hedge_at_fill) and hedge_at_fill > 0:
                hedge_bid = max(0.01, hedge_at_fill - hedge_offset)
            else:
                hedge_bid = max(0.01, 1.0 - fill_price - hedge_offset)

            # Scan for maker fill within timeout
            timeout_ts = int(ts[fill_idx] + config.hedge_timeout_secs * 1000)
            start_idx = fill_idx + 1
            end_idx = min(n, np.searchsorted(ts, timeout_ts, side='right'))

            if start_idx < end_idx:
                hedge_slice = hedge_asks[start_idx:end_idx]
                fill_mask = hedge_slice <= hedge_bid
                fill_indices = np.where(fill_mask)[0]

                if len(fill_indices) > 0:
                    # Maker fill — 0% fee
                    hedge_price = hedge_bid
                    hedge_type = 'maker'
                    hedge_fee = 0.0
                    is_hedged = True

            # If maker didn't fill → taker escalation at timeout ask
            if not is_hedged:
                # Use ask price at or just after timeout
                esc_idx = min(end_idx, n - 1)
                if esc_idx >= start_idx:
                    escalation_ask = hedge_asks[esc_idx]
                    if not np.isnan(escalation_ask) and escalation_ask > 0:
                        hedge_price = escalation_ask
                        hedge_type = 'taker_escalated'
                        esc_fee_rate = polymarket_taker_fee(escalation_ask)
                        hedge_fee = esc_fee_rate * escalation_ask * config.shares
                        is_hedged = True

        # ===== PnL CALCULATION =====
        if is_hedged:
            pair_cost = fill_price + hedge_price
            pnl_gross = (1.0 - pair_cost) * config.shares
        else:
            # Shouldn't happen in cycle model (only if data ends before timeout)
            if resolution == entry_side:
                pnl_gross = (1.0 - fill_price) * config.shares
            else:
                pnl_gross = -fill_price * config.shares

        pnl_net = pnl_gross - entry_fee - hedge_fee

        trades.append(TradeResult(
            market_slug=slug,
            entry_time_remaining=tr,
            expensive_side=exp_side,
            entry_side=entry_side,
            entry_price=fill_price,
            hedge_price=hedge_price,
            pair_cost=(fill_price + hedge_price) if is_hedged else None,
            is_hedged=is_hedged,
            hedge_type=hedge_type,
            pnl_gross=pnl_gross,
            pnl_net=pnl_net,
            correct_direction=(resolution == entry_side),
            shares=config.shares,
            ml_prob_up=ml_prob_up,
            ml_confidence=ml_conf,
            entry_fee=entry_fee,
            hedge_fee=hedge_fee,
            dataset=dataset_name,
            config_name=config.name,
        ))

    return trades


# =============================================================================
# CONFIG GENERATION — Cycle configs
# =============================================================================
# Entry window presets: (start_secs, end_secs, label)
WINDOW_PRESETS = [
    (9999, 0, "nowin"),       # No window — enter anytime
    (720, 120, "w120_720"),   # 12min to 2min before resolution
]


def generate_cycle_configs(force_expensive_side: bool) -> List[CycleConfig]:
    """Generate cycle configs for a given side mode."""
    tag = "EXP" if force_expensive_side else "ML"
    configs = []
    thresholds = [0.10, 0.15, 0.20, 0.25]

    for start_s, end_s, wlabel in WINDOW_PRESETS:
        for ct in thresholds:
            configs.append(CycleConfig(
                name=f'cycle_ct{int(ct*100)}_{wlabel}_{tag}',
                conf_threshold=ct,
                delay_ms=0,
                entry_start_secs=start_s,
                entry_end_secs=end_s,
                force_expensive_side=force_expensive_side,
            ))

    return configs


# =============================================================================
# METRICS — with hedge_type breakdown
# =============================================================================
def calculate_metrics(
    trades: List[TradeResult],
    duration_hours: float,
    config_name: str,
    dataset_name: str,
) -> Dict:
    """Calculate performance metrics for a set of trades."""
    if not trades:
        return {
            'config': config_name,
            'test_ds': dataset_name,
            'n_trades': 0,
            'hedge_rate': 0,
            'unhedged_pct': 100.0,
            'maker_hedge_pct': 0,
            'taker_esc_pct': 0,
            'avg_entry_price': 0,
            'avg_pair_cost': 0,
            'total_pnl': 0,
            'pnl_per_trade': 0,
            'pnl_per_hr': 0,
            'win_rate': 0,
            'fade_accuracy': 0,
            'total_entry_fees': 0,
            'total_hedge_fees': 0,
            'total_fees': 0,
            'markets_traded': 0,
            'max_drawdown_pct': 0,
            'roi_pct': 0,
        }

    n = len(trades)
    pnls = [t.pnl_net for t in trades]
    total_pnl = sum(pnls)

    # Hedge type breakdown
    maker_hedged = sum(1 for t in trades if t.hedge_type == 'maker')
    taker_escalated = sum(1 for t in trades if t.hedge_type == 'taker_escalated')
    unhedged = sum(1 for t in trades if t.hedge_type == 'none')
    hedge_rate = (maker_hedged + taker_escalated) / n * 100
    unhedged_pct = unhedged / n * 100
    maker_hedge_pct = maker_hedged / n * 100
    taker_esc_pct = taker_escalated / n * 100

    # Pair cost (only for hedged trades)
    hedged_trades = [t for t in trades if t.is_hedged]
    avg_pair_cost = np.mean([t.pair_cost for t in hedged_trades]) if hedged_trades else 0

    # Accuracy
    correct = sum(1 for t in trades if t.correct_direction)
    fade_accuracy = correct / n * 100

    # Win rate
    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / n * 100

    # Entry stats
    avg_entry_price = np.mean([t.entry_price for t in trades])
    total_entry_fees = sum(t.entry_fee for t in trades)
    total_hedge_fees = sum(t.hedge_fee for t in trades)
    total_fees = total_entry_fees + total_hedge_fees

    # Markets
    markets_traded = len(set(t.market_slug for t in trades))

    # Drawdown
    cumulative = np.cumsum(pnls)
    peak = np.maximum.accumulate(cumulative)
    drawdown = peak - cumulative
    max_dd = np.max(drawdown) if len(drawdown) > 0 else 0
    max_dd_pct = (max_dd / STARTING_CAPITAL) * 100

    return {
        'config': config_name,
        'test_ds': dataset_name,
        'n_trades': n,
        'hedge_rate': round(hedge_rate, 1),
        'unhedged_pct': round(unhedged_pct, 1),
        'maker_hedge_pct': round(maker_hedge_pct, 1),
        'taker_esc_pct': round(taker_esc_pct, 1),
        'avg_entry_price': round(avg_entry_price, 4),
        'avg_pair_cost': round(avg_pair_cost, 4),
        'total_pnl': round(total_pnl, 2),
        'pnl_per_trade': round(total_pnl / n, 3),
        'pnl_per_hr': round(total_pnl / duration_hours, 2) if duration_hours > 0 else 0,
        'win_rate': round(win_rate, 1),
        'fade_accuracy': round(fade_accuracy, 1),
        'total_entry_fees': round(total_entry_fees, 2),
        'total_hedge_fees': round(total_hedge_fees, 2),
        'total_fees': round(total_fees, 2),
        'markets_traded': markets_traded,
        'max_drawdown_pct': round(max_dd_pct, 1),
        'roi_pct': round(total_pnl / STARTING_CAPITAL * 100, 1),
    }


# =============================================================================
# MAIN
# =============================================================================
OBI_FEATURES = ['up_imbalance', 'down_imbalance']

# Feature set WITHOUT OBI — allows all 6 datasets (Jan18/OOS3+4 have no OBI data)
FEATURES_NO_OBI = [f for f in BEST_FEATURES if f not in OBI_FEATURES]


def run_backtest_pass(combined: pd.DataFrame, feature_list: List[str],
                      configs: List[CycleConfig], pass_label: str) -> pd.DataFrame:
    """Run one full backtest pass (all folds × all configs) with a given feature set."""
    print(f"\n{'#'*100}")
    print(f"# PASS: {pass_label} ({len(feature_list)} features, {len(configs)} configs)")
    print(f"{'#'*100}")

    datasets = sorted(DATASETS.keys())
    all_results = []

    for fold_idx, test_ds in enumerate(datasets):
        print(f"\n{'='*80}")
        print(f"LODO FOLD {fold_idx+1}/{len(datasets)}: Test on {test_ds} ({DATASETS[test_ds]['name']})")
        print(f"{'='*80}")

        # 1. Train ML model (once per fold)
        print(f"  Training XGBoost (train on {len(datasets)-1} datasets, test on {test_ds})...")

        # Temporarily override BEST_FEATURES for this pass
        global BEST_FEATURES
        saved_features = BEST_FEATURES
        BEST_FEATURES = feature_list

        test_preds, used_features = train_lodo_model(combined, test_ds)

        BEST_FEATURES = saved_features  # Restore

        if test_preds is None:
            print(f"  SKIPPING — insufficient data for ML training")
            continue
        print(f"  {len(test_preds):,} predictions, {len(used_features)} features used")
        print(f"  ML prob range: [{test_preds['ml_prob_up'].min():.3f}, "
              f"{test_preds['ml_prob_up'].max():.3f}], "
              f"mean conf: {test_preds['ml_confidence'].mean():.3f}")

        # 2. Load observer data (once per fold)
        print(f"  Loading observer data for {test_ds}...")
        obs_df, resolutions, duration_hours = load_observer_data(test_ds)
        if obs_df is None:
            print(f"  SKIPPING — no observer data")
            continue

        # 3. Precompute market arrays (once per fold)
        market_data = precompute_market_arrays(obs_df, resolutions)
        print(f"  {len(market_data)} markets with resolution data")

        # 4. Group predictions by market (once per fold)
        preds_by_market = {}
        for slug, group in test_preds.groupby('slug'):
            preds_by_market[slug] = group

        n_with_both = sum(1 for s in preds_by_market if s in market_data)
        print(f"  {n_with_both} markets with both predictions + observer data")

        # Free observer DataFrame (arrays are in market_data now)
        del obs_df

        # 5. Run all configs (main loop) — NO CAPITAL CONSTRAINT
        for config in tqdm(configs, desc=f"  Configs ({test_ds})"):
            all_trades = []

            for slug in sorted(market_data.keys()):
                if slug not in preds_by_market:
                    continue

                md = market_data[slug]
                market_preds = preds_by_market[slug]

                market_trades = simulate_market_cycle(
                    slug, md, market_preds, config, test_ds,
                )
                all_trades.extend(market_trades)

            metrics = calculate_metrics(all_trades, duration_hours, config.name, test_ds)
            metrics['pass'] = pass_label
            all_results.append(metrics)

        # Checkpoint after each fold
        checkpoint_path = OUTPUT_DIR / f"ml_cycle_checkpoint_{pass_label.replace(' ', '_').lower()}.csv"
        pd.DataFrame(all_results).to_csv(checkpoint_path, index=False)
        print(f"  Checkpoint: {len(all_results)} results saved")

    return pd.DataFrame(all_results)


def main():
    print("=" * 100)
    print("ML ENTRY BACKTEST — GUARANTEED-HEDGE CYCLE MODEL")
    print("=" * 100)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"ML: XGBoost LODO, horizon={HORIZON}s, features=NO_OBI ({len(FEATURES_NO_OBI)})")
    print(f"Cycle: taker entry → maker hedge → taker escalation @ {60}s timeout")
    print(f"Shares: 5 fixed per cycle, NO capital constraint")
    print(f"Cooldown: 10s between cycles")
    print(f"Hedge offset: tight=0.01 (weak signal), wide=0.04 (strong signal)")
    print(f"Sweep: 4 thresholds × 2 windows × 2 sides = 16 configs × 6 folds = 96 runs")
    print(f"Windows: {[(s, e, l) for s, e, l in WINDOW_PRESETS]}")
    print()

    # Load pre-computed features
    features_path = FEATURES_DIR / "stp_features_combined.csv"
    print(f"Loading features from {features_path.name}...")
    combined = pd.read_csv(features_path)
    print(f"  {len(combined):,} samples, {combined['dataset'].nunique()} datasets: "
          f"{sorted(combined['dataset'].unique())}")

    if TARGET_COL not in combined.columns:
        print(f"ERROR: Target column '{TARGET_COL}' not found in features")
        return

    up_rate = combined[TARGET_COL].dropna().mean()
    print(f"  Target '{TARGET_COL}': up_rate={up_rate:.1%}, "
          f"baseline_acc={max(up_rate, 1-up_rate):.1%}")

    configs_exp = generate_cycle_configs(force_expensive_side=True)
    configs_ml = generate_cycle_configs(force_expensive_side=False)
    print(f"\n{len(configs_exp)} EXP configs + {len(configs_ml)} ML configs × 6 folds = {(len(configs_exp)+len(configs_ml))*6} total runs")

    # ===== PASS 1: FORCE EXPENSIVE SIDE (only UP signals) =====
    results_exp = run_backtest_pass(combined, FEATURES_NO_OBI, configs_exp, "EXP_SIDE")

    # ===== PASS 2: ML DECIDES SIDE (UP→expensive, DOWN→cheap) =====
    results_ml = run_backtest_pass(combined, FEATURES_NO_OBI, configs_ml, "ML_SIDE")

    # Combine and save
    all_results = pd.concat([results_exp, results_ml], ignore_index=True)
    output_path = OUTPUT_DIR / "ml_cycle_backtest_results.csv"
    all_results.to_csv(output_path, index=False)

    print(f"\n{'='*100}")
    print(f"COMPLETE: {len(all_results)} results saved to {output_path}")
    print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # ===== TOP 16 CONFIGS (sorted by total PnL) =====
    print(f"\n{'='*100}")
    print("ALL CONFIGS — SORTED BY TOTAL PnL")
    print(f"{'='*100}")

    agg = all_results.groupby('config').agg(
        n_trades=('n_trades', 'sum'),
        total_pnl=('total_pnl', 'sum'),
        avg_pnl_hr=('pnl_per_hr', 'mean'),
        avg_hedge=('hedge_rate', 'mean'),
        avg_unh=('unhedged_pct', 'mean'),
        avg_maker_h=('maker_hedge_pct', 'mean'),
        avg_taker_esc=('taker_esc_pct', 'mean'),
        avg_pc=('avg_pair_cost', 'mean'),
        avg_wr=('win_rate', 'mean'),
        avg_acc=('fade_accuracy', 'mean'),
        total_entry_fees=('total_entry_fees', 'sum'),
        total_hedge_fees=('total_hedge_fees', 'sum'),
        total_fees=('total_fees', 'sum'),
        n_folds=('n_trades', 'count'),
    ).reset_index()
    agg = agg.sort_values('total_pnl', ascending=False)

    print(f"\n{'Config':<30} {'#':>5} {'PnL':>8} {'$/hr':>7} {'Hedge%':>6} "
          f"{'MkrH%':>5} {'TkrE%':>5} {'PC':>7} {'WR%':>5} {'Acc%':>5} "
          f"{'EntFee':>7} {'HdgFee':>7}")
    print("-" * 120)
    for _, row in agg.iterrows():
        print(f"{row['config']:<30} "
              f"{int(row['n_trades']):>5} "
              f"${row['total_pnl']:>7.2f} "
              f"${row['avg_pnl_hr']:>6.2f} "
              f"{row['avg_hedge']:>5.1f}% "
              f"{row['avg_maker_h']:>4.1f}% "
              f"{row['avg_taker_esc']:>4.1f}% "
              f"${row['avg_pc']:.4f} "
              f"{row['avg_wr']:>5.1f} "
              f"{row['avg_acc']:>5.1f} "
              f"${row['total_entry_fees']:>6.2f} "
              f"${row['total_hedge_fees']:>6.2f}")

    # Best config
    if len(agg) > 0:
        best = agg.iloc[0]
        print(f"\n{'='*100}")
        print(f"BEST CONFIG: {best['config']}")
        print(f"  Total PnL: ${best['total_pnl']:.2f} across {int(best['n_trades'])} trades ({int(best['n_folds'])} folds)")
        print(f"  PnL/hr (avg): ${best['avg_pnl_hr']:.2f}")
        print(f"  Hedge rate: {best['avg_hedge']:.1f}% (maker={best['avg_maker_h']:.1f}%, taker_esc={best['avg_taker_esc']:.1f}%)")
        print(f"  Avg pair cost: ${best['avg_pc']:.4f}")
        print(f"  Win rate: {best['avg_wr']:.1f}%, Accuracy: {best['avg_acc']:.1f}%")
        print(f"  Fees: entry=${best['total_entry_fees']:.2f}, hedge=${best['total_hedge_fees']:.2f}")
        print(f"\n  FADE baseline: $2.70/hr, 241.3% ROI")
        if best['avg_pnl_hr'] > 2.70:
            print(f"  -> BEATS FADE by ${best['avg_pnl_hr'] - 2.70:.2f}/hr")
        else:
            print(f"  -> BELOW FADE by ${2.70 - best['avg_pnl_hr']:.2f}/hr")

    # ===== EXP vs ML COMPARISON =====
    print(f"\n{'='*100}")
    print("EXP vs ML SIDE COMPARISON")
    print(f"{'='*100}")

    for wlabel in ['nowin', 'w120_720']:
        exp_rows = agg[agg['config'].str.contains(wlabel) & agg['config'].str.endswith('_EXP')]
        ml_rows = agg[agg['config'].str.contains(wlabel) & agg['config'].str.endswith('_ML')]
        exp_pnl = exp_rows['total_pnl'].sum() if len(exp_rows) > 0 else 0
        ml_pnl = ml_rows['total_pnl'].sum() if len(ml_rows) > 0 else 0
        exp_hedge = exp_rows['avg_hedge'].mean() if len(exp_rows) > 0 else 0
        ml_hedge = ml_rows['avg_hedge'].mean() if len(ml_rows) > 0 else 0
        exp_mkr = exp_rows['avg_maker_h'].mean() if len(exp_rows) > 0 else 0
        ml_mkr = ml_rows['avg_maker_h'].mean() if len(ml_rows) > 0 else 0
        winner = "EXP" if exp_pnl > ml_pnl else "ML"
        print(f"  {wlabel:<12}: EXP=${exp_pnl:>8.2f} (hedge={exp_hedge:.1f}%, mkr={exp_mkr:.1f}%) | "
              f"ML=${ml_pnl:>8.2f} (hedge={ml_hedge:.1f}%, mkr={ml_mkr:.1f}%) | "
              f"winner={winner} by ${abs(exp_pnl - ml_pnl):.2f}")

    # ===== WINDOW COMPARISON =====
    print(f"\n{'='*100}")
    print("WINDOW COMPARISON")
    print(f"{'='*100}")

    import re
    for tag in ['EXP', 'ML']:
        tag_rows = agg[agg['config'].str.endswith(f'_{tag}')]
        print(f"\n  --- {tag} SIDE ---")
        for wlabel in ['nowin', 'w120_720']:
            wrows = tag_rows[tag_rows['config'].str.contains(wlabel)]
            if len(wrows) == 0:
                continue
            w_pnl = wrows['total_pnl'].sum()
            w_hr = wrows['avg_pnl_hr'].mean()
            w_trades = int(wrows['n_trades'].sum())
            w_hedge = wrows['avg_hedge'].mean()
            w_mkr = wrows['avg_maker_h'].mean()
            print(f"    {wlabel:<12}: ${w_pnl:>8.2f} total, ${w_hr:>6.2f}/hr, "
                  f"{w_trades} trades, hedge={w_hedge:.1f}% (mkr={w_mkr:.1f}%)")

    # ===== PER-FOLD BREAKDOWN for best config =====
    if len(agg) > 0:
        best_name = agg.iloc[0]['config']
        best_folds = all_results[all_results['config'] == best_name]
        print(f"\n{'='*100}")
        print(f"BEST CONFIG PER-FOLD: {best_name}")
        print(f"{'='*100}")
        print(f"  {'Dataset':<12} {'#':>5} {'PnL':>8} {'$/hr':>7} {'Hedge%':>6} "
              f"{'MkrH%':>5} {'TkrE%':>5} {'PC':>7} {'WR%':>5}")
        print(f"  {'-'*70}")
        for _, row in best_folds.iterrows():
            print(f"  {row['test_ds']:<12} "
                  f"{int(row['n_trades']):>5} "
                  f"${row['total_pnl']:>7.2f} "
                  f"${row['pnl_per_hr']:>6.2f} "
                  f"{row['hedge_rate']:>5.1f}% "
                  f"{row['maker_hedge_pct']:>4.1f}% "
                  f"{row['taker_esc_pct']:>4.1f}% "
                  f"${row['avg_pair_cost']:.4f} "
                  f"{row['win_rate']:>5.1f}")

    # ===== MANDATORY CHECKS =====
    print(f"\n{'='*100}")
    print("MANDATORY CHECKS — ALL CONFIGS")
    print(f"{'='*100}")
    for _, row in agg.iterrows():
        hedge_ok = row['avg_hedge'] >= 80.0
        unh_ok = row['avg_unh'] <= 20.0
        status = "PASS" if (hedge_ok and unh_ok) else "FAIL"
        print(f"  [{status}] {row['config']:<30}: "
              f"PnL=${row['total_pnl']:>7.2f} "
              f"hedge={row['avg_hedge']:.1f}% [{'OK' if hedge_ok else 'FAIL'}] "
              f"unh={row['avg_unh']:.1f}% [{'OK' if unh_ok else 'FAIL'}]")

    # ===== FEE IMPACT ANALYSIS =====
    print(f"\n{'='*100}")
    print("FEE IMPACT ANALYSIS")
    print(f"{'='*100}")
    for _, row in agg.iterrows():
        pnl_before_fees = row['total_pnl'] + row['total_fees']
        fee_drag = row['total_fees'] / max(1, row['n_trades'])
        print(f"  {row['config']:<30}: "
              f"PnL_gross=${pnl_before_fees:>7.2f} "
              f"- fees=${row['total_fees']:>6.2f} "
              f"(entry=${row['total_entry_fees']:>5.2f} + hedge=${row['total_hedge_fees']:>5.2f}) "
              f"= net=${row['total_pnl']:>7.2f} "
              f"[${fee_drag:.3f}/trade]")


if __name__ == "__main__":
    main()
