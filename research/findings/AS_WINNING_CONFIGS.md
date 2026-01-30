# AS Strategy Winning Configurations

**Date:** January 29, 2026
**Data:** Training set (Jan 16-19), 62+ hours, 254 markets
**Base Config:** z>1.5, velocity_aligned=True, 5000ms pulling

---

## Top Configs by Total PnL

| Rank | Time Window | Max Age | Fills | Win% | Pair Cost | Total PnL | $/hr |
|------|-------------|---------|-------|------|-----------|-----------|------|
| **1** | **220-500s** | 5000ms | 420 | 65.0% | $1.038 | **$192.39** | **$15.53** |
| 2 | 250-500s | 5000ms | 405 | 65.2% | $1.038 | $191.48 | $15.46 |
| 3 | 180-500s | 5000ms | 432 | 64.6% | $1.034 | $183.08 | $14.78 |
| 4 | 200-500s | 5000ms | 425 | 64.5% | $1.036 | $181.23 | $14.63 |
| 5 | 220-450s | 5000ms | 288 | 68.4% | $1.037 | $159.41 | $12.87 |

**Recommended:** Config 1 (220-500s) - best balance of volume and accuracy.

---

## High Win Rate Configs (Best Accuracy)

| Rank | Time Window | Max Age | Fills | Win% | Pair Cost | Total PnL |
|------|-------------|---------|-------|------|-----------|-----------|
| **1** | **250-450s** | 5000ms | 272 | **68.8%** | $1.033 | $155.38 |
| 2 | 220-450s | 5000ms | 288 | 68.4% | $1.037 | $159.41 |
| 3 | 180-450s | 5000ms | 302 | 67.9% | $1.031 | $153.70 |
| 4 | 200-450s | 5000ms | 293 | 67.6% | $1.034 | $148.25 |
| 5 | 150-450s | 5000ms | 333 | 65.8% | $1.048 | $108.66 |

**Note:** Tighter windows (ending at 450s) have higher accuracy but fewer fills.

---

## Config Code Reference

### Config 1: Best Overall (220-500s)
```python
ASConfig(
    mode=StrategyMode.ASYMMETRIC_EWMA,
    entry_window_min_secs=220,
    entry_window_max_secs=500,
    max_order_age_ms=5000,
    z_threshold=1.5,
    require_velocity_aligned=True,
    max_adverse_move=0.03,
    min_entry_gap_ms=200,
    shares=10,
)
```

### Config 2: Best Accuracy (250-450s)
```python
ASConfig(
    mode=StrategyMode.ASYMMETRIC_EWMA,
    entry_window_min_secs=250,
    entry_window_max_secs=450,
    max_order_age_ms=5000,
    z_threshold=1.5,
    require_velocity_aligned=True,
    max_adverse_move=0.03,
    min_entry_gap_ms=200,
    shares=10,
)
```

---

## Pulling Speed Comparison

| Pulling Speed | Win% | Pair Cost | Total PnL | Notes |
|---------------|------|-----------|-----------|-------|
| **5000ms (slow)** | 65.0% | $1.038 | $192.39 | **Best** |
| 4000ms | 63.3% | $1.057 | $112.10 | Worse |
| 3000ms | 63.0% | $1.054 | $114.56 | Worse |

**Conclusion:** Slow pulling (5000ms) is optimal. Faster pulling reduces fills and hurts accuracy.

---

## Why These Configs Win

### Time Window Effect
| Window | Win% | Why |
|--------|------|-----|
| 600-900s | 52% | Too early, signal weak (EWMA learning) |
| 400-600s | ~58% | Transitional |
| **220-500s** | **65%** | Optimal signal strength |
| 200-400s | 70% | Best accuracy but fewer trades |
| <220s | N/A | Time stop (no entries) |

### Key Parameters
1. **Time Stop (220s min):** Avoids late-market adverse selection
2. **Time Cap (500s max):** Early signal is noisy
3. **Slow Pulling (5000ms):** Gives orders time to fill
4. **Strong Signal (z>1.5):** Higher accuracy threshold
5. **Velocity Aligned:** Confirmation filter

