# Plan: AGGRESSIVE_M Statistical Study + Maker Execution

## Part 1: Statistical Study (RESEARCH FIRST)

**Goal:** Understand which signals predict pullbacks that we can capture as a maker.

**Core Question:** The latency arb assumption failed (move priced in by 500-800ms). We need to find signals that predict:
1. Direction (which side wins)
2. Pullback existence (does price retrace before continuing?)
3. Pullback magnitude (how many cents does it pull back?)
4. Final move (does it move 5-8 cents in predicted direction after pullback?)

---

## Dataset Selection

### Primary: OOS3+4 (Jan 22-24) - Mixed conditions
| File | Size | Rows | Coverage |
|------|------|------|----------|
| `PROTECTED_btc_prices_oos3_oos4_combined.csv` | 600MB | ~15M | 60Hz BTC prices |
| `PROTECTED_grid_obs_oos3_oos4_combined.csv` | 221MB | 930K | Observer data |
| `market_resolutions.csv` | 18KB | 487 | Resolution outcomes |

### Additional: IS+OOS2 (Jan 16-19) - LOW volatility baseline
| File | Size | Rows | Coverage |
|------|------|------|----------|
| `btc_prices_20260118_060340.csv` | 294MB | 7.3M | 60Hz BTC (partial) |
| `PROTECTED_grid_obs_is_oos2_combined.csv` | 253MB | 1.09M | Observer data |

⚠️ **IS+OOS2 Caveat:** LOW volatility period (33% win rate observed). Use for:
- Baseline comparison (how does low vol differ?)
- Stress test (does strategy work in bad conditions?)
- Weight LOWER than OOS3+4 in combined analysis

### Validation: Feb 1-2 data - HIGH volatility
| File | Size | Rows |
|------|------|------|
| `btc_prices_20260201_123907.csv` | 1.1GB | 29M |
| `grid_obs_20260201.csv` | 156MB | 324K |
| `resolutions_20260201.csv` | 5.5KB | 75 |

### Regime Strategy
- **Report results separately by volatility regime** (LOW/MEDIUM/HIGH)
- Use volatility-adjusted offsets: `offset = base + k * current_volatility`
- Walk-forward validation: train on OOS3+4 → test on Feb 1-2

---

## Step-by-Step Statistical Analysis

### Step 1: Data Preparation

**1.1 Load and merge data:**
```python
# Load 60Hz BTC prices
btc_df = pd.read_csv('PROTECTED_btc_prices_oos3_oos4_combined.csv')

# Load observer data (already has binance_price merged)
obs_df = pd.read_csv('PROTECTED_grid_obs_oos3_oos4_combined.csv')

# Load resolutions
res_df = pd.read_csv('market_resolutions.csv')

# Merge resolution outcomes
obs_df = obs_df.merge(res_df, left_on='market_slug', right_on='slug', how='left')
```

**1.2 Compute derived signals:**
```python
# For each row, compute:
# - spike_detected (from EWMA with OU sigmoid threshold)
# - spike_magnitude
# - velocity_bps (already in data)
# - OBI (if available: up_imbalance, down_imbalance)
# - time_remaining_secs (already in data)
# - volatility regime (from velocity_zone or computed)
```

### ⚠️ OU Sigmoid Adaptive Threshold (Calibrated on IS+OOS2)

**CRITICAL:** We MUST use the same OU sigmoid threshold calibrated on IS+OOS2:

```python
# From aggressive_main_backtest.py (lines 73-79)
OU_BASE_THRESHOLD = 0.02       # Base spike threshold
OU_K_LOW = 0.5                  # Low volatility multiplier
OU_K_HIGH = 1.75                # High volatility multiplier
OU_SIGMOID_STEEPNESS = 1.5      # Sigmoid curve steepness
OU_MIN_THRESHOLD = 0.015        # Floor
OU_MAX_THRESHOLD = 0.10         # Ceiling

# Threshold calculation (from lines 150-160):
def compute_ou_adaptive_threshold(volatility, ou_params):
    log_vol = math.log(max(volatility, 1e-6))
    z_score = (log_vol - ou_params.mu) / ou_params.sigma_stat
    z_clamped = max(-10, min(10, z_score * OU_SIGMOID_STEEPNESS))
    sigmoid = 1.0 / (1.0 + math.exp(-z_clamped))
    multiplier = OU_K_LOW + (OU_K_HIGH - OU_K_LOW) * sigmoid
    threshold = OU_BASE_THRESHOLD * multiplier
    return max(OU_MIN_THRESHOLD, min(OU_MAX_THRESHOLD, threshold))
```

