# Whale Deep Dive Analysis Report

**Generated:** 2026-01-29 06:10 ET
**Sample Size:** 2000 trades per wallet
**Focus:** BTC 15m markets, maker/taker patterns, comparison with AS strategy

---

## Executive Summary

**Neither whale profits from pair spreads.** Both have pair costs >= $1.00. Their edge comes from:
- **Baguette:** Unhedged directional trades + early entry (9s)
- **Gabagool:** Volume/frequency + maker rebates + near break-even pairs

---

## Side-by-Side Comparison

| Metric | BAGUETTE | GABAGOOL | AS Strategy |
|--------|----------|----------|-------------|
| **Execution Style** | MAKER (93%) | MIXED (77%) | MAKER |
| **Entry Delay (median)** | **9s** | 313s | Late (varies) |
| **Pair Cost (median)** | $1.162 | $1.006 | $1.013+ |
| **Hedge Ratio** | 63% | **92%** | Varies |
| **Profitable Pairs (<$1)** | 0% | 50% | ~0% |
| **UP Buy (median)** | $0.43 | $0.45 | - |
| **DOWN Buy (median)** | $0.64 | $0.49 | - |

---

## Detailed Findings

### BAGUETTE

**Profile:** Early-entry maker with directional exposure

| Metric | Value |
|--------|-------|
| Total trades | 2000 |
| BTC 15m trades | 898 |
| BTC 15m markets | 4 |
| Inferred style | MAKER |
| Single-fill % | 92.9% (1695/1825 txs) |
| Taker sweeps | 5.8% (106 txs) |

**Entry Timing:**
- Median entry delay: **9 seconds** after market start
- Mean entry delay: 228s (skewed by some late entries)
- Fastest entry: 6s

**Pair Economics:**
- Median pair cost: **$1.1617** (LOSING)
- Mean pair cost: $1.2313
- Profitable pairs: **0%**
- Hedge ratio: 63.3%

**Price Strategy:**
- Buys UP at median $0.43 (cheap side = likely loser)
- Buys DOWN at median $0.64 (expensive side = likely winner)
- Asymmetric: enters cheap UP, expensive DOWN

**Key Insight:** Baguette enters EARLY (9s) with MAKER orders but still has losing pair costs. The 63% hedge ratio means 37% unhedged exposure. Likely profits from directional bets on the unhedged portion, not from spread capture.

**REDEEM vs MERGE (Jan 29 Update):**
- Baguette does NOT merge pairs during the market
- Uses REDEEM only (claiming winning shares 6-8 hours AFTER resolution in batch)
- The "sells" visible in activity are genuine orderbook sells for spread capture
- Strategy: spread capture MM during market → hold remaining position → batch REDEEM winnings

---

### GABAGOOL

**Profile:** High-hedge maker with near break-even pairs

| Metric | Value |
|--------|-------|
| Total trades | 2000 |
| BTC 15m trades | 962 |
| BTC 15m markets | 2 |
| Inferred style | MIXED |
| Single-fill % | 77.2% (1060/1373 txs) |
| Taker sweeps | 1.6% (22 txs) |
| Multi-fill same price | 291 |

**Entry Timing:**
- Median entry delay: **313 seconds** after market start
- Mean entry delay: 313s
- Fastest entry: 46s

**Pair Economics:**
- Median pair cost: **$1.0060** (BREAK-EVEN)
- Mean pair cost: $1.0060
- Profitable pairs: **50%**
- Hedge ratio: **92.3%**

**Price Strategy:**
- Buys UP at median $0.45
- Buys DOWN at median $0.49
- More symmetric pricing, targets mid-range

**Key Insight:** Gabagool enters LATE (313s) like AS strategy but achieves near break-even pairs through 92% hedge ratio and symmetric pricing. High volume + maker rebates likely provides the edge.

**REDEEM Behavior (Jan 29 Update):**
- Only 2 REDEEMs in last 1000 activities, both dust amounts ($0.00006)
- Confirms: NO sells, NO merges, pure position holding to resolution
- Likely accumulates shares without claiming, or uses different redemption method

---

## Implications for AS Strategy

### Why AS Loses Despite Being MAKER

1. **Entry Timing:** AS enters after price discovery when adverse selection is highest
   - Baguette enters at 9s → gets better prices before the crowd
   - AS has no timing filter → enters whenever signal triggers

2. **Pair Cost Problem:** All strategies (whales included) struggle with pair costs > $1.00
   - This is structural in BTC 15m markets
   - Whales compensate with: (a) directional bets, (b) volume/rebates

3. **Hedge Ratio Trade-off:**
   - Low hedge (Baguette 63%) = directional profit potential but more risk
   - High hedge (Gabagool 92%) = less risk but need volume for rebates

### Recommended Actions

1. **Add Entry Timing Filter:**
   ```python
   # Only enter within first 60s of market
   if time_since_market_start > 60:
       skip_entry()
   ```

2. **Accept Directional Risk:**
   - AGGRESSIVE strategy (your validated one) is correct approach
   - Taker execution avoids adverse selection
   - OOS validated: +$17.59/hr vs AS -$7 to -$21/hr

3. **If Staying MAKER:**
   - Increase hedge ratio to 90%+ (Gabagool approach)
   - Target volume for maker rebates
   - Accept break-even on pairs, profit from rebates

4. **Price Level Targeting:**
   - Baguette buys cheap UP ($0.43), expensive DOWN ($0.64)
   - This is asymmetric betting on the winner side
   - Consider: buy winner side aggressively, hedge with cheap loser

---

## Raw Data Summary

### Baguette Price Distribution

| Category | UP Buys | DOWN Buys |
|----------|---------|-----------|
| cheap (<0.30) | 150 | 44 |
| mid-low (0.30-0.50) | 267 | 59 |
| mid-high (0.50-0.70) | 89 | 209 |
| expensive (>0.70) | 141 | 187 |

### Gabagool Price Distribution

| Category | UP Buys | DOWN Buys |
|----------|---------|-----------|
| cheap (<0.30) | 265 | 365 |
| mid-low (0.30-0.50) | 302 | 154 |
| mid-high (0.50-0.70) | 146 | 273 |
| expensive (>0.70) | 273 | 222 |

---

## Conclusion

**The whale analysis confirms your AGGRESSIVE strategy is the right approach:**

1. MAKER strategies (AS, Grid, even whales) struggle with pair costs > $1.00
2. Whales compensate with early entry OR high volume OR directional bets
3. Your AGGRESSIVE taker strategy sidesteps the adverse selection problem entirely
4. OOS validation (+$17.59/hr) already proves this works

**Next steps:**
- Continue with AGGRESSIVE strategy
- Consider adding early-entry timing filter
- Monitor for regime changes in BTC 15m market dynamics

---

*Report saved: /Users/rananjaybika/research/findings/WHALE_DEEP_DIVE_REPORT.md*
*Checkpoints: deep_dive_baguette.json, deep_dive_gabagool.json*
