# Plan: Fix Entry Fill Simulation & Optimizer Analysis

**Date:** January 19, 2026
**Objective:** Fix passive entry fill simulation to get realistic trade counts

---

## Problem Summary

The optimizer shows unrealistic results:
- **Path 1**: 99.6% of orders pulled (only 1-4 trades execute out of ~740 signals)
- **Path 2**: Only 13 trades in best config (statistically meaningless)

### Root Causes Identified

| Issue | Finding | Impact |
|-------|---------|--------|
| Fill simulation too strict | Checks `ask <= order_price` | Undercounts fills |
| 90% LOW regime | Velocity zone is "neutral" 90% of time | Signals filtered |
| Tight spreads | 75% of time spread = 0.01 | Entry at best bid |

### Key Data Findings

```
Spread Distribution:
  <= 0.01:  75% of time -> entry = bid (at best bid)
  == 0.02:  16% of time -> entry = bid+0.01
  >  0.02:   9% of time -> entry = bid+0.01 (truly passive)

Ask drops of 0.01+ (fill opportunities): 3.2% of observations
Observer sample rate: 205ms (our reaction speed)

Regime distribution:
  neutral (LOW):      90.2% -> SKIP (42.5% accuracy)
  moderate (MEDIUM):   8.6% -> TRADE (58.9% accuracy)
  strong/v_strong:     1.2% -> TRADE (66.4% accuracy)
```

---

## The Fill Simulation Issue

**Current code (line 551-554):**
```python
def check_order_fill(order: Dict, current_ask: float) -> bool:
    """Check if a passive order would fill at current ask."""
    return current_ask <= order['price']  # Too strict!
```

**Problem:** This requires the ENTIRE ask side to drop to our bid level.

**In reality:**
- Our order sits on the order book
- It fills when someone places a market SELL order
- The ask price may not change even when we get filled
- We get filled if we're at or near best bid and there's selling pressure

---

## Proposed Fix Options

### Option A: Check if we're at/above best bid (Recommended)

```python
def check_order_fill(order: Dict, current_bid: float, current_ask: float,
                     time_elapsed_ms: int) -> bool:
    """
    Order fills if:
    1. Our price >= current_bid (we're competitive)
    2. Spread is tight (active market)
    3. Some time has passed (for market activity)
    """
    spread = current_ask - current_bid
    at_best_bid = order['price'] >= current_bid
    tight_spread = spread <= 0.03
    min_time = time_elapsed_ms >= 500  # 500ms minimum for fill

    return at_best_bid and tight_spread and min_time
```

**Pros:** More realistic, matches how real order books work
**Cons:** May overcount fills (not all time at best bid = fill)

### Option B: Probabilistic fill based on position

```python
def check_order_fill(order: Dict, current_bid: float, time_elapsed_ms: int) -> bool:
    """Fill probability based on queue position and time."""
    position_in_queue = order['price'] - current_bid  # 0 = best bid, negative = worse
    if position_in_queue < -0.01:
        return False  # Not competitive

    # Probability increases with time at best bid
    base_prob = 0.1  # 10% per 200ms observation
    fill_prob = base_prob * (time_elapsed_ms / 200)

    return random.random() < min(fill_prob, 0.95)
```

**Pros:** Accounts for queue position and time
**Cons:** Adds randomness, harder to reproduce

### Option C: Use bid movement as proxy (Simplest)

```python
def check_order_fill(order: Dict, current_bid: float, prev_bid: float) -> bool:
    """Fill if bid moved up past our order (someone bought before us)."""
    # If bid increased, someone took liquidity at or above our price
    return current_bid > order['price']
```

**Pros:** Simple, no parameters
**Cons:** Only detects fills when bid actually moves

---

## Answers to Your Questions

### Q1: Do asks trade through our bid level?

**Answer: Yes, but rarely (~3.2% of observations)**

With spread = 0.01 (75% of time):
- Our entry = min(bid+0.01, ask-0.01) = bid
- We're AT the best bid, not above it
- Ask needs to drop 0.01 to cross our level

With spread = 0.02:
- Our entry = bid+0.01 (0.01 below ask)
- Slightly more passive

**The simulation is actually correct** - it's just that passive fills at best bid are genuinely rare (~3% of time).

### Q2: How do we enter more without being a taker?

**Options:**
1. **Accept lower fill rate** - Current approach is correct for passive
2. **Widen entry offset** - Use `bid + 0.02` instead of `bid + 0.01` (more passive, fewer fills)
3. **Time-based entry** - Wait for spread to widen, then enter
4. **Grid entry** - Place orders at multiple levels (already doing this)

The `min(bid+0.01, ask-0.01)` cap is actually clever - it prevents us from crossing the spread.

### Q3: Any trades in LOW regime?

**Answer: Very few - and this is CORRECT**

**Key distinction:**
- 90.2% of TIME is in neutral (LOW) velocity zone
- But only **5.4% of SPIKES** occur in LOW regime (38 out of 703)
- Spikes are concentrated in higher volatility periods

**Why LOW regime signals are unfixable:**

