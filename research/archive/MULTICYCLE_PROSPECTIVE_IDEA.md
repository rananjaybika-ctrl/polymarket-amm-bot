# Multi-Cycle Mode - Prospective Idea (ARCHIVED)

**Date:** January 31, 2026
**Status:** ABANDONED - Single-cycle proven optimal
**Archived:** February 1, 2026

---

## Why Multi-Cycle Was Abandoned

After extensive testing, multi-cycle mode **destroyed profitability**:

| Mode | Win Rate | $/hr | Trades/hr |
|------|----------|------|-----------|
| SINGLE | 54.3% | +$1.37 | 2.9 |
| MULTI | 39.8% | -$26.70 | 29+ |

**Root Cause:** 99% of consecutive spikes are in the same direction and within 180s. These aren't independent opportunities—they're duplicate signals from the same BTC move. Multi-cycle re-traded the same signal at worse prices.

---

## Original Plan (For Future Reference)

### Problem Statement

The multi-cycle backtest had a **critical bug**: it allowed entering spikes in OPPOSITE directions simultaneously.

```
t=100: BTC spikes UP   → Enter Cycle 1: winner_side=UP, loser_side=DOWN
t=105: BTC reverses    → Enter Cycle 2: winner_side=DOWN, loser_side=UP
```

**Result:** Conflicting positions that fight each other.

### Proposed Solutions

#### 1. MULTI_BUILD (Same direction only)
- Only enter if spike direction matches existing active cycles
- If holding UP cycles, only enter new UP spikes
- Skip opposite direction spikes entirely

#### 2. MULTI_CLEAR (Wait for clear)
- Only enter opposite direction after ALL existing cycles are closed
- If holding UP cycles, wait until they all complete before entering DOWN
- More conservative, prevents rapid direction flipping

### Implementation (NEVER DEPLOYED TO LIVE)

Direction mode constants added to `src/core/trading_utils.py`:
```python
DIRECTION_MODE_SINGLE = "single"  # 1 cycle at a time (PRODUCTION)
DIRECTION_MODE_BUILD = "build"    # DEPRECATED
DIRECTION_MODE_CLEAR = "clear"    # DEPRECATED
```

### Why It Failed

Even with direction consistency fix, MULTI_BUILD and MULTI_CLEAR had identical behavior (both allow same direction, reject opposite). The real problem wasn't direction conflicts—it was that stacking same-direction trades catches weak follow-on spikes that dilute edge.

**The first spike is a strong signal; subsequent spikes are noise.**

---

## Conclusion

**Single-cycle's 180s blocking is the SECRET SAUCE**, not a limitation. It correctly ignores duplicate signals.

If multi-cycle is ever revisited:
1. Need completely different approach (not direction-based)
2. Would need to detect truly independent signals
3. Likely requires different spike detection logic entirely

**Reference:** `research/findings/SINGLE_CYCLE_OPTIMAL_20260131.md`

---

*Archived for historical reference. DO NOT wire to live trading.*
