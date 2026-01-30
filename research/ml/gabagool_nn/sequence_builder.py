"""
Sequence Builder for Gabagool NN

Creates overlapping sequences from time-series data for temporal models.

Sequence Configuration:
- SEQUENCE_LENGTH = 100 (20 seconds at 5Hz)
- STRIDE = 25 (5 seconds stride, 80% overlap)
"""

import numpy as np
import pandas as pd
from typing import List, Tuple, Dict, Optional, Generator
from dataclasses import dataclass
import torch
from torch.utils.data import Dataset, DataLoader
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class SequenceConfig:
    """Configuration for sequence building."""
    sequence_length: int = 100  # 20 seconds at 5Hz
    stride: int = 25  # 5 seconds stride (80% overlap)
    sample_rate_hz: float = 5.0
    min_valid_ratio: float = 0.8  # Min fraction of valid samples in sequence


@dataclass
class SequenceData:
    """Container for sequence data."""
    features: np.ndarray  # [N_sequences, seq_len, n_features]
    labels: Dict[str, np.ndarray]  # Each [N_sequences, ...]
    market_slugs: List[str]  # Market for each sequence
    timestamps: np.ndarray  # Start timestamp for each sequence


def create_sequences_for_market(features: np.ndarray,
                                labels: Dict[str, np.ndarray],
                                config: SequenceConfig) -> Tuple[np.ndarray, Dict[str, np.ndarray], np.ndarray]:
    """
    Create overlapping sequences from a single market's data.

    Args:
        features: [T, n_features] feature array
        labels: Dictionary of label arrays, each [T, ...]
        config: Sequence configuration

    Returns:
        sequences: [N, seq_len, n_features]
        seq_labels: Dict of [N, ...] arrays
        valid_indices: [N] indices of valid sequences
    """
    T = len(features)
    seq_len = config.sequence_length
    stride = config.stride

    if T < seq_len:
        return np.array([]), {}, np.array([])

    # Calculate number of sequences
    n_sequences = (T - seq_len) // stride + 1

    # Pre-allocate arrays
    n_features = features.shape[1]
    sequences = np.zeros((n_sequences, seq_len, n_features), dtype=np.float32)

    # Initialize label arrays
    seq_labels = {}
    for key, arr in labels.items():
        if arr.ndim == 1:
            seq_labels[key] = np.zeros(n_sequences, dtype=arr.dtype)
        else:
            seq_labels[key] = np.zeros((n_sequences,) + arr.shape[1:], dtype=arr.dtype)

    valid_indices = []

    for i in range(n_sequences):
        start = i * stride
        end = start + seq_len

        # Extract sequence
        sequences[i] = features[start:end]

        # Labels: use the LAST timestep's labels (predict at sequence end)
        for key, arr in labels.items():
            if key == 'valid_mask':
                # For valid_mask, check if enough samples are valid
                seq_labels[key][i] = arr[start:end].mean() >= config.min_valid_ratio
            elif arr.ndim == 1:
                seq_labels[key][i] = arr[end - 1]  # Label at end of sequence
            else:
                seq_labels[key][i] = arr[end - 1]

        # Check validity
        if 'valid_mask' in labels:
            if seq_labels['valid_mask'][i]:
                valid_indices.append(i)
        else:
            valid_indices.append(i)

    return sequences, seq_labels, np.array(valid_indices)


def build_sequences(feature_dfs: List[pd.DataFrame],
                    label_dicts: List[Dict[str, np.ndarray]],
                    market_slugs: List[str],
                    config: Optional[SequenceConfig] = None) -> SequenceData:
    """
    Build sequences from multiple markets.

    Args:
        feature_dfs: List of feature DataFrames (one per market)
        label_dicts: List of label dictionaries (one per market)
        market_slugs: List of market slugs
        config: Sequence configuration

    Returns:
        SequenceData with all sequences
    """
    if config is None:
        config = SequenceConfig()

    all_sequences = []
    all_labels = {key: [] for key in label_dicts[0].keys()}
    all_slugs = []
    all_timestamps = []

    for i, (features_df, labels, slug) in enumerate(zip(feature_dfs, label_dicts, market_slugs)):
        # Convert features to numpy (exclude metadata columns)
        skip_cols = ['timestamp_ms', 'market_slug', 'resolution']
        feature_cols = [c for c in features_df.columns if c not in skip_cols]
        features = features_df[feature_cols].values.astype(np.float32)

        # Get timestamps
        timestamps = features_df['timestamp_ms'].values if 'timestamp_ms' in features_df.columns else np.arange(len(features))

        # Create sequences for this market
        seqs, seq_labels, valid_idx = create_sequences_for_market(features, labels, config)

        if len(valid_idx) == 0:
            continue

        # Filter to valid sequences only
        seqs = seqs[valid_idx]
        for key in seq_labels:
            seq_labels[key] = seq_labels[key][valid_idx]

        all_sequences.append(seqs)
        for key in all_labels:
            all_labels[key].append(seq_labels[key])
        all_slugs.extend([slug] * len(seqs))

        # Start timestamps for each sequence
        start_indices = valid_idx * config.stride
        all_timestamps.extend(timestamps[start_indices].tolist())

        if (i + 1) % 50 == 0:
            logger.info(f"  Processed {i+1}/{len(market_slugs)} markets")

    if not all_sequences:
        raise ValueError("No valid sequences created")

    # Concatenate all
    sequences = np.concatenate(all_sequences, axis=0)
    labels = {key: np.concatenate(arrs, axis=0) for key, arrs in all_labels.items()}

    logger.info(f"Built {len(sequences)} sequences from {len(market_slugs)} markets")
    logger.info(f"  Sequence shape: {sequences.shape}")

    return SequenceData(
        features=sequences,
        labels=labels,
        market_slugs=all_slugs,
        timestamps=np.array(all_timestamps),
    )


