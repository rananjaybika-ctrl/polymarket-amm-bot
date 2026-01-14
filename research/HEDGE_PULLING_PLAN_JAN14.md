# Plan: Sequential Entry→Hedge with 6-Zone Offsets

**Date:** January 14, 2026
**Task:** Implement LIMIT ORDER entry/hedge with velocity-based offsets
**Status:** IMPLEMENTED

---

## CRITICAL: Pulling Behavior Analysis

### Current State (CONFUSION EXISTS IN CODE)

| Type of Pulling | Code Location | Status | Problem |
|-----------------|---------------|--------|---------|
| Entry pulling (velocity flip) | `should_pull_entry()` | **DISABLED** | Function deprecated, not called |
| Zone transition pulling | `check_velocity_zone_transition()` | **DISABLED** | Returns `sides_to_pull` but `run_paper_bot.py` ignores it (line 4397-4405) |
| Hedge target tightening | `maybe_tighten_hedge_target()` | **PARTIAL** | Updates internal state only |
| Hedge quote capping | `_generate_side_quotes()` | **ACTIVE** | Caps NEW quotes, but OLD orders stay on book |

### THE BUG: Tightening Doesn't Actually Pull Orders

```
Current behavior (BROKEN):
1. Entry fills at $0.55
2. Initial hedge_target = $0.41 (strong zone)
3. Bot posts hedge order at $0.41
4. Velocity strengthens → target tightens to $0.39
5. maybe_tighten_hedge_target() updates locked_hedge_target to $0.39
6. BUT the $0.41 order is STILL on the book!
7. If ask hits $0.41, we fill there instead of waiting for $0.39
```

### What Should Happen (CORRECT)

```
1. Entry fills at $0.55
2. Post hedge at initial target ($0.41)
3. Velocity strengthens → target tightens to $0.39
4. Strategy signals "hedge target changed"
5. Bot runner CANCELS $0.41 order  ← MISSING
6. Bot runner POSTS new order at $0.39
7. Fill at $0.39 (tightened price)
```

---

## Desired Final Configuration

| Order Type | Pulling | Reason |
|------------|---------|--------|
| **Entry** | **OFF** | Let entry fill even if velocity flips (backtest: +$0.30 profit vs pulling ON) |
| **Hedge** | **ON** | Pull and repost when target tightens (backtest: +$6.30 profit vs no tightening) |

---

## Implementation Plan

### Step 1: Add Hedge Target Change Detection

Add to `SpreadCaptureStrategy`:

```python
def check_hedge_target_change(self, velocity_bps: float) -> Tuple[bool, Optional[float], Optional[float]]:
    """
    Check if hedge target should be tightened and order pulled.

    Returns:
        (should_pull, old_target, new_target)
    """
    s = self.state
    if s.first_fill_side is None or s.locked_hedge_target is None:
        return (False, None, None)

    # Only tighten if velocity is in same direction as at entry
    current_vel_dir = "UP" if velocity_bps > 0 else "DOWN"
    if current_vel_dir != s.first_fill_velocity_dir:
        return (False, None, None)

    # Calculate new target based on current velocity zone
    new_target = self.calculate_hedge_target(s.first_fill_price, velocity_bps)

    # ONLY TIGHTEN (lower target), NEVER LOOSEN
    if new_target < s.locked_hedge_target:
        old_target = s.locked_hedge_target
        s.locked_hedge_target = new_target
        s.current_velocity_zone = self.get_velocity_zone_name(velocity_bps)
        return (True, old_target, new_target)

    return (False, None, None)
```

### Step 2: Update Bot Runner

In `run_paper_bot.py`, after getting quotes:

