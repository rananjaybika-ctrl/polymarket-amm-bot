# DEEP ANALYSIS: Telegram Trading Ideas vs Our Experience

## Executive Summary

After analyzing 17 HTML files (~565K lines) from the Polymarket Lounge Telegram chat and comparing against our documented strategies (AGGRESSIVE, AGGRESSIVE_M_V2), this document provides a **skeptical, evidence-based assessment** of what might actually work.

**Bottom Line:** The chat confirms many of our failures are universal, but reveals **one potentially viable path we haven't fully explored: pure market-making with directional tilt** (the "Gabagool" approach). However, this requires capabilities we don't have and comes with hidden risks.

---

## Part 1: What The Chat Says vs What We Know

### 1.1 The 500ms Taker Delay - CONFIRMED UNIVERSAL PROBLEM

**Chat Claims:**
> "The 500ms delay kills most bots" (messages9.html)
> "my round trip time for FAK order went up from 100ms to 750ms after the new speed bump rule" (messages2.html)
> "I was making money on this till they introduced the 500ms taker speed bump. then the PNL dipped" (messages2.html)

**Our Experience:**
- AGGRESSIVE strategy (taker-based) showed $15.20/hr in backtest
- Live testing with realistic latency: **-$25/hr**
- Root cause: By the time our taker order executes (500ms later), the edge is gone

**Assessment: FULLY VALIDATED** - Taker-based strategies are dead. Everyone agrees on this.

---

### 1.2 The "Gabagool" Strategy - MOST DISCUSSED

**What The Chat Claims Gabagool Does:**

| Claim | Source | Credibility |
|-------|--------|-------------|
| Uses LIMIT orders (GTC), not takers | messages10.html:27319 | HIGH - multiple sources |
| Buys BOTH sides to get sum < $1 | messages11.html:5399 | HIGH - on-chain verified |
| NOT pure arbitrage | messages10.html:10187 | HIGH - agreed by most |
| Has directional bias (bets heavy on predicted winner) | messages12.html:22967 | MEDIUM - inference |
| Keeps accumulating loser side at 3-8c to lower avg | messages10.html:15979 | HIGH - on-chain verified |
| Holds to resolution, doesn't sell early | messages8.html | HIGH - tx data shows |

**Critical Quote (messages12.html:22967):**
> "Based on my research the bots that do this are doing two things:
> 1. They have a very good prediction of BTC price and they buy the side heavy so they are semi directional since the beginning, the hedging is the icing on the cake.
> 2. They are market makers just basically profiting from spreads and rebates"

**What This Actually Means:**
1. **Directional prediction is core** - not a hedge, but a bet with safety net
2. **Accumulation strategy** - keep bidding on loser side as it gets cheap
3. **Resolution-based profit** - not trying to flip positions for spread

**Comparison to Our AGGRESSIVE_M_V2:**

| Aspect | Gabagool (Inferred) | Our AGGRESSIVE_M_V2 |
|--------|--------------------|--------------------|
| Signal source | Unknown (possibly options skew, Deribit) | BTC spike detection |
| Entry timing | Throughout 15-min window | At spike detection |
| Position sizing | Asymmetric (heavy on winner) | Fixed 5 shares |
| Loser accumulation | Yes, down to 3-8c | No (single entry) |
| Exit | Hold to resolution | Hold to resolution |

**KEY INSIGHT:** Gabagool's edge may not be spike detection at all. It could be:
1. **Options market skew** - using Deribit/CME data we don't have
2. **Order flow analysis** - seeing whale orders before they move price
3. **Statistical model of price distribution** - knowing mean-reversion probabilities
4. **Spread capture over time** - not trying to predict direction at all

**Our Gap:** We focus on SPIKE detection (momentum). Gabagool may focus on MEAN-REVERSION (counter-momentum). These are opposite strategies.

---

### 1.3 Why Paper Trading Fails - CONFIRMED

**Chat Claims:**
> "Unfortunately, there's no way to do it on paper. You have to go live with small size and do trial and errors with real money... When you trade on the orderbook you place orders and other bots can buy your orders, they can spoof bid/ask before you place etc..." (messages10.html:3483)

> "your simulation has no latency basically, youre not competing with anyone, which is why you win" (messages9.html)

