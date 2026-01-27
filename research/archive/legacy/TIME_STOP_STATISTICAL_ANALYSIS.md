# Time-Stop vs Price-Stop Statistical Analysis

## Executive Summary

This analysis investigates the relationship between time-based stops (120s, 180s) and price-based stops (12%, 15%) in trading configurations. The goal is to identify **when to use time stops vs price stops** for optimal PnL.

### Key Finding: The Contradiction Explained

The observed "contradiction" where:
- AGGRESSIVE configs (low win rate): 180s time-stop gives **+13.9%** better PnL than price-stop
- CONSERVATIVE configs (high win rate): Price-stop gives **+18.3%** better PnL than time-stop

**This is not a contradiction - it's a predictable pattern based on win rate and entry timing quality.**

---

## 1. Overall Stop Type Performance

| Stop Type | Mean PnL | Mean Win Rate | Mean Hourly Rate |
|-----------|----------|---------------|------------------|
| Price Stop (12%/15%) | $24.55 | 62.41% | $0.5783 |
| 120s Time Stop | $20.66 (-15.8%) | 58.55% (-3.9 ppts) | $0.4951 (-14.4%) |
| 180s Time Stop | $23.80 (-3.1%) | 65.77% (+3.4 ppts) | $0.5675 (-1.9%) |

**Takeaway**: On average, price stops outperform, but 180s time stops are very close. The 120s time stop is consistently worse.

---

## 2. Critical Statistical Correlations

The strongest predictors of whether time stops help a configuration:

| Variable | Correlation with 180s PnL Change | p-value | Significance |
|----------|----------------------------------|---------|--------------|
| **Base Win Rate** | r = -0.8392 | p < 0.0001 | *** |
| **Cycling (ON/OFF)** | r = +0.5408 | p = 0.0001 | *** |
| Premature Stop % | r = -0.3388 | p = 0.0161 | * |
| Base PnL | r = -0.0372 | p = 0.7978 | ns |
| Lookback Period | r = -0.0027 | p = 0.9853 | ns |

### Interpretation:
1. **Win Rate is the STRONGEST predictor** (r = -0.84): Lower win rate configs benefit MORE from time stops
2. **Cycling enabled strongly correlates** with time stop effectiveness
3. Base PnL and lookback period have NO significant effect

---

## 3. Cycling Impact Analysis

| Cycling | Price Stop PnL | 180s Time PnL | Change | Time Wins |
|---------|----------------|---------------|--------|-----------|
| **TRUE** | $26.72 | $27.03 | +1.2% | 19/39 (49%) |
| **FALSE** | $16.86 | $12.34 | -26.8% | 2/11 (18%) |

**Finding**: When cycling is DISABLED, price stops are clearly superior. Time stops only help when cycling is ENABLED.

---

## 4. Z-Score Method Impact

When Cycling = TRUE:

| Z-Score Method | Price Stop | 180s Time | Change | Time Wins |
|----------------|------------|-----------|--------|-----------|
| **EWMA** | $27.41 | $30.51 | **+11.3%** | 11/15 (73%) |
| **EWMA_RATIO** | $25.16 | $25.99 | +3.3% | 7/11 (64%) |
| **OU** | $26.78 | $22.56 | **-15.8%** | 0/11 (0%) |
| **PERCENTILE** | $29.75 | $31.28 | +5.1% | 1/2 (50%) |

**Finding**:
- EWMA and EWMA_RATIO methods benefit from time stops (particularly EWMA)
- OU method should NEVER use time stops - price stops are dramatically better (0% time wins)

---

## 5. Win Rate Segmentation Analysis

| Win Rate Segment | Price Stop PnL | 180s Time PnL | Change | Time Wins |
|------------------|----------------|---------------|--------|-----------|
| **Low (<60.7%)** | $23.22 | $26.45 | **+13.9%** | 19/25 (76%) |
| **High (>=60.7%)** | $25.88 | $21.15 | **-18.3%** | 2/25 (8%) |

**Finding**: This is the most decisive pattern:
- Low win rate (aggressive) configs: Time stops win 76% of the time
- High win rate (conservative) configs: Price stops win 92% of the time

---

## 6. Premature Stop Analysis

| Time Stop | Mean Premature % | Mean Premature PnL Lost |
|-----------|------------------|-------------------------|
| 120s | 45.90% | -$8.38 |
| 180s | 35.17% | -$6.07 |

The 180s time stop has ~10 percentage points fewer premature exits and loses ~$2.31 less per premature exit compared to 120s.

---

## 7. Statistical Significance Tests

### Paired T-Tests (comparing same configs with different stop types):

| Comparison | t-statistic | p-value | Significant? |
|------------|-------------|---------|--------------|
| 180s vs Price | -1.05 | 0.3007 | NO |
| 120s vs Price | -6.26 | <0.0001 | YES*** |

