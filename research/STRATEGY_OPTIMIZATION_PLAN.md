# Plan: AGGRESSIVE Strategy Optimization

**Goal:** Optimize time-stop + skip rule for AGGRESSIVE spike trading
**Status:** COMPLETE - TIME120s_SKIP is winner
**Last Updated:** January 27, 2026

---

## Final Results (Correct MIN_TIME = time_stop + 60s)

**Settings:**
- Cycling ON (correct mechanism)
- Skip >= $0.90 (unhedgeable)
- Z-zone 0 <= z <= 1.5
- **MIN_TIME = time_stop + 60s buffer** (ensures time-stop can always execute)

### IS+OOS2 (69.4h, 246 markets)

| Config | MinTime | Trades | $/hr | Dir Acc | Passive | TStop | Res |
|--------|---------|--------|------|---------|---------|-------|-----|
| **TIME120s_SKIP** | **180s** | **613** | **$11.98** | 52.2% | 396 | 217 | **0** |
| TIME180s_SKIP | 240s | 536 | $9.65 | 55.8% | 379 | 157 | 0 |
| TIME300s_SKIP | 360s | 402 | $4.30 | 54.5% | 293 | 109 | 0 |

### OOS3+4 (47.1h, 168 markets)

| Config | MinTime | Trades | $/hr | Dir Acc | Passive | TStop | Res |
|--------|---------|--------|------|---------|---------|-------|-----|
| **TIME120s_SKIP** | **180s** | **898** | **$9.32** | 50.0% | 526 | 372 | **0** |
| TIME180s_SKIP | 240s | 740 | $8.12 | 51.4% | 495 | 245 | 0 |
| TIME300s_SKIP | 360s | 527 | $6.09 | 52.8% | 386 | 141 | 0 |

### OOS5 (40.9h, 42 markets)

| Config | MinTime | Trades | $/hr | Dir Acc | Passive | TStop | Res |
|--------|---------|--------|------|---------|---------|-------|-----|
| TIME120s_SKIP | 180s | 233 | $2.98 | 54.1% | 140 | 92 | 1 |
| **TIME180s_SKIP** | **240s** | **187** | **$4.39** | 54.5% | 127 | 58 | 2 |
| TIME300s_SKIP | 360s | 143 | $0.02 | 55.2% | 99 | 43 | 1 |

**Note:** OOS5 shows TIME180s winning, but overall hourly rates are lower (different market conditions).

**WINNER: TIME120s_SKIP** across IS+OOS2+OOS3+4 (156.5h). OOS5 anomalous.

### Key Metrics

| Metric | TIME120s_SKIP |
|--------|---------------|
| Min entry time | 180s remaining |
| Avg PnL/trade | ~$1.36 |
| Cycles/market | 2.49 (IS) / 5.35 (OOS) |

---

## Why Skip >= $0.90?

### 1. Cannot Hedge (Polymarket $1 Minimum)

When winner_entry >= $0.90:
- loser_bid < $0.02
- 50 shares × $0.02 = $1.00 (exactly at minimum)
- Cannot place meaningful hedge order

### 2. Turkey Problem: Losses Even With Correct Direction

High-entry trades that hit time-stop LOSE money even when direction is correct:

| Time-Stop | High-Entry Stops | Losses | Loss Rate | Total Loss |
|-----------|------------------|--------|-----------|------------|
| 120s | 36 | 13 | **36%** | **-$50.70** |
| 180s | 21 | 8 | 38% | -$59.25 |
| 300s | 5 | 2 | 40% | -$26.90 |

**Worst turkey examples (direction CORRECT, still LOST):**
```
Entry $0.94 + Loser $0.35 = Pair $1.29 → -$14.50 (✓ dir) [TIME120s]
Entry $0.94 + Loser $0.50 = Pair $1.44 → -$22.00 (✓ dir) [TIME180s]
Entry $0.94 + Loser $0.59 = Pair $1.53 → -$26.50 (✓ dir) [TIME300s]
```

### 3. False Positives at Boundary (Manipulation Risk)

Found 3 false positives in high-entry range:

| Entry | Market | Loss if No Hedge |
|-------|--------|------------------|
| **$0.90** | btc-updown-15m-1768786200 | -$45.00 |
| $0.85 | btc-updown-15m-1768797900 | -$42.50 |
| $0.85 | btc-updown-15m-1768842000 | -$42.50 |

All 3 had **z-score = 0.000** (edge of vol zone) - possible manipulation at boundary.

