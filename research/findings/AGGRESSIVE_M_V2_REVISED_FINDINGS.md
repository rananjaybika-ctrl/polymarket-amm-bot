# AGGRESSIVE_M V2 - Revised Findings (Feb 6, 2026)

## Executive Summary

**FADE strategy works, but AGGRESSIVE filters are counterproductive.**

The key insight: expensive_ask threshold alone predicts FADE success. Our velocity/score filters were designed to predict REAL moves, so they actually hurt FADE accuracy.

---

## Key Finding: Filters Hurt FADE Accuracy

### IS+OOS2 (Jan 16-19)

| Signal Type | Count | FADE Accuracy |
|-------------|-------|---------------|
| **RAW spikes** (spike + $0.70) | 209 | **83.7%** |
| FILTERED (+ velocity + score) | 135 | 80.7% ← WORSE |

**AGGRESSIVE filters reduce accuracy by 3pp while reducing signal count 35%.**

---

## expensive_ask Threshold is the Primary Driver

### IS+OOS2 Raw Spikes

| Threshold | Signals | FADE Accuracy |
|-----------|---------|---------------|
| >= $0.70 | 209 | 83.7% |
| >= $0.75 | 177 | 87.6% |
| >= $0.80 | 149 | 92.6% |
| >= $0.85 | 120 | 96.7% |
| >= $0.90 | 101 | **98.0%** |

### OOS7 Raw Spikes (Jan 29-30)

| Threshold | Signals | FADE Accuracy |
|-----------|---------|---------------|
| >= $0.60 | 1061 | 85.2% |
| >= $0.65 | 916 | 88.4% |
| >= $0.70 | 806 | 90.7% |
| >= $0.75 | 706 | 92.8% |
| >= $0.80 | 633 | **94.5%** |

**Conclusion: Higher threshold = higher accuracy. No filters needed.**

---

## Adverse Selection: Filled vs All Signals

When expensive_ask DROPS to our bid (MAKER fill), accuracy drops:

### OOS7 @ $0.65 Threshold

| Filter | All Signals | Filled Only | Drop |
|--------|-------------|-------------|------|
| No OBI | 88.4% | ~84% | -4pp |
| OBI_INVERSE | 90.0% | 85.6% | -4pp |
| OBI_FOLLOW | 87.7% | 85.3% | -2pp |

### OOS7 @ $0.70 Threshold

| Filter | All Signals | Filled Only | Drop |
|--------|-------------|-------------|------|
| No OBI | 90.7% | ~87% | -4pp |
| OBI_INVERSE | 91.7% | 87.1% | -5pp |
| OBI_FOLLOW | 90.2% | 88.1% | -2pp |

**Insight**: When we get a MAKER fill (ask drops), the spike was more likely real. This is adverse selection - the fills we GET are the ones where FADE is less likely to work.

---

## OBI Analysis

### Does OBI Help?

| OBI Filter | Signals | FADE Accuracy |
|------------|---------|---------------|
| OBI_FOLLOW (>0) | 724 | 84.5% |
| OBI_INVERSE (<0) | 337 | 86.6% |

**OBI_INVERSE adds ~2pp but reduces signals by 53%.**

### OBI + Threshold (OOS7)

| Threshold | OBI_INVERSE All | OBI_INVERSE Filled |
|-----------|-----------------|-------------------|
| $0.65 | 90.0% (280) | 85.6% (181) |
| $0.70 | 91.7% (242) | 87.1% (147) |

| Threshold | OBI_FOLLOW All | OBI_FOLLOW Filled |
|-----------|----------------|-------------------|
| $0.65 | 87.7% (636) | 85.3% (510) |
| $0.70 | 90.2% (564) | 88.1% (438) |

**At filled level, OBI_FOLLOW actually performs better at $0.70 (88.1% vs 87.1%).**

---

## The Strategy Problem

We want signals where:
1. **Spike happens** → expensive_side pulls back momentarily (we get MAKER fill)
2. **Spike is noise** → expensive_side WINS at resolution

But:
- When expensive_ask STAYS HIGH → spike is noise → FADE wins 90%+ (but no fill!)
- When expensive_ask DROPS → spike is real → FADE wins ~85% (we get fill)

**The fills we get are the ones where FADE is least likely to work.**

---

## Revised Strategy Options

### Option 1: Higher Threshold, Accept Lower Fill Rate
- Use $0.80+ threshold
- 94%+ FADE accuracy at signal level
- ~90%+ when filled
- Fewer signals but higher quality

### Option 2: TAKER Entry at Signal
- Buy AT expensive_ask (taker) when signal fires
- 90%+ accuracy (no adverse selection from waiting)
- Pay 2% taker fee
- EV: 90% * $0.90 - 10% * $0.90 - 2% fee = $0.72 - $0.02 = $0.70/share

### Option 3: Hybrid
- Post MAKER order at 1c below ask
- If no fill in 5s, take at market
- Captures ~70% of fills at 0% fee, rest at 2%

---

## CRITICAL: Spread Capture vs Resolution

### The Discovery (OOS7 Analysis)

Instead of holding to resolution, capture spread by hedging both sides:

1. Fill expensive_side at pullback (MAKER)
2. Fill spike_side when it also pulls back (MAKER)
3. Profit = $1.00 - pair_cost

| Strategy | Trades | Total PnL | Per Trade |
|----------|--------|-----------|-----------|
| Pure Resolution (FADE) | 754 | $107.87 | $0.143 |
| Spread Capture + Fallback | 754 | $344.34 | $0.457 |
| **Pure Spread Capture** | 754 | $458.58 | **$0.608** |

**Spread capture is 4x better than resolution betting!**

### Why This Works

- **91.5% of filled signals** achieve pair_cost < $1.00 (profitable)
- **Avg profit: $0.136** when both sides fill
- Only **8.5% fail** to complete hedge
- Failed hedges have **44% FADE accuracy** (spike was real - cut losses)

### The Pattern

After spike:
1. expensive_side pulls back momentarily (79% drop >= 1c)
2. spike_side ALSO eventually pulls back (we hedge)
3. Both sides fill below $1.00 combined

We're not predicting resolution - we're capturing mean reversion on BOTH sides.

---

## Recommended Grid Search (SPREAD CAPTURE)

| Parameter | Values | Rationale |
|-----------|--------|-----------|
| expensive_ask threshold | [0.65, 0.70, 0.75] | Higher = fewer signals but cleaner |
| entry_offset | [1c, 2c] | Offset below expensive_ask |
| hedge_timeout | [15s, 30s, 60s] | How long to wait for hedge |
| stop_loss | [2c, 3c, 5c] | Cut if no hedge and price moves against |

**Key changes from V1:**
- NO resolution betting
- NO AGGRESSIVE filters (velocity, score)
- BOTH sides must fill for profit
- Cut losses quickly if hedge doesn't fill

---

## Files

- Study data: `research/findings/data/aggressive_m_v2_ewma_study_results.csv`
- This analysis: Based on IS+OOS2 and OOS7 datasets
- Grid search: `research/backtests/aggressive_m_v2_grid_search.py` (needs rewrite for spread capture)

---

*Updated: Feb 6, 2026*
*Major revision: Spread capture 4x better than resolution*
