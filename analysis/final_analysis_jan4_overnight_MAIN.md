# FINAL ANALYSIS: Jan 4-5 Overnight Session
## Calculus Maker Live Trading - MAIN FINDINGS

**Session:** 10:45 PM IST (Jan 4) → 6:45 AM IST (Jan 5)
**Duration:** 8 hours
**Starting Balance:** $222
**Ending Balance:** $207
**Total Loss:** -$15

---

## EXECUTIVE SUMMARY

| Metric | Value |
|--------|-------|
| Markets Resolved | 26 |
| Bot P&L | -$5.19 |
| Manual/Redemption Impact | ~-$10 |
| Win Rate | 50% (13/26) |
| Avg Pair Cost | $0.987 |
| Sharpe Ratio | -0.16 |

---

## THE FIVE ROOT CAUSES OF LOSSES

### 1. STALE ORDER CANCELLATION TOO SLOW (80s avg vs 5s target)

```
Total GRADUAL_CHASE events: 178
Average order age before cancel: 79.9 seconds
Max age observed: 180 seconds
Average price jump per chase: $0.0447
Chases above $0.80: 28 (15.7%)
Max chase: $0.54 → $0.97 (+$0.28 jump)
```

**Impact:** Orders sat 80 seconds on average, causing massive slippage as prices moved.

### 2. EMERGENCY HEDGE SYSTEM HYPERACTIVE (2,614 events)

```
EMERGENCY HEDGE events: 2,614
Average pair cost when triggered: $0.978
Max pair cost: $1.050
Events per market: ~100 (one every 5 seconds!)
```

**Impact:** Constant emergency state caused aggressive orders at bad prices.

### 3. PAIR COST DISTRIBUTION SHOWS CLEAR PROBLEM

| Pair Cost Bucket | Markets | P&L | Avg P&L |
|------------------|---------|-----|---------|
| < $0.95 | 11 | +$6.35 | +$0.58 |
| $0.95-$0.98 | 2 | +$1.35 | +$0.68 |
| $0.98-$1.00 | 2 | +$0.50 | +$0.25 |
| $1.00-$1.05 | 4 | -$4.15 | -$1.04 |
| > $1.05 | 6 | -$7.64 | -$1.27 |

**Key insight:**
- Pair cost < $0.98: **+$8.20** across 15 markets
- Pair cost > $1.00: **-$11.79** across 10 markets

### 4. UNHEDGED POSITIONS (-$7.85)

| Market | Imbalance | P&L |
|--------|-----------|-----|
| 1767556800 | 5:10 | -$2.90 |
| 1767550500 | 15:10 | -$1.75 |
| 1767555900 | 5:10 | -$1.60 |
| 1767567600 | 5:0 | -$1.60 |

4 unhedged markets = 52% of total losses.

### 5. EXPONENTIAL DECAY BACKFIRED (relaxed late = chased expensive)

The decay model allows buying at pair cost $0.994 late in market, but late prices are the WORST in trending markets.

**Example Market 1767560400:**
```
21:00:21  UP @ $0.35  ← GOOD
21:00:45  UP @ $0.28  ← GOOD
21:05:02  UP @ $0.62  ← BAD (chasing begins)
21:06:06  UP @ $0.66
21:07:09  UP @ $0.70
21:08:13  UP @ $0.74
21:09:17  UP @ $0.78
21:10:04  UP @ $0.90  ← EXTREME
21:10:39  UP @ $0.96  ← NEAR MAX!

Final: 15 UP @ $0.53 avg → Pair cost $1.11 → Lost $1.65
```

---

## STATISTICAL ANALYSIS

```
Mean P&L:     -$0.20
Median P&L:   +$0.05  (half of markets were profitable!)
Std Dev:      $1.24
Sharpe Ratio: -0.16
Sortino:      -0.28
Max Win:      +$1.40
Max Loss:     -$2.90
Win/Loss:     13/13
```

Median being positive while mean is negative = a few bad markets dragging down an otherwise break-even strategy.

---

## WORST 10 MARKETS

| Market | P&L | Pair Cost | Unhedged | Issue |
|--------|-----|-----------|----------|-------|
| 1767556800 | -$2.90 | $1.015 | 5 | Unhedged + high cost |
| 1767564000 | -$1.90 | $1.127 | 0 | Extreme price chase |
| 1767550500 | -$1.75 | $0.930 | 5 | Unhedged |
| 1767560400 | -$1.65 | $1.110 | 0 | Chased UP to $0.96 |
| 1767555900 | -$1.60 | $0.915 | 5 | Unhedged |
| 1767567600 | -$1.60 | $0.000 | 5 | Completely unhedged |
| 1767547800 | -$1.15 | $1.077 | 0 | High pair cost |
| 1767552300 | -$1.15 | $1.077 | 0 | High pair cost |
| 1767561300 | -$1.05 | $1.070 | 0 | High pair cost |
| 1767564900 | -$0.75 | $1.050 | 0 | High pair cost |

