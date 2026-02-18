# MASTER PLAN: Production Trading Strategies

**Status:** PIVOTING - Taker→Maker (February 5, 2026)
**Old:** AGGRESSIVE (taker-based) - DEPRECATED
**New:** MAKER-PREDICTION (Path B) + Frank-Wolfe (Path C)

---

## ⚠️ STRATEGY PIVOT (February 5, 2026)

**The AGGRESSIVE taker-based strategy is being deprecated.**

### Why the Pivot?

| Finding | Evidence | Impact |
|---------|----------|--------|
| Latency arb NOT viable | BTC velocity r=0.055 (0.3% variance) | 60Hz data useless |
| Taker fees hurt | 2% on every entry | Eats into edge |
| Pair building fails | 0/108 configs profitable | Avg pair cost > $1.00 |
| Prediction HAS edge | Expensive side = 57% baseline | Gabagool gets 67-70% |

### New Strategy: MAKER-PREDICTION

- **Entry**: MAKER (0% fee) via limit orders
- **Signal**: Prediction (expensive side = likely winner)
- **Sizing**: Consider Frank-Wolfe optimization
- **Full spec**: [strategies/STRATEGY_PIVOT_FEB2026.md](strategies/STRATEGY_PIVOT_FEB2026.md)

---

## Executive Summary

Three strategy paths for Polymarket BTC 15-minute binary markets:

| Path | Strategy | Status | Expected |
|------|----------|--------|----------|
| **A (OLD)** | AGGRESSIVE (taker) | ⚠️ DEPRECATED | Was $15/hr |
| **B (NEW)** | MAKER-PREDICTION | 🔧 IN DEVELOPMENT | TBD |
| **C (NEW)** | Frank-Wolfe Sizing | 🔬 RESEARCH | TBD |
| **2** | CONTRARIAN | ✅ READY | ~$618/hr @2500sh |

---

## Current Status

| Strategy | $/hr | Position Size | WR/Dir Acc | Status |
|----------|------|---------------|------------|--------|
| **AGGRESSIVE** | **$15.20** | 50 shares | ~50% (but +EV) | VALIDATED (EWMA_1000+TS30) |
| **CONTRARIAN** | $618 | 2,500 shares | 43.4% WR | PRODUCTION READY |
| **AS (Time Stop)** | $18.04 → **-$7/hr OOS** | 10 shares | 65% → 44% | **OVERFIT** (Jan 29) |

**UPDATE (Feb 3, 2026):** AGGRESSIVE upgraded to EWMA_1000 spike detection with 30s time-stop. With timestamp deduplication, performance improved from ~$9/hr to $15.20/hr across 167 hours of validation data. Win rate ~50% but avg_win ($4.18) > avg_loss ($2.45) = positive edge.

---

## Strategy Quick Reference

### AGGRESSIVE (Path 1) - EWMA_1000 + TS30
EWMA spike detection + full hedge + time-stop + skip rule

| Parameter | Value |
|-----------|-------|
| Spike Method | **EWMA_1000** (1000ms half-life) |
| Threshold | OU (adaptive sigmoid) |
| Z-Score | EWMA (no drift) |
| Stop | **30s TIME** |
| Min Time | 90s (time_stop + 60s) |
| Z-Zone | 0 < z < 1.5 |
| Skip | entries >= $0.90 |
| OBI Filter | ON |
| Cycling | ON |

**Full spec:** [strategies/AGGRESSIVE.md](strategies/AGGRESSIVE.md)
**EWMA findings:** [findings/AGGRESSIVE_EWMA_FINDINGS.md](findings/AGGRESSIVE_EWMA_FINDINGS.md)

### CONTRARIAN (Path 2)
Mean-reversion at 15-min scale + vol gate

| Parameter | Value |
|-----------|-------|
| Entry | 0.01% pullback + reversal confirmation |
| Filters | retrace >= 0.30, price >= $0.20 |
| Vol Gate | Adaptive EWMA (k=0.5, halflife=50) |
| Stop | None (hold to resolution) |

**Full spec:** [strategies/CONTRARIAN.md](strategies/CONTRARIAN.md)