**Why This Matters:**
- OU params (mu, sigma_stat) were estimated from IS+OOS2 Binance data
- Threshold adapts to volatility regime (low vol → lower threshold, high vol → higher)
- Using wrong threshold = signals not comparable across datasets

### Step 2: Signal Quality Analysis

**2.1 Define signal types to test:**

### Core Signals (from current plan)
| Signal | Definition | Expected Impact |
|--------|------------|-----------------|
| `spike_only` | EWMA spike detected | Baseline |
| `spike_large` | spike_magnitude > median | Better direction? |
| `spike_small` | spike_magnitude < median | More pullback? |
| `velocity_confirms` | velocity same direction as spike | Better quality? |
| `velocity_contradicts` | velocity opposite to spike | Filter out? |
| `obi_confirms` | OBI confirms spike direction | +4pp from whale analysis |
| `obi_contrarian` | OBI opposite to spike (Baguette style) | Better for maker? |
| `high_volatility` | velocity_zone == 'high' | Higher accuracy |
| `low_volatility` | velocity_zone == 'low' | Skip these |
| `time_300_600` | 300 <= time_remaining <= 600 | Optimal window |

### ⚠️ ADDITIONAL SIGNALS (from gap analysis)
| Signal | Definition | Source | Why Important |
|--------|------------|--------|---------------|
| `acceleration_bps2` | 2nd derivative of price | acceleration_signal_backtest.py | Predicts pullback TIMING |
| `jerk_bps3` | 3rd derivative | spike_quality_analysis.py | -49% diff good/bad spikes |
| `loser_spread` | Spread on losing side | ML_SPIKE_QUALITY_ANALYSIS.md | Importance: 0.149 (TOP feature) |
| `winner_ask_depth` | Depth at winner ask | ML_SPIKE_QUALITY_ANALYSIS.md | Importance: 0.130 |
| `retracement_frac` | pullback_depth / peak_move | LOSING_PATTERNS.md | **Best discriminator** (d=0.359) |
| `momentum_60s` | Price change over 60s | maker_prediction_backtest.py | Baguette uses this |
| `enhanced_score` | Composite signal | trading_utils.py | 40% mag + 30% vel + 20% confirm |

### Interaction Effects to Test
| Interaction | Formula | Rationale |
|-------------|---------|-----------|
| `mag_x_velocity` | spike_magnitude × velocity_bps | -32% diff good/bad in ML |
| `obi_x_spread` | obi_confirms × loser_spread | High OBI + wide spread = best |
| `depth_x_time` | depth_ratio × time_remaining | Low depth + high time = better fills |

**2.2 For each signal type, measure:**

```python
for signal_type in signal_types:
    filtered_df = apply_signal_filter(obs_df, signal_type)

    results[signal_type] = {
        'n_signals': len(filtered_df),
        'direction_accuracy': (filtered_df.spike_dir == filtered_df.winner).mean(),
        'resolution_pnl': compute_resolution_pnl(filtered_df),
    }
```

### Step 3: Pullback Pattern Analysis (CRITICAL)

**3.1 Define pullback for each spike:**

After spike detected at time T, price P:
- **Pullback** = Maximum adverse move in next N seconds
- **Continuation** = Maximum favorable move after pullback
- **Net move** = Price at resolution vs price at spike