class GabagoolDataset(Dataset):
    """PyTorch Dataset for gabagool NN training."""

    def __init__(self, sequence_data: SequenceData):
        """
        Initialize dataset.

        Args:
            sequence_data: SequenceData object
        """
        self.features = torch.from_numpy(sequence_data.features)
        self.labels = {
            key: torch.from_numpy(arr) for key, arr in sequence_data.labels.items()
            if key != 'valid_mask'  # Exclude mask from labels
        }
        self.market_slugs = sequence_data.market_slugs
        self.timestamps = sequence_data.timestamps

    def __len__(self) -> int:
        return len(self.features)

    def __getitem__(self, idx: int) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
        """
        Get a single sample.

        Returns:
            features: [seq_len, n_features]
            labels: Dict of label tensors
        """
        features = self.features[idx]
        labels = {key: arr[idx] for key, arr in self.labels.items()}
        return features, labels


def create_dataloaders(train_data: SequenceData,
                       val_data: SequenceData,
                       batch_size: int = 256,
                       num_workers: int = 0) -> Tuple[DataLoader, DataLoader]:
    """
    Create training and validation DataLoaders.

    Args:
        train_data: Training sequence data
        val_data: Validation sequence data
        batch_size: Batch size
        num_workers: Number of data loading workers

    Returns:
        train_loader, val_loader
    """
    train_dataset = GabagoolDataset(train_data)
    val_dataset = GabagoolDataset(val_data)

    train_loader = DataLoader(
        train_dataset,
        batch_size=batch_size,
        shuffle=True,
        num_workers=num_workers,
        pin_memory=True,
        drop_last=True,
    )

    val_loader = DataLoader(
        val_dataset,
        batch_size=batch_size,
        shuffle=False,
        num_workers=num_workers,
        pin_memory=True,
    )

    logger.info(f"Created DataLoaders:")
    logger.info(f"  Train: {len(train_dataset)} samples, {len(train_loader)} batches")
    logger.info(f"  Val:   {len(val_dataset)} samples, {len(val_loader)} batches")

    return train_loader, val_loader


def get_feature_dim(sequence_data: SequenceData) -> int:
    """Get number of features per timestep."""
    return sequence_data.features.shape[2]


def get_sequence_stats(sequence_data: SequenceData) -> Dict:
    """Compute statistics on sequence data."""
    features = sequence_data.features

    stats = {
        'n_sequences': len(features),
        'seq_length': features.shape[1],
        'n_features': features.shape[2],
        'n_markets': len(set(sequence_data.market_slugs)),
        'feature_mean': features.mean(axis=(0, 1)).tolist()[:10],  # First 10
        'feature_std': features.std(axis=(0, 1)).tolist()[:10],
    }

    # Label stats
    for key, arr in sequence_data.labels.items():
        if key == 'valid_mask':
            continue
        if arr.dtype in [np.float32, np.float64]:
            stats[f'{key}_mean'] = float(arr.mean())
            stats[f'{key}_std'] = float(arr.std())
        elif 'fill' in key:
            stats[f'{key}_rate'] = float(arr.mean())

    return stats


if __name__ == "__main__":
    # Test sequence building
    from data_loader import load_training_data, get_market_data
    from feature_engineer import engineer_features, normalize_features
    from label_constructor import construct_labels, labels_to_tensors, LabelConfig

    print("Loading data...")
    data = load_training_data()

    # Process a few markets for testing
    test_markets = data.train_markets[:5]

    print(f"\nProcessing {len(test_markets)} test markets...")

    feature_dfs = []
    label_dicts = []
    slugs = []

    for slug in test_markets:
        mdf = get_market_data(data.train_df, slug)
        resolution = data.resolutions.get(slug, 'UP')

        # Feature engineering
        features = engineer_features(mdf)

        # Label construction
        label_config = LabelConfig()
        labels = construct_labels(mdf, resolution, label_config)
        label_dict = labels_to_tensors(labels)

        feature_dfs.append(features)
        label_dicts.append(label_dict)
        slugs.append(slug)

    # Normalize all features together
    all_features = pd.concat(feature_dfs, ignore_index=True)
    _, norm_stats = normalize_features(all_features)

    # Re-normalize each market with global stats
    normalized_dfs = []
    for features in feature_dfs:
        norm_features, _ = normalize_features(features, norm_stats)
        normalized_dfs.append(norm_features)

    # Build sequences
    print("\nBuilding sequences...")
    config = SequenceConfig()
    sequence_data = build_sequences(normalized_dfs, label_dicts, slugs, config)

    print(f"\nSequence data:")
    print(f"  Features shape: {sequence_data.features.shape}")
    print(f"  Markets: {len(set(sequence_data.market_slugs))}")

    stats = get_sequence_stats(sequence_data)
    print("\nStatistics:")
    for key, value in stats.items():
        if isinstance(value, list):
            print(f"  {key}: [{', '.join(f'{v:.3f}' for v in value[:5])}...]")
        elif isinstance(value, float):
            print(f"  {key}: {value:.4f}")
        else:
            print(f"  {key}: {value}")

    # Test DataLoader
    print("\nTesting DataLoader...")
    train_loader, _ = create_dataloaders(sequence_data, sequence_data, batch_size=32)

    batch_x, batch_y = next(iter(train_loader))
    print(f"  Batch features: {batch_x.shape}")
    print(f"  Batch labels:")
    for key, tensor in batch_y.items():
        print(f"    {key}: {tensor.shape}")
