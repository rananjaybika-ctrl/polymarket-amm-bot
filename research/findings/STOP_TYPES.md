# Stop Types: Time vs Price Analysis

**Last Updated:** January 25, 2026

---

## Executive Summary

Statistical analysis of 50 configurations revealed a strong correlation (r = -0.84) between win rate and time-stop effectiveness:

- **Low win rate + EWMA z-score:** Use **180s TIME STOP**
- **High win rate + OU z-score:** Use **15% PRICE STOP**
- **Cycling OFF:** Use **PRICE STOP**

---

## Decision Framework

```
Cycling OFF?                          -> PRICE STOP (time wins only 18%)
Cycling ON + OU z-score?              -> PRICE STOP (time wins 0%)
Cycling ON + EWMA + WinRate < 61%?    -> TIME STOP (wins 86%)
Cycling ON + EWMA + WinRate >= 61%?   -> PRICE STOP
```

---

## Key Statistical Findings

### Correlation with Time-Stop Benefit

| Variable | Correlation | p-value | Significance |
|----------|-------------|---------|--------------|
| **Base Win Rate** | r = -0.84 | < 0.0001 | *** |
| **Cycling ON** | r = +0.54 | 0.0001 | *** |
| Premature Stop % | r = -0.34 | 0.016 | * |
| Base PnL | r = -0.04 | 0.80 | ns |
| Lookback | r = -0.003 | 0.99 | ns |

**Key insight:** Win rate is the STRONGEST predictor. Lower win rate configs benefit MORE from time stops.

### By Win Rate Segment

| Win Rate | Price Stop PnL | Time Stop PnL | Change | Time Wins |
|----------|----------------|---------------|--------|-----------|
| **< 60.7%** | $23.22 | $26.45 | **+13.9%** | 76% |
| **>= 60.7%** | $25.88 | $21.15 | **-18.3%** | 8% |

### By Z-Score Method (Cycling = TRUE)

| Method | Price Stop | Time Stop | Change | Time Wins |
|--------|------------|-----------|--------|-----------|
| **EWMA** | $27.41 | $30.51 | **+11.3%** | 73% |
| EWMA_RATIO | $25.16 | $25.99 | +3.3% | 64% |
| **OU** | $26.78 | $22.56 | **-15.8%** | 0% |

---

## Root Cause Analysis

### Why Time Stops Help Low Win Rate Configs

**AGGRESSIVE (low WR) behavior:**
- Trade entries are more speculative
- Trades move against initially before recovering
- Price stops trigger prematurely on eventual winners
- Time stops let mean reversion work

**Example:** Entry at $0.52, price dips to $0.45 (15% drop triggers price-stop), then recovers to $0.60. With time-stop, trade would have been profitable.

### Why Price Stops Help High Win Rate Configs

**CONSERVATIVE (high WR) behavior:**
- Trade entries already have good timing
- When trades move against, they're likely genuine losers
- Price stops correctly exit losing trades early
- Time stops keep bad trades open longer

---

## Premature Stop Analysis

| Stop Type | Mean Premature % | Mean PnL Lost |
|-----------|------------------|---------------|
| 15% Price | 43.9% | -$15.44 |
| 120s Time | 45.9% | -$8.38 |
| **180s Time** | **35.2%** | **-$6.07** |

The 180s time-stop has ~10pp fewer premature exits vs 120s.

---

## Configuration-Specific Results

### Best Configs for 180s TIME STOP

| Config | Price PnL | Time PnL | Improvement |
|--------|-----------|----------|-------------|
| OU+EWMA, Cycling=True, 0<z<1.5 | $19.51 | $28.95 | **+48.4%** |
| OU+EWMA, Cycling=True, 0<z<2.0 | $21.43 | $31.74 | +48.1% |
| OU+EWMA_RATIO, Cycling=True | $18.74 | $25.93 | +38.4% |

### Best Configs for PRICE STOP

| Config | Price PnL | Time PnL | Time Penalty |
|--------|-----------|----------|--------------|
| EWMA+EWMA, Cycling=False | $17.73 | $9.76 | -44.9% |
| OU+OU, Cycling=True | $23.47 | $15.81 | -32.7% |

---

## OOS Validation (Jan 23)

### Time-Stop vs Price-Stop on Fresh Data

| Config | Stop | OOS3 $/hr | OOS3 WR% | IS $/hr | IS WR% |
|--------|------|-----------|----------|---------|--------|
| **AGGRESSIVE** | **180s TIME** | **$17.59** | **70.2%** | **$7.76** | **68.9%** |
| BALANCED (EWMA) | 15% PRICE | $26.38 | 57.9% | $3.06 | 49.0% |
| BALANCED (OU) | 15% PRICE | $2.34 | 36.7% | $6.07 | 69.6% |

**Key finding:** AGGRESSIVE (time-stop) is the only config consistent across both IS and OOS3.

---

## Implementation

### Time-Stop Logic (volatility_filter_analysis.py)

```python
if config.time_stop_seconds is not None:
    elapsed_seconds = (future_ts - ts) / 1000.0
    if elapsed_seconds >= config.time_stop_seconds:
        # Check if we're in profit
        in_profit = current_winner_bid >= winner_entry
        if not in_profit:
            # Only time-stop if NOT in profit
            hedge_type = "timestop"
            # Take market exit
```

**Key behavior:** Time-stop only triggers if NOT in profit. Winners ride.

---

## Recommendations

### Production Use

| Config Type | Stop Type | Rationale |
|-------------|-----------|-----------|
| AGGRESSIVE | 180s TIME | +33% PnL improvement |
| BALANCED | 15% PRICE | Time-stop hurts (-15.8%) |
| CONSERVATIVE | 15% PRICE | Time-stop hurts |
| CONTRARIAN | NONE | Hold to resolution |

### Never Use

- **120s Time Stop:** Consistently underperforms both 180s and price stops

---

*Consolidated from: TIME_STOP_STATISTICAL_ANALYSIS.md, TIME_BASED_STOP_FINDINGS.md, VOL_FILTER_GRID_SEARCH_FINDINGS_JAN22.md*