```python
def analyze_pullback(btc_df, spike_ts, spike_price, spike_dir, window_seconds=60):
    """
    Measure pullback pattern after spike.

    Returns:
        pullback_pct: How much price retraced against spike direction
        pullback_time: When max pullback occurred
        continuation_pct: How much price moved in spike direction after pullback
        final_move_pct: Net price change at window end
    """
    window_df = btc_df[(btc_df.timestamp_ms >= spike_ts) &
                        (btc_df.timestamp_ms <= spike_ts + window_seconds*1000)]

    if spike_dir == 'UP':
        # For UP spike: pullback = price drops below spike_price
        pullback_pct = (spike_price - window_df.price.min()) / spike_price * 100
        continuation_pct = (window_df.price.max() - spike_price) / spike_price * 100
    else:
        # For DOWN spike: pullback = price rises above spike_price
        pullback_pct = (window_df.price.max() - spike_price) / spike_price * 100
        continuation_pct = (spike_price - window_df.price.min()) / spike_price * 100

    return {
        'pullback_pct': pullback_pct,
        'continuation_pct': continuation_pct,
        'net_move_pct': continuation_pct - pullback_pct,
    }
```

**3.2 Pullback analysis by signal type:**

```python
for signal_type in signal_types:
    spikes = get_spikes_for_signal(obs_df, signal_type)

    pullback_stats = []
    for spike in spikes:
        pb = analyze_pullback(btc_df, spike.ts, spike.price, spike.dir)
        pullback_stats.append(pb)

    results[signal_type]['pullback'] = {
        'mean_pullback_pct': np.mean([p['pullback_pct'] for p in pullback_stats]),
        'median_pullback_pct': np.median([p['pullback_pct'] for p in pullback_stats]),
        'pullback_exists_rate': sum(1 for p in pullback_stats if p['pullback_pct'] > 0.01) / len(pullback_stats),
        'mean_continuation_pct': np.mean([p['continuation_pct'] for p in pullback_stats]),
        'reaches_5cent_rate': sum(1 for p in pullback_stats if p['continuation_pct'] >= 0.05) / len(pullback_stats),
        'reaches_8cent_rate': sum(1 for p in pullback_stats if p['continuation_pct'] >= 0.08) / len(pullback_stats),
    }
```

### Step 4: Maker Fill Simulation

**4.1 For each spike, simulate maker order:**

```python
def simulate_maker_fill(btc_df, spike_ts, spike_price, spike_dir, offset_pct, timeout_seconds):
    """
    Simulate placing maker order at spike_price - offset_pct.

    Returns:
        filled: bool - Did order fill within timeout?
        fill_time_ms: Time to fill (if filled)
        fill_price: Price at fill
    """
    target_price = spike_price * (1 - offset_pct/100) if spike_dir == 'UP' else spike_price * (1 + offset_pct/100)

    window_df = btc_df[(btc_df.timestamp_ms >= spike_ts) &
                        (btc_df.timestamp_ms <= spike_ts + timeout_seconds*1000)]

    for _, row in window_df.iterrows():
        if spike_dir == 'UP' and row.ask <= target_price:
            return True, row.timestamp_ms - spike_ts, target_price
        elif spike_dir == 'DOWN' and row.bid >= target_price:
            return True, row.timestamp_ms - spike_ts, target_price

    return False, None, None
```

**4.2 Grid search maker parameters:**

```python
OFFSET_GRID = [0.01, 0.02, 0.03, 0.05, 0.08]  # % below best ask
TIMEOUT_GRID = [5, 10, 30, 60]  # seconds

for offset in OFFSET_GRID:
    for timeout in TIMEOUT_GRID:
        fill_results = []
        for spike in all_spikes:
            filled, fill_time, fill_price = simulate_maker_fill(
                btc_df, spike.ts, spike.price, spike.dir, offset, timeout
            )
            fill_results.append({
                'filled': filled,
                'fill_time_ms': fill_time,
                'spike_dir': spike.dir,
                'winner': spike.winner,  # From resolution
            })

        results[f'offset_{offset}_timeout_{timeout}'] = {
            'fill_rate': sum(r['filled'] for r in fill_results) / len(fill_results),
            'avg_fill_time': np.mean([r['fill_time_ms'] for r in fill_results if r['filled']]),
            'direction_accuracy_if_filled': ...,
        }
```

### Step 5: Signal Combination Analysis

**5.1 Test 2-way and 3-way combinations:**

