# Gabagool Merge & Net Exposure Analysis

**Date:** 2026-01-29
**Data Period:** Jan 29, 2026 (05:59 - 06:41 AM ET)

---

## Key Findings

### 1. Merge Behavior
- **Total merges (BTC 15m):** 32 operations
- **Merge frequency:** 14% of markets (4/28)
- **All merges occurred in ONE 37-minute window** (6:00-6:37 AM ET)
- **This is a batch cleanup, not constant merging**

### 2. Net Dollar Exposure Variation

| Metric | Value |
|--------|-------|
| **Gross position range** | 456 - 2,419 shares |
| **Net exposure range** | 147 - 1,540 shares |
| **Markets left (merged out)** | 4/28 (14%) |
| **Markets stayed (held to resolution)** | 24/28 (86%) |

### 3. When They Leave (Merge Out)

| Market | Time | Merged | Merge Timing |
|--------|------|--------|--------------|
| btc-updown-15m-1769686200 | 06:30 | $2,148 | 464s before end |
| btc-updown-15m-1769685300 | 06:15 | $4,246 | 24s before end |
| btc-updown-15m-1769684400 | 06:00 | $4,989 | AFTER end |
| btc-updown-15m-1769683500 | 05:45 | $108,804 | AFTER end |

### 4. Why They Leave

**Pattern: Time-of-day based**
- Hour 6 (6 AM ET): 100% merge rate
- All other hours: 0% merge rate

**Hypothesis:**
- Gabagool winds down positions at end of trading session (6 AM)
- Not a mid-market risk management technique
- Batch cleanup before stopping for the day

### 5. Position Sizing Pattern

| Condition | Avg Gross Position |
|-----------|-------------------|
| Left market | 1,073 shares |
| Stayed in market | 2,419 shares |

**Smaller positions more likely to be merged out**

---

## Comparison: Gabagool vs Baguette

| Behavior | GABAGOOL | BAGUETTE |
|----------|----------|----------|
| Merges during market | No (batch at end) | No |
| Sells on orderbook | No | Yes (spread capture) |
| Hold to resolution | 86% of markets | ~50% |
| Exit strategy | Batch MERGE at session end | Spread capture + directional |
| REDEEM after resolution | Minimal (dust) | Heavy (batch) |

---

## Implications for AS/Grid Strategy

1. **Gabagool is NOT using merges for risk management**
   - Merges are session cleanup, not mid-market exits
   - Holds positions through resolution in most cases

2. **Net exposure varies significantly (147-1,540 shares)**
   - Not running perfectly hedged
   - Accepts directional risk

3. **Session-based trading**
   - Gabagool appears to have trading windows
   - Winds down at 6 AM ET with batch merges
   - This is different from 24/7 AS strategy

4. **High frequency, high volume**
   - 50K activities in 42 minutes
   - Heavy maker flow
   - Likely earning rebates on volume

---

*Analysis based on Polymarket API data, Jan 29 2026*
