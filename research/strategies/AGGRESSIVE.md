# AGGRESSIVE Strategy (Path 1)

**Status:** VALIDATED - EWMA_1000 + TS30 + OBI Filter
**Last Updated:** February 3, 2026

---

## Overview

"Quality-first volume strategy" - Detect BTC spikes with OU threshold, enter winner side passively, hedge on loser side, exit via time-stop or passive fill.

---

## Configuration (Canonical) - EWMA_1000 + TS30 + OBI

```python
AGGRESSIVE = TradingConfig(
    name="AGGRESSIVE",
    spike_method="EWMA_1000",       # EWMA spike detection (1000ms half-life)
    threshold_method="ou",          # OU (adaptive sigmoid on z-score)
    zscore_method="ewma",           # EWMA (fully adaptive, no drift)
    lookback_ticks=72,              # 1200ms at 60Hz (used for velocity)
    lookback_ms=1200,
    stop_loss_pct=None,             # NO price-based stop
    time_stop_seconds=30.0,         # Exit after 30s (Feb 3, 2026 - EWMA winner)
    min_time_remaining=90.0,        # time_stop + 60s buffer (prevents resolution exits)
    min_cycle_gap_ms=50,            # Fast cycling (was 200)
    use_cycling=True,               # Re-enter after exit
    z_lo=0.0,                       # Z-zone lower bound
    z_hi=1.5,                       # Z-zone upper bound (skip z > 1.5)
    skip_high_entry=True,           # Skip entries >= $0.90 (unhedgeable)
    high_entry_threshold=0.90,      # Turkey problem cutoff
    use_obi_filter=True,            # Skip if OBI disagrees with spike (Jan 28)
)

# TESTING CONFIG (10 shares):
# high_entry_threshold=0.80 (lower because 10sh * $0.10 = $1.00 min order)
# base_size=10
```

### Parameter Summary

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| **Spike Method** | **EWMA_1000** | 1000ms half-life, cleaner signals (Feb 3) |
| Threshold Method | OU | Adaptive sigmoid mapping on z-score |
| Z-Score Method | **EWMA** | Adapts to regime shifts (OU drifts) |
| Lookback | 1200ms | 72 ticks at 60Hz (for velocity) |
| Stop | **30s TIME** | Time-stop exit (Feb 3, 2026 - EWMA winner) |
| Min Time | 90s | time_stop + 60s (prevents resolution exits) |
| Cycling | ON | Re-enter after each exit |
| Min Cycle Gap | 50ms | Fast re-entry (was 200ms) |
| Z-Zone | 0 < z < 1.5 | Skip very low and high volatility |
| Skip High Entry | **>= $0.90** | Cannot hedge (Polymarket $1 min) |
| Hedge | 100% full | Hedge on loser side |
| **OBI Filter** | **ON** | +4.1pp accuracy when orderbook confirms spike (Jan 28) |

---

## Performance Summary

### Cross-Validation Results (EWMA_1000 + TS30 + OBI + DEDUP) - Feb 3, 2026

| Period | Hours | Trades | PnL @50sh | $/hr | Win% | Sharpe | Status |
|--------|-------|--------|-----------|------|------|--------|--------|
| IS+OOS2 (Jan 16-19) | 62.7 | 309 | $163 | $2.60 | 51.5% | 0.28 | Training |
| OOS3+4 (Jan 22-24) | 42.4 | 704 | $759 | $17.91 | 51.7% | 1.15 | Validated |
| OOS7 (Jan 29-30) | 19.0 | 798 | $512 | $27.00 | 50.0% | 1.11 | Validated |
| OOS8 (Jan 31) | 18.1 | 912 | $412 | $22.75 | 50.4% | 0.77 | Validated |
| **OOS9 (Feb 1-3)** | **24.9** | **1095** | **$692** | **$27.78** | **46.3%** | **1.09** | **Validated** |
| **TOTAL** | **167.0** | **3818** | **$2,538** | **$15.20** | **49.7%** | **~0.90** | - |

