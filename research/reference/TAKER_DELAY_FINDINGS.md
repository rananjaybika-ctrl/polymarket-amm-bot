# Polymarket 500ms Taker Delay - Research Findings

**Date:** Jan 31, 2026
**Status:** Confirmed via Telegram + Docs

---

## Key Findings

### 1. Where is the Delay?

The delay is on the **CLOB (matching engine level)**, NOT the website UI.

From Polymarket docs and Telegram sources:
- "Orders are matched offchain before (Poly engine)" - matching happens at the operator level
- "500ms delay for taker orders" - specifically affects orders that cross the spread
- "my round trip time for FAK order went up from 100ms to 750ms after the new speed bump rule"

The delay is applied BEFORE matching occurs, not after. When you submit a marketable order (one that would immediately match), Polymarket holds it for 500ms before matching.

### 2. Maker vs Taker Mechanics

| Order Type | Crosses Spread? | Role | 500ms Delay? |
|------------|-----------------|------|--------------|
| Bid at/below best bid | No | **MAKER** | No |
| Bid above best bid but below best ask | No | **MAKER** (new best bid) | No |
| Bid at/above best ask | Yes | **TAKER** | **YES** |
| postOnly order that would cross | Rejected | N/A | N/A |
| FOK/FAK orders | Yes (by design) | **TAKER** | **YES** |

**Critical insight:** If you post a bid 2-5 cents above best bid:
- If `your_bid < best_ask`: You become the new BEST BID (MAKER) - no delay
- If `your_bid >= best_ask`: You CROSS the spread (TAKER) - 500ms delay

### 3. Timeline When You Submit an Order

```
t=0ms:     Submit order to Polymarket CLOB
t=5-50ms:  Order reaches matching engine (network latency)

IF ORDER WOULD MATCH RESTING LIQUIDITY (crosses spread):
  t=50ms:    Engine holds order for taker delay
  t=550ms:   Engine releases and matches order
  t=600ms+:  Fill confirmation returned

IF ORDER RESTS ON BOOK (doesn't cross spread):
  t=50ms:    Order immediately placed on book
  t=55ms:    Order live, waiting for taker
```

**Key insight:** The delay is 500ms FROM when the matching engine receives your order, not from when you submit it. Total round-trip for taker orders is ~600-750ms.

---

## Impact on Our Strategy

### Current Flow (TAKER Entry)

1. Detect BTC spike via 60Hz Binance feed
2. Calculate winner side (UP/DOWN based on velocity)
3. Submit aggressive buy order (FAK/GTC crossing spread)
4. **500ms delay applied**
5. Order fills (if still available) at stale price

**Problem:** During 500ms delay, orderbook has moved. Our edge from detecting spike is reduced.

### Evidence from Telegram

> "That's why 500ms delay was introduced some time ago in order to stop taking from market. But some guys still predict it better as Gabagool"

> "I was making money on this till they introduced the 500ms taker speed bump. then the PNL dipped"

> "my round trip time for FAK order went up from 100ms to 750ms after the new speed bump rule"

> "But with the 500ms speed bump it's not a game changer" (if signal+latency < 200ms)

### The Math

- Our spike detection: ~100-200ms after BTC move (Binance WS latency)
- Order submission: ~50-100ms to reach Poly
- Taker delay: 500ms
- **Total: 650-800ms from BTC move to fill**

During this time, sophisticated makers (Gabagool with <10ms latency) have already:
1. Updated their quotes
2. Pulled stale orders
3. Posted new prices

---

## FOK/FAK/GTC Order Types - Why FOK/FAK Are Problematic

### FOK Issues
- "getting a lot of failed orders in FOK order type too"
- "FOK will probably fail many times"
- "PolyApiException: order couldn't be fully filled. FOK orders are fully filled or killed"
- High failure rate in fast markets, requires retry loops

### FAK Issues
- "my round trip time for FAK order went up from 100ms to 750ms after the new speed bump rule"
- **CRITICAL BUG:** "for FAK order there is no event raised on the websocket" (no fill tracking!)
- "FAK leaves uneven and maybe below 5 shares to hedge leaving the hedging order unavailable"
- "A lot of missed entries(cancelled orders) because my bot uses FAK"
- Partial fills create unbalanced legs

### Key Community Insight

> "Speed only matters if you FOK or FAK, if you GTC, you are good to go"
> "When he buy 12, 16, 18 shares very probably he is doing limit order" (Gabagool uses GTC!)

**Conclusion:** GTC maker orders avoid the 500ms delay and have no WebSocket tracking issues.

---

## Mitigation Options

### Option 1: Accept Delay (Current Strategy)
- Works if signal is predictive >500ms ahead
- Pros: Guaranteed fill at desired price
- Cons: Market moves 500ms during delay, may get worse price

### Option 2: Aggressive Maker
- Post bid at `best_bid + spike_magnitude` (if < best_ask, you're maker, no delay)
- Pros: No delay, immediate placement
- Cons: May not fill if market moves away

### Option 3: Hybrid/Dynamic
- Use maker orders when spread is tight
- Use taker orders when spread is wide (delay cost < spread benefit)
- Dynamic based on market conditions

### Option 4: Focus on Hedge (Already Maker)
- Our hedge leg is already maker (post bid, wait for fill)
- The delay primarily affects entry, not hedge
- Consider: Tighter entry criteria, better signal quality

---

## Strategic Options for Entry

### Current: Always TAKER
```python
entry_price = winner_ask  # Buy at ask, instant fill
# Role: TAKER (500ms delay in live)
```

### New Idea: Magnitude-Based MAKER Entry
```python
entry_price = winner_bid + f(spike_magnitude)

# If entry_price < winner_ask: MAKER order, wait for fill (no delay)
# If entry_price >= winner_ask: effectively TAKER, instant fill (delay)
```

This is what we're testing in `entry_spike_magnitude_test.py`:
- **RAW_MAGNITUDE:** `entry_price = winner_bid + spike_magnitude`
- **OU_NORMALIZED:** `entry_price = winner_bid + spike_magnitude * (1 + z_score)`
- **THRESHOLD_RELATIVE:** `entry_price = winner_bid + spike_magnitude / threshold`

---

## Simulation vs Live Implementation

**In backtest we simulate:**
- **TAKER:** instant fill at winner_ask (current behavior)
- **MAKER:** fill when winner_ask <= our_bid (new magnitude-based entry)

**In live production:**
- Use GTC orders for maker entries (no 500ms delay)
- No WebSocket tracking issues
- No unbalanced legs from partial fills

---

## Sources

- [Polymarket CLOB Docs](https://docs.polymarket.com/developers/CLOB)
- [Place Single Order](https://docs.polymarket.com/developers/CLOB/orders/create-order)
- [Finance Magnates - Dynamic Fees](https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/)
- Telegram Chat: Polymarket Developers (ChatExport_2026-01-11)
  - Dec 7, 2025: "500ms delay for taker orders"
  - Dec 7, 2025: "I was making money on this till they introduced the 500ms taker speed bump"
  - Dec 7, 2025: "FAK order went up from 100ms to 750ms after the new speed bump rule"
