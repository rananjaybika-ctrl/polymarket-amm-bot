# POLYMARKET API IMPROVEMENT ANALYSIS

**Date:** January 21, 2026
**Source:** https://docs.polymarket.com/quickstart/overview
**Status:** Research only - no changes made

---

## Summary of Research

Thorough analysis of codebase (~11 strategies, comprehensive API integration) against official Polymarket documentation.

---

## 1. QUICK WINS (Low Effort, High Value)

### A. Update py-clob-client
```
Current: 0.34.4
Latest:  0.34.5
```
Minor update available - may contain bug fixes.

### B. Missing WebSocket Message Types
**Your code handles:**
- `book` (orderbook updates)
- `price_change` (bid/ask changes)
- `last_trade_price` (trades)
- `market_resolved` (resolution)

**Available but NOT used:**
- `tick_size_change` - Triggered when tick size adjusts at price extremes (>0.96 or <0.04)
- `best_bid_ask` - Direct best bid/ask updates (feature-flagged)
- `new_market` - Announces newly created markets (feature-flagged)

### C. Post-Only Orders
Docs mention `postOnly` parameter that **prevents immediate matching** against resting liquidity - rejects if order would cross spread.
- Could prevent accidental taker fills on maker strategies
- Code doesn't appear to use this flag

---

## 2. DATA API (Not Currently Used)

Code fetches positions via CLOB API. The **Data API** (`data-api.polymarket.com`) offers:

| Endpoint | Use Case |
|----------|----------|
| `/positions` | Filter positions by user with more params |
| `/trades` | Trade history with filtering |
| `/activity` | On-chain activity for a user |
| `/holders` | Top holders for a market (competitor analysis) |
| `/value` | Total portfolio value calculation |
| `/closed-positions` | Historical closed positions |
| `/leaderboard` | Trader rankings by category/time |
| `/open-interest` | Market-wide engagement metrics |
| `/live-volume` | Event-specific volume |

**Potential uses:**
- `/holders` - See who else is trading your markets
- `/leaderboard` - Track your ranking
- `/open-interest` - Gauge market activity before entering

---

## 3. RTDS (Real Time Data Stream)

Docs mention **RTDS** as a separate low-latency data feed:
- `RTDS Crypto Prices` - Real-time crypto prices (could replace/supplement Binance feed?)
- `RTDS Comments` - Live comments

**Current approach:** Direct Binance WebSocket for BTC prices
**Alternative:** RTDS might offer Polymarket-optimized crypto feeds

---

## 4. PRICING ENDPOINTS (Partially Used)

**Available endpoints:**
| Endpoint | Description | Usage |
|----------|-------------|-------|
| `GET /price` | Single token price | Used |
| `GET /prices` | Multiple token prices | Used |
| `GET /midpoint` | Midpoint price | Not sure |
| `GET /prices-history` | Historical price data | Not used |

**`/prices-history` could help:**
- Backtest strategies with historical data
- Analyze price patterns pre-trade

---

## 5. CONDITIONAL TOKEN FRAMEWORK (CTF)

Docs describe **split/merge/redeem** operations for outcome tokens:

| Operation | Description | Usage |
|-----------|-------------|-------|
| Split USDC → tokens | Convert collateral to outcome tokens | Not used |
| Merge tokens → USDC | Combine tokens back to collateral | Used (via merge arbitrage) |
| Redeem tokens | Claim winnings after resolution | Used (auto_redeemer) |

**Split could be useful for:**
- Pre-positioning in markets before trading opens
- Inventory management for market making
- Lower slippage by already having tokens

---

## 6. BUILDER PROGRAM FEATURES

Code uses standard API. Builder Program offers:

| Feature | Description | Benefit |
|---------|-------------|---------|
| Gasless transactions | Polygon relayer for users | Not relevant (you're the trader) |
| Order attribution | Track orders by builder | Analytics |
| Tiered rate limits | Scale with volume | Higher throughput |
| Revenue opportunities | Earn from referred volume | Monetization |

**Verdict:** Not directly useful unless building a platform for others.

---

## 7. ORDER SCORING & REWARDS

Docs mention:
- **Liquidity Rewards Program** - Earn rewards for providing liquidity
- **Maker Rebates Program** - Rebates for maker orders
- **Check Order Reward Scoring** endpoint - Verify if order qualifies

**Current code:**
- Estimates maker rebate at ~1%
- No endpoint call to verify scoring eligibility

**Potential improvement:**
- Call scoring endpoint before placing large orders
- Optimize order placement for reward eligibility

---

## 8. BATCH ORDER IMPROVEMENTS

**Docs specify:**
- Max **15 orders per batch**
- Per-order error handling (partial success possible)
- `postOnly` option available per order

**Current code:**
- Uses `place_orders()` for pair trades (2 orders)
- Could batch more orders if needed

---

## 9. TICK SIZE HANDLING

**Docs mention:**
- Dynamic tick sizes based on price
- `tick_size_change` WebSocket event when it changes
- Price extremes (>0.96 or <0.04) trigger tick size adjustments

**Current code:**
- Has `TICK_SIZES` constant in `polymarket_client.py`
- Doesn't subscribe to `tick_size_change` events

**Risk:** If tick size changes mid-trade, orders could be rejected.

---

## 10. @polymarket/clob-client (TypeScript)

**Not installed.** This is the TypeScript/JavaScript client.

**Stack is Python** - uses `py-clob-client` which is the correct choice.

**Only install if:**
- Building a Node.js frontend/dashboard
- Need features only in TS client

---

## 11. FEATURE FLAGS TO REQUEST

Some WebSocket features require `custom_feature_enabled`:
- `best_bid_ask` messages
- `new_market` messages
- `market_resolved` messages (already have this)

**Action:** Contact mm@polymarket.com to request feature flags if needed.

---

## PRIORITY RECOMMENDATIONS

| Priority | Item | Effort | Impact |
|----------|------|--------|--------|
| HIGH | Subscribe to `tick_size_change` events | Low | Prevent order rejections |
| HIGH | Use `postOnly` for maker strategies | Low | Prevent accidental taker fills |
| MED | Add `/prices-history` for analysis | Medium | Better backtesting |
| MED | Update py-clob-client to 0.34.5 | Low | Bug fixes |
| MED | Explore Data API `/holders` endpoint | Medium | Competitor intelligence |
| LOW | Check order reward scoring | Medium | Optimize rewards |
| LOW | Investigate RTDS crypto prices | Medium | Alternative price feed |
| LOW | Implement CTF split operations | High | Advanced inventory mgmt |

---

## FILES REFERENCE

| Component | Path |
|-----------|------|
| Polymarket Client | `src/api/polymarket_client.py` |
| WebSocket Clients | `src/api/websocket_client.py` |
| Config | `src/config.py` |
| Requirements | `requirements.txt` |

---

## DOCUMENTATION LINKS

- Overview: https://docs.polymarket.com/quickstart/overview
- CLOB API: https://docs.polymarket.com/api/clob
- Data API: https://docs.polymarket.com/api/data
- WebSocket: https://docs.polymarket.com/api/websocket
- Market Makers: https://docs.polymarket.com/developers/market-makers
- CTF: https://docs.polymarket.com/developers/CTF/overview
- RTDS: https://docs.polymarket.com/developers/rtds
