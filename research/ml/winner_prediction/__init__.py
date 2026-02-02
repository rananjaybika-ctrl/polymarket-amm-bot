"""
Winner Prediction Module for Gabagool Reverse Engineering

This module implements two approaches:
1. Approach A: Reverse engineer Gabagool's decision function
   - Learn f(orderbook_state) -> Gabagool_preferred_side

2. Approach B: Direct winner prediction
   - Learn f(orderbook_state, time_t) -> P(UP wins)

Goal: Achieve 70% prediction accuracy (matching Gabagool's performance)

Usage:
    # Train all models on both approaches
    python -m research.ml.winner_prediction.train_winner_model --approach both --model all

    # Quick validation
    python -m research.ml.winner_prediction.train_winner_model --validate

    # Evaluate results
    python -m research.ml.winner_prediction.evaluate_winner --quick
"""

__version__ = "0.1.0"

# Core data loading and cross-referencing
from .cross_reference import (
    cross_reference_trades_to_observer,
    validate_cross_reference,
    load_gabagool_trades,
    load_observer_data,
)

# Gabagool bias computation
from .gabagool_bias import (
    compute_gabagool_bias,
    compute_all_market_biases,
    validate_bias_against_resolutions,
    get_gabagool_bias_labels,
)

# Resolution loading
from .resolution_loader import (
    load_all_resolutions,
    ResolutionData,
    get_resolution_statistics,
)

# Feature engineering
from .winner_features import (
    compute_winner_features,
    WinnerFeatureConfig,
    get_winner_feature_names,
)
