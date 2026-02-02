#!/usr/bin/env python3
"""
Imbalance Predictor Model - V2

Goal: Build 70% accurate winner prediction model
Using only features available in all datasets.

Common features (IS+OOS2 and OOS7):
- Price: up_bid, up_ask, down_bid, down_ask, pair_cost
- BTC: binance_price, velocity_bps
- Time: time_remaining_secs
- Spike: spike_detected, spike_direction, spike_magnitude

Training: IS+OOS2 (Jan 16-19)
Validation: OOS7 (Jan 29-30)
"""

import pandas as pd
import numpy as np
from pathlib import Path
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, roc_auc_score, classification_report
import xgboost as xgb
from tqdm import tqdm
import warnings
warnings.filterwarnings('ignore')

BASE_DIR = Path("/Users/rananjaybika/polymarket-amm-bot")


def load_observer_data(period="train"):
    """Load observer data for specified period."""
    if period == "train":
        files = [
            "research/observer/grid_obs_20260116.csv",
            "research/observer/grid_obs_20260117.csv",
            "research/observer/grid_obs_20260118.csv",
            "research/observer/grid_obs_20260119.csv",
        ]
    else:  # val
        files = [
            "research/observer/grid_obs_20260129.csv",
            "research/observer/grid_obs_20260130.csv",
        ]

    dfs = []
    for f in files:
        path = BASE_DIR / f
        if path.exists():
            df = pd.read_csv(path, low_memory=False)
            dfs.append(df)
            print(f"  Loaded: {path.name} ({len(df):,} rows)")

    if dfs:
        return pd.concat(dfs, ignore_index=True)
    return None


def load_resolutions():
    """Load market resolution data."""
    res_path = BASE_DIR / "research/observer/market_resolutions_verified.csv"
    if res_path.exists():
        df = pd.read_csv(res_path)
        return dict(zip(df['slug'], df['winner']))
    return {}


def compute_features(df):
    """
    Compute features using only common columns.
    """
    features = pd.DataFrame(index=df.index)

    # ===== 1. PRICE FEATURES (the baseline) =====
    features['up_ask'] = df['up_ask']
    features['down_ask'] = df['down_ask']
    features['up_bid'] = df['up_bid']
    features['down_bid'] = df['down_bid']

    # Derived price features
    features['ask_diff'] = df['up_ask'] - df['down_ask']
    features['bid_diff'] = df['up_bid'] - df['down_bid']
    features['pair_cost'] = df['up_ask'] + df['down_ask']
    features['up_is_expensive'] = (df['up_ask'] > df['down_ask']).astype(float)

    # Mid prices
    features['up_mid'] = (df['up_ask'] + df['up_bid']) / 2
    features['down_mid'] = (df['down_ask'] + df['down_bid']) / 2
    features['mid_diff'] = features['up_mid'] - features['down_mid']

    # Spreads (proxy for orderbook tightness)
    features['up_spread'] = df['up_ask'] - df['up_bid']
    features['down_spread'] = df['down_ask'] - df['down_bid']
    features['spread_ratio'] = features['up_spread'] / (features['down_spread'] + 0.001)

    # Price ratios
    features['up_down_ask_ratio'] = df['up_ask'] / (df['down_ask'] + 0.001)
    features['up_down_bid_ratio'] = df['up_bid'] / (df['down_bid'] + 0.001)

    # ===== 2. VELOCITY FEATURES =====
    features['velocity'] = df['velocity_bps']
    features['velocity_abs'] = df['velocity_bps'].abs()
    features['velocity_positive'] = (df['velocity_bps'] > 0).astype(float)

    # Rolling velocity (within market)
    for window in [5, 10, 20]:
        features[f'velocity_ma{window}'] = df.groupby('market_slug')['velocity_bps'].transform(
            lambda x: x.rolling(window, min_periods=1).mean()
        )

    # ===== 3. BTC MOMENTUM =====
    # BTC price changes
    for window in [5, 10, 25, 50]:
        features[f'btc_ret_{window}'] = df.groupby('market_slug')['binance_price'].transform(
            lambda x: x.pct_change(window)
        )

    # BTC vs velocity alignment
    features['btc_vel_aligned'] = (
        (features['velocity'] > 0) & (features.get('btc_ret_5', 0) > 0)
    ).astype(float)

    # ===== 4. TIME FEATURES =====
    features['time_remaining'] = df['time_remaining_secs']
    features['time_urgency'] = 1 / (df['time_remaining_secs'] + 1)
    features['time_pct'] = df['time_remaining_secs'] / 900  # Normalize to 15 min

    # ===== 5. SPIKE FEATURES =====
    if 'spike_detected' in df.columns:
        features['spike_detected'] = df['spike_detected'].fillna(0).astype(float)
    if 'spike_magnitude' in df.columns:
        features['spike_magnitude'] = df['spike_magnitude'].fillna(0)

    # ===== 6. COMPOSITE SIGNALS =====
    # Price conviction (how far from 50/50)
    features['price_conviction'] = (features['up_mid'] - 0.5).abs()

    # Velocity-price alignment
    features['vel_agrees_with_price'] = (
        ((features['velocity'] > 0) & (features['up_is_expensive'] == 1)) |
        ((features['velocity'] < 0) & (features['up_is_expensive'] == 0))
    ).astype(float)

    return features


