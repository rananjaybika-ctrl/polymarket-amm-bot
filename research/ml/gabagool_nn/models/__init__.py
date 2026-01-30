"""
Model architectures for Gabagool NN.

Available models:
- TCN: Temporal Convolutional Network (primary)
- TransformerModel: Attention-based model (alternative)
- MLP: Simple feedforward baseline
- RandomForestBaseline: Interpretable baseline
"""

from .tcn import TCNModel
from .transformer import TransformerModel
from .baselines import MLPBaseline, create_random_forest_baseline

__all__ = [
    'TCNModel',
    'TransformerModel',
    'MLPBaseline',
    'create_random_forest_baseline',
]
