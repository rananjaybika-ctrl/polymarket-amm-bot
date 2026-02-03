# EWMA Spike Base Findings (Feb 3, 2026)

## Status: VALIDATED via Main Backtest

---

## Winner Config: E1000_TS30_OLD

| Parameter | Value | Notes |
|-----------|-------|-------|
| spike_method | EWMA_1000 | 1000ms half-life EWMA |
| time_stop_seconds | 30s | Short time-stop (was 180s) |
| DROP_MULTIPLIER | 0.50 | OLD formula |
| DROP_INTERCEPT | 0.08 | OLD formula |
| min_cycle_gap_ms | 50 | Faster cycling (was 200) |
| min_time_remaining | 90s | time_stop + 60s buffer |

---

## Source Scripts & Analysis Files

### Discovery & Testing
- **Grid search**: `research/optimizers/test_short_term.py` - Original EWMA spike method testing
- **EWMA variants tested**: `research/optimizers/test_ewma_spike_base.py`, `test_ewma_spike_base_ts180.py`

### Validation
- **Main backtest**: `research/backtests/aggressive_main_backtest.py` - Single-config validator
- **Results CSV**: `research/findings/data/aggressive_main_backtest_results.csv`
- **Summary CSV**: `research/findings/data/aggressive_main_backtest_summary.csv`

### Configuration
- **Config source**: `research/reference/TRADING_CONFIGS.py` - AGGRESSIVE config
- **Live implementation**: `src/strategies/enhanced_spike.py` - `_detect_spike_ewma()` method
- **Live runner**: `scripts/run_paper_bot.py` - `spike_method` parameter

---

## Performance (VALIDATED Feb 3, 2026)

### Full 60Hz Dataset Validation (OBI ON, skip >= $0.90)

| Dataset | Hours | Trades | PnL Net | $/hr | Sharpe |
|---------|-------|--------|---------|------|--------|
| IS+OOS2 | 23.44h | 119 | +$29.26 | +$1.25 | 0.13 |
| OOS3+4 | 47.15h | 384 | +$400.91 | +$8.50 | 0.77 |
| OOS7 | 18.95h | 322 | +$177.05 | +$9.34 | 0.60 |
| OOS8 | 18.12h | 460 | +$344.95 | +$19.03 | 0.93 |
| OOS9.1 | 7.74h | 166 | +$96.43 | +$12.45 | 0.67 |
| **Combined** | **115.4h** | **1451** | **+$1,048.61** | **+$9.09/hr** | - |

### OOS7+OOS8+OOS9.1 Only (matches grid search test)
| Combined | 44.81h | 948 | +$618.43 | **+$13.80/hr** | - |

### Why 60Hz Only?
- EWMA spike detection relies on high-frequency price updates
- OOS5 has only 1.3Hz data (observer binance_price, not 60Hz HF stream)
- Results would be misleading if low-frequency data is mixed in

---

## Test Config Impact Analysis (skip >= $0.80)

For low-risk testing, we use `high_entry_threshold=0.80` instead of production $0.90.

| Metric | All Trades | Below $0.80 | >= $0.80 |
|--------|-----------|-------------|----------|
| Trades | 1451 | 1307 (90.1%) | 144 (9.9%) |
| PnL | $1,048.61 | $882.18 | $166.42 |
| $/hr | $9.09 | **$7.64** | $1.44 |
| Win Rate | 51.1% | 46.2% | 95.1% |
| Avg PnL/trade | $0.72 | $0.67 | $1.16 |

**Test config ($0.80 skip) reduces hourly by 16%** but acceptable for low-risk validation.
**NOTE**: High-entry trades (>= $0.80) are actually MORE profitable - consider raising threshold to $0.85 after testing.

---

## Key Insight: Why EWMA Works Better

### Problem with FIXED Lookback
Fixed lookback compares current price to price N ticks ago. After a spike:
- Price stays elevated
- Every tick for ~1200ms still shows the same spike
- Result: 14 signals from ONE price move

### EWMA Solution
EWMA tracks a smoothed running average that adapts:
- After a spike, the EWMA gradually moves toward the new price
- Once EWMA catches up, the "spike" disappears
- Result: **ONE signal per price move**

