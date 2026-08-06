#!/usr/bin/env python3
"""
EMA / MACD / OBI Indicator Research Study for Polymarket Binary Markets
=======================================================================

COPIED FROM: v2_comprehensive_signal_study.py (data loading infrastructure)

Tests:
  1. EMA 20/50/100/200 on 1-second BTC candles
  2. EMA 20/50/100/200 on 1-minute BTC candles
  3. Multi-timeframe alignment (1s EMAs vs 1m EMAs)
  4. MACD (12/26/9) on 1s and 1m timeframes
  5. EMA slope / momentum decay detection
  6. OBI (orderbook imbalance) from L5 depth
  7. Combined ML analysis (logistic regression + random forest)

Datasets: OOS7, OOS8, OOS9 (Gen3 observer + matching HF Binance data)
"""

import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, Tuple, Optional
import warnings
from scipy import stats
from tqdm import tqdm

try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import roc_auc_score
    from sklearn.preprocessing import StandardScaler
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

warnings.filterwarnings('ignore')

BASE_DIR = Path("/Users/rananjaybika/polymarket-amm-bot")
OUTPUT_DIR = BASE_DIR / "research" / "signal_research" / "results"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# =========================================================================
# DATASETS — only those with BOTH HF Binance data AND Gen3 OBI
# Copied from v2_comprehensive_signal_study.py
# =========================================================================
DATASETS = {
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
}

EMA_PERIODS = [20, 50, 100, 200]
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
EVAL_TIMES = [600, 300, 120]


# =========================================================================
# DATA LOADING — copied from v2_comprehensive_signal_study.py (validated)
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
# INDICATOR HELPERS
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
    ind_df[f'rsi_14'] = compute_rsi(close, 14)

    # Bollinger Bands (20, 2σ)
    pct_b, bw = compute_bollinger(close, 20, 2)
    ind_df[f'bb_pct_b'] = pct_b
    ind_df[f'bb_bandwidth'] = bw

    # Stochastic (14/3)
    k, d = compute_stochastic(high, low, close, 14, 3)
    ind_df[f'stoch_k'] = k
    ind_df[f'stoch_d'] = d

    # ROC (10-period, as %)
    ind_df[f'roc_10'] = close.pct_change(10) * 100

    # ATR (14) — normalized to bps for cross-price comparison
    atr = compute_atr(high, low, close, 14)
    ind_df[f'atr_14'] = atr
    ind_df[f'atr_14_bps'] = atr / close * 10000

    return ind_df


# =========================================================================
# HF DATA LOADING + INDICATOR COMPUTATION
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

    print(f"    1s candles: {len(ind_1s):,}, 1m candles: {len(ind_1m):,}")
    return ind_1s, ind_1m


def get_indicators_at_time(ind_1s, ind_1m, ts_ms):
    """Look up all indicator values at a specific timestamp."""
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
    # New indicators (1s)
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
    # New indicators (1m)
    for col in ['rsi_14', 'bb_pct_b', 'bb_bandwidth', 'stoch_k', 'stoch_d', 'roc_10', 'atr_14_bps']:
        result[f'{col}_1m'] = r1m.get(col, np.nan)

    # Multi-TF delta
    for p in EMA_PERIODS:
        result[f'ema_{p}_1s_vs_1m'] = result[f'ema_{p}_1s'] - result[f'ema_{p}_1m']

    return result


