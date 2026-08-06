#!/usr/bin/env python3
"""
Signal Combination Study — PHOENIX V2 Phase 2
==============================================

COPIED FROM: ema_macd_obi_study.py (validated data loading + indicator computation)

Tests whether ML combination of technical indicators can beat market price accuracy.
Phase 1 showed individual AUCs 0.87-0.93 at T=300 — this tests proper combination.

Changes from source:
  - Expanded from 3 to 6 datasets (Jan18, OOS3+4, Feb5 added)
  - Dropped OBI features (unlocks 3 additional datasets)
  - Added short RSI (2, 5, 7) and short Stochastic (5, 10) on 1m candles
  - Z-score normalization for all continuous 1m indicators
  - Interaction features (RSI x MACD, RSI x Stoch, etc.)
  - Regime features (vol_regime, vol_ratio)
  - Lagged / rate-of-change features
  - ML: XGBoost, Random Forest, Stacking Ensemble, SHAP analysis
  - Validation: Leave-One-Dataset-Out (purged)
  - Edge analysis: model vs market disagreement

Datasets: Jan18, OOS3+4, OOS7, OOS8, OOS9, Feb5 (~500+ markets)
"""

import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, Tuple, Optional
import warnings
import json
from scipy import stats
from tqdm import tqdm

try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import RandomForestClassifier, StackingClassifier
    from sklearn.metrics import roc_auc_score, accuracy_score, classification_report
    from sklearn.preprocessing import StandardScaler
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

try:
    import xgboost as xgb
    HAS_XGB = True
except ImportError:
    HAS_XGB = False

try:
    import lightgbm as lgb
    HAS_LGB = True
except ImportError:
    HAS_LGB = False

try:
    import shap
    HAS_SHAP = True
except ImportError:
    HAS_SHAP = False

warnings.filterwarnings('ignore')

BASE_DIR = Path("/Users/rananjaybika/polymarket-amm-bot")
OUTPUT_DIR = BASE_DIR / "research" / "signal_research" / "results"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# =========================================================================
# DATASETS — 6 datasets, OBI dropped to unlock Jan18, OOS3+4, Feb5
# Copied from ema_macd_obi_study.py + 3 new datasets
# =========================================================================
DATASETS = {
    "Jan18": {
        "name": "Jan 18 (IS subset)",
        "obs_files": [
            "research/observer/grid_obs_20260118.csv",
        ],
        "res_files": [
            "research/observer/market_resolutions.csv",
        ],
        "hf_file": "research/binance_hf/btc_prices_20260118_060340.csv",
    },
    "OOS3+4": {
        "name": "OOS3+4 (Jan 22-24)",
        "obs_files": [
            "research/observer/PROTECTED_grid_obs_oos3_oos4_combined.csv",
        ],
        "res_files": [
            "research/observer/market_resolutions_verified.csv",
        ],
        "hf_file": "research/observer/PROTECTED_btc_prices_oos3_oos4_combined.csv",
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
        "hf_file": "research/binance_hf/btc_prices_20260129_160523.csv",
    },
    "OOS8": {
        "name": "OOS8 (Jan 31)",
        "obs_files": [
            "research/observer/grid_obs_20260131.csv",
        ],
        "res_files": [
            "research/observer/resolutions_20260131.csv",
        ],
        "hf_file": "research/binance_hf/btc_prices_20260131_055231.csv",
    },
    "OOS9": {
        "name": "OOS9 (Feb 1-3)",
        "obs_files": [
            "research/observer/grid_obs_oos9.csv",
        ],
        "res_files": [
            "research/observer/resolutions_oos9_1.csv",
            "research/observer/resolutions_oos9_2.csv",
        ],
        "hf_file": "research/binance_hf/btc_prices_oos9.csv",
    },
    "Feb5": {
        "name": "Feb 5",
        "obs_files": [
            "research/observer/grid_obs_20260205.csv",
        ],
        "res_files": [
            "research/observer/resolutions_20260205.csv",
        ],
        "hf_file": "research/binance_hf/btc_prices_20260204_190733.csv",
    },
}

EMA_PERIODS = [20, 50, 100, 200]
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
EVAL_TIMES = [600, 300, 120]


# =========================================================================
# DATA LOADING — copied from ema_macd_obi_study.py (validated)
# =========================================================================
def load_dataset(dataset_key: str) -> Tuple[Optional[pd.DataFrame], Dict[str, str]]:
    """Load observer data + resolutions for a dataset."""
    config = DATASETS[dataset_key]
    print(f"\n  Loading {config['name']}...")

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
        return None, {}

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])

    for col in ['up_ask', 'down_ask', 'up_bid', 'down_bid', 'binance_price',
                'velocity_bps', 'time_remaining_secs', 'pair_cost', 'spike_magnitude']:
        if col in obs_df.columns:
            obs_df[col] = pd.to_numeric(obs_df[col], errors='coerce')

    for col in ['acceleration_bps2', 'jerk_bps3', 'momentum_5s',
                'up_imbalance', 'down_imbalance']:
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

    n_markets = obs_df['market_slug'].nunique()
    n_resolved = sum(1 for s in obs_df['market_slug'].unique() if s in resolutions)
    print(f"    Combined: {len(obs_df):,} rows, {n_markets} markets, {n_resolved} resolved")

    return obs_df, resolutions


# =========================================================================
# INDICATOR HELPERS — copied from ema_macd_obi_study.py
# =========================================================================
def compute_rsi(series, period=14):
    """Wilder's RSI."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_bollinger(series, period=20, num_std=2):
    """Returns %B and bandwidth."""
    sma = series.rolling(period).mean()
    std = series.rolling(period).std()
    upper = sma + num_std * std
    lower = sma - num_std * std
    pct_b = (series - lower) / (upper - lower).replace(0, np.nan)
    bandwidth = (upper - lower) / sma.replace(0, np.nan)
    return pct_b, bandwidth


def compute_stochastic(high, low, close, k_period=14, d_period=3):
    """%K and %D."""
    lowest = low.rolling(k_period).min()
    highest = high.rolling(k_period).max()
    k = 100 * (close - lowest) / (highest - lowest).replace(0, np.nan)
    d = k.rolling(d_period).mean()
    return k, d


def compute_atr(high, low, close, period=14):
    """Average True Range."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def add_indicators(ind_df, ohlc_df, suffix):
    """Add RSI, Bollinger, Stochastic, ROC, ATR to indicator DataFrame."""
    close = ohlc_df['close']
    high = ohlc_df['high']
    low = ohlc_df['low']

    # RSI (14)
    ind_df['rsi_14'] = compute_rsi(close, 14)

    # Bollinger Bands (20, 2 sigma)
    pct_b, bw = compute_bollinger(close, 20, 2)
    ind_df['bb_pct_b'] = pct_b
    ind_df['bb_bandwidth'] = bw

    # Stochastic (14/3)
    k, d = compute_stochastic(high, low, close, 14, 3)
    ind_df['stoch_k'] = k
    ind_df['stoch_d'] = d

    # ROC (10-period, as %)
    ind_df['roc_10'] = close.pct_change(10) * 100

    # ATR (14) normalized to bps
    atr = compute_atr(high, low, close, 14)
    ind_df['atr_14'] = atr
    ind_df['atr_14_bps'] = atr / close * 10000

    return ind_df


