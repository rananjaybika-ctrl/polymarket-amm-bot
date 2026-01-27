# Volume Strategy Grid Search - Deep Parameter Analysis

**Date:** January 27, 2026
**Purpose:** Document parameter understanding and pruning decisions for volume strategy grid search

---

## Overview

Starting with 10 parameters (23,040 configs), pruned down to 7 parameters (720 configs) - **97% reduction**.

---

## Parameters Analyzed

### 1. `time_stop` [20, 40, 60, 80, 120] - **KEEP ALL 5**

**What it does:** Max seconds to wait for passive hedge fill before taker exit.

**Key insight:** This is the CORE volume question.
- **20s:** Exit fast → more cycles → more fees (taker exits)
- **120s:** Wait longer → fewer cycles → more passive fills

**Decision:** KEEP ALL 5 - This is the main thing we're testing.

---

### 2. `lookback_ms` [1200, 1600, 2000] - **PRUNED from 4 to 3**

**What it does:** Window for spike detection.

**Key insight:** Longer = more spikes (more time for price to move 0.02%).

| Lookback | Ticks | Spikes/hr |
|----------|-------|-----------|
| 1200ms | 72 | ~1000 |
| 1400ms | 84 | ~1050 |
| 1600ms | 96 | ~1100 |
| 2000ms | 120 | ~1200 |

**Decision:** PRUNED 1400ms - too close to 1200ms, redundant.

---

### 3. `drop_intercept` [0.04, 0.06, 0.08] - **KEEP ALL 3**

**What it does:** Base expected price drop for hedge bid.

```
loser_bid = loser_ask - (0.50 * magnitude + DROP_INTERCEPT)
```

| Value | Effect |
|-------|--------|
| 0.04 | Higher bid → faster fills → less profit/fill |
| 0.08 | Lower bid → slower fills → more profit/fill |

**Interaction with time_stop:**
- Short time_stop (20s) + high drop_intercept (0.08) = Potential bad combo
- Long time_stop (120s) + low drop_intercept (0.04) = Potential wasteful

**Decision:** KEEP ALL 3 - let data decide which combos work.

---

### 4. `target_pair_cost` [0.98, 0.99] - **PRUNED from 3 to 2**

**What it does:** Maximum allowed pair_cost before taking profit.

```
pair_cost = winner_entry + loser_fill
profit = (1.00 - pair_cost) * shares
```

| Value | Meaning |
|-------|---------|
| 0.97 | Take profit at $0.03/share (very aggressive) |
| 0.98 | Take profit at $0.02/share (moderate) |
| 0.99 | Take profit at $0.01/share (patient) |

**Decision:** PRUNED 0.97 - too aggressive, likely bad performance.

---

### 5. `cycling` [True, False] - **NEW, KEEP BOTH**

**What it does:** Re-enter after hedge completes or stop.

| Value | Behavior |
|-------|----------|
| True | Re-enter on next spike after hedge |
| False | One trade per market, ride to resolution |

**Previous validation:** True was better (more trades).

**Why test False?** With very short time_stop, maybe quality > quantity.

**Decision:** KEEP BOTH - test if re-entry helps with short time-stops.

---

### 6. `threshold_method` ["regime", "fixed"] - **KEEP BOTH**

**What it does:** How to set spike detection threshold.

| Method | LOW vol | MEDIUM vol | HIGH vol |
|--------|---------|------------|----------|
| regime | 0.01% | 0.02% | 0.035% |
| fixed | 0.02% | 0.02% | 0.02% |

**Key insight:**
- "regime" detects MORE spikes in LOW vol (lower threshold)
- "fixed" detects FEWER spikes in LOW vol (harder to reach 0.02%)

**Decision:** KEEP BOTH - compare if adaptive threshold helps.

---

### 7. `velocity_mode` ["all", "none"] - **NEW, KEEP BOTH**

**What it does:** How to use velocity confirmation.

| Mode | Behavior |
|------|----------|
| all | Reject only CONTRADICTING velocity |
| none | No velocity filter |

**Previous validation:** "all" was best, stricter modes were worse.

**Why test "none"?** With short time_stop, velocity might matter less.

**Decision:** KEEP BOTH - test if filter still helps with fast cycling.

---

## Parameters Fixed at Validated Values

### 8. `z_score_zone` = (0.0, 1.5) - **FIXED**

**What it does:** Filter trades by volatility level.

**Previous validation:** (0, 1.5) was BEST.

| Zone | Trades when |
|------|-------------|
| (None, None) | Any volatility (no filter) |
| (0, 1.5) | Average to moderately high vol |
| (-0.5, 1.5) | Slightly below avg to moderately high |
| (0, 2.0) | Average to very high vol |

**Decision:** FIXED at (0, 1.5) - already validated, no need to re-test.

---

### 9. `zscore_method` = "ewma" - **FIXED**

**What it does:** How to compute z-score.

| Method | Pros | Cons |
|--------|------|------|
| ewma | Adapts to current regime | Slower to react |
| ou | Consistent, pre-calibrated | Can drift if market changes |

**Decision:** FIXED at "ewma" - no drift risk, validated.

---

### 10. `skip_low_regime` = True - **FIXED**

**What it does:** Skip ALL spikes in LOW volatility regime.

**Previous validation:** True was better (LOW regime = 48% accuracy = trash).

**Decision:** FIXED at True - 48% accuracy is always trash.

---

## Final Grid Summary

### Parameters Varied (7):

| Parameter | Values | Count | Rationale |
|-----------|--------|-------|-----------|
| time_stop | [20,40,60,80,120] | 5 | Core question |
| lookback_ms | [1200,1600,2000] | 3 | Dropped 1400 |
| drop_intercept | [0.04,0.06,0.08] | 3 | Test fill speed |
| target_pair | [0.98,0.99] | 2 | Dropped 0.97 |
| cycling | [True,False] | 2 | Test re-entry |
| threshold_method | [regime,fixed] | 2 | Test adaptive |
| velocity_mode | [all,none] | 2 | Test filter |

### Parameters Fixed (3):

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| z_score_zone | (0,1.5) | Validated best |
| zscore_method | ewma | No drift |
| skip_low_regime | True | 48% = trash |

### Total Configurations:

**5 × 3 × 3 × 2 × 2 × 2 × 2 = 720 configs**

Down from 23,040 unpruned → **97% reduction**

---

## Potential Bad Combinations to Watch

After grid search, verify these hypotheses:

1. **time_stop=20 + drop_intercept=0.08:** Short wait + conservative bid = constant time-stop exits
2. **time_stop=120 + drop_intercept=0.04:** Long wait + aggressive bid = wasteful (could get better price)
3. **cycling=False + time_stop=20:** One trade + short window = very few trades

---

## Key Questions This Grid Will Answer

1. How short can time_stop go while remaining profitable?
2. Does longer lookback (more spikes) help or hurt?
3. Is aggressive hedging (low drop_intercept) better for volume?
4. Does cycling still help with short time-stops?
5. Does velocity filter matter for fast cycling?
6. Is adaptive threshold (regime) still better than fixed?

---

*Analysis completed: January 27, 2026*
