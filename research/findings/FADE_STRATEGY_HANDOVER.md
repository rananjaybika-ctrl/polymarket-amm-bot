# FADE Strategy Handover (Feb 6, 2026)

## Executive Summary

We discovered and validated a **FADE strategy** for Polymarket BTC markets:
- **Buy expensive_side when BTC spikes but Polymarket doesn't react**
- **94.7% FADE accuracy** with proper filters
- **$0.31/trade EV** (5 shares)
- **Hold to resolution, NO stop loss, NO hedge**

---

## Key Discovery: FADE the Spike

### What is FADE?
When BTC price spikes (detected by EWMA), sometimes Polymarket's expensive_side stays high.
This means the spike is **NOISE**, not real information.
**FADE = buy expensive_side, bet it wins at resolution.**

### Why It Works
- Polymarket LAGS BTC price movements
- When expensive_ask stays >= $0.80 despite spike, market has "priced in" the noise
- 94-97% of these signals resolve in favor of expensive_side

---

## Optimal Signal Filter

```python
def is_valid_fade_signal(spike_dir, velocity_bps, expensive_ask, time_remaining):
    # Threshold
    if expensive_ask < 0.80:
        return False

    # Min time (avoid manipulation zone)
    if time_remaining < 90:
        return False

    # Exclude confirmed DOWN spikes
    # When spike=DOWN AND velocity<0, spike is REAL (not noise)
    if spike_dir == 'DOWN' and velocity_bps < 0:
        return False

    return True
```

### Filter Explanation

| Spike | Velocity | Meaning | Keep? | FADE Acc |
|-------|----------|---------|-------|----------|
| UP | > 0 | BTC up, spike up | ✅ | 93.5% |
| UP | < 0 | BTC down, spike up (noise) | ✅ | 95.7% |
| DOWN | > 0 | BTC up, spike down (noise) | ✅ | 95.7% |
| DOWN | < 0 | BTC down, spike down (REAL) | ❌ | 79.7% |

**Skip when spike=DOWN is confirmed by velocity<0** = spike is real, FADE loses.

---

## Threshold Analysis

| Threshold | Signals | FADE Acc | EV/share |
|-----------|---------|----------|----------|
| $0.70 | 209 | 89.5% | $0.006 |
| $0.75 | 191 | 91.6% | $0.011 |
| **$0.80** | **171** | **94.7%** | **$0.026** |
| $0.85 | 142 | 95.8% | $0.016 |
| $0.90 | 111 | 98.2% | $0.021 |

**$0.80 is optimal**: Best EV/share (balances accuracy with profit margin)

---

## Execution Strategy

### Entry
- **MAKER order at best_ask - offset**
- Optimal offset: **-2c to -3c** from expensive_ask
- Dynamic: -3c for $0.80-0.85, -2c for $0.85-0.90, -1c for $0.90+

| Entry Offset | Fill Rate | Total PnL | Per Trade |
|--------------|-----------|-----------|-----------|
| -0c (at ask) | 100% | $11.41 | $0.127 |
| -2c | 70% | $13.45 | $0.213 |
| **-3c** | **58%** | **$16.20** | **$0.312** |

### Exit: HOLD TO RESOLUTION

**NO STOP LOSS** - Stop losses destroy value:

| Stop | Stopped Trades FADE Acc | Impact |
|------|-------------------------|--------|
| 10% | 80% | Stops WINNING trades |
| 15% | 88% | Stops WINNING trades |
| 20% | 80% | Stops WINNING trades |

When expensive_ask drops (triggering stop), it's temporary noise.
FADE still wins 80-88% of the time. Stops lock in losses on winners.

### Exit: NO HEDGE (Spread Capture)

| Strategy | PnL/Trade | Why |
|----------|-----------|-----|
| **Resolution Hold** | **$0.312** | Optimal |
| Spread Capture | $0.15-0.20 | Spread too small ($0.06) |

Resolution profit ($0.14/share) > Spread capture ($0.06/share)
With 94% accuracy, holding to resolution maximizes value.

---

## What Doesn't Matter (Drop These)

### OBI (Order Book Imbalance) - MEANINGLESS
- OBI correlates with expensive_ask (r = 0.318)
- Within $0.80+ threshold: OBI adds 0pp improvement
- **Drop OBI_FOLLOW and OBI_INVERSE from filter**

