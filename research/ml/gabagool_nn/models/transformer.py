"""
Transformer Model for Gabagool Strategy Learning

Alternative architecture using self-attention for sequence modeling.
May capture more complex temporal dependencies than TCN.

Architecture:
    INPUT: [batch, seq_len=100, features=80]

    Positional Encoding
    Transformer Encoder: 2 layers, 4 heads, dim=128
    Global Pooling (CLS token or mean)
    Dense: 256 -> 128

    OUTPUT HEADS:
    ├── Fill:      Dense(2, sigmoid)
    ├── Imbalance: Dense(1, tanh)
    ├── PnL:       Dense(1, linear)
    └── Grid:      Dense(5, softmax)
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
import math
from typing import Dict, Optional
from dataclasses import dataclass


@dataclass
class TransformerConfig:
    """Configuration for Transformer model."""
    input_dim: int = 80
    seq_length: int = 100

    # Transformer params
    d_model: int = 128
    n_heads: int = 4
    n_layers: int = 2
    dim_feedforward: int = 256
    dropout: float = 0.2

    # Dense layers
    dense_dims: tuple = (256, 128)

    # Output
    n_grid_levels: int = 5

    # Use CLS token for classification
    use_cls_token: bool = True


class PositionalEncoding(nn.Module):
    """Sinusoidal positional encoding."""

    def __init__(self, d_model: int, max_len: int = 200, dropout: float = 0.1):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)

        # Create positional encodings
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))

        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)  # [1, max_len, d_model]

        self.register_buffer('pe', pe)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [batch, seq_len, d_model]
        x = x + self.pe[:, :x.size(1), :]
        return self.dropout(x)


class TransformerModel(nn.Module):
    """
    Transformer model with multi-task output heads.

    Input: [batch, seq_len, features]
    Output: Dict with 'fill', 'imbalance', 'pnl', 'grid_level'
    """

    def __init__(self, config: Optional[TransformerConfig] = None):
        super().__init__()

        if config is None:
            config = TransformerConfig()
        self.config = config

        # Input projection
        self.input_proj = nn.Linear(config.input_dim, config.d_model)

        # CLS token (learnable)
        if config.use_cls_token:
            self.cls_token = nn.Parameter(torch.randn(1, 1, config.d_model))

        # Positional encoding
        max_len = config.seq_length + 1 if config.use_cls_token else config.seq_length
        self.pos_encoding = PositionalEncoding(config.d_model, max_len, config.dropout)

        # Transformer encoder
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=config.d_model,
            nhead=config.n_heads,
            dim_feedforward=config.dim_feedforward,
            dropout=config.dropout,
            batch_first=True,
        )
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=config.n_layers)

        # Dense layers
        dense_in = config.d_model
        self.dense_layers = nn.ModuleList()
        for dim in config.dense_dims:
            self.dense_layers.append(nn.Sequential(
                nn.Linear(dense_in, dim),
                nn.ReLU(),
                nn.Dropout(config.dropout),
            ))
            dense_in = dim

        # Output heads
        final_dim = config.dense_dims[-1]

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
        batch_size = x.size(0)

        # Input projection
        x = self.input_proj(x)  # [batch, seq_len, d_model]

        # Add CLS token
        if self.config.use_cls_token:
            cls_tokens = self.cls_token.expand(batch_size, -1, -1)  # [batch, 1, d_model]
            x = torch.cat([cls_tokens, x], dim=1)  # [batch, seq_len+1, d_model]

        # Positional encoding
        x = self.pos_encoding(x)

        # Transformer
        x = self.transformer(x)

        # Extract representation
        if self.config.use_cls_token:
            x = x[:, 0, :]  # CLS token: [batch, d_model]
        else:
            x = x.mean(dim=1)  # Mean pooling: [batch, d_model]

        # Dense layers
        for dense in self.dense_layers:
            x = dense(x)

        # Output heads
        outputs = {
            'fill': self.fill_head(x),
            'imbalance': self.imbalance_head(x).squeeze(-1),
            'pnl': self.pnl_head(x).squeeze(-1),
            'grid_level': self.grid_head(x),
        }

        return outputs

    def get_attention_weights(self, x: torch.Tensor) -> torch.Tensor:
        """Get attention weights from the first transformer layer."""
        batch_size = x.size(0)

        # Project and add CLS
        x = self.input_proj(x)
        if self.config.use_cls_token:
            cls_tokens = self.cls_token.expand(batch_size, -1, -1)
            x = torch.cat([cls_tokens, x], dim=1)
        x = self.pos_encoding(x)

        # Get attention from first layer
        # Note: This requires accessing internal state
        layer = self.transformer.layers[0]
        attn_output, attn_weights = layer.self_attn(
            x, x, x, need_weights=True, average_attn_weights=False
        )
        return attn_weights


def count_parameters(model: nn.Module) -> int:
    """Count trainable parameters."""
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


if __name__ == "__main__":
    # Test Transformer model
    print("Testing Transformer Model...")

    config = TransformerConfig(
        input_dim=80,
        seq_length=100,
    )

    model = TransformerModel(config)
    print(f"Model parameters: {count_parameters(model):,}")

    # Test forward pass
    batch_size = 32
    x = torch.randn(batch_size, config.seq_length, config.input_dim)

    outputs = model(x)
    print("\nOutput shapes:")
    for key, tensor in outputs.items():
        print(f"  {key}: {tensor.shape}")
