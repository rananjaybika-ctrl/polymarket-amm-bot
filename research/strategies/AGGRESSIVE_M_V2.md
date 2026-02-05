# AGGRESSIVE_M (V2) Strategy

**Status:** VALIDATED - Ready for grid search
**Date:** February 6, 2026 (Updated with deduplication)
**Replaces:** AGGRESSIVE (taker-based, DEPRECATED)

> **Evolution:** AGGRESSIVE_M V1 showed maker entry has adverse selection when FOLLOWING spikes. V2 **FADES** the spike instead when expensive_side >= $0.70.

---

## Executive Summary

AGGRESSIVE_M (V2) fades BTC spikes when the market doesn't believe them. When a spike is detected but the expensive_side (opposite to spike) remains expensive (>= $0.70), the spike is likely noise. We buy the expensive_side as a MAKER (0% fee) and hold to resolution.

**Key Insight:** When AGGRESSIVE filters detect a spike but Polymarket doesn't react (expensive_side stays expensive), the market is right 90% of the time.

---

## Validated Performance (With Proper Deduplication)

⚠️ **IMPORTANT:** All results use 10s cooldown deduplication per (market, direction) to reflect realistic trading capacity. Without dedup, signal counts are inflated ~80x.

### OBI Filter Comparison (10s cooldown, expensive_ask >= $0.70)

| OBI Strategy | Signals | FADE Accuracy | Avg Entry | $/trade | Total $ |
|--------------|---------|---------------|-----------|---------|---------|
| NO_OBI | 653 | 88.4% | $0.878 | $0.03 | $18 |
| **OBI_FOLLOW** | **424** | **90.1%** | $0.881 | **$0.10** | **$42** |
| OBI_FADE | 161 | 88.2% | $0.854 | $0.14 | $22 |

**Winner: OBI_FOLLOW** - Best balance of accuracy (90.1%) and signal volume (424)

### By Dataset (OBI_FOLLOW, 10s cooldown, >= $0.70)

| Dataset | Period | Signals | FADE Accuracy |
|---------|--------|---------|---------------|
| IS+OOS2 | Jan 16-19 | ~150 | ~90% |
| OOS7 | Jan 29-30 | ~274 | ~90% |
| **Combined** | - | **424** | **90.1%** |

### By Expensive Side Price Threshold (OBI_FOLLOW, 10s cooldown)

| Threshold | Signals | FADE Accuracy | $/trade |
|-----------|---------|---------------|---------|
| >= $0.65 | 473 | 86.7% | $0.04 |
| **>= $0.70** | **424** | **90.1%** | **$0.10** |
| >= $0.75 | 368 | 91.3% | $0.04 |
| >= $0.80 | 333 | 94.0% | $0.10 |

**Optimal threshold: $0.70** - Best $/trade and total expected value

---

## Strategy Logic

### Terminology (Updated Feb 2026)

| Old Term | New Term | Definition |
|----------|----------|------------|
| winner_side | spike_side | The side BTC spike predicts (direction of spike) |
| loser_side | expensive_side | The OPPOSITE side - what we actually BUY |
| winner_ask | spike_ask | Ask price of spike_side |
| loser_ask | expensive_ask | Ask price of expensive_side (our entry) |

**Why "expensive_side"?** We only trade when this side is >= $0.65 (expensive). The name describes our entry condition.

### Signal Generation (Same as AGGRESSIVE V1)

1. **EWMA Spike Detection** (1000ms halflife)
   - Compare current BTC price to EWMA
   - OU adaptive threshold (calibrated on IS+OOS2)

2. **Velocity Confirmation**
   - Velocity must confirm spike direction
   - Threshold: ±0.10 bps

3. **Enhanced Score >= Threshold**
   - Composite of magnitude, velocity, time remaining

4. **OBI Filter** (if available)
   - Enhanced OBI filter with expensive spread consideration

### AGGRESSIVE_M V2 Filter (NEW)

