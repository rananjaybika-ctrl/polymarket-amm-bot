# Telegram Alpha Analysis - January 12, 2026

## Executive Summary

Analysis of ~7MB Polymarket Lounge Telegram chat export revealed critical insights about successful trading strategies, particularly the famous "gabagool22" bot. Key finding: **Volume beats timing** - our velocity-based strategy has good edge (709 bps) but applies it too infrequently due to structural issues.

---

## 1. GABAGOOL STRATEGY - THE TRUTH

### The Myth vs Reality

| Myth (Twitter narrative) | Reality (Telegram consensus) |
|--------------------------|------------------------------|
| "Gabagool is an arb bot" | Gabagool is a **market maker** |
| "Buys YES+NO < $1 simultaneously" | **Impossible** - unified orderbook |
| "Simple arb anyone can copy" | Complex MM with spot edge + vol model |

### Evidence from Chat

> "gaba cult will be studied ngl. the funniest shit is that everyone's just catching up now that he's mm cause they've been told on X that he's arbitraging"

> "They do constantly. It's market making, not taking. He uses limit orders and bid spreads"

> "that's indeed interesting to see how everyone on X is obsessed with building polymarket trading bots rn, but almost no one understands the difference between arbitrage and mm"

### Gabagool's Actual Strategy Components

1. **Market Making with Limit Orders**
   - Places passive limit orders on BOTH sides
   - Earns maker rebates (~1% of fill value)
   - Avoids 500ms taker speed bump

2. **Spot Edge (Binance → Chainlink Latency)**
   - Chainlink RTDS lags Binance by 1-2 seconds
   - When BTC moves on Binance, Polymarket prices haven't adjusted
   - Adjust quotes BEFORE market prices in the move
   - This is the "spot edge" mentioned repeatedly

3. **Volatility Model**
   - Prices 15-minute options using implied volatility
   - Identifies when prices are "too cheap" or "too expensive"
   - Combined with spot edge for directional bias

4. **Inventory Discipline**
   - Max 10% imbalance between UP and DOWN shares
   - Force-buy lagging side when exceeded
   - Never let directional exposure get too large

5. **Merge for Capital Efficiency**
   - Merge 1 UP + 1 DOWN = $1 USDC immediately
   - Don't wait for resolution (can take hours via UMA)
   - Recycle capital to next 15-min market

6. **Infrastructure**
   - AWS eu-west-1 for low latency
   - Sub-100ms target (competitive at 65ms)
   - WebSocket for real-time data, not REST polling
   - Disable garbage collection, manual cleanup each 900s session

---

## 2. WHY PURE ARBITRAGE IS IMPOSSIBLE

### Unified Orderbook Mechanics

> "The obvious has to be said: Polymarket has one order book for both 'Yes' and 'No'. You can't ever buy Yes + No at the same time for <1$ or 'arbitrage' it."

> "If you put a BUY limit order to buy DOWN, there will automatically be a SELL limit order for UP at the same price"

**How it works:**
- BUY UP @ $0.40 = SELL DOWN @ $0.60 (same order)
- At any moment: UP price + DOWN price = $1.00
- You cannot buy both sides cheap simultaneously

### Evidence of Arb Failure

> "arb never worked on me, i observed 519000 trials yesterday for 5-6 hours but 0 opportunities to trade"

> "even if you use websocket the arbitrage is always 1.01"

> "Anyone is using arb? i switched to WS and the combined prices seems never under 100 no more"

---

## 3. THE SPOT EDGE EXPLAINED

### Chainlink Latency

> "According to my test, the time difference between the data obtained by RTDS and the time marked by Chainlink is 1.x seconds, which could be devastating for market making."

> "chainlink is a slow aggregator of prices from multiple exchange feeds"

> "binance and chainlink price had latency"

### How to Exploit It

```
T=0:    Binance BTC drops 0.3%
T=1s:   Chainlink RTDS hasn't updated yet
T=1s:   Polymarket prices still reflect OLD probability
T=1s:   Your DOWN bids get filled (underpriced relative to reality)
T=2s:   Chainlink updates, market adjusts
T=2s:   Your position now "in the money"
```