---

## PnL Breakdown (Config 1)

| Source | PnL | $/hr | Notes |
|--------|-----|------|-------|
| Realized (merges) | -$43.54 | -$3.52 | Pair cost > $1.00 |
| Unrealized (carry) | +$235.92 | +$19.04 | 65% on winners |
| **Total** | **+$192.39** | **+$15.53** | Carry dominates |

**Critical Insight:** Profit comes from directional carry, not spread capture.

---

## Capital Requirements

| Position Size | Without Merge | With Merge |
|---------------|---------------|------------|
| 10 shares | ~$400 | ~$150 |
| 50 shares | ~$2,000 | ~$750 |

**Merge advantage:** Faster capital recycling, more opportunities per dollar.

---

## Merging Impact Analysis

### Does Merging Change PnL?
**No.** Same final outcome:
- Merge: UP + DOWN → $1.00 now
- Hold: Winner → $1.00, Loser → $0.00 at resolution

### Does Merging = More Opportunities?
**Yes.** Merging unlocks capital faster:
- Without merge: Capital locked until resolution (~15 min)
- With merge: Capital returned immediately on pair completion
- Result: Can cycle ~3x more trades with same capital

---

## Recommendations

| Goal | Recommended Config |
|------|-------------------|
| **Max PnL** | 220-500s, 5000ms |
| **Max Accuracy** | 250-450s, 5000ms |
| **Balance** | 220-500s, 5000ms (best overall) |
| **Conservative** | 250-450s, 5000ms (higher WR, fewer trades) |

---

## OOS Validation Results (CRITICAL)

**Date:** January 29, 2026
**OOS3+4:** 42.9 hours, Jan 22-24
**OOS5:** 11.3 hours, Jan 24+

### Training vs OOS Performance

| Dataset | Strong (|z|>1.5) | Weak (0<|z|<1.5) | Gap |
|---------|------------------|------------------|-----|
| **Training** | +$15.53/hr, 65% | N/A | baseline |
| **OOS3+4** | -$7.71/hr, 44% | -$6.55/hr, 44% | -$23/hr |
| **OOS5** | -$21.45/hr, 38% | +$0.87/hr, 44% | -$37/hr to +$1/hr |

### Z-Zone Gating Comparison (OOS)

| Z-Zone | OOS3+4 $/hr | OOS5 $/hr | Win% | Verdict |
|--------|-------------|-----------|------|---------|
| **Strong** (|z| > 1.5) | -$7.71 | **-$21.45** | 38-44% | WORST - overfit |
| **Weak** (0 < |z| < 1.5) | -$6.55 | **+$0.87** | 44% | Better |
| **Medium** (0.5 < |z| < 2) | -$9.23 | **+$4.02** | 43-45% | Best on OOS5 |
| **No Filter** | -$13.71 | -$11.95 | 43-44% | Bad |

### Key Findings

1. **Training config is OVERFIT**: Strong signal (|z| > 1.5) = best on training, WORST on OOS
2. **Weak z-zone helps**: AGGRESSIVE-style filtering (0 < |z| < 1.5) reduces losses
3. **OOS5 turns positive**: With weak/medium z-zone, +$0.87 to +$4.02/hr
4. **OOS3+4 still negative**: Even with best z-zone filter, -$3.97/hr to -$6.55/hr
5. **Win% dropped 20pp**: Training 65% → OOS 43-44%

### Why This Matters

The signal that looked good on training (strong |z| > 1.5) doesn't generalize:
- Strong signal = market already moved = adverse selection
- Weak signal = less directional certainty = but also less adverse selection
- The training period may have had different regime characteristics

### Recommendation Update

| Environment | Recommended Config |
|-------------|-------------------|
| **Training** | Strong z>1.5, 220-500s |
| **OOS/Production** | Weak 0<z<1.5, 250-450s (or don't use AS) |

**CAUTION:** AS strategy shows signs of overfitting. Consider sticking with AGGRESSIVE which has consistent OOS performance.

---

*Last Updated: January 29, 2026*
