# PHOENIX Signal Specification — Session 2 Output

**Date:** February 17, 2026
**Purpose:** Complete signal specification for Session 3 (Strategy Architecture + Backtest)

---

## Signal Summary

| Signal | Role | Evidence | Required? |
|--------|------|----------|-----------|
| `expensive_ask > threshold` | **Bias formation** | 86% WR wallet, 87.6% backtest at $0.75 | YES — primary |
| `spike_detected` | **Entry trigger** | 88.9% at $0.65+ (Test 5), original FADE trigger | YES |
| `deceleration` | **Entry filter** | 92.1% at $0.65+ (p=0.043), 95.3% at $0.75+ (p=0.075) | OPTIONAL — boosts accuracy +12pp when present |
| `hour_of_day` | **Skip filter** | +$1,148 PnL in FADE backtest | YES |
| `session_stop (ADAPT25)` | **Risk mgmt** | 0/90 FADE configs profitable without it | YES |
| Maker execution | **Execution** | 0% fees vs 1.56% taker | YES |

### Dropped Signals (Session 1-2 evidence)

| Signal | Why Dropped |
|--------|------------|
| ML model bias | -0.7% vs simple heuristic (83.0% vs 83.7%) |
| OBI contrarian | p=0.50, 0/3 datasets significant |
| Velocity binary filter | -3pp in FADE backtest (83.7%→80.7%) |
| Acceleration direction | p=0.70, no signal |
| Acceleration reversals | p=1.0, no signal |
| Jerk direction | p=0.34, no signal |
| Velocity magnitude | p=0.33, no signal |
| Velocity direction | p=0.57, trend only |

---

## Bias Formation

**Method:** Simple price heuristic
```
expensive_ask = max(up_ask, down_ask)
cheap_ask = min(up_ask, down_ask)
expensive_side = "UP" if up_ask > down_ask else "DOWN"

bias = expensive_side  # bet that expensive side wins
```

**Threshold grid search:** [0.65, 0.70, 0.75, 0.80]
- $0.65: 83.3% baseline, higher trade count
- $0.75: 86.3% baseline, moderate count
- $0.80: 90.6% baseline, lower count

**Time window:** Evaluate at 300-600s remaining (proven accuracy window)

---

## Entry Signal

### Required conditions (ALL must be true):

1. **Bias is formed:** `expensive_ask >= threshold` (grid search: 0.65-0.80)
2. **Time window:** `time_remaining_secs` in `[entry_start, entry_end]` (grid search: 120-600s)
3. **Hour filter:** Skip UTC hours {3, 4, 8, 14, 20} (from FADE analysis)

### Entry execution:
4. **Maker bid on expensive side:** Place limit order at `expensive_ask - offset`
   - Offset grid search: [0.01, 0.02, 0.03, 0.04]
   - Fill: price-touch when `ask <= our_bid` (0ms delay, 0% fee)
   - Timeout: if not filled by `min_time_remaining`, cancel

### Optional booster (grid search on/off):
5. **Deceleration filter:** Only enter if `|velocity|` in second half of window drops >30% vs first half
   - When active: +12pp accuracy boost
   - When active: fewer trades (decel occurs in ~30% of markets at $0.75+)
   - Grid search: decel_required = [True, False]

---

## Hedge Signal

After entry fill on expensive side:

1. **Place maker bid on cheap side** at `cheap_ask - hedge_offset`
   - hedge_offset grid search: [0.01, 0.02, 0.03]

2. **Hard limit:** `entry_price + hedge_bid <= max_pair_cost`
   - `max_pair_cost` grid search: [0.96, 0.97, 0.98]
   - If hedge price would violate limit → skip hedge, stay directional

3. **Fill:** price-touch when cheap side `ask <= our_bid` (0ms, 0% fee)

4. **If hedged:** Guaranteed profit = `$1.00 - pair_cost` per pair
   **If not hedged:** Directional exposure, hold to resolution

---

## Position Sizing

```
max_per_market = balance * MAX_CAPITAL_FRACTION  # 0.50
base_shares = max_per_market / expensive_ask

# If unhedged: cap at lower fraction
unhedged_max = balance * UNHEDGED_FRACTION  # 0.25
```

Grid search: `base_shares` = [5, 10, 15, 20]

---

## Double-Down (Optional)

If unhedged position is losing (bias still valid):
1. Buy more on same side at current (cheaper) price
2. Attempt hedge on enlarged position
3. Cap: 1 double-down per market
4. Total exposure still < `MAX_CAPITAL_FRACTION`

Grid search: `double_down_enabled` = [True, False]

---

## Session Stop (ADAPT25)

From validated FADE config:
```
After N trades (grid: [15, 20, 25]):
    if cumulative_pnl < session_stop_threshold (grid: [-3, -5, -7]):
        enable max_drawdown stop (grid: [15%, 20%, 25%])
        if drawdown exceeds limit → stop trading for session
```

---

## Risk Parameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Starting capital | $170 | Current balance |
| Max per market | 50% of current balance | Dynamic sizing |
| Max unhedged | 25% of balance | Half of max for directional |
| Pair cost hard limit | Grid: $0.96-$0.98 | Guaranteed profit per pair |
| Entry ceiling | None (buying expensive side) | FADE-style, price > $0.65 |
| Per-trade stop loss | None | Hold to resolution |
| Session stop | ADAPT25 | Proven essential |

---

## Grid Search Parameters (Session 4)

```python
GRID = {
    # Bias
    "expensive_threshold": [0.65, 0.70, 0.75, 0.80],

    # Entry timing
    "entry_start_secs": [600, 480, 300],      # start of entry window
    "entry_end_secs": [180, 120],              # end of entry window

    # Entry execution
    "entry_offset": [0.01, 0.02, 0.03],       # maker bid offset from ask

    # Deceleration filter
    "decel_required": [True, False],

    # Hedge
    "hedge_offset": [0.01, 0.02, 0.03],
    "max_pair_cost": [0.96, 0.97, 0.98],

    # Sizing
    "base_shares": [5, 10, 15],

    # Double-down
    "double_down_enabled": [True, False],

    # Session stop
    "adapt_trades": [20, 25],
    "adapt_threshold": [-5.0],
    "max_drawdown_pct": [0.20],
}

# Total configs: 4 * 3 * 2 * 3 * 2 * 3 * 3 * 3 * 2 * 2 * 1 * 1 = 15,552
# After screening top ~100 by quick sim
```

---

## Chainlink Basis Risk Note

Chainlink oracle strike varies $50-150 from Binance ~5% of the time. This is structural — our Binance-based signals will be wrong ~5% of the time due to resolution oracle mismatch. This is the cost of real-time data speed and cannot be filtered.

---

## Dataset Split (Session 4)

| Set | Datasets | Purpose |
|-----|----------|---------|
| Training (80%) | IS+OOS2, OOS7, OOS8, OOS9 | Grid search optimization |
| Validation (20%) | OOS3+4 | Final holdout test |

---

## Files Referenced

| File | Purpose |
|------|---------|
| `research/findings/PHOENIX_SESSION1_FINDINGS.md` | Session 1 findings |
| `research/backtests/accel_velocity_signal_test.py` | Signal test script |
| `research/backtests/obi_contrarian_test.py` | OBI test (dropped) |
| `research/findings/data/accel_velocity_test_results.csv` | Raw test data |

*Session 2 Complete — Ready for Session 3: Strategy Architecture + Backtest*
