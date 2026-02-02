# AGGRESSIVE Strategy (Path 1)

**Status:** VALIDATED - TIME180s_SKIP + OBI Filter
**Last Updated:** February 1, 2026

---

## Overview

"Quality-first volume strategy" - Detect BTC spikes with OU threshold, enter winner side passively, hedge on loser side, exit via time-stop or passive fill.

---

## Configuration (Canonical) - TIME180s_SKIP + OBI

```python
AGGRESSIVE = TradingConfig(
    name="AGGRESSIVE",
    threshold_method="ou",          # OU (adaptive sigmoid on z-score)
    zscore_method="ewma",           # EWMA (fully adaptive, no drift)
    lookback_ticks=72,              # 1200ms at 60Hz
    lookback_ms=1200,
    stop_loss_pct=None,             # NO price-based stop
    time_stop_seconds=180.0,        # Exit after 180s (Jan 31, 2026 update)
    min_time_remaining=240.0,       # time_stop + 60s buffer (prevents resolution exits)
    use_cycling=True,               # Re-enter after exit
    z_lo=0.0,                       # Z-zone lower bound
    z_hi=1.5,                       # Z-zone upper bound (skip z > 1.5)
    skip_high_entry=True,           # Skip entries >= $0.90 (unhedgeable)
    high_entry_threshold=0.90,      # Turkey problem cutoff
    use_obi_filter=True,            # NEW: Skip if OBI disagrees with spike (Jan 28)
)

# TESTING CONFIG (10 shares):
# high_entry_threshold=0.80 (lower because 10sh * $0.10 = $1.00 min order)
# base_size=10
```

### Parameter Summary

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Threshold Method | OU | Adaptive sigmoid mapping on z-score |
| Z-Score Method | **EWMA** | Adapts to regime shifts (OU drifts) |
| Lookback | 1200ms | 72 ticks at 60Hz |
| Stop | **180s TIME** | Time-stop exit (Jan 31, 2026) |
| Min Time | 240s | time_stop + 60s (prevents resolution exits) |
| Cycling | ON | Re-enter after each exit |
| Z-Zone | 0 < z < 1.5 | Skip very low and high volatility |
| Skip High Entry | **>= $0.90** | Cannot hedge (Polymarket $1 min) |
| Hedge | 100% full | Hedge on loser side |
| **OBI Filter** | **ON** | +4.1pp accuracy when orderbook confirms spike (Jan 28) |

---

## Performance Summary

### Cross-Validation Results (TIME180s_SKIP + OBI)

| Period | Hours | Trades | PnL @50sh | $/hr | Win% | Passive% | Status |
|--------|-------|--------|-----------|------|------|----------|--------|
| IS+OOS2 (Jan 16-19) | 23.4 | 90 | $16 | $0.68 | 55.6% | 63.3% | Training |
| OOS3+4 (Jan 22-24) | 47.1 | 300 | $253 | $5.36 | 56.0% | 70.0% | Validated |
| OOS5 (Jan 26) | 41.7 | 234 | $24 | $0.58 | 59.8% | 62.4% | Validated |
| **OOS7 (Jan 29-30)** | **18.95** | **232** | **$261** | **$13.78** | **54.3%** | **71.1%** | **Validated** |
| **OOS8 (Jan 31)** | **18.12** | **267** | **$166** | **$9.17** | **53.2%** | **72.3%** | **Validated** |

**Combined OOS7+OOS8:** 37.07 hours, 499 trades, **$427 total**, **$11.53/hr**

*Note: IS+OOS2 is older data with OBI OFF and limited 60Hz coverage (23h).
OOS7/OOS8 are the primary validation sets (60Hz + OBI ON). Don't over-emphasize IS+OOS2 results.*

### Validation Data Sources (Feb 2, 2026)
- All datasets: `research/findings/data/timestop_offset_v2_results.csv` (CURRENT_TS180_NOSL_NOMML)
- OOS8: `research/findings/data/oos8_grid_results.csv` (CURRENT_TS180_NOSL_NOMML)

**Key insight:** Direction accuracy clusters around 54-60% across all OOS periods with consistent passive fill rates (62-72%).

### OOS7 Details (18.95 hours, Jan 29-30)

| Metric | Value |
|--------|-------|
| Total PnL @50sh | $261.15 |
| Hourly Rate | $13.78/hr |
| Direction Accuracy | 54.3% |
| Trades | 232 |
| Passive Fill Rate | 71.1% |
| Avg Pair Cost | $0.97 |

