# Project PHOENIX — Session 1 Findings: Intelligence Gathering

**Date:** February 17, 2026 (updated)
**Files analyzed:** Wallet 0xEd0C trades (151 raw, 65 buy-only) + ML Pipeline (3.3M rows, 726 markets)

---

## Part A: Wallet Trade Analysis (0xEd0C — User's Manual Trading)

### Filtering Pipeline
- **Raw trades:** 151 (all API trades from Nov 28 – Dec 8, 2025)
- **After gambling filter** (imbalance > 3:1): 120 trades across 54 markets
- **After sell-market filter** (remove any market with sell orders): **65 trades across 30 markets**
- Sell-market filter rationale: user posted limit orders (maker), sell trades represent exits/position management that don't reflect the core entry strategy

### Overview (Buy-Only Markets)
- **Period:** Nov 28 – Dec 8, 2025 (~11 days)
- **Total trades:** 65 buys across **30 markets**
- **Market types:** 22 BTC 15m, others
- **Total PnL:** **$105.77** across 29 resolved markets
- **Win rate:** **65.5%** (19 wins, 10 losses)
- **Avg PnL/market:** $3.65

### Key Pattern: Expensive Side is the Edge

| Entry Style | Markets | Avg Price | Win Rate | Insight |
|-------------|---------|-----------|----------|---------|
| Cheap first (<$0.35) | 9 | $0.24 | **44%** | Contrarian — below coin flip |
| Mid-range ($0.35-$0.55) | 6 | ~$0.45 | — | Small sample |
| Expensive first (>$0.55) | 15 | $0.90 | **86%** | FADE-like — clear edge |

**Critical insight:** Expensive-side entries at **86% WR** (12/14 resolved) vs cheap-side at 44% (4/9). The gap is even wider after removing sell-markets. This IS the user's edge.

### Directional Accuracy (One-Sided Markets)

| Side Bought | Markets | Win Rate | Avg Entry | Insight |
|-------------|---------|----------|-----------|---------|
| UP only | 10 | **90%** | $0.78 | Buys expensive UP, almost always right |
| DOWN only | 10 | **70%** | $0.69 | Buys expensive DOWN, good accuracy |

Both sides are buying the EXPENSIVE side (avg $0.73 overall). This is momentum/FADE, not contrarian.

### Hedging Patterns (Two-Sided Markets)

| Metric | Value | Implication for PHOENIX |
|--------|-------|------------------------|
| Two-sided (hedged) markets | 9/30 (30%) | User hedges about 1/3 of markets |
| Time-to-hedge | Mean 324s, Median 314s | Hedge is SLOW (>5 min avg) |
| Pair cost (hedged) | Mean $1.12, Only 33% < $1.00 | **Pair costs too high** — hedging done poorly manually |
| Entry side (hedged) | 67% cheap first, 33% expensive first | Mostly buys cheap side, then hedges |
| WR cheap-first hedged | 33% (2/6) | Hedged-contrarian underperforms |
| WR expensive-first hedged | 33% (1/3) | Too few samples |

**Key finding:** User's manual hedging is still **unprofitable** — only 33% of hedged pairs have pair cost < $1.00. Mean pair cost $1.12. PHOENIX must enforce pair_cost < $0.98 hard limit.

### Double-Down Pattern

| Metric | Value |
|--------|-------|
| Markets with double-down | 10/30 (33%) |
| Win rate on DD markets | **60%** (6/10) |
| Averaging down | 6 markets |
| Averaging up | 4 markets |

Double-downs at 60% WR — positive but more cautious than previous 71% estimate (which included sell-markets).

### Execution Style

| Metric | Value | Implication |
|--------|-------|-------------|
| **Order type** | **MAKER (limit orders)** | User confirms posting limit orders, NOT taking. API doesn't report maker/taker reliably. |
| Entry delay | Mean 356s (52% entries after 300s) | Late entries — waits for price to establish |
| Inter-trade interval | Median 209s | Manual pace, not automated |
| Position sizing | Median 20 shares, range 3-1130 | Wide variance (manual discretion) |
| Sell/Buy ratio | 0.00 (buy-only markets) | Pure hold-to-resolution in filtered set |

