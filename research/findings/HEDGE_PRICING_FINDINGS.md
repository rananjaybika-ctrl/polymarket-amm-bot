# Hedge Pricing Analysis Findings

**Date:** January 18, 2026
**Analyst:** Claude (automated analysis via hedge_pricing_analysis.py)

---

## Executive Summary

The hedge pricing analysis revealed a critical finding: **the current formula severely underpredicts expected drops**.

| Metric | Old Formula | New Formula | Actual |
|--------|-------------|-------------|--------|
| Expected drop (3% spike) | 0.030 | 0.105 | 0.101 |
| R² on 60s drops | -0.73 | N/A | N/A |
| RMSE | 0.110 | 0.083 | - |

**Key Insight:** spike_magnitude has essentially **zero correlation** (r = -0.01) with actual short-term drops. The current formula using 0.68 × spike is based on a spurious relationship.

---

## Analysis Details

### Data Used

| Dataset | Records | Description |
|---------|---------|-------------|
| signal_path1_v2.csv | 352 | 800ms lookback signals |
| signal_path2_v2.csv | 98 | 300-600ms lookback signals |
| grid_obs_*.csv | 471,871 | Observer orderbook data (5Hz) |
| market_resolutions_verified.csv | 270 | Verified UP/DOWN resolutions |

**Total signals analyzed:** 450
**Correct direction signals:** 277 (61.6%)
**Valid samples for regression:** 258

### Drop Measurements

We measured actual loser ask price drops over multiple time windows after signal detection:

| Window | Mean Drop | Median Drop | Std Dev | N |
|--------|-----------|-------------|---------|---|
| 30 seconds | 0.0882 | 0.0800 | 0.0727 | 264 |
| **60 seconds** | **0.1011** | **0.0900** | 0.0834 | 258 |
| 120 seconds | 0.1062 | 0.1000 | 0.0857 | 244 |

The 60-second window is most relevant for passive hedge fills.

---

## Regression Results

### Model 1: Simple Linear (Current Approach)
```
drop ~ spike_magnitude
```

| Metric | Value |
|--------|-------|
| R² | 0.0002 |
| p-value (spike) | 0.841 (not significant) |
| Correlation | -0.0125 |

**Conclusion:** spike_magnitude provides NO predictive power for short-term drops.

### Model 2: Multiple Regression
```
drop ~ spike_magnitude + velocity_bps + time_remaining + regime
```

| Metric | Value |
|--------|-------|
| R² | 0.389 |
| Adjusted R² | 0.379 |
| CV R² | 0.360 ± 0.117 |

**Note:** This high R² is for drops-until-resolution (not 60s window). The improvement comes primarily from `time_remaining` (correlation 0.60), not from spike or velocity.

### Model 3: With Interactions
```
drop ~ spike_magnitude + velocity_bps + spike*velocity + regime
```

| Metric | Value |
|--------|-------|
| R² | 0.398 |
| CV R² | 0.363 ± 0.111 |

**Note:** Severe multicollinearity (VIF > 40 for regime variables). Coefficients unstable.

---

## Formula Comparison

### Old Formula (spike_param_optimizer.py prior)
```python
expected_drop = 0.68 * spike_mag / 100 + 0.01
```

For a typical 3% spike: `expected_drop = 0.68 * 0.03 + 0.01 = 0.030`

**Problem:** Actual 60s mean drop is 0.101 - the formula underpredicts by 70%.

### New Formula (Recommended)
```python
expected_drop = 0.08 + 0.50 * spike_mag / 100 + regime_bonus
```

Where `regime_bonus` = {LOW: 0.0, MEDIUM: 0.01, HIGH: 0.02}

For a typical 3% spike in MEDIUM regime: `expected_drop = 0.08 + 0.015 + 0.01 = 0.105`

**Result:** Matches actual mean of 0.101 within 4%.

---

## Feature Correlations with 60s Drop

| Feature | Correlation | p-value | Significant? |
|---------|-------------|---------|--------------|
| spike_magnitude | -0.0125 | 0.84 | No |
| velocity_bps_abs | -0.0434 | 0.50 | No |
| time_remaining | 0.5999 | <0.001 | Yes (for resolution drops) |
| regime_HIGH | 0.1337 | <0.05 | Yes |

**Key Finding:** Neither spike magnitude nor velocity predict short-term drops. The time_remaining effect only appears when measuring drops until resolution (not relevant for 60s hedge fills).

