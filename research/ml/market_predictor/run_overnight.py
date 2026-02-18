#!/usr/bin/env python3
"""
Overnight Runner for ML Market Predictor Pipeline

Run this and go to sleep. Each step prints clearly, saves checkpoints,
and skips steps that already completed. Output goes to run_overnight.log too.

Usage:
    cd ~/polymarket-amm-bot/research/ml/market_predictor
    python run_overnight.py 2>&1 | tee run_overnight.log

Or to start from a specific step:
    python run_overnight.py --start 3    # skip steps 1-2
"""

import sys
import os
import time
import traceback
import pickle
from pathlib import Path
from datetime import datetime

# Add project paths
sys.path.insert(0, str(Path(__file__).parent))

MODEL_DIR = Path(__file__).parent / "models"
CHECKPOINT_DIR = Path(__file__).parent / "checkpoints"
MODEL_DIR.mkdir(exist_ok=True)
CHECKPOINT_DIR.mkdir(exist_ok=True)


def banner(msg):
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")


def step_done(step_num):
    """Check if a step already completed."""
    return (CHECKPOINT_DIR / f"step{step_num}_done").exists()


def mark_done(step_num, info=""):
    """Mark a step as completed."""
    (CHECKPOINT_DIR / f"step{step_num}_done").write_text(
        f"Completed at {datetime.now().isoformat()}\n{info}"
    )


def save_checkpoint(name, obj):
    """Save intermediate data."""
    path = CHECKPOINT_DIR / f"{name}.pkl"
    with open(path, "wb") as f:
        pickle.dump(obj, f)
    print(f"  Saved checkpoint: {path.name} ({path.stat().st_size / 1e6:.1f} MB)")


def load_checkpoint(name):
    """Load intermediate data."""
    path = CHECKPOINT_DIR / f"{name}.pkl"
    with open(path, "rb") as f:
        return pickle.load(f)


def run_step(step_num, description, func, start_from=1):
    """Run a step with error handling and skip logic."""
    if step_num < start_from:
        print(f"STEP {step_num}: {description} -- SKIPPED (--start {start_from})")
        return True

    if step_done(step_num):
        print(f"STEP {step_num}: {description} -- ALREADY DONE, skipping")
        return True

    banner(f"STEP {step_num}: {description}")
    t0 = time.time()
    try:
        func()
        elapsed = time.time() - t0
        mark_done(step_num, f"Took {elapsed:.1f}s")
        print(f"\n  STEP {step_num} DONE in {elapsed:.1f}s")
        return True
    except Exception as e:
        elapsed = time.time() - t0
        print(f"\n  STEP {step_num} FAILED after {elapsed:.1f}s")
        print(f"  Error: {e}")
        traceback.print_exc()
        return False


# ========================================================
# STEP 1: Load data
# ========================================================
def step1_load_data():
    from data_loader import load_all_with_labels

    print("Loading all datasets (train + test) with labels...")
    print("  Training: IS+OOS2 + OOS7 + OOS9 (134h)")
    print("  Testing:  OOS3+OOS4 + OOS6 + OOS8 (89h)")
    print("  This may take a few minutes for large CSVs...\n")

    train_df, test_df, resolutions = load_all_with_labels(
        include_orderbook=True,
        sample_frac=None,  # Full data
    )

    print(f"\n  Train: {len(train_df):,} rows, {train_df['market_slug'].nunique()} markets")
    print(f"  Test:  {len(test_df):,} rows, {test_df['market_slug'].nunique()} markets")

    save_checkpoint("train_raw", train_df)
    save_checkpoint("test_raw", test_df)
    save_checkpoint("resolutions", resolutions)


# ========================================================
# STEP 2: Feature engineering
# ========================================================
def step2_feature_engineering():
    from feature_engineer import engineer_all_features, get_feature_columns

    print("Loading raw data from checkpoint...")
    train_df = load_checkpoint("train_raw")
    test_df = load_checkpoint("test_raw")

    print(f"  Train: {train_df.shape}")
    print(f"  Test:  {test_df.shape}")

    print("\nEngineering features on TRAIN set...")
    train_df = engineer_all_features(train_df, include_rolling=True)

    print("\nEngineering features on TEST set...")
    test_df = engineer_all_features(test_df, include_rolling=True)

    feature_cols = get_feature_columns(train_df)
    print(f"\n  Features: {len(feature_cols)}")
    print(f"  Train shape: {train_df.shape}")
    print(f"  Test shape:  {test_df.shape}")

    save_checkpoint("train_featured", train_df)
    save_checkpoint("test_featured", test_df)
    save_checkpoint("feature_cols", feature_cols)


