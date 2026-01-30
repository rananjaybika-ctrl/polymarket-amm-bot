# CRITICAL FINDING: Time Stop + Order Pulling for AS Strategy

**Date:** January 29, 2026
**Status:** CRITICAL - Validated
**Hourly Rate:** $18.04/hr (total), -$2.86/hr (merge only)

---

## Executive Summary

**TIME STOP + ORDER PULLING is the missing piece for AS profitability.**

Without time stop: All AS configs NEGATIVE (-$9.92 to -$1.65/hr)
With time stop (220-500s) + pulling (5000ms): **+$18.04/hr**

However, the profit comes from **DIRECTIONAL CARRY**, not from merge/pair strategy.

---

## The Winning Configuration

| Parameter | Value | Why |
|-----------|-------|-----|
| Time Window | **220-500s** | Avoids late-market adverse selection |
| Order Pulling | **5000ms** | Gives orders time to fill |
| Z-Score Threshold | **1.5** (strong) | Higher accuracy signals |
| Velocity Aligned | **True** | Confirmation filter |
| Max Adverse Move | 0.03 (3%) | Standard |
| Min Entry Gap | 200ms | Polymarket execution reality |

---

## Performance Breakdown

### With Winning Config (220-500s, 5000ms, z>1.5, vel_aligned)

| Metric | Value |
|--------|-------|
| Total PnL | **+$223.43** |
| Hourly Rate | **$18.04/hr** |
| Winner Fill Rate | **65.5%** |
| Pair Cost | **$1.031** |
| Profitable Pairs | **41.7%** |
| Merges | 115 |
| Fills | 420 |

### PnL Source Breakdown

| Source | PnL | Hourly |
|--------|-----|--------|
| Merge (pairs) | -$35.40 | **-$2.86/hr** |
| Unrealized (carry) | +$258.83 | **+$20.90/hr** |
| **Total** | **+$223.43** | **+$18.04/hr** |

---

## Why Merge is Still Negative

### Fill Prices
```
Avg UP fill price:   $0.602
Avg DOWN fill price: $0.601
Sum of fill prices:  $1.203 (expensive!)
```

### Merge Pair Costs (better than fills)
```
Avg UP cost in merges:   $0.542
Avg DOWN cost in merges: $0.489
Avg pair cost:           $1.031 (still > $1.00)
```

### Pair Distribution
| Range | Count | % | Status |
|-------|-------|---|--------|
| $0.80-$0.90 | 15 | 13% | WIN |
| $0.90-$0.95 | 11 | 10% | WIN |
| $0.95-$1.00 | 18 | 16% | WIN |
| $1.00-$1.05 | 16 | 14% | LOSS |
| $1.05-$1.10 | 21 | 18% | LOSS |
| $1.10-$1.20 | 20 | 17% | LOSS |
| $1.20-$1.50 | 10 | 9% | LOSS |

**41.7% profitable, 58.3% losing** → Net loss on merges

---

## Why Total PnL is Positive (Directional Carry)

### Unhedged Position Accuracy
- Markets with unhedged inventory: 46
- **80.4% on CORRECT (winning) side**
- 19.6% on wrong side

### Settlement Mechanics
```
Winner settles at: $1.00
Loser settles at:  $0.00

Example: 50 UP shares @ $0.60 avg cost, UP wins
Profit = (1.00 - 0.60) × 50 = $20.00
```

The strategy accumulates more winners than losers (65.5% fill accuracy) and holds them to resolution.

---

## Why Time Stop Works

### Time Window Analysis (with signal filters)

| Window | Win% | Pair Cost | Total PnL |
|--------|------|-----------|-----------|
| 200-400s | 69.7% | $1.029 | +$129.62 |
| **220-500s** | **65.5%** | **$1.031** | **+$223.43** |
| 300-600s | 60.0% | $1.055 | +$164.24 |
| 400-700s | 51.9% | $1.016 | -$119.08 |
| 500-800s | 52.2% | $1.020 | -$13.40 |
| 600-900s | 51.6% | $1.015 | -$15.37 |

