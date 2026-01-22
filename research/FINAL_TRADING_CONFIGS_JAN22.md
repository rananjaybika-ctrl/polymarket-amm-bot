# Final Trading Configurations - January 22, 2026

## Executive Summary

Three trading configurations validated on 81.71 hours of data across 254 markets.
**All values verified with 0.0% deviation from expected.**

---

## The Three Configs

### AGGRESSIVE (Max $/hr)
**USE 180s TIME-STOP** (NOT price-stop!)

```
Threshold Method: OU
Z-Score Method:   EWMA
Lookback:         1200ms (72 ticks)
Stop:             180s TIME (no price-stop)
Cycling:          ON
Z-Zone:           0 < z < 1.5
```

| Metric | @5 shares | @50 shares |
|--------|-----------|------------|
| PnL | $28.95 | **$289.49** |
| $/hr | $0.953 | **$9.53** |
| Win Rate | 66.7% | 66.7% |
| Trades | 111 | 111 |
| Premature Stop % | 34.5% | 34.5% |
| PnL Lost to Premature | -$5.32 | -$53.20 |

**Exit Breakdown:**
- Passive fills: 57 (51.4%)
- Time stops: 29 (26.1%)
- Resolution: 25 (22.5%)

---

### BALANCED (High Win Rate + Good $/hr)
**USE 15% PRICE-STOP** (time-stop is worse here)

```
Threshold Method: OU
Z-Score Method:   OU
Lookback:         1400ms (84 ticks)
Stop:             15% PRICE (no time-stop)
Cycling:          ON
Z-Zone:           -0.5 < z < 1.5
```

| Metric | @5 shares | @50 shares |
|--------|-----------|------------|
| PnL | $27.12 | **$271.19** |
| $/hr | $0.615 | **$6.15** |
| Win Rate | 70.7% | 70.7% |
| Trades | 99 | 99 |
| Premature Stop % | 37.0% | 37.0% |
| PnL Lost to Premature | -$7.80 | -$78.00 |

**Exit Breakdown:**
- Passive fills: 60 (60.6%)
- Price stops: 27 (27.3%)
- Resolution: 12 (12.1%)

---

### CONSERVATIVE (Highest Win Rate)
**USE 15% PRICE-STOP** (time-stop is worse here)

```
Threshold Method: OU
Z-Score Method:   OU
Lookback:         1400ms (84 ticks)
Stop:             15% PRICE (no time-stop)
Cycling:          OFF
Z-Zone:           0 < z < 1.5
```

| Metric | @5 shares | @50 shares |
|--------|-----------|------------|
| PnL | $20.98 | **$209.76** |
| $/hr | $0.619 | **$6.19** |
| Win Rate | 75.0% | 75.0% |
| Trades | 52 | 52 |
| Premature Stop % | 23.1% | 23.1% |
| PnL Lost to Premature | -$1.73 | -$17.30 |

**Exit Breakdown:**
- Passive fills: 38 (73.1%)
- Price stops: 13 (25.0%)
- Resolution: 1 (1.9%)

---

## Why Different Stop Types?

### Statistical Analysis Results (r = -0.84 correlation)

| Win Rate | Best Stop | Time Stop Win Rate |
|----------|-----------|-------------------|
| < 61% (Aggressive) | **180s TIME** | 76% of configs |
| >= 61% (Conservative) | **15% PRICE** | 8% of configs |

### Z-Score Method Impact (Cycling=True)

| Method | Time Stop Wins | Recommendation |
|--------|---------------|----------------|
| **EWMA** | 73% | USE TIME STOP |
| **EWMA_RATIO** | 64% | USE TIME STOP |
| **OU** | 0% | USE PRICE STOP |

### Decision Framework

```
Cycling OFF?                          → PRICE STOP (time wins only 18%)
Cycling ON + OU z-score?              → PRICE STOP (time wins 0%)
Cycling ON + EWMA + WinRate < 61%?    → TIME STOP (wins 86%)
Cycling ON + EWMA + WinRate >= 61%?   → PRICE STOP
```

### Root Cause

- **Low win rate (AGGRESSIVE):** Trades move against initially before recovering. Time-stops let mean reversion work instead of cutting winners early.
- **High win rate (BALANCED/CONSERVATIVE):** Entries already have good timing. When trades move against, they're likely genuine losers. Price-stops cut losses correctly.

---

## Comparison Table

| Config | Stop Type | PnL @50sh | $/hr @50sh | Win% | Trades | Prem% |
|--------|-----------|-----------|------------|------|--------|-------|
| **AGGRESSIVE** | **180s TIME** | **$289.49** | **$9.53** | 66.7% | 111 | 34.5% |
| BALANCED | 15% PRICE | $271.19 | $6.15 | 70.7% | 99 | 37.0% |
| CONSERVATIVE | 15% PRICE | $209.76 | $6.19 | 75.0% | 52 | 23.1% |

---

## Production Constraints

1. **Minimum Order:** At 50 shares, enforce `loser_bid >= $0.02` (Polymarket $1 minimum)
2. **Live Z-Score:** Use `LiveZScoreTracker` from `src/services/volatility_tracker.py`
3. **OOS Validation:** Blocked pending observer data collection (crashed Jan 20+)

---

## Files Reference

| File | Purpose |
|------|---------|
| `research/TRADING_CONFIGS.py` | Master config definitions (Python) |
| `research/validate_three_configs.py` | Validation script |
| `research/three_config_validation_results.csv` | Validation output |
| `research/TIME_STOP_STATISTICAL_ANALYSIS.md` | Full statistical analysis |
| `research/TIME_BASED_STOP_FINDINGS.md` | Time-stop findings |
| `research/VOL_FILTER_GRID_SEARCH_FINDINGS_JAN22.md` | Full grid search findings |
| `src/services/volatility_tracker.py` | Live z-score tracker (NOT UPDATED YET) |

---

## Next Steps

1. [ ] Update `src/services/volatility_tracker.py` with time-stop support for live
2. [ ] Validate on OOS3 data when observer is fixed
3. [ ] Paper trade AGGRESSIVE config with 180s time-stop
4. [ ] Consider testing 150s and 240s time-stops

---

*Generated: January 22, 2026*
*Dataset: 81.71 hours, 254 markets*
*Validation: 0.0% deviation on all metrics*