---

## Why Did the Old Formula Fail?

1. **Different measurement window:** The old R=0.202 correlation was likely measured on drops-to-resolution (not short-term). This captures a fundamentally different phenomenon.

2. **Confounded relationship:** Larger spikes correlate with more volatile periods, which have higher time_remaining variance. When we control for time, spike effect disappears.

3. **Sample bias:** The original regression may have included incorrect predictions where larger spikes led to resolution losses (actual drop = 0), artificially creating a positive correlation.

---

## Implementation Changes

### File: `research/spike_param_optimizer.py`

**Constants updated (lines 49-54):**
```python
# Old values
DROP_MULTIPLIER = 0.68
DROP_INTERCEPT = 0.01

# New values (v2)
DROP_MULTIPLIER = 0.50   # Reduced - spike has weak predictive power
DROP_INTERCEPT = 0.08    # Increased - matches actual mean drop
DROP_REGIME_BONUS = {'LOW': 0.0, 'MEDIUM': 0.01, 'HIGH': 0.02}
```

**Function updated: `calc_loser_bid()`**
```python
def calc_loser_bid(winner_entry: float, spike_mag: float, regime: str = "MEDIUM") -> float:
    """
    Calculate loser side bid price (v2).
    Based on hedge_pricing_analysis.py regression results.
    """
    base_drop = DROP_INTERCEPT  # 0.08
    spike_term = DROP_MULTIPLIER * spike_mag / 100  # 0.50 * spike%
    regime_bonus = DROP_REGIME_BONUS.get(regime, 0.01)

    expected_drop = base_drop + spike_term + regime_bonus
    expected_drop = max(0.02, min(0.20, expected_drop))

    max_loser = TARGET_PAIR_COST - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
    return max(0.01, min(0.95, loser_bid))
```

---

## Expected Impact

| Scenario | Old Loser Bid | New Loser Bid | Change |
|----------|---------------|---------------|--------|
| 3% spike, winner @ 0.55, MEDIUM | 0.42 | 0.34 | -0.08 |
| 4% spike, winner @ 0.50, HIGH | 0.47 | 0.38 | -0.09 |
| 2% spike, winner @ 0.60, LOW | 0.37 | 0.31 | -0.06 |

**Net effect:** Loser bids will be **lower** (more aggressive), reflecting the actual larger drops observed. This should:
- Increase passive hedge fill rate (more room for loser to drop to our bid)
- Reduce pair cost when filled
- Improve $/hr performance

---

## Validation

Run sanity check:
```bash
python research/spike_param_optimizer.py --quick --path path1 --workers 2
```

Compare:
1. Total trades (should be similar)
2. Passive hedge % (may increase slightly)
3. $/hr (primary metric)

---

## Questions Answered

### 1. Linear vs Multiple Regression?
**Answer:** For short-term (60s) drops relevant to hedge fills, neither approach provides meaningful predictive power. A calibrated constant (0.10) outperforms both.

### 2. Interaction effects significant?
**Answer:** spike × velocity interaction was nearly significant (p=0.051) for drops-until-resolution, but **not significant for 60s drops** which matter for hedging.

### 3. Path-specific models?
**Answer:** Path 2 model showed higher R² (0.67) but with only 61 samples and counterintuitive coefficients. Not recommended for production.

### 4. Practical improvement?
**Answer:** The new formula reduces expected RMSE by 24% (0.110 → 0.083) by simply predicting closer to the actual mean drop.

---

## Files Created/Modified

| File | Action |
|------|--------|
| `research/hedge_pricing_analysis.py` | Created - full regression analysis script |
| `research/spike_param_optimizer.py` | Modified - updated `calc_loser_bid()` |
| `research/HEDGE_PRICING_FINDINGS.md` | Created - this document |
| `research/hedge_analysis_results.csv` | Created - model comparison data |

---

## Conclusion

The analysis confirms that **multiple regression does not meaningfully improve hedge pricing** for short-term (60s) drops because spike_magnitude has no predictive power.

However, the analysis revealed that the **current formula severely underpredicts drops** (0.03 vs actual 0.10). The fix is straightforward: increase the base expected drop from 0.01 to 0.08.

The recommended new formula:
```python
expected_drop = 0.08 + 0.50 * spike_mag / 100 + regime_bonus
```

This should improve passive hedge fill rates and reduce pair costs.