def prepare_dataset(obs_df, resolutions, sample_per_market=50):
    """Prepare dataset with sampling."""
    print("\nPreparing dataset...")

    # Add resolution labels
    obs_df['winner'] = obs_df['market_slug'].map(resolutions)
    obs_df = obs_df[obs_df['winner'].isin(['UP', 'DOWN'])].copy()
    obs_df['target'] = (obs_df['winner'] == 'UP').astype(int)

    print(f"  Markets with resolution: {obs_df['market_slug'].nunique()}")
    print(f"  Total observations: {len(obs_df):,}")

    # Sample from each market
    sampled_dfs = []
    for slug, mdf in tqdm(obs_df.groupby('market_slug'), desc="Sampling"):
        if len(mdf) < 10:
            continue
        n_sample = min(sample_per_market, len(mdf))
        indices = np.linspace(0, len(mdf)-1, n_sample, dtype=int)
        sampled = mdf.iloc[indices]
        sampled_dfs.append(sampled)

    if not sampled_dfs:
        return None, None

    dataset = pd.concat(sampled_dfs, ignore_index=True)
    print(f"  Sampled: {len(dataset):,} observations")

    # Compute features
    print("  Computing features...")
    features = compute_features(dataset)
    features['target'] = dataset['target'].values
    features['market_slug'] = dataset['market_slug'].values

    # Handle NaN (fill with 0 for derived features)
    for col in features.columns:
        if col not in ['target', 'market_slug']:
            features[col] = features[col].fillna(0)

    print(f"  Final: {len(features):,} observations, {len(features.columns)-2} features")

    return features, dataset['market_slug'].unique()