def add_short_indicators_1m(ind_df, ohlc_df):
    """Add short RSI (2, 5, 7) and short Stochastic (5, 10) to 1m candles.
    NEW for signal combination study — mean-reversion signals."""
    close = ohlc_df['close']
    high = ohlc_df['high']
    low = ohlc_df['low']

    # Short RSI periods (mean-reversion signals)
    for period in [2, 5, 7]:
        ind_df[f'rsi_{period}'] = compute_rsi(close, period)

    # Short Stochastic periods
    for k_period in [5, 10]:
        k, d = compute_stochastic(high, low, close, k_period, 3)
        ind_df[f'stoch_k_{k_period}'] = k
        ind_df[f'stoch_d_{k_period}'] = d

    return ind_df


def add_zscore_columns_1m(ind_df):
    """Z-score normalize continuous 1m indicators at candle level.
    NEW for signal combination study."""

    # Slow indicators: rolling window 100
    slow_cols = {
        'rsi_14': 'z_rsi_14',
        'macd_histogram': 'z_macd_hist',
        'bb_pct_b': 'z_bb_pctb',
        'stoch_k': 'z_stoch_k',
        'roc_10': 'z_roc_10',
        'atr_14_bps': 'z_atr_14',
    }
    for src, dst in slow_cols.items():
        if src in ind_df.columns:
            roll_mean = ind_df[src].rolling(100, min_periods=20).mean()
            roll_std = ind_df[src].rolling(100, min_periods=20).std().replace(0, np.nan)
            z = (ind_df[src] - roll_mean) / roll_std
            ind_df[dst] = z.clip(-3, 3)

    # Fast indicators: rolling window 20
    fast_cols = {
        'rsi_2': 'z_rsi_2',
        'rsi_5': 'z_rsi_5',
        'rsi_7': 'z_rsi_7',
        'stoch_k_5': 'z_stoch_k_5',
        'stoch_k_10': 'z_stoch_k_10',
    }
    for src, dst in fast_cols.items():
        if src in ind_df.columns:
            roll_mean = ind_df[src].rolling(20, min_periods=5).mean()
            roll_std = ind_df[src].rolling(20, min_periods=5).std().replace(0, np.nan)
            z = (ind_df[src] - roll_mean) / roll_std
            ind_df[dst] = z.clip(-3, 3)

    # EMA slope z-score (fast window)
    if 'ema_20_slope_5' in ind_df.columns:
        roll_mean = ind_df['ema_20_slope_5'].rolling(20, min_periods=5).mean()
        roll_std = ind_df['ema_20_slope_5'].rolling(20, min_periods=5).std().replace(0, np.nan)
        z = (ind_df['ema_20_slope_5'] - roll_mean) / roll_std
        ind_df['z_ema_20_slope_5m'] = z.clip(-3, 3)

    # Lagged features for rate-of-change
    for col in ['rsi_14', 'macd_histogram']:
        if col in ind_df.columns:
            ind_df[f'{col}_lag1'] = ind_df[col].shift(1)
            ind_df[f'{col}_change'] = ind_df[col] - ind_df[col].shift(1)

    # ATR median for regime detection
    if 'atr_14' in ind_df.columns:
        ind_df['atr_14_median_200'] = ind_df['atr_14'].rolling(200, min_periods=50).median()

    return ind_df


# =========================================================================
# HF DATA LOADING + INDICATOR COMPUTATION
# Copied from ema_macd_obi_study.py, extended with short indicators + z-scores
# =========================================================================
def load_hf_and_compute_indicators(hf_file: str):
    """Load Binance HF data, resample to 1s/1m, compute all indicators."""
    fpath = BASE_DIR / hf_file
    if not fpath.exists():
        print(f"    HF file NOT FOUND: {fpath}")
        return None, None

    print(f"    Loading HF data: {fpath.name}...")
    hf_df = pd.read_csv(fpath, usecols=['timestamp_ms', 'price'])
    hf_df['timestamp_ms'] = pd.to_numeric(hf_df['timestamp_ms'], errors='coerce')
    hf_df['price'] = pd.to_numeric(hf_df['price'], errors='coerce')
    hf_df = hf_df.dropna()
    print(f"    HF loaded: {len(hf_df):,} ticks")

    hf_df['datetime'] = pd.to_datetime(hf_df['timestamp_ms'], unit='ms')
    hf_df = hf_df.set_index('datetime').sort_index()

    # --- 1-SECOND OHLC CANDLES ---
    print(f"    Computing 1s candles + indicators...")
    ohlc_1s = hf_df['price'].resample('1s').ohlc().dropna()
    ohlc_1s.columns = ['open', 'high', 'low', 'close']

    ind_1s = pd.DataFrame(index=ohlc_1s.index)
    ind_1s['price'] = ohlc_1s['close']

    # EMAs
    for period in EMA_PERIODS:
        ind_1s[f'ema_{period}'] = ohlc_1s['close'].ewm(span=period, adjust=False).mean()

    # MACD
    ema_fast = ohlc_1s['close'].ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = ohlc_1s['close'].ewm(span=MACD_SLOW, adjust=False).mean()
    ind_1s['macd_line'] = ema_fast - ema_slow
    ind_1s['macd_signal'] = ind_1s['macd_line'].ewm(span=MACD_SIGNAL, adjust=False).mean()
    ind_1s['macd_histogram'] = ind_1s['macd_line'] - ind_1s['macd_signal']

    # EMA slopes
    for period in [20, 50]:
        ind_1s[f'ema_{period}_slope_5'] = ind_1s[f'ema_{period}'].diff(5) / 5
        ind_1s[f'ema_{period}_slope_20'] = ind_1s[f'ema_{period}'].diff(20) / 20

    # EMA relative positions
    ind_1s['price_vs_ema20'] = ind_1s['price'] - ind_1s['ema_20']
    ind_1s['price_vs_ema50'] = ind_1s['price'] - ind_1s['ema_50']
    ind_1s['price_vs_ema200'] = ind_1s['price'] - ind_1s['ema_200']
    ind_1s['ema20_vs_ema50'] = ind_1s['ema_20'] - ind_1s['ema_50']
    ind_1s['ema50_vs_ema200'] = ind_1s['ema_50'] - ind_1s['ema_200']

    # RSI, Bollinger, Stochastic, ROC, ATR
    ind_1s = add_indicators(ind_1s, ohlc_1s, '1s')

    # --- 1-MINUTE OHLC CANDLES ---
    print(f"    Computing 1m candles + indicators...")
    ohlc_1m = hf_df['price'].resample('1min').ohlc().dropna()
    ohlc_1m.columns = ['open', 'high', 'low', 'close']

    ind_1m = pd.DataFrame(index=ohlc_1m.index)
    ind_1m['price'] = ohlc_1m['close']

    for period in EMA_PERIODS:
        ind_1m[f'ema_{period}'] = ohlc_1m['close'].ewm(span=period, adjust=False).mean()

    ema_fast_1m = ohlc_1m['close'].ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow_1m = ohlc_1m['close'].ewm(span=MACD_SLOW, adjust=False).mean()
    ind_1m['macd_line'] = ema_fast_1m - ema_slow_1m
    ind_1m['macd_signal'] = ind_1m['macd_line'].ewm(span=MACD_SIGNAL, adjust=False).mean()
    ind_1m['macd_histogram'] = ind_1m['macd_line'] - ind_1m['macd_signal']

    for period in [20, 50]:
        ind_1m[f'ema_{period}_slope_5'] = ind_1m[f'ema_{period}'].diff(5) / 5
        ind_1m[f'ema_{period}_slope_20'] = ind_1m[f'ema_{period}'].diff(20) / 20

    ind_1m['price_vs_ema20'] = ind_1m['price'] - ind_1m['ema_20']
    ind_1m['price_vs_ema50'] = ind_1m['price'] - ind_1m['ema_50']
    ind_1m['price_vs_ema200'] = ind_1m['price'] - ind_1m['ema_200']
    ind_1m['ema20_vs_ema50'] = ind_1m['ema_20'] - ind_1m['ema_50']
    ind_1m['ema50_vs_ema200'] = ind_1m['ema_50'] - ind_1m['ema_200']

    # RSI, Bollinger, Stochastic, ROC, ATR on 1m
    ind_1m = add_indicators(ind_1m, ohlc_1m, '1m')

    # NEW: Short RSI (2, 5, 7) and short Stochastic (5, 10) on 1m only
    ind_1m = add_short_indicators_1m(ind_1m, ohlc_1m)

    # NEW: Z-score normalization for all continuous 1m indicators
    ind_1m = add_zscore_columns_1m(ind_1m)

    print(f"    1s candles: {len(ind_1s):,}, 1m candles: {len(ind_1m):,}")
    return ind_1s, ind_1m