### Quantitative Impact
| Method | OOS7 Spikes | OOS7 Trades |
|--------|-------------|-------------|
| FIXED | ~14 per move | Higher noise |
| EWMA_1000 | ~1 per move | Cleaner signals |

---

## Validation Status

- [x] Main backtest updated with EWMA spike detection
- [x] Run on 60Hz-only datasets (exclude OOS5)
- [x] Results match grid search for OOS7+8+9.1 ($13.80/hr)
- [x] Live code updated (`enhanced_spike.py` with `_detect_spike_ewma()`)
- [x] TRADING_CONFIGS.py updated with winner params
- [x] run_paper_bot.py wired to use spike_method from config

---

## Files Updated

| File | Changes |
|------|---------|
| `research/backtests/aggressive_main_backtest.py` | Added EWMA spike detection, SPIKE_METHOD param, is_60hz flag, deep metrics |
| `research/optimizers/aggressive_grid_search.py` | Added EWMA spike detection, fixed min_time bug, added OOS9.1, added spike_method to TestConfig |
| `research/reference/TRADING_CONFIGS.py` | Added spike_method="EWMA_1000", updated time_stop=30s, min_time=90s, min_cycle_gap_ms=50 |

---

## Live Implementation Notes

EWMA can be calculated on-the-fly in live trading:

```python
# Initialize once
ewma_price = first_price
alpha = 1 - 0.5 ** (1.0 / (halflife_ms / 16.67))  # ~60Hz

# On each tick
ewma_price = alpha * current_price + (1 - alpha) * ewma_price
change_pct = (current_price - ewma_price) / ewma_price * 100
spike_magnitude = abs(change_pct)

if spike_magnitude >= threshold:
    # Spike detected!
```

The EWMA state is just one float per market, making it lightweight for live execution.

---

## Circuit Breaker Recommendation (for Production)

### Current: $50 Cumulative Loss
- Never triggered on 60Hz data (max loss was only -$25.01)

### Recommended: Add 40% Trailing Stop
```python
if peak_pnl > 50 and current_pnl < peak_pnl * 0.60:
    stop_session()
```

40% trailing stop never triggers on historical data, captures full profit.

---

## Next Steps

1. Run `python research/backtests/aggressive_main_backtest.py`
2. Verify results match expectations within 1%
3. If validated, update this document to remove "TENTATIVE"
4. Consider live paper testing with EWMA_1000 + TS30

---

---

## Known Issues & Gaps (Feb 3, 2026 Audit)

### Fixed Issues

| Issue | File | Status |
|-------|------|--------|
| Frontend time_stop default 180→30 | web/static/index.html | FIXED |
| Frontend z-score fields (disable for EWMA) | web/static/index.html | FIXED |
| app.js handle null z-score | web/static/app.js | FIXED |
| run_paper_bot.py spike_method wiring | scripts/run_paper_bot.py | FIXED |
| enhanced_spike.py EWMA detection | src/strategies/enhanced_spike.py | FIXED |

### Outstanding Issues (Low Priority - Research Scripts Only)

| Issue | Files | Impact |
|-------|-------|--------|
| No EWMA dispatcher | test_fixed_offset_oos7.py, zscore_filter_test.py | Can't test EWMA variants in these scripts |
| Hardcoded MIN_TIME=240 | enhanced_spike.py line 197 | Unused constant, confusing |
| Hardcoded MIN_CYCLE_GAP_MS | Various analysis scripts (1000ms) | Research artifacts, intentionally different |

### Files That Are Correctly Updated

- `research/backtests/aggressive_main_backtest.py` - Has EWMA dispatcher
- `research/optimizers/aggressive_grid_search.py` - Has EWMA dispatcher
- `research/reference/TRADING_CONFIGS.py` - AGGRESSIVE config updated
- `src/strategies/enhanced_spike.py` - Live EWMA implementation
- `scripts/run_paper_bot.py` - Wired to use spike_method from config
- `web/static/index.html` - Test defaults updated
- `web/static/app.js` - Handles null z-score values
- `web/server.py` - Already imports from TRADING_CONFIGS.py (no changes needed)

---

*Generated: Feb 3, 2026*