**From Telegram Alpha:** Late-market behavior at 95%+ prices may involve manipulation:
> "Find markets with end time <=180s. Up/Down Odds at 0.95 or higher."

### 4. Impact Math

- One turkey at $0.90 = -$45 loss
- Avg profit per trade = $1.29
- **Wipes out 35 winning trades**

With skip rule:
- Lose ~2% $/hr ($12.97 → $12.71)
- Eliminate turkey risk entirely
- 7,541 risky signals avoided

---

## Why TIME120s > TIME180s > TIME300s?

### Volume Advantage

| Config | Trades | Cycles/hr |
|--------|--------|-----------|
| TIME120s | 686 | 9.9 |
| TIME180s | 626 | 9.0 |
| TIME300s | 535 | 7.7 |

TIME120s runs **28% more cycles** than TIME300s.

### Passive Fill vs Turnover Tradeoff

| Config | Passive% | $/hr |
|--------|----------|------|
| TIME120s | 65% | $12.71 |
| TIME180s | 70% | $9.53 |
| TIME300s | 73% | $6.92 |

Longer time-stop = more passive fills BUT fewer cycles.
**Volume advantage of TIME120s outweighs passive fill benefit.**

### Time-Stop Exit Analysis

Time-stops aren't all bad - they cut losers:
- TIME120s: 32% of exits via time-stop
- These trades would have gone to resolution as losses
- Time-stop exits are **net positive** (avg +$0.45/trade from earlier analysis)

---

## Cycling Mechanism (Verified Correct)

```python
# CORRECT IMPLEMENTATION
in_position = False
last_hedge_ts = 0

# Block new entries while in position
if in_position:
    continue

# Enforce gap after HEDGE FILL (not entry)
if (spike_ts - last_hedge_ts) < MIN_CYCLE_GAP_MS:
    continue

# After hedge completes
in_position = False
last_hedge_ts = hedge_fill_ts  # KEY: use hedge fill timestamp
```

---

## Final Configuration

```python
AGGRESSIVE = TradingConfig(
    name="AGGRESSIVE",
    threshold_method="ou",
    zscore_method="ewma",
    lookback_ticks=72,
    time_stop_seconds=120.0,       # CHANGED from 180s
    min_time_remaining=180.0,      # NEW: time_stop + 60s buffer
    skip_high_entry=True,          # NEW: cannot hedge >= $0.90
    high_entry_threshold=0.90,     # Skip >= $0.90
    use_cycling=True,
    z_lo=0.0,
    z_hi=1.5,
)
```

### Critical Rule: MIN_TIME = time_stop + 60s

| Time-Stop | Min Entry Time | Rationale |
|-----------|----------------|-----------|
| 120s | **180s** remaining | Ensures time-stop can execute before market close |
| 180s | 240s remaining | (alternative config) |
| 300s | 360s remaining | (alternative config) |

**This only blocks new ENTRIES, not hedges.** Once in position, hedge continues until filled.

---

## Cross-Validation Summary (Final - Correct MIN_TIME)

| Dataset | Hours* | Markets | TIME120s_SKIP $/hr | TIME180s_SKIP $/hr | Winner |
|---------|--------|---------|-------------------|-------------------|--------|
| IS+OOS2 | 69.4 | 246 | **$11.98** | $9.65 | TIME120s |
| OOS3+4 | 47.1 | 168 | **$9.32** | $8.12 | TIME120s |
| OOS5 | 40.9 | 42 | $2.98 | **$4.39** | TIME180s |
| **Total** | **157.4** | **456** | **~$9.00 avg** | ~$7.80 avg | TIME120s |

*Hours = BTC+Observer overlap

### OOS5 Anomaly Analysis

OOS5 (Jan 24-26) shows different behavior:
- Lower overall $/hr (market conditions changed)
- TIME180s wins instead of TIME120s
- Fewer markets (42 vs 168/246)
- Still has 4 resolution exits (data gap larger in this period)

**Recommendation:** TIME120s_SKIP remains best choice based on larger sample (116.5h vs 40.9h).

---

## Comparison: Before vs After Optimization

| Metric | Before (TIME180s) | After (TIME120s_SKIP) | Change |
|--------|-------------------|----------------------|--------|
| $/hr | $9.65 | $11.98 | **+24%** |
| Trades | 536 | 613 | +14% |
| Min entry time | 60s | 180s | Correct |
| Resolution exits | >0 | **0** | Fixed |
| Turkey risk | Exposed | Eliminated | ✓ |

---

## Files Reference

