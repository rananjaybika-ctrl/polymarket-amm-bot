# PHOENIX V2 Signal Research — Phase-by-Phase Analysis

**Date:** February 19, 2026
**Datasets:** IS+OOS2, OOS3+4, OOS7, OOS8, OOS9 (199.2 hours, ~670 markets)
**Tests run:** 17 implemented + Phase 3 ML pipeline
**Tests NOT implemented:** 2.3, 2.4, 2.10, 3.2, 4.2, 4.4, 4.6

---

## PHASE 1: Market-Level Analysis (7 tests)

### Test 1.1: Cheap-Side Win Rate by Spread Regime

**Question:** Do tight-spread markets give cheap side a higher chance of winning?

**Results at T=600s (10 min before resolution):**

| Spread Bucket | IS+OOS2 | OOS3+4 | OOS7 | OOS8 | OOS9 | Avg CWR | Avg Cheap$ | Edge |
|---------------|---------|--------|------|------|------|---------|-----------|------|
| 0.00-0.10 | 47.2% | 38.1% | 20.0% | 40.0% | 36.8% | 36.4% | $0.48 | -11.6pp |
| 0.10-0.20 | 43.5% | 52.4% | 37.5% | 42.1% | 46.7% | 44.4% | $0.43 | +1.4pp |
| 0.20-0.30 | 24.3% | 39.1% | 26.7% | 23.5% | 20.0% | 26.7% | $0.38 | -11.3pp |
| 0.30-0.40 | 36.0% | 33.3% | 36.4% | 13.3% | 25.0% | 28.8% | $0.33 | -4.2pp |
| 0.40-1.00 | 28.8% | 15.1% | 18.0% | 11.6% | 12.7% | 17.2% | $0.20 | -2.8pp |

**Edge = CWR - avg_cheap_price (positive = cheap underpriced)**

**Finding:** No consistent positive edge. At tight spread (0.00-0.10), CWR averages 36.4% but cheap costs $0.48 — **cheap is OVERPRICED** at tight spread. At 0.10-0.20 spread, edge is +1.4pp — essentially zero. At wide spread, cheap is consistently overpriced relative to its actual win rate.

**Consistency:** OOS7 is an outlier at tight spread (20.0% CWR — only 15 markets). Other datasets show 36-47% at tight spread. The signal does NOT replicate consistently across all 5 datasets.

---

### Test 1.2: Cheap-Side Win Rate by Strike Proximity

**Question:** Does BTC near the strike = higher cheap-side win rate?

| Proximity (bps) | IS+OOS2 | OOS3+4 | OOS7 | OOS8 | OOS9 | Avg CWR | n |
|-----------------|---------|--------|------|------|------|---------|---|
| 0-50 | 27.8% | 23.3% | 21.0% | 28.9% | 32.1% | 26.6% | 497 |
| 50-100 | — | — | — | 0.0% | — | 0.0% | 4 |

**Finding:** Nearly ALL markets fall in the 0-50bps bucket (497/501 markets with strikes). The strike proximity signal has effectively zero variation — BTC is almost always within 50bps of strike during the 15m market window. This makes strike proximity useless as a discriminator. The 0-50bps bucket CWR of 26.6% is near the base rate (~20-25% across all markets).

**Note:** The 50-100bps bucket has only 4 markets (all OOS8) with 0% CWR — not statistically meaningful.

---

### Test 1.3: Trajectory Divergence (Cheap Wins vs Cheap Loses)

**Question:** How early can we distinguish markets where cheap will win vs lose?

**IS+OOS2 Results (n=85 wins, 163 loses):**

| Time | Win Avg Cheap | Lose Avg Cheap | Difference | p-value |
|------|---------------|----------------|-----------|---------|
| T=900 | $0.498 | $0.502 | -$0.004 | 0.485 |
| T=750 | $0.415 | $0.407 | +$0.008 | 0.627 |
| T=660 | $0.389 | $0.339 | **+$0.050** | **0.003** |
| T=600 | $0.361 | $0.313 | +$0.048 | **0.001** |
| T=540 | $0.412 | $0.312 | **+$0.100** | **<0.001** |
| T=450 | $0.469 | $0.294 | +$0.176 | **<0.001** |
| T=300 | $0.547 | $0.217 | +$0.330 | **<0.001** |
| T=120 | $0.664 | $0.150 | +$0.514 | **<0.001** |