def get_indicators_at_time(ind_1s, ind_1m, ts_ms):
    """Look up all indicator values at a specific timestamp.
    Extended with new short indicators, z-scores, lagged features."""
    ts_dt = pd.Timestamp(ts_ms, unit='ms')
    result = {}

    # 1s lookup
    idx = ind_1s.index.searchsorted(ts_dt)
    if idx >= len(ind_1s):
        idx = len(ind_1s) - 1
    if idx > 0:
        d0 = abs((ind_1s.index[idx] - ts_dt).total_seconds())
        d1 = abs((ind_1s.index[idx - 1] - ts_dt).total_seconds())
        if d1 < d0:
            idx -= 1
    if abs((ind_1s.index[idx] - ts_dt).total_seconds()) > 2.0:
        return None

    r1s = ind_1s.iloc[idx]
    result['btc_price'] = r1s['price']

    for p in EMA_PERIODS:
        result[f'ema_{p}_1s'] = r1s[f'ema_{p}']
    result['macd_line_1s'] = r1s['macd_line']
    result['macd_signal_1s'] = r1s['macd_signal']
    result['macd_histogram_1s'] = r1s['macd_histogram']
    for p in [20, 50]:
        result[f'ema_{p}_slope_5s'] = r1s[f'ema_{p}_slope_5']
        result[f'ema_{p}_slope_20s'] = r1s[f'ema_{p}_slope_20']
    result['price_vs_ema20_1s'] = r1s['price_vs_ema20']
    result['price_vs_ema50_1s'] = r1s['price_vs_ema50']
    result['price_vs_ema200_1s'] = r1s['price_vs_ema200']
    result['ema20_vs_ema50_1s'] = r1s['ema20_vs_ema50']
    result['ema50_vs_ema200_1s'] = r1s['ema50_vs_ema200']
    # 1s indicators
    for col in ['rsi_14', 'bb_pct_b', 'bb_bandwidth', 'stoch_k', 'stoch_d', 'roc_10', 'atr_14_bps']:
        result[f'{col}_1s'] = r1s.get(col, np.nan)

    # 1m lookup
    idx_m = ind_1m.index.searchsorted(ts_dt)
    if idx_m >= len(ind_1m):
        idx_m = len(ind_1m) - 1
    if idx_m > 0:
        d0 = abs((ind_1m.index[idx_m] - ts_dt).total_seconds())
        d1 = abs((ind_1m.index[idx_m - 1] - ts_dt).total_seconds())
        if d1 < d0:
            idx_m -= 1
    if abs((ind_1m.index[idx_m] - ts_dt).total_seconds()) > 90:
        return None

    r1m = ind_1m.iloc[idx_m]
    for p in EMA_PERIODS:
        result[f'ema_{p}_1m'] = r1m[f'ema_{p}']
    result['macd_line_1m'] = r1m['macd_line']
    result['macd_signal_1m'] = r1m['macd_signal']
    result['macd_histogram_1m'] = r1m['macd_histogram']
    for p in [20, 50]:
        result[f'ema_{p}_slope_5m'] = r1m[f'ema_{p}_slope_5']
        result[f'ema_{p}_slope_20m'] = r1m[f'ema_{p}_slope_20']
    result['price_vs_ema20_1m'] = r1m['price_vs_ema20']
    result['price_vs_ema50_1m'] = r1m['price_vs_ema50']
    result['price_vs_ema200_1m'] = r1m['price_vs_ema200']
    result['ema20_vs_ema50_1m'] = r1m['ema20_vs_ema50']
    result['ema50_vs_ema200_1m'] = r1m['ema50_vs_ema200']

    # Standard 1m indicators
    for col in ['rsi_14', 'bb_pct_b', 'bb_bandwidth', 'stoch_k', 'stoch_d', 'roc_10', 'atr_14_bps']:
        result[f'{col}_1m'] = r1m.get(col, np.nan)

    # NEW: Short RSI on 1m
    for period in [2, 5, 7]:
        result[f'rsi_{period}_1m'] = r1m.get(f'rsi_{period}', np.nan)

    # NEW: Short Stochastic on 1m
    for k_period in [5, 10]:
        result[f'stoch_k_{k_period}_1m'] = r1m.get(f'stoch_k_{k_period}', np.nan)
        result[f'stoch_d_{k_period}_1m'] = r1m.get(f'stoch_d_{k_period}', np.nan)

    # NEW: Z-scored 1m indicators
    for zcol in ['z_rsi_14', 'z_rsi_2', 'z_rsi_5', 'z_rsi_7',
                  'z_macd_hist', 'z_bb_pctb', 'z_stoch_k', 'z_roc_10', 'z_atr_14',
                  'z_stoch_k_5', 'z_stoch_k_10', 'z_ema_20_slope_5m']:
        result[f'{zcol}_1m'] = r1m.get(zcol, np.nan)

    # NEW: Lagged features
    for col in ['rsi_14_lag1', 'rsi_14_change', 'macd_histogram_lag1', 'macd_histogram_change']:
        result[f'{col}_1m'] = r1m.get(col, np.nan)

    # NEW: ATR median for regime detection
    result['atr_14_median_200_1m'] = r1m.get('atr_14_median_200', np.nan)

    # Multi-TF delta
    for p in EMA_PERIODS:
        result[f'ema_{p}_1s_vs_1m'] = result[f'ema_{p}_1s'] - result[f'ema_{p}_1m']

    return result