```python
SIGNAL_COMBOS = [
    # Single signals
    ['spike_only'],
    ['spike_large'],

    # 2-way combos
    ['spike_large', 'obi_confirms'],
    ['spike_large', 'obi_contrarian'],
    ['spike_large', 'velocity_confirms'],
    ['spike_large', 'high_volatility'],
    ['spike_large', 'time_300_600'],

    # 3-way combos
    ['spike_large', 'obi_contrarian', 'high_volatility'],
    ['spike_large', 'obi_contrarian', 'time_300_600'],
    ['spike_large', 'velocity_confirms', 'high_volatility'],

    # Full combo
    ['spike_large', 'obi_contrarian', 'high_volatility', 'time_300_600'],
]

for combo in SIGNAL_COMBOS:
    filtered_df = apply_all_filters(obs_df, combo)

    # Measure direction accuracy
    # Measure pullback patterns
    # Simulate maker fills
    # Calculate expected PnL
```

**5.2 Use ML for feature importance:**

```python
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier

# Extended features (including new signals from gap analysis)
X = obs_df[['spike_magnitude', 'velocity_bps', 'obi_net', 'time_remaining',
            'volatility_regime', 'pair_cost',
            # NEW from gap analysis:
            'acceleration_bps2', 'loser_spread', 'winner_ask_depth',
            'retracement_frac', 'momentum_60s']].values

# Target: Did spike direction match winner?
y = (obs_df['spike_direction'] == obs_df['winner']).astype(int).values

# Gradient Boosting (best in prior ML study: 70.1% accuracy)
gb = GradientBoostingClassifier(n_estimators=100)
gb.fit(X, y)
print("GB Feature importance:", dict(zip(feature_names, gb.feature_importances_)))

# Random forest for comparison
rf = RandomForestClassifier(n_estimators=100)
rf.fit(X, y)
```

**5.3 Survival Analysis for Time-to-Fill (NEW):**

```python
from lifelines import KaplanMeierFitter, CoxPHFitter

# Time-to-fill is RIGHT-CENSORED (timeout = censored)
# Use survival analysis, not binary classification

kmf = KaplanMeierFitter()
for signal_type in ['spike_large', 'spike_small', 'obi_confirms']:
    subset = fill_data[fill_data.signal_type == signal_type]
    kmf.fit(
        durations=subset['time_to_event'],  # Time until fill or timeout
        event_observed=subset['filled'],     # 1 if filled, 0 if timeout
        label=signal_type
    )
    kmf.plot_survival_function()

# Cox regression for feature importance on fill timing
cox = CoxPHFitter()
cox.fit(fill_data[['spike_magnitude', 'obi_net', 'loser_spread',
                   'time_to_event', 'filled']],
        duration_col='time_to_event', event_col='filled')
cox.print_summary()
```

### Step 6: Profitability Simulation

**6.1 Full backtest with maker entry:**

```python
def backtest_maker_strategy(obs_df, btc_df, config):
    """
    Full backtest with:
    - Signal combination filter
    - Dynamic offset based on signal intensity
    - Maker fill simulation
    - Hedge pricing (existing logic)
    - PnL calculation (0% entry fee)
    """
    trades = []

    for spike in get_filtered_spikes(obs_df, config.signal_combo):
        # Calculate dynamic offset
        offset = config.base_offset + spike.magnitude * config.offset_multiplier
        offset = min(offset, config.max_offset)

        # Simulate maker fill
        filled, fill_time, fill_price = simulate_maker_fill(
            btc_df, spike.ts, spike.price, spike.dir, offset, config.timeout
        )

        if not filled:
            continue

        # Calculate hedge (existing logic from AGGRESSIVE)
        loser_bid = calculate_loser_bid(fill_price, spike.magnitude)

        # Simulate hedge fill or time-stop
        # ... (reuse from aggressive_main_backtest.py)

        # Calculate PnL with 0% entry fee
        pnl = calculate_pnl_with_fees(
            fill_price, loser_fill, shares,
            is_taker_entry=False,  # MAKER
            is_taker_exit=exit_is_taker,
        )

        trades.append({...})

    return pd.DataFrame(trades)
```

---

## Expected Outputs