**Interpretation**:
- 180s time stops are NOT significantly different from price stops overall
- 120s time stops are SIGNIFICANTLY WORSE than price stops
- This confirms that stop type selection should be based on CONFIG CHARACTERISTICS, not applied universally

---

## 8. Multi-Factor Decision Framework

### Decision Tree for Stop Type Selection:

```
START
  |
  +-- Is Cycling ENABLED?
       |
       +-- NO --> USE PRICE STOP (time wins only 18%)
       |
       +-- YES --> What is the Z-Score Method?
            |
            +-- OU --> USE PRICE STOP (time wins 0%)
            |
            +-- EWMA/EWMA_RATIO --> Check Win Rate
            |    |
            |    +-- Win Rate < 61% --> USE 180s TIME STOP (time wins 86%)
            |    |
            |    +-- Win Rate >= 61% --> USE PRICE STOP (time wins 0%)
            |
            +-- PERCENTILE --> PRICE STOP preferred (borderline)
```

---

## 9. Top Performing Configurations by Stop Type

### Best Configs for 180s Time Stop (highest improvement vs price):

| Rank | Config | Price PnL | 180s PnL | Improvement |
|------|--------|-----------|----------|-------------|
| 7 | OU+EWMA, Cycling=True, 0<z<1.5 | $19.51 | $28.95 | +48.4% |
| 6 | OU+EWMA, Cycling=True, 0<z<2.0 | $21.43 | $31.74 | +48.1% |
| 27 | OU+EWMA_RATIO, Cycling=True, 0<z<2.0 | $20.61 | $28.61 | +38.8% |
| 17 | OU+EWMA_RATIO, Cycling=True, 0<z<1.5 | $18.74 | $25.93 | +38.4% |
| 1 | OU+EWMA, Cycling=True, 0<z<1.5 | $21.68 | $28.95 | +33.5% |

### Best Configs for Price Stop (time stop hurts most):

| Rank | Config | Price PnL | 180s PnL | Time Penalty |
|------|--------|-----------|----------|--------------|
| 38 | EWMA+EWMA, Cycling=False, 0<z<2.0 | $17.73 | $9.76 | -44.9% |
| 35 | EWMA+EWMA, Cycling=False, 0<z<1.5 | $16.68 | $10.00 | -40.0% |
| 50 | EWMA+EWMA, Cycling=False, 0<z<1.5 | $15.94 | $10.00 | -37.2% |
| 24 | EWMA+PERCENTILE, Cycling=False, 0<z<1.5 | $16.31 | $10.72 | -34.3% |
| 43 | OU+OU, Cycling=True, -0.5<z<1.5 | $23.47 | $15.81 | -32.7% |

---

## 10. The Root Cause: Why Time Stops Help Aggressive Configs

### Theoretical Explanation:

**Low Win Rate (Aggressive) Configs:**
- Trade entries are more speculative with higher initial adverse movement
- Price stops trigger prematurely on what would become winning trades
- Time stops allow trades to "breathe" and recover
- The 180s window is sufficient for mean reversion to work

**High Win Rate (Conservative) Configs:**
- Trade entries already have good timing (high directional accuracy)
- When a trade moves against, it's more likely to be a genuine loser
- Price stops correctly exit losing trades early
- Time stops keep bad trades open longer, increasing losses

---

## 11. Z-Zone Analysis

| Z-Zone | Price PnL | 180s PnL | Change | Time Wins |
|--------|-----------|----------|--------|-----------|
| 0<z<2.0 | $21.15 | $22.59 | +6.8% | 4/8 (50%) |
| z>0 | $21.12 | $26.25 | +24.3% | 1/1 |
| -0.5<z<1.5 | $26.14 | $25.75 | -1.5% | 5/11 (45%) |
| 0<z<1.5 | $18.54 | $18.04 | -2.7% | 10/20 (50%) |
| -1<z<2.0 | $35.97 | $35.01 | -2.7% | 1/2 (50%) |
| z<1.0 | $38.34 | $33.34 | -13.1% | 0/5 (0%) |
| z<1.5 | $37.42 | $31.97 | -14.6% | 0/2 (0%) |

**Finding**: Wider z-zones (0<z<2.0, z>0) tend to benefit more from time stops. Narrow upper-bounded zones (z<1.0, z<1.5) strongly favor price stops.

---

## 12. Lookback Period Analysis

| Lookback | Price PnL | 180s PnL | Change | Time Wins |
|----------|-----------|----------|--------|-----------|
| 1200ms | $26.04 | $27.36 | +5.1% | 13/23 (57%) |
| 1000ms | $22.70 | $20.90 | -7.9% | 7/19 (37%) |
| 1400ms | $24.65 | $20.44 | -17.1% | 1/8 (12%) |