**Our Experience:**
- Backtests showed $15.20/hr for AGGRESSIVE
- Live: dramatically worse due to:
  - Fill rate assumptions wrong
  - Latency not simulated
  - No adverse selection modeling

**Assessment: FULLY VALIDATED** - Our backtests overestimate by potentially 10x or more.

---

### 1.4 Regime Dependency - CONFIRMED UNIVERSAL

**Chat Claims:**
> "my strategy is garbage when no volatility" (messages10.html:5679)
> "Ive got a live model i run which has been hitting 88% win rate in low/moderate vol markets, but only 52% - a coinflip - when market vol increases" (messages15.html:33871)
> "Would have an amazing day and then have a terrible day. Now just market making" (messages8.html:22207)

**Our Experience:**
- IS+OOS2 (low vol): AGGRESSIVE worked well
- OOS9 (trending): AGGRESSIVE_M_V2 lost -$50.63
- Momentum filter study: OOS9 has different regime characteristics

**Assessment: FULLY VALIDATED** - No strategy works in all regimes. Our current approach of tuning one strategy won't work.

---

## Part 2: Strategy Ideas From Chat - Critical Analysis

### 2.1 IDEA: Late-Second Sniping (0.95+ Entry)

**The Claim (messages13.html:5503):**
> "Find markets with end time <=180s, Up/Down Odds at 0.95 or higher, Put Limit order to buy at 0.95, Sell Order at 0.99 once you get filled. You made 4%."

**The Debunk (messages4.html:7987):**
> "I don't want to be the party pooper, but this strategy is mathematically doomed. If you're buying at 99.5 cents in the final seconds, you're risking 99.5 to make 0.5. You need to be right 200 times just to cover one loss."

**Our Analysis:**
- This is asymmetric payoff: Win 5c, Lose 95c
- Need 95% accuracy just to break even
- One "turkey" wipes out 19 wins
- Same problem as our "skip rule" for $0.90+ entries

**Verdict: AVOID** - We already know this doesn't work from our skip rule analysis.

---

### 2.2 IDEA: Hedging via Merge Instead of Selling

**The Claim (messages16.html:1783):**
> "if you are talking about m15, up and down shares the same book so its a hedge indeed, and you can merge both before expiration without paying any fee or slipping"

**The Claim (messages16.html:14739):**
> "Hedging is faster and can be done rougher and saves valuable time. There are three states of an order: CLOB confirmed -> Blockchain Mined -> Blockchain confirmed. For hedging you basically just need the first one"

**Our Current Approach:**
- AGGRESSIVE hedges by placing bid on loser side
- If not filled, uses time-stop (sell at market)

**What Merge Offers:**
1. **Instant exit** - no need to wait for loser bid to fill
2. **No slippage** - 1 YES + 1 NO = exactly $1
3. **No fees** - merge is free

**Gap in Our Strategy:**
- We don't use merge
- We sell via time-stop which incurs taker fees + slippage
- This could be costing us 2-3% per exit

**Verdict: WORTH IMPLEMENTING** - Low effort, clear benefit.

---

### 2.3 IDEA: Directional with Hedge Protection (Asymmetric Sizing)

**The Claim (messages12.html:23335):**
> "yes i pulled trades of some awesome bots from twitter and they are always directional when they enter. the only way is they have a good sense of where the market might go from the beginning. Believe me they loose a lot!!! they just have a lot of capital to offset losses when they are winning they go aggressively"

**What This Means:**
1. Start with directional bias (e.g., 70% confident UP wins)
2. Buy 70 shares UP at, say, $0.55
3. Buy 30 shares DOWN at, say, $0.40
4. Total cost: 70 × $0.55 + 30 × $0.40 = $38.5 + $12 = $50.5
5. If UP wins: Get 70 × $1 = $70 → Profit $19.5
6. If DOWN wins: Get 30 × $1 = $30 → Loss $20.5

**The Math:**
- If 70% accurate: 0.7 × $19.5 + 0.3 × (-$20.5) = $13.65 - $6.15 = **$7.50 EV**
- Contrast with our fixed sizing: 0.7 × $5 + 0.3 × (-$5) = $2 EV

**Why This Might Work:**
- Asymmetric sizing amplifies edge when you're right
- Hedge limits downside when wrong
- Capital efficiency: Don't need 90% accuracy, just 60-70%

**Why We Haven't Tried This:**
- Requires confidence score for sizing
- More complex position management
- Capital requirements higher

