#!/usr/bin/env python3
"""
Train Winner Prediction Model

Implements two approaches:
1. Approach A: Predict Gabagool's market bias (reverse engineering)
2. Approach B: Predict actual market winner (direct prediction)

Model options:
1. Logistic Regression - baseline, interpretable
2. XGBoost - recommended, handles non-linear interactions
3. TCN Classifier - if temporal patterns matter

Usage:
    python train_winner_model.py --approach both --model xgboost
    python train_winner_model.py --approach gabagool --model logistic --validate
"""

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import logging
import warnings

import numpy as np
import pandas as pd

# Suppress warnings
warnings.filterwarnings('ignore')

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import local modules
from research.ml.winner_prediction.cross_reference import (
    load_gabagool_trades,
    load_observer_data,
    cross_reference_trades_to_observer,
    validate_cross_reference,
)
from research.ml.winner_prediction.gabagool_bias import (
    compute_all_market_biases,
    validate_bias_against_resolutions,
    get_gabagool_bias_labels,
)
from research.ml.winner_prediction.resolution_loader import (
    load_all_resolutions,
    get_resolution_statistics,
)
from research.ml.winner_prediction.winner_features import (
    compute_winner_features,
    WinnerFeatureConfig,
)

# Import ML libraries
try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import (
        accuracy_score, roc_auc_score, classification_report,
        confusion_matrix, f1_score
    )
    from sklearn.model_selection import train_test_split, cross_val_score
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    logger.warning("sklearn not available - install with: pip install scikit-learn")

try:
    import xgboost as xgb
    XGBOOST_AVAILABLE = True
except ImportError:
    XGBOOST_AVAILABLE = False
    logger.warning("xgboost not available - install with: pip install xgboost")

try:
    import lightgbm as lgb
    LIGHTGBM_AVAILABLE = True
except ImportError:
    LIGHTGBM_AVAILABLE = False


# Output directory
OUTPUT_DIR = PROJECT_ROOT / "research" / "ml" / "winner_prediction" / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)


