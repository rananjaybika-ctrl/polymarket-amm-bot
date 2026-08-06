# PHOENIX Stop-Loss & Sequential Conditional Backtest Results

**Date:** February 19, 2026
**Datasets:** IS+OOS2, OOS3+4, OOS7, OOS8, OOS9 (199.2 hours, 573 unique markets)
**Scripts:** `phoenix_stoploss_backtest.py`, `phoenix_sequential_conditional_backtest.py`

---

## TL;DR

**Both modifications DESTROY profitability.** PHOENIX V1 (hold to resolution, no stop) remains the best approach.

| Strategy | $/hr | Worst Loss | WR% |
|----------|------|-----------|-----|
| **PHOENIX V1 Baseline** | **$1.22-$1.28** | **$-24.45** | **94.6%** |
| Best Stop-Loss Config | $-0.90 | $-22.50 | 84.3% |
| Best Sequential Conditional | $0.93 | $-24.35 | ~87% |
| Cheap-Only (no PHOENIX) | $-0.06 | $-4.80 | 33.5% |

---

## 1. STOP-LOSS BACKTEST

### Setup
- 58 stop-loss configs + 1 baseline = 59 configs
- Parameters: stop_threshold (3/5/8/12/20 cents), stop_delay (5/15/30/60/120 seconds), stop_exit_pct (64%/100%)
- 8 Baguette-style configs (profit target $0.05/$0.08 + stop-loss)
- Stop mechanics: taker sell at bid price, 542ms delay, taker fee applied

### Results: EVERY Stop Config is Unprofitable

```
Config                     $/hr      WR%   FalseStop%
BASELINE_NO_STOP          $1.28     94.6%     0.0%
STOP_T20_D120_E64        $-0.90     84.3%    68.0%   ← "best" stop
STOP_T03_D15_E100        $-1.32     39.3%    92.1%   ← worst stop
BAGUETTE_PT8_ST5_D60     $-1.06     61.6%    91.0%   ← Baguette-style
```

### Why Stop-Losses Fail

**68-92% of all stop triggers are FALSE STOPS** — trades that would have been profitable at resolution.

The reason is fundamental to binary market structure:
1. PHOENIX enters at $0.78-0.90 on the expensive side
2. Between entry and resolution (120-300 seconds), prices **oscillate wildly** due to BTC noise
3. A $0.03 dip in expensive_ask (which triggers a 3-cent stop) is routine noise, not a signal of losing
4. Binary markets resolve to $0 or $1 — intermediate prices are noise, not information
5. Selling during noise turns temporary paper losses into realized losses

**The stop-loss actually CREATES losses that wouldn't exist otherwise.** 94.6% of trades resolve profitably — stopping out converts many of those into losses.

### Quantitative Breakdown (aggregated across 5 datasets)

| Stop Threshold | Avg False Stop % | Avg $/hr | Avg WR Drop |
|---------------|-----------------|----------|-------------|
| 3 cents | 88-92% | -$1.05 to -$1.32 | -55 to -71pp |
| 5 cents | 83-90% | -$1.06 to -$1.26 | -23 to -52pp |
| 8 cents | 72-87% | -$1.14 to -$1.32 | -15 to -43pp |
| 12 cents | 64-87% | -$1.14 to -$1.28 | -15 to -39pp |
| 20 cents | 68-83% | -$0.90 to -$1.17 | -10 to -24pp |

Even the most conservative stop (20 cents, 120s delay): **68% false stops, $-0.90/hr.**

### Why This Differs from Baguette's Stop-Loss

Baguette CAN use stop-losses because:
1. Baguette enters at **$0.58 avg** (mid-range), not $0.78+ (expensive)
2. Baguette's exits are **scalp exits** at +$0.08 profit, not holds to resolution
3. Baguette is already taking profits 64% of the time — the stop is for the remaining 36%
4. At $0.58, a 5-cent dip to $0.53 is 8.6% loss. At $0.78, a 5-cent dip to $0.73 is still NOISE in a binary market

**Our PHOENIX entries at $0.78+ are too close to $1.00 resolution for any stop to distinguish noise from signal.**

---

## 2. SEQUENTIAL CONDITIONAL BACKTEST

### Setup
- Cheap-first probe at T=800/700/600/500 seconds, maker bid at cheap_ask - offset (2c or 4c)
- Cheap shares: 5, 10, or 15 per fill
- Expensive-side: PHOENIX standard (spike entry at T=300-120s, 25 shares)
- Expensive threshold: 0.80 or 0.85
- All positions held to resolution

### Results: Cheap Probe Reduces $/hr Without Meaningfully Reducing Risk

**Best configs by $/hr:**
| Config | $/hr | Insurance Savings | Worst Loss | Pair Cost |
|--------|------|-------------------|-----------|----------|
| BASELINE | $1.22 | — | $-24.45 | — |
| CT700_CO4_CS5_ET80 | $0.93 | $2.54/wrong | $-24.35 | $1.19 |
| CT700_CO2_CS5_ET80 | $0.88 | $6.39/wrong | $-24.45 | $1.21 |
| CT500_CO4_CS5_ET80 | $0.87 | $5.45/wrong | $-21.80 | $1.14 |

**Best configs by insurance value:**
| Config | $/hr | Insurance Savings | Worst Loss |
|--------|------|-------------------|-----------|
| CT500_CO2_CS5_ET85 | $0.38 | $18.49/wrong | $-22.90 |
| CT600_CO2_CS5_ET85 | $0.29 | $14.26/wrong | $-24.15 |
| CT500_CO4_CS5_ET85 | $0.56 | $14.05/wrong | $-22.80 |

### Why Insurance Has Negative Expected Value

With 94.6% win rate:

