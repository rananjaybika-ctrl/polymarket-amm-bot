# Whale vs OBI/Velocity Analysis - OOS6 Period

**Generated**: Jan 29, 2026
**Period**: Jan 28 00:00 UTC - Jan 29 10:40 UTC (35 hours)
**Data**: 528K observer rows, 123 BTC 15m markets

## Data Summary

| Whale | Trades | Markets | Time Range |
|-------|--------|---------|------------|
| Gabagool | 6,936 | 122 | Jan 28 00:00 - Jan 29 10:42 |
| Baguette | 3,766 | 122 | Jan 28 00:05 - Jan 29 10:43 |

---

## 1. Winner Prediction Accuracy

| Strategy | Accuracy |
|----------|----------|
| Random baseline | 50.0% |
| Expensive side only | 56.9% |
| **Gabagool** | **67.5%** |
| **Baguette** | **82.5%** |

**Baguette predicts winners with 82.5% accuracy** - significantly better than any simple signal.

---

## 2. OBI Signal Usage

| Whale | Trade WITH OBI | Trade AGAINST OBI |
|-------|----------------|-------------------|
| Gabagool | 48.7% | 51.3% |
| **Baguette** | **39.0%** | **61.0%** |

**Baguette is contrarian to OBI** - when order book imbalance favors UP, they tend to buy DOWN.

Neural network correlations:
- Gabagool: net_obi corr = -0.276 (slight contrarian)
- Baguette: net_obi corr = **-0.638** (strong contrarian)

---

## 3. Price-Based Decisions

| Whale | Buy CHEAPER side | Buy EXPENSIVE side |
|-------|------------------|-------------------|
| Gabagool | 46.3% | **53.7%** |
| Baguette | 30.8% | **69.2%** |

**Both buy the expensive (leading) side** - momentum/trend following on price.

By price difference threshold (Baguette):
- |diff| > 0.02: 70.0% expensive
- |diff| > 0.05: 70.7% expensive
- |diff| > 0.10: 72.9% expensive

---

## 4. Velocity Signal Analysis

**Trade frequency by velocity zone:**

| Zone | % of Time | Gabagool % Trades | Ratio | Baguette % Trades | Ratio |
|------|-----------|-------------------|-------|-------------------|-------|
| neutral | 61.2% | 39.0% | **0.64x** | 42.9% | 0.70x |
| moderate | 29.2% | 43.3% | 1.48x | 41.1% | 1.41x |
| strong | 6.8% | 12.1% | **1.77x** | 11.5% | **1.68x** |
| very_strong | 2.8% | 5.6% | **2.01x** | 4.6% | **1.63x** |

**Both AVOID neutral and SEEK high volatility.**

---

## 5. Volatility Gating Analysis

### NO VOLATILITY GATING FOUND - They SEEK Volatility

**High volatility (90th percentile) trading:**

| Whale | Trades in High Vol | Expected (10%) | Ratio |
|-------|-------------------|----------------|-------|
| Gabagool | 1,242 | 673 | **1.85x** |
| Baguette | 334 | 200 | **1.67x** |

Both trade 65-85% MORE in high volatility periods.

### Accuracy by Volatility Level (Baguette)

| Volatility | Correct | Total | Accuracy |
|------------|---------|-------|----------|
| Low | 7 | 7 | **100%** |
| High | 92 | 113 | 81.4% |

**Paradox**: Baguette is MORE accurate in low vol but trades MORE in high vol.

---

## 6. Position Size Analysis

### No Size Gating by Volatility

| Zone | Gabagool Avg Size | Baguette Avg Size |
|------|-------------------|-------------------|
| neutral | 21.2 | 23.1 |
| moderate | 21.3 | 23.0 |
| strong | 21.5 | 23.5 |
| very_strong | 22.1 | 25.8 |

Ratio (very_strong/neutral): Gabagool 1.05x, Baguette 1.11x - **No significant size gating**

### INVERSE SIZING: Baguette Bets SMALLER on Winners

| Outcome | Baguette Avg Position Size |
|---------|---------------------------|
| CORRECT (82.5%) | 415 shares |
| WRONG (17.5%) | **746 shares** |
| Ratio | **0.56x** |

**Baguette bets 44% SMALLER on winning trades.**

This is NOT from doubling down:
- Early trades avg: 23.7 shares
- Late trades avg: 23.5 shares
- Ratio: 0.99x (no increase)

---

## 7. What Separates Correct vs Wrong Predictions (Baguette)

| Metric | When CORRECT | When WRONG | Difference |
|--------|--------------|------------|------------|
| Position size | 415 shares | 746 shares | -44% |
| 60s price momentum | **+$0.032** | +$0.002 | +$0.030 |
| OBI | +0.28 | +0.19 | +0.09 |
| Time remaining | 745s | 766s | -21s |

**Key finding**: Baguette is more accurate when catching **rising momentum** (>3 cents/min).