# ========================================================
# STEP 3: Prepare X/y arrays
# ========================================================
def step3_prepare_arrays():
    import numpy as np

    print("Loading featured data from checkpoint...")
    train_df = load_checkpoint("train_featured")
    test_df = load_checkpoint("test_featured")
    feature_cols = load_checkpoint("feature_cols")

    # Filter to rows with labels
    train_df = train_df[train_df["winner_binary"].notna()].copy()
    test_df = test_df[test_df["winner_binary"].notna()].copy()

    print(f"  Train with labels: {len(train_df):,}")
    print(f"  Test with labels:  {len(test_df):,}")

    # Ensure all feature columns exist
    missing_train = [c for c in feature_cols if c not in train_df.columns]
    missing_test = [c for c in feature_cols if c not in test_df.columns]
    if missing_train:
        print(f"  WARNING: Missing {len(missing_train)} features in train: {missing_train[:5]}")
        feature_cols = [c for c in feature_cols if c in train_df.columns and c in test_df.columns]
        print(f"  Using {len(feature_cols)} features after filtering")

    X_train = train_df[feature_cols].values
    y_train = train_df["winner_binary"].values
    X_test = test_df[feature_cols].values
    y_test = test_df["winner_binary"].values

    # Handle NaN/inf
    X_train = np.nan_to_num(X_train, nan=0, posinf=0, neginf=0)
    X_test = np.nan_to_num(X_test, nan=0, posinf=0, neginf=0)

    print(f"\n  X_train: {X_train.shape}")
    print(f"  X_test:  {X_test.shape}")
    print(f"  y_train: UP={y_train.sum():.0f}, DOWN={len(y_train)-y_train.sum():.0f}")
    print(f"  y_test:  UP={y_test.sum():.0f}, DOWN={len(y_test)-y_test.sum():.0f}")

    save_checkpoint("X_train", X_train)
    save_checkpoint("X_test", X_test)
    save_checkpoint("y_train", y_train)
    save_checkpoint("y_test", y_test)
    save_checkpoint("feature_cols_final", feature_cols)


# ========================================================
# STEP 4: Train Logistic Regression
# ========================================================
def step4_logistic_regression():
    import numpy as np
    import joblib
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import cross_val_score, StratifiedKFold
    from sklearn.metrics import accuracy_score, roc_auc_score, f1_score, classification_report

    X_train = load_checkpoint("X_train")
    X_test = load_checkpoint("X_test")
    y_train = load_checkpoint("y_train")
    y_test = load_checkpoint("y_test")
    feature_cols = load_checkpoint("feature_cols_final")

    print("Scaling features...")
    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s = scaler.transform(X_test)

    print("Training Logistic Regression...")
    model = LogisticRegression(max_iter=1000, random_state=42)
    model.fit(X_train_s, y_train)

    y_pred = model.predict(X_test_s)
    y_proba = model.predict_proba(X_test_s)[:, 1]

    acc = accuracy_score(y_test, y_pred)
    auc = roc_auc_score(y_test, y_proba)
    f1 = f1_score(y_test, y_pred)

    print(f"\n  Accuracy: {acc:.4f}")
    print(f"  AUC:      {auc:.4f}")
    print(f"  F1:       {f1:.4f}")

    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cv_scores = cross_val_score(model, X_train_s, y_train, cv=cv, scoring="accuracy")
    print(f"  CV:       {np.mean(cv_scores):.4f} +/- {np.std(cv_scores):.4f}")

    print(f"\n{classification_report(y_test, y_pred, target_names=['DOWN', 'UP'])}")

    # Feature importance
    coefs = sorted(zip(feature_cols, np.abs(model.coef_[0])), key=lambda x: -x[1])
    print("Top 10 features (|coef|):")
    for i, (f, c) in enumerate(coefs[:10], 1):
        print(f"  {i}. {f}: {c:.4f}")

    joblib.dump({"model": model, "scaler": scaler}, MODEL_DIR / "logistic_regression.joblib")
    save_checkpoint("lr_results", {"accuracy": acc, "auc": auc, "f1": f1, "cv": cv_scores.tolist()})