**User already trades as MAKER (0% fees).** PHOENIX preserves this advantage.

### Active Hours (UTC)
Peak trading: UTC 17:00-22:00 (IST 22:30-03:30). Strongest at 20:00 (24 trades). This is **US Late session**.

### Chainlink Oracle Basis Risk
User notes: Chainlink strike price varies $50-150 from Binance ~5% of the time. This means ~5% of markets resolve differently than our Binance-based signals would predict. This is a **structural cost of speed** — we accept occasional wrong resolutions as the price for real-time data.

---

## Part B: ML Pipeline Review

### Existing Results Summary

| Model | Level | Accuracy | AUC | Key Insight |
|-------|-------|----------|-----|-------------|
| LogReg | tick | 74.8% | 0.846 | Best tick-level generalizer |
| RF | tick | 73.5% | 0.829 | Features: up_mid, ask_price_diff |
| XGBoost | tick | 67.7% | 0.785 | **Overfits** (CV 84% vs test 68%) |
| LogReg | market | **94.6%** | 0.980 | Uses last-25% data (near resolution) |
| GB | market | 93.9% | 0.982 | Top features: down_ask_last, up_ask_last |
| **LogReg (300-600s)** | **market** | **83.0%** | **0.893** | **Actionable window — NO better than baseline** |
| GB Entry Timing | entry | 94.1% | 0.981 | Learns FADE filter (expensive_ask + time) |

### Critical Finding: ML Cannot Beat Simple Heuristic at Actionable Time

At the 300-600s entry window:
- **ML (LogReg):** 83.0% accuracy
- **Baseline ("expensive side wins"):** **83.7%** accuracy
- **ML adds NEGATIVE value** (-0.7%)

The 94.6% market-level accuracy comes entirely from **late-game price convergence** near resolution, not from early prediction ability. By 300-600s remaining, prices already reflect the likely winner — ML just rediscovers the same price signal.

### Entry Timing Model — It's Just FADE

Top features of the Entry Timing GB model:
1. `expensive_ask`: **55% importance** (is the expensive side > $0.70?)
2. `time_remaining_secs`: **17% importance** (is time in [90, 600]?)
3. `time_urgency`: **11% importance** (urgency = 1/(time+1))
4. `time_urgency_sq`: **8% importance** (urgency squared)
5. `binance_price`: 3.7%
6. `cheap_ask`: 3.1%

**91% of model importance comes from FADE's two filters** (expensive_ask threshold + time window). The model learned the exact same strategy as FADE, just with smoother thresholds.

### PnL by Confidence Threshold

| Threshold | Signals | Win Rate | Avg PnL/share | Total PnL |
|-----------|---------|----------|---------------|-----------|
| P>=0.3 | 575,713 | 89.2% | $0.049 | $27,905 |
| P>=0.5 | 553,646 | 89.5% | $0.049 | $26,853 |
| **P>=0.7** | **455,620** | **92.0%** | **$0.049** | **$22,272** |
| P>=0.8 | 365,129 | 94.1% | $0.046 | $16,872 |

Average PnL/share is ~$0.05 regardless of threshold. Higher thresholds just filter more signals without improving per-signal quality. This confirms ML doesn't add alpha — it just recreates the expensive_ask filter.

### Feature Importance Consistency Across Models

| Rank | LogReg (|coef|) | Random Forest | XGBoost |
|------|-----------------|---------------|---------|
| 1 | up_ask_4 (OB depth) | up_mid | ask_price_diff_pct |
| 2 | down_ask_1 (OB depth) | ask_price_diff_pct | bid_price_diff |
| 3 | up_ask_3 (OB depth) | mid_price_diff | up_mid |
| 4 | up_bid_5 (OB depth) | down_mid | expensive_ask |
| 5 | up_bid_4 (OB depth) | bid_price_diff | down_ask |

**All models agree:** Price features dominate. The signal is fundamentally about **which side is more expensive**.

