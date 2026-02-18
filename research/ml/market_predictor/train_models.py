"""
Model Training for ML Market Predictor

Trains multiple models for market winner prediction:
1. Logistic Regression (baseline)
2. Random Forest (feature importance)
3. XGBoost (primary classifier)
4. Ensemble (stacked)

Author: Claude Code
Date: February 8, 2026
"""

import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import logging
import joblib
from dataclasses import dataclass, field

# Sklearn imports
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import cross_val_score, StratifiedKFold
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score,
    roc_auc_score, classification_report, confusion_matrix
)

# XGBoost
try:
    import xgboost as xgb
    HAS_XGBOOST = True
except ImportError:
    HAS_XGBOOST = False
    print("Warning: XGBoost not installed. Install with: pip install xgboost")

# Local imports
from data_loader import load_all_with_labels, get_market_level_data
from feature_engineer import engineer_all_features, get_feature_columns

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Model save directory
MODEL_DIR = Path(__file__).parent / "models"
MODEL_DIR.mkdir(exist_ok=True)


@dataclass
class ModelResult:
    """Results from training a model."""
    name: str
    accuracy: float
    precision: float
    recall: float
    f1: float
    auc: float
    cv_scores: List[float] = field(default_factory=list)
    feature_importance: Optional[Dict[str, float]] = None


