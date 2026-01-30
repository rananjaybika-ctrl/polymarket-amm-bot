# Gabagool Strategy Decoded

**Date**: January 30, 2026
**Data**: 63,293 trades across 199 markets (OOS6 + OOS7)
**Model**: TCN with Multi-Task Learning (247K params)

---

## Executive Summary

Gabagool runs a **passive two-sided accumulation strategy** that:
1. Buys both UP and DOWN tokens to lock in spreads
2. Holds all positions to market resolution (no selling)
3. Uses a 70% accurate imbalance predictor to slightly favor the winning side
4. Achieves 71.2% win rate through systematic execution

---

## Key Findings

### 1. Trade Analysis (63,293 trades)

| Metric | Value |
|--------|-------|
| Trade Direction | 100% BUY (0 sells) |
| UP Buys | 31,446 (49.7%) |
| DOWN Buys | 31,847 (50.3%) |
| Avg UP Price | $0.495 |
| Avg DOWN Price | $0.479 |
| **Implied Pair Cost** | **$0.974** |
| Median Trade Size | 17.6 shares |
| Total Volume | 950,020 shares |

### 2. Model Performance (3/4 targets met)

| Task | Result | Target | Status |
|------|--------|--------|--------|
| Fill AUC-ROC | 0.71 | > 0.75 | Close |
| Imbalance Direction | 70.09% | > 65% | ✓ |
| PnL MAE | $0.003/share | < $0.05 | ✓ |
| Grid Top-1 Accuracy | 95.5% | > 50% | ✓ |

### 3. Strategy Components

#### A. Pair Cost Arbitrage
```
Pair Cost = UP_price + DOWN_price
Target: Pair Cost < $1.00

At resolution:
- Winner pays $1.00
- Loser pays $0.00
- Guaranteed profit = $1.00 - Pair_Cost

With avg pair cost $0.974:
- Base profit = $0.026/pair (2.6%)
```

#### B. Imbalance Edge (The Secret Sauce)
- Model predicts winning side with **70% accuracy**
- Matches gabagool's **71.2% win rate** almost exactly
- Key features: velocity_bps, orderbook imbalance, price momentum
- Accumulate slightly more of predicted winner

#### C. Grid-Based Execution
- Passive orders at multiple price levels: 0.01, 0.02, 0.03, 0.04, 0.05 offset
- Grid level selection: **95% accuracy** (volatility-based)
- Consistent trade sizes (15-17 shares)
- High frequency: avg 318 trades/market

---

## Predictive Features (Top 10 by Importance)

1. **total_bid_depth** / **total_ask_depth** - Orderbook liquidity
2. **binance_price** - Reference price for fair value
3. **time_remaining_secs** - Market phase/urgency
4. **price_change_30s** - Short-term momentum
5. **velocity_bps** - Price velocity in basis points
6. **pair_cost** - Current hedging opportunity
7. **up_spread** / **down_spread** - Market tightness
8. **spike_magnitude** - Volatility events
9. **momentum_5s** - Very short-term direction
10. **expensive_side** - Which side is overpriced

---

## Strategy Replication Guide

### Core Logic
```python
# 1. Calculate pair cost
pair_cost = up_ask + down_ask

# 2. Only trade if profitable
if pair_cost < 1.00:
    # 3. Predict winning side (70% accuracy)
    predicted_winner = model.predict_imbalance(features)

    # 4. Accumulate both sides with slight bias
    if predicted_winner == 'UP':
        buy_up(size * 1.05)   # Slight overweight
        buy_down(size * 0.95)
    else:
        buy_up(size * 0.95)
        buy_down(size * 1.05)

    # 5. Hold to resolution - no selling
```

### Grid Placement
```python
# Volatility-based grid offset
if volatility < 0.1:
    offset = 0.01  # Tight grid
elif volatility < 0.3:
    offset = 0.02
else:
    offset = 0.03  # Wide grid in volatile conditions
```

---

## Model Artifacts

- `checkpoints/best_model.pt` - Trained TCN model
- `checkpoints/norm_stats.json` - Feature normalization
- `checkpoints/training_history.json` - Training metrics
- `research/findings/data/gabagool_trades_oos7.json` - Trade data

---

## Limitations & Future Work

1. **Fill Prediction (0.71 AUC)** - Below 0.75 target
   - Gabagool's fill timing depends on his position/capital
   - Try focal loss, tighter horizons

2. **Feature Gaps**
   - No gabagool position data
   - No capital/exposure constraints
   - Cross-market correlations not captured

3. **Data Limitations**
   - Only BTC 15-min markets analyzed
   - OOS6 validation lacks some orderbook data

---

## Conclusion

The gabagool strategy is well understood:
- **Mechanical**: Passive two-sided accumulation with pair cost < $1.00
- **Edge**: 70% accurate imbalance prediction (matches 71.2% win rate)
- **Execution**: Systematic grid orders, consistent sizing, hold to resolution

The model successfully reverse-engineered the core strategy. Ready for live implementation.