### Exit Breakdown (In-Sample)

| Exit Type | Count | % | Notes |
|-----------|-------|---|-------|
| Passive fills | 57 | 51.4% | Our bid got hit |
| Time stops | 29 | 26.1% | Exited after 180s |
| Resolution | 25 | 22.5% | Held to end |

---

## How It Works

### Entry Logic
1. Detect BTC price spike using OU threshold (adaptive sigmoid)
2. Check z-score is in range (0 < z < 1.5)
3. **Check OBI confirms spike direction** (Jan 28: +4.1pp accuracy)
4. Enter winner side at best ask (aggressive entry)
5. Place hedge order on loser side at calculated bid

### Exit Logic
1. **Passive fill**: Hedge bid gets hit -> exit with profit
2. **Time-stop (180s)**: If not filled AND not in profit after 180s -> take market exit (Jan 31, 2026)
3. **Resolution**: If still holding at market resolution -> settle based on outcome

### Why Time-Stop Instead of Price-Stop

From statistical analysis (r = -0.84 correlation between win rate and time-stop benefit):

| Z-Score Method | Time Stop Wins | Recommendation |
|----------------|----------------|----------------|
| **EWMA** | 73% of configs | **USE TIME STOP** |
| OU | 0% of configs | Use price stop |

**Root cause:** AGGRESSIVE has lower initial win rate (~57% before time-stop). Trades move against initially before recovering. Time-stops let mean reversion work instead of cutting winners early.

---

## Skip Rule: Turkey Problem Prevention

### The Problem
Entries at prices >= $0.90 are **unhedgeable** because:
1. Hedge bid must be <= $0.10 (to keep pair cost under $1.00)
2. At $0.10, fill requires 100 shares minimum (Polymarket $1 minimum order)
3. Result: -$45 "turkey losses" when these trades fail

### The Solution
```python
if skip_high_entry and winner_ask >= 0.90:
    return []  # Skip entirely
```

**Important:** Skip rule ONLY applies to PHASE 1 (new entries). PHASE 2 (hedging) is NEVER blocked.

### Cross-Validation Results (149.4 hours, TIME180s_SKIP + OBI)

| Dataset | Hours | Trades | $/hr | Win% | Status |
|---------|-------|--------|------|------|--------|
| IS+OOS2 | 23.4 | 90 | $0.68 | 55.6% | Training |
| OOS3+4 | 47.1 | 300 | $5.36 | 56.0% | Validated |
| OOS5 | 41.7 | 234 | $0.58 | 59.8% | Validated |
| OOS7 | 18.95 | 232 | $13.78 | 54.3% | Validated |
| OOS8 | 18.12 | 267 | $9.17 | 53.2% | Validated |

*Source: timestop_offset_v2_results.csv + oos8_grid_results.csv (CURRENT_TS180_NOSL_NOMML)*

---

## Why It Works

1. **OU threshold adapts** to volatility regime (sigmoid mapping)
2. **EWMA z-score doesn't drift** (unlike static OU z-score)
3. **Time-stop** lets winning trades ride while cutting losers
4. **Z-zone filter** (0 < z < 1.5) avoids extreme volatility noise
5. **Full hedge** limits downside to spread cost
6. **OBI confirmation** (Jan 28): +4.1pp accuracy when orderbook confirms spike

---

## OBI (Orderbook Imbalance) Filter

**Added:** January 28, 2026
**Analysis:** `research/analyze_obi_alpha.py`
**Data:** 11.5 hours depth data (Jan 28, 239 spikes)

### What is OBI?

```
OBI = (bid_depth - ask_depth) / (bid_depth + ask_depth)
```

- **Positive OBI** = more bids = buying pressure = price likely to rise
- **Negative OBI** = more asks = selling pressure = price likely to fall

### How OBI Improves AGGRESSIVE

OBI answers: "Do other traders agree with our spike signal?"

| Filter | 30-tick Accuracy | Count | Interpretation |
|--------|------------------|-------|----------------|
| All spikes | 84.9% | 239 | Baseline |
| **OBI confirms** | **89.0%** | 155 | +4.1pp improvement |
| OBI disagrees | 77.4% | 84 | Worse than baseline |

### Implementation