**OOS3+4 Results (n=47 wins, 122 loses):**
- Significance starts at T=660s (p=0.006)
- T=600: win=$0.364 vs lose=$0.284, diff=+$0.080, p<0.001
- T=300: win=$0.585 vs lose=$0.189, diff=+$0.396, p<0.001

**Finding:** Winning cheap sides diverge from losing ones starting around **T=660s** (p<0.005). By T=600s, the difference is statistically significant across all datasets. By T=300s, divergence is massive ($0.33-$0.40).

**BUT THIS IS THE TAUTOLOGY TRAP:** Markets where cheap wins MECHANICALLY have higher cheap_ask at every time point. This measures the OUTCOME embedded in price, not a predictive signal. The cheap_ask IS the market's probability estimate. Finding that "winners had higher cheap_ask" is circular — it says the market was already partially predicting the outcome.

**Actionable?** Only if we could identify cheap sides that are ABOVE their "expected trajectory" (adjusted for market regime) — which requires building a reference trajectory from losing markets and comparing. The raw trajectory divergence itself is not a trading signal.

---

### Test 1.4: Feature Importance for Cheap-Side Wins

**Question:** Which features predict cheap-side wins? (Individual feature AUC at T=600s and T=300s)

**At T=600s (averaged across 5 datasets):**

| Feature | Avg AUC | Avg r | Avg p-value | Signal? |
|---------|---------|-------|-------------|---------|
| cheap_ask | 0.62 | +0.20 | 0.005 | **TAUTOLOGICAL** — higher cheap price = higher WR |
| expensive_ask | 0.39 | -0.20 | 0.005 | Same tautology (inverse) |
| spread | 0.39 | -0.20 | 0.005 | Same tautology (spread encodes cheap level) |
| strike_proximity_bps | 0.37 | -0.21 | 0.06 | Moderate — closer to strike = higher CWR |
| cheap_side_imbalance | 0.72 | +0.33 | 0.004 | **INTERESTING** — OOS7 only (n=74), needs validation |
| velocity_bps | 0.52 | +0.04 | 0.55 | No signal |
| acceleration_bps2 | 0.45 | -0.04 | 0.55 | No signal |
| jerk_bps3 | 0.44 | -0.05 | 0.50 | No signal |
| momentum_5s | 0.53 | +0.06 | 0.50 | No signal |
| deceleration | 0.52 | +0.05 | 0.45 | No signal |
| kinematic_octant | 0.48 | -0.01 | 0.70 | No signal |
| cheap_ask_change_60s | 0.50 | 0.00 | 0.80 | No signal |
| cheap_ask_stdev_120s | 0.50 | +0.02 | 0.70 | No signal |

**At T=300s (closer to PHOENIX entry window):**

| Feature | Avg AUC | Avg r | Signal? |
|---------|---------|-------|---------|
| cheap_ask | 0.79 | +0.43 | **TAUTOLOGICAL** (stronger near resolution) |
| expensive_ask | 0.21 | -0.40 | Same tautology |
| spread | 0.21 | -0.40 | Same tautology |
| strike_proximity_bps | 0.22 | -0.22 | Moderate (closer = higher CWR) |
| cheap_ask_change_60s | 0.59 | +0.15 | Weak — recent cheap rise predicts win (tautological?) |
| All kinematics | 0.44-0.53 | near 0 | **No signal at any time** |

**Critical finding about cheap_side_imbalance:**
- OOS7 at T=600s: AUC=0.72, r=+0.33, p=0.004 — highest non-tautological signal
- Mean when cheap wins: +0.134, mean when cheap loses: -0.045
- BUT: only available in Gen3 datasets (OOS7, OOS8, OOS9)
- OOS8 and OOS9 not tested separately for this (they use different eval logic)
- **Needs cross-dataset validation before trusting**

**Conclusion:** All kinematic signals (velocity, acceleration, jerk, momentum, deceleration) are dead — AUC 0.44-0.53 (random). The only signals with any power are either tautological (cheap_ask, spread, expensive_ask) or have limited data (cheap_side_imbalance, strike_proximity). The market is efficient at pricing cheap-side probability.

---

### Test 1.5: Adverse Selection Correlation Decay

**Question:** At what time separation does the negative correlation between cheap and expensive decay?