---

## 8. Trading Pattern Analysis

### Gabagool
- **100% buys, 0 sells** (accumulates only)
- 108/122 markets show MERGE pattern (interleaved UP/DOWN buys)
- Avg pair cost: $1.07 (would LOSE $4,962 if merged)
- Balanced positions (0.5-2x ratio in 110/122 markets)

### Baguette
- Both buys AND sells (115/122 markets)
- More directional positions (18 heavy UP bias, 24 heavy DOWN bias)
- Avg pair cost: $1.18 (would LOSE $5,204 if merged)
- Sells at slightly higher prices (+$0.007 to +$0.046 spread capture)

---

## 9. Neural Network Analysis

### Predicting Whale Decisions

| Whale | NN Accuracy | Interpretation |
|-------|-------------|----------------|
| Gabagool | 53.6% | Nearly random - no learnable pattern |
| Baguette | 62.8% | Some learnable pattern |

### Predicting When Baguette is Correct

- NN achieved **82.4% accuracy** (matches their actual performance)
- Only weak feature correlation: `obi_change` (+0.10)
- Suggests complex feature interactions or unobservable signals

### Key Feature Correlations (Baguette)

| Feature | Correlation with buying UP | Interpretation |
|---------|---------------------------|----------------|
| velocity_bps | +0.24 | Slight momentum following |
| up_imbalance | **-0.64** | Strong contrarian |
| down_imbalance | **+0.64** | Strong contrarian |
| net_obi | **-0.64** | FADE OBI |
| price_diff | **+0.78** | Buy expensive side |

---

## 10. Reverse-Engineered Strategy

### Baguette's Apparent Strategy

1. **Entry Signal**:
   - Wait for price momentum > 2-3 cents/minute
   - Bet on the EXPENSIVE side (currently winning)
   - FADE OBI (contrarian to order flow)
   - SEEK volatility (trade more in high vol zones)

2. **Position Management**:
   - Buy both sides (hedged pairs)
   - Net bias toward predicted winner
   - **Smaller positions when confident** (inverse sizing)

3. **Exit**:
   - Sell positions before resolution
   - Capture spread on sells

### Gabagool's Apparent Strategy

1. **Entry**: Accumulate pairs via MERGE pattern
2. **No sells**: Hold to resolution
3. **Volatility seeking**: 2x more trades in very_strong velocity
4. **Balanced**: Nearly equal UP/DOWN in most markets

---

## 11. Simple Strategy Backtest Results

| Strategy | Accuracy |
|----------|----------|
| Pure momentum (60s) | 60.0% |
| Expensive + Momentum | 61.6% |
| Expensive + Momentum > 0.02 | 66.2% |
| **Baguette actual** | **82.5%** |

Gap of ~16% suggests additional edge we can't observe.

---

## 12. Implications for AS/Grid Strategy

1. **FADE OBI** - Don't follow order book imbalance, trade against it
2. **Follow price momentum** - Enter when one side has rising momentum (>2-3 cents/min)
3. **Buy expensive side** - The currently winning side wins 57% baseline, whales achieve 67-82%
4. **SEEK volatility** - Trade more in high velocity zones (contrary to typical gating)
5. **Consider inverse sizing** - Baguette bets smaller on high-conviction trades
6. **Exit before resolution** - Instead of merge, consider selling

---

## 13. Actual PnL Performance

### Session PnL (Jan 28-29 UTC)

| Metric | Gabagool | Baguette |
|--------|----------|----------|
| Total Trades | 6,936 | 3,766 |
| Buys | 6,936 | 2,385 |
| Sells | 0 | 1,381 |
| Total Buy Cost | $77,165 | $33,922 |
| Sell Revenue | $0 | $17,380 |
| **PnL (resolved)** | **$651** | **$1,007** |
| **ROI** | **1.14%** | **4.52%** |
| Avg PnL/market | $6.85 | $10.60 |

**Baguette achieves 4x better ROI** despite trading ~3x less volume.

### Key Differences:
- Gabagool: Pure accumulator, holds to resolution, lower ROI
- Baguette: Active exits (37% sells), captures spread, 82.5% accuracy → higher ROI

### Maker/Taker Status:
- Data-api trades endpoint does not include `isMaker` field
- Inference from fill patterns: Baguette 92.9% single-fill (likely maker), Gabagool 77.2% single-fill (mixed)

---

## 14. Open Questions

1. Why does Baguette bet SMALLER on winners? (Discipline? Risk management?)
2. What's the additional 16% edge beyond simple momentum + price?
3. Why seek volatility but have better accuracy in low vol?
4. How to access proper maker/taker data? (CLOB API requires auth)

---

## Appendix: Data Quality

- Whale trades fetched using conditionId method (historical access)
- 97% of Gabagool trades matched to observer data
- 84% of Baguette trades matched to observer data
- Resolution data extracted from final observer prices
