"""
Temporal Convolutional Network (TCN) for Gabagool Strategy Learning

Why TCN over LSTM/Transformer?
- Efficient for 5Hz data (1800 samples/market)
- Dilated convolutions capture short + long-range dependencies
- Parallelizable, faster than RNNs
- More stable gradients

Architecture:
    INPUT: [batch, seq_len=100, features=80]  # 20 seconds of data

    TCN Block 1: Conv1D(64, dilation=1,2) + BatchNorm + ReLU + Dropout(0.2)
    TCN Block 2: Conv1D(128, dilation=4,8) + BatchNorm + ReLU + Dropout(0.2)
    Multi-Head Self-Attention: 4 heads, dim=128
    Dense: 256 -> 128 (with dropout)

    OUTPUT HEADS:
    ├── Fill:      Dense(2, sigmoid)     -> [p_up_fill, p_down_fill]
    ├── Imbalance: Dense(1, tanh)        -> direction [-1, 1]
    ├── PnL:       Dense(1, linear)      -> expected profit
    └── Grid:      Dense(5, softmax)     -> offset probabilities
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Dict, Optional, Tuple
from dataclasses import dataclass


@dataclass
class TCNConfig:
    """Configuration for TCN model."""
    input_dim: int = 80  # Number of input features
    seq_length: int = 100  # Sequence length (20s at 5Hz)

    # TCN layers
    tcn_channels: Tuple[int, ...] = (64, 128)
    kernel_size: int = 3
    dilations: Tuple[Tuple[int, ...], ...] = ((1, 2), (4, 8))

    # Attention
    use_attention: bool = True
    n_heads: int = 4
    attention_dim: int = 128

    # Dense layers
    dense_dims: Tuple[int, ...] = (256, 128)

    # Regularization
    dropout: float = 0.2

    # Output heads
    n_grid_levels: int = 5


class CausalConv1d(nn.Module):
    """Causal 1D convolution with proper padding."""

    def __init__(self, in_channels: int, out_channels: int,
                 kernel_size: int, dilation: int = 1):
        super().__init__()
        self.padding = (kernel_size - 1) * dilation
        self.conv = nn.Conv1d(
            in_channels, out_channels, kernel_size,
            padding=self.padding, dilation=dilation
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [batch, channels, seq_len]
        out = self.conv(x)
        # Remove future timesteps (causal)
        if self.padding > 0:
            out = out[:, :, :-self.padding]
        return out


class TCNBlock(nn.Module):
    """Single TCN block with dilated convolutions."""

    def __init__(self, in_channels: int, out_channels: int,
                 kernel_size: int, dilations: Tuple[int, ...],
                 dropout: float = 0.2):
        super().__init__()

        self.convs = nn.ModuleList()
        self.norms = nn.ModuleList()
        self.dropouts = nn.ModuleList()

        channels = in_channels
        for i, dilation in enumerate(dilations):
            self.convs.append(
                CausalConv1d(channels, out_channels, kernel_size, dilation)
            )
            self.norms.append(nn.BatchNorm1d(out_channels))
            self.dropouts.append(nn.Dropout(dropout))
            channels = out_channels

        # Residual connection
        self.residual = nn.Conv1d(in_channels, out_channels, 1) if in_channels != out_channels else nn.Identity()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [batch, channels, seq_len]
        residual = self.residual(x)

        for conv, norm, dropout in zip(self.convs, self.norms, self.dropouts):
            x = conv(x)
            x = norm(x)
            x = F.relu(x)
            x = dropout(x)

        return F.relu(x + residual)


class MultiHeadAttention(nn.Module):
    """Multi-head self-attention layer."""

    def __init__(self, dim: int, n_heads: int, dropout: float = 0.1):
        super().__init__()
        self.attention = nn.MultiheadAttention(dim, n_heads, dropout=dropout, batch_first=True)
        self.norm = nn.LayerNorm(dim)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [batch, seq_len, dim]
        attn_out, _ = self.attention(x, x, x)
        x = self.norm(x + self.dropout(attn_out))
        return x


class TCNModel(nn.Module):
    """
    TCN model with multi-task output heads.

    Input: [batch, seq_len, features]
    Output: Dict with 'fill', 'imbalance', 'pnl', 'grid_level'
    """

    def __init__(self, config: Optional[TCNConfig] = None):
        super().__init__()

        if config is None:
            config = TCNConfig()
        self.config = config

        # Input projection
        self.input_proj = nn.Linear(config.input_dim, config.tcn_channels[0])

        # TCN blocks
        self.tcn_blocks = nn.ModuleList()
        in_channels = config.tcn_channels[0]
        for i, (out_channels, dilations) in enumerate(zip(config.tcn_channels, config.dilations)):
            self.tcn_blocks.append(
                TCNBlock(in_channels, out_channels, config.kernel_size, dilations, config.dropout)
            )
            in_channels = out_channels

        # Optional attention layer
        self.use_attention = config.use_attention
        if self.use_attention:
            self.attention = MultiHeadAttention(
                config.tcn_channels[-1], config.n_heads, config.dropout
            )

        # Dense layers
        tcn_out_dim = config.tcn_channels[-1]
        dense_in = tcn_out_dim

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

        # Fill prediction: [p_up_fill, p_down_fill]
        self.fill_head = nn.Sequential(
            nn.Linear(final_dim, 2),
            nn.Sigmoid(),
        )

        # Imbalance direction: [-1, 1]
        self.imbalance_head = nn.Sequential(
            nn.Linear(final_dim, 1),
            nn.Tanh(),
        )

        # PnL prediction: expected profit
        self.pnl_head = nn.Linear(final_dim, 1)

        # Grid level: softmax over offsets
        self.grid_head = nn.Sequential(
            nn.Linear(final_dim, config.n_grid_levels),
        )

    def forward(self, x: torch.Tensor) -> Dict[str, torch.Tensor]:
        """
        Forward pass.

        Args:
            x: [batch, seq_len, features]

        Returns:
            Dict with:
                - fill: [batch, 2]
                - imbalance: [batch, 1]
                - pnl: [batch, 1]
                - grid_level: [batch, n_grid_levels]
        """
        # Input projection
        x = self.input_proj(x)  # [batch, seq_len, tcn_dim]

        # TCN expects [batch, channels, seq_len]
        x = x.transpose(1, 2)

        # TCN blocks
        for block in self.tcn_blocks:
            x = block(x)

        # Back to [batch, seq_len, channels]
        x = x.transpose(1, 2)

        # Attention
        if self.use_attention:
            x = self.attention(x)

        # Global pooling: use last timestep (causal) or mean
        x = x[:, -1, :]  # [batch, channels]

        # Dense layers
        for dense in self.dense_layers:
            x = dense(x)

        # Output heads
        outputs = {
            'fill': self.fill_head(x),  # [batch, 2]
            'imbalance': self.imbalance_head(x).squeeze(-1),  # [batch]
            'pnl': self.pnl_head(x).squeeze(-1),  # [batch]
            'grid_level': self.grid_head(x),  # [batch, 5]
        }

        return outputs

    def get_attention_weights(self, x: torch.Tensor) -> torch.Tensor:
        """Get attention weights for visualization."""
        if not self.use_attention:
            return None

        # Forward through TCN
        x = self.input_proj(x)
        x = x.transpose(1, 2)
        for block in self.tcn_blocks:
            x = block(x)
        x = x.transpose(1, 2)

        # Get attention weights
        _, attn_weights = self.attention.attention(x, x, x)
        return attn_weights


def count_parameters(model: nn.Module) -> int:
    """Count trainable parameters."""
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


if __name__ == "__main__":
    # Test TCN model
    print("Testing TCN Model...")

    config = TCNConfig(
        input_dim=80,
        seq_length=100,
    )

    model = TCNModel(config)
    print(f"Model parameters: {count_parameters(model):,}")

    # Test forward pass
    batch_size = 32
    x = torch.randn(batch_size, config.seq_length, config.input_dim)

    outputs = model(x)
    print("\nOutput shapes:")
    for key, tensor in outputs.items():
        print(f"  {key}: {tensor.shape}")

    # Test attention weights
    attn = model.get_attention_weights(x)
    if attn is not None:
        print(f"\nAttention weights: {attn.shape}")
