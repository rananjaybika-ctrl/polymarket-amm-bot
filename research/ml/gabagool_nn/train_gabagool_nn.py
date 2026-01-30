#!/usr/bin/env python3
"""
Main Training Script for Gabagool NN

Trains neural networks to learn gabagool's passive two-sided grid market making
behavior from observer data.

Usage:
    python research/ml/gabagool_nn/train_gabagool_nn.py [--model tcn|transformer|mlp|rf]
                                                         [--epochs 100]
                                                         [--batch-size 256]
                                                         [--no-train]  # Just evaluate

Data Splits:
- Training: IS+OOS2 (Jan 16-19) + OOS5 (Jan 26)
- Validation: OOS3+OOS4 (Jan 20-24) + OOS6 (Jan 28-29)

Models:
- TCN: Temporal Convolutional Network (default, best for sequential data)
- Transformer: Attention-based model
- MLP: Feedforward baseline
- RF: Random Forest (interpretable baseline)
"""

import argparse
import sys
from pathlib import Path
import time
import json
import numpy as np
import torch
import logging

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from data_loader import load_training_data, get_market_data, DataSplit
from feature_engineer import engineer_features, normalize_features, FeatureConfig
from label_constructor import construct_labels, labels_to_tensors, LabelConfig, load_gabagool_trades
from sequence_builder import build_sequences, create_dataloaders, SequenceConfig, SequenceData
from trainer import Trainer, TrainingConfig, train_model
from evaluator import ModelEvaluator, evaluate_model, BacktestSimulator

from models import TCNModel, TransformerModel, MLPBaseline, create_random_forest_baseline
from models.tcn import TCNConfig
from models.transformer import TransformerConfig
from models.baselines import MLPConfig

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def prepare_data(data: DataSplit, max_markets: int = None) -> tuple:
    """
    Prepare data for training: feature engineering, labeling, sequence building.

    Args:
        data: DataSplit from data_loader
        max_markets: Optional limit on number of markets (for testing)

    Returns:
        train_data, val_data, norm_stats, feature_names
    """
    logger.info("=" * 60)
    logger.info("PREPARING DATA")
    logger.info("=" * 60)

    feature_config = FeatureConfig()
    label_config = LabelConfig()
    seq_config = SequenceConfig()

    # Load gabagool trades for trade-based fill labels
    logger.info("\nLoading gabagool trades for fill labels...")
    gabagool_trades = load_gabagool_trades()
    if gabagool_trades:
        logger.info(f"  Loaded {gabagool_trades.get('total_trades', 0)} trades from {gabagool_trades.get('markets_traded', 0)} markets")
    else:
        logger.warning("  No gabagool trades loaded - using price-based fill labels")

    # Process training data
    logger.info("\nProcessing TRAINING markets...")
    train_markets = data.train_markets[:max_markets] if max_markets else data.train_markets

    train_feature_dfs = []
    train_label_dicts = []
    train_slugs = []

    for i, slug in enumerate(train_markets):
        mdf = get_market_data(data.train_df, slug)
        resolution = data.resolutions.get(slug, 'UP')

        # Feature engineering
        features = engineer_features(mdf, feature_config)

        # Label construction (with actual gabagool trades)
        labels = construct_labels(
            mdf, resolution, label_config,
            whale_trades=gabagool_trades,
            market_slug=slug,
            use_trade_labels=True
        )
        label_dict = labels_to_tensors(labels)

        train_feature_dfs.append(features)
        train_label_dicts.append(label_dict)
        train_slugs.append(slug)

        if (i + 1) % 50 == 0:
            logger.info(f"  Processed {i+1}/{len(train_markets)} training markets")

    logger.info(f"  Total: {len(train_feature_dfs)} training markets")

    # Process validation data
    logger.info("\nProcessing VALIDATION markets...")
    val_markets = data.val_markets[:max_markets] if max_markets else data.val_markets

    val_feature_dfs = []
    val_label_dicts = []
    val_slugs = []

    for i, slug in enumerate(val_markets):
        mdf = get_market_data(data.val_df, slug)
        resolution = data.resolutions.get(slug, 'UP')

        features = engineer_features(mdf, feature_config)
        labels = construct_labels(
            mdf, resolution, label_config,
            whale_trades=gabagool_trades,
            market_slug=slug,
            use_trade_labels=True
        )
        label_dict = labels_to_tensors(labels)

        val_feature_dfs.append(features)
        val_label_dicts.append(label_dict)
        val_slugs.append(slug)

        if (i + 1) % 50 == 0:
            logger.info(f"  Processed {i+1}/{len(val_markets)} validation markets")

    logger.info(f"  Total: {len(val_feature_dfs)} validation markets")

    # Normalize features (fit on training, apply to validation)
    logger.info("\nNormalizing features...")

    # Find common feature columns between train and val
    skip_cols = ['timestamp_ms', 'market_slug', 'resolution']
    train_cols = set(c for c in train_feature_dfs[0].columns if c not in skip_cols)
    val_cols = set(c for c in val_feature_dfs[0].columns if c not in skip_cols)
    common_cols = sorted(train_cols & val_cols)

    logger.info(f"  Train features: {len(train_cols)}, Val features: {len(val_cols)}")
    logger.info(f"  Common features: {len(common_cols)}")

    # Use only common columns
    all_train_features = []
    for df in train_feature_dfs:
        all_train_features.append(df[common_cols])

    combined_train = np.vstack([df.values for df in all_train_features])
    feature_names = common_cols

    # Compute normalization stats
    norm_stats = {
        name: {'mean': combined_train[:, i].mean(), 'std': combined_train[:, i].std() + 1e-8}
        for i, name in enumerate(feature_names)
    }

    # Apply normalization to training data (using common columns)
    normalized_train_dfs = []
    for df in train_feature_dfs:
        # Keep only common features + metadata
        df_common = df[common_cols + ['timestamp_ms', 'market_slug']].copy()
        if 'resolution' in df.columns:
            df_common['resolution'] = df['resolution']
        norm_df, _ = normalize_features(df_common, norm_stats)
        normalized_train_dfs.append(norm_df)

    # Apply normalization to validation data (using common columns)
    normalized_val_dfs = []
    for df in val_feature_dfs:
        # Keep only common features + metadata
        df_common = df[common_cols + ['timestamp_ms', 'market_slug']].copy()
        if 'resolution' in df.columns:
            df_common['resolution'] = df['resolution']
        norm_df, _ = normalize_features(df_common, norm_stats)
        normalized_val_dfs.append(norm_df)

    # Build sequences
    logger.info("\nBuilding sequences...")
    logger.info(f"  Sequence length: {seq_config.sequence_length} ({seq_config.sequence_length / 5:.0f}s)")
    logger.info(f"  Stride: {seq_config.stride} ({seq_config.stride / 5:.0f}s)")

    train_seq_data = build_sequences(normalized_train_dfs, train_label_dicts, train_slugs, seq_config)
    val_seq_data = build_sequences(normalized_val_dfs, val_label_dicts, val_slugs, seq_config)

    logger.info(f"\nTraining sequences: {len(train_seq_data.features):,}")
    logger.info(f"Validation sequences: {len(val_seq_data.features):,}")
    logger.info(f"Feature dimension: {train_seq_data.features.shape[2]}")

    return train_seq_data, val_seq_data, norm_stats, feature_names


