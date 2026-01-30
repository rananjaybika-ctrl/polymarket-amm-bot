# Grid Strategy - Walk-Forward Validation Results

**Generated:** 2026-01-29 14:02:58

**Config:** GRID_45_NOSKEW

## Summary

**Overall Verdict:** FAIL - OVERFIT

## Period Results

| Period | Sharpe | $/hr | Pairs | Win Rate | Status |
|--------|--------|------|-------|----------|--------|
| IS | 0.401 | $1.86 | 4010 | 65.9% | FAIL |

## Sharpe Ratio Analysis


## Acceptance Criteria

| Criterion | Target | Actual | Status |
|-----------|--------|--------|--------|
| IS Sharpe | > 0.5 | 0.401 | FAIL |

## IS Detailed Results

| Metric | Value |
|--------|-------|
| Total PnL | $39.50 |
| Hedged PnL | $401.00 |
| Unhedged PnL | $-361.50 |
| Runtime | 21.27 hrs |
| Markets | 82 |
| Total Pairs | 4010 |
| Avg Pair Cost | $0.900 |

## Configuration (Fixed - No Re-tuning)

```python
name: GRID_45_NOSKEW
base_up_bid: 0.45
base_down_bid: 0.45
skew_per_inventory: 0.0
min_time_remaining: 60.0s
fill_delay_ms: 200
max_inventory: 30
```

## Key Insights

**Grid Strategy Mechanics:**
- Bids at FIXED LOW levels ($0.45 each side)
- Captures mean reversion when prices drop
- No tracking of mid-price (avoids adverse selection)
- Pair cost = $0.90 → captures $0.10 spread per pair

---

## CRITICAL COMPARISON: Grid vs AS

| Metric | Grid (Fixed Levels) | AS Fixed | AS Original |
|--------|---------------------|----------|-------------|
| **Hedged PnL** | **+$401.00** | +$11.60 | -$2.86/hr |
| **Unhedged PnL** | -$361.50 | -$24.50 | +$20.90/hr |
| **Pair Cost** | $0.900 | $0.967 | $1.013 |
| **Pairs** | 4,010 | 35 | ~500 |
| **$/hr** | $1.86 | -$1.01 | +$18.04 |
| **Sharpe** | 0.401 | -6.80 | N/A |

### The Fundamental Difference

**AS (Tracking Mid-Price):**
```
Market mid: $0.52
AS bids: $0.48 (spread below mid)
Fill happens when: Market ask drops to $0.48
This means: Market moved AGAINST us to fill
Result: Adverse selection → pair cost > $1.00
```

**Grid (Fixed Low Levels):**
```
Grid bids: $0.45 (fixed, regardless of mid)
Fill happens when: Price drops to $0.45
This means: We buy when side is CHEAP (losing)
Result: Mean reversion → pair cost < $1.00
```

### Why Grid's Hedged PnL is Positive

1. **Grid bids LOW** → only fills when prices crash to $0.45
2. **Both sides cheap** → UP@$0.45 + DOWN@$0.45 = $0.90 pair
3. **Mean reversion** → prices oscillate, we buy cheap
4. **$0.10 spread captured** per pair

### Why AS's Hedged PnL is Negative

1. **AS tracks mid** → bids move with market
2. **Fills on adverse moves** → we fill when market moves against us
3. **By fill time** → signal already confirmed, price already moved
4. **Pair cost > $1.00** → losing money on hedged positions

### Key Takeaway

**The Grid strategy SOLVES the adverse selection problem** that plagued AS:
- Grid: +$401 hedged PnL (pairs profitable)
- AS: -$2.86/hr hedged PnL (pairs losing)

The Grid's issue is unhedged positions (-$361.50), not the pair mechanics.

---

## Recommendations

1. **Grid approach is structurally sound** - hedged pairs are profitable
2. **Reduce inventory limits** to minimize unhedged exposure
3. **Combine with AGGRESSIVE** - use Grid for accumulation, AGGRESSIVE for directional
4. **Need OOS data** to validate robustness (only Jan 16-17 available)

### Files Summary

| File | Strategy | Hedged PnL | Status |
|------|----------|------------|--------|
| `as_grid_hybrid_backtest.py` | Grid (fixed levels) | +$401 | **Pairs Profitable** |
| `as_fixed_backtest.py` | AS Fixed (multiplicative) | +$11.60 | Marginal |
| `as_binary_backtest.py` | AS (calibrated for binary) | Negative | Failed |
| `as_proper_backtest.py` | AS (paper formulas) | Negative | Failed |

---

*The Grid strategy represents a fundamental shift from signal-following (AS) to mean-reversion capture (Grid).*