# =========================================================================
# PER-MARKET FEATURE EXTRACTION
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
            cheap_side = "DOWN" if ua >= da else "UP"
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

            # OBI from observer
            up_imb = obs_row.get('up_imbalance', np.nan)
            down_imb = obs_row.get('down_imbalance', np.nan)
            if not pd.isna(up_imb) and not pd.isna(down_imb):
                row['up_imbalance'] = float(up_imb)
                row['down_imbalance'] = float(down_imb)
                row['obi_bias_up'] = float(up_imb) - float(down_imb)

            row.update(indicators)

            # --- Derived bias signals ---
            btc = indicators['btc_price']

            # EMA bias score: fraction of EMAs below price (bullish)
            n_above_1s = sum(1 for p in EMA_PERIODS if btc > indicators[f'ema_{p}_1s'])
            n_above_1m = sum(1 for p in EMA_PERIODS if btc > indicators[f'ema_{p}_1m'])
            row['ema_bias_score_1s'] = n_above_1s / len(EMA_PERIODS)
            row['ema_bias_score_1m'] = n_above_1m / len(EMA_PERIODS)

            # EMA alignment (20 > 50 > 100 > 200 = bullish)
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
            row['multi_tf_bias'] = bias_1s if bias_1s == bias_1m else "MIXED"

            # MACD direction
            row['macd_bull_1s'] = indicators['macd_histogram_1s'] > 0
            row['macd_bull_1m'] = indicators['macd_histogram_1m'] > 0
            row['macd_cross_1s'] = indicators['macd_line_1s'] > indicators['macd_signal_1s']
            row['macd_cross_1m'] = indicators['macd_line_1m'] > indicators['macd_signal_1m']

            # EMA slowing down: recent slope magnitude < longer slope magnitude
            s5 = indicators.get('ema_20_slope_5s', 0) or 0
            s20 = indicators.get('ema_20_slope_20s', 0) or 0
            row['ema20_slowing_1s'] = abs(s5) < abs(s20) if s20 != 0 else False

            s5m = indicators.get('ema_20_slope_5m')
            s20m = indicators.get('ema_20_slope_20m')
            if s5m is not None and s20m is not None and not pd.isna(s5m) and not pd.isna(s20m) and s20m != 0:
                row['ema20_slowing_1m'] = abs(s5m) < abs(s20m)
            else:
                row['ema20_slowing_1m'] = False

            # RSI-derived signals
            rsi_1s = indicators.get('rsi_14_1s', 50)
            rsi_1m = indicators.get('rsi_14_1m', 50)
            row['rsi_bull_1s'] = rsi_1s > 50 if not pd.isna(rsi_1s) else False
            row['rsi_bull_1m'] = rsi_1m > 50 if not pd.isna(rsi_1m) else False
            row['rsi_overbought_1s'] = rsi_1s > 70 if not pd.isna(rsi_1s) else False
            row['rsi_oversold_1s'] = rsi_1s < 30 if not pd.isna(rsi_1s) else False
            row['rsi_overbought_1m'] = rsi_1m > 70 if not pd.isna(rsi_1m) else False
            row['rsi_oversold_1m'] = rsi_1m < 30 if not pd.isna(rsi_1m) else False

            # Bollinger Band position
            bb_1s = indicators.get('bb_pct_b_1s', 0.5)
            bb_1m = indicators.get('bb_pct_b_1m', 0.5)
            row['bb_upper_1s'] = bb_1s > 1.0 if not pd.isna(bb_1s) else False
            row['bb_lower_1s'] = bb_1s < 0.0 if not pd.isna(bb_1s) else False
            row['bb_upper_1m'] = bb_1m > 1.0 if not pd.isna(bb_1m) else False
            row['bb_lower_1m'] = bb_1m < 0.0 if not pd.isna(bb_1m) else False

            # Stochastic overbought/oversold
            stoch_1s = indicators.get('stoch_k_1s', 50)
            stoch_1m = indicators.get('stoch_k_1m', 50)
            row['stoch_overbought_1s'] = stoch_1s > 80 if not pd.isna(stoch_1s) else False
            row['stoch_oversold_1s'] = stoch_1s < 20 if not pd.isna(stoch_1s) else False
            row['stoch_overbought_1m'] = stoch_1m > 80 if not pd.isna(stoch_1m) else False
            row['stoch_oversold_1m'] = stoch_1m < 20 if not pd.isna(stoch_1m) else False

            # Composite vote (now includes RSI, Stochastic)
            votes_up = 0
            votes_total = 6  # EMA 1s, EMA 1m, MACD 1s, MACD 1m, RSI 1s, RSI 1m
            if row['ema_bias_score_1s'] > 0.5: votes_up += 1
            if row['ema_bias_score_1m'] > 0.5: votes_up += 1
            if row['macd_bull_1s']: votes_up += 1
            if row['macd_bull_1m']: votes_up += 1
            if row['rsi_bull_1s']: votes_up += 1
            if row['rsi_bull_1m']: votes_up += 1
            if 'obi_bias_up' in row and not pd.isna(row.get('obi_bias_up')):
                votes_total += 1
                if row['obi_bias_up'] > 0: votes_up += 1
            row['composite_up_pct'] = votes_up / votes_total

            all_rows.append(row)

    return pd.DataFrame(all_rows)