# ========================================================
# STEP 5: Train Random Forest
# ========================================================
def step5_random_forest():
    import numpy as np
    import joblib
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.model_selection import cross_val_score, StratifiedKFold
    from sklearn.metrics import accuracy_score, roc_auc_score, f1_score, classification_report

    X_train = load_checkpoint("X_train")
    X_test = load_checkpoint("X_test")
    y_train = load_checkpoint("y_train")
    y_test = load_checkpoint("y_test")
    feature_cols = load_checkpoint("feature_cols_final")

    print("Training Random Forest (100 trees, max_depth=10)...")
    model = RandomForestClassifier(
        n_estimators=100,
        max_depth=10,
        min_samples_split=5,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]

    acc = accuracy_score(y_test, y_pred)
    auc = roc_auc_score(y_test, y_proba)
    f1 = f1_score(y_test, y_pred)

    print(f"\n  Accuracy: {acc:.4f}")
    print(f"  AUC:      {auc:.4f}")
    print(f"  F1:       {f1:.4f}")

    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cv_scores = cross_val_score(model, X_train, y_train, cv=cv, scoring="accuracy")
    print(f"  CV:       {np.mean(cv_scores):.4f} +/- {np.std(cv_scores):.4f}")

    print(f"\n{classification_report(y_test, y_pred, target_names=['DOWN', 'UP'])}")

    # Feature importance
    imps = sorted(zip(feature_cols, model.feature_importances_), key=lambda x: -x[1])
    print("Top 15 features (importance):")
    for i, (f, c) in enumerate(imps[:15], 1):
        print(f"  {i}. {f}: {c:.4f}")

    joblib.dump(model, MODEL_DIR / "random_forest.joblib")
    save_checkpoint("rf_results", {"accuracy": acc, "auc": auc, "f1": f1, "cv": cv_scores.tolist()})


# ========================================================
# STEP 6: Train XGBoost
# ========================================================
def step6_xgboost():
    import numpy as np
    import joblib
    from sklearn.model_selection import cross_val_score, StratifiedKFold
    from sklearn.metrics import accuracy_score, roc_auc_score, f1_score, classification_report

    try:
        import xgboost as xgb
    except ImportError:
        print("  XGBoost not installed. Skipping.")
        print("  Install with: pip install xgboost")
        mark_done(6, "Skipped - xgboost not installed")
        return

    X_train = load_checkpoint("X_train")
    X_test = load_checkpoint("X_test")
    y_train = load_checkpoint("y_train")
    y_test = load_checkpoint("y_test")
    feature_cols = load_checkpoint("feature_cols_final")

    print("Training XGBoost (100 rounds, max_depth=6)...")
    model = xgb.XGBClassifier(
        n_estimators=100,
        max_depth=6,
        learning_rate=0.1,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        use_label_encoder=False,
        eval_metric="logloss",
    )
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]

    acc = accuracy_score(y_test, y_pred)
    auc = roc_auc_score(y_test, y_proba)
    f1 = f1_score(y_test, y_pred)

    print(f"\n  Accuracy: {acc:.4f}")
    print(f"  AUC:      {auc:.4f}")
    print(f"  F1:       {f1:.4f}")

    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cv_scores = cross_val_score(model, X_train, y_train, cv=cv, scoring="accuracy")
    print(f"  CV:       {np.mean(cv_scores):.4f} +/- {np.std(cv_scores):.4f}")

    print(f"\n{classification_report(y_test, y_pred, target_names=['DOWN', 'UP'])}")

    # Feature importance
    imps = sorted(zip(feature_cols, model.feature_importances_), key=lambda x: -x[1])
    print("Top 15 features (importance):")
    for i, (f, c) in enumerate(imps[:15], 1):
        print(f"  {i}. {f}: {c:.4f}")

    joblib.dump(model, MODEL_DIR / "xgboost.joblib")
    save_checkpoint("xgb_results", {"accuracy": acc, "auc": auc, "f1": f1, "cv": cv_scores.tolist()})


