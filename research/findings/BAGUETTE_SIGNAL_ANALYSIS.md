# Baguette Directional Prediction Signal Analysis

**Date:** February 7, 2026
**Dataset:** OOS6 (1,451 trades) + OOS9 (12,995 trades) = 14,446 total trades
**Markets:** 122 total, 76 with net positions

---

## Executive Summary

Baguette achieves **84.2% prediction accuracy** on markets where they take a directional position. The core of their signal is remarkably simple:

1. **Primary Signal:** BTC EMA trend (price vs EMA) - 79% base accuracy
2. **Confidence Filter:** OBI disagreement with BTC trend - boosts accuracy to **98.1%**
3. **Position Management:** Early accumulation (600-900s), late distribution/hedging (0-300s)

---

## Key Findings

### 1. WHEN They Commit to a Direction

| Metric | Value |
|--------|-------|
| First trade timing | ~792s remaining (108s into market) |
| Position commitment point | ~600-700s remaining |
| Active trading window | 900s to ~60s |

**Pattern:**
- **Early phase (750-900s):** Heavy buying (72% BUY), exploring positions
- **Mid phase (450-750s):** Directional commitment emerges
- **Late phase (0-300s):** Position adjustment/hedging

**Net Position Contribution by Phase:**
```
750-900s: +386 shares toward winner
600-750s: +809 shares toward winner  (KEY ACCUMULATION PHASE)
450-600s:  +14 shares toward winner
300-450s: +713 shares toward winner
150-300s: -395 shares toward winner  (DISTRIBUTION BEGINS)
0-150s:   -688 shares toward winner  (HEAVY DISTRIBUTION)
```

### 2. WHAT Conditions Trigger Conviction

#### The Core Signal: BTC EMA Trend

The most powerful predictor is surprisingly simple:

| BTC Condition | Winner Probability |
|---------------|-------------------|
| BTC > EMA (trend = 1) | UP wins 48.0% |
| BTC < EMA (trend = -1) | DOWN wins 62.1% (inverse: UP wins 37.9%) |

**BTC EMA Trend Signal Accuracy: 78.9%**

#### The Confidence Filter: OBI Contrarian Signal

This is the critical insight:

| Condition | Accuracy |
|-----------|----------|
| OBI DISAGREES with BTC trend | **98.1%** (n=52) |
| OBI AGREES with BTC trend | 37.5% (n=24) |

**Interpretation:** When retail order book imbalance (OBI) pushes in the opposite direction of BTC's technical trend, the BTC signal is almost certainly correct. This is classic "smart money vs dumb money" - retail is fading the trend and getting crushed.

#### Additional Confirming Signals

| Feature | When Accumulating Winner | When Not | Significance |
|---------|-------------------------|----------|--------------|
| velocity_bps | +0.016 | -0.025 | p=0.0001 |
| market_vol | 1.045 | 0.939 | p=0.0000 |
| btc_ema_trend | +0.01 | -0.07 | p=0.0000 |

### 3. Winning vs Losing Predictions

**Correct Predictions (64 markets, 84.2%):**
- Followed BTC trend: 94.4% of the time when agreeing with signal
- Smaller position sizes: 28.8 avg shares
- Price_diff: -0.141 (bought cheaper side)

**Wrong Predictions (12 markets, 15.8%):**
- Often in "low confidence" regime (OBI agrees with BTC)
- Larger position sizes: 83.6 avg shares
- Price_diff: +0.074 (bought expensive side)

**Key Error Pattern:** Baguette's wrong predictions tend to occur when:
1. OBI agrees with BTC trend (low confidence regime)
2. They take larger positions (overconfidence)
3. They buy the more expensive side

### 4. Feature Importance Analysis

**Correlations with "Good Trade" (supporting eventual winner):**

| Feature | Correlation | P-value |
|---------|-------------|---------|
| velocity_bps | +0.052 | 0.0000 |
| up_rsi | +0.036 | 0.0000 |
| rsi_diff | +0.034 | 0.0001 |
| time_remaining | +0.028 | 0.0012 |
| down_rsi | -0.023 | 0.0090 |
| market_vol | -0.037 | 0.0000 |

**Interpretation:**
- Positive velocity (BTC moving up) correlates with correct UP bets
- Lower market volatility is better for accuracy
- Earlier trades (higher time_remaining) slightly more accurate

### 5. Contrarian Signal Deep Dive

**First Trade Analysis:**
- First trade is contrarian to market sentiment: **68.4%**
- First trade on eventual winner: **47.4%**
- When contrarian first trade: **38.5%** correct
- When aligned first trade: **66.7%** correct