### AGGRESSIVE Filters (velocity threshold, enhanced_score) - COUNTERPRODUCTIVE
- These were designed to predict REAL moves
- For FADE, we want NOISE, so they hurt accuracy
- RAW spikes: 83.7% FADE vs FILTERED: 80.7% FADE

### Depth Filter
- Only available in OOS7, not IS+OOS2
- Not consistently available, drop from required filter

---

## Validation Results

| Dataset | Period | Hours | Signals | FADE Acc | Signals/Hour |
|---------|--------|-------|---------|----------|--------------|
| IS+OOS2 | Jan 16-19 | 16.2 | 69 | 97.1% | 4.3 |
| OOS7 | Jan 29-30 | 5.7 | 73 | 94.5% | 12.8 |
| **Combined** | | **21.9** | **142** | **95.8%** | **6.5** |

Works in both low volatility (IS+OOS2) and high volatility (OOS7) periods.

---

## Time Remaining Analysis

Current data has **min_time = 90s** (no signals in last 90s).

| Min Time | Signals | FADE Acc |
|----------|---------|----------|
| >= 90s | 171 | 94.7% |
| >= 180s | 140 | 95.0% |
| >= 300s | 96 | 96.9% |

**Keep min_time = 90s** - already avoids manipulation zone.

---

## Price Evolution After Spike

| Window | Expensive Drops | Pair Cost | Hedge Rate |
|--------|-----------------|-----------|------------|
| 15s | 5.0c (82%) | $0.953 | 87% |
| 30s | 7.1c (87%) | $0.923 | 95% |
| 60s | 9.9c (90%) | $0.881 | 99% |

Expensive side drops first (MAKER entry opportunity).

---

## Final Strategy Specification

```
SIGNAL:
  - EWMA spike detected (1000ms halflife, OU adaptive threshold)
  - expensive_ask >= $0.80
  - NOT(spike_direction == 'DOWN' AND velocity_bps < 0)
  - time_remaining >= 90s
  - 10s cooldown per (market, direction)

ENTRY:
  - MAKER order at expensive_ask - 2c (or dynamic offset)
  - 0% fee (maker)

EXIT:
  - HOLD TO RESOLUTION
  - NO stop loss
  - NO hedge

EXPECTED PERFORMANCE:
  - FADE accuracy: 94-95%
  - Signals/hour: 4-13 (varies with volatility)
  - EV/trade: ~$0.31 (5 shares)
  - Win profit: ~$0.70/trade
  - Loss: ~$4.30/trade (5.6% of trades)
```

---

## Files Created/Updated

1. **`research/findings/FADE_SIGNAL_FINDINGS.md`** - Full findings document
2. **`research/findings/FADE_STRATEGY_HANDOVER.md`** - This handover document

## Files to Reference

- `research/findings/data/aggressive_m_v2_ewma_study_results.csv` - 108K raw signals
- `research/backtests/aggressive_m_v2_grid_search.py` - Grid search script (needs update)
- `research/reference/TRADING_CONFIGS.py` - Config source of truth

---

## Next Tasks

### 1. Build Grid Search with FADE Filter
Update `aggressive_m_v2_grid_search.py` to use:
- New filter: $0.80 + NOT(DOWN+vel<0) + min_time 90s
- Entry offsets: [-1c, -2c, -3c, dynamic]
- NO stop loss
- Resolution hold exit

### 2. Validate on OOS8/OOS9
Run the strategy on holdout datasets to confirm performance.

### 3. Implement Live Strategy
- Update `TRADING_CONFIGS.py` with FADE parameters
- Implement in live paper trading

### 4. Monitor and Iterate
- Track actual vs expected performance
- Adjust threshold if accuracy differs

---

## Key Insights to Remember

1. **FADE = buy expensive side when spike is noise**
2. **$0.80 threshold is optimal** (best EV/share)
3. **NOT(DOWN+vel<0) filter** removes confirmed spikes
4. **NO STOP LOSS** - stops exit winning trades (80-88% FADE accuracy on stopped trades)
5. **NO HEDGE** - resolution EV > spread capture EV
6. **OBI is meaningless** - just correlates with expensive_ask
7. **AGGRESSIVE filters hurt FADE** - designed for opposite purpose
8. **min_time 90s** - already in data, avoids manipulation

---

*Created: Feb 6, 2026*
*For: Next conversation continuation*
