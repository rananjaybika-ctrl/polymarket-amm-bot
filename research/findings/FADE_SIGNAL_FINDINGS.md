# FADE Signal Findings (Feb 6, 2026)

## Executive Summary

**Signal:** Raw EWMA spike + buy expensive_side when it stays high
**Filter:** `expensive_ask >= $0.80 AND NOT(spike=DOWN AND velocity<0)`
**Result:** 94.7% FADE accuracy, $0.026 EV/share

---

## The FADE Strategy

When BTC spikes, Polymarket sometimes doesn't react (expensive_side stays high).
This means the spike is **noise**, not real information.
**FADE = buy the expensive side, bet it wins at resolution.**

---

## Optimal Signal Filter

### Primary Filter
```python
def is_valid_fade_signal(spike_dir, velocity_bps, expensive_ask):
    # Threshold
    if expensive_ask < 0.80:
        return False

    # Exclude confirmed DOWN spikes (spike is real, not noise)
    if spike_dir == 'DOWN' and velocity_bps < 0:
        return False

    return True
```

### Why This Works

| Spike | Velocity | Meaning | FADE Acc |
|-------|----------|---------|----------|
| UP | > 0 | BTC up, spike up | 93.5% ✅ |
| UP | < 0 | BTC down, spike up (noise) | 95.7% ✅ |
| DOWN | > 0 | BTC up, spike down (noise) | 95.7% ✅ |
| DOWN | < 0 | BTC down, spike down (REAL) | 79.7% ❌ SKIP |

**Skip when spike=DOWN is confirmed by velocity<0** = spike is real, FADE loses.

---

## Threshold Analysis

With velocity filter `NOT(DOWN+vel<0)`:

| Threshold | Signals | FADE Acc | Avg Entry | EV/share |
|-----------|---------|----------|-----------|----------|
| $0.70 | 209 | 89.5% | ~$0.82 | $0.006 |
| $0.75 | 191 | 91.6% | ~$0.85 | $0.011 |
| **$0.80** | **171** | **94.7%** | **~$0.88** | **$0.026** |
| $0.85 | 142 | 95.8% | ~$0.91 | $0.016 |
| $0.90 | 111 | 98.2% | ~$0.95 | $0.021 |

**$0.80 is optimal:** Best EV/share because it balances accuracy with profit margin.

---

## Validation Results

| Dataset | Period | Signals | FADE Acc | Signals/Hour |
|---------|--------|---------|----------|--------------|
| IS+OOS2 | Jan 16-19 | 69 | 97.1% | 4.3 |
| OOS7 | Jan 29-30 | 73 | 94.5% | 12.8 |
| **Combined** | | **142** | **95.8%** | **6.5** |

Works in both low volatility (IS+OOS2) and high volatility (OOS7) periods.

---

## What Doesn't Matter

### OBI (Order Book Imbalance) - MEANINGLESS

OBI appeared predictive but was confounded by expensive_ask:
- OBI correlates with expensive_ask (r = 0.318)
- Within $0.80+ threshold: OBI_FOLLOW = 92.1%, OBI_INVERSE = 90.9% (no difference)
- **Drop OBI from filter**

### AGGRESSIVE Filters (velocity, score) - COUNTERPRODUCTIVE

Original AGGRESSIVE filters were designed to predict REAL moves.
For FADE, we want NOISE, so these filters hurt:
- RAW spikes: 83.7% FADE accuracy
- FILTERED spikes: 80.7% FADE accuracy (-3pp)

---

## Features That Help

| Feature | Good Condition | FADE Acc Improvement |
|---------|---------------|---------------------|
| expensive_ask | >= $0.80 | +6.7pp |
| velocity filter | NOT(DOWN+vel<0) | +5pp |
| exp_depth | > 5000 (OOS7 only) | +3pp |
| signal_quality | > 0.4 | +4.7pp |
| spike_vs_velocity | SPIKE_ONLY | +12pp (rare) |