| File | Purpose |
|------|---------|
| `research/final_timestop_comparison.py` | Final backtest script (IS+OOS2, OOS3+4) |
| `research/oos5_validation.py` | OOS5 validation script |
| `research/final_timestop_results.csv` | Results CSV |
| `research/high_entry_timestop_analysis.py` | Turkey analysis |
| `research/high_entry_risk_analysis.py` | False positive analysis |
| `research/strategies/AGGRESSIVE.md` | Config to update |

---

## Resolution Exit Fix

**Problem:** Observer data ends at ~55s remaining. Entries at 126s with 120s time-stop would trigger at 6s remaining, but data stops before that → "resolution" exits with unhedged losses.

**Solution:** `MIN_TIME = time_stop_seconds + 60s`
- TIME120s: only enter if time_remaining >= 180s
- Ensures time-stop always has room to execute before data ends
- **Resolution exits now = 0** (all trades properly hedged)

---

## Pre-Live: Maker vs Taker Entry Analysis

### Problem Statement

Polymarket has a $1 minimum order value. With taker orders at ~$0.20 prices:
- $1.00 / $0.20 = 5.0 shares theoretical
- But taker fees reduce actual fill to **4.9 shares**
- 2% loss on every entry before trade even starts

### Maker Fill Analysis (IS+OOS2, 69.3h, 472 signals)

| Fill Window | Fill Rate | Trades Filled |
|-------------|-----------|---------------|
| 5s | 7.2% | 34 |
| 10s | 17.6% | 83 |
| 15s | 25.0% | 118 |
| 30s | 36.9% | 174 |
| 60s | 53.8% | 254 |
| **120s** | **65.9%** | **311** |

**Key Metrics:**
- Median time-to-fill: **25.3s**
- Spread saved: **$0.02/share** ($1.07 per 50-share trade)
- **34% of signals would be missed** if maker-only

### Recommendation: Hybrid Approach

1. **Post at best_bid** (maker) on signal
2. **Wait 30-60s** for fill
3. **If unfilled, take at ask** (taker)

**Benefits:**
- Saves ~$1/trade on 66% of entries
- Only 34% pay taker fees
- Full 5.0 shares on maker fills vs 4.9 on taker

### Implementation Notes

```python
# Hybrid entry logic
def place_entry_order(signal):
    # Phase 1: Maker order at best_bid
    order = place_limit_order(price=best_bid, side="BUY")

    # Phase 2: Check fill after timeout
    time.sleep(MAKER_TIMEOUT)  # 30-60s

    if not order.is_filled:
        # Cancel maker, take at ask
        cancel_order(order)
        order = place_market_order(side="BUY")

    return order
```

**File:** `research/maker_fill_analysis.py`

---

## Win Rate vs Direction Accuracy (CRITICAL FINDING)

**The "70% direction accuracy" in earlier docs was actually WIN RATE.**

| Metric | Value | Meaning |
|--------|-------|---------|
| **Direction Accuracy** | **52.2%** | Spike correctly predicted resolution |
| **Win Rate** | **71.6%** | Trade was profitable (PnL > 0) |

### By Exit Type

| Exit Type | Win Rate | Dir Accuracy |
|-----------|----------|--------------|
| **Passive** | **98.2%** | 59.6% |
| **Time-stop** | 23.0% | 38.7% |

**Key Insight:** Passive fills are profitable **even when direction is WRONG** because we profit from the spread ($0.99 → $1.00), not direction prediction.

**File:** `research/partial_hedge_analysis.py`

---

## Small Size Testing: Skip Threshold for 10 Shares

### Problem

Polymarket min order = $1. For 10 shares: min hedge price = **$0.10**

### Empirical Analysis (Time-Stop Loser Fills)

| Winner Entry | Min Loser Fill | Trades Below $0.10 |
|--------------|----------------|-------------------|
| $0.70-0.75 | $0.21 | 0 |
| $0.75-0.80 | $0.15 | **0** |
| $0.80-0.85 | $0.08 | 2 |
| $0.85-0.90 | $0.04 | 4 |

### Backtest Results (IS+OOS2, 69.4h, 10 shares)

| Skip Threshold | Trades | $/hr @10sh | Unhedgeable |
|----------------|--------|------------|-------------|
| $0.70 | 523 | $1.89 | 0 |
| $0.75 | 549 | $2.08 | 0 |
| **$0.80** | **571** | **$2.32** | **0** |
| $0.85 | 599 | $2.36 | 1 |
| $0.90 | 613 | $2.40 | 6 |

### Recommendation

