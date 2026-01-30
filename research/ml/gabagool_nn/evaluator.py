"""
Evaluation Metrics and Backtest Simulation for Gabagool NN

Per-Task Metrics:
| Task | Primary Metric | Target |
|------|----------------|--------|
| Fill Prediction | AUC-ROC | > 0.75 |
| Imbalance | Direction Accuracy | > 65% |
| PnL | MAE | < $0.05/share |
| Grid Level | Top-1 Accuracy | > 50% |

Strategy-Level (Backtest Simulation):
- Win rate: Target 71.2% (match gabagool)
- Pair cost: $1.00-$1.03
- Fill distribution across 95+ price levels
"""

import torch
import torch.nn as nn
import numpy as np
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from sklearn.metrics import (
    roc_auc_score, accuracy_score, precision_recall_fscore_support,
    mean_absolute_error, mean_squared_error, confusion_matrix
)
from torch.utils.data import DataLoader
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class EvaluationMetrics:
    """Container for all evaluation metrics."""
    # Fill prediction
    fill_auc_up: float = 0.0
    fill_auc_down: float = 0.0
    fill_auc_mean: float = 0.0
    fill_accuracy_up: float = 0.0
    fill_accuracy_down: float = 0.0

    # Imbalance prediction
    imbalance_direction_accuracy: float = 0.0
    imbalance_mae: float = 0.0
    imbalance_correlation: float = 0.0

    # PnL prediction
    pnl_mae: float = 0.0
    pnl_rmse: float = 0.0
    pnl_correlation: float = 0.0

    # Grid level prediction
    grid_top1_accuracy: float = 0.0
    grid_top2_accuracy: float = 0.0
    grid_confusion_matrix: Optional[np.ndarray] = None


@dataclass
class BacktestResult:
    """Results from backtest simulation."""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_pair_cost: float = 0.0
    unique_price_levels: int = 0
    avg_trades_per_market: float = 0.0
    direction_accuracy: float = 0.0
    trades: List[Dict] = field(default_factory=list)


