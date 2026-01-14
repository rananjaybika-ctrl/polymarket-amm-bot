# AWS 7-Hour Observer Run - Deep Analysis Report

**Date:** January 14, 2026
**Run Period:** January 13, 2026 19:03 IST → January 14, 2026 03:12 IST
**Duration:** 8.15 hours
**Server:** AWS eu-west-1 (54.170.244.221)

---

## Executive Summary

| Metric | Value |
|--------|-------|
| Total Markets | 33 |
| Total Samples | 57,900 |
| Sample Rate | ~2/sec |
| Velocity Prediction Accuracy | 94.4% (when clear signal) |
| **Corrected Total PnL** | **-$1,661.49** |
| Hourly Rate | -$203.86/hour |

**Key Finding:** The velocity signal correctly predicts market direction 94% of the time, but the execution logic destroys the edge by buying both sides at mid-market prices due to velocity flip behavior.

---

## 1. Market Resolutions

### Summary
- **UP wins:** 13 markets (39.4%)
- **DOWN wins:** 9 markets (27.3%)
- **Unclear:** 11 markets (33.3%)

### Detailed Resolution Table

| Market | Winner | UP Final | DOWN Final |
|--------|--------|----------|------------|
| 1768311000 | UP | $0.98 | $0.01 |
| 1768311900 | UNCLEAR | $0.60 | $0.39 |
| 1768312800 | UNCLEAR | $0.22 | $0.77 |
| 1768313700 | UP | $0.99 | $0.00 |
| 1768314600 | UP | $0.96 | $0.03 |
| 1768315500 | DOWN | $0.00 | $0.99 |
| 1768316400 | UP | $0.98 | $0.01 |
| 1768317300 | UNCLEAR | $0.13 | $0.86 |
| 1768318200 | UP | $0.98 | $0.01 |
| 1768319100 | UP | $0.99 | $0.00 |
| 1768320000 | UNCLEAR | $0.17 | $0.82 |
| 1768320900 | DOWN | $0.02 | $0.97 |
| 1768321800 | UP | $0.98 | $0.01 |
| 1768322700 | DOWN | $0.03 | $0.96 |
| 1768323600 | DOWN | $0.02 | $0.97 |
| 1768324500 | UP | $0.96 | $0.03 |
| 1768325400 | UNCLEAR | $0.20 | $0.79 |
| 1768326300 | DOWN | $0.01 | $0.98 |
| 1768327200 | UP | $0.96 | $0.03 |
| 1768328100 | UNCLEAR | $0.53 | $0.46 |
| 1768329000 | UNCLEAR | $0.84 | $0.14 |
| 1768329900 | UNCLEAR | $0.11 | $0.84 |
| 1768330800 | DOWN | $0.05 | $0.94 |
| 1768331700 | UNCLEAR | $0.89 | $0.10 |
| 1768332600 | UP | $0.99 | $0.00 |
| 1768333500 | UP | $1.00 | $0.00 |
| 1768334400 | UP | $0.99 | $0.00 |
| 1768335300 | UNCLEAR | $0.19 | $0.80 |
| 1768336200 | DOWN | $0.04 | $0.95 |
| 1768337100 | UP | $0.98 | $0.01 |
| 1768338000 | UNCLEAR | $0.63 | $0.36 |
| 1768338900 | DOWN | $0.06 | $0.93 |
| 1768339800 | DOWN | $0.01 | $0.98 |

---

## 2. Velocity Parameters

### Observed Values

| Parameter | Value |
|-----------|-------|
| Range | -3.07 to +1.77 bps |
| Mean | +0.006 bps |
| Std Dev | 0.28 bps |

### Zone Distribution

| Zone | Threshold | % of Time |
|------|-----------|-----------|
| Strong | \|v\| >= 0.10 bps | 61.9% |
| Moderate | 0.05-0.10 bps | 14.5% |
| Neutral | < 0.05 bps | 23.7% |

### Velocity Prediction Accuracy

| Metric | Value |
|--------|-------|
| Markets with clear outcome | 22/33 |
| Velocity made prediction | 18/22 |
| Correct predictions | 17/18 |
| **Accuracy** | **94.4%** |

---

## 3. Corrected PnL Analysis

### Methodology

Previous analysis only counted hedged pairs. This corrected analysis includes:
1. **Hedged PnL** = pairs × ($1.00 - pair_cost)
2. **Unhedged UP PnL** = excess_up × (resolution_up - avg_up_price)
3. **Unhedged DOWN PnL** = excess_down × (resolution_down - avg_down_price)

### Per-Market Breakdown