# =========================================================================
# ANALYSIS FUNCTIONS
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
        'ema20_vs_ema50_1s', 'ema50_vs_ema200_1s',
        'price_vs_ema20_1m', 'price_vs_ema50_1m', 'price_vs_ema200_1m',
        'ema20_vs_ema50_1m', 'ema50_vs_ema200_1m',
        # Multi-TF
        'ema_20_1s_vs_1m', 'ema_50_1s_vs_1m', 'ema_200_1s_vs_1m',
        # MACD
        'macd_histogram_1s', 'macd_histogram_1m', 'macd_line_1s', 'macd_line_1m',
        # Slopes
        'ema_20_slope_5s', 'ema_20_slope_20s', 'ema_50_slope_5s',
        'ema_20_slope_5m', 'ema_20_slope_20m', 'ema_50_slope_5m',
        # RSI
        'rsi_14_1s', 'rsi_14_1m',
        # Bollinger
        'bb_pct_b_1s', 'bb_pct_b_1m', 'bb_bandwidth_1s', 'bb_bandwidth_1m',
        # Stochastic
        'stoch_k_1s', 'stoch_d_1s', 'stoch_k_1m', 'stoch_d_1m',
        # ROC
        'roc_10_1s', 'roc_10_1m',
        # ATR (volatility)
        'atr_14_bps_1s', 'atr_14_bps_1m',
        # Composite / bias scores
        'ema_bias_score_1s', 'ema_bias_score_1m', 'composite_up_pct',
        # OBI
        'obi_bias_up',
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

        # Binary indicators
        for col, up_when_true in [
            ('ema_aligned_bull_1s', True), ('ema_aligned_bear_1s', False),
            ('ema_aligned_bull_1m', True), ('ema_aligned_bear_1m', False),
            ('macd_bull_1s', True), ('macd_bull_1m', True),
            ('macd_cross_1s', True), ('macd_cross_1m', True),
            ('rsi_bull_1s', True), ('rsi_bull_1m', True),
            ('rsi_overbought_1s', None), ('rsi_oversold_1s', None),
            ('rsi_overbought_1m', None), ('rsi_oversold_1m', None),
            ('bb_upper_1s', None), ('bb_lower_1s', None),
            ('bb_upper_1m', None), ('bb_lower_1m', None),
            ('stoch_overbought_1s', None), ('stoch_oversold_1s', None),
            ('stoch_overbought_1m', None), ('stoch_oversold_1m', None),
            ('ema20_slowing_1s', None), ('ema20_slowing_1m', None),
            ('multi_tf_agree', None),
        ]:
            if col not in edf.columns:
                continue
            valid = edf.dropna(subset=[col])
            if len(valid) < 30:
                continue
            x = valid[col].astype(bool)
            y_sub = valid['resolution_is_up']
            try:
                ct = pd.crosstab(x, y_sub)
                chi2, p, _, _ = stats.chi2_contingency(ct)
            except:
                chi2, p = 0, 1
            up_when_true_rate = y_sub[x].mean() if x.sum() > 0 else np.nan
            up_when_false_rate = y_sub[~x].mean() if (~x).sum() > 0 else np.nan
            results.append({
                'eval_time': eval_time, 'indicator': col, 'type': 'binary',
                'n': len(valid), 'chi2': chi2, 'p_value': p,
                'true_pct': x.mean(),
                'up_rate_when_true': up_when_true_rate,
                'up_rate_when_false': up_when_false_rate,
            })

    return pd.DataFrame(results)