5. **OBI Filter (OBI_FOLLOW)**
   ```python
   # OBI > 0 on spike_side = market confirms spike = better signal
   if obi_spike is not None and obi_spike > 0:
       pass  # Signal passes
   else:
       continue  # Skip signal
   ```

6. **Expensive Side Price Check**
   ```python
   # After all AGGRESSIVE filters pass:
   if expensive_ask >= 0.70:  # Market uncertain
       action = "FADE"   # Buy expensive_side
   else:
       action = "SKIP"   # Market agrees with spike
   ```

7. **Deduplication (10s cooldown)**
   ```python
   # Per CLAUDE_MISTAKES.md #50 - signals cluster in bursts
   COOLDOWN_SECONDS = 10
   if spike_ts - last_signal_ts[spike_dir] < cooldown_ms:
       continue  # Skip duplicate
   ```

### Entry

- **Side:** expensive_side (OPPOSITE of spike direction)
- **Order Type:** MAKER (limit order)
- **Entry Price:** expensive_ask (current ask)
- **Entry Fee:** 0% (maker)

### Exit

- **Hold to Resolution:** Primary exit
- **Time-stop:** Optional - saves ~$0.54/share on losing trades
- **Exit Fee:** 0% if maker exit, 2% if taker

---

## Expected Value

### Per Trade (5 shares, OBI_FOLLOW @ $0.70 threshold)

| Metric | Value |
|--------|-------|
| Accuracy | 90.1% |
| Avg entry | $0.881 |
| EV per share | $0.901 - $0.881 = **$0.020** |
| EV per trade (5 shares) | **$0.10** |

### Hourly Rate

With 10s cooldown: ~424 signals across IS+OOS2 + OOS7 datasets
- Estimated ~15-20 trades/hour depending on market activity
- **$1.50-2.00/hour** expected (conservative)

Note: EV is thin because accuracy (90.1%) barely exceeds entry price ($0.881). Higher thresholds ($0.80+) have better $/trade but fewer signals.

---

## Why This Works

### The Divergence Signal

When BTC spikes but Polymarket doesn't follow:
1. BTC moved → AGGRESSIVE detects spike
2. expensive_side stays expensive (>= $0.65) → Market says "I don't believe this spike"
3. Market is right 90% of the time

### Economic Intuition

- **Expensive expensive_side** = Market uncertain about outcome
- **Spike not moving Polymarket** = Spike is noise, not signal
- **FADE** = Trust Polymarket over short-term BTC noise

### vs AGGRESSIVE (V1 Taker)

| Aspect | AGGRESSIVE V1 (taker) | AGGRESSIVE_M V2 |
|--------|----------------------|-----------------|
| Direction | FOLLOW spike | FADE spike |
| Entry fee | 2% (taker) | 0% (maker) |
| Accuracy | 46% (when expensive_ask >= $0.65) | **90%** |
| Signal | Spike predicts winner | Spike predicts LOSER |

---

## Price Movement Analysis

After FADE signal (expensive_ask >= $0.65):

| Window | Mean Favorable | Reaches +$0.05 | Reaches +$0.10 |
|--------|----------------|----------------|----------------|
| 30s | $0.013 | 16.6% | 2.5% |
| 60s | $0.022 | 18.2% | 7.6% |
| 120s | $0.038 | 24.2% | 14.4% |
| 300s | $0.060 | 38.2% | 20.1% |

**Conclusion:** Price drifts slowly in our favor. This is a **hold-to-resolution strategy**, not scalping.

---

## Grid Search Parameters

### Core Parameters

| Parameter | Grid Values | Default | Rationale |
|-----------|-------------|---------|-----------|
| min_expensive_ask | [0.65, 0.70, 0.75, 0.80] | **0.70** | Best $/trade at 0.70 |
| obi_filter | [NO_OBI, OBI_FOLLOW] | **OBI_FOLLOW** | 90.1% vs 88.4% accuracy |
| cooldown_seconds | [10, 30] | **10** | More signals, similar accuracy |
| shares_per_trade | [5, 10, 25, 50] | 5 | Position sizing |

