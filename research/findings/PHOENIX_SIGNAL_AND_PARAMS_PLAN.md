# PHOENIX Signal & Parameter Optimization Plan

**Created**: Feb 19, 2026
**Status**: In Progress — Signal decision complete, parameter sweep next

---

## Decision 1: Signal — COMPLETE

**Winner: REGIME_x1.0 (fast/slow vol crossover)**

| Method | Avg $/hr | Trades | WR% | Hedge% | Spikes |
|--------|----------|--------|-----|--------|--------|
| **OU** (baseline) | $3.59 | 601 | 95.5% | 56.1% | 253K |
| REGIME_x0.5 | $3.38 | 689 | 93.0% | 59.5% | 1.05M |
| REGIME_x0.75 | $2.85 | 618 | 92.8% | 59.5% | 482K |
| **REGIME_x1.0** | **$3.72** | 552 | 95.2% | 57.1% | 227K |
| REGIME_x1.25 | $2.22 | 391 | 95.6% | 52.8% | 116K |
| REGIME_x1.5 | $1.81 | 262 | 97.2% | 49.4% | 64K |
| REGIME_x2.0 | $1.52 | 137 | 98.5% | 41.9% | 23K |

**Per-dataset: OU wins on calibration data, REGIME wins on newer data:**
- OU wins: IS+OOS2, OOS3+4, OOS7 (Jan — OU's calibration period)
- REGIME wins: OOS8, OOS9, OOS10 (late Jan / Feb — where OU goes stale)
- OOS8 delta: +$2.42/hr for REGIME (biggest swing — Jan 31 high-vol day)

**Rationale:**
- +3.6% avg $/hr over OU ($3.72 vs $3.59)
- Zero fitted parameters — fully self-calibrating
- Better on out-of-sample data (the data that matters for live)
- Similar spike count to OU (227K vs 253K) — not too loose or tight

**Implementation:**
- RegimeThreshold: fast_window=300 ticks (~5s), slow_window=3600 ticks (~60s)
- Regimes: CALM (<0.5 ratio) → 0.008%, NORMAL (0.5-1.5) → 0.015%, ACTIVE (1.5-3.0) → 0.025%, SPIKE (>3.0) → 0.050%
- Scale factor: 1.0 (no scaling, use base thresholds directly)
- Source: `research/strategies/adaptive_threshold.py` RegimeThreshold class (line 542)
- Backtest: `research/backtests/phoenix_threshold_comparison.py`
- Results: `research/findings/data/phoenix_threshold_comparison.csv`

---

## Key Finding: Signal Is NOT the Problem

OU vs REGIME: only $0.13/hr difference. Both have ~56% hedge rate.
The signal determines WHEN to enter. It does NOT affect hedging at all.
**The real damage is in the entry/hedge parameters.**

---

## Decision 2: Expensive Threshold + Pair Cost — NEXT

### The Core Problem

Current config: expensive_threshold=$0.80, max_pair_cost=$0.96
Result: **56% hedge rate = 44% unhedged. VIOLATES mandatory <20% target.**

### Why PC99 Is Not The Answer

PC99 gives $0.01/pair × 25 shares = $0.25/trade. Too thin.
600 trades fully hedged at PC99 = $150 total. Barely covers a single bad unhedged loss.
We need MEANINGFUL profit per hedged pair, not just hedge rate.

### The Better Lever: Lower the Threshold at PC96

PC96 keeps $0.04/pair × 25 shares = $1.00/trade (4x more than PC99).
The problem is hedge room. At $0.80 entry, max hedge bid = $0.16. Cheap side at $0.20 needs a $0.04 drop (20%).

**Lower entry = more hedge room at SAME PC96:**

| Entry | Max Hedge Bid (PC96) | Cheap Ask | Drop Needed | Drop % |
|-------|---------------------|-----------|-------------|--------|
| $0.80 | $0.16 | ~$0.20 | $0.04 | 20% |
| $0.75 | $0.21 | ~$0.25 | $0.04 | 16% |
| $0.70 | $0.26 | ~$0.30 | $0.04 | 13% |
| $0.65 | $0.31 | ~$0.35 | $0.04 | 11% |

At $0.70 entry: hedge bid = $0.26, cheap side only needs to drop 13% to fill.
Profit per hedged pair STILL = $0.04 = $1.00/trade. Same as $0.80 entry.

**Trade-off:** Lower WR. At $0.70, maybe only 75-80% WR. But if hedge rate rises from 56% to 85%+, the unhedged fraction is so small that lower WR barely matters.

**Quick EV comparison (assuming live-realistic 77% WR at $0.80):**
- T80/PC96: 56% hedged × $1.00 + 44% × (0.77×$5 - 0.23×$20) = $0.56 + 0.44×(-$0.75) = **$0.23/trade**
- T75/PC96 (est 85% hedged, 72% WR): 85% × $1.00 + 15% × (0.72×$6.25 - 0.28×$18.75) = $0.85 + 0.15×(-$0.75) = **$0.74/trade** (3.2x better)
- T70/PC96 (est 90% hedged, 68% WR): 90% × $1.00 + 10% × (0.68×$7.50 - 0.32×$17.50) = $0.90 + 0.10×(-$0.50) = **$0.85/trade** (3.7x better)

**NOTE:** WR estimates above are speculative. Must be verified by backtest.

### Also Test: PC97 as Middle Ground

PC97 gives $0.03/pair × 25 = $0.75/trade (still 3x PC99).
At $0.75 entry: max hedge bid = $0.22 (vs $0.21 at PC96). Slightly more room.
At $0.80 entry: max hedge bid = $0.17 (vs $0.16 at PC96). Marginal help.

### Proposed 2D Sweep

Lock: REGIME_x1.0 signal, entry_window=300-120s, base_shares=25, cooldown=10s

| Parameter | Values |
|-----------|--------|
| expensive_threshold | [0.65, 0.70, 0.75, 0.80, 0.85] |
| max_pair_cost | [0.96, 0.97, 0.98, 0.99] |

5 thresholds × 4 pair costs × 6 datasets = **120 runs**

Key metrics to track:
1. **hedge_rate** (must be > 80%, target > 90%)
2. **unhedged_pct** (must be < 20%)
3. **pnl_per_hr** (higher is better)
4. **win_rate** (informational — matters less if hedged)
5. **trades** (need enough volume)
6. **avg_pair_cost** (actual vs max)

### Success Criteria

The winning config must satisfy ALL of:
- unhedged_pct < 20% (mandatory)
- hedge_rate > 80% (mandatory)
- pnl_per_hr > $2.00 (must beat safe alternatives)
- Consistent across LODO folds (not overfit to one dataset)

---

## Decision 3: Entry Window — LATER

Current: 300-120s (5-2 min before resolution).
Less impactful than threshold/PC. Test after Decision 2 is locked.
Candidates: [420-120, 300-120, 300-90, 240-120] (wider/narrower variants).

---

## Dependency Graph

```
Signal (REGIME_x1.0) ← DONE
    ↓
Threshold × PairCost (2D sweep) ← NEXT
    ↓
Entry Window (1D sweep) ← AFTER
    ↓
Deploy to live paper trading ← FINAL
```

One wrong decision ruins all future paths.
Each decision constrains the next optimization space.
Test in order. Do not skip.

---

## Deep Reasoning: Why the Signal Is the Least Important Decision

The Polymarket expensive_ask ALREADY encodes BTC movement. If ask = $0.85, the market
is pricing 85% probability. The BTC spike is a timing gate that determines WHEN to place
orders, not WHETHER we have edge.

Edge comes from: (1) expensive side wins >80% of the time, (2) maker fills at 0% fee,
(3) hedging locks in profit. The spike just determines entry timing within the window.

OU vs REGIME: $0.13/hr difference.
PC96 vs better hedging: potentially $0.50+/hr difference.
The hedging parameters are 4x more impactful than signal choice.

---

## Live Bot vs Backtest Mismatch (IMPORTANT)

Live bot (TRADING_CONFIGS.py): patient_hedge=True, patient_bid=$0.04
Backtest (phoenix_threshold_comparison.py): patient_hedge=False

Patient hedge at $0.04 requires cheap side to drop from ~$0.20 to $0.04 (80% drop!).
Non-patient hedge at PC96 only needs drop from $0.20 to $0.16 (20% drop).

**Patient hedge is MUCH worse for hedge rate than max_pair_cost hedge.**
Live bot hedge rate is likely even worse than backtest's 56%.

After the 2D sweep identifies the optimal threshold/PC, the live bot config must
switch to patient_hedge=False with the winning max_pair_cost.