**On winning trades (94.6% of the time):**
- Cheap position resolves to $0 → lose ~$1.00 (5 shares × $0.20 avg entry)
- This is pure cost that subtracts from PHOENIX profit

**On losing trades (5.4% of the time):**
- Cheap position resolves to $1.00 → gain ~$4.00 (5 shares × ($1.00 - $0.20))
- This partially offsets PHOENIX's $19.50 loss

**Expected insurance value per trade:**
- Cost: 0.946 × $1.00 = -$0.946
- Benefit: 0.054 × $4.00 = +$0.216
- **Net: -$0.73 per trade** (insurance costs more than it saves)

The insurance only breaks even when WR < 82%. At PHOENIX's 94.6% WR, it's a losing proposition.

### Pair Cost Analysis

**Zero sub-$1.00 pair costs achieved across 275 config-dataset runs.** Average pair cost: $1.14-$1.26.

This confirms the prior finding: Polymarket's market efficiency prevents profitable pair building. Even with time separation (cheap at T=700, expensive at T=300), the market adjusts. When cheap drops to $0.20, expensive rises to $0.82+, maintaining pair_cost > $1.00.

### Cheap-Only Performance

Buying cheap side only (10 shares, maker bid):
- **Best:** CHEAP_ONLY_T700_O2: -$0.06/hr, 33.5% WR
- **Worst:** CHEAP_ONLY_T500_O2: -$1.04/hr, 24.3% WR
- Win rate declines as you get closer to resolution (cheaper = cheaper for a reason)
- The cheap side has ~15% base win rate, but maker offset means you enter slightly better → ~30% fill WR
  (because you only fill when cheap dips, which biases toward volatile/closer markets)

---

## 3. WHAT THIS MEANS FOR PHOENIX

### The Binary Market Paradox

PHOENIX's strength (97% directional accuracy, $0.78+ entries) IS its weakness for risk management:
- **High accuracy → stop-losses destroy value** (false stops > true stops)
- **High entry price → cheap-side insurance has negative EV** (costs more per winner than saves per loser)
- **Binary resolution → intermediate prices are noise** (no meaningful "support/resistance" levels)

### The Real Risk Issue

The $19.50 single-trade loss is painful, but the MATH works:
- PHOENIX makes ~$3.35/trade × 94.6% = $3.17 avg per winning trade
- Loses ~$12.53 avg × 5.4% = -$0.68 avg per losing trade
- **Net: +$2.49/trade average** → $1.28/hr

The occasional large loss is already priced into profitability. Adding risk management either:
1. Destroys profitable trades (stop-losses)
2. Taxes profitable trades to subsidize unprofitable insurance (cheap probe)

### Remaining Options (NOT tested yet)

1. **Position sizing reduction**: Instead of 25 shares, use 15 shares → max loss drops from $19.50 to $11.70
   - Pro: linear reduction in both profit AND loss, preserves structure
   - Con: also reduces $/hr proportionally

2. **Adaptive session stops** (ALREADY IMPLEMENTED): After 25 trades, if session PnL < -$5 → enable drawdown limit
   - This IS the current risk management — it's about SESSION-level risk, not per-trade risk
   - Much smarter than per-trade stops

3. **Market selection**: Avoid markets where expensive_ask > $0.90 (highest loss potential)
   - Already partially implemented via `max_pair_cost` filter
   - Could tighten threshold — e.g., only enter when expensive_ask < $0.85

---

## 4. CONCLUSION

| Approach | Verdict | Why |
|----------|---------|-----|
| Stop-loss | **DEAD** | 68-92% false stop rate, every config loses money |
| Baguette-style scalp + stop | **DEAD for us** | Works for Baguette's $0.58 entries, not our $0.78+ entries |
| Cheap-first probe | **DEAD** | Negative EV insurance at 94.6% WR, pair cost always > $1.00 |
| Cheap-only trading | **DEAD** | Break-even at best, negative at worst |
| **PHOENIX V1 as-is** | **KEEP** | $1.28/hr with current risk structure is the optimal operating point |

**PHOENIX V1 (FADE80_3c_ADAPT25_T5_DD20) remains the best strategy. The risk structure is inherent to binary markets at high accuracy — it cannot be improved by adding stop-losses or hedging without destroying the edge.**

The $19.50 worst-case loss is the cost of doing business. The adaptive session stop (ADAPT25) is the correct risk management layer — it operates at the SESSION level where variance can be managed, not at the per-trade level where binary noise dominates.

---

## Appendix: GitHub Notebook Review (Black-Scholes IV)

The notebook (`bsiv.ipynb` by Roman Paolucci, Quant Guild) covers:
- Black-Scholes European option pricing: `C = S_0 * N(d1) - K * e^(-rT) * N(d2)`
- Implied volatility derivation via optimization: `sigma_imp = argmin|C_BS - C_market|`
- IV skew/smile visualization (using SPX options data)
- IV surface (3D: strike x maturity x IV)

**Relevance to Polymarket:** Limited. Polymarket binary options have:
- Fixed payoff ($0 or $1), not continuous
- 15-minute expiry (ultra-short term)
- No explicit "strike" in the BS sense — the Chainlink price at expiry determines payoff
- No delta hedging, no Greeks — resolution is binary

The BS framework COULD theoretically model the binary option probability as `P(win) = N(d2)` where d2 involves distance-to-strike, time-to-expiry, and BTC volatility. But our empirical testing (28 signals, 17 tests) already showed that:
1. The market price IS the best estimate of probability
2. BTC kinematic signals add near-zero predictive value
3. Strike proximity has some regime-filtering value but doesn't improve per-trade prediction

A BS-derived "theoretical fair value" for the binary option would essentially reproduce what the Polymarket orderbook already reflects, making it redundant for our execution-based edge.
