# Volatility Filter Analysis

**Last Updated:** January 25, 2026

---

## Executive Summary

Grid search of 1,440 configurations confirmed that volatility filtering improves performance:

- **Best z-zone:** `0 < z < 1.5` (+52% $/hr vs no filter)
- **Best z-score method:** EWMA (adaptive, no drift)
- **Best threshold method:** OU (+30% vs EWMA threshold)

---

## Key Finding: Z-Zone 0 < z < 1.5

Skip BOTH very low volatility (z < 0) AND high volatility (z > 1.5).

| Zone | Avg $/hr | Avg Win% | Avg Trades |
|------|----------|----------|------------|
| **0 < z < 1.5** | **$0.378** | 57.7% | 78 |
| -0.5 < z < 1.5 | $0.360 | 59.3% | 95 |
| z < 1.5 | $0.314 | 61.6% | 123 |
| no_limit | $0.249 | 54.6% | 195 |

**Improvement:** +52% $/hr with optimal z-zone vs no filter.

---

## Z-Score Methods Compared

### Performance Ranking

| Method | Avg $/hr | Best $/hr | Stability (CV) |
|--------|----------|-----------|----------------|
| **EWMA** | $0.317 | $0.714 | 0.39 |
| ewma_ratio | $0.294 | $0.681 | 0.40 |
| ou | $0.307 | $0.619 | 0.39 |
| percentile | $0.288 | $0.586 | 0.42 |

**Winner: EWMA** (highest avg and best peak $/hr)

### Method Definitions

| Method | Formula | Pros | Cons |
|--------|---------|------|------|
| **EWMA** | `(log(vol) - rolling_mean) / rolling_std` | Self-calibrating, no params | Loses absolute reference |
| OU | `(log(vol) - mu) / sigma` | Absolute reference | Params go stale, drifts |
| Percentile | `norm.ppf(percentile_rank)` | Non-parametric | Slow (scipy) |
| EWMA Ratio | `(log(fast/slow) - mean) / std` | Detects vol spikes | Complex, similar to EWMA |

### Critical Discovery: OU Drift

OU z-score uses static mu=-3.9845 from in-sample fit. When BTC price level shifted in OOS3:
- EWMA adapted automatically (mu=-3.3229)
- OU did not adapt
- Result: Fewer signals, worse accuracy, excessive stop-outs

**Conclusion:** Use EWMA z-score in production (adaptive), not OU.

---

## Grid Search Results

### Top 10 Configs by $/hr (5 shares)

| Rank | Threshold | Z-Score | Lookback | SL | Cycling | Z-Zone | $/hr | Trades | Win% |
|------|-----------|---------|----------|-----|---------|--------|------|--------|------|
| 1 | ou | ewma | 1200ms | 15% | ON | 0<z<1.5 | $0.714 | 138 | 57.2% |
| 2 | ou | ewma | 1000ms | 15% | ON | 0<z<1.5 | $0.712 | 117 | 58.1% |
| 3 | ou | ewma | 1000ms | 12% | ON | 0<z<1.5 | $0.685 | 120 | 55.8% |
| 9 | ou | ou | 1400ms | 15% | OFF | 0<z<1.5 | $0.619 | 52 | 75.0% |
| 10 | ou | ou | 1400ms | 15% | ON | -0.5<z<1.5 | $0.615 | 99 | 70.7% |

### Top by Win Rate (min 50 trades)

| Threshold | Z-Score | Lookback | Cycling | Z-Zone | $/hr | Win% |
|-----------|---------|----------|---------|--------|------|------|
| ou | percentile | 1200ms | OFF | z<1.5 | $0.421 | 76.1% |
| ou | ou | 1400ms | OFF | 0<z<1.5 | $0.619 | 75.0% |
| ou | ou | 1000ms | ON | z<1.5 | $0.363 | 75.4% |

---

## Threshold Method Analysis

| Method | Avg $/hr | Best $/hr |
|--------|----------|-----------|
| **OU** | $0.341 | $0.714 |
| EWMA | $0.262 | $0.567 |

**Winner: OU threshold** (+30% better)

Note: This refers to the spike detection threshold, not the z-score filtering method.

---

## Cycling Impact

| Cycling | Avg $/hr | Best $/hr | Avg Win Rate | Trade Ratio |
|---------|----------|-----------|--------------|-------------|
| ON | $0.293 | $0.714 | 50.2% | 2.82x more |
| OFF | $0.309 | $0.619 | 64.6% | baseline |

**Finding:** Cycling ON produces 2.82x more trades but 14.4pp lower win rate.

---

## LiveZScoreTracker Implementation

Production z-score computation in `src/services/volatility_tracker.py`:

```python
from src.services.volatility_tracker import LiveZScoreTracker

# Create tracker
tracker = LiveZScoreTracker(
    method="ewma",  # or "ou", "ewma_ratio"
    z_lo=0.0,
    z_hi=1.5,
)

# On each Binance tick (O(1) per tick)
zscore = tracker.update(btc_price)

# Check if trade allowed
if tracker.should_trade():
    # Z-score is in [z_lo, z_hi]
    proceed_with_trade()

# Get regime classification
regime = tracker.get_regime()  # LOW/MEDIUM/HIGH/EXTREME
```

### Factory Functions

| Function | Method | Z-Bounds | Use Case |
|----------|--------|----------|----------|
| `create_aggressive_tracker()` | EWMA | [0, 1.5] | Max $/hr |
| `create_balanced_tracker()` | OU | [-0.5, 1.5] | High WR + good $/hr |
| `create_conservative_tracker()` | OU | [0, 1.5] | Highest WR (75%) |

---

## Bug History (Resolved)

Early grid search had critical bugs that invalidated results:

1. **Resolution PnL:** Overcounted losses by ~80% (charged pair_cost instead of winner_entry)
2. **Stop-loss logic:** Checked loser rise instead of winner drop
3. **Check order:** Triggered stop-loss before checking passive fills

After fixing, OU method confirmed as best ($2.94/hr at 50 shares, 74.6% WR).

---

## Recommended Production Settings

### AGGRESSIVE Strategy

```python
TradingConfig(
    threshold_method="ou",
    zscore_method="ewma",    # EWMA adapts, OU drifts
    lookback_ms=1200,
    stop_loss_pct=None,
    time_stop_seconds=180,
    use_cycling=True,
    z_lo=0.0,
    z_hi=1.5,
)
```

### Why EWMA Z-Score in Production

| Aspect | EWMA | OU |
|--------|------|-----|
| Calibration | None needed | Requires ou_params.json |
| Regime shift | Adapts automatically | Drifts, unreliable |
| OOS performance | Consistent | Degraded on OOS3 |
| Complexity | O(1) per tick | O(1) per tick |

---

*Consolidated from: VOL_FILTER_GRID_SEARCH_FINDINGS_JAN22.md, VOL_FILTER_FINDINGS.md, HANDOVER_JAN21_VOLATILITY_FILTER.md*
