# 2x2 TIME-STOP × HEDGE FORMULA ANALYSIS
## Config: HIGH_ENTRY=0.90, LOW_ENTRY=None

**Date:** Feb 3, 2026

---

## Executive Summary

| Config | Combined OBI $/hr | All OBI Profitable? | Taker Exit % | Worst Trade | VERDICT |
|--------|-------------------|---------------------|--------------|-------------|---------|
| **TS30_OLD** | **$15.55/hr** | YES | 53-57% | -$35.88 | Best $/hr, high taker % |
| **TS30_NEW** | $7.32/hr | YES | 31-33% | -$35.88 | Most balanced |
| TS180_OLD | $7.54/hr | NO (OOS9.1 loses) | 28-39% | -$36.85 | Fails trending |
| TS180_NEW | $1.35/hr | NO (OOS8 loses) | 13-15% | -$30.97 | Too conservative |

---

## Config Definitions

| Config | Time-Stop | Hedge Formula | Pair Cost Target |
|--------|-----------|---------------|------------------|
| TS30_OLD | 30s | DROP_MULT=0.50, DROP_INT=0.08 | ~0.91 |
| TS30_NEW | 30s | DROP_MULT=0.10, DROP_INT=0.035 | ~0.95 |
| TS180_OLD | 180s | DROP_MULT=0.50, DROP_INT=0.08 | ~0.91 |
| TS180_NEW | 180s | DROP_MULT=0.10, DROP_INT=0.035 | ~0.95 |

**Fixed params:** HIGH_ENTRY=0.90, LOW_ENTRY=None, OBI=auto-detect

---

## Per-Dataset Results

### OBI Datasets (Primary Validation)

#### OOS7 (18.95h)
| Config | $/hr | Trades | Win% | Passive% | Taker% | Sharpe | Worst Trade |
|--------|------|--------|------|----------|--------|--------|-------------|
| TS30_OLD | $9.40 | 401 | 50% | 43% | 57% | 0.55 | -$15.66 |
| TS30_NEW | $5.56 | 470 | 50% | 67% | 33% | 0.46 | -$15.12 |
| TS180_OLD | $13.99 | 233 | 55% | 71% | 29% | 0.70 | -$26.01 |
| TS180_NEW | $6.37 | 303 | 56% | 86% | 14% | 0.43 | -$23.12 |

#### OOS8 (18.12h)
| Config | $/hr | Trades | Win% | Passive% | Taker% | Sharpe | Worst Trade |
|--------|------|--------|------|----------|--------|--------|-------------|
| TS30_OLD | $22.07 | 557 | 53% | 47% | 53% | 1.03 | -$35.88 |
| TS30_NEW | $9.29 | 729 | 53% | 68% | 32% | 0.56 | -$35.88 |
| TS180_OLD | $9.42 | 268 | 53% | 72% | 28% | 0.35 | -$36.85 |
| TS180_NEW | **-$3.46** | 394 | 52% | 85% | 15% | -0.15 | -$30.97 |

#### OOS9.1 (7.74h) - TRENDING MARKET
| Config | $/hr | Trades | Win% | Passive% | Taker% | Sharpe | Worst Trade |
|--------|------|--------|------|----------|--------|--------|-------------|
| TS30_OLD | $15.32 | 234 | 42% | 44% | 56% | 0.73 | -$16.20 |
| TS30_NEW | $6.99 | 286 | 42% | 69% | 31% | 0.43 | -$16.20 |
| TS180_OLD | **-$12.69** | 119 | 44% | 61% | 39% | -0.43 | -$28.04 |
| TS180_NEW | $0.32 | 175 | 47% | 85% | 15% | 0.01 | -$27.06 |

### Combined OBI Results (44.8h total)

| Config | Total PnL | $/hr | Trades | Avg Passive% |
|--------|-----------|------|--------|--------------|
| **TS30_OLD** | **$696.79** | **$15.55** | 1,192 | 45% |
| TS30_NEW | $327.87 | $7.32 | 1,485 | 68% |
| TS180_OLD | $337.76 | $7.54 | 620 | 68% |
| TS180_NEW | $60.34 | $1.35 | 872 | 85% |

---

### Conservative Validation (Older Datasets)