class ModelEvaluator:
    """Evaluator for gabagool NN models."""

    def __init__(self, model: nn.Module, device: Optional[torch.device] = None):
        self.model = model
        self.device = device or torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.model.to(self.device)
        self.model.eval()

    @torch.no_grad()
    def get_predictions(self, data_loader: DataLoader) -> Tuple[Dict[str, np.ndarray], Dict[str, np.ndarray]]:
        """
        Get all predictions and targets from a data loader.

        Returns:
            predictions: Dict of numpy arrays
            targets: Dict of numpy arrays
        """
        all_preds = {'fill': [], 'imbalance': [], 'pnl': [], 'grid_level': []}
        all_targets = {'fill': [], 'imbalance': [], 'pnl': [], 'grid_level': []}

        for features, targets in data_loader:
            features = features.to(self.device)

            outputs = self.model(features)

            # Collect predictions
            all_preds['fill'].append(outputs['fill'].cpu().numpy())
            all_preds['imbalance'].append(outputs['imbalance'].cpu().numpy())
            all_preds['pnl'].append(outputs['pnl'].cpu().numpy())
            all_preds['grid_level'].append(outputs['grid_level'].cpu().numpy())

            # Collect targets
            all_targets['fill'].append(targets['fill'].numpy())
            all_targets['imbalance'].append(targets['imbalance'].numpy())
            all_targets['pnl'].append(targets['pnl'].numpy())
            all_targets['grid_level'].append(targets['grid_level'].numpy())

        # Concatenate
        predictions = {k: np.concatenate(v, axis=0) for k, v in all_preds.items()}
        targets = {k: np.concatenate(v, axis=0) for k, v in all_targets.items()}

        return predictions, targets

    def compute_metrics(self, predictions: Dict[str, np.ndarray],
                       targets: Dict[str, np.ndarray]) -> EvaluationMetrics:
        """
        Compute all evaluation metrics.

        Args:
            predictions: Model predictions
            targets: Ground truth

        Returns:
            EvaluationMetrics object
        """
        metrics = EvaluationMetrics()

        # Fill prediction metrics
        fill_pred = predictions['fill']  # [N, 2]
        fill_target = targets['fill']  # [N, 2]

        # AUC-ROC for each side
        try:
            metrics.fill_auc_up = roc_auc_score(fill_target[:, 0], fill_pred[:, 0])
        except ValueError:
            metrics.fill_auc_up = 0.5  # Default if only one class

        try:
            metrics.fill_auc_down = roc_auc_score(fill_target[:, 1], fill_pred[:, 1])
        except ValueError:
            metrics.fill_auc_down = 0.5

        metrics.fill_auc_mean = (metrics.fill_auc_up + metrics.fill_auc_down) / 2

        # Binary accuracy at 0.5 threshold
        metrics.fill_accuracy_up = accuracy_score(fill_target[:, 0], fill_pred[:, 0] > 0.5)
        metrics.fill_accuracy_down = accuracy_score(fill_target[:, 1], fill_pred[:, 1] > 0.5)

        # Imbalance direction metrics
        imb_pred = predictions['imbalance']
        imb_target = targets['imbalance']

        # Direction accuracy (sign match)
        pred_direction = np.sign(imb_pred)
        true_direction = np.sign(imb_target)
        metrics.imbalance_direction_accuracy = np.mean(pred_direction == true_direction)

        # MAE
        metrics.imbalance_mae = mean_absolute_error(imb_target, imb_pred)

        # Correlation
        if np.std(imb_pred) > 0 and np.std(imb_target) > 0:
            metrics.imbalance_correlation = np.corrcoef(imb_pred, imb_target)[0, 1]

        # PnL metrics
        pnl_pred = predictions['pnl']
        pnl_target = targets['pnl']

        metrics.pnl_mae = mean_absolute_error(pnl_target, pnl_pred)
        metrics.pnl_rmse = np.sqrt(mean_squared_error(pnl_target, pnl_pred))

        if np.std(pnl_pred) > 0 and np.std(pnl_target) > 0:
            metrics.pnl_correlation = np.corrcoef(pnl_pred, pnl_target)[0, 1]

        # Grid level metrics
        grid_pred = predictions['grid_level']  # [N, 5] logits
        grid_target = targets['grid_level'].astype(int)  # [N]

        # Top-1 accuracy
        grid_pred_class = np.argmax(grid_pred, axis=1)
        metrics.grid_top1_accuracy = accuracy_score(grid_target, grid_pred_class)

        # Top-2 accuracy
        top2_classes = np.argsort(grid_pred, axis=1)[:, -2:]
        top2_correct = np.any(top2_classes == grid_target[:, np.newaxis], axis=1)
        metrics.grid_top2_accuracy = np.mean(top2_correct)

        # Confusion matrix
        metrics.grid_confusion_matrix = confusion_matrix(grid_target, grid_pred_class,
                                                          labels=range(5))

        return metrics

    def evaluate(self, data_loader: DataLoader) -> EvaluationMetrics:
        """
        Full evaluation on a data loader.

        Args:
            data_loader: Validation/test data loader

        Returns:
            EvaluationMetrics
        """
        predictions, targets = self.get_predictions(data_loader)
        metrics = self.compute_metrics(predictions, targets)
        return metrics

    def print_metrics(self, metrics: EvaluationMetrics, title: str = "Evaluation Results"):
        """Print metrics in a formatted way."""
        print(f"\n{'='*60}")
        print(f"{title}")
        print(f"{'='*60}")

        print("\nFill Prediction:")
        print(f"  AUC-ROC UP:    {metrics.fill_auc_up:.4f} (target: > 0.75)")
        print(f"  AUC-ROC DOWN:  {metrics.fill_auc_down:.4f} (target: > 0.75)")
        print(f"  AUC-ROC Mean:  {metrics.fill_auc_mean:.4f}")
        print(f"  Accuracy UP:   {metrics.fill_accuracy_up:.4f}")
        print(f"  Accuracy DOWN: {metrics.fill_accuracy_down:.4f}")

        print("\nImbalance Direction:")
        print(f"  Direction Acc: {metrics.imbalance_direction_accuracy:.4f} (target: > 0.65)")
        print(f"  MAE:           {metrics.imbalance_mae:.4f}")
        print(f"  Correlation:   {metrics.imbalance_correlation:.4f}")

        print("\nPnL Prediction:")
        print(f"  MAE:           ${metrics.pnl_mae:.4f}/share (target: < $0.05)")
        print(f"  RMSE:          ${metrics.pnl_rmse:.4f}")
        print(f"  Correlation:   {metrics.pnl_correlation:.4f}")

        print("\nGrid Level:")
        print(f"  Top-1 Acc:     {metrics.grid_top1_accuracy:.4f} (target: > 0.50)")
        print(f"  Top-2 Acc:     {metrics.grid_top2_accuracy:.4f}")

        # Check targets
        print("\nTarget Summary:")
        targets_met = 0
        if metrics.fill_auc_mean > 0.75:
            targets_met += 1
            print("  [✓] Fill AUC > 0.75")
        else:
            print("  [✗] Fill AUC > 0.75")

        if metrics.imbalance_direction_accuracy > 0.65:
            targets_met += 1
            print("  [✓] Imbalance Direction > 65%")
        else:
            print("  [✗] Imbalance Direction > 65%")

        if metrics.pnl_mae < 0.05:
            targets_met += 1
            print("  [✓] PnL MAE < $0.05")
        else:
            print("  [✗] PnL MAE < $0.05")

        if metrics.grid_top1_accuracy > 0.50:
            targets_met += 1
            print("  [✓] Grid Top-1 > 50%")
        else:
            print("  [✗] Grid Top-1 > 50%")

        print(f"\nTargets met: {targets_met}/4")


