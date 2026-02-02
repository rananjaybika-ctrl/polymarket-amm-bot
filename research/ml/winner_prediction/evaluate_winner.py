#!/usr/bin/env python3
"""
Evaluate Winner Prediction Model

Comprehensive evaluation including:
1. Standard ML metrics (accuracy, AUC-ROC, F1)
2. Comparison to Gabagool's actual accuracy (~70%)
3. Accuracy vs time remaining analysis
4. Feature importance analysis
5. Backtest simulation

Usage:
    python evaluate_winner.py --model-path outputs/model.json
    python evaluate_winner.py --quick  # Quick evaluation with sample data
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import logging
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from research.ml.winner_prediction.cross_reference import (
    load_gabagool_trades,
    load_observer_data,
    cross_reference_trades_to_observer,
)
from research.ml.winner_prediction.gabagool_bias import (
    compute_all_market_biases,
    validate_bias_against_resolutions,
)
from research.ml.winner_prediction.resolution_loader import load_all_resolutions
from research.ml.winner_prediction.winner_features import compute_winner_features

try:
    from sklearn.metrics import (
        accuracy_score, roc_auc_score, f1_score,
        precision_score, recall_score, confusion_matrix,
        classification_report
    )
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False


def evaluate_gabagool_baseline(
    trades_df: pd.DataFrame,
    resolutions: Dict[str, str],
) -> Dict:
    """
    Evaluate Gabagool's actual prediction accuracy as baseline.

    This is what we're trying to match/beat.
    """
    logger.info("\n" + "=" * 60)
    logger.info("GABAGOOL BASELINE EVALUATION")
    logger.info("=" * 60)

    # Compute Gabagool's bias per market
    bias_df = compute_all_market_biases(trades_df, min_trades=10)

    # Add resolution
    bias_df['resolution'] = bias_df['market_slug'].map(resolutions)
    bias_df = bias_df[bias_df['resolution'].isin(['UP', 'DOWN'])]

    if len(bias_df) == 0:
        return {'error': 'No markets with resolution'}

    # Compute accuracy
    correct = (bias_df['bias'] == bias_df['resolution']).sum()
    total = len(bias_df)
    accuracy = correct / total

    # By bias direction
    up_bias = bias_df[bias_df['bias'] == 'UP']
    down_bias = bias_df[bias_df['bias'] == 'DOWN']

    up_accuracy = (up_bias['resolution'] == 'UP').mean() if len(up_bias) > 0 else 0
    down_accuracy = (down_bias['resolution'] == 'DOWN').mean() if len(down_bias) > 0 else 0

    # Average premium paid
    avg_premium = bias_df['premium'].mean()

    results = {
        'total_markets': total,
        'correct': correct,
        'accuracy': accuracy,
        'up_bias_count': len(up_bias),
        'up_bias_accuracy': up_accuracy,
        'down_bias_count': len(down_bias),
        'down_bias_accuracy': down_accuracy,
        'avg_premium': avg_premium,
    }

    logger.info(f"Markets evaluated: {total}")
    logger.info(f"Overall accuracy: {accuracy*100:.1f}% ({correct}/{total})")
    logger.info(f"UP-bias accuracy: {up_accuracy*100:.1f}% ({len(up_bias)} markets)")
    logger.info(f"DOWN-bias accuracy: {down_accuracy*100:.1f}% ({len(down_bias)} markets)")
    logger.info(f"Average premium paid: ${avg_premium:.4f}")

    return results


def evaluate_accuracy_by_time(
    observer_df: pd.DataFrame,
    predictions: np.ndarray,
    targets: np.ndarray,
    time_remaining: np.ndarray,
    time_buckets: List[int] = None,
) -> pd.DataFrame:
    """
    Evaluate prediction accuracy by time remaining.

    Hypothesis: Accuracy should improve as we get closer to resolution.
    """
    if time_buckets is None:
        time_buckets = [900, 600, 300, 180, 120, 60, 30]

    results = []

    for i, bucket in enumerate(time_buckets):
        if i == 0:
            mask = time_remaining >= bucket
            label = f">= {bucket}s"
        else:
            prev_bucket = time_buckets[i-1]
            mask = (time_remaining >= bucket) & (time_remaining < prev_bucket)
            label = f"{bucket}s - {prev_bucket}s"

        if mask.sum() == 0:
            continue

        bucket_preds = predictions[mask]
        bucket_targets = targets[mask]

        acc = accuracy_score(bucket_targets, bucket_preds)
        count = len(bucket_preds)

        results.append({
            'time_bucket': label,
            'min_time': bucket,
            'count': count,
            'accuracy': acc,
        })

    # Last bucket: < smallest time
    mask = time_remaining < time_buckets[-1]
    if mask.sum() > 0:
        bucket_preds = predictions[mask]
        bucket_targets = targets[mask]
        results.append({
            'time_bucket': f"< {time_buckets[-1]}s",
            'min_time': 0,
            'count': len(bucket_preds),
            'accuracy': accuracy_score(bucket_targets, bucket_preds),
        })

    return pd.DataFrame(results)


def evaluate_model_vs_gabagool(
    model_predictions: np.ndarray,
    gabagool_bias: np.ndarray,
    resolutions: np.ndarray,
) -> Dict:
    """
    Compare model predictions to Gabagool's actual decisions.

    Metrics:
    - Agreement with Gabagool
    - Model accuracy vs Gabagool accuracy
    - When they disagree, who is right?
    """
    # Agreement rate
    agreement = (model_predictions == gabagool_bias).mean()

    # Model accuracy
    model_correct = (model_predictions == resolutions)
    model_accuracy = model_correct.mean()

    # Gabagool accuracy
    gabagool_correct = (gabagool_bias == resolutions)
    gabagool_accuracy = gabagool_correct.mean()

    # When they disagree, who is right?
    disagree_mask = model_predictions != gabagool_bias
    if disagree_mask.sum() > 0:
        model_right_when_disagree = model_correct[disagree_mask].mean()
        gabagool_right_when_disagree = gabagool_correct[disagree_mask].mean()
    else:
        model_right_when_disagree = 0
        gabagool_right_when_disagree = 0

    return {
        'agreement_rate': agreement,
        'model_accuracy': model_accuracy,
        'gabagool_accuracy': gabagool_accuracy,
        'disagree_count': disagree_mask.sum(),
        'model_right_when_disagree': model_right_when_disagree,
        'gabagool_right_when_disagree': gabagool_right_when_disagree,
    }


def run_backtest_simulation(
    observer_df: pd.DataFrame,
    predictions: np.ndarray,
    prediction_proba: np.ndarray,
    resolutions: Dict[str, str],
    confidence_threshold: float = 0.6,
) -> Dict:
    """
    Simulate trading based on model predictions.

    Strategy: Trade the predicted winner when confidence > threshold.
    """
    logger.info("\n" + "=" * 60)
    logger.info("BACKTEST SIMULATION")
    logger.info("=" * 60)

    # Add predictions to dataframe
    df = observer_df.copy()
    df['prediction'] = predictions
    df['prediction_proba'] = prediction_proba
    df['predicted_winner'] = np.where(predictions == 1, 'UP', 'DOWN')

    # Filter to confident predictions
    confident_mask = (prediction_proba > confidence_threshold) | (prediction_proba < (1 - confidence_threshold))
    df_confident = df[confident_mask]

    logger.info(f"Total predictions: {len(df):,}")
    logger.info(f"Confident predictions (>{confidence_threshold*100:.0f}%): {len(df_confident):,}")

    # Simulate per market
    market_results = []

    for slug, mdf in df_confident.groupby('market_slug'):
        resolution = resolutions.get(slug)
        if resolution is None:
            continue

        # Use prediction at earliest confident time
        mdf_sorted = mdf.sort_values('time_remaining_secs', ascending=False)
        first_prediction = mdf_sorted.iloc[0]

        predicted = first_prediction['predicted_winner']
        correct = predicted == resolution

        # Estimate entry price (ask price of predicted winner)
        if predicted == 'UP':
            entry_price = first_prediction.get('up_ask', 0.55)
        else:
            entry_price = first_prediction.get('down_ask', 0.55)

        # PnL: Win = 1 - entry_price, Lose = -entry_price
        pnl = (1 - entry_price) if correct else (-entry_price)

        market_results.append({
            'market_slug': slug,
            'predicted': predicted,
            'resolution': resolution,
            'correct': correct,
            'entry_price': entry_price,
            'pnl': pnl,
            'time_remaining': first_prediction['time_remaining_secs'],
        })

    if not market_results:
        return {'error': 'No markets traded'}

    results_df = pd.DataFrame(market_results)

    # Compute metrics
    total_markets = len(results_df)
    wins = results_df['correct'].sum()
    win_rate = wins / total_markets
    total_pnl = results_df['pnl'].sum()
    avg_pnl = results_df['pnl'].mean()
    avg_entry = results_df['entry_price'].mean()

    summary = {
        'total_markets': total_markets,
        'wins': wins,
        'losses': total_markets - wins,
        'win_rate': win_rate,
        'total_pnl': total_pnl,
        'avg_pnl_per_market': avg_pnl,
        'avg_entry_price': avg_entry,
        'confidence_threshold': confidence_threshold,
    }

    logger.info(f"\nBacktest Results:")
    logger.info(f"  Markets traded: {total_markets}")
    logger.info(f"  Win rate: {win_rate*100:.1f}% ({wins}/{total_markets})")
    logger.info(f"  Total PnL: ${total_pnl:.2f}")
    logger.info(f"  Avg PnL/market: ${avg_pnl:.4f}")
    logger.info(f"  Avg entry price: ${avg_entry:.3f}")

    return summary


def generate_evaluation_report(
    gabagool_baseline: Dict,
    model_metrics: Dict,
    time_analysis: pd.DataFrame,
    backtest_results: Dict,
) -> str:
    """
    Generate comprehensive evaluation report.
    """
    report = []
    report.append("=" * 70)
    report.append("WINNER PREDICTION MODEL EVALUATION REPORT")
    report.append("=" * 70)

    # Gabagool Baseline
    report.append("\n## GABAGOOL BASELINE")
    report.append("-" * 40)
    if 'error' not in gabagool_baseline:
        report.append(f"Markets: {gabagool_baseline['total_markets']}")
        report.append(f"Accuracy: {gabagool_baseline['accuracy']*100:.1f}%")
        report.append(f"Premium paid: ${gabagool_baseline['avg_premium']:.4f}")

    # Model Performance
    report.append("\n## MODEL PERFORMANCE")
    report.append("-" * 40)
    if 'error' not in model_metrics:
        report.append(f"Validation Accuracy: {model_metrics.get('val_accuracy', 0)*100:.1f}%")
        report.append(f"Validation AUC-ROC: {model_metrics.get('val_auc', 0):.3f}")
        report.append(f"Validation F1: {model_metrics.get('val_f1', 0):.3f}")

    # Time Analysis
    report.append("\n## ACCURACY BY TIME REMAINING")
    report.append("-" * 40)
    if time_analysis is not None and len(time_analysis) > 0:
        for _, row in time_analysis.iterrows():
            report.append(f"  {row['time_bucket']:20} {row['accuracy']*100:5.1f}% (n={row['count']})")

    # Backtest
    report.append("\n## BACKTEST SIMULATION")
    report.append("-" * 40)
    if 'error' not in backtest_results:
        report.append(f"Markets traded: {backtest_results['total_markets']}")
        report.append(f"Win rate: {backtest_results['win_rate']*100:.1f}%")
        report.append(f"Total PnL: ${backtest_results['total_pnl']:.2f}")

    # Target Comparison
    report.append("\n## TARGET COMPARISON")
    report.append("-" * 40)

    targets = {
        'Winner Accuracy': (model_metrics.get('val_accuracy', 0), 0.65),
        'AUC-ROC': (model_metrics.get('val_auc', 0), 0.70),
        'Gabagool Agreement': (model_metrics.get('gabagool_agreement', 0), 0.85),
    }

    for name, (actual, target) in targets.items():
        status = "✓" if actual >= target else "✗"
        report.append(f"  {name}: {actual*100:.1f}% (target: {target*100:.0f}%) {status}")

    report.append("\n" + "=" * 70)

    return "\n".join(report)


def main():
    parser = argparse.ArgumentParser(description='Evaluate Winner Prediction Model')
    parser.add_argument('--model-path', type=str, help='Path to saved model results')
    parser.add_argument('--quick', action='store_true', help='Quick evaluation with sample data')
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("WINNER PREDICTION MODEL EVALUATION")
    logger.info("=" * 60)

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

    # Evaluate Gabagool baseline
    if trades_df is not None:
        gabagool_baseline = evaluate_gabagool_baseline(trades_df, resolutions)
    else:
        gabagool_baseline = {'error': 'No trades data'}

    # If model path provided, load and evaluate
    if args.model_path:
        model_path = Path(args.model_path)
        if model_path.exists():
            with open(model_path, 'r') as f:
                model_results = json.load(f)
            logger.info(f"\nLoaded model results from {model_path}")
        else:
            logger.error(f"Model file not found: {model_path}")
            model_results = {}
    else:
        # Train a quick model for evaluation
        logger.info("\n--- Training Quick Model for Evaluation ---")

        from research.ml.winner_prediction.train_winner_model import (
            prepare_data_for_training,
            train_xgboost,
        )

        X_train, X_val, y_train, y_val = prepare_data_for_training(
            observer_df=observer_df,
            trades_df=trades_df,
            resolutions=resolutions,
            approach='winner',
            sample_rate=20 if args.quick else 10,
        )

        model_results = train_xgboost(X_train, X_val, y_train, y_val)

    # Generate report
    report = generate_evaluation_report(
        gabagool_baseline=gabagool_baseline,
        model_metrics=model_results,
        time_analysis=None,  # Would need model predictions
        backtest_results={'error': 'Run full backtest separately'},
    )

    print(report)

    # Save report
    OUTPUT_DIR = PROJECT_ROOT / "research" / "ml" / "winner_prediction" / "outputs"
    OUTPUT_DIR.mkdir(exist_ok=True)

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = OUTPUT_DIR / f"evaluation_report_{timestamp}.txt"

    with open(report_path, 'w') as f:
        f.write(report)

    logger.info(f"\nReport saved to: {report_path}")


if __name__ == "__main__":
    main()
