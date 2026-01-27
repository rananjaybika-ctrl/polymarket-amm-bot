# Analysis: OU Recalibration & Path 1/Path 2 Strategy Mapping

**Date:** January 23, 2026
**Status:** Research context — no code changes needed

---

## Question 1: Should We Recalibrate OU on All Data?

### How OU Threshold Works for Spike Detection

The OU spike detection (used by AGGRESSIVE only) computes:
```python
log_vol = math.log(current_volatility)  # percentage returns
z_score = (log_vol - mu) / sigma_stat   # mu=-3.98, sigma=0.388
threshold = 0.02 * sigmoid(z_score)     # range [0.015, 0.10]
```

It compares current log-volatility against the **historical distribution** (mu, sigma).
The threshold scales via sigmoid: low-vol periods -> lower threshold (more sensitive),
high-vol periods -> higher threshold (less sensitive).

### Answer: Recalibration is Unnecessary and Potentially Harmful

**Why it doesn't help:**
1. OU params operate on **log percentage returns** — scale-invariant to BTC price level
2. AGGRESSIVE already works well with current params (70% WR on BOTH IS and OOS3)
3. The OU z-score filter is RETIRED (both configs use EWMA z-score now)
4. OU params only affect AGGRESSIVE's spike detection threshold

**Why it could harm:**
- Training (Jan 16-20): BTC $91K-95K, 4.63% range, occasional large moves
- OOS3 (Jan 22-23): BTC $88K-90K, 2.04% range, more consistent micro-chop
- Mixing these regimes shifts mu toward a compromise that fits NEITHER period optimally
- Classic overfit: tuning a static parameter to heterogeneous data

**The real lesson:** The EWMA threshold adapts to regime changes automatically.
The OU threshold works despite static params because spike detection is
fundamentally about "was THIS move significant relative to recent context" —
and the sigmoid mapping is forgiving enough to work across conditions.

---

## Question 2: Do BALANCED+EWMA and AGGRESSIVE Map to Path 1 and Path 2?

### Original Path Definitions (from spike_param_optimizer.py)

| | Path 1: Volume | Path 2: Quality |
|---|---------------|-----------------|
| **Philosophy** | Many signals, quick in/out | Few signals, asymmetric R:R |
| **Lookbacks** | 1000ms, 1200ms, 1400ms | 300ms, 400ms, 500ms (+1400ms) |
| **Key Feature** | Entry order pulling (3-30s) | Partial hedge (25-100%) |
| **Hedge** | Always 100% | Variable (25-100%) |
| **Cycling** | 1, 2, 3, 6 per market | 1, 2 per market |
| **Stop** | All types tested | Required for partial hedge |
| **Risk Profile** | Low (full hedge) | Higher (unhedged T2 portion) |

### Current Config Technical Parameters

| | AGGRESSIVE | BALANCED+EWMA |
|---|-----------|---------------|
| **Lookback** | 1200ms (Path 1 range) | 1400ms (Path 1 range) |
| **Hedge** | 100% | 100% |
| **Stop** | 180s time-stop | 15% price-stop |
| **Cycling** | Unlimited | Unlimited |
| **Partial hedge** | No | No |
| **Entry pulling** | No | No |

### Philosophical Mapping

| | Path 1 Philosophy | Current Config |
|---|-------------------|---------------|
| **Volume** | Trade often, profit from frequency | **BALANCED+EWMA** (388 trades, 50.8% WR) |
| **Quality** | Trade selectively, profit from accuracy | **AGGRESSIVE** (201 trades, 65.2% WR) |

**The intuition is correct on PHILOSOPHY but not on TECHNICAL FEATURES:**

- **BALANCED+EWMA = Volume path**: Many trades, lower WR, profits from high turnover
- **AGGRESSIVE = Quality path**: Fewer trades, higher WR, profits from selectivity