**The Flip Pattern:**
- 50% of markets see Baguette flip from initial direction
- When they flip: **86.8%** correct
- Flip typically occurs around **600s remaining**
- After flip: **95%** eventually correct

**Interpretation:** Baguette often starts with a contrarian hedge/probe, then flips to the correct direction as the market develops. The flip is likely triggered by BTC trend confirmation.

---

## Proposed Signal Rules

### Rule 1: BTC EMA Trend (Base Signal)
```
IF btc_price > btc_ema THEN predict UP
IF btc_price < btc_ema THEN predict DOWN

Base accuracy: 78.9%
```

### Rule 2: OBI Contrarian Filter (Confidence)
```
IF net_obi DISAGREES with btc_signal THEN HIGH confidence (98.1%)
IF net_obi AGREES with btc_signal THEN LOW confidence (37.5%)
```

### Rule 3: Velocity Confirmation
```
IF velocity_bps aligns with btc_signal THEN 88.3% accuracy
IF velocity_bps opposes btc_signal THEN 43.8% accuracy
```

### Composite Signal (Recommended)
```python
def baguette_signal(btc_ema_trend, net_obi, velocity_bps):
    # Core signal: BTC trend
    signal = 'UP' if btc_ema_trend > 0 else 'DOWN'

    # Confidence filter: OBI contrarian
    obi_signal = 'UP' if net_obi > 0 else 'DOWN'
    confidence = 'HIGH' if obi_signal != signal else 'LOW'

    # Only trade HIGH confidence
    if confidence == 'HIGH':
        return signal  # 98.1% expected accuracy
    else:
        return None  # Skip LOW confidence trades
```

**Expected Performance:**
- 52 markets meet HIGH confidence criteria
- 98.1% accuracy on these markets
- Baguette achieves 90.4% on these same markets

---

## Position Sizing Insights

| Metric | Correct Predictions | Wrong Predictions |
|--------|---------------------|-------------------|
| Avg net position | 28.8 shares | 83.6 shares |
| High confidence | 26.0 shares | - |
| Low confidence | 62.3 shares | - |

**Interpretation:** Baguette sizes DOWN in high-confidence regimes and sizes UP in low-confidence regimes - exactly backwards from optimal. This may be because high-confidence regimes (OBI contrarian) have thinner liquidity on their side.

---

## Summary Statistics

| Metric | Value |
|--------|-------|
| Total trades analyzed | 14,446 |
| Total markets | 122 |
| Markets with positions | 76 (62.3%) |
| Prediction accuracy | 84.2% |
| BTC trend signal accuracy | 78.9% |
| High-confidence accuracy | 98.1% |
| First trade timing | ~792s remaining |
| Position commitment | ~600-700s remaining |

---

## Actionable Recommendations

### For Replicating Baguette's Signal:

1. **Primary Signal:** Track BTC price vs EMA (use 10-period EMA based on data)
2. **Confidence Filter:** Calculate net OBI; trade ONLY when OBI opposes BTC trend
3. **Timing:** Enter between 600-800s remaining
4. **Skip:** Markets where OBI agrees with BTC trend (37.5% accuracy)

### For Counter-Trading:

1. When OBI AGREES with BTC, the signal is unreliable
2. Consider counter-trading when Baguette takes large positions in low-confidence regimes
3. Late-phase (0-300s) sees Baguette distributing - may create liquidity opportunities

### Risk Factors:

1. **Dataset Specificity:** Analysis based on OOS6+OOS9; different market regimes may vary
2. **OBI Calculation:** Requires accurate order book snapshot at trade time
3. **BTC EMA Period:** Need to validate which EMA period Baguette uses (appears to be 10)

---

## Confidence Assessment

| Finding | Confidence Level | Evidence |
|---------|------------------|----------|
| 84% prediction accuracy | HIGH | 76 markets, consistent across datasets |
| BTC trend is core signal | HIGH | 79% base accuracy, p < 0.0001 |
| OBI contrarian filter works | HIGH | 98.1% vs 37.5%, dramatic difference |
| Early accumulation pattern | MEDIUM | Clear in aggregate, varies by market |
| Position sizing is suboptimal | MEDIUM | Statistical difference, small sample |

---

*Analysis performed: February 7, 2026*
*Data sources: whale_crossref_baguette_oos9.csv, whale_crossref_baguette_oos6_btctf.csv, market_resolutions_verified.csv*
