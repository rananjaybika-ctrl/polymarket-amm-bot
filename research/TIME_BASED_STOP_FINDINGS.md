# Time-Based Stop Analysis Findings

**Date:** January 22, 2026
**Test Script:** `research/test_time_stops_top50.py`
**Dataset:** 81.71 hours, 254 markets

---

## Executive Summary

Time-based stops (120s, 180s) outperform the 15% price-based stop on **win rate** and **PnL preservation**, but show mixed results on raw PnL depending on the config.

### Key Finding
**180s time-stop is the best overall performer:**
- Higher win rates than price-stop (often +10-15pp)
- Lower premature stop % (30-40% vs 40-50% with price-stop)
- Better PnL on many configs, especially those with cycling ON
- Significantly less PnL lost from premature exits

---

## What is "Premature Stop %"?

**Premature Stop %** = Percentage of stops where direction prediction was actually correct.

A "premature stop" means:
- You entered with correct direction (would have won at resolution)
- Stop triggered due to temporary adverse price movement
- You exited at a loss despite being RIGHT

**Example:**
- Buy UP at $0.92, expecting BTC to rise
- UP temporarily drops to $0.78 (15% drop) -> price-stop triggers
- BTC recovers -> Market resolves UP -> You were RIGHT but got stopped out

Lower premature stop % is better - it means fewer "wrong" exits.

---

## Detailed Results by Config

### Top Configs with 180s Time-Stop

| Rank | Config | Stop Type | PnL | Win% | Prem% | vs Baseline |
|------|--------|-----------|-----|------|-------|-------------|
| 1 | ou/ewma/1200ms | 180s time | $28.95 | 66.7% | 34.5% | **+33%** |
| 6 | ou/ewma/1200ms (z<2.0) | 180s time | $31.74 | 59.5% | 37.1% | **+48%** |
| 16 | ou/percentile/1200ms (z>0) | 180s time | $26.25 | 50.0% | 33.3% | **+24%** |

### Why 180s Beats Price-Stop

From Rank 1 config (ou/ewma/1200ms, cycling ON, 0<z<1.5):

| Metric | 15% Price-Stop | 180s Time-Stop | Difference |
|--------|----------------|----------------|------------|
| Trades | 138 | 111 | -27 |
| PnL | $21.68 | $28.95 | **+$7.27** |
| Win Rate | 57.2% | 66.7% | **+9.5pp** |
| Passive Fills | N/A | 57 | - |
| Resolution | N/A | 25 | - |
| Premature Stop % | 43.9% | 34.5% | **-9.4pp** |
| Premature PnL Lost | -$15.44 | -$5.32 | **+$10.12** |

### Key Patterns

**180s time-stop works best when:**
1. Cycling is ON (more trades to optimize)
2. Z-zone is narrow (0<z<1.5, 0<z<2.0)
3. EWMA z-score method

**120s time-stop shows:**
- More premature stops than 180s (45-50% vs 30-40%)
- Lower win rates than 180s
- Sometimes higher PnL than 180s (config-dependent)

---

## Stop-Out Analysis (Top 10 Only)

**Important Limitation:** The `stop_out_analysis_results.csv` only covers ranks 1-10 (top 10 configs), NOT all 1440 grid search configs. This means the 44% premature stop finding is based on a small sample.

### From stop_out_analysis_results.csv

| Rank | Config | SL-Correct% | Notes |
|------|--------|-------------|-------|
| 1 | ou/ewma/1200ms | 43.9% | Highest $/hr, high premature rate |
| 9 | ou/ou/1400ms (cycling OFF) | **23.1%** | Lowest premature rate |
| 10 | ou/ou/1400ms (cycling ON) | 37.0% | Good balance |

### Correlations (Top 10 Only)

| With Premature Stop % | Correlation |
|-----------------------|-------------|
| Win Rate | -0.73 (higher win = fewer premature) |
| Total Stop-losses | +0.73 (more stops = more premature) |
| $/hr | +0.61 (higher profit configs have more premature stops) |

---

## FINAL RECOMMENDATIONS (Updated Jan 22, 2026)

**CRITICAL FINDING:** Time-stop vs price-stop depends on config style:
- **High-frequency configs (ewma z-score, cycling ON):** 180s TIME-STOP is better
- **Conservative configs (ou z-score):** 15% PRICE-STOP is better

---

### AGGRESSIVE (Max $/hr)
**USE 180s TIME-STOP** (NOT price-stop!)

```
Threshold: OU
Z-Score: EWMA
Lookback: 1200ms (72 ticks)
Stop: 180s TIME (no price-stop)
Cycling: ON
Z-Zone: 0 < z < 1.5
```

Expected @50 shares:
- PnL: **$289.50** (+33% vs price-stop)
- Win Rate: 66.7%
- Premature Stop: 34.5%

---

### BALANCED (High Win Rate + Good $/hr)
**USE 15% PRICE-STOP** (time-stop is worse here)

```
Threshold: OU
Z-Score: OU
Lookback: 1400ms (84 ticks)
Stop: 15% PRICE (no time-stop)
Cycling: ON
Z-Zone: -0.5 < z < 1.5
```

Expected @50 shares:
- PnL: **$271.20**
- Win Rate: 70.7%
- Premature Stop: 37.0%

---

### CONSERVATIVE (Highest Win Rate)
**USE 15% PRICE-STOP** (time-stop is worse here)

```
Threshold: OU
Z-Score: OU
Lookback: 1400ms (84 ticks)
Stop: 15% PRICE (no time-stop)
Cycling: OFF
Z-Zone: 0 < z < 1.5
```

Expected @50 shares:
- PnL: **$209.80**
- Win Rate: 75.0%
- Premature Stop: 23.1% (lowest)

---

## Implementation Notes

### Code Location
Time-stop is implemented in `research/volatility_filter_analysis.py`:

```python
@dataclass
class BacktestConfig:
    time_stop_seconds: Optional[float] = None  # Exit after N seconds if not filled
```

### Logic (lines 780-792)
```python
if config.time_stop_seconds is not None:
    elapsed_seconds = (future_ts - ts) / 1000.0
    if elapsed_seconds >= config.time_stop_seconds:
        # Check if we're in profit (winner price >= entry)
        in_profit = pd.notna(current_winner_bid) and current_winner_bid >= winner_entry
        if not in_profit:
            # Only time-stop if NOT in profit
            hedge_type = "timestop"
            ...
```

**Key behavior:** Time-stop only triggers if NOT in profit. If winner price >= entry, let it ride.

---

## Open Questions

1. **Wider Verification Needed:** Stop-out analysis only covers top 10 configs. Need to verify 44% premature finding holds across random configs from the full 1440.

2. **Optimal Time-Stop Value:** 180s beats 120s in most cases, but what about 150s or 240s?

3. **Hybrid Approach:** Could combine weak price-stop (e.g., 25%) with time-stop for better protection?

---

## Files Reference

| File | Purpose |
|------|---------|
| `research/test_time_stops_top50.py` | Test script for time-stop analysis |
| `research/time_stop_top50_results.csv` | Results from top 50 configs |
| `research/stop_out_analysis_results.csv` | Stop-out breakdown (top 10 only) |
| `research/vol_filter_grid_results_all_combined.csv` | Full grid search (1440 configs) |
| `research/VOL_FILTER_GRID_SEARCH_FINDINGS_JAN22.md` | Complete grid search findings |

---

## Next Steps

1. Run stop-out verification on 5 random configs (not just top 10) to confirm findings
2. Consider testing 150s and 240s time-stops
3. Validate on OOS3 data when available (blocked by observer crash)