def create_model(model_type: str, input_dim: int, seq_length: int):
    """Create model based on type."""
    if model_type == 'tcn':
        config = TCNConfig(input_dim=input_dim, seq_length=seq_length)
        model = TCNModel(config)
    elif model_type == 'transformer':
        config = TransformerConfig(input_dim=input_dim, seq_length=seq_length)
        model = TransformerModel(config)
    elif model_type == 'mlp':
        config = MLPConfig(input_dim=input_dim, seq_length=seq_length)
        model = MLPBaseline(config)
    elif model_type == 'rf':
        model = create_random_forest_baseline(n_estimators=100, max_depth=10)
    else:
        raise ValueError(f"Unknown model type: {model_type}")

    return model


def train_neural_model(model, train_data: SequenceData, val_data: SequenceData,
                       config: TrainingConfig):
    """Train neural network model."""
    # Create data loaders
    train_loader, val_loader = create_dataloaders(
        train_data, val_data,
        batch_size=config.batch_size,
        num_workers=0
    )

    # Train
    trainer = Trainer(model, config)
    state = trainer.train(train_loader, val_loader)

    # Load best model
    try:
        trainer.load_checkpoint('best_model.pt')
    except FileNotFoundError:
        pass

    # Save history
    trainer.save_training_history('training_history.json')

    return trainer.model, state, val_loader


def train_rf_model(model, train_data: SequenceData, val_data: SequenceData, feature_names: list):
    """Train Random Forest baseline."""
    logger.info("Training Random Forest baseline...")

    # Prepare data
    X_train = train_data.features
    y_train = {
        'fill': train_data.labels['fill'],
        'imbalance': train_data.labels['imbalance'],
        'pnl': train_data.labels['pnl'],
        'grid_level': train_data.labels['grid_level'],
    }

    # Fit
    model.fit(X_train, y_train, feature_names=feature_names)

    return model


