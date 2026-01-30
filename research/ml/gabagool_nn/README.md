# Gabagool Neural Network

Train neural networks to learn gabagool's passive two-sided grid market making behavior from observer data.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Train TCN model (recommended)
python train_gabagool_nn.py --model tcn --epochs 100

# Train all models for comparison
python train_gabagool_nn.py --model all --epochs 50

# Quick test (5 markets, 10 epochs)
python train_gabagool_nn.py --model tcn --epochs 10 --max-markets 5
```

## Data Splits

| Dataset | Period | Purpose |
|---------|--------|---------|
| **Training** | IS+OOS2 (Jan 16-19) + OOS5 (Jan 26) | Model training |
| **Validation** | OOS3+OOS4 (Jan 20-24) + OOS6 (Jan 28-29) | Hyperparameter tuning & final eval |

## Architecture

### TCN (Temporal Convolutional Network) - Recommended

```
INPUT: [batch, seq_len=100, features=80]  # 20 seconds at 5Hz

TCN Block 1: Conv1D(64, dilation=1,2) + BatchNorm + ReLU + Dropout(0.2)
TCN Block 2: Conv1D(128, dilation=4,8) + BatchNorm + ReLU + Dropout(0.2)
Multi-Head Self-Attention: 4 heads, dim=128
Dense: 256 -> 128 (with dropout)

OUTPUT HEADS:
├── Fill:      Dense(2, sigmoid)     -> [p_up_fill, p_down_fill]
├── Imbalance: Dense(1, tanh)        -> direction [-1, 1]
├── PnL:       Dense(1, linear)      -> expected profit
└── Grid:      Dense(5, softmax)     -> offset probabilities [0.01-0.05]
```

### Why TCN?
- Efficient for 5Hz data (1800 samples/market)
- Dilated convolutions capture short + long-range dependencies
- Parallelizable, faster than RNNs
- More stable gradients

## Prediction Targets (Multi-Task Learning)

| Task | Type | Target | Description |
|------|------|--------|-------------|
| Fill Prediction | Binary Classification | `[p_up_fill, p_down_fill]` | Will UP/DOWN side get filled in next 30s? |
| Imbalance Direction | Regression | `[-1, 1]` | Which side accumulates more? |
| Profitability | Regression | `expected_pnl` | Expected profit given current conditions |
| Grid Level | Ordinal Classification | `offset [0.01-0.05]` | Optimal passive offset from best bid |

## Evaluation Targets

| Task | Primary Metric | Target |
|------|----------------|--------|
| Fill Prediction | AUC-ROC | > 0.75 |
| Imbalance | Direction Accuracy | > 65% |
| PnL | MAE | < $0.05/share |
| Grid Level | Top-1 Accuracy | > 50% |

### Strategy-Level (Backtest)
- Win rate: Target 71.2% (match gabagool)
- Pair cost: $1.00-$1.03
- Fill distribution across 95+ price levels

## Features (~80 total)

### Raw Features
- **Price State**: binance_price, up/down bid/ask, pair_cost, orderbook depth (5 levels)
- **Velocity**: velocity_bps, velocity_zone, acceleration_bps2, jerk_bps3, momentum_5s
- **Order Book Imbalance**: up_imbalance, down_imbalance, depth ratios
- **Temporal**: time_remaining_secs, market_phase
- **Spike Detection**: spike_detected, spike_direction, spike_magnitude

### Engineered Features
- Rolling aggregations (1s, 5s, 30s): velocity_mean, velocity_std, price_change
- Interactions: velocity × OBI, spike × velocity
- Gabagool-specific: distance_from_grid_center, pair_cost_opportunity

## Training Configuration

```python
SEQUENCE_LENGTH = 100  # 20 seconds at 5Hz
STRIDE = 25            # 5 seconds stride (80% overlap)

# Multi-Task Loss
loss = (1.0 * BCE(fill) +           # Fill prediction
        0.5 * MSE(imbalance) +       # Imbalance direction
        0.3 * Huber(pnl) +           # Profitability
        0.2 * CCE(grid_level))       # Grid selection

# Hyperparameters
batch_size = 256
epochs = 100 (early_stopping patience=10)
optimizer = AdamW(lr=1e-3, weight_decay=1e-4)
scheduler = CosineAnnealingLR
gradient_clip = 1.0
```

## Files

```
gabagool_nn/
├── __init__.py           # Package init
├── data_loader.py        # Load IS+OOS2+OOS5 training data
├── feature_engineer.py   # Engineer all features
├── label_constructor.py  # Construct fill/imbalance/pnl labels
├── sequence_builder.py   # Create overlapping sequences
├── trainer.py            # Training loop with multi-task loss
├── evaluator.py          # Metrics and backtest simulation
├── train_gabagool_nn.py  # Main training script
├── models/
│   ├── __init__.py
│   ├── tcn.py            # TCN architecture (primary)
│   ├── transformer.py    # Transformer alternative
│   └── baselines.py      # MLP, Random Forest
├── requirements.txt
└── README.md
```

## Verification Plan

1. **Train baselines first** - Establish benchmarks with RF/MLP
2. **Train TCN model** - Compare AUC-ROC, direction accuracy
3. **Run backtest simulation** - Use model to simulate passive grid trading
4. **Compare with gabagool metrics**:
   - Win rate: 71.2%
   - Avg trades/market: 67-81
   - Pair cost: $1.006-$1.021
5. **Interpretability analysis** - Feature importance (RF), attention visualization (TCN)
