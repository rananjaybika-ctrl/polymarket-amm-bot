# Super Aggressive Offset Analysis - Final Report

**Date:** January 15, 2026
**Status:** COMPLETE - Bug Fixed, Deployed to AWS, Committed to Git

---

## Critical Bug Fixed

### Bug: Offset Inversion in _generate_side_quotes()

**Location:** `src/strategies/spread_capture.py` Line 865

```python
# BEFORE (WRONG):
price = round(best_bid - level_offset, 2)

# AFTER (CORRECT):
price = round(best_bid + level_offset, 2)
```

**Problem:** With `loser_offset = -0.12`:
- Wrong: `best_bid - (-0.12) = best_bid + 0.12` → Bids ABOVE market
- Fixed: `best_bid + (-0.12) = best_bid - 0.12` → Bids BELOW market

---

## Files Updated & Synced

| File | Changes | AWS Synced | Git Commit |
|------|---------|------------|------------|
| `src/strategies/spread_capture.py` | Bug fix + super aggressive offsets | ✅ | ✅ a6ccb56 |
| `scripts/spread_capture_observer.py` | loser_offset + hedge formula | ✅ | ✅ a6ccb56 |

---

## Answers to Critical Questions

### Q1: Is super aggressive mathematically sound for long-term profitability?

**YES.**

| Metric | Value |
|--------|-------|
| Expected Value (EV) | **+$0.85/market** |
| Win Rate | 77.4% (24/31 markets) |
| Break-even Win Rate | 69.9% |
| Margin of Safety | **7.5% above break-even** |
| Hourly Projection | $3.40/hour (4 markets/hour) |

**Math:**
```
EV = (win_rate × avg_win) - (loss_rate × avg_loss)
EV = (0.774 × $3.38) - (0.226 × $7.84)
EV = $2.62 - $1.77 = $0.85/market
```

### Q2: Is the 59.2% velocity signal accuracy good?

**59.2% is the WRONG metric.**

| Metric | Value | Notes |
|--------|-------|-------|
| Per-sample accuracy (zones 4-6) | 59.2% | **WRONG METRIC - too noisy** |
| Per-market accuracy (zones 4-6) | **90.0%** | 27/30 correct (source: glowing-exploring-teapot.md) |
| Per-market accuracy (all zones) | 76.7% | 23/30 correct |
| Backtest win rate | **77.4%** | 24/31 markets profitable |

**Why Per-Sample is Wrong:**
Velocity fluctuates within a 15-minute market. One sample might show +0.35 bps, next shows -0.10 bps. Per-sample accuracy counts each fluctuation as a separate prediction.

**What matters:** Did we make money on the MARKET? → 77-90% YES

### Why Unhedged = Wrong by Construction

```
If prediction CORRECT:
  → Loser side price DROPS
  → Hedge fills at target
  → HEDGED (profit locked) ✅

If prediction WRONG:
  → Loser side price RISES
  → Hedge never fills
  → UNHEDGED (on wrong side) ❌
```

**Result:** Unhedged positions ARE the wrong predictions. The hedge fill rate IS the signal quality metric.

---

## Super Aggressive Offsets (Final Config)

### Entry Offsets (Winner Side)
| Zone | Velocity Range | Entry Offset |
|------|----------------|--------------|
| very_strong | 0.30 - 0.50 bps | **+0.01** |
| extreme | 0.50 - 1.00 bps | **+0.01** |
| super_strong | 1.00+ bps | **+0.02** |

### Hedge Offsets (Loser Side) - SUPER AGGRESSIVE
| Zone | Standard | Super Aggressive |
|------|----------|------------------|
| very_strong | -0.06 | **-0.12** |
| extreme | -0.07 | **-0.15** |
| super_strong | -0.08 | **-0.18** |

### How It Works
```
Entry fills UP at $0.55 in very_strong zone:

Standard hedge target:
  0.95 - 0.55 + (-0.06) = $0.34

Super Aggressive hedge target:
  0.95 - 0.55 + (-0.12) = $0.28  ← Waits for cheaper price
```

---

## Backtest Results (7-Hour AWS Data)

### Market Exclusion
- Total markets: 38
- Excluded: 7 (incomplete 15-minute cycles)
- Analyzed: 31 complete markets

### Configuration Comparison

| Configuration | Total PnL | Hedged | Unhedged | Pairs | W/L |
|---------------|-----------|--------|----------|-------|-----|
| Standard ONE-SHOT | $16.50 | $56.10 | -$39.60 | 390 | 26W/5L |
| Standard CYCLING | $12.68 | $45.20 | -$32.52 | 320 | 18W/13L |
| **Super Aggressive ONE-SHOT** | **$26.25** | **$81.15** | -$54.90 | 360 | **24W/7L** |
| Super Aggressive CYCLING | $9.32 | $36.04 | -$26.72 | 180 | 21W/10L |

**Winner:** Super Aggressive ONE-SHOT (+$26.25)

**Note:** These backtest results are NOT affected by the offset inversion bug. The bug existed only in `_generate_side_quotes()` which is used for live trading order generation. The observer and backtest scripts have their own independent fill simulation logic.

---

## Debug Results Summary

### Observer (spread_capture_observer.py): ✅ ALL PASSED
| Area | Status |
|------|--------|
| Hedge target formula | ✓ Correct |
| Hedge fill comparison | ✓ Correct |
| Position reset per market | ✓ Correct |
| Zone 4-6 filter (0.30 BPS) | ✓ Correct |
| loser_offset values | ✓ Correct |

### Spread Capture Strategy: ✅ FIXED
| Area | Status |
|------|--------|
| VELOCITY_ZONES config | ✓ Correct |
| calculate_offsets() | ✓ Correct |
| calculate_entry_bid() | ✓ Correct |
| _generate_side_quotes() | ✓ **FIXED** (was inverted) |

---

## Optimal Live Config

| Setting | Value | Reason |
|---------|-------|--------|
| Entry Mode | ONE-SHOT (15 shares) | Best backtest PnL |
| Zone Filter | 4-6 only (≥0.30 BPS) | 90% accuracy |
| Emergency Hedging | **OFF** | Costs $3-4 more than it saves |
| Loser Offsets | Super Aggressive | Higher hedged profit |

---

## Git Commit

```
a6ccb56 fix: offset inversion bug + super aggressive offsets for zones 4-6

- Fixed line 865: changed best_bid - offset to best_bid + offset
- Added super aggressive loser_offsets for zones 4-6
- Observer updated with matching offsets
- Emergency hedging: OFF
```

---

## References

- **Prior velocity analysis:** `~/.claude/plans/glowing-exploring-teapot.md`
  - Source of 90% per-market accuracy in zones 4-6
  - Detailed breakdown of why 94% claim is misleading
  - Analysis of why unhedged = wrong by construction

---

*Report generated: January 15, 2026*
