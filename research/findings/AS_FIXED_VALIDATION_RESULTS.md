# AS Fixed Strategy - Walk-Forward Validation Results

**Generated:** 2026-01-29 13:35:47

**Config:** AS_FIXED_WF


## Summary

**Overall Verdict:** FAIL - OVERFIT


## Period Results

| Period | Sharpe | $/hr | Pairs | Win Rate | Status |
|--------|--------|------|-------|----------|--------|
| IS | -6.802 | $-1.01 | 35 | 65.7% | FAIL |
| OOS3+4 | 120.720 | $15.73 | 10 | 80.0% | PASS |
| OOS5 | -21.964 | $-3.26 | 2 | 50.0% | FAIL |

## Sharpe Ratio Analysis

- **IS Sharpe:** -6.802
- **OOS Sharpe:** 120.720
- **Degradation:** 0.0%

## Acceptance Criteria

| Criterion | Target | Actual | Status |
|-----------|--------|--------|--------|
| IS Sharpe | > 0.5 | -6.802 | FAIL |
| OOS3+4 Sharpe | > 0.3 | 120.720 | PASS |
| OOS5 Sharpe | > 0 | -21.964 | FAIL |

## IS Detailed Results

| Metric | Value |
|--------|-------|
| Total PnL | $-12.90 |
| Hedged PnL | $11.60 |
| Unhedged PnL | $-24.50 |
| Runtime | 12.75 hrs |
| Markets | 49 |
| Total Pairs | 35 |
| Avg Pair Cost | $0.967 |

## OOS3+4 Detailed Results

| Metric | Value |
|--------|-------|
| Total PnL | $69.90 |
| Hedged PnL | $6.60 |
| Unhedged PnL | $63.30 |
| Runtime | 4.44 hrs |
| Markets | 17 |
| Total Pairs | 10 |
| Avg Pair Cost | $0.934 |

## OOS5 Detailed Results

| Metric | Value |
|--------|-------|
| Total PnL | $-13.30 |
| Hedged PnL | $0.90 |
| Unhedged PnL | $-14.20 |
| Runtime | 4.08 hrs |
| Markets | 16 |
| Total Pairs | 2 |
| Avg Pair Cost | $0.955 |

## Configuration (Fixed After IS)

```python
name: AS_FIXED_WF
z_zone: [0.0, 1.5]
skip_low_regime: True
adaptive_time_stop: True
min_time_remaining: 180.0s
hedging: True
min_signal_score: 0.02
max_order_age_ms: 3000
```

## Recommendations

Strategy failed validation. Consider:
1. Simplify to fewer parameters
2. Remove time-based filters (empirical)
3. Focus on structural features only
4. Consider abandoning AS for AGGRESSIVE (which is robust)