| Δ (seconds) | IS+OOS2 | OOS3+4 | OOS7 | OOS8 | OOS9 | Avg r |
|-------------|---------|--------|------|------|------|-------|
| 0 (tick) | -0.50 | -0.61 | -0.79 | -0.74 | -0.69 | **-0.67** |
| 1 | -0.99 | -0.99 | -1.00 | -0.99 | -0.99 | **-0.99** |
| 5 | -0.97 | -0.97 | -0.97 | -0.96 | -0.97 | **-0.97** |
| 10 | -0.94 | -0.94 | -0.95 | -0.93 | -0.95 | **-0.94** |
| 30 | -0.83 | -0.83 | -0.88 | -0.82 | -0.86 | **-0.84** |
| 60 | -0.69 | -0.71 | -0.77 | -0.69 | -0.75 | **-0.72** |
| 120 | -0.50 | -0.53 | -0.62 | -0.48 | -0.60 | **-0.55** |
| 300 | -0.24 | -0.28 | -0.32 | -0.22 | -0.25 | **-0.26** |

**Finding:** Correlation is STRONGLY negative at all timescales up to 300 seconds. Even at 5 minutes separation, r = -0.26. This means buying both sides even minutes apart doesn't escape adverse selection. When cheap drops, expensive rises, and vice versa — this relationship persists throughout the market.

**Implication for both-side strategies:** This kills Family B (Gabagool-style DCA). The -0.55 correlation at 120 seconds means time-separated fills DON'T meaningfully reduce pair cost. Market efficiency maintains the inverse relationship for the entire market lifetime.

---

### Test 1.6: Strike Crossing Events

**Question:** When BTC crosses the strike, does this create cheap-side entry opportunities?

| Dataset | Crossings | Sides Flipped | Pre-Cheap Won Rate |
|---------|-----------|---------------|-------------------|
| IS+OOS2 | 672 | 21.1% | 42.4% |
| OOS3+4 | 814 | 41.5% | 44.2% |
| OOS7 | 653 | 37.4% | 45.8% |
| OOS8 | 532 | 34.0% | 55.8% |
| OOS9 | 895 | 34.1% | 43.1% |
| **Total** | **3,566** | **33.9%** | **45.6%** |

**Finding:** 3,566 strike crossings across 670 markets = ~5.3 crossings per market. Only 34% of crossings actually flip which side is cheap/expensive. The pre-crossing cheap side won 45.6% of the time — above the 26% overall cheap-side WR.

**BUT:** This is also somewhat tautological. Markets with many crossings are markets where BTC is oscillating near the strike, which means neither side has a strong edge — so cheap side has higher base WR in these markets. The "crossing" isn't creating an opportunity; it's identifying markets that are genuinely uncertain.

---

### Test 1.7: BTC Volatility as Regime Filter

| Volatility (bps) | Avg CWR | Total Markets | Avg Spikes |
|------------------|---------|---------------|------------|
| 0-5 | 26.7% | 139 | 2.5 |
| 5-15 | 17.3% | 246 | 15.5 |
| 15-30 | 19.8% | 179 | 37.6 |
| 30-100 | 12.0% | 93 | 198.8 |

**Finding:** Lower BTC volatility → higher cheap-side WR (26.7% at 0-5bps vs 12.0% at 30-100bps). This is intuitive: when BTC is stable, the market is uncertain, cheap side is close to 50/50. When BTC is volatile, it moves decisively, resolving the market directionally.

**Actionable?** Only if you want to AVOID high-volatility markets for cheap-side strategies. But the highest CWR (26.7% at 0-5bps, n=139) still doesn't meaningfully exceed the implied probability at those markets ($0.47 avg cheap). The edge is ZERO.

---

## PHASE 1 VERDICT

**Every test shows the same thing:** The Polymarket orderbook prices cheap-side probability correctly. No market-level filter (spread regime, strike proximity, BTC volatility, trajectory shape) identifies a subset where cheap is consistently underpriced.

The only non-tautological signal with any AUC was cheap_side_imbalance (OBI on cheap side) at AUC=0.72 — but this was tested on ONE dataset (OOS7, n=74). It needs cross-validation before it can be trusted.

---

## PHASE 2: Signal-Level Analysis (7 tests + 3 not implemented)

### Test 2.1: Velocity Toward Strike as Cheap Entry Timer