| Market | Winner | Pairs | Ex UP | Ex DN | Hedged | Unhedged | Total |
|--------|--------|-------|-------|-------|--------|----------|-------|
| 1768311000 | UP | 2400 | 605 | 0 | -$70.21 | +$179.66 | +$109.45 |
| 1768311900 | UNCLEAR | 2765 | 0 | 475 | -$159.84 | -$120.21 | -$280.05 |
| 1768312800 | UNCLEAR | 2355 | 0 | 760 | +$23.39 | +$35.56 | +$58.95 |
| 1768313700 | UP | 1820 | 0 | 1170 | +$282.91 | -$91.25 | +$191.66 |
| 1768314600 | UP | 3230 | 0 | 1120 | +$235.90 | -$279.57 | -$43.66 |
| 1768315500 | DOWN | 3740 | 0 | 230 | +$144.02 | +$65.91 | +$209.93 |
| 1768316400 | UP | 3795 | 0 | 15 | -$137.89 | -$10.11 | -$148.00 |
| 1768317300 | UNCLEAR | 3495 | 60 | 0 | +$73.64 | -$2.49 | +$71.15 |
| 1768318200 | UP | 2270 | 0 | 1355 | +$264.60 | -$122.48 | +$142.12 |
| 1768319100 | UP | 3240 | 0 | 425 | +$262.42 | -$151.85 | +$110.57 |
| 1768320000 | UNCLEAR | 3510 | 305 | 0 | -$45.69 | -$161.56 | -$207.25 |
| 1768320900 | DOWN | 3565 | 55 | 0 | -$22.70 | -$23.80 | -$46.50 |
| 1768321800 | UP | 2060 | 0 | 1785 | +$248.23 | -$213.43 | +$34.80 |
| 1768322700 | DOWN | 3030 | 0 | 45 | -$168.37 | +$27.77 | -$140.60 |
| 1768323600 | DOWN | 3190 | 0 | 595 | -$3.45 | +$265.45 | +$262.00 |
| 1768324500 | UP | 3275 | 155 | 0 | -$53.51 | +$80.91 | +$27.40 |
| 1768325400 | UNCLEAR | 3130 | 605 | 0 | +$1.78 | -$50.38 | -$48.60 |
| 1768326300 | DOWN | 3050 | 675 | 0 | -$47.68 | -$302.32 | -$350.00 |
| 1768327200 | UP | 2885 | 0 | 540 | +$111.67 | -$119.95 | -$8.28 |
| 1768328100 | UNCLEAR | 3295 | 450 | 0 | -$108.33 | -$68.47 | -$176.80 |
| 1768329000 | UNCLEAR | 3080 | 595 | 0 | -$266.36 | +$97.26 | -$169.10 |
| 1768329900 | UNCLEAR | 3055 | 0 | 375 | -$199.88 | +$81.28 | -$118.60 |
| 1768330800 | DOWN | 3110 | 0 | 500 | -$18.64 | +$131.04 | +$112.40 |
| 1768331700 | UNCLEAR | 2810 | 85 | 0 | -$100.34 | +$9.74 | -$90.60 |
| 1768332600 | UP | 3025 | 0 | 550 | +$139.37 | -$138.26 | +$1.11 |
| 1768333500 | UP | 2435 | 25 | 0 | +$81.87 | +$8.46 | +$90.32 |
| 1768334400 | UP | 2855 | 0 | 820 | -$13.70 | -$312.52 | -$326.22 |
| 1768335300 | UNCLEAR | 3090 | 0 | 440 | -$305.15 | +$137.70 | -$167.45 |
| 1768336200 | DOWN | 2835 | 745 | 0 | -$39.44 | -$214.49 | -$253.93 |
| 1768337100 | UP | 2990 | 300 | 0 | -$201.89 | +$108.34 | -$93.55 |
| 1768338000 | UNCLEAR | 2905 | 0 | 265 | -$194.60 | -$42.20 | -$236.80 |
| 1768338900 | DOWN | 2250 | 210 | 0 | -$170.55 | -$121.80 | -$292.35 |
| 1768339800 | DOWN | 1720 | 0 | 335 | +$8.15 | +$106.85 | +$115.00 |

### Summary

| Component | PnL |
|-----------|-----|
| Hedged (pairs merged) | -$450.29 |
| Unhedged positions | -$1,211.20 |
| **TOTAL** | **-$1,661.49** |
| Hourly Rate | -$203.86/hour |

---

## 4. Variance Analysis

| Metric | Value |
|--------|-------|
| Mean PnL/market | -$50.35 |
| Std Dev | $163.13 |
| Min (worst) | -$350.00 |
| Max (best) | +$262.00 |
| Sharpe (per market) | -0.31 |

### Win/Loss Breakdown

| Metric | Value |
|--------|-------|
| Winning markets | 14 |
| Losing markets | 19 |
| Win rate | 42.4% |
| Avg winning trade | +$109.78 |
| Avg losing trade | -$168.33 |
| Win/Loss Ratio | 0.65 |

---

## 5. Merging ON vs OFF Comparison

**Parameters:** Start Balance = $170, Target Shares = 15 per trade

### Scenario 1: Merging ON (Cycle Capital)

After each pair completes, merge UP+DOWN=$1 and recycle capital.

| Metric | Value |
|--------|-------|
| Start Balance | $170.00 |
| Final Balance | $169.31 |
| Total Profit | -$0.69 |
| Return | -0.4% |
| Markets Traded | 33 |
| Total Pairs | 495 |

### Scenario 2: Merging OFF (Hold to Expiry)

Buy positions and hold to expiry, no capital recycling.