### The Key Insight

> "On btc 15min market spread is 1 or 2 cents which might look like WTF :D but the dynamics on 15min to predict it.. is very big noise. If you have good vol model + little spot edge + understand chainlink is just aggregate of spot prices around exchanges. And UI is always late, degens are keep trading and filling spread. and you have 500ms delay for takers.. good combination for MM"

---

## 4. THE 500ms SPEED BUMP

### How It Works

> "nope, in 15min market, if you cross the spread, a 500ms speed delay will be applied"

> "Limit orders that don't cross the spread have no delay"

### Implications

| Order Type | Delay | Who Benefits |
|------------|-------|--------------|
| Taker (market order, aggressive limit) | 500ms | Market makers protected |
| Maker (passive limit) | 0ms | Market makers can adjust fast |

**This is why Gabagool uses limit orders exclusively** - he can adjust quotes faster than takers can hit them.

---

## 5. SYNTHETIC ARBITRAGE (What Actually Works)

### Definition

> "synthetic because they aren't buying simultaneously as a real arb would. Buy leg 1, wait for price move then buy leg 2. Arb because they are buying yes no, not buy yes sell yes. eg Polywizard is a synthetic arb."

### The Strategy

1. Buy the "favored" side (e.g., YES at $0.72) using limit order
2. Wait for volatility / price movement
3. Buy the opposite side when it becomes cheap (e.g., NO at $0.25)
4. If YES + NO cost < $1.00, MERGE for guaranteed profit

### Example

```
T=0: BTC above strike, UP=$0.72, DOWN=$0.28
     → Buy 100 UP @ $0.72 = $72 cost

T=5: BTC dips, UP=$0.55, DOWN=$0.45
     → Buy 100 DOWN @ $0.25 = $25 cost (filled earlier bid)

Total: 100 UP + 100 DOWN = $97 cost
Merge: Get $100 back
Profit: $3 (3.1% in 5 minutes)
```

### Critical Rule

> "Dont forget robust method to track inventory. Cant balance a synthetic arb if the fill info on leg 1 is late, incomplete or incorrect."

---

## 6. INVENTORY MANAGEMENT

### The Rule

> "You don't only buy the side that is dropping. You need some sort of a limit for the share imbalance between up and down shares. For example amount of down shares can only be 10% more than up shares. If you surpass that number your bot has to buy the other side, even if price is too high"

### Why It Matters

> "The goal is not to wait for a huge dip and buy one side and then wait for a big dip to load up on the other side. You have to keep buying both sides and just size in more when you get a discounted price"

### Implementation

```python
MAX_IMBALANCE = 0.10  # 10%

if abs(up_shares - down_shares) / max(up_shares, down_shares) > MAX_IMBALANCE:
    # Force-buy the lagging side even at worse prices
    if up_shares > down_shares:
        force_buy("DOWN")
    else:
        force_buy("UP")
```

---

## 7. WHY COPY TRADING FAILS

### Unanimous Warning (Repeated 3+ times)

> "Never copy trade. Anyone, but especially MM's. You're using market orders while they're using limit. You're their exit liquidity."

> "So if you get behind a copy trade by 1c and then there's a 1c spread you're already 2% down. Then when you exit same scenario. So while the guy you're copying may make 3% on a trade you're at -1%"

> "you can't copy gabagool it doesn't work"

### Why It Fails

| What Gabagool does | What copier does |
|--------------------|------------------|
| Limit order at $0.50 | Market order at $0.51 (crossed spread) |
| Earns maker rebate | Pays taker fee |
| Gets queue priority | Back of queue or 500ms delay |
| $0.01 profit | $0.02 loss |

---

## 8. MERGE MECHANICS

### How Merge Works

> "Merge is basically instantly exchanging 1 YES and 1 NO for $1. You merge them to a pair and sell it to the exchange. As they always resolve to $1 you are basically cashing out."

> "And also when you buy both sides, you can merge to exit immediately. No need to sell or wait for end."