**Verdict: PROMISING** - Aligns with Gabagool's observed behavior. Worth exploring.

---

### 2.4 IDEA: Sports/Event Frontrunning with Premium Data

**The Claim (messages11.html:18879):**
> "For traditional sportsbooks the odds/betting is freezed multiple seconds before a goal is scored... I believe this does not happen on polymarket, and 'frontrunning' polymarket with the knowledge from an API f.ex. might prove beneficial."

**The Rebuttal (messages11.html):**
> "sports market are the ones with the highest volumes on Poly - you can bet it's already full of bots doing it. If they are big bots they can even pay for premium sports data feeds"

**Our Situation:**
- We don't have premium sports data
- We're focused on BTC 15-min markets
- Sports markets are different structure (order books cleared at game start)

**Verdict: NOT APPLICABLE** - Requires infrastructure we don't have.

---

### 2.5 IDEA: Time-of-Day Filtering

**The Claim (messages8.html:565):**
> "WIZARD SLEEPING - Skipping 04:00 UTC - Reason: Asia/Europe handoff - stop hunting zone"

**The Claim (messages12.html:13663):**
> "I found out my strategy performing bad during UTC+2 night (23-7) - I've had 3 stop losses during this hours 3 nights in a row"

**Our Data:**
- We have 167 hours of backtest data
- We haven't analyzed by time-of-day

**Action Required:**
- Segment our existing results by UTC hour
- Identify if certain hours are consistently unprofitable
- Simple filter to add

**Verdict: QUICK WIN** - Low effort, potentially high impact.

---

## Part 3: Anti-Patterns Confirmed

### 3.1 Things NOT to Do (Validated by Chat + Our Experience)

