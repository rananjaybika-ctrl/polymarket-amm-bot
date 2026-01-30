"""
Training Loop for Gabagool NN

Multi-task learning with weighted loss:
    loss = (1.0 * BCE(fill) +           # Fill prediction
            0.5 * MSE(imbalance) +       # Imbalance direction
            0.3 * Huber(pnl) +           # Profitability
            0.2 * CCE(grid_level))       # Grid selection

Hyperparameters:
- Batch size: 256
- Epochs: 100 (early stopping patience=10)
- Optimizer: AdamW (lr=1e-3, weight_decay=1e-4)
- LR scheduler: Cosine decay
- Gradient clipping: 1.0
"""

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import CosineAnnealingLR
import numpy as np
from typing import Dict, Optional, Tuple, List, Callable
from dataclasses import dataclass, field
import time
from pathlib import Path
import json
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class TrainingConfig:
    """Configuration for training."""
    # Optimization
    batch_size: int = 256
    epochs: int = 100
    learning_rate: float = 1e-3
    weight_decay: float = 1e-4
    gradient_clip: float = 1.0

    # Scheduler
    use_scheduler: bool = True
    scheduler_T_max: int = 100

    # Early stopping
    early_stopping: bool = True
    patience: int = 10
    min_delta: float = 1e-4

    # Loss weights
    loss_weight_fill: float = 1.0
    loss_weight_imbalance: float = 0.5
    loss_weight_pnl: float = 0.3
    loss_weight_grid: float = 0.2

    # Logging
    log_interval: int = 100
    eval_interval: int = 1  # Evaluate every N epochs

    # Checkpointing
    checkpoint_dir: str = "checkpoints"
    save_best_only: bool = True


@dataclass
class TrainingState:
    """Training state for checkpointing."""
    epoch: int = 0
    best_val_loss: float = float('inf')
    best_epoch: int = 0
    train_losses: List[float] = field(default_factory=list)
    val_losses: List[float] = field(default_factory=list)
    train_metrics: List[Dict] = field(default_factory=list)
    val_metrics: List[Dict] = field(default_factory=list)


class MultiTaskLoss(nn.Module):
    """Multi-task loss function with configurable weights."""

    def __init__(self, config: TrainingConfig):
        super().__init__()
        self.config = config

        # Loss functions
        self.fill_loss = nn.BCELoss()  # Binary cross-entropy for fill prediction
        self.imbalance_loss = nn.MSELoss()  # MSE for imbalance direction
        self.pnl_loss = nn.HuberLoss(delta=0.05)  # Huber for PnL (robust to outliers)
        self.grid_loss = nn.CrossEntropyLoss()  # Cross-entropy for grid level

    def forward(self, outputs: Dict[str, torch.Tensor],
                targets: Dict[str, torch.Tensor]) -> Tuple[torch.Tensor, Dict[str, float]]:
        """
        Compute weighted multi-task loss.

        Args:
            outputs: Model predictions
            targets: Ground truth labels

        Returns:
            total_loss: Weighted sum of losses
            loss_dict: Individual loss values
        """
        losses = {}

        # Fill prediction loss
        if 'fill' in outputs and 'fill' in targets:
            fill_pred = outputs['fill']  # [batch, 2]
            fill_target = targets['fill'].float()  # [batch, 2]
            # Clamp predictions to valid range for BCE (handle numerical issues)
            fill_pred = torch.clamp(fill_pred, 1e-7, 1 - 1e-7)
            # Clamp targets to [0, 1]
            fill_target = torch.clamp(fill_target, 0, 1)
            losses['fill'] = self.fill_loss(fill_pred, fill_target)

        # Imbalance direction loss
        if 'imbalance' in outputs and 'imbalance' in targets:
            imb_pred = outputs['imbalance']  # [batch]
            imb_target = targets['imbalance'].float()  # [batch]
            losses['imbalance'] = self.imbalance_loss(imb_pred, imb_target)

        # PnL loss
        if 'pnl' in outputs and 'pnl' in targets:
            pnl_pred = outputs['pnl']  # [batch]
            pnl_target = targets['pnl'].float()  # [batch]
            losses['pnl'] = self.pnl_loss(pnl_pred, pnl_target)

        # Grid level loss
        if 'grid_level' in outputs and 'grid_level' in targets:
            grid_pred = outputs['grid_level']  # [batch, n_levels]
            grid_target = targets['grid_level'].long()  # [batch]
            losses['grid'] = self.grid_loss(grid_pred, grid_target)

        # Weighted sum
        total_loss = (
            self.config.loss_weight_fill * losses.get('fill', 0) +
            self.config.loss_weight_imbalance * losses.get('imbalance', 0) +
            self.config.loss_weight_pnl * losses.get('pnl', 0) +
            self.config.loss_weight_grid * losses.get('grid', 0)
        )

        # Convert to plain floats for logging
        loss_dict = {k: v.item() for k, v in losses.items()}
        loss_dict['total'] = total_loss.item()

        return total_loss, loss_dict


