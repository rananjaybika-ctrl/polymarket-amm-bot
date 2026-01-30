# AS Fixed Strategy Results

**Status:** IMPLEMENTATION COMPLETE - Ready for Backtest
**Last Updated:** January 29, 2026

---

## Executive Summary

This document describes the fixed Avellaneda-Stoikov (AS) strategy implementation that addresses the overfitting issues identified in the original AS strategy.

### Problem Statement

The original AS strategy showed:
- Training: +$18.04/hr (appeared profitable)
- OOS: -$7 to -$21/hr (complete failure)

Root causes identified:
1. **Strong z-score (>1.5)** = market ALREADY MOVED = buying at peak = adverse selection
2. **Absolute time window (220-500s)** = empirical, not structural
3. **Binary velocity gate** = spurious filter (only 2.5% autocorrelation)
4. **Additive signal formula** = R² = 0.017 (NOT significant)

### Solution

The fixed AS strategy addresses each issue:

| Issue | Original (Overfit) | Fixed (Robust) |
|-------|-------------------|----------------|
| Z-zone | Strong z>1.5 | Weak 0<z<1.5 |
| Time filter | Absolute 220-500s | REMOVED |
| Time stop | None | Adaptive (8/15/30/120s) |
| Velocity | Binary gate | In formula only |
| Signal | Implicit | Multiplicative |
| Regime | No filter | Hard skip LOW |
| Objective | $/hr | Sharpe ratio |

---

## Implementation Files

### 1. `as_fixed_signal_formula.py`

**Purpose:** New multiplicative signal formula based on statistically significant interactions.

**Key Functions:**
- `compute_signal_score()` - Multiplicative formula: spike × |velocity| × time_weight × regime_weight
- `get_adaptive_time_stop()` - 4-bucket time stop (8/15/30/120s by signal quality)
- `classify_regime()` - LOW/MEDIUM/HIGH based on velocity and z-score
- `is_in_z_zone()` - AGGRESSIVE-style weak z-zone filter

**Statistical Basis:**
```
Original additive: R² = 0.017, p = 0.85 (NOT significant)
Spike × velocity:  p = 0.001 (HIGHLY significant)
```

### 2. `as_fixed_backtest.py`

**Purpose:** Backtest with fixed configuration and Sharpe optimization.

**Key Features:**
- `ASFixedConfig` dataclass with structural (not empirical) parameters
- `simulate_market()` - Per-market simulation with fill mechanics
- `run_backtest()` - Aggregate backtest across all markets
- `compute_sharpe()` - Annualized Sharpe ratio calculation

**Configuration:**
```python
ASFixedConfig(
    z_min=0.0, z_max=1.5,           # Weak z-zone (AGGRESSIVE style)
    skip_low_regime=True,           # Hard skip LOW
    use_adaptive_time_stop=True,    # 4-bucket time stops
    enable_hedging=True,            # Keep hedge as risk cushion
    min_signal_score=0.1,           # Signal threshold
    max_order_age_ms=3000,          # Faster pulling
)
```

### 3. `validate_as_fixed_oos.py`

**Purpose:** Walk-forward out-of-sample validation.

**Protocol:**
1. **IS (60%):** Train and validate Sharpe > 0.5
2. **OOS3+4 (20%):** Validate Sharpe > 0.3 (no re-tuning)
3. **OOS5 (20%):** Final test Sharpe > 0 (no re-tuning)

**Key Principle:** Parameters are FIXED after IS training. No re-tuning allowed on OOS data.

---

## Key Design Decisions

### 1. Why Weak Z-Zone (0 < |z| < 1.5)?

From analysis of fill quality:

| Zscore at Fill | Winner Rate | Avg Pair Cost | Profitable % |
|----------------|-------------|---------------|--------------|
| Weak (|z| ≤ 1) | 57.8% | $1.051 | **46.8%** |
| Strong (|z| ≥ 2) | 79.1% | $1.232 | **13.8%** |

**The paradox:** Strong signal → High accuracy → BUT expensive price → WORSE pairs!

When |zscore| is high, the market has ALREADY MOVED. We're buying the winner at PEAK price.

### 2. Why Adaptive Time Stop?

Instead of fixed 220-500s entry window (empirical), we use signal-quality-based time stops:

| Signal Quality | Time Stop | Rationale |
|----------------|-----------|-----------|
| Weak (<0.3) | 8s | Cut losses fast |
| Low-medium | 15s | Limited patience |
| Medium | 30s | Moderate expectation |
| Strong (≥0.85) | 120s | Patient, expect bigger mispricing |

This is similar to AGGRESSIVE strategy's 120s time stop, which is robust OOS.

### 3. Why Skip LOW Regime?

LOW regime analysis:
- Accuracy: 42.5% (worse than random 50%)
- HIGH regime: +23.9pp accuracy vs LOW

LOW regime = unclear direction = coin flip = no edge.

### 4. Why Multiplicative Signal Formula?

