"""
Baseline Models for Gabagool Strategy Learning

Baselines:
1. MLP - Tests if temporal structure matters
2. Random Forest - Interpretable baseline (feature importance)
3. XGBoost - Gradient boosting baseline (optional)
"""

import numpy as np
import torch
import torch.nn as nn
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.multioutput import MultiOutputClassifier, MultiOutputRegressor
import joblib


@dataclass
class MLPConfig:
    """Configuration for MLP baseline."""
    input_dim: int = 80
    seq_length: int = 100  # Will be flattened
    hidden_dims: tuple = (512, 256, 128)
    dropout: float = 0.3
    n_grid_levels: int = 5


class MLPBaseline(nn.Module):
    """
    Simple MLP baseline that flattens sequences.

    Tests whether temporal structure is important - if TCN/Transformer
    only marginally outperforms MLP, temporal patterns may not be critical.
    """

    def __init__(self, config: Optional[MLPConfig] = None):
        super().__init__()

        if config is None:
            config = MLPConfig()
        self.config = config

        # Flatten input
        input_size = config.input_dim * config.seq_length

        # Build MLP layers
        layers = []
        in_dim = input_size
        for hidden_dim in config.hidden_dims:
            layers.extend([
                nn.Linear(in_dim, hidden_dim),
                nn.ReLU(),
                nn.Dropout(config.dropout),
            ])
            in_dim = hidden_dim

        self.backbone = nn.Sequential(*layers)

        # Output heads
        final_dim = config.hidden_dims[-1]

        self.fill_head = nn.Sequential(
            nn.Linear(final_dim, 2),
            nn.Sigmoid(),
        )

        self.imbalance_head = nn.Sequential(
            nn.Linear(final_dim, 1),
            nn.Tanh(),
        )

        self.pnl_head = nn.Linear(final_dim, 1)

        self.grid_head = nn.Linear(final_dim, config.n_grid_levels)

    def forward(self, x: torch.Tensor) -> Dict[str, torch.Tensor]:
        """
        Forward pass.

        Args:
            x: [batch, seq_len, features]

        Returns:
            Dict with outputs
        """
        # Flatten sequence
        batch_size = x.size(0)
        x = x.view(batch_size, -1)  # [batch, seq_len * features]

        # MLP backbone
        x = self.backbone(x)

        # Output heads
        outputs = {
            'fill': self.fill_head(x),
            'imbalance': self.imbalance_head(x).squeeze(-1),
            'pnl': self.pnl_head(x).squeeze(-1),
            'grid_level': self.grid_head(x),
        }

        return outputs