**Question:** Does BTC moving toward strike predict cheap-side price recovery?

| Velocity Bucket | Avg 30s Cheap Change | Avg Pct Positive | CWR |
|----------------|---------------------|------------------|-----|
| strong_away | -0.005 | 35.4% | 19.6% |
| weak_away | -0.009 | 33.1% | 19.7% |
| neutral | -0.011 | 32.1% | 19.7% |
| weak_toward | -0.009 | 34.1% | 19.2% |
| strong_toward | -0.008 | 37.3% | 20.5% |

**Finding:** Velocity toward strike has ZERO predictive power for cheap-side outcome (CWR ranges 19.2-20.5% — within noise). The 30s cheap price change is slightly better when BTC moves strongly away OR toward (both ~35% positive) vs neutral (32%). But the effect is tiny (3pp) and doesn't translate to win rate.

**Dead signal for cheap-side.** Same conclusion as FADE: velocity predicts nothing.

---

### Test 2.2: Kinematic State Octants

**Question:** Do the 8 velocity/acceleration/jerk combinations predict cheap-side price changes?

**Averaged across OOS3+4, OOS7, OOS8, OOS9 (Gen2+ datasets):**

| Octant | Label | Avg Cheap 30s Change | Pct Rises | CWR |
|--------|-------|---------------------|-----------|-----|
| 2 | [-v,+a,-j] | -0.002 | 35.8% | 24.3% |
| 6 | [+v,+a,-j] | -0.007 | 37.0% | 27.4% |
| 1 | [-v,-a,+j] | -0.006 | 36.2% | 25.7% |
| 3 | [-v,+a,+j] | -0.006 | 36.2% | 25.9% |
| 5 | [+v,-a,+j] | -0.010 | 35.6% | 25.5% |
| 0 | [-v,-a,-j] | -0.007 | 34.9% | 25.4% |
| 4 | [+v,-a,-j] | -0.009 | 35.8% | 25.7% |
| 7 | [+v,+a,+j] | -0.008 | 35.3% | 25.1% |

**Deceleration vs no deceleration:**
| Decel? | Avg Change | Pct Rises | CWR |
|--------|-----------|-----------|-----|
| NO | -0.008 | 35.3% | 25.4% |
| YES | -0.006 | 36.4% | 26.2% |

**Finding:** All 8 octants produce nearly identical results. CWR ranges 24.3-27.4% — a 3pp spread across 8 categories that should have VERY different physical meanings. Deceleration adds +0.8pp CWR — statistically meaningless.

**Dead signal.** The kinematic state of BTC (all derivatives up to jerk) tells us NOTHING about which side of a binary market will win. The market already knows.

---

### Test 2.5: Price Support Detection

**Question:** Does cheap-side price stability or positive slope predict wins?

**Slope quartiles at T=300s (IS+OOS2, n=250):**
| Quartile | Slope Range | CWR |
|----------|-----------|-----|
| Q1 (most negative) | -1.0 to -0.0002 | 15.9% |
| Q2 | -0.0002 to -0.0001 | 17.7% |
| Q3 | -0.0001 to 0.0000 | 21.0% |
| **Q4 (positive/flat)** | **0.0000 to +1.0** | **60.3%** |

**Stability quartiles at T=300s (IS+OOS2):**
| Quartile | CWR |
|----------|-----|
| Q1 (most stable) | 20.6% |
| Q4 (most volatile) | 42.9% |

**Finding:** Positive cheap-side slope at T=300 has 60.3% CWR! And high stdev (volatile cheap price) has 42.9% CWR.

**BUT THIS IS DEEPLY TAUTOLOGICAL:**
- Positive slope at T=300 means cheap price is RISING near resolution → the market is already telling you cheap is winning
- High stdev means cheap is bouncing = market is uncertain = higher base CWR
- Q4 slope (positive) with 60.3% CWR sounds amazing, but by T=300 with positive slope, the market has ALREADY MOVED toward cheap winning — you'd be buying after the move

**Replication check:** OOS3+4 Q4 slope at T=300 shows 32.6% CWR (much lower). The signal is noisy across datasets.

**Not actionable** — you can't trade on "cheap price was already rising" because by the time you detect it, the price has already moved.

---

### Test 2.6: Cross-Side Flow — **PROVEN TAUTOLOGICAL**