# =========================================================================
# PER-MARKET FEATURE EXTRACTION
# Copied from ema_macd_obi_study.py, extended with interaction + regime features
# OBI REMOVED
# =========================================================================
def extract_all_features(obs_df, resolutions, ind_1s, ind_1m, dataset_name):
    """Extract indicator features for each market at each eval time."""
    all_rows = []

    hf_start_ms = int(ind_1s.index[0].timestamp() * 1000)
    hf_end_ms = int(ind_1s.index[-1].timestamp() * 1000)
    warmup_ms = hf_start_ms + 200 * 60 * 1000  # 200 min for 1m EMA 200

    slugs = [s for s in obs_df['market_slug'].unique() if s in resolutions]

    for slug in tqdm(slugs, desc=f"  {dataset_name}"):
        resolution = resolutions[slug]
        mdf = obs_df[obs_df['market_slug'] == slug].sort_values('timestamp_ms')
        if len(mdf) < 10:
            continue

        for eval_time in EVAL_TIMES:
            nearby = mdf[
                (mdf['time_remaining_secs'] >= eval_time - 10) &
                (mdf['time_remaining_secs'] <= eval_time + 10)
            ]
            if len(nearby) == 0:
                continue

            obs_row = nearby.iloc[len(nearby) // 2]
            ts_ms = int(obs_row['timestamp_ms'])

            if ts_ms < warmup_ms or ts_ms > hf_end_ms:
                continue

            indicators = get_indicators_at_time(ind_1s, ind_1m, ts_ms)
            if indicators is None:
                continue

            ua = obs_row.get('up_ask', np.nan)
            da = obs_row.get('down_ask', np.nan)
            if pd.isna(ua) or pd.isna(da) or ua <= 0 or da <= 0:
                continue

            expensive_side = "UP" if ua >= da else "DOWN"
            resolution_is_up = (resolution == "UP")

            row = {
                'dataset': dataset_name,
                'slug': slug,
                'eval_time': eval_time,
                'resolution': resolution,
                'resolution_is_up': resolution_is_up,
                'expensive_side': expensive_side,
                'market_correct': (expensive_side == resolution),
                'up_ask': ua,
                'down_ask': da,
                'spread': abs(ua - da),
            }

            row.update(indicators)

            # --- Derived bias signals (kept from source) ---
            btc = indicators['btc_price']

            # EMA bias score
            n_above_1s = sum(1 for p in EMA_PERIODS if btc > indicators[f'ema_{p}_1s'])
            n_above_1m = sum(1 for p in EMA_PERIODS if btc > indicators[f'ema_{p}_1m'])
            row['ema_bias_score_1s'] = n_above_1s / len(EMA_PERIODS)
            row['ema_bias_score_1m'] = n_above_1m / len(EMA_PERIODS)

            # EMA alignment
            e1s = [indicators[f'ema_{p}_1s'] for p in EMA_PERIODS]
            e1m = [indicators[f'ema_{p}_1m'] for p in EMA_PERIODS]
            row['ema_aligned_bull_1s'] = all(e1s[i] > e1s[i+1] for i in range(3))
            row['ema_aligned_bear_1s'] = all(e1s[i] < e1s[i+1] for i in range(3))
            row['ema_aligned_bull_1m'] = all(e1m[i] > e1m[i+1] for i in range(3))
            row['ema_aligned_bear_1m'] = all(e1m[i] < e1m[i+1] for i in range(3))

            # Multi-TF agreement
            bias_1s = "UP" if row['ema_bias_score_1s'] > 0.5 else "DOWN"
            bias_1m = "UP" if row['ema_bias_score_1m'] > 0.5 else "DOWN"
            row['multi_tf_agree'] = (bias_1s == bias_1m)

            # MACD direction
            row['macd_bull_1s'] = indicators['macd_histogram_1s'] > 0
            row['macd_bull_1m'] = indicators['macd_histogram_1m'] > 0

            # RSI direction
            rsi_1m = indicators.get('rsi_14_1m', 50)
            row['rsi_bull_1m'] = rsi_1m > 50 if not pd.isna(rsi_1m) else False

            # --- NEW: Interaction features (from z-scored values) ---
            z_rsi_14 = indicators.get('z_rsi_14_1m', 0) or 0
            z_rsi_2 = indicators.get('z_rsi_2_1m', 0) or 0
            z_macd = indicators.get('z_macd_hist_1m', 0) or 0
            z_bb = indicators.get('z_bb_pctb_1m', 0) or 0
            z_stoch = indicators.get('z_stoch_k_1m', 0) or 0
            z_atr = indicators.get('z_atr_14_1m', 0) or 0
            z_ema_slope = indicators.get('z_ema_20_slope_5m_1m', 0) or 0

            row['rsi14_x_macd'] = z_rsi_14 * z_macd
            row['rsi2_x_stoch'] = z_rsi_2 * z_stoch
            row['macd_x_atr'] = z_macd * z_atr
            row['ema_slope_x_rsi'] = z_ema_slope * z_rsi_14
            row['rsi_squared'] = z_rsi_14 ** 2
            row['bb_x_rsi'] = z_bb * z_rsi_14

            # --- NEW: Regime features ---
            atr_14 = indicators.get('atr_14_bps_1m', np.nan)
            atr_median = indicators.get('atr_14_median_200_1m', np.nan)
            if not pd.isna(atr_14) and not pd.isna(atr_median) and atr_median > 0:
                row['vol_regime'] = 1 if atr_14 > atr_median else 0
                row['vol_ratio'] = atr_14 / atr_median
            else:
                row['vol_regime'] = np.nan
                row['vol_ratio'] = np.nan

            # --- NEW: Lagged / ROC features ---
            row['rsi_roc_1m'] = indicators.get('rsi_14_change_1m', np.nan)
            row['macd_hist_roc_1m'] = indicators.get('macd_histogram_change_1m', np.nan)

            all_rows.append(row)

    return pd.DataFrame(all_rows)


# =========================================================================
# ML ANALYSIS V2 — XGBoost, RF, Stacking, SHAP
# =========================================================================

# Feature groups for systematic testing
FEATURE_GROUPS = {
    'z_core_1m': [
        'z_rsi_14_1m', 'z_rsi_2_1m', 'z_rsi_5_1m', 'z_rsi_7_1m',
        'z_macd_hist_1m', 'z_bb_pctb_1m', 'z_stoch_k_1m',
        'z_roc_10_1m', 'z_atr_14_1m', 'z_ema_20_slope_5m_1m',
        'z_stoch_k_5_1m', 'z_stoch_k_10_1m',
    ],
    'interactions': [
        'rsi14_x_macd', 'rsi2_x_stoch', 'macd_x_atr',
        'ema_slope_x_rsi', 'rsi_squared', 'bb_x_rsi',
    ],
    'regime': ['vol_regime', 'vol_ratio'],
    'lagged': ['rsi_roc_1m', 'macd_hist_roc_1m'],
    'raw_1m': [
        'rsi_14_1m', 'rsi_2_1m', 'rsi_5_1m', 'rsi_7_1m',
        'macd_histogram_1m', 'bb_pct_b_1m', 'stoch_k_1m', 'stoch_d_1m',
        'roc_10_1m', 'atr_14_bps_1m',
        'ema_20_slope_5m', 'ema_20_slope_20m',
        'price_vs_ema20_1m', 'price_vs_ema50_1m', 'price_vs_ema200_1m',
        'ema20_vs_ema50_1m', 'ema50_vs_ema200_1m',
        'stoch_k_5_1m', 'stoch_k_10_1m',
    ],
    'raw_1s': [
        'rsi_14_1s', 'macd_histogram_1s', 'bb_pct_b_1s', 'stoch_k_1s',
        'roc_10_1s', 'atr_14_bps_1s',
        'price_vs_ema20_1s', 'price_vs_ema50_1s', 'price_vs_ema200_1s',
        'ema_20_slope_5s', 'ema_20_slope_20s',
    ],
    'ema_mtf': [
        'ema_20_1s_vs_1m', 'ema_50_1s_vs_1m', 'ema_200_1s_vs_1m',
    ],
}

# Composed feature sets
FEATURE_SETS = {
    'z_core': FEATURE_GROUPS['z_core_1m'],
    'z_core+interactions': FEATURE_GROUPS['z_core_1m'] + FEATURE_GROUPS['interactions'],
    'z_all': (FEATURE_GROUPS['z_core_1m'] + FEATURE_GROUPS['interactions'] +
              FEATURE_GROUPS['regime'] + FEATURE_GROUPS['lagged']),
    'raw_1m': FEATURE_GROUPS['raw_1m'],
    'raw_all': FEATURE_GROUPS['raw_1m'] + FEATURE_GROUPS['raw_1s'] + FEATURE_GROUPS['ema_mtf'],
    'z_all+market': (FEATURE_GROUPS['z_core_1m'] + FEATURE_GROUPS['interactions'] +
                     FEATURE_GROUPS['regime'] + FEATURE_GROUPS['lagged'] +
                     ['up_ask', 'down_ask', 'spread']),
}


def analyze_ml_v2(features_df):
    """ML combination analysis: XGBoost, RF, LR, Stacking with LODO validation."""
    if not HAS_SKLEARN:
        print("  sklearn not available, skipping ML analysis")
        return pd.DataFrame(), {}

    print("\n" + "=" * 70)
    print("ML COMBINATION ANALYSIS (V2)")
    print("=" * 70)

    results = []
    best_models = {}  # Store best model per eval_time for SHAP
    datasets = sorted(features_df['dataset'].unique())

    for eval_time in EVAL_TIMES:
        edf = features_df[features_df['eval_time'] == eval_time].copy()
        if len(edf) < 50:
            print(f"  T={eval_time}: only {len(edf)} obs, skipping")
            continue

        market_acc = edf['market_correct'].mean()
        print(f"\n  T={eval_time}: {len(edf)} obs, {edf['dataset'].nunique()} datasets, "
              f"market acc={market_acc:.1%}")

        best_auc_this_t = 0

        for fset_name, fset_cols in tqdm(FEATURE_SETS.items(), desc=f"  ML T={eval_time}"):
            avail = [c for c in fset_cols if c in edf.columns]
            if len(avail) < 3:
                continue

            # Leave-one-dataset-out cross-validation
            fold_results = []
            for test_ds in datasets:
                train = edf[edf['dataset'] != test_ds]
                test = edf[edf['dataset'] == test_ds]

                X_tr = train[avail].copy()
                y_tr = train['resolution_is_up'].astype(int)
                X_te = test[avail].copy()
                y_te = test['resolution_is_up'].astype(int)

                # Drop rows with NaN in features
                valid_tr = X_tr.dropna().index
                valid_te = X_te.dropna().index
                X_tr = X_tr.loc[valid_tr]
                y_tr = y_tr.loc[valid_tr]
                X_te = X_te.loc[valid_te]
                y_te = y_te.loc[valid_te]

                if len(X_tr) < 30 or len(X_te) < 5:
                    continue

                scaler = StandardScaler()
                X_tr_s = scaler.fit_transform(X_tr)
                X_te_s = scaler.transform(X_te)

                # Market accuracy on this test fold
                test_market_acc = edf.loc[valid_te, 'market_correct'].mean()

                # Models to train (n_jobs=1 to prevent memory blowup)
                models_to_run = [
                    ('LR', LogisticRegression(max_iter=1000, C=1.0, random_state=42)),
                    ('RF', RandomForestClassifier(
                        n_estimators=100, max_depth=5, min_samples_leaf=5,
                        random_state=42, n_jobs=1)),
                ]

                if HAS_XGB:
                    models_to_run.append(('XGB', xgb.XGBClassifier(
                        n_estimators=200, learning_rate=0.1, max_depth=4,
                        reg_alpha=0.5, reg_lambda=1.0,
                        eval_metric='logloss', random_state=42,
                        use_label_encoder=False, verbosity=0, n_jobs=1)))

                # Stacking: XGB + RF -> LR meta-learner
                if HAS_XGB:
                    estimators = [
                        ('xgb', xgb.XGBClassifier(
                            n_estimators=100, learning_rate=0.1, max_depth=4,
                            reg_alpha=0.5, reg_lambda=1.0,
                            eval_metric='logloss', random_state=42,
                            use_label_encoder=False, verbosity=0, n_jobs=1)),
                        ('rf', RandomForestClassifier(
                            n_estimators=50, max_depth=5, min_samples_leaf=5,
                            random_state=42, n_jobs=1)),
                    ]
                    models_to_run.append(('Stack', StackingClassifier(
                        estimators=estimators,
                        final_estimator=LogisticRegression(max_iter=1000),
                        cv=3, n_jobs=1)))

                for mname, model in models_to_run:
                    try:
                        model.fit(X_tr_s, y_tr)
                        prob = model.predict_proba(X_te_s)[:, 1]
                        pred = model.predict(X_te_s)
                        auc = roc_auc_score(y_te, prob)
                        acc = accuracy_score(y_te, pred)
                    except Exception as e:
                        continue

                    fold_result = {
                        'eval_time': eval_time,
                        'features': fset_name,
                        'n_features': len(avail),
                        'model': mname,
                        'test_ds': test_ds,
                        'train_n': len(X_tr),
                        'test_n': len(X_te),
                        'auc': auc,
                        'accuracy': acc,
                        'market_acc': test_market_acc,
                        'acc_vs_market': acc - test_market_acc,
                    }
                    results.append(fold_result)
                    fold_results.append(fold_result)

                    # Track best for SHAP
                    if auc > best_auc_this_t and mname in ('XGB', 'Stack'):
                        best_auc_this_t = auc
                        best_models[eval_time] = {
                            'model': model,
                            'scaler': scaler,
                            'features': avail,
                            'fset_name': fset_name,
                            'mname': mname,
                            'X_test': X_te_s,
                            'y_test': y_te,
                            'auc': auc,
                        }

        # Checkpoint save per eval_time
        if results:
            pd.DataFrame(results).to_csv(
                OUTPUT_DIR / f"combo_ml_checkpoint_T{eval_time}.csv", index=False)
            print(f"  Checkpoint saved: combo_ml_checkpoint_T{eval_time}.csv")

    results_df = pd.DataFrame(results)

    # SHAP analysis on best model
    shap_results = {}
    if HAS_SHAP and best_models:
        print("\n  Running SHAP analysis on best models...")
        for eval_time, bm in best_models.items():
            try:
                model = bm['model']
                X_test = bm['X_test']
                features = bm['features']

                # For stacking, use the final model's predict_proba
                if bm['mname'] == 'Stack':
                    # SHAP on the stacking model is complex; use KernelExplainer
                    explainer = shap.KernelExplainer(
                        model.predict_proba, X_test[:50], link='logit')
                    shap_values = explainer.shap_values(X_test[:100])
                elif bm['mname'] == 'XGB':
                    explainer = shap.TreeExplainer(model)
                    shap_values = explainer.shap_values(X_test)
                else:
                    continue

                # Get feature importance from SHAP
                if isinstance(shap_values, list):
                    sv = np.abs(shap_values[1]).mean(axis=0)  # class 1
                else:
                    sv = np.abs(shap_values).mean(axis=0)

                importance = sorted(zip(features, sv), key=lambda x: -x[1])
                shap_results[eval_time] = {
                    'model': bm['mname'],
                    'fset': bm['fset_name'],
                    'auc': bm['auc'],
                    'importance': importance[:20],
                }
                print(f"    T={eval_time} ({bm['mname']}): top features = "
                      f"{[f'{f}:{v:.3f}' for f, v in importance[:5]]}")
            except Exception as e:
                print(f"    SHAP failed for T={eval_time}: {e}")

    return results_df, shap_results


# =========================================================================
# EDGE ANALYSIS — model vs market disagreement
# =========================================================================
def analyze_edge(features_df, ml_results_df):
    """For the best model config, analyze edge when model disagrees with market."""
    if not HAS_SKLEARN or len(ml_results_df) == 0:
        return pd.DataFrame()

    print("\n" + "=" * 70)
    print("EDGE ANALYSIS — MODEL vs MARKET DISAGREEMENT")
    print("=" * 70)

    edge_results = []
    datasets = sorted(features_df['dataset'].unique())

    for eval_time in EVAL_TIMES:
        edf = features_df[features_df['eval_time'] == eval_time].copy()
        if len(edf) < 50:
            continue

        # Find best feature set + model from ML results
        ml_t = ml_results_df[ml_results_df['eval_time'] == eval_time]
        if len(ml_t) == 0:
            continue

        avg_by_config = ml_t.groupby(['features', 'model']).agg(
            avg_auc=('auc', 'mean'),
            avg_acc=('accuracy', 'mean'),
            n_folds=('auc', 'count'),
        ).reset_index()
        avg_by_config = avg_by_config[avg_by_config['n_folds'] >= 3]
        if len(avg_by_config) == 0:
            continue
        best = avg_by_config.sort_values('avg_auc', ascending=False).iloc[0]
        best_fset = best['features']
        best_model_name = best['model']

        avail = [c for c in FEATURE_SETS.get(best_fset, []) if c in edf.columns]
        if len(avail) < 3:
            continue

        print(f"\n  T={eval_time}: best={best_model_name} on '{best_fset}' "
              f"(avg AUC={best['avg_auc']:.3f}, avg acc={best['avg_acc']:.1%})")

        # Re-run LODO with edge tracking
        for test_ds in datasets:
            train = edf[edf['dataset'] != test_ds]
            test = edf[edf['dataset'] == test_ds]

            X_tr = train[avail].dropna()
            y_tr = train.loc[X_tr.index, 'resolution_is_up'].astype(int)
            X_te = test[avail].dropna()
            y_te = test.loc[X_te.index, 'resolution_is_up'].astype(int)

            if len(X_tr) < 30 or len(X_te) < 5:
                continue

            scaler = StandardScaler()
            X_tr_s = scaler.fit_transform(X_tr)
            X_te_s = scaler.transform(X_te)

            if best_model_name == 'XGB' and HAS_XGB:
                model = xgb.XGBClassifier(
                    n_estimators=200, learning_rate=0.1, max_depth=4,
                    reg_alpha=0.5, reg_lambda=1.0,
                    eval_metric='logloss', random_state=42,
                    use_label_encoder=False, verbosity=0, n_jobs=1)
            elif best_model_name == 'RF':
                model = RandomForestClassifier(
                    n_estimators=100, max_depth=5, min_samples_leaf=5,
                    random_state=42, n_jobs=1)
            elif best_model_name == 'LR':
                model = LogisticRegression(max_iter=1000, C=1.0, random_state=42)
            elif best_model_name == 'Stack' and HAS_XGB:
                estimators = [
                    ('xgb', xgb.XGBClassifier(
                        n_estimators=100, learning_rate=0.1, max_depth=4,
                        reg_alpha=0.5, reg_lambda=1.0,
                        eval_metric='logloss', random_state=42,
                        use_label_encoder=False, verbosity=0, n_jobs=1)),
                    ('rf', RandomForestClassifier(
                        n_estimators=50, max_depth=5, min_samples_leaf=5,
                        random_state=42, n_jobs=1)),
                ]
                model = StackingClassifier(
                    estimators=estimators,
                    final_estimator=LogisticRegression(max_iter=1000),
                    cv=3, n_jobs=1)
            else:
                continue

            try:
                model.fit(X_tr_s, y_tr)
                prob = model.predict_proba(X_te_s)[:, 1]
                pred = (prob >= 0.5).astype(int)
            except Exception:
                continue

            # Market prediction: expensive side wins
            test_sub = test.loc[X_te.index]
            market_pred = (test_sub['expensive_side'] == 'UP').astype(int)

            # Agreement / disagreement analysis
            agree_mask = (pred == market_pred.values)
            disagree_mask = ~agree_mask

            n_agree = agree_mask.sum()
            n_disagree = disagree_mask.sum()

            model_acc = accuracy_score(y_te, pred)
            market_acc_fold = accuracy_score(y_te, market_pred.values)

            # When they disagree, who wins?
            if n_disagree > 0:
                model_wins_disagree = accuracy_score(
                    y_te.values[disagree_mask], pred[disagree_mask])
                market_wins_disagree = accuracy_score(
                    y_te.values[disagree_mask], market_pred.values[disagree_mask])
            else:
                model_wins_disagree = np.nan
                market_wins_disagree = np.nan

            # Confidence analysis: model edge = abs(prob - 0.5)
            model_edge = np.abs(prob - 0.5)
            high_conf_mask = model_edge > 0.15  # top confidence predictions
            if high_conf_mask.sum() > 3:
                high_conf_acc = accuracy_score(
                    y_te.values[high_conf_mask], pred[high_conf_mask])
            else:
                high_conf_acc = np.nan

            edge_results.append({
                'eval_time': eval_time,
                'test_ds': test_ds,
                'model': best_model_name,
                'features': best_fset,
                'n_test': len(X_te),
                'model_acc': model_acc,
                'market_acc': market_acc_fold,
                'acc_diff': model_acc - market_acc_fold,
                'n_agree': n_agree,
                'n_disagree': n_disagree,
                'disagree_pct': n_disagree / len(X_te),
                'model_wins_disagree': model_wins_disagree,
                'market_wins_disagree': market_wins_disagree,
                'high_conf_acc': high_conf_acc,
                'mean_edge': model_edge.mean(),
                'median_edge': np.median(model_edge),
            })

    return pd.DataFrame(edge_results)


# =========================================================================
# INDIVIDUAL INDICATOR ANALYSIS (kept from source, OBI removed)
# =========================================================================
def analyze_individual(features_df):
    """Test each indicator for predicting UP resolution."""
    print("\n" + "=" * 70)
    print("INDIVIDUAL INDICATOR ANALYSIS")
    print("=" * 70)

    results = []
    continuous_cols = [
        # EMA position
        'price_vs_ema20_1s', 'price_vs_ema50_1s', 'price_vs_ema200_1s',
        'price_vs_ema20_1m', 'price_vs_ema50_1m', 'price_vs_ema200_1m',
        # MACD
        'macd_histogram_1s', 'macd_histogram_1m',
        # Slopes
        'ema_20_slope_5s', 'ema_20_slope_5m', 'ema_20_slope_20m',
        # RSI (including short)
        'rsi_14_1s', 'rsi_14_1m', 'rsi_2_1m', 'rsi_5_1m', 'rsi_7_1m',
        # Bollinger
        'bb_pct_b_1s', 'bb_pct_b_1m',
        # Stochastic (including short)
        'stoch_k_1s', 'stoch_k_1m', 'stoch_k_5_1m', 'stoch_k_10_1m',
        # ROC
        'roc_10_1s', 'roc_10_1m',
        # ATR
        'atr_14_bps_1s', 'atr_14_bps_1m',
        # Z-scored
        'z_rsi_14_1m', 'z_rsi_2_1m', 'z_rsi_5_1m',
        'z_macd_hist_1m', 'z_bb_pctb_1m', 'z_stoch_k_1m',
        # Interactions
        'rsi14_x_macd', 'rsi2_x_stoch', 'macd_x_atr', 'rsi_squared', 'bb_x_rsi',
        # Regime
        'vol_ratio',
        # Lagged
        'rsi_roc_1m', 'macd_hist_roc_1m',
        # Composite
        'ema_bias_score_1s', 'ema_bias_score_1m',
    ]

    for eval_time in EVAL_TIMES:
        edf = features_df[features_df['eval_time'] == eval_time]
        if len(edf) < 30:
            continue
        y = edf['resolution_is_up'].astype(int)

        for col in continuous_cols:
            if col not in edf.columns:
                continue
            x = edf[col].dropna()
            if len(x) < 30:
                continue
            y_sub = y.loc[x.index]
            try:
                auc = roc_auc_score(y_sub, x)
                r, p = stats.pointbiserialr(y_sub, x)
            except:
                continue
            results.append({
                'eval_time': eval_time, 'indicator': col, 'type': 'continuous',
                'n': len(x), 'auc': auc, 'correlation': r, 'p_value': p,
                'mean_when_up': x[y_sub == 1].mean(), 'mean_when_down': x[y_sub == 0].mean(),
            })

    return pd.DataFrame(results)


# =========================================================================
# MAIN
# =========================================================================
def main():
    print("=" * 70)
    print("SIGNAL COMBINATION STUDY — PHOENIX V2 Phase 2")
    print(f"EMAs: {EMA_PERIODS} on 1s+1m | MACD {MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL}")
    print(f"Eval times: T={EVAL_TIMES} | Datasets: {list(DATASETS.keys())}")
    print(f"NEW: Short RSI (2,5,7), Short Stoch (5,10), Z-scores, Interactions")
    print(f"ML: LogReg, XGBoost, RF, Stacking | Validation: LODO")
    print("=" * 70)

    all_features = []

    for ds_key, ds_config in DATASETS.items():
        print(f"\n{'=' * 60}")
        print(f"DATASET: {ds_config['name']}")
        print(f"{'=' * 60}")

        obs_df, resolutions = load_dataset(ds_key)
        if obs_df is None or len(resolutions) == 0:
            print(f"  SKIPPING — no data")
            continue

        ind_1s, ind_1m = load_hf_and_compute_indicators(ds_config['hf_file'])
        if ind_1s is None:
            print(f"  SKIPPING — no HF data")
            continue

        features = extract_all_features(obs_df, resolutions, ind_1s, ind_1m, ds_key)
        print(f"  -> {len(features)} observations extracted")

        if len(features) > 0:
            features.to_csv(OUTPUT_DIR / f"combo_features_{ds_key}.csv", index=False)
            all_features.append(features)

        # Free memory
        del ind_1s, ind_1m, obs_df

    if not all_features:
        print("\nNo data! Check paths.")
        return

    combined = pd.concat(all_features, ignore_index=True)
    combined.to_csv(OUTPUT_DIR / "combo_features_combined.csv", index=False)
    n_markets = combined['slug'].nunique()
    print(f"\nCOMBINED: {len(combined)} obs, {n_markets} markets, "
          f"{combined['dataset'].nunique()} datasets")

    # Per-dataset summary
    for ds in combined['dataset'].unique():
        ds_df = combined[combined['dataset'] == ds]
        mkt_acc = ds_df['market_correct'].mean()
        print(f"  {ds}: {ds_df['slug'].nunique()} markets, {len(ds_df)} obs, "
              f"market acc={mkt_acc:.1%}")

    # Analysis 1: Individual indicators
    r1 = analyze_individual(combined)
    r1.to_csv(OUTPUT_DIR / "combo_individual.csv", index=False)

    # Analysis 2: ML combination
    r2, shap_results = analyze_ml_v2(combined)
    if len(r2) > 0:
        r2.to_csv(OUTPUT_DIR / "combo_ml_results.csv", index=False)

    # Save SHAP results
    if shap_results:
        shap_summary = {}
        for t, sr in shap_results.items():
            shap_summary[str(t)] = {
                'model': sr['model'],
                'fset': sr['fset'],
                'auc': sr['auc'],
                'top_features': [(f, float(v)) for f, v in sr['importance']],
            }
        with open(OUTPUT_DIR / "combo_shap_summary.json", 'w') as f:
            json.dump(shap_summary, f, indent=2)

    # Analysis 3: Edge analysis
    r3 = pd.DataFrame()
    if len(r2) > 0:
        r3 = analyze_edge(combined, r2)
        if len(r3) > 0:
            r3.to_csv(OUTPUT_DIR / "combo_edge_analysis.csv", index=False)

    # =====================================================================
    # SUMMARY
    # =====================================================================
    print("\n" + "=" * 70)
    print("KEY FINDINGS")
    print("=" * 70)

    # Individual indicators
    if len(r1) > 0:
        print("\n--- TOP INDICATORS by AUC (all eval times) ---")
        for eval_time in EVAL_TIMES:
            t_df = r1[r1['eval_time'] == eval_time].sort_values('auc', ascending=False)
            if len(t_df) > 0:
                print(f"\n  T={eval_time}:")
                for _, row in t_df.head(8).iterrows():
                    print(f"    {row['indicator']:30s} AUC={row['auc']:.3f} "
                          f"r={row['correlation']:.3f} p={row['p_value']:.4f}")

    # ML results
    if len(r2) > 0:
        print("\n--- ML RESULTS (avg across LODO folds) ---")
        for eval_time in EVAL_TIMES:
            ml_t = r2[r2['eval_time'] == eval_time]
            if len(ml_t) == 0:
                continue
            avg = ml_t.groupby(['features', 'model']).agg(
                avg_auc=('auc', 'mean'),
                std_auc=('auc', 'std'),
                avg_acc=('accuracy', 'mean'),
                avg_vs_mkt=('acc_vs_market', 'mean'),
                n_folds=('auc', 'count'),
            ).reset_index()
            avg = avg[avg['n_folds'] >= 3].sort_values('avg_auc', ascending=False)
            print(f"\n  T={eval_time}:")
            market_acc = features_df_market_acc(combined, eval_time)
            print(f"    Market baseline: {market_acc:.1%}")
            for _, row in avg.head(10).iterrows():
                print(f"    {row['features']:25s} {row['model']:5s}: "
                      f"AUC={row['avg_auc']:.3f}+/-{row['std_auc']:.3f} "
                      f"acc={row['avg_acc']:.1%} vs_mkt={row['avg_vs_mkt']:+.1%} "
                      f"(n={row['n_folds']:.0f})")

    # Edge analysis
    if len(r3) > 0:
        print("\n--- EDGE ANALYSIS (model vs market disagreement) ---")
        for eval_time in EVAL_TIMES:
            edge_t = r3[r3['eval_time'] == eval_time]
            if len(edge_t) == 0:
                continue
            avg_model_wins = edge_t['model_wins_disagree'].dropna().mean()
            avg_disagree_pct = edge_t['disagree_pct'].mean()
            avg_acc_diff = edge_t['acc_diff'].mean()
            print(f"\n  T={eval_time}:")
            print(f"    Avg disagree rate: {avg_disagree_pct:.1%}")
            print(f"    Model wins when disagree: {avg_model_wins:.1%}")
            print(f"    Model acc - Market acc: {avg_acc_diff:+.1%}")
            for _, row in edge_t.iterrows():
                print(f"      {row['test_ds']:10s}: model={row['model_acc']:.1%} "
                      f"mkt={row['market_acc']:.1%} diff={row['acc_diff']:+.1%} "
                      f"disagree={row['n_disagree']}/{row['n_test']} "
                      f"model_wins={row['model_wins_disagree']:.1%}" if not pd.isna(row['model_wins_disagree']) else
                      f"      {row['test_ds']:10s}: model={row['model_acc']:.1%} "
                      f"mkt={row['market_acc']:.1%} diff={row['acc_diff']:+.1%} "
                      f"disagree={row['n_disagree']}/{row['n_test']}")

    # Success criteria check
    print("\n" + "=" * 70)
    print("SUCCESS CRITERIA CHECK")
    print("=" * 70)
    if len(r2) > 0:
        for eval_time in EVAL_TIMES:
            ml_t = r2[r2['eval_time'] == eval_time]
            if len(ml_t) == 0:
                continue
            avg = ml_t.groupby(['features', 'model']).agg(
                avg_auc=('auc', 'mean'),
                avg_acc=('accuracy', 'mean'),
                n_folds=('auc', 'count'),
            ).reset_index()
            avg = avg[avg['n_folds'] >= 3]
            if len(avg) == 0:
                continue
            best = avg.sort_values('avg_auc', ascending=False).iloc[0]
            market_acc = features_df_market_acc(combined, eval_time)

            auc_pass = best['avg_auc'] > 0.85
            acc_pass = best['avg_acc'] > 0.87
            beat_mkt = best['avg_acc'] > market_acc

            print(f"\n  T={eval_time} (best: {best['model']} on {best['features']}):")
            print(f"    [{'PASS' if auc_pass else 'FAIL'}] AUC > 0.85: {best['avg_auc']:.3f}")
            print(f"    [{'PASS' if acc_pass else 'FAIL'}] Accuracy > 87%: {best['avg_acc']:.1%}")
            print(f"    [{'PASS' if beat_mkt else 'FAIL'}] Beat market ({market_acc:.1%}): {best['avg_acc']:.1%}")

            if len(r3) > 0:
                edge_t = r3[r3['eval_time'] == eval_time]
                if len(edge_t) > 0:
                    mwd = edge_t['model_wins_disagree'].dropna().mean()
                    edge_pass = mwd > 0.55
                    print(f"    [{'PASS' if edge_pass else 'FAIL'}] Model wins >55% when disagree: {mwd:.1%}")

    print(f"\nAll results saved to: {OUTPUT_DIR}")


def features_df_market_acc(combined, eval_time):
    """Helper to compute market accuracy for a given eval time."""
    edf = combined[combined['eval_time'] == eval_time]
    return edf['market_correct'].mean() if len(edf) > 0 else 0.0


if __name__ == "__main__":
    main()