def evaluate_all_models(models: dict, val_loader, val_data: SequenceData):
    """Evaluate all trained models."""
    logger.info("\n" + "=" * 60)
    logger.info("EVALUATION RESULTS")
    logger.info("=" * 60)

    results = {}

    for name, model in models.items():
        logger.info(f"\n--- {name.upper()} ---")

        if name == 'rf':
            # RF evaluation
            X_val = val_data.features
            predictions = model.predict(X_val)
            targets = {
                'fill': val_data.labels['fill'],
                'imbalance': val_data.labels['imbalance'],
                'pnl': val_data.labels['pnl'],
                'grid_level': val_data.labels['grid_level'],
            }

            # Manual metric computation
            from evaluator import ModelEvaluator
            evaluator = ModelEvaluator.__new__(ModelEvaluator)
            evaluator.model = None
            metrics = evaluator.compute_metrics(predictions, targets)
            evaluator.print_metrics(metrics, f"{name.upper()} Results")

            # Feature importance
            logger.info("\nTop 10 Features (imbalance task):")
            for feat, imp in model.get_top_features('imbalance', n=10):
                logger.info(f"  {feat}: {imp:.4f}")

        else:
            # Neural network evaluation
            metrics = evaluate_model(model, val_loader, print_results=True)

        results[name] = metrics

    return results


def main():
    parser = argparse.ArgumentParser(description="Train Gabagool NN")
    parser.add_argument('--model', type=str, default='tcn',
                       choices=['tcn', 'transformer', 'mlp', 'rf', 'all'],
                       help='Model type to train')
    parser.add_argument('--epochs', type=int, default=100, help='Number of epochs')
    parser.add_argument('--batch-size', type=int, default=256, help='Batch size')
    parser.add_argument('--lr', type=float, default=1e-3, help='Learning rate')
    parser.add_argument('--no-train', action='store_true', help='Skip training, only evaluate')
    parser.add_argument('--max-markets', type=int, default=None,
                       help='Max markets to use (for testing)')
    parser.add_argument('--checkpoint-dir', type=str, default='checkpoints',
                       help='Checkpoint directory')
    args = parser.parse_args()

    # Title
    print("\n" + "=" * 60)
    print("GABAGOOL NEURAL NETWORK TRAINING")
    print("Reverse engineering passive grid market making")
    print("=" * 60 + "\n")

    start_time = time.time()

    # Load data
    logger.info("Loading data...")
    data = load_training_data()

    # Prepare data
    train_data, val_data, norm_stats, feature_names = prepare_data(
        data, max_markets=args.max_markets
    )

    # Get dimensions
    input_dim = train_data.features.shape[2]
    seq_length = train_data.features.shape[1]
    logger.info(f"\nModel input: [{args.batch_size}, {seq_length}, {input_dim}]")

    # Training config
    train_config = TrainingConfig(
        epochs=args.epochs,
        batch_size=args.batch_size,
        learning_rate=args.lr,
        checkpoint_dir=args.checkpoint_dir,
    )

    # Determine which models to train
    if args.model == 'all':
        model_types = ['tcn', 'transformer', 'mlp', 'rf']
    else:
        model_types = [args.model]

    trained_models = {}
    val_loader = None

    for model_type in model_types:
        logger.info(f"\n{'='*60}")
        logger.info(f"TRAINING: {model_type.upper()}")
        logger.info(f"{'='*60}")

        model = create_model(model_type, input_dim, seq_length)

        if model_type != 'rf':
            # Neural network
            n_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
            logger.info(f"Model parameters: {n_params:,}")

            if not args.no_train:
                model, state, val_loader = train_neural_model(
                    model, train_data, val_data, train_config
                )
            else:
                # Just create data loader for evaluation
                _, val_loader = create_dataloaders(
                    train_data, val_data,
                    batch_size=args.batch_size,
                    num_workers=0
                )

        else:
            # Random Forest
            if not args.no_train:
                model = train_rf_model(model, train_data, val_data, feature_names)

        trained_models[model_type] = model

    # Evaluate all models
    if val_loader is None:
        _, val_loader = create_dataloaders(
            train_data, val_data,
            batch_size=args.batch_size,
            num_workers=0
        )

    results = evaluate_all_models(trained_models, val_loader, val_data)

    # Summary
    total_time = time.time() - start_time
    logger.info(f"\n{'='*60}")
    logger.info("TRAINING COMPLETE")
    logger.info(f"{'='*60}")
    logger.info(f"Total time: {total_time/60:.1f} minutes")
    logger.info(f"Models trained: {', '.join(model_types)}")

    # Save normalization stats
    stats_path = Path(args.checkpoint_dir) / 'norm_stats.json'
    with open(stats_path, 'w') as f:
        json.dump(norm_stats, f, indent=2, default=float)
    logger.info(f"Saved normalization stats to {stats_path}")


if __name__ == "__main__":
    main()
