# Spread Capture Strategy Fix Plan

**Date:** January 14, 2026
**Status:** Analysis Complete, Implementation Pending

---

## Executive Summary

The 7-hour AWS observer run revealed that **velocity prediction works (94% accuracy)** but the **execution logic is broken**. Both sides fill at mid-market prices (~$0.50 each) resulting in pair costs >$1.00.

---

## Root Cause Analysis

### The Bug (lines 488-495 in spread_capture_observer.py)

```python
# Every sample: redefine which side is entry vs hedge based on current velocity
if entry_side == "UP":
    resting.up_bid = entry_price_up      # tight
    resting.down_bid = entry_price_down  # wide
elif entry_side == "DOWN":               # VELOCITY FLIPPED!
    resting.up_bid = entry_price_up      # NOW wide
    resting.down_bid = entry_price_down  # NOW tight
```

**What happens:**
1. t=924s: velocity=-0.078 → entry_side=DOWN → fills DOWN at $0.54
2. t=903s: velocity=+0.137 → entry_side=UP → fills UP at $0.47
3. Velocity FLIPPED, so strategy bought BOTH sides as "entry"!
4. Result: pair cost $1.06 LOSING

**The hedge logic never kicks in** because velocity flips redefine entry/hedge roles.

---

## Strategy Options Comparison

### Option 1: Current Behavior (Broken)

| Aspect | Description |
|--------|-------------|
| Logic | Redefine entry/hedge each sample based on velocity |
| Fill pattern | Both sides at mid-market (~$0.50) |
| Pair cost | ~$1.001 average |
| Expected P/L | -$0.001 per pair |

### Option 2: Fixed Hedge Price (Recommended)

| Aspect | Description |
|--------|-------------|
| Logic | Set hedge target ONCE when entry fills |
| Hedge price | `target_pair_cost - entry_fill_price` |
| Example | Entry $0.52 → Hedge target $0.45 → Pair cost $0.97 |
| Expected P/L | +$0.03 per pair (3% edge) |

**Implementation:**
```python
# When FIRST entry fills, lock in hedge target
if entry_filled and not hedge_target_set:
    if entry_side == "UP":
        hedge_target = 0.97 - entry_fill_price
        resting.down_bid = hedge_target  # FIXED, never change
    hedge_target_set = True
```

| Pros | Cons |
|------|------|
| Captures spread | May miss if price never reaches target |
| Pair cost < $1.00 by design | "Stuck" if prediction wrong |
| Clear profit target | Needs position tracking |

### Option 3: Pure Directional (No Hedge)

| Aspect | Description |
|--------|-------------|
| Logic | Buy predicted winner only, hold to expiry |
| Win rate | 94% (based on velocity accuracy) |
| Win profit | ~$0.45 (buy at $0.55, pays $1.00) |
| Loss | ~$0.55 (buy at $0.55, pays $0.00) |
| Expected P/L | +$0.39 per trade |

| Pros | Cons |
|------|------|
| Highest expected value | High variance |
| Simple logic | 6% total loss on wrong calls |
| No hedge complexity | Velocity flips cause confusion |

**User Decision:** Not comfortable with directional due to velocity flips and volatility.

---

## Clarification: Fill Timing

**Previous statement (inaccurate):**
> "Fill timing: Only at $0.01 (end of market)"

**Correction:** With fixed hedge, fills happen when `ask <= our_fixed_bid`:
- If hedge_target = $0.45 and ask drops to $0.45, fill happens
- NOT only at $0.01
- The $0.01 fills were an artifact of the chasing bug

---

## Implementation Plan

### Phase 1: Fix Hedge Logic

1. Track entry fill price when it occurs
2. Calculate hedge target: `target_pair_cost - entry_price`
3. Set hedge bid ONCE, don't update on velocity flips
4. Only fill hedge when `ask <= hedge_target`

### Phase 2: Add Position State Machine

```python
class MarketPosition:
    state: Literal["WAITING", "ENTRY_FILLED", "HEDGED"]
    entry_side: str
    entry_price: float
    hedge_target: float

    def on_entry_fill(self, side, price):
        self.state = "ENTRY_FILLED"
        self.entry_side = side
        self.entry_price = price
        self.hedge_target = 0.97 - price  # 3% edge target

    def on_hedge_fill(self, price):
        self.state = "HEDGED"
        # Pair complete, can merge
```

### Phase 3: Merge Cycling

When pair completes:
1. Merge UP + DOWN = $1.00 USDC
2. Recycle capital to next position
3. Track cumulative profit

---

## Files to Modify

1. `scripts/spread_capture_observer.py` - Fix hedge logic
2. `src/strategies/spread_capture.py` - Add state machine
3. NEW: `scripts/analyze_observer_fixed.py` - Backtest fixed logic

---

## Next Steps

1. Run deep analysis on 7-hour data with corrected resolution logic
2. Compare merging ON vs OFF scenarios
3. Calculate variance and risk metrics
4. Implement fixed hedge in observer
5. Run 12-hour validation test

---

*Plan created: January 14, 2026*