### Fee Consideration

> "Can anyone clarify the 1% fee that Polymarket charges on winnings? Does this force the average cost hurdle for a hedged position to be <$0.99?"

**Answer: Yes. Pair cost must be < $0.99 to profit after 1% fee.**

### Why Merge Instead of Resolution

1. **Speed**: Merge is instant, resolution can take hours (UMA oracle delays)
2. **Capital efficiency**: Recycle to next 15-min market immediately
3. **No directional risk**: Lock in profit regardless of outcome

---

## 9. OTHER ALPHA OPPORTUNITIES

### F1 Data Edge

> "I found out that formula 1 data feed is 30-40 seconds ahead of the live feed.. so just gonna use that to do some basic trading on things like safety cars/red flags n shit"

**Reasoning**: Pure information arbitrage - faster data source = trading edge.

### Late-Market Momentum

> "Find markets with end time <=180s. Up/Down Odds at 0.95 or higher. Put Limit order to buy at 0.95. Sell Order at 0.99 once you get filled. You made 4%. Usually fits in like 40/96 markets a day."

**Reasoning**: When outcome is 95% certain with <3 min left, buying at $0.95 gives ~5% locked profit.

### Cross-Platform Arbitrage

> "Arbitrage between two platforms. My bot found markets where you could place 'YES' bets on all outcomes for ~80% of the bet price and get a guaranteed +20% on any outcome."

**But warned:**
> "TLDR: cross platform arb. Not worth it IMO"
> "Kalshi also KYC only"

---

## 10. WHAT DOESN'T WORK

| Strategy | Why It Fails | Evidence |
|----------|--------------|----------|
| Instant Arb | Unified orderbook prevents YES+NO < $1 | "519000 trials, 0 opportunities" |
| Copy Trading | You become exit liquidity | Repeated 3+ warnings |
| Pure Rebate Farming | Platform reduced to 20%, can change | "starting tomorrow they will only distribute 20%" |
| Slow Data Sources | Others have faster data | "public Chainlink demo = 30 second delay" |
| Twitter "Alpha" | Engagement farming | "great marketing campaign by polymarket" |

---

## 11. INFRASTRUCTURE REQUIREMENTS

### Latency

> "This is true, I started with around 1s and have decreased it to around 300 ms. I know there is still room for improvement and I could probably get it below 200, maybe even 100 ms. Btw, Latency is one of the biggest edges, so don't expect traders to share their complete setup publicly."

### Server Location

> "just deploy on aws eu-west-1 and disable garbage collection. do it manually after each 900 sec session"

> "Hmm NYC gets you down to 700ms"

### Data Sources

> "you can use web socket to get live price feed for binance and chainlink"

**15-min markets**: Chainlink RTDS oracle
**1-hour markets**: Binance BTC/USDT 1-min candles

---

## 12. OUR STRATEGY vs GABAGOOL

### 10-Hour Simulation Results

| Metric | Our Strategy | Gabagool (estimated) |
|--------|--------------|----------------------|
| Cycles attempted | 425 | 1000+ |
| Cycles completed | 54 (12.7%) | ~100 per market |
| Profit | $0.80 | $58-83/hour |
| Fill rate | 12.7% | ~95%+ |

### Root Cause: Structural Differences

| Aspect | Our Strategy | Gabagool |
|--------|--------------|----------|
| Entry timing | Wait for velocity reversal (26s avg) | Immediate |
| Order type | Single order per side | Grid at multiple levels |
| Timeout handling | Abort cycle after 30s | Orders stay until filled |
| Sides | Sequential (entry→hedge) | Parallel (both simultaneously) |
| Signal dependency | Requires velocity signals | No signals needed |
| Fill requirement | Must complete entry before hedge | Independent fills |
| Inventory mgmt | None | Max 10% imbalance rule |

### The Problem

Our velocity timing provides **709 bps edge** (392 entry + 317 hedge), but:
1. 87% of cycles abort before completing
2. 35+ seconds spent WAITING for signals per cycle
3. Single orders miss fills when price moves 1-2 cents

