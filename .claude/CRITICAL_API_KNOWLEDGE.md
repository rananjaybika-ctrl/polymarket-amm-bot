# CRITICAL: Polymarket API Knowledge

## BTC 15-Minute Markets - API Routing

**NEVER USE CLOB API FOR btc-updown-15m MARKETS**

| API | Endpoint | Has btc-updown-15m? | Use For |
|-----|----------|---------------------|---------|
| **Gamma** | `gamma-api.polymarket.com/events?slug=X` | YES | BTC 15-min markets |
| **CLOB** | `clob.polymarket.com/markets` | NO | General markets only |

### Why This Matters

The CLOB `/markets` endpoint returns 1000+ paginated markets but **ZERO btc-updown-15m markets**.
Only Gamma API supports direct slug lookup for these time-based markets.

### The Bug (Jan 2026)

```python
# BROKEN - Tries CLOB first, fails, circuit breaker interferes
market = await self._get_market_from_clob(slug)  # Returns None for btc-updown
market = await self._get_market_from_gamma(slug)  # Fallback may not trigger

# FIXED - Route btc-updown directly to Gamma
if "btc-updown-15m" in slug:
    market = await self._get_market_from_gamma(slug)  # Direct, works
```

### Verification

```bash
# CLOB has NO btc-updown markets:
curl -s "https://clob.polymarket.com/markets" | grep -c "btc-updown"
# Returns: 0

# Gamma has them via slug query:
curl -s "https://gamma-api.polymarket.com/events?slug=btc-updown-15m-1768286700"
# Returns: Full market data
```

### File Reference
- `src/services/market_finder.py:get_market_by_slug()` - Fixed to route btc-updown to Gamma
- `scripts/spread_capture_observer.py` - Uses `get_current_and_upcoming_markets()`

---

## Garbage Detection - What's VALID vs INVALID

| Price Pattern | Status | Reason |
|--------------|--------|--------|
| UP=$0.97, DOWN=$0.03 | **VALID** | Near market end, one side winning |
| UP=$0.01, DOWN=$0.99 | **VALID** | Near market end, other side winning |
| UP=$0.49/$0.51, DOWN=$0.49/$0.51 | **VALID** | Early market, prices haven't moved yet |
| UP=0, DOWN=0 | **GARBAGE** | Empty orderbook, no bids/asks |

**Key insight:**
- Extreme prices ($0.01 or $0.99) are VALID near market end
- Static $0.49/$0.51 is VALID early in market
- Only flag ZERO prices as garbage (empty orderbook)
- To detect truly stale data, track price movement over time

---
**Date:** 2026-01-13
**Tags:** #api #critical #polymarket #market-finder