def analyze_vs_market(features_df):
    """Do indicators add value beyond market price?"""
    print("\n" + "=" * 70)
    print("INDICATORS vs MARKET PRICE")
    print("=" * 70)

    results = []
    for eval_time in EVAL_TIMES:
        edf = features_df[features_df['eval_time'] == eval_time]
        if len(edf) < 30:
            continue

        market_acc = edf['market_correct'].mean()

        for name, col, dir_fn in [
            ('ema_1s', 'ema_bias_score_1s', lambda x: 'UP' if x > 0.5 else 'DOWN'),
            ('ema_1m', 'ema_bias_score_1m', lambda x: 'UP' if x > 0.5 else 'DOWN'),
            ('macd_1s', 'macd_histogram_1s', lambda x: 'UP' if x > 0 else 'DOWN'),
            ('macd_1m', 'macd_histogram_1m', lambda x: 'UP' if x > 0 else 'DOWN'),
            ('rsi_1s', 'rsi_14_1s', lambda x: 'UP' if x > 50 else 'DOWN'),
            ('rsi_1m', 'rsi_14_1m', lambda x: 'UP' if x > 50 else 'DOWN'),
            ('stoch_1s', 'stoch_k_1s', lambda x: 'UP' if x > 50 else 'DOWN'),
            ('roc_1s', 'roc_10_1s', lambda x: 'UP' if x > 0 else 'DOWN'),
            ('roc_1m', 'roc_10_1m', lambda x: 'UP' if x > 0 else 'DOWN'),
            ('composite', 'composite_up_pct', lambda x: 'UP' if x > 0.5 else 'DOWN'),
        ]:
            if col not in edf.columns:
                continue
            valid = edf.dropna(subset=[col])
            if len(valid) < 30:
                continue

            ind_dir = valid[col].apply(dir_fn)
            ind_acc = (ind_dir == valid['resolution']).mean()

            agree = valid[ind_dir == valid['expensive_side']]
            disagree = valid[ind_dir != valid['expensive_side']]

            dis_market_right = np.nan
            dis_ind_right = np.nan
            if len(disagree) > 5:
                dis_market_right = (disagree['expensive_side'] == disagree['resolution']).mean()
                dis_ind_right = (ind_dir[disagree.index] == disagree['resolution']).mean()

            results.append({
                'eval_time': eval_time, 'indicator': name,
                'n': len(valid), 'market_acc': market_acc, 'indicator_acc': ind_acc,
                'diff': ind_acc - market_acc,
                'n_agree': len(agree), 'n_disagree': len(disagree),
                'disagree_market_wins': dis_market_right,
                'disagree_indicator_wins': dis_ind_right,
            })

    return pd.DataFrame(results)


def analyze_momentum(features_df):
    """EMA slope / slowing down analysis."""
    print("\n" + "=" * 70)
    print("EMA MOMENTUM / SLOWING DOWN")
    print("=" * 70)

    results = []
    for eval_time in EVAL_TIMES:
        edf = features_df[features_df['eval_time'] == eval_time]
        if len(edf) < 30:
            continue

        for tf in ['1s', '1m']:
            col = f'ema20_slowing_{tf}'
            if col not in edf.columns:
                continue
            slowing = edf[edf[col] == True]
            not_slowing = edf[edf[col] == False]
            if len(slowing) < 5 or len(not_slowing) < 5:
                continue

            results.append({
                'eval_time': eval_time, 'timeframe': tf,
                'n_slowing': len(slowing), 'n_not_slowing': len(not_slowing),
                'slowing_market_acc': slowing['market_correct'].mean(),
                'not_slowing_market_acc': not_slowing['market_correct'].mean(),
                'diff': not_slowing['market_correct'].mean() - slowing['market_correct'].mean(),
            })

        # Slope direction
        for slope_col in ['ema_20_slope_5s', 'ema_20_slope_20s', 'ema_50_slope_5s',
                          'ema_20_slope_5m', 'ema_20_slope_20m']:
            if slope_col not in edf.columns:
                continue
            valid = edf.dropna(subset=[slope_col])
            pos = valid[valid[slope_col] > 0]
            neg = valid[valid[slope_col] < 0]
            if len(pos) < 5 or len(neg) < 5:
                continue
            results.append({
                'eval_time': eval_time, 'slope': slope_col,
                'n_pos': len(pos), 'n_neg': len(neg),
                'pos_up_rate': pos['resolution_is_up'].mean(),
                'neg_up_rate': neg['resolution_is_up'].mean(),
                'pos_market_acc': pos['market_correct'].mean(),
                'neg_market_acc': neg['market_correct'].mean(),
            })

    return pd.DataFrame(results)