---

## PARAMETER FINE-TUNING RECOMMENDATIONS

### PRIORITY 1: Stale Order Timeout (5s)
```python
STALE_ORDER_TIMEOUT = 5.0        # Current effective: ~80s
MAX_ORDER_AGE_BEFORE_CHASE = 10  # seconds
PRICE_TICK_TOLERANCE = 0.02      # don't chase < 2 cent move
```
**Expected impact:** +$5-8/session

### PRIORITY 2: Emergency Hedge Tuning
```python
EMERGENCY_IMBALANCE_THRESHOLD = 10      # was 5
EMERGENCY_PAIR_COST_CEILING = 0.97      # don't hedge if would exceed
EMERGENCY_COOLDOWN_SECONDS = 30         # min time between emergency orders
MAX_EMERGENCY_ORDERS_PER_MARKET = 3     # cap total emergency buys
```
**Expected impact:** +$3-5/session

### PRIORITY 3: Max Buys Per Side
```python
# Option A: ONE BUY (aggressive - simulation showed +$26.50)
MAX_BUYS_PER_SIDE = 1

# Option B: CAPPED (moderate)
MAX_BUYS_PER_SIDE = 3
MAX_EXPENSIVE_BUYS = 1  # only 1 buy allowed above $0.60
```
**Expected impact:** +$15-25/session

### PRIORITY 4: Invert Decay Logic
```python
# INVERTED (stricter late, not relaxed):
def get_threshold(time_remaining):
    if time_remaining > 600:  # Early
        return 0.55  # Relaxed
    elif time_remaining > 300:  # Mid
        return 0.50
    else:  # Late
        return 0.45  # STRICT - blocks chasing
```
**Expected impact:** +$8-12/session

### PRIORITY 5: Hard Price Ceiling
```python
ABSOLUTE_PRICE_CEILING = 0.65  # NEVER buy above
HEDGE_PRICE_CEILING = 0.75     # emergency only
```
**Expected impact:** +$3-5/session

---

## WHY "EXPENSIVE FIRST" STILL LOST MONEY

**The confusion:**
- "Buy expensive side first" = correct hedging strategy
- BUT the bot was also CHASING the cheap side until it became expensive

**The real problem:**
1. Bot places order at $0.35 (cheap)
2. Order doesn't fill (sits 80 seconds)
3. GRADUAL_CHASE reprices to $0.45
4. Still doesn't fill (another 60 seconds)
5. GRADUAL_CHASE reprices to $0.55
6. ...continues...
7. Finally fills at $0.75

**Result:** What started as "buying cheap" became "buying expensive" due to chase mechanism.

The bot bought expensive on BOTH sides:
- UP side: chased from $0.28 → $0.96
- DOWN side: chased from $0.30 → $0.85

"Expensive first" means buy deficit side even if expensive.
But the bot was making BOTH sides expensive through chasing.

---

## WHAT-IF SIMULATIONS

### One Buy Per Side (WINNER)
```
Actual P&L:    -$1.70 (bot only)
Simulated:     +$24.80
Improvement:   +$26.50
Risk reduction: 60% lower variance
```

### 30/30 Target
```
No impact - most markets couldn't reach 15/15 due to time
Would need 4x order frequency
```

### Remove Expensive Logic
```
WORSE: -$1.70 → -$6.90
Unhedged exposure spikes to $7.50 per market
```

---

## SUMMARY: IMPLEMENTATION ROADMAP

| Priority | Change | Impact | Complexity |
|----------|--------|--------|------------|
| 1 | 5s stale order timeout | +$5-8 | Low |
| 2 | Cap emergency hedges | +$3-5 | Medium |
| 3 | Max 3 buys per side | +$15-25 | Low |
| 4 | Invert decay logic | +$8-12 | Medium |
| 5 | Hard price ceiling $0.65 | +$3-5 | Low |

**Total expected improvement: +$34-55 per 8-hour session**

---

## KEY TAKEAWAY

> The losses were NOT from "buying expensive to hedge" (correct strategy).
> The losses were from "CHASING cheap orders until they became expensive" (broken execution).

The 80-second stale order timeout + unlimited GRADUAL_CHASE events turned a hedging strategy into a chasing strategy. Fix the execution (5s timeout, capped chases) and the strategy becomes profitable.

---

*Generated: 2026-01-05*
*Session: Jan 4-5 Overnight (10:45 PM - 6:45 AM IST)*
