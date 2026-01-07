# Session Analysis Report - Jan 4-5, 2026

**Session**: 10:45 PM IST (Jan 4) to 06:42 AM IST (Jan 5)
**Strategy**: ACCUM (Calculus Maker)
**Net P&L**: ~-$10 (including manual trades)

---

## Executive Summary

### The Problem
Your live calc session lost ~$10 primarily due to **price chasing** in trending markets. The bot kept buying expensive shares (up to $0.96) instead of hedging the cheap side.

### The Solution
**Limit to 1 buy per side** would have turned a -$1.70 loss into a **+$24.80 profit** - a $26.50 improvement.

---

## Session Breakdown

| Category | Markets | Total P&L | Avg P&L |
|----------|---------|-----------|---------|
| Good (pair cost < $0.98) | 19 | +$14.60 | +$0.77 |
| OK ($0.98-$1.00) | 3 | +$0.50 | +$0.17 |
| Bad ($1.00-$1.05) | 5 | -$5.00 | -$1.00 |
| **Worst (>$1.05)** | **4** | **-$5.75** | **-$1.44** |

**Win Rate**: 61% (19/31 markets)
**Auto-Redeemer Activity**: $355+ recycled during session

---

## What-If Analysis Results

### Scenario 1: One Buy Per Side (WINNER)

```
Actual P&L:    -$1.70
Simulated:     +$24.80
Improvement:   +$26.50
```

**Why it works**:
- Early buys are cheap ($0.25-$0.45)
- Late buys are expensive ($0.70-$0.96)
- Limiting to 1 buy per side captures only cheap prices

**Risk reduction**:
- Variance: $1.19 → $0.47 (60% reduction)
- Max loss: -$2.90 → -$1.36 (53% reduction)

**Top 5 improved markets**:
| Market | Actual | Simulated | Delta |
|--------|--------|-----------|-------|
| 1767556800 | -$2.90 | +$0.69 | +$3.59 |
| 1767550500 | -$1.75 | +$1.05 | +$2.80 |
| 1767555900 | -$1.60 | +$1.11 | +$2.71 |
| 1767561300 | -$1.05 | +$1.38 | +$2.43 |
| 1767560400 | -$1.65 | +$0.58 | +$2.23 |

### Scenario 2: Target 30 Shares

```
Actual P&L:    -$1.70
Simulated:     -$1.70
Improvement:   $0.00
```

**Why no impact**: Most markets already end at 15/15 due to time constraints.

### Scenario 3: Cheap Side First

```
Actual P&L:    -$1.70
Simulated:     -$6.90
Improvement:   -$5.20 (WORSE)
```

**Why it failed**: In trending markets, you NEED to buy the expensive side to hedge. Avoiding expensive buys entirely leaves you unhedged and exposed to larger losses.

---

## Root Cause Deep Dive

### The 4 Worst Markets (All Had Price Chasing)

#### Market 1767560400 (21:15 UTC) - Lost $1.65
```
Pattern: UP trending → Bot chased UP
Early UP buys: $0.35, $0.28, $0.28
Late UP buys:  $0.62, $0.66, $0.70, $0.74, $0.78, $0.90, $0.96
Final UP avg:  $0.53 (should have been ~$0.30)
```

#### Market 1767564000 (22:15 UTC) - Lost $1.90 (WORST)
```
Pattern: UP trending → Bot chased UP → DOWN won
UP buys: $0.31, $0.41, $0.43... up to $0.95
Final pair cost: $1.13 (guaranteed loss)
```

#### Market 1767561300 (21:30 UTC) - Lost $1.05
```
Pattern: DOWN trending → Bot chased DOWN
DOWN buys: $0.50, $0.52... up to $0.86
Final DOWN avg: $0.85 (extremely expensive)
```

### Why Calculus Maker Exponential Decay Backfires

The current logic:
```
Early market (>10 min): Strict threshold, buy cheap only
Late market (<5 min):   Relaxed threshold, buy anything
```

The problem:
- In trending markets, late prices are the MOST expensive
- Relaxing the threshold causes chasing
- Should be the OPPOSITE: stricter late, not relaxed

---

## Recommendations

### Immediate Changes (High Impact)

1. **Limit buys per side**: Max 3 buys total, or 1 buy in first 5 minutes
2. **Hard price ceiling**: Never buy above $0.60 except for emergency hedge
3. **Invert decay logic**: Stricter thresholds late, not relaxed

### Medium-Term Changes

4. **Volume weighting**: Buy 3x at $0.30, 1x at $0.60
5. **Trend detection**: If price moved >20% in one direction, stop buying that side
6. **Earlier hedge trigger**: Start hedging at 5-share imbalance, not 10

### Monitoring Improvements

7. **Alert on expensive buys**: Notify when buying above $0.70
8. **Real-time pair cost tracking**: Stop market if pair cost exceeds $0.98

---

## Data Files Created

| File | Purpose |
|------|---------|
| `analysis/whatif_session_2026-01-04.json` | Raw data for simulations |
| `scripts/whatif_simulator.py` | Scenario runner |
| `analysis/session_analysis_2026-01-04.md` | This report |

### Running Simulations

```bash
# Run all scenarios
python scripts/whatif_simulator.py --session 2026-01-04 --scenario all

# Run specific scenario
python scripts/whatif_simulator.py --session 2026-01-04 --scenario one_buy_per_side
```

---

## Conclusion

The session loss was not due to bad luck or market randomness. It was caused by a systematic issue: **chasing expensive prices in trending markets**.

The fix is simple: **buy less, buy early, stop chasing**.

Implementing "one buy per side" would have turned a losing night into a winning one.