This test was proven tautological earlier in this session. The metric `toward_cheap_pct` measures "what fraction of ticks had cheap_ask increasing" — which mechanically correlates with cheap winning. Not a predictive signal.

---

### Test 2.7: FADE Bot Footprint at T=300

**Question:** Is there a detectable "FADE pulse" at T=300s where expensive jumps up and cheap dips?

| Dataset | Exp Change (310→270) | Cheap Change (310→270) | Cheap Recovery (270→240) |
|---------|---------------------|----------------------|-------------------------|
| IS+OOS2 | -0.001 | +0.001 | -0.008 |
| OOS3+4 | -0.005 | +0.005 | -0.000 |
| OOS7 | +0.023 | -0.024 | +0.013 |
| OOS8 | -0.002 | +0.002 | +0.012 |
| OOS9 | +0.009 | -0.010 | -0.009 |

**Finding:** No consistent FADE pulse. Some datasets show expensive rising at T=300 (OOS7, OOS9), others show it flat or declining. The cheap recovery after the "pulse" is also inconsistent (+0.013 in OOS7, -0.009 in OOS9). The FADE entry pattern is not detectable at the aggregate level, likely because different bots enter at different times in the 300-120s window, washing out any sharp pulse.

**Dead signal.** No exploitable FADE footprint.

---

### Test 2.9: Post-Spike Recovery

**Question:** After a BTC spike pushes cheap down, does cheap recover?

| Dataset | Avg Recovery | Pct Positive Recovery |
|---------|-------------|----------------------|
| IS+OOS2 | -0.0015 | 37.7% |
| OOS3+4 | -0.0059 | 38.2% |
| OOS7 | -0.0225 | 30.7% |

**Finding:** Post-spike, cheap side does NOT recover on average. Recovery is negative (cheap continues dropping). Only 31-38% of spikes show any cheap recovery — worse than coin flip. This means spikes are NOT overreactions that revert. They're real moves that continue.

**Dead signal for cheap-side entry timing.**

---

### Test 2.8: Spread Dynamics

Large dataset (60K+ rows of tick-level spread data). Tested spread velocity, spread volatility as regime indicators. Results show spread consistently widens over market lifetime — no mean-reversion of spread.

---

### Tests NOT Implemented (2.3, 2.4, 2.10)

- **2.3 (Post-spike deceleration):** Given that 2.9 shows spikes don't revert and kinematic signals (2.2) are dead, combining them would also be dead.
- **2.4 (OBI accumulation on cheap side):** The most promising missing test. OBI showed AUC=0.72 in Test 1.4 on OOS7. Would need to test temporal OBI dynamics specifically.
- **2.10 (Momentum divergence):** Given momentum (2.2) is dead, divergence from also-dead velocity would also be dead.

---

## PHASE 2 VERDICT

**All timing signals are dead.** Velocity, acceleration, jerk, momentum, deceleration, kinematic octants, post-spike recovery, FADE footprint, cross-side flow — NONE have predictive power for cheap-side outcome or cheap-side price recovery.

The ONLY signal with any life is **cheap_side_imbalance (OBI)** from Test 1.4, which showed AUC=0.72 on OOS7 but wasn't tested in Phase 2's temporal analysis (Test 2.4 not implemented).

---

## PHASE 3: ML & Combination Analysis

### Test 3.1: Multi-Signal ML Models

**Random Forest AUC for cheap-side win prediction (trained across datasets):**

| Test Dataset | T=300s AUC | T=600s AUC |
|-------------|-----------|-----------|
| IS+OOS2 | 0.782 | 0.577 |
| OOS3+4 | 0.730 | 0.628 |
| OOS7 | 0.805 | 0.566 |
| OOS8 | 0.869 | 0.627 |
| OOS9 | 0.812 | 0.744 |
| **Average** | **0.800** | **0.628** |

**Top features by importance (T=300s):**
1. expensive_ask: 0.206 — **TAUTOLOGICAL**
2. btc_range_bps: 0.143
3. cheap_trajectory_slope: 0.140 — **TAUTOLOGICAL**
4. strike_proximity_bps: 0.102
5. cheap_change_60s: 0.098 — **TAUTOLOGICAL**
6. cheap_stdev_120s: 0.076
7. spread: 0.074 — **TAUTOLOGICAL**
8. flow_toward_cheap_pct: 0.054 — **TAUTOLOGICAL**

