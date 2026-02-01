# MASTER PLAN: Production Trading Strategies

**Status:** LIVE READY - TIME120s_SKIP Deployed (January 27, 2026)
**Strategies:** AGGRESSIVE (Path 1) + CONTRARIAN (Path 2)

---

## Executive Summary

Two independent, validated trading strategies for Polymarket BTC 15-minute binary markets:
- **AGGRESSIVE**: Spike detection + full hedge, ~$9.00/hr @50sh (TIME120s_SKIP config)
- **CONTRARIAN**: Bet against BTC direction, ~$618/hr @2500sh

Both strategies are uncorrelated (different signals, different market conditions) and can run simultaneously.

---

## Current Status

| Strategy | $/hr | Position Size | WR/Dir Acc | Status |
|----------|------|---------------|------------|--------|
| **AGGRESSIVE** | ~$9.00 | 50 shares | ~70% dir | DEPLOYED (TIME120s_SKIP) |
| **CONTRARIAN** | $618 | 2,500 shares | 43.4% WR | PRODUCTION READY |
| **AS (Time Stop)** | $18.04 → **-$7/hr OOS** | 10 shares | 65% → 44% | **OVERFIT** (Jan 29) |

**UPDATE (Jan 29):** AS strategy OVERFIT - training shows +$18/hr but OOS shows -$7/hr to -$21/hr. Weak z-zone (like AGGRESSIVE) helps but still negative on most OOS data. See `research/findings/AS_WINNING_CONFIGS.md` for full analysis.

---

## Strategy Quick Reference

### AGGRESSIVE (Path 1) - TIME120s_SKIP
Spike detection + full hedge + time-stop + skip rule

| Parameter | Value |
|-----------|-------|
| Threshold | OU (adaptive sigmoid) |
| Z-Score | EWMA (no drift) |
| Lookback | 1200ms (72 ticks) |
| Stop | **120s TIME** |
| Min Time | 180s (time_stop + 60s) |
| Z-Zone | 0 < z < 1.5 |
| Skip | entries >= $0.90 |
| Cycling | ON |

**Full spec:** [strategies/AGGRESSIVE.md](strategies/AGGRESSIVE.md)

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
- [findings/AS_WINNING_CONFIGS.md](findings/AS_WINNING_CONFIGS.md) - **NEW** All winning AS configs with analysis
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

## UPDATE: Time-Stop & Loser Offset Optimization (January 31, 2026)

**Study:** `research/findings/TIMESTOP_OFFSET_STUDY_20260131.md`

### Key Findings

On OOS7 (60Hz + OBI ON) - best proxy for production:

| Rank | Config | $/hr | Notes |
|------|--------|------|-------|
| #1 | **CURRENT_TS180** | $13.31 | Baseline, validated |
| #2 | TIGHT_TS180 | $13.92 | Higher but needs validation |

### Config Changes

| Parameter | Old | New | Reason |
|-----------|-----|-----|--------|
| time_stop_seconds | 120s | **180s** | Better on OOS7 data |
| min_time_remaining | 180s | **240s** | time_stop + 60s buffer |

### Deployment Plan

1. **Testing Phase** (after paper trading ends):
   - `TARGET_SHARES = 10`
   - `HIGH_ENTRY_THRESHOLD = 0.80`
   - `time_stop_seconds = 180`

2. **Production Phase** (after 24-48h validation):
   - `TARGET_SHARES = 50`
   - `HIGH_ENTRY_THRESHOLD = 0.90`
   - `time_stop_seconds = 180`

---

*Last Updated: January 31, 2026*