| Metric | Value |
|--------|-------|
| Start Balance | $170.00 |
| Final Balance | $174.72 |
| Total Profit | +$4.72 |
| Return | +2.8% |
| Markets Traded | 12 |

### Comparison Table

| Metric | Merging ON | Merging OFF |
|--------|------------|-------------|
| Start Balance | $170.00 | $170.00 |
| Final Balance | $169.31 | $174.72 |
| Total Profit | -$0.69 | +$4.72 |
| Return | -0.4% | +2.8% |
| Markets Traded | 33 | 12 |
| Capital Efficiency | High (recycled) | Low (locked) |

### Variance Comparison

| Metric | Merging ON | Merging OFF |
|--------|------------|-------------|
| Mean PnL/market | -$0.02 | +$0.39 |
| Std Dev | $0.90 | $0.97 |
| Min | -$1.48 | -$1.02 |
| Max | +$2.33 | +$2.33 |
| Win Rate | 39.4% | 50.0% |

### Analysis

**Why Merging OFF performed better:**
1. Limited exposure to only 12 markets (ran out of capital)
2. Early markets (lower numbered) had better pair costs
3. Later markets in the session showed worse performance
4. Capital lock prevented compounding losses

**Why Merging ON underperformed:**
1. Exposed to ALL 33 markets
2. Each market with pair_cost > $1 caused cumulative loss
3. More trades = more exposure to the broken fill logic
4. Variance compounded over more trades

---

## 6. Root Cause Analysis

### The Core Bug

The strategy recalculates entry/hedge sides every sample based on velocity:

```python
if entry_side == "UP":
    resting.up_bid = entry_price_up      # tight
    resting.down_bid = entry_price_down  # wide
elif entry_side == "DOWN":               # VELOCITY FLIPPED
    resting.up_bid = entry_price_up      # NOW wide
    resting.down_bid = entry_price_down  # NOW tight
```

**Result:** When velocity flips, BOTH sides get filled as "entry" at mid-market prices.

### Evidence from Data

Market 1768322700 (DOWN won, lost -$140.60):
```
t=924s: velocity=-0.078 → entry_side=DOWN → fills DOWN at $0.54
t=903s: velocity=+0.137 → entry_side=UP   → fills UP at $0.47

Both sides filled as "entry" = $0.54 + $0.47 = $1.01 pair cost LOSING
```

### Why Hedge Doesn't Fill at Expected Price

**Current behavior:** Hedge bid recalculates every sample:
```
T=0:   DOWN bid=$0.49 → hedge=$0.45 (bid - 0.04)
T=5:   DOWN bid=$0.30 → hedge=$0.26 (chasing down)
T=10:  DOWN bid=$0.10 → hedge=$0.06 (still chasing)
T=14:  DOWN bid=$0.01 → hedge=$0.01 (only NOW fills)
```

**Fixed behavior (proposed):** Set hedge ONCE based on entry:
```
Entry UP at $0.52 → hedge_target = $0.97 - $0.52 = $0.45
Keep $0.45 bid until it fills (when ask drops to $0.45)
```

---

## 7. Key Findings

### What Works
1. **Velocity prediction is accurate** (94.4%) when there's a clear signal
2. **Binance→Polymarket lag exists** (1-2s via Chainlink)
3. **WebSocket data is reliable** at 200ms sampling

### What's Broken
1. **Velocity flip behavior** causes both sides to fill as entry
2. **Hedge bid chasing** prevents spread capture
3. **Pair costs average >$1.00** = structural loss

### Risk Assessment

| Risk | Impact | Frequency |
|------|--------|-----------|
| Velocity flips mid-market | High (-$150 avg) | 60% of markets |
| Unhedged position on wrong side | Very High (-$350 max) | 20% of markets |
| Pair cost > $1.00 | Medium (-$50 avg) | 58% of markets |

---

## 8. Recommendations

### Immediate Fix (High Priority)
1. **Lock hedge price on entry** - Don't recalculate every sample
2. **Track entry state** - Only one side is "entry", other is "hedge"
3. **Target pair cost** - Set hedge_target = $0.97 - entry_price

### Strategy Alternatives

| Strategy | Expected Return | Risk | Complexity |
|----------|-----------------|------|------------|
| Current (broken) | -200%/year | High | Low |
| Fixed hedge | +50-100%/year | Medium | Medium |
| Pure directional | +300-400%/year | Very High | Low |
| Grid MM (Gabagool) | +100-200%/year | Low | High |

### Next Steps
1. Implement fixed hedge logic in observer
2. Run 12-hour validation test
3. If profitable, deploy with $50 real capital
4. Scale to $500 if 1-week test passes

---

## 9. Data Files

| File | Description |
|------|-------------|
| `spread_capture_obs_20260113.csv` | Raw observer data (57,900 samples) |
| `/tmp/market_resolutions.csv` | Market resolution analysis |
| `/tmp/corrected_pnl.csv` | Per-market PnL breakdown |
| `/tmp/merging_on_results.csv` | Merging ON scenario results |
| `/tmp/merging_off_results.csv` | Merging OFF scenario results |

---

*Report generated: January 14, 2026*
*Analysis duration: 8.15 hours of data from AWS observer*