But NEITHER config uses the original Path 1/Path 2 unique features:
- No entry order pulling (Path 1)
- No partial hedge or aggressive hedge timeout (Path 2)
- Both use long lookbacks (Path 1's 1000-1400ms range)
- Both use full 100% hedging

### What Actually Creates the Volume vs Quality Difference

It's NOT the lookback or hedge approach. It's these 3 parameters:

1. **Spike detection threshold** (EWMA fires 2x more than OU -> more entries)
2. **Z-zone width** (-0.5<z<1.5 admits 59% vs 0<z<1.5 admits 37% -> more opportunities)
3. **Stop type** (price-stop exits faster -> quicker cycling -> more trades)

These combine multiplicatively: ~2x more spikes x ~1.6x wider zone x faster cycling = 388 vs 201 trades.

---

## Conclusion: Strategy Positioning

### Correct Mental Model

```
AGGRESSIVE = "Quality-first volume strategy"
  - Selective entry (OU threshold, narrow z-zone)
  - Patient exit (time-stop lets winners ride)
  - Consistent 65-70% WR across regimes

BALANCED+EWMA = "Volume-first growth strategy"
  - Liberal entry (EWMA threshold, wide z-zone)
  - Quick exit (price-stop, enables fast cycling)
  - Regime-dependent: 49% WR (IS) vs 58% WR (OOS3)
```

### Recommendation

- **AGGRESSIVE as PRIMARY** — reliable, consistent, lower variance
- **BALANCED+EWMA as EXPERIMENTAL** — higher ceiling but unproven stability
- **Do NOT recalibrate OU** — unnecessary, risks overfitting
- **Collect more OOS data** before elevating BALANCED+EWMA to primary

---

## Path 2: Timeline & Why It Was Overshadowed

### Jan 18: Path 2 DID Run (12,512 configs on 35.78 hours)

```
Path 2 Grid Search Results:
- Lookbacks tested: 300ms, 400ms, 500ms, 1400ms
- Hedge ratios: 25%, 50%, 75%, 100%
- Aggressive hedge timeouts: None, 5s, 10s, 15s
- Total configs: 12,512

Best result: $0.498/hr (1400ms, 25% hedge, 3% stop, 13 trades)
Best short-lookback: $0.155/hr (300ms, 25% hedge, 30 trades, 100% WR — tiny sample noise)
```

### Why Path 2 Results Were Weak

1. **Aggressive hedge timeout had ZERO effect** — identical results with None/5/10/15s.
   Passive fills were already happening quickly or trades stopped out first.
2. **Partial hedge (25%) beat full hedge (100%)**: avg $0.045/hr vs -$0.066/hr.
   Lower hedge costs = more profit when direction is right. But on 13 trades, this is noise.
3. **Short lookbacks (300-500ms) produced fewer signals**: 98 signals vs 352 for Path 1.
   On 35.78 hours, this means <3 signals/hour — too few to be statistically meaningful.

### Jan 20-22: Volatility Filter Discovery Made Path 1 Dominant

The volatility filter + z-score framework transformed Path 1:
- Before z-score: ~$0.44/hr (35.78h dataset, quick sanity check)
- After z-score + 81.71h grid search: **$6-9/hr** (1440 configs)

This 15-20x improvement made Path 2's $0.50/hr results irrelevant.

### Jan 22: Path 2 Explicitly Marked "NOT STARTED"

The HANDOVER_JAN22 correctly noted that Path 2 was never tested with:
- The volatility filter (EWMA z-score regime filtering)
- The corrected hedge pricing formula (v2)
- The larger 81.71h dataset
- Cycling with proper exit logic

### Current Status: Path 2 is Untested with Modern Framework

The original Path 2 run used:
- Old hedge pricing formula (0.68 * spike + 0.01 — severely wrong)
- No z-score filtering
- 35.78h dataset (vs 160h now available)
- buycount=1 (no cycling)
- Static OU threshold only

**Path 2 has NEVER been tested with the tools that made Path 1 work.**

---

## Key Takeaways for Master Plan

1. **AGGRESSIVE = evolved Path 1** (volume, OU threshold, time-stop, consistent)
2. **BALANCED+EWMA = aggressive Path 1 variant** (more volume, EWMA threshold, price-stop, regime-dependent)
3. **Original Path 2 (short lookbacks + partial hedge) = completely untested** with modern framework
4. **OU recalibration = unnecessary** for spike detection (scale-invariant, already working)

### OOS4 Conclusion (Jan 24, 2026)

OOS4 validation confirmed:
- **AGGRESSIVE**: Stable at $16.72/hr @50sh, 72.4% dir acc (consistent across IS/OOS3/OOS4)
- **BALANCED+EWMA**: Regressed to $11.17/hr (from $26.38/hr OOS3) — regime-dependent as suspected
- **Old Path 2 (partial hedge)**: Code and data DELETED. Never produced viable results.
- **New Path 2 = CONTRARIAN**: $618/hr @2500sh, 42% WR, validated independently

Path 2 partial hedge experiment is permanently closed. The OU recalibration analysis stands: no recalibration needed, AGGRESSIVE is stable.

---

## File References (Sources for this Analysis)

### Path 2 Definition & Design
| File | What it tells us |
|------|-----------------|
| `research/MASTER_PLAN_TWO_PATHS.md` (lines 192-218) | Original Path 2 spec: 300-600ms lookbacks, partial hedge, aggressive hedge timeout |
| `research/spike_param_optimizer.py` (lines 303-329) | Path 2 grid search config generation |

### Path 2 Execution & Results
| File | What it tells us |
|------|-----------------|
| `research/HANDOVER_JAN18.md` (lines 17-27) | Path 2 ran overnight Jan 18: ~12,000 configs |
| `research/path2_results_oos.csv` (12,512 rows) | Full Path 2 grid search output |
| `research/path1_results_oos.csv` (8,640 rows) | Path 1 comparison |

### Path 2 Status After Jan 18
| File | What it tells us |
|------|-----------------|
| `research/HANDOVER_JAN22_ADAPTIVE_CONFIGS.md` (lines 35-51) | States "Path 2: NOT STARTED" with new framework |
| `research/HANDOVER_JAN21_VOLATILITY_FILTER.md` | No mention of Path 2 — session focused on volatility filter |

### Why Path 1 Became Dominant
| File | What it tells us |
|------|-----------------|
| `research/VOL_FILTER_GRID_SEARCH_FINDINGS_JAN22.md` | 1440-config grid search -> Path 1 jumped to $6-9/hr |
| `research/FINAL_TRADING_CONFIGS_JAN22.md` | Three production configs (all Path 1 style) |
| `research/volatility_filter_analysis.py` | The framework that made Path 1 work |

### OU Parameters
| File | What it tells us |
|------|-----------------|
| `research/ou_params.json` | Static OU params: mu=-3.98, sigma=0.388 |
| `research/volatility_filter_analysis.py` (lines 213-231) | OU threshold sigmoid mapping |
| `research/volatility_filter_analysis.py` (lines 164-204) | EWMA threshold adaptive logic |

### Current Strategy Configs
| File | What it tells us |
|------|-----------------|
| `research/TRADING_CONFIGS.py` | AGGRESSIVE and BALANCED definitions |
| `research/validate_oos3.py` | OOS3 validation runner |
| `research/oos3_validation_results.csv` | Corrected OOS3 results |