| Config | IS+OOS2 (23.4h) | OOS3+4 (47.1h) | OOS5 (41.7h) | Combined |
|--------|-----------------|----------------|--------------|----------|
| TS30_OLD | $1.30/hr | $9.50/hr | **-$9.52/hr** | $0.72/hr |
| TS30_NEW | $0.59/hr | $5.19/hr | **-$19.86/hr** | -$5.09/hr |
| TS180_OLD | $0.68/hr | $5.44/hr | **$0.58/hr** | $2.65/hr |
| TS180_NEW | $0.87/hr | $0.10/hr | **-$6.02/hr** | -$2.02/hr |

**OOS5 is catastrophic for TS30 configs**

---

## Deep Metrics

### Sharpe Ratio (target > 1.0)
- Only **TS30_OLD on OOS8** passes (1.03)
- All others < 1.0

### Profitable Market % (target > 50%)
| Config | OOS7 | OOS8 | OOS9.1 |
|--------|------|------|--------|
| TS30_OLD | 57% | 67% | 63% |
| TS30_NEW | 65% | 68% | **80%** |
| TS180_OLD | 65% | 62% | 50% |
| TS180_NEW | 74% | 59% | 63% |

### Worst Single Trade
- ALL configs have worst trades between -$15 and -$37
- None meet the -$10 threshold

### Taker Exit % (formerly "unhedged %")
- TS30 configs: 31-57% taker exits
- TS180 configs: 14-39% taker exits
- Lower is better (more passive fills = lower fees)

---

## Key Insights

### 1. TS30 vs TS180 on Trending Markets

**TS30 survives trending markets (OOS9.1):**
- TS30_OLD: +$15.32/hr
- TS180_OLD: -$12.69/hr

The 180s time-stop holds losing positions too long when market trends against us.

### 2. OLD vs NEW Hedge Formula

**OLD (deep hedge 0.50/0.08):**
- Higher $/hr when it works
- Lower passive fill rate (45-71%)
- More aggressive = more profit but more risk

**NEW (tight hedge 0.10/0.035):**
- Lower $/hr but more consistent
- Higher passive fill rate (67-86%)
- Safer but leaves money on table

### 3. The TS30_OLD Trade Volume Game

TS30_OLD has highest $/hr because:
- Takes 2x more trades than TS180 (1,192 vs 620)
- Compensates for lower per-trade accuracy with volume
- Higher variance - could go badly in live

### 4. OOS5 is Problematic

All configs except TS180_OLD lose money on OOS5:
- TS30 configs: -$9 to -$20/hr
- TS180_NEW: -$6/hr
- TS180_OLD: +$0.58/hr (only survivor)

---

## Ranking

1. **TS30_NEW** - Most balanced (profitable all OBI, 68% passive)
2. **TS30_OLD** - Highest $/hr but risky (55% taker exits)
3. **TS180_OLD** - Most stable on older data but fails trending markets
4. **TS180_NEW** - Too conservative ($1.35/hr not worth it)

---

## EWMA Spike Base Discovery (Feb 3, 2026)

**Problem:** Fixed 72-tick lookback generates multiple spikes from ONE price move.
- One market had 14 trades in 49 seconds from same price movement
- 60% of consecutive trades happen within 60 seconds of each other

**Solution:** EWMA spike base adapts after spike, reducing redundant signals.

### Results: EWMA Works BETTER with TS180!

| Config | Method | Combined OBI $/hr | OOS9.1 $/hr |
|--------|--------|-------------------|-------------|
| TS30 | FIXED | $15.55 | +$15.32 |
| TS30 | EWMA_1000 | $13.05* | +$10.50* |
| **TS180** | **EWMA_1000** | **$9.12** | **+$2.28** ✅ |
| TS180 | FIXED | $7.54 | -$12.69 |

*Estimated from OOS8 ratio

**Key Insight:**
- TS30 needs volume → FIXED wins
- TS180 can be selective → EWMA wins (turns OOS9.1 loss into profit!)

**New Recommendation:**
- High risk/reward: TS30 + FIXED ($15.55/hr, volatile)
- Stable: **TS180 + EWMA_1000** ($9.12/hr, survives trends)

---

## Files

- Results CSV: `research/findings/data/short_term_test_results.csv`
- Deep metrics: `research/findings/data/short_term_deep_metrics.csv`
- Per-trade CSVs: `research/findings/data/short_term_trades_*.csv`