def prepare_data_for_training(
    observer_df: pd.DataFrame,
    trades_df: Optional[pd.DataFrame],
    resolutions: Dict[str, str],
    approach: str = 'both',
    sample_rate: int = 10,  # Use every Nth row to reduce data size
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
    """
    Prepare feature matrix and labels for training.

    Args:
        observer_df: Observer data
        trades_df: Gabagool trades (for approach A)
        resolutions: Market resolutions (for approach B)
        approach: 'gabagool', 'winner', or 'both'
        sample_rate: Downsample factor for training efficiency

    Returns:
        X_train, X_val, y_train, y_val
    """
    logger.info("Preparing data for training...")

    # Add resolution to observer data
    observer_df = observer_df.copy()
    observer_df['resolution'] = observer_df['market_slug'].map(resolutions)

    # Filter to markets with known resolution
    observer_df = observer_df[observer_df['resolution'].isin(['UP', 'DOWN'])]
    logger.info(f"  Markets with resolution: {observer_df['market_slug'].nunique()}")

    # Compute winner features
    logger.info("  Computing winner features...")
    winner_features = compute_winner_features(observer_df)

    # Combine with base features
    feature_cols = winner_features.columns.tolist()

    # Add some raw features if available
    raw_cols = ['velocity_bps', 'up_imbalance', 'down_imbalance', 'pair_cost', 'spike_magnitude']
    for col in raw_cols:
        if col in observer_df.columns and col not in feature_cols:
            winner_features[col] = observer_df[col].values
            feature_cols.append(col)

    # Create target variable
    if approach == 'gabagool' and trades_df is not None:
        # Approach A: Predict Gabagool's bias
        bias_df = compute_all_market_biases(trades_df)
        bias_map = dict(zip(bias_df['market_slug'], bias_df['bias']))
        observer_df['target'] = observer_df['market_slug'].map(bias_map)
        observer_df['target'] = (observer_df['target'] == 'UP').astype(int)
        logger.info(f"  Target: Gabagool bias (UP=1, DOWN=0)")
    else:
        # Approach B: Predict actual winner
        observer_df['target'] = (observer_df['resolution'] == 'UP').astype(int)
        logger.info(f"  Target: Market resolution (UP=1, DOWN=0)")

    # Filter to rows with valid target
    valid_mask = observer_df['target'].notna()
    winner_features = winner_features[valid_mask]
    targets = observer_df.loc[valid_mask, 'target'].astype(int)
    market_slugs = observer_df.loc[valid_mask, 'market_slug']

    # Downsample for training efficiency
    if sample_rate > 1:
        indices = np.arange(0, len(winner_features), sample_rate)
        winner_features = winner_features.iloc[indices]
        targets = targets.iloc[indices]
        market_slugs = market_slugs.iloc[indices]
        logger.info(f"  Downsampled to {len(winner_features):,} rows (1/{sample_rate})")

    # Split by market (prevent data leakage)
    unique_markets = market_slugs.unique()
    train_markets, val_markets = train_test_split(
        unique_markets, test_size=0.2, random_state=42
    )

    train_mask = market_slugs.isin(train_markets)
    val_mask = market_slugs.isin(val_markets)

    X_train = winner_features[train_mask]
    X_val = winner_features[val_mask]
    y_train = targets[train_mask]
    y_val = targets[val_mask]

    logger.info(f"  Train: {len(X_train):,} samples, {len(train_markets)} markets")
    logger.info(f"  Val: {len(X_val):,} samples, {len(val_markets)} markets")
    logger.info(f"  Features: {len(feature_cols)}")

    return X_train, X_val, y_train, y_val


def train_logistic_regression(
    X_train: pd.DataFrame,
    X_val: pd.DataFrame,
    y_train: pd.Series,
    y_val: pd.Series,
) -> Dict:
    """
    Train logistic regression baseline.

    Returns dict with model, metrics, and feature importances.
    """
    if not SKLEARN_AVAILABLE:
        return {'error': 'sklearn not available'}

    logger.info("\nTraining Logistic Regression...")

    # Scale features
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train.fillna(0))
    X_val_scaled = scaler.transform(X_val.fillna(0))

    # Train model
    model = LogisticRegression(
        max_iter=1000,
        class_weight='balanced',
        random_state=42,
        C=0.1,  # Regularization
    )
    model.fit(X_train_scaled, y_train)

    # Predictions
    y_train_pred = model.predict(X_train_scaled)
    y_val_pred = model.predict(X_val_scaled)
    y_val_proba = model.predict_proba(X_val_scaled)[:, 1]

    # Metrics
    train_acc = accuracy_score(y_train, y_train_pred)
    val_acc = accuracy_score(y_val, y_val_pred)
    val_auc = roc_auc_score(y_val, y_val_proba)
    val_f1 = f1_score(y_val, y_val_pred)

    # Feature importances (coefficients)
    feature_importance = pd.DataFrame({
        'feature': X_train.columns,
        'importance': np.abs(model.coef_[0]),
        'coefficient': model.coef_[0],
    }).sort_values('importance', ascending=False)

    logger.info(f"  Train Accuracy: {train_acc:.3f}")
    logger.info(f"  Val Accuracy: {val_acc:.3f}")
    logger.info(f"  Val AUC-ROC: {val_auc:.3f}")
    logger.info(f"  Val F1: {val_f1:.3f}")

    return {
        'model': model,
        'scaler': scaler,
        'train_accuracy': train_acc,
        'val_accuracy': val_acc,
        'val_auc': val_auc,
        'val_f1': val_f1,
        'feature_importance': feature_importance,
        'confusion_matrix': confusion_matrix(y_val, y_val_pred),
    }


def train_xgboost(
    X_train: pd.DataFrame,
    X_val: pd.DataFrame,
    y_train: pd.Series,
    y_val: pd.Series,
) -> Dict:
    """
    Train XGBoost classifier (recommended model).

    Returns dict with model, metrics, and feature importances.
    """
    if not XGBOOST_AVAILABLE:
        return {'error': 'xgboost not available'}

    logger.info("\nTraining XGBoost...")

    # Handle NaN
    X_train_clean = X_train.fillna(0)
    X_val_clean = X_val.fillna(0)

    # Create DMatrix
    dtrain = xgb.DMatrix(X_train_clean, label=y_train)
    dval = xgb.DMatrix(X_val_clean, label=y_val)

    # Parameters
    params = {
        'objective': 'binary:logistic',
        'eval_metric': ['logloss', 'auc'],
        'max_depth': 6,
        'learning_rate': 0.05,
        'subsample': 0.8,
        'colsample_bytree': 0.8,
        'min_child_weight': 10,
        'reg_alpha': 0.1,
        'reg_lambda': 1.0,
        'seed': 42,
    }

    # Train
    evals = [(dtrain, 'train'), (dval, 'val')]
    model = xgb.train(
        params,
        dtrain,
        num_boost_round=200,
        evals=evals,
        early_stopping_rounds=20,
        verbose_eval=False,
    )

    # Predictions
    y_train_proba = model.predict(dtrain)
    y_val_proba = model.predict(dval)
    y_train_pred = (y_train_proba > 0.5).astype(int)
    y_val_pred = (y_val_proba > 0.5).astype(int)

    # Metrics
    train_acc = accuracy_score(y_train, y_train_pred)
    val_acc = accuracy_score(y_val, y_val_pred)
    val_auc = roc_auc_score(y_val, y_val_proba)
    val_f1 = f1_score(y_val, y_val_pred)

    # Feature importances
    importance_dict = model.get_score(importance_type='gain')
    feature_importance = pd.DataFrame([
        {'feature': k, 'importance': v}
        for k, v in importance_dict.items()
    ]).sort_values('importance', ascending=False)

    logger.info(f"  Train Accuracy: {train_acc:.3f}")
    logger.info(f"  Val Accuracy: {val_acc:.3f}")
    logger.info(f"  Val AUC-ROC: {val_auc:.3f}")
    logger.info(f"  Val F1: {val_f1:.3f}")

    return {
        'model': model,
        'train_accuracy': train_acc,
        'val_accuracy': val_acc,
        'val_auc': val_auc,
        'val_f1': val_f1,
        'feature_importance': feature_importance,
        'confusion_matrix': confusion_matrix(y_val, y_val_pred),
    }