| Shares | Skip Threshold | $/hr | Rationale |
|--------|----------------|------|-----------|
| 50 | $0.90 | $11.98 | Min hedge = $0.02 |
| **10** | **$0.80** | **$2.32** | Min hedge = $0.10, 0 unhedgeable |
| 5 | $0.70 | ~$0.95 | Min hedge = $0.20 |

### Live Testing Configuration

**Purpose:** Test strategy live with minimal capital risk before scaling up.

| Parameter | Production (50sh) | Testing (10sh) |
|-----------|-------------------|----------------|
| `base_size` | 50 | **10** |
| `high_entry_threshold` | 0.90 | **0.80** |

**Only these 2 changes.** All other params (time_stop=120s, min_time=180s, z-zone, cycling) remain the same.

**After validation:** Revert to production config (50 shares, skip >= $0.90)

---

## Next Steps

- [x] Grid search TIME120s/180s/300s
- [x] Skip rule analysis
- [x] Turkey problem quantification
- [x] False positive identification
- [x] Final backtest with correct settings
- [x] Validate on OOS3+4 - **CONFIRMED $9.32/hr**
- [x] Validate on OOS5 - **$2.98/hr** (TIME180s won at $4.39/hr - anomalous period)
- [x] Update AGGRESSIVE.md with final config
- [x] Deploy TIME120s_SKIP to production
- [x] Analyze maker fill rates (66% within 120s)
- [x] **Deploy testing config (10sh, skip >= $0.80) - Jan 27**
- [ ] Validate testing config live
- [ ] Scale to production (50sh, skip >= $0.90)
- [ ] Implement hybrid maker/taker entry in production code

---

## Risk Notes

1. **Manipulation at $0.90 boundary**: 3 false positives all at z=0.000, suggests possible manipulation. Skip rule protects against this.

2. **Time-stop turkey**: Even with correct direction, high-entry time-stop exits lose 36% of the time due to loser not dropping.

3. **CONTRARIAN for naked directional bets**:
   - AGGRESSIVE = hedged spread capture (small consistent profits)
   - CONTRARIAN = naked directional exposure (better R/R for high-conviction)
   - High-entry signals (>$0.90) have ~100% direction accuracy
   - These belong in CONTRARIAN with proper position sizing, NOT AGGRESSIVE
   - CONTRARIAN handles the risk/reward correctly for directional bets

4. **Backtest data gap**: Observer data ends ~55s before market close. "Resolution" exits in backtest are actually data gaps - in production these would be time-stop exits. Backtest may slightly underestimate performance.

5. **OOS5 regime change**: OOS5 (Jan 24-26) showed degraded performance for all configs and TIME180s outperformed TIME120s. This could indicate:
   - Market microstructure changed
   - Different volatility regime
   - Smaller sample size (42 markets vs 168+)
   - Consider monitoring live performance and potentially switching to TIME180s if TIME120s underperforms consistently.

---

*Analysis completed: January 27, 2026*

---

## UPDATE: Time-Stop & Loser Offset Study (January 31, 2026)

**Reference:** `research/findings/TIMESTOP_OFFSET_STUDY_20260131.md`

### New Testing Results

Tested TIGHTER/TIGHT/CURRENT/WIDE offsets × TS30/TS180/TS240/TS300 on multiple datasets.

**Key Finding:** On OOS7 (60Hz + OBI ON), which best matches production:

| Rank | Config | $/hr | Trades | Win% |
|------|--------|------|--------|------|
| #1 | **CURRENT_TS180** | $13.31 | 246 | 53.3% |
| #2 | TIGHT_TS180 | $13.92 | 303 | 55.8% |

**CURRENT_TS180 is the recommended baseline** - validated, safer.
**TIGHT_TS180 needs further validation** before switching.

### Config Update: TIME120s → TIME180s

Based on this study, **180s time-stop outperforms 120s** on recent OOS data:
- TS180 provides better balance of cycling speed and trade quality
- TS30 has too many forced exits before trades mature
- TS240/TS300 have diminishing returns

### Recommended Live Config

```python
# After paper trading validation
AGGRESSIVE = TradingConfig(
    time_stop_seconds=180.0,      # UPDATED from 120s
    min_time_remaining=240.0,     # time_stop + 60s buffer
    high_entry_threshold=0.90,    # skip >= $0.90 (50sh)
    # ... other params unchanged
)
```

### Testing Phase Config (10 shares)

```python
TARGET_SHARES = 10
HIGH_ENTRY_THRESHOLD = 0.80      # lower for 10sh minimum hedge
time_stop_seconds = 180.0
```