Statistical testing showed:
- Additive formula: R² = 0.017, p = 0.85 (NOT significant)
- Spike × velocity interaction: p = 0.001 (HIGHLY significant)

The multiplicative formula captures the interaction that actually predicts outcomes.

### 5. Why Keep Hedging?

Despite hedging costing -$2.86/hr on training data, we keep it because:
- Acts as risk cushion
- Reduces variance
- Improves Sharpe even if reduces $/hr
- User preference: "Slight negative OK if good Sharpe"

---

## Actual Results (Walk-Forward Validation)

### Period Results

| Period | Sharpe | $/hr | Pairs | Win Rate | Status |
|--------|--------|------|-------|----------|--------|
| IS (60%) | -6.80 | -$1.01/hr | 35 | 65.7% | **FAIL** |
| OOS3+4 (20%) | 120.72 | +$15.73/hr | 10 | 80.0% | PASS |
| OOS5 (20%) | -21.96 | -$3.26/hr | 2 | 50.0% | **FAIL** |

### Verdict: **STRATEGY IS OVERFIT**

The AS Fixed strategy failed walk-forward validation:
- IS Sharpe is negative (-6.80) - fails the > 0.5 target
- OOS5 Sharpe is negative (-21.96) - fails the > 0 target
- Performance varies wildly between periods (not consistent)

### Key Observations

1. **Hedged PnL is positive** in IS ($11.60) but unhedged PnL is negative (-$24.50)
2. **OOS3+4 success was unhedged carry** ($63.30) not pair profits ($6.60)
3. **Pair costs are good** ($0.93-0.97 average) but directional bets are losing

### Root Cause Analysis

The AS maker execution still suffers from **adverse selection**:
- We bid at our price
- We only get filled when the market moves against us
- By the time we complete the pair, we're underwater

The AGGRESSIVE strategy works because it uses **TAKER execution** (known price at entry).

---

## Comparison with AGGRESSIVE Strategy

| Metric | AGGRESSIVE | AS Original | AS Fixed (Expected) |
|--------|------------|-------------|---------------------|
| Training | +$7.76/hr | +$15.53/hr | +$8-12/hr |
| OOS | +$17.59/hr | -$7.71/hr | +$0-5/hr |
| Execution | TAKER | MAKER | MAKER |
| Parameters | 7 structural | 15+ empirical | 8 structural |

**Key insight:** AGGRESSIVE is robust because it uses TAKER execution (known price at entry). AS Fixed may still suffer from adverse selection as a MAKER strategy.

---

## Usage

### Run Backtest
```bash
cd /Users/rananjaybika/research
python as_fixed_backtest.py
```

### Run Walk-Forward Validation
```bash
cd /Users/rananjaybika/research
python validate_as_fixed_oos.py
```

### Unit Tests
```bash
cd /Users/rananjaybika/research
python as_fixed_signal_formula.py  # Runs unit tests
```

---

## Conclusion & Next Steps

### Decision: **Abandon AS, Focus on AGGRESSIVE**

The walk-forward validation conclusively shows that the AS maker strategy is structurally flawed:
- The signal improvements (multiplicative formula, regime filtering, z-zone) helped pair profitability
- But the **maker execution** still causes adverse selection
- Directional carry (unhedged positions) dominates PnL, not hedged pairs

### Recommendation

1. **Abandon the AS (maker) strategy** - it cannot overcome adverse selection
2. **Focus on AGGRESSIVE (taker) strategy** - which showed OOS robustness:
   - Training: +$7.76/hr
   - OOS: +$17.59/hr (IMPROVED, not degraded)
3. **Use the signal formula insights** to improve AGGRESSIVE entry timing:
   - Weak z-zone (0 < |z| < 1.5) avoids buying at peak
   - Skip LOW regime markets
   - Multiplicative signal weighting

### Files Delivered

| File | Purpose | Status |
|------|---------|--------|
| `as_fixed_signal_formula.py` | Multiplicative signal formula | ✓ Complete |
| `as_fixed_backtest.py` | Fixed AS backtest | ✓ Complete |
| `validate_as_fixed_oos.py` | Walk-forward validation | ✓ Complete |
| `findings/AS_FIXED_RESULTS.md` | Results documentation | ✓ Complete |
| `findings/AS_FIXED_BACKTEST_RESULTS.md` | Backtest output | ✓ Complete |
| `findings/AS_FIXED_VALIDATION_RESULTS.md` | Validation output | ✓ Complete |

---

## References

- Plan: `/Users/rananjaybika/.claude/projects/-Users-rananjaybika/45316572-f9f5-4236-a2ba-7d290b8d8ff7.jsonl`
- Observer data: `/Users/rananjaybika/research/observer/grid_obs_*.csv`
- Existing backtest: `/Users/rananjaybika/research/passive_grid_mm_backtest.py`
- Signal discovery: `/Users/rananjaybika/research/short_term_signal_discovery.py`

---

*Generated: January 29, 2026*
