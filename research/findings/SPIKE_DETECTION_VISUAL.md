# Spike Detection Logic - Visual Explanation

## How Spikes Are Currently Detected

### The Core Formula

```
spike_magnitude = abs(price_now - price_72_ticks_ago) / price_72_ticks_ago * 100

if spike_magnitude >= threshold:
    direction = "UP" if price_now > price_72_ticks_ago else "DOWN"
    → SPIKE DETECTED
```

At 60Hz, 72 ticks = **1200ms lookback window**

---

## Visual: Single Price Move Generating Multiple Spikes

### The Problem

```
TIME (ms)    0    200   400   600   800   1000  1200  1400  1600  1800  2000  2200  2400
             │     │     │     │     │      │     │     │     │     │     │     │     │
BTC PRICE    ─────────────────/
             100000            \
                                ────────────────────────────────────────────────────
                               100025
             │     │     │     │     │      │     │     │     │     │     │     │     │
             │     │     │     │     │      │     │     │     │     │     │     │     │
LOOKBACK     │<────────1200ms────────>│     │     │     │     │     │     │     │     │
WINDOW @400  ├─────────────────┤     │     │     │     │     │     │     │     │     │
             │     │     │     │     │      │     │     │     │     │     │     │     │
LOOKBACK     │     │<────────1200ms────────>│     │     │     │     │     │     │     │
WINDOW @600  │     ├──────────────────┤    │     │     │     │     │     │     │     │
             │     │     │     │     │      │     │     │     │     │     │     │     │
LOOKBACK     │     │     │<────────1200ms────────>│     │     │     │     │     │     │
WINDOW @800  │     │     ├──────────────────┤     │     │     │     │     │     │     │
             │     │     │     │     │      │     │     │     │     │     │     │     │
LOOKBACK     │     │     │     │<────────1200ms────────>│     │     │     │     │     │
WINDOW @1000 │     │     │     ├──────────────────┤     │     │     │     │     │     │
```

**What happens:**
1. Price jumps from $100,000 to $100,025 at t=400ms
2. At t=400ms: Window compares $100,025 vs $100,000 → **SPIKE UP!**
3. At t=600ms: Window compares $100,025 vs $100,000 → **SPIKE UP!** (same comparison!)
4. At t=800ms: Window compares $100,025 vs $100,000 → **SPIKE UP!** (still!)
5. At t=1000ms: Window compares $100,025 vs $100,000 → **SPIKE UP!** (4th time!)
6. At t=1600ms: Window compares $100,025 vs $100,025 → No spike (finally stabilized)

**Result: ONE price move generates 4+ spike signals over 600ms**

---

## The MIN_CYCLE_GAP_MS "Protection"

Current setting: **MIN_CYCLE_GAP_MS = 50ms**

This only prevents entries within 50ms of the LAST HEDGE, not the last entry:

```
Timeline:
t=0:      Spike detected → ENTER TRADE #1
t=50ms:   Spike still detected, but we're IN_POSITION, so blocked
t=30000ms: Trade #1 hedges (passive fill after 30 seconds)
t=30050ms: Spike re-detected → ENTER TRADE #2  ← Gap met!
t=30100ms: Spike still detected → blocked (in position)
t=60000ms: Trade #2 hedges
t=60050ms: Spike re-detected → ENTER TRADE #3  ← Gap met!
```

**The gap is measured from HEDGE time, not entry time!**

---

## Evidence from OOS8 Data

### Market: btc-updown-15m-1769884200

**14 consecutive trades in 49 seconds:**