class Trainer:
    """Training manager for Gabagool NN."""

    def __init__(self, model: nn.Module, config: Optional[TrainingConfig] = None,
                 device: Optional[torch.device] = None):
        if config is None:
            config = TrainingConfig()
        self.config = config
        self.device = device or torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        # Move model to device
        self.model = model.to(self.device)

        # Loss function
        self.loss_fn = MultiTaskLoss(config)

        # Optimizer
        self.optimizer = optim.AdamW(
            self.model.parameters(),
            lr=config.learning_rate,
            weight_decay=config.weight_decay,
        )

        # Scheduler
        if config.use_scheduler:
            self.scheduler = CosineAnnealingLR(
                self.optimizer,
                T_max=config.scheduler_T_max,
            )
        else:
            self.scheduler = None

        # State
        self.state = TrainingState()

        # Checkpoint directory
        self.checkpoint_dir = Path(config.checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    def train_epoch(self, train_loader: DataLoader) -> Tuple[float, Dict]:
        """Train for one epoch."""
        self.model.train()

        total_loss = 0.0
        loss_accum = {'fill': 0.0, 'imbalance': 0.0, 'pnl': 0.0, 'grid': 0.0}
        n_batches = 0

        for batch_idx, (features, targets) in enumerate(train_loader):
            # Move to device
            features = features.to(self.device)
            targets = {k: v.to(self.device) for k, v in targets.items()}

            # Forward pass
            self.optimizer.zero_grad()
            outputs = self.model(features)

            # Compute loss
            loss, loss_dict = self.loss_fn(outputs, targets)

            # Backward pass
            loss.backward()

            # Gradient clipping
            if self.config.gradient_clip > 0:
                torch.nn.utils.clip_grad_norm_(
                    self.model.parameters(), self.config.gradient_clip
                )

            # Update weights
            self.optimizer.step()

            # Accumulate losses
            total_loss += loss_dict['total']
            for key in loss_accum:
                if key in loss_dict:
                    loss_accum[key] += loss_dict[key]
            n_batches += 1

            # Logging
            if (batch_idx + 1) % self.config.log_interval == 0:
                avg_loss = total_loss / n_batches
                logger.info(f"  Batch {batch_idx+1}/{len(train_loader)}, Loss: {avg_loss:.4f}")

        # Average losses
        avg_loss = total_loss / n_batches
        avg_metrics = {k: v / n_batches for k, v in loss_accum.items()}
        avg_metrics['total'] = avg_loss

        return avg_loss, avg_metrics

    @torch.no_grad()
    def evaluate(self, val_loader: DataLoader) -> Tuple[float, Dict]:
        """Evaluate on validation set."""
        self.model.eval()

        total_loss = 0.0
        loss_accum = {'fill': 0.0, 'imbalance': 0.0, 'pnl': 0.0, 'grid': 0.0}
        n_batches = 0

        for features, targets in val_loader:
            features = features.to(self.device)
            targets = {k: v.to(self.device) for k, v in targets.items()}

            outputs = self.model(features)
            loss, loss_dict = self.loss_fn(outputs, targets)

            total_loss += loss_dict['total']
            for key in loss_accum:
                if key in loss_dict:
                    loss_accum[key] += loss_dict[key]
            n_batches += 1

        avg_loss = total_loss / n_batches
        avg_metrics = {k: v / n_batches for k, v in loss_accum.items()}
        avg_metrics['total'] = avg_loss

        return avg_loss, avg_metrics

    def train(self, train_loader: DataLoader, val_loader: DataLoader,
              callbacks: Optional[List[Callable]] = None) -> TrainingState:
        """
        Full training loop.

        Args:
            train_loader: Training DataLoader
            val_loader: Validation DataLoader
            callbacks: Optional list of callback functions

        Returns:
            Training state with history
        """
        logger.info(f"Starting training on {self.device}")
        logger.info(f"  Epochs: {self.config.epochs}")
        logger.info(f"  Batch size: {self.config.batch_size}")
        logger.info(f"  Learning rate: {self.config.learning_rate}")

        patience_counter = 0
        start_time = time.time()

        for epoch in range(1, self.config.epochs + 1):
            self.state.epoch = epoch
            epoch_start = time.time()

            # Train
            logger.info(f"\nEpoch {epoch}/{self.config.epochs}")
            train_loss, train_metrics = self.train_epoch(train_loader)
            self.state.train_losses.append(train_loss)
            self.state.train_metrics.append(train_metrics)

            # Validate
            if epoch % self.config.eval_interval == 0:
                val_loss, val_metrics = self.evaluate(val_loader)
                self.state.val_losses.append(val_loss)
                self.state.val_metrics.append(val_metrics)

                epoch_time = time.time() - epoch_start
                logger.info(
                    f"  Train Loss: {train_loss:.4f} | Val Loss: {val_loss:.4f} | "
                    f"Time: {epoch_time:.1f}s"
                )
                logger.info(
                    f"  Fill: {val_metrics['fill']:.4f} | Imb: {val_metrics['imbalance']:.4f} | "
                    f"PnL: {val_metrics['pnl']:.4f} | Grid: {val_metrics['grid']:.4f}"
                )

                # Early stopping check
                if val_loss < self.state.best_val_loss - self.config.min_delta:
                    self.state.best_val_loss = val_loss
                    self.state.best_epoch = epoch
                    patience_counter = 0

                    # Save best model
                    if self.config.save_best_only:
                        self.save_checkpoint('best_model.pt')
                else:
                    patience_counter += 1

                if self.config.early_stopping and patience_counter >= self.config.patience:
                    logger.info(f"\nEarly stopping at epoch {epoch}")
                    break

            # Scheduler step
            if self.scheduler is not None:
                self.scheduler.step()

            # Callbacks
            if callbacks:
                for callback in callbacks:
                    callback(self.state)

        total_time = time.time() - start_time
        logger.info(f"\nTraining complete in {total_time/60:.1f} minutes")
        logger.info(f"Best val loss: {self.state.best_val_loss:.4f} at epoch {self.state.best_epoch}")

        return self.state

    def save_checkpoint(self, filename: str):
        """Save model checkpoint."""
        path = self.checkpoint_dir / filename
        torch.save({
            'model_state_dict': self.model.state_dict(),
            'optimizer_state_dict': self.optimizer.state_dict(),
            'scheduler_state_dict': self.scheduler.state_dict() if self.scheduler else None,
            'state': {
                'epoch': self.state.epoch,
                'best_val_loss': self.state.best_val_loss,
                'best_epoch': self.state.best_epoch,
            },
            'config': self.config,
        }, path)
        logger.info(f"Saved checkpoint to {path}")

    def load_checkpoint(self, filename: str):
        """Load model checkpoint."""
        path = self.checkpoint_dir / filename
        checkpoint = torch.load(path, map_location=self.device, weights_only=False)

        self.model.load_state_dict(checkpoint['model_state_dict'])
        self.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        if self.scheduler and checkpoint['scheduler_state_dict']:
            self.scheduler.load_state_dict(checkpoint['scheduler_state_dict'])

        state = checkpoint['state']
        self.state.epoch = state['epoch']
        self.state.best_val_loss = state['best_val_loss']
        self.state.best_epoch = state['best_epoch']

        logger.info(f"Loaded checkpoint from {path}")

    def save_training_history(self, filename: str):
        """Save training history to JSON."""
        path = self.checkpoint_dir / filename
        history = {
            'train_losses': self.state.train_losses,
            'val_losses': self.state.val_losses,
            'train_metrics': self.state.train_metrics,
            'val_metrics': self.state.val_metrics,
            'best_val_loss': self.state.best_val_loss,
            'best_epoch': self.state.best_epoch,
        }
        with open(path, 'w') as f:
            json.dump(history, f, indent=2)
        logger.info(f"Saved training history to {path}")


def train_model(model: nn.Module,
                train_loader: DataLoader,
                val_loader: DataLoader,
                config: Optional[TrainingConfig] = None) -> Tuple[nn.Module, TrainingState]:
    """
    Convenience function to train a model.

    Args:
        model: Model to train
        train_loader: Training data loader
        val_loader: Validation data loader
        config: Training configuration

    Returns:
        Trained model and training state
    """
    trainer = Trainer(model, config)
    state = trainer.train(train_loader, val_loader)

    # Load best weights
    try:
        trainer.load_checkpoint('best_model.pt')
    except FileNotFoundError:
        pass

    trainer.save_training_history('training_history.json')

    return trainer.model, state


if __name__ == "__main__":
    # Test training loop
    from models.tcn import TCNModel, TCNConfig
    import torch

    print("Testing Training Loop...")

    # Create dummy data
    batch_size = 32
    seq_len = 100
    n_features = 80
    n_samples = 500

    X = torch.randn(n_samples, seq_len, n_features)
    y = {
        'fill': torch.randint(0, 2, (n_samples, 2)).float(),
        'imbalance': torch.randn(n_samples),
        'pnl': torch.randn(n_samples) * 0.1,
        'grid_level': torch.randint(0, 5, (n_samples,)),
    }

    # Create dummy dataset
    from torch.utils.data import TensorDataset, DataLoader as TDL

    class DummyDataset:
        def __init__(self, X, y):
            self.X = X
            self.y = y

        def __len__(self):
            return len(self.X)

        def __getitem__(self, idx):
            return self.X[idx], {k: v[idx] for k, v in self.y.items()}

    dataset = DummyDataset(X, y)
    train_loader = TDL(dataset, batch_size=batch_size, shuffle=True)
    val_loader = TDL(dataset, batch_size=batch_size)

    # Create model
    config = TCNConfig(input_dim=n_features, seq_length=seq_len)
    model = TCNModel(config)

    # Train for a few epochs
    train_config = TrainingConfig(
        epochs=3,
        batch_size=batch_size,
        log_interval=10,
    )

    model, state = train_model(model, train_loader, val_loader, train_config)

    print(f"\nTraining complete!")
    print(f"Best val loss: {state.best_val_loss:.4f}")