---

## Go-Live Readiness Checklist

### AGGRESSIVE
- [x] Direction accuracy consistent (68-72% across 3 OOS periods)
- [x] Profitable in all test periods (IS, OOS3, OOS4)
- [x] Z-zone filter validated (0 < z < 1.5)
- [x] Time-stop logic validated (+33% vs price-stop)
- [x] **Binance Safety Gate implemented** (Jan 26)
- [x] WebSocket fills + REST backup working
- [ ] **START LIVE with 5 shares** ← NEXT
- [ ] Scale to 50 shares after verification
- [ ] Fund transfer for full size trading

### CONTRARIAN
- [x] Win rate (43.4%) well above breakeven (30%)
- [x] Cross-validated on IS (81.7h) + OOS3+4 (50.6h)
- [x] Adaptive vol gate working (~35% windows gated out)
- [x] Improved filters validated (retrace >= 0.30, price >= $0.20)
- [x] Binance Safety Gate implemented (Jan 26)
- [ ] Paper trade to measure $0.30 fill rate
- [ ] Determine bankroll for 2500sh trades ($750/trade)

---

## Data Summary

| Dataset | Hours | Markets | Period | Purpose |
|---------|-------|---------|--------|---------|
| IS (Training+OOS2) | 81.7 | 254 | Jan 16-19 | Grid search, optimization |
| OOS3 | 26.4 | 90 | Jan 22-23 | First validation |
| OOS4 | 24.2 | ~100 | Jan 23-24 | Second validation |
| Combined OOS | 50.6 | ~190 | Jan 22-24 | Final confidence |

---

## Quick Commands

```bash
# Validate AGGRESSIVE on combined data
python research/validate_oos4_all_paths.py --combined

# Run losing patterns analysis (training)
python research/validate_oos4_all_paths.py --training

# Validate CONTRARIAN filters
python research/validate_oos4_all_paths.py --combined
```

---

## Research Index

### Strategy Specifications
- [strategies/AGGRESSIVE.md](strategies/AGGRESSIVE.md) - Full AGGRESSIVE config + performance
- [strategies/CONTRARIAN.md](strategies/CONTRARIAN.md) - Full CONTRARIAN config + performance

### Research Findings
- [findings/AGGRESSIVE_EWMA_FINDINGS.md](findings/AGGRESSIVE_EWMA_FINDINGS.md) - **LATEST** EWMA spike + deduplication (Feb 3)
- [findings/AS_WINNING_CONFIGS.md](findings/AS_WINNING_CONFIGS.md) - All winning AS configs with analysis
- [findings/AS_TIME_STOP_CRITICAL_FINDING.md](findings/AS_TIME_STOP_CRITICAL_FINDING.md) - Time stop breakthrough
- [findings/STOP_TYPES.md](findings/STOP_TYPES.md) - Time vs price stop analysis
- [findings/VOLATILITY_FILTER.md](findings/VOLATILITY_FILTER.md) - Z-score filtering, z-zone analysis
- [findings/LOSING_PATTERNS.md](findings/LOSING_PATTERNS.md) - Winner/loser discriminators

### Technical Reference
- [reference/TRADING_CONFIGS.py](reference/TRADING_CONFIGS.py) - Python config definitions
- [reference/SCRIPTS_GUIDE.md](reference/SCRIPTS_GUIDE.md) - How to run research scripts

### Historical Documents
- [archive/handovers/](archive/handovers/) - Session handover documents
- [archive/legacy/](archive/legacy/) - Superseded research files

---

## Key Files

| File | Purpose |
|------|---------|
| `research/backtests/aggressive_main_backtest.py` | **AGGRESSIVE main backtest** (proper cycling) |
| `research/optimizers/aggressive_grid_search.py` | **AGGRESSIVE grid search** (720 configs) |
| `research/validation/validate_oos4_all_paths.py` | OOS validation (Path 1 + Path 2) |
| `research/analysis/volatility_filter_analysis.py` | Core backtest engine |
| `research/backtests/velocity_options_backtest.py` | BASELINE + velocity methods (zone grid search) |
| `research/backtests/acceleration_signal_backtest.py` | Acceleration signal methods |
| `research/backtests/regime_adaptive_backtest.py` | Regime detection methods |
| `research/backtests/multi_signal_backtest.py` | Multi-signal combination methods |
| `research/ML_DIMENSION_REDUCTION_PLAN.md` | ML analysis plan for parameter importance |
| `src/services/volatility_tracker.py` | LiveZScoreTracker for production |