```
Trade# | Time Remaining | Direction | Spike Mag | Gap from Prev | PnL
-------|----------------|-----------|-----------|---------------|--------
  12   | 267.0s         | DOWN      | 0.0155    | -             | +$4.52
  13   | 264.1s         | DOWN      | 0.0159    | 2.9s          | +$4.55
  14   | 261.2s         | DOWN      | 0.0160    | 2.9s          | +$4.36
  15   | 258.4s         | DOWN      | 0.0159    | 2.8s          | +$4.57
  16   | 255.5s         | DOWN      | 0.0160    | 2.9s          | +$4.58
  17   | 252.7s         | DOWN      | 0.0163    | 2.8s          | +$4.35
  18   | 249.8s         | DOWN      | 0.0164    | 2.9s          | +$4.14
  19   | 246.9s         | DOWN      | 0.0163    | 2.9s          | +$4.36
  20   | 244.1s         | DOWN      | 0.0164    | 2.8s          | +$4.34
  21   | 238.4s         | DOWN      | 0.0161    | 5.7s          | +$4.55
  22   | 232.7s         | DOWN      | 0.0159    | 5.7s          | +$4.41
  23   | 226.9s         | DOWN      | 0.0159    | 5.8s          | +$4.12
  24   | 221.2s         | DOWN      | 0.0161    | 5.7s          | -$0.15
  25   | 217.6s         | DOWN      | 0.0157    | 3.6s          | +$4.01
```

**All 14 trades have nearly identical spike magnitude (~0.016)**
**This is ONE price move being detected 14 times!**

---

## Why EWMA-Based Spike Detection Could Help

### Current: Fixed Lookback Window

```
spike = (price_now - price_1200ms_ago) / price_1200ms_ago

Problem: Window slides forward, keeps comparing to OLD pre-spike price
```

### Alternative: EWMA-Based Change Detection

```
ewma_price = α * price_now + (1-α) * ewma_price_prev

spike = (price_now - ewma_price) / ewma_price

Advantage: EWMA adapts! After spike, ewma_price rises toward new level
```

**Visual comparison:**

```
TIME           0    400   800   1200  1600  2000
               │     │     │     │     │     │
PRICE          ─────/─────────────────────────
               100k  100.025k
               │     │     │     │     │     │
FIXED LOOKBACK │     │     │     │     │     │
REFERENCE      ──────────────────────────────  ← Stays at 100k for 1200ms
               100k                              (keeps detecting spike)
               │     │     │     │     │     │
EWMA REFERENCE │     │     │     │     │     │
(α=0.1)        ────────/──────────────────────  ← Rises after spike
               100k  100.002k  100.010k  100.018k  (stops detecting)
```

**EWMA naturally "absorbs" the price move** and stops re-detecting after a few hundred ms.

---

## OU vs EWMA: What We Already Know

### For SPIKE THRESHOLD (adaptive threshold level):
| Method | $/hr | Winner |
|--------|------|--------|
| **OU** | +30% | ✅ |
| EWMA | baseline | |

**OU wins** because it adapts threshold to volatility regime.

### For Z-SCORE FILTERING (when to trade):
| Method | Stability | Winner |
|--------|-----------|--------|
| **EWMA** | Adapts | ✅ |
| OU | Drifts | |

**EWMA wins** because OU parameters drift with price regime changes.

### For SPIKE DETECTION BASE (what we're questioning now):
**NOT YET TESTED** - Current system uses fixed 72-tick lookback.

---

## Proposed Test: EWMA-Based Spike Detection

Replace:
```python
# Current: Fixed lookback
change = (price - price_history[-73]) / price_history[-73]
```

With:
```python
# EWMA-based
ewma = ewma_prev * (1 - alpha) + price * alpha
change = (price - ewma) / ewma

# Keep OU adaptive THRESHOLD
threshold = compute_ou_threshold(volatility)

if abs(change) >= threshold:
    spike_detected()
```

**Expected benefits:**
1. EWMA adapts to new price level quickly
2. One price move = ONE spike signal (not 14)
3. Still use OU for adaptive threshold (best of both)

**Risk:**
- May reduce trade volume significantly
- Might miss follow-on moves in same direction
- Need to tune α (half-life) carefully

---

## Recommendation

1. **Test EWMA spike base** with α corresponding to 200-500ms half-life
2. **Keep OU adaptive threshold** (proven +30% vs EWMA threshold)
3. **Keep EWMA z-score filtering** (proven stable)
4. **Alternatively**: Add spike cooldown (1-2 second lockout after spike)

The 14-trades-in-49-seconds pattern suggests we're detecting the same move repeatedly. Whether this is good (capturing every opportunity) or bad (capital inefficiency) depends on whether these are truly independent opportunities or just noise.

**From OOS8 data: 86% of rapid sequences were profitable** - so it's working, but may be overkill.