### 1. Signal Quality Report
```
| Signal Combo | N | Direction Acc | Pullback Rate | Avg Pullback | Reaches 5c | Reaches 8c |
|--------------|---|---------------|---------------|--------------|------------|------------|
| spike_only | 1000 | 62% | 85% | 0.03% | 45% | 32% |
| spike+obi | 600 | 68% | 80% | 0.025% | 52% | 38% |
| spike+obi+vol | 300 | 72% | 75% | 0.02% | 58% | 44% |
```

### 2. Maker Fill Analysis
```
| Offset | Timeout | Fill Rate | Avg Fill Time | Accuracy if Filled |
|--------|---------|-----------|---------------|-------------------|
| 0.01% | 10s | 75% | 3.2s | 63% |
| 0.02% | 10s | 55% | 5.1s | 67% |
| 0.03% | 30s | 60% | 8.4s | 69% |
```

### 3. Feature Importance (ML)
```
| Feature | Logistic Coef | RF Importance |
|---------|---------------|---------------|
| spike_magnitude | +0.45 | 0.28 |
| obi_contrarian | +0.32 | 0.22 |
| high_volatility | +0.28 | 0.18 |
| time_300_600 | +0.21 | 0.15 |
```

### 4. Profitability Matrix
```
| Config | Fill Rate | $/hr (Maker) | $/hr (Taker baseline) |
|--------|-----------|--------------|----------------------|
| Best combo, 0.02% offset | 60% | $X.XX | $15.35 |
```

---

## Existing Code to REUSE

| File | Reusable Component |
|------|-------------------|
| `research/backtests/aggressive_main_backtest.py` | Spike detection (`precompute_spikes_ewma`), cycling logic, dataset loading |
| `research/analysis/maker_fill_analysis.py` | Maker fill timing framework, time windows |
| `src/core/trading_utils.py` | `calculate_pnl_with_fees()`, `obi_confirms_spike()`, `velocity_confirms_spike()`, `compute_enhanced_score()` |
| `research/ml/spike_quality_analysis.py` | Feature extraction including order book depth |
| `research/backtests/acceleration_signal_backtest.py` | Acceleration signal computation |
| `research/findings/LOSING_PATTERNS.md` | Retracement fraction methodology |

---

## Files to MODIFY (NOT create new)

### 1. `research/backtests/aggressive_main_backtest.py`

**Modifications:**
- Add `ENTRY_MODE = "MAKER"` or `"TAKER"` flag
- Add maker fill simulation logic (instead of taker delay)
- Add pullback offset parameters
- Add fill timeout parameter
- Add new signals: acceleration, loser_spread, retracement_frac
- Change fee calculation to `is_taker_entry=False` when MAKER mode
- Add adverse selection tracking

### 2. `research/optimizers/aggressive_grid_search.py`

**Modifications:**
- Add `entry_mode` to grid params: `['TAKER', 'MAKER']`
- Add maker-specific params to grid:
  - `pullback_offset`: [0.01, 0.02, 0.03, 0.05]
  - `fill_timeout_seconds`: [10, 30, 60]
- Keep existing spike detection, OBI filter, time-stop params
- Output separate results by entry_mode for comparison

### 3. `research/analysis/maker_fill_analysis.py` (if exists)

**Modifications:**
- Add offset grid testing
- Add survival analysis (Kaplan-Meier)
- Add adverse selection measurement

### Key Changes in `aggressive_main_backtest.py`:

```python
# NEW: Entry mode flag
ENTRY_MODE = "MAKER"  # or "TAKER"
PULLBACK_OFFSET = 0.02  # 2 cents below best ask
FILL_TIMEOUT_SECONDS = 30

# In simulation loop:
if ENTRY_MODE == "MAKER":
    # Post limit order at pullback offset
    maker_limit_price = best_ask - PULLBACK_OFFSET
    filled, fill_time, fill_price = simulate_maker_fill(
        btc_df, spike_ts, best_ask, spike_dir, PULLBACK_OFFSET, FILL_TIMEOUT_SECONDS
    )
    if not filled:
        continue  # Skip this spike
    winner_entry = fill_price
    entry_fee = 0  # MAKER = 0% fee
else:  # TAKER
    # Existing logic: take at best ask with delay
    winner_entry = delayed_row['up_ask' if spike_dir == 'UP' else 'down_ask']
    entry_fee = polymarket_taker_fee(winner_entry)
```

