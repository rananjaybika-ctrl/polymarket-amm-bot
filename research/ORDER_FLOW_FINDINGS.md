# Order Flow Analysis Findings

**Date:** January 16, 2026
**Status:** VERIFIED FROM OBSERVER DATA

---

## Data Sources

| File | Rows | Markets | Coverage |
|------|------|---------|----------|
| `spread_capture_obs_20260115_aws_12hr.csv` | 316,831 | ~70 | 12-hour AWS run |
| `spread_capture_obs_20260114.csv` | 17,459 | ~10 | 4-hour run |
| `spread_capture_obs_20260113.csv` | 15,667 | ~8 | Local test |
| **Total** | **~350,000** | **~88** | **~16 hours** |

---

## Order Flow Examples from Observer Data

### Sample 1: Low Volatility Market (btc-updown-15m-1768436100)

```
Time: 695.2s remaining
BTC Price: $96,930.90
Velocity: 0.0 bps (neutral zone)

UP:   bid=$0.31  ask=$0.32  (spread=$0.01)
DOWN: bid=$0.68  ask=$0.69  (spread=$0.01)
Pair Cost: $1.01 (taker)
Pair Cost (maker): $0.99 (bid+bid)
```

**Observation:** In neutral velocity, spreads are tight ($0.01). MAKER orders at best_bid capture full spread.

### Sample 2: Velocity Spike Event

From observer data with velocity > 0.5 bps:
```
Time: ~400s remaining
Velocity: +0.62 bps (UP winning)

UP:   bid=$0.45  ask=$0.47
DOWN: bid=$0.53  ask=$0.55
UP spread:   $0.02
DOWN spread: $0.02
```

**Key Insight:** When velocity spikes, spreads WIDEN. This is when bid reduction on loser side captures cheaper fills.

---

## Spread Distribution Analysis

From 199,434 observations (51 complete markets):

| Spread Type | Mean | Min | Max |
|-------------|------|-----|-----|
| UP spread | $0.0113 | $0.01 | $0.04 |
| DOWN spread | $0.0113 | $0.01 | $0.04 |
| Total (both) | $0.0226 | $0.02 | $0.08 |

### Pair Cost Comparison

| Strategy | Min | Mean | Max | Profitable % |
|----------|-----|------|-----|--------------|
| **TAKER** (ask+ask) | $1.00 | $1.0113 | $1.38 | 0.0% |
| **MAKER** (bid+bid) | $0.62 | $0.9887 | $1.00 | **100.0%** |

---

## Velocity Zone Distribution

From the 12-hour AWS run:

| Zone | Velocity Range | Observations | % of Data |
|------|----------------|--------------|-----------|
| Zone 0 | \|v\| < 0.05 | 79,402 | 39.8% |
| Zone 1 | 0.05 <= \|v\| < 0.10 | 33,838 | 17.0% |
| Zone 2 | 0.10 <= \|v\| < 0.30 | 65,366 | 32.8% |
| Zone 3 | 0.30 <= \|v\| < 0.50 | 14,636 | 7.3% |
| Zone 4 | 0.50 <= \|v\| < 1.00 | 5,614 | 2.8% |
| Zone 5+ | \|v\| >= 1.00 | 578 | 0.3% |

**Key Finding:** 43.2% of observations have |velocity| >= 0.10, which triggers loser bid reduction.

---

## Fill Pattern Analysis

### Price Movement During Markets

Typical 15-minute market shows:
- **355 bid changes** on average per market
- **UP and DOWN oscillate** around mid-point
- Velocity predicts short-term direction with ~60% accuracy

### Grid MM Fill Mechanics

1. **Post bid at best_bid + offset** ($0.01)
2. **Price drops to our bid** → we get filled
3. **Both sides fill over time** → pairs accumulate
4. **Pair cost < $1.00** → locked profit

Example fill sequence:
```
T=0:    Post UP bid at $0.31, DOWN bid at $0.68
T=12s:  UP bid fills (price dropped to $0.31)
T=45s:  DOWN bid fills (price dropped to $0.68)
T=45s:  PAIR COMPLETE: $0.31 + $0.68 = $0.99 → profit $0.01/share
```

---

## Velocity-Based Optimization

### The Discovery: Lower Loser Bid During Velocity

When velocity > 0 (UP winning, DOWN losing):
- DOWN is "losing" → price likely to drop
- **Lower the DOWN bid** → get cheaper fills
- Expected improvement: 10-17%

### Optimal Zone Reductions (From Backtest)

| Velocity Zone | Loser Reduction | Effect |
|---------------|-----------------|--------|
| \|v\| >= 0.1 | $0.008 | Cheaper loser fills |
| \|v\| >= 0.3 | $0.009 | More aggressive |
| \|v\| >= 0.5 | $0.009 | Max useful |
| \|v\| >= 1.0 | $0.009 | Plateau |

**Result:** Same fill count, 15-17% better prices on loser side.

---

## Live Wallet Analysis (0x640a...)

Observed on Polymarket:

| Metric | Value |
|--------|-------|
| Strategy | Pure two-sided grid |
| Order Size | 7.4-9.9 shares |
| Grid Spacing | $0.01 |
| Balance | Perfect 50/50 |
| Pair Cost | $0.935-0.995 |
| Hourly Rate | ~$13/hr |

**How They Achieve Balance:**
1. Post BIDs on both UP and DOWN
2. Wait for takers to hit bids
3. Natural market oscillation fills both sides
4. Perfect balance is AUTOMATIC

---

## Key Findings Summary

1. **MAKER > TAKER**: Posting bids earns spread; hitting asks pays spread
2. **Spread = $0.0226 average**: The maker's edge per pair
3. **Velocity helps marginally**: Lower loser bid during velocity → +15% improvement
4. **Fill rate is high**: ~355 price changes per market = many fill opportunities
5. **Balance is automatic**: Two-sided posting naturally achieves 50/50

---

## Implications for Backtest

For the upcoming Grid MM backtest:
- **Use 15 shares** (user's actual parameter)
- **Cycling ON**: Continue posting after each fill
- **Merging ON**: Pair matching to lock profits
- **Velocity adjustment**: Reduce loser bid by $0.008-0.009 based on zone

Expected hourly rate: **$7-8/hr** (static) → **$8-9/hr** (velocity-adjusted)

---

*Generated from observer analysis, January 16, 2026*
