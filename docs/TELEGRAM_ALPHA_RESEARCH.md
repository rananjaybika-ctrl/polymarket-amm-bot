# Polymarket Alpha Research - Telegram Chat Analysis
> Extracted from Polymarket Lounge Telegram (January 2026)

---

## Table of Contents
1. [VPS & Infrastructure](#1-vps--infrastructure)
2. [Market Making Mechanics](#2-market-making-mechanics)
3. [Trading Strategies](#3-trading-strategies)
4. [Gabagool & Whale Analysis](#4-gabagool--whale-analysis)
5. [Key Resources](#5-key-resources)
6. [Critical Warnings](#6-critical-warnings)

---

## 1. VPS & Infrastructure

### Best VPS Locations (Ranked)

| Rank | Location | Latency to Origin | Notes |
|------|----------|-------------------|-------|
| 1 | **Ireland (Dublin)** | ~70-80ms | "Ireland wins", "Always Ireland", AWS eu-west-1 |
| 2 | **Netherlands (Amsterdam)** | ~80-90ms | "74ms for HTTP API" reported |
| 3 | **Sweden** | ~100ms | Privacy-friendly, njal.la accepts crypto |
| 4 | **UK (London)** | BLOCKED | "Yeah don't do London. Afaik you get blocked now by cloudflare" |
| 5 | **Germany (Frankfurt)** | Mixed | Some report blocks, others work fine |

### Where Are Polymarket Servers?
> "Servers are in London so Ireland is closer"
> "the clob servers are in west europe"
> "Polymarket is on AWS"

### Geo-Blocked Countries
- **USA** - Fully blocked
- **UK (London)** - Blocked by Cloudflare
- **France** - Blocked
- Some German IPs getting blocked recently

### Recommended VPS Providers

| Provider | Location | Price | Notes |
|----------|----------|-------|-------|
| **Njalla (njal.la)** | Sweden | ~$60/yr | Privacy, crypto payments, "Mullvad + njalla combo for winning" |
| **AWS EC2** | eu-west-1 | $5-20/mo | Best reliability, needs proxy for some |
| **Hetzner** | EU only | €4-5/mo | Cheapest, "great for data feeds, not live trading" |
| **Contabo** | Germany | €6/mo | Mixed reports on blocking |

### Infrastructure Best Practices

**Server Security:**
> "If you're running from a server with a private key on it: root disabled, password disabled, ssh only and fail2ban enabled"

**Development Setup:**
> "Set your terminal up with tmux and ohmyzsh"
> "deploy on aws eu-west-1 and disable garbage collection"

**VPN:**
> "if you really wanna use a good vpn you should use mullvad - they never track your data"

### Latency Benchmarks

| Setup | Latency | Notes |
|-------|---------|-------|
| Ireland AWS | 36ms avg | "python cant go less than that" |
| Netherlands | 74ms | HTTP API order placement |
| Home network | 1000-1100ms | Round trip |
| Target for competitiveness | <100ms | "In theory if you are 30ms you would get filled on 95% of your orders" |

### The 500ms Speed Bump (CRITICAL)
> "500ms delay for taker orders"
> "my round trip time for FAK order went up from 100ms to 750ms after the new speed bump rule"
> "The 500ms delay kills most bots"
> "With the 500ms speed bump it's not a game changer" (for low latency optimization)

**Implication:** Ultra-low latency matters LESS for taker strategies now. Maker strategies still benefit.

---

## 2. Market Making Mechanics

### How MMs Pull/Cancel Bids

**The Core Problem:**
> "You get rolled over if you're not quick enough to pull your quotes when Binance moves"

**Quote Management:**
- Use short expiration times (GTD orders) with 5-65 second expiry
- "I use 65 seconds expiry but the bot automatically cancels all unfilled orders after 5 seconds"
- Cancel-replace pattern: "if an order isn't bought in 20 seconds the bot cancels it"

### Price Sources

| Market Type | Resolution Source | Speed Source |
|-------------|-------------------|--------------|
| **15-minute** | Chainlink RTDS | Binance WebSocket |
| **1-hour** | Binance | Binance |

**Key Quotes:**
> "1 hour uses binance for feed, 15 min uses chainlink"
> "It would be foolish to use any other price source than the chainlink RTDS"
> "chainlink is a slow aggregator of prices from multiple exchange feeds"
> "most of the time binance is quicker"

**Latency Arbitrage:**
> "there are latency opportunities between chainlink and binance spot prices, but the strategy will fail at 2+ seconds executions"

### Order Types for MMs

| Type | Use Case | Notes |
|------|----------|-------|
| **GTC** | Primary MM method | "GTC and handle cancels yourself" |
| **FOK** | Taker strategies | High failure rate, 500ms delay |
| **FAK** | Partial fills | Creates unbalanced positions |
| **GTD** | Auto-cleanup | Short expiration for safety |

> "Speed only matters if you FOK or FAK, if you GTC, you are good to go"

### Staying Delta Neutral

**Core Principle:**
> "This idea of buying a dollar for less than a dollar, while keeping running balanced inventory ratio"
> "He buys hundreds of times every second and never allows his balance of up and down shares to be highly imbalanced"

**Balance Management:**
> "You need some sort of a limit for the share imbalance between up and down shares. For example amount of down shares can only be 10% more than up shares."
> "More trades that move with the market. You cannot do a little amount you need to do many throughout the market time."

**Balance by Share Count, NOT Dollar Value:**
> "It is - on shares numbers. Let's say it buys 1500 shares at 0.15 and 1500 shares at 0.84. If you look at $ value ofc it looks imbalanced, but shares number is similar."

### Why 500ms Speed Bump Protects MMs

> "They're speed bumped (500ms on market orders), at least that's what I see from my data, so you could be slow as a MM and still survive."
> "On btc 15min market spread is 1 or 2 cents... If you have good vol model + little spot edge + understand chainlink + 500ms delay for takers.. good combination for MM"

---

## 3. Trading Strategies

### The Gabagool Strategy (Market Making)

**Core Formula:**
```
min(Qty_YES, Qty_NO) - (Cost_YES + Cost_NO) > 0
```

**How It Works:**
1. Place limit orders on BOTH sides continuously
2. Keep combined average cost < $1
3. Build position over 15-minute window
4. Merge positions to lock in profit (don't wait for resolution)

> "He is basically market maker using limit orders. He is quoting all the time, I would say at best bids and lower."
> "dude all he is doing is buying up+down for less than $1 how complex can it be"

### Entry Signals

**When to Enter:**
> "I only buy if favored side is bigger than 0.75c and place an order at the other side of at least 0.20c"
> "Only bet from 0.75 to 0.9 if reached 0.99 I already merged my positions"

**Which Side First?**
> "I'm buying the cheaper side first"
> "If you are trying to buy the cheap side first while the expensive goes up you'll lose the leg"

**The Leg Strategy:**
> "Make a balance_leg function. For example if you buy 10 shares at 75, If Order_up_filled and actual_price_up < 74, Then order_buy_Down 30 at 30 cents, Order_buy_up 20 at 65 cents."

### Exit Strategy

**Merge vs Wait:**
> "when you buy both sides, you can merge to exit immediately. No need to sell or wait for end."
> "this is what he mentioned doing, he merges instead of redeems"

### Risk Management

**Position Sizing:**
- Minimum 5 shares for limit orders
- Minimum $1 for market orders

**Loss Patterns:**
> "He loses nearly 15% of his trades. Wins just outweigh the losses."
> "yeah gabagool sometimes loss up to $200"

**Hedging When Market Flips:**
> "He buys the other side in case it flips doesnt have Major losses"
> "When that happen you have to buy A LOT of the overpriced leg"

### Strategies That DON'T Work

**Last-Second Sniping:**
> "Last minute strategy is the worst man, don't waste your time there"
> ".99 strategy is not going to work. I have months of data collection"
> "last second reversals happen way more often than you think"

**Copy Trading:**
> "Never copy trade. Anyone, but especially MM's. You're using market orders while they're using limit. You're their exit liquidity."

**Paper Trading:**
> "Paper trade is BS. To improve the strategy you should lose some money. Test with real money."
> "On paper I had 3/4 strategies that were working awesomely but then reality hit hard."

---

## 4. Gabagool & Whale Analysis

### Gabagool22 Profile

| Attribute | Value |
|-----------|-------|
| Profile | polymarket.com/@gabagool22 |
| Alt Account | polymarket.com/@gabagool-inv |
| Registration | October 2025 |
| Daily Profit | ~$10,000 |
| Trades/Market | 100-1000+ |
| Order Type | 100% Limit Orders (GTC) |
| Exit Method | Merging |

**Key Insights:**
> "He is MM with best vol model and maybe some edge in spot moves"
> "wrong, I studied him for months and he is using a math formula that calculates the 'fair value' of both sides then position based of imbalance"
> "im not yet done because im still stuck on what time frame they are using to calculate the fair volatility"

### Other Whales

| Trader | Strategy | Notes |
|--------|----------|-------|
| **Sharky6999** | 99c strategy | "hardcore devs with sports books betting experience" |
| **Distinct-Baguette** | Similar to gabagool | Sports betting background |
| **SynthData** | Bittensor subnet | Different approach |

### Wallet Addresses

| Address | Notes |
|---------|-------|
| `0xa103eee98ac104a676c202d7afe5e859881c255c` | Trading at 49c before events |
| `0xA8c11F072F26049FF54F83a8e75cc5831f9764dB` | Trading both BTC/ETH 15-min |
| `0x7f69983eb28245bba0d5083502a78744a8f66162` | High volume MM |

### Why Copying Gabagool Doesn't Work

1. **Exit Liquidity:** You're using market orders, they're using limits
2. **Latency:** By the time you see their trade, price has moved
3. **Infrastructure:** They have superior infra you can't match
4. **Volume:** Strategy requires high volume to work

---

## 5. Key Resources

### GitHub Repositories

| Repo | Purpose |
|------|---------|
| `github.com/piokopl/gabagool` | Educational gabagool implementation |
| `github.com/AleSZanello/poly-analysis` | 30 days of gabagool trade data |
| `github.com/ItsNash0/polymarket-api` | API wrapper |
| `github.com/ItsNash0/price-latency-test` | Latency testing tool |
| `github.com/Polymarket/py-clob-client` | Official Python client |

### Data Downloads
- `cdn.nash0.dev/gabagool.zip` - Gabagool markets in LLM format

### Polymarket Technical Details

**Finding Markets:**
```
slug = btc-updown-15m-{timestamp}
```

**Order Book Structure:**
> "Polymarket has one order book for both 'Yes' and 'No'. You can't ever buy Yes + No at the same time for <1$"
> "If you put a BUY limit order to buy DOWN, there will automatically be a SELL limit order for UP at the same price"

---

## 6. Critical Warnings

### Scam Awareness

> "Profitable bots are never for sale beware everyone"
> "If anyone is selling you profitable polymarket bots for 1k$ you are getting scammed"
> "Never git clone a bot. Never pay for a bot. You will either get scammed or drained"

### Strategy Warnings

> "99% of bot devs are losing money"
> "Been building a 15 min market bot and I hit a wall, high win rate but negative net where 1 trade ruins 5 wins"

### Infrastructure Reality

> "u probably cant compete with those bots earning $200k per week, those bots in maintenance have a minimum of $10k per month"
> "we are competing against MMs who are protected by polymarket and has better feeds, better infra, better latency"

---

## Summary: Key Takeaways

### For VPS Setup
1. **Best location:** Ireland (AWS eu-west-1) - ~70-80ms latency
2. **Avoid:** USA, UK, France (geo-blocked)
3. **Provider:** Njalla for privacy, AWS for reliability
4. **The 500ms taker speed bump** makes ultra-low latency less critical

### For Market Making
1. **Use limit orders (GTC)**, not market orders
2. **Monitor Binance** for price signals, but Chainlink is resolution source
3. **Cancel stale quotes within 5-20 seconds** if not filled
4. **Keep share counts balanced**, not dollar values
5. **Merge positions** for faster exit vs waiting for resolution

### For Strategy
1. **Core formula:** `cost_up + cost_down < $1`
2. **Build positions over time**, not single-shot trades
3. **Accept 15% loss rate**, wins must outweigh losses
4. **Paper trading doesn't work** - test with real money
5. **Last-second sniping is dead** - too competitive

### For Competing
1. **Small size (10 shares) with 5c+ margins** - patience strategy
2. **Large size requires infrastructure** - Ireland VPS minimum
3. **Don't copy trade MMs** - you become their exit liquidity
4. **Focus on sports markets** for less competition

---

*Research compiled: January 6, 2026*
*Source: Polymarket Lounge Telegram Export*