def train_random_forest(
    X_train: pd.DataFrame,
    X_val: pd.DataFrame,
    y_train: pd.Series,
    y_val: pd.Series,
) -> Dict:
    """
    Train Random Forest classifier.
    """
    if not SKLEARN_AVAILABLE:
        return {'error': 'sklearn not available'}

    logger.info("\nTraining Random Forest...")

    # Handle NaN
    X_train_clean = X_train.fillna(0)
    X_val_clean = X_val.fillna(0)

    # Train model
    model = RandomForestClassifier(
        n_estimators=100,
        max_depth=10,
        min_samples_split=20,
        min_samples_leaf=10,
        class_weight='balanced',
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_train_clean, y_train)

    # Predictions
    y_train_pred = model.predict(X_train_clean)
    y_val_pred = model.predict(X_val_clean)
    y_val_proba = model.predict_proba(X_val_clean)[:, 1]

    # Metrics
    train_acc = accuracy_score(y_train, y_train_pred)
    val_acc = accuracy_score(y_val, y_val_pred)
    val_auc = roc_auc_score(y_val, y_val_proba)
    val_f1 = f1_score(y_val, y_val_pred)

    # Feature importances
    feature_importance = pd.DataFrame({
        'feature': X_train.columns,
        'importance': model.feature_importances_,
    }).sort_values('importance', ascending=False)

    logger.info(f"  Train Accuracy: {train_acc:.3f}")
    logger.info(f"  Val Accuracy: {val_acc:.3f}")
    logger.info(f"  Val AUC-ROC: {val_auc:.3f}")
    logger.info(f"  Val F1: {val_f1:.3f}")

    return {
        'model': model,
        'train_accuracy': train_acc,
        'val_accuracy': val_acc,
        'val_auc': val_auc,
        'val_f1': val_f1,
        'feature_importance': feature_importance,
        'confusion_matrix': confusion_matrix(y_val, y_val_pred),
    }


def analyze_feature_importance(results: Dict, top_n: int = 20) -> None:
    """
    Analyze and print feature importance from training results.
    """
    logger.info("\n" + "=" * 60)
    logger.info("FEATURE IMPORTANCE ANALYSIS")
    logger.info("=" * 60)

    for model_name, result in results.items():
        if 'feature_importance' not in result:
            continue

        fi = result['feature_importance'].head(top_n)
        logger.info(f"\n{model_name} - Top {top_n} Features:")
        logger.info("-" * 50)

        for i, row in fi.iterrows():
            feature = row['feature']
            importance = row['importance']
            logger.info(f"  {feature:40} {importance:.4f}")


