# Strategy Ideas for Analysis - Jan 5, 2026

Based on what-if analysis of Jan 4-5 session (31 markets, 10:45 PM - 6:42 AM IST)

---

## IDEA 1: One Buy Per Side (15/15)

### Concept
- Buy exactly ONE time per side per market
- 15 shares each side (or 5/5 conservative, 30/30 aggressive)
- Fixed threshold, no decay
- Stop after first buy on each side

### Simulated Results (from 31 markets)
```
Win Rate:        100% (if both sides hedge)
Avg Pair Cost:   $0.81
Total P&L:       +$87 (at 15/15)
P&L per market:  +$2.82
```

### The Catch
```
Both sides hit $0.47 threshold: 19/31 (61%) → hedged, profit
One side missed threshold:      12/31 (39%) → unhedged risk

Unhedged losses: $38.95 from the 12 markets where one side never went cheap
```

### Implementation
```python
up_bought = False
down_bought = False
THRESHOLD = 0.47  # Fixed, no decay
SHARES = 15

def on_price_update(side, price):
    if side == 'UP' and not up_bought and price < THRESHOLD:
        buy('UP', SHARES)
        up_bought = True

    if side == 'DOWN' and not down_bought and price < THRESHOLD:
        buy('DOWN', SHARES)
        down_bought = True
```

### Key Parameters to Test
- Threshold: $0.45, $0.47, $0.50, $0.55
- Shares: 5, 15, 30
- Force hedge at end? (with 2 min left, buy at any price)

---

## IDEA 2: Inverted Decay

### Concept
- REVERSE the current decay logic
- Early market = relaxed threshold (accumulate)
- Late market = strict threshold (don't chase)
- Still allows multiple buys, but blocks expensive late buys

### Current vs Inverted
```
CURRENT DECAY (causes chasing):
  > 10 min left:  Threshold = $0.45 (strict)
  5-10 min left:  Threshold = $0.55 (relaxed)
  < 5 min left:   Threshold = $0.65 (very relaxed) ← PROBLEM

INVERTED DECAY (proposed):
  > 10 min left:  Threshold = $0.55 (relaxed)
  5-10 min left:  Threshold = $0.50 (normal)
  < 5 min left:   Threshold = $0.45 (strict) ← BLOCKS CHASING
```

### Simulated Results
```
Blocked late buys:    24 expensive trades
Savings:              $33.70
Session P&L change:   -$1.70 → +$32.00
```

### Example: Market 1767560400
```
Current decay bought:
  $0.28, $0.35, $0.62, $0.66, $0.70, $0.74, $0.78, $0.90, $0.96
  Avg: $0.53, Lost $1.65

Inverted would buy:
  $0.28, $0.35 only (late buys blocked)
  Avg: $0.30, Profit ~$0.75
```

### Implementation
```python
def get_threshold(time_remaining_min):
    # INVERTED: stricter as time passes
    if time_remaining_min > 10:
        return 0.55  # Early: relaxed, accumulate
    elif time_remaining_min > 5:
        return 0.50  # Mid: normal
    else:
        return 0.45  # Late: strict, no chasing
```

### Key Parameters to Test
- Early threshold: $0.52, $0.55, $0.58
- Late threshold: $0.42, $0.45, $0.48
- Decay curve: linear vs exponential

---

## COMPARISON

| Aspect | 1-Buy (15/15) | Inverted Decay |
|--------|---------------|----------------|
| Complexity | Simple | Moderate |
| Buys per side | Exactly 1 | Multiple (early) |
| Late behavior | No buys | Strict threshold |
| Simulated P&L | +$87 | +$32 |
| Unhedged risk | 39% of markets | Similar |
| Implementation | Easy | Modify decay function |

---

## WHY CURRENT DECAY BACKFIRED

### The Problem
```
Late market = trending = one side expensive
Current logic: "Threshold relaxed, buy anyway" → CHASES

Example from 1767564000:
  UP chased from $0.31 → $0.95 (12 buys!)
  Lost $1.90 when DOWN won
```

### Root Cause
The decay assumes "I need to hedge before time runs out"
But in trending markets, hedging late = buying at worst prices

---

## DATA FILES FOR RE-ANALYSIS

- Session data: `analysis/whatif_session_2026-01-04.json`
- Trade logs: `web/live_trades_calculus_maker_2026-01-04.csv`
- What-if simulator: `scripts/whatif_simulator.py`

### Key Markets to Analyze (had chasing)
1. `1767560400` - Chased UP from $0.28 to $0.96
2. `1767561300` - Chased DOWN from $0.50 to $0.84
3. `1767564000` - Chased UP from $0.31 to $0.95

---

## QUESTIONS TO ANSWER IN DEEP ANALYSIS

1. What was the ACTUAL first buy price on each side? (not estimated)
2. At what timestamp did each buy occur?
3. Can we correlate buy timing with threshold decay?
4. What if we force hedge at 2 min regardless of price?
5. Optimal threshold value for 1-buy strategy?
6. Does inverted decay still leave us unhedged too often?

---

## SESSION CONTEXT

- Date: Jan 4-5, 2026
- Time: 10:45 PM IST to 6:42 AM IST
- Markets: 31 resolved
- Actual P&L: -$1.70 (bot), ~-$10 (including manual)
- Strategy: ACCUM (Calculus Maker)