def train_models(X_train, y_train, X_val, y_val, feature_names):
    """Train multiple models and compare."""
    results = {}

    # ===== BASELINE: Price only =====
    print("\n--- Baseline (expensive side = winner) ---")
    if 'up_is_expensive' in feature_names:
        idx = list(feature_names).index('up_is_expensive')
        # After scaling, need to threshold at 0 (which was 0.5 before scaling)
        baseline_pred = (X_val[:, idx] > 0).astype(int)
        baseline_acc = accuracy_score(y_val, baseline_pred)
        results['Baseline (expensive=winner)'] = {'accuracy': baseline_acc}
        print(f"  Accuracy: {baseline_acc:.4f}")

    # ===== Logistic Regression =====
    print("\n--- Logistic Regression (all features) ---")
    lr = LogisticRegression(max_iter=1000, C=0.1, random_state=42)
    lr.fit(X_train, y_train)
    lr_pred = lr.predict(X_val)
    lr_prob = lr.predict_proba(X_val)[:, 1]
    lr_acc = accuracy_score(y_val, lr_pred)
    lr_auc = roc_auc_score(y_val, lr_prob)
    results['Logistic Regression'] = {'accuracy': lr_acc, 'auc': lr_auc, 'model': lr}
    print(f"  Accuracy: {lr_acc:.4f}, AUC: {lr_auc:.4f}")

    # Top features
    print("  Top 10 features:")
    coef_abs = np.abs(lr.coef_[0])
    top_idx = coef_abs.argsort()[-10:][::-1]
    for i in top_idx:
        print(f"    {feature_names[i]:<25}: {lr.coef_[0][i]:>8.4f}")

    # ===== XGBoost =====
    print("\n--- XGBoost ---")
    xgb_model = xgb.XGBClassifier(
        n_estimators=100, max_depth=6, learning_rate=0.1,
        random_state=42, use_label_encoder=False, eval_metric='logloss'
    )
    xgb_model.fit(X_train, y_train)
    xgb_pred = xgb_model.predict(X_val)
    xgb_prob = xgb_model.predict_proba(X_val)[:, 1]
    xgb_acc = accuracy_score(y_val, xgb_pred)
    xgb_auc = roc_auc_score(y_val, xgb_prob)
    results['XGBoost'] = {'accuracy': xgb_acc, 'auc': xgb_auc, 'model': xgb_model}
    print(f"  Accuracy: {xgb_acc:.4f}, AUC: {xgb_auc:.4f}")

    # Feature importance
    print("  Top 10 features:")
    importance = xgb_model.feature_importances_
    top_idx = importance.argsort()[-10:][::-1]
    for i in top_idx:
        print(f"    {feature_names[i]:<25}: {importance[i]:>8.4f}")

    # ===== No-Price Model (velocity + time only) =====
    print("\n--- No-Price Model (velocity + time only) ---")
    no_price_cols = ['velocity', 'velocity_abs', 'velocity_positive',
                     'velocity_ma5', 'velocity_ma10', 'velocity_ma20',
                     'btc_ret_5', 'btc_ret_10', 'btc_ret_25',
                     'time_remaining', 'time_urgency', 'time_pct',
                     'spike_detected', 'spike_magnitude']
    no_price_idx = [i for i, f in enumerate(feature_names) if f in no_price_cols]

    if len(no_price_idx) >= 5:
        X_np_train = X_train[:, no_price_idx]
        X_np_val = X_val[:, no_price_idx]
        xgb_np = xgb.XGBClassifier(
            n_estimators=100, max_depth=6, random_state=42,
            use_label_encoder=False, eval_metric='logloss'
        )
        xgb_np.fit(X_np_train, y_train)
        np_pred = xgb_np.predict(X_np_val)
        np_acc = accuracy_score(y_val, np_pred)
        results['No-Price (velocity+time)'] = {'accuracy': np_acc}
        print(f"  Accuracy: {np_acc:.4f}")

    # ===== Price-Only Model =====
    print("\n--- Price-Only Model ---")
    price_cols = ['up_ask', 'down_ask', 'up_bid', 'down_bid', 'ask_diff',
                  'bid_diff', 'pair_cost', 'up_mid', 'down_mid', 'mid_diff']
    price_idx = [i for i, f in enumerate(feature_names) if f in price_cols]

    if len(price_idx) >= 3:
        X_p_train = X_train[:, price_idx]
        X_p_val = X_val[:, price_idx]
        lr_price = LogisticRegression(max_iter=1000, random_state=42)
        lr_price.fit(X_p_train, y_train)
        p_pred = lr_price.predict(X_p_val)
        p_acc = accuracy_score(y_val, p_pred)
        results['Price-Only'] = {'accuracy': p_acc}
        print(f"  Accuracy: {p_acc:.4f}")

    return results