**Early windows (200-500s) consistently positive, late windows negative.**

### Why Early Windows Win
1. **Less informed flow** - Early in market, fewer participants have strong opinions
2. **Cheaper prices** - Both sides more evenly priced
3. **Lower adverse selection** - Sellers don't have as much information edge
4. **Time to recover** - If wrong, market has time to move back

---

## Signal Comparison (with Time Stop)

All tested with 220-500s window, 5000ms pulling:

| Signal | Win% | Pair Cost | Total PnL |
|--------|------|-----------|-----------|
| **Strong z + vel aligned** | **65.5%** | **$1.031** | **+$223.43** |
| Velocity aligned | 65.0% | $1.038 | +$192.39 |
| Strong zscore (z>1.5) | 59.7% | $1.061 | +$22.92 |
| Baseline (no filter) | 59.7% | $1.069 | +$27.90 |
| Weak zscore (z>0.75) | 59.8% | $1.072 | +$5.01 |
| Pure Spread (symmetric) | 62.5% | $1.100 | -$52.86 |

**Strong zscore + velocity aligned is the best signal combination.**

---

## The Adverse Selection Paradox

### Without Time Stop
```
Signal accuracy: 69%
Fill accuracy:   53%  ← Adverse selection eats 16pp
Pair cost:       $1.06+ (losing)
```

### With Time Stop (220-500s)
```
Signal accuracy: 69% (same)
Fill accuracy:   65.5%  ← Time stop recovers 12pp
Pair cost:       $1.031 (almost breakeven)
```

Time stop filters out the worst adverse selection periods.

---

## Key Insight: This is DIRECTIONAL CARRY, Not Spread Capture

The winning AS configuration is essentially the **AGGRESSIVE taker strategy** with maker execution:
- Same signal (BTC velocity + zscore)
- Same time window importance
- Same directional accuracy (~65-70%)
- Different execution (limit orders vs market orders)

**The merge/pair strategy is still underwater.** Profit comes from holding winners to resolution.

---

## Implications for Strategy Development

### What Works
1. Time stop (220-500s window)
2. Strong signal filter (z>1.5 + velocity aligned)
3. Moderate order pulling (5000ms)
4. Directional carry (hold winners)

### What Doesn't Work
1. Pure spread capture (symmetric bidding)
2. Fast pulling (kills fill volume)
3. Late market entry (high adverse selection)
4. Weak signals (coin flip accuracy)

### Next Steps
1. Test multi-phase approach (accumulate cheap → signal-based skew → time stop)
2. Test fixed grid levels (Observer style) vs dynamic AS pricing
3. Consider abandoning pair strategy, focus on directional carry only

---

## Source Files

| File | Purpose |
|------|---------|
| `research/avellaneda_stoikov_backtest.py` | Main backtest code |
| `research/as_signal_discovery_results_checkpoint.csv` | Profile 15 results (without time stop) |
| `research/observer/grid_obs_20260117.csv` | Observer data |
| `~/.claude/plans/zany-bouncing-wren.md` | Working plan with full analysis |

---

## Code Reference

### Winning Config
```python
config = ASConfig(
    mode=StrategyMode.ASYMMETRIC_EWMA,
    gamma=0.1,
    base_spread=0.01,
    z_threshold=1.5,  # Strong signal
    require_velocity_aligned=True,  # Confirmation
    entry_window_min_secs=220,  # TIME STOP
    entry_window_max_secs=500,  # TIME STOP
    max_order_age_ms=5000,  # Slow pulling
    max_adverse_move=0.03,
    min_entry_gap_ms=200,
)
```

### Key Backtest Lines
- Fill condition: `avellaneda_stoikov_backtest.py:957`
- Merge calculation: `avellaneda_stoikov_backtest.py:755`
- Time window filter: `avellaneda_stoikov_backtest.py:884-890`

---

*Last Updated: January 29, 2026*
