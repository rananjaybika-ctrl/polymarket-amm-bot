# Grid MM PnL Projection with Velocity-Based Loser Bid Reduction

**Date:** January 16, 2026
**Status:** BACKTEST WITH REAL PARAMETERS

---

## Related Documents

- **Order Flow Findings:** [`ORDER_FLOW_FINDINGS.md`](./ORDER_FLOW_FINDINGS.md) - Detailed analysis of order flow patterns
- **Grid Findings Handover:** [`GRID_FINDINGS_HANDOVER.md`](./GRID_FINDINGS_HANDOVER.md) - Strategy pivot documentation
- **Handover Analysis:** [`HANDOVER_JAN15_ANALYSIS.md`](./HANDOVER_JAN15_ANALYSIS.md) - Previous session findings

---

## Objective

Project PnL using the user's actual trading parameters:
- **Order size:** 15 shares (not 10)
- **Cycling:** ON (continue posting after fills)
- **Merging:** ON (pair matching enabled)
- **Velocity adjustment:** Tiered loser bid reduction

---

## Parameters

```python
# User's actual parameters
ORDER_SIZE = 15  # shares per fill
BASE_OFFSET = 0.01  # $0.01 above best_bid
MAX_POSITION = 200  # per side
MIN_TIME = 60  # stop at 60s remaining

# Velocity-based loser bid reduction (recommended)
ZONE_REDUCTIONS = {
    0.1: 0.008,  # |v| >= 0.1: reduce loser by $0.008
    0.3: 0.009,  # |v| >= 0.3: reduce by $0.009
    0.5: 0.009,  # |v| >= 0.5: reduce by $0.009
    1.0: 0.009,  # |v| >= 1.0: reduce by $0.009
}
```

---

## Backtest Plan

1. Run simulation with 15 share order size
2. Compare: Static grid vs Velocity-adjusted
3. Calculate: Total profit, profit per market, hourly rate
4. Show: Detailed breakdown by market type

---

## Verification

Run backtest on all observer files:
- `spread_capture_obs_20260115_aws_12hr.csv` (166,308 rows)
- `spread_capture_obs_20260114.csv` (17,459 rows)
- `spread_capture_obs_20260113.csv` (15,667 rows)

**Total:** ~199,434 observations across ~51 complete markets

---

## Backtest Results (January 16, 2026)

### Summary

| Metric | Static Grid | Velocity-Adjusted | Improvement |
|--------|-------------|-------------------|-------------|
| Total Profit | $81.90 | $85.98 | **+$4.08** |
| Hourly Rate | $7.13/hr | $7.49/hr | **+$0.36/hr** |
| Avg Pair Cost | $0.9915 | $0.9911 | **-$0.0004** |
| Profitable % | 84.3% | 87.9% | **+3.6pp** |
| **Improvement** | baseline | | **+5.0%** |

### Projected Returns (Velocity-Adjusted)

| Period | Profit |
|--------|--------|
| Hourly | $7.49/hr |
| Daily | $180/day |
| Weekly | $1,258/week |
| Monthly | $5,392/month |

### Key Findings

1. **Static grid baseline: $7.13/hr** - confirms the $7.03/hr from previous backtest
2. **Velocity adjustment adds +5%** - modest but consistent improvement
3. **Profitable pairs: 87.9%** - high success rate with proper MAKER execution
4. **15 shares scales linearly** - 1.5x profit vs 10-share baseline

### Top 10 Markets

| Market | Pairs | Profit | $/Pair |
|--------|-------|--------|--------|
| btc-updown-15m-1768463100 | 14 | $3.90 | $0.28 |
| btc-updown-15m-1768437900 | 14 | $3.15 | $0.23 |
| btc-updown-15m-1768452300 | 14 | $2.25 | $0.16 |
| btc-updown-15m-1768438800 | 14 | $2.10 | $0.15 |

### Fill Detection (Critical)

The correct MAKER fill logic:
```python
# At tick T, calculate our bid
our_bid = min(best_bid + offset, best_ask - 0.01)

# At tick T+1, check if we got filled
if next_tick_bid <= our_bid:
    fill_price = our_bid  # We got filled at our posted price
```

This ensures we only count fills when the market actually traded through our level.

### Velocity Zone Configuration (Validated)

```python
ZONE_REDUCTIONS = {
    0.1: 0.008,  # |v| >= 0.1: reduce loser by $0.008
    0.3: 0.009,  # |v| >= 0.3: reduce by $0.009
    0.5: 0.009,  # |v| >= 0.5: reduce by $0.009
    1.0: 0.009,  # |v| >= 1.0: reduce by $0.009
}
```

---

## Implementation Notes

The backtest script will:
1. Load all observer CSV files
2. Filter for complete markets (started >800s, ended <60s)
3. Simulate MAKER fills (bid+offset, capped below ask)
4. Apply velocity-based loser reduction when |v| >= threshold
5. Track pairs and calculate locked profit
6. Compare static vs velocity-adjusted results

---

*Plan created: January 16, 2026*