| Metric | LOW (neutral) | Non-LOW |
|--------|---------------|---------|
| Signals | 38 (5.4%) | 665 (94.6%) |
| Max interaction score | 0.002143 | 0.231775 |
| Median interaction | 0.001684 | **0.008365** |

The interaction score `spike * velocity` in LOW regime is **4x weaker** than non-LOW.
- LOW regime has low velocity BY DEFINITION
- Low velocity → low interaction score → filtered out by threshold
- 42.5% accuracy is a FUNDAMENTAL issue, not fixable by scoring

**Conclusion:** The new interaction-based score formula (v2) naturally excludes LOW regime signals without explicit filtering. This is correct behavior - these are weak signals with poor predictive power

---

## Implementation Plan

### Step 1: Fix fill simulation (Option A)

**File:** `research/spike_param_optimizer.py`
**Line:** 551-554

```python
def check_order_fill(order: Dict, current_bid: float, current_ask: float,
                     time_since_placed_ms: int) -> bool:
    """
    Check if a passive order would fill.

    More realistic than just checking ask <= price:
    - Order fills if we're at or above best bid
    - And market is active (tight spread)
    - And enough time has passed for execution
    """
    at_best_bid = order['price'] >= current_bid
    spread = current_ask - current_bid
    active_market = spread <= 0.03
    min_time_elapsed = time_since_placed_ms >= 500  # 500ms

    return at_best_bid and active_market and min_time_elapsed
```

### Step 2: Update simulation loop

**Line ~665-698:** Pass `current_bid` to fill check, track time since order placed

```python
# Inside the scan loop
for j in range(obs_idx, len(mdf)):
    scan_row = mdf.iloc[j]
    scan_ts = scan_row['timestamp_ms']

    if winner_side == "UP":
        current_ask = scan_row['up_ask']
        current_bid = scan_row['up_bid']
    else:
        current_ask = scan_row['down_ask']
        current_bid = scan_row['down_bid']

    for order in winner_orders:
        if order['filled']:
            continue

        time_since_placed = scan_ts - order['placed_at']

        # Check for pull (entry orders only)
        if check_order_pull(order, scan_ts, config, order_type="entry"):
            pulled = True
            break

        # Check for fill (NEW LOGIC)
        if check_order_fill(order, current_bid, current_ask, time_since_placed):
            order['filled'] = True
            order['fill_price'] = order['price']
            total_winner_filled += order['size']
            total_winner_cost += order['price'] * order['size']
```

### Step 3: Re-run optimizer with quick mode

```bash
python research/spike_param_optimizer.py --quick --path path1 --workers 4
python research/spike_param_optimizer.py --quick --path path2 --workers 4
```

Compare:
- Total trades (should increase significantly)
- Win rate / direction accuracy (should stay similar)
- $/hr (primary metric)

---

## Files to Modify

| File | Changes |
|------|---------|
| `research/spike_param_optimizer.py` | Fix `check_order_fill()` function, update simulation loop |

---

## Verification

1. **Quick mode sanity check:**
   - Trades should increase from ~12 to ~50+ per config
   - Pulled % should decrease from 99% to ~50-70%

2. **Results should show:**
   - More statistically significant sample sizes
   - Better signal-to-noise in optimization

3. **Full optimizer re-run** after quick validation passes

---

## UPDATE: January 20, 2026 - Taker Strategy Resolution

### Solution Implemented

Instead of fixing passive fill simulation, we **reverted to TAKER entry** logic:

**File:** `research/spike_param_optimizer_taker.py`

### Changes Made

1. **Entry Logic:** `winner_ask` (immediate fill) instead of `min(bid+0.01, ask-0.01)`
2. **Score Threshold:** `ENHANCED_SCORE_THRESHOLD` lowered from 0.02 → **0.005**
3. **Stop-Loss:** Focused on `[None, 0.12, 0.15]` - 12% is optimal
4. **Taker Fees:** Added fee calculation to PnL

### Focused Test Results (6 configs)

| Buycount | SL | $/hr Net | Trades | Win% | Acc% |
|----------|-----|----------|--------|------|------|
| 1 | OFF | $2.16 | 46 | 89.1% | 67.4% |
| **1** | **12%** | **$2.74** | 46 | 73.9% | 67.4% |
| 1 | 15% | $2.46 | 46 | 73.9% | 67.4% |
| 3 | OFF | $1.66 | 76 | 89.5% | 68.4% |
| 5 | OFF | $1.35 | 89 | 91.0% | 67.4% |

### Key Finding: 12% SL Beats No SL

For Buycount=1:
- SL OFF: $77.44 net (resolution losses: -$105)
- SL 12%: **$98.04 net** (SL losses: -$54, no resolution)
- Difference: **+$20.60 or +27%**

### Why Taker Instead of Fixing Passive

1. Passive fills only 3.2% of time (ask drops rare)
2. Taker guarantees entry on every signal
3. Taker fees (4.5%) < opportunity cost of missed signals
4. Simpler logic, no fill simulation needed

### Next Steps

Run full taker optimizer:
```bash
caffeinate -dims python research/spike_param_optimizer_taker.py --path path1 --workers 4 2>&1 | tee research/taker_path1_full.log
```

Expected configs: ~2,000 (reduced from 6,150 due to focused SL options)