def analyze_obi(features_df):
    """OBI standalone and combined with EMAs."""
    print("\n" + "=" * 70)
    print("OBI ANALYSIS")
    print("=" * 70)

    results = []
    for eval_time in EVAL_TIMES:
        edf = features_df[features_df['eval_time'] == eval_time]
        obi = edf.dropna(subset=['obi_bias_up'])
        if len(obi) < 20:
            continue

        y = obi['resolution_is_up'].astype(int)
        x = obi['obi_bias_up']
        try:
            auc = roc_auc_score(y, x)
            r, p = stats.pointbiserialr(y, x)
        except:
            auc, r, p = 0.5, 0, 1

        results.append({
            'eval_time': eval_time, 'analysis': 'standalone',
            'n': len(obi), 'auc': auc, 'r': r, 'p': p,
        })

        # OBI + EMA combo
        obi_bull = obi['obi_bias_up'] > 0
        ema_bull = obi['ema_bias_score_1m'] > 0.5
        for label, mask in [
            ('both_bull', obi_bull & ema_bull),
            ('both_bear', ~obi_bull & ~ema_bull),
            ('disagree', obi_bull != ema_bull),
        ]:
            subset = obi[mask]
            if len(subset) < 5:
                continue
            results.append({
                'eval_time': eval_time, 'analysis': f'obi_ema_{label}',
                'n': len(subset),
                'up_rate': subset['resolution_is_up'].mean(),
                'market_acc': subset['market_correct'].mean(),
            })

    return pd.DataFrame(results)


def analyze_ml(features_df):
    """ML: logistic regression + random forest on feature groups."""
    if not HAS_SKLEARN:
        print("  sklearn not available")
        return pd.DataFrame()

    print("\n" + "=" * 70)
    print("ML COMBINATION ANALYSIS")
    print("=" * 70)

    feature_groups = {
        'ema_1s': ['price_vs_ema20_1s', 'price_vs_ema50_1s', 'price_vs_ema200_1s',
                    'ema20_vs_ema50_1s', 'ema50_vs_ema200_1s'],
        'ema_1m': ['price_vs_ema20_1m', 'price_vs_ema50_1m', 'price_vs_ema200_1m',
                    'ema20_vs_ema50_1m', 'ema50_vs_ema200_1m'],
        'macd': ['macd_histogram_1s', 'macd_histogram_1m', 'macd_line_1s', 'macd_line_1m'],
        'slopes': ['ema_20_slope_5s', 'ema_20_slope_20s', 'ema_50_slope_5s',
                    'ema_20_slope_5m', 'ema_20_slope_20m', 'ema_50_slope_5m'],
        'multi_tf': ['ema_20_1s_vs_1m', 'ema_50_1s_vs_1m', 'ema_200_1s_vs_1m'],
        'rsi': ['rsi_14_1s', 'rsi_14_1m'],
        'bollinger': ['bb_pct_b_1s', 'bb_pct_b_1m', 'bb_bandwidth_1s', 'bb_bandwidth_1m'],
        'stochastic': ['stoch_k_1s', 'stoch_d_1s', 'stoch_k_1m', 'stoch_d_1m'],
        'roc': ['roc_10_1s', 'roc_10_1m'],
        'volatility': ['atr_14_bps_1s', 'atr_14_bps_1m', 'bb_bandwidth_1s', 'bb_bandwidth_1m'],
        'obi': ['obi_bias_up'],
        'momentum_all': ['macd_histogram_1s', 'macd_histogram_1m', 'rsi_14_1s', 'rsi_14_1m',
                         'stoch_k_1s', 'stoch_k_1m', 'roc_10_1s', 'roc_10_1m'],
    }
    all_ind = []
    for v in feature_groups.values():
        all_ind.extend(v)
    feature_groups['all_indicators'] = all_ind
    feature_groups['all_plus_market'] = all_ind + ['up_ask', 'down_ask']

    results = []
    datasets = features_df['dataset'].unique()

    for eval_time in EVAL_TIMES:
        edf = features_df[features_df['eval_time'] == eval_time]
        if len(edf) < 50:
            continue

        for gname, gcols in tqdm(feature_groups.items(), desc=f"  ML T={eval_time}"):
            avail = [c for c in gcols if c in edf.columns]
            if not avail:
                continue

            for test_ds in datasets:
                train = edf[edf['dataset'] != test_ds]
                test = edf[edf['dataset'] == test_ds]

                X_tr = train[avail].dropna()
                y_tr = train.loc[X_tr.index, 'resolution_is_up'].astype(int)
                X_te = test[avail].dropna()
                y_te = test.loc[X_te.index, 'resolution_is_up'].astype(int)

                if len(X_tr) < 30 or len(X_te) < 10:
                    continue

                scaler = StandardScaler()
                X_tr_s = scaler.fit_transform(X_tr)
                X_te_s = scaler.transform(X_te)

                for mname, model in [
                    ('LR', LogisticRegression(max_iter=1000, random_state=42)),
                    ('RF', RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42)),
                ]:
                    try:
                        model.fit(X_tr_s, y_tr)
                        prob = model.predict_proba(X_te_s)[:, 1]
                        auc = roc_auc_score(y_te, prob)
                        acc = model.score(X_te_s, y_te)
                    except:
                        continue
                    results.append({
                        'eval_time': eval_time, 'features': gname,
                        'model': mname, 'test_ds': test_ds,
                        'train_n': len(X_tr), 'test_n': len(X_te),
                        'auc': auc, 'accuracy': acc,
                    })

    return pd.DataFrame(results)


