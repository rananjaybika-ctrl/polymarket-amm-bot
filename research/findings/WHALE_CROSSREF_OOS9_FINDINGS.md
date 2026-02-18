# Whale Cross-Reference Analysis: OOS9 Findings

## ⛔ DEPRECATED - February 5, 2026

**Status:** ABANDONED - Backtest showed -$2.93/hr despite 70% accuracy.
Cannot profitably replicate whale strategies.

---

**Date:** February 5, 2026
**Dataset:** OOS9 (101 markets, Jan 29-30, 2026)
**Trades Analyzed:** Gabagool 74,209 | Baguette 7,285

---

## Executive Summary

Cross-referencing whale trades with market state reveals **clear, actionable entry signals**:

| Signal | Gabagool | Baguette | Our Target |
|--------|----------|----------|------------|
| Contrarian rate | 47.3% | **56.8%** | 55%+ |
| Buy expensive side | 46.0% | **63.1%** | 60%+ |
| Avg volatility | 1.69 | **2.19** | >2.0 |
| Entry timing | 300-900s | 300-900s | 300-600s |

**Key Insight:** Baguette's edge comes from **buying the expensive side (momentum) while fading OBI (contrarian)**. This combination identifies when price momentum diverges from orderbook imbalance.

---

## Entry Signal Analysis

### 1. Expensive Side = Prediction (STRONGEST SIGNAL)

| Whale | Buy UP when UP expensive | Buy DOWN when DOWN expensive | Combined |
|-------|--------------------------|------------------------------|----------|
| Gabagool | 38.3% | 53.7% | **46.0%** |
| Baguette | **54.6%** | **71.7%** | **63.1%** |

**Finding:** Baguette buys the expensive side 63% of the time. When DOWN is expensive, Baguette buys DOWN 71.7% of the time.

**Implementation:**
```python
def get_prediction(up_ask, down_ask):
    if down_ask > up_ask:
        return "DOWN"  # DOWN expensive = buy DOWN
    return "UP"        # UP expensive = buy UP
```

### 2. OBI Contrarian (CONFIRMATION SIGNAL)

| Whale | Buy UP when OBI < 0 | Buy DOWN when OBI > 0 | Combined Contrarian |
|-------|---------------------|----------------------|---------------------|
| Gabagool | 43.9% | 50.8% | **47.3%** |
| Baguette | **52.7%** | **60.2%** | **56.8%** |

**Finding:** Baguette fades OBI 57% of the time. When OBI is bullish (>0), Baguette buys DOWN 60% of the time.

**Implementation:**
```python
def obi_confirms(predicted_side, net_obi):
    if predicted_side == "DOWN":
        return net_obi > 0  # Bullish OBI → buy DOWN (contrarian)
    return net_obi < 0      # Bearish OBI → buy UP (contrarian)
```

### 3. Volatility Gate (QUALITY FILTER)

| Whale | Avg Market Vol |
|-------|----------------|
| Gabagool | 1.69 |
| Baguette | **2.19** (+30%) |

**Finding:** Baguette trades in 30% higher volatility conditions. This is consistent with seeking momentum setups.

**Implementation:**
```python
def volatility_gate(market_vol, threshold=2.0):
    return market_vol >= threshold
```

### 4. Entry Price (QUALITY INDICATOR)

| Whale | Avg UP Entry | Avg DOWN Entry | Avg Combined |
|-------|--------------|----------------|--------------|
| Gabagool | $0.437 | $0.517 | **$0.477** |
| Baguette | $0.538 | $0.623 | **$0.580** |

**Finding:** Baguette pays 20% more per share on average. Buying expensive means higher entry cost but better direction.

**Implication:** Don't optimize for cheap entries. Optimize for correct direction.

---

## Timing Analysis

### Entry Time Distribution

| Time Remaining | Gabagool | Baguette |
|----------------|----------|----------|
| 0-60s | 0.8% | **5.0%** |
| 60-120s | 3.7% | 6.8% |
| 120-180s | 6.4% | 7.3% |
| 180-300s | 15.7% | 13.5% |
| **300-600s** | **37.9%** | **33.7%** |
| **600-900s** | **35.6%** | **33.8%** |

**Finding:** Both whales trade primarily in 300-900s range (73.5% Gabagool, 67.5% Baguette). But Baguette enters more in final minute (5% vs 0.8%).

**Timing sweet spot:** 300-600s remaining (high signal, time for price discovery)

---

## BTC Indicators (NOT USEFUL)

| Indicator | Gabagool | Baguette |
|-----------|----------|----------|
| BTC RSI-14 | 48.7 | 49.0 |
| BTC EMA trend | -0.04 | -0.03 |
| BTC momentum-20 | 0.01 | 0.02 |
| BTC vs EMA-10 | -0.00% | -0.00% |

**Finding:** BTC indicators show NO meaningful difference between whales. Confirms our earlier finding: BTC velocity r=0.055 is not actionable.

---

## Size Analysis

### Size by Entry Price