def save_results(results: Dict, approach: str) -> None:
    """
    Save training results to disk.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = OUTPUT_DIR / f"training_results_{approach}_{timestamp}.json"

    # Prepare serializable results
    serializable = {}
    for model_name, result in results.items():
        serializable[model_name] = {
            'train_accuracy': float(result.get('train_accuracy', 0)),
            'val_accuracy': float(result.get('val_accuracy', 0)),
            'val_auc': float(result.get('val_auc', 0)),
            'val_f1': float(result.get('val_f1', 0)),
        }

        if 'feature_importance' in result:
            fi = result['feature_importance'].head(30)
            serializable[model_name]['top_features'] = fi.to_dict('records')

        if 'confusion_matrix' in result:
            serializable[model_name]['confusion_matrix'] = result['confusion_matrix'].tolist()

    with open(output_file, 'w') as f:
        json.dump(serializable, f, indent=2)

    logger.info(f"\nResults saved to: {output_file}")


def main():
    """Main training function."""
    parser = argparse.ArgumentParser(description='Train Winner Prediction Model')
    parser.add_argument('--approach', type=str, default='both',
                        choices=['gabagool', 'winner', 'both'],
                        help='Prediction approach')
    parser.add_argument('--model', type=str, default='all',
                        choices=['logistic', 'xgboost', 'rf', 'all'],
                        help='Model type')
    parser.add_argument('--validate', action='store_true',
                        help='Run validation only')
    parser.add_argument('--sample-rate', type=int, default=10,
                        help='Downsampling rate (default: 10)')
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("WINNER PREDICTION MODEL TRAINING")
    logger.info("=" * 60)
    logger.info(f"Approach: {args.approach}")
    logger.info(f"Model: {args.model}")
    logger.info(f"Sample rate: 1/{args.sample_rate}")

    # Load data
    logger.info("\n--- Loading Data ---")

    try:
        trades_df = load_gabagool_trades()
        logger.info(f"Trades: {len(trades_df):,}")
    except FileNotFoundError as e:
        logger.warning(f"Could not load trades: {e}")
        trades_df = None

    try:
        observer_df = load_observer_data()
        logger.info(f"Observer: {len(observer_df):,} rows")
    except FileNotFoundError as e:
        logger.error(f"Could not load observer data: {e}")
        return

    resolution_data = load_all_resolutions()
    resolutions = resolution_data.resolutions
    logger.info(f"Resolutions: {len(resolutions)}")

    # Validate data
    if args.validate:
        logger.info("\n--- Running Validation ---")

        # Cross-reference validation
        if trades_df is not None:
            matched = cross_reference_trades_to_observer(trades_df, observer_df)
            validation = validate_cross_reference(matched)

            # Bias validation
            bias_df = compute_all_market_biases(trades_df)
            bias_validation = validate_bias_against_resolutions(bias_df, resolutions)

        # Resolution statistics
        res_stats = get_resolution_statistics(resolution_data)
        logger.info(f"\nResolution stats: UP={res_stats['up_ratio']*100:.1f}%, DOWN={res_stats['down_ratio']*100:.1f}%")

        return

    # Prepare training data
    approaches = ['gabagool', 'winner'] if args.approach == 'both' else [args.approach]

    for approach in approaches:
        logger.info(f"\n{'='*60}")
        logger.info(f"APPROACH: {'Gabagool Bias' if approach == 'gabagool' else 'Winner'} Prediction")
        logger.info("=" * 60)

        X_train, X_val, y_train, y_val = prepare_data_for_training(
            observer_df=observer_df,
            trades_df=trades_df,
            resolutions=resolutions,
            approach=approach,
            sample_rate=args.sample_rate,
        )

        # Train models
        results = {}

        models_to_train = []
        if args.model == 'all':
            models_to_train = ['logistic', 'xgboost', 'rf']
        else:
            models_to_train = [args.model]

        for model_type in models_to_train:
            if model_type == 'logistic':
                results['Logistic Regression'] = train_logistic_regression(
                    X_train, X_val, y_train, y_val
                )
            elif model_type == 'xgboost':
                results['XGBoost'] = train_xgboost(
                    X_train, X_val, y_train, y_val
                )
            elif model_type == 'rf':
                results['Random Forest'] = train_random_forest(
                    X_train, X_val, y_train, y_val
                )

        # Analyze feature importance
        analyze_feature_importance(results)

        # Print summary
        logger.info("\n" + "=" * 60)
        logger.info("SUMMARY")
        logger.info("=" * 60)
        logger.info(f"{'Model':<25} {'Train Acc':>10} {'Val Acc':>10} {'Val AUC':>10}")
        logger.info("-" * 60)
        for model_name, result in results.items():
            if 'error' in result:
                continue
            logger.info(f"{model_name:<25} {result['train_accuracy']:>10.3f} {result['val_accuracy']:>10.3f} {result['val_auc']:>10.3f}")

        # Check against targets
        logger.info("\n--- Target Comparison ---")
        best_val_acc = max(r.get('val_accuracy', 0) for r in results.values() if 'error' not in r)
        best_val_auc = max(r.get('val_auc', 0) for r in results.values() if 'error' not in r)

        logger.info(f"Best Val Accuracy: {best_val_acc:.3f} (Target: >0.65)")
        logger.info(f"Best Val AUC-ROC: {best_val_auc:.3f} (Target: >0.70)")
        logger.info(f"Accuracy Target Met: {best_val_acc > 0.65}")
        logger.info(f"AUC Target Met: {best_val_auc > 0.70}")

        # Save results
        save_results(results, approach)


if __name__ == "__main__":
    main()
