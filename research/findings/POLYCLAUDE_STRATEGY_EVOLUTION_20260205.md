# PolyClaude Strategy Evolution: Complete Journey

**Date:** February 5, 2026
**Status:** Strategy Pivot from Taker (AGGRESSIVE) to Maker (MAKER-PREDICTION)

---

## Executive Summary

This document captures the complete evolution of PolyClaude strategy development, from initial arbitrage attempts through the current pivot to maker-based prediction strategies.

**Key Outcome:** After testing multiple approaches, we discovered:
1. Binary arbitrage does NOT exist in 15m crypto markets (0.0001% opportunity rate)
2. Latency arbitrage is NOT viable (BTC velocity r=0.055, explains 0.3% variance)
3. Taker fees (2%) are fatal to thin margins
4. Prediction-based strategies with maker orders (0% fees) are the viable path

---

## Strategy Tree Overview

```
                              GOAL: Profit from
                           Polymarket BTC 15m Markets
                                     │
                    ┌────────────────┼────────────────┐
                    │                │                │
                    ▼                ▼                ▼
            ┌───────────┐    ┌───────────┐    ┌───────────┐
            │ ARBITRAGE │    │  LATENCY  │    │PREDICTION │
            │ (Path A)  │    │  (Taker)  │    │ (Path B)  │
            └─────┬─────┘    └─────┬─────┘    └─────┬─────┘
                  │                │                │
           ┌──────┴──────┐        │          ┌─────┴─────┐
           ▼             ▼        ▼          ▼           ▼
      ┌─────────┐  ┌──────────┐ ┌──────────┐ ┌─────────┐ ┌──────────┐
      │ Binary  │  │Sequential│ │AGGRESSIVE│ │CONTRARIAN│ │  MAKER-  │
      │  Arb    │  │Pair Build│ │  Spike   │ │Mean-Rev │ │PREDICTION│
      └────┬────┘  └────┬─────┘ └────┬─────┘ └────┬────┘ └────┬─────┘
           │            │            │            │           │
           ▼            ▼            ▼            ▼           ▼
        ❌ DEAD      ❌ DEAD     ❌ DEPRECATED  ✅ VALID   🔧 BUILDING
       (0.0001%)    (0/108)     (backtest     ($12/hr    (57%+ acc)
                               bugs + fees)   @50sh)
```

---

## Chronological Timeline

| Period | Strategy | Status | Key Metric | Why |
|--------|----------|--------|------------|-----|
| Jan 9-15 | Spread Capture | FAILED | $3.40/hr | Offset bug, pair costs >$1.03 |
| Jan 15-25 | AGGRESSIVE Spike | DEPRECATED | $15.20/hr (backtest) | 7 bugs found, taker fees |
| Jan 15-25 | CONTRARIAN | VALIDATED | $12.36/hr @50sh | Works, no capital constraint |
| Jan 26-31 | Z-Score/Vol Optimization | INTEGRATED | +52% improvement | EWMA z-score better than OU |
| Feb 1-4 | Backtest Bug Fixes | COMPLETED | 7 critical bugs | Backtest/live mismatch |
| Feb 5 | MAKER-PREDICTION | IN PROGRESS | 57%+ accuracy | 0% fees, prediction edge |

---

## What Failed (And Why)

### 1. Binary Arbitrage (Frank-Wolfe)

**Hypothesis:** Buy UP + DOWN when sum < $1.00 = guaranteed profit

| Dataset | Observations | Pair < $1.00 | Pair < $0.98 |
|---------|--------------|--------------|--------------|
| IS+OOS2 | 1,090,500 | 10 (0.0009%) | 1 (0.0001%) |
| OOS3+OOS4 | 930,960 | 135 (0.015%) | 0 (0.0%) |

**Why Failed:** Markets TOO EFFICIENT. Mean pair cost = $1.012.

### 2. Sequential Pair Building (Path A)

**Hypothesis:** Buy UP when cheap, wait, buy DOWN when cheap → combined cost < $1.00

| Metric | Result |
|--------|--------|
| Markets with sequential dips | 77% |
| Achievable P10 pair cost | $0.556 |
| Grid search configs tested | 108 |
| **Profitable configs** | **0/108** |
| Best config | -$8.03 |

**Why Failed:** By time second side is cheap, market resolves. Time-stop hedges at market prices destroy edge.

### 3. AGGRESSIVE Spike Strategy

**Hypothesis:** Detect BTC spike → Enter Polymarket before price updates

**Backtest looked good:**
- $15.20/hr validated across 167 hours
- 49.7% win rate, Sharpe 0.90

**Live reality (Feb 5):**
- Backtest: +$10.32/hr, 346 trades, 70% WR
- Paper: LOSS, 144 trades, 25% WR

**7 Critical Bugs Found:**
1. EWMA deduplication mismatch (2.4x trade difference)
2. Hedge bid formula wrong (3-5 cents worse)
3. Velocity filter boundary conditions
4. Time-stop guard blocking exits
5. Loss limit not enforced ($79 loss vs $10 limit)
6. CSV logging failing silently
7. Breakeven hold hardcoded wrong (2s vs 10s)

**Fundamental Issues:**
- BTC velocity correlation: r = 0.055 (explains 0.3% variance)
- 60Hz Binance data = NO latency advantage (public data)
- Taker fees: 2% on EVERY entry

### 4. BTC Velocity Latency Arbitrage

| Metric | Value |
|--------|-------|
| BTC velocity correlation | r = 0.055 |
| Variance explained | 0.3% |
| Model improvement | +0.1% |
| **Actionability** | **NONE** |

---

## What Worked

