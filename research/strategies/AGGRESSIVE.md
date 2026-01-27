# AGGRESSIVE Strategy (Path 1)

**Status:** VALIDATED - TIME120s_SKIP Config Deployed
**Last Updated:** January 27, 2026

---

## Overview

"Quality-first volume strategy" - Detect BTC spikes with OU threshold, enter winner side passively, hedge on loser side, exit via time-stop or passive fill.

---

## Configuration (Canonical) - TIME120s_SKIP

```python
AGGRESSIVE = TradingConfig(
    name="AGGRESSIVE",
    threshold_method="ou",          # OU (adaptive sigmoid on z-score)
    zscore_method="ewma",           # EWMA (fully adaptive, no drift)
    lookback_ticks=72,              # 1200ms at 60Hz
    lookback_ms=1200,
    stop_loss_pct=None,             # NO price-based stop
    time_stop_seconds=120.0,        # Exit after 120s (optimized from 180s)
    min_time_remaining=180.0,       # time_stop + 60s buffer (prevents resolution exits)
    use_cycling=True,               # Re-enter after exit
    z_lo=0.0,                       # Z-zone lower bound
    z_hi=1.5,                       # Z-zone upper bound (skip z > 1.5)
    skip_high_entry=True,           # Skip entries >= $0.90 (unhedgeable)
    high_entry_threshold=0.90,      # Turkey problem cutoff
)
```

### Parameter Summary

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Threshold Method | OU | Adaptive sigmoid mapping on z-score |
| Z-Score Method | **EWMA** | Adapts to regime shifts (OU drifts) |
| Lookback | 1200ms | 72 ticks at 60Hz |
| Stop | **120s TIME** | +24% hourly rate vs 180s |
| Min Time | 180s | time_stop + 60s (prevents resolution exits) |
| Cycling | ON | Re-enter after each exit |
| Z-Zone | 0 < z < 1.5 | Skip very low and high volatility |
| Skip High Entry | **>= $0.90** | Cannot hedge (Polymarket $1 min) |
| Hedge | 100% full | Hedge on loser side |

---

## Performance Summary

### Cross-Validation Results

| Period | Hours | Trades | $/hr @50sh | Dir Acc | Status |
|--------|-------|--------|------------|---------|--------|
| IS (Jan 16-19) | 81.7 | 90 | $7.76 | 68.9% | Training |
| OOS3 (Jan 22-23) | 26.4 | 84 | $17.59 | 70.2% | Validated |
| OOS4 (Jan 23-24) | 24.2 | 145 | **$16.72** | 72.4% | Validated |

**Key insight:** Direction accuracy is remarkably consistent (68.9% -> 70.2% -> 72.4%) across all periods.

### OOS4 Details (24.2 hours, Jan 23-24)

| Metric | Value |
|--------|-------|
| Total PnL @50sh | $404.62 |
| Hourly Rate | $16.72/hr |
| Direction Accuracy | 72.4% |
| Trades | 145 |
| Passive Fill Rate | ~55% |
| Time-Stop Exits | ~28% |

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
3. Enter winner side at best ask (aggressive entry)
4. Place hedge order on loser side at calculated bid

### Exit Logic
1. **Passive fill**: Hedge bid gets hit -> exit with profit
2. **Time-stop (180s)**: If not filled AND not in profit after 180s -> take market exit
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

### Cross-Validation Results (157.4 hours, 456 markets)

| Dataset | Hours | TIME120s_SKIP | TIME180s_SKIP | Winner |
|---------|-------|---------------|---------------|--------|
| IS+OOS2 | 69.4 | **$11.98/hr** | $9.65/hr | TIME120s |
| OOS3+4 | 47.1 | **$9.32/hr** | $8.12/hr | TIME120s |
| OOS5 | 40.9 | $2.98/hr | $4.39/hr | TIME180s* |
| **Total** | **157.4** | **~$9.00 avg** | ~$7.80 avg | **TIME120s** |

*OOS5 anomalous (smaller sample, different market conditions)

---

## Why It Works

1. **OU threshold adapts** to volatility regime (sigmoid mapping)
2. **EWMA z-score doesn't drift** (unlike static OU z-score)
3. **Time-stop** lets winning trades ride while cutting losers
4. **Z-zone filter** (0 < z < 1.5) avoids extreme volatility noise
5. **Full hedge** limits downside to spread cost

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

### Time-Stop Logic (120s)

```python
if elapsed_seconds >= 120.0:  # Optimized from 180s
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

| File | Purpose |
|------|---------|
| `research/validate_oos4_all_paths.py` | OOS validation script |
| `research/volatility_filter_analysis.py` | Core backtest engine |
| `research/TRADING_CONFIGS.py` | Config definitions (Python) |
| `src/services/volatility_tracker.py` | LiveZScoreTracker |
| `src/strategies/enhanced_spike.py` | Live trading strategy |

---

*Consolidated from: MASTER_PLAN_TWO_PATHS.md, FINAL_TRADING_CONFIGS_JAN22.md, VOL_FILTER_GRID_SEARCH_FINDINGS_JAN22.md, TIME_STOP_STATISTICAL_ANALYSIS.md*