| Entry Price | Gabagool Size | Baguette Size |
|-------------|---------------|---------------|
| <0.30 | 13.11 | 5.04 |
| 0.30-0.40 | 12.41 | 5.57 |
| 0.40-0.50 | 12.77 | 7.65 |
| 0.50-0.60 | 13.83 | 6.97 |
| 0.60-0.70 | 14.36 | 7.39 |
| >0.70 | **15.85** | 7.18 |

**Finding:** Gabagool sizes UP on expensive entries (15.85 at >$0.70). Baguette is more uniform.

### Size Correlations (WEAK)

| Variable | Gabagool r | Baguette r |
|----------|------------|------------|
| time_remaining | -0.051 | +0.027 |
| net_obi | -0.002 | +0.027 |
| price_diff | +0.004 | +0.031 |
| market_vol | +0.033 | -0.015 |

**Finding:** No strong sizing signal from market conditions. Size appears discretionary or inventory-based, not signal-based.

### Inverse Sizing Analysis (NEW - Feb 5, 2026)

**Price vs Size Correlation: +0.10** (weak positive, NOT inverse)

| Price Range | Baguette Avg Size | Median |
|-------------|-------------------|--------|
| $0.00-0.40 | 5.8 | **5** |
| $0.40-0.50 | 7.5 | **5** |
| $0.50-0.60 | 7.3 | **5** |
| $0.60-0.70 | 7.3 | **5** |
| $0.70-0.80 | 7.8 | **5** |
| $0.80-0.90 | 7.7 | **5** |
| $0.90-1.00 | 7.9 | **5** |

**Conclusion:** Baguette uses **fixed 5 shares per order** across ALL price levels.
Inverse sizing hypothesis REJECTED - share count is more consistent than dollar amount.

**Implication:** Use fixed 5 shares per order, matching Baguette's pattern.

---

## Whale Strategy Summary

### Baguette's Apparent Strategy (82.5% accuracy)

1. **Buy expensive side** (63% of time) → momentum following
2. **Fade OBI** (57% contrarian) → divergence signal
3. **Seek volatility** (2.19 vs 1.69) → better momentum
4. **Enter 300-900s** (67%) but also late entries (5% in final minute)
5. **Pay higher prices** ($0.58 vs $0.48) → quality over cheap

### Gabagool's Strategy (67-70% accuracy)

1. **Mixed expensive/cheap** (46% expensive) → more opportunistic
2. **Slight contrarian** (47%) → almost neutral on OBI
3. **Lower volatility** (1.69) → more conditions acceptable
4. **Enter 300-900s** (74%) → similar timing
5. **Sizes up on expensive** (15.85 at >$0.70) → conviction scaling

---

## Actionable Strategy: MOMENTUM-CONTRARIAN

Based on Baguette's patterns, the entry signal is:

```
ENTRY SIGNAL = (expensive_side) AND (OBI_contrarian) AND (volatility > threshold)
```

### Entry Rules

1. **Prediction:** Buy the expensive side (higher ask price)
2. **Confirmation:** OBI must be contrarian (opposite direction)
3. **Quality:** Market volatility > 2.0
4. **Timing:** 300-600s remaining preferred

### Expected Accuracy

| Signal Combination | Accuracy (estimated) |
|--------------------|----------------------|
| Random | 50% |
| Expensive side only | 57% |
| + OBI contrarian | 62-65% |
| + Volatility gate | 65-70% |
| + Timing (300-600s) | **68-72%** |

### Key Differences from Previous MAKER-PREDICTION

| Aspect | Old MAKER-PREDICTION | New MOMENTUM-CONTRARIAN |
|--------|---------------------|------------------------|
| Entry signal | Expensive + momentum | **Expensive + OBI contrarian** |
| Volatility | Any | **>2.0 gate** |
| Timing focus | Avoid last 2 min | **Prefer 300-600s window** |
| Entry price | Seek cheap | **Accept expensive** |

---

## Next Steps

1. ✅ **Backtest MOMENTUM-CONTRARIAN** (IN PROGRESS - Feb 5, 2026)
   - Expensive side prediction
   - OBI contrarian filter
   - Volatility > 2.0 gate
   - 30-600s timing window (no last 30s)
   - Fixed 5 shares per order (matching Baguette)
   - Partial exit: 64% at +$0.08 profit, hold 36% to resolution

2. **Compare accuracy** vs baseline 57%

3. ✅ **Partial exit logic** implemented (Baguette's 64/36 split)

4. ✅ **Test inverse sizing** - REJECTED (Baguette uses fixed sizing)

---

## Data Files

| File | Contents |
|------|----------|
| `research/findings/data/gabagool_crossref_oos9.csv` | 74,209 Gabagool trades with market state |
| `research/findings/data/baguette_crossref_oos9.csv` | 7,285 Baguette trades with market state |
| `research/findings/data/whale_combined_crossref_oos9.csv` | Combined 81,494 trades |

---

*Created: February 5, 2026*
*Updated: February 5, 2026 - Added inverse sizing analysis, updated next steps*
*Analysis: whale_crossref_oos9.py, whale_crossref_with_btc_tf.py*