*Note: OOS5 excluded (1.3Hz data incompatible with EWMA spike detection).*
*OOS9 = combined Feb 1-3 with 20.68h gap properly removed.*
*Results with timestamp deduplication - see Deduplication section below.*

### Validation Data Sources (Feb 3, 2026)
- Main backtest: `research/backtests/aggressive_main_backtest.py`
- Results: `research/findings/data/aggressive_main_backtest_results.csv`
- Summary: `research/findings/data/aggressive_main_backtest_summary.csv`
- EWMA findings: `research/findings/AGGRESSIVE_EWMA_FINDINGS.md`

**Key insight:** Win rate ~49% but avg win ($4.18) > avg loss ($2.45), giving positive edge.

### OOS9 Details (24.9 hours, Feb 1-3)

| Metric | Value |
|--------|-------|
| Total PnL @50sh | $691.88 |
| Hourly Rate | $27.78/hr |
| Direction Accuracy | 46.3% |
| Trades | 1095 |
| Passive Fill Rate | 46.5% |
| Avg Pair Cost | $0.98 |
| Sharpe | 1.09 |
| Max Drawdown | $73.83 (10.7%) |
| Profitable Markets | 72.2% |

### Exit Breakdown (OOS9)

| Exit Type | Count | % | Avg PnL | Notes |
|-----------|-------|---|---------|-------|
| Passive fills | 509 | 46.5% | +$4.18 | Always winning |
| Time stops | 583 | 53.2% | -$2.46 | Always losing |
| Resolution | 3 | 0.3% | -$0.40 | Rare |

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

### Cross-Validation Results (167 hours, EWMA_1000 + TS30 + OBI + DEDUP)

| Dataset | Hours | Trades | $/hr | Win% | Status |
|---------|-------|--------|------|------|--------|
| IS+OOS2 | 62.7 | 309 | $2.60 | 51.5% | Training |
| OOS3+4 | 42.4 | 704 | $17.91 | 51.7% | Validated |
| OOS7 | 19.0 | 798 | $27.00 | 50.0% | Validated |
| OOS8 | 18.1 | 912 | $22.75 | 50.4% | Validated |
| OOS9 | 24.9 | 1095 | $27.78 | 46.3% | Validated |

*Source: aggressive_main_backtest_summary.csv (Feb 3, 2026) - with deduplication*

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

## Deduplication (Feb 3, 2026)

### Why Deduplication Matters

Raw BTC data from Binance has ~67% duplicate timestamps (multiple messages in same millisecond). This significantly affects EWMA spike detection:

| Scenario | EWMA Update Rate | Signal Quality |
|----------|------------------|----------------|
| **Without dedup** | Every message (~180Hz) | EWMA catches up too fast, fewer spikes detected |
| **With dedup** | Once per unique timestamp (~60Hz) | EWMA adapts at correct rate, more valid signals |

### Impact on Results

| Metric | Without Dedup | With Dedup |
|--------|---------------|------------|
| Total Trades (167h) | ~2,380 | 3,818 |
| Total PnL Net | ~$1,644 | $2,538 |
| $/hr | ~$9.84 | $15.20 |

### Live Trading Requirement

**CRITICAL:** To replicate backtest results in live trading, EWMA must update at 60Hz (once per unique price), not at the 5-second trading loop rate.

Current live code updates EWMA only when `get_quotes()` is called (every 5s). This needs to be changed to update EWMA on every Binance tick, filtering duplicate prices.

```python
# In BinanceClient: already filters by price (line 223-231)
last_price = self._spike_price_history[-1] if self._spike_price_history else None
if last_price is None or price != last_price:
    self._spike_price_history.append(price)  # Only unique prices
```

The strategy's EWMA state should be updated at this same rate, not at the 5-second loop rate.

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

### Time-Stop Logic (30s)

```python
if elapsed_seconds >= 30.0:  # Updated Feb 3, 2026 (EWMA winner)
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