# ========================================================
# STEP 7: Tick-level comparison
# ========================================================
def step7_tick_comparison():
    import pandas as pd
    import numpy as np

    results = {}
    for name, key in [("LogisticRegression", "lr_results"),
                      ("RandomForest", "rf_results"),
                      ("XGBoost", "xgb_results")]:
        try:
            r = load_checkpoint(key)
            results[name] = r
        except FileNotFoundError:
            print(f"  {name}: no results found, skipped")

    if not results:
        print("  No model results found!")
        return

    print(f"\n{'Model':<22} {'Accuracy':>10} {'AUC':>10} {'F1':>10} {'CV Mean':>10}")
    print("-" * 62)
    for name, r in results.items():
        cv_mean = np.mean(r["cv"]) if r.get("cv") else 0
        print(f"{name:<22} {r['accuracy']:>10.4f} {r['auc']:>10.4f} {r['f1']:>10.4f} {cv_mean:>10.4f}")
    print("-" * 62)

    best = max(results.items(), key=lambda x: x[1]["auc"])
    print(f"\nBest by AUC: {best[0]} (AUC = {best[1]['auc']:.4f})")

    rows = []
    for name, r in results.items():
        rows.append({
            "model": name,
            "accuracy": r["accuracy"],
            "auc": r["auc"],
            "f1": r["f1"],
            "cv_mean": np.mean(r["cv"]) if r.get("cv") else 0,
        })
    pd.DataFrame(rows).to_csv(MODEL_DIR / "model_comparison_tick.csv", index=False)
    print(f"\nSaved: {MODEL_DIR / 'model_comparison_tick.csv'}")