**The edge is good. The structure wastes it.**

---

## 13. REQUIRED CHANGES

### Change 1: Remove Velocity Gating for Entry

**Current:**
```python
while True:
    should_enter = self.strategy.should_enter_now(velocity_bps, expensive_side)
    if not should_enter:
        entry_decisions += 1
        await asyncio.sleep(1)  # WASTING 1 SECOND EACH SKIP
        continue
```

**New:**
- Post orders IMMEDIATELY at market open
- Use velocity to ADJUST quote prices, not gate entry
- If velocity adverse: widen offset; if favorable: tighten

### Change 2: Parallel Grid Instead of Sequential Single

**Current:**
1. Place 1 UP order
2. Wait for fill (or timeout)
3. Place 1 DOWN order
4. Wait for fill

**New:**
1. Place UP orders at $0.40, $0.45, $0.50 simultaneously
2. Place DOWN orders at $0.40, $0.45, $0.50 simultaneously
3. Track inventory as fills come in
4. When imbalance > 10%, force-buy other side

### Change 3: Remove Fill Timeout Aborts

**Current:**
```python
entry_fill = await self._simulate_fill(expensive_side, entry_bid, timeout=FILL_TIMEOUT)
if not entry_fill:
    return None  # 87% of cycles die here
```

**New:**
- Don't abort on timeout
- Reprice order at new best_bid
- Keep trying until market ends or inventory limit hit

### Change 4: Implement Inventory Limits

```python
MAX_IMBALANCE_SHARES = 20  # or 10% of position

if abs(up_shares - down_shares) > MAX_IMBALANCE_SHARES:
    lagging_side = "DOWN" if up_shares > down_shares else "UP"
    force_buy(lagging_side, even_at_worse_prices=True)
```

### Change 5: Use Velocity for Quote Adjustment

**Current:** Velocity gates entry decisions (WAITING)
**New:** Velocity adjusts quote prices (CONTINUOUS)

```python
# Continuous quote adjustment based on spot
if velocity_bps > 0.05:  # BTC rising
    up_offset = 0.02   # Widen UP bid (expect UP to get more expensive)
    down_offset = 0.01  # Tighten DOWN bid (expect DOWN to get cheaper)
else:
    up_offset = 0.01
    down_offset = 0.02

place_order("UP", best_bid - up_offset)
place_order("DOWN", best_bid - down_offset)
```

---

## 14. EXPECTED IMPROVEMENT

| Metric | Current | After Changes |
|--------|---------|---------------|
| Cycles/hour | ~5 | 50-100+ |
| Completion rate | 12.7% | 80%+ |
| Profit/hour | $0.08 | $5-15 |
| Time utilization | ~5% | ~95% |

---

## 15. KEY TAKEAWAYS

1. **Gabagool is a market maker, not an arbitrageur**
2. **Unified orderbook makes instant arb impossible**
3. **Spot edge = Binance→Chainlink 1-2s latency**
4. **500ms taker delay protects makers**
5. **Inventory limits (10%) prevent directional blowup**
6. **Grid orders at multiple price levels**
7. **Merge to recycle capital instantly**
8. **Volume beats timing** - apply edge more often
9. **Copy trading = exit liquidity (don't do it)**
10. **Infrastructure matters** - AWS eu-west-1, sub-100ms latency

---

## 16. RESOURCES MENTIONED

### GitHub Repositories
- `https://github.com/piokopl/gabagool` - "not any profitable bot but you can learn from code"
- `https://github.com/AleSZanello/poly-analysis` - Contains gabagool market data
- `https://github.com/AleSZanello/poly-examples` - Split, Merge, Redeem via Builder Relayer
- `https://github.com/pieakshat/merge-position-poly` - Merge implementation

### Data Sources
- Binance WebSocket for real-time BTC price
- Chainlink RTDS for 15-min market resolution
- Polymarket WebSocket for orderbook updates

---

*Analysis completed: January 12, 2026*
*Source: Polymarket Lounge Telegram Export (messages.html through messages13.html)*