class BacktestSimulator:
    """
    Simulate gabagool's passive grid trading using model predictions.

    Uses model predictions to:
    1. Decide when to enter (fill prediction)
    2. Decide which side is expensive (imbalance direction)
    3. Estimate PnL
    4. Select grid level
    """

    def __init__(self, model: nn.Module, device: Optional[torch.device] = None):
        self.model = model
        self.device = device or torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.model.to(self.device)
        self.model.eval()

    @torch.no_grad()
    def simulate_market(self, features: torch.Tensor,
                        resolution: str,
                        fill_threshold: float = 0.6,
                        min_time: int = 60) -> List[Dict]:
        """
        Simulate trading on a single market.

        Args:
            features: [seq_len, n_features] or [n_sequences, seq_len, n_features]
            resolution: Market resolution ('UP' or 'DOWN')
            fill_threshold: Probability threshold for fill prediction
            min_time: Minimum time remaining to enter

        Returns:
            List of trade records
        """
        trades = []

        if features.dim() == 2:
            features = features.unsqueeze(0)  # Add batch dim

        features = features.to(self.device)
        outputs = self.model(features)

        fill_probs = outputs['fill'].cpu().numpy()  # [N, 2]
        imbalance = outputs['imbalance'].cpu().numpy()  # [N]
        pnl_pred = outputs['pnl'].cpu().numpy()  # [N]
        grid_logits = outputs['grid_level'].cpu().numpy()  # [N, 5]

        # Grid offsets
        grid_offsets = [0.01, 0.02, 0.03, 0.04, 0.05]

        in_position = False
        position_data = None

        for i in range(len(fill_probs)):
            # Check for entry
            if not in_position:
                # High fill probability on either side?
                p_up_fill = fill_probs[i, 0]
                p_down_fill = fill_probs[i, 1]

                if p_up_fill > fill_threshold or p_down_fill > fill_threshold:
                    # Determine expensive side from imbalance
                    expensive_side = 'UP' if imbalance[i] > 0 else 'DOWN'

                    # Select grid level
                    grid_idx = np.argmax(grid_logits[i])
                    offset = grid_offsets[grid_idx]

                    position_data = {
                        'entry_idx': i,
                        'expensive_side': expensive_side,
                        'offset': offset,
                        'expected_pnl': pnl_pred[i],
                        'fill_prob_up': p_up_fill,
                        'fill_prob_down': p_down_fill,
                    }
                    in_position = True

            # Check for exit (simulated fill)
            elif in_position and position_data:
                # Simple simulation: exit after some steps
                if i - position_data['entry_idx'] >= 10:  # ~2 seconds
                    # Calculate result
                    correct = position_data['expensive_side'] == resolution
                    pair_cost = 1.0 - abs(position_data['expected_pnl'])

                    trade = {
                        'entry_idx': position_data['entry_idx'],
                        'exit_idx': i,
                        'expensive_side': position_data['expensive_side'],
                        'resolution': resolution,
                        'correct': correct,
                        'offset': position_data['offset'],
                        'pair_cost': pair_cost,
                        'pnl': position_data['expected_pnl'] if correct else -position_data['expected_pnl'],
                    }
                    trades.append(trade)

                    in_position = False
                    position_data = None

        return trades

    def run_backtest(self, data_loader: DataLoader,
                     market_resolutions: Dict[str, str]) -> BacktestResult:
        """
        Run full backtest across all markets.

        Args:
            data_loader: Data loader with sequences
            market_resolutions: Dict mapping market_slug to resolution

        Returns:
            BacktestResult
        """
        all_trades = []

        for batch_features, batch_labels in data_loader:
            # For simplicity, process each sample in batch
            for i in range(len(batch_features)):
                features = batch_features[i]

                # Get resolution (would need market_slug from dataset)
                resolution = 'UP'  # Default

                trades = self.simulate_market(features, resolution)
                all_trades.extend(trades)

        if not all_trades:
            return BacktestResult()

        # Compute metrics
        wins = sum(1 for t in all_trades if t['pnl'] > 0)
        losses = sum(1 for t in all_trades if t['pnl'] <= 0)
        total_pnl = sum(t['pnl'] for t in all_trades)
        avg_pair_cost = np.mean([t['pair_cost'] for t in all_trades])
        unique_levels = len(set(t['offset'] for t in all_trades))
        correct_direction = sum(1 for t in all_trades if t['correct'])

        result = BacktestResult(
            total_trades=len(all_trades),
            wins=wins,
            losses=losses,
            win_rate=wins / len(all_trades) if all_trades else 0,
            total_pnl=total_pnl,
            avg_pair_cost=avg_pair_cost,
            unique_price_levels=unique_levels,
            direction_accuracy=correct_direction / len(all_trades) if all_trades else 0,
            trades=all_trades,
        )

        return result

    def print_backtest_results(self, result: BacktestResult):
        """Print backtest results."""
        print(f"\n{'='*60}")
        print("BACKTEST RESULTS")
        print(f"{'='*60}")

        print(f"\nTrades: {result.total_trades}")
        print(f"Wins:   {result.wins}")
        print(f"Losses: {result.losses}")
        print(f"\nWin Rate:           {result.win_rate:.1%} (target: 71.2%)")
        print(f"Total PnL:          ${result.total_pnl:.2f}")
        print(f"Avg Pair Cost:      ${result.avg_pair_cost:.3f} (target: $1.00-$1.03)")
        print(f"Direction Accuracy: {result.direction_accuracy:.1%}")
        print(f"Unique Grid Levels: {result.unique_price_levels}")

        # Compare to gabagool targets
        print("\nComparison to Gabagool:")
        if result.win_rate >= 0.70:
            print(f"  [✓] Win rate >= 70%")
        else:
            print(f"  [✗] Win rate >= 70%")

        if 1.00 <= result.avg_pair_cost <= 1.03:
            print(f"  [✓] Pair cost in range")
        else:
            print(f"  [✗] Pair cost in range")