---

## Claude Code Guidelines

Before creating ANY new backtest script:

1. **Read completely** the 3 most relevant existing files - not grep, actually READ
2. **Copy-paste first** - Start from working code, then modify. NEVER create from scratch
3. **Checklist before new script:**
   - [ ] CSV output included? (copy from optimizers/spike_param_optimizer.py)
   - [ ] Proper cycling logic with `in_position` flag? (copy from backtests/aggressive_main_backtest.py)
   - [ ] Matches existing patterns?
4. **Reference files for new backtests:**
   - `backtests/aggressive_main_backtest.py` - ONLY file with proper cycling
   - `optimizers/spike_param_optimizer.py` - CSV output pattern
   - `analysis/volatility_filter_analysis.py` - Grid search structure

### Proper Cycling Logic (CRITICAL)

**Broken (1-second gap):**
```python
MIN_CYCLE_GAP_MS = 1000
if (spike_ts - last_trade_ts) < MIN_CYCLE_GAP_MS:
    continue
last_trade_ts = spike_ts  # Set on ENTRY - WRONG!
```

**Correct (position blocking):**
```python
in_position = False
last_hedge_ts = 0

# Block new entries while in position
if in_position:
    continue

# Enforce gap after hedge fill (not entry)
if (spike_ts - last_hedge_ts) < MIN_CYCLE_GAP_MS:
    continue

# After trade simulation, track hedge fill timestamp
in_position = False
last_hedge_ts = hedge_fill_ts  # Set on HEDGE FILL - CORRECT!
```

### BASELINE Redundancy Rule

- **Run BASELINE only in `velocity_options_backtest.py`**
- Other scripts (acceleration, regime, multi_signal, kalman) should **skip BASELINE**
- This avoids 25 redundant BASELINE runs (5 scripts x 5 zones)

---

---

## UPDATE: EWMA Spike Detection + Deduplication (February 3, 2026)

**Study:** `research/findings/AGGRESSIVE_EWMA_FINDINGS.md`

### Key Findings

EWMA_1000 spike detection with timestamp deduplication across 167 hours:

| Dataset | Hours | Trades | PnL Net | $/hr | Sharpe |
|---------|-------|--------|---------|------|--------|
| IS+OOS2 | 62.7h | 309 | +$163 | +$2.60 | 0.28 |
| OOS3+4 | 42.4h | 704 | +$759 | +$17.91 | 1.15 |
| OOS7 | 19.0h | 798 | +$512 | +$27.00 | 1.11 |
| OOS8 | 18.1h | 912 | +$412 | +$22.75 | 0.77 |
| OOS9 | 24.9h | 1095 | +$692 | +$27.78 | 1.09 |
| **TOTAL** | **167.0h** | **3,818** | **+$2,538** | **+$15.20** | **~0.90** |

### Config Changes (Feb 3, 2026)

| Parameter | Old | New | Reason |
|-----------|-----|-----|--------|
| spike_method | FIXED | **EWMA_1000** | Reduces redundant signals |
| time_stop_seconds | 180s | **30s** | EWMA works better with short time-stop |
| min_time_remaining | 240s | **90s** | time_stop + 60s buffer |

### Deduplication Impact

Raw BTC data has ~67% duplicate timestamps:
- **Without dedup**: ~$9.84/hr (EWMA catches up too fast)
- **With dedup**: **$15.20/hr** (EWMA updates at correct 60Hz rate)

### Live Trading Fix (Feb 3, 2026)

BinanceClient now updates EWMA at ~60Hz (on every unique price tick) instead of at the 5-second trading loop rate. This matches backtest behavior exactly.

---

*Last Updated: February 3, 2026*
