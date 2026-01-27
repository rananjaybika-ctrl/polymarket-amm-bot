# Losing Patterns Analysis

**Last Updated:** January 25, 2026
**Source:** Analysis of training data (81.7h, Jan 16-19) and OOS3+4 (50.6h, Jan 22-24)

---

## Executive Summary

Analysis of what distinguishes winning vs losing trades in the reversal confirmation strategy. The **retracement fraction** (pullback / peak_move >= 0.30) is the best actionable discriminator, improving edge by +1.3pp.

---

## Top Discriminators (Cohen's d Effect Sizes)

| Rank | Dimension | Cohen's d | Winner Mean | Loser Mean | Actionable? |
|------|-----------|-----------|-------------|------------|-------------|
| 1 | `max_continuation_pct` | 0.565 (medium) | 0.020% | 0.087% | No (post-hoc) |
| 2 | **`retracement_frac`** | **0.359 (small)** | 0.431 | 0.347 | **YES** |
| 3 | `entry_price` | 0.316 (small) | $0.347 | $0.312 | YES |
| 4 | `pair_cost` | 0.257 (small) | 1.014 | 1.011 | Weak |
| 5 | `cheap_spread` | 0.186 (small) | 0.005 | 0.010 | Weak |

---

## Core Insight

**Losers are "brief pauses in a real trend"** - BTC continues moving after entry.

**Winners are "actual reversals"** - BTC stalls or reverses.

The strongest at-entry proxy for this distinction is **retracement fraction**: how deep the pullback is relative to the peak move.

---

## Max Continuation Analysis (Post-Hoc)

Informative but not actionable (requires knowing future):

| Max Continuation | Win Rate | PnL | Trades |
|------------------|----------|-----|--------|
| < 0.01% | **69.4%** | +$1,234 | 62 |
| 0.01 - 0.03% | 42.1% | - | - |
| 0.05 - 0.10% | 17.1% | - | - |
| > 0.10% | 10.5% | - | - |

**Insight:** When BTC continues < 0.01% after entry, we win 69.4% of the time.

---

## Actionable Filters

### Retracement Fraction

| Threshold | Trades | WR | Edge | PnL |
|-----------|--------|-----|------|-----|
| >= 0.20 | 189 | 39.3% | +6.7pp | $690 |
| **>= 0.30** | **181** | **41.9%** | **+8.0pp** | **$742** |
| >= 0.40 | 175 | 43.4% | +9.0pp | $790 |

**Recommended: >= 0.30** (0.40 shows overfit risk in OOS)

### Entry Price Floor

| Threshold | Trades | WR | Edge | PnL |
|-----------|--------|-----|------|-----|
| >= $0.15 | 189 | 40.7% | +7.1pp | $668 |
| **>= $0.20** | **179** | **42.2%** | **+8.1pp** | **$716** |
| >= $0.25 | 163 | 44.5% | +8.4pp | $690 |

**Why this works:** Very cheap entries (< $0.15) indicate strong trends that won't revert (13.3% WR).

---

## Cross-Validation Results

### Training (81.7h) vs OOS3+4 (50.6h)

| Filter Combo | Train WR | Train PnL | OOS WR | OOS PnL | Consistent? |
|--------------|----------|-----------|--------|---------|-------------|
| retrace >= 0.20 | 39.3% | $690 | 41.2% | $616 | YES |
| **retrace >= 0.30** | **41.9%** | **$742** | **41.8%** | **$592** | **YES** |
| retrace >= 0.40 | 43.4% | $790 | 43.0% | $577 | WR yes, PnL drops |
| **retrace >= 0.30, price >= $0.20** | **43.1%** | **$722** | **43.4%** | **$599** | **STRONG YES** |

---

## Final Recommended Filter

**Selection principle:** Only include filters where BOTH datasets agree on direction AND magnitude.

### Configuration

```python
# Current contrarian logic (reversal confirmation):
pullback_threshold = 0.0001  # 0.01% absolute pullback

# ADD these filters:
retracement_min = 0.30       # Pullback >= 30% of peak move
entry_price_min = 0.20       # Skip entries < $0.20

# Do NOT add (training/OOS disagree):
# - time cap
# - choppiness filter
```

### Cross-Validated Performance

| Metric | Training (81.7h) | OOS3+4 (50.6h) | Difference |
|--------|------------------|-----------------|------------|
| Trades | 181 | 152 | ~2.2-3.2/hr |
| WR | 43.1% | 43.4% | +0.3pp |
| Edge | +8.0pp | +7.9pp | -0.1pp |
| PnL | $722 | $599 | Directionally yes |

**Improvement over baseline:** +3.8pp WR, +1.3pp edge across 129 hours.

---

## What Didn't Help

### Choppiness Filter
- Individually promising (+7.8pp edge at >= 0.05)
- Hurts trade count disproportionately when combined with other filters
- OOS shows no improvement

### Max Entry Time
- Training: no improvement
- OOS: <= 300-420s helps
- **Disagreement = overfit risk**

### Entry Price Floor Alone
- Improves WR but kills PnL through fewer trades
- Works better as secondary filter with retracement

---

## Other Notable Patterns

| Factor | Finding |
|--------|---------|
| Direction | UP entries (41.8% WR) slightly better than DOWN (36.0%) |
| Velocity zone | "strong" = 54.5% WR vs "neutral" = 36.9% |
| Entry price counter-intuition | Very cheap (<$0.15) = 13.3% WR (strong trend, won't revert) |

---

## Implementation

### Filter Application

```python
def should_enter(pullback_pct, peak_move_pct, entry_price):
    # Original filter
    if pullback_pct < 0.0001:
        return False

    # Retracement filter (NEW)
    retracement_frac = pullback_pct / peak_move_pct
    if retracement_frac < 0.30:
        return False

    # Entry price filter (NEW)
    if entry_price < 0.20:
        return False

    return True
```

### Quick Commands

```bash
# Run losing patterns analysis (training data)
python research/validate_oos4_all_paths.py --training

# Run OOS validation with improved filters
python research/validate_oos4_all_paths.py --combined
```

---

*Source: HANDOVER_JAN25_LOSING_PATTERNS.md*