### CONTRARIAN Mean-Reversion Strategy

**Hypothesis:** BTC mean-reverts at 15-min scale → Buy OPPOSITE of BTC move

| Metric | Value |
|--------|-------|
| Entry | Buy opposite side at $0.30 |
| R:R Ratio | 2.33:1 (risk $0.30, reward $0.70) |
| Win Rate | 42-54% (breakeven = 30%) |
| **$/hr @ 50 shares** | **$12.36/hr** |
| Sharpe | 1.08+ |
| Validation | 129 hours across 3 datasets |

**Why It Works:**
- Asymmetric payoff overcomes lower win rate
- Adaptive EWMA vol gate self-adjusts (no calibration needed)
- Validated in live wallet (0xa5e8)

---

## Whale Analysis: What The Pros Do

### Gabagool ($728K from $200)

| Metric | Value |
|--------|-------|
| Pair cost | $1.0117 (LOSING on arbitrage) |
| Prediction accuracy | 70% |
| Entry timing | 313s median (LATE) |
| Order type | 77% single-fill (MAKER) |

**Key Insight:** Edge is PREDICTION, not arbitrage. Accepts 2.6% adverse selection because 70% accuracy overcomes it.

### Baguette (82.5% Accuracy)

| Metric | Value |
|--------|-------|
| Entry timing | 9s median (4x better ROI than Gabagool) |
| OBI correlation | -0.638 (STRONG CONTRARIAN) |
| Hedge ratio | 63% (takes directional risk) |
| ROI | 4.52% (vs Gabagool's 1.14%) |

**Baguette's Strategy:**
1. Enter EARLY (9s) before price discovery
2. FADE OBI (buy opposite of orderbook imbalance)
3. Buy expensive side (momentum)
4. Seek volatility (2x trades in high-vol zones)
5. **INVERSE SIZING: Smaller positions on high-confidence trades**

#### Inverse Sizing Detail (from WHALE_OBI_ANALYSIS.md)

| Outcome | Baguette Avg Position Size |
|---------|---------------------------|
| CORRECT (82.5%) | 415 shares |
| WRONG (17.5%) | **746 shares** |
| Ratio | **0.56x** |

**Baguette bets 44% SMALLER on winning trades.** This is NOT from doubling down - early vs late trade sizes are equal (0.99x ratio).

### Prediction Accuracy Ladder

| Signal | Accuracy |
|--------|----------|
| Random baseline | 50.0% |
| Expensive side only | 56.9% |
| + Momentum (60s) | 60.0% |
| + High momentum (>$0.02) | 66.2% |
| Gabagool (actual) | 67.5% |
| **UNEXPLAINED GAP** | **+15.0%** |
| Baguette (actual) | 82.5% |

The 15% gap likely comes from:
- Execution timing (9s vs 313s entry)
- Order flow visibility we don't have
- Active market making (not just hedging)

---

## Current Status (Feb 5, 2026)

### Deprecated
- Binary Arbitrage (0.0001% opportunity rate)
- Sequential Pair Building (0/108 configs profitable)
- AGGRESSIVE Taker (7 bugs, 2% fees, no latency edge)
- BTC Velocity Signal (r=0.055, not actionable)

### Validated
- **CONTRARIAN** - $12/hr @50 shares, Sharpe 1.08+

### In Development
- **MAKER-PREDICTION** (Path B) - 57%+ accuracy, 0% fees

---

## Path Forward: MAKER-PREDICTION

### Core Concept

Don't try to get pair cost < $1.00. Instead:
- PREDICT the winner with >50% accuracy
- Accept pair cost > $1.00 but WIN on direction
- Use MAKER orders (0% fees) instead of TAKER (2% fees)

### Components

| Component | Status | Details |
|-----------|--------|---------|
| Prediction signal | Baseline 57% | Expensive side + OBI + momentum |
| Time window gating | +20pp | 300-600s remaining |
| Multiplicative scoring | +8pp | spike × velocity × time_weight |
| MAKER entry | TODO | 0% fee, fill uncertainty |

### Expected Accuracy

| Signal Stack | Accuracy |
|--------------|----------|
| Expensive side only | 57% |
| + Time window (300-600s) | 77% |
| + Multiplicative scoring | 85% |
| **Realistic ceiling** | **79-82%** |

---

## Key Lessons Learned

1. **Arbitrage is dead in efficient markets** - 0.0001% opportunity rate
2. **Taker fees are fatal** - 2% per entry destroys thin margins
3. **Prediction > Execution** - 70% accuracy beats any execution edge
4. **Backtest ≠ Live** - 7 critical bugs discovered
5. **Timing > Signal** - Baguette's 9s entry = 4x better ROI
6. **Contrarian works** - Fade OBI (-0.638 correlation)
7. **No latency edge exists** - 60Hz Binance is public data

---

## Reference Files

| File | Purpose |
|------|---------|
| `research/findings/WHALE_OBI_ANALYSIS.md` | Whale trading patterns, inverse sizing |
| `research/findings/gabagool_strategy_decoded.md` | Gabagool's actual strategy |
| `research/findings/GABAGOOL_FRANK_WOLFE_BREGMAN_ANALYSIS.md` | Arbitrage analysis |
| `research/strategies/MAKER_PREDICTION.md` | Path B strategy spec |
| `research/strategies/STRATEGY_PIVOT_FEB2026.md` | Pivot documentation |
| `PolyClaude/research/findings/gabagool_btc_correlation_findings.md` | BTC velocity analysis |

---

*Created: February 5, 2026*
*Based on: Complete strategy development history Jan 9 - Feb 5, 2026*