```python
# In EnhancedSpikeStrategy.get_quotes():
if spike_direction == "UP" and up_imbalance is not None:
    if up_imbalance <= 0:
        spike_direction = None  # OBI disagrees, skip entry
elif spike_direction == "DOWN" and down_imbalance is not None:
    if down_imbalance <= 0:
        spike_direction = None  # OBI disagrees, skip entry
```

### Trade-off

| Metric | Without OBI | With OBI |
|--------|-------------|----------|
| Accuracy | 84.9% | 89.0% |
| Trade count | 100% | ~65% |
| Net effect | More trades, lower accuracy | Fewer trades, higher accuracy |

### OBI Alone is Useless

OBI as standalone signal has **negative edge** (-5.6pp at 100 ticks). Same pattern as velocity: useless alone, powerful as spike confirmation filter.

| Signal Type | Edge |
|-------------|------|
| OBI alone | -5.6pp (worse than random) |
| Spike alone | +34.9pp |
| **Spike + OBI confirms** | **+39.0pp** |

---

## Implementation Notes

### LiveZScoreTracker

Production z-score computation in `src/services/volatility_tracker.py`:

```python
from src.services.volatility_tracker import create_aggressive_tracker

tracker = create_aggressive_tracker()  # EWMA method, z=[0.0, 1.5]

# On each Binance tick
zscore = tracker.update(btc_price)
if not tracker.should_trade():
    return  # Skip - z-score out of bounds
```

### Time-Stop Logic (180s)

```python
if elapsed_seconds >= 180.0:  # Updated Jan 31, 2026
    # Only exit if NOT in profit
    in_profit = current_winner_bid >= winner_entry
    if not in_profit:
        hedge_type = "timestop"
        # Take market exit
```

### Polymarket Constraints

- Minimum order: $1.00
- At 50 shares: enforce `loser_bid >= $0.02`

---

## Deprecated Alternatives

### BALANCED+EWMA
- OOS3: $26.38/hr (dominant)
- OOS4: $11.17/hr (regressed)
- IS: $3.06/hr (weak)
- **Verdict:** Regime-dependent, not a stable edge

### OU Z-Score Configs
- OU z-score uses static mu=-3.9845 from in-sample fit
- BTC price level shift -> EWMA adapted, OU did not
- **Verdict:** Parameter drift makes OU z-score unreliable

---

## Files Reference

### Architecture (Feb 1, 2026)

**Two Sources of Truth:**
1. `src/core/` - Shared LOGIC (fee model, filters, calculations)
2. `research/reference/TRADING_CONFIGS.py` - Winner PARAMS for live trading

| File | Purpose |
|------|---------|
| **Shared Logic** | |
| `src/core/__init__.py` | Re-exports all shared functions |
| `src/core/trading_utils.py` | **LOGIC SOURCE OF TRUTH** - Fee model, OBI filter, score calc |
| **Parameters** | |
| `research/reference/TRADING_CONFIGS.py` | **PARAM SOURCE OF TRUTH** - Winner config for live |
| **Grid Search** | |
| `research/optimizers/aggressive_grid_search.py` | **MAIN GRID SEARCH** - 18 configs (defines own params) |
| `research/optimizers/aggressive_grid_search_v1_legacy.py` | Legacy grid search (720 configs, deprecated) |
| **Backtests** | |
| `research/backtests/aggressive_main_backtest.py` | **MAIN BACKTEST** - Quick single-config validation (imports from src/core) |
| `research/backtests/LEGACY_aggressive_backtest.py` | Legacy backtest (deprecated, no src/core imports) |
| **Live Trading** | |
| `src/strategies/enhanced_spike.py` | Live strategy (imports from src/core + TRADING_CONFIGS) |
| `src/services/volatility_tracker.py` | LiveZScoreTracker |
| `src/models/orderbook.py` | Orderbook with compute_imbalance() |

### Import Pattern

```python
# For shared LOGIC (both live and backtest):
from src.core import (
    polymarket_taker_fee,
    calculate_pnl_with_fees,
    velocity_confirms_spike,
    obi_confirms_spike,
    should_take_spike_enhanced,
    compute_enhanced_score,
    calculate_loser_bid,
    TradeResult,
    BacktestCycle,
)

# For PARAMS (live only):
from research.reference.TRADING_CONFIGS import AGGRESSIVE
```

---

*Consolidated from: MASTER_PLAN_TWO_PATHS.md, FINAL_TRADING_CONFIGS_JAN22.md, VOL_FILTER_GRID_SEARCH_FINDINGS_JAN22.md, TIME_STOP_STATISTICAL_ANALYSIS.md*
