# Calc Maker Velocity Simulation - 10 Hour Analysis

**Date:** January 9-10, 2026
**Duration:** ~10 hours (18:29 - 04:29 UTC)
**Source:** AWS EC2 velocity_sim_10h.log

---

## Executive Summary

| Metric | Value |
|--------|-------|
| Total Cycles | 425 attempted |
| Completed Cycles | 54 |
| Profitable Cycles | 47/54 (87.0%) |
| Avg Pair Cost | $0.9852 |
| Avg Profit/Cycle | $0.0148 |
| Total Profit | $0.80 |

**Key Finding:** Velocity timing provides **709 bps average improvement** (392 bps entry + 317 bps hedge).

---

## Velocity Threshold Analysis

The simulation recorded 2,688 velocity samples and analyzed what percentage exceeded different thresholds:

| Threshold (bps/sec) | Times Exceeded | Percentage | Notes |
|---------------------|----------------|------------|-------|
| 0.025 | 994 | 37.0% | Too sensitive - would trigger constantly |
| **0.050** | 848 | **31.5%** | **Current threshold** |
| 0.075 | 671 | 25.0% | Moderate - fewer triggers |
| 0.100 | 570 | 21.2% | Conservative - misses some reversals |
| 0.200 | 243 | 9.0% | Very conservative |
| 0.500 | 28 | 1.0% | Only major moves |

### Recommendation
**Keep 0.05 bps threshold** - it triggers ~31% of the time, which balances:
- Enough sensitivity to catch reversals (94.4% hedge triggers were reversals, not timeouts)
- Not too sensitive to cause constant pulling

---

## Velocity Timing Effectiveness

### Entry Phase
- **Average SKIPs before ENTER:** 26.3
- **Entry price improvement:** 392.59 bps avg
- Velocity reversal detection successfully identifies better entry points

### Hedge Phase
- **Average LET_RIDEs before HEDGE:** 9.4
- **Hedge price improvement:** 316.67 bps avg
- "Let it ride" strategy captures favorable price movement

### Hedge Trigger Distribution
| Trigger Type | Count | Percentage |
|--------------|-------|------------|
| Velocity Reversal | 51 | 94.4% |
| Force Timeout (120s) | 3 | 5.6% |

**Conclusion:** Velocity reversals trigger hedges 94% of the time - the "let it ride" strategy is working.

---

## Cycle Completion Issues

### High Abort Rate
- 425 cycles attempted
- Only 54 completed (12.7% completion rate)
- Most aborts due to **fill timeouts** (30s limit)

### Root Causes
1. **Entry fill timeout:** Market moved away before maker order filled
2. **Hedge fill timeout:** Cheap side liquidity insufficient
3. **Extreme price skew:** When UP=$0.99 or DOWN=$0.99, hard to get fills

### Sample Failed Cycles
```
18:32:13 | [ENTRY] Fill timeout after 30s, aborting cycle
18:33:35 | [ENTRY] Fill timeout after 30s, aborting cycle
18:40:28 | [ENTRY] Fill timeout after 30s, aborting cycle
```

---

## Velocity Statistics

| Metric | Value |
|--------|-------|
| Total Samples | 2,688 |
| Average |velocity|| 0.057 bps |
| Max |velocity|| 1.327 bps |
| Min |velocity|| 0.000 bps |

The average velocity (0.057 bps) is slightly above the threshold (0.05 bps), confirming the threshold is well-calibrated.

---

## Sample Successful Cycles

### Cycle 6 (Best Entry Improvement)
```
18:34:03 | [ENTRY] Reversal detected: vel=-0.133bps after 26 skips
18:34:19 | [ENTRY] Filled @ $0.440 in 15812ms
18:34:26 | [HEDGE] Filled @ $0.550 in 6849ms
18:34:26 | [CYCLE 6] Complete: pair=$0.990, profit=$0.010,
                     entry_improve=700.0bps, hedge_improve=200.0bps
```
- 26 SKIPs = waited for better price
- 700 bps entry improvement = $0.07 better than immediate entry

### Cycle 7 (Fast Completion)
```
18:34:53 | [ENTRY] Reversal detected: vel=0.104bps after 25 skips
18:35:05 | [ENTRY] Filled @ $0.490 in 12087ms
18:35:06 | [HEDGE] Filled @ $0.500 in 806ms
18:35:06 | [CYCLE 7] Complete: pair=$0.990, profit=$0.010,
                     entry_improve=300.0bps, hedge_improve=200.0bps
```
- Sub-second hedge fill after velocity reversal

---

## Unhedged Position Errors

Several cycles had hedge fill timeouts:
```
18:30:42 | [HEDGE] Fill timeout after 30s (unhedged 37s)
18:31:37 | [HEDGE] Fill timeout after 30s (unhedged 30s)
18:32:55 | [HEDGE] Fill timeout after 30s (unhedged 32s)
```

These represent **risk exposure** - entry filled but hedge didn't complete.

---

## Recommendations

### 1. Lower Velocity Threshold to 0.04 bps
- Would trigger 35-40% of the time
- More aggressive entry but still filtered

### 2. Increase Fill Timeout to 60s
- Current 30s timeout causes many aborts
- Longer patience = more completions

### 3. Tighter Entry Offset
- Current: best_bid - 0.01
- Try: best_bid - 0.005 for faster fills

### 4. Emergency Taker for Hedge
- If hedge doesn't fill in 45s, switch to taker
- Accept slightly worse price to avoid unhedged exposure

---

## Files Generated

- **Log:** `logs/velocity_sim_10h.log` (169KB)
- **CSV files:** Not written (script disconnected before save)

---

## Conclusion

The 10-hour simulation validates that **velocity-based timing works**:

1. **Entry timing saves 392 bps** on average
2. **Hedge timing saves 317 bps** on average
3. **0.05 bps threshold is appropriate** - triggers 31% of samples
4. **94% of hedges triggered by reversal** (not timeout)

**Main issue:** Low completion rate (12.7%) due to fill timeouts. Need to either:
- Increase timeout duration
- Use tighter offsets for faster fills
- Add taker fallback for hedges

*Generated from velocity_sim_10h.log - January 10, 2026*
