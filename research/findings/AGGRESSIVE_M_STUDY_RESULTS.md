# AGGRESSIVE_M Statistical Study Results

## VERDICT: MAKER ENTRY NOT VIABLE (for FOLLOWING spikes)

**Date:** February 5, 2026 (Updated: February 6, 2026)
**Dataset:** IS+OOS2 (Jan 16-19, LOW volatility period)

---

## UPDATE (Feb 6, 2026): AGGRESSIVE_M (V2) Validated with Deduplication

This study (AGGRESSIVE_M V1) found MAKER entry has severe adverse selection when **FOLLOWING** spikes. However, AGGRESSIVE_M (V2) discovered that **FADING** spikes avoids this problem:

| Approach | Action | Accuracy | Adverse Selection |
|----------|--------|----------|-------------------|
| AGGRESSIVE_M V1 | Follow spike as maker | 50-53% | **4-7pp drop** |
| **AGGRESSIVE_M V2** | Fade spike when expensive_side >= $0.70 | **90.1%** | None observed |

### Final Configuration (Feb 6, 2026)

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| min_expensive_ask | **$0.70** | Best $/trade ($0.10 vs $0.04 at $0.65) |
| obi_filter | **OBI_FOLLOW** | 90.1% accuracy, 424 signals |
| cooldown_seconds | **10s** | 36% more signals than 30s, similar accuracy |

### OBI Filter Comparison (10s cooldown, >= $0.70)

| Strategy | Signals | Accuracy | $/trade | Total $ |
|----------|---------|----------|---------|---------|
| NO_OBI | 653 | 88.4% | $0.03 | $18 |
| **OBI_FOLLOW** | **424** | **90.1%** | **$0.10** | **$42** |
| OBI_FADE (inverted) | 161 | 88.2% | $0.14 | $22 |

**See:** `research/strategies/AGGRESSIVE_M_V2.md` for full strategy spec.

---

## Executive Summary (Original Study)

The statistical study reveals **severe adverse selection** when using maker entry. When a maker order fills (price pulls back to our limit), accuracy drops significantly below the baseline spike accuracy.

| Entry Method | Direction Accuracy | Fee | Net Edge |
|--------------|-------------------|-----|----------|
| **TAKER** (current) | 57.9% | 2% | ~5% |
| MAKER at $0.01 | 53.4% | 0% | ~3% |
| MAKER at $0.02 | 53.5% | 0% | ~3% |
| MAKER at $0.03 | **50.9%** | 0% | **~1%** (coin flip) |
| MAKER at $0.05 | 51.1% | 0% | ~1% |

**Conclusion:** The 2% fee savings from maker entry is MORE than offset by the 4-7pp accuracy loss.

---

## Adverse Selection Analysis

### What is Adverse Selection?

When you place a limit order to buy below current price:
- If price NEVER reaches your limit → your signal was right, but you don't trade
- If price REACHES your limit → your signal may be wrong (price moving against prediction)

This creates selection bias: the trades that DO execute have lower accuracy than trades that DON'T.

### Measured Adverse Selection

| Offset | Fill Rate | Accuracy if Filled | Adverse Selection |
|--------|-----------|-------------------|-------------------|
| $0.01 | 63.6% | 53.4% | **+4.5pp** |
| $0.02 | 53.1% | 53.5% | **+4.3pp** |
| $0.03 | 44.8% | 50.9% | **+7.0pp** |
| $0.05 | 33.6% | 51.1% | **+6.8pp** |
| $0.08 | 25.0% | 52.4% | **+5.5pp** |
| $0.10 | 19.3% | 53.5% | **+4.4pp** |

**Key Insight:** The sweet spot for adverse selection is NOT at any offset - all offsets show significant accuracy degradation.

---

## Token Price Pullback Statistics

After a spike is detected, how much does the winner token price pull back?

| Metric | Value |
|--------|-------|
| Mean pullback | $0.054 |
| Median pullback | $0.020 |
| P75 pullback | $0.080 |
| P90 pullback | $0.150 |

~53% of spikes have a pullback of at least $0.02 within 60 seconds.

---

## Expected Value Comparison

Assuming symmetric payouts (win $0.40, lose $0.40):

**TAKER Entry:**
```
EV = (0.579 × $0.40) - (0.421 × $0.40) - (0.02 × $0.40)
   = $0.232 - $0.168 - $0.008
   = $0.055 per trade
```

**MAKER Entry at $0.03 offset:**
```
EV = (0.509 × $0.40) - (0.491 × $0.40) - $0
   = $0.204 - $0.196 - $0
   = $0.007 per trade
```

**TAKER is ~8x more profitable** despite 2% fee.

---

## Why Does This Happen?

1. **Spike signals are noisy** - 57.9% accuracy means 42% are wrong
2. **Wrong signals often reverse quickly** - price pulls back because prediction failed
3. **Maker orders only fill on pullbacks** - by definition, fills on bad predictions
4. **No fill = price continued in predicted direction** - good predictions don't fill

The maker strategy has an inherent structural problem: it filters OUT the best signals (those that continue immediately) and filters IN the worst signals (those that reverse).

---

## Recommendations

### 1. ABANDON Maker Entry for AGGRESSIVE Strategy

The adverse selection cost (4-7pp accuracy drop) far exceeds the fee savings (2%).

### 2. Continue with TAKER Execution

Current AGGRESSIVE strategy with taker execution remains the best approach:
- 57.9% spike accuracy
- 2% fee is acceptable cost for execution certainty
- No adverse selection

### 3. Alternative Approaches to Reduce Costs

Instead of maker entry, consider:
- **Smaller position sizes** - reduce absolute fee
- **Better signal filtering** - improve accuracy to offset fees
- **Exit optimization** - focus on maker exit (0% exit fee already captured)

---

## Data Files

| File | Contents |
|------|----------|
| `research/findings/data/token_pullback_study_is_oos2.csv` | Per-spike pullback analysis |

---

## Methodology

1. Loaded IS+OOS2 observer data (1,090,500 rows, 254 markets)
2. Identified 4,026 spike events from pre-computed `spike_detected` column
3. For each spike, analyzed winner token ask price for 60 seconds
4. Measured pullback (price drop below entry) and continuation (price rise above entry)
5. Simulated maker fills at various offset levels
6. Compared accuracy of filled trades vs overall spike accuracy

---

*Generated: February 5, 2026*
*Based on: AGGRESSIVE_M_PLAN.md statistical study*