---

## Part C: Signal Status Summary

### Signals KEPT for PHOENIX

| Signal | Status | Evidence | How Used |
|--------|--------|----------|----------|
| `expensive_ask > $0.75` | **PRIMARY** | 86% WR in wallet, 87.6% in backtests at $0.75+, 91.1% at $0.80+ | Bias formation — simple heuristic |
| `spike_detected` | **KEPT** | 94.7% accuracy in FADE (original trigger). Raw spike detection works. | Entry trigger — detect overreaction |
| Session stop (ADAPT25) | **KEPT** | OOS9: 0/90 configs profitable without it | Risk management |
| Hour-of-day filter | **KEPT** | +$1,148 PnL improvement in FADE backtest | Skip bad hours |
| Double-down | **KEPT** | 60% WR in wallet (10 markets) | Position management |
| Maker execution | **KEPT** | 0% fees vs 1.56% taker. User already uses limit orders. | Execution edge |

### Signals DROPPED from PHOENIX

| Signal | Dropped Because | Original Claim | Actual Evidence | Could Re-Test? |
|--------|----------------|----------------|-----------------|----------------|
| **ML model bias** | Adds -0.7% vs simple heuristic at 300-600s | 94.6% market-level accuracy | 83.0% at actionable window vs 83.7% baseline. 91% of Entry Timing model importance = FADE. | Not worth it — ML just learns expensive_ask. Unless we find new features the model hasn't seen. |
| **Velocity filter** | HURTS FADE accuracy by -3pp | +218% improvement | +218% was from old AGGRESSIVE taker strategy. FADE backtest: 83.7%→80.7% when filtering by velocity. ML gives 0.4% importance. | **Could re-test with different formulation** — maybe velocity as a continuous weight rather than binary filter? Or velocity of the CHEAP side specifically? |
| **OBI contrarian** | 0/3 datasets statistically significant | 82.5% accuracy (whale research) | 82.5% was Baguette's OVERALL win rate, not OBI's accuracy. Chi-squared test: p=0.50. At $0.80, OBI contra HURTS by -2.4pp. | **Could re-test** — maybe OBI needs larger window (30s rolling OBI vs instantaneous), or needs to be combined with price momentum. |
| **Acceleration/Jerk** | ML gives 0% importance, never backtested | Untested | No test run yet. ML feature importance = 0%. | **Could still test independently** — ML might not capture non-linear effects. Quick stat test on observer data would be definitive. |
| **Composite scores** | <0.1% ML importance | Engineered combinations | Never showed value | Not worth it |
| **signal_quality** | p=0.85 (proven useless) | Signal quality metric | Already deprecated | No |

### Honest Assessment: Are We Being Too Aggressive with Removals?

**What's solid:**
- ML bias → simple heuristic: **Very solid**. ML literally learns the same thing (91% of importance = expensive_ask + time). No alpha added.
- OBI contrarian: **Solid**. Statistical test across 3 datasets, p=0.50. No signal.

**What deserves a second look:**
- **Velocity**: The -3pp finding is from FADE's specific implementation (binary filter). Different formulations might work:
  - Velocity of expensive side specifically (does the winning side accelerate?)
  - Velocity magnitude as position sizing weight (higher velocity → smaller position)
  - Velocity CHANGE (deceleration = entry signal)
- **Acceleration/Jerk**: Never actually tested independently. ML 0% importance might be because ML can't capture the specific pattern (sign reversal after spike). A targeted stat test would be definitive.
- **OBI in different windows**: Tested at 300-600s per-market median, but OBI might work at shorter timescales (5-30s around entry decision).

**Recommendation:** The core strategy (expensive_ask > $0.75, maker entry, hold-to-resolution, session stop) is proven. Velocity and acceleration can be explored as **optional boosters** in Session 2 with targeted tests, but they should NOT be required for entry.

---

## Part D: OBI Contrarian Signal Statistical Test