**Finding**: 1200ms lookback shows the best compatibility with time stops. Shorter (1000ms) and longer (1400ms) lookbacks favor price stops.

---

## 13. Production Recommendations

### When to Use 180s TIME STOP:

1. **Cycling = TRUE** AND
2. **Z-Score Method = EWMA or EWMA_RATIO** AND
3. **Base Win Rate < 61%** (aggressive/speculative configs)

Expected improvement: **+13.9% to +48.4%** over price stops

### When to Use PRICE STOP (12% or 15%):

1. **Cycling = FALSE** (any other parameters) OR
2. **Z-Score Method = OU** (any other parameters) OR
3. **Base Win Rate >= 61%** (conservative configs)

Expected improvement: **+18% to +45%** over time stops

### Never Use:
- **120s Time Stop** - consistently underperforms both 180s and price stops

---

## 14. Summary Statistics

| Metric | Value |
|--------|-------|
| Total configs analyzed | 50 |
| Configs where 180s beats price | 21 (42%) |
| Configs where price beats 180s | 29 (58%) |
| Strongest predictor | Win Rate (r = -0.84) |
| Second strongest predictor | Cycling (r = +0.54) |
| Best combo for time stop | Cycling=True + EWMA + Low WR (86% win rate) |
| Best combo for price stop | Cycling=False OR OU method (82-100% win rate) |

---

## 15. Action Items for Production

1. **Implement conditional stop logic** based on the decision tree above
2. **Monitor win rate** of each config to dynamically select stop type
3. **Default to price stops** when uncertain (they're the safer choice overall)
4. **Avoid 120s time stops entirely** - they hurt performance in all scenarios
5. **Consider 180s time stops ONLY for**:
   - Cycling-enabled strategies
   - EWMA/EWMA_RATIO z-score methods
   - Configs with sub-60% win rates

---

## Appendix: Data Sources

- `/research/time_stop_top50_results.csv` - 150 rows (50 configs x 3 stop types)
- `/research/stop_out_analysis_results.csv` - 10 top configs with detailed stop-out breakdown
- `/research/vol_filter_grid_results_all_combined.csv` - 1440 rows (full grid search results)

Analysis performed: January 2026

---

## OOS3 VALIDATION UPDATE (January 23, 2026)

### Time-Stop vs Price-Stop on Fresh Data (After Cycling Bug Fix)

| Config | Stop Type | OOS3 $/hr @50sh | OOS3 WR% | IS $/hr @50sh | IS WR% |
|--------|-----------|-----------------|-----------|---------------|--------|
| **AGGRESSIVE** | **180s TIME** | **$17.59** | **70.2%** | **$7.76** | **68.9%** |
| BALANCED (EWMA) | 15% PRICE | $26.38 | 57.9% | $3.06 | 49.0% |
| BALANCED (OU) | 15% PRICE | $2.34 | 36.7% | $6.07 | 69.6% |
| CONSERVATIVE | 15% PRICE | $2.49 | 53.3% | - | - |

**Key insight:** AGGRESSIVE (time-stop) is the only config that performs consistently
across both in-sample and OOS3. BALANCED+EWMA dominates OOS3 but is mediocre on IS.

### OOS3 Stop Analysis Detail (Corrected)

**AGGRESSIVE (180s time-stop):**
- 24 time-stops out of 84 trades (28.6%)
- 8 premature stops (33.3%)
- PnL lost to premature stops: -$6.95 @5sh
- Consistent 70.2% WR across in-sample (68.9%) and OOS3

**BALANCED+EWMA (15% price-stop):**
- 66 price-stops out of 202 trades (32.7%) — corrected from 79/219 pre-fix
- On in-sample: 69 stops out of 147 trades (46.9%) — nearly half stopped out
- 49% WR on in-sample suggests price-stop eats edge in normal regimes
- OOS3 had 2x spike rate per hour, which may explain improved OOS3 performance

### Key OOS3 Finding for Stop Type

The in-sample rule "OU z-score → PRICE STOP, EWMA z-score → TIME STOP" is **partially overturned**:

| Z-Score + Stop | In-Sample Rule | OOS3 Result |
|----------------|----------------|-------------|
| EWMA + TIME | Recommended | $14.11/hr (good) |
| EWMA + PRICE | Not recommended | **$26.76/hr (BEST)** |
| OU + PRICE | Recommended | $1.44-3.15/hr (poor - OU drifted) |

**Updated rule:** With EWMA z-scores, BOTH stop types work, but **15% price-stop is actually better** on OOS3 (more trades survive to passive fill). The original time-stop advantage was partly an artifact of OU z-score's poor signal quality requiring more "breathing room."

### Recommendation
- EWMA z-score + 15% price-stop: Best OOS3 combination
- EWMA z-score + 180s time-stop: Still viable, higher WR but fewer trades
- OU z-score + any stop: Avoid in production (parameter drift risk)