**Top features by importance (T=600s):**
1. strike_proximity_bps: 0.160
2. btc_range_bps: 0.126
3. cheap_trajectory_slope: 0.110 — **TAUTOLOGICAL**
4. cheap_stdev_120s: 0.109
5. flow_toward_cheap_pct: 0.085 — **TAUTOLOGICAL**
6. cheap_ask: 0.085 — **TAUTOLOGICAL**

**Finding:** AUC=0.80 at T=300 looks impressive, but the top features are dominated by tautological signals (expensive_ask, cheap_trajectory_slope, spread, flow). These features encode the OUTCOME in the PRICE.

At T=600 (where we'd need to act for cheap-first strategies), AUC drops to 0.628 — barely above 0.50 random. And the top features are still dominated by price-level/trajectory signals that measure the outcome.

**The only non-tautological features with meaningful importance:** btc_range_bps (0.126-0.143) and strike_proximity_bps (0.102-0.160). Both are regime filters, not timing signals.

---

### Test 3.3: Conditional EV per Share

**Question:** Under what conditions does buying cheap have positive EV?

**At T=600s:**

| Condition | n | CWR | Avg Cheap$ | EV/share | Edge |
|-----------|---|-----|-----------|----------|------|
| ALL_MARKETS | 660 | 31.4% | $0.313 | +$0.001 | +0.1pp |
| spread<0.20 | 178 | 45.5% | $0.455 | -$0.000 | **0pp** |
| spread<0.10 | 89 | 41.6% | $0.482 | -$0.067 | -6.7pp |
| btc_vol<10bps | 287 | 34.1% | $0.349 | -$0.007 | -0.7pp |
| cheap>$0.35 | 278 | 42.4% | $0.429 | -$0.004 | -0.4pp |
| deceleration | 101 | 33.7% | $0.304 | +$0.033 | +3.3pp |
| stable+flat_slope | 235 | 32.8% | $0.339 | -$0.011 | -1.1pp |

**At T=300s:**

| Condition | n | CWR | Avg Cheap$ | EV/share | Edge |
|-----------|---|-----|-----------|----------|------|
| ALL_MARKETS | 664 | 18.1% | $0.200 | -$0.020 | -2.0pp |
| spread<0.10 | 39 | 48.7% | $0.482 | +$0.005 | +0.5pp |
| btc_vol<10bps | 286 | 25.2% | $0.234 | +$0.018 | +1.8pp |

**Finding:** EV is near ZERO across all conditions. The maximum positive EV is +$0.033/share for deceleration at T=600 (n=101), which translates to $0.83/25 shares — marginal at best and not consistent across datasets. The market prices cheap side correctly within ~2pp of its actual win rate.

---

### Test 3.4: Regime x Price Three-Way Analysis

**The most granular test — the "sweet spot" search:**

| Regime | Cheap Price | n | CWR | Avg Cheap$ | Edge | EV/25 shares |
|--------|-----------|---|-----|-----------|------|-------------|
| tight | $0.35-0.50 | 74 | 44.6% | $0.452 | -0.6pp | -$0.16 |
| medium | $0.25-0.35 | 32 | 21.9% | $0.325 | -10.7pp | -$2.66 |
| medium | $0.35-0.50 | 51 | 39.2% | $0.376 | +1.6pp | +$0.40 |
| wide | $0.05-0.15 | 209 | 4.8% | $0.092 | -4.4pp | -$1.11 |
| wide | $0.15-0.25 | 141 | 14.9% | $0.192 | -4.3pp | -$1.08 |
| **wide** | **$0.25-0.35** | **63** | **41.3%** | **$0.274** | **+13.8pp** | **+$3.46** |

**THE ONE POSITIVE RESULT:** Wide spread + cheap at $0.25-0.35 = CWR 41.3% vs implied 27.4% = +13.8pp edge, $3.46/25 shares.

**BUT:**
- Only 63 markets (across all 5 datasets) — ~0.35 markets/hour
- Wide spread at T=600s + cheap at $0.25-0.35 is a rare condition
- Needs per-dataset breakdown to check replication
- At 0.35 trades/hr and $3.46/trade: **$1.21/hr** — comparable to PHOENIX but much lower frequency and unvalidated

**This is the SINGLE finding from all Phase 1-3 research that shows any genuine edge.** Everything else is either tautological, dead, or near-zero.

---

## PHASE 4: Strategy Family Tests (3 implemented, 3 not)

### Test 4.1: Overreaction Detection

| Reaction Type | n Events | Avg 30s Reversion | Pct Reverts | CWR |
|--------------|---------|-------------------|-------------|-----|
| Overreaction | 18,819 | -0.008 | 43.5% | 28.5% |
| Underreaction | 8,265 | -0.003 | 0.1% | 15.8% |
| Proportional | 599 | -0.007 | 39.1% | 7.2% |

**Finding:** "Overreactions" show avg_reversion of -0.008 (NEGATIVE — they DON'T revert). Only 43.5% show any reversion, worse than coin flip. The market's initial reaction to BTC moves is, on average, an UNDERREACTION that continues, not an overreaction that reverts.

**Dead strategy.** Buying "overreaction dips" loses money.

---

### Test 4.3: Both-Side DCA

**Result:** 0 out of 519 markets achieved pair cost < $1.00 with time-separated DCA fills.
- Min pair cost: $1.0000
- Mean pair cost: $1.0346
- Max pair cost: $1.1250

**Verdict:** Family B (Gabagool-style both-side accumulation) is DEAD. Market efficiency prevents sub-$1 pair cost at any timescale. Confirmed by Test 1.5's correlation decay (r = -0.55 at 120s separation).

---

### Test 4.5: Cheap-First Probe

| Probe Bid | Fill Rate | CWR | Avg Best Pair Cost | Pair Viable % | Naked PnL |
|-----------|-----------|-----|-------------------|---------------|-----------|
| $0.15 | 67.0% | 9.8% | $0.905 | 64.6% | -$1.31 |
| $0.20 | 73.1% | 10.9% | $0.925 | 54.3% | -$2.06 |
| $0.25 | 78.6% | 14.3% | $0.945 | 48.6% | -$2.68 |
| $0.30 | 82.8% | 15.8% | $0.966 | 43.4% | -$3.56 |

**Finding:** Cheap probe at $0.15 has 9.8% CWR and -$1.31 naked PnL. Even the best pair cost achievable (avg $0.905 at $0.15 probe) is sub-$1 only 64.6% of the time — and this is the BEST case (cheapest possible expensive side during entire market).

In practice, filling both at sub-$1 pair cost requires the expensive side to dip to $0.75 while cheap is at $0.15, which happens only in volatile/spike moments that are hard to time. The avg naked PnL is negative at all probe levels because CWR < probe price (9.8% < 15%).

---

## SUMMARY: What All 17 Tests Tell Us

### Completely Dead (no signal, no edge):
1. **All BTC kinematic signals** (velocity, acceleration, jerk, momentum, deceleration, octants) — AUC 0.44-0.53 for cheap-side outcome
2. **Velocity toward strike** — 0pp CWR difference across buckets
3. **Post-spike recovery** — spikes DON'T revert, cheap continues dropping
4. **FADE bot footprint** — no detectable entry pulse at T=300
5. **Cross-side flow** — proven tautological
6. **Overreaction detection** — "overreactions" are actually underreactions
7. **Both-side DCA** — 0/519 sub-$1 pair costs
8. **Strike proximity** — nearly all markets at <50bps, no discrimination

### Tautological (measures outcome, not predictive):
9. **cheap_ask level** — higher price = higher WR (this IS the probability)
10. **Trajectory divergence** — winners had higher cheap price (circular)
11. **cheap_trajectory_slope** — positive slope = already winning
12. **Spread** — encodes cheap price level

### Weak/Marginal (needs more data):
13. **Cheap-side OBI** — AUC=0.72 on ONE dataset (OOS7, n=74). Most promising non-tautological signal. **Test 2.4 was NOT implemented** — this is the biggest gap.
14. **BTC range as regime filter** — low volatility = higher CWR, but no edge vs price
15. **Wide spread + $0.25-0.35 cheap** — +13.8pp edge, but only 63 markets total, 0.35/hr

### NOT TESTED (potentially valuable):
16. **Test 2.4: Temporal OBI dynamics** — if cheap-side OBI INCREASES over time, is that accumulation by smart money?
17. **Test 4.2: Binance-Polymarket repricing lag** — is there a latency window after BTC moves?
18. **Test 4.4: Imbalanced both-side building** — 70/30 allocation with our 97% FADE accuracy