| Anti-Pattern | Chat Evidence | Our Evidence |
|--------------|---------------|--------------|
| **Taker orders on 15-min** | "500ms speedbump kills bots" | AGGRESSIVE failed live |
| **Copy trading bots** | "you become exit liquidity" | N/A (didn't try) |
| **Scaling too fast** | "increased too fast, gave profits back" | N/A |
| **Trusting paper trading** | "paper testing impossible for fills" | Backtest overestimated by 10x |
| **Buying GitHub bots** | "edge you jump off after getting drained" | N/A |
| **Rebate-only strategy** | "not worth it, can change anytime" | N/A |
| **High win-rate asymmetric** | "1 loss wrecks 20-100 trades" | Skip rule turkey problem |

---

## Part 4: What The Chat Gets WRONG

### 4.1 "Gabagool is doing pure arb"

**The Chat Claim:**
> "if you know the entry and how the sizing works. he is absolutely doing arbs"

**The Reality:**
Multiple sources in the same chat contradict this:
> "You can't ever buy Yes + No at the same time for <1$"
> "arbitrage never really worked"

**Our Understanding:**
Polymarket uses a SINGLE order book for both sides. A BUY YES at $0.60 is automatically a SELL NO at $0.40. **True simultaneous arb is mathematically impossible.**

What Gabagool does is **time-distributed accumulation** - not arbitrage.

---

### 4.2 "Latency is everything"

**The Chat Claim:**
> "You need to be under 50ms to compete"

**The Counter-Evidence (messages11.html:2191):**
> "i can give you zero latency and you aren't replicating this. It may be in rust but latency is not the strategy here"

**Our Understanding:**
- For MAKER orders (GTC), latency doesn't matter for fills
- What matters is QUEUE POSITION (time-priority at each price level)
- Strategy matters more than speed for limit orders

---

### 4.3 "Just use Binance as signal"

**The Chat Claim:**
> "It's almost 99% correlation for Binance ticker and clob yes token price + latency"

**Our Data:**
- BTC velocity correlation: r = 0.055 (essentially zero)
- 60Hz Binance data provides NO predictive edge
- The correlation is LAGGING, not leading

**Why This Matters:**
- Chat assumes Binance leads Polymarket
- Our data shows they're synchronized (arbitrageurs keep them aligned)
- By the time you see a Binance move, it's already priced in

---

## Part 5: New Strategy Framework

Based on this analysis, here's what we should consider:

### 5.1 ABANDON: Pure Spike Following

**Reason:**
- 500ms taker delay killed it
- Maker version has adverse selection (fills only happen when signal is wrong)
- Chat confirms this is universal

### 5.2 EXPLORE: Market Making with Directional Tilt

**The Approach:**
1. **Don't try to predict direction precisely** - be a market maker
2. **Quote on both sides** - provide liquidity
3. **Tilt quotes based on signal** - wider spread on uncertain side
4. **Accumulate loser at cheap prices** - like Gabagool
5. **Hold to resolution** - don't panic sell

**Why This Might Work:**
- 0% maker fees + potential rebates
- No 500ms delay for limit orders
- Spread capture is more reliable than direction prediction
- Our AGGRESSIVE_M_V2 finding: 91.5% of signals achieve pair_cost < $1.00

**What We Need:**
- Confidence score → position sizing
- Continuous quoting logic (not just at spike)
- Accumulation strategy for loser side

### 5.3 QUICK WINS: Easy Improvements

| Improvement | Effort | Expected Impact |
|-------------|--------|-----------------|
| Use MERGE instead of time-stop sell | Low | Save 2-3% per exit |
| Time-of-day filter | Low | Avoid known bad hours |
| Momentum filter (0.1% threshold) | Done | +22pp regime filtering |

### 5.4 REQUIRES RESEARCH: Unanswered Questions

1. **What's Gabagool's actual signal?** - Not spike, possibly options skew
2. **What's the optimal loser accumulation strategy?** - At what prices do we bid?
3. **How do we size asymmetrically?** - Need confidence score methodology
4. **What's queue position impact?** - How long to wait for fills?

---

## Part 6: Recommended Next Steps

### Immediate (Today)
1. **Implement MERGE for exits** - Replace time-stop sell with merge
2. **Analyze by time-of-day** - Find if certain hours are unprofitable
3. **Add momentum filter to live** - 0.1% threshold, circular buffer

### Short-Term (This Week)
4. **Build market-making backtest** - Continuous quoting, not just spike-triggered
5. **Test asymmetric sizing** - Confidence-weighted positions
6. **Analyze loser accumulation** - At what prices does avg_sum < $1 become possible?

### Medium-Term (If Above Works)
7. **Explore alternative signals** - Options skew, order flow, etc.
8. **Multi-regime strategy** - Different parameters per volatility regime
9. **Live testing with small size** - Paper trading is worthless per chat consensus

---

## Appendix A: Key Quotes by Topic

### On Gabagool's Strategy
> "He is basically market maker using limit orders. He is quoting all the time, I would say at best bids and lower." (messages10.html:27319)

> "if you look closely, gabagool doesn't end here. he/she keeps trading and bring the avg for losing side down when it's in 3-8c range making his/her avg<1 by the end" (messages10.html:15979)

### On Why Speed Doesn't Matter for Makers
> "Speed only matters if you FOK or FAK, if you GTC, you are good to go" (messages9.html)

> "limit orders are FCFS [First Come First Served] - so whatever limit order was set first, gets filled first" (messages7.html)

### On Regime Dependency
> "I found out my strategy performing bad during UTC+2 night (23-7)" (messages12.html:13663)

> "Ive got a live model i run which has been hitting 88% win rate in low/moderate vol markets, but only 52% - a coinflip - when market vol increases" (messages15.html:33871)

### On Paper Trading Futility
> "On paper I had 3/4 strategies that were working awesomely but then reality hit hard" (messages10.html:3483)

---

## Appendix B: Our Strategy Performance Summary

| Strategy | Backtest $/hr | Live Reality | Root Cause |
|----------|---------------|--------------|------------|
| AGGRESSIVE (taker) | $15.20 | ~-$25 | 500ms delay, 2% fees |
| AGGRESSIVE_M_V2 (fade) | $1.50-2.00 | Unknown | Adverse selection on fills |
| CHEAP (buy $0.10) | $1.18-3.70 | Unknown | Regime dependent |

---

## Appendix C: Files Referenced

| File | Purpose |
|------|---------|
| `research/strategies/AGGRESSIVE.md` | Original taker strategy spec |
| `research/strategies/AGGRESSIVE_M_V2.md` | Fade strategy spec |
| `research/findings/AGGRESSIVE_M_V2_REVISED_FINDINGS.md` | Adverse selection discovery |
| `research/backtests/trend_filter_test.py` | Momentum filter analysis |

---

*Analysis completed: Feb 7, 2026*
*Sources: 17 Telegram HTML files (~565K lines), 4 strategy documents*