---

## Price Evolution After Spike

After spike, both sides pull back:

| Window | Expensive Drops | Spike Drops | Pair Cost | % Profitable |
|--------|-----------------|-------------|-----------|--------------|
| 5s | 3.6c (78%) | 0.0c (9%) | $0.974 | 65% |
| 15s | 5.0c (82%) | 0.7c (28%) | $0.953 | 87% |
| 30s | 7.1c (87%) | 1.6c (46%) | $0.923 | 95% |
| 60s | 9.9c (90%) | 2.9c (60%) | $0.881 | 99% |

**Key insight:** Expensive side drops first (MAKER entry), spike side drops later (hedge opportunity).

---

## Signal Frequency

- Low volatility (IS+OOS2): ~4 signals/hour
- High volatility (OOS7): ~13 signals/hour
- Average: ~6.5 signals/hour
- Multi-cycle: avg 5.9 signals per market

---

## Next Steps

1. Determine execution strategy:
   - Hold to resolution vs spread capture
   - Entry offset optimization
   - Hedge timing and sizing

2. Build grid search with optimal filter

3. Validate on OOS8/OOS9

---

## Execution Strategy Analysis

### Entry Offset Comparison

| Entry Offset | Fill Rate | Total PnL | Per Trade |
|--------------|-----------|-----------|-----------|
| -0c (at ask) | 100% | $11.41 | $0.127 |
| -1c | 82% | $13.00 | $0.176 |
| -2c | 70% | $13.45 | $0.213 |
| **-3c** | **58%** | **$16.20** | **$0.312** |

**Optimal: -3c offset** (or dynamic based on expensive_ask level)

### Stop Loss Analysis

| Stop Loss | Stops Triggered | Stopped FADE Acc | Total PnL |
|-----------|-----------------|------------------|-----------|
| None | 0% | N/A | **$16.20** |
| 10% | 29% | 80% | $12.03 |
| 15% | 15% | 88% | $8.90 |
| 20% | 10% | 80% | $12.66 |

**STOP LOSSES DESTROY VALUE because:**
- Stopped trades have 80-87% FADE accuracy
- We're stopping out of WINNING trades
- Temporary drops are noise, not signal
- Plus 2% TAKER fee on stop exit

### Resolution Hold vs Spread Capture

| Strategy | Total PnL | Per Trade | Notes |
|----------|-----------|-----------|-------|
| **Resolution Hold** | **$16.20** | **$0.312** | Simple, optimal |
| Spread Capture | $8-10 | $0.15-0.20 | Complex, lower EV |
| Spread + Fallback | $10-12 | $0.20-0.25 | Hybrid |

**Resolution hold is better because:**
- 94% FADE accuracy → high expected value
- Resolution profit ($0.14/share) > Spread ($0.06/share)
- Spread capture adds complexity without value

### Final Execution Recommendation

```python
# SIGNAL FILTER
def is_valid_fade_signal(spike_dir, velocity_bps, expensive_ask):
    if expensive_ask < 0.80:
        return False
    if spike_dir == 'DOWN' and velocity_bps < 0:
        return False
    return True

# ENTRY
# MAKER at best_ask - offset
# Dynamic offset:
#   $0.80-$0.85: -3c
#   $0.85-$0.90: -2c
#   $0.90+: -1c

# EXIT
# HOLD TO RESOLUTION
# NO STOP LOSS
# NO HEDGE
```

### Expected Performance

- **FADE accuracy:** 94-95%
- **Entry:** ~$0.86 avg (with -3c offset)
- **Win profit:** ~$0.70/trade (5 shares)
- **Loss:** ~$4.30/trade (5 shares)
- **Net EV:** ~$0.31/trade

---

*Created: Feb 6, 2026*
*Updated: Feb 6, 2026 (added execution analysis)*
*Datasets: IS+OOS2 (Jan 16-19), OOS7 (Jan 29-30)*