### Test Design
- **Hypothesis:** OBI contrarian (OBI disagrees with expensive side) improves expensive_side_wins prediction
- **Datasets:** IS+OOS2, OOS7, OOS8, OOS9, OOS3+4 (669 markets total at $0.55+ threshold)
- **Method:** Per-market median observation in 300-600s entry window, chi-squared test
- **Script:** `research/backtests/obi_contrarian_test.py`

### Results

| Threshold | Baseline Acc | OBI Contrarian Acc | OBI Agrees Acc | Delta | Significant? |
|-----------|-------------|-------------------|---------------|-------|-------------|
| >= $0.55 | 76.7% (n=669) | 76.5% (n=170) | 78.8% (n=80) | **-0.2%** | 0/3 datasets |
| >= $0.65 | 82.0% (n=517) | 83.0% (n=147) | 84.1% (n=63) | **+1.0%** | 0/3 datasets |
| >= $0.75 | 87.6% (n=356) | 86.1% (n=108) | 86.7% (n=45) | **-1.5%** | 0/3 datasets |
| >= $0.80 | 91.1% (n=280) | 88.7% (n=97) | 96.8% (n=31) | **-2.4%** | 0/3 datasets |

### Verdict: DROP OBI FROM PHOENIX

- **Zero datasets show statistical significance** (avg p=0.50 at $0.75)
- At $0.80, OBI contrarian actually **hurts** accuracy by 2.4pp
- Ironically, "OBI agrees with expensive side" performs slightly better (96.8% vs 88.7% at $0.80)
- This is consistent with ML giving OBI <0.2% importance

**The 82.5% claim was Baguette's overall win rate, NOT the OBI signal's accuracy.** Baguette's edge comes from something else we can't observe (timing, proprietary signal, or superior execution).

---

## Part E: Decisions Confirmed for Session 2

Based on user responses + data:

| Decision | Value | Evidence |
|----------|-------|---------|
| Entry side | **Expensive side** | 86% WR vs 44% for cheap (wallet, buy-only) |
| Bias formation | **Simple heuristic** (expensive_ask > $0.75) | ML adds -0.7% vs baseline at 300-600s |
| OBI signal | **DROPPED** | 0/3 datasets significant, -2.4pp at $0.80 |
| Velocity filter | **DROPPED from requirements** | FADE backtest shows -3pp. Could re-test different formulation as optional booster. |
| Acceleration/Jerk | **DEPRIORITIZED** | ML gives 0% importance; targeted test still possible |
| Order type | **MAKER** (user confirms limit orders) | API unreliable, user reports maker |
| Chainlink basis risk | **Accepted** | $50-150 divergence ~5% of time. Cost of speed. |

---

## Files Generated

| File | Description |
|------|-------------|
| `research/findings/wallet_0xEd0C_trades.csv` | 65 buy-only trades, raw CSV |
| `research/findings/wallet_analysis_0xEd0C.md` | Auto-generated strategy report |
| `research/findings/PHOENIX_SESSION1_FINDINGS.md` | This document |
| `research/findings/data/obi_contrarian_test_results.csv` | OBI test raw results |
| `research/backtests/obi_contrarian_test.py` | OBI statistical test script |

---

## Data References

| Source | Key Metrics | File |
|--------|-------------|------|
| Wallet Analysis | 65 trades (buy-only), 65.5% WR, $105.77 PnL | `scripts/reverse_engineer_wallet.py` |
| ML Pipeline | 94.6% market-level, 83% at 300-600s | `research/findings/ML_MARKET_PREDICTOR_FINDINGS.md` |
| Pair Trade Analysis | $0.75+ = 100% accuracy, H25 optimal | `research/findings/ML_MARKET_PREDICTOR_FINDINGS.md` (Section 7) |
| Feature Importance | Price = 85.7%, velocity 0.4%, OBI <0.2% | `research/ml/market_predictor/models/model_comparison_all.csv` |
| Velocity Contradiction | +218% AGGRESSIVE only, -3pp in FADE | `research/findings/AGGRESSIVE_M_V2_REVISED_FINDINGS.md` |

*Session 1 Complete — Ready for Session 2: Signal Engineering*