# ========================================================
# STEP 8: Market-level models (Option B)
#   Aggregate to 1 row per market, predict winner
# ========================================================
def step8_market_level():
    import numpy as np
    import joblib
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import cross_val_score, StratifiedKFold
    from sklearn.metrics import accuracy_score, roc_auc_score, f1_score, classification_report

    print("Loading featured data from checkpoint...")
    train_df = load_checkpoint("train_featured")
    test_df = load_checkpoint("test_featured")

    # Filter to labeled rows
    train_df = train_df[train_df["winner_binary"].notna()].copy()
    test_df = test_df[test_df["winner_binary"].notna()].copy()

    # Aggregate to market level - take MULTIPLE snapshots per market
    # Use median of last 25% of observations (near resolution, most informative)
    print("Aggregating to market level...")

    # Columns that must NOT be aggregated as features (labels + identifiers)
    label_cols = {"winner_binary", "winner", "expensive_wins", "good_entry", "trade_pnl"}

    def aggregate_market(group):
        # Take last 25% of observations (closest to resolution)
        n = len(group)
        tail = group.tail(max(1, n // 4))
        result = {}
        numeric_cols = [c for c in tail.select_dtypes(include=[np.number]).columns
                        if c not in label_cols]
        for col in numeric_cols:
            result[f"{col}_median"] = tail[col].median()
            result[f"{col}_std"] = tail[col].std()
            result[f"{col}_last"] = tail[col].iloc[-1]
        result["winner_binary"] = group["winner_binary"].iloc[0]
        result["n_observations"] = n
        return pd.Series(result)

    import pandas as pd
    train_mkt = train_df.groupby("market_slug").apply(aggregate_market, include_groups=False).reset_index()
    test_mkt = test_df.groupby("market_slug").apply(aggregate_market, include_groups=False).reset_index()

    print(f"  Train markets: {len(train_mkt)}")
    print(f"  Test markets:  {len(test_mkt)}")

    # Get feature columns (exclude identifiers and target)
    exclude = {"market_slug", "winner_binary", "n_observations"}
    feature_cols = [c for c in train_mkt.columns if c not in exclude and train_mkt[c].dtype in ['float64', 'int64', 'float32']]

    # Ensure same columns
    feature_cols = [c for c in feature_cols if c in test_mkt.columns]
    print(f"  Market-level features: {len(feature_cols)}")

    X_train = train_mkt[feature_cols].values
    y_train = train_mkt["winner_binary"].values
    X_test = test_mkt[feature_cols].values
    y_test = test_mkt["winner_binary"].values

    X_train = np.nan_to_num(X_train, nan=0, posinf=0, neginf=0)
    X_test = np.nan_to_num(X_test, nan=0, posinf=0, neginf=0)

    print(f"\n  X_train: {X_train.shape}, X_test: {X_test.shape}")

    # Scale
    scaler = StandardScaler()
    X_train_s = scaler.fit_transform(X_train)
    X_test_s = scaler.transform(X_test)

    results = {}

    # LR
    print("\nTraining Market-Level Logistic Regression...")
    lr = LogisticRegression(max_iter=2000, random_state=42, C=0.1)
    lr.fit(X_train_s, y_train)
    y_pred = lr.predict(X_test_s)
    y_proba = lr.predict_proba(X_test_s)[:, 1]
    acc = accuracy_score(y_test, y_pred)
    auc = roc_auc_score(y_test, y_proba)
    f1 = f1_score(y_test, y_pred)
    cv = cross_val_score(lr, X_train_s, y_train, cv=min(5, len(y_train)), scoring="accuracy")
    print(f"  LR: Acc={acc:.4f}, AUC={auc:.4f}, F1={f1:.4f}, CV={np.mean(cv):.4f}")
    results["MktLevel_LR"] = {"accuracy": acc, "auc": auc, "f1": f1, "cv": cv.tolist()}
    joblib.dump({"model": lr, "scaler": scaler, "features": feature_cols}, MODEL_DIR / "market_level_lr.joblib")

    # Gradient Boosting (better for small datasets than XGB)
    print("Training Market-Level Gradient Boosting...")
    gb = GradientBoostingClassifier(
        n_estimators=100, max_depth=4, learning_rate=0.05,
        subsample=0.8, random_state=42,
    )
    gb.fit(X_train, y_train)
    y_pred = gb.predict(X_test)
    y_proba = gb.predict_proba(X_test)[:, 1]
    acc = accuracy_score(y_test, y_pred)
    auc = roc_auc_score(y_test, y_proba)
    f1 = f1_score(y_test, y_pred)
    cv = cross_val_score(gb, X_train, y_train, cv=min(5, len(y_train)), scoring="accuracy")
    print(f"  GB: Acc={acc:.4f}, AUC={auc:.4f}, F1={f1:.4f}, CV={np.mean(cv):.4f}")
    results["MktLevel_GB"] = {"accuracy": acc, "auc": auc, "f1": f1, "cv": cv.tolist()}

    # Feature importance from GB
    imps = sorted(zip(feature_cols, gb.feature_importances_), key=lambda x: -x[1])
    print("\nTop 15 market-level features:")
    for i, (feat, imp) in enumerate(imps[:15], 1):
        print(f"  {i}. {feat}: {imp:.4f}")

    joblib.dump({"model": gb, "features": feature_cols}, MODEL_DIR / "market_level_gb.joblib")

    print(f"\n{classification_report(y_test, y_pred, target_names=['DOWN', 'UP'])}")

    save_checkpoint("market_level_results", results)


# ========================================================
# STEP 9: Entry timing model (Option C)
#   Predict: is NOW a good time to enter?
#   Label: observations where entering would have been profitable
# ========================================================
def step9_entry_timing():
    import numpy as np
    import pandas as pd
    import joblib
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import accuracy_score, roc_auc_score, f1_score, classification_report

    print("Loading featured data from checkpoint...")
    train_df = load_checkpoint("train_featured")
    test_df = load_checkpoint("test_featured")

    # Filter to labeled rows
    train_df = train_df[train_df["winner_binary"].notna()].copy()
    test_df = test_df[test_df["winner_binary"].notna()].copy()

    # Create entry timing labels
    # "Good entry" = buying expensive side as MAKER at (expensive_ask - 0.03)
    # would be profitable (expensive side wins and entry price < 1.0)
    print("Creating entry timing labels...")

    def add_entry_labels(df):
        # Entry price as maker
        df["entry_price"] = df["expensive_ask"] - 0.03
        df["entry_price"] = df["entry_price"].clip(lower=0.01)

        # Would this trade be profitable?
        # expensive_side=1 means UP is expensive, winner_binary=1 means UP wins
        df["expensive_wins"] = (
            ((df["expensive_side"] == 1) & (df["winner_binary"] == 1)) |
            ((df["expensive_side"] == 0) & (df["winner_binary"] == 0))
        ).astype(int)

        # Profit per share if expensive wins: (1.0 - entry_price)
        # Loss per share if expensive loses: entry_price
        df["trade_pnl"] = np.where(
            df["expensive_wins"] == 1,
            1.0 - df["entry_price"],   # win
            -df["entry_price"],         # lose
        )

        # Good entry = profitable AND meets FADE criteria
        df["good_entry"] = (
            (df["expensive_wins"] == 1) &
            (df["expensive_ask"] >= 0.70) &  # reasonable threshold
            (df["time_remaining_secs"] >= 90) &  # not too late
            (df["time_remaining_secs"] <= 600)   # not too early
        ).astype(int)

        return df

    train_df = add_entry_labels(train_df)
    test_df = add_entry_labels(test_df)

    print(f"  Train good entries: {train_df['good_entry'].sum():,} / {len(train_df):,} ({train_df['good_entry'].mean()*100:.1f}%)")
    print(f"  Test good entries:  {test_df['good_entry'].sum():,} / {len(test_df):,} ({test_df['good_entry'].mean()*100:.1f}%)")

    # Use original feature columns
    feature_cols = load_checkpoint("feature_cols_final")

    X_train = train_df[feature_cols].values
    y_train = train_df["good_entry"].values
    X_test = test_df[feature_cols].values
    y_test = test_df["good_entry"].values

    X_train = np.nan_to_num(X_train, nan=0, posinf=0, neginf=0)
    X_test = np.nan_to_num(X_test, nan=0, posinf=0, neginf=0)

    print(f"\n  X_train: {X_train.shape}, y=1: {y_train.sum()}")
    print(f"  X_test:  {X_test.shape}, y=1: {y_test.sum()}")

    # Train GB (handles imbalanced data better)
    print("\nTraining Entry Timing Gradient Boosting...")
    gb = GradientBoostingClassifier(
        n_estimators=100, max_depth=5, learning_rate=0.1,
        subsample=0.8, random_state=42,
    )
    gb.fit(X_train, y_train)

    y_pred = gb.predict(X_test)
    y_proba = gb.predict_proba(X_test)[:, 1]

    acc = accuracy_score(y_test, y_pred)
    auc = roc_auc_score(y_test, y_proba)
    f1 = f1_score(y_test, y_pred)

    print(f"\n  Accuracy: {acc:.4f}")
    print(f"  AUC:      {auc:.4f}")
    print(f"  F1:       {f1:.4f}")
    print(f"\n{classification_report(y_test, y_pred, target_names=['Bad Entry', 'Good Entry'])}")

    # Feature importance
    imps = sorted(zip(feature_cols, gb.feature_importances_), key=lambda x: -x[1])
    print("Top 15 entry-timing features:")
    for i, (feat, imp) in enumerate(imps[:15], 1):
        print(f"  {i}. {feat}: {imp:.4f}")

    # Expected PnL analysis at different confidence thresholds
    print("\nExpected PnL by confidence threshold:")
    test_df["entry_proba"] = y_proba
    for threshold in [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]:
        mask = test_df["entry_proba"] >= threshold
        if mask.sum() == 0:
            print(f"  P>={threshold}: 0 signals")
            continue
        subset = test_df[mask]
        avg_pnl = subset["trade_pnl"].mean()
        win_rate = subset["expensive_wins"].mean()
        n = len(subset)
        total_pnl = subset["trade_pnl"].sum()
        print(f"  P>={threshold}: {n:>7,} signals, win={win_rate:.1%}, avg_pnl=${avg_pnl:.4f}, total=${total_pnl:.2f}")

    joblib.dump({"model": gb, "features": feature_cols}, MODEL_DIR / "entry_timing_gb.joblib")
    save_checkpoint("entry_timing_results", {"accuracy": acc, "auc": auc, "f1": f1})


# ========================================================
# STEP 10: Final summary (all models)
# ========================================================
def step10_final_summary():
    import pandas as pd
    import numpy as np

    print("\n" + "=" * 70)
    print("  COMPLETE MODEL COMPARISON")
    print("=" * 70)

    # Tick-level models
    print("\n--- TICK-LEVEL MODELS (predict winner per observation) ---")
    print(f"{'Model':<22} {'Accuracy':>10} {'AUC':>10} {'F1':>10} {'CV Mean':>10}")
    print("-" * 62)
    for name, key in [("LogisticRegression", "lr_results"),
                      ("RandomForest", "rf_results"),
                      ("XGBoost", "xgb_results")]:
        try:
            r = load_checkpoint(key)
            cv_mean = np.mean(r["cv"]) if r.get("cv") else 0
            print(f"{name:<22} {r['accuracy']:>10.4f} {r['auc']:>10.4f} {r['f1']:>10.4f} {cv_mean:>10.4f}")
        except FileNotFoundError:
            pass

    # Market-level models
    print("\n--- MARKET-LEVEL MODELS (predict winner per market) ---")
    try:
        mkt_results = load_checkpoint("market_level_results")
        print(f"{'Model':<22} {'Accuracy':>10} {'AUC':>10} {'F1':>10} {'CV Mean':>10}")
        print("-" * 62)
        for name, r in mkt_results.items():
            cv_mean = np.mean(r["cv"]) if r.get("cv") else 0
            print(f"{name:<22} {r['accuracy']:>10.4f} {r['auc']:>10.4f} {r['f1']:>10.4f} {cv_mean:>10.4f}")
    except FileNotFoundError:
        print("  Not available")

    # Entry timing model
    print("\n--- ENTRY TIMING MODEL (predict good entry moments) ---")
    try:
        et_results = load_checkpoint("entry_timing_results")
        print(f"  Accuracy: {et_results['accuracy']:.4f}")
        print(f"  AUC:      {et_results['auc']:.4f}")
        print(f"  F1:       {et_results['f1']:.4f}")
    except FileNotFoundError:
        print("  Not available")

    # Save combined CSV
    all_rows = []
    for name, key in [("Tick_LR", "lr_results"), ("Tick_RF", "rf_results"), ("Tick_XGB", "xgb_results")]:
        try:
            r = load_checkpoint(key)
            all_rows.append({"model": name, "level": "tick", **{k: v for k, v in r.items() if k != "cv"}, "cv_mean": np.mean(r.get("cv", [0]))})
        except: pass
    try:
        for name, r in load_checkpoint("market_level_results").items():
            all_rows.append({"model": name, "level": "market", **{k: v for k, v in r.items() if k != "cv"}, "cv_mean": np.mean(r.get("cv", [0]))})
    except: pass
    try:
        r = load_checkpoint("entry_timing_results")
        all_rows.append({"model": "EntryTiming_GB", "level": "entry", **r, "cv_mean": 0})
    except: pass

    if all_rows:
        pd.DataFrame(all_rows).to_csv(MODEL_DIR / "model_comparison_all.csv", index=False)
        print(f"\nSaved: {MODEL_DIR / 'model_comparison_all.csv'}")

    print("\n" + "=" * 70)
    print("  DONE - Check run_overnight.log for full details")
    print("=" * 70)


# ========================================================
# MAIN
# ========================================================
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=1, help="Start from step N (skip earlier steps)")
    parser.add_argument("--reset", action="store_true", help="Clear all checkpoints and start fresh")
    args = parser.parse_args()

    if args.reset:
        import shutil
        if CHECKPOINT_DIR.exists():
            shutil.rmtree(CHECKPOINT_DIR)
            CHECKPOINT_DIR.mkdir()
            print("Cleared all checkpoints.\n")

    banner("ML MARKET PREDICTOR - OVERNIGHT PIPELINE V2")
    print("Steps:")
    print("  1. Load data (train + test) -- with fixed resolutions")
    print("  2. Feature engineering (110 features)")
    print("  3. Prepare X/y arrays")
    print("  4. Train Logistic Regression (tick-level)")
    print("  5. Train Random Forest (tick-level)")
    print("  6. Train XGBoost (tick-level)")
    print("  7. Tick-level comparison")
    print("  8. Market-level models (Option B) -- 1 row per market")
    print("  9. Entry timing model (Option C) -- predict WHEN to enter")
    print(" 10. Final summary (all models)")
    print(f"\nStarting from step {args.start}")
    print(f"Checkpoints in: {CHECKPOINT_DIR}")
    print()

    steps = [
        (1, "Load data",                step1_load_data),
        (2, "Feature engineering",       step2_feature_engineering),
        (3, "Prepare X/y arrays",        step3_prepare_arrays),
        (4, "Logistic Regression",       step4_logistic_regression),
        (5, "Random Forest",             step5_random_forest),
        (6, "XGBoost",                   step6_xgboost),
        (7, "Tick-level comparison",     step7_tick_comparison),
        (8, "Market-level models",       step8_market_level),
        (9, "Entry timing model",        step9_entry_timing),
        (10, "Final summary",            step10_final_summary),
    ]

    t_total = time.time()
    for num, desc, func in steps:
        ok = run_step(num, desc, func, start_from=args.start)
        if not ok:
            print(f"\n*** PIPELINE STOPPED at step {num}. Fix the error and rerun. ***")
            print(f"*** Completed steps are saved. Rerun will skip them. ***")
            sys.exit(1)

    elapsed = time.time() - t_total
    banner(f"ALL DONE! Total time: {elapsed/60:.1f} minutes")