def evaluate_model(model: nn.Module,
                   val_loader: DataLoader,
                   print_results: bool = True) -> EvaluationMetrics:
    """
    Convenience function to evaluate a model.

    Args:
        model: Trained model
        val_loader: Validation data loader
        print_results: Whether to print results

    Returns:
        EvaluationMetrics
    """
    evaluator = ModelEvaluator(model)
    metrics = evaluator.evaluate(val_loader)

    if print_results:
        evaluator.print_metrics(metrics)

    return metrics


if __name__ == "__main__":
    # Test evaluation
    import torch
    from models.tcn import TCNModel, TCNConfig

    print("Testing Evaluator...")

    # Create dummy model and data
    config = TCNConfig(input_dim=80, seq_length=100)
    model = TCNModel(config)

    # Dummy predictions and targets
    n_samples = 200
    predictions = {
        'fill': np.random.rand(n_samples, 2),
        'imbalance': np.random.randn(n_samples) * 0.5,
        'pnl': np.random.randn(n_samples) * 0.05,
        'grid_level': np.random.randn(n_samples, 5),
    }

    targets = {
        'fill': np.random.randint(0, 2, (n_samples, 2)).astype(float),
        'imbalance': np.random.randn(n_samples) * 0.5,
        'pnl': np.random.randn(n_samples) * 0.05,
        'grid_level': np.random.randint(0, 5, n_samples),
    }

    # Compute metrics
    evaluator = ModelEvaluator(model)
    metrics = evaluator.compute_metrics(predictions, targets)
    evaluator.print_metrics(metrics, "Random Baseline Metrics")