class RandomForestBaseline:
    """
    Random Forest baseline for interpretability.

    Uses separate models for each prediction task:
    - fill_up, fill_down: RandomForestClassifier
    - imbalance: RandomForestRegressor
    - pnl: RandomForestRegressor
    - grid_level: RandomForestClassifier

    Features are averaged across the sequence (loses temporal info intentionally).
    """

    def __init__(self, n_estimators: int = 100, max_depth: int = 10,
                 n_jobs: int = -1, random_state: int = 42):
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.n_jobs = n_jobs
        self.random_state = random_state

        # Models
        self.fill_model: Optional[MultiOutputClassifier] = None
        self.imbalance_model: Optional[RandomForestRegressor] = None
        self.pnl_model: Optional[RandomForestRegressor] = None
        self.grid_model: Optional[RandomForestClassifier] = None

        self.feature_names: List[str] = []
        self._fitted = False

    def _aggregate_features(self, X: np.ndarray) -> np.ndarray:
        """
        Aggregate sequence features for RF.

        Input: [N, seq_len, features]
        Output: [N, features*3] (mean, std, last)
        """
        # Mean across time
        X_mean = X.mean(axis=1)
        # Std across time
        X_std = X.std(axis=1)
        # Last timestep
        X_last = X[:, -1, :]

        # Concatenate
        return np.concatenate([X_mean, X_std, X_last], axis=1)

    def fit(self, X: np.ndarray, y: Dict[str, np.ndarray],
            feature_names: Optional[List[str]] = None):
        """
        Fit all models.

        Args:
            X: [N, seq_len, features]
            y: Dict with 'fill', 'imbalance', 'pnl', 'grid_level'
            feature_names: Optional feature names for interpretability
        """
        # Aggregate features
        X_agg = self._aggregate_features(X)

        if feature_names:
            self.feature_names = (
                [f"{n}_mean" for n in feature_names] +
                [f"{n}_std" for n in feature_names] +
                [f"{n}_last" for n in feature_names]
            )

        print(f"Fitting Random Forest baselines...")
        print(f"  Feature dim: {X_agg.shape[1]}")
        print(f"  Samples: {len(X_agg)}")

        # Fill model (multi-output classifier)
        self.fill_model = MultiOutputClassifier(
            RandomForestClassifier(
                n_estimators=self.n_estimators,
                max_depth=self.max_depth,
                n_jobs=self.n_jobs,
                random_state=self.random_state,
            )
        )
        y_fill = y['fill'].astype(int)  # [N, 2]
        self.fill_model.fit(X_agg, y_fill)
        print("  Fill model fitted")

        # Imbalance model (regressor)
        self.imbalance_model = RandomForestRegressor(
            n_estimators=self.n_estimators,
            max_depth=self.max_depth,
            n_jobs=self.n_jobs,
            random_state=self.random_state,
        )
        self.imbalance_model.fit(X_agg, y['imbalance'])
        print("  Imbalance model fitted")

        # PnL model (regressor)
        self.pnl_model = RandomForestRegressor(
            n_estimators=self.n_estimators,
            max_depth=self.max_depth,
            n_jobs=self.n_jobs,
            random_state=self.random_state,
        )
        self.pnl_model.fit(X_agg, y['pnl'])
        print("  PnL model fitted")

        # Grid model (classifier)
        self.grid_model = RandomForestClassifier(
            n_estimators=self.n_estimators,
            max_depth=self.max_depth,
            n_jobs=self.n_jobs,
            random_state=self.random_state,
        )
        self.grid_model.fit(X_agg, y['grid_level'])
        print("  Grid model fitted")

        self._fitted = True

    def predict(self, X: np.ndarray) -> Dict[str, np.ndarray]:
        """
        Predict all outputs.

        Args:
            X: [N, seq_len, features]

        Returns:
            Dict with predictions
        """
        if not self._fitted:
            raise RuntimeError("Models not fitted. Call fit() first.")

        X_agg = self._aggregate_features(X)

        # Fill predictions (probabilities)
        fill_proba = np.array([
            clf.predict_proba(X_agg)[:, 1]
            for clf in self.fill_model.estimators_
        ]).T  # [N, 2]

        outputs = {
            'fill': fill_proba,
            'imbalance': self.imbalance_model.predict(X_agg),
            'pnl': self.pnl_model.predict(X_agg),
            'grid_level': self.grid_model.predict_proba(X_agg),  # [N, n_classes]
        }

        return outputs

    def get_feature_importance(self, task: str = 'fill') -> Dict[str, float]:
        """
        Get feature importance for interpretability.

        Args:
            task: 'fill', 'imbalance', 'pnl', or 'grid_level'

        Returns:
            Dict mapping feature names to importance scores
        """
        if not self._fitted:
            raise RuntimeError("Models not fitted")

        if task == 'fill':
            # Average across fill_up and fill_down
            importances = np.mean([
                est.feature_importances_ for est in self.fill_model.estimators_
            ], axis=0)
        elif task == 'imbalance':
            importances = self.imbalance_model.feature_importances_
        elif task == 'pnl':
            importances = self.pnl_model.feature_importances_
        elif task == 'grid_level':
            importances = self.grid_model.feature_importances_
        else:
            raise ValueError(f"Unknown task: {task}")

        if self.feature_names:
            return dict(zip(self.feature_names, importances))
        return {f"feature_{i}": imp for i, imp in enumerate(importances)}

    def get_top_features(self, task: str = 'fill', n: int = 20) -> List[Tuple[str, float]]:
        """Get top N most important features."""
        importances = self.get_feature_importance(task)
        sorted_features = sorted(importances.items(), key=lambda x: -x[1])
        return sorted_features[:n]

    def save(self, path: str):
        """Save models to disk."""
        joblib.dump({
            'fill_model': self.fill_model,
            'imbalance_model': self.imbalance_model,
            'pnl_model': self.pnl_model,
            'grid_model': self.grid_model,
            'feature_names': self.feature_names,
        }, path)

    def load(self, path: str):
        """Load models from disk."""
        data = joblib.load(path)
        self.fill_model = data['fill_model']
        self.imbalance_model = data['imbalance_model']
        self.pnl_model = data['pnl_model']
        self.grid_model = data['grid_model']
        self.feature_names = data['feature_names']
        self._fitted = True


def create_random_forest_baseline(n_estimators: int = 100,
                                  max_depth: int = 10) -> RandomForestBaseline:
    """Factory function for RF baseline."""
    return RandomForestBaseline(n_estimators=n_estimators, max_depth=max_depth)


def count_parameters(model: nn.Module) -> int:
    """Count trainable parameters."""
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


if __name__ == "__main__":
    # Test MLP baseline
    print("Testing MLP Baseline...")

    config = MLPConfig(input_dim=80, seq_length=100)
    model = MLPBaseline(config)
    print(f"MLP parameters: {count_parameters(model):,}")

    batch_size = 32
    x = torch.randn(batch_size, config.seq_length, config.input_dim)
    outputs = model(x)

    print("\nMLP Output shapes:")
    for key, tensor in outputs.items():
        print(f"  {key}: {tensor.shape}")

    # Test RF baseline
    print("\nTesting Random Forest Baseline...")

    rf = RandomForestBaseline(n_estimators=10, max_depth=5)  # Small for testing

    # Dummy data
    n_samples = 100
    X = np.random.randn(n_samples, 100, 80).astype(np.float32)
    y = {
        'fill': np.random.randint(0, 2, (n_samples, 2)),
        'imbalance': np.random.randn(n_samples).astype(np.float32),
        'pnl': np.random.randn(n_samples).astype(np.float32),
        'grid_level': np.random.randint(0, 5, n_samples),
    }

    rf.fit(X, y)

    predictions = rf.predict(X[:10])
    print("\nRF Prediction shapes:")
    for key, arr in predictions.items():
        print(f"  {key}: {arr.shape}")

    # Feature importance
    top_features = rf.get_top_features('imbalance', n=5)
    print("\nTop imbalance features:")
    for name, importance in top_features:
        print(f"  {name}: {importance:.4f}")
