# Session Handover - January 21, 2026

## Session Summary

### Volatility Filter Analysis Tool

**Objective:** Analyze whether skipping extreme volatility periods improves overall PnL compared to trading through them.

**Deliverable:** Created `research/volatility_filter_analysis.py` - standalone script that runs post-optimizer to evaluate z-score cutoffs.

---

## Key Findings (17.5 hour test run)

### Z-Score Distribution
| Regime | Z-Score Range | % of Time |
|--------|---------------|-----------|
| LOW | z < 0 | 0% (this was high-vol period) |
| MEDIUM | 0 - 1.5 | 40.1% |
| HIGH | 1.5 - 2.5 | 30.5% |
| EXTREME | > 2.5 | 29.3% |

### Z-Score Cutoff Analysis
| Cutoff | Trades | Skipped | $/hr | Skip PnL | Dir Acc (kept) | Dir Acc (skipped) |
|--------|--------|---------|------|----------|----------------|-------------------|
| No limit | 40 | 0 | $-0.81 | $0.00 | 55.0% | -- |
| z < 2.5 | 24 | 16 | $-0.48 | $-8.26 | 66.7% | 37.5% |
| z < 1.75 | 17 | 23 | $-0.44 | $-10.10 | 64.7% | 47.8% |
| z < 1.25 | 8 | 32 | $-0.09 | $-13.81 | 75.0% | 50.0% |

**Key Insight:** Skipped trades are net losers at every cutoff level. Direction accuracy in skipped periods is near-random (47-50%).

---

## Per-Market Analysis (Critical Finding)

### Market PnL Breakdown
- **Winners:** 6 markets (+$3.21 total)
- **Losers:** 34 markets (-$17.44 total)

### What-If: Skip Worst Markets
| Skip Worst N | Markets Left | Trades Left | New PnL | Change |
|--------------|--------------|-------------|---------|--------|
| 5 | 35 | 35 | $-10.17 | +$4.07 |
| 10 | 30 | 30 | $-6.47 | +$7.76 |
| 15 | 25 | 25 | $-3.13 | +$11.10 |
| **20** | **20** | **20** | **$-0.23** | **+$14.00** |

**A losing config (-$14.23) becomes nearly breakeven (-$0.23) by skipping the 20 worst markets.**

---

## Trade Characteristics: Winners vs Losers

| Metric | Winners | Losers | Signal |
|--------|---------|--------|--------|
| Z-Score at Entry | 1.78 | 2.74 | **Lower is better** |
| Spike Magnitude | 0.028 | 0.025 | **Higher is better** |
| Time Remaining | 523s | 597s | **Closer to expiry is better** |
| Signal Score | 0.0136 | 0.0074 | **Higher is better** |

### PnL by Time Remaining
| Bucket | Trades | PnL | $/Trade | Dir Acc |
|--------|--------|-----|---------|---------|
| 5-7.5 min | 6 | $-0.56 | $-0.09 | 83.3% |
| 7.5-10 min | 17 | $-4.40 | $-0.26 | 47.1% |
| 10-15 min | 12 | $-7.03 | **$-0.59** | 50.0% |

**10-15 minute trades are the worst performers.**

---

## Script Usage

```bash
# With default config
python research/volatility_filter_analysis.py

# With winning config from optimizer
python research/volatility_filter_analysis.py --from-csv research/optimizer_results.csv

# Specify params directly
python research/volatility_filter_analysis.py --lookback 84 --shares 50 --stop-loss 0.12

# Filter time range
python research/volatility_filter_analysis.py --start-ts 1768705387229
```

### Output Sections
1. Z-Score distribution across dataset
2. Z-Score cutoff sweep table
3. Per-market PnL breakdown (worst/best markets)
4. "What-If" skip worst N markets analysis
5. Winner vs Loser trade characteristics
6. PnL by time remaining / spike magnitude

---

## Next Tasks

### Immediate
1. **Run volatility filter analysis with winning optimizer config**
   ```bash
   python research/volatility_filter_analysis.py --from-csv <optimizer_output.csv>
   ```

2. **Validate on OOS3** - Run analysis on held-out data to confirm z-score filter generalizes

3. **Implement combined filter** - Based on findings, test combining:
   - Z-score cutoff (< 1.75 or < 2.0)
   - Signal score minimum (> 0.01)
   - Time remaining window (5-10 min preferred over 10-15 min)

### Research Questions
1. Can we identify "bad markets" in advance (before entering)?
   - Check if opening volatility/z-score predicts market profitability
   - Check if certain market hours are consistently worse