def main():
    print("="*70)
    print("IMBALANCE PREDICTOR - V2")
    print("Goal: 70% winner prediction accuracy")
    print("="*70)

    # Load data
    print("\n--- Loading Training Data (IS+OOS2: Jan 16-19) ---")
    train_obs = load_observer_data("train")

    print("\n--- Loading Validation Data (OOS7: Jan 29-30) ---")
    val_obs = load_observer_data("val")

    resolutions = load_resolutions()
    print(f"\n  Resolutions: {len(resolutions)}")

    if train_obs is None or val_obs is None:
        print("Missing data!")
        return

    # Prepare datasets
    train_features, train_markets = prepare_dataset(train_obs, resolutions)
    val_features, val_markets = prepare_dataset(val_obs, resolutions)

    if train_features is None or val_features is None:
        print("Failed to prepare datasets!")
        return

    # Get common features
    feature_cols = [c for c in train_features.columns if c not in ['target', 'market_slug']]

    X_train = train_features[feature_cols].values
    y_train = train_features['target'].values
    X_val = val_features[feature_cols].values
    y_val = val_features['target'].values

    # Scale
    scaler = StandardScaler()
    X_train = scaler.fit_transform(X_train)
    X_val = scaler.transform(X_val)

    print(f"\n  Train: {len(X_train):,} samples, {X_train.shape[1]} features")
    print(f"  Val: {len(X_val):,} samples")
    print(f"  Train UP rate: {y_train.mean():.3f}")
    print(f"  Val UP rate: {y_val.mean():.3f}")

    # Train models
    results = train_models(X_train, y_train, X_val, y_val, feature_cols)

    # Summary
    print("\n" + "="*70)
    print("RESULTS SUMMARY")
    print("="*70)
    print(f"\n{'Model':<35} {'Accuracy':>10} {'AUC':>10}")
    print("-"*60)
    for name, res in sorted(results.items(), key=lambda x: x[1].get('accuracy', 0), reverse=True):
        acc = res.get('accuracy', 0)
        auc = res.get('auc', 'N/A')
        auc_str = f"{auc:.4f}" if isinstance(auc, float) else auc
        print(f"{name:<35} {acc:>10.4f} {auc_str:>10}")

    # Check 70% target
    best_acc = max(r['accuracy'] for r in results.values())
    print(f"\n{'='*70}")
    if best_acc >= 0.70:
        print(f"SUCCESS: Achieved {best_acc:.1%} accuracy (target: 70%)")
    else:
        print(f"BELOW TARGET: Best accuracy {best_acc:.1%} < 70% target")

    # Key findings
    baseline_acc = results.get('Baseline (expensive=winner)', {}).get('accuracy', 0)
    ml_acc = max(r['accuracy'] for name, r in results.items() if 'model' in r)

    print(f"\n{'='*70}")
    print("KEY FINDINGS")
    print("="*70)
    print(f"\n1. Baseline (expensive=winner): {baseline_acc:.1%}")
    print(f"2. Best ML model: {ml_acc:.1%}")
    print(f"3. Improvement: {(ml_acc - baseline_acc)*100:+.1f} pp")

    no_price_acc = results.get('No-Price (velocity+time)', {}).get('accuracy', 0)
    price_only_acc = results.get('Price-Only', {}).get('accuracy', 0)
    print(f"\n4. Price-only model: {price_only_acc:.1%}")
    print(f"5. No-price model (velocity+time): {no_price_acc:.1%}")

    if no_price_acc > 0.55:
        print("\n   Velocity/time signals have some predictive power!")
    else:
        print("\n   Velocity/time signals have minimal predictive power.")
        print("   PRICE is the dominant feature.")


if __name__ == "__main__":
    main()