### Stop Loss Parameters

| Parameter | Grid Values | Rationale |
|-----------|-------------|-----------|
| time_stop_seconds | [None, 30, 60, 120] | None = hold to resolution |
| price_stop_pct | [None, 0.10, 0.15, 0.20] | % adverse move to cut |

**Stop Loss Analysis (on 10% losing trades):**
- Time stop at 30s: saves ~$0.54/share vs resolution
- Time stop at 60s: saves ~$0.52/share vs resolution
- Time stop at 120s: saves ~$0.55/share vs resolution

### Hedge Ratio (Optional)

| Parameter | Grid Values | Description |
|-----------|-------------|-------------|
| hedge_ratio | [0.0, 0.50, 0.75, 1.0] | 0 = hold to resolution, 1 = full hedge |

**Recommendation:** Start with hedge_ratio = 0 (hold to resolution). Edge comes from resolution payout.

---

## Implementation

### Config (for TRADING_CONFIGS.py)

```python
@dataclass
class AggressiveMV2Config:
    # Signal detection (same as AGGRESSIVE V1)
    spike_method: str = "EWMA_1000"
    lookback_ticks: int = 72
    velocity_confirm_threshold: float = 0.10
    enhanced_score_threshold: float = 0.30

    # AGGRESSIVE_M V2 filters
    min_expensive_ask: float = 0.70  # Only trade when expensive_side >= this
    obi_filter: str = "OBI_FOLLOW"   # OBI > 0 on spike_side
    cooldown_seconds: int = 10       # Deduplication per (market, direction)

    # Entry
    entry_mode: str = "MAKER"  # 0% fee
    shares_per_trade: int = 5

    # Exit
    time_stop_seconds: Optional[float] = None  # None = hold to resolution
    price_stop_pct: Optional[float] = None
    hold_to_resolution: bool = True

    # Timing
    min_time_remaining: float = 90.0
```

### Source Files

| File | Purpose |
|------|---------|
| `research/backtests/aggressive_m_v2_ewma_study.py` | Validation study |
| `research/findings/data/aggressive_m_v2_ewma_study_results.csv` | Study results |
| `research/backtests/aggressive_main_backtest.py` | EWMA + OU detection (imported) |
| `research/findings/AGGRESSIVE_M_STUDY_RESULTS.md` | V1 adverse selection study |

---

## Risks

### 1. Adverse Selection (Mitigated)

AGGRESSIVE_M V1 study showed maker entry has 4-7pp adverse selection when FOLLOWING spikes. V2 mitigates this by:
- FADING instead of FOLLOWING
- Using divergence signal (expensive_ask >= $0.65) which filters for uncertainty
- Not chasing momentum (where adverse selection is worst)

### 2. Fill Rate (Unknown)

As MAKER, orders may not fill. Need to test:
- What % of signals actually fill?
- Does accuracy differ when filled vs unfilled?

### 3. Regime Dependence

| Regime | FADE Accuracy (at $0.65) |
|--------|--------------------------|
| LOW vol (IS+OOS2) | 91.8% |
| HIGH vol (OOS7) | 89.1% |
| HIGH vol (OOS8) | 86.5% |

Pattern holds across regimes but accuracy varies.

---

## Next Steps

1. **Build grid search** (`aggressive_m_v2_grid_search.py`)
   - min_expensive_ask: [0.65, 0.70, 0.75, 0.80]
   - obi_filter: [NO_OBI, OBI_FOLLOW]
   - time_stop_seconds: [None, 30, 60, 120]
   - shares_per_trade: [5, 10, 25, 50]

2. **Run on** IS+OOS2, OOS7 (OOS8, OOS9 if available)

3. **Holdout validation** on OOS10

---

*Created: February 6, 2026*
*Updated: February 6, 2026 (deduplication, OBI_FOLLOW, $0.70 threshold)*
*Validated on: IS+OOS2, OOS7*