---

## ⚠️ CRITICAL RISKS (from gap analysis)

### 1. Adverse Selection Risk (HIGH)
**Problem:** When your maker order FILLS, it may be because informed traders know something you don't.
- Fill = price crossed your level = momentum AGAINST you
- Unfilled = price didn't reach = you missed but avoided bad trade

**Analysis Needed:**
```python
# Compare win rate WHEN FILLED vs overall
win_rate_if_filled = (filled_trades.spike_dir == filled_trades.winner).mean()
win_rate_overall = (all_spikes.spike_dir == all_spikes.winner).mean()
adverse_selection_cost = win_rate_overall - win_rate_if_filled
```

### 2. Fill Rate Assumption (HIGH)
**Problem:** Plan assumes 70% fill rate but this is NOT empirically validated.

**Must Measure:**
- Actual fill rate by offset level
- Fill rate by signal type (strong signals may have LOWER fill rate)
- Fill rate by volatility regime

### 3. Queue Position Effects (MEDIUM)
**Problem:** Backtest assumes fill at price touch, but:
- Earlier orders at same price fill first
- You're not first in queue
- Realistic fill requires price to CROSS your level, not just touch

**Mitigation:** Use conservative fill assumption (price must go 0.5-1 cent past limit)

### 4. Partial Fills (MEDIUM)
**Problem:** Backtest assumes full 50-share fills, reality may have liquidity issues.

---

## Verification

1. [ ] OOS3+4 data loads correctly (930K obs rows, 15M BTC rows)
2. [ ] IS+OOS2 data loads and is flagged as LOW volatility
3. [ ] **OU sigmoid threshold uses calibrated params** (mu, sigma_stat from IS+OOS2)
4. [ ] Resolutions match market slugs (~400+ matches)
5. [ ] Spike detection matches existing EWMA + OU threshold logic
6. [ ] Pullback measurements are reasonable (0.01-0.10% typical)
7. [ ] Fill simulation produces realistic fill rates (40-80% range)
8. [ ] **NEW:** Adverse selection measured (win rate if filled vs overall)
9. [ ] ML feature importance is interpretable
10. [ ] Results reported SEPARATELY by volatility regime
11. [ ] Validate on Feb 1-2 data (different regime)

---

## Alternative Strategies to Consider (from gap analysis)

### 1. Hybrid Maker + Taker Fallback
```
Post maker order at offset
Wait X seconds
If no fill → take at market (taker)
```
**Evaluate:** What is expected cost of missed trades vs taker fee?

### 2. Dynamic Offset Based on Order Book
Instead of spike magnitude:
- When `winner_ask_depth` LOW → smaller offset (faster fill needed)
- When `winner_ask_depth` HIGH → larger offset (more room for pullback)

### 3. Probabilistic Entry Sizing
- High confidence signals → larger size at tight offset
- Low confidence signals → smaller size at wider offset

---

## Part 2: Implementation (AFTER RESEARCH)

After statistical study completes and we know:
- Which signal combinations work best
- What pullback offsets are realistic
- Expected fill rates and accuracy
- **Adverse selection cost** (win rate if filled vs overall)
- **Regime-specific performance** (LOW vs HIGH volatility)

Then MODIFY existing files:
1. **`aggressive_main_backtest.py`** - Add MAKER entry mode alongside TAKER
2. **`aggressive_grid_search.py`** - Add maker params to grid
3. **`TRADING_CONFIGS.py`** - Add AGGRESSIVE_M config with optimal maker params

---

## Timeline

| Phase | Task | Duration |
|-------|------|----------|
| 1 | Create statistical study script | 1-2 hours |
| 2 | Run on OOS3+4 | 30 min |
| 3 | Analyze results | 30 min |
| 4 | Validate on Feb 1-2 | 30 min |
| 5 | Create backtest if promising | 1 hour |

**Research first, implement second.**