# =========================================================================
# MAIN
# =========================================================================
def main():
    print("=" * 70)
    print("EMA / MACD / OBI INDICATOR RESEARCH")
    print(f"EMAs: {EMA_PERIODS} on 1s and 1m | MACD {MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL}")
    print(f"Eval times: T={EVAL_TIMES} | Datasets: {list(DATASETS.keys())}")
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

        features.to_csv(OUTPUT_DIR / f"ema_features_{ds_key}.csv", index=False)
        all_features.append(features)

        del ind_1s, ind_1m, obs_df

    if not all_features:
        print("\nNo data! Check paths.")
        return

    combined = pd.concat(all_features, ignore_index=True)
    combined.to_csv(OUTPUT_DIR / "ema_features_combined.csv", index=False)
    n_markets = combined['slug'].nunique()
    print(f"\nCOMBINED: {len(combined)} obs, {n_markets} markets, "
          f"{combined['dataset'].nunique()} datasets")

    # Analyses
    r1 = analyze_individual(combined)
    r1.to_csv(OUTPUT_DIR / "ema_individual.csv", index=False)

    r2 = analyze_vs_market(combined)
    r2.to_csv(OUTPUT_DIR / "ema_vs_market.csv", index=False)

    r3 = analyze_momentum(combined)
    r3.to_csv(OUTPUT_DIR / "ema_momentum.csv", index=False)

    r4 = analyze_obi(combined)
    r4.to_csv(OUTPUT_DIR / "ema_obi.csv", index=False)

    r5 = analyze_ml(combined)
    r5.to_csv(OUTPUT_DIR / "ema_ml.csv", index=False)

    # === SUMMARY ===
    print("\n" + "=" * 70)
    print("KEY FINDINGS")
    print("=" * 70)

    if len(r1) > 0:
        cont = r1[r1['type'] == 'continuous']
        if len(cont) > 0:
            print("\n--- TOP INDICATORS by AUC (T=300) ---")
            t300 = cont[cont['eval_time'] == 300].sort_values('auc', ascending=False)
            for _, row in t300.head(10).iterrows():
                print(f"  {row['indicator']:30s} AUC={row['auc']:.3f} r={row['correlation']:.3f} "
                      f"p={row['p_value']:.4f} n={row['n']:.0f}")

    if len(r2) > 0:
        print("\n--- INDICATORS vs MARKET (T=300) ---")
        t300 = r2[r2['eval_time'] == 300]
        for _, row in t300.iterrows():
            print(f"  {row['indicator']:15s}: ind={row['indicator_acc']:.1%} mkt={row['market_acc']:.1%} "
                  f"diff={row['diff']:+.1%} disagree={row['n_disagree']:.0f} "
                  f"(mkt wins {row['disagree_market_wins']:.1%})")

    if len(r5) > 0:
        print("\n--- ML RESULTS (avg AUC, T=300) ---")
        ml_avg = r5[r5['eval_time'] == 300].groupby(['features', 'model']).agg(
            avg_auc=('auc', 'mean'), n=('auc', 'count')).reset_index()
        ml_avg = ml_avg.sort_values('avg_auc', ascending=False)
        for _, row in ml_avg.head(8).iterrows():
            print(f"  {row['features']:25s} {row['model']}: AUC={row['avg_auc']:.3f} (n={row['n']:.0f})")

    print(f"\nResults saved to: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