def prepare_data(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    feature_cols: List[str],
    target_col: str = "winner_binary",
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Prepare data for training.

    Returns:
        X_train, X_test, y_train, y_test
    """
    # Filter to rows with labels
    train_df = train_df[train_df[target_col].notna()].copy()
    test_df = test_df[test_df[target_col].notna()].copy()

    logger.info(f"Training samples with labels: {len(train_df)}")
    logger.info(f"Testing samples with labels: {len(test_df)}")

    # Extract features and target
    X_train = train_df[feature_cols].values
    y_train = train_df[target_col].values

    X_test = test_df[feature_cols].values
    y_test = test_df[target_col].values

    # Handle NaN/inf
    X_train = np.nan_to_num(X_train, nan=0, posinf=0, neginf=0)
    X_test = np.nan_to_num(X_test, nan=0, posinf=0, neginf=0)

    return X_train, X_test, y_train, y_test


def train_logistic_regression(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_test: np.ndarray,
    y_test: np.ndarray,
    feature_names: List[str],
) -> ModelResult:
    """
    Train Logistic Regression baseline.
    """
    logger.info("Training Logistic Regression...")

    # Scale features
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    # Train
    model = LogisticRegression(max_iter=1000, random_state=42)
    model.fit(X_train_scaled, y_train)

    # Predict
    y_pred = model.predict(X_test_scaled)
    y_proba = model.predict_proba(X_test_scaled)[:, 1]

    # Metrics
    result = ModelResult(
        name="LogisticRegression",
        accuracy=accuracy_score(y_test, y_pred),
        precision=precision_score(y_test, y_pred),
        recall=recall_score(y_test, y_pred),
        f1=f1_score(y_test, y_pred),
        auc=roc_auc_score(y_test, y_proba),
    )

    # Cross-validation
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cv_scores = cross_val_score(model, X_train_scaled, y_train, cv=cv, scoring="accuracy")
    result.cv_scores = cv_scores.tolist()

    # Feature importance (coefficients)
    coefs = dict(zip(feature_names, np.abs(model.coef_[0])))
    result.feature_importance = dict(sorted(coefs.items(), key=lambda x: -x[1])[:20])

    # Save model
    joblib.dump({"model": model, "scaler": scaler}, MODEL_DIR / "logistic_regression.joblib")

    logger.info(f"  Accuracy: {result.accuracy:.4f}")
    logger.info(f"  AUC: {result.auc:.4f}")
    logger.info(f"  CV mean: {np.mean(cv_scores):.4f} +/- {np.std(cv_scores):.4f}")

    return result


def train_random_forest(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_test: np.ndarray,
    y_test: np.ndarray,
    feature_names: List[str],
) -> ModelResult:
    """
    Train Random Forest for feature importance.
    """
    logger.info("Training Random Forest...")

    # Train
    model = RandomForestClassifier(
        n_estimators=100,
        max_depth=10,
        min_samples_split=5,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_train, y_train)

    # Predict
    y_pred = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]

    # Metrics
    result = ModelResult(
        name="RandomForest",
        accuracy=accuracy_score(y_test, y_pred),
        precision=precision_score(y_test, y_pred),
        recall=recall_score(y_test, y_pred),
        f1=f1_score(y_test, y_pred),
        auc=roc_auc_score(y_test, y_proba),
    )

    # Cross-validation
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cv_scores = cross_val_score(model, X_train, y_train, cv=cv, scoring="accuracy")
    result.cv_scores = cv_scores.tolist()

    # Feature importance
    importances = dict(zip(feature_names, model.feature_importances_))
    result.feature_importance = dict(sorted(importances.items(), key=lambda x: -x[1])[:20])

    # Save model
    joblib.dump(model, MODEL_DIR / "random_forest.joblib")

    logger.info(f"  Accuracy: {result.accuracy:.4f}")
    logger.info(f"  AUC: {result.auc:.4f}")
    logger.info(f"  CV mean: {np.mean(cv_scores):.4f} +/- {np.std(cv_scores):.4f}")

    return result


def train_xgboost(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_test: np.ndarray,
    y_test: np.ndarray,
    feature_names: List[str],
) -> ModelResult:
    """
    Train XGBoost (primary classifier).
    """
    if not HAS_XGBOOST:
        logger.warning("XGBoost not available, skipping")
        return None

    logger.info("Training XGBoost...")

    # Train
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

    # Predict
    y_pred = model.predict(X_test)
    y_proba = model.predict_proba(X_test)[:, 1]

    # Metrics
    result = ModelResult(
        name="XGBoost",
        accuracy=accuracy_score(y_test, y_pred),
        precision=precision_score(y_test, y_pred),
        recall=recall_score(y_test, y_pred),
        f1=f1_score(y_test, y_pred),
        auc=roc_auc_score(y_test, y_proba),
    )

    # Cross-validation
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cv_scores = cross_val_score(model, X_train, y_train, cv=cv, scoring="accuracy")
    result.cv_scores = cv_scores.tolist()

    # Feature importance
    importances = dict(zip(feature_names, model.feature_importances_))
    result.feature_importance = dict(sorted(importances.items(), key=lambda x: -x[1])[:20])

    # Save model
    joblib.dump(model, MODEL_DIR / "xgboost.joblib")

    logger.info(f"  Accuracy: {result.accuracy:.4f}")
    logger.info(f"  AUC: {result.auc:.4f}")
    logger.info(f"  CV mean: {np.mean(cv_scores):.4f} +/- {np.std(cv_scores):.4f}")

    return result


def train_all_models(
    sample_frac: Optional[float] = None,
    include_orderbook: bool = True,
) -> Dict[str, ModelResult]:
    """
    Train all models and return results.

    Args:
        sample_frac: Optional fraction to sample data (for testing)
        include_orderbook: Whether to include orderbook features

    Returns:
        Dictionary of model name -> ModelResult
    """
    logger.info("=" * 60)
    logger.info("TRAINING ALL MODELS")
    logger.info("=" * 60)

    # Load data
    logger.info("Loading data...")
    train_df, test_df, resolutions = load_all_with_labels(
        include_orderbook=include_orderbook,
        sample_frac=sample_frac,
    )

    # Engineer features
    logger.info("Engineering features...")
    train_df = engineer_all_features(train_df, include_rolling=True)
    test_df = engineer_all_features(test_df, include_rolling=True)

    # Get feature columns
    feature_cols = get_feature_columns(train_df)
    logger.info(f"Using {len(feature_cols)} features")

    # Prepare data
    X_train, X_test, y_train, y_test = prepare_data(
        train_df, test_df, feature_cols, target_col="winner_binary"
    )

    logger.info(f"X_train shape: {X_train.shape}")
    logger.info(f"X_test shape: {X_test.shape}")

    # Train models
    results = {}

    # 1. Logistic Regression
    results["LogisticRegression"] = train_logistic_regression(
        X_train, y_train, X_test, y_test, feature_cols
    )

    # 2. Random Forest
    results["RandomForest"] = train_random_forest(
        X_train, y_train, X_test, y_test, feature_cols
    )

    # 3. XGBoost
    xgb_result = train_xgboost(X_train, y_train, X_test, y_test, feature_cols)
    if xgb_result:
        results["XGBoost"] = xgb_result

    # Summary
    logger.info("=" * 60)
    logger.info("MODEL COMPARISON")
    logger.info("=" * 60)
    print(f"\n{'Model':<20} {'Accuracy':>10} {'AUC':>10} {'F1':>10} {'CV Mean':>10}")
    print("-" * 60)
    for name, result in results.items():
        cv_mean = np.mean(result.cv_scores) if result.cv_scores else 0
        print(f"{name:<20} {result.accuracy:>10.4f} {result.auc:>10.4f} {result.f1:>10.4f} {cv_mean:>10.4f}")
    print("-" * 60)

    # Best model
    best_name = max(results.keys(), key=lambda x: results[x].auc)
    print(f"\nBest model by AUC: {best_name} (AUC = {results[best_name].auc:.4f})")

    # Feature importance from best model
    best_result = results[best_name]
    if best_result.feature_importance:
        print(f"\nTop 10 Features ({best_name}):")
        for i, (feat, imp) in enumerate(list(best_result.feature_importance.items())[:10], 1):
            print(f"  {i}. {feat}: {imp:.4f}")

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Train ML models for market prediction")
    parser.add_argument("--sample", type=float, default=None, help="Sample fraction (e.g., 0.1)")
    parser.add_argument("--no-orderbook", action="store_true", help="Exclude orderbook features")
    args = parser.parse_args()

    results = train_all_models(
        sample_frac=args.sample,
        include_orderbook=not args.no_orderbook,
    )

    # Save results summary
    summary = []
    for name, result in results.items():
        summary.append({
            "model": name,
            "accuracy": result.accuracy,
            "auc": result.auc,
            "f1": result.f1,
            "precision": result.precision,
            "recall": result.recall,
            "cv_mean": np.mean(result.cv_scores),
            "cv_std": np.std(result.cv_scores),
        })

    summary_df = pd.DataFrame(summary)
    summary_path = MODEL_DIR / "model_comparison.csv"
    summary_df.to_csv(summary_path, index=False)
    print(f"\nSaved model comparison to {summary_path}")