2. Is there a spike magnitude threshold below which we should skip?

3. Should the strategy have a "vol regime check" before each trade?

### Implementation (if filters prove robust)
1. Add z-score filter to live strategy (`enhanced_spike.py`)
2. Add signal score minimum threshold
3. Consider time-remaining filter (prefer 5-10 min window)

---

## Files Created/Modified

| File | Action | Purpose |
|------|--------|---------|
| `research/volatility_filter_analysis.py` | **CREATED** | Standalone post-optimizer analysis |
| `research/ou_params.json` | EXISTS | OU parameters for z-score computation |

---

## Technical Notes

### Z-Score Computation
- Uses OU parameters from `ou_params.json` (mu=-3.9845, sigma_stat=0.3877)
- BTC data resampled to **60-second intervals** to match OU calibration
- EWMA window = 60 samples for volatility estimation
- Z-score = (log(vol) - mu) / sigma_stat

### Correlation Finding
- Z-score vs PnL correlation: **-0.338** (negative = high vol hurts returns)
- This confirms volatility filtering has merit

---

## Session Context

- OU optimizer was running on combined Training + OOS2 data
- OOS3 reserved for final validation (not included in optimization)
- EWMA optimizer ready to run after OU analysis complete

---

## Grid Search Update (Jan 21, Evening)

### Currently Running
```bash
caffeinate -dims python research/volatility_filter_analysis.py --grid-search --zscore-method all
```

**Grid Parameters:**
- Threshold methods: 2 (ewma, ou)
- Lookbacks: 3 (60, 72, 84 ticks)
- Shares: 1 (5 only)
- Stop losses: 3 (7%, 12%, 15%)
- Cycling: 2 (ON, OFF)
- Z-zones: 10 (including dual-bound ranges like 0 < z < 2.0)

**Total: 36 configs × 10 zones × 4 z-score methods = 1,440 results**

**Z-Score Methods Being Tested:**
| Method | Description |
|--------|-------------|
| `ou` | Static OU params (μ, σ from calibration) |
| `ewma` | Fully adaptive rolling EWMA of log_vol |
| `percentile` | Rolling percentile rank (300s window) |
| `ewma_ratio` | Fast/slow volatility ratio z-score |

**Estimated Runtime:** ~8 hours on 8GB RAM Mac

**Output Files:**
- `research/vol_filter_grid_results_ou.csv`
- `research/vol_filter_grid_results_ewma.csv`
- `research/vol_filter_grid_results_percentile.csv`
- `research/vol_filter_grid_results_ewma_ratio.csv`
- `research/vol_filter_grid_results_all_combined.csv`

---

## CRITICAL: Live Trading Implementation Gap

### Problem
Current analysis **precomputes z-scores** for entire dataset upfront:
```python
zscore_df = compute_zscore_series(btc_df, ...)  # Needs ALL historical data
```

For live trading, we need **incremental/streaming** z-score computation.

### Solution Needed: LiveZScoreTracker

```python
class LiveZScoreTracker:
    """Compute z-score incrementally on each price tick."""

    def __init__(self, method="ewma", window=300):
        self.method = method
        self.ewma_var = None
        self.ewma_mean = None
        self.price_buffer = deque(maxlen=window)  # For percentile method

    def update(self, price: float) -> float:
        """O(1) update per tick, returns current z-score."""
        # Incremental EWMA update - no historical data needed
        ...
        return zscore

    def get_regime(self) -> str:
        """Returns LOW/MEDIUM/HIGH/EXTREME based on current z-score."""
        ...
```

### Feasibility by Method
| Method | Live Complexity | Notes |
|--------|-----------------|-------|
| `ou` | **Easy** | Static μ, σ. Just compute current vol → z-score |
| `ewma` | **Easy** | Single-pass incremental: `var = α*r² + (1-α)*var` |
| `ewma_ratio` | **Easy** | Two EWMA trackers (fast/slow), O(1) per tick |
| `percentile` | **Medium** | Needs 300-sample rolling buffer, O(n) ranking |

### Integration Point
Add to `enhanced_spike.py` or create `src/services/volatility_tracker.py`:
```python
# In strategy initialization
self.zscore_tracker = LiveZScoreTracker(method="ewma")

# On each Binance price tick
zscore = self.zscore_tracker.update(btc_price)
if zscore > ZSCORE_CUTOFF:
    return  # Skip trade - too volatile
```

### TODO After Grid Search
1. Identify best z-score method from grid results
2. Implement `LiveZScoreTracker` for that method
3. Add z-score filter to live trading strategy
4. Backtest with live-style incremental z-scores to verify parity