```python
# Check if hedge target changed (requires pulling hedge order)
should_pull_hedge, old_target, new_target = strategy.check_hedge_target_change(velocity_bps)

if should_pull_hedge:
    logger.info(f"[SPREADCAP] Hedge target tightened: ${old_target:.4f} → ${new_target:.4f}")

    # Determine hedge side (opposite of entry)
    hedge_side = "DOWN" if strategy.state.first_fill_side == "UP" else "UP"

    # Cancel existing hedge order
    await self._cancel_orders_for_side(market, hedge_side)

    # New quote will be generated with tightened target on next tick
```

### Step 3: Clean Up Conflicting Code

1. **Remove unused configs:**
   - `enable_auto_pull` (line 354) - not used
   - `pull_mode` (line 355) - not used

2. **Update docstrings to clarify:**
   - Entry pulling: OFF (deprecated)
   - Zone transition pulling: OFF (disabled)
   - Hedge tightening pulling: ON (active)

3. **Consider renaming:**
   - `maybe_tighten_hedge_target()` → now triggers actual order pull
   - Or split into `calculate_tightened_target()` + `check_hedge_target_change()`

---

## Files to Modify

| File | Changes |
|------|---------|
| `src/strategies/spread_capture.py` | Add `check_hedge_target_change()`, remove unused pull configs, update docstrings |
| `scripts/run_paper_bot.py` | Add hedge pull logic after zone transition check (~line 4406) |

---

## Model: Sequential Entry Then Hedge

```
PHASE 1: ENTRY (winner side only)
         - Post LIMIT BID at best_bid + winner_offset
         - NO pulling on velocity flip
         - Wait for fill via WebSocket

PHASE 2: HEDGE (after entry fills)
         - Calculate hedge_target = pair_target - entry_price
         - Post LIMIT BID at hedge_target
         - On each tick: check if target should tighten
         - If tighten: PULL old order, POST at new target
         - Wait for hedge fill
```

---

## 6-Zone Offset Scheme

| Zone | Velocity (bps) | Winner Offset | Pair Target |
|------|---------------|---------------|-------------|
| neutral | 0.00 - 0.05 | -0.01 | 0.97 |
| moderate | 0.05 - 0.10 | -0.01 | 0.97 |
| strong | 0.10 - 0.30 | 0.00 | 0.96 |
| very_strong | 0.30 - 0.50 | +0.01 | 0.95 |
| extreme | 0.50 - 1.00 | +0.01 | 0.94 |
| super_strong | 1.00+ | +0.02 | 0.93 |

**Tightening Logic:**
- Entry fills in "strong" zone → hedge_target = 0.96 - entry_price
- Velocity strengthens to "very_strong" → new_target = 0.95 - entry_price
- New target is LOWER → PULL and repost

---

## Backtest Results

### Entry Pulling: ON vs OFF

| Metric | Pulling ON | Pulling OFF | Winner |
|--------|-----------|-------------|--------|
| Hedged Trades | 30 | 31 | OFF |
| Wrong Side Fills | 0 | 1 | ON |
| Profit | $26.55 | $26.85 | **OFF (+$0.30)** |

**Decision: Entry Pulling OFF**

### Hedge Tightening: ON vs OFF

| Metric | Tighten ON | Tighten OFF | Winner |
|--------|-----------|-------------|--------|
| Avg Pair Cost | $0.9270 | $0.9410 | ON |
| Total Profit | $32.85 | $26.55 | **ON (+$6.30)** |
| ROI | 19.3% | 15.6% | ON |
| Tighten Events | 51 | 0 | - |

**Decision: Hedge Tightening ON** (requires hedge order pulling)

---

## Final Configuration Summary

```
ENTRY ORDER:
  - Post at: best_bid + winner_offset
  - Pulling: OFF (let it fill even if velocity flips)

HEDGE ORDER:
  - Post at: hedge_target = pair_target - entry_price
  - Pulling: ON (pull and repost when velocity strengthens)
  - Rule: Only tighten, NEVER loosen
```

**Expected Performance (based on 7-hour backtest):**
- ROI: +19.3%
- Hourly Profit: ~$4.03/hour
- Win Rate: 100% on hedged trades
